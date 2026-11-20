"""Optional cloud TLS CONNECT exit; upstream credentials remain end-to-end encrypted."""
from __future__ import annotations

import base64
import hmac
import ipaddress
import os
from pathlib import Path
import selectors
import socket
import socketserver
import ssl
import threading
import time
from urllib.parse import urlsplit

from .contracts import RemoteError

MAX_BYTES = 128 * 1024 * 1024
MAX_SECONDS = 600


def proxy_url(env=None):
    env = os.environ if env is None else env
    value = env.get('PIXIV_REMOTE_EGRESS_PROXY', '')
    if value == '':
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or parsed.username != 'worker'
                or not parsed.password or len(parsed.password) < 32 or parsed.port is None
                or any(c.isspace() for c in value)):
            raise ValueError
    except (ValueError, TypeError):
        raise RemoteError('cloud_egress_not_configured', 503) from None
    return value.rstrip('/')


def _public_ip(value):
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast or address.is_unspecified:
        raise ValueError('nonpublic destination')
    return str(address)


def resolve_destination(authority, allowlist, *, resolver=socket.getaddrinfo):
    """Only HTTPS port 443 to explicitly configured public hosts or pinned IPs."""
    parsed = urlsplit('//' + authority)
    if not parsed.hostname or parsed.port != 443 or parsed.username or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('invalid CONNECT target')
    host = parsed.hostname.lower().rstrip('.')
    allowed_ips = set()
    for allowed in allowlist:
        try:
            allowed_ips.add(_public_ip(allowed))
        except ValueError:
            for row in resolver(allowed, 443, type=socket.SOCK_STREAM):
                allowed_ips.add(_public_ip(row[4][0]))
    try:
        ip = _public_ip(host)
    except ValueError:
        if host not in allowlist:
            raise ValueError('host not allowed') from None
        values = {_public_ip(row[4][0]) for row in resolver(host, 443, type=socket.SOCK_STREAM)}
        if not values:
            raise ValueError('no destination')
        ip = sorted(values)[0]
    if ip not in allowed_ips:
        raise ValueError('address not allowed')
    return ip, 443


def relay(client, upstream, *, deadline, max_bytes=MAX_BYTES):
    total = 0
    with selectors.DefaultSelector() as selector:
        selector.register(client, selectors.EVENT_READ, upstream)
        selector.register(upstream, selectors.EVENT_READ, client)
        while time.monotonic() < deadline:
            buffered = [selector.get_key(stream) for stream in (client,upstream)
                        if isinstance(stream,ssl.SSLSocket) and stream.pending()]
            events = [(key,None) for key in buffered] or selector.select(timeout=min(1, max(0, deadline - time.monotonic())))
            for key, _ in events:
                data = key.fileobj.recv(65536)
                if not data:
                    return
                total += len(data)
                if total > max_bytes:
                    raise ValueError('tunnel byte limit')
                key.data.sendall(data)


class EgressHandler(socketserver.BaseRequestHandler):
    def handle(self):
        if not self.server.slots.acquire(blocking=False):
            return
        try:
            self.request.settimeout(15)
            # No makefile buffering: bytes after CONNECT belong to upstream TLS.
            header = bytearray()
            while not header.endswith(b'\r\n\r\n') and len(header) <= 8192:
                byte = self.request.recv(1)
                if not byte:
                    return
                header.extend(byte)
            lines = bytes(header).decode('ascii').split('\r\n')
            method, target, version = lines[0].split(' ')
            if method != 'CONNECT' or version not in {'HTTP/1.0','HTTP/1.1'} or len(header) > 8192:
                raise ValueError
            headers = {}
            for line in lines[1:]:
                if not line:
                    continue
                name, value = line.split(':', 1)
                name = name.lower()
                if name in headers:
                    raise ValueError
                headers[name] = value.strip()
            expected = 'Basic ' + base64.b64encode(('worker:' + self.server.secret).encode()).decode()
            if not hmac.compare_digest(headers.get('proxy-authorization', ''), expected):
                self.request.sendall(b'HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n')
                return
            destination = resolve_destination(target, self.server.allowlist)
            with socket.create_connection(destination, timeout=15) as upstream:
                self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                self.request.settimeout(15)
                upstream.settimeout(15)
                relay(self.request, upstream, deadline=time.monotonic() + MAX_SECONDS)
        except (ValueError, UnicodeError, OSError):
            return  # Never log headers, target URLs or tunneled bytes.
        finally:
            self.server.slots.release()


class EgressServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, context, secret, allowlist):
        self.context, self.secret, self.allowlist = context, secret, allowlist
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(address, EgressHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        try:
            return self.context.wrap_socket(connection, server_side=True), address
        except BaseException:
            connection.close()
            raise


def serve(env=None):
    env = os.environ if env is None else env
    secret = env.get('PIXIV_REMOTE_EGRESS_KEY', '')
    allowed = frozenset(value.strip().lower() for value in env.get('PIXIV_REMOTE_EGRESS_HOSTS', '').split(',') if value.strip())
    if len(secret) < 32 or not allowed or any(c.isspace() for c in secret):
        raise RemoteError('egress_configuration_required', 400)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    certificate, key = Path(env.get('PIXIV_REMOTE_EGRESS_CERT', '')), Path(env.get('PIXIV_REMOTE_EGRESS_PRIVATE_KEY', ''))
    if not certificate.is_absolute() or not key.is_absolute() or not certificate.is_file() or not key.is_file():
        raise RemoteError('egress_certificate_required', 400)
    if os.name != 'nt' and key.stat().st_mode & 0o077:
        raise RemoteError('egress_key_permissions', 400)
    context.load_cert_chain(certificate, key)
    host = env.get('PIXIV_REMOTE_EGRESS_LISTEN', '127.0.0.1')
    port = int(env.get('PIXIV_REMOTE_EGRESS_PORT', '8443'))
    with EgressServer((host, port), context, secret, allowed) as server:
        server.serve_forever(poll_interval=.5)

"""TLS remains end-to-end while the cloud tunnel opens the upstream socket."""
from datetime import datetime,timedelta,timezone
import ipaddress
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from pixiv_novel_sync.remote.egress import EgressServer,proxy_url,resolve_destination
from pixiv_novel_sync.remote.contracts import RemoteError


@pytest.mark.parametrize('env', [{}, {'PIXIV_REMOTE_EGRESS_PROXY': ''}])
def test_cloud_proxy_is_optional(env):
    assert proxy_url(env) is None


@pytest.mark.parametrize('value',['http://worker:abc@public.example:8443','https://public.example:8443','https://worker:short@public.example:8443'])
def test_cloud_proxy_must_be_https_and_authenticated(value):
    with pytest.raises(RemoteError): proxy_url({'PIXIV_REMOTE_EGRESS_PROXY':value})


def test_exit_rejects_private_ips_non_https_ports_and_unlisted_hosts():
    def resolve(host,port,**kw): return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',port))]
    assert resolve_destination('93.184.216.34:443',{'provider.example'},resolver=resolve)==('93.184.216.34',443)
    for target in ('127.0.0.1:443','169.254.169.254:443','93.184.216.34:80','other.example:443'):
        with pytest.raises(ValueError): resolve_destination(target,{'provider.example'},resolver=resolve)


def test_real_tls_connect_keeps_api_header_outside_proxy_plaintext(tmp_path,monkeypatch):
    from pixiv_novel_sync.remote import egress
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
          .not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True,path_length=None),True)
          .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.DNSName('provider.example'),
                         x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),False).sign(key,hashes.SHA256()))
    certfile=tmp_path/'cert.pem';keyfile=tmp_path/'key.pem'
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()))
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(certfile,keyfile)
    received=[]
    opaque=[]
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(8)
    def origin():
        try: sock,_=listener.accept()
        except OSError: return
        with context.wrap_socket(sock,server_side=True) as stream:
            request=bytearray()
            while not request.endswith(b'\r\n\r\n'): request.extend(stream.recv(4096))
            received.append(bytes(request))
            stream.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK')
    origin_thread=threading.Thread(target=origin,daemon=True);origin_thread.start()
    monkeypatch.setattr(egress,'resolve_destination',lambda target,allowed:listener.getsockname())
    original=egress.relay
    def tracked(client,upstream,**kwargs):
        class Recording:
            def fileno(self): return upstream.fileno()
            def recv(self,n):
                data=upstream.recv(n);opaque.append(data);return data
            def sendall(self,data): opaque.append(data);return upstream.sendall(data)
        return original(client,Recording(),**kwargs)
    monkeypatch.setattr(egress,'relay',tracked)
    server=EgressServer(('127.0.0.1',0),context,'S'*32,{'provider.example'})
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        import requests
        with requests.Session() as session:
            session.trust_env=False
            response=session.get('https://provider.example/',headers={'Authorization':'Bearer API_SENTINEL_4938'},
                proxies={'https':f'https://worker:{"S"*32}@localhost:{server.server_address[1]}'},
                verify=str(certfile),timeout=8)
            assert response.text=='OK'
        origin_thread.join(timeout=2)
        assert b'API_SENTINEL_4938' in received[0]
        assert b'API_SENTINEL_4938' not in b''.join(opaque)
    finally:
        server.shutdown();server.server_close();listener.close();thread.join(timeout=2)

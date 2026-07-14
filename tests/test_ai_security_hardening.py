from __future__ import annotations

import io
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest
import requests

from pixiv_novel_sync.ai.models import AIProviderConfig
from pixiv_novel_sync.ai import providers as provider_module
from pixiv_novel_sync.ai.providers import OpenAICompatibleProvider, ProviderConfigError, validate_base_url
from pixiv_novel_sync.ai_web import _content_disposition
from pixiv_novel_sync.ai.crypto import AISecretManager


# ── H1: base_url SSRF / 密钥外泄防护 ──────────────────────────────


def test_validate_base_url_accepts_public_https():
    assert validate_base_url("https://api.openai.com/v1/", resolve=False) == "https://api.openai.com/v1"


def test_validate_base_url_rejects_non_http_scheme():
    with pytest.raises(ProviderConfigError):
        validate_base_url("ftp://example.com/v1", resolve=False)
    with pytest.raises(ProviderConfigError):
        validate_base_url("file:///etc/passwd", resolve=False)


def test_validate_base_url_rejects_empty():
    with pytest.raises(ProviderConfigError):
        validate_base_url("", resolve=False)
    with pytest.raises(ProviderConfigError):
        validate_base_url(None, resolve=False)


def test_validate_base_url_requires_https_for_non_loopback():
    with pytest.raises(ProviderConfigError):
        validate_base_url("http://api.openai.com/v1", resolve=False)


def test_validate_base_url_allows_http_for_loopback():
    # 本机回环允许 http（本地自建模型服务）
    assert validate_base_url("http://localhost:8080/v1", resolve=False).startswith("http://localhost")


def test_validate_base_url_blocks_metadata_ip_at_request_time():
    with pytest.raises(ProviderConfigError):
        validate_base_url("https://169.254.169.254/v1", resolve=True)


def test_validate_base_url_blocks_loopback_ip_at_request_time():
    with pytest.raises(ProviderConfigError):
        validate_base_url("http://127.0.0.1:1234/v1", resolve=True)


def test_validate_base_url_blocks_private_ip_at_request_time():
    with pytest.raises(ProviderConfigError):
        validate_base_url("https://192.168.1.10/v1", resolve=True)


def test_validate_base_url_blocks_shared_address_by_default():
    with pytest.raises(ProviderConfigError):
        validate_base_url("https://100.64.0.1/v1", resolve=True)


def test_validate_base_url_allows_public_ipv4_mapped_ipv6():
    assert validate_base_url("https://[::ffff:8.8.8.8]/v1", resolve=True) == "https://[::ffff:8.8.8.8]/v1"


def test_validate_base_url_allows_private_with_opt_in(monkeypatch):
    monkeypatch.setenv("PIXIV_AI_ALLOW_PRIVATE_HOSTS", "1")
    # 私有/回环放行，但链路本地与共享地址始终拒绝
    assert validate_base_url("http://127.0.0.1:1234/v1", resolve=True).startswith("http://127.0.0.1")
    with pytest.raises(ProviderConfigError):
        validate_base_url("https://169.254.169.254/v1", resolve=True)
    with pytest.raises(ProviderConfigError):
        validate_base_url("https://100.64.0.1/v1", resolve=True)


def test_pinned_adapter_uses_ip_for_pool_and_hostname_for_tls():
    adapter = provider_module._PinnedHostAdapter(hostname="api.example.com", ip="93.184.216.34")
    request = requests.Request(
        "POST",
        "https://api.example.com/v1/messages",
        headers={"Host": "api.example.com"},
    ).prepare()

    try:
        host_params, pool_kwargs = adapter.build_connection_pool_key_attributes(request, verify=True)
        pool = adapter.get_connection_with_tls_context(request, verify=True)
    finally:
        adapter.close()

    assert host_params["host"] == "93.184.216.34"
    assert pool_kwargs["assert_hostname"] == "api.example.com"
    assert pool_kwargs["server_hostname"] == "api.example.com"
    assert pool.host == "93.184.216.34"
    assert pool.assert_hostname == "api.example.com"
    assert pool.conn_kw["server_hostname"] == "api.example.com"
    assert request.headers["Host"] == "api.example.com"


def test_pinned_adapter_omits_tls_parameters_for_http():
    adapter = provider_module._PinnedHostAdapter(hostname="local.example", ip="127.0.0.1")
    request = requests.Request(
        "POST",
        "http://local.example:8123/v1/messages",
        headers={"Host": "local.example:8123"},
    ).prepare()

    try:
        host_params, pool_kwargs = adapter.build_connection_pool_key_attributes(request, verify=True)
    finally:
        adapter.close()

    assert host_params["host"] == "127.0.0.1"
    assert "assert_hostname" not in pool_kwargs
    assert "server_hostname" not in pool_kwargs


def _make_provider(*, proxy: str | None = None) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        AIProviderConfig(
            id=1,
            name="security-test",
            provider_type="openai_compatible",
            base_url="https://example.com/v1",
            api_key="key",
            default_model="model-a",
            timeout_seconds=2,
            max_retries=0,
            stream_enabled=False,
            proxy=proxy,
        )
    )


@contextmanager
def _serve(handler_type: type[BaseHTTPRequestHandler]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _ProviderHandler(BaseHTTPRequestHandler):
    seen_host: str | None = None
    seen_destination_ip: str | None = None

    def do_POST(self) -> None:
        type(self).seen_host = self.headers.get("Host")
        type(self).seen_destination_ip = self.connection.getsockname()[0]
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        return


class _ForwardProxyHandler(BaseHTTPRequestHandler):
    seen_target: str | None = None
    seen_host: str | None = None

    def do_POST(self) -> None:
        type(self).seen_target = self.path
        type(self).seen_host = self.headers.get("Host")
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        return


def test_post_connects_to_validated_ip_and_preserves_host(monkeypatch):
    real_getaddrinfo = socket.getaddrinfo
    dns_queries: list[str] = []

    def resolve_once(host, port, *args, **kwargs):
        if host == "rebind.test":
            dns_queries.append(host)
            if len(dns_queries) > 1:
                raise AssertionError("Provider 原域名发生了第二次 DNS 解析")
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setenv("PIXIV_AI_ALLOW_PRIVATE_HOSTS", "1")
    monkeypatch.setattr(provider_module.socket, "getaddrinfo", resolve_once)
    _ProviderHandler.seen_host = None
    _ProviderHandler.seen_destination_ip = None

    with _serve(_ProviderHandler) as server:
        port = server.server_address[1]
        provider = _make_provider()
        provider.session.trust_env = False
        try:
            response = provider._post(f"http://rebind.test:{port}/v1/messages", json={"test": True})
            response.close()
        finally:
            provider.close()

    assert dns_queries == ["rebind.test"]
    assert _ProviderHandler.seen_destination_ip == "127.0.0.1"
    assert _ProviderHandler.seen_host == f"rebind.test:{port}"


def test_forward_proxy_receives_pinned_ip_target_and_original_host(monkeypatch):
    real_getaddrinfo = socket.getaddrinfo

    def fixed_loopback(host, port, *args, **kwargs):
        if host == "rebind.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setenv("PIXIV_AI_ALLOW_PRIVATE_HOSTS", "1")
    monkeypatch.setattr(provider_module.socket, "getaddrinfo", fixed_loopback)
    _ForwardProxyHandler.seen_target = None
    _ForwardProxyHandler.seen_host = None

    with _serve(_ForwardProxyHandler) as proxy_server:
        proxy_url = f"http://127.0.0.1:{proxy_server.server_address[1]}"
        provider = _make_provider(proxy=proxy_url)
        try:
            response = provider._post(
                "http://rebind.test:8123/v1/messages",
                json={"test": True},
                proxies=provider._proxies(),
            )
            response.close()
        finally:
            provider.close()

    target = urlparse(_ForwardProxyHandler.seen_target or "")
    assert target.hostname == "127.0.0.1"
    assert target.port == 8123
    assert "rebind.test" not in (_ForwardProxyHandler.seen_target or "")
    assert _ForwardProxyHandler.seen_host == "rebind.test:8123"


def test_post_reuses_origin_adapter_without_closing_active_pool(monkeypatch):
    def fixed_public(host, port, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port))]

    closed_adapters: list[provider_module._PinnedHostAdapter] = []
    original_close = provider_module._PinnedHostAdapter.close

    def track_close(adapter):
        closed_adapters.append(adapter)
        original_close(adapter)

    sent_urls: list[str] = []

    def fake_post(url, **_kwargs):
        sent_urls.append(url)
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO()
        return response

    monkeypatch.setattr(provider_module.socket, "getaddrinfo", fixed_public)
    monkeypatch.setattr(provider_module._PinnedHostAdapter, "close", track_close)
    provider = _make_provider()
    monkeypatch.setattr(provider.session, "post", fake_post)

    try:
        first_response = provider._post("https://pool.test/v1/messages", json={"request": 1})
        first_adapter = provider.session.get_adapter(sent_urls[0])
        second_response = provider._post("https://pool.test/v1/messages", json={"request": 2})
        second_adapter = provider.session.get_adapter(sent_urls[1])

        assert first_adapter is second_adapter
        assert first_adapter not in closed_adapters
        assert "https://8.8.8.8:443/" in provider.session.adapters
    finally:
        first_response.close()
        second_response.close()
        provider.close()


def test_post_uses_distinct_origin_adapters_for_changed_ip(monkeypatch):
    resolved_ips = iter(("8.8.8.8", "1.1.1.1"))

    def changing_public(host, port, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (next(resolved_ips), port))]

    sent_urls: list[str] = []

    def fake_post(url, **_kwargs):
        sent_urls.append(url)
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO()
        return response

    monkeypatch.setattr(provider_module.socket, "getaddrinfo", changing_public)
    provider = _make_provider()
    monkeypatch.setattr(provider.session, "post", fake_post)

    try:
        first_response = provider._post("https://pool.test/v1/messages", json={"request": 1})
        first_adapter = provider.session.get_adapter(sent_urls[0])
        second_response = provider._post("https://pool.test/v1/messages", json={"request": 2})
        second_adapter = provider.session.get_adapter(sent_urls[1])

        assert first_adapter is not second_adapter
        assert provider.session.get_adapter(sent_urls[0]) is first_adapter
        assert provider.session.get_adapter(sent_urls[1]) is second_adapter
        assert "https://8.8.8.8:443/" in provider.session.adapters
        assert "https://1.1.1.1:443/" in provider.session.adapters
    finally:
        first_response.close()
        second_response.close()
        provider.close()


# ── L5: Content-Disposition 头注入防护 ───────────────────────────


def test_content_disposition_strips_crlf_injection():
    header = _content_disposition("evil\r\nSet-Cookie: x=1.txt")
    assert "\r" not in header
    assert "\n" not in header


def test_content_disposition_preserves_unicode_via_rfc5987():
    header = _content_disposition("我的小说.txt")
    assert "filename*=UTF-8''" in header
    # ASCII 回退不含原始非 ASCII 字节
    assert "我" not in header.split("filename*")[0]


def test_content_disposition_strips_quote_escape():
    header = _content_disposition('a"b.txt')
    # 引号被移除，不能提前闭合 filename="..."
    assert header.count('"') == 2


# ── L4: 遗留 v1 密文识别（透明升级判据） ─────────────────────────


def test_is_legacy_ciphertext_detects_v1(monkeypatch):
    monkeypatch.setenv("PIXIV_NOVEL_SYNC_AI_SECRET_KEY", "test-secret-key")
    mgr = AISecretManager()
    v2 = mgr.encrypt("sk-abc123")
    assert mgr.is_legacy_ciphertext(v2) is False
    # 旧格式（无 v2$ 前缀）判为遗留
    assert mgr.is_legacy_ciphertext("gAAAAAB_legacy_token") is True
    assert mgr.is_legacy_ciphertext(None) is False


def test_v2_roundtrip(monkeypatch):
    monkeypatch.setenv("PIXIV_NOVEL_SYNC_AI_SECRET_KEY", "test-secret-key")
    mgr = AISecretManager()
    assert mgr.decrypt(mgr.encrypt("sk-secret-value")) == "sk-secret-value"

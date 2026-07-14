from __future__ import annotations

import pytest

from pixiv_novel_sync.ai.providers import ProviderConfigError, validate_base_url
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


def test_validate_base_url_allows_private_with_opt_in(monkeypatch):
    monkeypatch.setenv("PIXIV_AI_ALLOW_PRIVATE_HOSTS", "1")
    # 私有/回环放行，但 link-local 元数据地址始终拒绝
    assert validate_base_url("http://127.0.0.1:1234/v1", resolve=True).startswith("http://127.0.0.1")
    with pytest.raises(ProviderConfigError):
        validate_base_url("https://169.254.169.254/v1", resolve=True)


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

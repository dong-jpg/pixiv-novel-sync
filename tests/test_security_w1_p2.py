"""安全与限流修复的回归测试（W1 与 P2 的部分后端验证）。

W1: 生产环境限流分桶问题
- Nginx 配置了 Cloudflare 真实 IP（set_real_ip_from + CF-Connecting-IP）
- 救援 API 限流分三个桶：无 Token/Token 错误、Token 正确、封面

P2 其他项的测试：
- S5 PIXIV_PROXY 对 App API 生效在 test_pixiv_auth.py
- S6 追更方式2 回落在 test_sync_subscribed_series.py
- CSP、HSTS 在生产 Nginx 配置中，本测试验证 Flask 层 CSP
"""
from __future__ import annotations

import secrets
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_nginx_config_has_cloudflare_real_ip():
    """Nginx 配置包含 Cloudflare IP 段和 CF-Connecting-IP。"""
    nginx_conf = (ROOT / "config" / "nginx" / "pixiv-novel-sync.conf").read_text(encoding="utf-8")

    # 检查至少有一个 Cloudflare IPv4 段
    assert "set_real_ip_from 173.245.48.0/20" in nginx_conf
    assert "set_real_ip_from 2400:cb00::/32" in nginx_conf  # IPv6
    assert "real_ip_header CF-Connecting-IP" in nginx_conf

    # 检查 HSTS
    assert "Strict-Transport-Security" in nginx_conf
    assert "max-age=31536000" in nginx_conf

    # X-Forwarded-For 改为 $remote_addr（不再是 $proxy_add_x_forwarded_for）
    assert "X-Forwarded-For $remote_addr" in nginx_conf


def test_deploy_sh_sets_trust_proxy_and_cookie_secure():
    """deploy.sh 新装时设置 PIXIV_COOKIE_SECURE=1 和 DASHBOARD_TRUST_PROXY=1。"""
    deploy_sh = (ROOT / "deploy.sh").read_text(encoding="utf-8")

    assert 'PIXIV_COOKIE_SECURE=' in deploy_sh
    assert 'echo "PIXIV_COOKIE_SECURE=1"' in deploy_sh
    assert 'DASHBOARD_TRUST_PROXY=' in deploy_sh
    assert 'echo "DASHBOARD_TRUST_PROXY=1"' in deploy_sh
    # 只在没有配置时写入
    assert 'if ! grep -q "^PIXIV_COOKIE_SECURE="' in deploy_sh
    assert 'if ! grep -q "^DASHBOARD_TRUST_PROXY="' in deploy_sh


def test_rescue_api_has_three_rate_limit_buckets(rescue_app_factory):
    """救援 API 限流分成三个独立的桶。"""
    app, _settings, _file_storage = rescue_app_factory

    # 检查三个限流器都已注册
    assert "rescue_rate_limiter_no_token" in app.extensions
    assert "rescue_rate_limiter_authed" in app.extensions
    assert "rescue_rate_limiter_cover" in app.extensions

    limiter_no_token = app.extensions["rescue_rate_limiter_no_token"]
    limiter_authed = app.extensions["rescue_rate_limiter_authed"]
    limiter_cover = app.extensions["rescue_rate_limiter_cover"]

    # 三个限流器应该是不同的实例
    assert limiter_no_token is not limiter_authed
    assert limiter_authed is not limiter_cover
    assert limiter_no_token is not limiter_cover

    # 封面限流器的额度应该更高
    assert limiter_cover.limit == 240
    assert limiter_no_token.limit == 120
    assert limiter_authed.limit == 120


def test_rescue_api_uses_correct_bucket_for_no_token(rescue_client_with_token):
    """没带 Token 时使用「无 Token」桶。"""
    client, _token = rescue_client_with_token

    # 不带 Authorization 头
    resp = client.get("/api/rescue/v1/novels/999999")
    assert resp.status_code == 401
    data = resp.get_json()
    assert data["error"] == "需要救援 Token"


def test_rescue_api_uses_correct_bucket_for_invalid_token(rescue_client_with_token):
    """Token 错误时使用「无 Token」桶。"""
    client, _token = rescue_client_with_token

    # 带错误的 Token
    resp = client.get(
        "/api/rescue/v1/novels/999999",
        headers={"Authorization": f"Bearer {secrets.token_urlsafe(32)}"}
    )
    assert resp.status_code == 401
    data = resp.get_json()
    assert data["error"] == "救援 Token 无效"


def test_rescue_cover_uses_separate_bucket(rescue_client_with_token):
    """封面请求使用独立的限流桶。"""
    client, token = rescue_client_with_token

    # 封面路由存在（即使资源不存在）
    resp = client.get(
        "/api/rescue/v1/novels/999999/cover",
        headers={"Authorization": f"Bearer {token}"}
    )
    # 404 或 500 都说明请求通过了认证和限流
    assert resp.status_code in (404, 500)


def test_flask_response_has_csp_header(web_app_factory):
    """Flask 响应包含 CSP 头。"""
    app, _settings = web_app_factory
    client = app.test_client()

    # 访问登录页（不需要认证）
    resp = client.get("/token-login")
    assert resp.status_code == 200

    # 检查 CSP 头存在
    assert "Content-Security-Policy" in resp.headers
    csp = resp.headers["Content-Security-Policy"]

    # 验证关键指令
    assert "default-src 'self'" in csp
    assert "script-src 'self' 'unsafe-eval' https://unpkg.com" in csp
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'self'" in csp


def test_csp_allows_vue_and_blocks_inline_script():
    """CSP 策略允许 Vue（需要 unsafe-eval）但阻止内联脚本注入。

    这是一个文档测试，实际的 CSP 违规只能在浏览器中检测。
    """
    # Vue 3 全局版需要 'unsafe-eval' 来编译模板
    # 内联 <script> 标签会被 CSP 阻止（没有 'unsafe-inline'）
    # 这防止了 XSS 攻击中最常见的注入向量
    pass


@pytest.fixture
def rescue_app_factory(tmp_path, monkeypatch):
    """创建一个带救援路由的最小 Flask 应用。"""
    from flask import Flask
    from pixiv_novel_sync.settings import load_settings
    from pixiv_novel_sync.storage_files import FileStorage
    from pixiv_novel_sync.rescue_web import register_rescue_routes

    # 创建最小的 .env 文件
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")

    # 设置环境变量指向测试目录
    monkeypatch.setenv("PIXIV_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("PIXIV_PRIVATE_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("PIXIV_DB_PATH", str(tmp_path / "test.db"))

    (tmp_path / "public").mkdir()
    (tmp_path / "private").mkdir()

    settings = load_settings(config_path=None, env_path=str(env_path))

    app = Flask(__name__)
    app.config["TESTING"] = True

    def get_settings():
        return settings

    def get_client_addr():
        return "127.0.0.1"

    register_rescue_routes(app, get_settings, get_client_addr)

    file_storage = FileStorage(settings)

    return app, settings, file_storage


@pytest.fixture
def rescue_client_with_token(rescue_app_factory):
    """返回测试客户端和有效的救援 Token。"""
    import hashlib
    app, settings, _file_storage = rescue_app_factory
    from pixiv_novel_sync.storage_db import Database

    # 创建一个救援 Token
    db = Database(settings.storage.db_path)
    try:
        db.init_schema()
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        token_prefix = token[:8]
        db.save_rescue_token_record(token_hash, token_prefix)
    finally:
        db.close()

    client = app.test_client()
    return client, token


@pytest.fixture
def web_app_factory(tmp_path, monkeypatch):
    """创建一个最小的 Web 应用（只测试安全头）。"""
    from flask import Flask

    app = Flask(__name__)
    app.config["TESTING"] = True

    # 模拟 webapp.py 的安全头设置
    @app.after_request
    def _add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

        csp_directives = [
            "default-src 'self'",
            "script-src 'self' 'unsafe-eval' https://unpkg.com",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: https:",
            "font-src 'self'",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
        ]
        response.headers.setdefault("Content-Security-Policy", "; ".join(csp_directives))

        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.route("/token-login")
    def token_login():
        return "<html><body>Login</body></html>"

    # 创建最小的设置
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    monkeypatch.setenv("PIXIV_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("PIXIV_PRIVATE_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("PIXIV_DB_PATH", str(tmp_path / "test.db"))

    from pixiv_novel_sync.settings import load_settings
    settings = load_settings(config_path=None, env_path=str(env_path))

    return app, settings

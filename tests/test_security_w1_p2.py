"""W1 部署配置、救援 API 分桶限流（U5 服务端部分）与 CSP 的回归测试。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from pixiv_novel_sync.webapp import create_app

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "pixiv_novel_sync" / "templates"
NGINX_CONF = ROOT / "config" / "nginx" / "pixiv-novel-sync.conf"


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.setenv("PIXIV_DB_PATH", str(tmp_path / "state" / "w1.db"))
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    application = create_app(env_path=str(env_path), start_scheduler=False)
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _csrf_headers(client) -> dict[str, str]:
    token = client.get("/api/csrf-token").get_json()["csrf_token"]
    return {"X-CSRF-Token": token}


def _rotate_token(client) -> str:
    response = client.post("/api/dashboard/rescue-token/rotate", headers=_csrf_headers(client))
    assert response.status_code == 200
    return str(response.get_json()["data"]["token"])


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- Nginx / deploy.sh


def test_nginx_takes_client_ip_from_cloudflare_only():
    conf = NGINX_CONF.read_text(encoding="utf-8")
    assert "real_ip_header CF-Connecting-IP;" in conf
    assert "set_real_ip_from 173.245.48.0/20;" in conf
    assert "set_real_ip_from 2400:cb00::/32;" in conf
    # 只信任 CF 段：不能出现 0.0.0.0/0 之类的通配
    assert not re.search(r"set_real_ip_from\s+(0\.0\.0\.0/0|::/0)", conf)
    # XFF 只交出 Nginx 认定的真实地址，客户端伪造的 XFF 前缀不再透传给 Flask
    assert "$proxy_add_x_forwarded_for" not in conf
    assert conf.count("proxy_set_header X-Forwarded-For $remote_addr;") == 3


def test_nginx_https_server_sends_hsts():
    conf = NGINX_CONF.read_text(encoding="utf-8")
    https_block = conf.split("listen 443", 1)[1]
    assert re.search(r'add_header Strict-Transport-Security "max-age=\d+[^"]*" always;', https_block)


def test_deploy_writes_secure_defaults_without_overwriting():
    deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    for key in ("PIXIV_COOKIE_SECURE", "DASHBOARD_TRUST_PROXY"):
        assert f'grep -q "^{key}=' in deploy
        assert f'printf \'\\n%s\\n\' "{key}=1" >> .env' in deploy


# ---------------------------------------------------------------- 救援 API 分桶


def test_unauthenticated_flood_does_not_block_valid_token(app, client):
    """W1/U5：陌生人刷满「未认证」桶后，持正确 Token 的请求照常通过。"""
    token = _rotate_token(client)
    app.extensions["rescue_rate_limiter_unauthenticated"].limit = 3
    for _ in range(3):
        assert client.get("/api/rescue/v1/novels/10").status_code == 401
    assert client.get("/api/rescue/v1/novels/10").status_code == 429
    assert client.get("/api/rescue/v1/novels/10", headers=_bearer("wrong")).status_code == 429
    assert client.get("/api/rescue/v1/novels/10", headers=_bearer(token)).status_code == 404


def test_wrong_tokens_only_spend_the_unauthenticated_bucket(app, client):
    token = _rotate_token(client)
    app.extensions["rescue_rate_limiter"].limit = 1
    for _ in range(5):
        assert client.get("/api/rescue/v1/novels/10", headers=_bearer("wrong")).status_code == 401
    assert client.get("/api/rescue/v1/novels/10", headers=_bearer(token)).status_code == 404


def test_cover_requests_do_not_spend_the_text_bucket(app, client):
    """U5：封面单独限流，一路滑到底拉上百张封面后正文请求仍然可用。"""
    token = _rotate_token(client)
    app.extensions["rescue_rate_limiter"].limit = 2
    for _ in range(10):
        assert client.get("/api/rescue/v1/novels/10/cover", headers=_bearer(token)).status_code == 404
    assert client.get("/api/rescue/v1/novels/10", headers=_bearer(token)).status_code == 404
    assert client.get("/api/rescue/v1/novels/10", headers=_bearer(token)).status_code == 404
    assert client.get("/api/rescue/v1/novels/10", headers=_bearer(token)).status_code == 429


def test_cover_bucket_limits_covers(app, client):
    token = _rotate_token(client)
    app.extensions["rescue_rate_limiter_cover"].limit = 2
    for _ in range(2):
        assert client.get("/api/rescue/v1/series/20/cover", headers=_bearer(token)).status_code == 404
    assert client.get("/api/rescue/v1/series/20/cover", headers=_bearer(token)).status_code == 429
    assert client.get("/api/rescue/v1/novels/10", headers=_bearer(token)).status_code == 404


# ---------------------------------------------------------------- CSP


def _script_tags(html: str) -> list[str]:
    return re.findall(r"<script\b[^>]*>", html)


def test_html_pages_send_nonce_csp_and_every_script_carries_it(client):
    response = client.get("/dashboard")
    assert response.status_code == 200
    csp = response.headers["Content-Security-Policy"]
    match = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp)
    assert match, csp
    nonce = match.group(1)
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    assert "'unsafe-eval'" in csp  # Vue 全局版在浏览器里编译模板
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    tags = _script_tags(response.get_data(as_text=True))
    assert tags, "页面应当有脚本"
    for tag in tags:
        assert f'nonce="{nonce}"' in tag, tag


def test_csp_nonce_changes_per_request(client):
    first = client.get("/dashboard").headers["Content-Security-Policy"]
    second = client.get("/dashboard").headers["Content-Security-Policy"]
    assert first != second


def test_json_responses_do_not_carry_csp(client):
    response = client.get("/api/csrf-token")
    assert response.mimetype == "application/json"
    assert "Content-Security-Policy" not in response.headers


def test_every_template_script_tag_has_the_nonce_placeholder():
    """新加的内联 <script> 忘了 nonce 会在生产被 CSP 拦掉，整页失效。"""
    missing = []
    for path in sorted(TEMPLATES.glob("*.html")):
        text = re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.S)
        for tag in re.findall(r"(?m)^\s*(<script\b[^>]*>)", text):
            if 'nonce="{[ csp_nonce() ]}"' not in tag:
                missing.append(f"{path.name}: {tag}")
    assert not missing, missing

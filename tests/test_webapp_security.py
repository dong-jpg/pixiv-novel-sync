from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pixiv_novel_sync
from pixiv_novel_sync.jobs.models import JobSource, JobStatus
from pixiv_novel_sync import webapp as webapp_module
from pixiv_novel_sync.webapp import _oauth_task_public_payload, create_app


def test_no_dashboard_token_allows_localhost(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    response = client.get("/api/health", environ_base={"REMOTE_ADDR": "127.0.0.1"})

    assert response.status_code == 200
    assert response.get_json()["version"] == pixiv_novel_sync.__version__


def _local_app(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    app.config["RAW_HTTP"] = True
    return app


def test_no_token_post_without_csrf_is_forbidden(tmp_path, monkeypatch):
    app = _local_app(tmp_path, monkeypatch)
    client = app.test_client()

    blocked = client.post(
        "/api/dashboard/sync/start",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    token = client.get("/api/csrf-token", environ_base={"REMOTE_ADDR": "127.0.0.1"}).get_json()["csrf_token"]
    allowed = client.post(
        "/api/dashboard/sync/start",
        headers={"X-CSRF-Token": token},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert blocked.status_code == 403
    assert blocked.get_json()["error"] == "csrf token invalid"
    assert allowed.status_code != 403


def test_no_token_rejects_non_loopback_host(tmp_path, monkeypatch):
    app = _local_app(tmp_path, monkeypatch)
    client = app.test_client()

    blocked = client.get(
        "/api/health",
        headers={"Host": "evil.example"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    allowed = client.get(
        "/api/health",
        headers={"Host": "127.0.0.1:5010"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert blocked.status_code == 403
    assert blocked.get_json()["error"] == "host not allowed"
    assert allowed.status_code == 200
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    monkeypatch.setattr("pixiv_novel_sync.webapp.__version__", "sentinel-package-version")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    response = client.get("/api/health", environ_base={"REMOTE_ADDR": "127.0.0.1"})

    assert response.status_code == 200
    assert response.get_json()["version"] == "sentinel-package-version"


def test_no_dashboard_token_blocks_non_localhost(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    response = client.get("/dashboard", environ_base={"REMOTE_ADDR": "203.0.113.10"})

    assert response.status_code == 403


def test_configured_token_missing_session_returns_401_for_ai_api(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret-token")
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PIXIV_REFRESH_TOKEN=test\nDASHBOARD_TOKEN=secret-token\n",
        encoding="utf-8",
    )
    app = create_app(env_path=str(env_path), start_scheduler=False)

    response = app.test_client().post(
        "/api/dashboard/ai/agents",
        json={},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 401


def test_flask_secret_fallback_persists_to_env(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")

    first_app = create_app(env_path=str(env_path), start_scheduler=False)
    first_secret = first_app.secret_key
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    second_app = create_app(env_path=str(env_path), start_scheduler=False)

    content = env_path.read_text(encoding="utf-8")
    assert first_secret
    assert second_app.secret_key == first_secret
    assert f"PIXIV_FLASK_SECRET={first_secret}" in content



def test_dashboard_sync_start_submits_web_jobspec(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")

    ran = []

    def fake_run(self, job_id):
        ran.append(job_id)
        state = self.manager.get_job(job_id)
        self.manager.mark_running(job_id, "running")
        self.manager.mark_succeeded(job_id, "succeeded")
        return state

    monkeypatch.setattr("pixiv_novel_sync.webapp.JobRunner.run", fake_run)
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    response = client.post("/api/dashboard/sync/start")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["job"]["source"] == JobSource.WEB.value
    assert payload["job"]["task_list"] == ["bookmark", "following_users", "following_novels", "subscribed_series"]
    assert ran == [payload["job"]["job_id"]]



def test_dashboard_sync_status_reads_shared_web_job(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")

    def fake_run(self, job_id):
        self.manager.mark_running(job_id, "running")
        self.manager.update_progress(job_id, phase="同步收藏", current_task_index=0)
        self.manager.mark_succeeded(job_id, "succeeded")
        return self.manager.get_job(job_id)

    monkeypatch.setattr("pixiv_novel_sync.webapp.JobRunner.run", fake_run)
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    started = client.post("/api/dashboard/sync/start").get_json()
    job_id = started["job"]["job_id"]
    response = client.get(f"/api/dashboard/sync/status?job_id={job_id}")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["job"]["job_id"] == job_id
    assert payload["job"]["source"] == "web"
    assert payload["job"]["status"] == JobStatus.SUCCEEDED.value
    assert payload["job"]["progress"]["phase"] == "同步收藏"



def test_oauth_task_public_payload_redacts_tokens():
    task = SimpleNamespace(
        task_id="task-1",
        status="done",
        message="ok",
        refresh_token="secret-refresh",
        access_token="secret-access",
        user_id=123,
    )

    payload = _oauth_task_public_payload(task, mode="oauth")

    assert payload["task_id"] == "task-1"
    assert payload["has_refresh_token"] is True
    assert payload["has_access_token"] is True
    assert "refresh_token" not in payload
    assert "access_token" not in payload


def test_oauth_exchange_response_redacts_tokens(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    start_response = client.post("/api/token-jobs")
    start_payload = start_response.get_json()
    task_id = start_payload["task_id"]
    login_query = parse_qs(urlparse(start_payload["login_url"]).query)
    state = login_query["state"][0]

    def fake_exchange_code(self, task, code):
        task.status = "done"
        task.message = "Pixiv token 获取成功"
        task.refresh_token = "secret-refresh"
        task.access_token = "secret-access"
        task.user_id = 123
        return task

    monkeypatch.setattr("pixiv_novel_sync.oauth_helper.OAuthManager.exchange_code", fake_exchange_code)

    response = client.post(
        f"/oauth/exchange/{task_id}",
        json={"callback_url": f"https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?code=code-1&state={state}"},
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["has_refresh_token"] is True
    assert payload["has_access_token"] is True
    assert "refresh_token" not in payload
    assert "access_token" not in payload


def test_save_token_rejects_injection_and_bad_user_id(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    monkeypatch.delenv("PIXIV_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_USER_ID", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=old-token-value\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    injected = client.post(
        "/api/save-token",
        json={"refresh_token": "abcdefghij\nEVIL=1", "user_id": 1},
    )
    assert injected.status_code == 400
    assert "EVIL=" not in env_path.read_text(encoding="utf-8")

    short = client.post("/api/save-token", json={"refresh_token": "short"})
    assert short.status_code == 400

    bad_user = client.post(
        "/api/save-token",
        json={"refresh_token": "abcdefghijklmnop", "user_id": "nope"},
    )
    assert bad_user.status_code == 400
    rejected = env_path.read_text(encoding="utf-8")
    assert "PIXIV_REFRESH_TOKEN=old-token-value\n" in rejected
    assert "PIXIV_USER_ID=" not in rejected
    assert "EVIL=" not in rejected

    ok = client.post(
        "/api/save-token",
        json={"refresh_token": "abcdefghijklmnop", "user_id": "42"},
    )
    assert ok.status_code == 200
    saved = env_path.read_text(encoding="utf-8")
    assert "PIXIV_REFRESH_TOKEN=abcdefghijklmnop\n" in saved
    assert "PIXIV_USER_ID=42\n" in saved


def test_token_login_template_never_consumes_plaintext_token():
    """登录页不得消费后端响应里的明文 refresh_token。

    后端 `_oauth_task_public_payload` 只回 has_refresh_token，所以三条 OAuth 路径都必须
    按 has_refresh_token 判定成败，落盘走服务端自己持有 token 的 `/oauth/save/<task_id>`，
    而不是把明文回传 `/api/save-token`。该接口只保留给「直接填入凭证」表单（用户手上
    本来就有明文 token），不得出现在 OAuth 兑换路径上。
    """
    html = Path("src/pixiv_novel_sync/templates/token_login.html").read_text(encoding="utf-8")

    assert "data.refresh_token" not in html
    assert "/oauth/save/" in html

    save_token_lines = [line for line in html.splitlines() if "/api/save-token" in line]
    assert len(save_token_lines) == 1
    assert "data." not in save_token_lines[0]


def test_no_token_blocks_proxied_request_when_proxy_untrusted(tmp_path, monkeypatch):
    """反代后 remote_addr 恒为 127.0.0.1；存在代理头但未信任代理时必须拒绝，
    否则未配 token 的部署会把私密收藏暴露给全公网。"""
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    monkeypatch.delenv("DASHBOARD_TRUST_PROXY", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    response = client.get(
        "/dashboard",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"X-Forwarded-For": "203.0.113.10"},
    )

    assert response.status_code == 403


def test_no_token_blocks_spoofed_local_xff_when_proxy_untrusted(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    monkeypatch.setenv("DASHBOARD_TRUST_PROXY", "false")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    response = client.get(
        "/dashboard",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"X-Forwarded-For": "127.0.0.1, 203.0.113.10"},
    )

    assert response.status_code == 403


def test_no_token_blocks_spoofed_local_real_ip_when_proxy_untrusted(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    monkeypatch.setenv("DASHBOARD_TRUST_PROXY", "false")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    response = client.get(
        "/dashboard",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"X-Real-IP": "127.0.0.1"},
    )

    assert response.status_code == 403


def test_security_headers_are_set(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    response = client.get("/api/health", environ_base={"REMOTE_ADDR": "127.0.0.1"})

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_login_page_declares_utf8_and_keeps_chinese_text(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret-token")
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\nDASHBOARD_TOKEN=secret-token\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    response = client.get("/api/auth/login")

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert response.mimetype_params.get("charset", "").lower() == "utf-8"
    body = response.get_data(as_text=True)
    assert '<meta charset="utf-8">' in body
    assert 'placeholder="访问密码"' in body
    assert ">登录<" in body


def test_csrf_required_for_authenticated_mutating_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret-token")
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\nDASHBOARD_TOKEN=secret-token\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    assert client.post("/api/auth/login", data={"token": "secret-token"}).status_code == 302
    blocked = client.post("/api/auth/logout")
    token_payload = client.get("/api/csrf-token").get_json()
    allowed = client.post("/api/auth/logout", headers={"X-CSRF-Token": token_payload["csrf_token"]})

    assert blocked.status_code == 403
    assert allowed.status_code == 200


def test_chinese_dashboard_token_logs_in_and_counts_failures(tmp_path, monkeypatch):
    token = "访问密码"
    monkeypatch.setenv("DASHBOARD_TOKEN", token)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(f"PIXIV_REFRESH_TOKEN=test\nDASHBOARD_TOKEN={token}\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    ok = client.post("/api/auth/login", data={"token": token})
    assert ok.status_code == 302

    wrong = [client.post("/api/auth/login", data={"token": "错误密码"}) for _ in range(6)]
    assert [response.status_code for response in wrong[:5]] == [401, 401, 401, 401, 401]
    assert wrong[5].status_code == 429


def test_proxy_image_turns_non_200_into_502_and_forwards_cache_headers(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()
    url = "/proxy/image?url=https://i.pximg.net/c/1.jpg"

    class _Resp:
        def __init__(self, status_code, content, headers):
            self.status_code = status_code
            self.content = content
            self.headers = headers

    seen: list[dict] = []

    def fake_get(target, **kwargs):
        seen.append({"url": target, **kwargs})
        if len(seen) == 1:
            return _Resp(302, b"<html>go</html>", {"Location": "https://evil.example/a"})
        return _Resp(
            200,
            b"img",
            {"Content-Type": "image/jpeg", "Content-Length": "3", "Cache-Control": "public, max-age=60"},
        )

    monkeypatch.setattr(webapp_module.http_requests, "get", fake_get)

    blocked = client.get(url, environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert blocked.status_code == 502
    assert b"go" not in blocked.data
    assert seen[0]["allow_redirects"] is False

    ok = client.get(url, environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert ok.status_code == 200
    assert ok.data == b"img"
    assert ok.headers["Cache-Control"] == "public, max-age=60"
    assert ok.headers["Content-Length"] == "3"


def test_session_cookie_secure_reads_env_file(tmp_path, monkeypatch):
    """Secure 必须在 load_dotenv 之后判断。只写在 .env 里的反代开关，进程环境里还没有。"""
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    monkeypatch.delenv("PIXIV_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("DASHBOARD_TRUST_PROXY", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("DASHBOARD_TRUST_PROXY=true\n", encoding="utf-8")

    app = create_app(
        config_path=str(tmp_path / "missing.yaml"),
        env_path=str(env_path),
        start_scheduler=False,
    )

    assert app.config["SESSION_COOKIE_SECURE"] is True

    monkeypatch.delenv("DASHBOARD_TRUST_PROXY", raising=False)
    monkeypatch.delenv("PIXIV_COOKIE_SECURE", raising=False)
    forced = tmp_path / "forced.env"
    forced.write_text("DASHBOARD_TRUST_PROXY=true\nPIXIV_COOKIE_SECURE=false\n", encoding="utf-8")
    plain = create_app(
        config_path=str(tmp_path / "also-missing.yaml"),
        env_path=str(forced),
        start_scheduler=False,
    )
    assert plain.config["SESSION_COOKIE_SECURE"] is False


def test_login_rate_limit_blocks_repeated_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret-token")
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\nDASHBOARD_TOKEN=secret-token\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    responses = [client.post("/api/auth/login", data={"token": "bad"}) for _ in range(6)]

    assert [response.status_code for response in responses[:5]] == [401, 401, 401, 401, 401]
    assert responses[5].status_code == 429


def test_safe_name_strips_path_traversal_segments():
    from pixiv_novel_sync.webapp import safe_name

    assert safe_name('../bad:novel<>name', 'novel') == 'bad_novel_name'


def test_no_token_trusts_xff_client_when_proxy_trusted(tmp_path, monkeypatch):
    """显式信任代理（默认 1 层）时，按 XFF 右数第 1 个地址（可信代理追加的真实客户端）判定本机访问。"""
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    monkeypatch.setenv("DASHBOARD_TRUST_PROXY", "true")
    monkeypatch.delenv("DASHBOARD_TRUSTED_PROXY_HOPS", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    allowed = client.get(
        "/api/health",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"X-Forwarded-For": "127.0.0.1"},
    )
    blocked = client.get(
        "/dashboard",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"X-Forwarded-For": "203.0.113.10"},
    )

    assert allowed.status_code == 200
    assert blocked.status_code == 403


def test_no_token_blocks_missing_xff_when_proxy_trusted(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    monkeypatch.setenv("DASHBOARD_TRUST_PROXY", "true")
    monkeypatch.delenv("DASHBOARD_TRUSTED_PROXY_HOPS", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    response = client.get(
        "/dashboard",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 403


def test_no_token_blocks_short_xff_chain_when_proxy_trusted(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    monkeypatch.setenv("DASHBOARD_TRUST_PROXY", "true")
    monkeypatch.setenv("DASHBOARD_TRUSTED_PROXY_HOPS", "2")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    response = client.get(
        "/dashboard",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"X-Forwarded-For": "127.0.0.1"},
    )

    assert response.status_code == 403


def test_no_token_ignores_spoofed_leftmost_xff_when_proxy_trusted(tmp_path, monkeypatch):
    """M2: 攻击者在 XFF 左侧伪造 127.0.0.1，真实客户端 IP 由可信代理追加在右侧。
    必须按右数第 1 个（真实公网 IP）判定，拒绝访问——否则伪造最左值即可绕过本机判定。"""
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    monkeypatch.setenv("DASHBOARD_TRUST_PROXY", "true")
    monkeypatch.delenv("DASHBOARD_TRUSTED_PROXY_HOPS", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    response = client.get(
        "/dashboard",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"X-Forwarded-For": "127.0.0.1, 203.0.113.10"},
    )

    assert response.status_code == 403


def test_login_rate_limit_not_bypassed_by_rotating_spoofed_xff(tmp_path, monkeypatch):
    """M2: 信任代理时，轮换 XFF 左侧伪造 IP 不应绕过限流——限流键取右数第 1 个真实客户端 IP。"""
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret-token")
    monkeypatch.setenv("DASHBOARD_TRUST_PROXY", "true")
    monkeypatch.delenv("DASHBOARD_TRUSTED_PROXY_HOPS", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\nDASHBOARD_TOKEN=secret-token\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    # 真实客户端恒为 198.51.100.5（右侧），攻击者每次伪造不同的最左 IP
    responses = [
        client.post(
            "/api/auth/login",
            data={"token": "bad"},
            headers={"X-Forwarded-For": f"10.0.0.{i}, 198.51.100.5"},
        )
        for i in range(6)
    ]

    assert [r.status_code for r in responses[:5]] == [401, 401, 401, 401, 401]
    assert responses[5].status_code == 429


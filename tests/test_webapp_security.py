from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from pixiv_novel_sync.jobs.models import JobSource, JobStatus
from pixiv_novel_sync.webapp import _oauth_task_public_payload, create_app


def test_no_dashboard_token_allows_localhost(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path))
    client = app.test_client()

    response = client.get("/api/health", environ_base={"REMOTE_ADDR": "127.0.0.1"})

    assert response.status_code == 200


def test_no_dashboard_token_blocks_non_localhost(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path))
    client = app.test_client()

    response = client.get("/dashboard", environ_base={"REMOTE_ADDR": "203.0.113.10"})

    assert response.status_code == 403


def test_flask_secret_fallback_persists_to_env(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")

    first_app = create_app(env_path=str(env_path))
    first_secret = first_app.secret_key
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    second_app = create_app(env_path=str(env_path))

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
    app = create_app(env_path=str(env_path))
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
    app = create_app(env_path=str(env_path))
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
    app = create_app(env_path=str(env_path))
    client = app.test_client()

    start_response = client.post("/oauth/start")
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


def test_no_token_blocks_proxied_request_when_proxy_untrusted(tmp_path, monkeypatch):
    """反代后 remote_addr 恒为 127.0.0.1；存在代理头但未信任代理时必须拒绝，
    否则未配 token 的部署会把私密收藏暴露给全公网。"""
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    monkeypatch.delenv("DASHBOARD_TRUST_PROXY", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path))
    client = app.test_client()

    response = client.get(
        "/dashboard",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"X-Forwarded-For": "203.0.113.10"},
    )

    assert response.status_code == 403


def test_no_token_trusts_xff_localhost_when_proxy_trusted(tmp_path, monkeypatch):
    """显式信任代理时，按 XFF 最左地址判定本机访问。"""
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    monkeypatch.setenv("DASHBOARD_TRUST_PROXY", "true")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path))
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

"""T2-35：开关缓存、任务冲突、登录会话、健康检查、.env 空行和统计下沉。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from pixiv_novel_sync.settings import load_settings
from pixiv_novel_sync.web.managers import AutoSyncScheduler, SettingsManager
from pixiv_novel_sync.webapp import _load_or_create_flask_secret, create_app


def test_flask_secret_replaces_blank_env_line(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_FLASK_SECRET=\nOTHER=1\n", encoding="utf-8")
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)

    secret = _load_or_create_flask_secret(str(env_path))

    text = env_path.read_text(encoding="utf-8")
    assert text.count("PIXIV_FLASK_SECRET=") == 1
    assert f"PIXIV_FLASK_SECRET={secret}\n" in text
    assert "OTHER=1" in text


def test_health_returns_status_and_version_only(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)

    payload = app.test_client().get("/api/health").get_json()

    assert payload["status"] == "ok"
    assert payload["version"]
    assert set(payload) == {"status", "version"}


def test_busy_sync_returns_409(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)

    def keep_running(self, job_id):
        self.manager.mark_running(job_id, "running")
        return self.manager.get_job(job_id)

    monkeypatch.setattr("pixiv_novel_sync.webapp.JobRunner.run", keep_running)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()

    assert client.post("/api/dashboard/sync/start").status_code == 200
    blocked = client.post("/api/dashboard/sync/bookmark")

    assert blocked.status_code == 409
    assert blocked.get_json()["error"] == "已有同步任务正在运行，请稍后再试"


def test_login_clears_session_and_expires_after_seven_days(tmp_path, monkeypatch) -> None:
    from hashlib import sha256

    from pixiv_novel_sync.storage_db import Database

    monkeypatch.setenv("DASHBOARD_TOKEN", "secret-token")
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\nDASHBOARD_TOKEN=secret-token\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["stale"] = "yes"

    assert client.post("/api/auth/login", data={"token": "secret-token"}).status_code == 302
    with client.session_transaction() as sess:
        assert "stale" not in sess
        session_id = sess["auth_session_id"]
    with Database(load_settings(env_path=str(env_path)).storage.db_path) as db:
        record = db.get_web_auth_session(sha256(session_id.encode("ascii")).hexdigest())
    assert record is not None
    assert record["expires_at"] - record["created_at"] == 7 * 24 * 3600
    expired_now = record["created_at"] + 8 * 24 * 3600
    monkeypatch.setattr(time, "time", lambda: expired_now)

    expired = client.get("/api/dashboard/status")
    assert expired.status_code == 401


def test_user_status_check_reports_login_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)

    class _Auth:
        def __init__(self, settings):
            pass

        def login(self):
            raise RuntimeError("登录失败")

    monkeypatch.setattr("pixiv_novel_sync.auth.PixivAuthManager", _Auth)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)

    response = app.test_client().post("/api/dashboard/users/1/check")

    assert response.status_code == 500
    assert response.get_json()["error"] == "登录失败"


def test_auto_sync_toggle_persists_and_invalidates_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    monkeypatch.setattr(AutoSyncScheduler, "start", lambda self: None)
    monkeypatch.setattr(AutoSyncScheduler, "stop", lambda self: None)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("sync:\n  auto_sync_enabled: false\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(
        config_path=str(config_path),
        env_path=str(env_path),
        start_scheduler=False,
    )
    client = app.test_client()

    assert client.get("/api/dashboard/settings").get_json()["auto_sync_enabled"] is False
    toggled = client.post("/api/dashboard/auto-sync/toggle", json={"enabled": True})

    assert toggled.status_code == 200
    assert toggled.get_json()["enabled"] is True
    assert client.get("/api/dashboard/settings").get_json()["auto_sync_enabled"] is True
    assert load_settings(config_path, env_path).sync.auto_sync_enabled is True


def test_set_auto_sync_enabled_leaves_other_fields(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "sync:\n  auto_sync_enabled: false\n  delay_seconds_between_pages: 2.5\n",
        encoding="utf-8",
    )
    manager = SettingsManager(str(config_path))
    manager.load()

    manager.set_auto_sync_enabled(True)

    text = config_path.read_text(encoding="utf-8")
    assert "delay_seconds_between_pages" in text
    assert manager.load().sync.auto_sync_enabled is True


def test_library_export_comes_from_storage(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)

    payload = app.test_client().get("/api/dashboard/export/stats").get_json()

    assert payload["total_novels"] == 0
    assert payload["novels_by_status"] == {}
    assert payload["recent_tasks"] == []
    webapp_source = Path("src/pixiv_novel_sync/webapp.py").read_text(encoding="utf-8")
    export_body = webapp_source.split("def dashboard_export_stats", 1)[1].split("\n    @app.", 1)[0]
    status_body = webapp_source.split("def dashboard_status", 1)[1].split("\n    @app.", 1)[0]
    assert "SELECT" not in export_body
    assert "SELECT" not in status_body


def test_auth_exempt_list_has_no_nginx_health() -> None:
    source = Path("src/pixiv_novel_sync/webapp.py").read_text(encoding="utf-8")
    block = source.split("_AUTH_EXEMPT_PATHS = {", 1)[1].split("}", 1)[0]
    assert "/nginx-health" not in block


def test_web_token_ui_uses_waitress(monkeypatch, tmp_path) -> None:
    pytest.importorskip("waitress")
    captured: dict = {}

    def serve(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    import waitress

    monkeypatch.setattr(waitress, "serve", serve)
    monkeypatch.setattr(
        "pixiv_novel_sync.webapp.create_app",
        lambda **kwargs: captured.setdefault("created", object()),
    )
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("sync: {}\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "pixiv-novel-sync",
            "--config",
            str(config_path),
            "--env-file",
            str(env_path),
            "web-token-ui",
            "--host",
            "127.0.0.1",
            "--port",
            "5999",
        ],
    )

    from pixiv_novel_sync.cli import main

    main()

    assert captured["kwargs"]["host"] == "127.0.0.1"
    assert captured["kwargs"]["port"] == 5999
    assert captured["app"] is not None

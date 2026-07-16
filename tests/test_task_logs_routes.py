from __future__ import annotations

from pathlib import Path

from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.webapp import create_app


def _create_test_app(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "task-log-test-secret")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "storage:\n"
        f"  public_dir: {(tmp_path / 'public').as_posix()}\n"
        f"  private_dir: {(tmp_path / 'private').as_posix()}\n"
        f"  db_path: {(tmp_path / 'task-logs.db').as_posix()}\n"
        "sync:\n"
        "  auto_sync_enabled: false\n",
        encoding="utf-8",
    )
    app = create_app(config_path=str(config_path), env_path=str(env_path))
    app.config["TESTING"] = True
    return app


def test_global_logs_route_passes_ai_status_filter(tmp_path, monkeypatch):
    captured = {}

    def fake_get_ai_task_logs(self, **kwargs):
        captured.update(kwargs)
        return {"items": [], "page": 1, "page_size": 20, "total": 0, "total_pages": 0}

    app = _create_test_app(tmp_path, monkeypatch)
    monkeypatch.setattr(Database, "get_ai_task_logs", fake_get_ai_task_logs)

    response = app.test_client().get(
        "/api/dashboard/logs?category=ai&status=failed&days=3",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert captured["status"] == "failed"

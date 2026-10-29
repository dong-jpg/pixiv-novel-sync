from __future__ import annotations

from pathlib import Path

from pixiv_novel_sync.settings import load_settings
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
    app = create_app(config_path=str(config_path), env_path=str(env_path), start_scheduler=False)
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


def test_log_list_omits_log_lines_and_raw_json_but_detail_keeps_them(tmp_path, monkeypatch):
    """回归（审查 2026-10-28 性能 P4）：列表 SELECT * 把每行最多 500 条日志的原串与
    解析结果各发一遍（一页 4–5 MB），而列表页只显示摘要，日志行只在详情弹窗里用。"""
    app = _create_test_app(tmp_path, monkeypatch)
    # 按 app 实际读到的设置找库：环境变量优先于 YAML，conftest 的隔离夹具设了 PIXIV_DB_PATH
    db = Database(load_settings(tmp_path / "config.yaml", tmp_path / ".env").storage.db_path)
    db.init_schema()
    lines = [{"time": "00:00:00", "level": "info", "message": f"第 {index} 条日志"} for index in range(500)]
    log_id = db.create_task_log("bookmark", "同步收藏")
    db.update_task_log(log_id, "succeeded", stats={"novels": 3}, logs=lines)
    db.close()
    client = app.test_client()

    listed = client.get("/api/dashboard/logs?days=3", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    detail = client.get(f"/api/dashboard/logs/{log_id}", environ_base={"REMOTE_ADDR": "127.0.0.1"})

    assert listed.status_code == 200
    item = listed.get_json()["items"][0]
    assert item["stats"] == {"novels": 3}
    assert item["status"] == "succeeded"
    assert not {"logs", "logs_json", "stats_json"} & set(item)
    assert len(listed.data) < 2_000

    assert detail.status_code == 200
    payload = detail.get_json()
    assert payload["logs"] == lines
    assert not {"logs_json", "stats_json"} & set(payload)
    assert "第 499 条日志".encode("utf-8") in detail.data  # 中文不转成 \uXXXX

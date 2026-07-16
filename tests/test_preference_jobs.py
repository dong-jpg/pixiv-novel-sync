from __future__ import annotations

import time
from types import SimpleNamespace

from pixiv_novel_sync.jobs.models import JobStatus
from pixiv_novel_sync.web.managers import AutoSyncScheduler
from pixiv_novel_sync.webapp import create_app


def test_preference_analyze_route_runs_shared_job(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path))
    client = app.test_client()

    response = client.post(
        "/api/dashboard/preferences/profiles/analyze",
        json={"name": "本地偏好画像", "is_default": True, "scope": {"min_text_length": 1000}},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"] is True
    job_id = payload["data"]["job_id"]

    deadline = time.time() + 5
    final_job = None
    while time.time() < deadline:
        status_response = client.get(
            f"/api/dashboard/sync/status?job_id={job_id}",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        final_job = status_response.get_json()["job"]
        if final_job["status"] in {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
            break
        time.sleep(0.1)

    assert final_job is not None
    assert final_job["status"] == JobStatus.SUCCEEDED.value
    assert final_job["job_type"] == "preference_analyze"


def test_preference_analyze_writes_task_log(tmp_path, monkeypatch):
    """#9: 偏好分析 job 经统一提交器写入 task_logs，应出现在任务日志页。"""
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path))
    client = app.test_client()

    response = client.post(
        "/api/dashboard/preferences/profiles/analyze",
        json={"name": "本地偏好画像", "is_default": True, "scope": {"min_text_length": 1000}},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert response.status_code == 200
    job_id = response.get_json()["data"]["job_id"]

    deadline = time.time() + 5
    while time.time() < deadline:
        status_response = client.get(
            f"/api/dashboard/sync/status?job_id={job_id}",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        status = status_response.get_json()["job"]["status"]
        if status in {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
            break
        time.sleep(0.1)

    # 该 job 应作为一条 preference_analyze 记录出现在任务日志里
    logs_response = client.get(
        "/api/dashboard/logs?page=1&page_size=50&days=3",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert logs_response.status_code == 200
    rows = logs_response.get_json()["items"]
    matched = [r for r in rows if r.get("job_id") == job_id]
    assert matched, "偏好分析 job 未写入 task_logs"
    assert matched[0]["task_type"] == "preference_analyze"


def test_scheduled_preference_analysis_uses_shared_task(tmp_path, monkeypatch):
    class FakeSyncJobManager:
        def add_log(self, _job_id, _level, _message):
            return None

        def is_cancel_requested(self, _job_id):
            return False

    calls = []
    settings = SimpleNamespace(
        storage=SimpleNamespace(db_path=tmp_path / "preferences.db"),
        sync=SimpleNamespace(preference_analyze_batch_size=50),
    )
    manager = AutoSyncScheduler(config_path=None, env_path=None, sync_job_manager=FakeSyncJobManager())

    def fake_execute(task_type, current_settings, context):
        calls.append((task_type, current_settings, context))
        return {"processed_this_run": 1}

    monkeypatch.setattr("pixiv_novel_sync.web.managers.execute_task", fake_execute, raising=False)

    manager._sync_preference_analyze(settings, "job-1")

    assert calls[0][0] == "preference_analyze"
    assert calls[0][1] is settings
    assert calls[0][2]["params"]["scope"] == {"batch_size": 50, "max_batches": 1}

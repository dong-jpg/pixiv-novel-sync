from __future__ import annotations

import time

from pixiv_novel_sync.jobs.models import JobStatus
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

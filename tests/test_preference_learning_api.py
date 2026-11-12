"""Real Flask, shared runner, router and temporary business tables."""

from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus, JobType
from pixiv_novel_sync.webapp import create_app
from test_preference_learning_profiles import env, request_for  # noqa: F401


BASE = "/api/dashboard/preference-learning"


def wait_for(predicate):
    deadline = time.monotonic() + 8
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Synthetic shared worker did not finish")
        time.sleep(0.01)


@pytest.fixture
def learning_app(env, tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("sync:\n  auto_sync_enabled: false\n", encoding="utf-8")
    dotenv = tmp_path / ".env"
    dotenv.write_text("PIXIV_REFRESH_TOKEN=synthetic-never-used\n", encoding="utf-8")
    monkeypatch.setenv("PIXIV_DB_PATH", str(env.db.path))
    monkeypatch.setenv("DASHBOARD_TOKEN", "synthetic-dashboard-token")
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "synthetic-session-secret")
    app = create_app(str(config), str(dotenv), start_scheduler=False)
    app.config.update(TESTING=True, RAW_HTTP=True)
    app.extensions["pixiv_novel_sync.work_ai_factory"] = lambda: env.service
    scheduler = app.config["auto_sync_scheduler"]
    starts = []
    stops = []
    monkeypatch.setattr(scheduler, "start", lambda: starts.append(True))
    monkeypatch.setattr(scheduler, "stop", lambda: stops.append(True))
    client = app.test_client()
    value = SimpleNamespace(app=app, client=client, env=env, starts=starts, stops=stops,
                            service=app.extensions["pixiv_novel_sync.preference_learning"],
                            coordinator=app.extensions["work_analysis_coordinator"],
                            manager=app.config["job_manager"])
    try:
        yield value
    finally:
        active, waiting = value.manager.shared_admission_snapshot()
        for job in active + waiting:
            value.coordinator.cancel_shared_job(job.job_id)
        wait_for(value.manager.shared_admission_is_idle)
        app.extensions["close_work_analysis_owner"]()
        app.extensions["pixiv_novel_sync.ai_service"].close()


def login(api):
    assert api.client.post("/api/auth/login", data={"token": "synthetic-dashboard-token"}).status_code == 302
    return {"X-CSRF-Token": api.client.get("/api/csrf-token").get_json()["csrf_token"]}


def feedback(api, headers):
    response = api.client.put(BASE + "/feedback/series/10", headers=headers,
                              json={"reaction": "liked", "expected_version": 0})
    assert response.status_code == 200, response.get_json()


def create_body(api, headers):
    payload = request_for(api.env)
    response = api.client.post(BASE + "/profile-preview", headers=headers, json=payload)
    assert response.status_code == 200, response.get_json()
    preview = response.get_json()["data"]
    assert preview["can_create"] and preview["targets"]
    return {**payload, "preview_fingerprint": preview["preview_fingerprint"], "idempotency_key": "1" * 32}


@pytest.mark.parametrize("path", ["/state", "/feedback/series/10", "/profiles/" + "1" * 32,
                                  "/batches/" + "1" * 32, "/does-not-exist"])
def test_learning_auth_and_framework_errors_are_not_cacheable(learning_app, path):
    response = learning_app.client.get(BASE + path)
    assert response.status_code == 401
    assert "no-store" in response.headers["Cache-Control"]


def test_learning_body_boundary_requires_csrf_and_rejects_ambiguous_json(learning_app):
    api = learning_app
    headers = login(api)
    route = BASE + "/feedback/series/10"
    assert api.client.put(route, json={"reaction": "liked", "expected_version": 0}).status_code == 403
    before = api.env.db.conn.execute("SELECT count(*) FROM preference_reading_feedback").fetchone()[0]
    for raw in ('{"reaction":"liked","reaction":"disliked","expected_version":0}',
                '{"reaction":"liked","expected_version":NaN}', '[]'):
        response = api.client.put(route, headers=headers, content_type="application/json", data=raw)
        assert response.status_code == 400
        assert "no-store" in response.headers["Cache-Control"]
    assert api.env.db.conn.execute("SELECT count(*) FROM preference_reading_feedback").fetchone()[0] == before
    assert api.client.get(BASE + "/state?extra=1").status_code == 400
    assert api.client.get(BASE + "/batches/" + "1" * 32 + "?limit=2&limit=3").status_code == 400
    assert not api.env.summary_calls


def test_real_learning_workflow_reaches_recommendations_without_legacy_profile(learning_app):
    api = learning_app
    headers = login(api)
    assert api.env.db.get_default_preference_profile() is None
    feedback(api, headers)
    payload = create_body(api, headers)
    assert not api.env.summary_calls
    response = api.client.post(BASE + "/profiles", headers=headers, json=payload)
    assert response.status_code == 200, response.get_json()
    run_id = response.get_json()["data"]["run_id"]
    wait_for(lambda: api.service.detail(run_id)["status"] == "succeeded")
    again = api.client.post(BASE + "/profiles", headers=headers, json=payload)
    assert again.status_code == 200 and again.get_json()["data"]["run_id"] == run_id
    assert len(api.env.summary_calls) == 1
    detail = api.client.get(BASE + "/profiles/" + run_id).get_json()["data"]
    assert detail["result"]["preferences"][0]["evidence_refs"]
    activated = api.client.post(BASE + "/profiles/" + run_id + "/activate", headers=headers,
                                json={"expected_version": detail["version"]})
    assert activated.status_code == 200 and activated.get_json()["data"]["active"]
    profiles = api.client.get("/api/dashboard/preferences/profiles").get_json()["data"]
    assert profiles and profiles[0]["is_default"]
    plan = api.client.post("/api/dashboard/recommendations/search-plan", headers=headers,
                           json={"profile_id": profiles[0]["id"]})
    assert plan.status_code == 200
    assert any("Synthetic theme" in row["query"] for row in plan.get_json()["data"]["queries"])
    jobs = [job for job in api.manager._jobs.values() if job.spec.job_type == JobType.PREFERENCE_SUMMARIZE]
    assert len(jobs) == 1 and jobs[0].spec.params == {"run_id": run_id}
    generic = json.dumps([dict(row) for row in api.env.db.conn.execute("SELECT * FROM task_logs")])
    assert "PRIVATE_PROFILE_SENTINEL_8182" not in generic
    assert "Unique synthetic evidence." not in generic
    wait_for(api.manager.shared_admission_is_idle)


def test_queued_profile_waits_for_the_existing_shared_slot(learning_app):
    api = learning_app
    headers = login(api)
    feedback(api, headers)
    payload = create_body(api, headers)
    occupied = api.manager.submit(JobSpec(JobSource.WEB, ["bookmark"], JobType.SYNC))
    assert api.manager.acquire_run_slot(occupied.job_id)
    api.manager.mark_running(occupied.job_id)
    try:
        response = api.client.post(BASE + "/profiles", headers=headers, json=payload)
        assert response.status_code == 200
        run_id = response.get_json()["data"]["run_id"]
        assert api.service.detail(run_id)["status"] == "queued"
        assert not api.env.summary_calls
    finally:
        api.manager.mark_succeeded(occupied.job_id)
        api.manager.release_run_slot()
    wait_for(lambda: api.service.detail(run_id)["status"] == "succeeded")
    assert len(api.env.summary_calls) == 1


def test_shared_log_failure_after_submission_does_not_strand_or_duplicate_a_profile(learning_app, monkeypatch):
    api = learning_app
    headers = login(api)
    feedback(api, headers)
    payload = create_body(api, headers)
    admission = api.app.config["auto_sync_scheduler"].preference_learning_admission
    original = admission.submit_shared

    def fail_after_submit(*args, **kwargs):
        job = original(*args, **kwargs)
        api.manager.mark_failed(job.job_id, "synthetic log backfill failure")
        raise OSError("synthetic log backfill failure")

    monkeypatch.setattr(admission, "submit_shared", fail_after_submit)
    response = api.client.post(BASE + "/profiles", headers=headers, json=payload)
    assert response.status_code == 200
    run_id = response.get_json()["data"]["run_id"]
    assert api.service.detail(run_id)["status"] == "failed"
    again = api.client.post(BASE + "/profiles", headers=headers, json=payload)
    assert again.status_code == 200 and again.get_json()["data"]["run_id"] == run_id
    assert len(api.manager._jobs) == 1 and not api.env.summary_calls


def test_business_cancel_does_not_release_the_slot_or_publish_late_output(learning_app):
    api = learning_app
    headers = login(api)
    feedback(api, headers)
    payload = create_body(api, headers)
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        assert release.wait(6)

    api.env.before_summary = hold
    try:
        created = api.client.post(BASE + "/profiles", headers=headers, json=payload)
        assert created.status_code == 200
        run_id = created.get_json()["data"]["run_id"]
        assert entered.wait(5)
        running = api.service.detail(run_id)
        cancelled = api.client.post(BASE + "/profiles/" + run_id + "/cancel", headers=headers,
                                    json={"expected_version": running["version"]})
        assert cancelled.status_code == 200
        assert cancelled.get_json()["data"]["status"] == "cancelled"
        assert api.manager.acquire_run_slot() is False
    finally:
        release.set()
    wait_for(api.manager.shared_admission_is_idle)
    result = api.service.detail(run_id)
    assert result["result"] is None and result["usage"]["network_request_count"] == 1
    assert all(job.status == JobStatus.CANCELLED for job in api.manager._jobs.values())


def test_generic_cancel_finishes_a_waiting_profile_without_a_scheduler_tick(learning_app, monkeypatch):
    api = learning_app
    headers = login(api)
    feedback(api, headers)
    payload = create_body(api, headers)
    monkeypatch.setattr(api.coordinator, "_launch", lambda job: False)
    created = api.client.post(BASE + "/profiles", headers=headers, json=payload)
    assert created.status_code == 200
    run_id = created.get_json()["data"]["run_id"]
    active, waiting = api.manager.shared_admission_snapshot()
    assert not active and len(waiting) == 1
    response = api.client.post("/api/dashboard/sync/cancel", headers=headers,
                               json={"job_id": waiting[0].job_id})
    assert response.status_code == 200
    assert api.service.detail(run_id)["status"] == "cancelled"
    assert api.manager.shared_admission_is_idle()
    assert not api.env.summary_calls


def test_library_policy_keeps_the_shared_scheduler_when_sync_is_disabled(learning_app):
    api = learning_app
    headers = login(api)
    payload = {
        "scope": "all", "agent_id": api.env.agent_id, "output_tokens": 128,
        "work_budget": {"request_limit": 2, "token_limit": 100000, "active_seconds_limit": 60},
        "batch_budget": {"request_limit": 4, "token_limit": 200000, "active_seconds_limit": 120},
        "work_limit": 2, "interval_minutes": 60,
        "confirmations": {"external_send_confirmed": True, "budget_confirmed": True,
                          "current_archive_only": True, "order_confirmed": True},
    }
    response = api.client.post(BASE + "/batch-preview", headers=headers, json=payload)
    assert response.status_code == 200, response.get_json()
    state = api.client.get(BASE + "/state").get_json()["data"]
    request = deepcopy(payload) | {"enabled": True, "expected_version": state["library"]["policy"]["version"],
                                   "preview_fingerprint": response.get_json()["data"]["preview_fingerprint"]}
    enabled = api.client.put(BASE + "/automation", headers=headers, json=request)
    assert enabled.status_code == 200, enabled.get_json()
    starts = len(api.starts)
    disabled_sync = api.client.post("/api/dashboard/auto-sync/toggle", headers=headers, json={"enabled": False})
    assert disabled_sync.status_code == 200
    assert len(api.starts) == starts + 1 and not api.stops
    assert api.client.get(BASE + "/state").get_json()["data"]["library"]["policy"]["enabled"]
    assert not api.env.summary_calls


def test_unrecovered_learning_owner_rejects_paid_intents_but_allows_local_feedback(learning_app):
    api = learning_app
    headers = login(api)
    admission = api.app.config["auto_sync_scheduler"].preference_learning_admission
    admission.ready = False
    feedback(api, headers)
    payload = create_body(api, headers)
    response = api.client.post(BASE + "/profiles", headers=headers, json=payload)
    assert response.status_code == 503 and response.get_json()["code"] == "admission_unavailable"
    assert not api.env.summary_calls
    assert api.env.db.conn.execute("SELECT count(*) FROM preference_summary_runs").fetchone()[0] == 0

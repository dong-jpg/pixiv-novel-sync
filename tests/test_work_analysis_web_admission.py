from __future__ import annotations

import threading
import time

import pytest

import pixiv_novel_sync.webapp as webapp_module
from pixiv_novel_sync.jobs.models import JobStatus, JobType
from pixiv_novel_sync.storage_db import Database
from work_analysis_runtime_helpers import make_runtime_environment


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Web worker did not reach the expected state")
        time.sleep(0.005)


@pytest.fixture
def web_work(tmp_path, monkeypatch, request):
    env = make_runtime_environment(tmp_path, monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("sync:\n  auto_sync_enabled: false\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=synthetic-never-used\n", encoding="utf-8")
    monkeypatch.setenv("PIXIV_DB_PATH", str(env.db.path))
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "synthetic-test-secret")
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    monkeypatch.setattr("pixiv_novel_sync.web.managers.SCHEDULER_PREEMPT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(webapp_module, "execute_task", lambda task, settings, context: {"synthetic_sync": 1})
    fail_startup = getattr(request, "param", None) == "startup_failure"
    recovery_attempts = []
    if fail_startup:
        original_recovery = Database.fail_stale_task_logs

        def fail_once(self):
            recovery_attempts.append(True)
            if len(recovery_attempts) == 1:
                raise OSError("synthetic startup recovery failure")
            return original_recovery(self)

        monkeypatch.setattr(Database, "fail_stale_task_logs", fail_once)
    app = webapp_module.create_app(str(config_path), str(env_path), start_scheduler=False)
    coordinator = app.extensions["work_analysis_coordinator"]
    app.extensions["pixiv_novel_sync.work_ai_factory"] = lambda: env.service
    interrupted = env.db.get_work_analysis(env.run_id)
    if fail_startup:
        assert recovery_attempts == [True] and coordinator._recovered is False
        assert interrupted["status"] == "queued"
    else:
        env.db.resume_work_analysis(env.run_id, interrupted["version"])
    try:
        yield app, env
    finally:
        active, waiting = app.config["job_manager"].shared_admission_snapshot()
        for job in active + waiting:
            coordinator.cancel_shared_job(job.job_id)
        wait_for(app.config["job_manager"].shared_admission_is_idle)
        app.extensions["close_work_analysis_owner"]()
        env.close()


def test_web_manual_off_creation_uses_the_real_shared_runner_and_id_only_params(web_work):
    app, env = web_work
    app.extensions["work_analysis_coordinator"].wake("created")
    wait_for(lambda: env.db.get_work_analysis(env.run_id)["status"] == "succeeded")
    manager = app.config["job_manager"]
    jobs = list(manager._jobs.values())
    assert len(jobs) == 1 and jobs[0].spec.params == {"run_id": env.run_id}
    assert jobs[0].spec.job_type == JobType.WORK_ANALYSIS
    assert env.db.get_work_analysis_report(env.run_id)["synthesis_complete"] is True
    assert app.config["auto_sync_scheduler"].is_running() is False


def test_generic_http_cancel_cannot_report_success_before_parent_stop(web_work):
    app, env = web_work
    entered, release = threading.Event(), threading.Event()

    def hold(unit_id):
        entered.set()
        assert release.wait(timeout=5)

    env.provider.before_response = hold
    try:
        app.extensions["work_analysis_coordinator"].wake("created")
        assert entered.wait(timeout=5)
        job = app.config["job_manager"].shared_admission_snapshot()[0][0]
        response = app.test_client().post("/api/dashboard/sync/cancel", json={"job_id": job.job_id})
        assert response.status_code == 200
        assert env.db.get_work_analysis(env.run_id)["status"] == "cancelling"
        assert app.config["job_manager"].acquire_run_slot() is False
    finally:
        release.set()
    wait_for(lambda: env.db.get_work_analysis(env.run_id)["status"] == "cancelled")


@pytest.mark.parametrize("web_work", ["startup_failure"], indirect=True)
def test_generic_http_cancel_works_after_work_startup_recovery_failure(web_work, monkeypatch):
    app, env = web_work
    coordinator = app.extensions["work_analysis_coordinator"]
    manager = app.config["job_manager"]
    entered, release = threading.Event(), threading.Event()

    def hold_sync(task, settings, context):
        assert task == "bookmark"
        entered.set()
        assert release.wait(timeout=5)
        return {"synthetic_sync": 1}

    monkeypatch.setattr(webapp_module, "execute_task", hold_sync)
    before = env.db.get_work_analysis(env.run_id)
    client = app.test_client()
    try:
        response = client.post("/api/dashboard/sync/bookmark")
        assert response.status_code == 200
        assert entered.wait(timeout=5)
        job = manager.latest_job()
        response = client.post("/api/dashboard/sync/cancel", json={"job_id": job.job_id})
        assert response.status_code == 200
        assert manager.is_cancel_requested(job.job_id)
        assert coordinator.wake("created") is False
        response = client.post(f"/api/dashboard/work-analyses/{env.run_id}/pause",
                               json={"expected_version": before["version"]})
        assert response.status_code == 503
        assert response.get_json()["code"] == "admission_unavailable"
    finally:
        release.set()
        wait_for(manager.shared_admission_is_idle)
    assert job.status == JobStatus.CANCELLED
    assert coordinator._recovered is False
    assert env.db.get_work_analysis(env.run_id) == before
    assert env.provider.sent_units == []


def test_manual_p1_enters_existing_manager_while_work_is_draining(web_work):
    app, env = web_work
    entered, release = threading.Event(), threading.Event()

    def hold(unit_id):
        entered.set()
        assert release.wait(timeout=5)

    env.provider.before_response = hold
    try:
        app.extensions["work_analysis_coordinator"].wake("created")
        assert entered.wait(timeout=5)
        response = app.test_client().post("/api/dashboard/sync/bookmark")
        assert response.status_code == 200
        manager = app.config["job_manager"]
        active, waiting = manager.shared_admission_snapshot()
        assert len(active) == len(waiting) == 1
        assert waiting[0].spec.task_types == ["bookmark"] and waiting[0].started_at is None
        assert env.db.get_work_analysis(env.run_id)["reason"] == "sync_yield"
    finally:
        release.set()
    wait_for(lambda: env.db.get_work_analysis(env.run_id)["status"] == "succeeded")
    assert [job.spec.task_types for job in manager._jobs.values()] == [["work_analysis"], ["bookmark"], ["work_analysis"]]


def test_pending_work_placeholder_does_not_block_a_due_p1_scheduler_submission(web_work):
    app, env = web_work
    manager = app.config["job_manager"]
    assert manager.acquire_run_slot()
    coordinator = app.extensions["work_analysis_coordinator"]
    coordinator.wake("created")
    work = manager.shared_admission_snapshot()[1][0]
    submit = app.config["submit_shared_web_job"]
    settings = coordinator.settings_provider()
    sync = submit(webapp_module._scheduler_job_spec("bookmarks"), settings,
                  "bookmark", "Synthetic scheduled bookmark", is_auto_sync=True,
                  run_async=False, scheduler_priority=1)
    assert work.shared_waiting and sync.status == JobStatus.QUEUED
    # The unrelated test holder releases without an ordinary sync worker yet.
    # Its callback cannot launch work past the already-admitted sync item.
    manager.release_run_slot()
    assert work.shared_waiting and work.started_at is None
    app.config["run_shared_job"](sync.job_id)
    wait_for(lambda: env.db.get_work_analysis(env.run_id)["status"] == "succeeded")


def test_secondary_app_reuses_stopped_owner_and_does_not_recover_live_parent_again(web_work):
    app, env = web_work
    coordinator = app.extensions["work_analysis_coordinator"]
    second = webapp_module.create_app(app.config["work_config_path"], app.config["work_env_path"], start_scheduler=False)
    assert second.config["job_manager"] is app.config["job_manager"]
    assert second.extensions["work_analysis_coordinator"] is coordinator
    assert coordinator.startup_recover() == 0
    assert env.db.get_work_analysis(env.run_id)["status"] == "queued"
    assert not app.config["auto_sync_scheduler"].is_running()


def test_non_web_work_execution_remains_unsupported(web_work):
    app, env = web_work
    from pixiv_novel_sync.jobs.tasks import execute_task

    with pytest.raises(RuntimeError, match="Unsupported task type"):
        execute_task("work_analysis", app.extensions["work_analysis_coordinator"].settings_provider(), {})
    assert env.provider.sent_units == []


def test_idle_teardown_keeps_owner_pinned_until_terminal_runner_releases_slot(web_work, monkeypatch):
    app, env = web_work
    manager = app.config["job_manager"]
    coordinator = app.extensions["work_analysis_coordinator"]
    terminal_slot_held = threading.Event()
    release_slot = threading.Event()
    completed = threading.Event()
    original_release = manager.release_run_slot
    original_finished = coordinator.on_job_finished

    def blocked_release():
        terminal_slot_held.set()
        assert release_slot.wait(timeout=5)
        original_release()

    def observed_finished(job_id):
        try:
            original_finished(job_id)
        finally:
            completed.set()

    monkeypatch.setattr(manager, "release_run_slot", blocked_release)
    monkeypatch.setattr(coordinator, "on_job_finished", observed_finished)
    try:
        coordinator.wake("created")
        assert terminal_slot_held.wait(timeout=5)
        assert manager.latest_job().status == JobStatus.SUCCEEDED
        assert manager.shared_admission_snapshot() == ([], [])
        assert manager.acquire_run_slot() is False
        with pytest.raises(RuntimeError, match="shared_owner_still_active"):
            app.extensions["close_work_analysis_owner"]()
        second = webapp_module.create_app(
            app.config["work_config_path"], app.config["work_env_path"], start_scheduler=False,
        )
        assert second.config["job_manager"] is manager
        assert second.extensions["work_analysis_coordinator"] is coordinator
        assert coordinator._closed is False
        assert len(manager._jobs) == 1
    finally:
        release_slot.set()
        assert completed.wait(timeout=5)


def test_real_web_task_log_excludes_source_claim_and_report_sentinels(web_work, monkeypatch):
    import json

    app, env = web_work
    source_marker = "synthetic opening and ending"
    report_marker = "PRIVATE_SYNTHETIC_WEB_REPORT_6721"
    claim_marker = "PRIVATE_SYNTHETIC_WEB_CLAIM_8543"
    original_stream = env.provider.stream_generate

    def report_stream(messages, *args, **kwargs):
        sent = json.loads(messages[-1]["content"])
        block = next(item for item in sent["blocks"] if source_marker in item["text"])
        for chunk in original_stream(messages, *args, **kwargs):
            if chunk.type == "delta":
                result = json.loads(chunk.text)
                result.update(summary=report_marker, no_findings_reason=None, claims=[{
                    "dimension": "theme", "certainty": "interpretation", "label": "synthetic",
                    "statement": claim_marker,
                    "quotes": [{"block_id": block["block_id"], "quote": source_marker}],
                }])
                chunk.text = json.dumps(result)
            yield chunk

    monkeypatch.setattr(env.provider, "stream_generate", report_stream)
    app.extensions["work_analysis_coordinator"].wake("created")
    wait_for(lambda: env.db.get_work_analysis(env.run_id)["status"] == "succeeded")
    manager = app.config["job_manager"]
    jobs = list(manager._jobs.values())
    assert len(jobs) == 1
    job = jobs[0]
    wait_for(lambda: env.db.conn.execute(
        "SELECT status FROM task_logs WHERE id=? AND job_id=?",
        (job.progress["log_id"], job.job_id),
    ).fetchone()["status"] == "succeeded")
    assert job.status == JobStatus.SUCCEEDED
    report = env.db.get_work_analysis_report(env.run_id)
    artifact = report["artifact"]
    assert artifact["summary"] == report_marker
    assert artifact["claims"][0]["statement"] == claim_marker
    assert next(iter(artifact["evidence"].values()))["quote"] == source_marker
    task_logs = [dict(row) for row in env.db.conn.execute("SELECT * FROM task_logs")]
    generic = [dict(row) for row in env.db.conn.execute("SELECT * FROM ai_jobs")]
    attempts = [dict(row) for row in env.db.conn.execute("SELECT * FROM ai_job_model_attempts")]
    assert task_logs and generic and attempts
    stored = json.dumps(task_logs + generic + attempts, ensure_ascii=False)
    shared = json.dumps({"params": job.spec.params, "progress": job.progress, "stats": job.stats,
                         "logs": [entry.message for entry in job.logs]}, ensure_ascii=False)
    for sentinel in (source_marker, report_marker, claim_marker):
        assert sentinel not in stored and sentinel not in shared

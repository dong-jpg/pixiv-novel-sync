from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus, JobType
from pixiv_novel_sync.jobs.runner import JobRunner
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.web.managers import AutoSyncScheduler
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError
from pixiv_novel_sync.work_analysis.coordinator import WorkAnalysisCoordinator
from pixiv_novel_sync.work_analysis.executor import WorkAnalysisExecutor
from work_analysis_runtime_helpers import make_runtime_environment


def wait_for(predicate):
    end = time.monotonic() + 5
    while not predicate():
        if time.monotonic() >= end:
            raise AssertionError("synthetic worker did not reach its event")
        time.sleep(0.005)


def make_bridge(tmp_path, monkeypatch, *, auto_enabled=False):
    env = make_runtime_environment(tmp_path, monkeypatch)
    manager = JobManager()
    settings = SimpleNamespace(sync=SimpleNamespace(
        auto_sync_enabled=auto_enabled, auto_sync_bookmarks_enabled=auto_enabled,
        auto_sync_timezone="UTC",
    ))
    scheduler = AutoSyncScheduler(None, None, shared_job_manager=manager)
    scheduler._running = auto_enabled
    monkeypatch.setattr("pixiv_novel_sync.web.managers.SCHEDULER_PREEMPT_POLL_SECONDS", 0.01)
    entered = threading.Event()
    release_provider = threading.Event()
    sync_entered = threading.Event()
    release_sync = threading.Event()
    threads = []
    starts = []
    holder = {}

    def hold_response(unit_id):
        entered.set()
        assert release_provider.wait(timeout=5)

    env.provider.before_response = hold_response
    executor = WorkAnalysisExecutor(env.db_factory, lambda: env.service)

    def execute(task, context):
        starts.append(task)
        if task == "work_analysis":
            context = {**context, "work_control_scope": holder["coordinator"].work_control_scope}
            return executor.run(context["params"]["run_id"], context)
        assert task == "bookmark"
        sync_entered.set()
        assert release_sync.wait(timeout=5)
        return {"bookmarks": 1}

    runner = JobRunner(manager, execute)

    def submit(spec, current_settings, task_type, task_name, *, run_async=True,
               work_admission_priority=None, progress=None):
        assert run_async is False and current_settings is settings
        job = manager.submit_work_waiting(spec, work_admission_priority)
        if progress:
            manager.update_progress(job.job_id, **progress)
        return job

    def launch(job_id):
        def run_and_complete(current_job_id):
            try:
                runner.run(current_job_id)
            finally:
                holder["coordinator"].on_job_finished(current_job_id)

        def watched():
            job = manager.get_job(job_id)
            if job.spec.job_type == JobType.WORK_ANALYSIS:
                scheduler._run_and_watch_for_preemption(settings, job_id, "work_analysis", run_and_complete)
            else:
                run_and_complete(job_id)

        thread = threading.Thread(target=watched, daemon=True)
        threads.append(thread)
        thread.start()

    coordinator = WorkAnalysisCoordinator(manager, submit, launch, lambda: settings,
                                          db_factory=env.db_factory, scheduler=scheduler)
    holder["coordinator"] = coordinator
    scheduler.work_coordinator = coordinator
    # Production calls recovery BEFORE creation. This fixture created its run
    # during setup, so prove startup fencing then explicitly restore the intent.
    coordinator.startup_recover()
    interrupted = env.db.get_work_analysis(env.run_id)
    env.db.resume_work_analysis(env.run_id, interrupted["version"])

    def close():
        release_provider.set()
        release_sync.set()
        for thread in list(threads):
            thread.join(timeout=5)
            assert not thread.is_alive()
        coordinator.close()
        env.close()

    return SimpleNamespace(env=env, manager=manager, scheduler=scheduler, settings=settings,
                           coordinator=coordinator, entered=entered, release_provider=release_provider,
                           sync_entered=sync_entered, release_sync=release_sync, threads=threads,
                           starts=starts, close=close)


def test_manual_sync_waits_for_real_exit_then_work_resumes_with_auto_sync_off(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        before = bridge.env.db.get_work_analysis(bridge.env.run_id, include_private=True)
        sync = bridge.coordinator.request_sync(JobSpec(JobSource.WEB, ["bookmark"]), bridge.settings,
                                               "bookmark", "Synthetic bookmark sync")
        stopping = bridge.env.db.get_work_analysis(bridge.env.run_id, include_private=True)
        assert stopping["status"] == "pausing" and stopping["reason"] == "sync_yield"
        assert stopping["epoch"] == before["epoch"]
        assert sync.shared_waiting and not bridge.sync_entered.is_set()
        assert bridge.manager.acquire_run_slot() is False
        bridge.release_provider.set()
        assert bridge.sync_entered.wait(timeout=5)
        paused = bridge.env.db.get_work_analysis(bridge.env.run_id)
        assert paused["status"] == "paused" and paused["reason"] == "sync_yield"
        assert paused["active_seconds_debited"] < 30
        assert len(bridge.env.provider.sent_units) == 1
        bridge.release_sync.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "succeeded")
        assert bridge.starts == ["work_analysis", "bookmark", "work_analysis"]
        assert bridge.env.db.get_work_analysis(bridge.env.run_id)["request_count"] == 2
    finally:
        bridge.close()


def test_sync_yield_retries_progress_cas_before_worker_can_classify_or_settle(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    stop_resolution_waiting = threading.Event()
    settled = threading.Event()
    attempts = []
    original_stop = Database.request_work_analysis_stop
    original_settle = Database.settle_work_analysis
    original_scope = WorkAnalysisCoordinator.work_control_scope

    @contextmanager
    def observed_scope(self):
        job = self._job_for_run(bridge.env.run_id)
        if job is not None and self.manager.is_cancel_requested(job.job_id):
            stop_resolution_waiting.set()
        with original_scope(self):
            yield

    def progress_before_stop(self, run_id, action, expected_version):
        if action == "sync_yield":
            attempts.append(expected_version)
            if len(attempts) == 1:
                job = bridge.coordinator._job_for_run(run_id)
                assert bridge.manager.is_cancel_requested(job.job_id)
                with self.transaction() as conn:
                    conn.execute("UPDATE work_analysis_runs SET version=version+1 WHERE run_id=?", (run_id,))
                bridge.release_provider.set()
                assert stop_resolution_waiting.wait(timeout=5)
                assert not settled.is_set()
                assert bridge.manager.acquire_run_slot() is False
        return original_stop(self, run_id, action, expected_version)

    def checked_settle(self, lease, **kwargs):
        if not settled.is_set():
            assert self.get_work_analysis(lease.run_id)["reason"] == "sync_yield"
            settled.set()
        return original_settle(self, lease, **kwargs)

    monkeypatch.setattr(WorkAnalysisCoordinator, "work_control_scope", observed_scope)
    monkeypatch.setattr(Database, "request_work_analysis_stop", progress_before_stop)
    monkeypatch.setattr(Database, "settle_work_analysis", checked_settle)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        before = bridge.env.db.get_work_analysis(bridge.env.run_id, include_private=True)
        bridge.coordinator.request_sync(JobSpec(JobSource.WEB, ["bookmark"]), bridge.settings,
                                        "bookmark", "Synthetic conflict sync")
        assert bridge.sync_entered.wait(timeout=5)
        paused = bridge.env.db.get_work_analysis(bridge.env.run_id, include_private=True)
        assert attempts == [before["version"], before["version"] + 1]
        assert settled.is_set() and paused["status"] == "paused" and paused["reason"] == "sync_yield"
        assert paused["request_count"] == 1 and len(bridge.env.provider.sent_units) == 1
        bridge.release_sync.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "succeeded")
        assert bridge.starts == ["work_analysis", "bookmark", "work_analysis"]
        assert bridge.env.db.get_work_analysis(bridge.env.run_id)["request_count"] == 2
    finally:
        bridge.close()


@pytest.mark.parametrize("intervening", ["pause", "cancel", "delete", "new_owner"])
def test_internal_yield_retry_never_overwrites_stronger_control_or_new_owner(tmp_path, monkeypatch, intervening):
    bridge = make_bridge(tmp_path, monkeypatch)
    original_stop = Database.request_work_analysis_stop
    attempts = []

    def change_owner_or_intent(self, run_id, action, expected_version):
        if action == "sync_yield":
            attempts.append(expected_version)
            if intervening == "new_owner":
                with self.transaction() as conn:
                    conn.execute("UPDATE work_analysis_runs SET epoch=epoch+1,owner_token=?,version=version+1 "
                                 "WHERE run_id=?", ("e" * 64, run_id))
            else:
                original_stop(self, run_id, intervening, expected_version)
        return original_stop(self, run_id, action, expected_version)

    monkeypatch.setattr(Database, "request_work_analysis_stop", change_owner_or_intent)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        job = bridge.coordinator._job_for_run(bridge.env.run_id)
        assert bridge.coordinator.cancel_shared_job(job.job_id, action="sync_yield") is False
        control = bridge.env.db.get_work_analysis_control(bridge.env.run_id)
        assert len(attempts) == 1
        if intervening == "new_owner":
            assert control["owner_token"] == "e" * 64 and control["status"] == "running"
        elif intervening == "delete":
            assert control["deleted_at"] is not None
        else:
            assert control["reason"] == {"pause": "user_pause", "cancel": "user_cancel"}[intervening]
        bridge.release_provider.set()
        wait_for(lambda: job.status == JobStatus.CANCELLED)
        after = bridge.env.db.get_work_analysis_control(bridge.env.run_id)
        if intervening == "new_owner":
            assert (after["epoch"], after["owner_token"]) == (control["epoch"], control["owner_token"])
        assert bridge.starts == ["work_analysis"] and len(bridge.env.provider.sent_units) == 1
    finally:
        bridge.close()


def test_public_control_version_conflict_remains_single_attempt_strict_cas(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    original_stop = Database.request_work_analysis_stop
    attempts = []

    def progress_before_stop(self, run_id, action, expected_version):
        attempts.append(expected_version)
        with self.transaction() as conn:
            conn.execute("UPDATE work_analysis_runs SET version=version+1 WHERE run_id=?", (run_id,))
        return original_stop(self, run_id, action, expected_version)

    monkeypatch.setattr(Database, "request_work_analysis_stop", progress_before_stop)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        before = bridge.env.db.get_work_analysis(bridge.env.run_id)
        with pytest.raises(WorkAnalysisError) as caught:
            bridge.coordinator.request_control(bridge.env.run_id, "pause", before["version"])
        assert caught.value.code == "version_conflict" and attempts == [before["version"]]
        assert bridge.env.db.get_work_analysis(bridge.env.run_id)["reason"] is None
        bridge.release_provider.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "cancelled")
    finally:
        bridge.close()


def test_internal_yield_retry_is_bounded_and_never_acknowledges_failed_persistence(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    original_stop = Database.request_work_analysis_stop
    attempts = []

    def always_conflict(self, run_id, action, expected_version):
        attempts.append(expected_version)
        with self.transaction() as conn:
            conn.execute("UPDATE work_analysis_runs SET version=version+1 WHERE run_id=?", (run_id,))
        return original_stop(self, run_id, action, expected_version)

    monkeypatch.setattr(Database, "request_work_analysis_stop", always_conflict)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        job = bridge.coordinator._job_for_run(bridge.env.run_id)
        with pytest.raises(WorkAnalysisError) as caught:
            bridge.coordinator.cancel_shared_job(job.job_id, action="sync_yield")
        assert caught.value.code == "control_unavailable" and len(attempts) == 3
        assert bridge.manager.is_cancel_requested(job.job_id)
        bridge.release_provider.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "cancelled")
        assert bridge.starts == ["work_analysis"] and len(bridge.env.provider.sent_units) == 1
    finally:
        bridge.close()


@pytest.mark.parametrize("action,expected", [("pause", "paused"), ("cancel", "cancelled")])
def test_user_stop_is_never_automatically_requeued(tmp_path, monkeypatch, action, expected):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        state = bridge.env.db.get_work_analysis(bridge.env.run_id)
        bridge.coordinator.request_control(bridge.env.run_id, action, state["version"])
        bridge.release_provider.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == expected)
        bridge.coordinator.wake("completion")
        assert bridge.starts == ["work_analysis"]
        assert bridge.env.db.get_work_analysis(bridge.env.run_id)["request_count"] == 1
    finally:
        bridge.close()


def test_generic_cancel_persists_parent_intent_before_acknowledgement(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        job = bridge.manager.shared_admission_snapshot()[0][0]
        assert bridge.coordinator.cancel_shared_job(job.job_id)
        state = bridge.env.db.get_work_analysis(bridge.env.run_id)
        assert state["status"] == "cancelling" and state["reason"] == "user_cancel"
        bridge.release_provider.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "cancelled")
    finally:
        bridge.close()


def test_stop_persistence_failure_keeps_memory_cancel_and_cannot_publish(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        state = bridge.env.db.get_work_analysis(bridge.env.run_id)
        original = Database.request_work_analysis_stop

        def disk_failure(self, run_id, action, expected_version):
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(Database, "request_work_analysis_stop", disk_failure)
        with pytest.raises(WorkAnalysisError) as caught:
            bridge.coordinator.request_control(bridge.env.run_id, "pause", state["version"])
        assert caught.value.code == "control_unavailable"
        job = bridge.manager.shared_admission_snapshot()[0][0]
        assert bridge.manager.is_cancel_requested(job.job_id)
        monkeypatch.setattr(Database, "request_work_analysis_stop", original)
        bridge.release_provider.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "cancelled")
        assert bridge.env.db.get_work_analysis(bridge.env.run_id)["has_report"] is False
    finally:
        bridge.close()


def test_finalization_first_refuses_both_business_and_generic_cancellation(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    checked = []
    original = Database.publish_work_analysis

    def try_cancel_at_publication(self, lease, artifact):
        state = bridge.env.db.get_work_analysis(lease.run_id)
        with pytest.raises(WorkAnalysisError) as caught:
            bridge.coordinator.request_control(lease.run_id, "cancel", state["version"])
        assert caught.value.code == "finalizing"
        job = bridge.manager.shared_admission_snapshot()[0][0]
        assert bridge.coordinator.cancel_shared_job(job.job_id) is False
        checked.append(True)
        return original(self, lease, artifact)

    monkeypatch.setattr(Database, "publish_work_analysis", try_cancel_at_publication)
    try:
        bridge.release_provider.set()
        bridge.coordinator.wake("created")
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "succeeded")
        assert checked == [True]
    finally:
        bridge.close()


def test_startup_recovery_runs_once_and_never_activates_old_intent(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch)
    manager = JobManager()
    scheduler = AutoSyncScheduler(None, None, shared_job_manager=manager)
    calls = []
    coordinator = WorkAnalysisCoordinator(manager, lambda *a, **k: calls.append("submit"),
                                          lambda job: calls.append("run"), lambda: SimpleNamespace(),
                                          db_factory=env.db_factory, scheduler=scheduler)
    try:
        assert coordinator.startup_recover() == 1
        state = env.db.get_work_analysis(env.run_id)
        assert state["status"] == "paused" and state["reason"] == "process_interrupted"
        assert coordinator.startup_recover() == 0
        assert calls == [] and manager.latest_job() is None
    finally:
        coordinator.close()
        env.close()


def test_fresh_manager_restart_repairs_only_latest_published_log_without_resending(tmp_path, monkeypatch):
    class SyntheticProcessExit(BaseException):
        pass

    env = make_runtime_environment(tmp_path, monkeypatch)
    old_manager = JobManager()
    executor = WorkAnalysisExecutor(env.db_factory, lambda: env.service)
    runner = JobRunner(old_manager, lambda task, context: executor.run(env.run_id, context))
    fresh_coordinator = None

    def submit_slice():
        job = old_manager.submit(JobSpec(JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS,
                                         {"run_id": env.run_id}))
        log_id = env.db.create_task_log("work_analysis", "Synthetic activation", job_id=job.job_id)
        old_manager.update_progress(job.job_id, log_id=log_id)
        return job, log_id

    try:
        old_job, old_log = submit_slice()

        def cancel_first_slice(unit_id):
            state = env.db.get_work_analysis(env.run_id)
            assert old_manager.request_cancel(old_job.job_id)
            env.db.request_work_analysis_stop(env.run_id, "cancel", state["version"])

        env.provider.before_response = cancel_first_slice
        runner.run(old_job.job_id)
        assert old_job.status == JobStatus.CANCELLED
        env.db.update_task_log(old_log, "cancelled")
        old_row = dict(env.db.conn.execute("SELECT * FROM task_logs WHERE id=?", (old_log,)).fetchone())
        stopped = env.db.get_work_analysis(env.run_id)
        env.db.resume_work_analysis(env.run_id, stopped["version"])
        env.provider.before_response = None
        latest_job, latest_log = submit_slice()

        def crash_before_settlement(self, lease, **kwargs):
            assert self.get_work_analysis(lease.run_id)["status"] == "succeeded"
            raise SyntheticProcessExit()

        with monkeypatch.context() as crash_patch:
            crash_patch.setattr(Database, "settle_work_analysis", crash_before_settlement)
            with pytest.raises(SyntheticProcessExit):
                runner.run(latest_job.job_id)
        before = env.db.get_work_analysis(env.run_id, include_private=True)
        assert before["shared_job_id"] == latest_job.job_id and before["shared_log_id"] == latest_log
        assert before["shared_epoch"] == before["epoch"] and before["owner_token"] is not None
        assert env.db.conn.execute("SELECT status FROM task_logs WHERE id=?", (latest_log,)).fetchone()[0] == "running"
        duplicate = env.db.create_task_log("work_analysis", "Unbound duplicate", job_id=latest_job.job_id)
        unrelated = env.db.create_task_log("bookmark", "Unrelated sync", job_id="f" * 32)
        sends_before = list(env.provider.sent_units)
        calls = []
        fresh_manager = JobManager()
        fresh_scheduler = AutoSyncScheduler(None, None, shared_job_manager=fresh_manager)
        fresh_coordinator = WorkAnalysisCoordinator(
            fresh_manager, lambda *a, **k: calls.append("submit"), lambda job: calls.append("run"),
            lambda: SimpleNamespace(), db_factory=env.db_factory, scheduler=fresh_scheduler,
        )
        assert fresh_coordinator.startup_recover() == 1
        after = env.db.get_work_analysis(env.run_id, include_private=True)
        latest_row = dict(env.db.conn.execute("SELECT * FROM task_logs WHERE id=?", (latest_log,)).fetchone())
        assert latest_row["status"] == "succeeded" and latest_row["error_message"] is None
        assert latest_row["finished_at"] == after["completed_at"]
        assert latest_row["stats_json"] is None and latest_row["logs_json"] is None
        assert dict(env.db.conn.execute("SELECT * FROM task_logs WHERE id=?", (old_log,)).fetchone()) == old_row
        for log_id in (duplicate, unrelated):
            assert env.db.conn.execute("SELECT status FROM task_logs WHERE id=?", (log_id,)).fetchone()[0] == "failed"
        assert after["status"] == "succeeded" and after["report"] == before["report"]
        assert after["owner_token"] is None and after["slice_settled"]
        assert after["active_seconds_debited"] == before["active_seconds_debited"]
        assert after["request_count"] == before["request_count"] == len(sends_before)
        assert fresh_coordinator.startup_recover() == 0
        assert fresh_coordinator.wake("completion") is False
        assert fresh_manager.latest_job() is None and calls == []
        assert env.provider.sent_units == sends_before and env.db.queued_work_analysis_ids() == []
    finally:
        if fresh_coordinator is not None:
            fresh_coordinator.close()
        env.close()


def test_failed_slot_reservation_retries_same_shared_item_on_release(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        assert bridge.manager.acquire_run_slot()
        bridge.coordinator.wake("created")
        active, waiting = bridge.manager.shared_admission_snapshot()
        assert active == [] and len(waiting) == 1
        assert bridge.threads == []
        original_id = waiting[0].job_id
        bridge.release_provider.set()
        bridge.manager.release_run_slot()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "succeeded")
        wait_for(lambda: bridge.manager.get_job(original_id).status == JobStatus.SUCCEEDED)
        assert bridge.manager.get_job(original_id).status == JobStatus.SUCCEEDED
        assert len(bridge.manager._jobs) == 1
    finally:
        bridge.close()


def test_manual_work_watcher_yields_to_new_due_sync_even_after_many_yields(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch, auto_enabled=True)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        bridge.scheduler._task_preempt_streak["work_analysis"] = 100
        bridge.scheduler._task_preempted_at["work_analysis"] = time.time()
        bridge.scheduler._task_next_run["bookmarks"] = time.time() - 1
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "pausing")
        assert bridge.env.db.get_work_analysis(bridge.env.run_id)["reason"] == "sync_yield"
        bridge.release_provider.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "paused")
        assert bridge.starts == ["work_analysis"]
        assert bridge.scheduler._task_next_run["bookmarks"] < time.time()
        # No scheduler thread runs in this fixture. Clear the synthetic due
        # entry to model its completion; the ordinary completion wake resumes.
        bridge.scheduler._task_next_run.clear()
        bridge.coordinator.wake("completion")
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "succeeded")
    finally:
        bridge.close()


def test_late_old_shared_completion_cannot_promote_its_cancelled_slice(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        old = bridge.manager.shared_admission_snapshot()[0][0]
        bridge.coordinator.request_sync(JobSpec(JobSource.WEB, ["bookmark"]), bridge.settings,
                                        "bookmark", "Synthetic sync")
        bridge.release_provider.set()
        assert bridge.sync_entered.wait(timeout=5)
        bridge.release_sync.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "succeeded")
        bridge.coordinator.on_job_finished(old.job_id)
        assert old.status == JobStatus.CANCELLED
        assert bridge.env.db.get_work_analysis_report(bridge.env.run_id)["synthesis_complete"] is True
    finally:
        bridge.close()


def test_committed_report_repairs_latest_shared_projection_without_another_call(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        bridge.release_provider.set()
        bridge.coordinator.wake("created")
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "succeeded")
        for thread in list(bridge.threads):
            thread.join(timeout=5)
            assert not thread.is_alive()
        latest = bridge.manager.latest_job()
        before_calls = list(bridge.env.provider.sent_units)
        bridge.manager.mark_failed(latest.job_id, "synthetic_memory_projection_failure")
        bridge.coordinator.on_job_finished(latest.job_id)
        assert latest.status == JobStatus.SUCCEEDED
        assert bridge.env.provider.sent_units == before_calls
    finally:
        bridge.close()


def test_cancelled_waiting_parent_allows_next_explicit_intent_without_auto_scheduler(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        assert bridge.manager.acquire_run_slot()
        bridge.coordinator.wake("created")
        state = bridge.env.db.get_work_analysis(bridge.env.run_id)
        stopped = bridge.coordinator.request_control(bridge.env.run_id, "cancel", state["version"])
        assert stopped["status"] == "cancelled"
        assert bridge.manager.shared_admission_snapshot() == ([], [])
        bridge.manager.release_run_slot()
        assert bridge.env.provider.sent_units == []
    finally:
        bridge.close()


def test_expired_owner_sweep_pauses_without_paid_replay_when_auto_sync_is_off(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        lease = bridge.env.db.claim_work_analysis(bridge.env.run_id, now=datetime.now(timezone.utc), slice_seconds=30)
        before = bridge.env.db.get_work_analysis(bridge.env.run_id, include_private=True)
        with bridge.env.db.transaction() as conn:
            conn.execute("UPDATE work_analysis_runs SET lease_until='2000-01-01 00:00:00' WHERE run_id=?",
                         (bridge.env.run_id,))
        bridge.coordinator.wake("completion")
        state = bridge.env.db.get_work_analysis(bridge.env.run_id, include_private=True)
        assert state["status"] == "paused" and state["reason"] == "process_interrupted"
        assert state["active_seconds_debited"] == before["active_seconds_debited"]
        assert state["epoch"] > lease.epoch and state["owner_token"] is None
        assert bridge.env.provider.sent_units == [] and bridge.threads == []
    finally:
        bridge.close()

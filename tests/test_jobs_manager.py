from __future__ import annotations

from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus


def test_submit_creates_queued_job():
    manager = JobManager(max_logs=3, max_jobs=10)
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))

    assert state.job_id
    assert state.status == JobStatus.QUEUED
    assert manager.get_job(state.job_id) is state


def test_add_log_trims_old_entries():
    manager = JobManager(max_logs=2, max_jobs=10)
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))

    manager.add_log(state.job_id, "info", "one")
    manager.add_log(state.job_id, "info", "two")
    manager.add_log(state.job_id, "info", "three")

    assert [entry.message for entry in state.logs] == ["two", "three"]


def test_update_progress_updates_message_when_provided():
    manager = JobManager(max_logs=3, max_jobs=10)
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))

    manager.update_progress(state.job_id, phase="准备", message="正在准备")

    assert state.progress["phase"] == "准备"
    assert state.message == "正在准备"


def test_request_cancel_marks_queued_job_cancel_requested():
    manager = JobManager(max_logs=3, max_jobs=10)
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))

    assert manager.request_cancel(state.job_id) is True
    assert state.status == JobStatus.CANCEL_REQUESTED


def test_mark_running_does_not_override_cancel_request():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))
    manager.request_cancel(state.job_id)

    assert manager.mark_running(state.job_id) is False
    assert state.status == JobStatus.CANCEL_REQUESTED


def test_cleanup_old_jobs_keeps_recent_completed_jobs():
    manager = JobManager(max_logs=3, max_jobs=2)
    first = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))
    second = manager.submit(JobSpec(source=JobSource.WEB, task_types=["following_users"]))
    third = manager.submit(JobSpec(source=JobSource.WEB, task_types=["following_novels"]))
    manager.mark_succeeded(first.job_id)
    manager.mark_succeeded(second.job_id)
    manager.mark_running(third.job_id)

    manager.cleanup_old_jobs()

    assert manager.get_job(first.job_id) is None
    assert manager.get_job(second.job_id) is second
    assert manager.get_job(third.job_id) is third


def test_mark_methods_update_state():
    manager = JobManager(max_logs=3, max_jobs=10)
    running = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))
    succeeded = manager.submit(JobSpec(source=JobSource.WEB, task_types=["following_users"]))
    failed = manager.submit(JobSpec(source=JobSource.WEB, task_types=["following_novels"]))
    cancelled = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))

    assert manager.mark_running(running.job_id, "syncing") is True
    assert running.status == JobStatus.RUNNING
    assert running.message == "syncing"
    assert running.started_at is not None

    assert manager.mark_succeeded(succeeded.job_id, "done") is True
    assert succeeded.status == JobStatus.SUCCEEDED
    assert succeeded.message == "done"
    assert succeeded.finished_at is not None

    assert manager.mark_failed(failed.job_id, "boom", "failed during sync") is True
    assert failed.status == JobStatus.FAILED
    assert failed.error == "boom"
    assert failed.message == "failed during sync"
    assert failed.finished_at is not None

    assert manager.mark_cancelled(cancelled.job_id, "stopped") is True
    assert cancelled.status == JobStatus.CANCELLED
    assert cancelled.message == "stopped"
    assert cancelled.finished_at is not None

    assert manager.mark_running("missing") is False
    assert manager.mark_succeeded("missing") is False
    assert manager.mark_failed("missing", "boom") is False
    assert manager.mark_cancelled("missing") is False


def test_cancel_request_flag():
    manager = JobManager(max_logs=3, max_jobs=10)
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))

    assert manager.is_cancel_requested(state.job_id) is False
    assert manager.request_cancel(state.job_id) is True
    assert manager.is_cancel_requested(state.job_id) is True
    assert manager.is_cancel_requested("missing") is False


def test_run_slot_allows_only_one_holder():
    manager = JobManager(max_logs=3, max_jobs=10)

    assert manager.acquire_run_slot() is True
    assert manager.acquire_run_slot() is False
    manager.release_run_slot()
    assert manager.acquire_run_slot() is True
    manager.release_run_slot()


def test_run_slot_rejects_over_release():
    manager = JobManager(max_logs=3, max_jobs=10)

    assert manager.acquire_run_slot() is True
    manager.release_run_slot()
    try:
        manager.release_run_slot()
    except ValueError:
        pass
    else:
        raise AssertionError("expected over-release to raise ValueError")


def test_finalization_claim_blocks_cancel_and_finishes_last_task_atomically():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))
    assert manager.mark_running(state.job_id) is True

    claim = manager.try_begin_finalization(state.job_id)

    assert claim is not None
    assert manager.request_cancel(state.job_id) is False
    assert claim.finish({"rescue_catalog_items": 2}, is_last_task=True) is True
    assert state.status == JobStatus.SUCCEEDED
    assert state.stats["rescue_catalog_items"] == 2
    assert state.finished_at is not None
    assert manager.request_cancel(state.job_id) is False


def test_cancel_before_finalization_prevents_claim():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))
    assert manager.mark_running(state.job_id) is True
    assert manager.request_cancel(state.job_id) is True

    assert manager.try_begin_finalization(state.job_id) is None
    assert state.status == JobStatus.CANCEL_REQUESTED


def test_aborted_finalization_accepts_cancel_again():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))
    assert manager.mark_running(state.job_id) is True
    claim = manager.try_begin_finalization(state.job_id)
    assert claim is not None

    assert claim.abort() is True
    assert manager.request_cancel(state.job_id) is True


def test_non_last_finalization_releases_cancel_gate():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark", "novel_status"]))
    assert manager.mark_running(state.job_id) is True
    claim = manager.try_begin_finalization(state.job_id)
    assert claim is not None

    assert claim.finish({"novels": 1}, is_last_task=False) is True
    assert state.status == JobStatus.RUNNING
    assert state.stats["novels"] == 1
    assert manager.request_cancel(state.job_id) is True


def test_stale_finalization_abort_cannot_clear_new_claim():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["a", "b"]))
    assert manager.mark_running(state.job_id) is True

    first = manager.try_begin_finalization(state.job_id)
    assert first is not None
    assert first.finish({"a": 1}, is_last_task=False) is True

    second = manager.try_begin_finalization(state.job_id)
    assert second is not None
    assert first.abort() is False
    assert manager.request_cancel(state.job_id) is False
    assert second.abort() is True
    assert manager.request_cancel(state.job_id) is True

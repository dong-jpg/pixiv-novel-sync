from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.jobs import quick_sync, services
from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus
from pixiv_novel_sync.jobs.runner import JobRunner
from pixiv_novel_sync.jobs.tasks import execute_task
from pixiv_novel_sync.settings import PixivSettings, Settings, StorageSettings, SyncSettings
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService
from pixiv_novel_sync.webapp import _task_log_status_for_stats


@pytest.fixture
def backup_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        pixiv=PixivSettings(
            refresh_token="", access_token=None, proxy=None,
            timeout=30, verify_ssl=True, user_id=None,
        ),
        sync=SyncSettings(
            enabled=True, initial_manual_only=False, download_assets=False,
            write_markdown=True, write_raw_text=True, bookmark_restricts=["public"],
            max_items_per_run=None, max_pages_per_run=None,
            delay_seconds_between_items=0, delay_seconds_between_pages=0,
        ),
        storage=StorageSettings(
            public_dir=tmp_path / "public", private_dir=tmp_path / "private",
            db_path=tmp_path / "backup.db",
        ),
    )
    db = Database(settings.storage.db_path)
    try:
        db.init_schema()
        db.conn.execute("INSERT INTO users(user_id, name, raw_json) VALUES(1, 'test', '{}')")
    finally:
        db.close()
    case = SimpleNamespace(
        settings=settings, failed_ids=set(), novel_count=5, visited=[],
        stopped=False, cancel_id=None, interrupt=False, on_cancel=None,
        logs=[], rebuilds=[],
    )
    api = SimpleNamespace(
        user_novels=lambda **kwargs: SimpleNamespace(
            novels=[SimpleNamespace(id=n, title=str(n)) for n in range(1, case.novel_count + 1)],
            next_url=None,
        ),
        parse_qs=lambda value: None,
    )

    def sync_inner(self, novel_id, novel, restrict, download_assets,
                   write_markdown, write_raw_text, source_type, source_key):
        case.visited.append((int(source_key), novel_id))
        if novel_id == case.cancel_id:
            case.stopped = True
            if case.on_cancel is not None:
                case.on_cancel()
            if case.interrupt:
                raise InterruptedError("Task stopped by user")
        if novel_id in case.failed_ids:
            raise RuntimeError("simulated transient novel failure")
        return {"novels": 1, "skipped": 0, "assets_downloaded": 0}

    def rebuild(db, reporter=None):
        case.rebuilds.append(True)
        return {}

    # Only isolate network/content I/O and derived-catalog work. Keep the real
    # error wrapper, user backup, scheduled aggregation, database and Runner.
    monkeypatch.setattr(services, "_login", lambda settings: api)
    monkeypatch.setattr(BookmarkNovelSyncService, "_sync_novel_inner", sync_inner)
    monkeypatch.setattr(services, "_rebuild_rescue_catalog", rebuild)
    monkeypatch.setattr(quick_sync, "_rebuild_rescue_catalog", rebuild)
    case.reporter = SimpleNamespace(
        add_log=lambda level, message: case.logs.append((level, message)),
        update_progress=lambda **kwargs: None,
    )
    return case


def run_backup(case, entry):
    kwargs = {"reporter": case.reporter, "stop_requested": lambda: case.stopped}
    if entry == "direct":
        return services.run_user_backup_task(case.settings, 1, **kwargs)
    return quick_sync.run_scheduled_user_backup(case.settings, **kwargs)


@pytest.mark.parametrize("entry", ["direct", "scheduled"])
def test_four_successes_then_one_failure_marks_backup_partial(backup_case, entry):
    backup_case.failed_ids = {5}
    result = run_backup(backup_case, entry)

    assert result["novels"] == 4
    assert result.get("failed", 0) == 1
    assert result["incomplete"] is True
    assert result["aborted_reason"] == "novel_backup_errors"
    assert result["stopped"] is False
    assert result.get("failed_users", 0) == 0
    assert _task_log_status_for_stats(result) == "partial"
    assert backup_case.logs[-1][0] == "warning"
    assert backup_case.visited == [(1, n) for n in range(1, 6)]


@pytest.mark.parametrize("entry", ["direct", "scheduled"])
def test_all_successful_novels_keep_successful_terminal_state(backup_case, entry):
    result = run_backup(backup_case, entry)

    assert result["novels"] == 5
    assert result.get("failed", 0) == 0
    assert not result.get("incomplete")
    assert not result.get("aborted_reason")
    assert result["stopped"] is False
    assert _task_log_status_for_stats(result) == "succeeded"
    assert backup_case.logs[-1][0] == "success"


def test_scheduled_backup_sums_failed_novels_across_users(backup_case):
    db = Database(backup_case.settings.storage.db_path)
    try:
        db.conn.execute("INSERT INTO users(user_id, name, raw_json) VALUES(2, 'test', '{}')")
    finally:
        db.close()
    backup_case.failed_ids = {5}

    result = run_backup(backup_case, "scheduled")

    assert result["novels"] == 8
    assert result.get("failed", 0) == 2
    assert result["failed_users"] == 0
    assert result["incomplete"] is True
    assert result["aborted_reason"] == "novel_backup_errors"
    assert _task_log_status_for_stats(result) == "partial"


@pytest.mark.parametrize("entry", ["direct", "scheduled"])
def test_cancel_before_backup_does_not_count_failure(backup_case, entry):
    backup_case.stopped = True
    result = run_backup(backup_case, entry)

    assert result["stopped"] is True
    assert result.get("failed", 0) == 0
    assert result.get("failed_users", 0) == 0
    assert not result.get("incomplete")
    assert backup_case.visited == []
    assert backup_case.rebuilds == []


@pytest.mark.parametrize("entry", ["direct", "scheduled"])
@pytest.mark.parametrize("interrupt", [False, True])
def test_real_runner_cancellation_wins_over_backup_completion(backup_case, entry, interrupt):
    manager = JobManager()
    task_type = "user_backup:1" if entry == "direct" else "user_backup"
    job = manager.submit(JobSpec(source=JobSource.CLI, task_types=[task_type]))
    backup_case.cancel_id = 4
    backup_case.interrupt = interrupt
    backup_case.on_cancel = lambda: manager.request_cancel(job.job_id)

    result = JobRunner(
        manager, lambda kind, context: execute_task(kind, backup_case.settings, context),
    ).run(job.job_id)

    assert result.status == JobStatus.CANCELLED
    assert result.stats.get("failed", 0) == 0
    assert result.stats.get("failed_users", 0) == 0
    assert backup_case.visited == [(1, n) for n in range(1, 5)]
    assert backup_case.rebuilds == []


@pytest.mark.parametrize("entry", ["direct", "scheduled"])
def test_cancelled_backup_keeps_prior_nonfatal_failure_count(backup_case, entry):
    manager = JobManager()
    task_type = "user_backup:1" if entry == "direct" else "user_backup"
    job = manager.submit(JobSpec(source=JobSource.CLI, task_types=[task_type]))
    backup_case.failed_ids = {5}
    backup_case.novel_count = 6
    backup_case.cancel_id = 6
    backup_case.on_cancel = lambda: manager.request_cancel(job.job_id)

    result = JobRunner(
        manager, lambda kind, context: execute_task(kind, backup_case.settings, context),
    ).run(job.job_id)

    assert result.status == JobStatus.CANCELLED
    assert result.stats.get("failed", 0) == 1
    assert result.stats["incomplete"] is True
    assert result.stats.get("failed_users", 0) == 0
    assert backup_case.rebuilds == []


@pytest.mark.parametrize("entry", ["direct", "scheduled"])
@pytest.mark.parametrize(
    ("novel_count", "failed_ids", "expected_failure"),
    [(5, {1}, "1/1"), (50, set(range(41, 51)), "10/50")],
    ids=["ratio-threshold", "absolute-threshold"],
)
def test_existing_failure_thresholds_still_abort_user(
    backup_case, entry, novel_count, failed_ids, expected_failure,
):
    backup_case.novel_count = novel_count
    backup_case.failed_ids = failed_ids
    if entry == "direct":
        with pytest.raises(RuntimeError, match=expected_failure + " novels failed"):
            run_backup(backup_case, entry)
    else:
        result = run_backup(backup_case, entry)
        assert result["failed_users"] == 1
        assert result["incomplete"] is True
        assert result["aborted_reason"] == "user_backup_errors"
        assert result["stopped"] is False
        assert _task_log_status_for_stats(result) == "partial"
    assert len(backup_case.visited) == int(expected_failure.split("/")[1])
    assert backup_case.rebuilds == []

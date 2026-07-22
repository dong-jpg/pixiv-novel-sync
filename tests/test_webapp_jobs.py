from __future__ import annotations

from contextlib import nullcontext
import threading
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.jobs.models import JobSource, JobStatus, JobType
from pixiv_novel_sync.storage_db import Database as RealDatabase
from pixiv_novel_sync.webapp import AutoSyncScheduler, SyncJobManager, SyncJobState, _web_job_spec, create_app


def _disabled_scheduler_settings(tmp_path):
    return SimpleNamespace(
        storage=SimpleNamespace(db_path=tmp_path / "scheduler.db"),
        sync=SimpleNamespace(auto_sync_enabled=False, auto_sync_timezone="UTC"),
    )


def test_web_job_spec_for_sync_tasks():
    spec = _web_job_spec(["bookmark", "following_novels"])

    assert spec.source == JobSource.WEB
    assert spec.job_type == JobType.SYNC
    assert spec.task_types == ["bookmark", "following_novels"]


def test_web_job_spec_for_user_backup():
    spec = _web_job_spec(["user_backup:123"])

    assert spec.source == JobSource.WEB
    assert spec.job_type == JobType.USER_BACKUP
    assert spec.params["user_id"] == 123


def test_auto_sync_scheduler_delegates_status_and_pending_detection_services(monkeypatch):
    calls = []

    def fake_user_status(settings, reporter=None, stop_requested=None):
        calls.append(("user_status", settings, reporter, stop_requested))
        return {"checked_count": 1}

    def fake_novel_status(
        settings,
        reporter=None,
        stop_requested=None,
        claim_finalization=None,
    ):
        calls.append(("novel_status", settings, reporter, stop_requested))
        return {"checked_count": 2}

    def fake_series_status(
        settings,
        reporter=None,
        stop_requested=None,
        claim_finalization=None,
    ):
        calls.append(("series_status", settings, reporter, stop_requested))
        return {"checked_count": 3}

    def fake_pending_detection(settings, reporter=None, stop_requested=None):
        calls.append(("pending_detection", settings, reporter, stop_requested))
        return {"new_pending": 4}

    class Manager:
        def __init__(self):
            self.cancelled = False

        def add_log(self, job_id, level, message):
            pass

        def update_progress(self, job_id, **kwargs):
            pass

        def is_cancel_requested(self, job_id):
            assert job_id == "job-1"
            return self.cancelled

    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_user_status_task", fake_user_status)
    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_novel_status_task", fake_novel_status)
    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_series_status_task", fake_series_status)
    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_pending_deletion_detection_task", fake_pending_detection)
    manager = Manager()
    scheduler = AutoSyncScheduler(config_path=None, env_path=None, sync_job_manager=manager)
    scheduler._running = True
    settings = object()

    scheduler._sync_user_status(settings, "job-1")
    novel_result = scheduler._sync_novel_status(settings, "job-1")
    series_result = scheduler._sync_series_status(settings, "job-1")
    scheduler._sync_pending_detection(settings, "job-1")

    assert [call[0] for call in calls] == ["user_status", "novel_status", "series_status", "pending_detection"]
    assert all(call[1] is settings for call in calls)
    assert all(call[2] is not None for call in calls)
    assert all(call[2].manager is manager for call in calls)
    assert all(call[2].job_id == "job-1" for call in calls)
    assert all(call[3] is not None for call in calls)
    assert all(call[3]() is False for call in calls)
    manager.cancelled = True
    assert all(call[3]() is True for call in calls)
    assert novel_result == {"checked_count": 2}
    assert series_result == {"checked_count": 3}


def test_auto_sync_scheduler_delegates_user_backup_to_each_user(monkeypatch):
    calls = []

    def fake_user_backup(
        settings,
        user_id,
        reporter=None,
        stop_requested=None,
        *,
        rebuild_catalog=True,
    ):
        calls.append((settings, user_id, reporter, stop_requested, rebuild_catalog))
        return {"user_id": user_id, "novels": 1, "stopped": user_id == 20}

    class Manager:
        def add_log(self, job_id, level, message):
            pass

        def update_progress(self, job_id, **kwargs):
            pass

        def is_cancel_requested(self, job_id):
            assert job_id == "job-1"
            return False

    class Sync:
        auto_sync_following_novels_users_limit = 0

    class Settings:
        sync = Sync()

    class Conn:
        def execute(self, sql):
            assert "SELECT user_id FROM users ORDER BY user_id" in sql
            return self

        def fetchall(self):
            return [(10,), (20,)]

    class FakeDatabase:
        conn = Conn()
        rebuild_catalog_calls = 0

        def __init__(self, db_path):
            assert str(db_path) == "db.sqlite"

        def init_schema(self):
            pass

        def get_watermark(self, key):
            assert key == "user_backup_rotation"
            return {"offset": 0}

        def update_watermark(self, key, data):
            assert key == "user_backup_rotation"

        def rebuild_rescue_catalog(self):
            type(self).rebuild_catalog_calls += 1
            return {"items": 2, "sources": 3}

        def close(self):
            pass

    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_user_backup_task", fake_user_backup)
    monkeypatch.setattr("pixiv_novel_sync.web.managers.Database", FakeDatabase)
    settings = Settings()
    settings.storage = type("Storage", (), {"db_path": "db.sqlite"})()
    scheduler = AutoSyncScheduler(config_path=None, env_path=None, sync_job_manager=Manager())
    scheduler._running = True

    result = scheduler._sync_user_backup(settings, "job-1")

    assert [call[1] for call in calls] == [10, 20]
    assert all(call[0] is settings for call in calls)
    assert all(call[2] is not None for call in calls)
    assert all(call[2].job_id == "job-1" for call in calls)
    assert all(call[3] is not None for call in calls)
    assert all(call[3]() is False for call in calls)
    assert all(call[4] is False for call in calls)
    assert FakeDatabase.rebuild_catalog_calls == 0
    assert result is not None and result["stopped"] is True


@pytest.mark.parametrize(
    ("user_ids", "cancel_after_batch"),
    [([10, 20], False), ([], False), ([10], True), ([], True)],
)
def test_auto_sync_scheduler_rebuilds_catalog_once_after_user_backup_batch(
    monkeypatch, user_ids, cancel_after_batch
):
    calls = []

    def fake_user_backup(
        settings,
        user_id,
        reporter=None,
        stop_requested=None,
        *,
        rebuild_catalog=True,
    ):
        calls.append((user_id, rebuild_catalog))
        if cancel_after_batch and user_id == user_ids[-1]:
            manager.cancelled = True
        return {"user_id": user_id, "novels": 1, "stopped": False}

    class Job:
        stats = None

    class Manager:
        job = Job()

        def __init__(self):
            self.cancelled = False

        def get_job(self, job_id):
            assert job_id == "job-1"
            return self.job

        def add_log(self, job_id, level, message):
            pass

        def update_progress(self, job_id, **kwargs):
            pass

        def is_cancel_requested(self, job_id):
            return self.cancelled

    class Conn:
        def execute(self, sql):
            return self

        def fetchall(self):
            return [(user_id,) for user_id in user_ids]

    class FakeDatabase:
        rebuild_catalog_calls = 0

        def __init__(self, db_path):
            self.conn = Conn()

        def init_schema(self):
            pass

        def get_watermark(self, key):
            return {"offset": 0}

        def update_watermark(self, key, data):
            pass

        def rebuild_rescue_catalog(self):
            type(self).rebuild_catalog_calls += 1
            return {"items": 2, "sources": 3}

        def close(self):
            pass

    settings = type(
        "Settings",
        (),
        {
            "storage": type("Storage", (), {"db_path": "db.sqlite"})(),
            "sync": type("Sync", (), {"auto_sync_following_novels_users_limit": 0})(),
        },
    )()
    manager = Manager()
    if cancel_after_batch and not user_ids:
        manager.cancelled = True
    scheduler = AutoSyncScheduler(config_path=None, env_path=None, sync_job_manager=manager)
    scheduler._running = True
    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_user_backup_task", fake_user_backup)
    monkeypatch.setattr("pixiv_novel_sync.web.managers.Database", FakeDatabase)
    monkeypatch.setattr("pixiv_novel_sync.jobs.services.perf_counter", lambda: 1.0)

    result = scheduler._sync_user_backup(settings, "job-1")

    assert calls == [(user_id, False) for user_id in user_ids]
    expected_stats = {
        "novels": len(user_ids),
        "skipped": 0,
        "assets_downloaded": 0,
        "stopped": cancel_after_batch,
    }
    if cancel_after_batch:
        assert FakeDatabase.rebuild_catalog_calls == 0
    else:
        assert FakeDatabase.rebuild_catalog_calls == 1
        expected_stats.update(
            rescue_catalog_items=2,
            rescue_catalog_sources=3,
            rescue_catalog_duration_ms=0,
        )
    assert manager.job.stats == expected_stats
    assert result == manager.job.stats


@pytest.mark.parametrize(
    ("stop_after_completed", "expected_users", "expected_offset"),
    [(1, [21], 21), (0, [], 20)],
    ids=["one-completed", "immediate-stop"],
)
def test_auto_sync_user_backup_watermark_tracks_only_completed_users(
    monkeypatch,
    stop_after_completed,
    expected_users,
    expected_offset,
):
    completed_users = []
    watermark_updates = []

    class Manager:
        def __init__(self):
            self.cancelled = stop_after_completed == 0

        def add_log(self, job_id, level, message):
            pass

        def update_progress(self, job_id, **kwargs):
            pass

        def is_cancel_requested(self, job_id):
            return self.cancelled

    manager = Manager()

    def fake_user_backup(
        settings,
        user_id,
        reporter=None,
        stop_requested=None,
        *,
        rebuild_catalog=True,
    ):
        completed_users.append(user_id)
        if len(completed_users) == stop_after_completed:
            manager.cancelled = True
        return {"user_id": user_id, "novels": 1, "stopped": False}

    class Conn:
        def execute(self, sql):
            assert "SELECT user_id FROM users ORDER BY user_id" in sql
            return self

        def fetchall(self):
            return [(user_id,) for user_id in range(1, 101)]

    class Database:
        conn = Conn()

        def __init__(self, db_path):
            pass

        def init_schema(self):
            pass

        def get_watermark(self, key):
            assert key == "user_backup_rotation"
            return {"offset": 20}

        def update_watermark(self, key, data):
            assert key == "user_backup_rotation"
            watermark_updates.append(data)

        def rebuild_rescue_catalog(self):
            raise AssertionError("a stopped batch must not rebuild the catalog")

        def close(self):
            pass

    settings = SimpleNamespace(
        storage=SimpleNamespace(db_path="db.sqlite"),
        sync=SimpleNamespace(auto_sync_following_novels_users_limit=10),
    )
    scheduler = AutoSyncScheduler(
        config_path=None,
        env_path=None,
        sync_job_manager=manager,
    )
    scheduler._running = True
    monkeypatch.setattr(
        "pixiv_novel_sync.jobs.services.run_user_backup_task",
        fake_user_backup,
    )
    monkeypatch.setattr("pixiv_novel_sync.web.managers.Database", Database)

    result = scheduler._sync_user_backup(settings, "job-1")

    assert completed_users == expected_users
    assert result is not None and result["stopped"] is True
    assert len(watermark_updates) == 1
    assert watermark_updates[0]["offset"] == expected_offset


def test_auto_sync_user_backup_empty_batch_log_has_no_inverted_range(monkeypatch):
    logs = []

    class Manager:
        def add_log(self, job_id, level, message):
            logs.append(message)

        def update_progress(self, job_id, **kwargs):
            pass

        def is_cancel_requested(self, job_id):
            return False

    class Conn:
        def execute(self, sql):
            return self

        def fetchall(self):
            return []

    class Database:
        conn = Conn()

        def __init__(self, db_path):
            pass

        def init_schema(self):
            pass

        def get_watermark(self, key):
            return None

        def update_watermark(self, key, data):
            pass

        def rebuild_rescue_catalog(self):
            return {"items": 0, "sources": 0}

        def close(self):
            pass

    settings = SimpleNamespace(
        storage=SimpleNamespace(db_path="db.sqlite"),
        sync=SimpleNamespace(auto_sync_following_novels_users_limit=10),
    )
    scheduler = AutoSyncScheduler(
        config_path=None,
        env_path=None,
        sync_job_manager=Manager(),
    )
    scheduler._running = True
    monkeypatch.setattr("pixiv_novel_sync.web.managers.Database", Database)

    scheduler._sync_user_backup(settings, "job-1")

    assert logs
    assert all("1-0/0" not in message for message in logs)


@pytest.mark.parametrize(
    ("method_name", "expected_key", "cancel_after", "cancel_before_service"),
    [
        ("_sync_bookmarks", "novels", False, False),
        ("_sync_following_novels", "novels", False, False),
        ("_sync_subscribed_series", "series_synced", False, False),
        ("_sync_bookmarks", "novels", True, False),
        ("_sync_following_novels", "novels", True, False),
        ("_sync_subscribed_series", "series_synced", True, False),
        ("_sync_following_novels", "novels", False, True),
    ],
)
def test_scheduler_legacy_sync_refreshes_catalog_once(
    method_name, expected_key, cancel_after, cancel_before_service, monkeypatch, tmp_path
):
    created = {}

    class Auth:
        def __init__(self, pixiv_settings):
            pass

        def login(self):
            return object(), SimpleNamespace(user_id=123)

    class Database:
        def __init__(self, db_path):
            self.rebuild_catalog_calls = 0
            created["db"] = self

        def init_schema(self):
            pass

        def rebuild_rescue_catalog(self):
            self.rebuild_catalog_calls += 1
            return {"items": 9, "sources": 10}

        def close(self):
            pass

    class Storage:
        def __init__(self, settings):
            pass

        def ensure_dirs(self, dirs):
            pass

    class Service:
        def __init__(self, api, db, storage, settings):
            if cancel_before_service:
                scheduler._stop_current_task = True

        def sync(self, **kwargs):
            return finish({"novels": 1})

        def sync_following_novels(self, **kwargs):
            return finish({"novels": 2})

        def sync_subscribed_series(self, **kwargs):
            return finish({"series_synced": 3})

    class Manager:
        def __init__(self):
            self.logs = []

        def add_log(self, job_id, level, message):
            self.logs.append((job_id, level, message))

        def update_progress(self, job_id, **kwargs):
            pass

        def is_cancel_requested(self, job_id):
            return False

    settings = SimpleNamespace(
        pixiv=SimpleNamespace(user_id=None),
        storage=SimpleNamespace(
            db_path=tmp_path / "test.db",
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
        ),
        sync=SimpleNamespace(
            bookmark_restricts=["public"],
            download_assets=True,
            write_markdown=True,
            write_raw_text=False,
            delay_seconds_between_pages=0,
            series_sync_limit=0,
        ),
    )
    manager = Manager()
    scheduler = AutoSyncScheduler(config_path=None, env_path=None, sync_job_manager=manager)

    def finish(stats):
        if cancel_after:
            scheduler._stop_current_task = True
        return stats

    scheduler._running = True
    monkeypatch.setattr("pixiv_novel_sync.web.managers.Database", Database)
    monkeypatch.setattr("pixiv_novel_sync.web.managers.FileStorage", Storage)
    monkeypatch.setattr("pixiv_novel_sync.auth.PixivAuthManager", Auth)
    monkeypatch.setattr("pixiv_novel_sync.sync_engine.BookmarkNovelSyncService", Service)
    monkeypatch.setattr("pixiv_novel_sync.jobs.services.perf_counter", lambda: 1.0)

    result = getattr(scheduler, method_name)(settings, "job-1")

    if cancel_before_service:
        assert result == {"stopped": True}
        assert created["db"].rebuild_catalog_calls == 0
        return
    assert result[expected_key] in {1, 2, 3}
    if cancel_after:
        assert result["stopped"] is True
        assert "rescue_catalog_items" not in result
        assert created["db"].rebuild_catalog_calls == 0
    else:
        assert result["rescue_catalog_items"] == 9
        assert result["rescue_catalog_sources"] == 10
        assert created["db"].rebuild_catalog_calls == 1
        assert any("救援目录刷新完成" in message for _job, _level, message in manager.logs)


def _following_novels_finalization_race(monkeypatch, tmp_path):
    service_entered = threading.Event()
    allow_service_return = threading.Event()
    rebuild_entered = threading.Event()
    allow_rebuild_return = threading.Event()

    class Auth:
        def __init__(self, pixiv_settings):
            pass

        def login(self):
            return object(), SimpleNamespace(user_id=123)

    class Database:
        rebuild_catalog_calls = 0

        def __init__(self, db_path):
            pass

        def init_schema(self):
            pass

        def create_task_log(self, **kwargs):
            return 1

        def update_task_log(self, log_id, status, **kwargs):
            pass

        def rebuild_rescue_catalog(self):
            type(self).rebuild_catalog_calls += 1
            rebuild_entered.set()
            if not allow_rebuild_return.wait(timeout=3):
                raise RuntimeError("test rebuild release timeout")
            return {"items": 1, "sources": 1}

        def close(self):
            pass

    class Storage:
        def __init__(self, settings):
            pass

        def ensure_dirs(self, dirs):
            pass

    class Service:
        def __init__(self, api, db, storage, settings):
            pass

        def sync_following_novels(self, **kwargs):
            service_entered.set()
            if not allow_service_return.wait(timeout=3):
                raise RuntimeError("test service release timeout")
            return {"novels": 1}

    settings = SimpleNamespace(
        pixiv=SimpleNamespace(user_id=None),
        storage=SimpleNamespace(
            db_path=tmp_path / "race.db",
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
        ),
        sync=SimpleNamespace(
            download_assets=True,
            write_markdown=True,
            write_raw_text=False,
        ),
    )
    manager = SyncJobManager(config_path=None, env_path=None)
    scheduler = AutoSyncScheduler(
        config_path=None,
        env_path=None,
        sync_job_manager=manager,
    )
    scheduler._running = True
    monkeypatch.setattr("pixiv_novel_sync.web.managers.Database", Database)
    monkeypatch.setattr("pixiv_novel_sync.web.managers.FileStorage", Storage)
    monkeypatch.setattr("pixiv_novel_sync.auth.PixivAuthManager", Auth)
    monkeypatch.setattr(
        "pixiv_novel_sync.sync_engine.BookmarkNovelSyncService",
        Service,
    )

    return SimpleNamespace(
        scheduler=scheduler,
        manager=manager,
        settings=settings,
        database_type=Database,
        service_entered=service_entered,
        allow_service_return=allow_service_return,
        rebuild_entered=rebuild_entered,
        allow_rebuild_return=allow_rebuild_return,
    )


def test_scheduler_cancel_before_catalog_finalization_claim_wins(monkeypatch, tmp_path):
    race = _following_novels_finalization_race(monkeypatch, tmp_path)
    race.allow_rebuild_return.set()
    worker = threading.Thread(
        target=race.scheduler._run_single_task,
        args=(race.settings, "following_novels", "_sync_following_novels"),
        daemon=True,
    )
    worker.start()
    try:
        assert race.service_entered.wait(timeout=3)
        assert race.scheduler.stop_current_task() is True
        race.allow_service_return.set()
        worker.join(timeout=3)
    finally:
        race.allow_service_return.set()
        race.allow_rebuild_return.set()
    assert not worker.is_alive()

    job = race.manager.latest_job()
    assert race.database_type.rebuild_catalog_calls == 0
    assert job is not None and job.status == "cancelled"


def test_scheduler_catalog_finalization_claim_rejects_late_cancel(monkeypatch, tmp_path):
    race = _following_novels_finalization_race(monkeypatch, tmp_path)
    race.allow_service_return.set()
    worker = threading.Thread(
        target=race.scheduler._run_single_task,
        args=(race.settings, "following_novels", "_sync_following_novels"),
        daemon=True,
    )
    worker.start()
    stop_accepted = None
    try:
        assert race.rebuild_entered.wait(timeout=3)
        stop_accepted = race.scheduler.stop_current_task()
    finally:
        race.allow_rebuild_return.set()
        worker.join(timeout=3)
    assert not worker.is_alive()

    job = race.manager.latest_job()
    assert stop_accepted is False
    assert race.database_type.rebuild_catalog_calls == 1
    assert job is not None and job.status == "succeeded"


def test_scheduler_initializes_missing_catalog_once_in_background_when_disabled(
    monkeypatch, tmp_path
):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    start_gate = threading.Event()
    caller_ids = []
    rebuild_thread_ids = []
    auth_calls = []

    class Database:
        rebuild_calls = 0

        def __init__(self, db_path):
            pass

        def init_schema(self):
            pass

        def transaction(self):
            return nullcontext()

        def get_rescue_catalog_meta(self):
            return None

        def rebuild_rescue_catalog(self):
            type(self).rebuild_calls += 1
            rebuild_thread_ids.append(threading.get_ident())
            entered.set()
            if not release.wait(timeout=3):
                raise RuntimeError("test release timeout")
            finished.set()
            return {"items": 1, "sources": 2}

        def close(self):
            pass

    class ForbiddenAuth:
        def __init__(self, *args, **kwargs):
            auth_calls.append(True)

    settings = _disabled_scheduler_settings(tmp_path)
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    scheduler._last_cleanup_time = float("inf")
    monkeypatch.setattr("pixiv_novel_sync.web.managers.load_settings", lambda *args: settings)
    monkeypatch.setattr("pixiv_novel_sync.web.managers.Database", Database)
    monkeypatch.setattr("pixiv_novel_sync.auth.PixivAuthManager", ForbiddenAuth)

    def call_start():
        caller_ids.append(threading.get_ident())
        start_gate.wait(timeout=3)
        scheduler.start()

    callers = [threading.Thread(target=call_start) for _ in range(4)]
    for caller in callers:
        caller.start()
    start_gate.set()

    worker = None
    try:
        for caller in callers:
            caller.join(timeout=3)
            assert not caller.is_alive()
        assert entered.wait(timeout=3)
        worker = scheduler._thread
        assert worker is not None
        scheduler.start()
        assert scheduler._thread is worker
        assert Database.rebuild_calls == 1
        assert rebuild_thread_ids[0] not in caller_ids
        assert auth_calls == []
        release.set()
        assert finished.wait(timeout=3)
    finally:
        release.set()
        scheduler.stop()
        if worker is not None:
            worker.join(timeout=3)
    assert worker is not None and not worker.is_alive()


def test_scheduler_initialization_serializes_across_database_instances(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "shared-scheduler.db"
    seed = RealDatabase(db_path)
    seed.init_schema()
    seed.close()

    first_rebuild_entered = threading.Event()
    second_transaction_attempted = threading.Event()

    class CountingDatabase(RealDatabase):
        rebuild_calls = 0
        rebuild_lock = threading.Lock()
        first_rebuild_thread_id = None

        def init_schema(self):
            pass

        def transaction(self):
            first_thread_id = type(self).first_rebuild_thread_id
            if (
                first_thread_id is not None
                and threading.get_ident() != first_thread_id
            ):
                second_transaction_attempted.set()
            return super().transaction()

        def rebuild_rescue_catalog(self):
            with type(self).rebuild_lock:
                type(self).rebuild_calls += 1
                is_first = type(self).rebuild_calls == 1
                if is_first:
                    type(self).first_rebuild_thread_id = threading.get_ident()
            if is_first:
                first_rebuild_entered.set()
                if not second_transaction_attempted.wait(timeout=3):
                    raise RuntimeError("second initialization did not attempt a transaction")
            return super().rebuild_rescue_catalog()

    settings = _disabled_scheduler_settings(tmp_path)
    settings.storage.db_path = db_path
    schedulers = [
        AutoSyncScheduler(config_path=None, env_path=None),
        AutoSyncScheduler(config_path=None, env_path=None),
    ]
    monkeypatch.setattr(
        "pixiv_novel_sync.web.managers.Database",
        CountingDatabase,
    )

    workers = [
        threading.Thread(
            target=scheduler._initialize_rescue_catalog,
            args=(settings,),
            daemon=True,
        )
        for scheduler in schedulers
    ]
    workers[0].start()
    assert first_rebuild_entered.wait(timeout=3)
    workers[1].start()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert CountingDatabase.rebuild_calls == 1
    check = RealDatabase(db_path)
    try:
        assert check.get_rescue_catalog_meta() is not None
    finally:
        check.close()


def test_scheduler_failed_initialization_rolls_back_before_another_instance_retries(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "retry-scheduler.db"
    seed = RealDatabase(db_path)
    seed.init_schema()
    seed.close()

    class FailingOnceDatabase(RealDatabase):
        rebuild_calls = 0

        def init_schema(self):
            pass

        def rebuild_rescue_catalog(self):
            type(self).rebuild_calls += 1
            first_attempt = type(self).rebuild_calls == 1
            refreshed_at = "partial" if first_attempt else "complete"
            self.conn.execute(
                """
                INSERT INTO rescue_catalog_meta (
                    singleton_id, refreshed_at, item_count, duration_ms
                ) VALUES (1, ?, 0, 0)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    refreshed_at = excluded.refreshed_at,
                    item_count = excluded.item_count,
                    duration_ms = excluded.duration_ms
                """,
                (refreshed_at,),
            )
            self._commit_if_needed()
            if first_attempt:
                raise RuntimeError("rebuild failed after a partial write")
            return {"items": 0, "sources": 0, "duration_ms": 0}

    settings = _disabled_scheduler_settings(tmp_path)
    settings.storage.db_path = db_path
    monkeypatch.setattr(
        "pixiv_novel_sync.web.managers.Database",
        FailingOnceDatabase,
    )

    first = AutoSyncScheduler(config_path=None, env_path=None)
    second = AutoSyncScheduler(config_path=None, env_path=None)
    first._initialize_rescue_catalog(settings)
    second._initialize_rescue_catalog(settings)

    assert FailingOnceDatabase.rebuild_calls == 2
    check = RealDatabase(db_path)
    try:
        meta = check.get_rescue_catalog_meta()
        assert meta is not None
        assert meta["refreshed_at"] == "complete"
    finally:
        check.close()


def test_scheduler_skips_initialization_when_catalog_meta_exists(monkeypatch, tmp_path):
    checked = threading.Event()

    class Database:
        def __init__(self, db_path):
            pass

        def init_schema(self):
            pass

        def transaction(self):
            return nullcontext()

        def get_rescue_catalog_meta(self):
            checked.set()
            return {"refreshed_at": "now"}

        def rebuild_rescue_catalog(self):
            raise AssertionError("existing catalog must not rebuild")

        def close(self):
            pass

    settings = _disabled_scheduler_settings(tmp_path)
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    scheduler._last_cleanup_time = float("inf")
    monkeypatch.setattr("pixiv_novel_sync.web.managers.load_settings", lambda *args: settings)
    monkeypatch.setattr("pixiv_novel_sync.web.managers.Database", Database)

    scheduler.start()
    worker = scheduler._thread
    try:
        assert checked.wait(timeout=3)
    finally:
        scheduler.stop()
        if worker is not None:
            worker.join(timeout=3)
    assert worker is not None and not worker.is_alive()


def test_scheduler_retries_failed_initialization_after_stop_and_restart(
    monkeypatch, tmp_path, caplog
):
    attempted = [threading.Event(), threading.Event()]

    class Database:
        rebuild_calls = 0

        def __init__(self, db_path):
            pass

        def init_schema(self):
            pass

        def transaction(self):
            return nullcontext()

        def get_rescue_catalog_meta(self):
            return None

        def rebuild_rescue_catalog(self):
            index = type(self).rebuild_calls
            type(self).rebuild_calls += 1
            attempted[index].set()
            if index == 0:
                raise RuntimeError("catalog boom")
            return {"items": 1, "sources": 1}

        def close(self):
            pass

    settings = _disabled_scheduler_settings(tmp_path)
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    scheduler._last_cleanup_time = float("inf")
    monkeypatch.setattr("pixiv_novel_sync.web.managers.load_settings", lambda *args: settings)
    monkeypatch.setattr("pixiv_novel_sync.web.managers.Database", Database)

    scheduler.start()
    first_worker = scheduler._thread
    try:
        assert attempted[0].wait(timeout=3)
    finally:
        scheduler.stop()
        if first_worker is not None:
            first_worker.join(timeout=3)
    assert first_worker is not None and not first_worker.is_alive()
    assert "救援目录初始化失败: catalog boom" in caplog.text

    scheduler.start()
    second_worker = scheduler._thread
    try:
        assert scheduler._stop_current_task is False
        assert attempted[1].wait(timeout=3)
    finally:
        scheduler.stop()
        if second_worker is not None:
            second_worker.join(timeout=3)
    assert second_worker is not None and second_worker is not first_worker
    assert not second_worker.is_alive()
    assert Database.rebuild_calls == 2


def test_scheduler_catalog_initialization_failure_only_warns(monkeypatch, tmp_path, caplog):
    class Database:
        def __init__(self, db_path):
            pass

        def init_schema(self):
            raise RuntimeError("schema boom")

        def close(self):
            pass

    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    monkeypatch.setattr("pixiv_novel_sync.web.managers.Database", Database)

    scheduler._initialize_rescue_catalog(_disabled_scheduler_settings(tmp_path))

    assert "救援目录初始化失败: schema boom" in caplog.text


def test_scheduler_restart_rejects_alive_worker_without_blocking(caplog):
    old_started = threading.Event()
    old_release = threading.Event()
    restart_returned = threading.Event()
    new_started = threading.Event()

    def old_worker():
        old_started.set()
        old_release.wait(timeout=5)

    def new_worker(stop_event):
        new_started.set()
        stop_event.wait(timeout=5)

    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    old_thread = threading.Thread(target=old_worker, daemon=True)
    scheduler._thread = old_thread
    scheduler._running = True
    scheduler._run_scheduler = new_worker
    old_thread.start()
    assert old_started.wait(timeout=2)
    scheduler.stop()

    caller = threading.Thread(
        target=lambda: (scheduler.start(), restart_returned.set()),
        daemon=True,
    )
    caller.start()
    try:
        assert restart_returned.wait(timeout=2)
        assert scheduler._thread is old_thread
        assert scheduler.is_running() is False
        assert new_started.is_set() is False
        assert "旧调度线程仍在停止，拒绝重复启动" in caplog.text
    finally:
        old_release.set()
        caller.join(timeout=3)
        current_thread = scheduler._thread
        scheduler.stop()
        old_thread.join(timeout=3)
        if current_thread is not None and current_thread is not old_thread:
            current_thread.join(timeout=3)


class SynchronousThread:
    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon

    def start(self):
        self.target(*self.args, **self.kwargs)


class RecordingDatabase:
    created_logs: list[dict] = []
    updated_logs: list[dict] = []

    def __init__(self, db_path):
        self.db_path = db_path

    def init_schema(self):
        return None

    def create_task_log(self, **kwargs):
        self.created_logs.append(kwargs)
        return len(self.created_logs)

    def update_task_log(self, log_id, status, **kwargs):
        self.updated_logs.append({"log_id": log_id, "status": status, **kwargs})

    def close(self):
        return None


def _app(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    monkeypatch.setenv("FLASK_DEBUG", "1")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    return create_app(env_path=str(env_path))


def test_create_app_shares_scheduler_owner_by_normalized_db_path_and_releases_on_stop(
    tmp_path,
    monkeypatch,
):
    relative_db_path = ".\\scheduler.db"
    absolute_db_path = str((tmp_path / "scheduler.db").resolve())
    monkeypatch.chdir(tmp_path)
    assert str((tmp_path / relative_db_path).resolve()) == absolute_db_path

    start_calls = []
    original_start = AutoSyncScheduler.start

    def recording_start(self):
        start_calls.append(self)
        return original_start(self)

    def idle_scheduler(self, stop_event=None):
        (stop_event or self._stop_event).wait(timeout=5)

    monkeypatch.setattr(AutoSyncScheduler, "start", recording_start)
    monkeypatch.setattr(AutoSyncScheduler, "_run_scheduler", idle_scheduler)
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "test-secret")
    monkeypatch.setenv("PIXIV_REFRESH_TOKEN", "test-refresh-token")
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    monkeypatch.delenv("WERKZEUG_SERVER_FD", raising=False)
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)

    started_owners = []
    try:
        monkeypatch.setenv("PIXIV_DB_PATH", relative_db_path)
        create_app()
        monkeypatch.setenv("PIXIV_DB_PATH", absolute_db_path)
        second_app = create_app()
        starts_while_owner_active = len(start_calls)

        first_owner = start_calls[0]
        owner_job = first_owner.sync_job_manager.start_auto_job("bookmarks", "bookmarks")
        assert owner_job is not None

        class ForbiddenDatabase:
            def __init__(self, db_path):
                raise AssertionError("manual submission reached the database")

        monkeypatch.setattr("pixiv_novel_sync.webapp.Database", ForbiddenDatabase)
        blocked = second_app.test_client().post("/api/dashboard/sync/user_status")
        assert blocked.status_code == 400
        assert "已有同步任务" in blocked.get_json()["error"]

        status_response = second_app.test_client().get(
            f"/api/dashboard/sync/status?job_id={owner_job.job_id}"
        )
        assert status_response.get_json()["job"]["job_id"] == owner_job.job_id
        owner_job.status = "succeeded"
        first_owner.sync_job_manager._semaphore.release()
        first_owner.stop()

        create_app()
        started_owners = list(start_calls)
    finally:
        for scheduler in start_calls:
            scheduler.stop()
            worker = scheduler._thread
            if worker is not None:
                worker.join(timeout=3)

    assert starts_while_owner_active == 1
    assert len(start_calls) == 2
    assert len(started_owners) == 2
    assert started_owners[1] is not started_owners[0]


def test_scheduler_registry_keeps_stopping_owner_until_worker_exits(tmp_path):
    import pixiv_novel_sync.webapp as webapp_module

    key = webapp_module._scheduler_registry_key(tmp_path / "stopping-owner.db")
    worker_entered = threading.Event()
    release_worker = threading.Event()
    manager = SyncJobManager(config_path=None, env_path=None)
    first, created = webapp_module._get_or_create_scheduler_owner(
        key,
        config_path=None,
        env_path=None,
        sync_job_manager=manager,
    )
    assert created is True

    def blocking_scheduler(stop_event):
        worker_entered.set()
        release_worker.wait(timeout=5)

    first._run_scheduler = blocking_scheduler
    first.start()
    assert worker_entered.wait(timeout=3)

    try:
        first.stop()
        while_stopping, created_while_stopping = webapp_module._get_or_create_scheduler_owner(
            key,
            config_path=None,
            env_path=None,
            sync_job_manager=SyncJobManager(config_path=None, env_path=None),
        )
        assert while_stopping is first
        assert created_while_stopping is False
    finally:
        release_worker.set()
        worker = first._thread
        if worker is not None:
            worker.join(timeout=3)

    replacement, replacement_created = webapp_module._get_or_create_scheduler_owner(
        key,
        config_path=None,
        env_path=None,
        sync_job_manager=SyncJobManager(config_path=None, env_path=None),
    )
    try:
        assert replacement_created is True
        assert replacement is not first
    finally:
        replacement.stop()


def test_scheduler_old_worker_cannot_release_restarted_owner(tmp_path):
    import pixiv_novel_sync.webapp as webapp_module

    key = webapp_module._scheduler_registry_key(tmp_path / "restart-owner.db")
    first_release_entered = threading.Event()
    allow_first_release = threading.Event()
    second_worker_started = threading.Event()
    release_second_worker = threading.Event()
    scheduler, created = webapp_module._get_or_create_scheduler_owner(
        key,
        config_path=None,
        env_path=None,
        sync_job_manager=SyncJobManager(config_path=None, env_path=None),
    )
    assert created is True

    worker_count = 0
    worker_count_lock = threading.Lock()

    def controlled_scheduler(stop_event):
        nonlocal worker_count
        with worker_count_lock:
            worker_count += 1
            generation = worker_count
        if generation == 1:
            return
        second_worker_started.set()
        release_second_worker.wait(timeout=5)

    original_release = scheduler._lifecycle_release
    release_count = 0

    def blocking_first_release(owner):
        nonlocal release_count
        release_count += 1
        if release_count == 1:
            first_release_entered.set()
            assert allow_first_release.wait(timeout=3)
        assert original_release is not None
        original_release(owner)

    scheduler._run_scheduler = controlled_scheduler
    scheduler._lifecycle_release = blocking_first_release
    scheduler.start()
    assert first_release_entered.wait(timeout=3)

    restart = threading.Thread(target=scheduler.start, daemon=True)
    restart.start()
    try:
        allow_first_release.set()
        assert second_worker_started.wait(timeout=3)
        restart.join(timeout=3)
        assert not restart.is_alive()
        with webapp_module._auto_sync_scheduler_registry_lock:
            owner = webapp_module._auto_sync_scheduler_registry.get(key)
        assert owner is not None and owner.scheduler is scheduler
    finally:
        allow_first_release.set()
        release_second_worker.set()
        scheduler.stop()
        worker = scheduler._thread
        if worker is not None:
            worker.join(timeout=3)


def test_shared_sync_blocks_concurrent_sync_submission(tmp_path, monkeypatch):
    """shared 路径有任务运行时，新的 sync 提交应被阻断返回 400。"""
    def keep_shared_job_running(self, job_id):
        self.manager.mark_running(job_id, "running")
        return self.manager.get_job(job_id)

    monkeypatch.setattr("pixiv_novel_sync.webapp.JobRunner.run", keep_shared_job_running)

    app = _app(tmp_path, monkeypatch)
    client = app.test_client()

    started = client.post("/api/dashboard/sync/start")
    blocked = client.post("/api/dashboard/sync/user_status")

    assert started.status_code == 200
    assert blocked.status_code == 400
    assert blocked.get_json()["error"] == "已有同步任务正在运行，请稍后再试"


def test_shared_sync_success_updates_task_log(tmp_path, monkeypatch):
    RecordingDatabase.created_logs = []
    RecordingDatabase.updated_logs = []

    def successful_run(self, job_id):
        self.manager.mark_running(job_id, "running")
        self.manager.add_log(job_id, "info", "done")
        state = self.manager.get_job(job_id)
        state.stats["novels"] = 2
        self.manager.mark_succeeded(job_id, "succeeded")
        return self.manager.get_job(job_id)

    monkeypatch.setattr("pixiv_novel_sync.webapp.Database", RecordingDatabase)
    monkeypatch.setattr("pixiv_novel_sync.webapp.threading.Thread", SynchronousThread)
    monkeypatch.setattr("pixiv_novel_sync.webapp.JobRunner.run", successful_run)

    app = _app(tmp_path, monkeypatch)
    response = app.test_client().post("/api/dashboard/sync/start")

    assert response.status_code == 200
    assert RecordingDatabase.updated_logs == [
        {
            "log_id": 1,
            "status": JobStatus.SUCCEEDED.value,
            "stats": {"novels": 2},
            "logs": [{"time": RecordingDatabase.updated_logs[0]["logs"][0]["time"], "level": "info", "message": "done"}],
        }
    ]


def test_shared_sync_failure_updates_task_log(tmp_path, monkeypatch):
    RecordingDatabase.created_logs = []
    RecordingDatabase.updated_logs = []

    def failed_run(self, job_id):
        self.manager.mark_running(job_id, "running")
        self.manager.add_log(job_id, "error", "boom")
        self.manager.mark_failed(job_id, "boom")
        return self.manager.get_job(job_id)

    monkeypatch.setattr("pixiv_novel_sync.webapp.Database", RecordingDatabase)
    monkeypatch.setattr("pixiv_novel_sync.webapp.threading.Thread", SynchronousThread)
    monkeypatch.setattr("pixiv_novel_sync.webapp.JobRunner.run", failed_run)

    app = _app(tmp_path, monkeypatch)
    response = app.test_client().post("/api/dashboard/sync/start")

    assert response.status_code == 200
    assert RecordingDatabase.updated_logs == [
        {
            "log_id": 1,
            "status": JobStatus.FAILED.value,
            "error_message": "boom",
            "logs": [{"time": RecordingDatabase.updated_logs[0]["logs"][0]["time"], "level": "error", "message": "boom"}],
        }
    ]


def test_auto_sync_failure_persists_error_message(monkeypatch):
    RecordingDatabase.created_logs = []
    RecordingDatabase.updated_logs = []

    monkeypatch.setattr("pixiv_novel_sync.web.managers.Database", RecordingDatabase)

    manager = SyncJobManager(config_path=None, env_path=None)
    scheduler = AutoSyncScheduler(config_path=None, env_path=None, sync_job_manager=manager)

    def failing_task(settings, job_id):
        raise RuntimeError("auto boom")

    scheduler._sync_failing_task = failing_task
    settings = type("Settings", (), {"storage": type("Storage", (), {"db_path": "ignored"})()})()

    scheduler._run_single_task(settings, "failing_task", "_sync_failing_task")

    assert RecordingDatabase.updated_logs == [
        {
            "log_id": 1,
            "status": "failed",
            "stats": None,
            "error_message": "auto boom",
            "logs": [],
        }
    ]


def test_auto_sync_success_records_returned_stats(monkeypatch):
    RecordingDatabase.created_logs = []
    RecordingDatabase.updated_logs = []
    monkeypatch.setattr("pixiv_novel_sync.web.managers.Database", RecordingDatabase)
    manager = SyncJobManager(config_path=None, env_path=None)
    scheduler = AutoSyncScheduler(config_path=None, env_path=None, sync_job_manager=manager)

    def successful_task(settings, job_id):
        return {"novels": 1, "rescue_catalog_items": 2}

    scheduler._sync_successful_task = successful_task
    settings = type("Settings", (), {"storage": type("Storage", (), {"db_path": "ignored"})()})()

    scheduler._run_single_task(settings, "successful_task", "_sync_successful_task")

    job = manager.latest_job()
    assert job is not None
    assert job.status == "succeeded"
    assert job.stats == {"novels": 1, "rescue_catalog_items": 2}
    assert RecordingDatabase.updated_logs[0]["stats"] == job.stats


def test_auto_sync_stopped_result_is_cancelled_and_keeps_stats(monkeypatch):
    RecordingDatabase.created_logs = []
    RecordingDatabase.updated_logs = []
    monkeypatch.setattr("pixiv_novel_sync.web.managers.Database", RecordingDatabase)
    manager = SyncJobManager(config_path=None, env_path=None)
    scheduler = AutoSyncScheduler(config_path=None, env_path=None, sync_job_manager=manager)

    def stopped_task(settings, job_id):
        return {"novels": 1, "stopped": True}

    scheduler._sync_stopped_task = stopped_task
    settings = type("Settings", (), {"storage": type("Storage", (), {"db_path": "ignored"})()})()

    scheduler._run_single_task(settings, "stopped_task", "_sync_stopped_task")

    job = manager.latest_job()
    assert job is not None
    assert job.status == "cancelled"
    assert job.stats == {"novels": 1, "stopped": True}
    assert RecordingDatabase.updated_logs[0]["status"] == "cancelled"


class FailingOnceDatabase:
    fail_init_once = True

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def init_schema(self) -> None:
        if FailingOnceDatabase.fail_init_once:
            FailingOnceDatabase.fail_init_once = False
            raise RuntimeError("schema unavailable")

    def create_task_log(self, **kwargs) -> int:
        return 123

    def close(self) -> None:
        pass


def test_dashboard_sync_start_releases_gate_when_database_init_fails(tmp_path, monkeypatch):
    ran = []

    def fake_run(self, job_id):
        ran.append(job_id)
        self.manager.mark_running(job_id, "running")
        self.manager.mark_succeeded(job_id, "succeeded")
        return self.manager.get_job(job_id)

    FailingOnceDatabase.fail_init_once = False
    monkeypatch.setattr("pixiv_novel_sync.webapp.Database", FailingOnceDatabase)
    monkeypatch.setattr("pixiv_novel_sync.webapp.JobRunner.run", fake_run)
    app = _app(tmp_path, monkeypatch)
    client = app.test_client()

    FailingOnceDatabase.fail_init_once = True
    failed = client.post("/api/dashboard/sync/start")
    failed_payload = failed.get_json()
    assert failed.status_code == 400
    assert failed_payload["ok"] is False
    assert "schema unavailable" in failed_payload["error"]

    retried = client.post("/api/dashboard/sync/start")

    payload = retried.get_json()
    assert retried.status_code == 200
    assert payload["ok"] is True
    assert payload["job"]["source"] == JobSource.WEB.value
    assert ran == [payload["job"]["job_id"]]

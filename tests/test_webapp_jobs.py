from __future__ import annotations

from pixiv_novel_sync.jobs.models import JobSource, JobType
from pixiv_novel_sync.webapp import AutoSyncScheduler, SyncJobManager, SyncJobState, _web_job_spec


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


def test_sync_job_manager_start_job_records_job_spec(tmp_path):
    manager = SyncJobManager(config_path=None, env_path=None)
    spec = _web_job_spec(["bookmark"])

    job = manager.start_job(spec.task_types)

    assert job.task_list == ["bookmark"]


def test_auto_sync_scheduler_delegates_status_and_pending_detection_services(monkeypatch):
    calls = []

    def fake_user_status(settings, reporter=None, stop_requested=None):
        calls.append(("user_status", settings, reporter, stop_requested))
        return {"checked_count": 1}

    def fake_novel_status(settings, reporter=None, stop_requested=None):
        calls.append(("novel_status", settings, reporter, stop_requested))
        return {"checked_count": 2}

    def fake_series_status(settings, reporter=None, stop_requested=None):
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
    scheduler._sync_novel_status(settings, "job-1")
    scheduler._sync_series_status(settings, "job-1")
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


def test_sync_job_manager_delegates_target_tasks_to_services(monkeypatch):
    calls = []

    def fake_user_status(settings, reporter=None, stop_requested=None):
        calls.append(("user_status", settings, reporter, stop_requested))
        return {"checked_count": 1}

    def fake_novel_status(settings, reporter=None, stop_requested=None):
        calls.append(("novel_status", settings, reporter, stop_requested))
        return {"checked_count": 2}

    def fake_series_status(settings, reporter=None, stop_requested=None):
        calls.append(("series_status", settings, reporter, stop_requested))
        return {"checked_count": 3}

    def fake_user_backup(settings, user_id, reporter=None, stop_requested=None):
        calls.append(("user_backup", settings, reporter, stop_requested, user_id))
        return {"novels": 4}

    def fake_pending_detection(settings, reporter=None, stop_requested=None):
        calls.append(("pending_detection", settings, reporter, stop_requested))
        return {"new_pending": 5}

    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_user_status_task", fake_user_status)
    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_novel_status_task", fake_novel_status)
    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_series_status_task", fake_series_status)
    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_user_backup_task", fake_user_backup)
    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_pending_deletion_detection_task", fake_pending_detection)
    manager = SyncJobManager(config_path=None, env_path=None)
    manager._jobs["job-1"] = SyncJobState(job_id="job-1", status="running")
    settings = object()

    assert manager._run_single_sync(settings, "user_status", "job-1") == {"checked_count": 1}
    assert manager._run_single_sync(settings, "novel_status", "job-1") == {"checked_count": 2}
    assert manager._run_single_sync(settings, "series_status", "job-1") == {"checked_count": 3}
    assert manager._run_single_sync(settings, "user_backup:123", "job-1") == {"novels": 4}
    assert manager._run_single_sync(settings, "pending_deletion_detection", "job-1") == {"new_pending": 5}

    assert [call[0] for call in calls] == ["user_status", "novel_status", "series_status", "user_backup", "pending_detection"]
    assert calls[3][4] == 123
    assert all(call[1] is settings for call in calls)
    assert all(call[2] is not None for call in calls)
    assert all(call[2].manager is manager for call in calls)
    assert all(call[2].job_id == "job-1" for call in calls)
    assert all(call[3] is not None for call in calls)
    assert all(call[3]() is False for call in calls)


def test_auto_sync_scheduler_delegates_user_backup_to_each_user(monkeypatch):
    calls = []

    def fake_user_backup(settings, user_id, reporter=None, stop_requested=None):
        calls.append((settings, user_id, reporter, stop_requested))
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

        def __init__(self, db_path):
            assert db_path == "db.sqlite"

        def init_schema(self):
            pass

        def get_watermark(self, key):
            assert key == "user_backup_rotation"
            return {"offset": 0}

        def update_watermark(self, key, data):
            assert key == "user_backup_rotation"

        def close(self):
            pass

    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_user_backup_task", fake_user_backup)
    monkeypatch.setattr("pixiv_novel_sync.webapp.Database", FakeDatabase)
    settings = Settings()
    settings.storage = type("Storage", (), {"db_path": "db.sqlite"})()
    scheduler = AutoSyncScheduler(config_path=None, env_path=None, sync_job_manager=Manager())
    scheduler._running = True

    scheduler._sync_user_backup(settings, "job-1")

    assert [call[1] for call in calls] == [10, 20]
    assert all(call[0] is settings for call in calls)
    assert all(call[2] is not None for call in calls)
    assert all(call[2].job_id == "job-1" for call in calls)
    assert all(call[3] is not None for call in calls)
    assert all(call[3]() is False for call in calls)

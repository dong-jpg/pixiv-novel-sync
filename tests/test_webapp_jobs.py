from __future__ import annotations

from pixiv_novel_sync.jobs.models import JobSource, JobStatus, JobType
from pixiv_novel_sync.webapp import AutoSyncScheduler, SyncJobManager, SyncJobState, _web_job_spec, create_app


class StubSyncJobManager(SyncJobManager):
    def __init__(self, *args, task_stats=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.task_stats = task_stats or {}

    def _run_single_sync(self, settings, task_type, current_job_id):
        assert current_job_id == "job-1"
        return self.task_stats[task_type]


def _run_manager_job(monkeypatch, task_stats):
    manager = StubSyncJobManager(config_path=None, env_path=None, task_stats=task_stats)
    job_id = "job-1"
    job = SyncJobState(job_id=job_id, status="running", task_list=list(task_stats))
    manager._jobs[job_id] = job
    assert manager._semaphore.acquire(blocking=False)
    monkeypatch.setattr("pixiv_novel_sync.webapp.load_settings", lambda config_path, env_path: object())

    manager._run_job(job_id)

    return job


def test_run_job_preserves_status_counts_stats_without_failing(monkeypatch):
    job = _run_manager_job(
        monkeypatch,
        {
            "novel_status": {"status_counts": {"exists": 2, "deleted": 1}, "stopped": False},
        },
    )

    assert job.status == "succeeded"
    assert job.error is None
    assert job.stats == {"status_counts": {"exists": 2, "deleted": 1}, "stopped": False}


def test_run_job_preserves_pending_detection_stats_without_failing(monkeypatch):
    job = _run_manager_job(
        monkeypatch,
        {
            "pending_deletion_detection": {
                "bookmark": {},
                "series": {},
                "new_pending": 0,
                "stopped": True,
            },
        },
    )

    assert job.status == "succeeded"
    assert job.error is None
    assert job.stats == {"bookmark": {}, "series": {}, "new_pending": 0, "stopped": True}


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
            assert str(db_path) == "db.sqlite"

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
    monkeypatch.setattr("pixiv_novel_sync.web.managers.Database", FakeDatabase)
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


def test_shared_sync_blocks_legacy_sync_routes(tmp_path, monkeypatch):
    def keep_shared_job_running(self, job_id):
        self.manager.mark_running(job_id, "running")
        return self.manager.get_job(job_id)

    def legacy_job_should_not_start(self, task_list=None):
        raise AssertionError("legacy job should be blocked before start_job")

    monkeypatch.setattr("pixiv_novel_sync.webapp.JobRunner.run", keep_shared_job_running)
    monkeypatch.setattr("pixiv_novel_sync.webapp.SyncJobManager._run_job", lambda self, job_id: None)
    monkeypatch.setattr("pixiv_novel_sync.webapp.SyncJobManager.start_job", legacy_job_should_not_start)

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
    assert failed.status_code == 400
    assert "schema unavailable" in failed.get_json()["error"]

    retried = client.post("/api/dashboard/sync/start")

    payload = retried.get_json()
    assert retried.status_code == 200
    assert payload["ok"] is True
    assert payload["job"]["source"] == JobSource.WEB.value
    assert ran == [payload["job"]["job_id"]]

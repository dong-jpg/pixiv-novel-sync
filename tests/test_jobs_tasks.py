from __future__ import annotations

from types import SimpleNamespace

import pytest

from pixiv_novel_sync.jobs import tasks as tasks_module
from pixiv_novel_sync.jobs.tasks import (
    _build_progress_callback,
    build_default_task_list,
    execute_task,
    merge_stats,
    task_label,
)


@pytest.fixture
def direct_sync_env(tmp_path, monkeypatch):
    created = {}

    class FakeAuthManager:
        def __init__(self, pixiv_settings):
            created["auth"] = self

        def login(self):
            return object(), SimpleNamespace(user_id=123)

    class FakeDatabase:
        def __init__(self, db_path):
            self.rebuild_catalog_calls = 0
            self.closed = False
            created["db"] = self

        def init_schema(self):
            pass

        def rebuild_rescue_catalog(self):
            self.rebuild_catalog_calls += 1
            return {"items": 6, "sources": 7}

        def close(self):
            self.closed = True

    class FakeStorage:
        def __init__(self, settings):
            pass

        def ensure_dirs(self, dirs):
            pass

    class FakeSyncService:
        def __init__(self, api, db, storage, settings):
            created["service"] = self

        def sync_following_list(self, progress_callback=None):
            return {"users": 1}

        def sync_following_novels(self, **kwargs):
            return {"novels": 2}

        def sync_subscribed_series(self, **kwargs):
            return {"series_synced": 3}

    monkeypatch.setattr("pixiv_novel_sync.auth.PixivAuthManager", FakeAuthManager)
    monkeypatch.setattr("pixiv_novel_sync.storage_db.Database", FakeDatabase)
    monkeypatch.setattr("pixiv_novel_sync.storage_files.FileStorage", FakeStorage)
    monkeypatch.setattr("pixiv_novel_sync.sync_engine.BookmarkNovelSyncService", FakeSyncService)

    settings = SimpleNamespace(
        pixiv=SimpleNamespace(user_id=None),
        storage=SimpleNamespace(
            db_path=tmp_path / "test.db",
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
        ),
        sync=SimpleNamespace(
            series_sync_limit=0,
            auto_sync_following_novels_users_limit=0,
            download_assets=True,
            write_markdown=True,
            write_raw_text=False,
        ),
    )
    return settings, created, FakeDatabase, FakeSyncService


def test_build_default_task_list_uses_sync_settings():
    class Sync:
        sync_bookmarks = True
        sync_following_users = False
        sync_following_novels = True
        sync_subscribed_series = True

    class Settings:
        sync = Sync()

    assert build_default_task_list(Settings()) == ["bookmark", "following_novels", "subscribed_series"]


def test_task_label_for_known_and_unknown_tasks():
    assert task_label("bookmark") == "收藏小说"
    assert task_label("user_backup:123") == "用户 123 全量备份"
    assert task_label("custom") == "custom"


def test_merge_stats_adds_numbers_and_overwrites_non_numbers():
    total = {"novels": 1, "status": "old"}

    result = merge_stats(total, {"novels": 2, "status": "new", "failed": 1})

    assert result is total
    assert result == {"novels": 3, "status": "new", "failed": 1}


def test_merge_stats_does_not_add_booleans():
    total = {"ok": True}

    result = merge_stats(total, {"ok": True})

    assert result["ok"] is True


def test_merge_stats_overwrites_rescue_catalog_snapshot_values():
    total = {
        "rescue_catalog_items": 2,
        "rescue_catalog_sources": 3,
        "rescue_catalog_duration_ms": 4,
    }

    result = merge_stats(
        total,
        {
            "rescue_catalog_items": 5,
            "rescue_catalog_sources": 6,
            "rescue_catalog_duration_ms": 7,
        },
    )

    assert result == {
        "rescue_catalog_items": 5,
        "rescue_catalog_sources": 6,
        "rescue_catalog_duration_ms": 7,
    }


def test_execute_task_dispatches_bookmark(monkeypatch):
    calls = []

    def fake_run_bookmark_sync(settings, stop_requested=None):
        calls.append((settings, stop_requested))
        return {"novels": 1}

    monkeypatch.setattr("pixiv_novel_sync.jobs.quick_sync.run_bookmark_sync", fake_run_bookmark_sync)
    settings = object()
    manager = object()

    result = execute_task("bookmark", settings, {"manager": manager, "job_id": "job-1"})

    assert result == {"novels": 1}
    assert calls[0][0] is settings
    assert calls[0][1] is not None


def test_execute_task_dispatches_sync_check_without_releasing_runner_slot(monkeypatch):
    calls = []

    def fake_run_check_bookmarks_task(
        settings,
        manager,
        job_id,
        release_semaphore=True,
        raise_on_error=False,
        stop_requested=None,
    ):
        calls.append((settings, manager, job_id, release_semaphore, raise_on_error, stop_requested))
        return {"total_checked": 2}

    monkeypatch.setattr("pixiv_novel_sync.jobs.quick_sync.run_check_bookmarks_task", fake_run_check_bookmarks_task)
    settings = object()
    manager = object()

    result = execute_task("sync_check", settings, {"manager": manager, "job_id": "job-1"})

    assert result == {"total_checked": 2}
    assert calls[0][:5] == (settings, manager, "job-1", False, True)
    assert calls[0][5] is not None


def test_execute_task_propagates_sync_check_failure(monkeypatch):
    def fake_run_check_bookmarks_task(
        settings,
        manager,
        job_id,
        release_semaphore=True,
        raise_on_error=False,
        stop_requested=None,
    ):
        assert release_semaphore is False
        assert raise_on_error is True
        assert stop_requested is not None
        raise RuntimeError("sync check failed")

    monkeypatch.setattr("pixiv_novel_sync.jobs.quick_sync.run_check_bookmarks_task", fake_run_check_bookmarks_task)

    with pytest.raises(RuntimeError, match="sync check failed"):
        execute_task("sync_check", object(), {"manager": object(), "job_id": "job-1"})


def test_direct_sync_progress_callback_ignores_missing_manager_methods():
    callback = _build_progress_callback(object(), "job-1")

    assert callback is not None

    callback("page", {"page": 1})
    callback("phase", {"phase": "测试"})
    callback("user_start", {"current": 1, "total": 2})


def test_direct_sync_progress_callback_calls_manager_methods_when_available():
    logs = []
    updates = []

    class Manager:
        def add_log(self, job_id, level, message):
            logs.append((job_id, level, message))

        def update_progress(self, job_id, **kwargs):
            updates.append((job_id, kwargs))

    callback = _build_progress_callback(Manager(), "job-1")

    assert callback is not None

    callback("page", {"page": 1})
    callback("phase", {"phase": "测试"})
    callback("user_start", {"current": 1, "total": 2, "author": "作者"})

    assert logs == [("job-1", "info", "正在获取第 1 页...")]
    assert updates == [
        ("job-1", {"phase": "测试", "message": "测试"}),
        ("job-1", {"phase": "同步用户小说", "current": 1, "total": 2, "author": "作者"}),
    ]


def test_direct_sync_progress_callback_raises_when_cancel_requested():
    callback = _build_progress_callback(object(), "job-1", stop_requested=lambda: True)

    assert callback is not None
    with pytest.raises(InterruptedError, match="Task stopped by user"):
        callback("page", {"page": 1})


def test_execute_task_rejects_unknown_task_with_clear_error():
    with pytest.raises(RuntimeError, match="Unsupported task type for CLI execution: custom_task"):
        execute_task("custom_task", object())


@pytest.mark.parametrize(
    ("task_type", "expected_business_key"),
    [("following_novels", "novels"), ("subscribed_series", "series_synced")],
)
def test_direct_sync_tasks_rebuild_catalog_once_after_success(
    task_type, expected_business_key, direct_sync_env
):
    settings, created, _fake_db, _fake_service = direct_sync_env

    result = execute_task(task_type, settings)

    assert expected_business_key in result
    assert result["rescue_catalog_items"] == 6
    assert result["rescue_catalog_sources"] == 7
    assert created["db"].rebuild_catalog_calls == 1
    assert created["db"].closed is True


def test_following_users_direct_sync_does_not_rebuild_catalog(direct_sync_env):
    settings, created, _fake_db, _fake_service = direct_sync_env

    result = execute_task("following_users", settings)

    assert result == {"users": 1}
    assert created["db"].rebuild_catalog_calls == 0


def test_direct_sync_keeps_stats_when_catalog_rebuild_fails(
    direct_sync_env, monkeypatch, caplog
):
    settings, created, fake_db, _fake_service = direct_sync_env

    def fail_rebuild(self):
        self.rebuild_catalog_calls += 1
        raise RuntimeError("catalog boom")

    monkeypatch.setattr(fake_db, "rebuild_rescue_catalog", fail_rebuild)

    result = execute_task("following_novels", settings)

    assert result == {"novels": 2}
    assert created["db"].rebuild_catalog_calls == 1
    assert "救援目录刷新失败: catalog boom" in caplog.text


@pytest.mark.parametrize("error", [RuntimeError("sync boom"), InterruptedError("cancelled")])
def test_direct_sync_propagates_business_failure_without_rebuild(
    error, direct_sync_env, monkeypatch
):
    settings, created, _fake_db, fake_service = direct_sync_env

    def fail_sync(self, **kwargs):
        raise error

    monkeypatch.setattr(fake_service, "sync_following_novels", fail_sync)

    with pytest.raises(type(error), match=str(error)):
        execute_task("following_novels", settings)

    assert created["db"].rebuild_catalog_calls == 0
    assert created["db"].closed is True


def test_direct_sync_skips_rebuild_when_cancelled_after_service_returns(
    direct_sync_env, monkeypatch
):
    settings, created, _fake_db, fake_service = direct_sync_env

    class Manager:
        cancelled = False

        def add_log(self, job_id, level, message):
            pass

        def update_progress(self, job_id, **kwargs):
            pass

        def is_cancel_requested(self, job_id):
            return self.cancelled

    manager = Manager()

    def sync_then_cancel(self, **kwargs):
        manager.cancelled = True
        return {"novels": 2}

    monkeypatch.setattr(fake_service, "sync_following_novels", sync_then_cancel)

    with pytest.raises(InterruptedError, match="Task stopped by user"):
        execute_task("following_novels", settings, {"manager": manager, "job_id": "job-1"})

    assert created["db"].rebuild_catalog_calls == 0
    assert created["db"].closed is True



def test_execute_task_dispatches_user_backup_service(monkeypatch):
    calls = []

    def fake_run_user_backup_task(settings, user_id, reporter=None, stop_requested=None):
        calls.append((settings, user_id, reporter, stop_requested))
        return {"novels": 2}

    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_user_backup_task", fake_run_user_backup_task)
    settings = object()
    manager = object()

    result = execute_task("user_backup:123", settings, {"manager": manager, "job_id": "job-1"})

    assert result == {"novels": 2}
    assert calls[0][0] is settings
    assert calls[0][1] == 123
    assert calls[0][2] is not None
    assert calls[0][3] is not None



def test_execute_task_dispatches_status_services(monkeypatch):
    calls = []

    def fake_user_status(settings, reporter=None, stop_requested=None):
        calls.append(("user_status", settings, reporter, stop_requested))
        return {"checked_users": 1}

    def fake_novel_status(settings, reporter=None, stop_requested=None):
        calls.append(("novel_status", settings, reporter, stop_requested))
        return {"checked_novels": 2}

    def fake_series_status(settings, reporter=None, stop_requested=None):
        calls.append(("series_status", settings, reporter, stop_requested))
        return {"checked_series": 3}

    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_user_status_task", fake_user_status)
    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_novel_status_task", fake_novel_status)
    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_series_status_task", fake_series_status)
    settings = object()

    assert execute_task("user_status", settings, {"manager": object(), "job_id": "job-1"}) == {"checked_users": 1}
    assert execute_task("novel_status", settings, {"manager": object(), "job_id": "job-2"}) == {"checked_novels": 2}
    assert execute_task("series_status", settings, {"manager": object(), "job_id": "job-3"}) == {"checked_series": 3}
    assert [call[0] for call in calls] == ["user_status", "novel_status", "series_status"]
    assert all(call[2] is not None for call in calls)
    assert all(call[3] is not None for call in calls)



def test_execute_task_stop_requested_uses_job_manager_cancel_state(monkeypatch):
    observed = []

    def fake_run_user_status_task(settings, reporter=None, stop_requested=None):
        observed.append(stop_requested())
        return {"checked_users": 0}

    class Manager:
        def add_log(self, job_id, level, message):
            pass

        def update_progress(self, job_id, **kwargs):
            pass

        def is_cancel_requested(self, job_id):
            assert job_id == "job-1"
            return True

    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_user_status_task", fake_run_user_status_task)

    result = execute_task("user_status", object(), {"manager": Manager(), "job_id": "job-1"})

    assert result == {"checked_users": 0}
    assert observed == [True]


def test_execute_task_dispatches_pending_deletion_detection_service(monkeypatch):
    calls = []

    def fake_pending_detection(settings, reporter=None, stop_requested=None):
        calls.append((settings, reporter, stop_requested))
        return {"new_pending": 4}

    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_pending_deletion_detection_task", fake_pending_detection)
    settings = object()

    result = execute_task("pending_deletion_detection", settings, {"manager": object(), "job_id": "job-1"})

    assert result == {"new_pending": 4}
    assert calls[0][0] is settings
    assert calls[0][1] is not None
    assert calls[0][2] is not None


def test_preference_analyze_defaults_scope_limit(monkeypatch):
    captured = {}

    class FakeReporter:
        def add_log(self, level, message):
            pass

    class FakeDb:
        def init_schema(self):
            pass

        def get_default_preference_profile(self):
            return None  # 不存在 -> 走 create 分支

        def create_preference_profile(self, data):
            captured["profile"] = data
            return 1

        def update_preference_profile(self, profile_id, data):
            captured["updated"] = (profile_id, data)

        def reset_preference_accumulator(self):
            captured["reset"] = True

        def close(self):
            pass

    class FakeAnalyzer:
        def __init__(self, db):
            self.db = db

        def analyze_incremental(self, batch_size, max_batches, min_text_length=1000, progress=None):
            captured["incremental"] = {
                "batch_size": batch_size,
                "max_batches": max_batches,
                "min_text_length": min_text_length,
            }
            return {
                "processed_this_run": 5,
                "analyzed_total": 5,
                "remaining": 0,
                "done": True,
            }

        def rebuild_profile_from_accumulator(self):
            return {
                "source_scope": {"min_text_length": 1000, "incremental": True},
                "stats": {"novel_count": 5, "total_chars": 5000},
                "profile": {"positive_preferences": {"tags": ["甜文"]}},
            }

        def build_profile(self, stats):
            captured["rebuilt_stats"] = stats
            return {"positive_preferences": {"tags": ["甜文"]}}

    monkeypatch.setattr("pixiv_novel_sync.jobs.tasks._job_reporter_from_context", lambda context: FakeReporter())
    monkeypatch.setattr("pixiv_novel_sync.jobs.tasks.Database", lambda path: FakeDb(), raising=False)
    monkeypatch.setattr("pixiv_novel_sync.preferences.PreferenceAnalyzer", FakeAnalyzer)
    monkeypatch.setattr("pixiv_novel_sync.jobs.tasks.PreferenceAnalyzer", FakeAnalyzer, raising=False)
    monkeypatch.setattr("pixiv_novel_sync.storage_db.Database", lambda path: FakeDb())

    sync_obj = type("Sync", (), {"preference_analyze_batch_size": 200})()
    settings = type("Settings", (), {
        "storage": type("Storage", (), {"db_path": "ignored"})(),
        "sync": sync_obj,
    })()

    result = execute_task(
        "preference_analyze",
        settings,
        {"params": {"scope": {"min_text_length": 1000}, "is_default": True}},
    )

    assert result["profile_id"] == 1
    assert result["done"] is True
    assert result["analyzed_total"] == 5
    # 增量分析使用 settings 的 batch_size,手动触发默认跑多批
    assert captured["incremental"]["batch_size"] == 200
    assert captured["incremental"]["max_batches"] == 10
    # 默认画像不存在时走创建分支,且标记为默认
    assert captured["profile"]["is_default"] is True


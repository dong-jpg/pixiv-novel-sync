from __future__ import annotations

from types import SimpleNamespace

import pytest

from pixiv_novel_sync.jobs import quick_sync


class FakeDb:
    def __init__(self, db_path) -> None:
        self.db_path = db_path
        self.init_schema_called = False
        self.closed = False
        self.rebuild_catalog_calls = 0

    def init_schema(self) -> None:
        self.init_schema_called = True

    def close(self) -> None:
        self.closed = True

    def rebuild_rescue_catalog(self) -> dict[str, int]:
        self.rebuild_catalog_calls += 1
        return {"items": 4, "sources": 5}


class FakeStorage:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.ensure_dirs_calls: list[list[object]] = []

    def ensure_dirs(self, dirs: list[object]) -> None:
        self.ensure_dirs_calls.append(dirs)


class FakeAuthManager:
    def __init__(self, pixiv_settings) -> None:
        self.pixiv_settings = pixiv_settings
        self.api = object()
        self.auth_result = SimpleNamespace(user_id=123)
        self.login_called = False

    def login(self):
        self.login_called = True
        return self.api, self.auth_result


class FakeSyncService:
    def __init__(self, api, db, storage, settings, sync_check_scope=None) -> None:
        self.api = api
        self.db = db
        self.storage = storage
        self.settings = settings
        self.sync_check_scope = sync_check_scope
        self.sync_callback = None
        self.check_callback = None

    def sync(
        self,
        user_id,
        restricts,
        download_assets=True,
        write_markdown=True,
        write_raw_text=True,
        progress_callback=None,
    ):
        self.sync_callback = progress_callback
        if progress_callback is not None:
            progress_callback("page", {"page": 1})
        return {"novels": 1, "skipped": 0, "assets_downloaded": 0}

    def check_all_existence(self, user_id, restricts, progress_callback=None):
        self.check_callback = progress_callback
        if progress_callback is not None:
            progress_callback("page", {"page": 1})
        return {
            "total_checked": 1,
            "new": 1,
            "existing": 0,
            "bookmarks": {"total": 1, "new": 1, "existing": 0},
            "following_novels": {"total": 0, "new": 0, "existing": 0},
            "subscribed_series": {"total": 0, "new": 0, "existing": 0},
        }


class FakeJobManager:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str, str]] = []
        self.progress: list[tuple[str, dict[str, object]]] = []
        self.jobs = {"job-1": SimpleNamespace(spec=object(), progress={})}
        self.released = False

    def add_log(self, job_id, level, message):
        self.logs.append((job_id, level, message))

    def update_progress(self, job_id, **kwargs):
        self.progress.append((job_id, kwargs))

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    @property
    def _semaphore(self):
        return SimpleNamespace(release=self._release)

    def _release(self):
        self.released = True


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(
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
            sync_bookmarks=True,
            sync_following_novels=False,
            sync_subscribed_series=False,
        ),
    )


@pytest.fixture
def quick_sync_env(monkeypatch):
    created: dict[str, object] = {}

    def make_auth(pixiv_settings):
        auth = FakeAuthManager(pixiv_settings)
        created["auth"] = auth
        return auth

    def make_db(db_path):
        db = FakeDb(db_path)
        created["db"] = db
        return db

    def make_storage(current_settings):
        storage = FakeStorage(current_settings)
        created["storage"] = storage
        return storage

    def make_service(api, db, storage, settings, sync_check_scope=None):
        service = FakeSyncService(api, db, storage, settings, sync_check_scope=sync_check_scope)
        created["service"] = service
        return service

    monkeypatch.setattr(quick_sync, "PixivAuthManager", make_auth)
    monkeypatch.setattr(quick_sync, "Database", make_db)
    monkeypatch.setattr(quick_sync, "FileStorage", make_storage)
    monkeypatch.setattr(quick_sync, "BookmarkNovelSyncService", make_service)
    return created


def test_run_bookmark_sync_stops_before_login(settings, quick_sync_env):
    with pytest.raises(InterruptedError, match="Task stopped by user"):
        quick_sync.run_bookmark_sync(settings, stop_requested=lambda: True)

    assert "auth" not in quick_sync_env


def test_run_bookmark_sync_rebuilds_catalog_once_after_success(settings, quick_sync_env):
    result = quick_sync.run_bookmark_sync(settings)

    assert result["rescue_catalog_items"] == 4
    assert result["rescue_catalog_sources"] == 5
    assert quick_sync_env["db"].rebuild_catalog_calls == 1
    assert quick_sync_env["db"].closed is True


def test_run_bookmark_sync_keeps_stats_when_catalog_rebuild_fails(
    settings, quick_sync_env, monkeypatch, caplog
):
    def fail_rebuild(self):
        self.rebuild_catalog_calls += 1
        raise RuntimeError("catalog boom")

    monkeypatch.setattr(FakeDb, "rebuild_rescue_catalog", fail_rebuild)

    result = quick_sync.run_bookmark_sync(settings)

    assert result == {"novels": 1, "skipped": 0, "assets_downloaded": 0}
    assert quick_sync_env["db"].rebuild_catalog_calls == 1
    assert "救援目录刷新失败: catalog boom" in caplog.text


def test_run_bookmark_sync_stops_from_progress_callback(settings, quick_sync_env):
    stop_calls = iter([False, True])

    with pytest.raises(InterruptedError, match="Task stopped by user"):
        quick_sync.run_bookmark_sync(settings, stop_requested=lambda: next(stop_calls))

    assert quick_sync_env["db"].closed is True
    assert quick_sync_env["db"].rebuild_catalog_calls == 0


def test_run_bookmark_sync_propagates_business_failure_without_rebuild(
    settings, quick_sync_env, monkeypatch
):
    def fail_sync(self, *args, **kwargs):
        raise RuntimeError("sync boom")

    monkeypatch.setattr(FakeSyncService, "sync", fail_sync)

    with pytest.raises(RuntimeError, match="sync boom"):
        quick_sync.run_bookmark_sync(settings)

    assert quick_sync_env["db"].rebuild_catalog_calls == 0
    assert quick_sync_env["db"].closed is True


def test_run_bookmark_sync_skips_rebuild_when_cancelled_after_sync(
    settings, quick_sync_env, monkeypatch
):
    cancelled = False
    original_sync = FakeSyncService.sync

    def sync_then_cancel(self, *args, **kwargs):
        nonlocal cancelled
        result = original_sync(self, *args, **kwargs)
        cancelled = True
        return result

    monkeypatch.setattr(FakeSyncService, "sync", sync_then_cancel)

    with pytest.raises(InterruptedError, match="Task stopped by user"):
        quick_sync.run_bookmark_sync(settings, stop_requested=lambda: cancelled)

    assert quick_sync_env["db"].rebuild_catalog_calls == 0
    assert quick_sync_env["db"].closed is True


def test_run_bookmark_sync_skips_rebuild_when_finalization_claim_is_rejected(
    settings, quick_sync_env
):
    claim_calls = []

    with pytest.raises(InterruptedError, match="Task stopped by user"):
        quick_sync.run_bookmark_sync(
            settings,
            claim_finalization=lambda: claim_calls.append(True) or False,
        )

    assert claim_calls == [True]
    assert quick_sync_env["db"].rebuild_catalog_calls == 0
    assert quick_sync_env["db"].closed is True


def test_run_check_bookmarks_task_stops_before_login(settings, quick_sync_env):
    manager = FakeJobManager()

    with pytest.raises(InterruptedError, match="Task stopped by user"):
        quick_sync.run_check_bookmarks_task(
            settings,
            manager,
            "job-1",
            release_semaphore=False,
            raise_on_error=True,
            stop_requested=lambda: True,
        )

    assert "auth" not in quick_sync_env
    assert manager.released is False


def test_run_check_bookmarks_task_stops_from_progress_callback(settings, quick_sync_env):
    manager = FakeJobManager()
    stop_calls = iter([False, True])

    with pytest.raises(InterruptedError, match="Task stopped by user"):
        quick_sync.run_check_bookmarks_task(
            settings,
            manager,
            "job-1",
            release_semaphore=False,
            raise_on_error=True,
            stop_requested=lambda: next(stop_calls),
        )

    assert quick_sync_env["db"].closed is True
    assert manager.released is False

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pixiv_novel_sync.jobs import services


class DummyReporter:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []
        self.progress_updates: list[dict[str, object]] = []

    def add_log(self, level: str, message: str) -> None:
        self.logs.append((level, message))

    def update_progress(self, **kwargs: object) -> None:
        self.progress_updates.append(kwargs)


class FakeConn:
    def __init__(self, users: list[tuple[int, str]]) -> None:
        self.users = users
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, params: tuple[object, ...] = ()):
        self.executed.append((query, params))
        if "SELECT user_id FROM users ORDER BY user_id" in query:
            return SimpleNamespace(fetchall=lambda: [(user_id,) for user_id, _name in self.users])
        if "SELECT name FROM users WHERE user_id = ?" in query:
            target_id = int(params[0])
            for user_id, name in self.users:
                if user_id == target_id:
                    return SimpleNamespace(fetchone=lambda: (name,))
            return SimpleNamespace(fetchone=lambda: None)
        raise AssertionError(f"Unexpected query: {query}")


class FakeDatabase:
    def __init__(self, db_path) -> None:
        self.db_path = db_path
        self.init_schema_called = False
        self.closed = False
        self.user_pages = [
            {"items": [{"user_id": 101, "name": "Alice"}], "total_pages": 2},
            {"items": [{"user_id": 202, "name": "Bob"}], "total_pages": 2},
        ]
        self.list_users_calls: list[tuple[int, int]] = []
        self.user_status_upserts: list[tuple[int, str]] = []
        self.novel_ids = [11, 22]
        self.novel_status_upserts: list[tuple[int, str]] = []
        self.series_ids = [31, 42]
        self.series_status_upserts: list[tuple[int, str]] = []
        self.backup_users = [(101, "Alice"), (202, "Bob")]
        self.conn = FakeConn(self.backup_users)
        self.watermark_updates: list[tuple[str, dict[str, object]]] = []

    def init_schema(self) -> None:
        self.init_schema_called = True

    def close(self) -> None:
        self.closed = True

    def list_users(self, page: int = 1, page_size: int = 10, status: str = "all"):
        self.list_users_calls.append((page, page_size))
        return self.user_pages[page - 1] if page <= len(self.user_pages) else {"items": [], "total_pages": page - 1}

    def upsert_user_status(self, user_id: int, status: str) -> None:
        self.user_status_upserts.append((user_id, status))

    def get_all_novel_ids(self) -> list[int]:
        return list(self.novel_ids)

    def upsert_novel_status(self, novel_id: int, status: str) -> None:
        self.novel_status_upserts.append((novel_id, status))

    def get_all_series_ids(self) -> list[int]:
        return list(self.series_ids)

    def upsert_series_status(self, series_id: int, status: str) -> None:
        self.series_status_upserts.append((series_id, status))

    def update_watermark(self, key: str, value: dict[str, object]) -> None:
        self.watermark_updates.append((key, value))


class FakeBookmarkNovelSyncService:
    def __init__(self, api, db, storage, settings) -> None:
        self.api = api
        self.db = db
        self.storage = storage
        self.settings = settings
        self.calls: list[dict[str, object]] = []
        self.detection_calls: list[dict[str, object]] = []
        self.detection_result: dict[str, object] = {
            "bookmark": {"new_pending": 1},
            "series": {"new_pending": 2},
            "new_pending": 3,
        }
        self.detection_error: Exception | None = None

    def _sync_novel(
        self,
        novel,
        restrict: str,
        download_assets: bool,
        write_markdown: bool,
        write_raw_text: bool,
        source_type: str,
        source_key: str | None = None,
    ) -> dict[str, int]:
        call = {
            "novel_id": int(novel.id),
            "restrict": restrict,
            "download_assets": download_assets,
            "write_markdown": write_markdown,
            "write_raw_text": write_raw_text,
            "source_type": source_type,
            "source_key": source_key,
        }
        self.calls.append(call)
        return {"novels": 1, "assets_downloaded": 2}

    def run_detection(self, user_id: int, restricts, progress_callback=None) -> dict[str, object]:
        self.detection_calls.append(
            {
                "user_id": user_id,
                "restricts": list(restricts),
                "progress_callback": progress_callback,
            }
        )
        if self.detection_error is not None:
            raise self.detection_error
        if progress_callback is not None:
            progress_callback("phase", {"phase": "检测收藏状态"})
            progress_callback("rate_limit", {"seconds": 1})
        return dict(self.detection_result)


class FakeApi:
    def __init__(self) -> None:
        self.user_novels_calls: list[dict[str, object]] = []
        self.user_novels_pages: dict[int, list[SimpleNamespace]] = {}
        self.parse_qs_results: dict[object, dict[str, object] | None] = {}

    def user_novels(self, **query):
        self.user_novels_calls.append(dict(query))
        user_id = int(query["user_id"])
        page = int(query.get("page", 1))
        novels = self.user_novels_pages.get(
            page,
            [SimpleNamespace(id=user_id * 10 + 1), SimpleNamespace(id=user_id * 10 + 2)],
        )
        next_url = None
        if page < len(self.user_novels_pages):
            next_url = f"page-{page + 1}"
        return SimpleNamespace(novels=novels, next_url=next_url)

    def parse_qs(self, next_url):
        return self.parse_qs_results.get(next_url)


class FakeStorage:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.ensure_dirs_calls: list[list[object]] = []
        self.ensure_dirs_error: Exception | None = None

    def ensure_dirs(self, dirs: list[object]) -> None:
        self.ensure_dirs_calls.append(dirs)
        if self.ensure_dirs_error is not None:
            raise self.ensure_dirs_error


class FakeAuthManager:
    def __init__(self, pixiv_settings) -> None:
        self.pixiv_settings = pixiv_settings
        self.api = FakeApi()
        self.auth_result = SimpleNamespace(user_id=999)
        self.login_called = False

    def login(self):
        self.login_called = True
        return self.api, self.auth_result


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
            delay_seconds_between_skips=0,
            download_assets=True,
            write_markdown=True,
            write_raw_text=False,
            delay_seconds_between_items=0,
            delay_seconds_between_pages=0,
        ),
    )


@pytest.fixture
def service_env(monkeypatch):
    created: dict[str, object] = {}

    def make_db(db_path):
        db = FakeDatabase(db_path)
        created["db"] = db
        return db

    def make_storage(settings):
        storage = FakeStorage(settings)
        created["storage"] = storage
        return storage

    def make_auth(pixiv_settings):
        auth = FakeAuthManager(pixiv_settings)
        created["auth"] = auth
        return auth

    def make_sync_service(api, db, storage, settings):
        sync_service = FakeBookmarkNovelSyncService(api, db, storage, settings)
        created["sync_service"] = sync_service
        return sync_service

    monkeypatch.setattr(services, "Database", make_db)
    monkeypatch.setattr(services, "FileStorage", make_storage)
    monkeypatch.setattr(services, "PixivAuthManager", make_auth)
    monkeypatch.setattr(services, "time", SimpleNamespace(sleep=lambda seconds: None))
    monkeypatch.setattr(services, "BookmarkNovelSyncService", make_sync_service, raising=False)
    return created


def test_run_user_status_task_calls_user_db_and_status_checker(settings, service_env, monkeypatch):
    checked_user_ids: list[int] = []

    def fake_check(api, user_id: int) -> str:
        checked_user_ids.append(user_id)
        return {101: "normal", 202: "no_novels"}[user_id]

    monkeypatch.setattr(services, "_check_pixiv_user_status", fake_check)
    reporter = DummyReporter()

    result = services.run_user_status_task(settings, reporter=reporter)

    db = service_env["db"]
    assert checked_user_ids == [101, 202]
    assert db.list_users_calls == [(1, 500), (2, 500)]
    assert db.user_status_upserts == [(101, "normal"), (202, "no_novels")]
    assert db.closed is True
    assert result == {
        "checked_count": 2,
        "total_users": 2,
        "status_counts": {"normal": 1, "no_novels": 1},
        "stopped": False,
    }
    assert reporter.progress_updates[-1]["current"] == 2
    assert reporter.progress_updates[-1]["total"] == 2


def test_run_novel_status_task_calls_novel_db_and_status_checker(settings, service_env, monkeypatch):
    checked_novel_ids: list[int] = []

    def fake_check(api, novel_id: int) -> str:
        checked_novel_ids.append(novel_id)
        return {11: "normal", 22: "restricted"}[novel_id]

    monkeypatch.setattr(services, "_check_novel_status", fake_check)

    result = services.run_novel_status_task(settings)

    db = service_env["db"]
    assert checked_novel_ids == [11, 22]
    assert db.novel_status_upserts == [(11, "normal"), (22, "restricted")]
    assert db.closed is True
    assert result == {
        "checked_count": 2,
        "total_novels": 2,
        "status_counts": {"normal": 1, "restricted": 1},
        "stopped": False,
    }


def test_run_series_status_task_calls_series_db_and_status_checker(settings, service_env, monkeypatch):
    checked_series_ids: list[int] = []

    def fake_check(api, series_id: int) -> str:
        checked_series_ids.append(series_id)
        return {31: "normal", 42: "deleted"}[series_id]

    monkeypatch.setattr(services, "_check_series_status", fake_check)

    result = services.run_series_status_task(settings)

    db = service_env["db"]
    assert checked_series_ids == [31, 42]
    assert db.series_status_upserts == [(31, "normal"), (42, "deleted")]
    assert db.closed is True
    assert result == {
        "checked_count": 2,
        "total_series": 2,
        "status_counts": {"normal": 1, "deleted": 1},
        "stopped": False,
    }


def test_run_user_status_task_stops_when_requested(settings, service_env, monkeypatch):
    monkeypatch.setattr(services, "_check_pixiv_user_status", lambda api, user_id: "normal")
    stop_calls = iter([False, True])

    result = services.run_user_status_task(settings, stop_requested=lambda: next(stop_calls))

    db = service_env["db"]
    assert db.user_status_upserts == [(101, "normal")]
    assert result == {
        "checked_count": 1,
        "total_users": 2,
        "status_counts": {"normal": 1},
        "stopped": True,
    }


def test_run_novel_status_task_accepts_missing_reporter(settings, service_env, monkeypatch):
    monkeypatch.setattr(services, "_check_novel_status", lambda api, novel_id: "normal")

    result = services.run_novel_status_task(settings, reporter=None)

    assert result["checked_count"] == 2
    assert result["stopped"] is False


def test_run_user_backup_task_syncs_target_user_novels_with_expected_options(settings, service_env):
    reporter = DummyReporter()

    result = services.run_user_backup_task(settings, user_id=202, reporter=reporter)

    auth = service_env["auth"]
    db = service_env["db"]
    storage = service_env["storage"]
    sync_service = service_env["sync_service"]

    assert settings.pixiv.user_id == 999
    assert sync_service.api is auth.api
    assert sync_service.db is db
    assert sync_service.storage is storage
    assert sync_service.settings is settings
    assert auth.api.user_novels_calls == [{"user_id": 202}]
    assert sync_service.calls == [
        {
            "novel_id": 2021,
            "restrict": "public",
            "download_assets": True,
            "write_markdown": True,
            "write_raw_text": False,
            "source_type": "user_backup",
            "source_key": "202",
        },
        {
            "novel_id": 2022,
            "restrict": "public",
            "download_assets": True,
            "write_markdown": True,
            "write_raw_text": False,
            "source_type": "user_backup",
            "source_key": "202",
        },
    ]
    assert db.closed is True
    assert result == {
        "user_id": 202,
        "novels": 2,
        "skipped": 0,
        "assets_downloaded": 4,
        "stopped": False,
    }
    assert reporter.progress_updates[-1]["current"] == 2
    assert reporter.progress_updates[-1]["total"] == 2


def test_run_user_backup_task_accepts_missing_reporter(settings, service_env):
    result = services.run_user_backup_task(settings, user_id=101, reporter=None)

    assert result["user_id"] == 101
    assert result["novels"] == 2
    assert result["stopped"] is False


def test_run_pending_deletion_detection_task_calls_service_and_returns_stats(settings, service_env):
    settings.sync.bookmark_restricts = ["public", "private"]
    reporter = DummyReporter()

    result = services.run_pending_deletion_detection_task(settings, reporter=reporter)

    auth = service_env["auth"]
    db = service_env["db"]
    storage = service_env["storage"]
    sync_service = service_env["sync_service"]

    assert settings.pixiv.user_id == 999
    assert sync_service.api is auth.api
    assert sync_service.db is db
    assert sync_service.storage is storage
    assert sync_service.settings is settings
    assert sync_service.detection_calls == [
        {
            "user_id": 999,
            "restricts": ["public", "private"],
            "progress_callback": sync_service.detection_calls[0]["progress_callback"],
        }
    ]
    assert db.closed is True
    assert result == {
        "bookmark": {"new_pending": 1},
        "series": {"new_pending": 2},
        "new_pending": 3,
        "stopped": False,
    }
    assert reporter.logs[0] == ("info", "=== 开始检测取消收藏/追更 ===")
    assert reporter.logs[1] == ("success", "登录成功, 用户ID: 999")
    assert reporter.logs[-1] == ("success", "检测完成: 发现 3 条新的待确认记录")
    assert reporter.progress_updates == [{"phase": "pending_deletion_detection", "current": 0, "total": 0}]



def test_run_pending_deletion_detection_task_preserves_existing_stopped_flag(settings, service_env, monkeypatch):
    original_factory = services.BookmarkNovelSyncService

    def make_sync_service(api, db, storage, settings):
        sync_service = original_factory(api, db, storage, settings)
        sync_service.detection_result = {
            "bookmark": {"new_pending": 0},
            "series": {"new_pending": 0},
            "new_pending": 0,
            "stopped": True,
        }
        service_env["sync_service"] = sync_service
        return sync_service

    monkeypatch.setattr(services, "BookmarkNovelSyncService", make_sync_service)

    result = services.run_pending_deletion_detection_task(settings)

    assert result["stopped"] is True
    assert service_env["db"].closed is True

    monkeypatch.setattr(services, "BookmarkNovelSyncService", original_factory)



def test_run_pending_deletion_detection_task_returns_stopped_when_cancelled_during_detection(
    settings, service_env
):
    stop_calls = iter([False, False, False, True])

    result = services.run_pending_deletion_detection_task(settings, stop_requested=lambda: next(stop_calls))

    assert result == {
        "bookmark": {},
        "series": {},
        "new_pending": 0,
        "stopped": True,
    }
    assert service_env["db"].closed is True



def test_run_pending_deletion_detection_task_accepts_missing_reporter(settings, service_env):
    result = services.run_pending_deletion_detection_task(settings, reporter=None)

    assert result == {
        "bookmark": {"new_pending": 1},
        "series": {"new_pending": 2},
        "new_pending": 3,
        "stopped": False,
    }
    assert service_env["db"].closed is True



def test_run_pending_deletion_detection_task_stops_before_initialization_when_requested(settings, service_env):
    result = services.run_pending_deletion_detection_task(settings, stop_requested=lambda: True)

    assert "auth" not in service_env
    assert "db" not in service_env
    assert "storage" not in service_env
    assert "sync_service" not in service_env
    assert result == {
        "bookmark": {},
        "series": {},
        "new_pending": 0,
        "stopped": True,
    }
    assert settings.pixiv.user_id is None



def test_run_pending_deletion_detection_task_closes_db_when_storage_init_fails(settings, service_env, monkeypatch):
    storage = service_env.setdefault("storage", FakeStorage(settings))
    storage.ensure_dirs_error = RuntimeError("storage init failed")
    monkeypatch.setattr(services, "FileStorage", lambda current_settings: storage)

    with pytest.raises(RuntimeError, match="storage init failed"):
        services.run_pending_deletion_detection_task(settings)

    assert service_env["db"].init_schema_called is True
    assert service_env["db"].closed is True
    assert "sync_service" not in service_env
    assert settings.pixiv.user_id == 999



def test_run_pending_deletion_detection_task_closes_db_and_propagates_errors(settings, service_env, monkeypatch):
    original_factory = services.BookmarkNovelSyncService

    def make_sync_service(api, db, storage, settings):
        sync_service = original_factory(api, db, storage, settings)
        sync_service.detection_error = RuntimeError("boom")
        service_env["sync_service"] = sync_service
        return sync_service

    monkeypatch.setattr(services, "BookmarkNovelSyncService", make_sync_service)

    with pytest.raises(RuntimeError, match="boom"):
        services.run_pending_deletion_detection_task(settings)

    assert service_env["db"].closed is True
    assert settings.pixiv.user_id == 999

    monkeypatch.setattr(services, "BookmarkNovelSyncService", original_factory)



def test_run_user_backup_task_stops_before_initialization_when_requested(settings, service_env):
    result = services.run_user_backup_task(settings, user_id=101, stop_requested=lambda: True)

    assert "auth" not in service_env
    assert "db" not in service_env
    assert "storage" not in service_env
    assert "sync_service" not in service_env
    assert result == {
        "user_id": 101,
        "novels": 0,
        "skipped": 0,
        "assets_downloaded": 0,
        "stopped": True,
    }
    assert settings.pixiv.user_id is None


def test_run_user_backup_task_raises_on_failed_sync_counter_and_closes_db(settings, service_env, monkeypatch):
    original_factory = services.BookmarkNovelSyncService

    def make_sync_service(api, db, storage, settings):
        sync_service = original_factory(api, db, storage, settings)

        def fail_sync(*args, **kwargs):
            return {"novels": 0, "skipped": 0, "assets_downloaded": 0, "failed": 1}

        sync_service._sync_novel = fail_sync
        service_env["sync_service"] = sync_service
        return sync_service

    monkeypatch.setattr(services, "BookmarkNovelSyncService", make_sync_service)

    with pytest.raises(RuntimeError, match="novel sync failures"):
        services.run_user_backup_task(settings, user_id=101)

    assert service_env["db"].closed is True
    assert service_env["auth"].api.user_novels_calls == [{"user_id": 101}]

    monkeypatch.setattr(services, "BookmarkNovelSyncService", original_factory)




def test_run_user_backup_task_closes_db_on_sync_error(settings, service_env, monkeypatch):
    original_factory = services.BookmarkNovelSyncService

    def make_sync_service(api, db, storage, settings):
        sync_service = original_factory(api, db, storage, settings)

        def raise_sync_error(*args, **kwargs):
            raise RuntimeError("boom")

        sync_service._sync_novel = raise_sync_error
        service_env["sync_service"] = sync_service
        return sync_service

    monkeypatch.setattr(services, "BookmarkNovelSyncService", make_sync_service)

    with pytest.raises(RuntimeError, match="boom"):
        services.run_user_backup_task(settings, user_id=101)

    assert service_env["db"].closed is True
    assert service_env["auth"].api.user_novels_calls == [{"user_id": 101}]
    assert settings.pixiv.user_id == 999
    assert service_env["storage"].ensure_dirs_calls
    assert service_env["db"].init_schema_called is True
    assert service_env["db"].watermark_updates == []
    assert service_env["db"].conn.executed[-1] == ("SELECT name FROM users WHERE user_id = ?", (101,))
    assert service_env["auth"].api.parse_qs(None) is None
    assert "sync_service" in service_env

    monkeypatch.setattr(services, "BookmarkNovelSyncService", original_factory)



def test_run_user_backup_task_closes_db_after_success(settings, service_env):
    services.run_user_backup_task(settings, user_id=101)

    assert service_env["db"].closed is True


def test_run_user_backup_task_stops_before_syncing_next_page_and_closes_db(settings, service_env, monkeypatch):
    settings.pixiv.user_id = 999
    auth = FakeAuthManager(settings.pixiv)
    auth.api.user_novels_pages = {
        1: [SimpleNamespace(id=1011), SimpleNamespace(id=1012)],
        2: [SimpleNamespace(id=1013)],
    }
    auth.api.parse_qs_results = {"page-2": {"user_id": 101, "page": 2}}
    service_env["auth"] = auth
    monkeypatch.setattr(services, "PixivAuthManager", lambda pixiv_settings: auth)
    stop_calls = iter([False, False, False, False, True])

    result = services.run_user_backup_task(settings, user_id=101, stop_requested=lambda: next(stop_calls))

    assert result == {
        "user_id": 101,
        "novels": 2,
        "skipped": 0,
        "assets_downloaded": 4,
        "stopped": True,
    }
    assert auth.api.user_novels_calls == [{"user_id": 101}]
    assert [call["novel_id"] for call in service_env["sync_service"].calls] == [1011, 1012]
    assert service_env["db"].closed is True



def test_run_user_backup_task_stops_before_syncing_next_novel_and_closes_db(settings, service_env):
    stop_calls = iter([False, False, False, True])

    result = services.run_user_backup_task(settings, user_id=101, stop_requested=lambda: next(stop_calls))

    assert result == {
        "user_id": 101,
        "novels": 1,
        "skipped": 0,
        "assets_downloaded": 2,
        "stopped": True,
    }
    assert [call["novel_id"] for call in service_env["sync_service"].calls] == [1011]
    assert service_env["db"].closed is True



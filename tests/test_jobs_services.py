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


class FakeStorage:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.ensure_dirs_calls: list[list[object]] = []

    def ensure_dirs(self, dirs: list[object]) -> None:
        self.ensure_dirs_calls.append(dirs)


class FakeAuthManager:
    def __init__(self, pixiv_settings) -> None:
        self.pixiv_settings = pixiv_settings
        self.api = SimpleNamespace(name="api")
        self.auth_result = SimpleNamespace(user_id=999)

    def login(self):
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
        sync=SimpleNamespace(delay_seconds_between_skips=0),
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

    monkeypatch.setattr(services, "Database", make_db)
    monkeypatch.setattr(services, "FileStorage", make_storage)
    monkeypatch.setattr(services, "PixivAuthManager", make_auth)
    monkeypatch.setattr(services.time, "sleep", lambda seconds: None)
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

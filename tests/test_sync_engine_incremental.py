from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pixiv_novel_sync.models import NovelRecord, NovelTextRecord, UserRecord
from pixiv_novel_sync.storage_db import Database
import pytest

from pixiv_novel_sync import sync_engine
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService, _to_plain
from pixiv_novel_sync.utils_hashing import sha256_text, stable_json_dumps
from pixiv_novel_sync.utils_text import normalize_text


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        pixiv=SimpleNamespace(timeout=1, verify_ssl=True, proxy=None),
        sync=SimpleNamespace(
            delay_seconds_between_pages=0,
            delay_seconds_between_items=0,
            delay_seconds_between_skips=0,
            max_items_per_run=None,
            max_pages_per_run=None,
            download_assets=True,
            sync_bookmarks=True,
            sync_following_novels=False,
            sync_subscribed_series=False,
        ),
        storage=SimpleNamespace(
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
            db_path=tmp_path / "test.db",
        ),
    )


class _ImageUrls:
    large = "https://i.pximg.net/img-original/img/1.jpg"
    medium = None
    square_medium = None


class _User:
    id = 1
    name = "author"
    account = "acc"


class _Novel:
    id = 100
    user = _User()
    caption = "caption"
    tags = []
    image_urls = _ImageUrls()
    series = None
    title = "title"
    visible = True
    x_restrict = 0
    text_length = 4
    total_bookmarks = 2
    total_view = 3
    create_date = "2026-01-01T00:00:00+00:00"


class _Api:
    def __init__(self, novel: object = _Novel(), body: str = "body") -> None:
        self.novel = novel
        self.body = body
        self.bookmark_calls = []

    def novel_detail(self, novel_id: int) -> SimpleNamespace:
        return SimpleNamespace(novel=self.novel)

    def webview_novel(self, novel_id: int) -> dict:
        return {"text": self.body}

    def user_bookmarks_novel(self, **kwargs):
        self.bookmark_calls.append(kwargs)
        return SimpleNamespace(novels=[SimpleNamespace(id=100), SimpleNamespace(id=101)], next_url=None)

    def parse_qs(self, next_url):
        return None


class _Storage:
    def __init__(self) -> None:
        self.text_writes = []
        self.downloads = []

    def novel_dir(self, restrict, user_id, user_name, novel_id, title):
        return Path("archive") / str(novel_id)

    def write_text(self, path, text):
        self.text_writes.append((path, text))

    def asset_path(self, novel_dir, asset_type, filename):
        return novel_dir / asset_type / filename

    def download_asset(self, url, target, timeout, verify_ssl, proxy):
        self.downloads.append((url, target))
        return "asset-hash"


def test_unchanged_novel_skips_text_db_writes_and_repairs_missing_assets(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    novel = _Novel()
    body = normalize_text("body")
    meta_plain = _to_plain(novel)
    db.upsert_user(UserRecord(user_id=1, name="author", account="acc", raw_json="{}"))
    db.upsert_novel(
        NovelRecord(
            novel_id=100,
            user_id=1,
            series_id=None,
            title="title",
            caption="caption",
            visible=True,
            restrict="public",
            x_restrict=0,
            text_length=4,
            total_bookmarks=2,
            total_views=3,
            cover_url="https://i.pximg.net/img-original/img/1.jpg",
            tags_json="[]",
            create_date="2026-01-01T00:00:00+00:00",
            raw_json=stable_json_dumps(meta_plain),
            meta_hash=sha256_text(stable_json_dumps(meta_plain)),
        )
    )
    db.upsert_novel_text(NovelTextRecord(novel_id=100, text_raw=body, text_markdown=None, text_hash=sha256_text(body)))
    storage = _Storage()
    service = BookmarkNovelSyncService(_Api(novel, body), db, storage, settings)

    result = service._sync_novel_inner(
        100,
        novel,
        "public",
        download_assets=True,
        write_markdown=True,
        write_raw_text=True,
        source_type="bookmark_public",
        source_key="1",
    )

    assert result["skipped"] == 1
    assert result["assets_downloaded"] == 1
    assert storage.text_writes == []
    assert db.get_recorded_asset_urls(100) == {"https://i.pximg.net/img-original/img/1.jpg"}
    assert db.conn.execute("SELECT 1 FROM sources WHERE novel_id = 100 AND source_type = 'bookmark_public'").fetchone() is not None


def test_check_bookmarks_existence_batches_sync_check_writes(tmp_path: Path) -> None:
    class FakeDb:
        def __init__(self):
            self.items = None
            self.scope = None

        def init_sync_check_table(self):
            pass

        def clear_sync_check_list(self, scope):
            self.scope = scope

        def get_existing_novel_ids(self, novel_ids, require_assets=False):
            assert novel_ids == [100, 101]
            assert require_assets is True
            return {100}

        def upsert_sync_check_items(self, items, scope="_"):
            self.items = items
            self.scope = scope

    settings = _settings(tmp_path)
    db = FakeDb()
    service = BookmarkNovelSyncService(_Api(), db, _Storage(), settings, sync_check_scope="scope")

    result = service.check_bookmarks_existence(1, ["public"])

    assert result == {"total_checked": 2, "existing": 1, "new": 1}
    assert db.items == [(100, True), (101, False)]
    assert db.scope == "scope"


def test_sleep_with_progress_cancel_raises_when_progress_callback_requests_stop(monkeypatch) -> None:
    slept = []
    events = []

    def progress_callback(event_type, data):
        events.append((event_type, data))
        if event_type == "_cancel_check":
            raise InterruptedError("Task stopped by user")

    monkeypatch.setattr(sync_engine.time, "sleep", lambda seconds: slept.append(seconds))

    with pytest.raises(InterruptedError, match="Task stopped by user"):
        sync_engine._sleep_with_progress_cancel(1.0, progress_callback, interval=0.25)

    assert slept == []
    assert events == [("_cancel_check", {})]


def test_sync_uses_cancellable_sleep_for_item_delay(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    settings.sync.delay_seconds_between_items = 1.0
    db = Database(settings.storage.db_path)
    db.init_schema()
    storage = _Storage()
    service = BookmarkNovelSyncService(_Api(), db, storage, settings)
    sleep_calls = []

    def fake_sleep(seconds, progress_callback, interval=0.2):
        sleep_calls.append((seconds, progress_callback, interval))

    monkeypatch.setattr(sync_engine, "_sleep_with_progress_cancel", fake_sleep)

    try:
        result = service.sync(1, ["public"], progress_callback=lambda event_type, data: None)
    finally:
        db.close()

    assert result["novels"] == 2
    assert sleep_calls == [(1.0, sleep_calls[0][1], 0.2), (1.0, sleep_calls[1][1], 0.2)]


def test_sync_engine_sleep_calls_are_routed_through_cancellable_helper() -> None:
    source = Path(sync_engine.__file__).read_text(encoding="utf-8")
    raw_sleep_lines = [
        line.strip()
        for line in source.splitlines()
        if "time.sleep(" in line and "time.sleep(seconds)" not in line and "time.sleep(sleep_for)" not in line
    ]

    assert raw_sleep_lines == []


def test_sync_novel_propagates_interrupted_error(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    service = BookmarkNovelSyncService(_Api(), db, _Storage(), settings)

    monkeypatch.setattr(
        service,
        "_sync_novel_inner",
        lambda *args, **kwargs: (_ for _ in ()).throw(InterruptedError("Task stopped by user")),
    )

    try:
        with pytest.raises(InterruptedError, match="Task stopped by user"):
            service._sync_novel(_Novel(), "public", True, True, True, source_type="bookmark_public")
    finally:
        db.close()


def test_stop_requested_from_progress_returns_none_when_no_callback() -> None:
    assert sync_engine._stop_requested_from_progress(None) is None


def test_stop_requested_from_progress_bridges_interrupted_error() -> None:
    events: list[str] = []

    def progress_callback(event_type: str, data) -> None:
        events.append(event_type)
        raise InterruptedError("Task stopped by user")

    stop = sync_engine._stop_requested_from_progress(progress_callback)
    assert stop is not None
    assert stop() is True
    assert events == ["_cancel_check"]


def test_stop_requested_from_progress_returns_false_when_not_stopped() -> None:
    def progress_callback(event_type: str, data) -> None:
        return None

    stop = sync_engine._stop_requested_from_progress(progress_callback)
    assert stop is not None
    assert stop() is False

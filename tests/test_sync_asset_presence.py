"""补缺资源要看磁盘，不能只看 assets 表；资源写入要 fsync（审查 S9）。

以前磁盘上被删或截断成空文件的封面，assets 表仍有记录：`novel_archive_complete` 为真，
作品被永远跳过；强制重访（全量备份）也因「已记录」而不重下。资源文件在 `os.replace`
之前不 fsync，掉电后可能留下截断的文件，表里照样记着「已下载」。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from pixiv_novel_sync import sync_engine
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.storage_files import FileStorage
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService

NOVEL_ID = 4242


class _OfflineStorage(FileStorage):
    """真实的目录布局与原子写，只把资源下载换成本地写文件。"""

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.downloads: list[str] = []

    def download_asset(self, url, target, timeout, verify_ssl, proxy, max_retries=3, stop_requested=None):
        self.downloads.append(url)
        return self.write_bytes(Path(target), b"img")


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        pixiv=SimpleNamespace(timeout=1, verify_ssl=True, proxy=None, web_cookie=None),
        sync=SimpleNamespace(
            delay_seconds_between_pages=0,
            delay_seconds_between_items=0,
            delay_seconds_between_skips=0,
            max_items_per_run=None,
            max_pages_per_run=None,
            bookmark_max_pages_per_run=None,
        ),
        storage=SimpleNamespace(
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
            db_path=tmp_path / "test.db",
        ),
    )


def _novel(novel_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=novel_id,
        user=SimpleNamespace(id=7, name="author", account="acc"),
        caption="",
        tags=[],
        image_urls=SimpleNamespace(large=f"https://i.pximg.net/c/{novel_id}.jpg", medium=None, square_medium=None),
        series=None,
        title=f"title {novel_id}",
        visible=True,
        x_restrict=0,
        text_length=4,
        total_bookmarks=1,
        total_view=1,
        create_date="2026-01-01T00:00:00+00:00",
    )


class _BookmarkApi:
    def __init__(self, bookmark_ids: list[int]) -> None:
        self.bookmark_ids = list(bookmark_ids)
        self.detail_calls: list[int] = []

    def user_bookmarks_novel(self, **kwargs):
        return SimpleNamespace(novels=[SimpleNamespace(id=i) for i in self.bookmark_ids], next_url=None)

    def novel_detail(self, novel_id):
        self.detail_calls.append(int(novel_id))
        return SimpleNamespace(novel=_novel(int(novel_id)))

    def webview_novel(self, novel_id):
        return {"text": "body"}

    def parse_qs(self, next_url):
        return None


def _cover(settings, novel_id: int = NOVEL_ID) -> Path:
    (cover,) = settings.storage.public_dir.rglob(f"{novel_id}.jpg")
    return cover


def test_bookmark_sync_redownloads_cover_deleted_from_disk(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    storage = _OfflineStorage(settings)
    api = _BookmarkApi([NOVEL_ID])
    try:
        service = BookmarkNovelSyncService(api, db, storage, settings)
        service.sync(1, ["public"])
        service.sync(1, ["public"])
        assert api.detail_calls == [NOVEL_ID]  # 第二轮已归档完整，直接跳过
        cover = _cover(settings)
        cover.unlink()

        stats = service.sync(1, ["public"])
    finally:
        db.close()

    # 表里还记着封面，但磁盘上没了：不能再当成归档完整跳过
    assert api.detail_calls == [NOVEL_ID, NOVEL_ID]
    assert stats["assets_downloaded"] == 1
    assert cover.stat().st_size > 0


def test_forced_revisit_redownloads_empty_cover(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    storage = _OfflineStorage(settings)
    try:
        service = BookmarkNovelSyncService(_BookmarkApi([]), db, storage, settings)
        assert service._sync_novel(_novel(NOVEL_ID), "public", True, True, True, source_type="user_backup")["novels"] == 1
        cover = _cover(settings)
        cover.write_bytes(b"")  # 掉电后留下的空文件

        counters = service._sync_novel(_novel(NOVEL_ID), "public", True, True, True, source_type="user_backup")
    finally:
        db.close()

    assert counters["skipped"] == 1  # 内容没变
    assert counters["assets_downloaded"] == 1
    assert cover.stat().st_size > 0


def test_cover_repair_visits_wait_between_requests(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    storage = _OfflineStorage(settings)
    api = _BookmarkApi([NOVEL_ID, NOVEL_ID + 1])
    try:
        service = BookmarkNovelSyncService(api, db, storage, settings)
        service.sync(1, ["public"])
        _cover(settings, NOVEL_ID).unlink()
        _cover(settings, NOVEL_ID + 1).unlink()
        slept: list[float] = []
        monkeypatch.setattr(sync_engine.time, "sleep", slept.append)
        settings.sync.delay_seconds_between_items = 0.5

        stats = service.sync(1, ["public"])
    finally:
        db.close()

    # 只补资源的访问不计预算，也要照常间隔，否则一串缺封面的作品会连发请求
    assert stats["assets_downloaded"] == 2
    assert slept == [0.5, 0.5]


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self) -> None:
        pass

    def iter_content(self, chunk_size):
        yield self.body


class _FakeSession:
    def get(self, url, **kwargs):
        return _FakeResponse(b"img")


def test_atomic_writes_fsync_before_replace(tmp_path: Path, monkeypatch) -> None:
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def replace(src, dst):
        events.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    storage = FileStorage(_settings(tmp_path))
    storage._session = _FakeSession()

    storage.write_text(tmp_path / "a" / "text.txt", "正文")
    storage.write_bytes(tmp_path / "a" / "data.bin", b"x")
    file_hash = storage.download_asset(
        "https://i.pximg.net/c/1.jpg", tmp_path / "a" / "cover.jpg", timeout=1, verify_ssl=True, proxy=None
    )

    assert file_hash is not None
    assert events == ["fsync", "replace"] * 3
    assert (tmp_path / "a" / "text.txt").read_text(encoding="utf-8") == "正文"

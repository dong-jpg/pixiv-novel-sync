"""已归档判定是纯本地点查，不再 sleep delay_seconds_between_skips（审查性能 P20）。

收藏每轮约 20 页 × 30 条 × 0.1 s ≈ 60 s，6 轮/天约 6 分钟白等。真实请求之间的间隔不变。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pixiv_novel_sync import sync_engine
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService


def _settings(tmp_path: Path, skip_delay: float = 0.1) -> SimpleNamespace:
    return SimpleNamespace(
        pixiv=SimpleNamespace(timeout=1, verify_ssl=True, proxy=None, web_cookie=None, user_id=1),
        sync=SimpleNamespace(
            delay_seconds_between_pages=0,
            delay_seconds_between_items=0,
            delay_seconds_between_skips=skip_delay,
            max_items_per_run=None,
            max_pages_per_run=None,
            bookmark_max_pages_per_run=None,
            following_max_novels_per_author=None,
        ),
        storage=SimpleNamespace(public_dir=tmp_path / "public", private_dir=tmp_path / "private"),
    )


class _ArchivedDb:
    """所有作品都已归档完整（纯本地判定）。"""

    def __init__(self) -> None:
        self.watermark = None
        self.sources: list[int] = []

    def novel_archive_complete(self, novel_id, require_assets=False):
        return True

    def upsert_source(self, record):
        self.sources.append(int(record.novel_id))

    def touch_novel(self, novel_id):
        pass

    def update_watermark(self, key, value):
        self.watermark = value

    def get_watermark(self, key):
        return self.watermark

    def get_authors_without_novels(self):
        return set()


class _Api:
    def __init__(self, novel_ids: list[int]) -> None:
        self.novel_ids = list(novel_ids)
        self.detail_calls = 0

    def user_bookmarks_novel(self, **kwargs):
        return SimpleNamespace(novels=[SimpleNamespace(id=i) for i in self.novel_ids], next_url=None)

    def user_following(self, **kwargs):
        return SimpleNamespace(user_previews=[SimpleNamespace(user=SimpleNamespace(id=7, name="作者"))], next_url=None)

    def user_novels(self, **kwargs):
        return SimpleNamespace(novels=[SimpleNamespace(id=i) for i in self.novel_ids], next_url=None)

    def novel_detail(self, novel_id):
        self.detail_calls += 1
        raise AssertionError("已归档的作品不该发详情请求")

    def parse_qs(self, next_url):
        return None


def test_bookmark_local_skips_do_not_sleep(tmp_path: Path, monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(sync_engine.time, "sleep", slept.append)
    db = _ArchivedDb()
    api = _Api([1, 2, 3])

    stats = BookmarkNovelSyncService(api, db, SimpleNamespace(), _settings(tmp_path)).sync(1, ["public"])

    assert stats["skipped"] == 3
    assert db.sources == [1, 2, 3]  # 来源照常登记
    assert api.detail_calls == 0
    assert slept == []


def test_following_scan_local_skips_do_not_sleep(tmp_path: Path, monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(sync_engine.time, "sleep", slept.append)
    api = _Api([11, 12, 13])

    stats = BookmarkNovelSyncService(api, _ArchivedDb(), SimpleNamespace(), _settings(tmp_path)).sync_following_novels(
        users_limit=1
    )

    assert stats["skipped"] == 3
    assert api.detail_calls == 0
    assert slept == []

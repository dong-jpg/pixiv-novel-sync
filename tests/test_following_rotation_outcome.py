"""关注作者轮转：user_last_synced 只在作者扫描自然结束时写（审查「逻辑优化」第 2 条）。

以前 `_sync_author` 一开始就写 `user_last_synced`：被让位、取消或作品列表拉取失败的
作者都算扫过，推后整圈（约 12.8 天），是 S1 的放大器。现在失败记进 `user_fail_streak`，
连续 3 次照常写入（防止坏作者霸占队首），成功一次清零；让位/取消不写也不计数。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        pixiv=SimpleNamespace(timeout=1, verify_ssl=True, proxy=None, web_cookie=None, user_id=1),
        sync=SimpleNamespace(
            delay_seconds_between_pages=0,
            delay_seconds_between_items=0,
            delay_seconds_between_skips=0,
            max_items_per_run=None,
            max_pages_per_run=None,
            following_max_novels_per_author=None,
            series_max_pages_per_run=None,
        ),
        storage=SimpleNamespace(public_dir=tmp_path / "public", private_dir=tmp_path / "private"),
    )


class _Db:
    def __init__(self) -> None:
        self.watermark: dict | None = None

    def novel_archive_complete(self, novel_id, require_assets=False):
        return False

    def get_watermark(self, key):
        return self.watermark

    def update_watermark(self, key, value):
        self.watermark = value

    def get_authors_without_novels(self):
        return set()


class _Api:
    """关注列表固定；``failing`` 里的作者作品列表回 Pixiv 错误页。"""

    def __init__(self, author_ids: list[int], failing: set[int] = frozenset()) -> None:
        self.author_ids = list(author_ids)
        self.failing = set(failing)
        self.scanned: list[int] = []

    def user_following(self, **kwargs):
        return SimpleNamespace(
            user_previews=[SimpleNamespace(user=SimpleNamespace(id=u, name=f"作者{u}")) for u in self.author_ids],
            next_url=None,
        )

    def user_novels(self, **kwargs):
        user_id = int(kwargs["user_id"])
        self.scanned.append(user_id)
        if user_id in self.failing:
            return {"error": {"message": "Rate Limit"}}
        return SimpleNamespace(novels=[], next_url=None)

    def parse_qs(self, next_url):
        return None


def _round(tmp_path, api, db, users_limit, progress_callback=None):
    service = BookmarkNovelSyncService(api, db, SimpleNamespace(), _settings(tmp_path))
    return service.sync_following_novels(users_limit=users_limit, progress_callback=progress_callback)


def test_failed_author_is_not_marked_as_synced(tmp_path: Path) -> None:
    db = _Db()

    stats = _round(tmp_path, _Api([1, 2], failing={2}), db, users_limit=2)

    assert "1" in db.watermark["user_last_synced"]
    assert "2" not in db.watermark["user_last_synced"]
    assert db.watermark["user_fail_streak"] == {"2": 1}
    assert stats["rotation_never_synced"] == 1  # 失败的作者仍算没扫过
    assert stats["aborted_reason"] == "fetch_failed"


def test_failed_author_stays_at_the_front_until_three_failures(tmp_path: Path) -> None:
    db = _Db()
    api = _Api([1, 2], failing={1})

    for _ in range(4):
        _round(tmp_path, api, db, users_limit=1)

    # 失败的作者留在队首重试，连续 3 次后照常推到队尾，第 4 轮轮到作者 2
    assert api.scanned == [1, 1, 1, 2]
    assert db.watermark["user_fail_streak"] == {"1": 3}


def test_success_clears_the_fail_streak(tmp_path: Path) -> None:
    db = _Db()
    api = _Api([1], failing={1})
    _round(tmp_path, api, db, users_limit=1)
    _round(tmp_path, api, db, users_limit=1)
    assert db.watermark["user_fail_streak"] == {"1": 2}

    api.failing.clear()
    _round(tmp_path, api, db, users_limit=1)

    assert "1" in db.watermark["user_last_synced"]
    assert db.watermark["user_fail_streak"] == {}


def test_cancelled_author_is_neither_synced_nor_counted_as_failed(tmp_path: Path) -> None:
    db = _Db()

    def progress_callback(event_type: str, data: dict) -> None:
        if event_type == "user_start" and data.get("author_id") == 2:
            raise InterruptedError("stop")

    with pytest.raises(InterruptedError):
        _round(tmp_path, _Api([1, 2]), db, users_limit=2, progress_callback=progress_callback)

    assert "1" in db.watermark["user_last_synced"]
    assert "2" not in db.watermark["user_last_synced"]
    assert "2" not in db.watermark.get("user_fail_streak", {})

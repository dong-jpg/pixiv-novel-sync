"""收藏同步的单轮预算（审查 S8）。

`max_items_per_run` 以前把「正文确实不可获取」（content_unavailable）的条目也算进
已同步数：它们永远归档不了，每轮都排在前面重做一遍，攒够预算后排在后面的未归档收藏
永远轮不到；预算触顶又只是静默 break，任务日志一直是绿色。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pixiv_novel_sync import sync_engine
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService
from pixiv_novel_sync.webapp import _task_log_status_for_stats


def _settings(tmp_path: Path, max_items: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        pixiv=SimpleNamespace(timeout=1, verify_ssl=True, proxy=None, web_cookie=None),
        sync=SimpleNamespace(
            delay_seconds_between_pages=0,
            delay_seconds_between_items=0,
            delay_seconds_between_skips=0,
            max_items_per_run=max_items,
            max_pages_per_run=None,
            bookmark_max_pages_per_run=None,
        ),
        storage=SimpleNamespace(
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
            db_path=tmp_path / "test.db",
        ),
    )


class _Storage:
    def novel_dir(self, restrict, user_id, user_name, novel_id, title):
        return Path("archive") / str(novel_id)

    def write_text(self, path, text):
        pass

    def asset_path(self, novel_dir, asset_type, filename):
        return novel_dir / asset_type / filename

    def download_asset(self, url, target, timeout, verify_ssl, proxy, stop_requested=None):
        return "asset-hash"

    def relative_novel_dir(self, user_id, user_name, novel_id, title):
        return f"authors/{user_id}/novels/{novel_id}"

    def base_dir(self, restrict):
        return Path("archive")

    def resolve_archive_dir(self, restrict, archive_dir, user_id, user_name, novel_id, title):
        return Path("archive") / str(archive_dir)


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
    """单页收藏列表；``unavailable`` 里的作品详情回 Pixiv 的「尚无此页」错误页。"""

    def __init__(self, bookmark_ids: list[int], unavailable: set[int] = frozenset()) -> None:
        self.bookmark_ids = list(bookmark_ids)
        self.unavailable = set(unavailable)
        self.detail_calls: list[int] = []

    def user_bookmarks_novel(self, **kwargs):
        return SimpleNamespace(novels=[SimpleNamespace(id=i) for i in self.bookmark_ids], next_url=None)

    def novel_detail(self, novel_id):
        self.detail_calls.append(int(novel_id))
        if int(novel_id) in self.unavailable:
            return {"error": {"user_message": "尚无此页", "message": "", "reason": ""}}
        return SimpleNamespace(novel=_novel(int(novel_id)))

    def webview_novel(self, novel_id):
        return {"text": f"body {novel_id}"}

    def parse_qs(self, next_url):
        return None


def _run(tmp_path: Path, api: _BookmarkApi, max_items: int | None, db: Database | None = None) -> dict:
    own_db = db is None
    if db is None:
        db = Database(tmp_path / "test.db")
        db.init_schema()
    try:
        service = BookmarkNovelSyncService(api, db, _Storage(), _settings(tmp_path, max_items))
        return service.sync(1, ["public"])
    finally:
        if own_db:
            db.close()


def test_unavailable_bookmarks_do_not_consume_the_run_budget(tmp_path: Path) -> None:
    api = _BookmarkApi([1, 2, 3, 4, 5], unavailable={1, 2, 3})

    stats = _run(tmp_path, api, max_items=2)

    # 3 本失效收藏排在前面也不能挡住后面的 2 本
    assert api.detail_calls == [1, 2, 3, 4, 5]
    assert stats["content_unavailable"] == 3
    assert stats["novels"] == 2
    assert "truncated" not in stats
    assert _task_log_status_for_stats(stats) == "succeeded"


def test_budget_exhausted_with_unarchived_bookmarks_left_is_partial(tmp_path: Path) -> None:
    api = _BookmarkApi([1, 2, 3])

    stats = _run(tmp_path, api, max_items=2)

    assert api.detail_calls == [1, 2]
    assert stats["novels"] == 2
    assert stats["truncated"] is True
    assert stats["incomplete"] is True
    assert stats["budget_exhausted"] is True
    assert _task_log_status_for_stats(stats) == "partial"


def test_budget_reached_but_only_archived_bookmarks_left_is_not_truncated(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.init_schema()
    try:
        _run(tmp_path, _BookmarkApi([3]), max_items=None, db=db)  # 先把第 3 本归档好
        api = _BookmarkApi([1, 2, 3])

        stats = _run(tmp_path, api, max_items=2, db=db)
    finally:
        db.close()

    # 预算刚好用完，剩下的那本已归档：本轮其实已取完，不能误报截断
    assert api.detail_calls == [1, 2]
    assert stats["novels"] == 2
    assert stats["skipped"] == 1
    assert "truncated" not in stats
    assert "budget_exhausted" not in stats


def test_unavailable_bookmarks_still_wait_between_requests(tmp_path: Path, monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(sync_engine.time, "sleep", slept.append)
    db = Database(tmp_path / "test.db")
    db.init_schema()
    try:
        settings = _settings(tmp_path, max_items=None)
        settings.sync.delay_seconds_between_items = 0.5
        api = _BookmarkApi([1, 2], unavailable={1, 2})
        service = BookmarkNovelSyncService(api, db, _Storage(), settings)

        stats = service.sync(1, ["public"])
    finally:
        db.close()

    # 失效条目不再受预算约束，所以每次详情请求后都要照常间隔
    assert stats["content_unavailable"] == 2
    assert slept == [0.5, 0.5]

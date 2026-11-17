from __future__ import annotations

from types import SimpleNamespace

from pixivpy3.utils import JsonDict

from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService
from pixiv_novel_sync.web.managers import AutoSyncScheduler
from tests.test_sync_engine_incremental import (
    _FollowingFakeDb,
    _Novel,
    _seed_subscribed_series,
    _settings,
    _Storage,
)

# pixivpy 被限流时不抛异常，回的是这种 JsonDict（user_message 为空 ⇒ 临时故障）
_RATE_LIMIT_PAGE = {"error": {"user_message": "", "message": "Rate Limit", "reason": "", "user_message_details": {}}}
# 作品被删除或设为不可见（user_message 非空 ⇒ 确实不可获取）
_DELETED_PAGE = {"error": {"user_message": "该作品已被删除，或作品ID不存在。", "message": "", "reason": "", "user_message_details": {}}}


class _BoomApi:
    def user_bookmarks_novel(self, **kwargs):
        raise RuntimeError("boom")


def test_bookmark_page_failure_marks_fetch_failed(tmp_path) -> None:
    settings = _settings(tmp_path)
    service = BookmarkNovelSyncService(
        _BoomApi(),
        SimpleNamespace(update_watermark=lambda *args, **kwargs: None),
        SimpleNamespace(),
        settings,
    )
    stats = service.sync(1, ["public"], download_assets=False, write_markdown=False, write_raw_text=False)
    assert stats["incomplete"] is True
    assert stats["aborted_reason"] == "fetch_failed"


def test_bookmark_id_page_cap_is_not_a_bare_runtime_error(tmp_path) -> None:
    class _Endless:
        def user_bookmarks_novel(self, **kwargs):
            return SimpleNamespace(novels=[SimpleNamespace(id=1)], next_url="http://next")

        def parse_qs(self, url):
            return {"user_id": 1, "restrict": "public"}

    settings = _settings(tmp_path)
    service = BookmarkNovelSyncService(_Endless(), SimpleNamespace(), SimpleNamespace(), settings)

    def capped(user_id, restricts, progress_callback=None, max_pages=200):
        return BookmarkNovelSyncService._fetch_remote_bookmark_ids(
            service, user_id, restricts, progress_callback, 1
        )

    service._fetch_remote_bookmark_ids = capped
    stats = service.detect_unbookmarked_novels(1, ["public"])
    assert stats["truncated"] is True
    assert stats["aborted_reason"] == "fetch_failed"
    assert stats["new_pending"] == 0


def test_failed_yield_does_not_count_as_preemption() -> None:
    scheduler = AutoSyncScheduler(None, None, cancel_task=lambda job_id: False)
    assert scheduler._request_yield("job-1", "novel_status", 1) is False
    assert scheduler._task_preempt_streak.get("novel_status", 0) == 0


def test_author_novels_error_page_is_a_failure_not_an_empty_author(tmp_path) -> None:
    """回归（审查 2026-10-28 S1）：user_novels 回错误页时曾被当成空列表，
    作者照常记为已扫、推后一整圈，stats 却没有任何失败标记（日志绿色）。"""

    class _Api:
        def user_following(self, **kwargs):
            return SimpleNamespace(
                user_previews=[SimpleNamespace(user=SimpleNamespace(id=7, name="作者"))],
                next_url=None,
            )

        def user_novels(self, **kwargs):
            return JsonDict(_RATE_LIMIT_PAGE)

        def parse_qs(self, next_url):
            return None

    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    service = BookmarkNovelSyncService(_Api(), _FollowingFakeDb(), _Storage(), settings)

    stats = service.sync_following_novels()

    assert stats["failed"] == 1
    assert stats["incomplete"] is True
    assert stats["aborted_reason"] == "fetch_failed"


def test_series_next_page_error_counts_as_truncated(tmp_path, monkeypatch) -> None:
    """回归（审查 2026-10-28 S1）：系列翻页拿到错误页时静默停止，缺章的系列显示成功。"""

    class _Api:
        def __init__(self) -> None:
            self.calls = 0

        def novel_series(self, series_id, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return JsonDict({
                    "novel_series_detail": {
                        "title": "长系列",
                        "caption": "",
                        "user": {"id": 1, "name": "作者", "account": "acc"},
                        "content_count": 60,
                    },
                    "novels": [{"id": 1001}],
                    "next_url": "https://example.invalid/next?last_order=1",
                })
            return JsonDict(_RATE_LIMIT_PAGE)

        def parse_qs(self, url):
            return {"last_order": "1"}

    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    _seed_subscribed_series(db, 1, start=11471205)
    monkeypatch.setattr(db, "novel_archive_complete", lambda *a, **k: True)
    monkeypatch.setattr(db, "count_series_complete_novels", lambda *a, **k: 0)
    api = _Api()
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    try:
        stats = service.sync_subscribed_series()
    finally:
        db.close()

    assert api.calls == 2
    assert stats["truncated_series"] == 1
    assert stats["truncated"] is True
    assert stats["incomplete"] is True


class _OneChapterSeriesApi:
    def novel_series(self, series_id, **kwargs):
        return {
            "novel_series_detail": {
                "title": "系列",
                "caption": "",
                "user": {"id": 1, "name": "作者", "account": "acc"},
                "content_count": 1,
            },
            "novels": [{"id": 1001}],
            "next_url": None,
        }


def test_expired_web_cookie_falls_back_to_db_but_is_not_green(tmp_path, monkeypatch) -> None:
    """回归（审查 2026-10-28 S4）：配了 Cookie 但追更清单 401 且刷新失败时，静默回落到
    数据库里的旧订阅，stats 没有任何标记——新追更永远进不来，日志却是绿色。"""
    import requests

    settings = _settings(tmp_path)
    settings.pixiv.web_cookie = "PHPSESSID=expired"
    settings.pixiv.username = None
    settings.pixiv.password = None
    monkeypatch.setattr(requests, "get", lambda *a, **k: SimpleNamespace(status_code=401))
    db = Database(settings.storage.db_path)
    db.init_schema()
    _seed_subscribed_series(db, 1)
    monkeypatch.setattr(db, "novel_archive_complete", lambda *a, **k: True)
    monkeypatch.setattr(db, "count_series_complete_novels", lambda *a, **k: 0)
    service = BookmarkNovelSyncService(_OneChapterSeriesApi(), db, _Storage(), settings)

    try:
        stats = service.sync_subscribed_series()
    finally:
        db.close()

    assert stats["series_total"] == 1  # 回落仍然照常同步已有订阅
    assert stats["watchlist_unavailable"] is True
    assert stats["aborted_reason"] == "watchlist_unavailable"
    assert stats["incomplete"] is True


def test_without_web_cookie_the_db_fallback_stays_green(tmp_path, monkeypatch) -> None:
    """没配 Cookie 是预期内的回落，不能因为 S4 的修复变黄。"""
    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    _seed_subscribed_series(db, 1)
    monkeypatch.setattr(db, "novel_archive_complete", lambda *a, **k: True)
    monkeypatch.setattr(db, "count_series_complete_novels", lambda *a, **k: 0)
    service = BookmarkNovelSyncService(_OneChapterSeriesApi(), db, _Storage(), settings)

    try:
        stats = service.sync_subscribed_series()
    finally:
        db.close()

    assert "watchlist_unavailable" not in stats
    assert "aborted_reason" not in stats
    assert "incomplete" not in stats


def test_unfollow_detection_flags_unreadable_watchlist_only_when_cookie_is_configured(tmp_path) -> None:
    settings = _settings(tmp_path)
    service = BookmarkNovelSyncService(SimpleNamespace(), SimpleNamespace(), _Storage(), settings)
    service._fetch_remote_subscribed_series_ids = lambda: None

    assert "aborted_reason" not in service.detect_unfollowed_series()
    settings.pixiv.web_cookie = "PHPSESSID=expired"
    assert service.detect_unfollowed_series()["aborted_reason"] == "watchlist_unavailable"


class _DetailApi:
    """novel_detail 可切换成错误页；webview 始终成功且正文与归档时不同。"""

    def __init__(self, detail_page: dict | None = None) -> None:
        self.detail_page = detail_page
        self.webview_calls = 0

    def novel_detail(self, novel_id):
        if self.detail_page is not None:
            return JsonDict(self.detail_page)
        return SimpleNamespace(novel=_Novel())

    def webview_novel(self, novel_id):
        self.webview_calls += 1
        return {"text": "正文已更新" if self.detail_page is not None else "body"}


def _archived_service(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    service = BookmarkNovelSyncService(_DetailApi(), db, _Storage(), settings)
    first = service._sync_novel(_Novel(), "public", False, True, True, source_type="bookmark_public", source_key="1")
    assert first["novels"] == 1
    return service, db


def _novel_row(db):
    return tuple(db.conn.execute(
        "SELECT title, user_id, raw_json IS NOT NULL FROM novels WHERE novel_id = 100"
    ).fetchone())


def test_novel_detail_rate_limit_page_does_not_overwrite_archive(tmp_path) -> None:
    """回归（审查 2026-10-28 S2）：novel_detail 回错误 JSON 时，JsonDict 对缺键返回 None，
    getattr 的默认值不生效，已归档作品被覆写成 title=novel_<id>、user_id=0 的占位数据。"""
    service, db = _archived_service(tmp_path)
    try:
        before = _novel_row(db)
        service.api = _DetailApi(_RATE_LIMIT_PAGE)
        result = service._sync_novel(_Novel(), "public", False, True, True, source_type="bookmark_public", source_key="1")
        assert result["failed"] == 1
        assert service.api.webview_calls == 0  # 错误页之后不再继续请求、写库
        assert _novel_row(db) == before == ("title", 1, 1)
    finally:
        db.close()


def test_novel_detail_deleted_page_counts_as_unavailable_and_keeps_archive(tmp_path) -> None:
    """S2 的另一面：作品确实被删时记 content_unavailable，归档保持原样。"""
    service, db = _archived_service(tmp_path)
    try:
        service.api = _DetailApi(_DELETED_PAGE)
        result = service._sync_novel(_Novel(), "public", False, True, True, source_type="bookmark_public", source_key="1")
        assert result["content_unavailable"] == 1
        assert result["failed"] == 0
        assert _novel_row(db) == ("title", 1, 1)
    finally:
        db.close()


def test_novel_detail_without_novel_key_falls_back_to_list_item(tmp_path) -> None:
    """detail 没有 novel 字段（但不是错误页）时用列表条目的元数据，而不是 None。"""
    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()

    class _EmptyDetailApi(_DetailApi):
        def novel_detail(self, novel_id):
            return JsonDict({})

    service = BookmarkNovelSyncService(_EmptyDetailApi(), db, _Storage(), settings)
    try:
        result = service._sync_novel(_Novel(), "public", False, True, True, source_type="bookmark_public", source_key="1")
        assert result["novels"] == 1
        assert _novel_row(db) == ("title", 1, 1)
    finally:
        db.close()

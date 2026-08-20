from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from pixivpy3 import PixivError

from pixiv_novel_sync.models import NovelRecord, NovelTextRecord, UserRecord
from pixiv_novel_sync.storage_db import Database
import pytest

from pixiv_novel_sync import sync_engine
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService, _to_plain
from pixiv_novel_sync.utils_hashing import sha256_text, stable_json_dumps
from pixiv_novel_sync.utils_text import normalize_text


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        pixiv=SimpleNamespace(timeout=1, verify_ssl=True, proxy=None, web_cookie=None),
        sync=SimpleNamespace(
            delay_seconds_between_pages=0,
            delay_seconds_between_items=0,
            delay_seconds_between_skips=0,
            delay_seconds_between_series=0,
            delay_seconds_between_chapters=0,
            max_items_per_run=None,
            max_pages_per_run=None,
            download_assets=True,
            write_markdown=True,
            write_raw_text=True,
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


def test_check_bookmarks_existence_stops_at_page_safety_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class EndlessUntilThirdPageApi:
        def __init__(self) -> None:
            self.calls = 0

        def user_bookmarks_novel(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(
                novels=[SimpleNamespace(id=100 + self.calls)],
                next_url=f"page-{self.calls + 1}",
            )

        def parse_qs(self, next_url):
            if self.calls >= 3:
                return None
            return {"user_id": 1, "restrict": "public", "page": self.calls + 1}

    class FakeDb:
        def init_sync_check_table(self):
            pass

        def clear_sync_check_list(self, scope):
            pass

        def get_existing_novel_ids(self, novel_ids, require_assets=False):
            return set()

        def upsert_sync_check_items(self, items, scope="_"):
            pass

    monkeypatch.setattr(sync_engine, "_CHECK_PAGE_SAFETY_LIMIT", 2)
    api = EndlessUntilThirdPageApi()
    service = BookmarkNovelSyncService(
        api,
        FakeDb(),
        _Storage(),
        _settings(tmp_path),
    )

    result = service.check_bookmarks_existence(1, ["public"])

    assert api.calls == 2
    assert result["total_checked"] == 2


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


def test_sync_subscribed_series_propagates_interrupted_error(tmp_path: Path, monkeypatch) -> None:
    """回归：章节循环里的取消必须上抛，不能被 series 的 except Exception 吞掉。

    InterruptedError 是 Exception 子类；修复前它会落到 `except Exception` 分支，
    被当成"获取系列失败"记 warning 并继续下一个系列，取消信号被彻底湮灭。
    """
    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    # DB fallback 里放一个订阅系列（web_cookie=None 会走 DB 分支）
    db.conn.execute(
        """
        INSERT INTO series (series_id, title, user_id, cover_url, total_novels, is_subscribed)
        VALUES (777, '测试系列', 1, '', 2, 1)
        """
    )
    db.conn.commit()

    class _SeriesApi:
        def novel_series(self, series_id, **kwargs):
            return {
                "novel_series_detail": {
                    "title": "测试系列",
                    "caption": "",
                    "user": {"id": 1, "name": "作者", "account": "acc"},
                    "content_count": 2,
                },
                "novels": [{"id": 501}, {"id": 502}],
                "next_url": None,
            }

        def parse_qs(self, url):
            return None

    service = BookmarkNovelSyncService(_SeriesApi(), db, _Storage(), settings)
    # 章节实际同步时抛取消（模拟用户在系列同步中途点停止）
    monkeypatch.setattr(
        service,
        "_sync_novel",
        lambda *args, **kwargs: (_ for _ in ()).throw(InterruptedError("Task stopped by user")),
    )
    # 让本地判定为"未完整"，从而进入章节同步而非跳过
    monkeypatch.setattr(db, "novel_archive_complete", lambda *a, **k: False)
    monkeypatch.setattr(db, "count_series_complete_novels", lambda *a, **k: 0)

    try:
        with pytest.raises(InterruptedError, match="Task stopped by user"):
            service.sync_subscribed_series(progress_callback=lambda event_type, data: None)
    finally:
        db.close()


class _FollowingFakeDb:
    """sync_following_novels 所需的最小 DB 假件。"""

    def __init__(self) -> None:
        self.watermark_updates: list[dict] = []

    def get_sync_check_list(self, scope):
        return {}

    def get_watermark(self, key):
        return None

    def update_watermark(self, key, value):
        self.watermark_updates.append(value)

    def upsert_sync_check_item(self, novel_id, exists, scope):
        pass


def test_sync_following_novels_outer_pagination_has_safety_limit(tmp_path: Path) -> None:
    """关注列表接口返回自引用 next_url 时，外层翻页必须在兜底上限处停止。"""

    class EndlessFollowingApi:
        def __init__(self) -> None:
            self.following_calls = 0

        def user_following(self, **kwargs):
            self.following_calls += 1
            return SimpleNamespace(user_previews=[], next_url="loop")

        def parse_qs(self, next_url):
            if next_url == "loop":
                return {"user_id": 1, "restrict": "public", "page": self.following_calls + 1}
            return None

    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    assert settings.sync.max_pages_per_run is None  # 默认无限页数配置
    api = EndlessFollowingApi()
    service = BookmarkNovelSyncService(api, _FollowingFakeDb(), _Storage(), settings)

    service.sync_following_novels()

    # 关注列表用专属上限，不受 max_pages_per_run 影响
    assert api.following_calls == sync_engine.FOLLOWING_LIST_MAX_PAGES


def test_sync_following_novels_author_pagination_has_safety_limit(tmp_path: Path) -> None:
    """单作者作品列表返回自引用 next_url 时，内层翻页必须在兜底上限处停止。"""

    class EndlessAuthorNovelsApi:
        def __init__(self) -> None:
            self.user_novels_calls = 0

        def user_following(self, **kwargs):
            return SimpleNamespace(
                user_previews=[SimpleNamespace(user=SimpleNamespace(id=7, name="作者"))],
                next_url=None,
            )

        def user_novels(self, **kwargs):
            self.user_novels_calls += 1
            return SimpleNamespace(novels=[], next_url="loop")

        def parse_qs(self, next_url):
            if next_url == "loop":
                return {"user_id": 7, "page": self.user_novels_calls + 1}
            return None

    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    api = EndlessAuthorNovelsApi()
    service = BookmarkNovelSyncService(api, _FollowingFakeDb(), _Storage(), settings)

    service.sync_following_novels()

    assert api.user_novels_calls == 100  # safety_limit 兜底


# ── 未完成/中止标记 + 连续已存在提前退出 ─────────────────────────


def _seed_subscribed_series(db: Database, count: int, start: int = 900) -> list[int]:
    """在 DB 里写入若干订阅系列（sync_subscribed_series 的 DB fallback 数据源）。"""
    series_ids: list[int] = []
    for offset in range(count):
        series_id = start + offset
        db.conn.execute(
            "INSERT INTO series (series_id, title, description, user_id, cover_url, total_novels, is_subscribed)"
            " VALUES (?, ?, '', 1, '', 0, 1)",
            (series_id, f"系列{series_id}"),
        )
        series_ids.append(series_id)
    db.conn.commit()
    return series_ids


def test_sync_subscribed_series_marks_abort_reason_after_consecutive_failures(tmp_path: Path) -> None:
    """连续 5 次系列详情失败中止时，stats 必须带 aborted_reason，不能伪装成"跑完了"。"""

    class _NoDetailApi:
        def __init__(self) -> None:
            self.calls = 0

        def novel_series(self, series_id, **kwargs):
            self.calls += 1
            return {"novel_series_detail": None}

        def parse_qs(self, url):
            return None

    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    _seed_subscribed_series(db, 8)
    api = _NoDetailApi()
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    try:
        stats = service.sync_subscribed_series()
    finally:
        db.close()

    assert api.calls == 5  # 连续失败达到阈值立即中止，剩余 3 个系列没被触碰
    assert stats["aborted_reason"] == "rate_limited"
    assert stats["incomplete"] is True
    assert stats["series_synced"] == 0
    assert stats["series_total"] == 8
    assert stats["series_processed"] == 5
    assert stats["series_remaining"] == 3


def test_sync_subscribed_series_marks_abort_reason_on_empty_responses(tmp_path: Path) -> None:
    """空响应路径同样要产出 aborted_reason 标记。"""

    class _EmptyApi:
        def __init__(self) -> None:
            self.calls = 0

        def novel_series(self, series_id, **kwargs):
            self.calls += 1
            return {}

        def parse_qs(self, url):
            return None

    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    _seed_subscribed_series(db, 6, start=1200)
    api = _EmptyApi()
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    try:
        stats = service.sync_subscribed_series()
    finally:
        db.close()

    assert api.calls == 5
    assert stats["aborted_reason"] == "rate_limited"
    assert stats["incomplete"] is True


def test_sync_subscribed_series_completes_without_abort_markers(tmp_path: Path, monkeypatch) -> None:
    """正常跑完时不能出现 aborted_reason / truncated / incomplete。"""

    class _SeriesApi:
        def novel_series(self, series_id, **kwargs):
            return {
                "novel_series_detail": {
                    "title": "普通系列",
                    "caption": "",
                    "user": {"id": 1, "name": "作者", "account": "acc"},
                    "content_count": 1,
                },
                "novels": [{"id": 601}],
                "next_url": None,
            }

        def parse_qs(self, url):
            return None

    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    _seed_subscribed_series(db, 2, start=1300)
    monkeypatch.setattr(db, "novel_archive_complete", lambda *a, **k: True)
    monkeypatch.setattr(db, "count_series_complete_novels", lambda *a, **k: 0)
    service = BookmarkNovelSyncService(_SeriesApi(), db, _Storage(), settings)

    try:
        stats = service.sync_subscribed_series()
    finally:
        db.close()

    assert "aborted_reason" not in stats
    assert "truncated" not in stats
    assert "incomplete" not in stats
    assert stats["series_processed"] == 2
    assert stats["series_remaining"] == 0


def test_sync_subscribed_series_marks_truncated_when_page_limit_reached(tmp_path: Path, monkeypatch) -> None:
    """max_pages_per_run 截断长系列时，stats 必须体现"本轮未取完"。"""

    class _PagedSeriesApi:
        def __init__(self) -> None:
            self.calls = 0

        def novel_series(self, series_id, **kwargs):
            self.calls += 1
            return {
                "novel_series_detail": {
                    "title": "长系列",
                    "caption": "",
                    "user": {"id": 1, "name": "作者", "account": "acc"},
                    "content_count": 60,
                },
                "novels": [{"id": 1000 + self.calls}],
                "next_url": f"https://example.invalid/next?last_order={self.calls}",
            }

        def parse_qs(self, url):
            return {"last_order": "10"}

    settings = _settings(tmp_path)
    settings.sync.max_pages_per_run = 2
    db = Database(settings.storage.db_path)
    db.init_schema()
    _seed_subscribed_series(db, 1, start=11471205)
    monkeypatch.setattr(db, "novel_archive_complete", lambda *a, **k: True)
    monkeypatch.setattr(db, "count_series_complete_novels", lambda *a, **k: 0)
    api = _PagedSeriesApi()
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    try:
        stats = service.sync_subscribed_series()
    finally:
        db.close()

    assert api.calls == 2  # 首页 + 1 次翻页后触顶
    assert stats["truncated"] is True
    assert stats["truncated_series"] == 1
    assert stats["incomplete"] is True
    assert "aborted_reason" not in stats  # 截断不是风控中止


def test_sync_bookmarks_marks_truncated_when_page_limit_reached(tmp_path: Path) -> None:
    """收藏同步触及翻页上限时，同样要标记未取完。"""

    class _EndlessBookmarkApi:
        def __init__(self) -> None:
            self.calls = 0

        def user_bookmarks_novel(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(novels=[], next_url="loop")

        def parse_qs(self, next_url):
            if next_url == "loop":
                return {"user_id": 1, "restrict": "public", "offset": self.calls * 30}
            return None

    settings = _settings(tmp_path)
    settings.sync.max_pages_per_run = 2
    db = Database(settings.storage.db_path)
    db.init_schema()
    api = _EndlessBookmarkApi()
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    try:
        stats = service.sync(1, ["public"])
    finally:
        db.close()

    assert api.calls == 2
    assert stats["truncated"] is True
    assert stats["incomplete"] is True


def test_sync_following_novels_marks_truncated_on_author_page_limit(tmp_path: Path) -> None:
    """单作者作品列表触及翻页上限时，stats 必须标记未取完。"""

    class EndlessAuthorNovelsApi:
        def __init__(self) -> None:
            self.user_novels_calls = 0

        def user_following(self, **kwargs):
            return SimpleNamespace(
                user_previews=[SimpleNamespace(user=SimpleNamespace(id=7, name="作者"))],
                next_url=None,
            )

        def user_novels(self, **kwargs):
            self.user_novels_calls += 1
            return SimpleNamespace(novels=[], next_url="loop")

        def parse_qs(self, next_url):
            if next_url == "loop":
                return {"user_id": 7, "page": self.user_novels_calls + 1}
            return None

    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    settings.sync.max_pages_per_run = 3
    api = EndlessAuthorNovelsApi()
    service = BookmarkNovelSyncService(api, _FollowingFakeDb(), _Storage(), settings)

    stats = service.sync_following_novels()

    assert api.user_novels_calls == 3
    assert stats["truncated"] is True
    assert stats["incomplete"] is True


class _ExistingCheckListDb(_FollowingFakeDb):
    """预检查结果里所有小说都标记为"已存在"。"""

    def get_sync_check_list(self, scope):
        return {novel_id: True for novel_id in range(1, 200)}


def test_sync_following_novels_stops_author_scan_immediately_on_existing_streak(
    tmp_path: Path,
    caplog,
) -> None:
    """连续命中已存在达到阈值时：只打印一次停止日志，并立刻退出该用户的扫描。

    回归：修复前只置 stop_author_scan 标记却不 break 本页循环，剩余条目继续跑，
    每条都重复打印"Stopping user ... scan"（生产日志里同一用户从 48 打到 60）。
    """

    class _PagedAuthorApi:
        def __init__(self) -> None:
            self.user_novels_calls = 0

        def user_following(self, **kwargs):
            return SimpleNamespace(
                user_previews=[SimpleNamespace(user=SimpleNamespace(id=7, name="作者"))],
                next_url=None,
            )

        def user_novels(self, **kwargs):
            self.user_novels_calls += 1
            start = (self.user_novels_calls - 1) * 30 + 1
            return SimpleNamespace(
                novels=[SimpleNamespace(id=novel_id) for novel_id in range(start, start + 30)],
                next_url="loop",
            )

        def parse_qs(self, next_url):
            if next_url == "loop":
                return {"user_id": 7, "offset": self.user_novels_calls * 30}
            return None

    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    api = _PagedAuthorApi()
    service = BookmarkNovelSyncService(api, _ExistingCheckListDb(), _Storage(), settings)

    with caplog.at_level(logging.INFO, logger="pixiv_novel_sync.sync_engine"):
        stats = service.sync_following_novels()

    stop_logs = [r for r in caplog.records if "consecutive existing novels" in r.getMessage()]
    assert len(stop_logs) == 1  # 只打印一次
    assert api.user_novels_calls == 2  # 第二页刚触顶就退出，没有继续翻页
    assert stats["skipped"] == 31  # 第一页 30 本 + 第二页触发阈值的那 1 本


# ── 正文不可获取（作品被删除/设为私密）识别 ─────────────────────

_UNAVAILABLE_BODY = (
    '{"error":{"user_message":"尚无此页","message":"","reason":"","user_message_details":{}}}'
)
_PIXIVPY_MISLEADING_REASON = (
    "Extract novel content error: 'NoneType' object has no attribute 'groups'"
)


def test_sync_novel_reports_content_unavailable_with_clear_chinese_log(
    tmp_path: Path,
    caplog,
) -> None:
    """webview 返回 {"error": ...} 时：清晰中文日志 + 单独计入 content_unavailable。"""

    class _UnavailableApi(_Api):
        def webview_novel(self, novel_id: int):
            raise PixivError(_PIXIVPY_MISLEADING_REASON, body=_UNAVAILABLE_BODY)

    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    service = BookmarkNovelSyncService(_UnavailableApi(), db, _Storage(), settings)

    try:
        with caplog.at_level(logging.WARNING, logger="pixiv_novel_sync.sync_engine"):
            counters = service._sync_novel(
                _Novel(), "public", True, True, True, source_type="bookmark_public"
            )
    finally:
        db.close()

    assert counters["content_unavailable"] == 1
    assert counters.get("failed", 0) == 0
    messages = [record.getMessage() for record in caplog.records]
    assert any("小说 100 正文不可获取" in msg and "尚无此页" in msg for msg in messages)
    assert any("可能已被删除或设为私密" in msg for msg in messages)
    # 不能再把 pixivpy 内部那句误导性的 AttributeError 文本抛给用户
    assert all("has no attribute" not in msg for msg in messages)


def test_sync_novel_keeps_failed_path_when_error_json_unparsable(tmp_path: Path) -> None:
    """fail-safe：解析不出 error JSON 的一律按原有失败路径处理（仍可重试）。"""

    class _HtmlErrorApi(_Api):
        def webview_novel(self, novel_id: int):
            raise PixivError("Extract novel content error: boom", body="<html>502</html>")

    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    service = BookmarkNovelSyncService(_HtmlErrorApi(), db, _Storage(), settings)

    try:
        counters = service._sync_novel(
            _Novel(), "public", True, True, True, source_type="bookmark_public"
        )
    finally:
        db.close()

    assert counters["failed"] == 1
    assert "content_unavailable" not in counters


def test_sync_novel_treats_rate_limit_error_json_as_retryable_failure(tmp_path: Path) -> None:
    """限流的 error JSON 是临时失败，不能被归类为"正文不可获取"。"""

    class _RateLimitedApi(_Api):
        def webview_novel(self, novel_id: int):
            raise PixivError(
                "Extract novel content error: boom",
                body='{"error":{"user_message":"","message":"Rate Limit","reason":""}}',
            )

    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    service = BookmarkNovelSyncService(_RateLimitedApi(), db, _Storage(), settings)

    try:
        counters = service._sync_novel(
            _Novel(), "public", True, True, True, source_type="bookmark_public"
        )
    finally:
        db.close()

    assert counters["failed"] == 1
    assert "content_unavailable" not in counters


def test_sync_subscribed_series_counts_content_unavailable_separately(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    """系列章节正文不可获取时单独计数，不混进 failed。"""

    class _SeriesApi:
        def novel_series(self, series_id, **kwargs):
            return {
                "novel_series_detail": {
                    "title": "含失效章节的系列",
                    "caption": "",
                    "user": {"id": 1, "name": "作者", "account": "acc"},
                    "content_count": 1,
                },
                "novels": [{"id": 19940607}],
                "next_url": None,
            }

        def parse_qs(self, url):
            return None

        def novel_detail(self, novel_id: int):
            return SimpleNamespace(novel=_Novel())

        def webview_novel(self, novel_id: int):
            raise PixivError(_PIXIVPY_MISLEADING_REASON, body=_UNAVAILABLE_BODY)

    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    _seed_subscribed_series(db, 1, start=1400)
    monkeypatch.setattr(db, "novel_archive_complete", lambda *a, **k: False)
    monkeypatch.setattr(db, "count_series_complete_novels", lambda *a, **k: 0)
    service = BookmarkNovelSyncService(_SeriesApi(), db, _Storage(), settings)

    try:
        with caplog.at_level(logging.WARNING, logger="pixiv_novel_sync.sync_engine"):
            stats = service.sync_subscribed_series()
    finally:
        db.close()

    assert stats["content_unavailable"] == 1
    assert stats.get("failed", 0) == 0
    assert any(
        "小说 19940607 正文不可获取" in record.getMessage() for record in caplog.records
    )


# ── 关注作者轮转（users_limit 不能永远只跑最前面几个） ──────────


class _RotationFakeDb(_FollowingFakeDb):
    """会持久化 watermark 的 DB 假件，用于跨轮次轮转断言。"""

    def __init__(self, watermark: dict | None = None) -> None:
        super().__init__()
        self.watermark = watermark

    def get_watermark(self, key):
        return self.watermark

    def update_watermark(self, key, value):
        self.watermark_updates.append(value)
        self.watermark = value


class _EightAuthorsApi:
    """8 个关注作者，返回顺序固定（模拟 Pixiv 按关注时间倒序的稳定顺序）。"""

    def __init__(self, author_ids: list[int] | None = None) -> None:
        self.author_ids = author_ids or list(range(1, 9))
        self.scanned_user_ids: list[int] = []
        self.following_calls = 0

    def user_following(self, **kwargs):
        self.following_calls += 1
        return SimpleNamespace(
            user_previews=[
                SimpleNamespace(user=SimpleNamespace(id=uid, name=f"作者{uid}"))
                for uid in self.author_ids
            ],
            next_url=None,
        )

    def user_novels(self, **kwargs):
        self.scanned_user_ids.append(int(kwargs["user_id"]))
        return SimpleNamespace(novels=[], next_url=None)

    def parse_qs(self, next_url):
        return None


def test_sync_following_novels_rotates_users_across_runs(tmp_path: Path) -> None:
    """users_limit 生效时必须轮转：第二轮换下一批作者，而不是永远跑最前面 3 个。

    回归：生产配置 users_limit=5 / 关注 53 人，每轮都只同步最前面 5 个作者，
    其余 48 个作者的新作永远抓不到（日志里连续 9 次 following_users_scanned=5）。
    """
    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    db = _RotationFakeDb()
    api = _EightAuthorsApi()
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    first = service.sync_following_novels(users_limit=3)
    assert api.scanned_user_ids == [1, 2, 3]
    assert first["following_users_scanned"] == 3
    assert first["users_total"] == 8
    assert first["users_remaining"] == 5
    assert first["incomplete"] is True

    api.scanned_user_ids.clear()
    service.sync_following_novels(users_limit=3)
    assert api.scanned_user_ids == [4, 5, 6]  # 自动轮转到下一批

    api.scanned_user_ids.clear()
    third = service.sync_following_novels(users_limit=3)
    assert api.scanned_user_ids[:2] == [7, 8]  # 剩下没同步过的优先
    assert api.scanned_user_ids[2] == 1  # 再回到最久未同步的作者
    assert third["users_remaining"] == 5


def test_sync_following_novels_prioritises_never_synced_users(tmp_path: Path) -> None:
    """从未同步过的作者优先于已同步过的（不论上次同步时间多早）。"""
    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    db = _RotationFakeDb(
        {
            "last_sync_time": "2026-08-10T00:00:00+00:00",
            "user_max_ids": {"1": 100},
            "user_last_synced": {
                "1": "2026-08-01T00:00:00+00:00",
                "2": "2026-08-02T00:00:00+00:00",
                "3": "2020-01-01T00:00:00+00:00",  # 很久以前，但仍排在"从未同步"之后
            },
        }
    )
    api = _EightAuthorsApi()
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    service.sync_following_novels(users_limit=3)

    assert api.scanned_user_ids == [4, 5, 6]


def test_sync_following_novels_accepts_legacy_watermark_without_last_synced(tmp_path: Path) -> None:
    """旧格式 watermark（只有 user_max_ids）必须能正常加载，且不丢原有字段。"""
    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    db = _RotationFakeDb(
        {
            "last_sync_time": "2026-08-10T00:00:00+00:00",
            "user_max_ids": {"1": 100, "2": 200},
        }
    )
    api = _EightAuthorsApi()
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    stats = service.sync_following_novels(users_limit=3)

    assert api.scanned_user_ids == [1, 2, 3]  # 无 last_synced 记录 → 全部视为从未同步
    assert stats["users_total"] == 8
    assert db.watermark["user_max_ids"] == {"1": 100, "2": 200}  # 旧字段保留
    assert set(db.watermark["user_last_synced"]) == {"1", "2", "3"}  # 新字段补写


def test_sync_following_novels_without_users_limit_keeps_full_scan(tmp_path: Path) -> None:
    """users_limit=0（全部）时保持原行为：按关注列表顺序全量扫描，不标记未完成。"""
    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    db = _RotationFakeDb()
    api = _EightAuthorsApi()
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    stats = service.sync_following_novels()

    assert api.scanned_user_ids == [1, 2, 3, 4, 5, 6, 7, 8]
    assert stats["following_users_scanned"] == 8
    assert "incomplete" not in stats
    assert "users_remaining" not in stats
    # 即便不限量，也要记录每个作者的同步时间，供后续限量轮次轮转
    assert set(db.watermark["user_last_synced"]) == {str(uid) for uid in range(1, 9)}


# ── 关注列表候选集不能被 max_pages_per_run 砍掉 ────────────────────


class _PagedFollowingApi:
    """按每页 30 人分页返回关注列表（复刻 user_following 的真实分页）。"""

    PAGE_SIZE = 30

    def __init__(self, author_count: int) -> None:
        self.author_ids = list(range(1, author_count + 1))
        self.scanned_user_ids: list[int] = []
        self.following_calls = 0

    def user_following(self, **kwargs):
        offset = int(kwargs.get("offset") or 0)
        self.following_calls += 1
        page = self.author_ids[offset : offset + self.PAGE_SIZE]
        has_more = offset + self.PAGE_SIZE < len(self.author_ids)
        return SimpleNamespace(
            user_previews=[
                SimpleNamespace(user=SimpleNamespace(id=uid, name=f"作者{uid}")) for uid in page
            ],
            next_url=f"offset={offset + self.PAGE_SIZE}" if has_more else None,
        )

    def user_novels(self, **kwargs):
        self.scanned_user_ids.append(int(kwargs["user_id"]))
        return SimpleNamespace(novels=[], next_url=None)

    def parse_qs(self, next_url):
        if not next_url:
            return None
        return {"user_id": 1, "restrict": "public", "offset": int(str(next_url).split("=")[1])}


def test_following_candidate_set_ignores_max_pages_per_run(tmp_path: Path) -> None:
    """候选集枚举不能复用 max_pages_per_run。

    回归：生产 max_pages_per_run=2、user_following 每页 30 人 ⇒ 候选集封顶 60 人，
    关注数一旦超过 60，第 61 位之后永远排除在轮转之外（刚修好的 bug 换位置复发）。
    """
    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    settings.sync.max_pages_per_run = 2  # 生产配置
    db = _RotationFakeDb()
    api = _PagedFollowingApi(author_count=70)
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    stats = service.sync_following_novels(users_limit=5)

    assert stats["users_total"] == 70  # 不是被砍到 60
    assert api.following_calls == 3  # 3 页全部取回
    assert "truncated" not in stats


def test_following_rotation_reaches_users_beyond_page_cap(tmp_path: Path) -> None:
    """第 61 位及之后的关注作者必须能轮到（最久未同步优先）。"""
    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    settings.sync.max_pages_per_run = 2
    db = _RotationFakeDb(
        {
            "last_sync_time": "2026-08-18T00:00:00+00:00",
            "user_max_ids": {},
            # 前 60 位刚同步过，只剩 61~70 从未同步
            "user_last_synced": {str(uid): "2026-08-18T00:00:00+00:00" for uid in range(1, 61)},
        }
    )
    api = _PagedFollowingApi(author_count=70)
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    service.sync_following_novels(users_limit=5)

    assert api.scanned_user_ids == [61, 62, 63, 64, 65]


def test_following_list_pagination_keeps_self_referencing_loop_guard(tmp_path: Path) -> None:
    """自引用 next_url 死循环兜底仍在，只是换成关注列表专用上限。"""

    class EndlessFollowingApi:
        def __init__(self) -> None:
            self.following_calls = 0

        def user_following(self, **kwargs):
            self.following_calls += 1
            return SimpleNamespace(user_previews=[], next_url="loop")

        def parse_qs(self, next_url):
            if next_url == "loop":
                return {"user_id": 1, "restrict": "public", "page": self.following_calls + 1}
            return None

    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    settings.sync.max_pages_per_run = 2
    api = EndlessFollowingApi()
    service = BookmarkNovelSyncService(api, _RotationFakeDb(), _Storage(), settings)

    stats = service.sync_following_novels(users_limit=5)

    assert api.following_calls == sync_engine.FOLLOWING_LIST_MAX_PAGES
    assert stats["incomplete"] is True  # 触顶必须暴露出来


def test_sync_following_list_stores_users_beyond_page_cap(tmp_path: Path) -> None:
    """关注用户任务同样不能被 max_pages_per_run 截断，否则第 61 人起永远进不了库。"""
    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    settings.sync.max_pages_per_run = 2
    db = Database(settings.storage.db_path)
    db.init_schema()
    api = _PagedFollowingApi(author_count=70)
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    try:
        stats = service.sync_following_list()
    finally:
        db.close()

    assert stats["users"] == 70
    assert "truncated" not in stats

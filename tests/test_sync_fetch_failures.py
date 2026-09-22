from __future__ import annotations

from types import SimpleNamespace

from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService, RemoteListTruncated
from pixiv_novel_sync.web.managers import AutoSyncScheduler
from tests.test_sync_engine_incremental import _settings


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

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.sync.rescan import BookmarkSweep
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService
from test_sync_engine_incremental import _settings, _Storage


def test_sweep_preserves_newest_page_and_resumes_within_page_budget(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.sync.max_pages_per_run = 2
    with Database(settings.storage.db_path) as db:
        db.init_schema()
        now = datetime.now(timezone.utc)
        key = "bookmark_sweep_v1:1:public"
        db.update_watermark(key, {"baseline_at": (now - timedelta(days=8)).isoformat()})
        calls, refreshed = [], []

        class API:
            def user_bookmarks_novel(self, **query):
                offset = query.get("offset", 0)
                calls.append(offset)
                return SimpleNamespace(
                    novels=[SimpleNamespace(id=offset + 1)],
                    next_url=f"https://app-api.pixiv.net/v1/user/bookmarks/novel?offset={offset + 30}" if offset < 90 else None,
                )

            def parse_qs(self, url):
                return {"user_id": 1, "restrict": "public", "offset": int(url)} if url else None

        service = BookmarkNovelSyncService(API(), db, _Storage(), settings)
        monkeypatch.setattr(service, "_archive_complete_on_disk", lambda *args: True)
        monkeypatch.setattr(db, "upsert_source", lambda *args: None)
        monkeypatch.setattr(db, "touch_novel", lambda *args: None)
        monkeypatch.setattr(
            service,
            "_sync_novel",
            lambda novel, *args, **kwargs: refreshed.append(novel.id) or {"skipped": 1},
        )
        service.sync(1, ["public"])
        assert calls == [0, 30]
        assert db.get_watermark(key)["cursor"]["offset"] == 60
        calls.clear()
        service.sync(1, ["public"])
        assert calls == [0, 60]
        calls.clear()
        service.sync(1, ["public"])
        assert calls == [0, 90]
        assert "completed_at" in db.get_watermark(key)
        assert {1, 31, 61, 91} <= set(refreshed)


def test_single_page_budget_alternates_head_and_saved_sweep(tmp_path):
    with Database(tmp_path / "sweep.db") as db:
        db.init_schema()
        now = datetime.now(timezone.utc)
        db.update_watermark(
            "bookmark_sweep_v1:1:private",
            {
                "baseline_at": (now - timedelta(days=8)).isoformat(),
                "started_at": now.isoformat(),
                "cursor": {"user_id": 1, "restrict": "private", "offset": 60},
            },
        )
        head = BookmarkSweep(db, 1, "private", 7, 1, now=now)
        assert head.initial_query() == {"user_id": 1, "restrict": "private"}
        head.next_query({"offset": 30}, 1, page_ok=True)
        continuation = BookmarkSweep(db, 1, "private", 7, 1, now=now)
        assert continuation.initial_query()["offset"] == 60
        continuation.next_query(None, 1, page_ok=True)
        assert "completed_at" in db.get_watermark("bookmark_sweep_v1:1:private")


def test_failed_page_preserves_checkpoint_and_restricts_are_independent(tmp_path):
    with Database(tmp_path / "sweep.db") as db:
        db.init_schema()
        now = datetime.now(timezone.utc)
        db.update_watermark(
            "bookmark_sweep_v1:1:public", {"baseline_at": (now - timedelta(days=8)).isoformat()}
        )
        sweep = BookmarkSweep(db, 1, "public", 7, 2, now=now)
        sweep.next_query({"offset": 30}, 1, page_ok=False)
        assert db.get_watermark(sweep.key)["cursor"].get("offset", 0) == 0
        assert db.get_watermark("bookmark_sweep_v1:1:private") is None


def test_real_bookmark_id_cursor_is_preserved(tmp_path):
    with Database(tmp_path / "sweep.db") as db:
        db.init_schema()
        now = datetime.now(timezone.utc)
        db.update_watermark(
            "bookmark_sweep_v1:1:public", {"baseline_at": (now - timedelta(days=8)).isoformat()}
        )
        first = BookmarkSweep(db, 1, "public", 7, 2, now=now)
        first.next_query({"user_id": "1", "restrict": "public", "max_bookmark_id": "12345"}, 1, page_ok=True)
        resumed = BookmarkSweep(db, 1, "public", 7, 2, now=now)
        assert resumed.next_query({"max_bookmark_id": "99999"}, 1, page_ok=True)["max_bookmark_id"] == 12345
        assert not resumed.can_stop_incremental(2, page_unchanged=True)

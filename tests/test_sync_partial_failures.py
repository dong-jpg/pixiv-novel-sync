from __future__ import annotations

from types import SimpleNamespace

import pytest

from pixiv_novel_sync import cli
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.storage_files import FileStorage
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService
from tests.test_sync_engine_incremental import _Api, _settings, _seed_subscribed_series


@pytest.mark.parametrize("task", ["bookmark", "following", "series"])
def test_novel_body_fetch_failure_marks_sync_incomplete(tmp_path, task):
    class BrokenBodyApi(_Api):
        def user_bookmarks_novel(self, **kwargs):
            return SimpleNamespace(novels=[SimpleNamespace(id=100)], next_url=None)

        def user_following(self, **kwargs):
            return SimpleNamespace(user_previews=[SimpleNamespace(user=SimpleNamespace(id=1, name="作者"))], next_url=None)

        def user_novels(self, **kwargs):
            return SimpleNamespace(novels=[SimpleNamespace(id=100)], next_url=None)

        def novel_series(self, series_id, **kwargs):
            return {"novel_series_detail": {
                "title": "系列", "caption": "", "user": {"id": 1, "name": "作者", "account": "acc"},
                "content_count": 1,
            }, "novels": [{"id": 100}], "next_url": None}

    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    with Database(settings.storage.db_path) as db:
        db.init_schema()
        service = BookmarkNovelSyncService(BrokenBodyApi(body=""), db, FileStorage(settings), settings)
        if task == "bookmark":
            stats = service.sync(1, ["public"])
        elif task == "following":
            stats = service.sync_following_novels(users_limit=1)
        else:
            _seed_subscribed_series(db, 1)
            stats = service.sync_subscribed_series()
        assert stats["failed"] == 1
        assert stats["incomplete"] is True
        assert stats["aborted_reason"]
        assert db.conn.execute("SELECT COUNT(*) FROM novels").fetchone()[0] == 0


def test_sync_bookmarks_cli_reports_nonzero_for_failed_novel(monkeypatch):
    monkeypatch.setattr("sys.argv", ["pixiv-novel-sync", "sync-bookmarks"])
    monkeypatch.setattr(cli, "load_settings", lambda **kwargs: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr("pixiv_novel_sync.jobs.quick_sync.run_bookmark_sync", lambda settings: {"failed": 1})
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 1

"""Chapter identity and files, rather than a count, decide whether a series is complete."""
from __future__ import annotations

import pytest

from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService
from tests.test_sync_engine_incremental import _settings, _Storage, _seed_subscribed_series


@pytest.mark.parametrize("case,expected,requests", [
    ("unchanged", [], 1),
    ("replacement", [503], 1),
    ("missing_asset", [502], 1),
    ("paged_replacement", [503], 2),
])
def test_equal_series_counts_still_check_chapter_identity_and_files(tmp_path, monkeypatch, case, expected, requests):
    settings = _settings(tmp_path)
    calls, synced = [], []

    class API:
        def novel_series(self, series_id, **kwargs):
            calls.append(kwargs)
            first = {"novel_series_detail": {"title": "Series", "content_count": 2},
                     "novels": [{"id": 501}, {"id": 502}], "next_url": None}
            if case == "replacement":
                first["novels"][1]["id"] = 503
            if case == "paged_replacement":
                if kwargs:
                    return {"novels": [{"id": 503}], "next_url": None}
                first.update(novels=[{"id": 501}], next_url="next")
            return first

        def parse_qs(self, url):
            return {"last_order": "1"}

    with Database(settings.storage.db_path) as db:
        db.init_schema()
        _seed_subscribed_series(db, 1)
        monkeypatch.setattr(db, "count_series_complete_novels", lambda *a, **kw: 2)
        service = BookmarkNovelSyncService(API(), db, _Storage(), settings)
        complete = {501} if case == "missing_asset" else {501, 502}
        monkeypatch.setattr(service, "_archive_complete_on_disk", lambda n, assets: n in complete)

        def sync(novel, *args, **kwargs):
            synced.append(novel.id)
            return {"novels": 1}

        monkeypatch.setattr(service, "_sync_novel", sync)
        stats = service.sync_subscribed_series()
    assert synced == expected
    assert len(calls) == requests
    assert stats["skipped"] == 2 - len(expected)
    assert stats["novels"] == len(expected)
    assert not stats.get("incomplete")


def test_invalid_series_continuation_is_incomplete(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    class API:
        def novel_series(self, series_id, **kwargs):
            if kwargs:
                return None
            return {"novel_series_detail": {"title": "Series", "content_count": 2},
                    "novels": [{"id": 501}], "next_url": "next"}

        def parse_qs(self, url):
            return {"last_order": "1"}

    with Database(settings.storage.db_path) as db:
        db.init_schema()
        _seed_subscribed_series(db, 1)
        service = BookmarkNovelSyncService(API(), db, _Storage(), settings)
        monkeypatch.setattr(service, "_archive_complete_on_disk", lambda *args: True)
        stats = service.sync_subscribed_series()
    assert stats["incomplete"] and stats["truncated"]
    assert stats["truncated_series"] == 1

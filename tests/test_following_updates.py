from types import SimpleNamespace

import pytest

from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.sync.following_snapshot import known_following_members, save_following_snapshot
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService
from test_user_backup_incremental import AUTHOR_ID, _Api, _OfflineStorage, _env


class FeedApi(_Api):
    def __init__(self):
        super().__init__()
        self.feeds = {"public": list(range(12, 0, -1)), "private": []}
        self.feed_calls = []
        self.failure = None

    def novel_follow(self, restrict, offset=0):
        self.feed_calls.append((restrict, offset))
        if restrict == self.failure:
            return {"error": {"message": "temporary failure"}}
        ids = self.feeds[restrict][offset:offset + 2]
        return {"novels": [{"id": identity, "user": {"id": AUTHOR_ID}} for identity in ids],
                "next_url": (f"https://app-api.pixiv.net/v1/novel/follow?restrict={restrict}&offset={offset + 2}"
                             if offset + 2 < len(self.feeds[restrict]) else None)}


@pytest.fixture
def feed(tmp_path, monkeypatch):
    settings, unused = _env(tmp_path, monkeypatch)
    settings.sync.max_pages_per_run = 3
    settings.sync.max_items_per_run = 2
    db = Database(settings.storage.db_path)
    save_following_snapshot(db, 999, [], "private")
    api = FeedApi()
    service = BookmarkNovelSyncService(api, db, _OfflineStorage(settings), settings)
    yield SimpleNamespace(settings=settings, db=db, api=api, service=service)
    db.close()


def test_bounded_feed_resumes_and_downloads_every_discovered_id_after_restart(feed):
    result = feed.service.sync_following_updates()
    assert result["feed_backlog"] and result["feed_pending"] == 4
    assert len(feed.api.detail_calls) == 2
    feed.db.close()
    feed.service.db = feed.db = Database(feed.settings.storage.db_path)
    for unused in range(8):
        result = feed.service.sync_following_updates()
    assert result["feed_pending"] == 0
    assert sorted(feed.api.detail_calls) == list(range(1, 13))
    before = list(feed.api.detail_calls)
    feed.service.sync_following_updates()
    assert feed.api.detail_calls == before


def test_changed_head_does_not_skip_new_pages_before_resuming(feed):
    feed.service.sync_following_updates()
    feed.api.feeds["public"] = [16, 15, 14, 13] + feed.api.feeds["public"]
    for unused in range(12):
        feed.service.sync_following_updates()
    assert sorted(feed.api.detail_calls) == list(range(1, 17))
    assert ("public", 2) in feed.api.feed_calls


def test_newest_works_get_download_budget_before_historical_backlog(feed):
    feed.service.sync_following_updates()
    feed.api.feeds["public"] = [14, 13] + feed.api.feeds["public"]
    before = len(feed.api.detail_calls)
    feed.service.sync_following_updates()
    assert feed.api.detail_calls[before:] == [14, 13]


def test_removed_anchor_resets_to_verified_contiguous_pages(feed):
    feed.service.sync_following_updates()
    feed.api.feeds["public"] = [12, 11, 4, 3, 2, 1]
    for unused in range(8):
        feed.service.sync_following_updates()
    assert set(feed.api.feeds["public"]) <= set(feed.api.detail_calls)
    assert feed.db.get_watermark("following_feed:999:public")["completed_at"]


def test_failed_feed_scope_does_not_starve_persisted_downloads(feed):
    feed.service.sync_following_updates()
    feed.api.failure = "public"
    before = len(feed.api.detail_calls)
    result = feed.service.sync_following_updates()
    assert len(feed.api.detail_calls) == before + 2
    assert result["aborted_reason"] == "following_feed_unavailable"
    assert ("private", 0) in feed.api.feed_calls


def test_explicit_rate_limit_stops_feed_and_body_requests(feed):
    feed.service.sync_following_updates()
    previous = list(feed.api.detail_calls)
    calls = []

    def limited(**query):
        calls.append(query)
        return {"error": {"message": "Rate Limit"}}

    feed.api.novel_follow = limited
    result = feed.service.sync_following_updates()
    assert result["aborted_reason"] == "rate_limited" and len(calls) == 1
    assert feed.api.detail_calls == previous
    assert feed.db.conn.execute("SELECT COUNT(*) FROM following_feed_pending").fetchone()[0] > 0


def test_unavailable_private_membership_keeps_public_downloads_progressing(feed):
    feed.service.sync_following_updates()
    feed.db.update_watermark("following_private:999", {})
    feed.service.sync_following_list = lambda *args: {"incomplete": True}
    before = len(feed.api.detail_calls)
    result = feed.service.sync_following_updates()
    assert len(feed.api.detail_calls) == before + 2
    assert "following_snapshot_unavailable" in result["aborted_reason"]


@pytest.mark.parametrize("cursor", [
    "https://evil.example/v1/novel/follow?offset=2",
    "https://app-api.pixiv.net/v1/novel/follow?offset=0",
    "https://app-api.pixiv.net/v1/novel/follow?offset=2&restrict=private",
    "https://app-api.pixiv.net/v1/novel/follow?offset=2&offset=4",
    "https://app-api.pixiv.net/v1/user/novels?offset=2",
    "https://app-api.pixiv.net/v1/novel/follow?offset=bad",
])
def test_invalid_feed_cursor_never_advances_the_checkpoint(feed, cursor):
    original = feed.api.novel_follow

    def malformed(restrict, offset=0):
        response = original(restrict, offset)
        if restrict == "public":
            response["next_url"] = cursor
        return response

    feed.api.novel_follow = malformed
    result = feed.service.sync_following_updates()
    assert result["aborted_reason"] == "invalid_feed_cursor"
    assert feed.db.get_watermark("following_feed:999:public") is None
    assert sorted(feed.api.detail_calls) == [11, 12]


def test_private_followed_author_is_archived_in_private_tree(feed):
    save_following_snapshot(feed.db, 999, [])
    save_following_snapshot(feed.db, 999, [SimpleNamespace(id=AUTHOR_ID)], "private")
    feed.api.feeds = {"public": [], "private": [77]}
    feed.service.sync_following_updates()
    row = feed.db.conn.execute("SELECT restrict_value,archive_dir FROM novels WHERE novel_id=77").fetchone()
    assert row["restrict_value"] == "private"
    assert (feed.settings.storage.private_dir / row["archive_dir"] / "text.txt").is_file()
    assert not list(feed.settings.storage.public_dir.rglob("text.txt"))


def test_cancel_after_discovery_preserves_the_persistent_pending_ids(feed):
    feed.service.stop_requested = lambda: bool(feed.db.conn.execute("SELECT 1 FROM following_feed_pending").fetchone())
    with pytest.raises(InterruptedError):
        feed.service.sync_following_updates()
    assert feed.api.detail_calls == []
    assert feed.db.conn.execute("SELECT COUNT(*) FROM following_feed_pending").fetchone()[0] == 2


def test_following_membership_resolves_last_authenticated_owner_and_keeps_scopes(feed):
    assert known_following_members(feed.db, None) == {AUTHOR_ID: ["public"]}
    save_following_snapshot(feed.db, 999, [SimpleNamespace(id=AUTHOR_ID), SimpleNamespace(id=8)], "private")
    assert known_following_members(feed.db, 999) == {AUTHOR_ID: ["public", "private"], 8: ["private"]}
    assert known_following_members(feed.db, 1000) is None
    save_following_snapshot(feed.db, 999, [])
    assert known_following_members(feed.db, None) == {AUTHOR_ID: ["private"], 8: ["private"]}
    feed.db.save_self_profile({"user_id": 1000})
    assert known_following_members(feed.db, None) is None


def test_unarchived_id_failure_backoff_survives_reopening_database(feed):
    for unused in range(3):
        feed.db.record_novel_fetch_failure(9999, 3, 3)
    feed.db.close()
    feed.db = Database(feed.settings.storage.db_path)
    assert feed.db.novel_in_fetch_cooldown(9999)
    feed.db.clear_novel_fetch_failures(9999)
    assert not feed.db.novel_in_fetch_cooldown(9999)

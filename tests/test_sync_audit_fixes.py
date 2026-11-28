import errno
from pathlib import Path
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.jobs import services
from pixiv_novel_sync.jobs.quick_sync import run_scheduled_user_backup
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.sync.following_snapshot import save_following_snapshot
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService
from test_following_updates import FeedApi
from test_user_backup_incremental import AUTHOR_ID, _env, _novel, _OfflineStorage


@pytest.fixture
def archive(tmp_path, monkeypatch):
    settings, api = _env(tmp_path, monkeypatch)
    scope = SimpleNamespace(value="public")
    api.user_bookmarks_novel = lambda *, user_id, restrict: SimpleNamespace(
        novels=[_novel(31)] if restrict == scope.value else [], next_url=None,
    )
    with Database(settings.storage.db_path) as db:
        service = BookmarkNovelSyncService(api, db, _OfflineStorage(settings), settings)
        yield service, scope


@pytest.mark.parametrize("initial,changed", [("public", "private"), ("private", "public")])
def test_bookmark_scope_change_moves_unchanged_archive_without_network(archive, initial, changed):
    service, scope = archive
    scope.value = initial
    service.sync(999, [initial])
    original_asset = Path(service.db.conn.execute("SELECT local_path FROM assets").fetchone()[0])
    scope.value = changed
    result = service.sync(999, [changed])
    stored, relative = service.db.conn.execute("SELECT restrict_value,archive_dir FROM novels").fetchone()
    assert not result.get("incomplete") and stored == changed
    assert not (service.storage.base_dir(initial) / relative).exists()
    assert (service.storage.base_dir(changed) / relative / "text.txt").read_text() == "body\n"
    assert not original_asset.exists()
    assert Path(service.db.conn.execute("SELECT local_path FROM assets").fetchone()[0]).read_bytes() == b"img"
    assert {row[0] for row in service.db.conn.execute("SELECT source_type FROM sources")} == {f"bookmark_{changed}"}
    assert service.api.detail_calls == [31]


def test_existing_text_is_materialized_locally_after_output_settings_change(archive):
    service, scope = archive
    service.sync(999, [scope.value], write_markdown=False, write_raw_text=False)
    directory = service.storage.base_dir(scope.value) / service.db.conn.execute("SELECT archive_dir FROM novels").fetchone()[0]
    assert not (directory / "text.txt").exists()
    (directory / "text.md").touch()
    service.sync(999, [scope.value], write_markdown=True, write_raw_text=True)
    assert (directory / "text.txt").read_text() == "body\n"
    assert "body" in (directory / "text.md").read_text(encoding="utf-8")
    assert service.api.detail_calls == [31]


@pytest.mark.parametrize("reversed_again", [False, True])
def test_interrupted_scope_switch_recovers_without_writing_to_stale_root(archive, monkeypatch, reversed_again):
    service, scope = archive
    service.sync(999, ["public"])
    scope.value = "private"

    def fail_commit(*args):
        raise OSError("simulated database finalization failure")

    with monkeypatch.context() as patch:
        patch.setattr(service.db, "refresh_rescue_item", fail_commit)
        assert service.sync(999, [scope.value])["aborted_reason"] == "archive_scope_error"
    assert service.db.get_watermark("bookmark_scope_move", "31")
    assert not service._archive_complete_on_disk(31, False)
    assert service._sync_novel(_novel(31), None, True, True, True, "user_backup")["archive_scope_pending"]
    assert not list(service.storage.base_dir("public").rglob("text.txt"))
    scope.value = "public" if reversed_again else "private"
    assert not service.sync(999, [scope.value]).get("incomplete")
    assert service.db.get_watermark("bookmark_scope_move", "31") is None
    assert service.db.conn.execute("SELECT restrict_value FROM novels").fetchone()[0] == scope.value
    assert len(list(service.storage.base_dir(scope.value).rglob("text.txt"))) == 1
    assert not list(service.storage.base_dir("private" if reversed_again else "public").rglob("text.txt"))


def test_cross_volume_scope_move_verifies_files_and_keeps_conflicts(archive, monkeypatch):
    service, scope = archive
    service.sync(999, ["public"])
    relative = service.db.conn.execute("SELECT archive_dir FROM novels").fetchone()[0]
    previous = service.storage.base_dir("public") / relative
    destination = service.storage.base_dir("private") / relative
    destination.mkdir(parents=True)
    conflict = destination / "text.txt"
    conflict.write_text("other archive", encoding="utf-8")
    scope.value = "private"
    assert service.sync(999, ["private"])["aborted_reason"] == "archive_scope_error"
    assert conflict.read_text() == "other archive" and (previous / "text.txt").read_text() == "body\n"
    assert service.db.conn.execute("SELECT restrict_value FROM novels").fetchone()[0] == "public"
    conflict.unlink()
    assert not service.sync(999, ["private"]).get("incomplete")

    def cross_device(source, target):
        raise OSError(errno.EXDEV, "different volumes")

    monkeypatch.setattr(Path, "rename", cross_device)
    scope.value = "public"
    assert not service.sync(999, ["public"]).get("incomplete")
    assert not destination.exists() and (previous / "text.txt").read_text() == "body\n"
    assert Path(service.db.conn.execute("SELECT local_path FROM assets").fetchone()[0]).read_bytes() == b"img"


@pytest.mark.parametrize("changed", [[4, 3, 2, 1], [6, 5, 4, 3, 2, 1], [5, 2, 3, 4, 1]])
def test_history_checkpoint_drift_never_certifies_a_missing_novel(tmp_path, monkeypatch, changed):
    settings, api = _env(tmp_path, monkeypatch)
    identities = [5, 4, 3, 2, 1]

    def page(*, user_id, offset=0):
        return SimpleNamespace(novels=[_novel(identity) for identity in identities[offset:offset + 1]],
                               next_url=str(offset + 1) if offset + 1 < len(identities) else None)

    api.user_novels = page
    api.parse_qs = lambda url: {"offset": url} if url else None

    def backup():
        return services.run_user_backup_task(settings, AUTHOR_ID, scheduled_owner=999, api=api, rebuild_catalog=False)

    assert backup()["more_pages"]
    identities[:] = changed
    for unused in range(8):
        if backup()["history_complete"]:
            assert set(identities) <= set(api.detail_calls)
            break
    else:
        pytest.fail("a stable catalog never finished")


def test_unverified_legacy_completion_is_rechecked_in_normal_rotation(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    settings, api = _env(tmp_path, monkeypatch)
    with Database(settings.storage.db_path) as db:
        save_following_snapshot(db, 999, [], "private")
        db.update_watermark("user_backup_page", {"offset": 0, "completed_at": datetime.now(timezone.utc).isoformat(),
                                                "last_archive_complete": True}, f"999:{AUTHOR_ID}")
    assert run_scheduled_user_backup(settings, include_private=True)["pages_read"] == 1
    assert api.detail_calls
    assert run_scheduled_user_backup(settings, include_private=True)["pages_read"] == 0


def test_backup_rate_limit_keeps_page_and_stops_before_next_author(tmp_path, monkeypatch):
    settings, api = _env(tmp_path, monkeypatch)
    with Database(settings.storage.db_path) as db:
        save_following_snapshot(db, 999, [SimpleNamespace(id=7), SimpleNamespace(id=8)])
    calls = []

    def listing(*, user_id, offset=0):
        calls.append((user_id, offset))
        return SimpleNamespace(novels=[_novel(1)], next_url="next") if not offset else {"error": {"message": "Rate Limit"}}

    api.user_novels = listing
    api.parse_qs = lambda url: {"offset": 1} if url else None
    stats = run_scheduled_user_backup(settings)
    assert calls == [(7, 0), (7, 1)] and stats["aborted_reason"] == "rate_limited"
    assert stats["novels_created"] == 1 and stats["authors_with_more_pages"] == 1
    with Database(settings.storage.db_path) as db:
        assert db.get_watermark("user_backup_page", "999:7")["offset"] == 1
        assert db.get_watermark("user_backup_page", "999:8") is None


@pytest.mark.parametrize("endpoint", ["novel_detail", "webview_novel"])
def test_feed_download_rate_limit_stops_without_penalizing_titles(tmp_path, monkeypatch, endpoint):
    settings, unused = _env(tmp_path, monkeypatch)
    settings.sync.max_items_per_run = 4
    api = FeedApi()
    api.feeds = {"public": [5, 4, 3, 2], "private": []}
    calls = []

    def limited(novel_id):
        calls.append(novel_id)
        return {"error": {"message": "Rate Limit"}}

    setattr(api, endpoint, limited)
    with Database(settings.storage.db_path) as db:
        save_following_snapshot(db, 999, [], "private")
        service = BookmarkNovelSyncService(api, db, _OfflineStorage(settings), settings)
        stats = service.sync_following_updates()
        assert calls == [5] and stats["aborted_reason"] == "rate_limited"
        assert stats["failed"] == 0 and stats["feed_pending"] == 4
        assert db.conn.execute("SELECT COUNT(*) FROM following_feed_pending WHERE retry_at IS NOT NULL").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM novel_fetch_attempts").fetchone()[0] == 0

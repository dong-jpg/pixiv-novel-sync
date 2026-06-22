from __future__ import annotations

from pathlib import Path

import pytest

from pixiv_novel_sync.models import AssetRecord, NovelRecord, NovelTextRecord, SourceRecord, UserRecord
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    db.init_schema()
    return db


def _insert_user_and_novel(db: Database, novel_id: int = 100, user_id: int = 1, series_id: int | None = None) -> None:
    db.upsert_user(UserRecord(user_id=user_id, name="u", account="acc", raw_json="{}"))
    db.upsert_novel(
        NovelRecord(
            novel_id=novel_id,
            user_id=user_id,
            series_id=series_id,
            title="title",
            caption="caption",
            visible=True,
            restrict="public",
            x_restrict=0,
            text_length=10,
            total_bookmarks=1,
            total_views=2,
            cover_url="https://i.pximg.net/test.jpg",
            tags_json="[]",
            create_date="2026-01-01T00:00:00+00:00",
            raw_json="{}",
            meta_hash="meta",
        )
    )


def test_foreign_keys_enabled(db: Database) -> None:
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_child_tables_reject_orphan_rows(db: Database) -> None:
    with pytest.raises(Exception):
        db.upsert_novel_text(NovelTextRecord(novel_id=999, text_raw="x", text_markdown=None, text_hash="h"))
    with pytest.raises(Exception):
        db.record_asset(999, "cover", "https://i.pximg.net/x.jpg", "x.jpg", "hash")
    with pytest.raises(Exception):
        db.upsert_source(SourceRecord(novel_id=999, source_type="bookmark_public", source_key="1"))


def test_delete_novel_cascades_child_rows_and_cleans_satellites(db: Database) -> None:
    _insert_user_and_novel(db)
    db.upsert_novel_text(NovelTextRecord(novel_id=100, text_raw="body", text_markdown="md", text_hash="text"))
    db.record_asset(100, "cover", "https://i.pximg.net/x.jpg", "x.jpg", "hash")
    db.upsert_source(SourceRecord(novel_id=100, source_type="bookmark_public", source_key="1"))
    db.replace_fts(100, "title", "caption", "author", "body")
    db.init_sync_check_table()
    db.upsert_sync_check_item(100, True)
    db.conn.execute("INSERT INTO pending_deletions (item_type, item_id, title, reason, status) VALUES ('novel', 100, 't', 'r', 'pending')")
    db.conn.execute("INSERT INTO recommendation_items (run_id, profile_id, item_type, novel_id, title, tags_json, matched_json, status) VALUES (1, 1, 'novel', 100, 't', '[]', '{}', 'pending')")
    db.conn.execute("INSERT INTO recommendation_feedback (item_type, feedback_type, novel_id) VALUES ('novel', 'dismiss', 100)")
    db.conn.commit()

    db.delete_novel(100)

    assert db.conn.execute("SELECT 1 FROM novels WHERE novel_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM novel_texts WHERE novel_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM assets WHERE novel_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM sources WHERE novel_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM novel_fts WHERE novel_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM sync_check_list WHERE novel_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM pending_deletions WHERE item_type = 'novel' AND item_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM recommendation_items WHERE novel_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM recommendation_feedback WHERE novel_id = 100").fetchone() is None


def test_delete_user_removes_owned_novels_and_children(db: Database) -> None:
    _insert_user_and_novel(db, novel_id=101, user_id=5)
    db.upsert_novel_text(NovelTextRecord(novel_id=101, text_raw="body", text_markdown=None, text_hash="text"))
    db.record_asset(101, "cover", "https://i.pximg.net/x.jpg", "x.jpg", "hash")
    db.upsert_source(SourceRecord(novel_id=101, source_type="bookmark_public", source_key="5"))
    db.replace_fts(101, "title", "caption", "author", "body")
    db.init_sync_check_table()
    db.upsert_sync_check_item(101, True)
    db.conn.execute("INSERT INTO pending_deletions (item_type, item_id, title, reason, status) VALUES ('user', 5, 'u', 'r', 'pending')")
    db.conn.execute("INSERT INTO pending_deletions (item_type, item_id, title, reason, status) VALUES ('novel', 101, 'n', 'r', 'pending')")
    db.conn.execute("INSERT INTO recommendation_feedback (item_type, feedback_type, novel_id, author_id) VALUES ('novel', 'dismiss', 101, 5)")
    db.conn.commit()

    db.delete_user(5)

    assert db.conn.execute("SELECT 1 FROM users WHERE user_id = 5").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM novels WHERE novel_id = 101").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM novel_texts WHERE novel_id = 101").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM assets WHERE novel_id = 101").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM sources WHERE novel_id = 101").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM novel_fts WHERE novel_id = 101").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM pending_deletions WHERE item_type = 'user' AND item_id = 5").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM pending_deletions WHERE item_type = 'novel' AND item_id = 101").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM recommendation_feedback WHERE author_id = 5 OR novel_id = 101").fetchone() is None


def test_delete_series_only_nulls_series_id(db: Database) -> None:
    db.conn.execute("INSERT INTO series (series_id, title, description, user_id, cover_url, total_novels, is_subscribed, status) VALUES (9, 's', '', 1, NULL, 0, 0, 'unknown')")
    db.conn.commit()
    _insert_user_and_novel(db, novel_id=102, user_id=1, series_id=9)

    db.delete_series(9)

    row = db.conn.execute("SELECT series_id FROM novels WHERE novel_id = 102").fetchone()
    assert row is not None
    assert row[0] is None
    assert db.conn.execute("SELECT 1 FROM series WHERE series_id = 9").fetchone() is None


def test_cleanup_old_pending_deletions_does_not_auto_confirm_pending(db: Database) -> None:
    db.conn.execute(
        """
        INSERT INTO pending_deletions (item_type, item_id, title, reason, status, detected_at)
        VALUES ('novel', 200, 'old pending', 'missing', 'pending', datetime('now', '-365 days'))
        """
    )
    db.conn.execute(
        """
        INSERT INTO pending_deletions (item_type, item_id, title, reason, status, confirmed_at)
        VALUES ('novel', 201, 'old confirmed', 'missing', 'confirmed', datetime('now', '-30 days'))
        """
    )
    db.conn.commit()

    result = db.cleanup_old_pending_deletions(grace_period_days=1, cleanup_confirmed_days=7)

    assert result["auto_confirmed"] == 0
    assert result["cleaned_up"] == 1
    pending = db.conn.execute("SELECT status FROM pending_deletions WHERE item_id = 200").fetchone()
    assert pending is not None
    assert pending[0] == "pending"
    assert db.conn.execute("SELECT 1 FROM pending_deletions WHERE item_id = 201").fetchone() is None


def test_batch_sync_check_upsert(db: Database) -> None:
    db.init_sync_check_table()
    db.upsert_sync_check_items([(1, True), (2, False), (3, True)], scope="scope")
    assert db.get_sync_check_list("scope") == {1: True, 2: False, 3: True}


def test_batch_record_assets(db: Database) -> None:
    _insert_user_and_novel(db, novel_id=103)
    db.record_assets(
        [
            AssetRecord(103, "cover", "https://i.pximg.net/a.jpg", "a.jpg", "h1"),
            AssetRecord(103, "image", "https://i.pximg.net/b.jpg", "b.jpg", "h2"),
        ]
    )
    assert db.get_recorded_asset_urls(103) == {"https://i.pximg.net/a.jpg", "https://i.pximg.net/b.jpg"}

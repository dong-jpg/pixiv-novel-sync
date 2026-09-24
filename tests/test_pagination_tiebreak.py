from __future__ import annotations

from pixiv_novel_sync.storage_db import Database


def test_same_timestamp_novels_order_by_id_desc(tmp_path) -> None:
    db = Database(tmp_path / "pages.db")
    db.init_schema()
    db.conn.execute("INSERT INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')")
    for novel_id in (1, 2):
        db.conn.execute(
            """
            INSERT INTO novels (
                novel_id, title, user_id, visible, restrict_value, x_restrict,
                text_length, total_bookmarks, total_views, tags_json, raw_json,
                meta_hash, last_seen_at
            ) VALUES (?, '一篇', 1, 1, 'public', 0, 10, 0, 0, '[]', '{}', 'h', '2026-01-01 00:00:00')
            """,
            (novel_id,),
        )

    items = db.list_recent_novels(sort="updated_desc", page_size=10)["items"]

    assert [item["novel_id"] for item in items] == [2, 1]
    db.close()


def test_wal_journal_size_is_capped(tmp_path) -> None:
    db = Database(tmp_path / "wal.db")
    db.init_schema()
    try:
        limit = db.conn.execute("PRAGMA journal_size_limit").fetchone()[0]
    finally:
        db.close()

    assert limit == 67108864

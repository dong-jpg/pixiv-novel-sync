from __future__ import annotations

from pixiv_novel_sync.storage_db import Database


def test_status_rotation_puts_never_checked_first_and_uses_index(tmp_path) -> None:
    db = Database(tmp_path / "rotate.db")
    db.init_schema()
    db.conn.execute("INSERT INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')")
    for novel_id, checked in ((1, "2020-01-01 00:00:00"), (2, None), (3, "2024-01-01 00:00:00")):
        db.conn.execute(
            """
            INSERT INTO novels (
                novel_id, title, user_id, visible, restrict_value, x_restrict,
                text_length, total_bookmarks, total_views, tags_json, raw_json,
                meta_hash, last_checked_at
            ) VALUES (?, '一篇', 1, 1, 'public', 0, 10, 0, 0, '[]', '{}', 'h', ?)
            """,
            (novel_id, checked),
        )

    assert db.get_novel_ids_for_status_check() == [2, 1, 3]
    plan = " ".join(
        str(row[-1])
        for row in db.conn.execute(
            "EXPLAIN QUERY PLAN SELECT novel_id FROM novels ORDER BY last_checked_at, novel_id"
        )
    )
    assert "idx_novels_last_checked" in plan
    dropped = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_assets_novel_id'"
    ).fetchone()
    assert dropped is None
    db.close()

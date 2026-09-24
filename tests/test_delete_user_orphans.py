from __future__ import annotations

from pixiv_novel_sync.storage_db import Database


def _novel(db: Database, novel_id: int, user_id: int, series_id: int | None) -> None:
    db.conn.execute(
        """
        INSERT INTO novels (
            novel_id, title, user_id, series_id, visible, restrict_value, x_restrict,
            text_length, total_bookmarks, total_views, tags_json, raw_json, meta_hash
        ) VALUES (?, '一篇', ?, ?, 1, 'public', 0, 10, 0, 0, '[]', '{}', 'h')
        """,
        (novel_id, user_id, series_id),
    )


def test_delete_user_removes_series_and_orphan_rows(tmp_path) -> None:
    db = Database(tmp_path / "user.db")
    db.init_schema()
    db.conn.execute("INSERT INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')")
    db.upsert_subscribed_series(3, "系列", "", 1, "https://img/a.jpg", 1)
    _novel(db, 10, 1, 3)
    db.conn.execute("INSERT INTO reading_progress (novel_id, progress) VALUES (10, 4)")
    db.conn.execute("INSERT INTO preference_analyzed_novels (novel_id) VALUES (10)")

    db.delete_user(1)

    assert db.list_following_series()["total"] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM reading_progress").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM preference_analyzed_novels").fetchone()[0] == 0
    db.close()


def test_delete_novel_clears_progress_and_analysis(tmp_path) -> None:
    db = Database(tmp_path / "novel.db")
    db.init_schema()
    db.conn.execute("INSERT INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')")
    _novel(db, 10, 1, None)
    db.conn.execute("INSERT INTO reading_progress (novel_id, progress) VALUES (10, 4)")
    db.conn.execute("INSERT INTO preference_analyzed_novels (novel_id) VALUES (10)")

    db.delete_novel(10)

    assert db.conn.execute("SELECT COUNT(*) FROM reading_progress").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM preference_analyzed_novels").fetchone()[0] == 0
    db.close()

from __future__ import annotations

import sqlite3

import pytest

from pixiv_novel_sync.storage_db import Database


def test_init_schema_rejects_leftover_rebuild_table(tmp_path) -> None:
    db = Database(tmp_path / "leftover.db")
    db.init_schema()
    db.conn.execute("CREATE TABLE assets_old (asset_id INTEGER)")
    with pytest.raises(RuntimeError, match="assets_old"):
        db.init_schema()
    db.close()


def test_failed_foreign_key_rebuild_rolls_back(tmp_path) -> None:
    db = Database(tmp_path / "rebuild.db")
    db.init_schema()
    db.conn.execute(
        "INSERT INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')"
    )
    db.conn.execute(
        """
        INSERT INTO novels (
            novel_id, title, user_id, visible, restrict_value, x_restrict,
            text_length, total_bookmarks, total_views, tags_json, raw_json, meta_hash
        ) VALUES (10, '一篇', 1, 1, 'public', 0, 10, 0, 0, '[]', '{}', 'h')
        """
    )
    db.conn.execute(
        """
        INSERT INTO novel_texts (novel_id, text_raw, text_hash)
        VALUES (10, '正文', 'hash')
        """
    )

    with pytest.raises(sqlite3.OperationalError):
        db._rebuild_table_with_foreign_key(
            "novel_texts",
            """
            CREATE TABLE novel_texts (
                novel_id INTEGER PRIMARY KEY,
                text_raw TEXT NOT NULL,
                text_hash TEXT NOT NULL
            )
            """,
            "INSERT INTO novel_texts (novel_id) SELECT missing_column FROM novel_texts_old",
        )

    names = {
        row[0]
        for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "novel_texts" in names
    assert "novel_texts_old" not in names
    text = db.conn.execute("SELECT text_raw FROM novel_texts WHERE novel_id = 10").fetchone()
    assert text[0] == "正文"
    db.close()

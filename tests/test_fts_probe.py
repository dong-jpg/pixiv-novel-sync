from __future__ import annotations

from pixiv_novel_sync.storage_db import Database


def test_fts_probe_rebuilds_when_only_the_tail_is_misaligned(tmp_path) -> None:
    db = Database(tmp_path / "fts.db")
    db.init_schema()
    db.conn.execute(
        "INSERT INTO novel_fts (rowid, novel_id, title, caption, author_name, body) "
        "VALUES (1, 1, '对齐', '', '', '')"
    )
    db.conn.execute(
        "INSERT INTO novel_fts (rowid, novel_id, title, caption, author_name, body) "
        "VALUES (50, 2, '错位', '', '', '')"
    )

    db._migrate_novel_fts_rowid()

    mismatched = db.conn.execute(
        "SELECT COUNT(*) FROM novel_fts WHERE rowid != novel_id"
    ).fetchone()[0]
    assert mismatched == 0
    db.close()

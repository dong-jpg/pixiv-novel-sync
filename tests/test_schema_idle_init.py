from __future__ import annotations

from pathlib import Path

from pixiv_novel_sync.storage_db import Database


def _traced(db: Database) -> list[str]:
    seen: list[str] = []
    db.conn.set_trace_callback(seen.append)
    return seen


def test_repeat_init_skips_full_foreign_key_check(tmp_path: Path) -> None:
    db = Database(tmp_path / "fresh.db")
    db.init_schema()
    seen = _traced(db)
    db.init_schema()
    db.close()

    assert not any("foreign_key_check" in sql for sql in seen)


def test_repeat_init_skips_source_url_and_membership_writes(tmp_path: Path) -> None:
    db = Database(tmp_path / "filled.db")
    db.init_schema()
    db.conn.execute(
        "INSERT INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')"
    )
    db.conn.execute(
        """
        INSERT INTO novels (
            novel_id, title, user_id, visible, restrict_value, x_restrict,
            text_length, total_bookmarks, total_views, tags_json, raw_json,
            meta_hash, source_url
        ) VALUES (10, '一篇', 1, 1, 'public', 0, 100, 0, 0, '[]', '{}', 'h', ?)
        """,
        ("https://www.pixiv.net/novel/show.php?id=10",),
    )
    db.conn.execute(
        """
        INSERT INTO series (series_id, title, user_id, source_url)
        VALUES (3, '系列', 1, ?)
        """,
        ("https://www.pixiv.net/novel/series/3",),
    )
    db._commit_if_needed()

    seen = _traced(db)
    db.init_schema()
    db.close()

    joined = "\n".join(seen)
    assert "UPDATE novels SET source_url" not in joined
    assert "UPDATE series SET source_url" not in joined
    assert "INSERT OR IGNORE INTO rescue_catalog_memberships" not in joined
    assert "UPDATE users SET status" not in joined

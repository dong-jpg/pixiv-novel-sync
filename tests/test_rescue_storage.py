from __future__ import annotations

from pathlib import Path

import pytest

from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "rescue.db")
    database.init_schema()
    yield database
    database.close()


def _seed_novel(db: Database, novel_id: int = 1) -> None:
    db.conn.execute(
        "INSERT OR IGNORE INTO users (user_id, name, raw_json) VALUES (2, '作者', '{}')"
    )
    db.conn.execute(
        """
        INSERT INTO novels (
            novel_id, user_id, title, visible, restrict_value, x_restrict,
            text_length, total_bookmarks, total_views, tags_json, raw_json,
            meta_hash
        ) VALUES (?, 2, '小说', 1, 'public', 0, 1, 0, 0, '[]', '{}', 'h')
        """,
        (novel_id,),
    )
    db.conn.commit()


def _seed_series(db: Database, series_id: int = 9) -> None:
    db.conn.execute(
        "INSERT OR IGNORE INTO users (user_id, name, raw_json) VALUES (2, '作者', '{}')"
    )
    db.conn.execute(
        "INSERT INTO series (series_id, title, user_id) VALUES (?, '系列', 2)",
        (series_id,),
    )
    db.conn.commit()


def test_rescue_schema_and_override_crud(db: Database) -> None:
    tables = {
        str(row[0])
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"rescue_overrides", "rescue_api_token"} <= tables

    _seed_novel(db)
    assert db.get_rescue_override("novel", 1) is None
    saved = db.set_rescue_override("novel", 1, "include", "页面已失效")
    assert saved["action"] == "include"
    assert db.get_rescue_override("novel", 1)["note"] == "页面已失效"
    assert db.delete_rescue_override("novel", 1) is True
    assert db.get_rescue_override("novel", 1) is None


@pytest.mark.parametrize(
    ("item_type", "action", "message"),
    [
        ("user", "include", "item_type"),
        ("novel", "restore", "action"),
    ],
)
def test_rescue_override_rejects_invalid_values(
    db: Database,
    item_type: str,
    action: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        db.set_rescue_override(item_type, 1, action)


def test_delete_novel_cleans_rescue_override(db: Database) -> None:
    _seed_novel(db)
    db.set_rescue_override("novel", 1, "include")

    db.delete_novel(1)

    assert db.get_rescue_override("novel", 1) is None


def test_delete_series_cleans_rescue_override(db: Database) -> None:
    _seed_series(db)
    db.set_rescue_override("series", 9, "exclude")

    db.delete_series(9)

    assert db.get_rescue_override("series", 9) is None

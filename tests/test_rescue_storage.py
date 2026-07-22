from __future__ import annotations

from pathlib import Path

import pytest

from pixiv_novel_sync.models import NovelTextRecord
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "rescue.db")
    database.init_schema()
    yield database
    database.close()


def _seed_novel(
    db: Database,
    novel_id: int = 1,
    *,
    status: str = "unknown",
    text: str | None = "正文",
    series_id: int | None = None,
    title: str = "小说",
) -> None:
    db.conn.execute(
        "INSERT OR IGNORE INTO users (user_id, name, raw_json) VALUES (2, '作者', '{}')"
    )
    db.conn.execute(
        """
        INSERT INTO novels (
            novel_id, user_id, series_id, title, visible, restrict_value, x_restrict,
            text_length, total_bookmarks, total_views, tags_json, raw_json,
            meta_hash, status
        ) VALUES (?, 2, ?, ?, 1, 'public', 0, ?, 0, 0, '["标签"]', '{}', ?, ?)
        """,
        (
            novel_id,
            series_id,
            title,
            len(text or ""),
            f"h-{novel_id}",
            status,
        ),
    )
    if text is not None:
        db.upsert_novel_text(
            NovelTextRecord(
                novel_id=novel_id,
                text_raw=text,
                text_markdown=None,
                text_hash=f"t-{novel_id}",
            )
        )
    db.conn.commit()


def test_novel_text_maintains_has_content(db: Database) -> None:
    _seed_novel(db, novel_id=90, text="正文")
    assert db.conn.execute(
        "SELECT has_content FROM novel_texts WHERE novel_id = 90"
    ).fetchone()[0] == 1

    _seed_novel(db, novel_id=91, text="  \n")
    assert db.conn.execute(
        "SELECT has_content FROM novel_texts WHERE novel_id = 91"
    ).fetchone()[0] == 0

    db.upsert_novel_text(
        NovelTextRecord(
            novel_id=90,
            text_raw=" \t\n",
            text_markdown=None,
            text_hash="t-90-empty",
        )
    )
    assert db.conn.execute(
        "SELECT has_content FROM novel_texts WHERE novel_id = 90"
    ).fetchone()[0] == 0


def test_novel_text_migration_backfills_and_preserves_foreign_key(db: Database) -> None:
    _seed_novel(db, novel_id=92, text="历史正文")
    _seed_novel(db, novel_id=93, text="   ")

    db.conn.executescript(
        """
        ALTER TABLE novel_texts RENAME TO novel_texts_current;
        CREATE TABLE novel_texts (
            novel_id INTEGER PRIMARY KEY,
            text_raw TEXT NOT NULL,
            text_markdown TEXT,
            text_hash TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO novel_texts (novel_id, text_raw, text_markdown, text_hash, fetched_at)
        SELECT novel_id, text_raw, text_markdown, text_hash, fetched_at
        FROM novel_texts_current;
        DROP TABLE novel_texts_current;
        """
    )
    db.conn.commit()

    db.init_schema()

    rows = db.conn.execute(
        """
        SELECT novel_id, has_content
        FROM novel_texts
        WHERE novel_id IN (92, 93)
        ORDER BY novel_id
        """
    ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [(92, 1), (93, 0)]
    assert any(
        row[2] == "novels" and row[3] == "novel_id"
        for row in db.conn.execute("PRAGMA foreign_key_list(novel_texts)").fetchall()
    )


def _seed_series(
    db: Database,
    series_id: int = 9,
    *,
    status: str = "unknown",
    total_novels: int = 0,
    title: str = "系列",
) -> None:
    db.conn.execute(
        "INSERT OR IGNORE INTO users (user_id, name, raw_json) VALUES (2, '作者', '{}')"
    )
    db.conn.execute(
        "INSERT INTO series (series_id, title, user_id, total_novels, status) VALUES (?, ?, 2, ?, ?)",
        (series_id, title, total_novels, status),
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


def test_delete_user_cleans_owned_novel_rescue_overrides(db: Database) -> None:
    _seed_novel(db, novel_id=14)
    db.set_rescue_override("novel", 14, "include")

    db.delete_user(2)

    assert db.get_rescue_override("novel", 14) is None


@pytest.mark.parametrize("status", ["deleted", "restricted"])
def test_unavailable_novel_with_body_is_rescue_success(
    db: Database,
    status: str,
) -> None:
    _seed_novel(db, 10, status=status, text="救援正文")

    item = db.get_rescue_novel(10)

    assert item is not None
    assert item["rescue_state"] == "success"
    assert item["eligibility_reason"] == "novel_unavailable"
    assert item["text_raw"] == "救援正文"
    assert item["tags"] == ["标签"]


@pytest.mark.parametrize("text", [None, "", "   \n"])
def test_unavailable_novel_without_body_is_hidden(
    db: Database,
    text: str | None,
) -> None:
    _seed_novel(db, 11, status="deleted", text=text)

    assert db.get_rescue_novel(11) is None


def test_novel_override_changes_availability_but_not_completeness(db: Database) -> None:
    _seed_novel(db, 12, status="normal", text="正文")
    assert db.get_rescue_novel(12) is None

    db.set_rescue_override("novel", 12, "include")
    assert db.get_rescue_novel(12)["rescue_state"] == "success"

    db.set_rescue_override("novel", 12, "exclude")
    assert db.get_rescue_novel(12) is None

    _seed_novel(db, 13, status="normal", text="")
    db.set_rescue_override("novel", 13, "include")
    assert db.get_rescue_novel(13) is None


def test_series_strict_success_requires_all_expected_bodies(db: Database) -> None:
    _seed_series(db, 20, status="deleted", total_novels=3)
    _seed_novel(db, 21, series_id=20, text="一")
    _seed_novel(db, 22, series_id=20, text="二")
    _seed_novel(db, 23, series_id=20, text="三")

    item = db.get_rescue_series(20)

    assert item is not None
    assert item["rescue_state"] == "success"
    assert item["expected_count"] == 3
    assert item["local_count"] == 3
    assert item["complete_count"] == 3


def test_series_is_partial_when_total_unknown_or_any_local_body_missing(
    db: Database,
) -> None:
    _seed_series(db, 30, status="deleted", total_novels=0)
    _seed_novel(db, 31, series_id=30, text="一")
    assert db.get_rescue_series(30)["rescue_state"] == "partial"

    _seed_series(db, 40, status="deleted", total_novels=2)
    _seed_novel(db, 41, series_id=40, text="一")
    _seed_novel(db, 42, series_id=40, text="")
    item = db.get_rescue_series(40)
    assert item["rescue_state"] == "partial"
    assert item["local_count"] == 2
    assert item["complete_count"] == 1


def test_parent_series_allows_normal_chapter_and_exclude_blocks_it(db: Database) -> None:
    _seed_series(db, 50, status="deleted", total_novels=1)
    _seed_novel(db, 51, series_id=50, status="normal", text="章节")

    item = db.get_rescue_novel(51)
    assert item is not None
    assert item["eligibility_reason"] == "parent_series_unavailable"

    db.set_rescue_override("series", 50, "exclude")
    assert db.get_rescue_novel(51) is None


def test_rescue_list_deduplicates_series_chapters_and_filters(db: Database) -> None:
    _seed_series(db, 60, status="deleted", total_novels=1, title="目标系列")
    _seed_novel(db, 61, series_id=60, status="deleted", text="系列章节")
    _seed_novel(db, 62, status="restricted", text="单篇", title="目标单篇")

    payload = db.list_rescues(page=1, page_size=10)
    identities = {(item["item_type"], item["item_id"]) for item in payload["items"]}
    assert identities == {("series", 60), ("novel", 62)}

    novels = db.list_rescues(
        page=1,
        page_size=10,
        item_type="novel",
        search="目标单篇",
    )
    assert [(item["item_type"], item["item_id"]) for item in novels["items"]] == [
        ("novel", 62)
    ]


def test_rescue_series_chapters_are_paginated_without_bodies(db: Database) -> None:
    _seed_series(db, 70, status="deleted", total_novels=2)
    _seed_novel(db, 71, series_id=70, text="第一章")
    _seed_novel(db, 72, series_id=70, text="第二章")

    payload = db.list_rescue_series_chapters(70, page=2, page_size=1)

    assert payload is not None
    assert payload["total"] == 2
    assert payload["items"][0]["novel_id"] == 72
    assert "text_raw" not in payload["items"][0]


def test_rescue_token_record_is_singleton(db: Database) -> None:
    assert db.get_rescue_token_record() is None

    first = db.save_rescue_token_record("hash-1", "rsq_one")
    second = db.save_rescue_token_record("hash-2", "rsq_two")

    assert first["token_hash"] == "hash-1"
    assert second["token_hash"] == "hash-2"
    assert db.conn.execute("SELECT COUNT(*) FROM rescue_api_token").fetchone()[0] == 1

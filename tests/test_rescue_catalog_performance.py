from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from pixiv_novel_sync.storage_db import Database


def _insert_rescue_novels(db: Database, start: int, stop: int) -> None:
    db.conn.execute(
        "INSERT OR IGNORE INTO users (user_id, name, raw_json) "
        "VALUES (1, '性能测试作者', '{}')"
    )
    db.conn.executemany(
        """
        INSERT INTO novels (
            novel_id, user_id, title, visible, restrict_value, x_restrict,
            text_length, total_bookmarks, total_views, tags_json, raw_json,
            meta_hash, status, last_checked_at, last_seen_at
        ) VALUES (?, 1, ?, 1, 'public', 0, 2, 0, 0, '[]', '{}', ?,
                  'deleted', ?, ?)
        """,
        [
            (
                novel_id,
                f"救援小说 {novel_id}",
                f"h-{novel_id}",
                f"2024-01-{novel_id % 28 + 1:02d} 00:00:00",
                f"2024-02-{novel_id % 28 + 1:02d} 00:00:00",
            )
            for novel_id in range(start, stop)
        ],
    )
    db.conn.executemany(
        """
        INSERT INTO novel_texts (novel_id, text_raw, has_content, text_hash)
        VALUES (?, '正文', 1, ?)
        """,
        [(novel_id, f"t-{novel_id}") for novel_id in range(start, stop)],
    )
    db.conn.executemany(
        "INSERT INTO sources (novel_id, source_type, source_key) "
        "VALUES (?, 'bookmark_public', '1')",
        [(novel_id,) for novel_id in range(start, stop)],
    )
    db.conn.commit()


def _is_select_statement(statement: str) -> bool:
    return bool(
        re.match(
            r"(?is)^\s*(?:(?:--[^\n]*\n)|(?:/\*.*?\*/\s*))*\s*(?:SELECT|WITH)\b",
            statement,
        )
    )


def _trace_catalog_page(
    db: Database,
    page: int = 1,
) -> tuple[dict, list[str], set[tuple[str, str]]]:
    statements: list[str] = []
    reads: set[tuple[str, str]] = set()

    def authorize(
        action: int,
        table: str | None,
        column: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and table:
            reads.add((table, column or ""))
        return sqlite3.SQLITE_OK

    db.conn.set_trace_callback(statements.append)
    db.conn.set_authorizer(authorize)
    try:
        payload = db.list_rescues(page=page, page_size=25)
    finally:
        db.conn.set_trace_callback(None)
        db.conn.set_authorizer(None)
    selects = [
        statement
        for statement in statements
        if _is_select_statement(statement)
    ]
    return payload, selects, reads


def test_catalog_list_query_count_is_constant_and_uses_sql_pagination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(tmp_path / "catalog-performance.db")
    db.init_schema()
    try:
        def fail_live_read(*_args, **_kwargs):
            raise AssertionError("目录列表不得调用实时正文读取方法")

        _insert_rescue_novels(db, 1, 21)
        db.rebuild_rescue_catalog()
        with monkeypatch.context() as patcher:
            patcher.setattr(Database, "get_rescue_novel", fail_live_read)
            patcher.setattr(Database, "get_rescue_series", fail_live_read)
            small_payload, small_selects, small_reads = _trace_catalog_page(db)

        _insert_rescue_novels(db, 21, 201)
        db.conn.execute(
            """
            INSERT INTO series (
                series_id, title, user_id, total_novels, status, last_checked_at
            ) VALUES (900, '性能测试系列', 1, 1, 'deleted', '2030-01-01 00:00:00')
            """
        )
        db.conn.execute("UPDATE novels SET series_id = 900 WHERE novel_id = 200")
        db.conn.commit()
        db.rebuild_rescue_catalog()
        with monkeypatch.context() as patcher:
            patcher.setattr(Database, "get_rescue_novel", fail_live_read)
            patcher.setattr(Database, "get_rescue_series", fail_live_read)
            payload, selects, reads = _trace_catalog_page(db)
            second_page, second_selects, second_reads = _trace_catalog_page(db, page=2)
    finally:
        db.close()

    normalized_sql = [" ".join(statement.upper().split()) for statement in selects]
    normalized_second_sql = [
        " ".join(statement.upper().split()) for statement in second_selects
    ]
    assert len(small_selects) == len(selects)
    assert len(small_selects) <= 4
    assert len(second_selects) <= 4
    assert small_payload["total"] == 20
    assert payload["total"] == 200
    assert len(payload["items"]) == 25
    assert second_page["total"] == 200
    assert len(second_page["items"]) == 25
    assert payload["items"][0]["item_type"] == "series"
    assert {
        (item["item_type"], item["item_id"])
        for item in payload["items"]
    }.isdisjoint(
        (item["item_type"], item["item_id"])
        for item in second_page["items"]
    )
    all_selects = small_selects + selects + second_selects
    assert all("TEXT_RAW" not in statement.upper() for statement in all_selects)
    allowed_tables = {
        "rescue_catalog",
        "rescue_catalog_sources",
        "rescue_catalog_meta",
    }
    assert {table for table, _column in small_reads | reads | second_reads} <= allowed_tables

    page_queries = [
        statement
        for statement in normalized_sql
        if "FROM RESCUE_CATALOG RC" in statement and "COUNT(" not in statement
    ]
    second_page_queries = [
        statement
        for statement in normalized_second_sql
        if "FROM RESCUE_CATALOG RC" in statement and "COUNT(" not in statement
    ]
    assert len(page_queries) == len(second_page_queries) == 1
    assert "ORDER BY" in page_queries[0]
    assert "LIMIT 25 OFFSET 0" in page_queries[0]
    assert "ORDER BY" in second_page_queries[0]
    assert "LIMIT 25 OFFSET 25" in second_page_queries[0]

    source_queries = [
        statement
        for statement in normalized_sql
        if "FROM RESCUE_CATALOG_SOURCES" in statement
    ]
    assert len(source_queries) == 1
    assert source_queries[0].count("ITEM_TYPE =") == len(payload["items"])
    assert source_queries[0].count("ITEM_ID =") == len(payload["items"])


def test_empty_initialized_catalog_skips_source_query(tmp_path: Path) -> None:
    db = Database(tmp_path / "empty-catalog.db")
    db.init_schema()
    try:
        db.rebuild_rescue_catalog()
        payload, selects, reads = _trace_catalog_page(db)
    finally:
        db.close()

    assert payload["items"] == []
    assert len(selects) <= 4
    assert not any("FROM RESCUE_CATALOG_SOURCES" in sql.upper() for sql in selects)
    assert {table for table, _column in reads} <= {
        "rescue_catalog",
        "rescue_catalog_meta",
    }

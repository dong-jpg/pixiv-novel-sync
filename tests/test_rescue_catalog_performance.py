from __future__ import annotations

import re
import sqlite3
import threading
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


def _freeze_payload(value):
    if isinstance(value, dict):
        return tuple(
            sorted((str(key), _freeze_payload(item)) for key, item in value.items())
        )
    if isinstance(value, list):
        return tuple(_freeze_payload(item) for item in value)
    return value


def _catalog_signature(payload: dict) -> tuple:
    return (
        payload["refreshed_at"],
        payload["total"],
        _freeze_payload(payload["items"]),
    )


def test_catalog_list_uses_one_snapshot_during_concurrent_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_timestamp = "2024-01-01 00:00:00"
    new_timestamp = "2025-01-01 00:00:00"
    db_path = tmp_path / "concurrent-catalog.db"
    writer = Database(db_path)
    reader = Database(db_path)
    writer.init_schema()
    reader.init_schema()
    assert writer.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    _insert_rescue_novels(writer, 1, 3)
    monkeypatch.setattr(writer, "_catalog_timestamp", lambda: old_timestamp)
    writer.rebuild_rescue_catalog()
    old_payload = writer.list_rescues(page=1, page_size=10)

    # Prepare the next generation before the reader starts. Inside the
    # controlled race the writer performs only the atomic catalog rebuild.
    _insert_rescue_novels(writer, 3, 4)
    writer.conn.execute("UPDATE novels SET status = 'normal' WHERE novel_id IN (1, 2)")
    writer.conn.execute("DELETE FROM sources WHERE novel_id = 3")
    writer.conn.execute(
        "INSERT INTO sources (novel_id, source_type, source_key) "
        "VALUES (3, 'following_user_scan', '1')"
    )
    writer.conn.commit()
    monkeypatch.setattr(writer, "_catalog_timestamp", lambda: new_timestamp)

    page_query_reached = threading.Event()
    writer_committed = threading.Event()
    reader_wait_timed_out = threading.Event()
    results: list[dict] = []
    errors: list[BaseException] = []
    callback_errors: list[BaseException] = []

    def read_catalog() -> None:
        conn = reader.conn

        def pause_before_page(statement: str) -> None:
            try:
                normalized = " ".join(statement.upper().split())
                is_page_query = (
                    "FROM RESCUE_CATALOG RC" in normalized
                    and "COUNT(" not in normalized
                    and "LIMIT" in normalized
                )
                if is_page_query and not page_query_reached.is_set():
                    page_query_reached.set()
                    if not writer_committed.wait(timeout=10):
                        reader_wait_timed_out.set()
            except BaseException as exc:  # sqlite3 otherwise swallows callback errors
                callback_errors.append(exc)
                page_query_reached.set()

        conn.set_trace_callback(pause_before_page)
        try:
            results.append(reader.list_rescues(page=1, page_size=10))
        except BaseException as exc:  # pragma: no cover - asserted in the main thread
            errors.append(exc)
        finally:
            conn.set_trace_callback(None)

    thread = threading.Thread(target=read_catalog, name="rescue-catalog-reader")
    thread.start()
    try:
        assert page_query_reached.wait(timeout=10), "读者未在 page SELECT 前暂停"

        writer.rebuild_rescue_catalog()
        writer_committed.set()

        thread.join(timeout=10)
        assert not thread.is_alive(), "读者线程未结束"
        assert not reader_wait_timed_out.is_set(), "读者等待写者提交超时"
        assert callback_errors == []
        assert errors == []
        assert len(results) == 1

        new_payload = writer.list_rescues(page=1, page_size=10)
        assert _catalog_signature(old_payload) != _catalog_signature(new_payload)
        assert _catalog_signature(results[0]) in {
            _catalog_signature(old_payload),
            _catalog_signature(new_payload),
        }
    finally:
        writer_committed.set()
        thread.join(timeout=10)
        reader.close()
        writer.close()


def test_catalog_list_joins_existing_write_transaction(tmp_path: Path) -> None:
    db = Database(tmp_path / "nested-catalog.db")
    db.init_schema()
    _insert_rescue_novels(db, 1, 2)
    db.rebuild_rescue_catalog()
    original_title = db.get_rescue_catalog_item("novel", 1)["title"]
    try:
        with pytest.raises(RuntimeError, match="rollback outer"):
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE rescue_catalog SET title = '未提交标题' "
                    "WHERE item_type = 'novel' AND item_id = 1"
                )
                payload = db.list_rescues(page=1, page_size=10)
                assert payload["items"][0]["title"] == "未提交标题"
                assert conn.in_transaction
                raise RuntimeError("rollback outer")

        assert not db.conn.in_transaction
        assert db.list_rescues(page=1, page_size=10)["items"][0]["title"] == original_title
    finally:
        db.close()


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

"""Rescue classification overrides and read-only API token storage."""
from __future__ import annotations

import sqlite3
from typing import Any


class RescueMixin:
    """Storage operations for rescue overrides and derived rescue views."""

    conn: sqlite3.Connection
    _lock: Any
    _commit_if_needed: Any

    _RESCUE_ITEM_TABLES = {"novel": "novels", "series": "series"}
    _RESCUE_ACTIONS = {"include", "exclude"}

    @classmethod
    def _validate_rescue_item_type(cls, item_type: str) -> str:
        normalized = str(item_type or "").strip().lower()
        if normalized not in cls._RESCUE_ITEM_TABLES:
            raise ValueError("item_type 必须是 novel 或 series")
        return normalized

    @classmethod
    def _validate_rescue_action(cls, action: str) -> str:
        normalized = str(action or "").strip().lower()
        if normalized not in cls._RESCUE_ACTIONS:
            raise ValueError("action 必须是 include 或 exclude")
        return normalized

    def _rescue_item_exists(self, item_type: str, item_id: int) -> bool:
        table = self._RESCUE_ITEM_TABLES[item_type]
        id_column = "novel_id" if item_type == "novel" else "series_id"
        row = self.conn.execute(
            f"SELECT 1 FROM {table} WHERE {id_column} = ? LIMIT 1",
            (item_id,),
        ).fetchone()
        return row is not None

    def get_rescue_override(
        self,
        item_type: str,
        item_id: int,
    ) -> dict[str, Any] | None:
        normalized_type = self._validate_rescue_item_type(item_type)
        row = self.conn.execute(
            """
            SELECT item_type, item_id, action, note, created_at, updated_at
            FROM rescue_overrides
            WHERE item_type = ? AND item_id = ?
            """,
            (normalized_type, int(item_id)),
        ).fetchone()
        return dict(row) if row else None

    def set_rescue_override(
        self,
        item_type: str,
        item_id: int,
        action: str,
        note: str = "",
    ) -> dict[str, Any]:
        normalized_type = self._validate_rescue_item_type(item_type)
        normalized_action = self._validate_rescue_action(action)
        normalized_note = str(note or "").strip()
        if len(normalized_note) > 500:
            raise ValueError("note 不能超过 500 个字符")
        normalized_id = int(item_id)
        if not self._rescue_item_exists(normalized_type, normalized_id):
            raise ValueError("救援对象不存在")

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO rescue_overrides (
                    item_type, item_id, action, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(item_type, item_id) DO UPDATE SET
                    action = excluded.action,
                    note = excluded.note,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (normalized_type, normalized_id, normalized_action, normalized_note),
            )
            self._commit_if_needed()
        return self.get_rescue_override(normalized_type, normalized_id) or {}

    def delete_rescue_override(self, item_type: str, item_id: int) -> bool:
        normalized_type = self._validate_rescue_item_type(item_type)
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM rescue_overrides WHERE item_type = ? AND item_id = ?",
                (normalized_type, int(item_id)),
            )
            self._commit_if_needed()
        return bool(cursor.rowcount)

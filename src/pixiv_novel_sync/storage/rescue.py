"""Rescue classification overrides and read-only API token storage."""
from __future__ import annotations

import json
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

    @staticmethod
    def _remote_unavailable(
        item_type: str,
        remote_status: str,
        override_action: str | None,
    ) -> bool:
        if override_action == "exclude":
            return False
        if override_action == "include":
            return True
        if item_type == "novel":
            return remote_status in {"deleted", "restricted"}
        return remote_status == "deleted"

    @staticmethod
    def _series_state(
        remote_unavailable: bool,
        expected_count: int,
        local_count: int,
        complete_count: int,
    ) -> str | None:
        if not remote_unavailable or complete_count == 0:
            return None
        if (
            expected_count > 0
            and local_count >= expected_count
            and complete_count == local_count
        ):
            return "success"
        return "partial"

    def _series_summary_rows(self, series_id: int | None = None) -> list[dict[str, Any]]:
        where_sql = "WHERE se.series_id = ?" if series_id is not None else ""
        params: tuple[Any, ...] = (int(series_id),) if series_id is not None else ()
        rows = self.conn.execute(
            f"""
            SELECT
                se.series_id,
                se.title,
                se.description,
                se.user_id,
                u.name AS author_name,
                COALESCE(
                    NULLIF(se.cover_url, ''),
                    (
                        SELECT n2.cover_url
                        FROM novels n2
                        WHERE n2.series_id = se.series_id
                          AND n2.cover_url IS NOT NULL
                          AND n2.cover_url != ''
                        ORDER BY n2.create_date ASC, n2.novel_id ASC
                        LIMIT 1
                    )
                ) AS cover_url,
                COALESCE(se.total_novels, 0) AS expected_count,
                COUNT(n.novel_id) AS local_count,
                COALESCE(SUM(
                    CASE
                        WHEN TRIM(COALESCE(nt.text_raw, '')) != '' THEN 1
                        ELSE 0
                    END
                ), 0) AS complete_count,
                se.status AS remote_status,
                se.last_checked_at,
                se.last_seen_at AS updated_at,
                ro.action AS override_action,
                ro.note AS override_note
            FROM series se
            LEFT JOIN users u ON u.user_id = se.user_id
            LEFT JOIN novels n ON n.series_id = se.series_id
            LEFT JOIN novel_texts nt ON nt.novel_id = n.novel_id
            LEFT JOIN rescue_overrides ro
              ON ro.item_type = 'series' AND ro.item_id = se.series_id
            {where_sql}
            GROUP BY
                se.series_id, se.title, se.description, se.user_id, u.name,
                se.cover_url, se.total_novels, se.status, se.last_checked_at,
                se.last_seen_at, ro.action, ro.note
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def _series_evaluation_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        expected_count = int(row.get("expected_count") or 0)
        local_count = int(row.get("local_count") or 0)
        complete_count = int(row.get("complete_count") or 0)
        remote_status = str(row.get("remote_status") or "unknown")
        remote_unavailable = self._remote_unavailable(
            "series",
            remote_status,
            row.get("override_action"),
        )
        state = self._series_state(
            remote_unavailable,
            expected_count,
            local_count,
            complete_count,
        )
        return {
            "item_type": "series",
            "item_id": int(row["series_id"]),
            "series_id": int(row["series_id"]),
            "title": str(row.get("title") or f"系列 {row['series_id']}"),
            "description": str(row.get("description") or ""),
            "user_id": int(row.get("user_id") or 0),
            "author_name": str(row.get("author_name") or "未知作者"),
            "cover_url": row.get("cover_url"),
            "rescue_state": state,
            "remote_status": remote_status,
            "remote_unavailable": remote_unavailable,
            "eligibility_reason": "series_unavailable" if state else None,
            "expected_count": expected_count if expected_count > 0 else None,
            "local_count": local_count,
            "complete_count": complete_count,
            "last_checked_at": row.get("last_checked_at"),
            "updated_at": row.get("updated_at"),
            "override_action": row.get("override_action"),
            "override_note": str(row.get("override_note") or ""),
        }

    def _series_rescue_payload(self, row: dict[str, Any]) -> dict[str, Any] | None:
        payload = self._series_evaluation_payload(row)
        return payload if payload["rescue_state"] is not None else None

    def evaluate_rescue_series(self, series_id: int) -> dict[str, Any] | None:
        rows = self._series_summary_rows(int(series_id))
        if not rows:
            return None
        return self._series_evaluation_payload(rows[0])

    def get_rescue_series(self, series_id: int) -> dict[str, Any] | None:
        payload = self.evaluate_rescue_series(int(series_id))
        if payload is None or payload["rescue_state"] is None:
            return None
        return payload

    @staticmethod
    def _decode_tags(value: Any) -> list[Any]:
        try:
            parsed = json.loads(str(value or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []

    def _novel_rescue_row(self, novel_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT
                n.novel_id,
                n.title,
                n.caption,
                n.user_id,
                u.name AS author_name,
                n.series_id,
                n.cover_url,
                n.tags_json,
                n.create_date,
                n.status AS remote_status,
                n.last_checked_at,
                n.last_seen_at AS updated_at,
                nt.text_raw,
                ro.action AS override_action,
                ro.note AS override_note
            FROM novels n
            LEFT JOIN users u ON u.user_id = n.user_id
            LEFT JOIN novel_texts nt ON nt.novel_id = n.novel_id
            LEFT JOIN rescue_overrides ro
              ON ro.item_type = 'novel' AND ro.item_id = n.novel_id
            WHERE n.novel_id = ?
            """,
            (int(novel_id),),
        ).fetchone()
        return dict(row) if row else None

    def _novel_evaluation_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        text_raw = str(data.get("text_raw") or "")
        body_complete = bool(text_raw.strip())

        remote_status = str(data.get("remote_status") or "unknown")
        own_unavailable = self._remote_unavailable(
            "novel",
            remote_status,
            data.get("override_action"),
        )
        parent: dict[str, Any] | None = None
        eligibility_reason: str | None = None
        rescue_state: str | None = None
        if body_complete and own_unavailable:
            eligibility_reason = "novel_unavailable"
            rescue_state = "success"
        elif body_complete:
            series_id = data.get("series_id")
            if series_id is not None:
                parent = self.get_rescue_series(int(series_id))
                if parent is not None:
                    eligibility_reason = "parent_series_unavailable"
                    rescue_state = str(parent["rescue_state"])

        return {
            "item_type": "novel",
            "item_id": int(data["novel_id"]),
            "novel_id": int(data["novel_id"]),
            "title": str(data.get("title") or f"小说 {data['novel_id']}"),
            "caption": str(data.get("caption") or ""),
            "user_id": int(data.get("user_id") or 0),
            "author_name": str(data.get("author_name") or "未知作者"),
            "series_id": int(data["series_id"]) if data.get("series_id") is not None else None,
            "cover_url": data.get("cover_url"),
            "tags": self._decode_tags(data.get("tags_json")),
            "create_date": data.get("create_date"),
            "text_raw": text_raw,
            "rescue_state": rescue_state,
            "remote_status": remote_status,
            "remote_unavailable": own_unavailable,
            "body_complete": body_complete,
            "eligibility_reason": eligibility_reason,
            "expected_count": parent.get("expected_count") if parent else None,
            "local_count": int(parent.get("local_count") or 0) if parent else 1,
            "complete_count": int(parent.get("complete_count") or 0) if parent else int(body_complete),
            "last_checked_at": data.get("last_checked_at"),
            "updated_at": data.get("updated_at"),
            "override_action": data.get("override_action"),
            "override_note": str(data.get("override_note") or ""),
        }

    def evaluate_rescue_novel(self, novel_id: int) -> dict[str, Any] | None:
        data = self._novel_rescue_row(int(novel_id))
        if data is None:
            return None
        payload = self._novel_evaluation_payload(data)
        return {
            key: value
            for key, value in payload.items()
            if key not in {"text_raw", "caption", "tags"}
        }

    def get_rescue_novel(self, novel_id: int) -> dict[str, Any] | None:
        data = self._novel_rescue_row(int(novel_id))
        if data is None:
            return None
        payload = self._novel_evaluation_payload(data)
        if payload["rescue_state"] is None:
            return None
        return payload

    def list_rescues(
        self,
        page: int = 1,
        page_size: int = 12,
        state: str = "all",
        item_type: str = "all",
        search: str = "",
        sort: str = "checked_desc",
    ) -> dict[str, Any]:
        normalized_state = str(state or "all").strip().lower()
        normalized_type = str(item_type or "all").strip().lower()
        normalized_sort = str(sort or "checked_desc").strip().lower()
        if normalized_state not in {"all", "success", "partial"}:
            raise ValueError("state 参数无效")
        if normalized_type not in {"all", "novel", "series"}:
            raise ValueError("item_type 参数无效")
        if normalized_sort not in {"checked_desc", "updated_desc"}:
            raise ValueError("sort 参数无效")

        series_items = [
            payload
            for row in self._series_summary_rows()
            if (payload := self._series_rescue_payload(row)) is not None
        ]
        rescue_series_ids = {int(item["series_id"]) for item in series_items}
        novel_rows = self.conn.execute(
            """
            SELECT n.novel_id, n.series_id
            FROM novels n
            JOIN novel_texts nt ON nt.novel_id = n.novel_id
            LEFT JOIN rescue_overrides ro
              ON ro.item_type = 'novel' AND ro.item_id = n.novel_id
            WHERE TRIM(COALESCE(nt.text_raw, '')) != ''
              AND (
                    ro.action = 'include'
                    OR (
                        ro.action IS NULL
                        AND n.status IN ('deleted', 'restricted')
                    )
              )
            """
        ).fetchall()
        novel_items: list[dict[str, Any]] = []
        for row in novel_rows:
            if row["series_id"] is not None and int(row["series_id"]) in rescue_series_ids:
                continue
            payload = self.get_rescue_novel(int(row["novel_id"]))
            if payload is not None and payload["eligibility_reason"] == "novel_unavailable":
                novel_items.append(payload)

        items = series_items + novel_items
        if normalized_state != "all":
            items = [item for item in items if item["rescue_state"] == normalized_state]
        if normalized_type != "all":
            items = [item for item in items if item["item_type"] == normalized_type]
        query = str(search or "").strip().casefold()
        if query:
            items = [
                item
                for item in items
                if query in str(item.get("title") or "").casefold()
                or query in str(item.get("author_name") or "").casefold()
            ]

        primary_key = "last_checked_at" if normalized_sort == "checked_desc" else "updated_at"
        items.sort(
            key=lambda item: (
                str(item.get(primary_key) or ""),
                str(item.get("updated_at") or ""),
                int(item["item_id"]),
            ),
            reverse=True,
        )

        normalized_page = max(int(page), 1)
        normalized_size = max(int(page_size), 1)
        total = len(items)
        total_pages = max((total + normalized_size - 1) // normalized_size, 1)
        normalized_page = min(normalized_page, total_pages)
        offset = (normalized_page - 1) * normalized_size
        return {
            "items": items[offset:offset + normalized_size],
            "page": normalized_page,
            "page_size": normalized_size,
            "total": total,
            "total_pages": total_pages,
            "category": "rescue",
        }

    def list_rescue_series_chapters(
        self,
        series_id: int,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any] | None:
        series = self.get_rescue_series(int(series_id))
        if series is None:
            return None
        normalized_page = max(int(page), 1)
        normalized_size = max(int(page_size), 1)
        total_row = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM novels n
            JOIN novel_texts nt ON nt.novel_id = n.novel_id
            WHERE n.series_id = ? AND TRIM(COALESCE(nt.text_raw, '')) != ''
            """,
            (int(series_id),),
        ).fetchone()
        total = int(total_row[0]) if total_row else 0
        total_pages = max((total + normalized_size - 1) // normalized_size, 1)
        normalized_page = min(normalized_page, total_pages)
        offset = (normalized_page - 1) * normalized_size
        rows = self.conn.execute(
            """
            WITH available AS (
                SELECT
                    n.novel_id,
                    n.title,
                    n.create_date,
                    n.status AS remote_status,
                    n.text_length,
                    ROW_NUMBER() OVER (
                        ORDER BY n.create_date ASC, n.novel_id ASC
                    ) AS chapter_number
                FROM novels n
                JOIN novel_texts nt ON nt.novel_id = n.novel_id
                WHERE n.series_id = ?
                  AND TRIM(COALESCE(nt.text_raw, '')) != ''
            )
            SELECT * FROM available
            ORDER BY chapter_number ASC
            LIMIT ? OFFSET ?
            """,
            (int(series_id), normalized_size, offset),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["api_path"] = f"/api/rescue/v1/novels/{item['novel_id']}"
            items.append(item)
        return {
            "items": items,
            "page": normalized_page,
            "page_size": normalized_size,
            "total": total,
            "total_pages": total_pages,
            "rescue_state": series["rescue_state"],
            "expected_count": series["expected_count"],
            "local_count": series["local_count"],
            "complete_count": series["complete_count"],
        }

    def get_rescue_token_record(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT token_hash, token_prefix, rotated_at
            FROM rescue_api_token
            WHERE singleton_id = 1
            """
        ).fetchone()
        return dict(row) if row else None

    def save_rescue_token_record(
        self,
        token_hash: str,
        token_prefix: str,
    ) -> dict[str, Any]:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO rescue_api_token (
                    singleton_id, token_hash, token_prefix, rotated_at
                ) VALUES (1, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    token_hash = excluded.token_hash,
                    token_prefix = excluded.token_prefix,
                    rotated_at = CURRENT_TIMESTAMP
                """,
                (str(token_hash), str(token_prefix)),
            )
            self._commit_if_needed()
        return self.get_rescue_token_record() or {}

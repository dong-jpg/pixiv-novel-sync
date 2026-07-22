"""Rescue classification overrides and read-only API token storage."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any


def _sqlite_casefold(value: Any) -> str:
    return str(value or "").casefold()


class CatalogNotReadyError(RuntimeError):
    """Raised when the rescue catalog has not completed its first rebuild."""


class RescueMixin:
    """Storage operations for rescue overrides and derived rescue views."""

    conn: sqlite3.Connection
    _lock: Any
    _commit_if_needed: Any

    _RESCUE_ITEM_TABLES = {"novel": "novels", "series": "series"}
    _RESCUE_ACTIONS = {"include", "exclude"}
    _CATALOG_SOURCE_ORDER = {
        "bookmark": 0,
        "subscribed_series": 1,
        "following_user": 2,
        "user_backup": 3,
        "other": 4,
    }
    _CATALOG_COLUMNS = (
        "item_type", "item_id", "content_kind", "series_id", "title", "user_id",
        "author_name", "cover_url", "rescue_state", "remote_status",
        "eligibility_reason", "expected_count", "local_count", "complete_count",
        "last_checked_at", "updated_at", "refreshed_at",
    )
    _CONTENT_KIND_LABELS = {
        "series": "系列",
        "series_chapter": "系列单章",
        "standalone": "独立小说",
    }

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

    @staticmethod
    def _row_value(row: sqlite3.Row | dict[str, Any], key: str, default: Any = None) -> Any:
        """Read a value from either sqlite3.Row or a test-friendly mapping."""
        try:
            return row[key]
        except (IndexError, KeyError):
            return default

    @classmethod
    def _normalize_source(cls, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        """Map raw source facts to the stable catalog source vocabulary."""
        raw_type = str(cls._row_value(row, "source_type", "") or "").strip()
        raw_key = str(cls._row_value(row, "source_key", "") or "").strip()

        if raw_type.startswith("bookmark_"):
            source_kind = "bookmark"
            source_key = ""
        elif raw_type == "subscribed_series":
            source_kind = "subscribed_series"
            source_key = ""
        elif raw_type == "following_user_scan":
            source_kind = "following_user"
            source_key = raw_key
        elif raw_type == "user_backup":
            source_kind = "user_backup"
            source_key = raw_key
        else:
            source_kind = "other"
            # Keep distinct unknown facts distinct while still exposing one kind.
            source_key = f"{raw_type}:{raw_key}" if raw_type or raw_key else ""

        source_user_id = cls._row_value(row, "source_user_id")
        if source_kind in {"following_user", "user_backup"}:
            try:
                source_user_id = int(source_user_id if source_user_id is not None else raw_key)
            except (TypeError, ValueError):
                source_user_id = None
            if source_user_id is not None:
                source_key = str(source_user_id)
            source_user_name = cls._row_value(row, "source_user_name")
            if source_user_name is not None:
                source_user_name = str(source_user_name).strip() or None
        else:
            source_user_id = None
            source_user_name = None

        return {
            "source_kind": source_kind,
            "source_type": raw_type or source_kind,
            "source_key": source_key,
            "source_user_id": source_user_id,
            "source_user_name": source_user_name,
        }

    @classmethod
    def _source_sort_key(cls, source: dict[str, Any]) -> tuple[Any, ...]:
        return (
            cls._CATALOG_SOURCE_ORDER.get(str(source.get("source_kind") or "other"), 99),
            str(source.get("source_user_name") or "").casefold(),
            int(source.get("source_user_id") or 0),
            str(source.get("source_key") or ""),
            str(source.get("source_type") or ""),
        )

    @staticmethod
    def _catalog_timestamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _catalog_series_rows(
        self,
        series_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Return eligible series snapshots using only body completeness flags."""
        normalized_ids = sorted({int(value) for value in series_ids or set()})
        if series_ids is not None and not normalized_ids:
            return []
        where_sql = ""
        params: tuple[int, ...] = ()
        if normalized_ids:
            placeholders = ", ".join("?" for _ in normalized_ids)
            where_sql = f"WHERE se.series_id IN ({placeholders})"
            params = tuple(normalized_ids)
        rows = self.conn.execute(
            f"""
            SELECT
                se.series_id,
                se.title,
                se.user_id,
                u.name AS author_name,
                COALESCE(NULLIF(se.cover_url, ''), (
                    SELECT n2.cover_url
                    FROM novels n2
                    WHERE n2.series_id = se.series_id
                      AND n2.cover_url IS NOT NULL
                      AND n2.cover_url != ''
                    ORDER BY n2.create_date ASC, n2.novel_id ASC
                    LIMIT 1
                )) AS cover_url,
                COALESCE(se.total_novels, 0) AS expected_count,
                COUNT(n.novel_id) AS local_count,
                COALESCE(SUM(CASE WHEN COALESCE(nt.has_content, 0) = 1 THEN 1 ELSE 0 END), 0)
                    AS complete_count,
                COALESCE(se.status, 'unknown') AS remote_status,
                se.last_checked_at,
                se.last_seen_at AS updated_at,
                ro.action AS override_action
            FROM series se
            LEFT JOIN users u ON u.user_id = se.user_id
            LEFT JOIN novels n ON n.series_id = se.series_id
            LEFT JOIN novel_texts nt ON nt.novel_id = n.novel_id
            LEFT JOIN rescue_overrides ro
              ON ro.item_type = 'series' AND ro.item_id = se.series_id
            {where_sql}
            GROUP BY
                se.series_id, se.title, se.user_id, u.name, se.cover_url,
                se.total_novels, se.status, se.last_checked_at, se.last_seen_at,
                ro.action
            ORDER BY se.series_id
            """,
            params,
        ).fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            if data.get("override_action") == "exclude":
                continue
            remote_status = str(data.get("remote_status") or "unknown")
            remote_unavailable = (
                data.get("override_action") == "include" or remote_status == "deleted"
            )
            expected_count = int(data.get("expected_count") or 0)
            local_count = int(data.get("local_count") or 0)
            complete_count = int(data.get("complete_count") or 0)
            if not remote_unavailable or complete_count == 0:
                continue
            rescue_state = (
                "success"
                if expected_count > 0
                and local_count >= expected_count
                and complete_count == local_count
                else "partial"
            )
            series_id = int(data["series_id"])
            result.append(
                {
                    "item_type": "series",
                    "item_id": series_id,
                    "content_kind": "series",
                    "series_id": series_id,
                    "title": str(data.get("title") or f"系列 {series_id}"),
                    "user_id": int(data.get("user_id") or 0),
                    "author_name": str(data.get("author_name") or ""),
                    "cover_url": data.get("cover_url"),
                    "rescue_state": rescue_state,
                    "remote_status": remote_status,
                    "eligibility_reason": "series_unavailable",
                    "expected_count": expected_count if expected_count > 0 else None,
                    "local_count": local_count,
                    "complete_count": complete_count,
                    "last_checked_at": data.get("last_checked_at"),
                    "updated_at": data.get("updated_at"),
                }
            )
        return result

    def _catalog_novel_rows(
        self,
        rescue_series_ids: set[int],
        novel_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Return eligible standalone/chapter snapshots in one set query."""
        normalized_ids = sorted({int(value) for value in novel_ids or set()})
        if novel_ids is not None and not normalized_ids:
            return []
        filter_sql = ""
        params: tuple[int, ...] = ()
        if normalized_ids:
            placeholders = ", ".join("?" for _ in normalized_ids)
            filter_sql = f"AND n.novel_id IN ({placeholders})"
            params = tuple(normalized_ids)
        rows = self.conn.execute(
            f"""
            SELECT
                n.novel_id,
                n.series_id,
                n.title,
                n.user_id,
                u.name AS author_name,
                n.cover_url,
                COALESCE(n.status, 'unknown') AS remote_status,
                n.last_checked_at,
                n.last_seen_at AS updated_at,
                ro.action AS override_action
            FROM novels n
            LEFT JOIN users u ON u.user_id = n.user_id
            JOIN novel_texts nt ON nt.novel_id = n.novel_id
            LEFT JOIN rescue_overrides ro
              ON ro.item_type = 'novel' AND ro.item_id = n.novel_id
            WHERE COALESCE(nt.has_content, 0) = 1
              AND COALESCE(ro.action, '') != 'exclude'
              AND (
                    ro.action = 'include'
                    OR COALESCE(n.status, 'unknown') IN ('deleted', 'restricted')
              )
              {filter_sql}
            ORDER BY n.novel_id
            """,
            params,
        ).fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            series_value = data.get("series_id")
            series_id = int(series_value) if series_value is not None else None
            if series_id is not None and series_id in rescue_series_ids:
                continue
            novel_id = int(data["novel_id"])
            result.append(
                {
                    "item_type": "novel",
                    "item_id": novel_id,
                    "content_kind": "series_chapter" if series_id is not None else "standalone",
                    "series_id": series_id,
                    "title": str(data.get("title") or f"小说 {novel_id}"),
                    "user_id": int(data.get("user_id") or 0),
                    "author_name": str(data.get("author_name") or ""),
                    "cover_url": data.get("cover_url"),
                    "rescue_state": "success",
                    "remote_status": str(data.get("remote_status") or "unknown"),
                    "eligibility_reason": "novel_unavailable",
                    "expected_count": None,
                    "local_count": 1,
                    "complete_count": 1,
                    "last_checked_at": data.get("last_checked_at"),
                    "updated_at": data.get("updated_at"),
                }
            )
        return result

    @staticmethod
    def _source_user_expr(alias: str = "s") -> str:
        return (
            f"CASE WHEN {alias}.source_type IN ('following_user_scan', 'user_backup') "
            f"THEN CAST({alias}.source_key AS INTEGER) END"
        )

    def _catalog_source_fact_rows(
        self,
        item_type: str | None = None,
        item_id: int | None = None,
    ) -> list[sqlite3.Row]:
        """Fetch raw source facts for one item or every current catalog item."""
        user_expr = self._source_user_expr()
        if item_type is None:
            fact_user_expr = self._source_user_expr("fact")
            rows = self.conn.execute(
                f"""
                WITH source_facts AS (
                    SELECT rc.item_type, rc.item_id, s.source_type, s.source_key
                    FROM rescue_catalog rc
                    JOIN sources s ON s.novel_id = rc.item_id
                    WHERE rc.item_type = 'novel'
                    UNION ALL
                    SELECT rc.item_type, rc.item_id, s.source_type, s.source_key
                    FROM rescue_catalog rc
                    JOIN novels n ON n.series_id = rc.item_id
                    JOIN sources s ON s.novel_id = n.novel_id
                    WHERE rc.item_type = 'series'
                    UNION ALL
                    SELECT rc.item_type, rc.item_id, 'subscribed_series', ''
                    FROM rescue_catalog rc
                    JOIN series se ON se.series_id = rc.item_id
                    WHERE rc.item_type = 'series' AND se.is_subscribed = 1
                )
                SELECT fact.item_type, fact.item_id, fact.source_type, fact.source_key,
                       {fact_user_expr} AS source_user_id, u.name AS source_user_name
                FROM source_facts fact
                LEFT JOIN users u ON u.user_id = {fact_user_expr}
                ORDER BY fact.item_type, fact.item_id, fact.source_type, fact.source_key
                """
            ).fetchall()
            return rows

        normalized_type = self._validate_rescue_item_type(item_type)
        normalized_id = int(item_id) if item_id is not None else None
        if normalized_type == "novel":
            rows = self.conn.execute(
                f"""
                SELECT 'novel' AS item_type, ? AS item_id, s.source_type, s.source_key,
                       {user_expr} AS source_user_id, u.name AS source_user_name
                FROM sources s
                LEFT JOIN users u ON u.user_id = {user_expr}
                WHERE s.novel_id = ?
                ORDER BY s.source_type, s.source_key
                """,
                (normalized_id, normalized_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"""
                SELECT 'series' AS item_type, ? AS item_id, s.source_type, s.source_key,
                       {user_expr} AS source_user_id, u.name AS source_user_name
                FROM novels n
                JOIN sources s ON s.novel_id = n.novel_id
                LEFT JOIN users u ON u.user_id = {user_expr}
                WHERE n.series_id = ?
                UNION ALL
                SELECT 'series', ?, 'subscribed_series', '', NULL, NULL
                FROM series se
                WHERE se.series_id = ? AND se.is_subscribed = 1
                ORDER BY source_type, source_key
                """,
                (normalized_id, normalized_id, normalized_id, normalized_id),
            ).fetchall()
        return rows

    def _catalog_sources(self, item_type: str, item_id: int) -> list[dict[str, Any]]:
        normalized_type = self._validate_rescue_item_type(item_type)
        rows = self._catalog_source_fact_rows(normalized_type, int(item_id))
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            source = self._normalize_source(row)
            key = (str(source["source_kind"]), str(source["source_key"]))
            if key in seen:
                continue
            seen.add(key)
            result.append(source)
        result.sort(key=self._source_sort_key)
        return result

    def _catalog_all_sources(self) -> dict[tuple[str, int], list[dict[str, Any]]]:
        grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
        seen: set[tuple[str, int, str, str]] = set()
        for row in self._catalog_source_fact_rows():
            item_type = str(row["item_type"])
            item_id = int(row["item_id"])
            source = self._normalize_source(row)
            key = (item_type, item_id, str(source["source_kind"]), str(source["source_key"]))
            if key in seen:
                continue
            seen.add(key)
            grouped.setdefault((item_type, item_id), []).append(source)
        for sources in grouped.values():
            sources.sort(key=self._source_sort_key)
        return grouped

    @classmethod
    def _catalog_row_values(
        cls,
        row: dict[str, Any],
        refreshed_at: str,
    ) -> tuple[Any, ...]:
        return tuple(
            refreshed_at if column == "refreshed_at" else row[column]
            for column in cls._CATALOG_COLUMNS
        )

    def _insert_catalog_rows(
        self,
        rows: list[dict[str, Any]],
        refreshed_at: str,
    ) -> None:
        if not rows:
            return
        columns = ", ".join(self._CATALOG_COLUMNS)
        placeholders = ", ".join("?" for _ in self._CATALOG_COLUMNS)
        self.conn.executemany(
            f"INSERT INTO rescue_catalog ({columns}) VALUES ({placeholders})",
            [self._catalog_row_values(row, refreshed_at) for row in rows],
        )

    def _insert_catalog_sources_for_rows(
        self,
        rows: list[dict[str, Any]],
    ) -> int:
        values = [
            (
                row["item_type"],
                row["item_id"],
                source["source_kind"],
                source["source_type"],
                source["source_key"],
                source["source_user_id"],
                source["source_user_name"],
            )
            for row in rows
            for source in self._catalog_sources(str(row["item_type"]), int(row["item_id"]))
        ]
        if values:
            self.conn.executemany(
                """
                INSERT INTO rescue_catalog_sources (
                    item_type, item_id, source_kind, source_type, source_key,
                    source_user_id, source_user_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        return len(values)

    def _delete_catalog_scope(
        self,
        series_ids: set[int],
        novel_ids: set[int],
    ) -> None:
        keys = {("series", int(value)) for value in series_ids}
        keys.update(("novel", int(value)) for value in novel_ids)
        if series_ids:
            normalized_ids = sorted({int(value) for value in series_ids})
            placeholders = ", ".join("?" for _ in normalized_ids)
            rows = self.conn.execute(
                f"""
                SELECT item_type, item_id
                FROM rescue_catalog
                WHERE (item_type = 'series' AND item_id IN ({placeholders}))
                   OR (item_type = 'novel' AND series_id IN ({placeholders}))
                """,
                (*normalized_ids, *normalized_ids),
            ).fetchall()
            keys.update((str(row["item_type"]), int(row["item_id"])) for row in rows)
        if not keys:
            return
        values = sorted(keys)
        self.conn.executemany(
            "DELETE FROM rescue_catalog_sources WHERE item_type = ? AND item_id = ?",
            values,
        )
        self.conn.executemany(
            "DELETE FROM rescue_catalog WHERE item_type = ? AND item_id = ?",
            values,
        )

    def _catalog_novel_series_ids(self, novel_ids: set[int]) -> set[int]:
        """Return current and last-known parents for the given novels."""
        if not novel_ids:
            return set()
        placeholders = ", ".join("?" for _ in novel_ids)
        params = tuple(sorted(novel_ids))
        rows = self.conn.execute(
            f"""
            SELECT series_id
            FROM novels
            WHERE novel_id IN ({placeholders}) AND series_id IS NOT NULL
            UNION
            SELECT series_id
            FROM rescue_catalog_memberships
            WHERE novel_id IN ({placeholders})
            UNION
            SELECT series_id
            FROM rescue_catalog
            WHERE item_type = 'novel'
              AND item_id IN ({placeholders})
              AND series_id IS NOT NULL
            """,
            (*params, *params, *params),
        ).fetchall()
        return {
            int(row["series_id"])
            for row in rows
            if row["series_id"] is not None
        }

    def _rescue_novel_series_ids(self, novel_id: int) -> set[int]:
        return self._catalog_novel_series_ids({int(novel_id)})

    def _catalog_series_novel_ids(self, series_ids: set[int]) -> set[int]:
        """Return current and last-known members for the given series."""
        if not series_ids:
            return set()
        placeholders = ", ".join("?" for _ in series_ids)
        params = tuple(sorted(series_ids))
        rows = self.conn.execute(
            f"""
            SELECT novel_id
            FROM novels
            WHERE series_id IN ({placeholders})
            UNION
            SELECT novel_id
            FROM rescue_catalog_memberships
            WHERE series_id IN ({placeholders})
            UNION
            SELECT item_id AS novel_id
            FROM rescue_catalog
            WHERE item_type = 'novel'
              AND series_id IN ({placeholders})
            """,
            (*params, *params, *params),
        ).fetchall()
        return {int(row["novel_id"]) for row in rows}

    def _expand_catalog_scope(
        self,
        series_ids: set[int],
        novel_ids: set[int],
    ) -> None:
        """Expand current/snapshot relationships until the affected scope is stable."""
        while True:
            previous_size = (len(series_ids), len(novel_ids))
            novel_ids.update(self._catalog_series_novel_ids(series_ids))
            series_ids.update(self._catalog_novel_series_ids(novel_ids))
            if previous_size == (len(series_ids), len(novel_ids)):
                return

    def _refresh_catalog_memberships(
        self,
        series_ids: set[int],
        novel_ids: set[int],
    ) -> None:
        """Replace persisted links for the incrementally affected scope."""
        if not series_ids and not novel_ids:
            return
        if series_ids:
            self.conn.executemany(
                "DELETE FROM rescue_catalog_memberships WHERE series_id = ?",
                [(value,) for value in sorted(series_ids)],
            )
        if novel_ids:
            self.conn.executemany(
                "DELETE FROM rescue_catalog_memberships WHERE novel_id = ?",
                [(value,) for value in sorted(novel_ids)],
            )
            placeholders = ", ".join("?" for _ in novel_ids)
            self.conn.execute(
                f"""
                INSERT INTO rescue_catalog_memberships (novel_id, series_id)
                SELECT n.novel_id, n.series_id
                FROM novels n
                JOIN series se ON se.series_id = n.series_id
                WHERE n.novel_id IN ({placeholders})
                """,
                tuple(sorted(novel_ids)),
            )

    def _replace_catalog_memberships(self) -> None:
        self.conn.execute("DELETE FROM rescue_catalog_memberships")
        self.conn.execute(
            """
            INSERT INTO rescue_catalog_memberships (novel_id, series_id)
            SELECT n.novel_id, n.series_id
            FROM novels n
            JOIN series se ON se.series_id = n.series_id
            """
        )

    def _update_catalog_meta(self, refreshed_at: str, duration_ms: int) -> None:
        item_count = int(self.conn.execute("SELECT COUNT(*) FROM rescue_catalog").fetchone()[0])
        self.conn.execute(
            """
            INSERT INTO rescue_catalog_meta (
                singleton_id, refreshed_at, item_count, duration_ms
            ) VALUES (1, ?, ?, ?)
            ON CONFLICT(singleton_id) DO UPDATE SET
                refreshed_at = excluded.refreshed_at,
                item_count = excluded.item_count,
                duration_ms = excluded.duration_ms
            """,
            (refreshed_at, item_count, duration_ms),
        )

    def rebuild_rescue_catalog(self) -> dict[str, int]:
        """Build the complete rescue catalog and source snapshot atomically."""
        started = time.perf_counter()
        refreshed_at = self._catalog_timestamp()
        with self.transaction():
            series_rows = self._catalog_series_rows()
            rescue_series_ids = {int(row["series_id"]) for row in series_rows}
            novel_rows = self._catalog_novel_rows(rescue_series_ids)

            # Sources have a foreign key to the catalog; clear both explicitly so
            # this remains correct even for databases created before the FK pragma.
            self.conn.execute("DELETE FROM rescue_catalog_sources")
            self.conn.execute("DELETE FROM rescue_catalog")
            self._insert_catalog_rows(series_rows + novel_rows, refreshed_at)

            grouped_sources = self._catalog_all_sources()
            source_values = [
                (
                    item_type,
                    item_id,
                    source["source_kind"],
                    source["source_type"],
                    source["source_key"],
                    source["source_user_id"],
                    source["source_user_name"],
                )
                for (item_type, item_id), sources in grouped_sources.items()
                for source in sources
            ]
            if source_values:
                self.conn.executemany(
                    """
                    INSERT INTO rescue_catalog_sources (
                        item_type, item_id, source_kind, source_type, source_key,
                        source_user_id, source_user_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    source_values,
                )

            self._replace_catalog_memberships()

            duration_ms = max(0, int((time.perf_counter() - started) * 1000))
            self._update_catalog_meta(refreshed_at, duration_ms)

        return {
            "items": len(series_rows) + len(novel_rows),
            "sources": len(source_values),
            "duration_ms": duration_ms,
        }

    def refresh_rescue_item(self, item_type: str, item_id: int) -> dict[str, int]:
        """Atomically rebuild one item, its parent series, and affected chapters."""
        normalized_type = self._validate_rescue_item_type(item_type)
        normalized_id = int(item_id)
        started = time.perf_counter()
        refreshed_at = self._catalog_timestamp()

        with self.transaction():
            series_ids: set[int] = set()
            novel_ids: set[int] = set()
            target_exists = self._rescue_item_exists(normalized_type, normalized_id)

            if normalized_type == "novel":
                novel_ids.add(normalized_id)
                series_ids.update(self._rescue_novel_series_ids(normalized_id))
            else:
                series_ids.add(normalized_id)

            self._expand_catalog_scope(
                series_ids,
                novel_ids,
            )

            self._delete_catalog_scope(series_ids, novel_ids)

            existing_series_ids: set[int] = set()
            if series_ids:
                placeholders = ", ".join("?" for _ in series_ids)
                existing_rows = self.conn.execute(
                    f"SELECT series_id FROM series WHERE series_id IN ({placeholders})",
                    tuple(sorted(series_ids)),
                ).fetchall()
                existing_series_ids = {
                    int(row["series_id"])
                    for row in existing_rows
                }

            rows: list[dict[str, Any]] = []
            should_rebuild = (
                target_exists
                or normalized_type == "novel"
                or bool(existing_series_ids)
            )
            if should_rebuild:
                series_rows = self._catalog_series_rows(existing_series_ids)
                rescue_series_ids = {
                    int(row["series_id"])
                    for row in series_rows
                }
                rebuild_novel_ids = set(novel_ids)
                if rebuild_novel_ids and series_ids:
                    placeholders = ", ".join("?" for _ in rebuild_novel_ids)
                    current_rows = self.conn.execute(
                        f"""
                        SELECT novel_id, series_id
                        FROM novels
                        WHERE novel_id IN ({placeholders})
                        """,
                        tuple(sorted(rebuild_novel_ids)),
                    ).fetchall()
                    rebuild_novel_ids = {
                        int(row["novel_id"])
                        for row in current_rows
                        if row["series_id"] is None
                        or int(row["series_id"]) in existing_series_ids
                    }
                novel_rows = self._catalog_novel_rows(
                    rescue_series_ids,
                    rebuild_novel_ids,
                )
                rows = series_rows + novel_rows
                self._insert_catalog_rows(rows, refreshed_at)

            source_count = self._insert_catalog_sources_for_rows(rows)
            self._refresh_catalog_memberships(series_ids, novel_ids)
            duration_ms = max(0, int((time.perf_counter() - started) * 1000))

        return {
            "items": len(rows),
            "sources": source_count,
            "duration_ms": duration_ms,
        }

    def get_rescue_catalog_item(
        self,
        item_type: str,
        item_id: int,
    ) -> dict[str, Any] | None:
        normalized_type = self._validate_rescue_item_type(item_type)
        row = self.conn.execute(
            """
            SELECT *
            FROM rescue_catalog
            WHERE item_type = ? AND item_id = ?
            """,
            (normalized_type, int(item_id)),
        ).fetchone()
        return dict(row) if row else None

    def get_rescue_catalog_meta(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT refreshed_at, item_count, duration_ms
            FROM rescue_catalog_meta
            WHERE singleton_id = 1
            """
        ).fetchone()
        return dict(row) if row else None

    def list_rescue_catalog_sources(
        self,
        item_type: str,
        item_id: int,
    ) -> list[dict[str, Any]]:
        normalized_type = self._validate_rescue_item_type(item_type)
        rows = self.conn.execute(
            """
            SELECT source_kind, source_type, source_key,
                   source_user_id, source_user_name
            FROM rescue_catalog_sources
            WHERE item_type = ? AND item_id = ?
            """,
            (normalized_type, int(item_id)),
        ).fetchall()
        result = [dict(row) for row in rows]
        result.sort(key=self._source_sort_key)
        return result

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
        content_kind: str = "all",
        source_kind: str = "all",
    ) -> dict[str, Any]:
        with self.read_transaction():
            return self._list_rescues_snapshot(
                page=page,
                page_size=page_size,
                state=state,
                item_type=item_type,
                search=search,
                sort=sort,
                content_kind=content_kind,
                source_kind=source_kind,
            )

    def _list_rescues_snapshot(
        self,
        page: int = 1,
        page_size: int = 12,
        state: str = "all",
        item_type: str = "all",
        search: str = "",
        sort: str = "checked_desc",
        content_kind: str = "all",
        source_kind: str = "all",
    ) -> dict[str, Any]:
        normalized_state = str(state or "all").strip().lower()
        normalized_type = str(item_type or "all").strip().lower()
        normalized_sort = str(sort or "checked_desc").strip().lower()
        normalized_content_kind = str(content_kind or "all").strip().lower()
        normalized_source_kind = str(source_kind or "all").strip().lower()
        if normalized_state not in {"all", "success", "partial"}:
            raise ValueError("state 参数无效")
        if normalized_type not in {"all", "novel", "series"}:
            raise ValueError("item_type 参数无效")
        if normalized_sort not in {"checked_desc", "updated_desc"}:
            raise ValueError("sort 参数无效")
        if normalized_content_kind not in {
            "all", "series", "series_chapter", "standalone"
        }:
            raise ValueError("content_kind 参数无效")
        if normalized_source_kind not in {
            "all", "bookmark", "subscribed_series", "following_user", "user_backup"
        }:
            raise ValueError("source_kind 参数无效")

        meta = self.get_rescue_catalog_meta()
        if meta is None:
            raise CatalogNotReadyError("救援目录尚未生成")

        where_clauses: list[str] = []
        params: list[Any] = []
        if normalized_state != "all":
            where_clauses.append("rc.rescue_state = ?")
            params.append(normalized_state)
        if normalized_content_kind != "all":
            where_clauses.append("rc.content_kind = ?")
            params.append(normalized_content_kind)
        elif normalized_type != "all":
            where_clauses.append("rc.item_type = ?")
            params.append(normalized_type)
        query = str(search or "").strip()
        if query:
            self.conn.create_function(
                "CASEFOLD",
                1,
                _sqlite_casefold,
                deterministic=True,
            )
            folded_query = query.casefold()
            where_clauses.append(
                "(INSTR(CASEFOLD(rc.title), ?) > 0 "
                "OR INSTR(CASEFOLD(rc.author_name), ?) > 0)"
            )
            params.extend((folded_query, folded_query))
        if normalized_source_kind != "all":
            where_clauses.append(
                "EXISTS ("
                "SELECT 1 FROM rescue_catalog_sources rcf "
                "WHERE rcf.item_type = rc.item_type "
                "AND rcf.item_id = rc.item_id "
                "AND rcf.source_kind = ?"
                ")"
            )
            params.append(normalized_source_kind)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        order_sql = {
            "checked_desc": (
                "rc.last_checked_at DESC, rc.updated_at DESC, "
                "rc.item_id DESC, rc.item_type DESC"
            ),
            "updated_desc": "rc.updated_at DESC, rc.item_id DESC, rc.item_type DESC",
        }[normalized_sort]
        total = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM rescue_catalog rc {where_sql}",
                tuple(params),
            ).fetchone()[0]
        )

        normalized_page = max(int(page), 1)
        normalized_size = max(int(page_size), 1)
        total_pages = max((total + normalized_size - 1) // normalized_size, 1)
        normalized_page = min(normalized_page, total_pages)
        offset = (normalized_page - 1) * normalized_size
        rows = self.conn.execute(
            f"""
            SELECT
                rc.item_type, rc.item_id, rc.content_kind, rc.series_id,
                rc.title, rc.user_id, rc.author_name, rc.cover_url,
                rc.rescue_state, rc.remote_status, rc.eligibility_reason,
                rc.expected_count, rc.local_count, rc.complete_count,
                rc.last_checked_at, rc.updated_at
            FROM rescue_catalog rc
            {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            (*params, normalized_size, offset),
        ).fetchall()

        items = [dict(row) for row in rows]
        sources_by_item: dict[tuple[str, int], list[dict[str, Any]]] = {}
        if items:
            page_predicates = " OR ".join(
                "(item_type = ? AND item_id = ?)" for _item in items
            )
            source_params = tuple(
                value
                for item in items
                for value in (str(item["item_type"]), int(item["item_id"]))
            )
            source_rows = self.conn.execute(
                f"""
                SELECT item_type, item_id, source_kind,
                       source_user_id, source_user_name
                FROM rescue_catalog_sources
                WHERE {page_predicates}
                ORDER BY
                    CASE source_kind
                        WHEN 'bookmark' THEN 0
                        WHEN 'subscribed_series' THEN 1
                        WHEN 'following_user' THEN 2
                        WHEN 'user_backup' THEN 3
                        ELSE 4
                    END,
                    source_user_name COLLATE NOCASE,
                    source_user_id,
                    source_key,
                    source_type
                """,
                source_params,
            ).fetchall()
            for row in source_rows:
                key = (str(row["item_type"]), int(row["item_id"]))
                source_name = str(row["source_user_name"] or "") or None
                source_user_id = (
                    int(row["source_user_id"])
                    if row["source_user_id"] is not None
                    else None
                )
                source_kind_value = str(row["source_kind"])
                if source_kind_value == "bookmark":
                    label = "我的收藏"
                elif source_kind_value == "subscribed_series":
                    label = "我的追更"
                elif source_kind_value == "following_user":
                    label = f"关注用户：{source_name or source_user_id or '未知用户'}"
                elif source_kind_value == "user_backup":
                    label = f"用户备份：{source_name or source_user_id or '未知用户'}"
                else:
                    label = "其他来源"
                sources_by_item.setdefault(key, []).append(
                    {
                        "kind": source_kind_value,
                        "label": label,
                        "user_id": source_user_id,
                        "user_name": source_name,
                    }
                )

        for item in items:
            item_type_value = str(item["item_type"])
            item_id_value = int(item["item_id"])
            item["content_kind_label"] = self._CONTENT_KIND_LABELS[
                str(item["content_kind"])
            ]
            item["sources"] = sources_by_item.get(
                (item_type_value, item_id_value),
                [],
            )
            if item_type_value == "novel":
                item["novel_id"] = item_id_value

        return {
            "items": items,
            "page": normalized_page,
            "page_size": normalized_size,
            "total": total,
            "total_pages": total_pages,
            "category": "rescue",
            "refreshed_at": meta["refreshed_at"],
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

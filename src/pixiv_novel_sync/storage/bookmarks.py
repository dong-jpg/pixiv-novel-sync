"""Bookmark and sync check operations."""
from __future__ import annotations

from typing import Any


class BookmarksMixin:
    """收藏和同步检查 mixin。

    提供收藏列表查询和同步检查表操作。
    """

    def list_bookmark_novels(self, page: int = 1, page_size: int = 10,
                            search: str = "", sort: str = "") -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(page_size, 1)
        where_clauses: list[str] = ["s.source_type LIKE 'bookmark_%'"]
        params_count: list[Any] = []
        if search:
            where_clauses.append("n.novel_id IN (SELECT novel_id FROM novel_fts WHERE novel_fts MATCH ?)")
            params_count.append(search)
        where_sql = f"WHERE {' AND '.join(where_clauses)}"
        total = int(
            self.conn.execute(
                f"SELECT COUNT(DISTINCT n.novel_id) FROM novels n LEFT JOIN users AS u ON u.user_id = n.user_id LEFT JOIN sources s ON s.novel_id = n.novel_id {where_sql}",
                params_count,
            ).fetchone()[0]
        )
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size

        order_sql = "n.last_seen_at DESC, n.novel_id DESC"
        if sort == "updated_desc":
            order_sql = "n.last_seen_at DESC"
        elif sort == "bookmarks_desc":
            order_sql = "n.total_bookmarks DESC"
        elif sort == "views_desc":
            order_sql = "n.total_views DESC"

        params_query: list[Any] = []
        if search:
            params_query.append(search)
        params_query.extend([page_size, offset])

        rows = self.conn.execute(
            f"""
            SELECT DISTINCT
                n.novel_id, n.title, n.user_id, n.series_id,
                u.name AS author_name, n.cover_url, n.restrict_value,
                n.total_bookmarks, n.total_views, n.last_seen_at, n.first_seen_at,
                CASE WHEN n.series_id IS NULL THEN 'single' ELSE 'series' END AS novel_kind,
                rp.status AS reading_status,
                rp.progress AS reading_progress
            FROM novels AS n
            LEFT JOIN users AS u ON u.user_id = n.user_id
            LEFT JOIN sources AS s ON s.novel_id = n.novel_id
            {where_sql}
            LEFT JOIN reading_progress AS rp ON rp.novel_id = n.novel_id
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            params_query,
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "page": page, "page_size": page_size,
            "total": total, "total_pages": total_pages, "category": "bookmark",
        }

    def get_all_novel_ids(self) -> list[int]:
        rows = self.conn.execute("SELECT novel_id FROM novels ORDER BY novel_id").fetchall()
        return [row[0] for row in rows]

    def clear_sync_check_list(self, scope: str = "_") -> None:
        """清空同步检查列表"""
        with self._lock:
            self.conn.execute("DELETE FROM sync_check_list WHERE scope = ?", (scope,))
            self._commit_if_needed()

    def upsert_sync_check_item(self, novel_id: int, exists_local: bool, scope: str = "_") -> None:
        """更新同步检查项"""
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO sync_check_list (scope, novel_id, exists_local, checked_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(scope, novel_id) DO UPDATE SET
                    exists_local = excluded.exists_local,
                    checked_at = CURRENT_TIMESTAMP
                """,
                (scope, novel_id, 1 if exists_local else 0),
            )
            self._commit_if_needed()

    def upsert_sync_check_items(self, items: list[tuple[int, bool]], scope: str = "_") -> None:
        if not items:
            return
        with self.transaction():
            self.conn.executemany(
                """
                INSERT INTO sync_check_list (scope, novel_id, exists_local, checked_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(scope, novel_id) DO UPDATE SET
                    exists_local = excluded.exists_local,
                    checked_at = CURRENT_TIMESTAMP
                """,
                [(scope, novel_id, 1 if exists_local else 0) for novel_id, exists_local in items],
            )

    def get_sync_check_list(self, scope: str = "_") -> dict[int, bool]:
        """获取同步检查列表，返回 {novel_id: exists_local}"""
        rows = self.conn.execute(
            "SELECT novel_id, exists_local FROM sync_check_list WHERE scope = ?",
            (scope,),
        ).fetchall()
        return {row[0]: bool(row[1]) for row in rows}

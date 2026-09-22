"""Bookmark listing operations."""
from __future__ import annotations

from typing import Any

from .utils import escape_fts_query


class BookmarksMixin:
    """收藏列表查询 mixin。"""

    def list_bookmark_novels(self, page: int = 1, page_size: int = 10,
                            search: str = "", sort: str = "") -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(page_size, 1)
        # 转义用户搜索词为 FTS5 短语，避免特殊字符导致 OperationalError（HTTP 500）
        search = escape_fts_query(search)
        where_clauses: list[str] = ["s.source_type LIKE 'bookmark_%'"]
        params_count: list[Any] = []
        if search:
            # 走 rowid（== novel_id）：novel_id 是 UNINDEXED 列，SELECT novel_id 只能
            # 全表扫描 FTS 索引（生产实测 0.22 秒，走 rowid 是 0.002 秒）。
            where_clauses.append("n.novel_id IN (SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?)")
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
            order_sql = "n.last_seen_at DESC, n.novel_id DESC"
        elif sort == "bookmarks_desc":
            order_sql = "n.total_bookmarks DESC, n.novel_id DESC"
        elif sort == "views_desc":
            order_sql = "n.total_views DESC, n.novel_id DESC"

        params_query: list[Any] = []
        if search:
            params_query.append(search)
        params_query.extend([page_size, offset])

        rows = self.conn.execute(
            f"""
            SELECT DISTINCT
                n.novel_id, n.title, n.user_id, n.series_id,
                u.name AS author_name, n.cover_url, n.restrict_value,
                n.x_restrict, u.raw_json AS author_raw_json,
                n.total_bookmarks, n.total_views, n.last_seen_at, n.first_seen_at,
                CASE WHEN n.series_id IS NULL THEN 'single' ELSE 'series' END AS novel_kind,
                rp.status AS reading_status,
                rp.progress AS reading_progress
            FROM novels AS n
            LEFT JOIN users AS u ON u.user_id = n.user_id
            LEFT JOIN reading_progress AS rp ON rp.novel_id = n.novel_id
            LEFT JOIN sources AS s ON s.novel_id = n.novel_id
            {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            params_query,
        ).fetchall()
        items = [dict(row) for row in rows]
        # 提取作者头像（与 list_following_series 同款），前端字段名是 author_avatar_url
        for item in items:
            raw_json = item.pop("author_raw_json", None)
            if raw_json:
                from ..storage_db import Database
                item["author_avatar_url"] = Database._extract_user_avatar(Database._load_raw_json(raw_json))
            else:
                item["author_avatar_url"] = None
        return {
            "items": items,
            "page": page, "page_size": page_size,
            "total": total, "total_pages": total_pages, "category": "bookmark",
        }

    def get_all_novel_ids(self) -> list[int]:
        rows = self.conn.execute("SELECT novel_id FROM novels ORDER BY novel_id").fetchall()
        return [row[0] for row in rows]

from __future__ import annotations

import sqlite3
from typing import Any

from .utils import escape_fts_query


class SeriesMixin:
    """系列数据管理 Mixin"""

    # 从基类访问的属性
    conn: sqlite3.Connection
    _lock: Any
    _commit_if_needed: Any

    def upsert_series(self, series_id: int, title: str, description: str, user_id: int, cover_url: str | None) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO series (series_id, title, description, user_id, cover_url, total_novels, last_seen_at)
                VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(series_id) DO UPDATE SET
                    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE series.title END,
                    description = CASE WHEN excluded.description != '' THEN excluded.description ELSE series.description END,
                    cover_url = CASE WHEN excluded.cover_url IS NOT NULL AND excluded.cover_url != '' THEN excluded.cover_url ELSE series.cover_url END,
                    user_id = CASE WHEN excluded.user_id != 0 THEN excluded.user_id ELSE series.user_id END,
                    total_novels = (SELECT COUNT(*) FROM novels WHERE series_id = ?),
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (series_id, title, description, user_id, cover_url, series_id),
            )
            self._commit_if_needed()

    def upsert_series_status(self, series_id: int, status: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE series SET status = ?, last_checked_at = CURRENT_TIMESTAMP WHERE series_id = ?",
                (status, series_id),
            )
            self._commit_if_needed()

    def upsert_subscribed_series(self, series_id: int, title: str, description: str, user_id: int, cover_url: str | None, total_novels: int = 0) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO series (series_id, title, description, user_id, cover_url, total_novels, is_subscribed, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(series_id) DO UPDATE SET
                    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE series.title END,
                    description = CASE WHEN excluded.description != '' THEN excluded.description ELSE series.description END,
                    user_id = CASE WHEN excluded.user_id != 0 THEN excluded.user_id ELSE series.user_id END,
                    cover_url = COALESCE(excluded.cover_url, series.cover_url),
                    total_novels = CASE WHEN excluded.total_novels > 0 THEN excluded.total_novels ELSE series.total_novels END,
                    is_subscribed = 1,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (series_id, title, description, user_id, cover_url, total_novels),
            )
            self._commit_if_needed()

    def repair_blank_series_titles(self) -> int:
        """用已归档小说的系列信息修复空标题，避免追更列表显示未命名系列。"""
        with self._lock:
            cursor = self.conn.execute(
                """
                UPDATE series
                SET title = COALESCE(
                        NULLIF((
                            SELECT json_extract(n.raw_json, '$.series.title')
                            FROM novels n
                            WHERE n.series_id = series.series_id
                              AND json_extract(n.raw_json, '$.series.title') IS NOT NULL
                              AND json_extract(n.raw_json, '$.series.title') != ''
                            ORDER BY n.create_date ASC
                            LIMIT 1
                        ), ''),
                        NULLIF((
                            SELECT MIN(n.title)
                            FROM novels n
                            WHERE n.series_id = series.series_id
                              AND n.title IS NOT NULL
                              AND n.title != ''
                        ), '')
                    ),
                    total_novels = CASE
                        WHEN total_novels > 0 THEN total_novels
                        ELSE (SELECT COUNT(*) FROM novels n WHERE n.series_id = series.series_id)
                    END
                WHERE (title IS NULL OR title = '')
                  AND EXISTS (SELECT 1 FROM novels n WHERE n.series_id = series.series_id)
                """
            )
            self._commit_if_needed()
            return cursor.rowcount if cursor.rowcount is not None else 0

    def clear_subscribed_series(self) -> None:
        """清除所有订阅标记"""
        with self._lock:
            self.conn.execute("UPDATE series SET is_subscribed = 0")
            self._commit_if_needed()

    def get_series_detail(self, series_id: int) -> dict[str, Any] | None:
        series_row = self.conn.execute(
            "SELECT * FROM series WHERE series_id = ?", (series_id,)
        ).fetchone()
        if series_row is None:
            novels = self.conn.execute(
                """
                SELECT n.*, u.name AS author_name FROM novels n
                LEFT JOIN users u ON u.user_id = n.user_id
                WHERE n.series_id = ?
                ORDER BY n.create_date ASC
                """,
                (series_id,),
            ).fetchall()
            if not novels:
                return None
            first = dict(novels[0])
            series_info = {
                "series_id": series_id,
                "title": first.get("title", f"系列 {series_id}"),
                "description": "",
                "user_id": first.get("user_id"),
                "author_name": first.get("author_name", "未知"),
                "cover_url": first.get("cover_url"),
                "total_novels": len(novels),
            }
        else:
            series_info = dict(series_row)
            # 回退空标题和空封面到第一本小说的数据
            novels = self.conn.execute(
                """
                SELECT n.*, u.name AS author_name FROM novels n
                LEFT JOIN users u ON u.user_id = n.user_id
                WHERE n.series_id = ?
                ORDER BY n.create_date ASC
                """,
                (series_id,),
            ).fetchall()
            if not series_info.get("title") and novels:
                series_info["title"] = dict(novels[0]).get("title", "")
            if not series_info.get("cover_url") and novels:
                for n in novels:
                    cu = dict(n).get("cover_url")
                    if cu:
                        series_info["cover_url"] = cu
                        break
        series_info["novels"] = [dict(row) for row in novels]
        # 用本地实际记录数覆盖远端 total_novels
        series_info["total_novels"] = len(novels)
        # 计算系列总字数
        total_text_length = sum(row.get("text_length", 0) or 0 for row in series_info["novels"])
        series_info["total_text_length"] = total_text_length
        return series_info

    def list_following_series(self, page: int = 1, page_size: int = 10,
                             search: str = "", sort: str = "") -> dict[str, Any]:
        """获取订阅的系列列表"""
        page = max(page, 1)
        page_size = max(page_size, 1)

        where_clauses: list[str] = ["se.is_subscribed = 1"]
        params_count: list[Any] = []
        if search:
            where_clauses.append(
                """(se.title LIKE ? OR (
                   (se.title IS NULL OR se.title = '') AND EXISTS (
                     SELECT 1 FROM novels n0 WHERE n0.series_id = se.series_id AND n0.novel_id IN (SELECT novel_id FROM novel_fts WHERE novel_fts MATCH ?)
                   )
                   ) OR u.name LIKE ?)"""
            )
            search_pattern = f"%{search}%"
            params_count.extend([search_pattern, escape_fts_query(search), search_pattern])

        where_sql = " AND ".join(where_clauses)
        total = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM series se LEFT JOIN users AS u ON u.user_id = se.user_id WHERE {where_sql}",
                params_count,
            ).fetchone()[0]
        )
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size

        order_sql = "se.last_seen_at DESC"
        if sort == "updated_desc":
            order_sql = "se.last_seen_at DESC"
        elif sort == "bookmarks_desc":
            # Phase 5.5: 子查询改JOIN预聚合消除N+1
            order_sql = "COALESCE(agg.total_bookmarks, 0) DESC"
        elif sort == "views_desc":
            # Phase 5.5: 子查询改JOIN预聚合消除N+1
            order_sql = "COALESCE(agg.total_views, 0) DESC"

        params_query: list[Any] = []
        if search:
            search_pattern = f"%{search}%"
            params_query.extend([search_pattern, escape_fts_query(search), search_pattern])
        params_query.extend([page_size, offset])

        # Phase 5.5: 预聚合避免ORDER BY子查询
        aggregation_join = ""
        if sort in ("bookmarks_desc", "views_desc"):
            aggregation_join = """
            LEFT JOIN (
                SELECT series_id,
                       SUM(total_bookmarks) AS total_bookmarks,
                       SUM(total_views) AS total_views
                FROM novels
                GROUP BY series_id
            ) agg ON agg.series_id = se.series_id
            """

        rows = self.conn.execute(
            f"""
            SELECT
                se.series_id,
                CASE WHEN se.title IS NOT NULL AND se.title != '' THEN se.title
                     ELSE (SELECT MIN(n.title) FROM novels n WHERE n.series_id = se.series_id)
                END AS series_title,
                se.description AS series_description,
                se.user_id,
                u.name AS author_name,
                CASE WHEN se.cover_url IS NOT NULL AND se.cover_url != '' THEN se.cover_url
                     ELSE (SELECT n2.cover_url FROM novels n2 WHERE n2.series_id = se.series_id AND n2.cover_url IS NOT NULL AND n2.cover_url != '' LIMIT 1)
                END AS cover_url,
                u.raw_json AS author_raw_json,
                se.total_novels AS chapter_count,
                se.last_seen_at AS last_updated,
                COALESCE((SELECT SUM(n.text_length) FROM novels n WHERE n.series_id = se.series_id), 0) AS total_text_length
            FROM series se
            LEFT JOIN users AS u ON u.user_id = se.user_id
            {aggregation_join}
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            params_query,
        ).fetchall()
        items = [dict(row) for row in rows]
        # 6.7: 提取作者头像
        for item in items:
            raw_json = item.pop("author_raw_json", None)
            if raw_json:
                from ..storage_db import Database
                item["author_avatar"] = Database._extract_user_avatar(Database._load_raw_json(raw_json))
            else:
                item["author_avatar"] = None
        return {
            "items": items,
            "page": page, "page_size": page_size,
            "total": total, "total_pages": total_pages, "category": "following",
        }

    def delete_series(self, series_id: int) -> list[int]:
        """删除系列（不删除小说，只解除关联）"""
        from .connection import DatabaseConnection
        # 使用基类的 transaction 方法
        with DatabaseConnection.transaction(self):
            chapter_rows = self.conn.execute(
                "SELECT novel_id FROM novels WHERE series_id = ?",
                (series_id,),
            ).fetchall()
            chapter_ids = [int(row["novel_id"]) for row in chapter_rows]
            catalog_rows = self.conn.execute(
                """
                SELECT item_type, item_id
                FROM rescue_catalog
                WHERE (item_type = 'series' AND item_id = ?)
                   OR (item_type = 'novel' AND series_id = ?)
                """,
                (series_id, series_id),
            ).fetchall()
            catalog_keys = {("series", int(series_id))}
            catalog_keys.update(
                ("novel", int(row["novel_id"]))
                for row in chapter_rows
            )
            catalog_keys.update(
                (str(row["item_type"]), int(row["item_id"]))
                for row in catalog_rows
            )
            self.conn.executemany(
                "DELETE FROM rescue_catalog_sources WHERE item_type = ? AND item_id = ?",
                sorted(catalog_keys),
            )
            self.conn.executemany(
                "DELETE FROM rescue_catalog WHERE item_type = ? AND item_id = ?",
                sorted(catalog_keys),
            )
            self.conn.execute("UPDATE novels SET series_id = NULL WHERE series_id = ?", (series_id,))
            self.conn.execute(
                "DELETE FROM rescue_catalog_memberships WHERE series_id = ?",
                (series_id,),
            )
            self.conn.execute("DELETE FROM recommendation_items WHERE item_type = 'series' AND series_id = ?", (series_id,))
            self.conn.execute("DELETE FROM recommendation_feedback WHERE series_id = ?", (series_id,))
            self.conn.execute("DELETE FROM pending_deletions WHERE item_type = 'series' AND item_id = ?", (series_id,))
            self.conn.execute(
                "DELETE FROM rescue_overrides WHERE item_type = 'series' AND item_id = ?",
                (series_id,),
            )
            self.conn.execute("DELETE FROM series WHERE series_id = ?", (series_id,))
        return chapter_ids

    def get_all_series_ids(self) -> list[int]:
        rows = self.conn.execute("SELECT series_id FROM series ORDER BY series_id").fetchall()
        return [row[0] for row in rows]

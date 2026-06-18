"""User CRUD operations."""
from __future__ import annotations

import json
from typing import Any


class UsersMixin:
    """用户管理 mixin。

    提供用户的 CRUD 操作和关注列表查询。
    """

    def upsert_user(self, record) -> None:
        """插入或更新用户记录"""
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO users (user_id, name, account, raw_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  name = excluded.name,
                  account = CASE WHEN excluded.account IS NOT NULL AND excluded.account != '' THEN excluded.account ELSE users.account END,
                  raw_json = CASE WHEN excluded.raw_json != '{}' AND excluded.raw_json != '' THEN excluded.raw_json ELSE users.raw_json END,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (record.user_id, record.name, record.account, record.raw_json),
            )
            self._commit_if_needed()

    def upsert_user_status(self, user_id: int, status: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE users SET status = ?, last_checked_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (status, user_id),
            )
            self._commit_if_needed()

    def get_user_summary(self, user_id: int | None) -> dict[str, Any] | None:
        if user_id:
            row = self.conn.execute(
                "SELECT user_id, name, account, raw_json, updated_at FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is not None:
                raw = self._load_raw_json(row["raw_json"])
                return {
                    "user_id": row["user_id"],
                    "name": row["name"],
                    "account": row["account"],
                    "avatar_url": self._extract_user_avatar(raw),
                    "updated_at": row["updated_at"],
                    "is_fallback": False,
                }

        fallback = self.conn.execute(
            """
            SELECT user_id, name, account, raw_json, updated_at
            FROM users
            ORDER BY updated_at DESC, user_id DESC
            LIMIT 1
            """
        ).fetchone()
        if fallback is None:
            return None
        raw = self._load_raw_json(fallback["raw_json"])
        return {
            "user_id": user_id or fallback["user_id"],
            "resolved_user_id": fallback["user_id"],
            "name": fallback["name"],
            "account": fallback["account"],
            "avatar_url": self._extract_user_avatar(raw),
            "updated_at": fallback["updated_at"],
            "is_fallback": True,
        }

    def list_followed_users(self, page: int = 1, page_size: int = 10) -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(page_size, 1)
        total = int(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            """
            SELECT user_id, name, account, raw_json, updated_at
            FROM users
            ORDER BY updated_at DESC, user_id DESC
            LIMIT ? OFFSET ?
            """,
            (page_size, offset),
        ).fetchall()
        items = [
            {
                "user_id": row["user_id"],
                "name": row["name"],
                "account": row["account"],
                "avatar_url": self._extract_user_avatar(self._load_raw_json(row["raw_json"])),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }

    def list_users(self, page: int = 1, page_size: int = 10, status: str = "all") -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(page_size, 1)
        where_clause = ""
        params: list[Any] = []
        if status != "all":
            where_clause = "WHERE u.status = ?"
            params.append(status)
        total = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM users u {where_clause}", params
            ).fetchone()[0]
        )
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            f"""
            SELECT u.user_id, u.name, u.account, u.raw_json, u.status, u.last_checked_at, u.updated_at,
                   (SELECT COUNT(*) FROM novels n WHERE n.user_id = u.user_id) AS novel_count
            FROM users u
            {where_clause}
            ORDER BY CASE u.status WHEN 'no_novels' THEN 1 WHEN 'suspended' THEN 2 ELSE 0 END, u.updated_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, offset],
        ).fetchall()
        items = []
        for row in rows:
            raw = self._load_raw_json(row["raw_json"])
            items.append({
                "user_id": row["user_id"],
                "name": row["name"],
                "account": row["account"],
                "avatar_url": self._extract_user_avatar(raw),
                "status": row["status"] or "unknown",
                "last_checked_at": row["last_checked_at"],
                "updated_at": row["updated_at"],
                "novel_count": row["novel_count"],
            })
        return {
            "items": items,
            "page": page, "page_size": page_size,
            "total": total, "total_pages": total_pages,
        }

    def get_user_detail(self, user_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        raw = self._load_raw_json(row["raw_json"])
        novel_count = int(
            self.conn.execute("SELECT COUNT(*) FROM novels WHERE user_id = ?", (user_id,)).fetchone()[0]
        )
        return {
            "user_id": row["user_id"],
            "name": row["name"],
            "account": row["account"],
            "avatar_url": self._extract_user_avatar(raw),
            "status": row["status"] or "unknown",
            "last_checked_at": row["last_checked_at"],
            "updated_at": row["updated_at"],
            "novel_count": novel_count,
        }

    def list_user_novels(self, user_id: int, page: int = 1, page_size: int = 10, category: str = "all") -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(page_size, 1)

        where_extra = ""
        if category == "single":
            where_extra = " AND n.series_id IS NULL"
        elif category == "series":
            where_extra = " AND n.series_id IS NOT NULL"

        total = int(
            self.conn.execute(f"SELECT COUNT(*) FROM novels n WHERE n.user_id = ?{where_extra}", (user_id,)).fetchone()[0]
        )
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            f"""
            SELECT n.novel_id, n.title, n.series_id, n.cover_url, n.restrict_value,
                   n.total_bookmarks, n.total_views, n.last_seen_at, n.text_length,
                   CASE WHEN n.series_id IS NULL THEN 'single' ELSE 'series' END AS novel_kind,
                   se.title AS series_title
            FROM novels n
            LEFT JOIN series se ON se.series_id = n.series_id
            WHERE n.user_id = ?{where_extra}
            ORDER BY n.last_seen_at DESC
            LIMIT ? OFFSET ?
            """,
            [user_id, page_size, offset],
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "page": page, "page_size": page_size,
            "total": total, "total_pages": total_pages,
        }

    def list_user_series(self, user_id: int, page: int = 1, page_size: int = 10) -> dict[str, Any]:
        """获取某个用户的所有系列"""
        page = max(page, 1)
        page_size = max(page_size, 1)
        total = int(
            self.conn.execute(
                "SELECT COUNT(DISTINCT series_id) FROM novels WHERE user_id = ? AND series_id IS NOT NULL",
                (user_id,),
            ).fetchone()[0]
        )
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            """
            SELECT
                n.series_id,
                CASE WHEN se.title IS NOT NULL AND se.title != '' THEN se.title ELSE MIN(n.title) END AS series_title,
                se.description AS series_description,
                CASE WHEN se.cover_url IS NOT NULL AND se.cover_url != '' THEN se.cover_url
                     ELSE (SELECT n2.cover_url FROM novels n2 WHERE n2.series_id = n.series_id AND n2.cover_url IS NOT NULL AND n2.cover_url != '' LIMIT 1)
                END AS cover_url,
                COUNT(n.novel_id) AS chapter_count,
                COALESCE(SUM(n.text_length), 0) AS total_text_length,
                MAX(n.last_seen_at) AS last_updated,
                u.name AS author_name
            FROM novels n
            LEFT JOIN series se ON se.series_id = n.series_id
            LEFT JOIN users u ON u.user_id = n.user_id
            WHERE n.user_id = ? AND n.series_id IS NOT NULL
            GROUP BY n.series_id
            ORDER BY last_updated DESC
            LIMIT ? OFFSET ?
            """,
            [user_id, page_size, offset],
        ).fetchall()
        items = [dict(row) for row in rows]
        return {
            "items": items,
            "page": page, "page_size": page_size,
            "total": total, "total_pages": total_pages,
        }

    def delete_user(self, user_id: int) -> None:
        """删除用户及其所有小说（单一事务，批量删除）

        ✅ Bug #5 修复: 按正确顺序删除（从属表→主表），避免中间失败导致数据不一致
        """
        with self.transaction():
                # 1. 先获取要删除的小说 ID 列表
                novel_ids = [row[0] for row in self.conn.execute("SELECT novel_id FROM novels WHERE user_id = ?", (user_id,)).fetchall()]

                # 2. 删除小说相关的从属数据（按依赖顺序）
                # 2.1 删除 FTS 索引
                self.conn.execute("DELETE FROM novel_fts WHERE novel_id IN (SELECT novel_id FROM novels WHERE user_id = ?)", (user_id,))
                # 2.2 删除小说相关的其他从属表
                self.conn.execute("DELETE FROM sync_check_list WHERE novel_id IN (SELECT novel_id FROM novels WHERE user_id = ?)", (user_id,))
                self.conn.execute("DELETE FROM recommendation_items WHERE novel_id IN (SELECT novel_id FROM novels WHERE user_id = ?)", (user_id,))
                # 2.3 删除每个小说的反馈和待删除记录
                for novel_id in novel_ids:
                    self.conn.execute("DELETE FROM recommendation_feedback WHERE novel_id = ?", (novel_id,))
                    self.conn.execute("DELETE FROM pending_deletions WHERE item_type = 'novel' AND item_id = ?", (novel_id,))

                # 3. 删除小说主表
                self.conn.execute("DELETE FROM novels WHERE user_id = ?", (user_id,))

                # 4. 删除用户相关的其他数据
                self.conn.execute("DELETE FROM recommendation_feedback WHERE author_id = ?", (user_id,))
                self.conn.execute("DELETE FROM pending_deletions WHERE item_type = 'user' AND item_id = ?", (user_id,))

                # 5. 最后删除用户主表
                self.conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))

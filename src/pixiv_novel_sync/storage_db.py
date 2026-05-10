from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import AssetRecord, NovelRecord, NovelTextRecord, SourceRecord, UserRecord


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                account TEXT,
                raw_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',
                last_checked_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS novels (
                novel_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                series_id INTEGER,
                title TEXT NOT NULL,
                caption TEXT,
                visible INTEGER NOT NULL,
                restrict_value TEXT NOT NULL,
                x_restrict INTEGER NOT NULL,
                text_length INTEGER NOT NULL,
                total_bookmarks INTEGER NOT NULL,
                total_views INTEGER NOT NULL,
                cover_url TEXT,
                tags_json TEXT NOT NULL,
                create_date TEXT,
                raw_json TEXT NOT NULL,
                meta_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS novel_texts (
                novel_id INTEGER PRIMARY KEY,
                text_raw TEXT NOT NULL,
                text_markdown TEXT,
                text_hash TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS assets (
                asset_id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id INTEGER NOT NULL,
                asset_type TEXT NOT NULL,
                remote_url TEXT NOT NULL,
                local_path TEXT NOT NULL,
                file_hash TEXT,
                downloaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(novel_id, asset_type, remote_url)
            );

            CREATE TABLE IF NOT EXISTS sources (
                novel_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                source_key TEXT NOT NULL,
                discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (novel_id, source_type, source_key)
            );

            CREATE TABLE IF NOT EXISTS series (
                series_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                user_id INTEGER NOT NULL,
                cover_url TEXT,
                total_novels INTEGER DEFAULT 0,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS novel_fts USING fts5(
                novel_id UNINDEXED,
                title,
                caption,
                author_name,
                body
            );

            CREATE TABLE IF NOT EXISTS task_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                task_name TEXT NOT NULL,
                job_id TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_seconds REAL,
                stats_json TEXT,
                error_message TEXT,
                logs_json TEXT,
                is_auto_sync INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_task_logs_type ON task_logs(task_type);
            CREATE INDEX IF NOT EXISTS idx_task_logs_started_at ON task_logs(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_task_logs_auto_sync ON task_logs(is_auto_sync);
            """
        )
        # 迁移：为旧版 users 表添加 status 和 last_checked_at 字段
        self._migrate_users_table()
        # 迁移：为 series 表添加 is_subscribed 字段
        self._migrate_series_table()
        self.conn.commit()

    def upsert_user(self, record: UserRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO users (user_id, name, account, raw_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              name = excluded.name,
              account = excluded.account,
              raw_json = excluded.raw_json,
              updated_at = CURRENT_TIMESTAMP
            """,
            (record.user_id, record.name, record.account, record.raw_json),
        )
        self.conn.commit()

    def upsert_novel(self, record: NovelRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO novels (
                novel_id, user_id, series_id, title, caption, visible, restrict_value,
                x_restrict, text_length, total_bookmarks, total_views, cover_url,
                tags_json, create_date, raw_json, meta_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(novel_id) DO UPDATE SET
                user_id = excluded.user_id,
                series_id = excluded.series_id,
                title = excluded.title,
                caption = excluded.caption,
                visible = excluded.visible,
                restrict_value = excluded.restrict_value,
                x_restrict = excluded.x_restrict,
                text_length = excluded.text_length,
                total_bookmarks = excluded.total_bookmarks,
                total_views = excluded.total_views,
                cover_url = excluded.cover_url,
                tags_json = excluded.tags_json,
                create_date = excluded.create_date,
                raw_json = excluded.raw_json,
                meta_hash = excluded.meta_hash,
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (
                record.novel_id,
                record.user_id,
                record.series_id,
                record.title,
                record.caption,
                1 if record.visible else 0,
                record.restrict,
                record.x_restrict,
                record.text_length,
                record.total_bookmarks,
                record.total_views,
                record.cover_url,
                record.tags_json,
                record.create_date,
                record.raw_json,
                record.meta_hash,
            ),
        )
        self.conn.commit()

    def upsert_novel_text(self, record: NovelTextRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO novel_texts (novel_id, text_raw, text_markdown, text_hash, fetched_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(novel_id) DO UPDATE SET
                text_raw = excluded.text_raw,
                text_markdown = excluded.text_markdown,
                text_hash = excluded.text_hash,
                fetched_at = CURRENT_TIMESTAMP
            """,
            (record.novel_id, record.text_raw, record.text_markdown, record.text_hash),
        )
        self.conn.commit()

    def upsert_source(self, record: SourceRecord) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO sources (novel_id, source_type, source_key)
            VALUES (?, ?, ?)
            """,
            (record.novel_id, record.source_type, record.source_key),
        )
        self.conn.commit()

    def replace_fts(self, novel_id: int, title: str, caption: str, author_name: str, body: str) -> None:
        self.conn.execute("DELETE FROM novel_fts WHERE novel_id = ?", (novel_id,))
        self.conn.execute(
            "INSERT INTO novel_fts (novel_id, title, caption, author_name, body) VALUES (?, ?, ?, ?, ?)",
            (novel_id, title, caption, author_name, body),
        )
        self.conn.commit()

    def get_novel_text_hash(self, novel_id: int) -> str | None:
        row = self.conn.execute("SELECT text_hash FROM novel_texts WHERE novel_id = ?", (novel_id,)).fetchone()
        return str(row[0]) if row else None

    def novel_exists(self, novel_id: int) -> bool:
        """检查小说是否已存在（有元数据或正文）"""
        row = self.conn.execute("SELECT 1 FROM novels WHERE novel_id = ? UNION SELECT 1 FROM novel_texts WHERE novel_id = ? LIMIT 1", (novel_id, novel_id)).fetchone()
        return row is not None

    def get_novel_meta_hash(self, novel_id: int) -> str | None:
        """获取小说的 meta_hash，用于增量同步判断"""
        row = self.conn.execute("SELECT meta_hash FROM novels WHERE novel_id = ?", (novel_id,)).fetchone()
        return str(row[0]) if row else None

    def touch_novel(self, novel_id: int) -> None:
        """更新小说的 last_seen_at 时间戳"""
        self.conn.execute(
            "UPDATE novels SET last_seen_at = CURRENT_TIMESTAMP WHERE novel_id = ?",
            (novel_id,),
        )
        self.conn.commit()

    def record_asset(self, novel_id: int, asset_type: str, remote_url: str, local_path: str, file_hash: str | None) -> None:
        self.conn.execute(
            """
            INSERT INTO assets (novel_id, asset_type, remote_url, local_path, file_hash, downloaded_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(novel_id, asset_type, remote_url) DO UPDATE SET
                local_path = excluded.local_path,
                file_hash = excluded.file_hash,
                downloaded_at = CURRENT_TIMESTAMP
            """,
            (novel_id, asset_type, remote_url, local_path, file_hash),
        )
        self.conn.commit()

    def export_stats(self) -> str:
        row = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM users) AS users_count, (SELECT COUNT(*) FROM novels) AS novels_count"
        ).fetchone()
        return json.dumps(dict(row), ensure_ascii=False)

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

    def list_recent_novels(self, page: int = 1, page_size: int = 10, category: str = "all") -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(page_size, 1)

        where_clauses: list[str] = []
        params: list[Any] = []
        empty_message = None

        if category == "series":
            where_clauses.append("n.series_id IS NOT NULL")
        elif category == "single":
            where_clauses.append("n.series_id IS NULL")
        elif category == "following":
            where_clauses.append("EXISTS (SELECT 1 FROM sources s WHERE s.novel_id = n.novel_id AND (s.source_type = 'following_user_scan' OR s.source_type LIKE 'follow_feed_%'))")
            empty_message = "当前还没有“关注用户小说列表”数据，请后续开启关注用户小说同步链路后再查看。"

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        total = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM novels n {where_sql}",
                params,
            ).fetchone()[0]
        )
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size

        rows = self.conn.execute(
            f"""
            SELECT
                n.novel_id,
                n.title,
                n.user_id,
                n.series_id,
                u.name AS author_name,
                n.cover_url,
                n.restrict_value,
                n.total_bookmarks,
                n.total_views,
                n.last_seen_at,
                n.first_seen_at,
                CASE WHEN n.series_id IS NULL THEN 'single' ELSE 'series' END AS novel_kind
            FROM novels AS n
            LEFT JOIN users AS u ON u.user_id = n.user_id
            {where_sql}
            ORDER BY n.last_seen_at DESC, n.novel_id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, offset],
        ).fetchall()
        items = [dict(row) for row in rows]
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "category": category,
            "empty_message": empty_message,
        }

    def get_novel_detail(self, novel_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT
                n.novel_id,
                n.title,
                n.caption,
                n.user_id,
                n.series_id,
                u.name AS author_name,
                u.account AS author_account,
                n.cover_url,
                n.restrict_value,
                n.total_bookmarks,
                n.total_views,
                n.text_length,
                n.create_date,
                n.first_seen_at,
                n.last_seen_at,
                nt.text_raw,
                nt.text_markdown,
                n.raw_json
            FROM novels AS n
            LEFT JOIN users AS u ON u.user_id = n.user_id
            LEFT JOIN novel_texts AS nt ON nt.novel_id = n.novel_id
            WHERE n.novel_id = ?
            """,
            (novel_id,),
        ).fetchone()
        if row is None:
            return None

        data = dict(row)
        data["novel_kind"] = "single" if data.get("series_id") is None else "series"
        data["raw_json"] = self._load_raw_json(str(data.get("raw_json") or "{}"))
        return data

    @staticmethod
    def _load_raw_json(raw_json: str) -> dict[str, Any]:
        try:
            data = json.loads(raw_json)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @classmethod
    def _extract_user_avatar(cls, raw: dict[str, Any]) -> str | None:
        for key in (
            "profile_image_urls",
            "image_urls",
            "profile_image_url",
            "profile_image",
            "avatar",
        ):
            value = raw.get(key)
            url = cls._pick_image_url(value)
            if url:
                return url

        user_block = raw.get("user")
        if isinstance(user_block, dict):
            for key in (
                "profile_image_urls",
                "image_urls",
                "profile_image_url",
                "profile_image",
                "avatar",
            ):
                url = cls._pick_image_url(user_block.get(key))
                if url:
                    return url
        return None

    @classmethod
    def _pick_image_url(cls, value: Any) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for key in ("medium", "main", "large", "px_170x170", "px_50x50", "square_medium"):
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    return item.strip()
            for item in value.values():
                nested = cls._pick_image_url(item)
                if nested:
                    return nested
        if isinstance(value, list):
            for item in value:
                nested = cls._pick_image_url(item)
                if nested:
                    return nested
        return None

    def close(self) -> None:
        self.conn.close()

    def _migrate_users_table(self) -> None:
        """为旧版 users 表添加 status 和 last_checked_at 字段"""
        cursor = self.conn.execute("PRAGMA table_info(users)")
        columns = {row[1] for row in cursor.fetchall()}
        if "status" not in columns:
            self.conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown'")
        if "last_checked_at" not in columns:
            self.conn.execute("ALTER TABLE users ADD COLUMN last_checked_at TEXT")

    def _migrate_series_table(self) -> None:
        """为 series 表添加 is_subscribed 字段"""
        cursor = self.conn.execute("PRAGMA table_info(series)")
        columns = {row[1] for row in cursor.fetchall()}
        if "is_subscribed" not in columns:
            self.conn.execute("ALTER TABLE series ADD COLUMN is_subscribed INTEGER NOT NULL DEFAULT 0")

    def upsert_user_status(self, user_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE users SET status = ?, last_checked_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (status, user_id),
        )
        self.conn.commit()

    def upsert_series(self, series_id: int, title: str, description: str, user_id: int, cover_url: str | None) -> None:
        self.conn.execute(
            """
            INSERT INTO series (series_id, title, description, user_id, cover_url, total_novels, last_seen_at)
            VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(series_id) DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                cover_url = COALESCE(excluded.cover_url, series.cover_url),
                total_novels = (SELECT COUNT(*) FROM novels WHERE series_id = ?),
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (series_id, title, description, user_id, cover_url, series_id),
        )
        self.conn.commit()

    def upsert_subscribed_series(self, series_id: int, title: str, description: str, user_id: int, cover_url: str | None, total_novels: int = 0) -> None:
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
        self.conn.commit()

    def clear_subscribed_series(self) -> None:
        """清除所有订阅标记"""
        self.conn.execute("UPDATE series SET is_subscribed = 0")
        self.conn.commit()

    def list_bookmark_novels(self, page: int = 1, page_size: int = 10) -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(page_size, 1)
        where_sql = "WHERE s.source_type LIKE 'bookmark_%'"
        total = int(
            self.conn.execute(
                f"SELECT COUNT(DISTINCT n.novel_id) FROM novels n LEFT JOIN sources s ON s.novel_id = n.novel_id {where_sql}"
            ).fetchone()[0]
        )
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT
                n.novel_id, n.title, n.user_id, n.series_id,
                u.name AS author_name, n.cover_url, n.restrict_value,
                n.total_bookmarks, n.total_views, n.last_seen_at, n.first_seen_at,
                CASE WHEN n.series_id IS NULL THEN 'single' ELSE 'series' END AS novel_kind
            FROM novels AS n
            LEFT JOIN users AS u ON u.user_id = n.user_id
            LEFT JOIN sources AS s ON s.novel_id = n.novel_id
            {where_sql}
            ORDER BY n.last_seen_at DESC, n.novel_id DESC
            LIMIT ? OFFSET ?
            """,
            [page_size, offset],
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "page": page, "page_size": page_size,
            "total": total, "total_pages": total_pages, "category": "bookmark",
        }

    def list_following_series(self, page: int = 1, page_size: int = 10) -> dict[str, Any]:
        """获取订阅的系列列表"""
        page = max(page, 1)
        page_size = max(page_size, 1)
        total = int(
            self.conn.execute(
                """
                SELECT COUNT(*) FROM series WHERE is_subscribed = 1
                """
            ).fetchone()[0]
        )
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            """
            SELECT
                se.series_id,
                se.title AS series_title,
                se.description AS series_description,
                se.user_id,
                u.name AS author_name,
                se.cover_url,
                se.total_novels AS chapter_count,
                se.last_seen_at AS last_updated,
                COALESCE((SELECT SUM(n.text_length) FROM novels n WHERE n.series_id = se.series_id), 0) AS total_text_length
            FROM series se
            LEFT JOIN users AS u ON u.user_id = se.user_id
            WHERE se.is_subscribed = 1
            ORDER BY se.last_seen_at DESC
            LIMIT ? OFFSET ?
            """,
            [page_size, offset],
        ).fetchall()
        items = [dict(row) for row in rows]
        return {
            "items": items,
            "page": page, "page_size": page_size,
            "total": total, "total_pages": total_pages, "category": "following",
        }

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
            novels = self.conn.execute(
                """
                SELECT n.*, u.name AS author_name FROM novels n
                LEFT JOIN users u ON u.user_id = n.user_id
                WHERE n.series_id = ?
                ORDER BY n.create_date ASC
                """,
                (series_id,),
            ).fetchall()
        series_info["novels"] = [dict(row) for row in novels]
        # 计算系列总字数
        total_text_length = sum(row.get("text_length", 0) or 0 for row in series_info["novels"])
        series_info["total_text_length"] = total_text_length
        return series_info

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
            ORDER BY u.updated_at DESC
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
                COALESCE(se.title, MIN(n.title)) AS series_title,
                se.description AS series_description,
                se.cover_url,
                COUNT(n.novel_id) AS chapter_count,
                COALESCE(SUM(n.text_length), 0) AS total_text_length,
                MAX(n.last_seen_at) AS last_updated
            FROM novels n
            LEFT JOIN series se ON se.series_id = n.series_id
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

    def delete_novel(self, novel_id: int) -> None:
        """删除小说及其相关数据"""
        self.conn.execute("DELETE FROM novel_texts WHERE novel_id = ?", (novel_id,))
        self.conn.execute("DELETE FROM assets WHERE novel_id = ?", (novel_id,))
        self.conn.execute("DELETE FROM sources WHERE novel_id = ?", (novel_id,))
        self.conn.execute("DELETE FROM novel_fts WHERE novel_id = ?", (novel_id,))
        self.conn.execute("DELETE FROM novels WHERE novel_id = ?", (novel_id,))
        self.conn.commit()

    def delete_user(self, user_id: int) -> None:
        """删除用户及其所有小说"""
        # 先删除用户的所有小说
        novel_rows = self.conn.execute("SELECT novel_id FROM novels WHERE user_id = ?", (user_id,)).fetchall()
        for row in novel_rows:
            self.delete_novel(row[0])
        # 删除用户
        self.conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        self.conn.commit()

    def delete_series(self, series_id: int) -> None:
        """删除系列（不删除小说，只解除关联）"""
        self.conn.execute("UPDATE novels SET series_id = NULL WHERE series_id = ?", (series_id,))
        self.conn.execute("DELETE FROM series WHERE series_id = ?", (series_id,))
        self.conn.commit()

    def delete_bookmark(self, novel_id: int) -> None:
        """删除收藏记录"""
        self.conn.execute(
            "DELETE FROM sources WHERE novel_id = ? AND source_type LIKE 'bookmark_%'",
            (novel_id,),
        )
        self.conn.commit()

    def init_sync_check_table(self) -> None:
        """初始化同步检查表"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sync_check_list (
                novel_id INTEGER PRIMARY KEY,
                exists_local INTEGER NOT NULL DEFAULT 0,
                checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self.conn.commit()

    def create_task_log(self, task_type: str, task_name: str, job_id: str | None = None, is_auto_sync: bool = False) -> int:
        """创建任务日志记录"""
        cursor = self.conn.execute(
            """
            INSERT INTO task_logs (task_type, task_name, job_id, status, started_at, is_auto_sync)
            VALUES (?, ?, ?, 'running', datetime('now'), ?)
            """,
            (task_type, task_name, job_id, 1 if is_auto_sync else 0)
        )
        self.conn.commit()
        return cursor.lastrowid

    def update_task_log(self, log_id: int, status: str, stats: dict[str, Any] | None = None,
                       error_message: str | None = None, logs: list[dict[str, Any]] | None = None) -> None:
        """更新任务日志"""
        self.conn.execute(
            """
            UPDATE task_logs
            SET status = ?,
                finished_at = datetime('now'),
                duration_seconds = (julianday(datetime('now')) - julianday(started_at)) * 86400,
                stats_json = ?,
                error_message = ?,
                logs_json = ?
            WHERE id = ?
            """,
            (status, json.dumps(stats) if stats else None, error_message, json.dumps(logs) if logs else None, log_id)
        )
        self.conn.commit()

    def get_task_logs(self, page: int = 1, page_size: int = 20,
                     task_type: str | None = None, is_auto_sync: bool | None = None,
                     days: int = 3) -> dict[str, Any]:
        """获取任务日志列表"""
        offset = (page - 1) * page_size
        
        conditions = ["started_at >= datetime('now', ? || ' days')"]
        params: list[Any] = [f"-{days}"]
        
        if task_type:
            conditions.append("task_type = ?")
            params.append(task_type)
        
        if is_auto_sync is not None:
            conditions.append("is_auto_sync = ?")
            params.append(1 if is_auto_sync else 0)
        
        where_clause = " AND ".join(conditions)
        
        # 获取总数
        total = self.conn.execute(
            f"SELECT COUNT(*) FROM task_logs WHERE {where_clause}", params
        ).fetchone()[0]
        
        # 获取数据
        rows = self.conn.execute(
            f"""
            SELECT * FROM task_logs
            WHERE {where_clause}
            ORDER BY started_at DESC
            LIMIT ? OFFSET ?
            """,
            params + [page_size, offset]
        ).fetchall()
        
        total_pages = (total + page_size - 1) // page_size
        
        result = []
        for row in rows:
            item = dict(row)
            if item.get("stats_json"):
                item["stats"] = json.loads(item["stats_json"])
            if item.get("logs_json"):
                item["logs"] = json.loads(item["logs_json"])
            item["is_auto_sync"] = bool(item["is_auto_sync"])
            result.append(item)
        
        return {
            "items": result,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }

    def cleanup_old_task_logs(self, days: int = 3) -> int:
        """清理旧的任务日志"""
        cursor = self.conn.execute(
            "DELETE FROM task_logs WHERE started_at < datetime('now', ? || ' days')",
            (f"-{days}",)
        )
        self.conn.commit()
        return cursor.rowcount

    def clear_sync_check_list(self) -> None:
        """清空同步检查列表"""
        self.conn.execute("DELETE FROM sync_check_list")
        self.conn.commit()

    def upsert_sync_check_item(self, novel_id: int, exists_local: bool) -> None:
        """更新同步检查项"""
        self.conn.execute(
            """
            INSERT INTO sync_check_list (novel_id, exists_local, checked_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(novel_id) DO UPDATE SET
                exists_local = excluded.exists_local,
                checked_at = CURRENT_TIMESTAMP
            """,
            (novel_id, 1 if exists_local else 0),
        )
        self.conn.commit()

    def get_sync_check_list(self) -> dict[int, bool]:
        """获取同步检查列表，返回 {novel_id: exists_local}"""
        rows = self.conn.execute("SELECT novel_id, exists_local FROM sync_check_list").fetchall()
        return {row[0]: bool(row[1]) for row in rows}

    def get_existing_novel_ids(self, novel_ids: list[int]) -> set[int]:
        """批量检查小说是否已存在，返回已存在的 ID 集合"""
        if not novel_ids:
            return set()
        placeholders = ",".join(["?"] * len(novel_ids))
        rows = self.conn.execute(
            f"SELECT novel_id FROM novels WHERE novel_id IN ({placeholders})",
            novel_ids,
        ).fetchall()
        return {row[0] for row in rows}

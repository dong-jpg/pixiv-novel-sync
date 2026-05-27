from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import AssetRecord, NovelRecord, NovelTextRecord, SourceRecord, UserRecord


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False 允许在多个线程间共享连接（SQLite C 层是线程安全的）；
        # 我们用显式 RLock 串行化所有写入。读不需要锁（WAL 模式允许并发读）。
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self._lock: threading.RLock = threading.RLock()
        self._transaction_depth = 0

    def _commit_if_needed(self) -> None:
        if self._transaction_depth == 0:
            self.conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务上下文：with db.transaction() as conn: ... 在退出时统一 commit / rollback。

        与 sqlite3 内置的隐式事务不同，使用显式 BEGIN IMMEDIATE 抢占写锁，
        避免多线程下 SQLITE_BUSY。嵌套调用是安全的（RLock）。
        """
        with self._lock:
            self._transaction_depth += 1
            outermost = self._transaction_depth == 1
            try:
                if outermost:
                    self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
                if outermost:
                    self.conn.commit()
            except Exception:
                if outermost:
                    self.conn.rollback()
                raise
            finally:
                self._transaction_depth -= 1

    def init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                PRAGMA busy_timeout=30000;
                """
            )
            self.conn.executescript(
                """

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

            CREATE INDEX IF NOT EXISTS idx_novels_user_id ON novels(user_id);
            CREATE INDEX IF NOT EXISTS idx_novels_series_id ON novels(series_id);
            CREATE INDEX IF NOT EXISTS idx_sources_source_type ON sources(source_type);
            """
        )
        # 迁移：为旧版 users 表添加 status 和 last_checked_at 字段
        self._migrate_users_table()
        # 修复：重置错误标记为 cleared 的用户状态
        self._fix_cleared_status()
        # 迁移：为 novels 表添加 status 和 last_checked_at 字段
        self._migrate_novels_table()
        # 迁移：为 series 表添加 is_subscribed、status、last_checked_at 字段
        self._migrate_series_table()
        # 修复：将进程重启后遗留的 running 状态日志标记为 failed
        self._fix_stale_running_logs()
        # 迁移：创建待确认删除表
        self._migrate_pending_deletions_table()
        # 迁移：创建同步水位线表
        self._migrate_sync_watermarks_table()
        # 迁移：创建/升级预检查表。旧服务端库可能已有无 scope 的 sync_check_list。
        self.init_sync_check_table()
        # 迁移：创建 AI 创作工作台相关表
        self._migrate_ai_tables()
        self._commit_if_needed()

    def upsert_user(self, record: UserRecord) -> None:
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

    def upsert_novel(self, record: NovelRecord) -> None:
        with self._lock:
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
            self._commit_if_needed()

    def upsert_novel_text(self, record: NovelTextRecord) -> None:
        with self._lock:
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
            self._commit_if_needed()

    def upsert_source(self, record: SourceRecord) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO sources (novel_id, source_type, source_key)
                VALUES (?, ?, ?)
                """,
                (record.novel_id, record.source_type, record.source_key),
            )
            self._commit_if_needed()

    def replace_fts(self, novel_id: int, title: str, caption: str, author_name: str, body: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM novel_fts WHERE novel_id = ?", (novel_id,))
            self.conn.execute(
                "INSERT INTO novel_fts (novel_id, title, caption, author_name, body) VALUES (?, ?, ?, ?, ?)",
                (novel_id, title, caption, author_name, body),
            )
            self._commit_if_needed()

    def get_novel_text_hash(self, novel_id: int) -> str | None:
        row = self.conn.execute("SELECT text_hash FROM novel_texts WHERE novel_id = ?", (novel_id,)).fetchone()
        return str(row[0]) if row else None

    def novel_exists(self, novel_id: int) -> bool:
        """检查小说是否已存在（有元数据或正文）"""
        row = self.conn.execute("SELECT 1 FROM novels WHERE novel_id = ? UNION SELECT 1 FROM novel_texts WHERE novel_id = ? LIMIT 1", (novel_id, novel_id)).fetchone()
        return row is not None

    def novel_text_exists(self, novel_id: int) -> bool:
        """检查小说正文是否已存在"""
        row = self.conn.execute("SELECT 1 FROM novel_texts WHERE novel_id = ? LIMIT 1", (novel_id,)).fetchone()
        return row is not None

    def get_novel_meta_hash(self, novel_id: int) -> str | None:
        """获取小说的 meta_hash，用于增量同步判断"""
        row = self.conn.execute("SELECT meta_hash FROM novels WHERE novel_id = ?", (novel_id,)).fetchone()
        return str(row[0]) if row else None

    def touch_novel(self, novel_id: int) -> None:
        """更新小说的 last_seen_at 时间戳"""
        with self._lock:
            self.conn.execute(
                "UPDATE novels SET last_seen_at = CURRENT_TIMESTAMP WHERE novel_id = ?",
                (novel_id,),
            )
            self._commit_if_needed()

    def record_asset(self, novel_id: int, asset_type: str, remote_url: str, local_path: str, file_hash: str | None) -> None:
        with self._lock:
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
            self._commit_if_needed()

    def export_stats(self) -> str:
        row = self.conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM users) AS users_count, "
            "(SELECT COUNT(*) FROM novels) AS novels_count, "
            "(SELECT COUNT(*) FROM series) AS series_count, "
            "(SELECT COUNT(*) FROM pending_deletions WHERE status = 'pending') AS pending_count"
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

    def list_recent_novels(self, page: int = 1, page_size: int = 10, category: str = "all",
                           search: str = "", sort: str = "") -> dict[str, Any]:
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
            empty_message = '当前还没有"关注用户小说列表"数据，请后续开启关注用户小说同步链路后再查看。'

        if search:
            where_clauses.append("(n.title LIKE ? OR u.name LIKE ?)")
            search_pattern = f"%{search}%"
            params.extend([search_pattern, search_pattern])

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        total = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM novels n LEFT JOIN users AS u ON u.user_id = n.user_id {where_sql}",
                params,
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
            ORDER BY {order_sql}
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
                n.status,
                n.last_checked_at,
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

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def _migrate_users_table(self) -> None:
        """为旧版 users 表添加 status 和 last_checked_at 字段"""
        cursor = self.conn.execute("PRAGMA table_info(users)")
        columns = {row[1] for row in cursor.fetchall()}
        if "status" not in columns:
            self.conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown'")
        if "last_checked_at" not in columns:
            self.conn.execute("ALTER TABLE users ADD COLUMN last_checked_at TEXT")

    def _fix_cleared_status(self) -> None:
        """重置错误标记为 cleared 的用户状态为 unknown"""
        try:
            self.conn.execute("UPDATE users SET status = 'unknown' WHERE status = 'cleared'")
            self._commit_if_needed()
        except Exception:
            pass

    def _fix_stale_running_logs(self) -> None:
        """将进程重启后遗留的 running 状态日志标记为 failed"""
        try:
            self.conn.execute(
                "UPDATE task_logs SET status = 'failed', error_message = '进程重启，任务中断', "
                "finished_at = CURRENT_TIMESTAMP WHERE status = 'running'"
            )
            self._commit_if_needed()
        except Exception:
            pass

    def _migrate_novels_table(self) -> None:
        """为 novels 表添加 status 和 last_checked_at 字段"""
        cursor = self.conn.execute("PRAGMA table_info(novels)")
        columns = {row[1] for row in cursor.fetchall()}
        if "status" not in columns:
            self.conn.execute("ALTER TABLE novels ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown'")
        if "last_checked_at" not in columns:
            self.conn.execute("ALTER TABLE novels ADD COLUMN last_checked_at TEXT")

    def _migrate_series_table(self) -> None:
        """为 series 表添加 is_subscribed、status、last_checked_at 字段"""
        cursor = self.conn.execute("PRAGMA table_info(series)")
        columns = {row[1] for row in cursor.fetchall()}
        if "is_subscribed" not in columns:
            self.conn.execute("ALTER TABLE series ADD COLUMN is_subscribed INTEGER NOT NULL DEFAULT 0")
        if "status" not in columns:
            self.conn.execute("ALTER TABLE series ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown'")
        if "last_checked_at" not in columns:
            self.conn.execute("ALTER TABLE series ADD COLUMN last_checked_at TEXT")

    def upsert_user_status(self, user_id: int, status: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE users SET status = ?, last_checked_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (status, user_id),
            )
            self._commit_if_needed()

    def upsert_novel_status(self, novel_id: int, status: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE novels SET status = ?, last_checked_at = CURRENT_TIMESTAMP WHERE novel_id = ?",
                (status, novel_id),
            )
            self._commit_if_needed()

    def upsert_series_status(self, series_id: int, status: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE series SET status = ?, last_checked_at = CURRENT_TIMESTAMP WHERE series_id = ?",
                (status, series_id),
            )
            self._commit_if_needed()

    def get_all_novel_ids(self) -> list[int]:
        rows = self.conn.execute("SELECT novel_id FROM novels ORDER BY novel_id").fetchall()
        return [row[0] for row in rows]

    def get_all_series_ids(self) -> list[int]:
        rows = self.conn.execute("SELECT series_id FROM series ORDER BY series_id").fetchall()
        return [row[0] for row in rows]

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
        cursor = self.conn.execute(  # 读操作为主，写入量小，WAL 模式下可接受
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

    def list_bookmark_novels(self, page: int = 1, page_size: int = 10,
                            search: str = "", sort: str = "") -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(page_size, 1)
        where_clauses: list[str] = ["s.source_type LIKE 'bookmark_%'"]
        params_count: list[Any] = []
        if search:
            where_clauses.append("(n.title LIKE ? OR u.name LIKE ?)")
            search_pattern = f"%{search}%"
            params_count.extend([search_pattern, search_pattern])
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
            search_pattern = f"%{search}%"
            params_query.extend([search_pattern, search_pattern])
        params_query.extend([page_size, offset])

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

    def list_following_series(self, page: int = 1, page_size: int = 10,
                             search: str = "", sort: str = "") -> dict[str, Any]:
        """获取订阅的系列列表"""
        page = max(page, 1)
        page_size = max(page_size, 1)

        where_clauses: list[str] = ["se.is_subscribed = 1"]
        params_count: list[Any] = []
        if search:
            search_pattern = f"%{search}%"
            where_clauses.append(
                """(se.title LIKE ? OR (
                   (se.title IS NULL OR se.title = '') AND EXISTS (
                     SELECT 1 FROM novels n0 WHERE n0.series_id = se.series_id AND n0.title LIKE ?
                   )
                   ) OR u.name LIKE ?)"""
            )
            params_count.extend([search_pattern, search_pattern, search_pattern])

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
            order_sql = """(SELECT COALESCE(SUM(n.total_bookmarks), 0) FROM novels n WHERE n.series_id = se.series_id) DESC"""
        elif sort == "views_desc":
            order_sql = """(SELECT COALESCE(SUM(n.total_views), 0) FROM novels n WHERE n.series_id = se.series_id) DESC"""

        params_query: list[Any] = []
        if search:
            search_pattern = f"%{search}%"
            params_query.extend([search_pattern, search_pattern, search_pattern])
        params_query.extend([page_size, offset])

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
                se.total_novels AS chapter_count,
                se.last_seen_at AS last_updated,
                COALESCE((SELECT SUM(n.text_length) FROM novels n WHERE n.series_id = se.series_id), 0) AS total_text_length
            FROM series se
            LEFT JOIN users AS u ON u.user_id = se.user_id
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            params_query,
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

    def delete_novel(self, novel_id: int) -> None:
        """删除小说及其相关数据"""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DELETE FROM novel_texts WHERE novel_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM assets WHERE novel_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM sources WHERE novel_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM novel_fts WHERE novel_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM novels WHERE novel_id = ?", (novel_id,))
                self._commit_if_needed()
            except Exception:
                self.conn.rollback()
                raise

    def delete_user(self, user_id: int) -> None:
        """删除用户及其所有小说（单一事务，批量删除）"""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DELETE FROM novel_texts WHERE novel_id IN (SELECT novel_id FROM novels WHERE user_id = ?)", (user_id,))
                self.conn.execute("DELETE FROM assets WHERE novel_id IN (SELECT novel_id FROM novels WHERE user_id = ?)", (user_id,))
                self.conn.execute("DELETE FROM sources WHERE novel_id IN (SELECT novel_id FROM novels WHERE user_id = ?)", (user_id,))
                self.conn.execute("DELETE FROM novel_fts WHERE novel_id IN (SELECT novel_id FROM novels WHERE user_id = ?)", (user_id,))
                self.conn.execute("DELETE FROM novels WHERE user_id = ?", (user_id,))
                self.conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
                self._commit_if_needed()
            except Exception:
                self.conn.rollback()
                raise

    def delete_series(self, series_id: int) -> None:
        """删除系列（不删除小说，只解除关联）"""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("UPDATE novels SET series_id = NULL WHERE series_id = ?", (series_id,))
                self.conn.execute("DELETE FROM series WHERE series_id = ?", (series_id,))
                self._commit_if_needed()
            except Exception:
                self.conn.rollback()
                raise

    def delete_bookmark(self, novel_id: int) -> None:
        """删除收藏记录"""
        with self._lock:
            self.conn.execute(
                "DELETE FROM sources WHERE novel_id = ? AND source_type LIKE 'bookmark_%'",
                (novel_id,),
            )
            self._commit_if_needed()

    def init_sync_check_table(self) -> None:
        """初始化同步检查表"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sync_check_list (
                scope TEXT NOT NULL DEFAULT '_',
                novel_id INTEGER NOT NULL,
                exists_local INTEGER NOT NULL DEFAULT 0,
                checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (scope, novel_id)
            );
            """
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(sync_check_list)").fetchall()}
        if "scope" not in columns:
            self.conn.executescript(
                """
                ALTER TABLE sync_check_list RENAME TO sync_check_list_old;
                CREATE TABLE sync_check_list (
                    scope TEXT NOT NULL DEFAULT '_',
                    novel_id INTEGER NOT NULL,
                    exists_local INTEGER NOT NULL DEFAULT 0,
                    checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scope, novel_id)
                );
                INSERT OR REPLACE INTO sync_check_list (scope, novel_id, exists_local, checked_at)
                SELECT '_', novel_id, exists_local, checked_at FROM sync_check_list_old;
                DROP TABLE sync_check_list_old;
                """
            )
        self._commit_if_needed()

    def create_task_log(self, task_type: str, task_name: str, job_id: str | None = None, is_auto_sync: bool = False) -> int:
        """创建任务日志记录"""
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO task_logs (task_type, task_name, job_id, status, started_at, is_auto_sync)
                VALUES (?, ?, ?, 'running', datetime('now'), ?)
                """,
                (task_type, task_name, job_id, 1 if is_auto_sync else 0)
            )
            self._commit_if_needed()
            return cursor.lastrowid

    def update_task_log(self, log_id: int, status: str, stats: dict[str, Any] | None = None,
                       error_message: str | None = None, logs: list[dict[str, Any]] | None = None) -> None:
        """更新任务日志"""
        with self._lock:
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
            self._commit_if_needed()

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
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM task_logs WHERE started_at < datetime('now', ? || ' days')",
                (f"-{days}",)
            )
            self._commit_if_needed()
            return cursor.rowcount

    def get_task_log_by_id(self, log_id: int) -> dict[str, Any] | None:
        """获取单条任务日志详情"""
        row = self.conn.execute(
            "SELECT * FROM task_logs WHERE id = ?", (log_id,)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        if item.get("stats_json"):
            try:
                item["stats"] = json.loads(item["stats_json"])
            except (TypeError, ValueError):
                item["stats"] = None
        if item.get("logs_json"):
            try:
                item["logs"] = json.loads(item["logs_json"])
            except (TypeError, ValueError):
                item["logs"] = None
        item["is_auto_sync"] = bool(item.get("is_auto_sync"))
        return item

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

    def get_sync_check_list(self, scope: str = "_") -> dict[int, bool]:
        """获取同步检查列表，返回 {novel_id: exists_local}"""
        rows = self.conn.execute(
            "SELECT novel_id, exists_local FROM sync_check_list WHERE scope = ?",
            (scope,),
        ).fetchall()
        return {row[0]: bool(row[1]) for row in rows}

    def get_existing_novel_ids(self, novel_ids: list[int]) -> set[int]:
        """批量检查小说是否已存在，返回已存在的 ID 集合（分批查询避免超出 SQLite 变量限制）"""
        if not novel_ids:
            return set()
        result: set[int] = set()
        batch_size = 500
        for i in range(0, len(novel_ids), batch_size):
            batch = novel_ids[i:i + batch_size]
            placeholders = ",".join(["?"] * len(batch))
            rows = self.conn.execute(
                f"SELECT novel_id FROM novels WHERE novel_id IN ({placeholders})",
                batch,
            ).fetchall()
            result.update(row[0] for row in rows)
        return result

    # ── AI 创作工作台 ──────────────────────────────────────────────

    def _migrate_ai_tables(self) -> None:
        """创建 AI 创作工作台相关表。"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL,
                base_url TEXT,
                api_key_encrypted TEXT,
                default_model TEXT,
                available_models_json TEXT,
                timeout_seconds INTEGER NOT NULL DEFAULT 120,
                max_retries INTEGER NOT NULL DEFAULT 2,
                proxy TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                task_type TEXT NOT NULL,
                provider_id INTEGER NOT NULL,
                model TEXT,
                system_prompt TEXT NOT NULL,
                temperature REAL NOT NULL DEFAULT 0.8,
                top_p REAL NOT NULL DEFAULT 0.9,
                max_tokens INTEGER NOT NULL DEFAULT 4000,
                context_window INTEGER NOT NULL DEFAULT 16000,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL UNIQUE,
                task_type TEXT NOT NULL,
                agent_id INTEGER,
                status TEXT NOT NULL DEFAULT 'running',
                input_json TEXT NOT NULL,
                output_text TEXT,
                output_json TEXT,
                error_message TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source_job_id TEXT,
                parent_draft_id INTEGER,
                style_profile_id INTEGER,
                novel_profile_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                source_type TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                metadata_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_style_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source_type TEXT,
                source_ids_json TEXT,
                profile_json TEXT NOT NULL,
                sample_prompt TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_novel_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source_type TEXT,
                source_ids_json TEXT,
                profile_json TEXT NOT NULL,
                continuation_prompt TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_ai_agents_task_type ON ai_agents(task_type);
            CREATE INDEX IF NOT EXISTS idx_ai_agents_provider_id ON ai_agents(provider_id);
            CREATE INDEX IF NOT EXISTS idx_ai_jobs_job_id ON ai_jobs(job_id);
            CREATE INDEX IF NOT EXISTS idx_ai_jobs_created_at ON ai_jobs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_drafts_updated_at ON ai_drafts(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_documents_hash ON ai_documents(content_hash);
            """
        )

    def _row_to_ai_provider(self, row: sqlite3.Row, include_secret: bool = False) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        item["has_api_key"] = bool(item.get("api_key_encrypted"))
        if item.get("available_models_json"):
            try:
                item["available_models"] = json.loads(item["available_models_json"])
            except (TypeError, ValueError):
                item["available_models"] = []
        else:
            item["available_models"] = []
        if not include_secret:
            item.pop("api_key_encrypted", None)
        item.pop("available_models_json", None)
        return item

    def list_ai_providers(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM ai_providers ORDER BY id DESC").fetchall()
        return [self._row_to_ai_provider(row) for row in rows]

    def get_ai_provider(self, provider_id: int, include_secret: bool = False) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ai_providers WHERE id = ?", (provider_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_ai_provider(row, include_secret=include_secret)

    def create_ai_provider(self, data: dict[str, Any]) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO ai_providers (
                    name, provider_type, base_url, api_key_encrypted, default_model,
                    available_models_json, timeout_seconds, max_retries, proxy, enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("name"),
                    data.get("provider_type"),
                    data.get("base_url"),
                    data.get("api_key_encrypted"),
                    data.get("default_model"),
                    json.dumps(data.get("available_models") or [], ensure_ascii=False),
                    int(data.get("timeout_seconds") or 120),
                    int(data.get("max_retries") or 2),
                    data.get("proxy"),
                    1 if data.get("enabled", True) else 0,
                ),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_ai_provider(self, provider_id: int, data: dict[str, Any]) -> None:
        allowed = {
            "name", "provider_type", "base_url", "api_key_encrypted", "default_model",
            "available_models", "timeout_seconds", "max_retries", "proxy", "enabled",
        }
        fields: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key not in data:
                continue
            column = "available_models_json" if key == "available_models" else key
            value = json.dumps(data[key] or [], ensure_ascii=False) if key == "available_models" else data[key]
            if key == "enabled":
                value = 1 if value else 0
            fields.append(f"{column} = ?")
            params.append(value)
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(provider_id)
        with self._lock:
            self.conn.execute(f"UPDATE ai_providers SET {', '.join(fields)} WHERE id = ?", params)
            self._commit_if_needed()

    def delete_ai_provider(self, provider_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_providers WHERE id = ?", (provider_id,))
            self._commit_if_needed()

    def _row_to_ai_agent(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        return item

    def list_ai_agents(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT a.*, p.name AS provider_name, p.provider_type AS provider_type
            FROM ai_agents a
            LEFT JOIN ai_providers p ON p.id = a.provider_id
            ORDER BY a.id DESC
            """
        ).fetchall()
        return [self._row_to_ai_agent(row) for row in rows]

    def get_ai_agent(self, agent_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ai_agents WHERE id = ?", (agent_id,)).fetchone()
        return self._row_to_ai_agent(row) if row else None

    def create_ai_agent(self, data: dict[str, Any]) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO ai_agents (
                    name, task_type, provider_id, model, system_prompt, temperature,
                    top_p, max_tokens, context_window, enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("name"), data.get("task_type"), int(data.get("provider_id")),
                    data.get("model"), data.get("system_prompt"), float(data.get("temperature") or 0.8),
                    float(data.get("top_p") or 0.9), int(data.get("max_tokens") or 4000),
                    int(data.get("context_window") or 16000), 1 if data.get("enabled", True) else 0,
                ),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_ai_agent(self, agent_id: int, data: dict[str, Any]) -> None:
        allowed = {"name", "task_type", "provider_id", "model", "system_prompt", "temperature", "top_p", "max_tokens", "context_window", "enabled"}
        fields: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key not in data:
                continue
            value = data[key]
            if key in {"provider_id", "max_tokens", "context_window"}:
                value = int(value)
            elif key in {"temperature", "top_p"}:
                value = float(value)
            elif key == "enabled":
                value = 1 if value else 0
            fields.append(f"{key} = ?")
            params.append(value)
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(agent_id)
        with self._lock:
            self.conn.execute(f"UPDATE ai_agents SET {', '.join(fields)} WHERE id = ?", params)
            self._commit_if_needed()

    def delete_ai_agent(self, agent_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_agents WHERE id = ?", (agent_id,))
            self._commit_if_needed()

    def create_ai_job(self, job_id: str, task_type: str, agent_id: int | None, input_data: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO ai_jobs (job_id, task_type, agent_id, status, input_json, started_at)
                VALUES (?, ?, ?, 'running', ?, CURRENT_TIMESTAMP)
                """,
                (job_id, task_type, agent_id, json.dumps(input_data, ensure_ascii=False)),
            )
            self._commit_if_needed()

    def update_ai_job(self, job_id: str, status: str, output_text: str | None = None,
                      output_json: dict[str, Any] | None = None, error_message: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                """
                UPDATE ai_jobs
                SET status = ?, output_text = COALESCE(?, output_text),
                    output_json = COALESCE(?, output_json), error_message = ?,
                    finished_at = CASE WHEN ? IN ('succeeded', 'failed', 'cancelled') THEN CURRENT_TIMESTAMP ELSE finished_at END
                WHERE job_id = ?
                """,
                (
                    status,
                    output_text,
                    json.dumps(output_json, ensure_ascii=False) if output_json is not None else None,
                    error_message,
                    status,
                    job_id,
                ),
            )
            self._commit_if_needed()

    def get_ai_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ai_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        for key in ("input_json", "output_json"):
            if item.get(key):
                try:
                    item[key[:-5]] = json.loads(item[key])
                except (TypeError, ValueError):
                    item[key[:-5]] = None
        return item

    def list_ai_drafts(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(page_size, 1)
        total = int(self.conn.execute("SELECT COUNT(*) FROM ai_drafts").fetchone()[0])
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            "SELECT * FROM ai_drafts ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
        return {"items": [dict(row) for row in rows], "page": page, "page_size": page_size, "total": total, "total_pages": total_pages}

    def get_ai_draft(self, draft_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ai_drafts WHERE id = ?", (draft_id,)).fetchone()
        return dict(row) if row else None

    def create_ai_draft(self, data: dict[str, Any]) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO ai_drafts (title, content, source_job_id, parent_draft_id, style_profile_id, novel_profile_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (data.get("title"), data.get("content"), data.get("source_job_id"), data.get("parent_draft_id"), data.get("style_profile_id"), data.get("novel_profile_id")),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_ai_draft(self, draft_id: int, data: dict[str, Any]) -> None:
        allowed = {"title", "content", "source_job_id", "parent_draft_id", "style_profile_id", "novel_profile_id"}
        fields: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key in data:
                fields.append(f"{key} = ?")
                params.append(data[key])
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(draft_id)
        with self._lock:
            self.conn.execute(f"UPDATE ai_drafts SET {', '.join(fields)} WHERE id = ?", params)
            self._commit_if_needed()

    def delete_ai_draft(self, draft_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_drafts WHERE id = ?", (draft_id,))
            self._commit_if_needed()

    def create_ai_document(self, data: dict[str, Any]) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO ai_documents (title, source_type, content, content_hash, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (data.get("title"), data.get("source_type"), data.get("content"), data.get("content_hash"), json.dumps(data.get("metadata") or {}, ensure_ascii=False)),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def get_ai_document(self, document_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ai_documents WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        if item.get("metadata_json"):
            try:
                item["metadata"] = json.loads(item["metadata_json"])
            except (TypeError, ValueError):
                item["metadata"] = {}
        item.pop("metadata_json", None)
        return item

    # ── 待确认删除 ──────────────────────────────────────────────

    def _migrate_pending_deletions_table(self) -> None:
        """创建待确认删除表"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_deletions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                title TEXT,
                author_name TEXT,
                cover_url TEXT,
                source_type TEXT,
                detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'pending',
                confirmed_at TEXT,
                restored_at TEXT,
                UNIQUE(item_type, item_id)
            );
            CREATE INDEX IF NOT EXISTS idx_pending_deletions_status ON pending_deletions(status);
            CREATE INDEX IF NOT EXISTS idx_pending_deletions_detected_at ON pending_deletions(detected_at DESC);
            """
        )

    def _migrate_sync_watermarks_table(self) -> None:
        """创建同步水位线表"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sync_watermarks (
                sync_type TEXT NOT NULL,
                key TEXT NOT NULL DEFAULT '_',
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (sync_type, key)
            );
            """
        )

    def get_watermark(self, sync_type: str, key: str = "_") -> dict | None:
        """获取水位线，返回 value_json 解析后的 dict，不存在返回 None"""
        row = self.conn.execute(
            "SELECT value_json FROM sync_watermarks WHERE sync_type = ? AND key = ?",
            (sync_type, key),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    def update_watermark(self, sync_type: str, value: dict, key: str = "_") -> None:
        """写入/更新水位线"""
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO sync_watermarks (sync_type, key, value_json, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(sync_type, key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (sync_type, key, json.dumps(value, ensure_ascii=False)),
            )
            self._commit_if_needed()

    def clear_watermark(self, sync_type: str, key: str = "_") -> None:
        """删除指定水位线"""
        with self._lock:
            self.conn.execute(
                "DELETE FROM sync_watermarks WHERE sync_type = ? AND key = ?",
                (sync_type, key),
            )
            self._commit_if_needed()

    def add_pending_deletion(self, item_type: str, item_id: int, reason: str,
                             title: str, author_name: str, cover_url: str,
                             source_type: str | None = None) -> None:
        """插入待确认删除记录（已有 pending 记录则跳过，已确认/恢复的记录会被清除后重新插入）"""
        with self._lock:
            # 清除已确认或已恢复的旧记录，允许重新检测
            self.conn.execute(
                "DELETE FROM pending_deletions WHERE item_type = ? AND item_id = ? AND status IN ('confirmed', 'restored')",
                (item_type, item_id),
            )
            self.conn.execute(
                """
                INSERT OR IGNORE INTO pending_deletions
                    (item_type, item_id, reason, title, author_name, cover_url, source_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (item_type, item_id, reason, title, author_name, cover_url, source_type),
            )
            self._commit_if_needed()

    def list_pending_deletions(self, page: int = 1, page_size: int = 20,
                               item_type: str | None = None) -> dict[str, Any]:
        """分页查询 pending 状态的待确认删除记录"""
        page = max(page, 1)
        page_size = max(page_size, 1)
        where_clauses = ["status = 'pending'"]
        params: list[Any] = []
        if item_type in ("novel", "series"):
            where_clauses.append("item_type = ?")
            params.append(item_type)
        where_sql = f"WHERE {' AND '.join(where_clauses)}"
        total = int(self.conn.execute(f"SELECT COUNT(*) FROM pending_deletions {where_sql}", params).fetchone()[0])
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            f"SELECT * FROM pending_deletions {where_sql} ORDER BY detected_at DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "page": page, "page_size": page_size,
            "total": total, "total_pages": total_pages,
        }

    def confirm_pending_deletion(self, deletion_id: int) -> dict[str, Any] | None:
        """确认删除，返回记录详情，更新状态为 confirmed"""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM pending_deletions WHERE id = ? AND status = 'pending'", (deletion_id,)
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE pending_deletions SET status = 'confirmed', confirmed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (deletion_id,),
            )
            self._commit_if_needed()
            return dict(row)

    def restore_pending_deletion(self, deletion_id: int) -> dict[str, Any] | None:
        """恢复，返回记录详情，更新状态为 restored"""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM pending_deletions WHERE id = ? AND status = 'pending'", (deletion_id,)
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE pending_deletions SET status = 'restored', restored_at = CURRENT_TIMESTAMP WHERE id = ?",
                (deletion_id,),
            )
            self._commit_if_needed()
            return dict(row)

    def get_pending_deletion_count(self) -> int:
        """获取 pending 状态的记录总数"""
        return int(self.conn.execute("SELECT COUNT(*) FROM pending_deletions WHERE status = 'pending'").fetchone()[0])

    def cleanup_stale_pending(self, remote_ids: set[int], item_type: str) -> int:
        """清除已重新出现在远程列表中的 pending 记录（用户重新收藏/追更了）"""
        if not remote_ids:
            return 0
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, item_id FROM pending_deletions WHERE item_type = ? AND status = 'pending'",
                (item_type,),
            ).fetchall()
            count = 0
            for row in rows:
                if row[1] in remote_ids:
                    self.conn.execute("UPDATE pending_deletions SET status = 'restored', restored_at = CURRENT_TIMESTAMP WHERE id = ?", (row[0],))
                    count += 1
            if count:
                self._commit_if_needed()
            return count

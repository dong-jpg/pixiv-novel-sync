from __future__ import annotations

import json
import sqlite3
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import AssetRecord, NovelRecord, NovelTextRecord, SourceRecord, UserRecord


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # threading.local 每线程连接:消除共享单连接导致的游标交错/ProgrammingError。
        # WAL 允许多个独立连接并发读,BEGIN IMMEDIATE 串行化写。
        self._local = threading.local()
        self._lock: threading.RLock = threading.RLock()  # 仅保护元状态(如 _all_conns)
        self._all_conns: set[sqlite3.Connection] = set()  # 弱引用集合供 close() 关闭全部

    @property
    def conn(self) -> sqlite3.Connection:
        """当前线程的 SQLite 连接,首次访问时 lazy 创建并初始化 PRAGMA。"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            # 每个连接独立开启 WAL + 设置超时
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
            with self._lock:
                self._all_conns.add(conn)
        return self._local.conn

    @property
    def _transaction_depth(self) -> int:
        """当前线程的事务嵌套深度,thread-local 化避免跨线程串台。"""
        return getattr(self._local, "transaction_depth", 0)

    @_transaction_depth.setter
    def _transaction_depth(self, value: int) -> None:
        self._local.transaction_depth = value

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
        # PRAGMA 已在 conn property 中每连接执行,这里只建表
        with self._lock:
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

            -- Phase 5性能:高频WHERE条件索引
            CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
            CREATE INDEX IF NOT EXISTS idx_assets_novel_id ON assets(novel_id);
            CREATE INDEX IF NOT EXISTS idx_sources_novel_id ON sources(novel_id);
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
        # 迁移：创建偏好画像与推书相关表
        self._migrate_preference_tables()
        # 迁移：创建 AI 写作项目（章节/伏笔/状态记忆）相关表
        self._migrate_ai_writing_tables()
        # 迁移：创建阅读进度追踪表
        self._migrate_reading_progress_table()
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
        """更新FTS索引。注意:与upsert_novel分离调用时存在漂移风险(一个成功一个失败)。
        Phase 5批量事务化后自然原子化。当前调用方(sync_engine.py:1632)未封装事务。"""
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

    def novel_archive_complete(self, novel_id: int, require_assets: bool = False) -> bool:
        """检查小说是否已达到可跳过同步的最低完整度。

        元数据本身不足以代表归档完成；正文必须存在。启用资源下载时，
        如果元数据里有封面 URL，还要求封面已成功记录到 assets 表。
        """
        row = self.conn.execute(
            """
            SELECT n.cover_url, nt.text_raw
            FROM novels n
            JOIN novel_texts nt ON nt.novel_id = n.novel_id
            WHERE n.novel_id = ?
            """,
            (novel_id,),
        ).fetchone()
        if row is None or not str(row["text_raw"] or "").strip():
            return False
        cover_url = str(row["cover_url"] or "").strip()
        if require_assets and cover_url:
            asset = self.conn.execute(
                """
                SELECT 1
                FROM assets
                WHERE novel_id = ? AND remote_url = ? AND file_hash IS NOT NULL AND file_hash != ''
                LIMIT 1
                """,
                (novel_id, cover_url),
            ).fetchone()
            if asset is None:
                return False
        return True

    def count_series_novel_texts(self, series_id: int) -> int:
        """统计本地已保存正文的系列章节数。"""
        row = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM novels n
            JOIN novel_texts nt ON nt.novel_id = n.novel_id
            WHERE n.series_id = ?
            """,
            (series_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def count_series_complete_novels(self, series_id: int, require_assets: bool = False) -> int:
        """统计系列里达到可跳过同步完整度的章节数。"""
        if not require_assets:
            return self.count_series_novel_texts(series_id)
        row = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM novels n
            JOIN novel_texts nt ON nt.novel_id = n.novel_id
            WHERE n.series_id = ?
              AND nt.text_raw IS NOT NULL
              AND nt.text_raw != ''
              AND (
                    n.cover_url IS NULL
                    OR n.cover_url = ''
                    OR EXISTS (
                        SELECT 1
                        FROM assets a
                        WHERE a.novel_id = n.novel_id
                          AND a.remote_url = n.cover_url
                          AND a.file_hash IS NOT NULL
                          AND a.file_hash != ''
                    )
                  )
            """,
            (series_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def list_series_novel_texts(self, series_id: int) -> list[dict[str, Any]]:
        """按创建时间升序列出系列下所有小说正文（含 title/text_raw/text_markdown/create_date）。"""
        rows = self.conn.execute(
            """
            SELECT nt.text_raw, nt.text_markdown, n.title, n.create_date
            FROM novels n
            LEFT JOIN novel_texts nt ON nt.novel_id = n.novel_id
            WHERE n.series_id = ?
            ORDER BY n.create_date ASC
            """,
            (series_id,),
        ).fetchall()
        return [dict(row) for row in rows]

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
            where_clauses.append("n.novel_id IN (SELECT novel_id FROM novel_fts WHERE novel_fts MATCH ?)")
            params.append(search)

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
                CASE WHEN n.series_id IS NULL THEN 'single' ELSE 'series' END AS novel_kind,
                rp.status AS reading_status,
                rp.progress AS reading_progress
            FROM novels AS n
            LEFT JOIN users AS u ON u.user_id = n.user_id
            {where_sql}
            LEFT JOIN reading_progress AS rp ON rp.novel_id = n.novel_id
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
                n.raw_json,
                rp.status AS reading_status,
                rp.progress AS reading_progress,
                rp.last_read_at
            FROM novels AS n
            LEFT JOIN users AS u ON u.user_id = n.user_id
            LEFT JOIN novel_texts AS nt ON nt.novel_id = n.novel_id
            LEFT JOIN reading_progress AS rp ON rp.novel_id = n.novel_id
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

    def list_novel_archive_refs(
        self,
        novel_ids: list[int] | None = None,
        user_id: int | None = None,
        series_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """列出删除本地归档文件所需的小说元数据和已记录资源路径。"""
        where_clauses: list[str] = []
        params: list[Any] = []
        if novel_ids is not None:
            if not novel_ids:
                return []
            placeholders = ",".join(["?"] * len(novel_ids))
            where_clauses.append(f"n.novel_id IN ({placeholders})")
            params.extend(int(nid) for nid in novel_ids)
        if user_id is not None:
            where_clauses.append("n.user_id = ?")
            params.append(int(user_id))
        if series_id is not None:
            where_clauses.append("n.series_id = ?")
            params.append(int(series_id))
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT
                n.novel_id,
                n.restrict_value,
                n.user_id,
                COALESCE(u.name, 'unknown') AS author_name,
                n.title,
                GROUP_CONCAT(a.local_path, char(10)) AS asset_paths
            FROM novels n
            LEFT JOIN users u ON u.user_id = n.user_id
            LEFT JOIN assets a ON a.novel_id = n.novel_id
            {where_sql}
            GROUP BY n.novel_id
            """,
            params,
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw_paths = str(item.get("asset_paths") or "")
            item["asset_paths"] = [path for path in raw_paths.split("\n") if path]
            result.append(item)
        return result

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
        """关闭所有线程的连接。多线程场景下,每线程都可能有独立连接。"""
        with self._lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        # 清理当前线程的 local 状态
        if hasattr(self._local, "conn"):
            self._local.conn = None

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
            params_count.extend([search_pattern, search, search_pattern])

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
            params_query.extend([search_pattern, search, search_pattern])
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
                u.raw_json AS author_raw_json,
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
        # 6.7: 提取作者头像
        for item in items:
            raw_json = item.pop("author_raw_json", None)
            if raw_json:
                item["author_avatar"] = self._extract_user_avatar(self._load_raw_json(raw_json))
            else:
                item["author_avatar"] = None
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
        with self.transaction():
                self.conn.execute("DELETE FROM novel_texts WHERE novel_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM assets WHERE novel_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM sources WHERE novel_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM novel_fts WHERE novel_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM novels WHERE novel_id = ?", (novel_id,))
                # Purge satellite rows so a deleted novel can't resurface as a
                # "new" recommendation or linger in the preflight/pending tables.
                self.conn.execute("DELETE FROM sync_check_list WHERE novel_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM pending_deletions WHERE item_type = 'novel' AND item_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM recommendation_items WHERE novel_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM recommendation_feedback WHERE novel_id = ?", (novel_id,))

    def delete_user(self, user_id: int) -> None:
        """删除用户及其所有小说（单一事务，批量删除）"""
        with self.transaction():
                self.conn.execute("DELETE FROM novel_texts WHERE novel_id IN (SELECT novel_id FROM novels WHERE user_id = ?)", (user_id,))
                self.conn.execute("DELETE FROM assets WHERE novel_id IN (SELECT novel_id FROM novels WHERE user_id = ?)", (user_id,))
                self.conn.execute("DELETE FROM sources WHERE novel_id IN (SELECT novel_id FROM novels WHERE user_id = ?)", (user_id,))
                self.conn.execute("DELETE FROM novel_fts WHERE novel_id IN (SELECT novel_id FROM novels WHERE user_id = ?)", (user_id,))
                self.conn.execute("DELETE FROM sync_check_list WHERE novel_id IN (SELECT novel_id FROM novels WHERE user_id = ?)", (user_id,))
                self.conn.execute("DELETE FROM recommendation_items WHERE novel_id IN (SELECT novel_id FROM novels WHERE user_id = ?)", (user_id,))
                self.conn.execute("DELETE FROM novels WHERE user_id = ?", (user_id,))
                self.conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
                self.conn.execute("DELETE FROM recommendation_feedback WHERE author_id = ?", (user_id,))
                self.conn.execute("DELETE FROM pending_deletions WHERE item_type = 'user' AND item_id = ?", (user_id,))

    def delete_series(self, series_id: int) -> None:
        """删除系列（不删除小说，只解除关联）"""
        with self.transaction():
                self.conn.execute("UPDATE novels SET series_id = NULL WHERE series_id = ?", (series_id,))
                self.conn.execute("DELETE FROM recommendation_items WHERE item_type = 'series' AND series_id = ?", (series_id,))
                self.conn.execute("DELETE FROM recommendation_feedback WHERE series_id = ?", (series_id,))
                self.conn.execute("DELETE FROM pending_deletions WHERE item_type = 'series' AND item_id = ?", (series_id,))
                self.conn.execute("DELETE FROM series WHERE series_id = ?", (series_id,))

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
            return int(cursor.lastrowid)

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
                try:
                    item["stats"] = json.loads(item["stats_json"])
                except (TypeError, ValueError):
                    item["stats"] = None
            if item.get("logs_json"):
                try:
                    item["logs"] = json.loads(item["logs_json"])
                except (TypeError, ValueError):
                    item["logs"] = None
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

    def get_existing_novel_ids(self, novel_ids: list[int], require_assets: bool = False) -> set[int]:
        """批量检查小说是否已完整归档，返回可安全跳过的 ID 集合。"""
        if not novel_ids:
            return set()
        result: set[int] = set()
        batch_size = 500
        for i in range(0, len(novel_ids), batch_size):
            batch = novel_ids[i:i + batch_size]
            placeholders = ",".join(["?"] * len(batch))
            if require_assets:
                rows = self.conn.execute(
                    f"""
                    SELECT n.novel_id
                    FROM novels n
                    JOIN novel_texts nt ON nt.novel_id = n.novel_id
                    WHERE n.novel_id IN ({placeholders})
                      AND nt.text_raw IS NOT NULL
                      AND nt.text_raw != ''
                      AND (
                            n.cover_url IS NULL
                            OR n.cover_url = ''
                            OR EXISTS (
                                SELECT 1
                                FROM assets a
                                WHERE a.novel_id = n.novel_id
                                  AND a.remote_url = n.cover_url
                                  AND a.file_hash IS NOT NULL
                                  AND a.file_hash != ''
                            )
                          )
                    """,
                    batch,
                ).fetchall()
            else:
                rows = self.conn.execute(
                    f"""
                    SELECT n.novel_id
                    FROM novels n
                    JOIN novel_texts nt ON nt.novel_id = n.novel_id
                    WHERE n.novel_id IN ({placeholders})
                      AND nt.text_raw IS NOT NULL
                      AND nt.text_raw != ''
                    """,
                    batch,
                ).fetchall()
            result.update(row[0] for row in rows)
        return result

    # ── 偏好画像与推书 ──────────────────────────────────────────────

    def _migrate_preference_tables(self) -> None:
        """创建偏好画像与推荐相关表。"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS preference_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                source_scope_json TEXT NOT NULL,
                stats_json TEXT NOT NULL,
                profile_json TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS recommendation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                search_plan_json TEXT NOT NULL,
                stats_json TEXT,
                error_message TEXT,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS recommendation_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                profile_id INTEGER NOT NULL,
                item_type TEXT NOT NULL,
                novel_id INTEGER,
                series_id INTEGER,
                title TEXT NOT NULL,
                author_id INTEGER,
                author_name TEXT,
                caption TEXT,
                tags_json TEXT NOT NULL,
                text_length INTEGER NOT NULL DEFAULT 0,
                series_total_text_length INTEGER NOT NULL DEFAULT 0,
                series_total_novels INTEGER NOT NULL DEFAULT 0,
                total_bookmarks INTEGER NOT NULL DEFAULT 0,
                total_views INTEGER NOT NULL DEFAULT 0,
                score REAL NOT NULL DEFAULT 0,
                reason TEXT,
                matched_json TEXT NOT NULL,
                source_query TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_recommendation_items_identity
              ON recommendation_items(item_type, COALESCE(novel_id, 0), COALESCE(series_id, 0));
            CREATE INDEX IF NOT EXISTS idx_recommendation_items_status ON recommendation_items(status);
            CREATE INDEX IF NOT EXISTS idx_recommendation_items_score ON recommendation_items(score DESC);

            CREATE TABLE IF NOT EXISTS recommendation_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type TEXT NOT NULL,
                novel_id INTEGER,
                series_id INTEGER,
                author_id INTEGER,
                feedback_type TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS recommendation_mutes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mute_type TEXT NOT NULL,
                mute_value TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(mute_type, mute_value)
            );
            """
        )

    def _row_to_preference_profile(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for source, target, fallback in (
            ("source_scope_json", "source_scope", {}),
            ("stats_json", "stats", {}),
            ("profile_json", "profile", {}),
        ):
            try:
                item[target] = json.loads(item.get(source) or "")
            except (TypeError, ValueError):
                item[target] = fallback
            item.pop(source, None)
        item["is_default"] = bool(item.get("is_default"))
        return item

    def list_preference_profiles(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM preference_profiles ORDER BY is_default DESC, updated_at DESC").fetchall()
        return [self._row_to_preference_profile(row) for row in rows]

    def get_preference_profile(self, profile_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM preference_profiles WHERE id = ?", (profile_id,)).fetchone()
        return self._row_to_preference_profile(row) if row else None

    def get_default_preference_profile(self) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM preference_profiles WHERE is_default = 1 ORDER BY updated_at DESC LIMIT 1").fetchone()
        return self._row_to_preference_profile(row) if row else None

    def create_preference_profile(self, data: dict[str, Any]) -> int:
        with self._lock:
            if data.get("is_default"):
                self.conn.execute("UPDATE preference_profiles SET is_default = 0")
            cursor = self.conn.execute(
                """
                INSERT INTO preference_profiles (name, description, source_scope_json, stats_json, profile_json, is_default)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("name") or "未命名偏好画像",
                    data.get("description"),
                    json.dumps(data.get("source_scope") or {}, ensure_ascii=False),
                    json.dumps(data.get("stats") or {}, ensure_ascii=False),
                    json.dumps(data.get("profile") or {}, ensure_ascii=False),
                    1 if data.get("is_default") else 0,
                ),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_preference_profile(self, profile_id: int, data: dict[str, Any]) -> None:
        fields: list[str] = []
        params: list[Any] = []
        for key in ("name", "description"):
            if key in data:
                fields.append(f"{key} = ?")
                params.append(data[key])
        for key, column in (("source_scope", "source_scope_json"), ("stats", "stats_json"), ("profile", "profile_json")):
            if key in data:
                fields.append(f"{column} = ?")
                params.append(json.dumps(data[key] or {}, ensure_ascii=False))
        if "is_default" in data:
            fields.append("is_default = ?")
            params.append(1 if data["is_default"] else 0)
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(profile_id)
        with self._lock:
            if data.get("is_default"):
                self.conn.execute("UPDATE preference_profiles SET is_default = 0 WHERE id != ?", (profile_id,))
            self.conn.execute(f"UPDATE preference_profiles SET {', '.join(fields)} WHERE id = ?", params)
            self._commit_if_needed()

    def set_default_preference_profile(self, profile_id: int) -> None:
        with self._lock:
            self.conn.execute("UPDATE preference_profiles SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END", (profile_id,))
            self._commit_if_needed()

    def delete_preference_profile(self, profile_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM preference_profiles WHERE id = ?", (profile_id,))
            self._commit_if_needed()

    def fetch_preference_source_rows(self, min_text_length: int = 1000, limit: int = 0) -> list[dict[str, Any]]:
        sql = """
            SELECT n.novel_id, n.title, n.caption, n.user_id, n.series_id, n.text_length,
                   n.total_bookmarks, n.total_views, n.tags_json, n.x_restrict, n.create_date,
                   u.name AS author_name, nt.text_raw,
                   GROUP_CONCAT(s.source_type) AS source_types
            FROM novels n
            LEFT JOIN users u ON u.user_id = n.user_id
            LEFT JOIN novel_texts nt ON nt.novel_id = n.novel_id
            LEFT JOIN sources s ON s.novel_id = n.novel_id
            WHERE n.text_length >= ? AND nt.text_raw IS NOT NULL AND nt.text_raw != ''
            GROUP BY n.novel_id
            ORDER BY n.total_bookmarks DESC, n.text_length DESC
        """
        params: list[Any] = [int(min_text_length)]
        if limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def create_recommendation_run(self, profile_id: int, search_plan: dict[str, Any], status: str = "running") -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO recommendation_runs (profile_id, status, search_plan_json)
                VALUES (?, ?, ?)
                """,
                (profile_id, status, json.dumps(search_plan, ensure_ascii=False)),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_recommendation_run(self, run_id: int, status: str, stats: dict[str, Any] | None = None, error_message: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                """
                UPDATE recommendation_runs
                SET status = ?, stats_json = ?, error_message = ?, finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, json.dumps(stats or {}, ensure_ascii=False), error_message, run_id),
            )
            self._commit_if_needed()

    def _row_to_recommendation_run(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for source, target in (("search_plan_json", "search_plan"), ("stats_json", "stats")):
            try:
                item[target] = json.loads(item.get(source) or "{}")
            except (TypeError, ValueError):
                item[target] = {}
            item.pop(source, None)
        return item

    def list_recommendation_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM recommendation_runs ORDER BY started_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [self._row_to_recommendation_run(row) for row in rows]

    def get_recommendation_run(self, run_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM recommendation_runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_recommendation_run(row) if row else None

    def _row_to_recommendation_item(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for source, target, fallback in (("tags_json", "tags", []), ("matched_json", "matched", {})):
            try:
                item[target] = json.loads(item.get(source) or "")
            except (TypeError, ValueError):
                item[target] = fallback
            item.pop(source, None)
        return item

    def upsert_recommendation_item(self, data: dict[str, Any]) -> int:
        item_type = data["item_type"]
        novel_id = data.get("novel_id")
        series_id = data.get("series_id")
        values = (
            int(data["run_id"]), int(data["profile_id"]), item_type, novel_id, series_id,
            data.get("title") or "未命名", data.get("author_id"), data.get("author_name"), data.get("caption"),
            json.dumps(data.get("tags") or [], ensure_ascii=False), int(data.get("text_length") or 0),
            int(data.get("series_total_text_length") or 0), int(data.get("series_total_novels") or 0),
            int(data.get("total_bookmarks") or 0), int(data.get("total_views") or 0), float(data.get("score") or 0),
            data.get("reason"), json.dumps(data.get("matched") or {}, ensure_ascii=False), data.get("source_query"),
            data.get("status") or "new",
        )
        with self._lock:
            existing = self.conn.execute(
                """
                SELECT id FROM recommendation_items
                WHERE item_type = ? AND COALESCE(novel_id, 0) = ? AND COALESCE(series_id, 0) = ?
                """,
                (item_type, int(novel_id or 0), int(series_id or 0)),
            ).fetchone()
            if existing:
                item_id = int(existing[0])
                self.conn.execute(
                    """
                    UPDATE recommendation_items SET
                        run_id = ?, profile_id = ?, item_type = ?, novel_id = ?, series_id = ?, title = ?,
                        author_id = ?, author_name = ?, caption = ?, tags_json = ?, text_length = ?,
                        series_total_text_length = ?, series_total_novels = ?, total_bookmarks = ?, total_views = ?,
                        score = ?, reason = ?, matched_json = ?, source_query = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    values[:-1] + (item_id,),
                )
            else:
                cursor = self.conn.execute(
                    """
                    INSERT INTO recommendation_items (
                        run_id, profile_id, item_type, novel_id, series_id, title, author_id, author_name,
                        caption, tags_json, text_length, series_total_text_length, series_total_novels,
                        total_bookmarks, total_views, score, reason, matched_json, source_query, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                item_id = int(cursor.lastrowid)
            self._commit_if_needed()
            return item_id

    def list_recommendation_items(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM recommendation_items"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY score DESC, updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_recommendation_item(row) for row in rows]

    def get_recommendation_item(self, item_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM recommendation_items WHERE id = ?",
            (int(item_id),),
        ).fetchone()
        return self._row_to_recommendation_item(row) if row else None

    def update_recommendation_item_status(self, item_id: int, status: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE recommendation_items SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, item_id))
            self._commit_if_needed()

    def create_recommendation_feedback(self, data: dict[str, Any]) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO recommendation_feedback (item_type, novel_id, series_id, author_id, feedback_type, note)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (data["item_type"], data.get("novel_id"), data.get("series_id"), data.get("author_id"), data["feedback_type"], data.get("note")),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def list_recommendation_mutes(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM recommendation_mutes ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def create_recommendation_mute(self, mute_type: str, mute_value: str, reason: str | None = None) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO recommendation_mutes (mute_type, mute_value, reason)
                VALUES (?, ?, ?)
                ON CONFLICT(mute_type, mute_value) DO UPDATE SET reason = excluded.reason
                """,
                (mute_type, mute_value, reason),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def delete_recommendation_mute(self, mute_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM recommendation_mutes WHERE id = ?", (mute_id,))
            self._commit_if_needed()

    def get_recommendation_filter_state(self) -> dict[str, Any]:
        archived_ids = {int(row[0]) for row in self.conn.execute("SELECT novel_id FROM novels").fetchall()}
        recommended_ids = {int(row[0]) for row in self.conn.execute("SELECT novel_id FROM recommendation_items WHERE novel_id IS NOT NULL").fetchall()}
        dismissed_ids = {int(row[0]) for row in self.conn.execute("SELECT novel_id FROM recommendation_items WHERE novel_id IS NOT NULL AND status IN ('dismissed', 'muted')").fetchall()}
        mutes = self.list_recommendation_mutes()
        return {
            "archived_novel_ids": archived_ids,
            "recommended_novel_ids": recommended_ids,
            "dismissed_novel_ids": dismissed_ids,
            "muted_authors": {str(item["mute_value"]) for item in mutes if item["mute_type"] == "author"},
            "muted_tags": {str(item["mute_value"]) for item in mutes if item["mute_type"] == "tag"},
        }

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
                context_window INTEGER NOT NULL DEFAULT 128000,
                stream_enabled INTEGER NOT NULL DEFAULT 1,
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

            CREATE TABLE IF NOT EXISTS ai_prompt_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                template TEXT NOT NULL,
                description TEXT,
                is_builtin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_ai_agents_task_type ON ai_agents(task_type);
            CREATE INDEX IF NOT EXISTS idx_ai_agents_provider_id ON ai_agents(provider_id);
            CREATE INDEX IF NOT EXISTS idx_ai_jobs_job_id ON ai_jobs(job_id);
            CREATE INDEX IF NOT EXISTS idx_ai_jobs_created_at ON ai_jobs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_drafts_updated_at ON ai_drafts(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_documents_hash ON ai_documents(content_hash);
            CREATE INDEX IF NOT EXISTS idx_ai_prompt_templates_category ON ai_prompt_templates(category);
            """
        )
        # 迁移：为已有 ai_providers 表添加 context_window 列
        try:
            self.conn.execute("ALTER TABLE ai_providers ADD COLUMN context_window INTEGER NOT NULL DEFAULT 128000")
            self.conn.commit()
        except Exception:
            pass  # 列已存在则忽略
        # 迁移：为已有 ai_providers 表添加 stream_enabled 列
        try:
            self.conn.execute("ALTER TABLE ai_providers ADD COLUMN stream_enabled INTEGER NOT NULL DEFAULT 1")
            self.conn.commit()
        except Exception:
            pass

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
                    available_models_json, timeout_seconds, max_retries, proxy, context_window, stream_enabled, enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    int(data.get("context_window") or 128000),
                    1 if data.get("stream_enabled", True) else 0,
                    1 if data.get("enabled", True) else 0,
                ),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_ai_provider(self, provider_id: int, data: dict[str, Any]) -> None:
        allowed = {
            "name", "provider_type", "base_url", "api_key_encrypted", "default_model",
            "available_models", "timeout_seconds", "max_retries", "proxy", "context_window", "stream_enabled", "enabled",
        }
        fields: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key not in data:
                continue
            column = "available_models_json" if key == "available_models" else key
            value = json.dumps(data[key] or [], ensure_ascii=False) if key == "available_models" else data[key]
            if key in ("enabled", "stream_enabled"):
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

    # ── ai_jobs 补全 ────────────────────────────────────────────

    def list_ai_jobs(self, task_type: str | None = None, status: str | None = None,
                     page: int = 1, page_size: int = 20) -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(page_size, 1)
        conditions: list[str] = []
        params: list[Any] = []
        if task_type:
            conditions.append("task_type = ?")
            params.append(task_type)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        total = int(self.conn.execute(f"SELECT COUNT(*) FROM ai_jobs {where}", params).fetchone()[0])
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            f"SELECT * FROM ai_jobs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key in ("input_json", "output_json"):
                if item.get(key):
                    try:
                        item[key[:-5]] = json.loads(item[key])
                    except (TypeError, ValueError):
                        item[key[:-5]] = None
            items.append(item)
        return {"items": items, "page": page, "page_size": page_size, "total": total, "total_pages": total_pages}

    def delete_ai_job(self, job_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_jobs WHERE job_id = ?", (job_id,))
            self._commit_if_needed()

    def cleanup_ai_jobs(self, keep_days: int = 30, keep_failed_days: int | None = None) -> int:
        """清理 ai_jobs：默认保留最近 30 天的已完成任务，失败任务可单独配置保留天数。

        返回删除的行数。
        """
        if keep_failed_days is None:
            keep_failed_days = keep_days
        with self._lock:
            cur = self.conn.execute(
                """
                DELETE FROM ai_jobs
                WHERE (status IN ('succeeded', 'done', 'completed', 'success')
                       AND created_at < datetime('now', ? || ' days'))
                   OR (status IN ('failed', 'error', 'cancelled')
                       AND created_at < datetime('now', ? || ' days'))
                """,
                (f"-{int(keep_days)}", f"-{int(keep_failed_days)}"),
            )
            deleted = cur.rowcount or 0
            self._commit_if_needed()
        return int(deleted)

    def fail_stale_ai_jobs(self, older_than_minutes: int = 30) -> int:
        """把卡在 'running' 且早于阈值的 AI job 标记为 failed。

        客户端断连/进程重启会让 SSE 任务永远停留在 'running'，UI 一直转圈且
        cleanup_ai_jobs 也不会回收。建议在服务启动时调用一次做对账。返回修复行数。
        """
        with self._lock:
            cur = self.conn.execute(
                """
                UPDATE ai_jobs
                SET status = 'failed',
                    error_message = COALESCE(NULLIF(error_message, ''), '任务中断（服务重启时检测到未完成）'),
                    finished_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
                  AND created_at < datetime('now', ? || ' minutes')
                """,
                (f"-{int(older_than_minutes)}",),
            )
            fixed = cur.rowcount or 0
            self._commit_if_needed()
        return int(fixed)

    # ── ai_style_profiles ───────────────────────────────────────

    def _row_to_ai_style_profile(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        if item.get("profile_json"):
            try:
                item["profile"] = json.loads(item["profile_json"])
            except (TypeError, ValueError):
                item["profile"] = {}
        if item.get("source_ids_json"):
            try:
                item["source_ids"] = json.loads(item["source_ids_json"])
            except (TypeError, ValueError):
                item["source_ids"] = []
        item.pop("profile_json", None)
        item.pop("source_ids_json", None)
        return item

    def list_ai_style_profiles(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(page_size, 1)
        total = int(self.conn.execute("SELECT COUNT(*) FROM ai_style_profiles").fetchone()[0])
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            "SELECT * FROM ai_style_profiles ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
        return {"items": [self._row_to_ai_style_profile(row) for row in rows], "page": page, "page_size": page_size, "total": total, "total_pages": total_pages}

    def get_ai_style_profile(self, profile_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ai_style_profiles WHERE id = ?", (profile_id,)).fetchone()
        return self._row_to_ai_style_profile(row) if row else None

    def create_ai_style_profile(self, data: dict[str, Any]) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO ai_style_profiles (name, source_type, source_ids_json, profile_json, sample_prompt)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    data.get("name"),
                    data.get("source_type"),
                    json.dumps(data.get("source_ids") or [], ensure_ascii=False),
                    json.dumps(data.get("profile") or {}, ensure_ascii=False),
                    data.get("sample_prompt"),
                ),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_ai_style_profile(self, profile_id: int, data: dict[str, Any]) -> None:
        allowed = {"name", "source_type", "sample_prompt"}
        fields: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key in data:
                fields.append(f"{key} = ?")
                params.append(data[key])
        if "source_ids" in data:
            fields.append("source_ids_json = ?")
            params.append(json.dumps(data["source_ids"] or [], ensure_ascii=False))
        if "profile" in data:
            fields.append("profile_json = ?")
            params.append(json.dumps(data["profile"] or {}, ensure_ascii=False))
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(profile_id)
        with self._lock:
            self.conn.execute(f"UPDATE ai_style_profiles SET {', '.join(fields)} WHERE id = ?", params)
            self._commit_if_needed()

    def delete_ai_style_profile(self, profile_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_style_profiles WHERE id = ?", (profile_id,))
            self._commit_if_needed()

    # ── ai_novel_profiles ───────────────────────────────────────

    def _row_to_ai_novel_profile(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        if item.get("profile_json"):
            try:
                item["profile"] = json.loads(item["profile_json"])
            except (TypeError, ValueError):
                item["profile"] = {}
        if item.get("source_ids_json"):
            try:
                item["source_ids"] = json.loads(item["source_ids_json"])
            except (TypeError, ValueError):
                item["source_ids"] = []
        item.pop("profile_json", None)
        item.pop("source_ids_json", None)
        return item

    def list_ai_novel_profiles(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(page_size, 1)
        total = int(self.conn.execute("SELECT COUNT(*) FROM ai_novel_profiles").fetchone()[0])
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            "SELECT * FROM ai_novel_profiles ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
        return {"items": [self._row_to_ai_novel_profile(row) for row in rows], "page": page, "page_size": page_size, "total": total, "total_pages": total_pages}

    def get_ai_novel_profile(self, profile_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ai_novel_profiles WHERE id = ?", (profile_id,)).fetchone()
        return self._row_to_ai_novel_profile(row) if row else None

    def create_ai_novel_profile(self, data: dict[str, Any]) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO ai_novel_profiles (name, source_type, source_ids_json, profile_json, continuation_prompt)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    data.get("name"),
                    data.get("source_type"),
                    json.dumps(data.get("source_ids") or [], ensure_ascii=False),
                    json.dumps(data.get("profile") or {}, ensure_ascii=False),
                    data.get("continuation_prompt"),
                ),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_ai_novel_profile(self, profile_id: int, data: dict[str, Any]) -> None:
        allowed = {"name", "source_type", "continuation_prompt"}
        fields: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key in data:
                fields.append(f"{key} = ?")
                params.append(data[key])
        if "source_ids" in data:
            fields.append("source_ids_json = ?")
            params.append(json.dumps(data["source_ids"] or [], ensure_ascii=False))
        if "profile" in data:
            fields.append("profile_json = ?")
            params.append(json.dumps(data["profile"] or {}, ensure_ascii=False))
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(profile_id)
        with self._lock:
            self.conn.execute(f"UPDATE ai_novel_profiles SET {', '.join(fields)} WHERE id = ?", params)
            self._commit_if_needed()

    def delete_ai_novel_profile(self, profile_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_novel_profiles WHERE id = ?", (profile_id,))
            self._commit_if_needed()

    # ── ai_documents 补全 ───────────────────────────────────────

    def list_ai_documents(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(page_size, 1)
        total = int(self.conn.execute("SELECT COUNT(*) FROM ai_documents").fetchone()[0])
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            "SELECT id, title, source_type, content_hash, created_at FROM ai_documents ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
        return {"items": [dict(row) for row in rows], "page": page, "page_size": page_size, "total": total, "total_pages": total_pages}

    def delete_ai_document(self, document_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_documents WHERE id = ?", (document_id,))
            self._commit_if_needed()

    # ── ai_prompt_templates ─────────────────────────────────────

    def list_ai_prompt_templates(self, category: str | None = None) -> list[dict[str, Any]]:
        if category:
            rows = self.conn.execute("SELECT * FROM ai_prompt_templates WHERE category = ? ORDER BY is_builtin DESC, id DESC", (category,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM ai_prompt_templates ORDER BY is_builtin DESC, id DESC").fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["is_builtin"] = bool(item.get("is_builtin"))
            items.append(item)
        return items

    def get_ai_prompt_template(self, template_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ai_prompt_templates WHERE id = ?", (template_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["is_builtin"] = bool(item.get("is_builtin"))
        return item

    def create_ai_prompt_template(self, data: dict[str, Any]) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO ai_prompt_templates (name, category, template, description, is_builtin)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    data.get("name"),
                    data.get("category", "general"),
                    data.get("template"),
                    data.get("description"),
                    1 if data.get("is_builtin") else 0,
                ),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_ai_prompt_template(self, template_id: int, data: dict[str, Any]) -> None:
        allowed = {"name", "category", "template", "description"}
        fields: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key in data:
                fields.append(f"{key} = ?")
                params.append(data[key])
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(template_id)
        with self._lock:
            self.conn.execute(f"UPDATE ai_prompt_templates SET {', '.join(fields)} WHERE id = ?", params)
            self._commit_if_needed()

    def delete_ai_prompt_template(self, template_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_prompt_templates WHERE id = ?", (template_id,))
            self._commit_if_needed()

    # ── ai_drafts 补全 ──────────────────────────────────────────

    def get_ai_draft_history(self, draft_id: int) -> list[dict[str, Any]]:
        """递归获取草稿版本链（从当前回溯到根）。"""
        chain: list[dict[str, Any]] = []
        visited: set[int] = set()
        current_id: int | None = draft_id
        while current_id is not None and current_id not in visited:
            visited.add(current_id)
            row = self.conn.execute("SELECT * FROM ai_drafts WHERE id = ?", (current_id,)).fetchone()
            if row is None:
                break
            chain.append(dict(row))
            current_id = row["parent_draft_id"]
        return chain

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

    # ══════════════════════════════════════════════════════════════
    # AI 写作项目 / 章节 / 伏笔 / 状态记忆
    # ══════════════════════════════════════════════════════════════

    def _migrate_ai_writing_tables(self) -> None:
        """创建 AI 写作项目相关表（项目、章节、伏笔、状态记忆、对话向导）。"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_writing_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                outline_json TEXT,
                style_profile_id INTEGER,
                novel_profile_id INTEGER,
                settings_json TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                chapter_number INTEGER NOT NULL,
                title TEXT,
                content TEXT,
                summary TEXT,
                key_events_json TEXT,
                outline TEXT,
                word_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'draft',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, chapter_number)
            );

            CREATE TABLE IF NOT EXISTS ai_foreshadows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                planted_chapter INTEGER,
                target_resolve_chapter INTEGER,
                resolved_chapter INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                importance TEXT NOT NULL DEFAULT 'normal',
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_project_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                state_type TEXT NOT NULL,
                content TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, state_type)
            );

            CREATE TABLE IF NOT EXISTS ai_chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id INTEGER,
                scope TEXT NOT NULL DEFAULT 'wizard',
                title TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'active',
                imported_project_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_ai_chapters_project ON ai_chapters(project_id, chapter_number);
            CREATE INDEX IF NOT EXISTS idx_ai_foreshadows_project ON ai_foreshadows(project_id, status);
            CREATE INDEX IF NOT EXISTS idx_ai_project_states_project ON ai_project_states(project_id);
            CREATE INDEX IF NOT EXISTS idx_ai_chat_sessions_scope ON ai_chat_sessions(scope, status);
            CREATE INDEX IF NOT EXISTS idx_ai_chat_messages_session ON ai_chat_messages(session_id);
            """
        )
        # 给已有 ai_chapters 表补 metadata_json 列（老库迁移）
        try:
            cols = {row[1] for row in self.conn.execute("PRAGMA table_info(ai_chapters)").fetchall()}
            if "metadata_json" not in cols:
                self.conn.execute("ALTER TABLE ai_chapters ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
        except sqlite3.OperationalError:
            pass

    # ── ai_writing_projects CRUD ───────────────────────────────────

    def list_ai_writing_projects(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                """SELECT p.*, (SELECT COUNT(*) FROM ai_chapters c WHERE c.project_id = p.id) AS chapter_count,
                   (SELECT COALESCE(SUM(c.word_count), 0) FROM ai_chapters c WHERE c.project_id = p.id) AS total_words
                   FROM ai_writing_projects p WHERE p.status = ? ORDER BY p.updated_at DESC""",
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT p.*, (SELECT COUNT(*) FROM ai_chapters c WHERE c.project_id = p.id) AS chapter_count,
                   (SELECT COALESCE(SUM(c.word_count), 0) FROM ai_chapters c WHERE c.project_id = p.id) AS total_words
                   FROM ai_writing_projects p ORDER BY p.updated_at DESC"""
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            if item.get("outline_json"):
                try:
                    item["outline"] = json.loads(item["outline_json"])
                except (TypeError, ValueError):
                    item["outline"] = None
            else:
                item["outline"] = None
            item.pop("outline_json", None)
            if item.get("settings_json"):
                try:
                    item["settings"] = json.loads(item["settings_json"])
                except (TypeError, ValueError):
                    item["settings"] = {}
            else:
                item["settings"] = {}
            item.pop("settings_json", None)
            results.append(item)
        return results

    def get_ai_writing_project(self, project_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ai_writing_projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        if item.get("outline_json"):
            try:
                item["outline"] = json.loads(item["outline_json"])
            except (TypeError, ValueError):
                item["outline"] = None
        else:
            item["outline"] = None
        item.pop("outline_json", None)
        if item.get("settings_json"):
            try:
                item["settings"] = json.loads(item["settings_json"])
            except (TypeError, ValueError):
                item["settings"] = {}
        else:
            item["settings"] = {}
        item.pop("settings_json", None)
        return item

    def create_ai_writing_project(self, data: dict[str, Any]) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """INSERT INTO ai_writing_projects (name, description, outline_json, style_profile_id, novel_profile_id, settings_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    data.get("name"),
                    data.get("description"),
                    json.dumps(data["outline"], ensure_ascii=False) if data.get("outline") else None,
                    data.get("style_profile_id"),
                    data.get("novel_profile_id"),
                    json.dumps(data.get("settings") or {}, ensure_ascii=False),
                ),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_ai_writing_project(self, project_id: int, data: dict[str, Any]) -> None:
        allowed = {"name", "description", "outline", "style_profile_id", "novel_profile_id", "settings", "status"}
        fields: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key not in data:
                continue
            if key == "outline":
                fields.append("outline_json = ?")
                params.append(json.dumps(data[key], ensure_ascii=False) if data[key] else None)
            elif key == "settings":
                fields.append("settings_json = ?")
                params.append(json.dumps(data[key] or {}, ensure_ascii=False))
            else:
                fields.append(f"{key} = ?")
                params.append(data[key])
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(project_id)
        with self._lock:
            self.conn.execute(f"UPDATE ai_writing_projects SET {', '.join(fields)} WHERE id = ?", params)
            self._commit_if_needed()

    def delete_ai_writing_project(self, project_id: int) -> None:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DELETE FROM ai_chapters WHERE project_id = ?", (project_id,))
                self.conn.execute("DELETE FROM ai_foreshadows WHERE project_id = ?", (project_id,))
                self.conn.execute("DELETE FROM ai_project_states WHERE project_id = ?", (project_id,))
                self.conn.execute("DELETE FROM ai_writing_projects WHERE id = ?", (project_id,))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # ── ai_chapters CRUD ───────────────────────────────────────────

    def list_ai_chapter_refs(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, chapter_number FROM ai_chapters WHERE project_id = ? ORDER BY chapter_number ASC",
            (project_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_ai_chapters(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM ai_chapters WHERE project_id = ? ORDER BY chapter_number ASC",
            (project_id,),
        ).fetchall()
        return [self._row_to_chapter(row) for row in rows]

    def get_ai_chapter(self, chapter_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ai_chapters WHERE id = ?", (chapter_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_chapter(row)

    def get_ai_chapter_by_number(self, project_id: int, chapter_number: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM ai_chapters WHERE project_id = ? AND chapter_number = ?",
            (project_id, chapter_number),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_chapter(row)

    def create_ai_chapter(self, data: dict[str, Any]) -> int:
        content = data.get("content") or ""
        with self._lock:
            cursor = self.conn.execute(
                """INSERT INTO ai_chapters (project_id, chapter_number, title, content, summary, key_events_json, outline, word_count, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data["project_id"],
                    data["chapter_number"],
                    data.get("title"),
                    content,
                    data.get("summary"),
                    json.dumps(data["key_events"], ensure_ascii=False) if data.get("key_events") else None,
                    data.get("outline"),
                    len(content),
                    data.get("status", "draft"),
                ),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_ai_chapter(self, chapter_id: int, data: dict[str, Any]) -> None:
        allowed = {"title", "content", "summary", "key_events", "outline", "status", "metadata"}
        fields: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key not in data:
                continue
            if key == "key_events":
                fields.append("key_events_json = ?")
                params.append(json.dumps(data[key], ensure_ascii=False) if data[key] else None)
            elif key == "metadata":
                fields.append("metadata_json = ?")
                params.append(json.dumps(data[key] or {}, ensure_ascii=False))
            elif key == "content":
                fields.append("content = ?")
                params.append(data[key])
                fields.append("word_count = ?")
                params.append(len(data[key] or ""))
            else:
                fields.append(f"{key} = ?")
                params.append(data[key])
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(chapter_id)
        with self._lock:
            self.conn.execute(f"UPDATE ai_chapters SET {', '.join(fields)} WHERE id = ?", params)
            self._commit_if_needed()

    def patch_ai_chapter_metadata(self, chapter_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        """对 chapter.metadata_json 做浅合并 patch。返回合并后的完整 metadata 字典。"""
        with self._lock:
            row = self.conn.execute("SELECT metadata_json FROM ai_chapters WHERE id = ?", (chapter_id,)).fetchone()
            current: dict[str, Any] = {}
            if row and row[0]:
                try:
                    current = json.loads(row[0]) or {}
                except (TypeError, ValueError):
                    current = {}
            current.update(patch or {})
            self.conn.execute(
                "UPDATE ai_chapters SET metadata_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(current, ensure_ascii=False), chapter_id),
            )
            self._commit_if_needed()
            return current

    def update_ai_chapters_outlines_and_metadata(self, updates: list[dict[str, Any]]) -> None:
        if not updates:
            return
        with self._lock:
            for item in updates:
                chapter_id = int(item.get("id") or item.get("chapter_id") or 0)
                if not chapter_id:
                    continue
                outline = item.get("outline")
                metadata = item.get("metadata") or {}
                self.conn.execute(
                    "UPDATE ai_chapters SET outline = ?, metadata_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (outline, json.dumps(metadata, ensure_ascii=False), chapter_id),
                )
            self._commit_if_needed()

    def delete_ai_chapter(self, chapter_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_chapters WHERE id = ?", (chapter_id,))
            self._commit_if_needed()

    def get_next_chapter_number(self, project_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(chapter_number), 0) + 1 FROM ai_chapters WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return int(row[0])

    def _row_to_chapter(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        if item.get("key_events_json"):
            try:
                item["key_events"] = json.loads(item["key_events_json"])
            except (TypeError, ValueError):
                item["key_events"] = []
        else:
            item["key_events"] = []
        item.pop("key_events_json", None)
        if item.get("metadata_json"):
            try:
                item["metadata"] = json.loads(item["metadata_json"])
            except (TypeError, ValueError):
                item["metadata"] = {}
        else:
            item["metadata"] = {}
        item.pop("metadata_json", None)
        return item

    # ── ai_foreshadows CRUD ────────────────────────────────────────

    def list_ai_foreshadows(self, project_id: int, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM ai_foreshadows WHERE project_id = ? AND status = ? ORDER BY planted_chapter ASC",
                (project_id, status),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM ai_foreshadows WHERE project_id = ? ORDER BY planted_chapter ASC",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_ai_foreshadow(self, foreshadow_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ai_foreshadows WHERE id = ?", (foreshadow_id,)).fetchone()
        return dict(row) if row else None

    def create_ai_foreshadow(self, data: dict[str, Any]) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """INSERT INTO ai_foreshadows (project_id, description, planted_chapter, target_resolve_chapter, status, importance, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    data["project_id"],
                    data["description"],
                    data.get("planted_chapter"),
                    data.get("target_resolve_chapter"),
                    data.get("status", "pending"),
                    data.get("importance", "normal"),
                    data.get("notes"),
                ),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_ai_foreshadow(self, foreshadow_id: int, data: dict[str, Any]) -> None:
        allowed = {"description", "planted_chapter", "target_resolve_chapter", "resolved_chapter", "status", "importance", "notes"}
        fields: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key not in data:
                continue
            fields.append(f"{key} = ?")
            params.append(data[key])
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(foreshadow_id)
        with self._lock:
            self.conn.execute(f"UPDATE ai_foreshadows SET {', '.join(fields)} WHERE id = ?", params)
            self._commit_if_needed()

    def delete_ai_foreshadow(self, foreshadow_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_foreshadows WHERE id = ?", (foreshadow_id,))
            self._commit_if_needed()

    def get_approaching_foreshadows(self, project_id: int, current_chapter: int) -> list[dict[str, Any]]:
        """获取即将到期的伏笔（当前章节 >= target - 2 且未回收）。"""
        rows = self.conn.execute(
            """SELECT * FROM ai_foreshadows
               WHERE project_id = ? AND status = 'pending'
                 AND target_resolve_chapter IS NOT NULL
                 AND target_resolve_chapter <= ?
               ORDER BY target_resolve_chapter ASC""",
            (project_id, current_chapter + 2),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_overdue_foreshadows(self, project_id: int, current_chapter: int) -> list[dict[str, Any]]:
        """获取已超期的伏笔（当前章节 > target 且未回收）。"""
        rows = self.conn.execute(
            """SELECT * FROM ai_foreshadows
               WHERE project_id = ? AND status = 'pending'
                 AND target_resolve_chapter IS NOT NULL
                 AND target_resolve_chapter < ?
               ORDER BY target_resolve_chapter ASC""",
            (project_id, current_chapter),
        ).fetchall()
        return [dict(row) for row in rows]

    # ── ai_project_states CRUD ─────────────────────────────────────

    def get_ai_project_state(self, project_id: int, state_type: str) -> str | None:
        row = self.conn.execute(
            "SELECT content FROM ai_project_states WHERE project_id = ? AND state_type = ?",
            (project_id, state_type),
        ).fetchone()
        return str(row[0]) if row else None

    def get_all_project_states(self, project_id: int) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT state_type, content FROM ai_project_states WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def upsert_ai_project_state(self, project_id: int, state_type: str, content: str) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO ai_project_states (project_id, state_type, content, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(project_id, state_type) DO UPDATE SET
                     content = excluded.content, updated_at = CURRENT_TIMESTAMP""",
                (project_id, state_type, content),
            )
            self._commit_if_needed()

    def delete_ai_project_state(self, project_id: int, state_type: str) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM ai_project_states WHERE project_id = ? AND state_type = ?",
                (project_id, state_type),
            )
            self._commit_if_needed()

    # ══════════════════════════════════════════════════════════════
    # AI 对话向导 / 多轮聊天
    # ══════════════════════════════════════════════════════════════

    def list_ai_chat_sessions(self, scope: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql = """SELECT s.*,
                   (SELECT COUNT(*) FROM ai_chat_messages m WHERE m.session_id = s.id) AS message_count,
                   (SELECT m.content FROM ai_chat_messages m WHERE m.session_id = s.id ORDER BY m.id DESC LIMIT 1) AS last_message,
                   a.name AS agent_name
                 FROM ai_chat_sessions s
                 LEFT JOIN ai_agents a ON a.id = s.agent_id
                 WHERE 1=1"""
        params: list[Any] = []
        if scope:
            sql += " AND s.scope = ?"
            params.append(scope)
        if status:
            sql += " AND s.status = ?"
            params.append(status)
        sql += " ORDER BY s.updated_at DESC"
        rows = self.conn.execute(sql, params).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["metadata"] = self._parse_json_field(item.pop("metadata_json", None), {})
            results.append(item)
        return results

    def get_ai_chat_session(self, session_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT s.*, a.name AS agent_name
               FROM ai_chat_sessions s LEFT JOIN ai_agents a ON a.id = s.agent_id
               WHERE s.id = ?""",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["metadata"] = self._parse_json_field(item.pop("metadata_json", None), {})
        return item

    def create_ai_chat_session(self, data: dict[str, Any]) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """INSERT INTO ai_chat_sessions (agent_id, scope, title, metadata_json, status)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    data.get("agent_id"),
                    data.get("scope") or "wizard",
                    data.get("title"),
                    json.dumps(data.get("metadata") or {}, ensure_ascii=False),
                    data.get("status") or "active",
                ),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_ai_chat_session(self, session_id: int, data: dict[str, Any]) -> None:
        allowed = {"agent_id", "title", "scope", "status", "imported_project_id", "metadata"}
        fields: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key not in data:
                continue
            if key == "metadata":
                fields.append("metadata_json = ?")
                params.append(json.dumps(data[key] or {}, ensure_ascii=False))
            else:
                fields.append(f"{key} = ?")
                params.append(data[key])
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(session_id)
        with self._lock:
            self.conn.execute(f"UPDATE ai_chat_sessions SET {', '.join(fields)} WHERE id = ?", params)
            self._commit_if_needed()

    def patch_ai_chat_session_metadata(self, session_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        """对 session.metadata_json 做浅合并 patch。返回合并后字典。"""
        with self._lock:
            row = self.conn.execute("SELECT metadata_json FROM ai_chat_sessions WHERE id = ?", (session_id,)).fetchone()
            current: dict[str, Any] = {}
            if row and row[0]:
                try:
                    current = json.loads(row[0]) or {}
                except (TypeError, ValueError):
                    current = {}
            current.update(patch or {})
            self.conn.execute(
                "UPDATE ai_chat_sessions SET metadata_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(current, ensure_ascii=False), session_id),
            )
            self._commit_if_needed()
            return current

    def delete_ai_chat_session(self, session_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_chat_messages WHERE session_id = ?", (session_id,))
            self.conn.execute("DELETE FROM ai_chat_sessions WHERE id = ?", (session_id,))
            self._commit_if_needed()

    def list_ai_chat_messages(self, session_id: int, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM ai_chat_messages WHERE session_id = ? ORDER BY id ASC"
        params: list[Any] = [session_id]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["metadata"] = self._parse_json_field(item.pop("metadata_json", None), {})
            results.append(item)
        return results

    def append_ai_chat_message(self, session_id: int, role: str, content: str, metadata: dict[str, Any] | None = None) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """INSERT INTO ai_chat_messages (session_id, role, content, metadata_json)
                   VALUES (?, ?, ?, ?)""",
                (session_id, role, content, json.dumps(metadata or {}, ensure_ascii=False)),
            )
            self.conn.execute("UPDATE ai_chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def delete_ai_chat_messages_after(self, session_id: int, message_id: int) -> int:
        """删除指定消息（含）之后的所有消息，用于'重发'功能。返回删除条数。"""
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM ai_chat_messages WHERE session_id = ? AND id >= ?",
                (session_id, message_id),
            )
            self._commit_if_needed()
            return int(cursor.rowcount or 0)

    @staticmethod
    def _parse_json_field(value: Any, default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    # ══════════════════════════════════════════════════════════════
    #  阅读进度追踪
    # ══════════════════════════════════════════════════════════════

    def _migrate_reading_progress_table(self) -> None:
        """创建阅读进度追踪表。"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reading_progress (
                novel_id INTEGER PRIMARY KEY,
                progress INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'unread',
                last_read_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_reading_progress_status ON reading_progress(status);
            CREATE INDEX IF NOT EXISTS idx_reading_progress_last_read ON reading_progress(last_read_at DESC);
            """
        )

    def upsert_reading_progress(self, novel_id: int, progress: int, status: str) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO reading_progress (novel_id, progress, status, last_read_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(novel_id) DO UPDATE SET
                    progress = excluded.progress,
                    status = excluded.status,
                    last_read_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (novel_id, progress, status),
            )
            self._commit_if_needed()

    def get_reading_progress(self, novel_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM reading_progress WHERE novel_id = ?", (novel_id,)
        ).fetchone()
        return dict(row) if row else None

    def delete_reading_progress(self, novel_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM reading_progress WHERE novel_id = ?", (novel_id,))
            self._commit_if_needed()

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import NovelRecord, NovelTextRecord, SourceRecord, UserRecord


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                account TEXT,
                raw_json TEXT NOT NULL,
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

            CREATE TABLE IF NOT EXISTS sources (
                novel_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                source_key TEXT NOT NULL,
                discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (novel_id, source_type, source_key)
            );

            CREATE TABLE IF NOT EXISTS assets (
                novel_id INTEGER NOT NULL,
                asset_type TEXT NOT NULL,
                remote_url TEXT NOT NULL,
                local_path TEXT NOT NULL,
                file_hash TEXT,
                downloaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (novel_id, asset_type, remote_url)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS novel_fts USING fts5(
                novel_id UNINDEXED,
                title,
                caption,
                author_name,
                body
            );
            """
        )
        self.conn.commit()

    def upsert_user(self, record: UserRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO users (user_id, name, account, raw_json, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
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
        if not user_id:
            return None
        row = self.conn.execute(
            "SELECT user_id, name, account, raw_json, updated_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        raw = self._load_raw_json(row["raw_json"])
        return {
            "user_id": row["user_id"],
            "name": row["name"],
            "account": row["account"],
            "avatar_url": self._extract_user_avatar(raw),
            "updated_at": row["updated_at"],
        }

    def list_followed_users(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT user_id, name, account, raw_json, updated_at
            FROM users
            ORDER BY updated_at DESC, user_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "user_id": row["user_id"],
                "name": row["name"],
                "account": row["account"],
                "avatar_url": self._extract_user_avatar(self._load_raw_json(row["raw_json"])),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def list_recent_novels(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
                n.novel_id,
                n.title,
                n.user_id,
                u.name AS author_name,
                n.cover_url,
                n.restrict_value,
                n.total_bookmarks,
                n.total_views,
                n.last_seen_at
            FROM novels AS n
            LEFT JOIN users AS u ON u.user_id = n.user_id
            ORDER BY n.last_seen_at DESC, n.novel_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

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
        return None

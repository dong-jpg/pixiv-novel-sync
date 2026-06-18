from __future__ import annotations

import json
from typing import Any

from ..models import AssetRecord, NovelRecord, NovelTextRecord, SourceRecord


class NovelsMixin:
    """小说相关数据库操作 Mixin"""

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

    def record_assets(self, records: list[AssetRecord]) -> None:
        if not records:
            return
        with self.transaction():
            self.conn.executemany(
                """
                INSERT INTO assets (novel_id, asset_type, remote_url, local_path, file_hash, downloaded_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(novel_id, asset_type, remote_url) DO UPDATE SET
                    local_path = excluded.local_path,
                    file_hash = excluded.file_hash,
                    downloaded_at = CURRENT_TIMESTAMP
                """,
                [(record.novel_id, record.asset_type, record.remote_url, record.local_path, record.file_hash) for record in records],
            )

    def get_recorded_asset_urls(self, novel_id: int) -> set[str]:
        rows = self.conn.execute(
            """
            SELECT remote_url
            FROM assets
            WHERE novel_id = ? AND file_hash IS NOT NULL AND file_hash != ''
            """,
            (novel_id,),
        ).fetchall()
        return {str(row[0]) for row in rows}

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

    def get_novel_text_hash(self, novel_id: int) -> str | None:
        row = self.conn.execute("SELECT text_hash FROM novel_texts WHERE novel_id = ?", (novel_id,)).fetchone()
        return str(row[0]) if row else None

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
        # Phase 5.4: novel_ids分批避免超过SQLite参数限制
        if novel_ids is not None:
            if not novel_ids:
                return []
            BATCH_SIZE = 900
            results: list[dict[str, Any]] = []
            for i in range(0, len(novel_ids), BATCH_SIZE):
                batch = novel_ids[i:i + BATCH_SIZE]
                results.extend(self._list_novel_archive_refs_batch(batch, user_id, series_id))
            return results
        return self._list_novel_archive_refs_batch(None, user_id, series_id)

    def _list_novel_archive_refs_batch(
        self,
        novel_ids: list[int] | None,
        user_id: int | None,
        series_id: int | None,
    ) -> list[dict[str, Any]]:
        """内部批次查询"""
        where_clauses: list[str] = []
        params: list[Any] = []
        if novel_ids is not None:
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

    def delete_novel(self, novel_id: int) -> None:
        """删除小说及其相关数据"""
        with self.transaction():
                self.conn.execute("DELETE FROM novel_fts WHERE novel_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM sync_check_list WHERE novel_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM pending_deletions WHERE item_type = 'novel' AND item_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM recommendation_items WHERE novel_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM recommendation_feedback WHERE novel_id = ?", (novel_id,))
                self.conn.execute("DELETE FROM novels WHERE novel_id = ?", (novel_id,))

    def delete_bookmark(self, novel_id: int) -> None:
        """删除收藏记录"""
        with self._lock:
            self.conn.execute(
                "DELETE FROM sources WHERE novel_id = ? AND source_type LIKE 'bookmark_%'",
                (novel_id,),
            )
            self._commit_if_needed()

    def replace_fts(self, novel_id: int, title: str, caption: str, author_name: str, body: str) -> None:
        """更新FTS索引。

        ✅ Bug #6 修复: 使用 transaction() 确保 DELETE 和 INSERT 的原子性。
        注意:与upsert_novel分离调用时存在漂移风险(一个成功一个失败)。
        Phase 5批量事务化后自然原子化。当前调用方(sync_engine.py:1723)未封装事务。
        """
        with self.transaction():
            self.conn.execute("DELETE FROM novel_fts WHERE novel_id = ?", (novel_id,))
            self.conn.execute(
                "INSERT INTO novel_fts (novel_id, title, caption, author_name, body) VALUES (?, ?, ?, ?, ?)",
                (novel_id, title, caption, author_name, body),
            )

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

    def export_stats(self) -> str:
        row = self.conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM users) AS users_count, "
            "(SELECT COUNT(*) FROM novels) AS novels_count, "
            "(SELECT COUNT(*) FROM series) AS series_count, "
            "(SELECT COUNT(*) FROM pending_deletions WHERE status = 'pending') AS pending_count"
        ).fetchone()
        return json.dumps(dict(row), ensure_ascii=False)

    def get_existing_novel_ids(self, novel_ids: list[int], require_assets: bool = False) -> set[int]:
        """批量检查小说是否已完整归档，返回可安全跳过的 ID 集合。"""
        if not novel_ids:
            return set()
        result: set[int] = set()
        batch_size = 900  # Phase 5.4: 提升到900避免SQLite限制
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

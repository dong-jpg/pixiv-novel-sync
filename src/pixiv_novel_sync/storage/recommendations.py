"""推荐系统相关数据库操作 Mixin"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .utils import _LazyNovelMembership


class RecommendationsMixin:
    """推荐相关的数据库操作方法集合"""

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

    def get_recent_recommendation_items(self, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        """获取最近推荐项目用于相似度检测"""
        return self.list_recommendation_items(status=status, limit=limit)

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
        # 5.3: archived 判断走主键索引 EXISTS 惰性查询,不再 SELECT 全表灌进内存。
        # recommendation_items 量级小(通常几百条),保留 set 即可。
        archived_ids = _LazyNovelMembership(
            self.conn, "SELECT 1 FROM novels WHERE novel_id = ? LIMIT 1"
        )
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

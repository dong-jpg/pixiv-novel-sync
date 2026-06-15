from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import AssetRecord, NovelRecord, NovelTextRecord, SourceRecord, UserRecord
from .storage.connection import DatabaseConnection
from .storage.schema import SchemaMixin
from .storage.utils import _LazyNovelMembership
from .storage.novels import NovelsMixin
from .storage.users import UsersMixin
from .storage.series import SeriesMixin
from .storage.bookmarks import BookmarksMixin
from .storage.tasks import TasksMixin


class Database(
    NovelsMixin,
    UsersMixin,
    SeriesMixin,
    BookmarksMixin,
    TasksMixin,
    SchemaMixin,
    DatabaseConnection,
):
    def __init__(self, path: Path) -> None:
        super().__init__(path)

    
    def export_stats(self) -> str:
        row = self.conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM users) AS users_count, "
            "(SELECT COUNT(*) FROM novels) AS novels_count, "
            "(SELECT COUNT(*) FROM series) AS series_count, "
            "(SELECT COUNT(*) FROM pending_deletions WHERE status = 'pending') AS pending_count"
        ).fetchone()
        return json.dumps(dict(row), ensure_ascii=False)

    # ── 偏好画像与推书 ──────────────────────────────────────────────

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

    # ── AI 创作工作台 ──────────────────────────────────────────────

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
            # Phase 5.4: 分批避免超过SQLite参数限制(999)
            BATCH_SIZE = 900
            remote_list = list(remote_ids)
            total_count = 0
            for i in range(0, len(remote_list), BATCH_SIZE):
                batch = remote_list[i:i + BATCH_SIZE]
                placeholders = ",".join("?" * len(batch))
                result = self.conn.execute(
                    f"UPDATE pending_deletions SET status = 'restored', restored_at = CURRENT_TIMESTAMP "
                    f"WHERE item_type = ? AND status = 'pending' AND item_id IN ({placeholders})",
                    (item_type, *batch),
                )
                total_count += result.rowcount
            if total_count:
                self._commit_if_needed()
            return total_count

    def cleanup_old_pending_deletions(self, grace_period_days: int = 30, cleanup_confirmed_days: int = 7) -> dict[str, int]:
        """Phase 3.2: 清理过期的pending_deletions记录

        Args:
            grace_period_days: pending状态保留天数,超过此时间自动确认删除
            cleanup_confirmed_days: 已确认/已恢复记录保留天数,超过后清理

        Returns:
            {"auto_confirmed": 自动确认数, "cleaned_up": 清理数}
        """
        with self._lock:
            # 自动确认超过grace period的pending记录
            auto_confirmed = self.conn.execute(
                """
                UPDATE pending_deletions
                SET status = 'confirmed', confirmed_at = CURRENT_TIMESTAMP
                WHERE status = 'pending'
                AND datetime(detected_at) < datetime('now', '-' || ? || ' days')
                """,
                (grace_period_days,)
            ).rowcount

            # 清理过期的已确认/已恢复记录
            cleaned_up = self.conn.execute(
                """
                DELETE FROM pending_deletions
                WHERE status IN ('confirmed', 'restored')
                AND (
                    (status = 'confirmed' AND datetime(confirmed_at) < datetime('now', '-' || ? || ' days'))
                    OR (status = 'restored' AND datetime(restored_at) < datetime('now', '-' || ? || ' days'))
                )
                """,
                (cleanup_confirmed_days, cleanup_confirmed_days)
            ).rowcount

            self.conn.commit()
            return {"auto_confirmed": auto_confirmed, "cleaned_up": cleaned_up}

    # ══════════════════════════════════════════════════════════════
    # AI 写作项目 / 章节 / 伏笔 / 状态记忆
    # ══════════════════════════════════════════════════════════════

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
        # 1.2: 统一用 transaction() 上下文,不再手写 BEGIN IMMEDIATE/commit/rollback。
        with self.transaction() as conn:
            conn.execute("DELETE FROM ai_chapters WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM ai_foreshadows WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM ai_project_states WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM ai_writing_projects WHERE id = ?", (project_id,))

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

"""AI 写作项目、章节、伏笔和对话存储。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


class AiWritingMixin:
    """AI 写作相关的数据库操作混入类。"""

    # ── ai_writing_projects CRUD ──────────────────────────────────

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
        allowed = {
            "name",
            "description",
            "outline",
            "style_profile_id",
            "novel_profile_id",
            "settings",
            "status",
            "cover_path",
        }
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

"""AI 文档、草稿、配置文件和提示模板的存储操作。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any


class AiDocumentsMixin:
    """AI 文档、草稿、风格配置、小说配置和提示模板的数据库操作混入类。"""

    # ── ai_drafts ──────────────────────────────────────────────

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

    # ── ai_documents ───────────────────────────────────────────

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

    # ── ai_style_profiles ──────────────────────────────────────

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

    # ── ai_novel_profiles ──────────────────────────────────────

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

    # ── ai_prompt_templates ────────────────────────────────────

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

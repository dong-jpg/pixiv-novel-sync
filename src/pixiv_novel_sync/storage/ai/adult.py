"""Storage boundary for adult polish metadata and owner-scoped candidates."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any


_FORBIDDEN_SNAPSHOT_KEYS = {
    "api_key",
    "before",
    "after",
    "candidate",
    "chapter_content",
    "messages",
    "output_text",
    "prompt",
    "system_prompt",
    "target_text",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_snapshot(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or key.lower() in _FORBIDDEN_SNAPSHOT_KEYS:
                raise ValueError("成人快照包含禁止保存的字段")
            result[key] = _safe_snapshot(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_snapshot(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("成人快照包含不可序列化值")


def _validation_payload(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        payload = asdict(value)
    elif isinstance(value, dict):
        payload = dict(value)
    else:
        raise ValueError("validation 必须是结构化结果")
    forbidden = _FORBIDDEN_SNAPSHOT_KEYS.intersection(str(key).lower() for key in payload)
    if forbidden:
        raise ValueError("validation 包含正文")
    return _safe_snapshot(payload)


class AdultStorageMixin:
    def _adult_character_row(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        try:
            aliases = json.loads(item.pop("aliases_json", "[]") or "[]")
        except (TypeError, ValueError):
            aliases = []
        item["aliases"] = aliases if isinstance(aliases, list) else []
        item["fictional"] = bool(item.get("fictional"))
        item["active"] = bool(item.get("active"))
        item["revision"] = int(item.get("revision") or 0)
        return item

    def list_adult_characters(
        self,
        project_id: int,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM ai_project_characters WHERE project_id = ?"
        params: list[Any] = [int(project_id)]
        if not include_inactive:
            sql += " AND active = 1"
        sql += " ORDER BY canonical_name, character_id"
        return [
            self._adult_character_row(row)
            for row in self.conn.execute(sql, params).fetchall()
        ]

    def get_adult_character(self, character_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM ai_project_characters WHERE character_id = ?",
            (character_id,),
        ).fetchone()
        return self._adult_character_row(row) if row is not None else None

    def get_adult_confirmation(self, project_id: int) -> dict[str, Any] | None:
        project = self.get_ai_writing_project(int(project_id))
        if project is None:
            return None
        entries = project.get("adult_characters_json")
        return {
            "project_id": int(project_id),
            "adult_content_enabled": bool(project.get("adult_content_enabled")),
            "adult_characters_confirmed": bool(project.get("adult_characters_confirmed")),
            "fictional_characters_confirmed": bool(
                project.get("fictional_characters_confirmed")
            ),
            "adult_characters": entries if isinstance(entries, list) else [],
            "adult_confirmation_revision": int(
                project.get("adult_confirmation_revision") or 0
            ),
            "adult_confirmation_updated_at": project.get(
                "adult_confirmation_updated_at"
            ),
        }

    def get_adult_review_bindings(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM ai_adult_review_bindings ORDER BY review_kind"
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            enabled = bool(item.get("enabled"))
            if not enabled:
                result[str(item["review_kind"])] = {"enabled": False}
                continue
            try:
                capabilities = json.loads(item.get("required_capabilities_json") or "[]")
            except (TypeError, ValueError):
                capabilities = []
            result[str(item["review_kind"])] = {
                "enabled": True,
                "binding_type": item.get("binding_type"),
                "provider_id": item.get("provider_id"),
                "model": item.get("model"),
                "model_pool_id": item.get("model_pool_id"),
                "required_capabilities": capabilities,
                "version": int(item.get("version") or 1),
                "updated_at": item.get("updated_at"),
            }
        return result

    def list_adult_policy_state(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM ai_adult_policy_state ORDER BY policy_kind"
            ).fetchall()
        ]

    def get_adult_job(self, job_id: str, owner_scope: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM ai_jobs
            WHERE job_id = ? AND task_type = 'adult_polish' AND owner_scope = ?
            """,
            (job_id, owner_scope),
        ).fetchone()
        if row is None:
            return None
        return self._ai_job_from_row(row, include_attempts=True)

    def find_job_by_idempotency(
        self,
        owner_scope: str,
        idempotency_key_hash: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM ai_jobs
            WHERE task_type = 'adult_polish' AND owner_scope = ?
              AND idempotency_key_hash = ?
            """,
            (owner_scope, idempotency_key_hash),
        ).fetchone()
        return self._ai_job_from_row(row, include_attempts=True) if row else None

    def create_adult_job(
        self,
        *,
        job_id: str,
        agent_id: int,
        input_data: dict[str, Any],
        owner_scope: str,
        owner_token: str,
        idempotency_key_hash: str,
        parent_job_id: str | None = None,
    ) -> dict[str, Any]:
        safe_input = _safe_snapshot(input_data)
        if not isinstance(safe_input, dict):
            raise ValueError("成人 job 输入必须是对象")
        with self.transaction() as conn:
            existing = conn.execute(
                """
                SELECT * FROM ai_jobs
                WHERE task_type = 'adult_polish' AND owner_scope = ?
                  AND idempotency_key_hash = ?
                """,
                (owner_scope, idempotency_key_hash),
            ).fetchone()
            if existing is not None:
                return self._ai_job_from_row(existing, include_attempts=True)
            self.create_ai_job(
                job_id,
                "adult_polish",
                agent_id,
                safe_input,
                owner_token=owner_token,
                stage="main",
                parent_job_id=parent_job_id,
            )
            conn.execute(
                """
                UPDATE ai_jobs SET owner_scope = ?, idempotency_key_hash = ?
                WHERE job_id = ?
                """,
                (owner_scope, idempotency_key_hash, job_id),
            )
            created = conn.execute(
                "SELECT * FROM ai_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            return self._ai_job_from_row(created, include_attempts=True)

    def save_candidate_application(self, data: dict[str, Any]) -> int:
        validation = _validation_payload(data.get("validation"))
        snapshots = _safe_snapshot(data.get("snapshots") or {})
        candidate = data.get("candidate")
        if candidate is not None and not isinstance(candidate, str):
            raise ValueError("candidate 必须是字符串")
        owner_token = data.get("owner_token")
        if not isinstance(owner_token, str) or not owner_token:
            raise ValueError("成人 job owner token 无效")
        columns = (
            "source_job_id",
            "owner_scope",
            "project_id",
            "chapter_id",
            "target_start",
            "target_end",
            "chapter_revision_before",
            "chapter_hash_before",
            "target_hash_before",
            "project_facts_hash",
            "adult_confirmation_revision",
            "adult_characters_hash",
            "participant_hash",
            "provider_scope_hash",
            "main_binding_hash",
            "safety_binding_hash",
            "fact_guard_binding_hash",
            "safety_policy_hash",
            "safety_prompt_hash",
            "fact_guard_prompt_hash",
            "validator_policy_hash",
            "validation_hash",
            "warning_ack_hash",
            "access_token_hash",
            "snapshots_json",
            "validation_json",
            "applicable",
        )
        values = {
            **data,
            "main_binding_hash": data.get("main_binding_hash", ""),
            "safety_binding_hash": data.get("safety_binding_hash", ""),
            "fact_guard_binding_hash": data.get("fact_guard_binding_hash", ""),
            "safety_prompt_hash": data.get("safety_prompt_hash", ""),
            "fact_guard_prompt_hash": data.get("fact_guard_prompt_hash", ""),
            "warning_ack_hash": data.get("warning_ack_hash", ""),
            "snapshots_json": _json(snapshots),
            "validation_json": _json(validation),
            "applicable": 1 if data.get("applicable") else 0,
        }
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM ai_polish_applications WHERE source_job_id = ?",
                (data["source_job_id"],),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            cursor = conn.execute(
                f"""
                INSERT INTO ai_polish_applications ({', '.join(columns)})
                VALUES ({', '.join('?' for _ in columns)})
                """,
                tuple(values.get(column) for column in columns),
            )
            updated = conn.execute(
                """
                UPDATE ai_jobs
                SET output_text = ?, output_json = ?, status = ?,
                    finished_at = CURRENT_TIMESTAMP, lease_until = NULL
                WHERE job_id = ? AND owner_scope = ? AND owner_token = ?
                  AND status = 'running'
                """,
                (
                    candidate,
                    _json({"validation_hash": data["validation_hash"]}),
                    "succeeded" if data.get("applicable") else "failed",
                    data["source_job_id"],
                    data["owner_scope"],
                    owner_token,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("成人 job owner CAS 失败")
            return int(cursor.lastrowid)

    def get_application_for_owner(
        self,
        source_job_id: str,
        owner_scope: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM ai_polish_applications
            WHERE source_job_id = ? AND owner_scope = ?
            """,
            (source_job_id, owner_scope),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        for raw_key, public_key in (
            ("snapshots_json", "snapshots"),
            ("validation_json", "validation"),
        ):
            try:
                item[public_key] = json.loads(item.pop(raw_key) or "{}")
            except (TypeError, ValueError):
                item[public_key] = {}
        item["applicable"] = bool(item.get("applicable"))
        return item


__all__ = ["AdultStorageMixin"]

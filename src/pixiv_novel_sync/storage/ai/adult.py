"""Storage boundary for adult polish metadata and owner-scoped candidates."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from ...ai.adult_types import AdultConflictError, canonical_sha256


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
    @staticmethod
    def _invalidate_adult_confirmation(conn: Any, project_id: int) -> None:
        updated = conn.execute(
            """
            UPDATE ai_writing_projects
            SET adult_characters_confirmed = 0,
                fictional_characters_confirmed = 0,
                adult_confirmation_revision = adult_confirmation_revision + 1,
                adult_confirmation_updated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(project_id),),
        )
        if updated.rowcount != 1:
            raise ValueError("写作项目不存在")

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

    def create_adult_character(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.transaction() as conn:
            project_id = int(data["project_id"])
            if conn.execute(
                "SELECT 1 FROM ai_writing_projects WHERE id = ?",
                (project_id,),
            ).fetchone() is None:
                raise ValueError("写作项目不存在")
            conn.execute(
                """
                INSERT INTO ai_project_characters (
                    character_id, project_id, revision, canonical_name,
                    aliases_json, age_years, age_basis, fictional, active
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, 1)
                """,
                (
                    data["character_id"],
                    project_id,
                    data["canonical_name"],
                    _json(data.get("aliases") or []),
                    data.get("age_years"),
                    data["age_basis"],
                    1 if data["fictional"] else 0,
                ),
            )
            self._invalidate_adult_confirmation(conn, project_id)
            row = conn.execute(
                "SELECT * FROM ai_project_characters WHERE character_id = ?",
                (data["character_id"],),
            ).fetchone()
            if row is None:
                raise RuntimeError("成人角色创建失败")
            return self._adult_character_row(row)

    def cas_update_adult_character(
        self,
        character_id: str,
        expected_revision: int,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {
            "canonical_name",
            "aliases",
            "age_years",
            "age_basis",
            "fictional",
            "active",
        }
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ValueError(f"成人角色包含未知字段: {', '.join(unknown)}")
        if not changes:
            raise ValueError("成人角色没有可更新字段")

        assignments: list[str] = []
        params: list[Any] = []
        for key, value in changes.items():
            if key == "aliases":
                assignments.append("aliases_json = ?")
                params.append(_json(value))
            elif key in {"fictional", "active"}:
                assignments.append(f"{key} = ?")
                params.append(1 if value else 0)
            else:
                assignments.append(f"{key} = ?")
                params.append(value)
        assignments.extend(
            ["revision = revision + 1", "updated_at = CURRENT_TIMESTAMP"]
        )

        with self.transaction() as conn:
            current = conn.execute(
                """
                SELECT project_id FROM ai_project_characters
                WHERE character_id = ? AND revision = ?
                """,
                (character_id, int(expected_revision)),
            ).fetchone()
            if current is None:
                raise AdultConflictError("角色 revision 已变化")
            updated = conn.execute(
                f"""
                UPDATE ai_project_characters SET {', '.join(assignments)}
                WHERE character_id = ? AND revision = ?
                """,
                [*params, character_id, int(expected_revision)],
            )
            if updated.rowcount != 1:
                raise AdultConflictError("角色 revision 已变化")
            self._invalidate_adult_confirmation(conn, int(current["project_id"]))
            row = conn.execute(
                "SELECT * FROM ai_project_characters WHERE character_id = ?",
                (character_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("成人角色更新失败")
            return self._adult_character_row(row)

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
        confirmation = {
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
        confirmation["adult_characters_hash"] = canonical_sha256(
            confirmation["adult_characters"]
        )
        return confirmation

    def set_adult_confirmation(
        self,
        project_id: int,
        expected_revision: int,
        data: dict[str, Any],
        character_ids: Sequence[str],
    ) -> dict[str, Any]:
        if len(character_ids) > 100:
            raise ValueError("成人角色确认最多 100 个角色")
        if len(set(character_ids)) != len(character_ids):
            raise ValueError("成人角色 ID 不得重复")
        for key in (
            "adult_content_enabled",
            "adult_characters_confirmed",
            "fictional_characters_confirmed",
        ):
            if not isinstance(data.get(key), bool):
                raise ValueError(f"{key} 必须是布尔值")

        adult_confirmed = data["adult_characters_confirmed"]
        fictional_confirmed = data["fictional_characters_confirmed"]
        if adult_confirmed != fictional_confirmed:
            raise ValueError("成人与虚构角色确认状态必须同时确认")
        if adult_confirmed and not character_ids:
            raise ValueError("成人角色确认至少需要一个角色")

        with self.transaction() as conn:
            project = conn.execute(
                """
                SELECT adult_confirmation_revision
                FROM ai_writing_projects WHERE id = ?
                """,
                (int(project_id),),
            ).fetchone()
            if project is None:
                raise ValueError("写作项目不存在")
            if int(project["adult_confirmation_revision"] or 0) != int(
                expected_revision
            ):
                raise AdultConflictError("成人确认 revision 已变化")

            entries: list[dict[str, Any]] = []
            if adult_confirmed:
                rows = conn.execute(
                    "SELECT * FROM ai_project_characters WHERE project_id = ?",
                    (int(project_id),),
                ).fetchall()
                by_id = {str(row["character_id"]): row for row in rows}
                confirmed_at = datetime.now(timezone.utc).replace(
                    microsecond=0
                ).isoformat()
                for character_id in sorted(character_ids):
                    row = by_id.get(character_id)
                    if row is None:
                        raise ValueError("成人角色不存在或不属于当前项目")
                    if not bool(row["active"]):
                        raise ValueError("成人角色已停用或不可用，不能确认")
                    if row["age_years"] is None:
                        raise ValueError("成人角色年龄必须明确")
                    if int(row["age_years"]) < 18:
                        raise ValueError("成人角色必须明确年满 18 岁")
                    if not bool(row["fictional"]):
                        raise ValueError("成人角色必须明确为虚构角色")
                    entry = {
                        "character_id": character_id,
                        "character_revision": int(row["revision"]),
                        "confirmed_at": confirmed_at,
                    }
                    if len(_json(entry).encode("utf-8")) > 2_048:
                        raise ValueError("单个成人角色确认记录过大")
                    entries.append(entry)

            serialized = _json(entries)
            if len(serialized.encode("utf-8")) > 65_536:
                raise ValueError("成人角色确认记录总量过大")
            updated = conn.execute(
                """
                UPDATE ai_writing_projects
                SET adult_content_enabled = ?,
                    adult_characters_confirmed = ?,
                    fictional_characters_confirmed = ?,
                    adult_characters_json = ?,
                    adult_confirmation_revision = adult_confirmation_revision + 1,
                    adult_confirmation_updated_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND adult_confirmation_revision = ?
                """,
                (
                    1 if data["adult_content_enabled"] else 0,
                    1 if adult_confirmed else 0,
                    1 if fictional_confirmed else 0,
                    serialized,
                    int(project_id),
                    int(expected_revision),
                ),
            )
            if updated.rowcount != 1:
                raise AdultConflictError("成人确认 revision 已变化")
            result = self.get_adult_confirmation(int(project_id))
            if result is None:
                raise RuntimeError("成人角色确认保存失败")
            return result

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

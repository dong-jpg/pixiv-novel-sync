"""Storage boundary for adult polish metadata and owner-scoped candidates."""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from ...ai.adult_types import (
    AdultConflictError,
    AdultValidationResult,
    canonical_sha256,
    raw_sha256,
    warning_ack_hash,
)
from ...ai.adult_validation import compute_validation_hash


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


def _stored_validation_result(application: Mapping[str, Any]) -> AdultValidationResult:
    payload = application.get("validation")
    if not isinstance(payload, Mapping):
        raw_payload = application.get("validation_json")
        if not isinstance(raw_payload, str):
            raise ValueError("validation_json 缺失")
        payload = json.loads(raw_payload)
    expected_fields = {
        "applicable",
        "warnings",
        "blocking_issues",
        "protected_terms_missing",
        "paragraph_delta",
        "length_ratio",
        "perspective_warning",
        "new_number_tokens",
        "diff_summary",
        "validation_hash",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected_fields:
        raise ValueError("validation_json 字段无效")

    def string_tuple(name: str) -> tuple[str, ...]:
        value = payload[name]
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            raise ValueError(f"{name} 无效")
        return tuple(value)

    applicable = payload["applicable"]
    perspective_warning = payload["perspective_warning"]
    paragraph_delta = payload["paragraph_delta"]
    length_ratio = payload["length_ratio"]
    diff_summary = payload["diff_summary"]
    validation_hash = payload["validation_hash"]
    if not isinstance(applicable, bool) or not isinstance(perspective_warning, bool):
        raise ValueError("validation_json 布尔字段无效")
    if isinstance(paragraph_delta, bool) or not isinstance(paragraph_delta, int):
        raise ValueError("paragraph_delta 无效")
    if isinstance(length_ratio, bool) or not isinstance(length_ratio, (int, float)):
        raise ValueError("length_ratio 无效")
    if not isinstance(diff_summary, Mapping) or set(diff_summary) != {
        "inserted",
        "deleted",
        "replaced",
    }:
        raise ValueError("diff_summary 无效")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in diff_summary.values()
    ):
        raise ValueError("diff_summary 无效")
    if not isinstance(validation_hash, str):
        raise ValueError("validation_hash 无效")
    return AdultValidationResult(
        applicable=applicable,
        warnings=string_tuple("warnings"),
        blocking_issues=string_tuple("blocking_issues"),
        protected_terms_missing=string_tuple("protected_terms_missing"),
        paragraph_delta=paragraph_delta,
        length_ratio=float(length_ratio),
        perspective_warning=perspective_warning,
        new_number_tokens=string_tuple("new_number_tokens"),
        diff_summary=dict(diff_summary),
        validation_hash=validation_hash,
    )


class AdultStorageMixin:
    def assert_adult_application_validation(
        self,
        application: Mapping[str, Any],
    ) -> AdultValidationResult:
        try:
            validation = _stored_validation_result(application)
            stored_hash = application.get("validation_hash")
            if not isinstance(stored_hash, str):
                raise ValueError("validation_hash 缺失")
            computed_hash = compute_validation_hash(validation)
            if not hmac.compare_digest(validation.validation_hash, stored_hash):
                raise ValueError("validation_json hash 不一致")
            if not hmac.compare_digest(computed_hash, stored_hash):
                raise ValueError("validation canonical hash 不一致")
            if bool(application.get("applicable")) != validation.applicable:
                raise ValueError("validation applicable 不一致")
            return validation
        except (TypeError, ValueError, KeyError) as exc:
            raise AdultConflictError("成人润色校验快照损坏") from exc

    @staticmethod
    def _cleanup_adult_jobs_locked(
        conn: Any,
        keep_days: int,
        keep_failed_days: int,
    ) -> int:
        conn.execute(
            """
            DELETE FROM ai_polish_applications
            WHERE applied_at IS NULL
              AND created_at < datetime('now', ? || ' days')
            """,
            (f"-{int(keep_days)}",),
        )
        deleted = conn.execute(
            """
            DELETE FROM ai_jobs
            WHERE task_type = 'adult_polish'
              AND NOT EXISTS (
                SELECT 1 FROM ai_polish_applications AS application
                WHERE application.source_job_id = ai_jobs.job_id
                  AND application.applied_at IS NULL
              )
              AND (
                (status IN ('succeeded', 'partial', 'done', 'completed', 'success')
                 AND created_at < datetime('now', ? || ' days'))
                OR
                (status IN ('failed', 'error', 'cancelled')
                 AND created_at < datetime('now', ? || ' days'))
              )
            """,
            (f"-{int(keep_days)}", f"-{int(keep_failed_days)}"),
        )
        return int(deleted.rowcount or 0)

    def cleanup_adult_jobs(
        self,
        keep_days: int = 3,
        keep_failed_days: int | None = None,
    ) -> int:
        if keep_failed_days is None:
            keep_failed_days = keep_days
        with self.transaction() as conn:
            return self._cleanup_adult_jobs_locked(
                conn,
                keep_days,
                keep_failed_days,
            )

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
            item = self._adult_review_binding_row(row)
            enabled = bool(item["enabled"])
            if not enabled:
                result[str(item["review_kind"])] = {"enabled": False}
                continue
            result[str(item["review_kind"])] = item
        return result

    @staticmethod
    def _adult_review_binding_row(row: Any) -> dict[str, Any]:
        item = dict(row)
        try:
            capabilities = json.loads(
                item.pop("required_capabilities_json", "[]") or "[]"
            )
        except (TypeError, ValueError):
            capabilities = []
        item["enabled"] = bool(item.get("enabled"))
        item["required_capabilities"] = (
            capabilities if isinstance(capabilities, list) else []
        )
        item["version"] = int(item.get("version") or 1)
        return item

    def get_adult_review_binding(
        self,
        review_kind: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM ai_adult_review_bindings WHERE review_kind = ?",
            (review_kind,),
        ).fetchone()
        return self._adult_review_binding_row(row) if row is not None else None

    def cas_update_review_binding(
        self,
        review_kind: str,
        *,
        expected_version: int,
        route: dict[str, Any],
    ) -> dict[str, Any]:
        if review_kind not in {"safety", "fact_guard"}:
            raise ValueError("成人审查绑定类型无效")
        allowed = {
            "binding_type",
            "provider_id",
            "model",
            "model_pool_id",
            "enabled",
        }
        unknown = sorted(set(route) - allowed)
        if unknown:
            raise ValueError(f"成人审查绑定包含未知字段: {', '.join(unknown)}")
        enabled = route.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")

        binding_type: str | None = None
        provider_id: int | None = None
        model: str | None = None
        model_pool_id: int | None = None
        if enabled:
            binding_type = route.get("binding_type")
            if binding_type not in {"fixed", "pool"}:
                raise ValueError("成人审查绑定类型必须是 fixed 或 pool")
            if binding_type == "fixed":
                raw_provider_id = route.get("provider_id")
                if (
                    isinstance(raw_provider_id, bool)
                    or not isinstance(raw_provider_id, int)
                    or raw_provider_id <= 0
                ):
                    raise ValueError("固定成人审查绑定缺少 provider_id")
                if route.get("model_pool_id") is not None:
                    raise ValueError("固定模型和模型池不能同时提交")
                raw_model = route.get("model")
                if raw_model is not None and not isinstance(raw_model, str):
                    raise ValueError("model 必须是字符串或 null")
                provider_id = raw_provider_id
                model = raw_model.strip() if isinstance(raw_model, str) else None
                model = model or None
            else:
                raw_pool_id = route.get("model_pool_id")
                if (
                    isinstance(raw_pool_id, bool)
                    or not isinstance(raw_pool_id, int)
                    or raw_pool_id <= 0
                ):
                    raise ValueError("模型池成人审查绑定缺少 model_pool_id")
                if route.get("provider_id") is not None or route.get("model") is not None:
                    raise ValueError("固定模型和模型池不能同时提交")
                model_pool_id = raw_pool_id

        with self.transaction() as conn:
            updated = conn.execute(
                """
                UPDATE ai_adult_review_bindings
                SET binding_type = ?, provider_id = ?, model = ?,
                    model_pool_id = ?, required_capabilities_json = '["json"]',
                    enabled = ?, version = version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE review_kind = ? AND version = ?
                """,
                (
                    binding_type,
                    provider_id,
                    model,
                    model_pool_id,
                    1 if enabled else 0,
                    review_kind,
                    int(expected_version),
                ),
            )
            if updated.rowcount != 1:
                raise AdultConflictError("成人审查绑定 revision 已变化")
            row = conn.execute(
                "SELECT * FROM ai_adult_review_bindings WHERE review_kind = ?",
                (review_kind,),
            ).fetchone()
            if row is None:
                raise RuntimeError("成人审查绑定更新失败")
            return self._adult_review_binding_row(row)

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

    def get_adult_job_execution(
        self,
        job_id: str,
        owner_scope: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM ai_jobs
            WHERE job_id = ? AND task_type = 'adult_polish' AND owner_scope = ?
            """,
            (job_id, owner_scope),
        ).fetchone()
        if row is None or not row["owner_token"]:
            return None
        item = self._ai_job_from_row(row, include_attempts=True)
        item["owner_token"] = str(row["owner_token"])
        return item

    def create_adult_review_job(
        self,
        *,
        job_id: str,
        parent_job_id: str,
        review_kind: str,
        input_data: dict[str, Any],
        owner_scope: str,
        owner_token: str,
        parent_owner_token: str,
        allow_succeeded_parent: bool = False,
    ) -> None:
        if review_kind not in {"safety", "fact_guard"}:
            raise ValueError("成人审查类型无效")
        safe_input = _safe_snapshot(input_data)
        if not isinstance(safe_input, dict):
            raise ValueError("成人审查 job 输入必须是对象")
        with self.transaction() as conn:
            parent = conn.execute(
                """
                SELECT status FROM ai_jobs
                WHERE job_id = ? AND task_type = 'adult_polish'
                  AND owner_scope = ? AND owner_token = ?
                """,
                (parent_job_id, owner_scope, parent_owner_token),
            ).fetchone()
            allowed_parent_statuses = (
                {"running", "succeeded"}
                if allow_succeeded_parent
                else {"running"}
            )
            if parent is None or parent["status"] not in allowed_parent_statuses:
                raise AdultConflictError("成人主生成任务已终结或 owner 不匹配")
            if allow_succeeded_parent:
                application = conn.execute(
                    """
                    SELECT 1 FROM ai_polish_applications
                    WHERE source_job_id = ? AND owner_scope = ?
                      AND applied_at IS NULL AND applicable = 1
                    """,
                    (parent_job_id, owner_scope),
                ).fetchone()
                if application is None:
                    raise AdultConflictError("成人候选不可重审或 owner 不匹配")
            self.create_ai_job(
                job_id,
                (
                    "adult_safety_review"
                    if review_kind == "safety"
                    else "adult_fact_guard"
                ),
                None,
                safe_input,
                owner_token=owner_token,
                stage="validation",
                parent_job_id=parent_job_id,
            )
            conn.execute(
                "UPDATE ai_jobs SET owner_scope = ? WHERE job_id = ?",
                (owner_scope, job_id),
            )

    def cas_finish_adult_job(
        self,
        job_id: str,
        owner_scope: str,
        owner_token: str,
        status: str,
        *,
        error_code: str,
        error_message: str,
        summary: Mapping[str, Any] | None = None,
    ) -> bool:
        if status not in {"failed", "partial", "cancelled"}:
            raise ValueError("成人 job 终态无效")
        if (
            not isinstance(error_code, str)
            or not error_code
            or len(error_code) > 100
            or any(
                not ("a" <= char <= "z" or char.isdigit() or char == "_")
                for char in error_code
            )
        ):
            raise ValueError("成人 job 错误代码无效")
        if not isinstance(error_message, str) or not error_message:
            raise ValueError("成人 job 错误消息无效")
        safe_message = "".join(
            char for char in error_message[:500] if ord(char) >= 32
        )
        safe_summary = _safe_snapshot(dict(summary or {}))
        output_json = _json({"code": error_code, **safe_summary})
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT status FROM ai_jobs
                WHERE job_id = ? AND task_type = 'adult_polish'
                  AND owner_scope = ? AND owner_token = ?
                """,
                (job_id, owner_scope, owner_token),
            ).fetchone()
            if row is None or row["status"] not in {"running", status}:
                return False
            cursor = conn.execute(
                """
                UPDATE ai_jobs
                SET status = ?, output_text = NULL, output_json = ?,
                    error_message = ?,
                    finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP),
                    lease_until = NULL
                WHERE job_id = ? AND task_type = 'adult_polish'
                  AND owner_scope = ? AND owner_token = ?
                  AND status IN ('running', ?)
                """,
                (
                    status,
                    output_json,
                    safe_message,
                    job_id,
                    owner_scope,
                    owner_token,
                    status,
                ),
            )
            return cursor.rowcount == 1

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
                """
                SELECT id, owner_scope FROM ai_polish_applications
                WHERE source_job_id = ?
                """,
                (data["source_job_id"],),
            ).fetchone()
            if existing is not None:
                if existing["owner_scope"] != data["owner_scope"]:
                    raise ValueError("成人候选 application owner 不匹配")
                raise ValueError("成人 job owner CAS 失败")
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
                    _json(
                        {
                            "validation_hash": data["validation_hash"],
                            "code": data.get("terminal_code")
                            or (
                                "succeeded"
                                if data.get("applicable")
                                else "validation_failed"
                            ),
                        }
                    ),
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

    def refresh_adult_application_validation(
        self,
        *,
        job_id: str,
        owner_scope: str,
        expected_validation_hash: str,
        validation: Any,
        provider_scope_hash: str,
        main_binding_hash: str,
        safety_binding_hash: str,
        fact_guard_binding_hash: str,
        safety_policy_hash: str,
        safety_prompt_hash: str,
        fact_guard_prompt_hash: str,
        validator_policy_hash: str,
        snapshots: Mapping[str, Any],
    ) -> None:
        safe_validation = _validation_payload(validation)
        safe_snapshots = _safe_snapshot(dict(snapshots))
        with self.transaction() as conn:
            updated = conn.execute(
                """
                UPDATE ai_polish_applications
                SET provider_scope_hash = ?, main_binding_hash = ?,
                    safety_binding_hash = ?, fact_guard_binding_hash = ?,
                    safety_policy_hash = ?, safety_prompt_hash = ?,
                    fact_guard_prompt_hash = ?, validator_policy_hash = ?,
                    validation_hash = ?, warning_ack_hash = '',
                    validation_json = ?, snapshots_json = ?
                WHERE source_job_id = ? AND owner_scope = ?
                  AND applied_at IS NULL AND applicable = 1
                  AND validation_hash = ?
                """,
                (
                    provider_scope_hash,
                    main_binding_hash,
                    safety_binding_hash,
                    fact_guard_binding_hash,
                    safety_policy_hash,
                    safety_prompt_hash,
                    fact_guard_prompt_hash,
                    validator_policy_hash,
                    safe_validation["validation_hash"],
                    _json(safe_validation),
                    _json(safe_snapshots),
                    job_id,
                    owner_scope,
                    expected_validation_hash,
                ),
            )
            if updated.rowcount != 1:
                raise AdultConflictError("成人润色校验快照已变化")

    def apply_adult_polish(
        self,
        job_id: str,
        owner_scope: str,
        warning_ack: str,
        access_token_hash: str,
        expected_snapshot: Any | None,
    ) -> dict[str, Any]:
        def snapshot_value(name: str) -> Any:
            if isinstance(expected_snapshot, Mapping):
                return expected_snapshot.get(name)
            return getattr(expected_snapshot, name, None)

        with self.transaction() as conn:
            application_row = conn.execute(
                """
                SELECT * FROM ai_polish_applications
                WHERE source_job_id = ? AND owner_scope = ?
                """,
                (job_id, owner_scope),
            ).fetchone()
            if application_row is None:
                raise AdultConflictError("成人润色候选不存在或 owner 不匹配")
            application = dict(application_row)
            self.assert_adult_application_validation(application)
            if not hmac.compare_digest(
                str(application.get("access_token_hash") or ""),
                access_token_hash,
            ):
                raise AdultConflictError("成人润色访问凭证无效")
            if application.get("applied_at") is not None:
                if (
                    application.get("chapter_revision_after") is None
                    or not application.get("chapter_hash_after")
                ):
                    raise AdultConflictError("成人润色应用终态不完整")
                return {
                    "application_id": int(application["id"]),
                    "chapter_revision_after": int(
                        application["chapter_revision_after"]
                    ),
                    "chapter_hash_after": str(application["chapter_hash_after"]),
                    "already_applied": True,
                }
            if expected_snapshot is None:
                raise AdultConflictError("成人润色应用快照缺失")
            if int(snapshot_value("application_id") or 0) != int(
                application["id"]
            ):
                raise AdultConflictError("成人润色 application 已变化")
            if not bool(application.get("applicable")):
                raise AdultConflictError("成人润色候选包含阻断项，不能应用")

            job = conn.execute(
                """
                SELECT status, output_text FROM ai_jobs
                WHERE job_id = ? AND task_type = 'adult_polish'
                  AND owner_scope = ?
                """,
                (job_id, owner_scope),
            ).fetchone()
            if job is None or job["status"] != "succeeded":
                raise AdultConflictError("成人润色任务终态已变化")
            candidate = job["output_text"]
            if not isinstance(candidate, str) or not candidate:
                raise AdultConflictError("成人润色候选已过期，请重新生成")
            if not hmac.compare_digest(
                raw_sha256(candidate),
                str(snapshot_value("candidate_hash") or ""),
            ):
                raise AdultConflictError("成人润色候选已变化")

            comparisons = (
                ("project_id", "project_id", "项目"),
                ("chapter_id", "chapter_id", "章节"),
                ("target_start", "target_start", "目标片段范围"),
                ("target_end", "target_end", "目标片段范围"),
                ("chapter_revision_before", "chapter_revision", "章节 revision"),
                ("chapter_hash_before", "chapter_hash", "章节正文"),
                ("target_hash_before", "target_hash", "目标片段"),
                ("project_facts_hash", "project_facts_hash", "项目事实"),
                (
                    "adult_confirmation_revision",
                    "adult_confirmation_revision",
                    "成人确认 revision",
                ),
                ("adult_characters_hash", "adult_characters_hash", "成人角色"),
                ("participant_hash", "participant_hash", "参与者"),
                ("provider_scope_hash", "provider_scope_hash", "Provider 范围"),
                ("main_binding_hash", "main_binding_hash", "写作 binding"),
                ("safety_binding_hash", "safety_binding_hash", "安全审查 binding"),
                (
                    "fact_guard_binding_hash",
                    "fact_guard_binding_hash",
                    "事实审查 binding",
                ),
                ("safety_policy_hash", "safety_policy_hash", "安全策略"),
                ("safety_prompt_hash", "safety_prompt_hash", "安全审查 Prompt"),
                (
                    "fact_guard_prompt_hash",
                    "fact_guard_prompt_hash",
                    "事实审查 Prompt",
                ),
                ("validator_policy_hash", "validator_policy_hash", "校验策略"),
                ("validation_hash", "validation_hash", "校验结果"),
            )
            for stored_key, snapshot_key, label in comparisons:
                if application.get(stored_key) != snapshot_value(snapshot_key):
                    raise AdultConflictError(f"{label}已变化")

            validation = self.assert_adult_application_validation(application)
            warnings = list(validation.warnings)
            expected_warning_ack = (
                warning_ack_hash(
                    str(application["validation_hash"]),
                    str(application["safety_policy_hash"]),
                    str(application["validator_policy_hash"]),
                    warnings,
                )
                if warnings
                else ""
            )
            if not hmac.compare_digest(expected_warning_ack, warning_ack):
                raise AdultConflictError("warning 确认已失效")

            chapter = conn.execute(
                """
                SELECT project_id, content, chapter_revision
                FROM ai_chapters WHERE id = ?
                """,
                (application["chapter_id"],),
            ).fetchone()
            if chapter is None or int(chapter["project_id"]) != int(
                application["project_id"]
            ):
                raise AdultConflictError("章节或项目关系已变化")
            content = chapter["content"]
            if not isinstance(content, str):
                raise AdultConflictError("章节正文已变化")
            start = int(application["target_start"])
            end = int(application["target_end"])
            if (
                int(chapter["chapter_revision"])
                != int(snapshot_value("chapter_revision"))
                or raw_sha256(content) != snapshot_value("chapter_hash")
                or end > len(content)
                or raw_sha256(content[start:end]) != snapshot_value("target_hash")
            ):
                raise AdultConflictError("章节或目标片段已变化")

            new_content = content[:start] + candidate + content[end:]
            revision_after = int(chapter["chapter_revision"]) + 1
            chapter_hash_after = raw_sha256(new_content)
            updated = conn.execute(
                """
                UPDATE ai_chapters
                SET content = ?, word_count = ?,
                    chapter_revision = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND chapter_revision = ?
                """,
                (
                    new_content,
                    len(new_content),
                    revision_after,
                    application["chapter_id"],
                    chapter["chapter_revision"],
                ),
            )
            if updated.rowcount != 1:
                raise AdultConflictError("章节 revision 已变化")
            conn.execute(
                """
                UPDATE ai_polish_applications
                SET applied_at = CURRENT_TIMESTAMP,
                    chapter_hash_after = ?, chapter_revision_after = ?,
                    warning_ack_hash = ?
                WHERE id = ? AND applied_at IS NULL
                """,
                (
                    chapter_hash_after,
                    revision_after,
                    warning_ack,
                    application["id"],
                ),
            )
            conn.execute(
                """
                UPDATE ai_jobs
                SET output_text = NULL,
                    output_json = ?, finished_at = COALESCE(
                        finished_at, CURRENT_TIMESTAMP
                    )
                WHERE job_id = ? AND owner_scope = ?
                """,
                (
                    _json(
                        {
                            "application_id": int(application["id"]),
                            "chapter_hash_after": chapter_hash_after,
                            "chapter_revision_after": revision_after,
                            "code": "applied",
                        }
                    ),
                    job_id,
                    owner_scope,
                ),
            )
            conn.execute(
                """
                INSERT INTO ai_chapter_derivative_invalidations (
                    chapter_id, chapter_revision, reason, status
                ) VALUES (?, ?, 'adult_polish_applied', 'pending')
                ON CONFLICT(chapter_id) DO UPDATE SET
                    chapter_revision = excluded.chapter_revision,
                    reason = excluded.reason,
                    status = 'pending',
                    updated_at = CURRENT_TIMESTAMP
                """,
                (application["chapter_id"], revision_after),
            )
            return {
                "application_id": int(application["id"]),
                "chapter_revision_after": revision_after,
                "chapter_hash_after": chapter_hash_after,
                "already_applied": False,
            }


__all__ = ["AdultStorageMixin"]

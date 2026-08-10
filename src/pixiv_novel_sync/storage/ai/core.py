"""AI providers/agents/jobs storage mixin."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from ...ai.providers import _redact_secrets


ADULT_AI_TASK_TYPES = (
    "adult_fact_guard",
    "adult_polish",
    "adult_safety_review",
)
_ADULT_AI_TASK_TYPES_SQL = ", ".join(
    f"'{task_type}'" for task_type in ADULT_AI_TASK_TYPES
)


class AIJobConflictError(RuntimeError):
    """AI job 不存在、已终结或 owner 不匹配。"""


class AIRouteBudgetExhausted(AIJobConflictError):
    """AI job 的候选或网络请求硬预算已耗尽。"""

    error_category = "route_budget_exhausted"


_JOB_STAGES = {"internal", "main", "validation"}
_JOB_TERMINAL_STATUSES = {"succeeded", "failed", "partial", "cancelled"}
_ATTEMPT_FIELDS = {
    "pool_id",
    "provider_id",
    "provider_model_id",
    "pool_version_snapshot",
    "pool_position_snapshot",
    "model_key",
    "pool_name_snapshot",
    "provider_name_snapshot",
    "agent_config_hash",
    "provider_config_hash",
    "candidate_list_hash",
    "stage",
    "lease_until",
}
_ATTEMPT_FINISH_FIELDS = {
    "error_scope",
    "error_message",
    "error_category",
    "finish_reason",
    "output_started",
    "latency_ms",
}
_FORBIDDEN_SNAPSHOT_KEYS = {
    "api_key",
    "api_key_encrypted",
    "messages",
    "output_text",
    "prompt",
    "request_body",
    "response_headers",
}
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sql_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _validate_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError(f"{field} 必须是有效时间")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"{field} 必须是有效时间")
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是有效时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return _sql_timestamp(parsed)


def _bounded_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} 必须是有效 UTF-8 文本") from exc
    text = value
    if any(unicodedata.category(character) == "Cc" for character in text):
        raise ValueError(f"{field} 不能包含控制字符")
    if len(text) > limit:
        raise ValueError(f"{field} 不能超过 {limit} 个字符")
    return text


def _safe_error_text(value: Any) -> str:
    text = str(value or "").encode("utf-8", errors="replace").decode("utf-8")
    text = "".join(
        " " if unicodedata.category(character) == "Cc" else character
        for character in text
    )
    return _redact_secrets(text)[:2000]


def _validate_hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} 必须是 64 位小写十六进制摘要")
    return value


def _validate_snapshot_content(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("候选快照字段名必须是字符串")
            _bounded_text(key, "候选快照字段名", 200)
            if key.lower() in _FORBIDDEN_SNAPSHOT_KEYS:
                raise ValueError("候选快照包含禁止保存的敏感字段")
            _validate_snapshot_content(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_snapshot_content(child)
    elif isinstance(value, str):
        _bounded_text(value, "候选快照文本", 256 * 1024)


def _canonical_snapshot(value: Any) -> tuple[str, str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("候选快照必须是有效 JSON 对象") from exc
    else:
        parsed = value
    if not isinstance(parsed, Mapping):
        raise ValueError("候选快照必须是 JSON 对象")
    _validate_snapshot_content(parsed)
    try:
        serialized = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded = serialized.encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("候选快照无法序列化") from exc
    if len(encoded) > 256 * 1024:
        raise ValueError("候选快照不能超过 256 KiB")
    return serialized, hashlib.sha256(encoded).hexdigest()


class AIProviderReferenceError(RuntimeError):
    """Provider 仍被 AI 配置引用。"""


class AiCoreMixin:
    """AI 核心对象（providers、agents、jobs）存储操作 mixin."""

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
        with self.transaction() as conn:
            fixed_agent = conn.execute(
                """
                SELECT 1 FROM ai_agents
                WHERE binding_type = 'fixed' AND provider_id = ?
                LIMIT 1
                """,
                (provider_id,),
            ).fetchone()
            if fixed_agent is not None:
                raise AIProviderReferenceError(
                    "Provider 仍被固定 Agent 引用，无法删除"
                )

            pool_member = conn.execute(
                """
                SELECT 1
                FROM ai_model_pool_members AS pm
                JOIN ai_provider_models AS model
                  ON model.id = pm.provider_model_id
                WHERE model.provider_id = ?
                LIMIT 1
                """,
                (provider_id,),
            ).fetchone()
            if pool_member is not None:
                raise AIProviderReferenceError(
                    "Provider 的模型仍被模型池引用，无法删除"
                )

            catalog_model = conn.execute(
                """
                SELECT 1 FROM ai_provider_models
                WHERE provider_id = ?
                LIMIT 1
                """,
                (provider_id,),
            ).fetchone()
            if catalog_model is not None:
                raise AIProviderReferenceError(
                    "Provider 仍有模型目录记录，无法删除"
                )
            conn.execute("DELETE FROM ai_providers WHERE id = ?", (provider_id,))

    def _row_to_ai_agent(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        try:
            capabilities = json.loads(item.get("required_capabilities_json") or "[]")
        except (TypeError, ValueError):
            capabilities = []
        item["required_capabilities"] = (
            capabilities if isinstance(capabilities, list) else []
        )
        item.pop("required_capabilities_json", None)
        item["binding_version"] = int(item.get("binding_version") or 1)
        item["model_pool_name"] = item.get("model_pool_name")
        if item.get("binding_type") == "pool":
            pool_name = item.get("model_pool_name") or f"#{item.get('model_pool_id')}"
            item["binding_summary"] = f"模型池：{pool_name}"
        else:
            provider_name = item.get("provider_name") or f"#{item.get('provider_id')}"
            model_name = item.get("model") or "默认模型"
            item["binding_summary"] = f"固定：{provider_name} / {model_name}"
        return item

    def list_ai_agents(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT a.*, p.name AS provider_name, p.provider_type AS provider_type,
                   mp.name AS model_pool_name
            FROM ai_agents a
            LEFT JOIN ai_providers p ON p.id = a.provider_id
            LEFT JOIN ai_model_pools mp ON mp.id = a.model_pool_id
            ORDER BY a.id DESC
            """
        ).fetchall()
        return [self._row_to_ai_agent(row) for row in rows]

    def get_ai_agent(self, agent_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT a.*, p.name AS provider_name, p.provider_type AS provider_type,
                   mp.name AS model_pool_name
            FROM ai_agents a
            LEFT JOIN ai_providers p ON p.id = a.provider_id
            LEFT JOIN ai_model_pools mp ON mp.id = a.model_pool_id
            WHERE a.id = ?
            """,
            (agent_id,),
        ).fetchone()
        return self._row_to_ai_agent(row) if row else None

    def create_ai_agent(self, data: dict[str, Any]) -> int:
        provider_id = data.get("provider_id")
        model_pool_id = data.get("model_pool_id")
        capabilities = sorted(data.get("required_capabilities") or [])
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO ai_agents (
                    name, task_type, binding_type, provider_id, model, model_pool_id,
                    required_capabilities_json, binding_version, system_prompt,
                    temperature, top_p, max_tokens, context_window, enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("name"), data.get("task_type"),
                    data.get("binding_type") or "fixed",
                    int(provider_id) if provider_id is not None else None,
                    data.get("model"),
                    int(model_pool_id) if model_pool_id is not None else None,
                    json.dumps(capabilities, ensure_ascii=False, separators=(",", ":")),
                    int(data.get("binding_version") or 1),
                    data.get("system_prompt"), float(data.get("temperature") or 0.8),
                    float(data.get("top_p") or 0.9), int(data.get("max_tokens") or 4000),
                    int(data.get("context_window") or 16000), 1 if data.get("enabled", True) else 0,
                ),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_ai_agent(self, agent_id: int, data: dict[str, Any]) -> None:
        allowed = {
            "name", "task_type", "binding_type", "provider_id", "model",
            "model_pool_id", "required_capabilities", "system_prompt",
            "temperature", "top_p", "max_tokens", "context_window", "enabled",
        }
        fields: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key not in data:
                continue
            value = data[key]
            column = key
            if key in {"provider_id", "model_pool_id"}:
                value = int(value) if value is not None else None
            elif key in {"max_tokens", "context_window"}:
                value = int(value)
            elif key in {"temperature", "top_p"}:
                value = float(value)
            elif key == "enabled":
                value = 1 if value else 0
            elif key == "required_capabilities":
                column = "required_capabilities_json"
                value = json.dumps(
                    sorted(value or []),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            fields.append(f"{column} = ?")
            params.append(value)
        fields.append("binding_version = binding_version + 1")
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(agent_id)
        with self._lock:
            self.conn.execute(f"UPDATE ai_agents SET {', '.join(fields)} WHERE id = ?", params)
            self._commit_if_needed()

    def delete_ai_agent(self, agent_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_agents WHERE id = ?", (agent_id,))
            self._commit_if_needed()

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["attempt_index"] = int(item["attempt_index"])
        item["output_started"] = bool(item.get("output_started"))
        item.pop("owner_token", None)
        item.pop("lease_until", None)
        item.pop("heartbeat_at", None)
        return item

    def list_ai_job_model_attempts(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM ai_job_model_attempts
            WHERE job_id = ?
            ORDER BY attempt_index
            """,
            (job_id,),
        ).fetchall()
        return [self._attempt_from_row(row) for row in rows]

    @staticmethod
    def _route_summary(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not attempts:
            return None
        main_attempts = [attempt for attempt in attempts if attempt.get("stage") == "main"]
        selected = (main_attempts or attempts)[-1]
        return {
            "attempt_index": selected["attempt_index"],
            "stage": selected.get("stage"),
            "status": selected.get("status"),
            "pool_id": selected.get("pool_id"),
            "pool_name": selected.get("pool_name_snapshot"),
            "provider_id": selected.get("provider_id"),
            "provider_name": selected.get("provider_name_snapshot"),
            "provider_model_id": selected.get("provider_model_id"),
            "model_key": selected.get("model_key"),
        }

    def _ai_job_from_row(
        self,
        row: sqlite3.Row,
        *,
        include_attempts: bool,
    ) -> dict[str, Any]:
        item = dict(row)
        for raw_key, public_key in (
            ("input_json", "input"),
            ("output_json", "output"),
            ("candidate_snapshot_json", "candidate_snapshot"),
            ("prompt_budget_json", "prompt_budget"),
        ):
            raw_value = item.get(raw_key)
            if raw_value:
                try:
                    item[public_key] = json.loads(raw_value)
                except (TypeError, ValueError):
                    item[public_key] = None
            elif public_key in {"candidate_snapshot", "prompt_budget"}:
                item[public_key] = None
        item.pop("candidate_snapshot_json", None)
        item.pop("prompt_budget_json", None)
        item.pop("owner_token", None)
        item.pop("owner_scope", None)
        item.pop("lease_until", None)
        item.pop("heartbeat_at", None)
        for key in (
            "next_attempt_index",
            "network_request_count",
            "candidate_attempt_count",
        ):
            item[key] = int(item.get(key) or 0)
        if include_attempts:
            attempts = self.list_ai_job_model_attempts(str(item["job_id"]))
            item["attempts"] = attempts
            item["route_summary"] = self._route_summary(attempts)
        return item

    def create_ai_job(
        self,
        job_id: str,
        task_type: str,
        agent_id: int | None,
        input_data: dict[str, Any],
        *,
        owner_token: str | None = None,
        stage: str = "main",
        route_deadline_at: str | None = None,
        parent_job_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        if stage not in _JOB_STAGES:
            raise ValueError("AI job stage 无效")
        if owner_token is not None:
            owner_token = _bounded_text(owner_token, "owner_token", 256)
            if not owner_token:
                raise ValueError("owner_token 不能为空")
        if route_deadline_at is not None:
            route_deadline_at = _validate_timestamp(
                route_deadline_at,
                "route_deadline_at",
            )
        if parent_job_id is not None:
            parent_job_id = _bounded_text(parent_job_id, "parent_job_id", 128)
        if idempotency_key is not None:
            idempotency_key = _bounded_text(idempotency_key, "idempotency_key", 128)
        try:
            serialized_input = json.dumps(input_data, ensure_ascii=False)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("AI job input_data 无法序列化") from exc
        now = _utc_now()
        heartbeat_at = _sql_timestamp(now) if owner_token is not None else None
        lease_until = (
            _sql_timestamp(now + timedelta(seconds=45))
            if owner_token is not None
            else None
        )
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO ai_jobs (
                    job_id, task_type, agent_id, status, input_json,
                    owner_token, lease_until, heartbeat_at, stage,
                    route_deadline_at, parent_job_id, idempotency_key,
                    started_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    task_type,
                    agent_id,
                    serialized_input,
                    owner_token,
                    lease_until,
                    heartbeat_at,
                    stage,
                    route_deadline_at,
                    parent_job_id,
                    idempotency_key,
                    _sql_timestamp(now),
                ),
            )

    def create_or_get_ai_resume_job(
        self,
        job_id: str,
        task_type: str,
        agent_id: int,
        input_data: dict[str, Any],
        *,
        owner_token: str,
        route_deadline_at: str,
        parent_job_id: str,
        idempotency_key: str,
        candidate_snapshot: Mapping[str, Any],
        candidate_snapshot_hash: str,
        resume_candidate_index: int,
    ) -> tuple[dict[str, Any], bool]:
        """原子创建幂等继续任务 child，存在时返回原记录。"""

        job_id = _bounded_text(job_id, "job_id", 128)
        task_type = _bounded_text(task_type, "task_type", 100)
        parent_job_id = _bounded_text(parent_job_id, "parent_job_id", 128)
        idempotency_key = _bounded_text(idempotency_key, "idempotency_key", 128)
        owner_token = _bounded_text(owner_token, "owner_token", 256)
        route_deadline_at = _validate_timestamp(
            route_deadline_at,
            "route_deadline_at",
        )
        if not job_id or not task_type or not parent_job_id or not idempotency_key:
            raise ValueError("继续任务标识不能为空")
        if not owner_token:
            raise ValueError("owner_token 不能为空")
        if isinstance(agent_id, bool) or not isinstance(agent_id, int) or agent_id <= 0:
            raise ValueError("agent_id 必须是正整数")
        if (
            isinstance(resume_candidate_index, bool)
            or not isinstance(resume_candidate_index, int)
            or resume_candidate_index < 0
        ):
            raise ValueError("resume_candidate_index 必须是非负整数")
        if input_data.get("resume_candidate_index") != resume_candidate_index:
            raise ValueError("继续任务输入中的候选索引不匹配")
        try:
            serialized_input = json.dumps(
                input_data,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("AI job input_data 无法序列化") from exc
        serialized_snapshot, computed_hash = _canonical_snapshot(candidate_snapshot)
        expected_hash = _validate_hash(
            candidate_snapshot_hash,
            "候选快照摘要",
        )
        if computed_hash != expected_hash:
            raise ValueError("候选快照摘要不匹配")

        now = _utc_now()
        heartbeat_at = _sql_timestamp(now)
        lease_until = _sql_timestamp(now + timedelta(seconds=45))
        with self.transaction() as conn:
            existing = conn.execute(
                """
                SELECT * FROM ai_jobs
                WHERE parent_job_id = ? AND idempotency_key = ?
                """,
                (parent_job_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["task_type"] != task_type
                    or existing["agent_id"] != agent_id
                    or existing["candidate_snapshot_hash"] != expected_hash
                    or existing["input_json"] != serialized_input
                ):
                    raise AIJobConflictError("幂等键已用于不同的继续请求")
                return self._private_ai_job_from_row(existing), False

            parent = conn.execute(
                """
                SELECT status, agent_id, candidate_snapshot_json,
                       candidate_snapshot_hash
                FROM ai_jobs WHERE job_id = ?
                """,
                (parent_job_id,),
            ).fetchone()
            if parent is None:
                raise AIJobConflictError("父 AI job 不存在")
            if parent["status"] not in _JOB_TERMINAL_STATUSES:
                raise AIJobConflictError("父 AI job 尚未进入终态")
            if parent["agent_id"] != agent_id:
                raise AIJobConflictError("父 AI job 的 Agent 不匹配")
            if (
                parent["candidate_snapshot_hash"] != expected_hash
                or parent["candidate_snapshot_json"] != serialized_snapshot
            ):
                raise AIJobConflictError("父 AI job 候选快照已变化")

            conn.execute(
                """
                INSERT INTO ai_jobs (
                    job_id, task_type, agent_id, status, input_json,
                    candidate_snapshot_json, candidate_snapshot_hash,
                    owner_token, lease_until, heartbeat_at, stage,
                    route_deadline_at, parent_job_id, idempotency_key,
                    started_at
                ) VALUES (
                    ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, 'main', ?, ?, ?, ?
                )
                """,
                (
                    job_id,
                    task_type,
                    agent_id,
                    serialized_input,
                    serialized_snapshot,
                    expected_hash,
                    owner_token,
                    lease_until,
                    heartbeat_at,
                    route_deadline_at,
                    parent_job_id,
                    idempotency_key,
                    heartbeat_at,
                ),
            )
            created = conn.execute(
                "SELECT * FROM ai_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if created is None:
                raise RuntimeError("继续任务 child job 创建失败")
            return self._private_ai_job_from_row(created), True

    def update_ai_job(
        self,
        job_id: str,
        status: str,
        output_text: str | None = None,
        output_json: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        serialized_output = (
            json.dumps(output_json, ensure_ascii=False)
            if output_json is not None
            else None
        )
        safe_error = _safe_error_text(error_message) if error_message is not None else None
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE ai_jobs
                SET status = ?, output_text = COALESCE(?, output_text),
                    output_json = COALESCE(?, output_json), error_message = ?,
                    finished_at = CASE
                        WHEN ? IN ('succeeded', 'failed', 'partial', 'cancelled')
                        THEN CURRENT_TIMESTAMP ELSE finished_at END,
                    lease_until = CASE
                        WHEN ? IN ('succeeded', 'failed', 'partial', 'cancelled')
                        THEN NULL ELSE lease_until END
                WHERE job_id = ? AND status = 'running'
                """,
                (
                    status,
                    output_text,
                    serialized_output,
                    safe_error,
                    status,
                    status,
                    job_id,
                ),
            )

    def get_ai_job(
        self,
        job_id: str,
        owner_scope: str | None = None,
    ) -> dict[str, Any] | None:
        if owner_scope is None:
            query = "SELECT * FROM ai_jobs WHERE job_id = ?"
            params = (job_id,)
        else:
            query = f"""
                SELECT * FROM ai_jobs
                WHERE job_id = ?
                  AND (task_type NOT IN ({_ADULT_AI_TASK_TYPES_SQL}) OR owner_scope = ?)
            """
            params = (job_id, owner_scope)
        row = self.conn.execute(query, params).fetchone()
        if row is None:
            return None
        return self._ai_job_from_row(row, include_attempts=True)

    def _private_ai_job_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        owner_token = row["owner_token"]
        item = self._ai_job_from_row(row, include_attempts=True)
        item["owner_token"] = owner_token
        return item

    def get_ai_resume_job_execution_state(
        self,
        parent_job_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM ai_jobs
            WHERE parent_job_id = ? AND idempotency_key = ?
            """,
            (parent_job_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return self._private_ai_job_from_row(row)

    def get_ai_job_route_state(
        self,
        job_id: str,
        owner_token: str,
    ) -> dict[str, Any] | None:
        """Return the private execution state only to the matching owner."""

        row = self.conn.execute(
            """
            SELECT status, stage, agent_id, pinned_candidate_index,
                   network_request_count, candidate_attempt_count,
                   route_deadline_at, candidate_snapshot_hash
            FROM ai_jobs
            WHERE job_id = ? AND owner_token = ?
            """,
            (job_id, owner_token),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        for key in ("network_request_count", "candidate_attempt_count"):
            item[key] = int(item.get(key) or 0)
        if item.get("pinned_candidate_index") is not None:
            item["pinned_candidate_index"] = int(item["pinned_candidate_index"])
        return item

    def list_ai_jobs(
        self,
        task_type: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
        owner_scope: str | None = None,
    ) -> dict[str, Any]:
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
        if owner_scope is not None:
            conditions.append(
                f"(task_type NOT IN ({_ADULT_AI_TASK_TYPES_SQL}) OR owner_scope = ?)"
            )
            params.append(owner_scope)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        total = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM ai_jobs {where}",
                params,
            ).fetchone()[0]
        )
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            f"SELECT * FROM ai_jobs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
        return {
            "items": [
                self._ai_job_from_row(row, include_attempts=False) for row in rows
            ],
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }

    def set_ai_job_candidate_snapshot(
        self,
        job_id: str,
        owner_token: str,
        snapshot_json: Any,
        snapshot_hash: str,
    ) -> bool:
        serialized, computed_hash = _canonical_snapshot(snapshot_json)
        expected_hash = _validate_hash(snapshot_hash, "候选快照摘要")
        if computed_hash != expected_hash:
            raise ValueError("候选快照摘要不匹配")
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE ai_jobs
                SET candidate_snapshot_json = ?, candidate_snapshot_hash = ?
                WHERE job_id = ? AND status = 'running' AND owner_token = ?
                  AND candidate_snapshot_json IS NULL
                  AND candidate_snapshot_hash IS NULL
                """,
                (serialized, expected_hash, job_id, owner_token),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _attempt_value(
        data: dict[str, Any],
        field: str,
        *,
        positive: bool = True,
    ) -> int | None:
        value = data.get(field)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field} 必须是整数")
        if positive and value <= 0:
            raise ValueError(f"{field} 必须是正整数")
        return value

    def allocate_ai_model_attempt(
        self,
        job_id: str,
        owner_token: str,
        data: dict[str, Any],
    ) -> int:
        unknown = sorted(set(data) - _ATTEMPT_FIELDS)
        if unknown:
            raise ValueError(f"不允许提交 attempt 字段：{', '.join(unknown)}")
        model_key = _bounded_text(data.get("model_key"), "model_key", 300)
        if not model_key or len(model_key.encode("utf-8")) > 1200:
            raise ValueError("model_key 不能为空且不能超过 1200 个 UTF-8 字节")
        stage = data.get("stage", "main")
        if stage not in _JOB_STAGES:
            raise ValueError("attempt stage 无效")
        provider_name = data.get("provider_name_snapshot")
        if provider_name is not None:
            provider_name = _bounded_text(
                provider_name,
                "provider_name_snapshot",
                200,
            )
        pool_name = data.get("pool_name_snapshot")
        if pool_name is not None:
            pool_name = _bounded_text(pool_name, "pool_name_snapshot", 100)
        lease_until = data.get("lease_until")
        if lease_until is not None:
            lease_until = _validate_timestamp(lease_until, "lease_until")
        normalized = {
            "pool_id": self._attempt_value(data, "pool_id"),
            "provider_id": self._attempt_value(data, "provider_id"),
            "provider_model_id": self._attempt_value(data, "provider_model_id"),
            "pool_version_snapshot": self._attempt_value(
                data,
                "pool_version_snapshot",
            ),
            "pool_position_snapshot": self._attempt_value(
                data,
                "pool_position_snapshot",
            ),
            "model_key": model_key,
            "pool_name_snapshot": pool_name,
            "provider_name_snapshot": provider_name,
            "agent_config_hash": _validate_hash(
                data.get("agent_config_hash"),
                "agent_config_hash",
            ),
            "provider_config_hash": _validate_hash(
                data.get("provider_config_hash"),
                "provider_config_hash",
            ),
            "candidate_list_hash": _validate_hash(
                data.get("candidate_list_hash"),
                "candidate_list_hash",
            ),
            "stage": stage,
            "lease_until": lease_until,
        }
        if normalized["provider_id"] is None:
            raise ValueError("provider_id 必须是正整数")
        with self.transaction() as conn:
            job = conn.execute(
                """
                SELECT status, owner_token, next_attempt_index,
                       candidate_attempt_count, lease_until, stage
                FROM ai_jobs WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if (
                job is None
                or job["status"] != "running"
                or job["owner_token"] != owner_token
            ):
                raise AIJobConflictError("AI job 已终结或 owner 不匹配")
            if int(job["candidate_attempt_count"] or 0) >= 16:
                raise AIRouteBudgetExhausted("候选尝试次数已达到 16 次上限")
            attempt_index = int(job["next_attempt_index"] or 0)
            attempt_lease = normalized["lease_until"] or job["lease_until"]
            conn.execute(
                """
                INSERT INTO ai_job_model_attempts (
                    job_id, attempt_index, pool_id, provider_id,
                    provider_model_id, pool_version_snapshot,
                    pool_position_snapshot, model_key, pool_name_snapshot,
                    provider_name_snapshot, agent_config_hash,
                    provider_config_hash, candidate_list_hash, stage, status,
                    output_started, owner_token, lease_until, heartbeat_at,
                    started_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running',
                    0, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """,
                (
                    job_id,
                    attempt_index,
                    normalized["pool_id"],
                    normalized["provider_id"],
                    normalized["provider_model_id"],
                    normalized["pool_version_snapshot"],
                    normalized["pool_position_snapshot"],
                    normalized["model_key"],
                    normalized["pool_name_snapshot"],
                    normalized["provider_name_snapshot"],
                    normalized["agent_config_hash"],
                    normalized["provider_config_hash"],
                    normalized["candidate_list_hash"],
                    normalized["stage"],
                    owner_token,
                    attempt_lease,
                ),
            )
            conn.execute(
                """
                UPDATE ai_jobs
                SET next_attempt_index = next_attempt_index + 1,
                    candidate_attempt_count = candidate_attempt_count + 1
                WHERE job_id = ? AND status = 'running' AND owner_token = ?
                """,
                (job_id, owner_token),
            )
            return attempt_index

    def claim_ai_job_network_request(
        self,
        job_id: str,
        owner_token: str,
    ) -> int:
        with self.transaction() as conn:
            job = conn.execute(
                """
                SELECT status, owner_token, network_request_count
                FROM ai_jobs WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if (
                job is None
                or job["status"] != "running"
                or job["owner_token"] != owner_token
            ):
                raise AIJobConflictError("AI job 已终结或 owner 不匹配")
            current = int(job["network_request_count"] or 0)
            if current >= 32:
                raise AIRouteBudgetExhausted("网络请求次数已达到 32 次上限")
            next_count = current + 1
            conn.execute(
                """
                UPDATE ai_jobs SET network_request_count = ?
                WHERE job_id = ? AND status = 'running' AND owner_token = ?
                """,
                (next_count, job_id, owner_token),
            )
            return next_count

    def heartbeat_ai_job(
        self,
        job_id: str,
        owner_token: str,
        lease_until: str,
    ) -> bool:
        safe_lease = _validate_timestamp(lease_until, "lease_until")
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE ai_jobs
                SET lease_until = ?, heartbeat_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND status = 'running' AND owner_token = ?
                """,
                (safe_lease, job_id, owner_token),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                """
                UPDATE ai_job_model_attempts
                SET lease_until = ?, heartbeat_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND status = 'running' AND owner_token = ?
                """,
                (safe_lease, job_id, owner_token),
            )
            return True

    def mark_ai_job_output_started(
        self,
        job_id: str,
        attempt_index: int,
        owner_token: str,
        candidate_index: int,
    ) -> bool:
        """Atomically mark the first main body delta and persist its pin."""

        if (
            isinstance(candidate_index, bool)
            or not isinstance(candidate_index, int)
            or candidate_index < 0
        ):
            raise ValueError("candidate_index 必须是非负整数")
        with self.transaction() as conn:
            job = conn.execute(
                """
                SELECT pinned_candidate_index
                FROM ai_jobs
                WHERE job_id = ? AND status = 'running' AND owner_token = ?
                """,
                (job_id, owner_token),
            ).fetchone()
            attempt = conn.execute(
                """
                SELECT stage
                FROM ai_job_model_attempts
                WHERE job_id = ? AND attempt_index = ?
                  AND status = 'running' AND owner_token = ?
                """,
                (job_id, int(attempt_index), owner_token),
            ).fetchone()
            if job is None or attempt is None or attempt["stage"] != "main":
                return False
            pinned = job["pinned_candidate_index"]
            if pinned is not None and int(pinned) != candidate_index:
                return False
            job_cursor = conn.execute(
                """
                UPDATE ai_jobs
                SET pinned_candidate_index = COALESCE(
                        pinned_candidate_index, ?
                    ),
                    heartbeat_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND status = 'running' AND owner_token = ?
                  AND (
                      pinned_candidate_index IS NULL
                      OR pinned_candidate_index = ?
                  )
                """,
                (candidate_index, job_id, owner_token, candidate_index),
            )
            if job_cursor.rowcount != 1:
                return False
            attempt_cursor = conn.execute(
                """
                UPDATE ai_job_model_attempts
                SET output_started = 1, heartbeat_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND attempt_index = ?
                  AND status = 'running' AND owner_token = ?
                """,
                (job_id, int(attempt_index), owner_token),
            )
            return attempt_cursor.rowcount == 1

    def finish_ai_model_attempt(
        self,
        job_id: str,
        attempt_index: int,
        owner_token: str,
        status: str,
        **fields: Any,
    ) -> bool:
        if status not in _JOB_TERMINAL_STATUSES:
            raise ValueError("attempt 终态无效")
        unknown = sorted(set(fields) - _ATTEMPT_FINISH_FIELDS)
        if unknown:
            raise ValueError(f"不允许提交 attempt 终态字段：{', '.join(unknown)}")
        assignments = ["status = ?"]
        params: list[Any] = [status]
        if "error_scope" in fields:
            scope = fields["error_scope"]
            if scope not in {None, "model", "provider"}:
                raise ValueError("error_scope 无效")
            assignments.append("error_scope = ?")
            params.append(scope)
        if "error_message" in fields:
            assignments.append("error_message = ?")
            params.append(_safe_error_text(fields["error_message"]))
        if "error_category" in fields:
            category = fields["error_category"]
            if category is not None:
                category = _bounded_text(category, "error_category", 100)
            assignments.append("error_category = ?")
            params.append(category)
        if "finish_reason" in fields:
            reason = fields["finish_reason"]
            if reason not in {
                None,
                "stop",
                "complete",
                "length",
                "content_filter",
                "missing",
                "cancelled",
                "error",
            }:
                raise ValueError("finish_reason 无效")
            assignments.append("finish_reason = ?")
            params.append(reason)
        if "output_started" in fields:
            output_started = fields["output_started"]
            if output_started not in {True, False, 0, 1}:
                raise ValueError("output_started 必须是布尔值")
            assignments.append("output_started = ?")
            params.append(1 if output_started else 0)
        if "latency_ms" in fields:
            latency = fields["latency_ms"]
            if isinstance(latency, bool) or not isinstance(latency, int) or latency < 0:
                raise ValueError("latency_ms 必须是非负整数")
            assignments.append("latency_ms = ?")
            params.append(latency)
        assignments.extend(
            [
                "finished_at = CURRENT_TIMESTAMP",
                "lease_until = NULL",
                "heartbeat_at = CURRENT_TIMESTAMP",
            ]
        )
        with self.transaction() as conn:
            if status == "partial":
                attempt = conn.execute(
                    """
                    SELECT stage, output_started FROM ai_job_model_attempts
                    WHERE job_id = ? AND attempt_index = ?
                      AND status = 'running' AND owner_token = ?
                    """,
                    (job_id, int(attempt_index), owner_token),
                ).fetchone()
                if attempt is not None and attempt["stage"] != "main":
                    raise ValueError("只有 main attempt 可以收口为 partial")
                if attempt is not None:
                    effective_output_started = fields.get(
                        "output_started",
                        bool(attempt["output_started"]),
                    )
                    if not bool(effective_output_started):
                        raise ValueError("正文尚未开始，不能收口为 partial")
            cursor = conn.execute(
                f"""
                UPDATE ai_job_model_attempts
                SET {', '.join(assignments)}
                WHERE job_id = ? AND attempt_index = ?
                  AND status = 'running' AND owner_token = ?
                  AND EXISTS (
                      SELECT 1 FROM ai_jobs
                      WHERE ai_jobs.job_id = ai_job_model_attempts.job_id
                        AND ai_jobs.status = 'running'
                        AND ai_jobs.owner_token = ?
                  )
                """,
                [*params, job_id, int(attempt_index), owner_token, owner_token],
            )
            return cursor.rowcount == 1

    def finish_ai_job_cas(
        self,
        job_id: str,
        owner_token: str,
        status: str,
        **fields: Any,
    ) -> bool:
        if status not in _JOB_TERMINAL_STATUSES:
            raise ValueError("AI job 终态无效")
        allowed = {
            "output_text",
            "output_json",
            "error_message",
            "pinned_candidate_index",
        }
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ValueError(f"不允许提交 AI job 终态字段：{', '.join(unknown)}")
        assignments = ["status = ?"]
        params: list[Any] = [status]
        if "output_text" in fields:
            assignments.append("output_text = ?")
            params.append(fields["output_text"])
        if "output_json" in fields:
            try:
                output_json = json.dumps(fields["output_json"], ensure_ascii=False)
            except (TypeError, ValueError, UnicodeError) as exc:
                raise ValueError("output_json 无法序列化") from exc
            assignments.append("output_json = ?")
            params.append(output_json)
        if "error_message" in fields:
            assignments.append("error_message = ?")
            params.append(_safe_error_text(fields["error_message"]))
        if "pinned_candidate_index" in fields:
            pinned = fields["pinned_candidate_index"]
            if isinstance(pinned, bool) or not isinstance(pinned, int) or pinned < 0:
                raise ValueError("pinned_candidate_index 必须是非负整数")
            assignments.append("pinned_candidate_index = ?")
            params.append(pinned)
        assignments.extend(["finished_at = CURRENT_TIMESTAMP", "lease_until = NULL"])
        with self.transaction() as conn:
            if status == "partial":
                job = conn.execute(
                    """
                    SELECT stage, output_text FROM ai_jobs
                    WHERE job_id = ? AND status = 'running' AND owner_token = ?
                    """,
                    (job_id, owner_token),
                ).fetchone()
                if job is not None and job["stage"] != "main":
                    raise ValueError("只有 main job 可以收口为 partial")
                if job is not None:
                    effective_output = fields.get("output_text", job["output_text"])
                    if not isinstance(effective_output, str) or not effective_output:
                        raise ValueError("正文尚未开始，不能收口为 partial")
            cursor = conn.execute(
                f"""
                UPDATE ai_jobs SET {', '.join(assignments)}
                WHERE job_id = ? AND status = 'running' AND owner_token = ?
                  AND (? != 'partial' OR stage = 'main')
                """,
                [*params, job_id, owner_token, status],
            )
            return cursor.rowcount == 1

    def delete_ai_job(self, job_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_jobs WHERE job_id = ?", (job_id,))
            self._commit_if_needed()

    def cleanup_ai_jobs(
        self,
        keep_days: int = 3,
        keep_failed_days: int | None = None,
        owner_scope: str | None = None,
    ) -> int:
        """清理 ai_jobs：与统一任务日志（task_logs）一致，默认保留最近 3 天，失败任务可单独配置保留天数。

        返回删除的行数。
        """
        if keep_failed_days is None:
            keep_failed_days = keep_days
        with self.transaction() as conn:
            general_task_condition = "task_type != 'adult_polish'"
            if owner_scope is None:
                deleted = self._cleanup_adult_jobs_locked(
                    conn,
                    keep_days,
                    keep_failed_days,
                )
            else:
                conn.execute(
                    """
                    DELETE FROM ai_polish_applications
                    WHERE owner_scope = ? AND applied_at IS NULL
                      AND created_at < datetime('now', ? || ' days')
                    """,
                    (owner_scope, f"-{int(keep_days)}"),
                )
                adult_cursor = conn.execute(
                    f"""
                    DELETE FROM ai_jobs
                    WHERE task_type IN ({_ADULT_AI_TASK_TYPES_SQL}) AND owner_scope = ?
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
                    (
                        owner_scope,
                        f"-{int(keep_days)}",
                        f"-{int(keep_failed_days)}",
                    ),
                )
                deleted = int(adult_cursor.rowcount or 0)
                general_task_condition = (
                    f"task_type NOT IN ({_ADULT_AI_TASK_TYPES_SQL})"
                )
            cur = conn.execute(
                f"""
                DELETE FROM ai_jobs
                WHERE {general_task_condition}
                  AND ((status IN ('succeeded', 'partial', 'done', 'completed', 'success')
                       AND created_at < datetime('now', ? || ' days'))
                   OR (status IN ('failed', 'error', 'cancelled')
                       AND created_at < datetime('now', ? || ' days')))
                """,
                (f"-{int(keep_days)}", f"-{int(keep_failed_days)}"),
            )
            return deleted + int(cur.rowcount or 0)

    def request_adult_job_cancel(
        self,
        job_id: str,
        owner_scope: str,
        owner_token: str | None = None,
    ) -> bool:
        """Cancel an owner-scoped adult job without racing a committed candidate."""

        if owner_token is not None and not owner_token:
            return False

        with self.transaction() as conn:
            token_condition = " AND owner_token = ?" if owner_token is not None else ""
            params: tuple[Any, ...] = (
                (job_id, owner_scope, owner_token)
                if owner_token is not None
                else (job_id, owner_scope)
            )
            row = conn.execute(
                f"""
                SELECT owner_token FROM ai_jobs
                WHERE job_id = ? AND task_type = 'adult_polish'
                  AND owner_scope = ? AND status = 'running'
                  {token_condition}
                """,
                params,
            ).fetchone()
            if row is None:
                return False
            cursor = conn.execute(
                f"""
                UPDATE ai_jobs
                SET status = 'cancelled', output_text = NULL,
                    output_json = '{{"code":"cancelled"}}',
                    error_message = '成人润色任务已取消',
                    finished_at = CURRENT_TIMESTAMP, lease_until = NULL,
                    heartbeat_at = CURRENT_TIMESTAMP
                WHERE job_id = ? AND task_type = 'adult_polish'
                  AND owner_scope = ? AND status = 'running'
                  {token_condition}
                  AND NOT EXISTS (
                    SELECT 1 FROM ai_polish_applications AS application
                    WHERE application.source_job_id = ai_jobs.job_id
                  )
                """,
                params,
            )
            if cursor.rowcount != 1:
                return False
            active_owner_token = row["owner_token"]
            if active_owner_token:
                conn.execute(
                    """
                    UPDATE ai_job_model_attempts
                    SET status = 'cancelled', error_category = 'cancelled',
                        error_message = '成人润色任务已取消',
                        finish_reason = 'cancelled',
                        finished_at = CURRENT_TIMESTAMP, lease_until = NULL,
                        heartbeat_at = CURRENT_TIMESTAMP
                    WHERE job_id = ? AND owner_token = ? AND status = 'running'
                    """,
                    (job_id, active_owner_token),
                )
            return True

    def bind_adult_application_access(
        self,
        job_id: str,
        owner_scope: str,
        access_token_hash: str,
    ) -> dict[str, Any] | None:
        """Bind a verified route token by hash without storing the token itself."""

        safe_hash = _validate_hash(access_token_hash, "access_token_hash")
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT application.id, application.applied_at,
                       application.chapter_revision_after,
                       application.chapter_hash_after
                FROM ai_polish_applications AS application
                JOIN ai_jobs AS job
                  ON job.job_id = application.source_job_id
                WHERE application.source_job_id = ?
                  AND application.owner_scope = ?
                  AND job.task_type = 'adult_polish'
                  AND job.owner_scope = ?
                  AND job.status = 'succeeded'
                """,
                (job_id, owner_scope, owner_scope),
            ).fetchone()
            if row is None:
                return None
            if row["applied_at"] is not None:
                return {
                    "applied": True,
                    "application_id": int(row["id"]),
                    "chapter_revision_after": int(row["chapter_revision_after"]),
                    "chapter_hash_after": str(row["chapter_hash_after"]),
                }
            cursor = conn.execute(
                """
                UPDATE ai_polish_applications
                SET access_token_hash = ?
                WHERE source_job_id = ? AND owner_scope = ?
                  AND applied_at IS NULL AND applicable = 1
                """,
                (safe_hash, job_id, owner_scope),
            )
            if cursor.rowcount != 1:
                return None
            return {"applied": False, "application_id": int(row["id"])}

    def fail_stale_ai_jobs(
        self,
        older_than_minutes: int = 30,
        *,
        now: datetime | None = None,
        grace_seconds: int = 45,
    ) -> int:
        """按 owner lease/heartbeat 收口崩溃任务，并兼容旧 ownerless job。"""

        current = now or _utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        now_sql = _sql_timestamp(current)
        heartbeat_cutoff = _sql_timestamp(
            current - timedelta(seconds=max(1, int(grace_seconds)))
        )
        legacy_cutoff = _sql_timestamp(
            current - timedelta(minutes=max(1, int(older_than_minutes)))
        )
        interrupted_message = "任务中断（owner 租约与 heartbeat 已失效）"
        fixed = 0
        with self.transaction() as conn:
            repaired = conn.execute(
                """
                UPDATE ai_jobs
                SET status = 'failed', output_text = NULL,
                    output_json = '{"code":"orphaned_adult_candidate"}',
                    error_message = '成人润色候选记录不完整，请重新生成',
                    finished_at = COALESCE(finished_at, ?), lease_until = NULL,
                    heartbeat_at = ?
                WHERE task_type = 'adult_polish' AND status != 'running'
                  AND output_text IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM ai_polish_applications AS application
                    WHERE application.source_job_id = ai_jobs.job_id
                  )
                """,
                (now_sql, now_sql),
            )
            fixed += int(repaired.rowcount or 0)
            jobs = conn.execute(
                """
                SELECT * FROM ai_jobs
                WHERE status = 'running'
                  AND (
                    (
                      owner_token IS NOT NULL
                      AND lease_until IS NOT NULL AND lease_until <= ?
                      AND (heartbeat_at IS NULL OR heartbeat_at <= ?)
                    )
                    OR (
                      owner_token IS NULL AND created_at <= ?
                    )
                  )
                ORDER BY created_at, job_id
                """,
                (now_sql, heartbeat_cutoff, legacy_cutoff),
            ).fetchall()
            for job in jobs:
                owner = job["owner_token"]
                if owner is None:
                    owner_clause = "owner_token IS NULL"
                    owner_params: list[Any] = []
                else:
                    owner_clause = "owner_token = ?"
                    owner_params = [owner]
                attempts = conn.execute(
                    f"""
                    SELECT attempt_index, stage, output_started, status
                    FROM ai_job_model_attempts
                    WHERE job_id = ? AND {owner_clause}
                    ORDER BY attempt_index
                    """,
                    [job["job_id"], *owner_params],
                ).fetchall()
                output_started = any(
                    attempt["stage"] == "main" and bool(attempt["output_started"])
                    for attempt in attempts
                )
                for attempt in attempts:
                    if attempt["status"] != "running":
                        continue
                    attempt_status = (
                        "partial"
                        if attempt["stage"] == "main"
                        and bool(attempt["output_started"])
                        else "failed"
                    )
                    conn.execute(
                        f"""
                        UPDATE ai_job_model_attempts
                        SET status = ?, error_category = 'process_interrupted',
                            error_message = ?, finish_reason = 'error',
                            finished_at = ?, lease_until = NULL,
                            heartbeat_at = ?
                        WHERE job_id = ? AND attempt_index = ?
                          AND status = 'running' AND {owner_clause}
                        """,
                        [
                            attempt_status,
                            interrupted_message,
                            now_sql,
                            now_sql,
                            job["job_id"],
                            attempt["attempt_index"],
                            *owner_params,
                        ],
                    )
                job_status = (
                    "partial"
                    if job["stage"] == "main" and output_started
                    else "failed"
                )
                cursor = conn.execute(
                    f"""
                    UPDATE ai_jobs
                    SET status = ?,
                        error_message = COALESCE(
                            NULLIF(error_message, ''), ?
                        ),
                        finished_at = ?, lease_until = NULL,
                        heartbeat_at = ?
                    WHERE job_id = ? AND status = 'running'
                      AND {owner_clause}
                    """,
                    [
                        job_status,
                        interrupted_message,
                        now_sql,
                        now_sql,
                        job["job_id"],
                        *owner_params,
                    ],
                )
                fixed += int(cursor.rowcount or 0)
        return fixed

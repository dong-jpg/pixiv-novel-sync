from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from ...storage.ai.core import AIJobConflictError, AIProviderReferenceError
from ...storage_db import Database
from ..model_catalog import (
    ModelCatalogConflictError,
    ModelCatalogValidationError,
    normalize_capabilities,
)
from ..model_pools import (
    ModelPoolConflictError,
    ModelPoolValidationError,
    expand_pool_ids,
)
from ..model_router import CandidateSnapshot, ModelRouteConflictError, ModelRouter
from ..model_sync import ModelSyncConflictError
from ..models import AIAgentConfig, AIProviderConfig, AIStreamChunk
from ..providers import ProviderConfigError, validate_base_url
from ..prompts import (
    DEFAULT_CHAPTER_SUMMARY_PROMPT,
    DEFAULT_FORESHADOW_RESOLVE_PROMPT,
    DEFAULT_KEYWORD_CLEAN_PROMPT,
    DEFAULT_POLISH_DIALOGUE_PROMPT,
    DEFAULT_POLISH_PSYCHOLOGY_PROMPT,
    DEFAULT_WIZARD_PROMPT,
)
from .core import (
    AINotFoundError,
    AIConflictError,
    AIServiceError,
    RouteResumeSpec,
)


_MANUAL_MODEL_CREATE_FIELDS = {
    "model_key",
    "enabled",
    "manual_display_name",
    "manual_capabilities",
    "manual_context_window",
}
_MANUAL_MODEL_UPDATE_FIELDS = {
    "enabled",
    "manual_display_name",
    "manual_capabilities",
    "manual_context_window",
}
_MODEL_POOL_FIELDS = {
    "name",
    "description",
    "pool_kind",
    "fallback_pool_id",
    "enabled",
}
_RESUME_FIELDS = {
    "parent_job_id",
    "idempotency_key",
    "candidate_snapshot_hash",
    "resume_candidate_index",
}
_RESUME_TASK_DISPATCH = {
    "continue": "stream_continue",
    "rewrite": "stream_rewrite",
    "distill_style": "stream_distill_style",
    "distill_novel": "stream_distill_novel",
    "audit": "stream_audit",
    "plan": "stream_plan",
    "longform_plan": "stream_longform_plan",
    "longform_plan_details": "stream_longform_plan_details",
    "update_state": "stream_update_project_state",
    "extract_summary": "stream_extract_chapter_summary",
    "resolve_foreshadow": "stream_auto_resolve_foreshadows",
}
_RESUME_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_INTERNAL_AGENT_TASK_TYPES = frozenset(
    {"adult_safety_review", "adult_fact_guard"}
)
_FORBIDDEN_AGENT_POLICY_FIELDS = frozenset(
    {
        "policy_id",
        "policy_text",
        "output_schema",
        "safety_policy_hash",
        "validator_policy_hash",
        "binding_version",
    }
)


class AIAdminMixin:
    @staticmethod
    def _resume_request_payload(
        parent_job_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise AIServiceError("请求体必须是 JSON 对象")
        data = dict(payload)
        unknown = sorted(set(data) - _RESUME_FIELDS)
        if unknown:
            raise AIServiceError(f"不允许提交字段：{', '.join(unknown)}")
        if set(data) != _RESUME_FIELDS:
            missing = sorted(_RESUME_FIELDS - set(data))
            raise AIServiceError(f"继续请求缺少字段：{', '.join(missing)}")
        if not isinstance(parent_job_id, str) or not parent_job_id:
            raise AIServiceError("父 AI job 标识无效")
        body_parent = data["parent_job_id"]
        if body_parent != parent_job_id:
            raise AIServiceError("路径中的父 AI job 与请求体不一致")
        if (
            not isinstance(body_parent, str)
            or len(body_parent) > 128
            or any(ord(character) < 0x20 for character in body_parent)
        ):
            raise AIServiceError("父 AI job 标识无效")
        idempotency_key = data["idempotency_key"]
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.isascii()
            or not (16 <= len(idempotency_key) <= 128)
            or any(not (0x21 <= ord(character) <= 0x7E) for character in idempotency_key)
        ):
            raise AIServiceError("idempotency_key 必须是 16-128 位 ASCII 字符")
        snapshot_hash = data["candidate_snapshot_hash"]
        if (
            not isinstance(snapshot_hash, str)
            or _RESUME_HASH_PATTERN.fullmatch(snapshot_hash) is None
        ):
            raise AIServiceError("candidate_snapshot_hash 必须是 64 位小写十六进制摘要")
        resume_index = data["resume_candidate_index"]
        if (
            isinstance(resume_index, bool)
            or not isinstance(resume_index, int)
            or resume_index < 0
        ):
            raise AIServiceError("resume_candidate_index 必须是非负整数")
        return {
            "parent_job_id": body_parent,
            "idempotency_key": idempotency_key,
            "candidate_snapshot_hash": snapshot_hash,
            "resume_candidate_index": resume_index,
        }

    @staticmethod
    def _resume_next_candidate_index(
        snapshot: CandidateSnapshot,
        attempts: list[dict[str, Any]],
    ) -> int:
        attempted_indices: list[int] = []
        for attempt in attempts:
            if attempt.get("stage") != "main":
                continue
            if attempt.get("status") == "running":
                raise AIConflictError("父 AI job 仍有未完成的模型尝试")
            candidate_hash = attempt.get("candidate_list_hash")
            if candidate_hash != snapshot.snapshot_hash:
                raise AIConflictError("父 AI job 的尝试记录不属于当前候选快照")
            provider_id = attempt.get("provider_id")
            model_key = attempt.get("model_key")
            provider_model_id = attempt.get("provider_model_id")
            matches = [
                candidate
                for candidate in snapshot.candidates
                if candidate.provider_id == provider_id
                and candidate.model_key == model_key
                and (
                    provider_model_id is None
                    or candidate.provider_model_id == provider_model_id
                )
            ]
            if len(matches) != 1:
                raise AIConflictError("父 AI job 的尝试无法匹配候选快照")
            attempted_indices.append(matches[0].candidate_index)
        next_index = max(attempted_indices, default=-1) + 1
        if next_index >= len(snapshot.candidates):
            raise AIConflictError("候选快照没有未尝试的模型")
        return next_index

    def _replay_resume_child(
        self,
        db: Database,
        child_id: str,
    ) -> Iterator[AIStreamChunk]:
        child = db.get_ai_job(child_id)
        if child is None:
            raise AIConflictError("继续任务 child job 不存在")
        return self._stream_replayed_route_job(child)

    def stream_job_with_next_model(
        self,
        job_id: str,
        payload: Mapping[str, Any],
    ) -> Iterator[AIStreamChunk]:
        """在返回 SSE 前校验并准备手动候选继续任务。"""

        request_data = self._resume_request_payload(job_id, payload)
        db = self._db()
        try:
            parent = db.get_ai_job(job_id)
            if parent is None:
                raise AINotFoundError("父 AI job 不存在")
            if parent.get("status") not in {
                "succeeded",
                "failed",
                "partial",
                "cancelled",
            }:
                raise AIConflictError("父 AI job 尚未进入终态")
            if parent.get("candidate_snapshot_hash") != request_data[
                "candidate_snapshot_hash"
            ]:
                raise AIConflictError("候选快照摘要不匹配")
            snapshot_payload = parent.get("candidate_snapshot")
            if not isinstance(snapshot_payload, Mapping):
                raise AIConflictError("父 AI job 缺少候选快照")
            try:
                snapshot = ModelRouter.candidate_snapshot_from_payload(
                    snapshot_payload,
                    request_data["candidate_snapshot_hash"],
                )
            except ModelRouteConflictError as exc:
                raise AIConflictError(str(exc)) from exc
            expected_index = self._resume_next_candidate_index(
                snapshot,
                parent.get("attempts") or [],
            )
            if request_data["resume_candidate_index"] != expected_index:
                raise AIConflictError(
                    f"resume_candidate_index 必须是 {expected_index}"
                )

            task_type = str(parent.get("task_type") or "")
            dispatch_name = _RESUME_TASK_DISPATCH.get(task_type)
            if dispatch_name is None:
                raise AIConflictError("该任务类型暂不支持手动候选继续")
            dispatch = getattr(self, dispatch_name, None)
            if not callable(dispatch):
                raise AIConflictError("该任务类型缺少继续处理器")

            existing = db.get_ai_resume_job_execution_state(
                job_id,
                request_data["idempotency_key"],
            )
            if existing is not None:
                if existing.get("candidate_snapshot_hash") != request_data[
                    "candidate_snapshot_hash"
                ] or (
                    existing.get("input") or {}
                ).get("resume_candidate_index") != request_data[
                    "resume_candidate_index"
                ]:
                    raise AIConflictError("幂等键已用于不同的继续请求")
                return self._replay_resume_child(db, str(existing["job_id"]))

            agent_id = parent.get("agent_id")
            if isinstance(agent_id, bool) or not isinstance(agent_id, int) or agent_id <= 0:
                raise AIConflictError("父 AI job 缺少有效 Agent")
            try:
                agent = self._load_agent_config(db, agent_id)
                self.model_router.validate_resume_snapshot(
                    agent,
                    snapshot,
                    request_data["resume_candidate_index"],
                )
            except ModelRouteConflictError as exc:
                raise AIConflictError(str(exc)) from exc
            except AIServiceError as exc:
                raise AIConflictError(str(exc)) from exc

            parent_input = parent.get("input")
            if not isinstance(parent_input, Mapping):
                raise AIConflictError("父 AI job 输入不可恢复")
            child_input = dict(parent_input)
            # Agent 绑定是权威来源，不能信任旧 job 输入中缺失或过期的值。
            child_input["agent_id"] = int(agent.id)
            child_input["parent_job_id"] = job_id
            child_input["candidate_snapshot_hash"] = request_data[
                "candidate_snapshot_hash"
            ]
            child_input["resume_candidate_index"] = request_data[
                "resume_candidate_index"
            ]
            child_id = uuid.uuid4().hex
            owner_token = secrets.token_urlsafe(32)
            deadline = (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).strftime("%Y-%m-%d %H:%M:%S")
            child, created = db.create_or_get_ai_resume_job(
                child_id,
                task_type,
                int(agent.id),
                child_input,
                owner_token=owner_token,
                route_deadline_at=deadline,
                parent_job_id=job_id,
                idempotency_key=request_data["idempotency_key"],
                candidate_snapshot=self._snapshot_payload(snapshot),
                candidate_snapshot_hash=request_data["candidate_snapshot_hash"],
                resume_candidate_index=request_data["resume_candidate_index"],
            )
            if not created:
                return self._replay_resume_child(db, str(child["job_id"]))
            resume_spec = RouteResumeSpec(
                parent_job_id=job_id,
                idempotency_key=request_data["idempotency_key"],
                candidate_snapshot_hash=request_data["candidate_snapshot_hash"],
                resume_candidate_index=request_data["resume_candidate_index"],
            )
            stream = dispatch(child_input)
            return self._stream_with_route_resume(resume_spec, stream)
        except AIJobConflictError as exc:
            raise AIConflictError(str(exc)) from exc
        finally:
            db.close()

    @staticmethod
    def _reject_unknown_fields(
        payload: dict[str, Any],
        allowed: set[str],
    ) -> None:
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise AIServiceError(f"不允许提交字段：{', '.join(unknown)}")

    @staticmethod
    def _require_boolean(payload: dict[str, Any], field: str) -> None:
        if field in payload and not isinstance(payload[field], bool):
            raise AIServiceError(f"{field} 必须是布尔值")

    @staticmethod
    def _reject_control_text(value: Any, field: str, max_length: int) -> str:
        if not isinstance(value, str):
            raise AIServiceError(f"{field} 必须是字符串")
        if any(unicodedata.category(character) == "Cc" for character in value):
            raise AIServiceError(f"{field} 不能包含控制字符")
        if len(value) > max_length:
            raise AIServiceError(f"{field} 不能超过 {max_length} 个字符")
        return value

    @staticmethod
    def _expected_version(payload: dict[str, Any]) -> int:
        value = payload.get("expected_version")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AIServiceError("expected_version 必须是非负整数")
        return value

    @classmethod
    def _normalize_model_pool_payload(
        cls,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = dict(payload)
        if "name" in normalized:
            normalized["name"] = cls._reject_control_text(
                normalized["name"],
                "name",
                100,
            )
        if "description" in normalized:
            normalized["description"] = cls._reject_control_text(
                normalized["description"],
                "description",
                2000,
            )
        if "fallback_pool_id" in normalized:
            fallback_id = normalized["fallback_pool_id"]
            if fallback_id is not None and (
                isinstance(fallback_id, bool)
                or not isinstance(fallback_id, int)
                or fallback_id <= 0
            ):
                raise AIServiceError("fallback_pool_id 必须是正整数或 null")
        return normalized

    @staticmethod
    def _require_provider_row(db: Database, provider_id: int) -> dict[str, Any]:
        provider = db.get_ai_provider(provider_id)
        if provider is None:
            raise AINotFoundError("Provider 不存在")
        return provider

    @staticmethod
    def _require_provider_model_row(db: Database, model_id: int) -> dict[str, Any]:
        model = db.get_ai_provider_model(model_id)
        if model is None:
            raise AINotFoundError("Provider 模型不存在")
        return model

    @staticmethod
    def _require_model_pool_row(db: Database, pool_id: int) -> dict[str, Any]:
        pool = db.get_ai_model_pool(pool_id)
        if pool is None:
            raise AINotFoundError("模型池不存在")
        return pool

    def list_providers(self) -> list[dict[str, Any]]:
        db = self._db()
        try:
            providers = db.list_ai_providers()
            for provider in providers:
                provider.pop("models_sync_owner", None)
            return providers
        finally:
            db.close()

    def create_provider(self, payload: dict[str, Any]) -> int:
        data = self._normalize_provider_payload(payload, require_key=bool(payload.get("api_key")))
        db = self._db()
        try:
            return db.create_ai_provider(data)
        finally:
            db.close()

    def update_provider(self, provider_id: int, payload: dict[str, Any]) -> None:
        data = self._normalize_provider_payload(payload, require_key=False, partial=True)
        db = self._db()
        try:
            db.update_ai_provider(provider_id, data)
            self._invalidate_provider(provider_id)
        finally:
            db.close()

    def delete_provider(self, provider_id: int) -> None:
        db = self._db()
        try:
            try:
                db.delete_ai_provider(provider_id)
            except AIProviderReferenceError as exc:
                raise AIConflictError(str(exc)) from exc
            self._invalidate_provider(provider_id)
        finally:
            db.close()

    def test_provider(self, provider_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            provider_config = self._load_provider_config(db, provider_id)
        finally:
            db.close()
        model = provider_config.default_model
        if not model:
            raise AIServiceError("Provider 未配置默认模型")
        provider = self._get_provider(provider_config)
        started = time.time()
        text_parts: list[str] = []
        for chunk in provider.stream_generate(
            [{"role": "user", "content": "请只回复 OK。"}],
            model=model,
            temperature=0,
            top_p=1,
            max_tokens=32,
        ):
            if chunk.type == "delta":
                text_parts.append(chunk.text)
        return {"ok": True, "model": model, "latency_ms": int((time.time() - started) * 1000), "text": "".join(text_parts).strip()[:100]}

    def list_provider_models(
        self,
        provider_id: int,
        *,
        search: str | None = None,
        routable_only: bool = False,
        enabled_only: bool = False,
    ) -> dict[str, Any]:
        if search is not None:
            search = self._reject_control_text(search, "search", 300)
            if len(search.encode("utf-8")) > 1200:
                raise AIServiceError("search 不能超过 1200 个 UTF-8 字节")
        db = self._db()
        try:
            self._require_provider_row(db, provider_id)
            return db.list_ai_provider_models(
                provider_id,
                search=search,
                routable_only=bool(routable_only),
                enabled_only=bool(enabled_only),
            )
        finally:
            db.close()

    def create_manual_model(
        self,
        provider_id: int,
        payload: dict[str, Any],
    ) -> int:
        self._reject_unknown_fields(payload, _MANUAL_MODEL_CREATE_FIELDS)
        self._require_boolean(payload, "enabled")
        data = dict(payload)
        data["provider_id"] = provider_id
        db = self._db()
        try:
            self._require_provider_row(db, provider_id)
            try:
                return db.create_ai_provider_model(data)
            except ModelCatalogValidationError as exc:
                raise AIServiceError(str(exc)) from exc
            except sqlite3.IntegrityError as exc:
                raise AIServiceError("该 Provider 已存在同名模型") from exc
        finally:
            db.close()

    def update_provider_model(
        self,
        model_id: int,
        payload: dict[str, Any],
    ) -> None:
        self._reject_unknown_fields(payload, _MANUAL_MODEL_UPDATE_FIELDS)
        if not payload:
            raise AIServiceError("至少提交一个可写模型字段")
        self._require_boolean(payload, "enabled")
        db = self._db()
        try:
            self._require_provider_model_row(db, model_id)
            try:
                db.update_ai_provider_model(model_id, payload)
            except ModelCatalogValidationError as exc:
                raise AIServiceError(str(exc)) from exc
        finally:
            db.close()

    def delete_provider_model(self, model_id: int) -> None:
        db = self._db()
        try:
            self._require_provider_model_row(db, model_id)
            try:
                db.remove_ai_provider_model_manual(model_id)
            except ModelCatalogConflictError as exc:
                raise AIConflictError(str(exc)) from exc
        finally:
            db.close()

    def start_model_sync(self, provider_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            self._require_provider_row(db, provider_id)
        finally:
            db.close()
        try:
            return super().start_model_sync(provider_id)
        except ModelSyncConflictError as exc:
            data = (
                {"operation_id": exc.existing_operation_id}
                if exc.existing_operation_id
                else None
            )
            raise AIConflictError(str(exc), data=data) from exc

    def get_model_sync_operation(self, operation_id: str) -> dict[str, Any]:
        try:
            return super().get_model_sync_operation(operation_id)
        except ModelSyncConflictError as exc:
            raise AINotFoundError("模型同步 operation 不存在") from exc

    def cancel_model_sync(self, operation_id: str) -> bool:
        self.get_model_sync_operation(operation_id)
        return super().cancel_model_sync(operation_id)

    def confirm_model_sync_empty(
        self,
        operation_id: str,
        generation: int,
        result_digest: str,
    ) -> dict[str, int]:
        self.get_model_sync_operation(operation_id)
        try:
            return super().confirm_model_sync_empty(
                operation_id,
                generation,
                result_digest,
            )
        except ModelSyncConflictError as exc:
            raise AIConflictError(str(exc)) from exc

    def iter_model_sync_events(
        self,
        operation_id: str,
        poll_interval: float = 0.25,
    ):
        self.get_model_sync_operation(operation_id)
        return super().iter_model_sync_events(
            operation_id,
            poll_interval=poll_interval,
        )

    def list_model_pools(self) -> list[dict[str, Any]]:
        db = self._db()
        try:
            return db.list_ai_model_pools()
        finally:
            db.close()

    def get_model_pool(self, pool_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            return self._require_model_pool_row(db, pool_id)
        finally:
            db.close()

    def create_model_pool(self, payload: dict[str, Any]) -> int:
        self._reject_unknown_fields(payload, _MODEL_POOL_FIELDS)
        self._require_boolean(payload, "enabled")
        data = self._normalize_model_pool_payload(payload)
        db = self._db()
        try:
            try:
                return db.create_ai_model_pool(data)
            except ModelPoolValidationError as exc:
                raise AIServiceError(str(exc)) from exc
            except sqlite3.IntegrityError as exc:
                raise AIServiceError("模型池名称重复或引用无效") from exc
        finally:
            db.close()

    def update_model_pool(
        self,
        pool_id: int,
        payload: dict[str, Any],
    ) -> int:
        self._reject_unknown_fields(payload, _MODEL_POOL_FIELDS | {"expected_version"})
        expected_version = self._expected_version(payload)
        patch = {key: value for key, value in payload.items() if key != "expected_version"}
        self._require_boolean(patch, "enabled")
        patch = self._normalize_model_pool_payload(patch)
        db = self._db()
        try:
            self._require_model_pool_row(db, pool_id)
            try:
                return db.update_ai_model_pool(pool_id, patch, expected_version)
            except ModelPoolConflictError as exc:
                raise AIConflictError(str(exc)) from exc
            except ModelPoolValidationError as exc:
                raise AIServiceError(str(exc)) from exc
            except sqlite3.IntegrityError as exc:
                raise AIServiceError("模型池名称重复或引用无效") from exc
        finally:
            db.close()

    def replace_model_pool_members(
        self,
        pool_id: int,
        payload: dict[str, Any],
    ) -> int:
        self._reject_unknown_fields(payload, {"expected_version", "members"})
        expected_version = self._expected_version(payload)
        members = payload.get("members")
        if not isinstance(members, list):
            raise AIServiceError("members 必须是数组")
        normalized: list[dict[str, Any]] = []
        for index, member in enumerate(members):
            if not isinstance(member, dict):
                raise AIServiceError(f"members[{index}] 必须是对象")
            self._reject_unknown_fields(member, {"provider_model_id", "enabled"})
            model_id = member.get("provider_model_id")
            if isinstance(model_id, bool) or not isinstance(model_id, int) or model_id <= 0:
                raise AIServiceError(
                    f"members[{index}].provider_model_id 必须是正整数"
                )
            self._require_boolean(member, "enabled")
            normalized.append(
                {
                    "provider_model_id": model_id,
                    "enabled": member.get("enabled", True),
                }
            )
        db = self._db()
        try:
            self._require_model_pool_row(db, pool_id)
            try:
                return db.replace_ai_model_pool_members(
                    pool_id,
                    normalized,
                    expected_version,
                )
            except ModelPoolConflictError as exc:
                raise AIConflictError(str(exc)) from exc
            except ModelPoolValidationError as exc:
                raise AIServiceError(str(exc)) from exc
        finally:
            db.close()

    def delete_model_pool(self, pool_id: int) -> None:
        db = self._db()
        try:
            self._require_model_pool_row(db, pool_id)
            try:
                db.delete_ai_model_pool(pool_id)
            except ModelPoolConflictError as exc:
                raise AIConflictError(str(exc)) from exc
        finally:
            db.close()

    def list_model_pool_attempts(
        self,
        pool_id: int,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        db = self._db()
        try:
            self._require_model_pool_row(db, pool_id)
            return db.list_ai_model_pool_attempts(pool_id, limit=limit)
        finally:
            db.close()

    def list_agents(self) -> list[dict[str, Any]]:
        db = self._db()
        try:
            return [
                row
                for row in db.list_ai_agents()
                if row.get("task_type") not in _INTERNAL_AGENT_TASK_TYPES
            ]
        finally:
            db.close()

    def create_agent(self, payload: dict[str, Any]) -> int:
        data = self._normalize_agent_payload(payload)
        db = self._db()
        try:
            with db.transaction():
                self._validate_agent_binding(db, data)
                return db.create_ai_agent(data)
        finally:
            db.close()

    def update_agent(self, agent_id: int, payload: dict[str, Any]) -> None:
        data = self._normalize_agent_payload(payload, partial=True)
        db = self._db()
        try:
            with db.transaction():
                existing = db.get_ai_agent(agent_id)
                if not existing:
                    raise AIServiceError("Agent 不存在")
                if existing.get("task_type") in _INTERNAL_AGENT_TASK_TYPES:
                    raise AIServiceError("内部审查 Agent 不允许通过普通接口修改")
                merged = {**existing, **data}
                binding_type = merged.get("binding_type") or "fixed"
                update_data = dict(data)
                update_data["binding_type"] = binding_type
                if binding_type == "pool":
                    if data.get("provider_id") is not None or data.get("model") is not None:
                        raise AIServiceError("固定模型和模型池不能同时提交")
                    merged["provider_id"] = None
                    merged["model"] = None
                    update_data["provider_id"] = None
                    update_data["model"] = None
                    if merged.get("model_pool_id") is None:
                        raise AIServiceError("缺少 Agent 字段：model_pool_id")
                else:
                    if data.get("model_pool_id") is not None:
                        raise AIServiceError("固定模型和模型池不能同时提交")
                    merged["model_pool_id"] = None
                    update_data["model_pool_id"] = None
                    if merged.get("provider_id") is None:
                        raise AIServiceError("缺少 Agent 字段：provider_id")
                self._validate_agent_binding(db, merged)
                db.update_ai_agent(agent_id, update_data)
        finally:
            db.close()

    def delete_agent(self, agent_id: int) -> None:
        db = self._db()
        try:
            existing = db.get_ai_agent(agent_id)
            if not existing:
                raise AIServiceError("Agent 不存在")
            if existing.get("task_type") in _INTERNAL_AGENT_TASK_TYPES:
                raise AIServiceError("内部审查 Agent 不允许通过普通接口删除")
            db.delete_ai_agent(agent_id)
        finally:
            db.close()

    def create_document(self, payload: dict[str, Any]) -> int:
        content = str(payload.get("content") or "")
        if not content.strip():
            raise AIServiceError("文档内容不能为空")
        data = {
            "title": str(payload.get("title") or "未命名文档")[:200],
            "source_type": str(payload.get("source_type") or "manual"),
            "content": content,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "metadata": payload.get("metadata") or {},
        }
        db = self._db()
        try:
            return db.create_ai_document(data)
        finally:
            db.close()

    def list_drafts(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        db = self._db()
        try:
            return db.list_ai_drafts(page=page, page_size=page_size)
        finally:
            db.close()

    def create_draft(self, payload: dict[str, Any]) -> int:
        title = str(payload.get("title") or "未命名草稿")[:200]
        content = str(payload.get("content") or "")
        if not content.strip():
            raise AIServiceError("草稿内容不能为空")
        db = self._db()
        try:
            return db.create_ai_draft({**payload, "title": title, "content": content})
        finally:
            db.close()

    def update_draft(self, draft_id: int, payload: dict[str, Any]) -> None:
        db = self._db()
        try:
            db.update_ai_draft(draft_id, payload)
        finally:
            db.close()

    def delete_draft(self, draft_id: int) -> None:
        db = self._db()
        try:
            db.delete_ai_draft(draft_id)
        finally:
            db.close()

    def _normalize_provider_payload(self, payload: dict[str, Any], require_key: bool = False, partial: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {}
        keys = ["name", "provider_type", "base_url", "default_model", "available_models", "timeout_seconds", "max_retries", "proxy", "context_window", "stream_enabled", "enabled"]
        for key in keys:
            if key in payload:
                data[key] = payload[key]
        if not partial:
            for key in ("name", "provider_type"):
                if not data.get(key):
                    raise AIServiceError(f"缺少 Provider 字段：{key}")
        # H1: 校验 base_url，阻止 SSRF / 密钥外泄。写入时不做 DNS 解析（避免把
        # 配置保存耦合到网络可达性），请求时再带解析校验抵御 rebinding。
        if data.get("base_url") is not None:
            base_url = str(data["base_url"]).strip()
            if base_url:
                try:
                    data["base_url"] = validate_base_url(base_url, resolve=False)
                except ProviderConfigError as exc:
                    raise AIServiceError(str(exc)) from exc
            else:
                data["base_url"] = None
        if data.get("provider_type") not in {None, "openai_compatible", "anthropic", "xai"}:
            raise AIServiceError("不支持的 Provider 类型")
        api_key = str(payload.get("api_key") or "")
        if api_key:
            data["api_key_encrypted"] = self.secret_manager.encrypt(api_key)
        elif require_key:
            raise AIServiceError("缺少 API key")
        return data

    def _normalize_agent_payload(self, payload: dict[str, Any], partial: bool = False) -> dict[str, Any]:
        forbidden = sorted(_FORBIDDEN_AGENT_POLICY_FIELDS.intersection(payload))
        if forbidden:
            raise AIServiceError(
                f"内部策略字段不允许通过普通 Agent 接口提交：{', '.join(forbidden)}"
            )
        task_type = payload.get("task_type")
        if task_type in _INTERNAL_AGENT_TASK_TYPES:
            raise AIServiceError("内部审查 Agent 只能通过固定审查绑定配置")
        data = {
            key: payload[key]
            for key in (
                "name",
                "task_type",
                "binding_type",
                "provider_id",
                "model",
                "model_pool_id",
                "required_capabilities",
                "system_prompt",
                "temperature",
                "top_p",
                "max_tokens",
                "context_window",
                "enabled",
            )
            if key in payload
        }
        if (
            "required_capabilities" not in data
            and "required_capabilities_json" in payload
        ):
            try:
                legacy_capabilities = json.loads(
                    str(payload["required_capabilities_json"])
                )
            except (TypeError, ValueError) as exc:
                raise AIServiceError("required_capabilities_json 必须是有效 JSON") from exc
            data["required_capabilities"] = legacy_capabilities
        binding_type = data.get("binding_type")
        if binding_type is None and not partial:
            binding_type = "fixed"
            data["binding_type"] = binding_type
        if binding_type is not None and binding_type not in {"fixed", "pool"}:
            raise AIServiceError("不支持的 Agent 绑定类型")
        if binding_type == "pool" and (
            data.get("provider_id") is not None or data.get("model") is not None
        ):
            raise AIServiceError("固定模型和模型池不能同时提交")
        if binding_type == "fixed" and data.get("model_pool_id") is not None:
            raise AIServiceError("固定模型和模型池不能同时提交")
        if "required_capabilities" in data:
            try:
                capabilities = normalize_capabilities(
                    data["required_capabilities"],
                    reject_unknown=True,
                )
            except ModelCatalogValidationError as exc:
                raise AIServiceError(str(exc)) from exc
            data["required_capabilities"] = sorted(capabilities)
        if not partial:
            for key in ("name", "task_type", "system_prompt"):
                if not data.get(key):
                    raise AIServiceError(f"缺少 Agent 字段：{key}")
            binding_key = "provider_id" if binding_type == "fixed" else "model_pool_id"
            if not data.get(binding_key):
                raise AIServiceError(f"缺少 Agent 字段：{binding_key}")
        if data.get("task_type") not in {None, "continue", "rewrite", "distill_style", "distill_novel", "audit", "general", "plan", "wizard", "chat", "extract_summary", "resolve_foreshadow", "polish_dialogue", "polish_psychology", "keyword_clean", "adult_polish"}:
            raise AIServiceError("不支持的 Agent 类型")
        return data

    def _validate_agent_binding(self, db: Database, data: dict[str, Any]) -> None:
        if data.get("binding_type", "fixed") == "pool":
            pool_id = int(data["model_pool_id"])
            pools = db.list_ai_model_pools()
            pools_by_id = {int(pool["id"]): pool for pool in pools}
            pool = pools_by_id.get(pool_id)
            if pool is None:
                raise AIServiceError("绑定的模型池不存在")
            if not bool(pool.get("enabled")):
                raise AIServiceError("绑定的模型池必须启用")
            try:
                expanded_ids = expand_pool_ids(pool_id, pools_by_id)
            except ModelPoolValidationError as exc:
                raise AIServiceError(str(exc)) from exc

            candidates: list[dict[str, Any]] = []
            for expanded_id in expanded_ids:
                expanded_pool = pools_by_id[expanded_id]
                if not bool(expanded_pool.get("enabled")):
                    raise AIServiceError("绑定链中的后备模型池必须启用")
                for member in expanded_pool.get("members") or []:
                    if not bool(member.get("enabled")):
                        continue
                    model = db.get_ai_provider_model(
                        int(member["provider_model_id"])
                    )
                    if model and bool(model.get("routable")):
                        candidates.append(model)
            if not candidates:
                raise AIServiceError("绑定的模型池不能为空或没有可用候选")

            required = set(data.get("required_capabilities") or [])
            if required and not any(
                required.issubset(set(candidate["capabilities"]))
                for candidate in candidates
            ):
                raise AIServiceError("模型池没有满足全部必需能力的可用候选")
            return
        provider_id = int(data["provider_id"])
        provider = db.get_ai_provider(provider_id)
        if not provider:
            raise AIServiceError("Provider 不存在")
        if not bool(provider.get("enabled")):
            raise AIServiceError("Provider 已禁用")

        required = set(data.get("required_capabilities") or [])
        if not required:
            return
        model_key = data.get("model") or provider.get("default_model")
        models = db.list_ai_provider_models(
            provider_id,
            routable_only=True,
        )["items"]
        catalog_model = next(
            (item for item in models if item["model_key"] == model_key),
            None,
        )
        if catalog_model is None:
            raise AIServiceError("声明能力要求时，固定模型必须存在于可用模型目录")
        missing = sorted(required.difference(catalog_model["capabilities"]))
        if missing:
            raise AIServiceError(f"固定模型缺少必需能力：{', '.join(missing)}")

    def _load_provider_config(self, db: Database, provider_id: int) -> AIProviderConfig:
        row = db.get_ai_provider(provider_id, include_secret=True)
        if not row:
            raise AIServiceError("Provider 不存在")
        if not bool(row.get("enabled")):
            raise AIServiceError("Provider 已禁用")
        stored_cipher = row.get("api_key_encrypted")
        api_key = self.secret_manager.decrypt(stored_cipher)
        # L4: 若命中已废弃的 v1（无盐 SHA-256）KDF，解密成功后透明升级到 v2 回写。
        if api_key and self.secret_manager.is_legacy_ciphertext(stored_cipher):
            try:
                db.update_ai_provider(provider_id, {"api_key_encrypted": self.secret_manager.encrypt(api_key)})
            except Exception:
                pass  # 升级失败不影响本次使用；下次再试
        return AIProviderConfig(
            id=int(row["id"]), name=row["name"], provider_type=row["provider_type"],
            base_url=row.get("base_url"), api_key=api_key, default_model=row.get("default_model"),
            timeout_seconds=int(row.get("timeout_seconds") or 120), max_retries=int(row.get("max_retries") or 2),
            proxy=row.get("proxy"), context_window=int(row.get("context_window") or 128000),
            stream_enabled=bool(row.get("stream_enabled", 1)),
            enabled=bool(row.get("enabled")),
        )

    def _load_agent_config(self, db: Database, agent_id: int) -> AIAgentConfig:
        row = db.get_ai_agent(agent_id)
        if not row:
            raise AIServiceError("Agent 不存在")
        if row.get("task_type") in _INTERNAL_AGENT_TASK_TYPES:
            raise AIServiceError("内部审查 Agent 不能作为普通写作 Agent 使用")
        if not bool(row.get("enabled")):
            raise AIServiceError("Agent 已禁用")
        provider_id = row.get("provider_id")
        return AIAgentConfig(
            id=int(row["id"]), name=row["name"], task_type=row["task_type"],
            provider_id=int(provider_id) if provider_id is not None else None,
            model=row.get("model"), system_prompt=row["system_prompt"], temperature=float(row.get("temperature") or 0.8),
            top_p=float(row.get("top_p") or 0.9), max_tokens=int(row.get("max_tokens") or 4000),
            context_window=int(row.get("context_window") or 16000), enabled=bool(row.get("enabled")),
            binding_type=row.get("binding_type") or "fixed",
            model_pool_id=(
                int(row["model_pool_id"])
                if row.get("model_pool_id") is not None
                else None
            ),
            required_capabilities=tuple(row.get("required_capabilities") or []),
            binding_version=int(row.get("binding_version") or 1),
        )

    def _resolve_input_text(self, db: Database, payload: dict[str, Any]) -> str:
        source_type = payload.get("source_type") or "manual"
        if source_type == "archive_novel":
            novel_id = int(payload.get("novel_id") or 0)
            novel = db.get_novel_detail(novel_id)
            if not novel:
                raise AIServiceError("归档小说不存在")
            text = novel.get("text_raw") or novel.get("text_markdown") or ""
        elif source_type == "archive_series":
            series_id = int(payload.get("series_id") or 0)
            if not series_id:
                raise AIServiceError("请选择系列")
            novels = db.list_series_novel_texts(series_id)
            if not novels:
                raise AIServiceError("系列下没有找到小说")
            parts: list[str] = []
            for r in novels:
                title = r.get("title", "")
                content = r.get("text_raw") or r.get("text_markdown") or ""
                if content.strip():
                    parts.append(f"{'=' * 40}\n【{title}】\n{'=' * 40}\n\n{content}")
            if not parts:
                raise AIServiceError("系列下的小说均无文本内容（可能尚未抓取正文）")
            text = "\n\n".join(parts)
        elif source_type == "document":
            document_id = int(payload.get("document_id") or 0)
            document = db.get_ai_document(document_id)
            if not document:
                raise AIServiceError("AI 文档不存在")
            text = document.get("content") or ""
        else:
            text = str(payload.get("text") or "")
        if not text.strip():
            raise AIServiceError("输入文本不能为空")
        return text

    def list_jobs(self, task_type: str | None = None, status: str | None = None,
                  page: int = 1, page_size: int = 20) -> dict[str, Any]:
        db = self._db()
        try:
            return db.list_ai_jobs(task_type=task_type, status=status, page=page, page_size=page_size)
        finally:
            db.close()

    def get_job(self, job_id: str) -> dict[str, Any]:
        db = self._db()
        try:
            job = db.get_ai_job(job_id)
            if not job:
                raise AIServiceError("任务不存在")
            return job
        finally:
            db.close()

    def cleanup_jobs(
        self,
        keep_days: int = 3,
        keep_failed_days: int | None = None,
        owner_scope: str | None = None,
    ) -> int:
        """清理超期的 ai_jobs 历史记录，返回删除条数。"""
        db = self._db()
        try:
            return db.cleanup_ai_jobs(
                keep_days=keep_days,
                keep_failed_days=keep_failed_days,
                owner_scope=owner_scope,
            )
        finally:
            db.close()

    def save_style_profile(self, payload: dict[str, Any]) -> int:
        db = self._db()
        try:
            return db.create_ai_style_profile(payload)
        finally:
            db.close()

    def list_style_profiles(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        db = self._db()
        try:
            return db.list_ai_style_profiles(page=page, page_size=page_size)
        finally:
            db.close()

    def get_style_profile(self, profile_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            profile = db.get_ai_style_profile(profile_id)
            if not profile:
                raise AIServiceError("风格档案不存在")
            return profile
        finally:
            db.close()

    def update_style_profile(self, profile_id: int, payload: dict[str, Any]) -> None:
        db = self._db()
        try:
            db.update_ai_style_profile(profile_id, payload)
        finally:
            db.close()

    def delete_style_profile(self, profile_id: int) -> None:
        db = self._db()
        try:
            db.delete_ai_style_profile(profile_id)
        finally:
            db.close()

    def save_novel_profile(self, payload: dict[str, Any]) -> int:
        db = self._db()
        try:
            return db.create_ai_novel_profile(payload)
        finally:
            db.close()

    def list_novel_profiles(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        db = self._db()
        try:
            return db.list_ai_novel_profiles(page=page, page_size=page_size)
        finally:
            db.close()

    def get_novel_profile(self, profile_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            profile = db.get_ai_novel_profile(profile_id)
            if not profile:
                raise AIServiceError("小说档案不存在")
            return profile
        finally:
            db.close()

    def update_novel_profile(self, profile_id: int, payload: dict[str, Any]) -> None:
        db = self._db()
        try:
            db.update_ai_novel_profile(profile_id, payload)
        finally:
            db.close()

    def delete_novel_profile(self, profile_id: int) -> None:
        db = self._db()
        try:
            db.delete_ai_novel_profile(profile_id)
        finally:
            db.close()

    def list_prompt_templates(self, category: str | None = None) -> list[dict[str, Any]]:
        db = self._db()
        try:
            return db.list_ai_prompt_templates(category=category)
        finally:
            db.close()

    def get_prompt_template(self, template_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            template = db.get_ai_prompt_template(template_id)
            if not template:
                raise AIServiceError("Prompt 模板不存在")
            return template
        finally:
            db.close()

    def create_prompt_template(self, payload: dict[str, Any]) -> int:
        name = str(payload.get("name") or "").strip()
        template = str(payload.get("template") or "").strip()
        if not name:
            raise AIServiceError("模板名称不能为空")
        if not template:
            raise AIServiceError("模板内容不能为空")
        db = self._db()
        try:
            return db.create_ai_prompt_template(payload)
        finally:
            db.close()

    def update_prompt_template(self, template_id: int, payload: dict[str, Any]) -> None:
        db = self._db()
        try:
            existing = db.get_ai_prompt_template(template_id)
            if not existing:
                raise AIServiceError("Prompt 模板不存在")
            if existing.get("is_builtin") and not payload.get("force"):
                raise AIServiceError("内置模板不可修改，如需自定义请复制后修改")
            db.update_ai_prompt_template(template_id, payload)
        finally:
            db.close()

    def delete_prompt_template(self, template_id: int) -> None:
        db = self._db()
        try:
            existing = db.get_ai_prompt_template(template_id)
            if not existing:
                raise AIServiceError("Prompt 模板不存在")
            if existing.get("is_builtin"):
                raise AIServiceError("内置模板不可删除")
            db.delete_ai_prompt_template(template_id)
        finally:
            db.close()

    def seed_builtin_templates(self) -> None:
        """初始化内置 Prompt 模板（幂等，已存在则跳过）。"""
        templates = [
            {"name": "续写-默认", "category": "continue", "template": "你是专业中文小说续写助手。\n你的任务是根据用户提供的上下文继续写正文。\n规则：\n1. 你要续写，不要总结，不要解释。\n2. 保持人物设定、叙述视角、语气和文风。\n3. 不要突然跳剧情，不要随意引入新角色或重大设定。\n4. 不要输出标题、列表、分析或写作说明。\n5. 只输出续写后的小说正文。", "description": "标准续写 prompt", "is_builtin": True},
            {"name": "续写-心理描写", "category": "continue", "template": "你是专业中文小说续写助手，擅长细腻的心理描写。\n你的任务是根据用户提供的上下文继续写正文。\n规则：\n1. 重点描写角色的内心活动、情感变化和心理冲突。\n2. 保持人物设定、叙述视角、语气和文风。\n3. 不要突然跳剧情。\n4. 只输出续写后的小说正文。", "description": "侧重心理描写的续写 prompt", "is_builtin": True},
            {"name": "改写-润色", "category": "rewrite", "template": "你是专业中文小说改写助手。\n你的任务是润色文本，提升文学质量。\n规则：\n1. 保留原剧情事实和关键信息。\n2. 优化用词和句式，提升文学性。\n3. 不新增重大事件，不删除关键情节。\n4. 只输出改写后的正文。", "description": "标准润色 prompt", "is_builtin": True},
            {"name": "改写-去AI味", "category": "rewrite", "template": "你是专业中文小说改写助手，专门去除AI生成痕迹。\n\n禁用词汇：仿佛、宛如、不禁、竟然、微微、轻轻、缓缓、深吸一口气、嘴角上扬、眼眸、心中暗道、似乎、好像（每段最多1次）、不由自主、若有所思\n\n句式要求：\n- 禁止连续3句以上用相同句式开头\n- 长短句交替，禁止排比句\n- 对话不要全部用\"XX说\"\n\n描写要求：\n- 禁止抽象描写，用具体细节\n- 每段至少1个感官细节\n- 对话要有信息量\n\n整体要求：像真人写的，允许不完美表达，情感要克制。", "description": "去除 AI 痕迹的改写 prompt（详细版）", "is_builtin": True},
            {"name": "审计-全面审查", "category": "audit", "template": "你是专业的小说内容审计专家。\n请从角色一致性、剧情连贯性、文风统一性、伏笔追踪、节奏把控、对话质量、描写质量七个维度进行审查。\n每个维度给出评分（1-10）和具体意见。", "description": "全面内容审计 prompt", "is_builtin": True},
            {"name": "蒸馏-风格提取", "category": "distill", "template": "你是专业的文学风格分析专家。\n请从叙事视角、语气特征、句式特点、用词风格、描写手法、对话风格、节奏特征、常用修辞手法等维度提取写作风格特征。", "description": "风格蒸馏 prompt", "is_builtin": True},
            {"name": "蒸馏-小说设定提取", "category": "distill", "template": "你是专业的小说结构分析专家。\n请提取角色列表及关系、世界观设定、关键剧情点、伏笔列表、时间线、主题与情感基调。", "description": "小说蒸馏 prompt", "is_builtin": True},
            {"name": "摘要提取", "category": "summarize", "template": "你是专业的小说文本摘要提取助手。\n请保留主要角色当前状态、正在进行的剧情线、最近发生的重要事件、已埋伏笔、情感氛围、时间地点信息。\n摘要控制在原文 10%-20% 篇幅。", "description": "长文本摘要提取 prompt", "is_builtin": True},
            {"name": "写前构思", "category": "plan", "template": "你是专业的小说创作总编。\n请根据已有上文，为接下来的续写制定章节构思，包含：本次目标、读者期待、该兑现的伏笔、暂不掀开的悬念、必须发生的改变、章尾钩子、不要做的事。", "description": "续写前的章节规划 prompt", "is_builtin": True},
        ]
        db = self._db()
        try:
            existing = db.list_ai_prompt_templates()
            existing_names = {t["name"] for t in existing}
            for t in templates:
                if t["name"] not in existing_names:
                    db.create_ai_prompt_template(t)
        finally:
            db.close()

    def seed_builtin_agents(self, provider_id: int) -> dict[str, int]:
        """初始化内置 Agent（幂等，同名则跳过）。返回 {name: id}。"""
        from ..prompts import DEAI_RULES
        agents = [
            {
                "name": "通用续写助手",
                "task_type": "continue",
                "system_prompt": "你是专业中文小说续写助手。\n你的任务是根据用户提供的上下文继续写正文。\n\n规则：\n1. 你要续写，不要总结，不要解释。\n2. 保持人物设定、叙述视角、语气和文风。\n3. 不要突然跳剧情，不要随意引入新角色或重大设定。\n4. 不要输出标题、列表、分析或写作说明。\n5. 只输出续写后的小说正文。\n6. 续写自然流畅，像原作者的风格继续写下去。\n7. 注意保持伏笔的延续，不要忘记前文埋下的线索。",
                "temperature": 0.85,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "续写-心理描写专精",
                "task_type": "continue",
                "system_prompt": '你是专业中文小说续写助手，擅长细腻的心理描写。\n\n写作要求：\n1. 重点描写角色的内心活动、情感变化和心理冲突。\n2. 通过行为细节暗示心理，而非直接说"他很伤心"。\n3. 保持人物设定、叙述视角、语气和文风。\n4. 不要突然跳剧情。\n5. 只输出续写后的小说正文。\n\n心理描写技巧：\n- 用身体反应暗示情绪（手指攥紧、呼吸变浅、眼神躲闪）\n- 用环境映射心理（光线变暗暗示心情沉重）\n- 用内心独白展现纠结（但不要每段都有）\n- 克制表达，不要动不动就"热泪盈眶"',
                "temperature": 0.8,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "续写-对话专精",
                "task_type": "continue",
                "system_prompt": '你是专业中文小说续写助手，擅长写自然生动的对话。\n\n写作要求：\n1. 对话要符合角色身份、性格和说话习惯。\n2. 对话要有信息量，推进剧情或展现人物关系。\n3. 禁止废话对话（嗯、哦、好吧、你说得对）。\n4. 对话中穿插动作描写，不要全是XX说。\n5. 每个角色的说话方式要有区别。\n6. 只输出续写后的小说正文。\n\n对话技巧：\n- 潜台词：角色不会把所有想法都说出来\n- 打断：真实对话中经常有打断和插话\n- 省略：有时话说一半比说完更有力量\n- 口癖：给角色设计1-2个口头禅（但不要滥用）',
                "temperature": 0.85,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "通用改写助手",
                "task_type": "rewrite",
                "system_prompt": "你是专业中文小说改写助手。\n你的任务是按用户要求改写文本。\n\n规则：\n1. 保留原剧情事实和关键信息。\n2. 不新增重大事件，不删除关键情节。\n3. 按用户指定的改写目标调整表达。\n4. 不要解释修改过程。\n5. 只输出改写后的正文。",
                "temperature": 0.7,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "去AI味改写专家",
                "task_type": "rewrite",
                "system_prompt": f"你是专业中文小说改写助手，专门去除AI生成痕迹。\n\n{DEAI_RULES}\n\n改写原则：\n1. 保留原剧情、人物关系、关键信息不变。\n2. 不新增重大事件，不删除关键情节。\n3. 重点改造句式、用词、描写方式，让文本读起来像真人写的。\n4. 不要解释修改过程。\n5. 只输出改写后的正文。\n\n记住：你的目标是让文本通过AI检测工具，同时保持文学质量。",
                "temperature": 0.9,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "润色助手",
                "task_type": "rewrite",
                "system_prompt": "你是专业中文小说润色助手。\n你的任务是提升文本的文学质量，但不改变剧情。\n\n润色方向：\n1. 优化用词，替换平淡的动词和形容词。\n2. 改善句式，增加长短句变化。\n3. 增强画面感，添加感官细节。\n4. 优化节奏，该快则快该慢则慢。\n5. 保留原剧情和人物关系不变。\n6. 只输出润色后的正文。\n\n注意：润色不是重写，要尊重原文风格。",
                "temperature": 0.75,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "内容审计专家",
                "task_type": "audit",
                "system_prompt": "你是专业的小说内容审计专家。\n\n请从以下维度进行审查，每个维度给出评分（1-10）和具体意见：\n\n1. 角色一致性：角色行为是否符合其性格设定，有无前后矛盾\n2. 剧情连贯性：情节发展是否自然流畅，有无逻辑漏洞\n3. 文风统一性：叙述风格是否前后一致，有无突兀的风格转变\n4. 伏笔追踪：已埋伏笔是否有回收，有无遗漏的线索\n5. 节奏把控：叙事节奏是否合理，有无拖沓或过于仓促之处\n6. 对话质量：对话是否自然、有信息量、符合角色身份\n7. 描写质量：场景描写、心理描写是否生动有效\n\n输出格式为 JSON，包含 overall_score（总分）、各维度的 score 和 comments，以及 issues 列表（发现的具体问题）和 suggestions 列表（改进建议）。",
                "temperature": 0.3,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "风格蒸馏师",
                "task_type": "distill_style",
                "system_prompt": "你是专业的文学风格分析专家。\n\n请从以下维度提取写作风格特征：\n1. 叙事视角（第一人称/第三人称/上帝视角等）\n2. 语气特征（冷峻/温暖/幽默/严肃等）\n3. 句式特点（长短句比例、句式结构偏好）\n4. 用词风格（口语化/书面化/文言色彩等）\n5. 描写手法（白描/工笔/意识流等）\n6. 对话风格（简洁/冗长、方言使用、语气词频率）\n7. 节奏特征（紧凑/舒缓、段落长度分布）\n8. 常用修辞手法\n9. 标志性表达（作者常用的句式或词汇）\n\n输出 JSON 格式的风格档案。",
                "temperature": 0.4,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "小说设定提取师",
                "task_type": "distill_novel",
                "system_prompt": "你是专业的小说结构分析专家。\n\n请提取以下内容：\n1. 角色列表：每个角色的姓名、身份、性格特征、与其他角色的关系\n2. 世界观设定：时代背景、地点、社会环境、特殊设定\n3. 关键剧情点：已发生的重要事件及其影响\n4. 伏笔列表：已埋下但未回收的伏笔和悬念\n5. 时间线：按时间顺序排列的主要事件\n6. 主题与情感基调\n7. 势力/阵营关系\n\n输出 JSON 格式的小说档案。",
                "temperature": 0.4,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "全能写作助手",
                "task_type": "general",
                "system_prompt": "你是专业中文小说写作助手，可以完成续写、改写、润色、审计等多种任务。\n\n根据用户的具体要求灵活调整：\n- 续写时：保持原文风格和剧情连贯\n- 改写时：按用户指定方向调整，保留核心信息\n- 审计时：从多个维度分析文本质量\n- 蒸馏时：提取结构化的风格或设定信息\n\n始终以专业、认真的态度完成任务。",
                "temperature": 0.8,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "章节构思师",
                "task_type": "plan",
                "system_prompt": "你是专业的小说创作总编（不是写手），擅长在动笔前规划章节走向。\n你的任务是根据已有上文，为接下来的续写制定一份简洁清晰的章节构思。\n\n【输出结构 - 严格按照以下格式输出 Markdown，不要输出其他内容】\n\n## 本次目标\n（一句话说明本段续写要达到什么效果，≤ 50 字）\n\n## 读者此刻在等什么\n（基于上文，分析读者最期待看到的剧情走向，最多 3 点）\n\n## 该兑现的伏笔/线索\n（列出 1-3 条上文已埋下、本次应当推进或回收的线索）\n\n## 暂不掀开的\n（列出 1-2 条可继续埋藏的悬念）\n\n## 本次必须发生的改变\n（明确 1-3 条具体变化：信息/关系/物理/情感/力量变化，要可验证）\n\n## 章尾钩子\n（设计一个让读者想继续看下去的悬念点）\n\n## 不要做的事\n（针对本段具体内容，列出 2-4 条禁忌）\n\n【原则】\n- 构思必须基于上文事实，不要脱离已有剧情发明新设定\n- 每节内容用一两句话表达，不要长篇大论\n- 不要写正文，只写规划",
                "temperature": 0.6,
                "max_tokens": 2000,
                "context_window": 16000,
            },
            {
                "name": "创作向导",
                "task_type": "wizard",
                "system_prompt": DEFAULT_WIZARD_PROMPT,
                "temperature": 0.85,
                "max_tokens": 6000,
                "context_window": 32000,
            },
            {
                "name": "章节摘要师",
                "task_type": "extract_summary",
                "system_prompt": DEFAULT_CHAPTER_SUMMARY_PROMPT,
                "temperature": 0.3,
                "max_tokens": 2000,
                "context_window": 16000,
            },
            {
                "name": "伏笔追踪师",
                "task_type": "resolve_foreshadow",
                "system_prompt": DEFAULT_FORESHADOW_RESOLVE_PROMPT,
                "temperature": 0.2,
                "max_tokens": 2000,
                "context_window": 16000,
            },
            {
                "name": "对话润色师",
                "task_type": "polish_dialogue",
                "system_prompt": DEFAULT_POLISH_DIALOGUE_PROMPT,
                "temperature": 0.75,
                "max_tokens": 6000,
                "context_window": 16000,
            },
            {
                "name": "心理描写润色师",
                "task_type": "polish_psychology",
                "system_prompt": DEFAULT_POLISH_PSYCHOLOGY_PROMPT,
                "temperature": 0.75,
                "max_tokens": 6000,
                "context_window": 16000,
            },
            {
                "name": "关键词清洗师",
                "task_type": "keyword_clean",
                "system_prompt": DEFAULT_KEYWORD_CLEAN_PROMPT,
                "temperature": 0.3,
                "max_tokens": 2000,
                "context_window": 16000,
            },
        ]
        db = self._db()
        created: dict[str, int] = {}
        try:
            existing = db.list_ai_agents()
            existing_names = {a["name"] for a in existing}
            for a in agents:
                if a["name"] not in existing_names:
                    agent_id = db.create_ai_agent({**a, "provider_id": provider_id, "enabled": True})
                    created[a["name"]] = agent_id
                else:
                    for ea in existing:
                        if ea["name"] == a["name"]:
                            created[a["name"]] = ea["id"]
                            break
        finally:
            db.close()
        return created

    def get_draft_history(self, draft_id: int) -> list[dict[str, Any]]:
        db = self._db()
        try:
            draft = db.get_ai_draft(draft_id)
            if not draft:
                raise AIServiceError("草稿不存在")
            return db.get_ai_draft_history(draft_id)
        finally:
            db.close()

    def fork_draft(self, draft_id: int, payload: dict[str, Any]) -> int:
        db = self._db()
        try:
            original = db.get_ai_draft(draft_id)
            if not original:
                raise AIServiceError("原草稿不存在")
            new_content = str(payload.get("content") or original.get("content", ""))
            new_title = str(payload.get("title") or f"{original.get('title', '未命名')} - 新版本")
            return db.create_ai_draft({
                "title": new_title,
                "content": new_content,
                "parent_draft_id": draft_id,
                "source_job_id": original.get("source_job_id"),
                "style_profile_id": original.get("style_profile_id"),
                "novel_profile_id": original.get("novel_profile_id"),
            })
        finally:
            db.close()

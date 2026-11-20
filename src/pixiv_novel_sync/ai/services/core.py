from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import uuid
from collections.abc import Callable, Generator, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from ...storage_db import Database, prepare_schema
from ..preference_context import (
    PreferenceStrength,
    build_preference_context,
    inject_preference_context,
    normalize_preference_strength,
)
from ...storage.ai.core import _validate_timestamp
from ...work_analysis.configuration import work_configuration_identity
from ...work_analysis.contracts import WorkAnalysisError, WorkCallBinding
from ...preference_learning.contracts import PreferenceCallBinding
from ..crypto import AISecretError, AISecretManager
from ..invocation import TextInvocation, invocation_fingerprint, invocation_messages, normalize_text_invocation
from ..invocation_planner import ESTIMATOR_VERSION, PLANNER_VERSION, InvocationPlan
from ..invocation_snapshot import PlannedCandidateSnapshot, planned_snapshot_payload
from ..model_router import (
    estimate_token_count,
    CandidateSnapshot,
    ModelRouter,
    PromptBudget,
    RouteRequest,
    RouteResumeSpec,
    RouteResult,
)
from ..model_sync import ModelSyncCoordinator
from ..models import AIAgentConfig, AIProviderConfig, AIStreamChunk
from ..providers import AIProvider, AIProviderError
from ..retrieval import BaseRetriever, create_retriever


class AIServiceError(RuntimeError):
    pass


class AIConflictError(AIServiceError):
    def __init__(self, message: str, *, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.data = data or {}


def _message_tokens(messages: list[dict[str, str]]) -> int:
    return sum(estimate_token_count(str(m.get("content") or "")) for m in messages)


def _fit_route_messages(
    messages: list[dict[str, str]],
    input_budget: int,
    *,
    estimator: Callable[[list[dict[str, str]]], int] = _message_tokens,
) -> list[dict[str, str]]:
    fitted = [dict(message) for message in messages]
    if not fitted or estimator(fitted) <= input_budget:
        return fitted
    trim_index = next((i for i in range(len(fitted) - 1, -1, -1)
                       if fitted[i].get("role") not in {"system", "developer"}), None)
    if trim_index is None:
        raise AIServiceError("Prompt 固定内容超过可用输入预算")
    def build(text: str) -> list[dict[str, str]]:
        result = [dict(message) for message in fitted]
        result[trim_index]["content"] = text
        return result
    return _fit_tail_text_messages(
        build, str(fitted[trim_index].get("content") or ""), input_budget,
        estimator=estimator,
    )


def _fit_tail_text_messages(
    build_messages: Callable[[str], list[dict[str, str]]],
    text: str,
    input_budget: int,
    *,
    estimator: Callable[[list[dict[str, str]]], int] = _message_tokens,
) -> list[dict[str, str]]:
    messages = build_messages(text)
    if estimator(messages) <= input_budget:
        return messages
    best = build_messages("")
    if estimator(best) > input_budget:
        raise AIServiceError("Prompt 固定内容超过可用输入预算")
    low, high = 0, len(text)
    while low < high:
        size = (low + high + 1) // 2
        candidate = build_messages(text[-size:])
        if estimator(candidate) <= input_budget:
            low, best = size, candidate
        else:
            high = size - 1
    return best


def _split_prompt_text(
    text: str,
    build_messages: Callable[[str], list[dict[str, str]]],
    input_budget: int,
    *,
    estimator: Callable[[list[dict[str, str]]], int] = _message_tokens,
) -> list[str]:
    """Partition without loss, charging the rendered prompt for every segment."""
    parts = []
    while text:
        low, high = 0, len(text)
        while low < high:
            size = (low + high + 1) // 2
            if estimator(build_messages(text[:size])) <= input_budget:
                low = size
            else:
                high = size - 1
        if not low:
            raise AIServiceError("Prompt 固定内容超过可用输入预算")
        parts.append(text[:low])
        text = text[low:]
    return parts


def _drop_oldest_history_messages(
    messages: list[dict[str, str]],
    input_budget: int,
    *,
    estimator: Callable[[list[dict[str, str]]], int] = _message_tokens,
    budget_for_messages: Callable[[list[dict[str, str]]], int] | None = None,
) -> list[dict[str, str]]:
    fitted = [dict(message) for message in messages]
    fixed_roles = {"system", "developer"}
    # max_history 可能切在一轮中间；不把失去提问的回答发送给模型。
    first_user = next((i for i, m in enumerate(fitted) if m.get("role") == "user"), None)
    if first_user is not None:
        fitted = [m for i, m in enumerate(fitted) if i >= first_user or m.get("role") in fixed_roles]
    def budget() -> int:
        return budget_for_messages(fitted) if budget_for_messages else input_budget
    while estimator(fitted) > budget():
        users = [i for i, m in enumerate(fitted) if m.get("role") == "user"]
        if len(users) < 2:
            break
        start, end = users[0], users[1]
        fitted = [m for i, m in enumerate(fitted)
                  if not start <= i < end or m.get("role") in fixed_roles]
    return _fit_route_messages(fitted, budget(), estimator=estimator)


class AINotFoundError(AIServiceError):
    pass


_ROUTE_RESUME_CONTEXT: ContextVar[RouteResumeSpec | None] = ContextVar(
    "pixiv_novel_sync_route_resume",
    default=None,
)


@dataclass(frozen=True, slots=True)
class RouteJobContext:
    job_id: str
    owner_token: str
    agent: AIAgentConfig
    candidate_snapshot: CandidateSnapshot | PlannedCandidateSnapshot
    prompt_budget: PromptBudget | InvocationPlan
    preference_context: str | None = None
    resume_candidate_index: int = 0
    invocation: TextInvocation | None = None
    retention_policy: Literal["metadata_only", "keyword_compat"] | None = None
    work_binding: WorkCallBinding | None = None
    preference_binding: PreferenceCallBinding | None = None


class AIServiceCore:
    def __init__(self, db_path: Path, secret_manager: AISecretManager | None = None) -> None:
        self.db_path = db_path
        self.secret_manager = secret_manager or AISecretManager()
        self._retriever: BaseRetriever | None = None
        self._retriever_config_key: tuple[str | None, str | None, str, int, bool] | None = None
        self._retriever_lock = threading.Lock()  # 7.7: 保护retriever缓存
        self._provider_cache: dict[tuple[Any, ...], AIProvider] = {}
        self._provider_cache_by_id: dict[int, tuple[Any, ...]] = {}
        self._provider_lock = threading.Lock()
        self._model_sync_lock = threading.Lock()
        self._model_sync_coordinator: ModelSyncCoordinator | None = None
        self._closed = False
        self.model_router = ModelRouter(
            self._db,
            self._load_provider_config,
            self._get_provider,
            load_invocation_provider_config=self._load_invocation_provider_config,
            prepare_invocation_providers=self._prepare_invocation_providers,
        )

    @staticmethod
    def _retriever_settings_key() -> tuple[str | None, str | None, str, int, bool]:
        """只读取配置，允许索引发布时复核而不初始化模型或访问网络。"""
        embedding_base_url = (
            os.getenv("PIXIV_NOVEL_SYNC_EMBEDDING_BASE_URL")
            or os.getenv("QWEN_EMBEDDING_BASE_URL")
        )
        embedding_api_key = (
            os.getenv("PIXIV_NOVEL_SYNC_EMBEDDING_API_KEY")
            or os.getenv("QWEN_EMBEDDING_API_KEY")
        )
        use_embeddings = os.getenv("PIXIV_NOVEL_SYNC_USE_EMBEDDINGS", "false").strip().lower() in {
            "1", "true", "yes", "on",
        }
        embedding_model = (
            os.getenv("PIXIV_NOVEL_SYNC_EMBEDDING_MODEL")
            or os.getenv("QWEN_EMBEDDING_MODEL")
            or ("paraphrase-multilingual-MiniLM-L12-v2"
                if use_embeddings and not embedding_base_url else "Qwen3-Embedding-8B")
        )
        timeout_raw = os.getenv("PIXIV_NOVEL_SYNC_EMBEDDING_TIMEOUT", "60")
        try:
            embedding_timeout = max(int(timeout_raw), 1)
        except ValueError:
            embedding_timeout = 60
        return (embedding_base_url, embedding_api_key, embedding_model, embedding_timeout, use_embeddings)

    def _get_retriever(self) -> BaseRetriever:
        # 7.7: 加锁保护缓存逻辑,避免多线程竞态
        with self._retriever_lock:
            if self._closed:
                raise AIServiceError("AI 服务已关闭")
            config_key = self._retriever_settings_key()
            if self._retriever is None or self._retriever_config_key != config_key:
                if self._retriever is not None and hasattr(self._retriever, "close"):
                    self._retriever.close()  # type: ignore[attr-defined]
                embedding_base_url, embedding_api_key, embedding_model, embedding_timeout, use_embeddings = config_key
                self._retriever = create_retriever(
                    self.db_path,
                    use_embeddings=use_embeddings,
                    model_name=embedding_model,
                    api_base_url=embedding_base_url,
                    api_key=embedding_api_key,
                    api_timeout=embedding_timeout,
                )
                self._retriever_config_key = config_key
            return self._retriever

    def _db(self) -> Database:
        # Database.ensure_schema 已做进程级按路径记忆，这里不再自持一份缓存。
        db = Database(self.db_path)
        try:
            return prepare_schema(db)
        except BaseException:
            db.close()
            raise

    @staticmethod
    def _resolve_preference_context(
        db: Database,
        payload: Mapping[str, Any],
        project: Mapping[str, Any] | None = None,
    ) -> tuple[int | None, PreferenceStrength, str | None]:
        project = project or {}
        raw_profile_id = (
            payload.get("preference_profile_id")
            if "preference_profile_id" in payload
            else project.get("preference_profile_id")
        )
        raw_strength = (
            payload.get("preference_injection_strength")
            if "preference_injection_strength" in payload
            else project.get("preference_injection_strength", "off")
        )

        if raw_strength is None or raw_strength == "":
            strength = normalize_preference_strength("off")
        else:
            normalized_input = (
                raw_strength.strip().lower()
                if isinstance(raw_strength, str)
                else ""
            )
            strength = normalize_preference_strength(raw_strength)
            if normalized_input != strength:
                raise AIServiceError(
                    "偏好注入强度必须是 off、light、standard 或 strong"
                )

        if raw_profile_id in (None, "", 0):
            return None, "off", None
        if isinstance(raw_profile_id, bool):
            raise AIServiceError("偏好画像 ID 无效")
        try:
            profile_id = int(raw_profile_id)
        except (TypeError, ValueError) as exc:
            raise AIServiceError("偏好画像 ID 无效") from exc
        if profile_id <= 0:
            raise AIServiceError("偏好画像 ID 无效")

        profile = db.get_preference_profile(profile_id)
        if profile is None:
            raise AIServiceError("偏好画像不存在")
        context = build_preference_context(profile, strength)
        return profile_id, strength, context

    @staticmethod
    def _preference_project(
        db: Database,
        payload: Mapping[str, Any],
        project: Mapping[str, Any] | None,
    ) -> Mapping[str, Any] | None:
        if project is not None:
            return project

        project_id: int | None = None
        raw_project_id = payload.get("project_id")
        if raw_project_id not in (None, "", 0) and not isinstance(raw_project_id, bool):
            try:
                parsed_project_id = int(raw_project_id)
            except (TypeError, ValueError):
                parsed_project_id = 0
            if parsed_project_id > 0:
                project_id = parsed_project_id

        if project_id is None:
            raw_chapter_id = payload.get("chapter_id")
            if raw_chapter_id not in (None, "", 0) and not isinstance(raw_chapter_id, bool):
                try:
                    chapter_id = int(raw_chapter_id)
                except (TypeError, ValueError):
                    chapter_id = 0
                if chapter_id > 0:
                    chapter = db.get_ai_chapter(chapter_id)
                    if chapter:
                        project_id = int(chapter.get("project_id") or 0) or None

        return db.get_ai_writing_project(project_id) if project_id else None

    @staticmethod
    def _fit_preference_messages(
        messages: list[dict[str, str]],
        input_budget: int,
    ) -> list[dict[str, str]]:
        trim_index = next((i for i in range(len(messages) - 1, -1, -1)
                           if messages[i].get("role") != "system"), -1)
        if trim_index < 0:
            if _message_tokens(messages) > input_budget:
                raise AIServiceError("偏好画像与固定 Prompt 超过可用输入预算")
            return messages
        def build(text: str) -> list[dict[str, str]]:
            result = [dict(m) for m in messages]
            result[trim_index]["content"] = text
            return result
        return _fit_tail_text_messages(
            build, str(messages[trim_index].get("content") or ""), input_budget,
        )

    def _provider_cache_key(self, config: AIProviderConfig) -> tuple[Any, ...]:
        return (
            config.id,
            config.provider_type,
            config.base_url,
            config.api_key,
            config.timeout_seconds,
            config.max_retries,
            config.proxy,
            config.stream_enabled,
            os.environ.get('PIXIV_REMOTE_AI_EXECUTION','local'),
        )

    def _get_provider(self, config: AIProviderConfig) -> AIProvider:
        key = self._provider_cache_key(config)
        with self._provider_lock:
            if self._closed:
                raise AIServiceError("AI 服务已关闭")
            cached = self._provider_cache.get(key)
            if cached is not None:
                return cached
            if config.id is not None:
                old_key = self._provider_cache_by_id.get(config.id)
                if old_key is not None and old_key != key:
                    old_provider = self._provider_cache.pop(old_key, None)
                    if old_provider is not None:
                        old_provider.close()
            from .. import service as service_facade

            provider = service_facade.create_provider(config)
            if os.environ.get('PIXIV_REMOTE_AI_EXECUTION','local')=='remote':
                from ...remote.ai_task import RemoteAIProvider
                provider=RemoteAIProvider(provider)
            self._provider_cache[key] = provider
            if config.id is not None:
                self._provider_cache_by_id[config.id] = key
            return provider

    def _invalidate_provider(self, provider_id: int) -> None:
        with self._provider_lock:
            key = self._provider_cache_by_id.pop(provider_id, None)
            if key is None:
                return
            provider = self._provider_cache.pop(key, None)
            if provider is not None:
                provider.close()

    def _resolve_model_sync_provider(self, db: Database, provider_id: int) -> AIProvider:
        config = self._load_provider_config(db, provider_id)
        return self._get_provider(config)

    def _prepare_invocation_providers(self, provider_ids: tuple[int, ...]) -> None:
        db = self._db()
        try:
            for provider_id in provider_ids:
                row = db.get_ai_provider(provider_id, include_secret=True)
                if row is None or not self.secret_manager.is_legacy_ciphertext(row.get("api_key_encrypted")):
                    continue
                try:
                    self._load_provider_config(db, provider_id)
                except (AISecretError, AIServiceError):
                    # Execution records the provider-scoped configuration failure.
                    continue
        finally:
            db.close()

    def _load_invocation_provider_config(self, db: Database, provider_id: int) -> AIProviderConfig:
        try:
            return self._load_provider_config(db, provider_id, upgrade_legacy=False)
        except (AISecretError, AIServiceError):
            raise AIProviderError("Provider credentials are unavailable", category="configuration", scope="provider") from None

    @staticmethod
    def _snapshot_payload(snapshot: CandidateSnapshot | PlannedCandidateSnapshot) -> dict[str, Any]:
        if isinstance(snapshot, PlannedCandidateSnapshot):
            return planned_snapshot_payload(snapshot)
        return {
            "agent_config_hash": snapshot.agent_config_hash,
            "binding_version": snapshot.binding_version,
            "candidates": [asdict(candidate) for candidate in snapshot.candidates],
        }

    @staticmethod
    def _persist_prompt_budget(
        db: Database,
        job_id: str,
        owner_token: str,
        budget: PromptBudget | InvocationPlan,
    ) -> bool:
        serialized = json.dumps(
            asdict(budget),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE ai_jobs
                SET prompt_budget_json = ?
                WHERE job_id = ? AND status = 'running' AND owner_token = ?
                  AND prompt_budget_json IS NULL
                """,
                (serialized, job_id, owner_token),
            )
            return cursor.rowcount == 1

    def _start_route_job(
        self,
        db: Database,
        task_type: str,
        agent: AIAgentConfig,
        input_data: dict[str, Any],
        *,
        messages: list[dict[str, str]],
        max_tokens: int,
        preference_payload: Mapping[str, Any] | None = None,
        preference_project: Mapping[str, Any] | None = None,
        parent_job_id: str | None = None,
        idempotency_key: str | None = None,
        snapshot: CandidateSnapshot | PlannedCandidateSnapshot | None = None,
        resume_candidate_index: int = 0,
        invocation: TextInvocation | None = None,
        retention_policy: Literal["metadata_only", "keyword_compat"] | None = None,
        deadline_at: str | None = None,
        work_binding: WorkCallBinding | None = None,
        preference_binding: PreferenceCallBinding | None = None,
    ) -> RouteJobContext:
        if self._closed:
            raise AIServiceError("AI 服务已关闭")
        if (
            isinstance(resume_candidate_index, bool)
            or not isinstance(resume_candidate_index, int)
            or resume_candidate_index < 0
        ):
            raise AIServiceError("resume_candidate_index 必须是非负整数")
        if deadline_at is not None:
            deadline_at = _validate_timestamp(deadline_at, "deadline_at")

        job_input_data = dict(input_data)
        preference_context: str | None = None
        budget_messages = [dict(message) for message in messages]
        if preference_payload is not None:
            project = self._preference_project(
                db,
                preference_payload,
                preference_project,
            )
            profile_id, strength, preference_context = (
                self._resolve_preference_context(db, preference_payload, project)
            )
            job_input_data["preference_profile_id"] = profile_id
            job_input_data["preference_injection_strength"] = strength
            budget_messages = inject_preference_context(
                budget_messages,
                preference_context,
            )

        prepared_resume = _ROUTE_RESUME_CONTEXT.get()
        if work_binding is not None and preference_binding is not None:
            raise WorkAnalysisError("configuration_changed", 409)
        business_binding = work_binding if work_binding is not None else preference_binding
        if business_binding is not None:
            expected_type = WorkCallBinding if work_binding is not None else PreferenceCallBinding
            if (not isinstance(business_binding, expected_type) or prepared_resume is not None
                    or invocation is None or retention_policy != "metadata_only"):
                raise WorkAnalysisError("configuration_changed", 409)
            current_identity = work_configuration_identity(db.conn, agent.id, invocation.max_tokens)
            if current_identity["configuration_fingerprint"] != business_binding.configuration_fingerprint:
                raise WorkAnalysisError("configuration_changed", 409)
        if prepared_resume is None:
            job_id = uuid.uuid4().hex
            owner_token = secrets.token_urlsafe(32)
            deadline = (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).strftime("%Y-%m-%d %H:%M:%S")
            if deadline_at is not None:
                deadline = min(deadline, deadline_at)
            # Keep pasted source out of job listings while retaining a durable
            # document reference for resume. Commit the source and job atomically.
            with db.transaction():
                raw_text = job_input_data.pop("text", None)
                if retention_policy is None and raw_text is not None and job_input_data.get("source_type") not in {
                    "archive_novel", "archive_series", "document",
                }:
                    source_text = str(raw_text)
                    document_id = db.create_ai_document({
                        "title": f"AI job source {job_id}",
                        "source_type": "manual",
                        "content": source_text,
                        "content_hash": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                        "metadata": {"job_id": job_id},
                    })
                    job_input_data.update(source_type="document", document_id=document_id)
                db.create_ai_job(
                    job_id,
                    task_type,
                    agent.id,
                    job_input_data,
                    owner_token=owner_token,
                    stage="main",
                    route_deadline_at=deadline,
                    parent_job_id=parent_job_id,
                    idempotency_key=idempotency_key,
                    retention_policy=retention_policy,
                    validation_status="not_requested" if invocation is not None else None,
                    **({"work_binding": work_binding} if work_binding is not None else {}),
                    **({"preference_binding": preference_binding} if preference_binding is not None else {}),
                )
        else:
            if (
                parent_job_id is not None
                or idempotency_key is not None
                or snapshot is not None
                or resume_candidate_index != 0
            ):
                raise AIServiceError("继续任务恢复上下文冲突")
            resumed_job = db.get_ai_resume_job_execution_state(
                prepared_resume.parent_job_id,
                prepared_resume.idempotency_key,
            )
            if resumed_job is None:
                raise AIConflictError("继续任务 child job 不存在")
            if resumed_job.get("status") != "running":
                raise AIConflictError("继续任务 child job 已终结")
            if resumed_job.get("task_type") != task_type:
                raise AIConflictError("继续任务类型不匹配")
            if resumed_job.get("agent_id") != agent.id:
                raise AIConflictError("继续任务 Agent 不匹配")
            if (
                resumed_job.get("candidate_snapshot_hash")
                != prepared_resume.candidate_snapshot_hash
            ):
                raise AIConflictError("继续任务候选快照不匹配")
            resumed_input = resumed_job.get("input")
            if not isinstance(resumed_input, Mapping) or (
                resumed_input.get("resume_candidate_index")
                != prepared_resume.resume_candidate_index
            ):
                raise AIConflictError("继续任务候选索引不匹配")
            snapshot_payload = resumed_job.get("candidate_snapshot")
            if not isinstance(snapshot_payload, Mapping):
                raise AIConflictError("继续任务缺少候选快照")
            snapshot = ModelRouter.candidate_snapshot_from_payload(
                snapshot_payload,
                prepared_resume.candidate_snapshot_hash,
            )
            resume_candidate_index = prepared_resume.resume_candidate_index
            job_id = str(resumed_job["job_id"])
            owner_token = str(resumed_job.get("owner_token") or "")
            if not owner_token:
                raise AIConflictError("继续任务 owner 无效")

        try:
            if invocation is not None and (
                type(max_tokens) is not int
                or messages != invocation_messages(invocation)
                or max_tokens != invocation.max_tokens
            ):
                raise AIServiceError("Text invocation and legacy request arguments disagree")
            if prepared_resume is None:
                if invocation is not None:
                    candidate_snapshot = self.model_router.resolve_invocation(agent, invocation)
                    if business_binding is not None:
                        current_identity = work_configuration_identity(db.conn, agent.id, invocation.max_tokens)
                        if current_identity["configuration_fingerprint"] != business_binding.configuration_fingerprint:
                            raise WorkAnalysisError("configuration_changed", 409)
                else:
                    candidate_snapshot = self.model_router.resolve_candidates(
                        agent,
                        stage="main",
                        snapshot=snapshot,
                    )
            elif isinstance(snapshot, PlannedCandidateSnapshot):
                candidate_snapshot = self.model_router.validate_resume_snapshot(
                    agent, snapshot, resume_candidate_index, invocation=invocation,
                )
            else:
                candidate_snapshot = self.model_router.validate_resume_snapshot(
                    agent,
                    snapshot,
                    resume_candidate_index,
                )
            if isinstance(candidate_snapshot, PlannedCandidateSnapshot):
                prompt_budget = candidate_snapshot.plan
            else:
                budget_snapshot = candidate_snapshot
                if resume_candidate_index:
                    remaining_candidates = tuple(
                        candidate
                        for candidate in candidate_snapshot.candidates
                        if candidate.candidate_index >= resume_candidate_index
                    )
                    if not remaining_candidates:
                        raise AIConflictError("继续任务没有剩余候选模型")
                    budget_snapshot = CandidateSnapshot(
                        candidates=remaining_candidates,
                        snapshot_hash=candidate_snapshot.snapshot_hash,
                        agent_config_hash=candidate_snapshot.agent_config_hash,
                        binding_version=candidate_snapshot.binding_version,
                    )
                prompt_budget = self.model_router.build_prompt_budget(
                    agent,
                    budget_snapshot,
                    budget_messages,
                    max_tokens,
                )
            if prepared_resume is None:
                if not db.set_ai_job_candidate_snapshot(
                    job_id,
                    owner_token,
                    self._snapshot_payload(candidate_snapshot),
                    candidate_snapshot.snapshot_hash,
                ):
                    raise AIConflictError("AI job 候选快照保存冲突")
            if not self._persist_prompt_budget(
                db,
                job_id,
                owner_token,
                prompt_budget,
            ):
                raise AIConflictError("AI job PromptBudget 保存冲突")
            if retention_policy == "metadata_only" and invocation is not None:
                if not db.replace_ai_job_audit(
                    job_id, owner_token, self._build_metadata_audit(task_type, invocation, prompt_budget),
                ):
                    raise AIConflictError("AI job audit could not be frozen")
        except Exception as error:
            db.finish_ai_job_cas(
                job_id,
                owner_token,
                "failed",
                error_message="AI invocation failed" if retention_policy == "metadata_only" else str(error),
            )
            if business_binding is not None and isinstance(error, WorkAnalysisError):
                raise
            if retention_policy == "metadata_only":
                raise AIServiceError("AI invocation failed") from None
            raise

        return RouteJobContext(
            job_id=job_id,
            owner_token=owner_token,
            agent=agent,
            candidate_snapshot=candidate_snapshot,
            prompt_budget=prompt_budget,
            preference_context=preference_context,
            resume_candidate_index=resume_candidate_index,
            invocation=invocation,
            retention_policy=retention_policy,
            work_binding=work_binding,
            preference_binding=preference_binding,
        )

    @staticmethod
    def _stream_with_route_resume(
        resume_spec: RouteResumeSpec,
        chunks: Iterator[AIStreamChunk],
    ) -> Iterator[AIStreamChunk]:
        def generate() -> Iterator[AIStreamChunk]:
            token = _ROUTE_RESUME_CONTEXT.set(resume_spec)
            try:
                yield from chunks
            finally:
                _ROUTE_RESUME_CONTEXT.reset(token)

        return generate()

    @staticmethod
    def _stream_replayed_route_job(
        job: Mapping[str, Any],
    ) -> Iterator[AIStreamChunk]:
        def generate() -> Iterator[AIStreamChunk]:
            job_id = str(job["job_id"])
            yield AIStreamChunk(
                type="metadata",
                data={
                    "job_id": job_id,
                    "parent_job_id": job.get("parent_job_id"),
                    "replayed": True,
                },
            )
            output_text = str(job.get("output_text") or "")
            if output_text:
                yield AIStreamChunk(type="delta", text=output_text)
            status = str(job.get("status") or "")
            if status == "succeeded":
                yield AIStreamChunk(
                    type="done",
                    data={
                        "job_id": job_id,
                        "chars": len(output_text),
                        "replayed": True,
                    },
                )
                return
            if status == "running":
                message = "相同的继续请求正在执行"
            else:
                message = str(job.get("error_message") or "继续任务未成功完成")
            yield AIStreamChunk(
                type="error",
                data={"job_id": job_id, "message": message, "replayed": True},
            )

        return generate()

    def _route_estimator(
        self, context: RouteJobContext,
    ) -> Callable[[list[dict[str, str]]], int]:
        estimate = getattr(self.model_router, "estimate_messages", None)
        if not callable(estimate) or not hasattr(context, "candidate_snapshot"):
            return _message_tokens
        snapshot = context.candidate_snapshot
        if context.resume_candidate_index:
            snapshot = CandidateSnapshot(
                candidates=tuple(c for c in snapshot.candidates
                                 if c.candidate_index >= context.resume_candidate_index),
                snapshot_hash=snapshot.snapshot_hash,
                agent_config_hash=snapshot.agent_config_hash,
                binding_version=snapshot.binding_version,
            )
        return lambda messages: estimate(snapshot, messages)[0]

    @staticmethod
    def _route_input_budget(
        context: RouteJobContext, messages: list[dict[str, str]],
    ) -> int:
        budget = context.prompt_budget
        return min(budget.input_budget, budget.effective_context_window
                   - budget.output_reserve - budget.safety_margin
                   - (4 * len(messages) + 2))

    def _stream_route(
        self,
        context: RouteJobContext,
        messages: list[dict[str, str]],
        *,
        stage: Literal["internal", "main", "validation"] = "main",
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        stop_requested: Callable[[], bool] | None = None,
        deadline_at: str | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Generator[AIStreamChunk, None, RouteResult]:
        """``is_cancelled`` 是调用方的取消回调（偏好分析传 stop_requested，覆盖用户
        取消与调度让位）。路由在候选之间、流式每一帧和重试退避的分片里都会查它。"""
        if self._closed:
            raise AIServiceError("AI 服务已关闭")
        if stop_requested is not None and is_cancelled is not None and stop_requested is not is_cancelled:
            raise AIServiceError("Conflicting cancellation callbacks")
        stop_requested = stop_requested if stop_requested is not None else is_cancelled
        output_reserve = (
            context.prompt_budget.output_reserve
            if max_tokens is None
            else max_tokens
        )
        invocation = context.invocation
        if invocation is not None:
            if (any(type(value) not in (int, float) for value in (temperature, top_p) if value is not None)
                    or (max_tokens is not None and type(max_tokens) is not int)
                    or messages != invocation_messages(invocation)
                    or (temperature is not None and temperature != invocation.temperature)
                    or (top_p is not None and top_p != invocation.top_p)
                    or (max_tokens is not None and max_tokens != invocation.max_tokens)):
                raise AIConflictError("Text request changed after planning")
            route_messages = invocation_messages(invocation)
            output_reserve = invocation.max_tokens
            route_temperature, route_top_p = invocation.temperature, invocation.top_p
        else:
            route_messages = [dict(message) for message in messages]
            route_temperature = context.agent.temperature if temperature is None else temperature
            route_top_p = context.agent.top_p if top_p is None else top_p
        if (
            isinstance(output_reserve, bool)
            or not isinstance(output_reserve, int)
            or output_reserve <= 0
            or output_reserve > context.prompt_budget.output_reserve
        ):
            raise AIServiceError(
                "max_tokens 必须为正整数且不能超过已保存的输出预算"
            )
        if invocation is None:
            if stage == "main" and context.preference_context:
                route_messages = inject_preference_context(
                    route_messages,
                    context.preference_context,
                )
            route_messages = _fit_route_messages(
                route_messages, self._route_input_budget(context, route_messages),
                estimator=self._route_estimator(context),
            )
        request = RouteRequest(
            job_id=context.job_id,
            stage=stage,
            messages=route_messages,
            candidate_snapshot=context.candidate_snapshot,
            max_tokens=output_reserve,
            owner_token=context.owner_token,
            on_delta=lambda _text: None,
            on_progress=lambda _data: None,
            temperature=route_temperature,
            top_p=route_top_p,
            resume_candidate_index=context.resume_candidate_index,
            invocation=invocation,
            retention_policy=context.retention_policy,
            work_binding=context.work_binding,
            preference_binding=context.preference_binding,
            is_cancelled=stop_requested,
            route_deadline_at=deadline_at,
        )
        return (yield from self.model_router.execute_stream(request))

    @staticmethod
    def _finish_route_job(
        db: Database,
        context: RouteJobContext,
        status: str,
        output_text: str,
        output_json: dict[str, Any] | None = None,
        error_message: str | None = None,
        validation_status: Literal["not_requested", "passed", "failed"] | None = None,
    ) -> bool:
        fields: dict[str, Any] = {"output_text": output_text}
        if output_json is not None:
            fields["output_json"] = output_json
        if error_message is not None:
            fields["error_message"] = error_message
        if validation_status is not None:
            fields["validation_status"] = validation_status
        return db.finish_ai_job_cas(
            context.job_id,
            context.owner_token,
            status,
            **fields,
        )

    @staticmethod
    def _build_metadata_audit(
        task_type: str, invocation: TextInvocation, plan: InvocationPlan | None = None,
    ) -> dict[str, Any]:
        provider_ids = invocation.allowed_provider_ids or ()
        payload: dict[str, Any] = {
            "audit_version": 1,
            "business_type": task_type,
            "input_chars": sum(len(message.content) for message in invocation.messages),
            "message_count": len(invocation.messages),
            "input_fingerprint": invocation_fingerprint(invocation, provider_ids=provider_ids),
            "planner_version": PLANNER_VERSION,
            "estimator_version": ESTIMATOR_VERSION,
            "snapshot_version": 2,
            "request_schema_version": 1,
            "allowed_provider_ids": list(provider_ids),
        }
        if plan is not None:
            public = plan.to_public_dict()
            payload.update(
                input_fingerprint=plan.request_fingerprint,
                allowed_provider_ids=public["allowed_provider_ids"],
                planner_version=public["planner_version"],
                estimator_version=public["estimator_version"],
                snapshot_version=public["version"],
            )
        return payload

    def invoke_text(
        self, agent_id: int, payload: Mapping[str, Any], *,
        stop_requested: Callable[[], bool] | None = None,
        deadline_at: str | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        work_binding: WorkCallBinding | None = None,
        preference_binding: PreferenceCallBinding | None = None,
    ) -> RouteResult:
        db = self._db()
        try:
            agent = self._load_agent_config(db, agent_id)
            if not agent.enabled:
                raise AIServiceError("Agent is disabled")
            invocation = normalize_text_invocation(payload, agent=agent)
        finally:
            db.close()
        result, _parsed = self._invoke_text(
            agent, invocation, retention_policy="metadata_only", validator=None,
            input_data=self._build_metadata_audit(agent.task_type, invocation),
            stop_requested=stop_requested, deadline_at=deadline_at,
            on_delta=on_delta, on_progress=on_progress,
            work_binding=work_binding,
            preference_binding=preference_binding,
        )
        return result

    def _invoke_text(
        self, agent: AIAgentConfig, invocation: TextInvocation, *,
        retention_policy: Literal["metadata_only", "keyword_compat"],
        input_data: dict[str, Any],
        validator: Callable[[str], dict[str, Any]] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        deadline_at: str | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        work_binding: WorkCallBinding | None = None,
        preference_binding: PreferenceCallBinding | None = None,
    ) -> tuple[RouteResult, dict[str, Any] | None]:
        if deadline_at is not None:
            deadline_at = _validate_timestamp(deadline_at, "deadline_at")
        db = self._db()
        context: RouteJobContext | None = None
        stream: Generator[AIStreamChunk, None, RouteResult] | None = None
        try:
            task_type = "keyword_clean" if retention_policy == "keyword_compat" else agent.task_type
            context = self._start_route_job(
                db, task_type, agent, input_data,
                messages=invocation_messages(invocation), max_tokens=invocation.max_tokens,
                invocation=invocation, retention_policy=retention_policy, deadline_at=deadline_at,
                work_binding=work_binding,
                preference_binding=preference_binding,
            )
            stream = self._stream_route(
                context, invocation_messages(invocation),
                temperature=invocation.temperature, top_p=invocation.top_p,
                max_tokens=invocation.max_tokens, stop_requested=stop_requested, deadline_at=deadline_at,
            )
            while True:
                try:
                    chunk = next(stream)
                except StopIteration as stopped:
                    result = stopped.value
                    break
                if chunk.type == "delta" and on_delta is not None:
                    on_delta(chunk.text)
                elif chunk.type == "progress" and on_progress is not None:
                    on_progress(chunk.data or {})
            stream.close()
            stream = None

            parsed = None
            validation = "not_requested"
            job_status = {
                "succeeded": "succeeded", "partial": "partial",
                "cancelled": "cancelled", "failed_before_output": "failed",
            }[result.finish_state]
            safe_error = None
            if result.finish_state == "succeeded" and validator is not None:
                try:
                    parsed = validator(result.output_text)
                    validation = "passed"
                except (TypeError, ValueError):
                    validation, job_status, safe_error = "failed", "failed", "validation_failed"
            pending = db.get_ai_job(context.job_id)
            if pending is not None and pending["status"] == "running" and any(
                attempt["status"] == "running" for attempt in pending["attempts"]
            ):
                raise AIConflictError("AI invocation ended before its attempt")
            finished_here = self._finish_route_job(
                db, context, job_status, result.output_text,
                output_json=parsed, error_message=safe_error, validation_status=validation,
            )
            saved = db.get_ai_job(context.job_id)
            if saved is None or saved["status"] == "running":
                raise AIConflictError("AI job no longer exists or ownership changed")
            if not finished_here and result.finish_state == "succeeded":
                transport = {"succeeded": "succeeded", "partial": "partial",
                             "cancelled": "cancelled", "failed": "failed_before_output"}[saved["status"]]
                result = replace(result, output_text=str(saved.get("output_text") or ""), finish_state=transport)
                parsed = None
            final_validation = saved["validation_status"]
            result = replace(
                result, job_status=saved["status"],
                validation_status=None if final_validation == "unknown" else final_validation,
                attempts=tuple(saved["attempts"]),
                usage_summary=db.get_ai_job_usage_summary(context.job_id),
            )
            return result, parsed if result.job_status == "succeeded" else None
        except Exception as error:
            # Close first: GeneratorExit owns cancellation and heartbeat cleanup.
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
                stream = None
            if context is not None:
                try:
                    attempts = db.list_ai_job_model_attempts(context.job_id)
                    partial = any(item["stage"] == "main" and item["output_started"] for item in attempts)
                    for attempt in attempts:
                        if attempt["status"] == "running":
                            db.finish_ai_model_attempt(
                                context.job_id, attempt["attempt_index"], context.owner_token,
                                "partial" if attempt["stage"] == "main" and attempt["output_started"] else "failed",
                                error_category="unexpected_error", error_message="AI invocation failed",
                                finish_reason="error",
                            )
                    self._finish_route_job(
                        db, context, "partial" if partial else "failed", "",
                        error_message="AI invocation failed", validation_status="not_requested",
                    )
                except Exception:
                    # A failed DB write must not expose callback/provider payloads.
                    pass
            if (work_binding is not None or preference_binding is not None) and isinstance(error, WorkAnalysisError):
                raise
            if retention_policy == "metadata_only":
                raise AIServiceError("AI invocation failed") from None
            raise
        finally:
            try:
                if stream is not None:
                    stream.close()
            finally:
                db.close()

    @staticmethod
    def _cancel_route_job(
        db: Database,
        context: RouteJobContext,
        error_message: str | None = None,
    ) -> bool:
        fields: dict[str, Any] = {}
        if error_message is not None:
            fields["error_message"] = error_message
        return db.finish_ai_job_cas(
            context.job_id,
            context.owner_token,
            "cancelled",
            **fields,
        )

    def _model_sync(self) -> ModelSyncCoordinator:
        with self._model_sync_lock:
            if self._closed:
                raise RuntimeError("AI 服务已关闭")
            if self._model_sync_coordinator is None:
                self._model_sync_coordinator = ModelSyncCoordinator(
                    self.db_path,
                    provider_resolver=self._resolve_model_sync_provider,
                )
            return self._model_sync_coordinator

    def start_model_sync(self, provider_id: int) -> dict[str, Any]:
        return self._model_sync().start(provider_id)

    def get_model_sync_operation(self, operation_id: str) -> dict[str, Any]:
        return self._model_sync().get(operation_id)

    def cancel_model_sync(self, operation_id: str) -> bool:
        return self._model_sync().cancel(operation_id)

    def confirm_model_sync_empty(
        self,
        operation_id: str,
        generation: int,
        result_digest: str,
    ) -> dict[str, int]:
        return self._model_sync().confirm_empty(
            operation_id,
            generation,
            result_digest,
        )

    def iter_model_sync_events(
        self,
        operation_id: str,
        poll_interval: float = 0.25,
    ):
        return self._model_sync().events(operation_id, poll_interval=poll_interval)

    def reconcile_model_sync_operations(self) -> int:
        return self._model_sync().reconcile()

    def close(self) -> None:
        with self._model_sync_lock:
            self._closed = True
            coordinator = self._model_sync_coordinator
            self._model_sync_coordinator = None
        if coordinator is not None:
            coordinator.close()
        close_router = getattr(self.model_router, "close", None)
        if callable(close_router):
            close_router()
        with self._provider_lock:
            providers = list(self._provider_cache.values())
            self._provider_cache.clear()
            self._provider_cache_by_id.clear()
        for provider in providers:
            provider.close()
        with self._retriever_lock:
            retriever = self._retriever
            self._retriever = None
            self._retriever_config_key = None
        if retriever is not None and hasattr(retriever, "close"):
            retriever.close()  # type: ignore[attr-defined]

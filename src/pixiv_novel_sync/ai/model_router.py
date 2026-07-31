"""Deterministic AI model candidate routing contracts."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Generator, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from ..storage.ai.core import (
    AIJobConflictError,
    AIRouteBudgetExhausted,
)
from .model_catalog import ModelCatalogValidationError, normalize_model_key
from .model_sync import provider_model_sync_config_hash
from .models import AIAgentConfig, AIProviderConfig, AIStreamChunk
from .providers import AIProvider, AIProviderError


_ROUTE_STAGES = {"internal", "main", "validation"}
_CONTEXT_WINDOW_MIN = 256
_CONTEXT_WINDOW_MAX = 10_000_000
_MAX_OUTPUT_TOKENS = 1_000_000
_MAX_CANDIDATES = 64
_MAX_POOL_NODES = 8
_SAFETY_MARGIN = 256
_HEARTBEAT_INTERVAL_SECONDS = 15.0
_LEASE_SECONDS = 45
_ATTEMPT_FINISH_REASONS = {
    "stop",
    "complete",
    "length",
    "content_filter",
    "missing",
    "cancelled",
    "error",
}
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ModelRouteError(RuntimeError):
    """Model routing configuration or execution failed."""


class ModelRouteConflictError(ModelRouteError):
    """A saved candidate snapshot no longer matches current configuration."""


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    provider_id: int
    provider_name: str
    model_key: str
    provider_model_id: int | None
    pool_id: int | None
    pool_name: str | None
    pool_version: int | None
    pool_position: int | None
    provider_config_hash: str
    capabilities: tuple[str, ...] = ()
    context_window: int | None = None
    fallback_depth: int = 0
    candidate_index: int = 0


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    candidates: tuple[ModelCandidate, ...]
    snapshot_hash: str
    agent_config_hash: str
    binding_version: int


@dataclass(frozen=True, slots=True)
class RouteResumeSpec:
    parent_job_id: str
    idempotency_key: str
    candidate_snapshot_hash: str
    resume_candidate_index: int


@dataclass(slots=True)
class RouteRequest:
    job_id: str
    stage: Literal["internal", "main", "validation"]
    messages: list[dict[str, str]]
    candidate_snapshot: CandidateSnapshot
    max_tokens: int
    owner_token: str
    on_delta: Callable[[str], None]
    on_progress: Callable[[dict[str, Any]], None]
    temperature: float = 0.8
    top_p: float = 0.9
    resume_candidate_index: int = 0
    is_cancelled: Callable[[], bool] | None = None


@dataclass(frozen=True, slots=True)
class RouteResult:
    job_id: str
    output_text: str
    candidate_snapshot_hash: str
    attempts: tuple[dict[str, Any], ...]
    finish_state: Literal[
        "succeeded",
        "failed_before_output",
        "partial",
        "cancelled",
    ]


@dataclass(frozen=True, slots=True)
class PromptBudget:
    effective_context_window: int
    input_budget: int
    output_reserve: int
    message_overhead: int
    safety_margin: int
    estimator: Literal["provider", "utf8_bytes"]


class ModelRouter:
    def __init__(
        self,
        db_factory: Callable[[], Any],
        load_provider_config: Callable[[Any, int], AIProviderConfig],
        get_provider: Callable[[AIProviderConfig], AIProvider],
    ) -> None:
        self._db_factory = db_factory
        self._load_provider_config = load_provider_config
        self._get_provider = get_provider
        self._lifecycle_lock = threading.Lock()
        self._closed = threading.Event()
        self._heartbeat_workers: dict[
            threading.Event,
            threading.Thread,
        ] = {}

    def close(self) -> None:
        self._closed.set()
        with self._lifecycle_lock:
            workers = list(self._heartbeat_workers.items())
        for stopped, _thread in workers:
            stopped.set()
        for _stopped, thread in workers:
            if thread is not threading.current_thread():
                thread.join(timeout=1.0)

    @staticmethod
    def _canonical_hash(payload: Mapping[str, Any]) -> str:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def _agent_config_hash(cls, agent: AIAgentConfig | Mapping[str, Any]) -> str:
        def value(name: str, default: Any = None) -> Any:
            if isinstance(agent, Mapping):
                return agent.get(name, default)
            return getattr(agent, name, default)

        provider_id = value("provider_id")
        model_pool_id = value("model_pool_id")
        payload = {
            "id": int(value("id")),
            "name": value("name"),
            "task_type": value("task_type"),
            "provider_id": int(provider_id) if provider_id is not None else None,
            "model": value("model"),
            "temperature": float(value("temperature", 0.8)),
            "top_p": float(value("top_p", 0.9)),
            "max_tokens": int(value("max_tokens", 4_000)),
            "context_window": int(value("context_window", 16_000)),
            "enabled": bool(value("enabled", True)),
            "binding_type": value("binding_type", "fixed"),
            "model_pool_id": (
                int(model_pool_id) if model_pool_id is not None else None
            ),
            "required_capabilities": list(value("required_capabilities", ()) or ()),
            "binding_version": int(value("binding_version", 1)),
        }
        return cls._canonical_hash(payload)

    @classmethod
    def _snapshot_hash(
        cls,
        candidates: tuple[ModelCandidate, ...],
        agent_config_hash: str,
        binding_version: int,
    ) -> str:
        return cls._canonical_hash(
            {
                "agent_config_hash": agent_config_hash,
                "binding_version": binding_version,
                "candidates": [asdict(candidate) for candidate in candidates],
            }
        )

    @staticmethod
    def _snapshot_integer(
        value: Any,
        label: str,
        *,
        minimum: int = 0,
        optional: bool = False,
    ) -> int | None:
        if value is None and optional:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ModelRouteConflictError(f"候选快照中的 {label} 无效")
        return value

    @classmethod
    def candidate_snapshot_from_payload(
        cls,
        payload: Mapping[str, Any],
        snapshot_hash: str,
    ) -> CandidateSnapshot:
        """从持久化且不含密钥的 JSON 重建强类型候选快照。"""

        try:
            if not isinstance(payload, Mapping):
                raise ModelRouteConflictError("候选快照内容无效")
            if set(payload) != {
                "agent_config_hash",
                "binding_version",
                "candidates",
            }:
                raise ModelRouteConflictError("候选快照字段无效")
            agent_config_hash = payload.get("agent_config_hash")
            if (
                not isinstance(agent_config_hash, str)
                or _HASH_PATTERN.fullmatch(agent_config_hash) is None
            ):
                raise ModelRouteConflictError("候选快照中的 Agent 摘要无效")
            binding_version = cls._snapshot_integer(
                payload.get("binding_version"),
                "binding_version",
                minimum=1,
            )
            raw_candidates = payload.get("candidates")
            if not isinstance(raw_candidates, list):
                raise ModelRouteConflictError("候选快照中的 candidates 无效")

            candidates: list[ModelCandidate] = []
            candidate_fields = {
                "provider_id",
                "provider_name",
                "model_key",
                "provider_model_id",
                "pool_id",
                "pool_name",
                "pool_version",
                "pool_position",
                "provider_config_hash",
                "capabilities",
                "context_window",
                "fallback_depth",
                "candidate_index",
            }
            for raw_candidate in raw_candidates:
                if not isinstance(raw_candidate, Mapping) or set(raw_candidate) != candidate_fields:
                    raise ModelRouteConflictError("候选快照中的候选字段无效")
                provider_name = raw_candidate.get("provider_name")
                model_key = raw_candidate.get("model_key")
                pool_name = raw_candidate.get("pool_name")
                provider_config_hash = raw_candidate.get("provider_config_hash")
                capabilities = raw_candidate.get("capabilities")
                if not isinstance(provider_name, str) or not provider_name:
                    raise ModelRouteConflictError("候选快照中的 Provider 名称无效")
                if not isinstance(model_key, str) or not model_key:
                    raise ModelRouteConflictError("候选快照中的 model_key 无效")
                if pool_name is not None and not isinstance(pool_name, str):
                    raise ModelRouteConflictError("候选快照中的模型池名称无效")
                if (
                    not isinstance(provider_config_hash, str)
                    or _HASH_PATTERN.fullmatch(provider_config_hash) is None
                ):
                    raise ModelRouteConflictError("候选快照中的 Provider 摘要无效")
                if not isinstance(capabilities, (list, tuple)) or not all(
                    isinstance(item, str) for item in capabilities
                ):
                    raise ModelRouteConflictError("候选快照中的能力列表无效")
                context_window = cls._snapshot_integer(
                    raw_candidate.get("context_window"),
                    "context_window",
                    minimum=_CONTEXT_WINDOW_MIN,
                    optional=True,
                )
                candidates.append(
                    ModelCandidate(
                        provider_id=int(
                            cls._snapshot_integer(
                                raw_candidate.get("provider_id"),
                                "provider_id",
                                minimum=1,
                            )
                        ),
                        provider_name=provider_name,
                        model_key=model_key,
                        provider_model_id=cls._snapshot_integer(
                            raw_candidate.get("provider_model_id"),
                            "provider_model_id",
                            minimum=1,
                            optional=True,
                        ),
                        pool_id=cls._snapshot_integer(
                            raw_candidate.get("pool_id"),
                            "pool_id",
                            minimum=1,
                            optional=True,
                        ),
                        pool_name=pool_name,
                        pool_version=cls._snapshot_integer(
                            raw_candidate.get("pool_version"),
                            "pool_version",
                            minimum=1,
                            optional=True,
                        ),
                        pool_position=cls._snapshot_integer(
                            raw_candidate.get("pool_position"),
                            "pool_position",
                            minimum=1,
                            optional=True,
                        ),
                        provider_config_hash=provider_config_hash,
                        capabilities=tuple(capabilities),
                        context_window=context_window,
                        fallback_depth=int(
                            cls._snapshot_integer(
                                raw_candidate.get("fallback_depth"),
                                "fallback_depth",
                            )
                        ),
                        candidate_index=int(
                            cls._snapshot_integer(
                                raw_candidate.get("candidate_index"),
                                "candidate_index",
                            )
                        ),
                    )
                )
            if not isinstance(snapshot_hash, str) or _HASH_PATTERN.fullmatch(snapshot_hash) is None:
                raise ModelRouteConflictError("候选快照摘要无效")
            snapshot = CandidateSnapshot(
                candidates=tuple(candidates),
                snapshot_hash=snapshot_hash,
                agent_config_hash=agent_config_hash,
                binding_version=int(binding_version),
            )
            if cls._snapshot_hash(
                snapshot.candidates,
                snapshot.agent_config_hash,
                snapshot.binding_version,
            ) != snapshot_hash:
                raise ModelRouteConflictError("候选快照校验失败，请重新开始任务")
            return snapshot
        except ModelRouteConflictError:
            raise
        except (TypeError, ValueError, OverflowError) as exc:
            raise ModelRouteConflictError("候选快照内容无效") from exc

    @staticmethod
    def _context_window(value: Any, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ModelRouteError(f"{label}上下文窗口必须是整数")
        if not (_CONTEXT_WINDOW_MIN <= value <= _CONTEXT_WINDOW_MAX):
            raise ModelRouteError(
                f"{label}上下文窗口必须在 {_CONTEXT_WINDOW_MIN} 到 "
                f"{_CONTEXT_WINDOW_MAX} 之间"
            )
        return value

    @classmethod
    def _candidate_context_window(
        cls,
        provider_row: Mapping[str, Any],
        model_row: Mapping[str, Any] | None,
    ) -> int:
        windows = [
            cls._context_window(
                provider_row.get("context_window"),
                "Provider ",
            )
        ]
        if model_row is not None and model_row.get("context_window") is not None:
            windows.append(
                cls._context_window(model_row["context_window"], "模型 ")
            )
        return min(windows)

    @staticmethod
    def _provider_row(db: Any, provider_id: int) -> dict[str, Any]:
        row = db.get_ai_provider(provider_id, include_secret=True)
        if row is None:
            raise ModelRouteError("Provider 不存在")
        if not bool(row.get("enabled")):
            raise ModelRouteError("Provider 已禁用")
        return row

    @staticmethod
    def _matching_catalog_model(
        db: Any,
        provider_id: int,
        model_key: str,
    ) -> dict[str, Any] | None:
        result = db.list_ai_provider_models(provider_id, routable_only=True)
        return next(
            (item for item in result["items"] if item["model_key"] == model_key),
            None,
        )

    @classmethod
    def _resolve_fixed(
        cls,
        db: Any,
        agent: AIAgentConfig,
    ) -> tuple[ModelCandidate, ...]:
        if agent.provider_id is None:
            raise ModelRouteError("固定 Agent 未配置 Provider")
        provider = cls._provider_row(db, agent.provider_id)
        raw_model_key = (
            agent.model if agent.model is not None else provider.get("default_model")
        )
        if raw_model_key is None:
            raise ModelRouteError("固定 Agent 未配置模型，且 Provider 没有默认模型")
        try:
            model_key = normalize_model_key(raw_model_key)
        except ModelCatalogValidationError as exc:
            raise ModelRouteError(str(exc)) from exc

        catalog_model = cls._matching_catalog_model(db, agent.provider_id, model_key)
        required = set(agent.required_capabilities)
        if required:
            if catalog_model is None:
                raise ModelRouteError("声明能力要求时，固定模型必须存在于可用模型目录")
            missing = sorted(required.difference(catalog_model["capabilities"]))
            if missing:
                raise ModelRouteError(f"固定模型缺少必需能力：{', '.join(missing)}")

        capabilities = (
            tuple(catalog_model["capabilities"]) if catalog_model is not None else ()
        )
        return (
            ModelCandidate(
                provider_id=agent.provider_id,
                provider_name=str(provider["name"]),
                model_key=model_key,
                provider_model_id=(
                    int(catalog_model["id"]) if catalog_model is not None else None
                ),
                pool_id=None,
                pool_name=None,
                pool_version=None,
                pool_position=None,
                provider_config_hash=provider_model_sync_config_hash(provider),
                capabilities=capabilities,
                context_window=cls._candidate_context_window(provider, catalog_model),
            ),
        )

    @classmethod
    def _resolve_pool(
        cls,
        db: Any,
        agent: AIAgentConfig,
    ) -> tuple[ModelCandidate, ...]:
        if agent.model_pool_id is None:
            raise ModelRouteError("模型池 Agent 未配置模型池")

        candidates: list[ModelCandidate] = []
        seen_candidates: set[tuple[int, str]] = set()
        seen_pools: set[int] = set()
        provider_rows: dict[int, dict[str, Any]] = {}
        required = set(agent.required_capabilities)
        pool_id: int | None = agent.model_pool_id
        fallback_depth = 0

        while pool_id is not None:
            if pool_id in seen_pools:
                raise ModelRouteError("后备模型池链存在循环")
            if len(seen_pools) >= _MAX_POOL_NODES:
                raise ModelRouteError("后备模型池链最多包含 8 个节点")
            seen_pools.add(pool_id)

            pool = db.get_ai_model_pool(pool_id)
            if pool is None:
                raise ModelRouteError("模型池不存在")
            next_pool_id = pool.get("fallback_pool_id")
            if not bool(pool.get("enabled")):
                pool_id = int(next_pool_id) if next_pool_id is not None else None
                fallback_depth += 1
                continue

            for member in pool["members"]:
                if not bool(member.get("enabled")):
                    continue
                model = db.get_ai_provider_model(int(member["provider_model_id"]))
                if model is None or not bool(model.get("routable")):
                    continue
                capabilities = tuple(model.get("capabilities") or ())
                if required.difference(capabilities):
                    continue

                provider_id = int(model["provider_id"])
                candidate_key = (provider_id, str(model["model_key"]))
                if candidate_key in seen_candidates:
                    continue
                provider = provider_rows.get(provider_id)
                if provider is None:
                    provider = cls._provider_row(db, provider_id)
                    provider_rows[provider_id] = provider

                if len(candidates) >= _MAX_CANDIDATES:
                    raise ModelRouteError("展开后的候选模型不能超过 64 个")
                seen_candidates.add(candidate_key)
                candidates.append(
                    ModelCandidate(
                        provider_id=provider_id,
                        provider_name=str(provider["name"]),
                        model_key=str(model["model_key"]),
                        provider_model_id=int(model["id"]),
                        pool_id=int(pool["id"]),
                        pool_name=str(pool["name"]),
                        pool_version=int(pool["version"]),
                        pool_position=int(member["position"]),
                        provider_config_hash=provider_model_sync_config_hash(provider),
                        capabilities=capabilities,
                        context_window=cls._candidate_context_window(provider, model),
                        fallback_depth=fallback_depth,
                        candidate_index=len(candidates),
                    )
                )

            pool_id = int(next_pool_id) if next_pool_id is not None else None
            fallback_depth += 1

        if not candidates:
            raise ModelRouteError("模型池没有可用模型")
        return tuple(candidates)

    @classmethod
    def _validate_saved_snapshot(
        cls,
        db: Any,
        agent: AIAgentConfig,
        snapshot: CandidateSnapshot,
        *,
        resume_candidate_index: int = 0,
    ) -> CandidateSnapshot:
        expected_hash = cls._snapshot_hash(
            snapshot.candidates,
            snapshot.agent_config_hash,
            snapshot.binding_version,
        )
        if snapshot.snapshot_hash != expected_hash:
            raise ModelRouteConflictError("候选快照校验失败，请重新开始任务")
        if not snapshot.candidates or len(snapshot.candidates) > _MAX_CANDIDATES:
            raise ModelRouteConflictError("候选快照内容无效")
        if [item.candidate_index for item in snapshot.candidates] != list(
            range(len(snapshot.candidates))
        ):
            raise ModelRouteConflictError("候选快照顺序无效")
        if (
            isinstance(resume_candidate_index, bool)
            or not isinstance(resume_candidate_index, int)
            or not (0 <= resume_candidate_index < len(snapshot.candidates))
        ):
            raise ModelRouteConflictError("继续候选索引超出候选快照范围")

        current_agent = db.get_ai_agent(agent.id)
        if current_agent is None or not bool(current_agent.get("enabled")):
            raise ModelRouteConflictError("Agent 已不存在或已禁用")
        current_binding_version = int(current_agent.get("binding_version") or 1)
        if current_binding_version != snapshot.binding_version:
            raise ModelRouteConflictError("Agent 配置版本已变更，请重新开始任务")
        if cls._agent_config_hash(current_agent) != snapshot.agent_config_hash:
            raise ModelRouteConflictError("Agent 配置已变更，请重新开始任务")

        provider_hashes: dict[int, str] = {}
        pool_versions: dict[int, int] = {}
        for candidate in snapshot.candidates[resume_candidate_index:]:
            provider_hash = provider_hashes.get(candidate.provider_id)
            if provider_hash is None:
                provider = db.get_ai_provider(
                    candidate.provider_id,
                    include_secret=True,
                )
                if provider is None or not bool(provider.get("enabled")):
                    raise ModelRouteConflictError("Provider 已不存在或已禁用")
                provider_hash = provider_model_sync_config_hash(provider)
                provider_hashes[candidate.provider_id] = provider_hash
            if provider_hash != candidate.provider_config_hash:
                raise ModelRouteConflictError("Provider 配置已变更，请重新开始任务")

            if candidate.pool_id is None:
                continue
            pool_version = pool_versions.get(candidate.pool_id)
            if pool_version is None:
                pool = db.get_ai_model_pool(candidate.pool_id)
                if pool is None or not bool(pool.get("enabled")):
                    raise ModelRouteConflictError("模型池已不存在或已禁用")
                pool_version = int(pool["version"])
                pool_versions[candidate.pool_id] = pool_version
            if pool_version != candidate.pool_version:
                raise ModelRouteConflictError("模型池配置已变更，请重新开始任务")
        return snapshot

    def validate_resume_snapshot(
        self,
        agent: AIAgentConfig,
        snapshot: CandidateSnapshot,
        resume_candidate_index: int,
    ) -> CandidateSnapshot:
        db = self._db_factory()
        try:
            with db.read_transaction():
                return self._validate_saved_snapshot(
                    db,
                    agent,
                    snapshot,
                    resume_candidate_index=resume_candidate_index,
                )
        finally:
            db.close()

    def resolve_candidates(
        self,
        agent: AIAgentConfig,
        stage: Literal["internal", "main", "validation"] = "main",
        snapshot: CandidateSnapshot | None = None,
    ) -> CandidateSnapshot:
        if stage not in _ROUTE_STAGES:
            raise ModelRouteError("路由阶段无效")
        if not agent.enabled:
            raise ModelRouteError("Agent 已禁用")

        db = self._db_factory()
        try:
            with db.read_transaction():
                if snapshot is not None:
                    return self._validate_saved_snapshot(db, agent, snapshot)
                if agent.binding_type == "fixed":
                    candidates = self._resolve_fixed(db, agent)
                elif agent.binding_type == "pool":
                    candidates = self._resolve_pool(db, agent)
                else:
                    raise ModelRouteError("Agent 绑定类型无效")

                agent_config_hash = self._agent_config_hash(agent)
                binding_version = int(agent.binding_version)
                snapshot_hash = self._snapshot_hash(
                    candidates,
                    agent_config_hash,
                    binding_version,
                )
                return CandidateSnapshot(
                    candidates=candidates,
                    snapshot_hash=snapshot_hash,
                    agent_config_hash=agent_config_hash,
                    binding_version=binding_version,
                )
        finally:
            db.close()

    def build_prompt_budget(
        self,
        agent: AIAgentConfig,
        snapshot: CandidateSnapshot,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> PromptBudget:
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or not (1 <= max_tokens <= _MAX_OUTPUT_TOKENS)
        ):
            raise ModelRouteError("max_tokens 必须在 1 到 1000000 之间")
        if not snapshot.candidates:
            raise ModelRouteError("候选快照没有可用模型")

        agent_window = self._context_window(agent.context_window, "Agent ")
        provider_configs: dict[int, AIProviderConfig] = {}
        provider_instances: list[AIProvider] = []
        candidate_windows: list[int] = []
        db = self._db_factory()
        try:
            for candidate in snapshot.candidates:
                config = provider_configs.get(candidate.provider_id)
                if config is None:
                    config = self._load_provider_config(db, candidate.provider_id)
                    provider_configs[candidate.provider_id] = config
                    provider_instances.append(self._get_provider(config))
                provider_window = self._context_window(
                    config.context_window,
                    "Provider ",
                )
                windows = [provider_window]
                if candidate.context_window is not None:
                    windows.append(
                        self._context_window(candidate.context_window, "候选模型 ")
                    )
                candidate_windows.append(min(windows))
        finally:
            db.close()

        effective_context_window = min(agent_window, *candidate_windows)
        message_overhead = 4 * len(messages) + 2
        input_budget = (
            effective_context_window
            - max_tokens
            - message_overhead
            - _SAFETY_MARGIN
        )
        if input_budget <= 0:
            raise ModelRouteError("Prompt 输入预算必须大于 0")

        estimates = [
            provider.estimate_message_tokens(messages)
            for provider in provider_instances
        ]
        if estimates and all(
            isinstance(estimate, int)
            and not isinstance(estimate, bool)
            and estimate > 0
            for estimate in estimates
        ):
            estimator: Literal["provider", "utf8_bytes"] = "provider"
            estimated_input = max(int(estimate) for estimate in estimates)
        else:
            estimator = "utf8_bytes"
            estimated_input = sum(
                len(str(message.get("content", "")).encode("utf-8"))
                for message in messages
            )
        if estimated_input > input_budget:
            raise ModelRouteError("Prompt 内容超过可用输入预算")

        return PromptBudget(
            effective_context_window=effective_context_window,
            input_budget=input_budget,
            output_reserve=max_tokens,
            message_overhead=message_overhead,
            safety_margin=_SAFETY_MARGIN,
            estimator=estimator,
        )

    @staticmethod
    def _lease_until() -> str:
        return (
            datetime.now(timezone.utc) + timedelta(seconds=_LEASE_SECONDS)
        ).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _deadline_expired(value: Any) -> bool:
        if not value:
            return False
        try:
            deadline = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ModelRouteError("AI job 路由截止时间无效") from exc
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        else:
            deadline = deadline.astimezone(timezone.utc)
        return datetime.now(timezone.utc) >= deadline

    def _active_route_state(self, db: Any, request: RouteRequest) -> dict[str, Any]:
        state = db.get_ai_job_route_state(request.job_id, request.owner_token)
        if state is None or state.get("status") != "running":
            raise AIJobConflictError("AI job 已终结或 owner 不匹配")
        stored_hash = state.get("candidate_snapshot_hash")
        if stored_hash != request.candidate_snapshot.snapshot_hash:
            raise ModelRouteConflictError("AI job 候选快照不匹配")
        if self._deadline_expired(state.get("route_deadline_at")):
            raise AIRouteBudgetExhausted("AI job 已超过路由截止时间")
        return state

    @staticmethod
    def _check_candidate_budget(state: Mapping[str, Any]) -> None:
        if int(state.get("candidate_attempt_count") or 0) >= 16:
            raise AIRouteBudgetExhausted("候选尝试次数已达到 16 次上限")
        if int(state.get("network_request_count") or 0) >= 32:
            raise AIRouteBudgetExhausted("网络请求次数已达到 32 次上限")

    def _validate_execution_snapshot(
        self,
        db: Any,
        request: RouteRequest,
        candidates: tuple[ModelCandidate, ...] | list[ModelCandidate],
        state: Mapping[str, Any],
    ) -> None:
        agent_id = state.get("agent_id")
        if agent_id is not None:
            current_agent = db.get_ai_agent(int(agent_id))
            if current_agent is None or not bool(current_agent.get("enabled")):
                raise ModelRouteConflictError("Agent 已不存在或已禁用")
            if int(current_agent.get("binding_version") or 1) != (
                request.candidate_snapshot.binding_version
            ):
                raise ModelRouteConflictError("Agent 配置版本已变更")
            if self._agent_config_hash(current_agent) != (
                request.candidate_snapshot.agent_config_hash
            ):
                raise ModelRouteConflictError("Agent 配置已变更")

        pool_versions: dict[int, int] = {}
        provider_hashes: dict[int, str] = {}
        for candidate in candidates:
            if candidate.pool_id is not None:
                pool_version = pool_versions.get(candidate.pool_id)
                if pool_version is None:
                    pool = db.get_ai_model_pool(candidate.pool_id)
                    if pool is None or not bool(pool.get("enabled")):
                        raise ModelRouteConflictError("模型池已不存在或已禁用")
                    pool_version = int(pool["version"])
                    pool_versions[candidate.pool_id] = pool_version
                if pool_version != candidate.pool_version:
                    raise ModelRouteConflictError("模型池配置已变更")

            provider_hash = provider_hashes.get(candidate.provider_id)
            if provider_hash is None:
                provider = db.get_ai_provider(
                    candidate.provider_id,
                    include_secret=True,
                )
                if provider is None or not bool(provider.get("enabled")):
                    raise ModelRouteConflictError("Provider 已不存在或已禁用")
                provider_hash = provider_model_sync_config_hash(provider)
                provider_hashes[candidate.provider_id] = provider_hash
            if provider_hash != candidate.provider_config_hash:
                raise ModelRouteConflictError("Provider 配置已变更")

    def _start_heartbeat(
        self,
        request: RouteRequest,
    ) -> tuple[threading.Event, threading.Thread]:
        stopped = threading.Event()

        def heartbeat() -> None:
            while not stopped.is_set():
                db = self._db_factory()
                try:
                    if not db.heartbeat_ai_job(
                        request.job_id,
                        request.owner_token,
                        self._lease_until(),
                    ):
                        return
                finally:
                    db.close()
                stopped.wait(_HEARTBEAT_INTERVAL_SECONDS)

        thread = threading.Thread(
            target=heartbeat,
            name=f"ai-route-heartbeat-{request.job_id}",
            daemon=True,
        )
        with self._lifecycle_lock:
            if self._closed.is_set():
                raise ModelRouteError("ModelRouter 已关闭")
            self._heartbeat_workers[stopped] = thread
            thread.start()
        return stopped, thread

    def _request_cancelled(self, request: RouteRequest) -> bool:
        return self._closed.is_set() or (
            request.is_cancelled is not None and request.is_cancelled()
        )

    @staticmethod
    def _route_progress(
        request: RouteRequest,
        candidate: ModelCandidate,
        *,
        action: str,
        reason: str | None = None,
    ) -> AIStreamChunk:
        data: dict[str, Any] = {
            "phase": "route",
            "action": action,
            "stage": request.stage,
            "candidate_index": candidate.candidate_index,
            "provider_id": candidate.provider_id,
            "provider_name": candidate.provider_name,
            "provider_model_id": candidate.provider_model_id,
            "model_key": candidate.model_key,
            "pool_id": candidate.pool_id,
            "pool_name": candidate.pool_name,
            "pool_position": candidate.pool_position,
            "fallback_depth": candidate.fallback_depth,
        }
        if reason:
            data["reason"] = reason
        return AIStreamChunk(type="progress", data=data)

    @staticmethod
    def _emit_progress(
        request: RouteRequest,
        chunk: AIStreamChunk,
    ) -> AIStreamChunk:
        request.on_progress(dict(chunk.data or {}))
        return chunk

    @staticmethod
    def _attempt_data(
        request: RouteRequest,
        candidate: ModelCandidate,
    ) -> dict[str, Any]:
        return {
            "pool_id": candidate.pool_id,
            "provider_id": candidate.provider_id,
            "provider_model_id": candidate.provider_model_id,
            "pool_version_snapshot": candidate.pool_version,
            "pool_position_snapshot": candidate.pool_position,
            "model_key": candidate.model_key,
            "pool_name_snapshot": candidate.pool_name,
            "provider_name_snapshot": candidate.provider_name,
            "agent_config_hash": request.candidate_snapshot.agent_config_hash,
            "provider_config_hash": candidate.provider_config_hash,
            "candidate_list_hash": request.candidate_snapshot.snapshot_hash,
            "stage": request.stage,
        }

    @staticmethod
    def _finish_reason(error: AIProviderError) -> str:
        reason = error.finish_reason
        return reason if reason in _ATTEMPT_FINISH_REASONS else "error"

    @staticmethod
    def _candidate_fits(
        request: RouteRequest,
        candidate: ModelCandidate,
    ) -> bool:
        if candidate.context_window is None:
            return True
        overhead = 4 * len(request.messages) + 2
        content_bytes = sum(
            len(str(message.get("content", "")).encode("utf-8"))
            for message in request.messages
        )
        return (
            content_bytes + overhead + request.max_tokens + _SAFETY_MARGIN
            <= candidate.context_window
        )

    def _claim_network_request(self, request: RouteRequest) -> None:
        if self._request_cancelled(request):
            raise AIProviderError(
                "AI 请求已取消",
                category="cancelled",
                scope="model",
                finish_reason="cancelled",
            )
        db = self._db_factory()
        try:
            self._active_route_state(db, request)
            db.claim_ai_job_network_request(request.job_id, request.owner_token)
        finally:
            db.close()

    @staticmethod
    def _attempts(db: Any, job_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(db.list_ai_job_model_attempts(job_id))

    def _result(
        self,
        db: Any,
        request: RouteRequest,
        output_text: str,
        finish_state: Literal[
            "succeeded",
            "failed_before_output",
            "partial",
            "cancelled",
        ],
    ) -> RouteResult:
        return RouteResult(
            job_id=request.job_id,
            output_text=output_text,
            candidate_snapshot_hash=request.candidate_snapshot.snapshot_hash,
            attempts=self._attempts(db, request.job_id),
            finish_state=finish_state,
        )

    def _terminal_race_result(
        self,
        db: Any,
        request: RouteRequest,
    ) -> RouteResult:
        job = db.get_ai_job(request.job_id)
        if job is None:
            return self._result(db, request, "", "cancelled")
        status = job.get("status")
        if status == "partial":
            finish_state = "partial"
        elif status == "succeeded":
            finish_state = "succeeded"
        elif status == "cancelled":
            finish_state = "cancelled"
        else:
            finish_state = "failed_before_output"
        return self._result(
            db,
            request,
            str(job.get("output_text") or ""),
            finish_state,
        )

    def _finish_job_failure(
        self,
        db: Any,
        request: RouteRequest,
        *,
        message: str,
        category: str,
    ) -> RouteResult:
        db.finish_ai_job_cas(
            request.job_id,
            request.owner_token,
            "failed",
            error_message=f"{category}: {message}",
        )
        return self._terminal_race_result(db, request)

    def execute(self, request: RouteRequest) -> RouteResult:
        stream = self.execute_stream(request)
        while True:
            try:
                next(stream)
            except StopIteration as stopped:
                return stopped.value

    def execute_stream(
        self,
        request: RouteRequest,
    ) -> Generator[AIStreamChunk, None, RouteResult]:
        if self._closed.is_set():
            raise ModelRouteError("ModelRouter 已关闭")
        if request.stage not in _ROUTE_STAGES:
            raise ModelRouteError("路由阶段无效")
        if (
            isinstance(request.resume_candidate_index, bool)
            or not isinstance(request.resume_candidate_index, int)
            or request.resume_candidate_index < 0
        ):
            raise ModelRouteError("resume_candidate_index 必须是非负整数")

        db = self._db_factory()
        try:
            heartbeat_stop, heartbeat_thread = self._start_heartbeat(request)
        except BaseException:
            db.close()
            raise
        output_parts: list[str] = []
        blocked_providers: set[int] = set()
        current_attempt: int | None = None
        current_iterator: Any = None
        current_output_started = False
        try:
            state = self._active_route_state(db, request)
            candidates = [
                candidate
                for candidate in request.candidate_snapshot.candidates
                if candidate.candidate_index >= request.resume_candidate_index
            ]
            pinned = state.get("pinned_candidate_index")
            if request.stage == "main" and pinned is not None:
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate.candidate_index == int(pinned)
                ]
            self._validate_execution_snapshot(db, request, candidates, state)

            for candidate in candidates:
                if candidate.provider_id in blocked_providers:
                    yield self._emit_progress(
                        request,
                        self._route_progress(
                            request,
                            candidate,
                            action="skipped",
                            reason="provider_short_circuit",
                        ),
                    )
                    continue
                if self._request_cancelled(request):
                    db.finish_ai_job_cas(
                        request.job_id,
                        request.owner_token,
                        "cancelled",
                    )
                    return self._terminal_race_result(db, request)

                current_state = self._active_route_state(db, request)
                self._check_candidate_budget(current_state)
                yield self._emit_progress(
                    request,
                    self._route_progress(request, candidate, action="attempt"),
                )
                try:
                    current_state = self._active_route_state(db, request)
                    self._validate_execution_snapshot(
                        db,
                        request,
                        (candidate,),
                        current_state,
                    )
                    current_attempt = db.allocate_ai_model_attempt(
                        request.job_id,
                        request.owner_token,
                        self._attempt_data(request, candidate),
                    )
                except AIRouteBudgetExhausted as error:
                    return self._finish_job_failure(
                        db,
                        request,
                        message=str(error),
                        category="route_budget_exhausted",
                    )
                except AIJobConflictError:
                    return self._terminal_race_result(db, request)

                started_at = time.monotonic()
                current_output_started = False
                if not self._candidate_fits(request, candidate):
                    db.finish_ai_model_attempt(
                        request.job_id,
                        current_attempt,
                        request.owner_token,
                        "failed",
                        error_scope="model",
                        error_message="Prompt 超过候选模型上下文窗口",
                        error_category="context_overflow",
                        finish_reason="error",
                        output_started=False,
                        latency_ms=0,
                    )
                    current_attempt = None
                    yield self._emit_progress(
                        request,
                        self._route_progress(
                            request,
                            candidate,
                            action="switch",
                            reason="context_overflow",
                        ),
                    )
                    continue

                try:
                    try:
                        config = self._load_provider_config(
                            db,
                            candidate.provider_id,
                        )
                        provider = self._get_provider(config)
                    except AIProviderError:
                        raise
                    except Exception as error:
                        raise AIProviderError(
                            str(error),
                            category="provider_configuration",
                            scope="provider",
                        ) from error
                    current_iterator = iter(
                        provider.stream_generate(
                            request.messages,
                            candidate.model_key,
                            request.temperature,
                            request.top_p,
                            request.max_tokens,
                            request_guard=lambda: self._claim_network_request(request),
                            is_cancelled=lambda: self._request_cancelled(request),
                        )
                    )
                    attempt_parts: list[str] = []
                    saw_done = False
                    finish_reason = "stop"
                    for chunk in current_iterator:
                        if self._request_cancelled(request):
                            raise AIProviderError(
                                "AI 请求已取消",
                                category="cancelled",
                                scope="model",
                                finish_reason="cancelled",
                            )
                        if chunk.type == "progress":
                            yield self._emit_progress(request, chunk)
                            continue
                        if chunk.type == "delta" and chunk.text:
                            if request.stage == "main" and not current_output_started:
                                if not db.mark_ai_job_output_started(
                                    request.job_id,
                                    current_attempt,
                                    request.owner_token,
                                    candidate.candidate_index,
                                ):
                                    return self._terminal_race_result(db, request)
                                current_output_started = True
                            attempt_parts.append(chunk.text)
                            output_parts.append(chunk.text)
                            request.on_delta(chunk.text)
                            yield chunk
                            continue
                        if chunk.type == "done":
                            saw_done = True
                            raw_reason = (chunk.data or {}).get("finish_reason")
                            if raw_reason in _ATTEMPT_FINISH_REASONS:
                                finish_reason = str(raw_reason)
                            if finish_reason not in {"stop", "complete"}:
                                if finish_reason == "cancelled":
                                    raise AIProviderError(
                                        "Provider 请求已取消",
                                        category="cancelled",
                                        scope="model",
                                        finish_reason="cancelled",
                                    )
                                raise AIProviderError(
                                    "Provider 未正常完成"
                                    f"（finish_reason={finish_reason}）",
                                    category="incomplete_response",
                                    scope="model",
                                    finish_reason=finish_reason,
                                )
                            break

                    if not saw_done:
                        raise AIProviderError(
                            "Provider 流缺少正常结束标记",
                            category="incomplete_response",
                            scope="model",
                            finish_reason="missing",
                        )
                    if not attempt_parts:
                        raise AIProviderError(
                            "Provider 返回空响应",
                            category="empty_response",
                            scope="model",
                            finish_reason=finish_reason,
                        )
                    if not db.finish_ai_model_attempt(
                        request.job_id,
                        current_attempt,
                        request.owner_token,
                        "succeeded",
                        finish_reason=finish_reason,
                        output_started=current_output_started,
                        latency_ms=max(
                            0,
                            int((time.monotonic() - started_at) * 1000),
                        ),
                    ):
                        return self._terminal_race_result(db, request)
                    current_attempt = None
                    return self._result(
                        db,
                        request,
                        "".join(output_parts),
                        "succeeded",
                    )
                except AIProviderError as error:
                    latency_ms = max(
                        0,
                        int((time.monotonic() - started_at) * 1000),
                    )
                    cancelled = (
                        error.category == "cancelled"
                        or error.finish_reason == "cancelled"
                    )
                    if cancelled:
                        db.finish_ai_model_attempt(
                            request.job_id,
                            current_attempt,
                            request.owner_token,
                            "cancelled",
                            error_scope=error.scope,
                            error_message=str(error),
                            error_category=error.category,
                            finish_reason="cancelled",
                            output_started=current_output_started,
                            latency_ms=latency_ms,
                        )
                        current_attempt = None
                        db.finish_ai_job_cas(
                            request.job_id,
                            request.owner_token,
                            "cancelled",
                        )
                        return self._terminal_race_result(db, request)

                    if request.stage == "main" and current_output_started:
                        db.finish_ai_model_attempt(
                            request.job_id,
                            current_attempt,
                            request.owner_token,
                            "partial",
                            error_scope=error.scope,
                            error_message=str(error),
                            error_category=error.category,
                            finish_reason=self._finish_reason(error),
                            output_started=True,
                            latency_ms=latency_ms,
                        )
                        current_attempt = None
                        db.finish_ai_job_cas(
                            request.job_id,
                            request.owner_token,
                            "partial",
                            output_text="".join(output_parts),
                            error_message=str(error),
                        )
                        return self._terminal_race_result(db, request)

                    if not db.finish_ai_model_attempt(
                        request.job_id,
                        current_attempt,
                        request.owner_token,
                        "failed",
                        error_scope=error.scope,
                        error_message=str(error),
                        error_category=error.category,
                        finish_reason=self._finish_reason(error),
                        output_started=False,
                        latency_ms=latency_ms,
                    ):
                        return self._terminal_race_result(db, request)
                    current_attempt = None
                    if error.scope == "provider":
                        blocked_providers.add(candidate.provider_id)
                    yield self._emit_progress(
                        request,
                        self._route_progress(
                            request,
                            candidate,
                            action="switch",
                            reason=error.category,
                        ),
                    )
                except AIRouteBudgetExhausted as error:
                    if request.stage == "main" and current_output_started:
                        db.finish_ai_model_attempt(
                            request.job_id,
                            current_attempt,
                            request.owner_token,
                            "partial",
                            error_scope="provider",
                            error_message=str(error),
                            error_category="route_budget_exhausted",
                            finish_reason="error",
                            output_started=True,
                            latency_ms=max(
                                0,
                                int((time.monotonic() - started_at) * 1000),
                            ),
                        )
                        current_attempt = None
                        db.finish_ai_job_cas(
                            request.job_id,
                            request.owner_token,
                            "partial",
                            output_text="".join(output_parts),
                            error_message=str(error),
                        )
                        return self._terminal_race_result(db, request)
                    db.finish_ai_model_attempt(
                        request.job_id,
                        current_attempt,
                        request.owner_token,
                        "failed",
                        error_scope="provider",
                        error_message=str(error),
                        error_category="route_budget_exhausted",
                        finish_reason="error",
                        output_started=current_output_started,
                        latency_ms=max(
                            0,
                            int((time.monotonic() - started_at) * 1000),
                        ),
                    )
                    current_attempt = None
                    return self._finish_job_failure(
                        db,
                        request,
                        message=str(error),
                        category="route_budget_exhausted",
                    )
                except AIJobConflictError:
                    return self._terminal_race_result(db, request)
                finally:
                    if current_iterator is not None:
                        close = getattr(current_iterator, "close", None)
                        if callable(close):
                            close()
                        current_iterator = None

            if request.stage == "internal":
                return self._result(
                    db,
                    request,
                    "".join(output_parts),
                    "failed_before_output",
                )
            category = (
                "validation_failed"
                if request.stage == "validation"
                else "route_exhausted"
            )
            return self._finish_job_failure(
                db,
                request,
                message="所有候选模型均失败",
                category=category,
            )
        except AIRouteBudgetExhausted as error:
            return self._finish_job_failure(
                db,
                request,
                message=str(error),
                category="route_budget_exhausted",
            )
        except GeneratorExit:
            if current_iterator is not None:
                close = getattr(current_iterator, "close", None)
                if callable(close):
                    close()
            if current_attempt is not None:
                db.finish_ai_model_attempt(
                    request.job_id,
                    current_attempt,
                    request.owner_token,
                    "cancelled",
                    error_category="cancelled",
                    finish_reason="cancelled",
                    output_started=current_output_started,
                )
            db.finish_ai_job_cas(
                request.job_id,
                request.owner_token,
                "cancelled",
            )
            raise
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1.0)
            with self._lifecycle_lock:
                self._heartbeat_workers.pop(heartbeat_stop, None)
            db.close()


__all__ = [
    "CandidateSnapshot",
    "ModelCandidate",
    "ModelRouteConflictError",
    "ModelRouteError",
    "ModelRouter",
    "PromptBudget",
    "RouteRequest",
    "RouteResult",
]

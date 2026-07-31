"""Deterministic AI model candidate routing contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Generator, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .model_catalog import ModelCatalogValidationError, normalize_model_key
from .model_sync import provider_model_sync_config_hash
from .models import AIAgentConfig, AIProviderConfig, AIStreamChunk
from .providers import AIProvider


_ROUTE_STAGES = {"internal", "main", "validation"}
_CONTEXT_WINDOW_MIN = 256
_CONTEXT_WINDOW_MAX = 10_000_000
_MAX_OUTPUT_TOKENS = 1_000_000
_MAX_CANDIDATES = 64
_MAX_POOL_NODES = 8
_SAFETY_MARGIN = 256


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
        for candidate in snapshot.candidates:
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

    def execute(self, request: RouteRequest) -> RouteResult:
        del request
        raise NotImplementedError

    def execute_stream(
        self,
        request: RouteRequest,
    ) -> Generator[AIStreamChunk, None, RouteResult]:
        del request
        raise NotImplementedError
        yield  # pragma: no cover


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

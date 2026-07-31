from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from collections.abc import Generator, Sequence
from dataclasses import FrozenInstanceError, asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Generic, TypeVar

import pytest

from pixiv_novel_sync.ai.model_router import (
    CandidateSnapshot,
    ModelCandidate,
    ModelRouteConflictError,
    ModelRouteError,
    ModelRouter,
    PromptBudget,
    RouteRequest,
    RouteResult,
)
from pixiv_novel_sync.ai.models import AIAgentConfig, AIProviderConfig, AIStreamChunk
from pixiv_novel_sync.ai.providers import AIProvider, AIProviderError
from pixiv_novel_sync.storage_db import Database


MESSAGES = [{"role": "user", "content": "正文"}]
T = TypeVar("T")
R = TypeVar("R")


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "model-router.db")
    database.init_schema()
    try:
        yield database
    finally:
        database.close()


def seed_provider(
    db: Database,
    *,
    name: str,
    default_model: str | None = None,
    context_window: int = 128_000,
    enabled: bool = True,
) -> int:
    return db.create_ai_provider(
        {
            "name": name,
            "provider_type": "openai_compatible",
            "base_url": f"https://{name}.example.test/v1",
            "api_key_encrypted": f"cipher-{name}",
            "default_model": default_model,
            "timeout_seconds": 30,
            "max_retries": 2,
            "context_window": context_window,
            "stream_enabled": True,
            "enabled": enabled,
        }
    )


def agent_from_row(db: Database, agent_id: int) -> AIAgentConfig:
    row = db.get_ai_agent(agent_id)
    assert row is not None
    provider_id = row.get("provider_id")
    return AIAgentConfig(
        id=int(row["id"]),
        name=row["name"],
        task_type=row["task_type"],
        provider_id=int(provider_id) if provider_id is not None else None,
        model=row.get("model"),
        system_prompt=row["system_prompt"],
        temperature=float(row.get("temperature") or 0.8),
        top_p=float(row.get("top_p") or 0.9),
        max_tokens=int(row.get("max_tokens") or 4000),
        context_window=int(row.get("context_window") or 16_000),
        enabled=bool(row.get("enabled")),
        binding_type=row.get("binding_type") or "fixed",
        model_pool_id=(
            int(row["model_pool_id"])
            if row.get("model_pool_id") is not None
            else None
        ),
        required_capabilities=tuple(row.get("required_capabilities") or []),
        binding_version=int(row.get("binding_version") or 1),
    )


class EstimatingProvider(AIProvider):
    def __init__(self, config: AIProviderConfig, estimate: int | None) -> None:
        super().__init__(config)
        self.estimate = estimate

    def estimate_message_tokens(self, messages: list[dict[str, str]]) -> int | None:
        del messages
        return self.estimate


@pytest.fixture
def provider_state() -> dict[str, Any]:
    return {"calls": [], "estimates": {}}


@pytest.fixture
def router(db: Database, provider_state: dict[str, Any]) -> ModelRouter:
    def db_factory() -> Database:
        return Database(db.path)

    def load_provider_config(database: Database, provider_id: int) -> AIProviderConfig:
        row = database.get_ai_provider(provider_id, include_secret=True)
        if row is None:
            raise RuntimeError("Provider 不存在")
        return AIProviderConfig(
            id=int(row["id"]),
            name=row["name"],
            provider_type=row["provider_type"],
            base_url=row.get("base_url"),
            api_key="decrypted-test-key",
            default_model=row.get("default_model"),
            timeout_seconds=int(row.get("timeout_seconds") or 120),
            max_retries=int(row.get("max_retries") or 2),
            proxy=row.get("proxy"),
            context_window=int(row.get("context_window") or 128_000),
            stream_enabled=bool(row.get("stream_enabled", 1)),
            enabled=bool(row.get("enabled")),
        )

    def get_provider(config: AIProviderConfig) -> AIProvider:
        provider_state["calls"].append(config.id)
        estimate = provider_state["estimates"].get(config.id)
        return EstimatingProvider(config, estimate)

    return ModelRouter(db_factory, load_provider_config, get_provider)


@pytest.fixture
def fixed_agent(db: Database) -> AIAgentConfig:
    provider_id = seed_provider(
        db,
        name="fixed-provider",
        default_model="provider-default",
        context_window=32_000,
    )
    agent_id = db.create_ai_agent(
        {
            "name": "固定 Agent",
            "task_type": "continue",
            "binding_type": "fixed",
            "provider_id": provider_id,
            "model": "fixed-m",
            "system_prompt": "prompt-secret",
            "context_window": 16_000,
        }
    )
    return agent_from_row(db, agent_id)


def seed_pool_setup(db: Database) -> dict[str, Any]:
    p1 = seed_provider(db, name="p1", context_window=64_000)
    p2 = seed_provider(db, name="p2", context_window=16_000)
    p3 = seed_provider(db, name="p3", context_window=32_000)
    m1 = db.create_ai_provider_model(
        {
            "provider_id": p1,
            "model_key": "m1",
            "manual_capabilities": ["streaming", "json"],
            "manual_context_window": 32_000,
        }
    )
    m2 = db.create_ai_provider_model(
        {
            "provider_id": p2,
            "model_key": "m2",
            "manual_capabilities": ["streaming"],
        }
    )
    m3 = db.create_ai_provider_model(
        {
            "provider_id": p3,
            "model_key": "m3",
            "manual_capabilities": ["streaming", "json"],
            "manual_context_window": 8_000,
        }
    )

    fallback_id = db.create_ai_model_pool(
        {"name": "fallback", "pool_kind": "secondary"}
    )
    fallback_version = db.replace_ai_model_pool_members(
        fallback_id,
        [
            {"provider_model_id": m1, "enabled": True},
            {"provider_model_id": m3, "enabled": True},
        ],
        expected_version=1,
    )
    fallback_version = db.update_ai_model_pool(
        fallback_id,
        {"enabled": True},
        expected_version=fallback_version,
    )

    root_id = db.create_ai_model_pool(
        {
            "name": "primary",
            "pool_kind": "primary",
            "fallback_pool_id": fallback_id,
        }
    )
    root_version = db.replace_ai_model_pool_members(
        root_id,
        [
            {"provider_model_id": m1, "enabled": True},
            {"provider_model_id": m2, "enabled": True},
        ],
        expected_version=1,
    )
    root_version = db.update_ai_model_pool(
        root_id,
        {"enabled": True},
        expected_version=root_version,
    )
    agent_id = db.create_ai_agent(
        {
            "name": "模型池 Agent",
            "task_type": "continue",
            "binding_type": "pool",
            "provider_id": None,
            "model": None,
            "model_pool_id": root_id,
            "required_capabilities": [],
            "system_prompt": "pool-prompt-secret",
            "context_window": 128_000,
        }
    )
    return {
        "agent": agent_from_row(db, agent_id),
        "providers": (p1, p2, p3),
        "models": (m1, m2, m3),
        "root_id": root_id,
        "root_version": root_version,
        "fallback_id": fallback_id,
        "fallback_version": fallback_version,
    }


@pytest.fixture
def pool_setup(db: Database) -> dict[str, Any]:
    return seed_pool_setup(db)


@pytest.fixture
def pool_agent(pool_setup: dict[str, Any]) -> AIAgentConfig:
    return pool_setup["agent"]


def snapshot_with_context_windows(*windows: int) -> CandidateSnapshot:
    candidates = tuple(
        ModelCandidate(
            provider_id=index + 1,
            provider_name=f"p{index + 1}",
            model_key=f"m{index + 1}",
            provider_model_id=index + 1,
            pool_id=1,
            pool_name="pool",
            pool_version=1,
            pool_position=index + 1,
            provider_config_hash=chr(97 + index) * 64,
            context_window=window,
            candidate_index=index,
        )
        for index, window in enumerate(windows)
    )
    return CandidateSnapshot(
        candidates=candidates,
        snapshot_hash="f" * 64,
        agent_config_hash="e" * 64,
        binding_version=1,
    )


def test_route_contract_field_order_and_immutability() -> None:
    assert [field.name for field in fields(ModelCandidate)] == [
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
    ]
    assert [field.name for field in fields(CandidateSnapshot)] == [
        "candidates",
        "snapshot_hash",
        "agent_config_hash",
        "binding_version",
    ]
    assert [field.name for field in fields(RouteRequest)] == [
        "job_id",
        "stage",
        "messages",
        "candidate_snapshot",
        "max_tokens",
        "owner_token",
        "on_delta",
        "on_progress",
        "temperature",
        "top_p",
        "resume_candidate_index",
        "is_cancelled",
    ]
    assert [field.name for field in fields(RouteResult)] == [
        "job_id",
        "output_text",
        "candidate_snapshot_hash",
        "attempts",
        "finish_state",
    ]
    assert [field.name for field in fields(PromptBudget)] == [
        "effective_context_window",
        "input_budget",
        "output_reserve",
        "message_overhead",
        "safety_margin",
        "estimator",
    ]

    candidate = snapshot_with_context_windows(8_000).candidates[0]
    with pytest.raises(FrozenInstanceError):
        candidate.model_key = "changed"  # type: ignore[misc]


def test_fixed_agent_resolves_one_legacy_candidate(
    router: ModelRouter,
    fixed_agent: AIAgentConfig,
    provider_state: dict[str, Any],
) -> None:
    snapshot = router.resolve_candidates(fixed_agent)

    assert [
        (candidate.provider_id, candidate.model_key, candidate.pool_id)
        for candidate in snapshot.candidates
    ] == [(fixed_agent.provider_id, "fixed-m", None)]
    assert snapshot.candidates[0].provider_model_id is None
    assert snapshot.candidates[0].context_window == 32_000
    assert len(snapshot.snapshot_hash) == 64
    assert snapshot.binding_version == fixed_agent.binding_version
    assert provider_state["calls"] == []


def test_fixed_agent_uses_provider_default_and_rejects_missing_model(
    router: ModelRouter,
    fixed_agent: AIAgentConfig,
    db: Database,
) -> None:
    fixed_agent.model = None
    snapshot = router.resolve_candidates(fixed_agent)
    assert snapshot.candidates[0].model_key == "provider-default"

    assert fixed_agent.provider_id is not None
    db.update_ai_provider(fixed_agent.provider_id, {"default_model": None})
    with pytest.raises(ModelRouteError, match="未配置模型"):
        router.resolve_candidates(fixed_agent)


def test_fixed_agent_rejects_invalid_opaque_model_key(
    router: ModelRouter,
    fixed_agent: AIAgentConfig,
) -> None:
    fixed_agent.model = "bad\nmodel"
    with pytest.raises(ModelRouteError, match="model_key"):
        router.resolve_candidates(fixed_agent)


def test_fixed_required_capability_needs_matching_catalog_model(
    router: ModelRouter,
    fixed_agent: AIAgentConfig,
    db: Database,
) -> None:
    fixed_agent.required_capabilities = ("json",)
    with pytest.raises(ModelRouteError, match="能力"):
        router.resolve_candidates(fixed_agent)

    assert fixed_agent.provider_id is not None
    model_id = db.create_ai_provider_model(
        {
            "provider_id": fixed_agent.provider_id,
            "model_key": "fixed-m",
            "manual_capabilities": ["json", "streaming"],
            "manual_context_window": 4_000,
        }
    )
    snapshot = router.resolve_candidates(fixed_agent)
    assert snapshot.candidates[0].provider_model_id == model_id
    assert snapshot.candidates[0].capabilities == ("json", "streaming")
    assert snapshot.candidates[0].context_window == 4_000


def test_pool_resolution_preserves_member_then_fallback_order_and_deduplicates(
    router: ModelRouter,
    pool_agent: AIAgentConfig,
) -> None:
    snapshot = router.resolve_candidates(pool_agent)

    assert [
        (candidate.provider_name, candidate.model_key)
        for candidate in snapshot.candidates
    ] == [("p1", "m1"), ("p2", "m2"), ("p3", "m3")]
    assert [candidate.candidate_index for candidate in snapshot.candidates] == [0, 1, 2]
    assert [candidate.fallback_depth for candidate in snapshot.candidates] == [0, 0, 1]
    assert len(
        {(candidate.provider_id, candidate.model_key) for candidate in snapshot.candidates}
    ) == 3


def test_pool_capability_filter_and_empty_result_fail_before_provider(
    router: ModelRouter,
    pool_agent: AIAgentConfig,
    provider_state: dict[str, Any],
) -> None:
    pool_agent.required_capabilities = ("json",)
    snapshot = router.resolve_candidates(pool_agent)
    assert [candidate.model_key for candidate in snapshot.candidates] == ["m1", "m3"]

    pool_agent.required_capabilities = ("vision",)
    with pytest.raises(ModelRouteError, match="模型池没有可用模型"):
        router.resolve_candidates(pool_agent)
    assert provider_state["calls"] == []


def test_pool_resolution_skips_disabled_and_invalid_runtime_rows(
    router: ModelRouter,
    pool_agent: AIAgentConfig,
    pool_setup: dict[str, Any],
    db: Database,
) -> None:
    p1, _p2, _p3 = pool_setup["providers"]
    _m1, m2, m3 = pool_setup["models"]
    db.update_ai_provider(p1, {"enabled": False})
    db.update_ai_provider_model(m2, {"enabled": False})
    db.conn.execute(
        "UPDATE ai_provider_models SET manual = 0, discovered_available = 0 WHERE id = ?",
        (m3,),
    )
    db.conn.commit()

    with pytest.raises(ModelRouteError, match="模型池没有可用模型"):
        router.resolve_candidates(pool_agent)


def test_pool_resolution_skips_disabled_pool_and_continues_fallback(
    router: ModelRouter,
    pool_agent: AIAgentConfig,
    pool_setup: dict[str, Any],
    db: Database,
) -> None:
    db.conn.execute(
        "UPDATE ai_model_pools SET enabled = 0 WHERE id = ?",
        (pool_setup["root_id"],),
    )
    db.conn.commit()

    snapshot = router.resolve_candidates(pool_agent)
    assert [candidate.model_key for candidate in snapshot.candidates] == ["m1", "m3"]
    assert [candidate.fallback_depth for candidate in snapshot.candidates] == [1, 1]


def test_pool_runtime_cycle_is_rejected(
    router: ModelRouter,
    pool_agent: AIAgentConfig,
    pool_setup: dict[str, Any],
    db: Database,
) -> None:
    db.conn.execute(
        "UPDATE ai_model_pools SET fallback_pool_id = ? WHERE id = ?",
        (pool_setup["root_id"], pool_setup["fallback_id"]),
    )
    db.conn.commit()

    with pytest.raises(ModelRouteError, match="循环"):
        router.resolve_candidates(pool_agent)


def test_snapshot_hash_is_stable_reconstructable_and_contains_no_secret(
    router: ModelRouter,
    pool_agent: AIAgentConfig,
) -> None:
    first = router.resolve_candidates(pool_agent)
    second = router.resolve_candidates(pool_agent)
    payload = {
        "agent_config_hash": first.agent_config_hash,
        "binding_version": first.binding_version,
        "candidates": [asdict(candidate) for candidate in first.candidates],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert first == second
    assert first.snapshot_hash == hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    assert "cipher-" not in serialized
    assert "decrypted-test-key" not in serialized
    assert "pool-prompt-secret" not in serialized


def test_saved_snapshot_rejects_tampering_and_provider_config_change(
    router: ModelRouter,
    pool_agent: AIAgentConfig,
    pool_setup: dict[str, Any],
    db: Database,
) -> None:
    snapshot = router.resolve_candidates(pool_agent)
    with pytest.raises(ModelRouteConflictError, match="快照"):
        router.resolve_candidates(
            pool_agent,
            snapshot=replace(snapshot, snapshot_hash="0" * 64),
        )

    first_provider = pool_setup["providers"][0]
    db.update_ai_provider(first_provider, {"timeout_seconds": 99})
    with pytest.raises(ModelRouteConflictError, match="Provider"):
        router.resolve_candidates(pool_agent, snapshot=snapshot)


def test_saved_snapshot_rejects_pool_or_agent_version_change(
    router: ModelRouter,
    pool_agent: AIAgentConfig,
    pool_setup: dict[str, Any],
    db: Database,
) -> None:
    snapshot = router.resolve_candidates(pool_agent)
    db.update_ai_model_pool(
        pool_setup["root_id"],
        {"description": "changed"},
        expected_version=pool_setup["root_version"],
    )
    with pytest.raises(ModelRouteConflictError, match="模型池"):
        router.resolve_candidates(pool_agent, snapshot=snapshot)

    fresh_snapshot = router.resolve_candidates(pool_agent)
    db.update_ai_agent(pool_agent.id, {"temperature": 0.5})
    with pytest.raises(ModelRouteConflictError, match="Agent"):
        router.resolve_candidates(pool_agent, snapshot=fresh_snapshot)


def test_prompt_budget_uses_smallest_candidate_window(
    router: ModelRouter,
    pool_agent: AIAgentConfig,
    provider_state: dict[str, Any],
) -> None:
    snapshot = router.resolve_candidates(pool_agent)
    budget = router.build_prompt_budget(
        pool_agent,
        snapshot,
        MESSAGES,
        max_tokens=1_000,
    )

    assert budget.effective_context_window == 8_000
    assert budget.safety_margin == 256
    assert budget.message_overhead == 6
    assert budget.input_budget == 8_000 - 1_000 - 6 - 256
    assert budget.estimator == "utf8_bytes"
    assert provider_state["calls"] == list(pool_setup_provider_ids(snapshot))


def pool_setup_provider_ids(snapshot: CandidateSnapshot) -> tuple[int, ...]:
    return tuple(dict.fromkeys(candidate.provider_id for candidate in snapshot.candidates))


def test_prompt_budget_uses_provider_estimator_only_when_all_are_positive(
    router: ModelRouter,
    pool_agent: AIAgentConfig,
    provider_state: dict[str, Any],
) -> None:
    snapshot = router.resolve_candidates(pool_agent)
    provider_ids = pool_setup_provider_ids(snapshot)
    provider_state["estimates"] = {provider_id: 123 for provider_id in provider_ids}

    budget = router.build_prompt_budget(
        pool_agent,
        snapshot,
        MESSAGES,
        max_tokens=1_000,
    )
    assert budget.estimator == "provider"

    provider_state["estimates"][provider_ids[-1]] = 0
    fallback = router.build_prompt_budget(
        pool_agent,
        snapshot,
        MESSAGES,
        max_tokens=1_000,
    )
    assert fallback.estimator == "utf8_bytes"


@pytest.mark.parametrize("max_tokens", [0, 1_000_001, True])
def test_prompt_budget_rejects_invalid_output_reserve(
    max_tokens: Any,
    router: ModelRouter,
    pool_agent: AIAgentConfig,
) -> None:
    snapshot = router.resolve_candidates(pool_agent)
    with pytest.raises(ModelRouteError, match="max_tokens"):
        router.build_prompt_budget(pool_agent, snapshot, MESSAGES, max_tokens=max_tokens)


def test_prompt_budget_rejects_invalid_window_or_non_positive_budget(
    router: ModelRouter,
    pool_agent: AIAgentConfig,
) -> None:
    snapshot = router.resolve_candidates(pool_agent)
    pool_agent.context_window = 255
    with pytest.raises(ModelRouteError, match="上下文窗口"):
        router.build_prompt_budget(pool_agent, snapshot, MESSAGES, max_tokens=100)

    pool_agent.context_window = 8_000
    with pytest.raises(ModelRouteError, match="预算"):
        router.build_prompt_budget(pool_agent, snapshot, MESSAGES, max_tokens=7_800)


def test_provider_default_estimator_returns_none(fixed_agent: AIAgentConfig) -> None:
    assert fixed_agent.provider_id is not None
    provider = AIProvider(
        AIProviderConfig(
            id=fixed_agent.provider_id,
            name="base",
            provider_type="openai_compatible",
            base_url="https://example.test/v1",
            api_key="key",
            default_model="m",
        )
    )
    try:
        assert provider.estimate_message_tokens(MESSAGES) is None
    finally:
        provider.close()


def normal_done() -> AIStreamChunk:
    return AIStreamChunk(type="done", data={"finish_reason": "stop"})


class FakeNetworkRequest:
    pass


class FakeStreamingProvider(AIProvider):
    def __init__(
        self,
        config: AIProviderConfig,
        registry: "FakeProviderRegistry",
    ) -> None:
        super().__init__(config)
        self.registry = registry

    def stream_generate(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
        *,
        request_guard=None,
        is_cancelled=None,
    ) -> Iterator[AIStreamChunk]:
        del messages, temperature, top_p, max_tokens
        self.registry.calls.append((self.config.name, model))
        if is_cancelled is not None and is_cancelled():
            raise AIProviderError(
                "请求已取消",
                category="cancelled",
                scope="model",
                finish_reason="cancelled",
            )
        if request_guard is not None:
            request_guard()
        self.registry.network_calls.append((self.config.name, model))
        events = self.registry.responses.get(
            (self.config.name, model),
            self.registry.responses.get((self.config.name, None)),
        )
        if events is None:
            events = [
                AIProviderError(
                    "未配置 fake 响应",
                    category="test_missing_response",
                    scope="model",
                )
            ]
        try:
            for event in events:
                if isinstance(event, FakeNetworkRequest):
                    if request_guard is not None:
                        request_guard()
                    continue
                if callable(event):
                    event = event()
                if isinstance(event, BaseException):
                    raise event
                if event is not None:
                    yield event
        finally:
            self.registry.closed_calls.append((self.config.name, model))


class FakeProviderRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.network_calls: list[tuple[str, str]] = []
        self.responses: dict[tuple[str, str | None], list[Any]] = {}
        self.providers: dict[int, FakeStreamingProvider] = {}
        self.closed_calls: list[tuple[str, str]] = []

    def get_provider(self, config: AIProviderConfig) -> AIProvider:
        provider = self.providers.get(config.id)
        if provider is None:
            provider = FakeStreamingProvider(config, self)
            self.providers[config.id] = provider
        return provider

    def succeed(
        self,
        provider_name: str,
        chunks: list[AIStreamChunk],
        *,
        model: str | None = None,
    ) -> None:
        self.responses[(provider_name, model)] = list(chunks)

    def fail(
        self,
        provider_name: str,
        error: AIProviderError,
        *,
        model: str | None = None,
    ) -> None:
        self.responses[(provider_name, model)] = [error]

    def partial_then_fail(
        self,
        provider_name: str,
        text: str,
        error: AIProviderError,
        *,
        model: str | None = None,
    ) -> None:
        self.responses[(provider_name, model)] = [
            AIStreamChunk(type="delta", text=text),
            error,
        ]

    def close(self) -> None:
        for provider in self.providers.values():
            provider.close()


@pytest.fixture
def fake_providers() -> Iterator[FakeProviderRegistry]:
    registry = FakeProviderRegistry()
    try:
        yield registry
    finally:
        registry.close()


@pytest.fixture
def route_router(
    db: Database,
    fake_providers: FakeProviderRegistry,
) -> ModelRouter:
    def db_factory() -> Database:
        return Database(db.path)

    def load_provider_config(database: Database, provider_id: int) -> AIProviderConfig:
        row = database.get_ai_provider(provider_id, include_secret=True)
        if row is None:
            raise RuntimeError("Provider 不存在")
        return AIProviderConfig(
            id=int(row["id"]),
            name=row["name"],
            provider_type=row["provider_type"],
            base_url=row.get("base_url"),
            api_key="decrypted-test-key",
            default_model=row.get("default_model"),
            timeout_seconds=int(row.get("timeout_seconds") or 120),
            max_retries=int(row.get("max_retries") or 2),
            proxy=row.get("proxy"),
            context_window=int(row.get("context_window") or 128_000),
            stream_enabled=bool(row.get("stream_enabled", 1)),
            enabled=bool(row.get("enabled")),
        )

    return ModelRouter(
        db_factory,
        load_provider_config,
        fake_providers.get_provider,
    )


def snapshot_with_candidates(
    router: ModelRouter,
    snapshot: CandidateSnapshot,
    candidates: tuple[ModelCandidate, ...],
) -> CandidateSnapshot:
    snapshot_hash = router._snapshot_hash(  # noqa: SLF001 - canonical test setup
        candidates,
        snapshot.agent_config_hash,
        snapshot.binding_version,
    )
    return CandidateSnapshot(
        candidates=candidates,
        snapshot_hash=snapshot_hash,
        agent_config_hash=snapshot.agent_config_hash,
        binding_version=snapshot.binding_version,
    )


def make_route_request(
    db: Database,
    router: ModelRouter,
    agent: AIAgentConfig,
    *,
    job_id: str,
    stage: str = "main",
    snapshot: CandidateSnapshot | None = None,
) -> RouteRequest:
    resolved = snapshot or router.resolve_candidates(agent)
    db.create_ai_job(
        job_id,
        "continue",
        agent.id,
        {"source": "router-test"},
        owner_token="route-owner",
        stage="main",
        route_deadline_at="2099-01-01 00:00:00",
    )
    snapshot_payload = {
        "agent_config_hash": resolved.agent_config_hash,
        "binding_version": resolved.binding_version,
        "candidates": [asdict(candidate) for candidate in resolved.candidates],
    }
    assert db.set_ai_job_candidate_snapshot(
        job_id,
        "route-owner",
        snapshot_payload,
        resolved.snapshot_hash,
    )
    return RouteRequest(
        job_id=job_id,
        stage=stage,  # type: ignore[arg-type]
        messages=MESSAGES,
        candidate_snapshot=resolved,
        max_tokens=1_000,
        owner_token="route-owner",
        on_delta=lambda _text: None,
        on_progress=lambda _data: None,
    )


@pytest.fixture
def route_request(
    db: Database,
    route_router: ModelRouter,
    pool_agent: AIAgentConfig,
) -> RouteRequest:
    return make_route_request(
        db,
        route_router,
        pool_agent,
        job_id="route-job",
    )


@pytest.fixture
def internal_request(
    db: Database,
    route_router: ModelRouter,
    pool_agent: AIAgentConfig,
) -> RouteRequest:
    return make_route_request(
        db,
        route_router,
        pool_agent,
        job_id="internal-job",
        stage="internal",
    )


def test_failure_before_first_delta_switches_across_providers(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
) -> None:
    fake_providers.fail(
        "p1",
        AIProviderError("down", category="gateway", scope="provider"),
    )
    fake_providers.succeed(
        "p2",
        [AIStreamChunk(type="delta", text="正文"), normal_done()],
    )

    result = route_router.execute(route_request)

    assert result.finish_state == "succeeded"
    assert result.output_text == "正文"
    assert [attempt["status"] for attempt in result.attempts] == [
        "failed",
        "succeeded",
    ]


def test_failure_after_first_main_delta_is_partial_and_never_switches(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    fake_providers.partial_then_fail(
        "p1",
        "半截",
        AIProviderError("drop", category="network", scope="provider"),
    )
    fake_providers.succeed(
        "p2",
        [AIStreamChunk(type="delta", text="不应调用"), normal_done()],
    )

    result = route_router.execute(route_request)

    assert result.finish_state == "partial"
    assert result.output_text == "半截"
    assert fake_providers.calls == [("p1", "m1")]
    assert db.get_ai_job(route_request.job_id)["status"] == "partial"


def test_provider_scope_failure_skips_remaining_models_for_same_provider(
    route_router: ModelRouter,
    pool_agent: AIAgentConfig,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    base = route_router.resolve_candidates(pool_agent)
    first = base.candidates[0]
    same_provider = replace(
        first,
        model_key="m1-secondary",
        provider_model_id=None,
        pool_position=2,
        candidate_index=1,
    )
    other_provider = replace(
        base.candidates[1],
        pool_position=3,
        candidate_index=2,
    )
    snapshot = snapshot_with_candidates(
        route_router,
        base,
        (first, same_provider, other_provider),
    )
    request = make_route_request(
        db,
        route_router,
        pool_agent,
        job_id="provider-scope-job",
        snapshot=snapshot,
    )
    fake_providers.fail(
        "p1",
        AIProviderError(
            "429",
            category="rate_limited",
            scope="provider",
            retry_after=60,
        ),
    )
    fake_providers.succeed(
        "p2",
        [AIStreamChunk(type="delta", text="ok"), normal_done()],
    )

    result = route_router.execute(request)

    assert fake_providers.calls == [("p1", "m1"), ("p2", "m2")]
    assert result.attempts[0]["error_scope"] == "provider"
    assert result.finish_state == "succeeded"


def test_internal_failure_never_creates_partial_or_pins_main(
    route_router: ModelRouter,
    internal_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    fake_providers.partial_then_fail(
        "p1",
        "内部摘要",
        AIProviderError("drop", category="network", scope="model"),
    )

    result = route_router.execute(internal_request)

    assert result.finish_state == "failed_before_output"
    job = db.get_ai_job(internal_request.job_id)
    assert job["status"] == "running"
    assert job["pinned_candidate_index"] is None


@dataclass
class GeneratorCapture(Generic[T, R]):
    items: list[T]
    return_value: R


def collect_generator_return(
    generator: Generator[T, None, R],
) -> GeneratorCapture[T, R]:
    items: list[T] = []
    while True:
        try:
            items.append(next(generator))
        except StopIteration as stopped:
            return GeneratorCapture(items=items, return_value=stopped.value)


def collected_delta(chunks: Sequence[AIStreamChunk]) -> str:
    return "".join(chunk.text for chunk in chunks if chunk.type == "delta")


def test_execute_stream_forwards_progress_delta_and_callbacks(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
) -> None:
    progress_callbacks: list[dict[str, Any]] = []
    delta_callbacks: list[str] = []
    route_request.on_progress = progress_callbacks.append
    route_request.on_delta = delta_callbacks.append
    fake_providers.succeed(
        "p1",
        [
            AIStreamChunk(type="progress", data={"phase": "provider"}),
            AIStreamChunk(type="delta", text="正文"),
            normal_done(),
        ],
    )

    captured = collect_generator_return(route_router.execute_stream(route_request))

    assert [chunk.type for chunk in captured.items] == [
        "progress",
        "progress",
        "delta",
    ]
    assert progress_callbacks[0]["phase"] == "route"
    assert progress_callbacks[1] == {"phase": "provider"}
    assert delta_callbacks == ["正文"]
    assert captured.return_value.finish_state == "succeeded"


def test_first_main_delta_pins_candidate_for_later_call(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    fake_providers.succeed(
        "p1",
        [AIStreamChunk(type="delta", text="第一批"), normal_done()],
    )
    first = route_router.execute(route_request)
    fake_providers.succeed(
        "p1",
        [AIStreamChunk(type="delta", text="第二批"), normal_done()],
    )

    second = route_router.execute(route_request)

    assert first.finish_state == second.finish_state == "succeeded"
    assert fake_providers.calls == [("p1", "m1"), ("p1", "m1")]
    job = db.get_ai_job(route_request.job_id)
    assert job["status"] == "running"
    assert job["pinned_candidate_index"] == 0
    assert [attempt["status"] for attempt in job["attempts"]] == [
        "succeeded",
        "succeeded",
    ]


def test_context_overflow_records_skip_without_provider_call(
    route_router: ModelRouter,
    pool_agent: AIAgentConfig,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    base = route_router.resolve_candidates(pool_agent)
    candidates = (
        replace(base.candidates[0], context_window=256),
        base.candidates[1],
    )
    snapshot = snapshot_with_candidates(route_router, base, candidates)
    request = make_route_request(
        db,
        route_router,
        pool_agent,
        job_id="context-overflow-job",
        snapshot=snapshot,
    )
    request.messages = [{"role": "user", "content": "x" * 500}]
    request.max_tokens = 100
    fake_providers.succeed(
        "p2",
        [AIStreamChunk(type="delta", text="ok"), normal_done()],
    )

    result = route_router.execute(request)

    assert fake_providers.calls == [("p2", "m2")]
    assert result.finish_state == "succeeded"
    assert [attempt["error_category"] for attempt in result.attempts] == [
        "context_overflow",
        None,
    ]


def test_attempt_budget_exhaustion_fails_before_provider(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    candidate = route_request.candidate_snapshot.candidates[0]
    for _index in range(16):
        attempt_index = db.allocate_ai_model_attempt(
            route_request.job_id,
            route_request.owner_token,
            route_router._attempt_data(route_request, candidate),  # noqa: SLF001
        )
        assert db.finish_ai_model_attempt(
            route_request.job_id,
            attempt_index,
            route_request.owner_token,
            "failed",
            error_category="seeded",
            finish_reason="error",
        )

    result = route_router.execute(route_request)

    assert result.finish_state == "failed_before_output"
    assert fake_providers.calls == []
    job = db.get_ai_job(route_request.job_id)
    assert job["status"] == "failed"
    assert "route_budget_exhausted" in job["error_message"]


def test_network_budget_exhaustion_finishes_current_attempt_and_job(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    db.conn.execute(
        "UPDATE ai_jobs SET network_request_count = 32 WHERE job_id = ?",
        (route_request.job_id,),
    )
    db.conn.commit()
    fake_providers.succeed(
        "p1",
        [AIStreamChunk(type="delta", text="不应出现"), normal_done()],
    )

    result = route_router.execute(route_request)

    assert result.finish_state == "failed_before_output"
    assert fake_providers.network_calls == []
    assert result.attempts == ()
    assert db.get_ai_job(route_request.job_id)["status"] == "failed"


def test_expired_deadline_fails_without_attempt_or_provider(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    db.conn.execute(
        "UPDATE ai_jobs SET route_deadline_at = '2000-01-01 00:00:00' "
        "WHERE job_id = ?",
        (route_request.job_id,),
    )
    db.conn.commit()

    result = route_router.execute(route_request)

    assert result.finish_state == "failed_before_output"
    assert result.attempts == ()
    assert fake_providers.calls == []
    assert "route_budget_exhausted" in db.get_ai_job(route_request.job_id)[
        "error_message"
    ]


def test_cancellation_after_delta_closes_iterator_and_never_switches(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    cancelled = False

    def on_delta(_text: str) -> None:
        nonlocal cancelled
        cancelled = True

    route_request.on_delta = on_delta
    route_request.is_cancelled = lambda: cancelled
    fake_providers.succeed(
        "p1",
        [AIStreamChunk(type="delta", text="半截"), normal_done()],
    )
    fake_providers.succeed(
        "p2",
        [AIStreamChunk(type="delta", text="不应调用"), normal_done()],
    )

    result = route_router.execute(route_request)

    assert result.finish_state == "cancelled"
    assert fake_providers.calls == [("p1", "m1")]
    assert fake_providers.closed_calls == [("p1", "m1")]
    job = db.get_ai_job(route_request.job_id)
    assert job["status"] == "cancelled"
    assert job["attempts"][0]["status"] == "cancelled"


def test_generator_exit_cancels_running_attempt_and_job(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    fake_providers.succeed(
        "p1",
        [AIStreamChunk(type="delta", text="正文"), normal_done()],
    )
    stream = route_router.execute_stream(route_request)
    while True:
        chunk = next(stream)
        if chunk.type == "delta":
            break

    stream.close()

    assert fake_providers.closed_calls == [("p1", "m1")]
    job = db.get_ai_job(route_request.job_id)
    assert job["status"] == "cancelled"
    assert job["attempts"][0]["status"] == "cancelled"


def test_validation_exhaustion_fails_job_without_partial_or_pin(
    route_router: ModelRouter,
    pool_agent: AIAgentConfig,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    request = make_route_request(
        db,
        route_router,
        pool_agent,
        job_id="validation-job",
        stage="validation",
    )
    fake_providers.partial_then_fail(
        "p1",
        "审阅片段",
        AIProviderError("review drop", category="network", scope="provider"),
    )

    result = route_router.execute(request)

    assert result.finish_state == "failed_before_output"
    job = db.get_ai_job(request.job_id)
    assert job["status"] == "failed"
    assert job["pinned_candidate_index"] is None
    assert all(attempt["status"] == "failed" for attempt in job["attempts"])


def test_heartbeat_runs_on_its_own_database_and_stops_after_route(
    monkeypatch: pytest.MonkeyPatch,
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
) -> None:
    attempt_heartbeat_seen = threading.Event()
    original = Database.heartbeat_ai_job
    monkeypatch.setattr(
        "pixiv_novel_sync.ai.model_router._HEARTBEAT_INTERVAL_SECONDS",
        0.01,
    )

    def observed_heartbeat(
        database: Database,
        job_id: str,
        owner_token: str,
        lease_until: str,
    ) -> bool:
        renewed = original(database, job_id, owner_token, lease_until)
        running_attempt = database.conn.execute(
            """
            SELECT 1 FROM ai_job_model_attempts
            WHERE job_id = ? AND status = 'running'
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if renewed and running_attempt is not None:
            attempt_heartbeat_seen.set()
        return renewed

    monkeypatch.setattr(Database, "heartbeat_ai_job", observed_heartbeat)

    def wait_for_heartbeat() -> AIStreamChunk:
        assert attempt_heartbeat_seen.wait(timeout=2)
        return AIStreamChunk(type="progress", data={"phase": "waiting"})

    fake_providers.responses[("p1", None)] = [
        wait_for_heartbeat,
        AIStreamChunk(type="delta", text="正文"),
        normal_done(),
    ]

    result = route_router.execute(route_request)

    assert result.finish_state == "succeeded"
    assert attempt_heartbeat_seen.is_set()
    assert not any(
        thread.name == f"ai-route-heartbeat-{route_request.job_id}"
        and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_terminal_race_discards_late_delta_and_result(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    callbacks: list[str] = []
    route_request.on_delta = callbacks.append

    def finish_elsewhere() -> AIStreamChunk:
        assert db.finish_ai_job_cas(
            route_request.job_id,
            route_request.owner_token,
            "failed",
            error_message="external terminal winner",
        )
        return AIStreamChunk(type="delta", text="迟到正文")

    fake_providers.responses[("p1", None)] = [finish_elsewhere, normal_done()]

    result = route_router.execute(route_request)

    assert result.finish_state == "failed_before_output"
    assert result.output_text == ""
    assert callbacks == []
    assert db.get_ai_job(route_request.job_id)["status"] == "failed"


def test_terminal_race_after_route_progress_returns_terminal_result(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    stream = route_router.execute_stream(route_request)
    first = next(stream)
    assert first.type == "progress"
    assert db.finish_ai_job_cas(
        route_request.job_id,
        route_request.owner_token,
        "cancelled",
    )

    captured = collect_generator_return(stream)

    assert captured.items == []
    assert captured.return_value.finish_state == "cancelled"
    assert fake_providers.calls == []


def test_provider_config_load_failure_is_recorded_and_switches_provider(
    monkeypatch: pytest.MonkeyPatch,
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
) -> None:
    first_provider_id = route_request.candidate_snapshot.candidates[0].provider_id
    original = route_router._load_provider_config  # noqa: SLF001

    def load_provider_config(database: Database, provider_id: int) -> AIProviderConfig:
        if provider_id == first_provider_id:
            raise RuntimeError("Provider 配置无法解密")
        return original(database, provider_id)

    monkeypatch.setattr(route_router, "_load_provider_config", load_provider_config)
    fake_providers.succeed(
        "p2",
        [AIStreamChunk(type="delta", text="ok"), normal_done()],
    )

    result = route_router.execute(route_request)

    assert result.finish_state == "succeeded"
    assert fake_providers.calls == [("p2", "m2")]
    assert result.attempts[0]["status"] == "failed"
    assert result.attempts[0]["error_scope"] == "provider"
    assert result.attempts[0]["error_category"] == "provider_configuration"


def test_network_budget_exhaustion_after_main_delta_is_partial(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    db.conn.execute(
        "UPDATE ai_jobs SET network_request_count = 31 WHERE job_id = ?",
        (route_request.job_id,),
    )
    db.conn.commit()
    fake_providers.responses[("p1", None)] = [
        AIStreamChunk(type="delta", text="半截"),
        FakeNetworkRequest(),
        normal_done(),
    ]

    result = route_router.execute(route_request)

    assert result.finish_state == "partial"
    assert result.output_text == "半截"
    assert result.attempts[-1]["status"] == "partial"
    assert result.attempts[-1]["error_category"] == "route_budget_exhausted"
    assert db.get_ai_job(route_request.job_id)["status"] == "partial"


def test_execute_rejects_provider_config_change_before_attempt_or_call(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    first_provider_id = route_request.candidate_snapshot.candidates[0].provider_id
    db.update_ai_provider(first_provider_id, {"timeout_seconds": 99})

    with pytest.raises(ModelRouteConflictError, match="Provider"):
        route_router.execute(route_request)

    assert fake_providers.calls == []
    assert db.get_ai_job(route_request.job_id)["attempts"] == []


def test_non_normal_done_after_main_delta_is_partial_and_does_not_switch(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    fake_providers.succeed(
        "p1",
        [
            AIStreamChunk(type="delta", text="被截断"),
            AIStreamChunk(type="done", data={"finish_reason": "length"}),
        ],
    )
    fake_providers.succeed(
        "p2",
        [AIStreamChunk(type="delta", text="不应调用"), normal_done()],
    )

    result = route_router.execute(route_request)

    assert result.finish_state == "partial"
    assert result.output_text == "被截断"
    assert fake_providers.calls == [("p1", "m1")]
    assert result.attempts[0]["finish_reason"] == "length"
    assert db.get_ai_job(route_request.job_id)["status"] == "partial"


def test_empty_response_switches_to_next_candidate(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
) -> None:
    fake_providers.succeed("p1", [normal_done()])
    fake_providers.succeed(
        "p2",
        [AIStreamChunk(type="delta", text="ok"), normal_done()],
    )

    result = route_router.execute(route_request)

    assert result.finish_state == "succeeded"
    assert fake_providers.calls == [("p1", "m1"), ("p2", "m2")]
    assert result.attempts[0]["error_category"] == "empty_response"
    assert result.attempts[1]["status"] == "succeeded"


def test_main_exhaustion_finishes_job_with_route_exhausted(
    route_router: ModelRouter,
    route_request: RouteRequest,
    fake_providers: FakeProviderRegistry,
    db: Database,
) -> None:
    result = route_router.execute(route_request)

    assert result.finish_state == "failed_before_output"
    assert len(result.attempts) == 3
    assert all(attempt["status"] == "failed" for attempt in result.attempts)
    job = db.get_ai_job(route_request.job_id)
    assert job["status"] == "failed"
    assert "route_exhausted" in job["error_message"]
    assert fake_providers.calls == [("p1", "m1"), ("p2", "m2"), ("p3", "m3")]

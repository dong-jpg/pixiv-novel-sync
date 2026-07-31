from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, asdict, fields, replace
from pathlib import Path
from typing import Any

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
from pixiv_novel_sync.ai.models import AIAgentConfig, AIProviderConfig
from pixiv_novel_sync.ai.providers import AIProvider
from pixiv_novel_sync.storage_db import Database


MESSAGES = [{"role": "user", "content": "正文"}]


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

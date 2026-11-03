from __future__ import annotations

import json
from dataclasses import asdict, fields, replace
from pathlib import Path

import pytest

from pixiv_novel_sync.ai.invocation import InvocationValidationError, invocation_messages, normalize_text_invocation
from pixiv_novel_sync.ai.invocation_planner import TokenEstimate
from pixiv_novel_sync.ai.model_router import ModelRouteConflictError, ModelRouter, RouteRequest
from pixiv_novel_sync.ai.models import AIAgentConfig, AIProviderConfig, AIStreamChunk
from pixiv_novel_sync.ai.providers import AIProvider, AIProviderError
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "invocation.db")
    database.init_schema()
    try:
        yield database
    finally:
        database.close()


def seed_provider(db, name="provider", **overrides):
    return db.create_ai_provider({
        "name": name, "provider_type": "openai_compatible",
        "base_url": "https://provider.example.test/v1",
        "api_key_encrypted": "synthetic-cipher-" + name,
        "default_model": "model-a", "context_window": 128_000,
        "enabled": True, **overrides,
    })


def agent_from_row(db, agent_id):
    row = db.get_ai_agent(agent_id)
    values = {field.name: row[field.name] for field in fields(AIAgentConfig)}
    values["enabled"] = bool(values["enabled"])
    values["required_capabilities"] = tuple(values["required_capabilities"] or ())
    return AIAgentConfig(**values)


def seed_agent(db, provider_id, **overrides):
    agent_id = db.create_ai_agent({
        "name": "agent", "task_type": "general", "binding_type": "fixed",
        "provider_id": provider_id, "model": "model-a", "system_prompt": "not injected",
        "context_window": 16_000, "max_tokens": 1_000, **overrides,
    })
    return agent_from_row(db, agent_id)


def request_for(agent, **overrides):
    return normalize_text_invocation({
        "messages": [{"role": "system", "content": "complete system"},
                     {"role": "user", "content": "synthetic full text TAIL"}],
        **overrides,
    }, agent=agent)


def load_config(db, provider_id):
    row = db.get_ai_provider(provider_id)
    return AIProviderConfig(
        row["id"], row["name"], row["provider_type"], row["base_url"],
        "synthetic-decrypted-key", row["default_model"],
        context_window=row["context_window"],
    )


def forbidden(*args, **kwargs):
    raise AssertionError("Credential, provider, or estimation work is forbidden")


@pytest.fixture
def router(db):
    value = ModelRouter(lambda: Database(db.path), load_config, forbidden)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def fixed_agent(db):
    return seed_agent(db, seed_provider(db))


def seed_pool(db, members, *, fallback=None, enabled=True, name="pool"):
    pool_id = db.create_ai_model_pool({"name": name, "pool_kind": "custom", "fallback_pool_id": fallback})
    version = db.replace_ai_model_pool_members(pool_id, members, expected_version=1)
    if enabled:
        db.update_ai_model_pool(pool_id, {"enabled": True}, expected_version=version)
    return pool_id


def pool_setup(db, windows=(64_000, 32_000), *, same_provider=False):
    providers, models, members = [], [], []
    for index, window in enumerate(windows):
        provider_id = providers[0] if same_provider and providers else seed_provider(db, "p" + str(index), context_window=1_000_000)
        model_id = db.create_ai_provider_model({
            "provider_id": provider_id, "model_key": "model-" + str(index),
            "manual_context_window": window, "manual_capabilities": ["streaming"],
        })
        providers.append(provider_id)
        models.append(model_id)
        members.append({"provider_model_id": model_id, "enabled": True})
    pool_id = seed_pool(db, members)
    agent = seed_agent(db, None, binding_type="pool", model=None, model_pool_id=pool_id, context_window=1_000_000)
    return agent, providers, models, pool_id


def test_planning_never_loads_credentials_or_provider(router, fixed_agent, monkeypatch):
    monkeypatch.setattr(router, "_load_provider_config", forbidden)
    monkeypatch.setattr(router, "_get_provider", forbidden)
    plan = router.plan_invocation(fixed_agent, request_for(fixed_agent))
    assert plan.can_run
    serialized = json.dumps(asdict(plan))
    assert "synthetic-cipher" not in serialized
    assert "synthetic full text" not in serialized
    assert "not injected" not in serialized


def test_v2_fixed_planning_and_validation_do_not_materialize_catalog(db, router, fixed_agent, monkeypatch):
    monkeypatch.setattr(Database, "list_ai_provider_models", forbidden)
    invocation = request_for(fixed_agent)
    snapshot = router.resolve_invocation(fixed_agent, invocation)
    assert router.validate_resume_snapshot(fixed_agent, snapshot, 0, invocation=invocation) == snapshot
    db.create_ai_provider_model({"provider_id": fixed_agent.provider_id, "model_key": fixed_agent.model, "enabled": False})
    assert router.plan_invocation(fixed_agent, invocation).excluded[0].reason == "model_disabled"


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("network_error", [False, True])
def test_v2_deadline_keeps_owned_body_in_memory_and_safe_usage(db, route_router, registry, monkeypatch, published, network_error):
    from datetime import datetime, timezone

    agent, providers, _, _ = pool_setup(db)
    request = make_route_request(db, route_router, agent, retention_policy="metadata_only")
    # Exercise the real final-write retention gate, not an output-redacting mock.
    db.conn.execute("UPDATE ai_jobs SET input_json = ?, route_deadline_at = ? WHERE job_id = ?",
                    (json.dumps({"retention_policy": "metadata_only"}), "2030-01-01 00:10:00", request.job_id))
    db.conn.commit()
    clock = {"now": datetime(2030, 1, 1, tzinfo=timezone.utc)}
    monkeypatch.setattr("pixiv_novel_sync.ai.model_router._utc_now", lambda: clock["now"])

    def expire(_messages):
        clock["now"] = datetime(2030, 1, 1, 0, 10, tzinfo=timezone.utc)
        if network_error:
            return AIProviderError("PRIVATE-NETWORK-ERROR", category="network", scope="provider")
        return AIStreamChunk(type="delta", text="UNPUBLISHED")

    events = [AIStreamChunk(type="delta", text="PRIVATE-PARTIAL")] if published else []
    registry.responses[(providers[0], "model-0")] = [*events, expire]
    chunks, result = collect_result(route_router.execute_stream(request))
    assert result.finish_state == ("partial" if published else "failed_before_output")
    assert result.output_text == ("PRIVATE-PARTIAL" if published else "")
    assert [c.text for c in chunks if c.type == "delta"] == (["PRIVATE-PARTIAL"] if published else [])
    job = db.get_ai_job(request.job_id)
    assert job["error_message"] == "route_budget_exhausted"
    assert job["output_text"] in (None, "") and job["output_json"] is None
    assert "PRIVATE-" not in json.dumps(job)
    assert job["attempts"][0]["error_category"] == "route_budget_exhausted"
    assert db.get_ai_job_usage_summary(request.job_id)["unreported_requests"] == 1
    assert registry.calls == [(providers[0], "model-0")] == registry.closed_calls


def test_v2_heartbeat_retry_does_not_log_exception_payload(db, route_router, registry, fixed_agent, monkeypatch, caplog):
    import threading

    request = make_route_request(db, route_router, fixed_agent, retention_policy="metadata_only")
    original = Database.heartbeat_ai_job
    recovered = threading.Event()
    calls = []

    def heartbeat(database, *args):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("PRIVATE-HEARTBEAT-PAYLOAD")
        result = original(database, *args)
        recovered.set()
        return result

    def wait(_messages):
        assert recovered.wait(2)
        return AIStreamChunk(type="delta", text="OK")

    monkeypatch.setattr(Database, "heartbeat_ai_job", heartbeat)
    monkeypatch.setattr("pixiv_novel_sync.ai.model_router._HEARTBEAT_INTERVAL_SECONDS", 0.01)
    registry.responses[(fixed_agent.provider_id, fixed_agent.model)] = [wait, AIStreamChunk(type="done", data={"finish_reason": "stop"})]
    assert route_router.execute(request).finish_state == "succeeded"
    assert len(calls) >= 2
    assert "PRIVATE-HEARTBEAT-PAYLOAD" not in caplog.text
    assert any(record.name == "pixiv_novel_sync.ai.model_router" for record in caplog.records)


def test_known_disabled_fixed_is_rejected_but_never_registered_key_survives(db, router, fixed_agent):
    req = request_for(fixed_agent)
    before = router.plan_invocation(fixed_agent, req)
    assert before.can_run
    assert before.eligible[0].profile.provider_model_id is None
    assert before.eligible[0].profile.model_context_window is None
    assert before.eligible[0].profile.max_output_tokens is None
    model_id = db.create_ai_provider_model({"provider_id": fixed_agent.provider_id, "model_key": fixed_agent.model, "enabled": False})
    plan = router.plan_invocation(fixed_agent, req)
    assert not plan.can_run
    assert plan.excluded[0].profile.provider_model_id == model_id
    assert plan.excluded[0].reason == "model_disabled"


@pytest.mark.parametrize("change", [{"enabled": False}, {"binding_type": "invalid"}, {"provider_id": None}, {"binding_type": "pool", "model_pool_id": None}, {"model": ""}])
def test_planning_invalid_binding_uses_fixed_local_configuration_error(router, fixed_agent, change):
    with pytest.raises(InvocationValidationError) as caught:
        router.plan_invocation(replace(fixed_agent, **change), request_for(fixed_agent))
    assert caught.value.code == "invalid_configuration"


def test_planning_fixed_default_normalizes_key_and_retains_unavailable_catalog(db, router, fixed_agent):
    db.update_ai_provider(fixed_agent.provider_id, {"default_model": " model-a "})
    agent = replace(fixed_agent, model=None)
    model_id = db.create_ai_provider_model({"provider_id": agent.provider_id, "model_key": " model-a ", "manual_max_output_tokens": 800})
    plan = router.plan_invocation(agent, request_for(agent))
    assert plan.excluded[0].reason == "output_limit"
    assert plan.excluded[0].profile.model_key == " model-a "
    assert plan.excluded[0].profile.max_output_tokens == 800
    db.conn.execute("UPDATE ai_provider_models SET manual = 0, discovered_available = 0 WHERE id = ?", (model_id,))
    db.conn.commit()
    plan = router.plan_invocation(agent, request_for(agent))
    assert plan.excluded[0].reason == "model_unavailable"
    db.update_ai_provider(agent.provider_id, {"enabled": False})
    assert router.plan_invocation(agent, request_for(agent)).excluded[0].reason == "provider_disabled"


@pytest.mark.parametrize("disabled_pool", [False, True])
def test_disabled_occurrence_does_not_hide_later_eligible_pool_member(db, router, disabled_pool):
    provider_id = seed_provider(db)
    model_id = db.create_ai_provider_model({"provider_id": provider_id, "model_key": "m"})
    fallback = seed_pool(db, [{"provider_model_id": model_id}], name="fallback")
    root = seed_pool(db, [{"provider_model_id": model_id}], fallback=fallback, enabled=not disabled_pool)
    db.conn.execute("UPDATE ai_model_pool_members SET enabled = 0 WHERE pool_id = ?", (root,))
    db.conn.commit()
    agent = seed_agent(db, None, binding_type="pool", model=None, model_pool_id=root)
    plan = router.plan_invocation(agent, request_for(agent))
    assert len(plan.eligible) == 1
    assert plan.eligible[0].candidate_index == 0
    assert plan.eligible[0].profile.source_index == 1
    assert plan.eligible[0].profile.pool_id == fallback
    assert plan.eligible[0].profile.fallback_depth == 1
    assert plan.excluded[0].reason == ("pool_disabled" if disabled_pool else "member_disabled")
    db.update_ai_provider(provider_id, {"enabled": False})
    assert {row.reason for row in router.plan_invocation(agent, request_for(agent)).excluded} == {"provider_disabled"}


@pytest.mark.parametrize("broken", ["pool", "model", "provider", "cycle", "ninth", "members"])
def test_projection_rejects_broken_references_and_raw_pool_bounds(db, router, broken):
    agent, providers, models, root = pool_setup(db, windows=(16_000,))
    db.conn.execute("PRAGMA foreign_keys = OFF")
    if broken == "pool":
        db.conn.execute("UPDATE ai_model_pools SET fallback_pool_id = 99999 WHERE id = ?", (root,))
    elif broken == "model":
        db.conn.execute("UPDATE ai_model_pool_members SET provider_model_id = 99999 WHERE pool_id = ?", (root,))
    elif broken == "provider":
        db.conn.execute("UPDATE ai_provider_models SET provider_id = 99999 WHERE id = ?", (models[0],))
    elif broken == "cycle":
        db.conn.execute("UPDATE ai_model_pools SET fallback_pool_id = id WHERE id = ?", (root,))
    elif broken == "ninth":
        previous = root
        for index in range(8):
            cursor = db.conn.execute("INSERT INTO ai_model_pools (name, pool_kind, enabled) VALUES (?, 'custom', 0)", ("extra" + str(index),))
            db.conn.execute("UPDATE ai_model_pools SET fallback_pool_id = ? WHERE id = ?", (cursor.lastrowid, previous))
            previous = cursor.lastrowid
    else:
        for index in range(64):
            db.conn.execute("INSERT INTO ai_model_pool_members (pool_id, provider_model_id, position, enabled) VALUES (?, ?, ?, 0)", (root, 99_000 + index, index + 2))
    db.conn.commit()
    with pytest.raises(InvocationValidationError) as caught:
        router.plan_invocation(agent, request_for(agent))
    assert caught.value.code == "invalid_configuration"


def test_resolution_prepares_only_ordered_unique_eligible_ids_then_refreezes(db):
    from pixiv_novel_sync.ai.model_sync import provider_model_sync_config_hash

    agent, providers, models, _ = pool_setup(db, windows=(64_000, 64_000, 64_000), same_provider=True)
    calls = []

    def prepare(ids):
        calls.append(ids)
        db.update_ai_provider(providers[0], {"api_key_encrypted": "synthetic-new-cipher"})

    router = ModelRouter(lambda: Database(db.path), forbidden, forbidden,
                         token_estimator=lambda *_: TokenEstimate(7, "test-exact", True, True),
                         estimator_version="test-v1", prepare_invocation_providers=prepare)
    try:
        req = request_for(agent)
        assert router.plan_invocation(agent, req).eligible[0].input_tokens == 7
        assert calls == []
        snapshot = router.resolve_invocation(agent, req)
        assert calls == [(providers[0],)]
        expected = provider_model_sync_config_hash(db.get_ai_provider(providers[0], include_secret=True))
        assert {candidate.provider_config_hash for candidate in snapshot.candidates} == {expected}
        assert snapshot.plan.estimator_version == "test-v1"
        router.resolve_invocation(agent, request_for(agent, allowed_provider_ids=[]))
        assert calls == [(providers[0],)]
    finally:
        router.close()


class FakeProvider(AIProvider):
    def __init__(self, config, registry):
        super().__init__(config)
        self.registry = registry

    def stream_generate(self, messages, model, temperature, top_p, max_tokens, *, request_guard=None, is_cancelled=None, on_usage=None, deadline=None):
        registry = self.registry
        key = (self.config.id, model)
        registry.calls.append(key)
        registry.received.append({"messages": [dict(item) for item in messages], "temperature": temperature, "top_p": top_p, "max_tokens": max_tokens})
        if registry.before_send is not None:
            registry.before_send(self.config)
        if request_guard is not None:
            request_guard()
        registry.network_calls.append(key)
        try:
            for event in registry.responses.get(key, [AIStreamChunk(type="delta", text="SUCCESS"), AIStreamChunk(type="done", data={"finish_reason": "stop"})]):
                if callable(event):
                    event = event(messages)
                if isinstance(event, BaseException):
                    raise event
                if event is not None:
                    yield event
        finally:
            registry.closed_calls.append(key)


class FakeRegistry:
    def __init__(self):
        self.calls = []
        self.network_calls = []
        self.closed_calls = []
        self.responses = {}
        self.received = []
        self.providers = []
        self.before_send = None

    def get_provider(self, config):
        provider = FakeProvider(config, self)
        self.providers.append(provider)
        return provider

    def close(self):
        for provider in self.providers:
            provider.close()


@pytest.fixture
def registry():
    registry = FakeRegistry()
    try:
        yield registry
    finally:
        registry.close()


@pytest.fixture
def route_router(db, registry):
    router = ModelRouter(lambda: Database(db.path), forbidden, registry.get_provider, load_invocation_provider_config=load_config)
    try:
        yield router
    finally:
        router.close()


def make_route_request(db, router, agent, *, invocation=None, snapshot=None, stage="main", job_id="route-job", retention_policy=None):
    from pixiv_novel_sync.ai.invocation_snapshot import planned_snapshot_payload

    invocation = invocation or request_for(agent)
    snapshot = snapshot or router.resolve_invocation(agent, invocation)
    db.create_ai_job(job_id, "general", agent.id, {"source": "v2-test"}, owner_token="route-owner", stage="main", route_deadline_at="2099-01-01 00:00:00")
    assert db.set_ai_job_candidate_snapshot(job_id, "route-owner", planned_snapshot_payload(snapshot), snapshot.snapshot_hash)
    return RouteRequest(
        job_id, stage, invocation_messages(invocation), snapshot, invocation.max_tokens,
        "route-owner", lambda _: None, lambda _: None,
        temperature=invocation.temperature, top_p=invocation.top_p,
        invocation=invocation, retention_policy=retention_policy,
    )


def route_counts(db, job_id):
    row = db.conn.execute("SELECT candidate_attempt_count, network_request_count FROM ai_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return tuple(row)


def collect_result(generator):
    chunks = []
    while True:
        try:
            chunks.append(next(generator))
        except StopIteration as stopped:
            return chunks, stopped.value


def test_v2_long_request_uses_only_large_eligible_model(db, route_router, registry):
    agent, providers, _, _ = pool_setup(db, windows=(128_000, 1_000_000))
    route_router._token_estimator = lambda *_: TokenEstimate(200_000, "test-exact", True, True)
    invocation = request_for(agent, messages=[{"role": "user", "content": "x" * 200_000 + "TAIL"}])
    request = make_route_request(db, route_router, agent, invocation=invocation)
    result = route_router.execute(request)
    assert result.output_text == "SUCCESS"
    assert result.finish_state == "succeeded"
    assert registry.network_calls == [(providers[1], "model-1")]
    assert route_counts(db, request.job_id) == (1, 1)
    assert request.candidate_snapshot.plan.eligible[0].profile.source_index == 1
    assert request.candidate_snapshot.candidates[0].candidate_index == 0
    assert registry.received[0]["messages"] == invocation_messages(invocation)
    assert result.snapshot_version == 2
    assert (result.actual_provider_id, result.actual_model_key, result.finish_reason) == (providers[1], "model-1", "stop")
    assert result.job_status is None and result.usage_summary is None and result.validation_status is None


def test_v2_per_candidate_estimates_do_not_use_a_shared_maximum_minimum(db, route_router, registry):
    agent, providers, _, _ = pool_setup(db, windows=(128_000, 1_000_000))
    route_router._token_estimator = lambda _, profile: TokenEstimate(100_000 if profile.model_key == "model-0" else 200_000, "per-model", True, True)
    request = make_route_request(db, route_router, agent)
    assert len(request.candidate_snapshot.candidates) == 2
    assert route_router.execute(request).finish_state == "succeeded"
    assert registry.network_calls == [(providers[0], "model-0")]


def test_v2_no_candidate_records_plan_and_zero_attempts(db, route_router, registry, fixed_agent, monkeypatch):
    request = make_route_request(db, route_router, fixed_agent, invocation=request_for(fixed_agent, allowed_provider_ids=[]))
    monkeypatch.setattr(route_router, "_start_heartbeat", forbidden)
    result = route_router.execute(request)
    assert result.finish_state == "failed_before_output" and result.snapshot_version == 2
    assert result.actual_provider_id is None
    assert registry.calls == []
    assert route_counts(db, request.job_id) == (0, 0)
    job = db.get_ai_job(request.job_id)
    assert job["candidate_snapshot"]["version"] == 2
    assert job["candidate_snapshot"]["plan"]["excluded"][0]["reason"] == "provider_not_allowed"
    assert job["status"] == "failed"
    assert job["error_message"] == "no_eligible_candidate: No model satisfies the complete request"


@pytest.mark.parametrize("empty", [False, True])
def test_v2_expired_route_terminalizes_without_network_even_with_empty_plan(db, route_router, registry, fixed_agent, monkeypatch, empty):
    invocation = request_for(fixed_agent, **({"allowed_provider_ids": []} if empty else {}))
    request = make_route_request(db, route_router, fixed_agent, invocation=invocation)
    db.conn.execute("UPDATE ai_jobs SET route_deadline_at = '2000-01-01 00:00:00' WHERE job_id = ?", (request.job_id,))
    db.conn.commit()
    if empty:
        monkeypatch.setattr(route_router, "_start_heartbeat", forbidden)
    result = route_router.execute(request)
    assert result.finish_state == "failed_before_output"
    assert db.get_ai_job(request.job_id)["status"] == "failed"
    assert db.get_ai_job(request.job_id)["error_message"].startswith("route_budget_exhausted:")
    assert registry.calls == [] and route_counts(db, request.job_id) == (0, 0)


def test_v2_mutated_messages_fail_before_guard(db, route_router, registry, fixed_agent, monkeypatch):
    request = make_route_request(db, route_router, fixed_agent)
    request.messages[-1]["content"] += "changed"
    monkeypatch.setattr(route_router, "_start_heartbeat", forbidden)
    with pytest.raises(ModelRouteConflictError):
        route_router.execute(request)
    assert registry.calls == [] and route_counts(db, request.job_id) == (0, 0)


@pytest.mark.parametrize("change", ["max_tokens", "temperature", "top_p", "scope", "missing", "estimator", "planner"])
def test_v2_changed_parameter_or_scope_fails_before_guard(db, route_router, registry, fixed_agent, monkeypatch, change):
    invocation = request_for(fixed_agent, allowed_provider_ids=[fixed_agent.provider_id])
    request = make_route_request(db, route_router, fixed_agent, invocation=invocation)
    if change == "max_tokens":
        request.max_tokens -= 1
    elif change == "temperature":
        request.temperature = 1.8
    elif change == "top_p":
        request.top_p = 0.1
    elif change == "scope":
        request.invocation = replace(invocation, allowed_provider_ids=())
    elif change == "missing":
        request.invocation = None
    elif change == "estimator":
        route_router._estimator_version = "changed"
    else:
        request.candidate_snapshot = replace(request.candidate_snapshot, plan=replace(request.candidate_snapshot.plan, planner_version="changed"))
    monkeypatch.setattr(route_router, "_start_heartbeat", forbidden)
    with pytest.raises(ModelRouteConflictError):
        route_router.execute(request)
    assert registry.calls == [] and route_counts(db, request.job_id) == (0, 0)


@pytest.mark.parametrize("field,value", [("temperature", False), ("top_p", False), ("max_tokens", True), ("max_tokens", 1.0)])
def test_v2_equal_but_noncanonical_parameter_types_fail_before_guard(db, route_router, registry, fixed_agent, monkeypatch, field, value):
    invocation = request_for(fixed_agent, temperature=0.0, top_p=0.0, max_tokens=1)
    request = make_route_request(db, route_router, fixed_agent, invocation=invocation)
    setattr(request, field, value)
    monkeypatch.setattr(route_router, "_start_heartbeat", forbidden)
    with pytest.raises(ModelRouteConflictError):
        route_router.execute(request)
    assert registry.calls == [] and route_counts(db, request.job_id) == (0, 0)


def test_v2_rehashed_reserve_change_still_conflicts_with_the_immutable_request(db, route_router, fixed_agent, monkeypatch):
    from pixiv_novel_sync.ai.invocation_snapshot import planned_snapshot_payload

    request = make_route_request(db, route_router, fixed_agent)
    payload = planned_snapshot_payload(request.candidate_snapshot)
    payload["plan"]["output_reserve"] -= 1
    for row in payload["plan"]["eligible"]:
        row["output_reserve"] -= 1
        row["input_budget"] += 1
    request.candidate_snapshot = ModelRouter.candidate_snapshot_from_payload(payload, ModelRouter._canonical_hash(payload))
    monkeypatch.setattr(route_router, "_start_heartbeat", forbidden)
    with pytest.raises(ModelRouteConflictError):
        route_router.execute(request)
    assert route_counts(db, request.job_id) == (0, 0)


def test_v2_provider_mutation_does_not_pollute_fallback(db, route_router, registry):
    agent, providers, _, _ = pool_setup(db)
    request = make_route_request(db, route_router, agent)
    canonical = invocation_messages(request.invocation)
    registry.responses[(providers[0], "model-0")] = [
        lambda messages: messages[-1].update(content="MUTATED"),
        AIProviderError("synthetic failure", category="gateway", scope="model"),
    ]
    result = route_router.execute(request)
    assert result.output_text == "SUCCESS"
    assert registry.received[1]["messages"] == canonical
    assert request.messages == canonical and invocation_messages(request.invocation) == canonical
    assert result.actual_provider_id == providers[1]


def test_v2_runtime_does_not_reestimate_or_reload_model_limits(db, route_router, registry, monkeypatch):
    agent, _, models, _ = pool_setup(db)
    request = make_route_request(db, route_router, agent)
    db.update_ai_provider_model(models[0], {"manual_context_window": 256})
    monkeypatch.setattr(route_router, "_token_estimator", forbidden)
    monkeypatch.setattr(route_router, "estimate_messages", forbidden)
    monkeypatch.setattr(route_router, "build_prompt_budget", forbidden)
    assert route_router.execute(request).finish_state == "succeeded"
    with pytest.raises(ModelRouteConflictError):
        route_router.validate_resume_snapshot(agent, request.candidate_snapshot, 0, invocation=request.invocation)


@pytest.mark.parametrize("change", ["manual_context_window", "manual_max_output_tokens", "manual_capabilities", "enabled", "registered", "estimator", "scope", "fingerprint", "model_hash"])
def test_v2_resume_rejects_model_or_estimator_change(db, route_router, registry, fixed_agent, change):
    from pixiv_novel_sync.ai.invocation_snapshot import planned_snapshot_payload
    from pixiv_novel_sync.ai.model_router import ModelRouter

    model_id = None if change == "registered" else db.create_ai_provider_model({"provider_id": fixed_agent.provider_id, "model_key": fixed_agent.model})
    invocation = request_for(fixed_agent)
    snapshot = route_router.resolve_invocation(fixed_agent, invocation)
    if change in {"manual_context_window", "manual_max_output_tokens", "manual_capabilities", "enabled"}:
        values = {"manual_context_window": 128_000, "manual_max_output_tokens": 2000, "manual_capabilities": ["json"], "enabled": False}
        db.update_ai_provider_model(model_id, {change: values[change]})
    elif change == "registered":
        db.create_ai_provider_model({"provider_id": fixed_agent.provider_id, "model_key": fixed_agent.model})
    elif change == "estimator":
        route_router._estimator_version = "changed"
    elif change == "scope":
        invocation = replace(invocation, allowed_provider_ids=())
    else:
        payload = planned_snapshot_payload(snapshot)
        if change == "fingerprint":
            payload["plan"]["request_fingerprint"] = "e" * 64
        else:
            payload["plan"]["eligible"][0]["profile"]["model_config_hash"] = "e" * 64
        snapshot = ModelRouter.candidate_snapshot_from_payload(payload, ModelRouter._canonical_hash(payload))
    with pytest.raises(ModelRouteConflictError):
        route_router.validate_resume_snapshot(fixed_agent, snapshot, 0, invocation=invocation)
    assert registry.calls == [] and registry.providers == []


def test_v2_resume_requires_request_checks_remaining_models_without_renumbering(db, route_router):
    agent, _, models, _ = pool_setup(db)
    invocation = request_for(agent)
    snapshot = route_router.resolve_invocation(agent, invocation)
    with pytest.raises(ModelRouteConflictError):
        route_router.validate_resume_snapshot(agent, snapshot, 0)
    db.update_ai_provider_model(models[0], {"manual_context_window": 256})
    db.update_ai_provider_model(models[1], {"manual_display_name": "renamed only"})
    restored = route_router.validate_resume_snapshot(agent, snapshot, 1, invocation=invocation)
    assert restored is snapshot
    assert [candidate.candidate_index for candidate in restored.candidates] == [0, 1]
    for invalid in (True, -1, 2):
        with pytest.raises(ModelRouteConflictError):
            route_router.validate_resume_snapshot(agent, snapshot, invalid, invocation=invocation)


@pytest.mark.parametrize("change", ["agent", "provider", "pool"])
def test_v2_keeps_current_configuration_guards_for_execution_and_resume(db, route_router, registry, change):
    agent, providers, _, pool_id = pool_setup(db)
    request = make_route_request(db, route_router, agent)
    if change == "agent":
        db.conn.execute("UPDATE ai_agents SET context_window = context_window + 1 WHERE id = ?", (agent.id,))
    elif change == "provider":
        db.conn.execute("UPDATE ai_providers SET context_window = context_window + 1 WHERE id = ?", (providers[0],))
    else:
        db.conn.execute("UPDATE ai_model_pools SET version = version + 1 WHERE id = ?", (pool_id,))
    db.conn.commit()
    with pytest.raises(ModelRouteConflictError):
        route_router.execute(request)
    with pytest.raises(ModelRouteConflictError):
        route_router.validate_resume_snapshot(agent, request.candidate_snapshot, 0, invocation=request.invocation)
    assert registry.calls == [] and route_counts(db, request.job_id) == (0, 0)


def test_v2_missing_read_only_loader_never_falls_back_to_legacy_loader(db, route_router, registry, fixed_agent):
    request = make_route_request(db, route_router, fixed_agent)
    route_router._load_invocation_provider_config = None
    result = route_router.execute(request)
    assert result.finish_state == "failed_before_output"
    assert result.attempts[0]["error_category"] == "configuration"
    assert result.attempts[0]["error_scope"] == "provider"
    assert registry.calls == [] and registry.providers == []
    assert route_counts(db, request.job_id) == (1, 0)


def test_v1_resume_still_uses_legacy_hash_and_budget(db, router, fixed_agent):
    from pixiv_novel_sync.ai.model_router import CandidateSnapshot

    snapshot = router.resolve_candidates(fixed_agent)
    payload = {"agent_config_hash": snapshot.agent_config_hash, "binding_version": snapshot.binding_version, "candidates": [asdict(item) for item in snapshot.candidates]}
    restored = router.candidate_snapshot_from_payload(json.loads(json.dumps(payload)), snapshot.snapshot_hash)
    assert type(restored) is CandidateSnapshot
    assert router.validate_resume_snapshot(fixed_agent, restored, 0) == snapshot


@pytest.mark.parametrize("failure", ["provider", "unexpected", "callback"])
def test_v2_metadata_only_partial_returns_owned_memory_without_fallback(db, route_router, registry, monkeypatch, failure):
    agent, providers, _, _ = pool_setup(db)
    request = make_route_request(db, route_router, agent, retention_policy="metadata_only")
    original = Database.finish_ai_job_cas

    def retain_metadata_only(database, job_id, owner_token, status, **fields):
        won = original(database, job_id, owner_token, status, **fields)
        if won and job_id == request.job_id:
            with database.transaction() as conn:
                conn.execute("UPDATE ai_jobs SET output_text = NULL WHERE job_id = ?", (job_id,))
        return won

    monkeypatch.setattr(Database, "finish_ai_job_cas", retain_metadata_only)
    error = (AIProviderError("synthetic failure", category="gateway", scope="provider")
             if failure == "provider" else RuntimeError("PRIVATE-ERROR"))

    def progress(data):
        if data.get("phase") == "provider":
            raise error

    request.on_progress = progress
    event = AIStreamChunk(type="progress", data={"phase": "provider"}) if failure == "callback" else error
    registry.responses[(providers[0], "model-0")] = [AIStreamChunk(type="delta", text="PRIVATE-PARTIAL"), event]
    chunks, result = collect_result(route_router.execute_stream(request))
    assert result.output_text == "PRIVATE-PARTIAL" and result.finish_state == "partial"
    job = db.get_ai_job(request.job_id)
    assert job["status"] == "partial" and job["output_text"] in (None, "")
    assert [chunk.text for chunk in chunks if chunk.type == "delta"] == ["PRIVATE-PARTIAL"]
    assert [attempt["status"] for attempt in result.attempts] == ["partial"]
    assert result.attempts[0]["output_started"] is True
    assert result.attempts[0]["finish_reason"] == "error"
    assert registry.calls == [(providers[0], "model-0")]
    assert registry.closed_calls == registry.calls
    assert route_counts(db, request.job_id) == (1, 1)
    assert (result.actual_provider_id, result.actual_model_key) == (providers[0], "model-0")
    assert job["pinned_candidate_index"] == 0
    if failure != "provider":
        assert result.attempts[0]["error_category"] == "unexpected_error"
        assert result.attempts[0]["error_message"] == "Unexpected invocation execution error"
        assert "PRIVATE-ERROR" not in json.dumps(job)


@pytest.mark.parametrize("failure", ["provider", "unexpected"])
@pytest.mark.parametrize("race", ["finished", "deleted", "owner_changed"])
def test_v2_lost_terminal_cas_never_returns_discarded_memory(db, route_router, registry, monkeypatch, failure, race):
    agent, providers, _, _ = pool_setup(db)
    request = make_route_request(db, route_router, agent, retention_policy="metadata_only")
    original = Database.finish_ai_job_cas

    def competing_finish(database, job_id, owner_token, status, **fields):
        if race == "finished":
            assert original(database, job_id, owner_token, "cancelled", output_text="WINNER")
        elif race == "deleted":
            database.delete_ai_job(job_id)
        else:
            with database.transaction() as conn:
                conn.execute("UPDATE ai_jobs SET owner_token = 'new-owner', output_text = NULL WHERE job_id = ?", (job_id,))
        return original(database, job_id, owner_token, status, **fields)

    monkeypatch.setattr(Database, "finish_ai_job_cas", competing_finish)
    error = (AIProviderError("synthetic failure", category="gateway", scope="model")
             if failure == "provider" else RuntimeError("PRIVATE-ERROR"))
    registry.responses[(providers[0], "model-0")] = [AIStreamChunk(type="delta", text="DISCARDED"), error]
    result = route_router.execute(request)
    assert result.output_text == ("WINNER" if race == "finished" else "")
    assert result.finish_state == ("cancelled" if race == "finished" else "failed_before_output")
    assert registry.calls == [(providers[0], "model-0")]
    assert registry.closed_calls == registry.calls


@pytest.mark.parametrize("failure", ["provider", "provider_progress_callback", "route_progress_callback", "cancellation_callback"])
def test_v2_unexpected_error_before_body_fails_closed(db, route_router, registry, failure):
    agent, providers, _, _ = pool_setup(db)
    request = make_route_request(db, route_router, agent, retention_policy="metadata_only")
    error = RuntimeError("PRIVATE-ERROR")
    deltas = []
    request.on_delta = deltas.append

    def progress(data):
        if (failure == "route_progress_callback"
                or failure == "provider_progress_callback" and data.get("phase") == "provider"):
            raise error

    def cancelled():
        if registry.network_calls:
            raise error
        return False

    request.on_progress = progress
    if failure == "cancellation_callback":
        request.is_cancelled = cancelled
    registry.responses[(providers[0], "model-0")] = (
        [error] if failure == "provider" else
        [AIStreamChunk(type="progress", data={"phase": "provider"}), AIStreamChunk(type="delta", text="UNPUBLISHED")]
    )
    chunks, result = collect_result(route_router.execute_stream(request))
    assert result.finish_state == "failed_before_output" and result.output_text == ""
    assert deltas == [] and not any(chunk.type == "delta" for chunk in chunks)
    job = db.get_ai_job(request.job_id)
    assert job["status"] == "failed" and job["output_text"] in (None, "")
    assert job["pinned_candidate_index"] is None
    assert job["error_message"] == "unexpected_error: Unexpected invocation execution error"
    count = 0 if failure == "route_progress_callback" else 1
    assert registry.calls == [(providers[0], "model-0")] * count
    assert registry.closed_calls == registry.calls
    assert route_counts(db, request.job_id) == (count, count)
    assert [attempt["status"] for attempt in result.attempts] == ["failed"] * count
    for attempt in result.attempts:
        assert attempt["error_category"] == "unexpected_error"
        assert attempt["error_message"] == "Unexpected invocation execution error"
        assert attempt["finish_reason"] == "error" and attempt["output_started"] is False


@pytest.mark.parametrize("stage", ["internal", "validation"])
def test_v2_unexpected_stage_error_discards_buffered_fragments(db, route_router, registry, stage):
    agent, providers, _, _ = pool_setup(db)
    request = make_route_request(db, route_router, agent, stage=stage, retention_policy="metadata_only")
    deltas = []
    request.on_delta = deltas.append
    registry.responses[(providers[0], "model-0")] = [AIStreamChunk(type="delta", text="DISCARDED"), RuntimeError("PRIVATE-ERROR")]
    chunks, result = collect_result(route_router.execute_stream(request))
    assert result.finish_state == "failed_before_output" and result.output_text == ""
    assert deltas == [] and not any(chunk.type == "delta" for chunk in chunks)
    assert [attempt["status"] for attempt in result.attempts] == ["failed"]
    assert result.attempts[0]["output_started"] is False
    assert result.attempts[0]["error_category"] == "unexpected_error"
    assert result.attempts[0]["error_message"] == "Unexpected invocation execution error"
    assert result.actual_provider_id is None and result.actual_model_key is None
    job = db.get_ai_job(request.job_id)
    assert job["status"] == ("running" if stage == "internal" else "failed")
    assert job["pinned_candidate_index"] is None and job["output_text"] in (None, "")
    assert registry.calls == [(providers[0], "model-0")]
    assert registry.closed_calls == registry.calls
    assert route_counts(db, request.job_id) == (1, 1)


def test_v2_late_request_validation_conflict_stays_typed(db, route_router, registry, fixed_agent):
    request = make_route_request(db, route_router, fixed_agent)
    stream = route_router.execute_stream(request)
    assert next(stream).type == "progress"
    request.messages[-1]["content"] += "changed"
    with pytest.raises(ModelRouteConflictError):
        next(stream)
    assert registry.calls == [] and route_counts(db, request.job_id) == (1, 0)
    job = db.get_ai_job(request.job_id)
    assert job["status"] == "running" and job["attempts"][0]["status"] == "running"


@pytest.mark.parametrize("failure", ["cancelled", "budget"])
def test_v2_owned_memory_survives_cancellation_and_budget_terminal_paths(db, route_router, registry, failure):
    from pixiv_novel_sync.storage.ai.core import AIRouteBudgetExhausted

    agent, providers, _, _ = pool_setup(db)
    request = make_route_request(db, route_router, agent, retention_policy="metadata_only")
    error = (AIProviderError("cancel", category="cancelled", scope="model", finish_reason="cancelled")
             if failure == "cancelled" else AIRouteBudgetExhausted("limit"))
    registry.responses[(providers[0], "model-0")] = [AIStreamChunk(type="delta", text="PRIVATE-PARTIAL"), error]
    result = route_router.execute(request)
    assert result.output_text == "PRIVATE-PARTIAL"
    assert result.finish_state == ("cancelled" if failure == "cancelled" else "partial")
    assert registry.calls == [(providers[0], "model-0")]


@pytest.mark.parametrize("stage", ["internal", "validation"])
def test_v2_internal_and_validation_discard_failed_candidate_fragments(db, route_router, registry, stage):
    agent, providers, _, _ = pool_setup(db)
    request = make_route_request(db, route_router, agent, stage=stage, retention_policy="metadata_only")
    registry.responses[(providers[0], "model-0")] = [AIStreamChunk(type="delta", text="DISCARDED"), AIProviderError("synthetic failure", category="gateway", scope="model")]
    chunks, result = collect_result(route_router.execute_stream(request))
    assert result.output_text == "SUCCESS" and result.finish_state == "succeeded"
    assert [chunk.text for chunk in chunks if chunk.type == "delta"] == ["SUCCESS"]
    assert [attempt["status"] for attempt in result.attempts] == ["failed", "succeeded"]


def test_v2_generator_close_cancels_without_an_extra_result(db, route_router, registry):
    agent, providers, _, _ = pool_setup(db)
    request = make_route_request(db, route_router, agent, retention_policy="metadata_only")
    stream = route_router.execute_stream(request)
    assert next(stream).type == "progress"
    assert next(stream).type == "delta"
    stream.close()
    assert db.get_ai_job(request.job_id)["status"] == "cancelled"
    assert registry.closed_calls == [(providers[0], "model-0")]


@pytest.fixture
def service(db, registry, monkeypatch):
    from pixiv_novel_sync.ai import service as service_facade

    monkeypatch.setattr(service_facade, "create_provider", registry.get_provider)
    value = service_facade.AIWritingService(db.path)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def legacy_key_provider(db, monkeypatch):
    from pixiv_novel_sync.ai.crypto import AISecretManager

    monkeypatch.setenv("PIXIV_NOVEL_SYNC_AI_SECRET_KEY", "synthetic-master")
    manager = AISecretManager()
    ciphertext = manager._fernet_v1().encrypt(b"synthetic-provider-key").decode("ascii")
    provider_id = seed_provider(db, "legacy-test", api_key_encrypted=ciphertext)
    agent = seed_agent(db, provider_id)
    return manager, provider_id, agent.id, ciphertext


def start_context(db, service, agent, invocation):
    return service._start_route_job(db, "general", agent, {"source": "synthetic-audit"},
                                    messages=invocation_messages(invocation), max_tokens=invocation.max_tokens,
                                    invocation=invocation, retention_policy="metadata_only")


def test_v2_credential_upgrade_precedes_frozen_hash(db, service, registry, legacy_key_provider):
    from pixiv_novel_sync.ai.model_sync import provider_model_sync_config_hash

    _, provider_id, agent_id, ciphertext = legacy_key_provider
    agent = agent_from_row(db, agent_id)
    invocation = request_for(agent, temperature=0.0, top_p=0.0)
    assert service.model_router.plan_invocation(agent, invocation).can_run
    assert db.get_ai_provider(provider_id, include_secret=True)["api_key_encrypted"] == ciphertext
    context = start_context(db, service, agent, invocation)
    frozen_hash = context.candidate_snapshot.candidates[0].provider_config_hash
    assert registry.calls == []
    assert db.get_ai_job(context.job_id)["candidate_snapshot"]["version"] == 2

    def verify_first_send(config):
        row = db.get_ai_provider(provider_id, include_secret=True)
        assert row["api_key_encrypted"].startswith("v2$")
        assert config.api_key == "synthetic-provider-key"
        assert provider_model_sync_config_hash(row) == frozen_hash

    registry.before_send = verify_first_send
    _, result = collect_result(service._stream_route(context, invocation_messages(invocation)))
    assert result.finish_state == "succeeded"
    assert (registry.received[0]["temperature"], registry.received[0]["top_p"]) == (0.0, 0.0)
    assert service._finish_route_job(db, context, "succeeded", result.output_text)
    assert provider_model_sync_config_hash(db.get_ai_provider(provider_id, include_secret=True)) == frozen_hash


def test_v2_credential_upgrade_failure_does_not_retry_after_freeze(db, service, registry, legacy_key_provider, monkeypatch):
    from pixiv_novel_sync.ai.model_sync import provider_model_sync_config_hash

    _, provider_id, agent_id, ciphertext = legacy_key_provider
    writes = []

    def failed_upgrade(database, provider_id, expected, replacement):
        writes.append(provider_id)
        raise OSError("synthetic storage write failed")

    monkeypatch.setattr(Database, "upgrade_ai_provider_ciphertext", failed_upgrade, raising=False)
    agent = agent_from_row(db, agent_id)
    invocation = request_for(agent)
    context = start_context(db, service, agent, invocation)
    assert writes == [provider_id]
    frozen_hash = context.candidate_snapshot.candidates[0].provider_config_hash
    _, result = collect_result(service._stream_route(context, invocation_messages(invocation)))
    assert result.finish_state == "succeeded" and writes == [provider_id]
    row = db.get_ai_provider(provider_id, include_secret=True)
    assert row["api_key_encrypted"] == ciphertext
    assert provider_model_sync_config_hash(row) == frozen_hash
    assert route_counts(db, context.job_id) == (1, 1)


def test_v2_credential_failure_allows_scoped_fallback(db, service, registry, legacy_key_provider, monkeypatch):
    from pixiv_novel_sync.ai.crypto import AISecretManager

    manager, _, _, _ = legacy_key_provider
    monkeypatch.setenv("SYNTHETIC_OTHER_MASTER", "other-synthetic-master")
    other = AISecretManager(env_name="SYNTHETIC_OTHER_MASTER")
    wrong = other._fernet_v1().encrypt(b"wrong-master-provider").decode("ascii")
    valid = manager._fernet_v1().encrypt(b"valid-synthetic-provider").decode("ascii")
    excluded = manager._fernet_v1().encrypt(b"excluded-synthetic-provider").decode("ascii")
    agent, providers, _, _ = pool_setup(db, windows=(64_000, 64_000, 64_000))
    for provider_id, ciphertext in zip(providers, (wrong, valid, excluded)):
        db.update_ai_provider(provider_id, {"api_key_encrypted": ciphertext})
    decrypts = []
    original = AISecretManager.decrypt

    def record_decrypt(self, ciphertext):
        decrypts.append(ciphertext)
        return original(self, ciphertext)

    monkeypatch.setattr(AISecretManager, "decrypt", record_decrypt)
    invocation = request_for(agent, allowed_provider_ids=providers[:2])
    assert service.model_router.plan_invocation(agent, invocation).can_run
    assert decrypts == []
    context = start_context(db, service, agent, invocation)
    _, result = collect_result(service._stream_route(context, invocation_messages(invocation)))
    assert result.output_text == "SUCCESS"
    assert registry.calls == [(providers[1], "model-1")]
    assert registry.network_calls == [(providers[1], "model-1")]
    assert result.attempts[0]["error_category"] == "configuration"
    assert result.attempts[0]["error_scope"] == "provider"
    assert result.attempts[0]["error_message"] == "configuration"
    assert [item["status"] for item in result.attempts] == ["failed", "succeeded"]
    assert excluded not in decrypts
    assert db.get_ai_provider(providers[2], include_secret=True)["api_key_encrypted"] == excluded
    assert route_counts(db, context.job_id) == (2, 1)


def test_credential_ciphertext_cas_preserves_concurrent_replacement(db, legacy_key_provider):
    manager, provider_id, _, legacy = legacy_key_provider
    fresh = manager.encrypt("new-synthetic-user-key")
    stale_upgrade = manager.encrypt("synthetic-provider-key")
    db.update_ai_provider(provider_id, {"api_key_encrypted": fresh})
    assert db.upgrade_ai_provider_ciphertext(provider_id, legacy, stale_upgrade) is False
    assert db.get_ai_provider(provider_id, include_secret=True)["api_key_encrypted"] == fresh


def test_v2_credential_racing_upgrade_refreezes_user_key_not_stale_decrypted_key(db, service, registry, legacy_key_provider, monkeypatch):
    from pixiv_novel_sync.ai.model_sync import provider_model_sync_config_hash

    manager, provider_id, agent_id, legacy = legacy_key_provider
    replacement = manager.encrypt("new-synthetic-user-key")
    original = Database.upgrade_ai_provider_ciphertext
    outcomes = []

    def racing_upgrade(database, current_provider_id, expected, ciphertext):
        assert expected == legacy
        with Database(db.path) as other:
            other.update_ai_provider(current_provider_id, {"api_key_encrypted": replacement})
        won = original(database, current_provider_id, expected, ciphertext)
        outcomes.append(won)
        return won

    monkeypatch.setattr(Database, "upgrade_ai_provider_ciphertext", racing_upgrade)
    agent = agent_from_row(db, agent_id)
    invocation = request_for(agent)
    context = start_context(db, service, agent, invocation)
    _, result = collect_result(service._stream_route(context, invocation_messages(invocation)))
    assert outcomes == [False]
    assert registry.providers[0].config.api_key == "new-synthetic-user-key"
    row = db.get_ai_provider(provider_id, include_secret=True)
    assert row["api_key_encrypted"] == replacement
    assert provider_model_sync_config_hash(row) == context.candidate_snapshot.candidates[0].provider_config_hash
    assert result.finish_state == "succeeded"


def test_v2_empty_core_scope_persists_plan_without_credentials_or_send(db, service, registry, legacy_key_provider, monkeypatch):
    _, provider_id, agent_id, legacy = legacy_key_provider
    monkeypatch.setattr(service, "_load_provider_config", forbidden)
    agent = agent_from_row(db, agent_id)
    invocation = request_for(agent, allowed_provider_ids=[])
    context = start_context(db, service, agent, invocation)
    job = db.get_ai_job(context.job_id)
    assert job["candidate_snapshot"]["version"] == 2 and not job["prompt_budget"]["eligible"]
    _, result = collect_result(service._stream_route(context, invocation_messages(invocation)))
    assert result.finish_state == "failed_before_output"
    assert route_counts(db, context.job_id) == (0, 0) and registry.calls == []
    assert db.get_ai_provider(provider_id, include_secret=True)["api_key_encrypted"] == legacy


def test_v2_core_low_level_resume_reuses_frozen_plan_without_resolution_or_shared_budget(db, service, registry, monkeypatch):
    from pixiv_novel_sync.ai.invocation_snapshot import planned_snapshot_payload
    from pixiv_novel_sync.ai.model_router import RouteResumeSpec
    from pixiv_novel_sync.ai.services.core import _ROUTE_RESUME_CONTEXT

    agent, providers, _, _ = pool_setup(db)
    for provider_id in providers:
        db.update_ai_provider(provider_id, {"api_key_encrypted": None})
    invocation = request_for(agent)
    parent = start_context(db, service, agent, invocation)
    assert db.finish_ai_job_cas(parent.job_id, parent.owner_token, "failed")
    snapshot = parent.candidate_snapshot
    child, created = db.create_or_get_ai_resume_job(
        "child", "general", agent.id, {"resume_candidate_index": 1}, owner_token="child-owner",
        route_deadline_at="2099-01-01 00:00:00", parent_job_id=parent.job_id,
        idempotency_key="resume-test", candidate_snapshot=planned_snapshot_payload(snapshot),
        candidate_snapshot_hash=snapshot.snapshot_hash, resume_candidate_index=1,
    )
    assert created
    monkeypatch.setattr(service.model_router, "resolve_invocation", forbidden)
    monkeypatch.setattr(service.model_router, "build_prompt_budget", forbidden)
    token = _ROUTE_RESUME_CONTEXT.set(RouteResumeSpec(parent.job_id, "resume-test", snapshot.snapshot_hash, 1))
    try:
        context = start_context(db, service, agent, invocation)
    finally:
        _ROUTE_RESUME_CONTEXT.reset(token)
    assert context.job_id == child["job_id"]
    assert context.prompt_budget == snapshot.plan
    assert context.resume_candidate_index == 1
    assert [item.candidate_index for item in context.candidate_snapshot.candidates] == [0, 1]
    _, result = collect_result(service._stream_route(context, invocation_messages(invocation)))
    assert result.output_text == "SUCCESS" and registry.calls == [(providers[1], "model-1")]

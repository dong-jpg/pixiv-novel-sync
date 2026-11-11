# Task 1C: Pure Configuration Snapshot

Part of the [implementation plan](../2026-11-05-work-xp-analysis.md) based on the approved specification.
This is planned code, not authorization to implement. Complete Task1A/1B first;
Task3 must never import an unimplemented Task4 prerequisite.

**Files:** Create `src/pixiv_novel_sync/work_analysis/configuration.py` and
`tests/test_work_analysis_configuration.py`.

**Interfaces:** `snapshot_work_configuration(conn, agent_id, output_tokens)` returns
the safe configuration, saved Agent prompt in memory, and pure full-payload
preflight closure. `work_configuration_identity` returns only the safe
configuration. `work_can_segment` and `work_targets` are private work-layer
projections, not extensions to the global public v2 contract.

- [ ] **1C.1 Add these complete failing tests.**

```python
import json

import pytest

from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.work_analysis.configuration import snapshot_work_configuration, work_configuration_identity
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError


@pytest.fixture
def configured_db(tmp_path):
    db = Database(tmp_path / "configuration.db")
    db.init_schema()
    provider = db.create_ai_provider({
        "name": "synthetic", "provider_type": "openai_compatible", "enabled": True,
        "base_url": "https://provider.invalid/v1", "api_key_encrypted": "legacy-synthetic-cipher",
        "default_model": "synthetic-model", "context_window": 100000,
    })
    agent = db.create_ai_agent({
        "name": "synthetic", "task_type": "general", "provider_id": provider,
        "model": "synthetic-model", "system_prompt": "PRIVATE_AGENT_PROMPT", "context_window": 100000,
    })
    try:
        yield db, provider, agent
    finally:
        db.close()


def test_snapshot_is_read_only_and_prompt_changes_invalidate_identity(configured_db, monkeypatch):
    from pixiv_novel_sync.ai.crypto import AISecretManager

    db, provider, agent = configured_db

    def forbidden(*args, **kwargs):
        raise AssertionError("secret access forbidden")

    monkeypatch.setattr(AISecretManager, "decrypt", forbidden)
    monkeypatch.setattr(AISecretManager, "encrypt", forbidden)
    changes = db.conn.total_changes
    with db.read_transaction():
        identity, prompt, preflight = snapshot_work_configuration(db.conn, agent, 100)
    assert db.conn.total_changes == changes
    assert identity["legacy_provider_ids"] == [provider]
    assert prompt == "PRIVATE_AGENT_PROMPT"
    serialized = json.dumps(identity)
    assert "PRIVATE_AGENT_PROMPT" not in serialized and "legacy-synthetic-cipher" not in serialized
    assert "provider.invalid" not in serialized
    assert preflight({"messages": [{"role": "system", "content": prompt},
                                   {"role": "user", "content": "synthetic body"}], "max_tokens": 100})["can_run"]
    assert db.conn.total_changes == changes
    db.update_ai_agent(agent, {"system_prompt": "CHANGED_AGENT_PROMPT"})
    assert work_configuration_identity(db.conn, agent, 100)["configuration_fingerprint"] != identity["configuration_fingerprint"]


def test_missing_key_is_not_hidden_as_a_capacity_problem(configured_db):
    db, provider, agent = configured_db
    db.update_ai_provider(provider, {"api_key_encrypted": ""})
    with pytest.raises(WorkAnalysisError) as caught:
        snapshot_work_configuration(db.conn, agent, 100)
    assert caught.value.code == "configuration_unavailable"


def test_returned_scope_mutation_cannot_change_preflight_after_database_close(configured_db):
    db, provider, agent = configured_db
    configuration, prompt, preflight = snapshot_work_configuration(db.conn, agent, 100)
    payload = {"messages": [{"role": "user", "content": "synthetic body"}], "max_tokens": 100}
    original = preflight(payload)
    assert original["can_run"] is True
    original_fingerprint = configuration["configuration_fingerprint"]
    assert configuration["allowed_provider_ids"] == [provider]
    db.close()

    configuration["allowed_provider_ids"].clear()
    for _ in range(2):
        assert preflight(payload) == original
        assert preflight({**payload, "allowed_provider_ids": [provider]}) == original
    assert configuration["configuration_fingerprint"] == original_fingerprint
    for scope in ([], [provider + 1]):
        with pytest.raises(WorkAnalysisError) as caught:
            preflight({**payload, "allowed_provider_ids": scope})
        assert caught.value.code == "configuration_changed"


@pytest.mark.parametrize("output_tokens, content", [(1000, ""), (740, "x" * 1000)],
                         ids=["output_fills_context", "framing_exceeds_context"])
def test_fixed_capacity_failure_does_not_authorize_segmentation(configured_db, output_tokens, content):
    db, provider, agent = configured_db
    db.update_ai_provider(provider, {"context_window": 1000})
    configuration, prompt, preflight = snapshot_work_configuration(db.conn, agent, output_tokens)
    result = preflight({"messages": [{"role": "user", "content": content}], "max_tokens": output_tokens})
    assert result["can_run"] is False
    assert result["excluded"][0]["reason"] == "context_overflow"
    assert result["excluded"][0]["input_budget"] < 0
    assert result["work_can_segment"] is False


@pytest.mark.parametrize("provider_type, output_tokens, can_segment", [
    ("openai_compatible", 730, True),
    ("openai_compatible", 731, False),
    ("anthropic", 733, True),
    ("anthropic", 734, False),
])
def test_segmentation_floor_uses_candidate_message_framing(
    configured_db, monkeypatch, provider_type, output_tokens, can_segment,
):
    from dataclasses import replace
    from pixiv_novel_sync.work_analysis import configuration as module

    db, provider, agent = configured_db
    original = module.collect_candidate_profiles
    monkeypatch.setattr(module, "collect_candidate_profiles", lambda reader, current_agent: (
        replace(original(reader, current_agent)[0], provider_type=provider_type, provider_context_window=1000),
    ))
    configuration, prompt, preflight = module.snapshot_work_configuration(db.conn, agent, output_tokens)
    messages = [{"role": role, "content": "x" * 1000} for role in ("system", "system", "user")]
    result = preflight({"messages": messages, "max_tokens": output_tokens})
    assert result["can_run"] is False
    assert result["excluded"][0]["reason"] == "context_overflow"
    assert result["work_can_segment"] is can_segment
    # Anthropic merges system messages, retaining the separator even for empty content.
    minimum = preflight({"messages": [{**message, "content": ""} for message in messages],
                         "max_tokens": output_tokens})
    assert minimum["can_run"] is can_segment
    assert minimum["work_can_segment"] is False


def test_segmentation_does_not_combine_context_and_output_limits_across_candidates(configured_db, monkeypatch):
    from dataclasses import replace
    from pixiv_novel_sync.work_analysis import configuration as module

    db, provider, agent = configured_db
    original = module.collect_candidate_profiles

    def profiles(reader, current_agent):
        base = original(reader, current_agent)[0]
        return (replace(base, provider_context_window=1000),
                replace(base, source_index=1, model_key="output-limited", max_output_tokens=16))

    monkeypatch.setattr(module, "collect_candidate_profiles", profiles)
    configuration, prompt, preflight = module.snapshot_work_configuration(db.conn, agent, 1000)
    result = preflight({"messages": [{"role": "user", "content": "x" * 1000}], "max_tokens": 1000})
    assert result["can_run"] is False
    assert [item["reason"] for item in result["excluded"]] == ["context_overflow", "output_limit"]
    assert result["work_can_segment"] is False


@pytest.mark.parametrize("output_tokens, can_segment", [(100, True), (512, False)])
def test_private_capacity_gate_survives_public_exclusion_truncation(
    configured_db, monkeypatch, output_tokens, can_segment,
):
    from dataclasses import replace
    from pixiv_novel_sync.work_analysis import configuration as module

    db, provider, agent = configured_db
    original = module.collect_candidate_profiles

    def profiles(reader, current_agent):
        base = original(reader, current_agent)[0]
        excluded = [replace(base, source_index=index, unavailable_reason="member_disabled")
                    for index in range(64)]
        return tuple(excluded + [replace(base, source_index=64, provider_context_window=512)])

    monkeypatch.setattr(module, "collect_candidate_profiles", profiles)
    configuration, prompt, preflight = module.snapshot_work_configuration(db.conn, agent, output_tokens)
    result = preflight({"messages": [{"role": "user", "content": "x" * 5000}], "max_tokens": output_tokens})
    assert result["can_run"] is False
    assert len(result["excluded"]) == 64 and result["excluded_count"] == 65
    assert all(item["reason"] == "member_disabled" for item in result["excluded"])
    assert result["work_can_segment"] is can_segment


def test_output_only_capacity_failure_does_not_authorize_segmentation(configured_db, monkeypatch):
    from dataclasses import replace
    from pixiv_novel_sync.work_analysis import configuration as module

    db, provider, agent = configured_db
    original = module.collect_candidate_profiles
    monkeypatch.setattr(module, "collect_candidate_profiles", lambda reader, current_agent: (
        replace(original(reader, current_agent)[0], max_output_tokens=16),
    ))
    configuration, prompt, preflight = module.snapshot_work_configuration(db.conn, agent, 100)
    result = preflight({"messages": [{"role": "user", "content": "short"}], "max_tokens": 100})
    assert result["can_run"] is False and result["work_can_segment"] is False
    assert result["excluded"][0]["reason"] == "output_limit"


def test_whole_input_margin_does_not_reject_a_candidate_that_fits_smaller_inputs(configured_db):
    db, provider, agent = configured_db
    db.update_ai_provider(provider, {"context_window": 1000})
    configuration, prompt, preflight = snapshot_work_configuration(db.conn, agent, 100)
    whole = preflight({"messages": [{"role": "user", "content": "x" * 100000}], "max_tokens": 100})
    assert whole["can_run"] is False
    assert whole["excluded"][0]["reason"] == "context_overflow"
    assert whole["excluded"][0]["input_budget"] < 0
    assert whole["work_can_segment"] is True
    smaller = preflight({"messages": [{"role": "user", "content": "short synthetic input"}], "max_tokens": 100})
    assert smaller["can_run"] is True


def test_capacity_excluded_future_reducer_target_is_disclosed_from_initial_capture(configured_db, monkeypatch):
    from dataclasses import replace
    from pixiv_novel_sync.work_analysis import configuration as module

    db, provider, agent = configured_db
    second_provider = db.create_ai_provider({
        "name": "Small reducer provider", "provider_type": "openai_compatible", "enabled": True,
        "base_url": "https://small.invalid/v1", "api_key_encrypted": "v2$synthetic-only",
        "default_model": "small-exact-key", "context_window": 1000,
    })
    original = module.collect_candidate_profiles

    def two_profiles(reader, current_agent):
        large = original(reader, current_agent)[0]
        small = replace(large, source_index=1, provider_id=second_provider,
                        provider_name="Small reducer provider", model_key="small-exact-key",
                        provider_context_window=1000)
        return large, small

    monkeypatch.setattr(module, "collect_candidate_profiles", two_profiles)
    configuration, prompt, preflight = module.snapshot_work_configuration(db.conn, agent, 100)
    large = preflight({"messages": [{"role": "user", "content": "x" * 20000}], "max_tokens": 100})
    assert {row["provider_id"] for row in large["eligible"]} == {provider}
    assert {row["provider_id"] for row in large["work_targets"]} == {provider, second_provider}
    assert any(row["provider_id"] == second_provider and row["reason"] == "context_overflow"
               for row in large["excluded"])
    small = preflight({"messages": [{"role": "user", "content": "short validated reducer input"}], "max_tokens": 100})
    assert {row["provider_id"] for row in small["eligible"]} == {provider, second_provider}
    assert small["work_targets"] == large["work_targets"]
    assert all(set(row) == {"provider_id", "provider_name", "provider_model_id", "model_key", "display_name",
                            "pool_id", "pool_name", "pool_position", "fallback_depth"}
               for row in small["work_targets"])
    large["work_targets"][0]["provider_name"] = "caller mutation"
    repeated = preflight({"messages": [{"role": "user", "content": "short"}], "max_tokens": 100})
    assert repeated["work_targets"][0]["provider_name"] != "caller mutation"
```

- [ ] **1C.2 Run RED.** `python -m pytest tests/test_work_analysis_configuration.py -q`.
Expected: missing new module. Do not decrypt credentials or invoke a Provider.

- [ ] **1C.3 Add the complete configuration implementation.**

```python
from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import asdict, fields, replace
from types import MappingProxyType
from typing import Any

from ..ai.invocation import InvocationValidationError, normalize_text_invocation
from ..ai.invocation_candidates import collect_candidate_profiles
from ..ai.invocation_planner import plan_invocation
from ..ai.models import AIAgentConfig
from ..storage.ai.catalog import CatalogMixin
from .contracts import WorkAnalysisError, fingerprint


class _ConfigurationReader(CatalogMixin):
    def __init__(self, conn):
        self.conn = conn

    def read_transaction(self):
        return nullcontext(self.conn)

    def get_ai_provider(self, provider_id, include_secret=False):
        row = self.conn.execute("SELECT * FROM ai_providers WHERE id=?", (provider_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["stream_enabled"] = bool(result["stream_enabled"])
        if not include_secret:
            result.pop("api_key_encrypted", None)
        return result

    def get_ai_model_pool(self, pool_id):
        row = self.conn.execute("SELECT * FROM ai_model_pools WHERE id=?", (pool_id,)).fetchone()
        return dict(row) if row is not None else None


def snapshot_work_configuration(conn, agent_id: int, output_tokens: int):
    if type(agent_id) is not int or agent_id <= 0:
        raise WorkAnalysisError("invalid_agent", 400)
    if type(output_tokens) is not int or not 1 <= output_tokens <= 1_000_000:
        raise WorkAnalysisError("invalid_output_tokens", 400)
    row = conn.execute("SELECT * FROM ai_agents WHERE id=?", (agent_id,)).fetchone()
    if row is None:
        raise WorkAnalysisError("configuration_unavailable", 409)
    raw = dict(row)
    try:
        capabilities = json.loads(raw["required_capabilities_json"])
        if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
            raise ValueError("invalid capabilities")
        raw["required_capabilities"] = tuple(capabilities)
        raw["enabled"] = bool(raw["enabled"])
        agent = AIAgentConfig(**{field.name: raw[field.name] for field in fields(AIAgentConfig)})
        reader = _ConfigurationReader(conn)
        profiles = collect_candidate_profiles(reader, agent)
    except (KeyError, TypeError, ValueError, InvocationValidationError):
        raise WorkAnalysisError("configuration_unavailable", 409) from None
    if not profiles:
        raise WorkAnalysisError("configuration_unavailable", 409)
    provider_ids = tuple(sorted({profile.provider_id for profile in profiles}))
    legacy_provider_ids = []
    for provider_id in provider_ids:
        provider = reader.get_ai_provider(provider_id, include_secret=True)
        ciphertext = provider.get("api_key_encrypted") if provider else None
        relevant = any(
            profile.provider_id == provider_id and profile.unavailable_reason is None
            and not set(agent.required_capabilities).difference(profile.capabilities)
            for profile in profiles
        )
        if relevant and (not isinstance(ciphertext, str) or not ciphertext.strip()):
            raise WorkAnalysisError("configuration_unavailable", 409)
        if isinstance(ciphertext, str) and ciphertext and not ciphertext.startswith("v2$"):
            legacy_provider_ids.append(provider_id)
    pools = []
    pool_id = agent.model_pool_id if agent.binding_type == "pool" else None
    seen = set()
    while pool_id is not None:
        if pool_id in seen or len(seen) >= 8:
            raise WorkAnalysisError("configuration_unavailable", 409)
        seen.add(pool_id)
        pool = reader.get_ai_model_pool(pool_id)
        if pool is None:
            raise WorkAnalysisError("configuration_unavailable", 409)
        members = [dict(item) for item in conn.execute(
            "SELECT provider_model_id,position,enabled FROM ai_model_pool_members "
            "WHERE pool_id=? ORDER BY position LIMIT 65", (pool_id,),
        ).fetchall()]
        if len(members) > 64:
            raise WorkAnalysisError("configuration_unavailable", 409)
        pools.append({
            "id": pool_id, "version": pool["version"], "enabled": bool(pool["enabled"]),
            "fallback_pool_id": pool["fallback_pool_id"], "members": members,
        })
        pool_id = pool["fallback_pool_id"]
    agent_identity = asdict(agent)
    agent_prompt = agent_identity.pop("system_prompt")
    if not isinstance(agent_prompt, str):
        raise WorkAnalysisError("configuration_unavailable", 409)
    agent_identity["system_prompt_hash"] = fingerprint(agent_prompt)
    identity = {
        "schema_version": 1, "agent_id": agent_id, "output_tokens": output_tokens,
        "agent": agent_identity, "profiles": [asdict(profile) for profile in profiles], "pools": pools,
    }
    configuration = {
        "schema_version": 1, "agent_id": agent_id, "output_tokens": output_tokens,
        "configuration_fingerprint": fingerprint(identity),
        "allowed_provider_ids": list(provider_ids), "legacy_provider_ids": legacy_provider_ids,
    }
    target_rows = []
    target_keys = set()
    for profile in profiles:
        if (profile.unavailable_reason is not None
                or set(agent.required_capabilities).difference(profile.capabilities)):
            continue
        key = (profile.provider_id, profile.model_key)
        if key in target_keys:
            continue
        target_keys.add(key)
        if len(target_keys) > 64:
            raise WorkAnalysisError("configuration_unavailable", 409)
        target_rows.append(MappingProxyType({
            "provider_id": profile.provider_id, "provider_name": profile.provider_name,
            "provider_model_id": profile.provider_model_id, "model_key": profile.model_key,
            "display_name": profile.display_name, "pool_id": profile.pool_id,
            "pool_name": profile.pool_name, "pool_position": profile.pool_position,
            "fallback_depth": profile.fallback_depth,
        }))
    work_targets = tuple(target_rows)

    def preflight(payload: Mapping[str, Any]):
        if not isinstance(payload, Mapping):
            raise WorkAnalysisError("invalid_request", 400)
        supplied = dict(payload)
        if supplied.get("max_tokens", output_tokens) != output_tokens:
            raise WorkAnalysisError("configuration_changed", 409)
        if "allowed_provider_ids" in supplied and supplied["allowed_provider_ids"] != list(provider_ids):
            raise WorkAnalysisError("configuration_changed", 409)
        supplied["max_tokens"] = output_tokens
        supplied["allowed_provider_ids"] = list(provider_ids)
        try:
            request = normalize_text_invocation(supplied, agent=agent)
            planned = plan_invocation(
                request, profiles, agent_context_window=agent.context_window,
                required_capabilities=agent.required_capabilities,
            )
            public = planned.to_public_dict()
            # The v2 exclusion list is display-bounded at 64, not exhaustive.
            can_segment = False
            seen_candidates = set()
            if not planned.can_run:
                minimum_request = replace(
                    request, messages=tuple(replace(message, content="") for message in request.messages),
                )
                for profile in profiles:
                    if (profile.unavailable_reason is not None
                            or set(agent.required_capabilities).difference(profile.capabilities)):
                        continue
                    identity = (profile.provider_id, profile.model_key)
                    if identity in seen_candidates:
                        continue
                    seen_candidates.add(identity)
                    single = plan_invocation(
                        request, (profile,), agent_context_window=agent.context_window,
                        required_capabilities=agent.required_capabilities,
                    )
                    if any(item.reason == "context_overflow" for item in single.excluded):
                        # Reuse the same candidate's framing and shrinking safety
                        # margin; fixed output/framing failure cannot be segmented.
                        minimum = plan_invocation(
                            minimum_request, (profile,), agent_context_window=agent.context_window,
                            required_capabilities=agent.required_capabilities,
                        )
                        if minimum.can_run:
                            can_segment = True
                            break
            public["work_can_segment"] = can_segment
            public["work_targets"] = [dict(target) for target in work_targets]
            return public
        except InvocationValidationError:
            raise WorkAnalysisError("configuration_unavailable", 409) from None

    return configuration, agent_prompt, preflight


def work_configuration_identity(conn, agent_id: int, output_tokens: int):
    configuration, agent_prompt, preflight = snapshot_work_configuration(conn, agent_id, output_tokens)
    return configuration
```

The caller owns one short read or write transaction. The configuration reader's nested catalog read scope is intentionally a no-op, not a new transaction and never a nested write. The returned preflight closes over immutable projected values, not the connection; it remains pure after the caller closes the database. The Agent prompt is returned only in memory and must be explicitly included by Task 2's message materializer. `work_can_segment` is a private planner decision, derived from the full candidate projection even when the public v2 exclusion list is truncated; Task 2 consumes it, while Task 7 constructs an allowlisted response without exposing this internal addition or changing global v2 enums. `work_targets` is a private, immutable-captured inventory of up to 64 deduplicated Provider/exact-model identities with only the safe fields shown above; each preflight returns fresh copies. Task 7 uses this inventory for consent disclosure instead of only that payload's eligible targets: an input-capacity-excluded model may be eligible for a future smaller reducer. Configuration changes still invalidate the parent rather than expanding this captured inventory. No service instance, DNS request, decrypt, probe, health update, catalog materialization, or job creation occurs.

The captured Provider scope is a tuple; the returned configuration exposes a separate normalized list. Explicit submitted scope must still equal that list, and caller mutations cannot change later preflight results, even after database closure. Segmentation requires a current context_overflow candidate whose empty-content request with the same message roles/count and output reserve is eligible under the existing planner. This preserves protocol-specific framing (including Anthropic system separators), recomputes the smaller-input safety margin, and never combines limits from different candidates. A negative whole-input budget alone is neither sufficient to authorize segmentation nor sufficient to reject a feasible smaller input.

- [ ] **1C.4 Run GREEN and review the configuration/consent boundary.**

```powershell
python -m pytest tests/test_work_analysis_configuration.py tests/test_ai_invocation_planner.py tests/test_ai_preflight_api.py -q
python -m pyflakes src/pixiv_novel_sync/work_analysis/configuration.py
git diff --check
git add src/pixiv_novel_sync/work_analysis/configuration.py tests/test_work_analysis_configuration.py
git commit -m "feat: freeze secret-free work analysis configuration and target disclosure"
```

Expected: all selected tests pass. Review read-only actual connection behavior,
missing-key failure without decrypt/probe, full exclusion-inventory capacity
decision even when the whole-input safety margin makes its budget negative,
and disclosure of a small target that may be used only by a future
reducer. No implementation or test-success claim is made by this plan.

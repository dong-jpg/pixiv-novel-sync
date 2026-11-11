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

from dataclasses import replace
import json

import pytest

from pixiv_novel_sync.ai.invocation import InvocationValidationError, TextInvocation, TextMessage
from pixiv_novel_sync.ai.invocation_planner import (
    CandidateProfile,
    TokenEstimate,
    estimate_text_invocation,
    plan_from_payload,
    plan_invocation,
    plan_to_payload,
    protocol_message_overhead,
)


def request(text="START\nEND", *, scope=None, output=1000):
    return TextInvocation((TextMessage("user", text),), 0.0, 0.9, output, scope)


def messages(text="START\nEND"):
    return (
        TextMessage("system", "SYS-A"),
        TextMessage("system", "SYS-B"),
        TextMessage("user", text),
    )


def profile(index, window, *, model=None, provider_id=None, protocol="openai_compatible"):
    return CandidateProfile(
        source_index=index,
        provider_id=provider_id or index + 1,
        provider_name="provider-" + str(index),
        provider_type=protocol,
        model_key=model or "model-" + str(index),
        provider_context_window=1_000_000,
        model_context_window=window,
        provider_config_hash="a" * 64,
        model_config_hash="b" * 64,
        provider_model_id=index + 1,
    )


def exact(count, *, includes_overhead=True):
    return TokenEstimate(count, "test", True, includes_overhead)


def test_long_input_excludes_short_fallback_without_global_minimum():
    profiles = (profile(0, 1_000_000), profile(1, 128_000))
    plan = plan_invocation(
        request(), profiles, agent_context_window=1_000_000,
        estimate=lambda req, p: exact(200_000),
    )
    assert [row.profile.source_index for row in plan.eligible] == [0]
    assert plan.eligible[0].candidate_index == 0
    assert plan.excluded[0].reason == "context_overflow"
    assert plan.excluded[0].candidate_index is None


def test_same_provider_models_use_independent_estimates_not_global_cross_product():
    calls = []

    def estimator(req, p):
        calls.append(p.model_key)
        return exact({"a": 90_000, "b": 200_000}[p.model_key])

    profiles = (
        profile(0, 100_000, model="a", provider_id=7),
        profile(1, 1_000_000, model="b", provider_id=7),
    )
    plan = plan_invocation(request(), profiles, agent_context_window=1_000_000, estimate=estimator)
    assert calls == ["a", "b"]
    assert [row.input_tokens for row in plan.eligible] == [90_000, 200_000]
    assert len(plan.eligible) == 2


def test_empty_scope_does_not_estimate_or_default_to_all():
    def forbidden(req, p):
        raise AssertionError("out-of-scope candidates must not be estimated")

    plan = plan_invocation(
        request(scope=()), (profile(0, 1_000_000),),
        agent_context_window=1_000_000, estimate=forbidden,
    )
    assert not plan.can_run
    assert plan.allowed_provider_ids == ()
    assert plan.excluded[0].reason == "provider_not_allowed"


def test_output_limit_unknown_cap_and_framing_are_distinct():
    profiles = (replace(profile(0, 100_000), max_output_tokens=500), profile(1, 100_000))
    plan = plan_invocation(
        request(output=1000), profiles, agent_context_window=1_000_000,
        estimate=lambda req, p: exact(100),
    )
    assert plan.excluded[0].reason == "output_limit"
    assert plan.eligible[0].profile.max_output_tokens is None
    assert plan.eligible[0].message_overhead == 0
    assert plan.eligible[0].safety_margin == 256


@pytest.mark.parametrize("includes_overhead", [True, False])
def test_estimate_framing_is_counted_only_when_not_already_included(includes_overhead):
    req = TextInvocation(messages(), 0.0, 0.9, 1000)
    p = profile(0, 1_000_000)
    plan = plan_invocation(req, (p,), agent_context_window=1_000_000,
                           estimate=lambda req, p: exact(100, includes_overhead=includes_overhead))
    row = plan.eligible[0]
    assert row.message_overhead == (0 if includes_overhead else protocol_message_overhead(req, p))
    assert row.input_tokens == 100


def test_anthropic_joins_multiple_system_messages_for_framing_and_estimation():
    req = TextInvocation(messages(), 0.0, 0.9, 1000)
    openai_profile = profile(0, 1_000_000)
    anthropic_profile = profile(1, 1_000_000, protocol="anthropic")
    assert protocol_message_overhead(req, openai_profile) == 4 * 3 + 2
    assert protocol_message_overhead(req, anthropic_profile) == 4 * 2 + 2
    assert estimate_text_invocation(req, anthropic_profile).count > 0


@pytest.mark.parametrize("count", [-1, None, True, float("nan")])
def test_bad_external_estimate_falls_back_without_losing_whitespace_or_tail(count):
    req = request(" " * 4000 + "中文🙂TAIL")
    p = profile(0, 100_000)
    estimated = estimate_text_invocation(req, p)
    assert estimated.count > 0
    plan = plan_invocation(req, (p,), agent_context_window=1_000_000,
                          estimate=lambda req, p: TokenEstimate(count, "bad", True, True))
    row = plan.eligible[0]
    assert row.exact is False
    assert row.estimator_source == "local_heuristic"
    assert row.safety_margin == max(256, (row.input_tokens + row.message_overhead + 9) // 10)
    assert req.messages[0].content.endswith("TAIL")


def test_required_capabilities_are_checked_before_estimation():
    def forbidden(req, p):
        raise AssertionError("missing capability must not be estimated")

    p = replace(profile(0, 100_000), capabilities=("json",))
    plan = plan_invocation(request(), (p,), agent_context_window=1_000_000,
                           required_capabilities=("streaming",), estimate=forbidden)
    assert plan.excluded[0].reason == "capability_missing"


def test_disabled_first_duplicate_does_not_consume_enabled_occurrence():
    disabled = replace(profile(0, 100_000), unavailable_reason="member_disabled")
    enabled = replace(profile(1, 100_000), provider_id=1, model_key="model-0")
    plan = plan_invocation(request(), (disabled, enabled), agent_context_window=1_000_000)
    assert plan.excluded[0].reason == "member_disabled"
    assert plan.eligible[0].profile.source_index == 1
    assert plan.eligible[0].candidate_index == 0


def test_scope_ids_outside_configured_range_exclude_all_without_estimation():
    def forbidden(req, p):
        raise AssertionError("out-of-scope candidates must not be estimated")

    plan = plan_invocation(
        request(scope=(999,)), (profile(0, 1_000_000),),
        agent_context_window=1_000_000, estimate=forbidden,
    )
    assert not plan.can_run
    assert plan.allowed_provider_ids == ()
    assert plan.excluded[0].reason == "provider_not_allowed"


def test_more_than_64_structurally_available_unique_models_is_configuration_error():
    profiles = tuple(profile(index, 100_000) for index in range(65))
    with pytest.raises(InvocationValidationError):
        plan_invocation(request(), profiles, agent_context_window=1_000_000)


def test_512_disabled_occurrences_retain_64_bounded_exclusion_details():
    profiles = tuple(
        replace(profile(index, 100_000), unavailable_reason="model_unavailable")
        for index in range(512)
    )
    plan = plan_invocation(request(), profiles, agent_context_window=1_000_000)
    assert plan.excluded_count == 512
    assert len(plan.excluded) == 64
    public = plan.to_public_dict()
    assert public["excluded_truncated"] is True


@pytest.mark.parametrize("agent_window,provider_window,expected", [
    (16_000, 128_000, ["agent"]),
    (1_000_000, 128_000, ["provider"]),
])
def test_existing_agent_or_provider_limit_remains_actual_bottleneck(agent_window, provider_window, expected):
    p = replace(profile(0, 1_000_000), provider_context_window=provider_window)
    plan = plan_invocation(request(), (p,), agent_context_window=agent_window,
                           estimate=lambda req, p: exact(100))
    assert plan.eligible[0].context_window == min(agent_window, provider_window)
    assert plan.to_public_dict()["eligible"][0]["limiting_factors"] == expected


def test_public_plan_contains_only_safe_decision_facts():
    plan = plan_invocation(
        request("PRIVATE-BODY-TAIL"), (profile(0, 100_000),),
        agent_context_window=1_000_000,
    )
    public = plan.to_public_dict()
    rendered = json.dumps(public)
    assert public["can_run"] is True
    assert public["eligible"][0]["model_key"] == "model-0"
    for secret in ("PRIVATE-BODY-TAIL", "request_fingerprint", "config_hash", "base_url", "api_key"):
        assert secret not in rendered


def test_private_plan_round_trips_with_tuples_and_validates():
    plan = plan_invocation(
        request(scope=(1,)), (profile(0, 100_000),), agent_context_window=1_000_000,
        estimate=lambda req, p: exact(100, includes_overhead=False),
    )
    payload = plan_to_payload(plan)
    assert payload["eligible"] == json.loads(json.dumps(payload["eligible"]))
    restored = plan_from_payload(payload)
    assert restored == plan


def mutate_profile(payload, **changes):
    payload["eligible"][0]["profile"].update(changes)
    return payload


def mutate_row(payload, **changes):
    payload["eligible"][0].update(changes)
    return payload


def valid_payload():
    plan = plan_invocation(
        request(scope=(1,)), (profile(0, 100_000),), agent_context_window=1_000_000,
        estimate=lambda req, p: exact(100, includes_overhead=False),
    )
    return plan_to_payload(plan)


@pytest.mark.parametrize("mutator", [
    lambda p: mutate_profile(p, provider_id=True),
    lambda p: mutate_profile(p, provider_model_id=True),
    lambda p: mutate_profile(p, pool_id=True),
    lambda p: mutate_profile(p, pool_version=True),
    lambda p: mutate_profile(p, pool_position=True),
    lambda p: mutate_profile(p, source_index=512),
    lambda p: mutate_profile(p, fallback_depth=8),
    lambda p: mutate_profile(p, provider_type="unknown"),
    lambda p: mutate_profile(p, provider_context_window=255),
    lambda p: mutate_profile(p, model_context_window=10_000_001),
    lambda p: mutate_profile(p, max_output_tokens=0),
    lambda p: mutate_profile(p, model_key="model\x000"),
    lambda p: mutate_profile(p, provider_name="provider\u0000"),
    lambda p: mutate_profile(p, display_name="display\u0000"),
    lambda p: mutate_profile(p, pool_name="pool\u0000"),
    lambda p: mutate_profile(p, provider_config_hash="A" * 64),
    lambda p: mutate_profile(p, model_config_hash="b"),
    lambda p: mutate_profile(p, capabilities=[{"capability": "json"}]),
    lambda p: mutate_profile(p, unavailable_reason="arbitrary"),
    lambda p: mutate_row(p, input_tokens=True),
    lambda p: mutate_row(p, message_overhead=-1),
    lambda p: mutate_row(p, safety_margin=None),
    lambda p: mutate_row(p, input_budget=12345),
    lambda p: mutate_row(p, candidate_index=1),
    lambda p: mutate_row(p, reason="context_overflow"),
    lambda p: mutate_row(p, exact=True, safety_margin=255),
    lambda p: p.update({"allowed_provider_ids": [2, 1]}),
    lambda p: p.update({"allowed_provider_ids": [0]}),
    lambda p: p.update({"request_fingerprint": "z" * 64}),
    lambda p: p.update({"output_reserve": 0}),
    lambda p: p.update({"planner_version": ""}),
    lambda p: p.update({"estimator_version": "bad identifier"}),
    lambda p: p.update({"excluded_count": 513}),
    lambda p: p["eligible"][0].update({"messages": []}),
    lambda p: p["eligible"][0]["profile"].update({"messages": []}),
    lambda p: p.update({"messages": []}),
])
def test_invalid_private_snapshot_is_rejected(mutator):
    payload = valid_payload()
    mutator(payload)
    with pytest.raises(InvocationValidationError):
        plan_from_payload(payload)


def test_scope_must_contain_every_eligible_provider():
    payload = valid_payload()
    payload["allowed_provider_ids"] = []
    with pytest.raises(InvocationValidationError):
        plan_from_payload(payload)


def test_excluded_rows_require_reason_and_null_candidate_index():
    payload = valid_payload()
    row = payload["eligible"][0]
    row["candidate_index"] = None
    payload["excluded"].append(row)
    payload["excluded_count"] = 1
    with pytest.raises(InvocationValidationError):
        plan_from_payload(payload)


def test_duplicate_retained_source_index_is_rejected():
    payload = valid_payload()
    payload["excluded"].append(payload["eligible"][0])
    payload["excluded_count"] = 1
    with pytest.raises(InvocationValidationError):
        plan_from_payload(payload)


def test_planner_validates_raw_profiles_before_planning():
    invalid = replace(profile(0, 100_000), provider_type="unknown")
    with pytest.raises(InvocationValidationError):
        plan_invocation(request(), (invalid,), agent_context_window=1_000_000)
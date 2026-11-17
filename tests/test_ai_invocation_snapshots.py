from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, asdict, fields, replace

import pytest

from pixiv_novel_sync.ai.invocation import InvocationValidationError, normalize_text_invocation
from pixiv_novel_sync.ai.invocation_planner import CandidateProfile, plan_invocation
from pixiv_novel_sync.ai.model_router import CandidateSnapshot, ModelRouteConflictError, ModelRouter
from pixiv_novel_sync.ai.models import AIAgentConfig


def canonical_hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def test_v1_unversioned_payload_keeps_exact_shape_and_hash():
    payload = {"agent_config_hash": "a" * 64, "binding_version": 1, "candidates": [{
        "provider_id": 1, "provider_name": "old", "model_key": "manual/old ",
        "provider_model_id": None, "pool_id": None, "pool_name": None,
        "pool_version": None, "pool_position": None, "provider_config_hash": "b" * 64,
        "capabilities": [], "context_window": 16_000, "fallback_depth": 0, "candidate_index": 0,
    }]}
    digest = canonical_hash(payload)
    restored = ModelRouter.candidate_snapshot_from_payload(payload, digest)
    assert type(restored) is CandidateSnapshot
    assert restored.snapshot_hash == digest
    encoded = {"agent_config_hash": restored.agent_config_hash,
               "binding_version": restored.binding_version,
               "candidates": [asdict(item) for item in restored.candidates]}
    assert json.loads(json.dumps(encoded)) == payload
    assert canonical_hash(encoded) == digest
    assert "version" not in encoded and "max_output_tokens" not in encoded["candidates"][0]


def pure_plan(*, allowed=None):
    agent = AIAgentConfig(1, "agent", "general", 1, "model-a", "unused")
    payload = {"messages": [{"role": "user", "content": "synthetic full text"}]}
    if allowed is not None:
        payload["allowed_provider_ids"] = allowed
    invocation = normalize_text_invocation(payload, agent=agent)
    profile = CandidateProfile(
        0, 1, "provider", "openai_compatible", "model-a", 128_000,
        "b" * 64, "c" * 64, model_context_window=64_000,
        max_output_tokens=8_000, capabilities=("streaming",),
    )
    return plan_invocation(invocation, (profile,), agent_context_window=16_000)


def frozen_payload():
    from pixiv_novel_sync.ai.invocation_snapshot import freeze_plan, planned_snapshot_payload

    snapshot = freeze_plan("a" * 64, 1, pure_plan())
    return snapshot, planned_snapshot_payload(snapshot)


def test_v2_envelope_roundtrip_preserves_exact_arrays_hash_and_frozen_fields():
    from pixiv_novel_sync.ai.invocation_snapshot import PlannedCandidateSnapshot, planned_snapshot_payload

    snapshot, payload = frozen_payload()
    assert set(payload) == {"version", "agent_config_hash", "binding_version", "candidates", "plan"}
    assert [field.name for field in fields(snapshot)] == [
        "candidates", "snapshot_hash", "agent_config_hash", "binding_version", "plan", "version",
    ]
    assert snapshot.version == 2
    assert payload == json.loads(json.dumps(payload))
    assert "synthetic full text" not in json.dumps(payload)
    assert "unused" not in json.dumps(payload)
    restored = ModelRouter.candidate_snapshot_from_payload(json.loads(json.dumps(payload)), snapshot.snapshot_hash)
    assert type(restored) is PlannedCandidateSnapshot
    assert restored == snapshot
    assert planned_snapshot_payload(restored) == payload
    assert canonical_hash(payload) == snapshot.snapshot_hash
    with pytest.raises(FrozenInstanceError):
        snapshot.version = 1


@pytest.mark.parametrize("change", [
    "output_reserve", "scope", "fingerprint_shape", "window", "eligible_index",
    "candidate_key", "profile_key", "model_hash_shape", "version_bool", "version_unknown",
    "messages", "candidate_extra", "plan_tuple", "candidate_capability_tuple", "extra_top",
])
def test_v2_rehash_cannot_bypass_strict_shape_formula_or_projection(change):
    _, payload = frozen_payload()
    row = payload["plan"]["eligible"][0]
    if change == "output_reserve":
        payload["plan"]["output_reserve"] += 1
    elif change == "scope":
        payload["plan"]["allowed_provider_ids"] = []
    elif change == "fingerprint_shape":
        payload["plan"]["request_fingerprint"] = "not-a-digest"
    elif change == "window":
        row["context_window"] += 1
    elif change == "eligible_index":
        row["candidate_index"] = 1
    elif change == "candidate_key":
        payload["candidates"][0]["model_key"] = "changed"
    elif change == "profile_key":
        row["profile"]["model_key"] = "changed"
    elif change == "model_hash_shape":
        row["profile"]["model_config_hash"] = "invalid"
    elif change == "version_bool":
        payload["version"] = True
    elif change == "version_unknown":
        payload["version"] = 3
    elif change == "messages":
        row["profile"]["messages"] = [{"role": "user", "content": "sensitive"}]
    elif change == "candidate_extra":
        payload["candidates"][0]["max_output_tokens"] = 8_000
    elif change == "plan_tuple":
        payload["plan"]["eligible"] = tuple(payload["plan"]["eligible"])
    elif change == "candidate_capability_tuple":
        payload["candidates"][0]["capabilities"] = ("streaming",)
    else:
        payload["snapshot_hash"] = "f" * 64
    with pytest.raises(ModelRouteConflictError):
        ModelRouter.candidate_snapshot_from_payload(payload, canonical_hash(payload))


@pytest.mark.parametrize("field", ["request_fingerprint", "model_config_hash"])
def test_v2_valid_rehashed_digest_is_structural_not_a_signature(field):
    from pixiv_novel_sync.ai.invocation_snapshot import planned_snapshot_payload

    _, payload = frozen_payload()
    target = payload["plan"] if field == "request_fingerprint" else payload["plan"]["eligible"][0]["profile"]
    target[field] = "d" * 64
    restored = ModelRouter.candidate_snapshot_from_payload(payload, canonical_hash(payload))
    assert planned_snapshot_payload(restored) == payload


@pytest.mark.parametrize("value", [None, {}, [], "bad", True, float("nan")])
def test_v2_malformed_plan_always_uses_local_snapshot_error(value):
    from pixiv_novel_sync.ai.invocation_snapshot import planned_snapshot_from_payload

    _, payload = frozen_payload()
    payload["plan"] = value
    expected_hash = "a" * 64 if isinstance(value, float) else canonical_hash(payload)
    with pytest.raises(InvocationValidationError) as caught:
        planned_snapshot_from_payload(payload, expected_hash)
    assert caught.value.code == "invalid_snapshot"


def test_v2_empty_plan_roundtrip_is_allowed():
    from pixiv_novel_sync.ai.invocation_snapshot import freeze_plan, planned_snapshot_payload

    snapshot = freeze_plan("a" * 64, 1, pure_plan(allowed=[]))
    assert snapshot.candidates == ()
    assert not snapshot.plan.can_run
    payload = planned_snapshot_payload(snapshot)
    assert ModelRouter.candidate_snapshot_from_payload(payload, snapshot.snapshot_hash) == snapshot


def test_v2_payload_limit_and_digest_mismatch_are_local_errors():
    from pixiv_novel_sync.ai.invocation_snapshot import planned_snapshot_from_payload

    snapshot, payload = frozen_payload()
    with pytest.raises(InvocationValidationError, match="digest"):
        planned_snapshot_from_payload(payload, "e" * 64)
    payload["plan"]["eligible"][0]["profile"]["display_name"] = "X" * (256 * 1024)
    with pytest.raises(InvocationValidationError, match="256 KiB"):
        planned_snapshot_from_payload(payload, canonical_hash(payload))


def test_v2_freeze_rejects_invalid_planning_values():
    from pixiv_novel_sync.ai.invocation_snapshot import freeze_plan

    with pytest.raises(InvocationValidationError):
        freeze_plan("a" * 64, 1, replace(pure_plan(), output_reserve=-1))

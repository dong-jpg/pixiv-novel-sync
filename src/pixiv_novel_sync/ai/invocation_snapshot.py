"""Version-two frozen route envelopes; the legacy route DTO stays unchanged."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any

from .invocation import InvocationValidationError
from .invocation_planner import InvocationPlan, plan_from_payload, plan_to_payload, validate_plan

if TYPE_CHECKING:
    from .model_router import ModelCandidate


@dataclass(frozen=True, slots=True)
class PlannedCandidateSnapshot:
    candidates: tuple[ModelCandidate, ...]
    snapshot_hash: str
    agent_config_hash: str
    binding_version: int
    plan: InvocationPlan
    version: int = 2


def _digest(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise InvocationValidationError("invalid_snapshot", "Planning snapshot is not valid JSON") from None
    if len(encoded) > 256 * 1024:
        raise InvocationValidationError("invalid_snapshot", "Planning snapshot exceeds 256 KiB")
    return hashlib.sha256(encoded).hexdigest()


def planned_snapshot_payload(snapshot: PlannedCandidateSnapshot) -> dict[str, Any]:
    payload = {"version": 2, "agent_config_hash": snapshot.agent_config_hash,
               "binding_version": snapshot.binding_version,
               "candidates": [asdict(candidate) for candidate in snapshot.candidates],
               "plan": plan_to_payload(snapshot.plan)}
    return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))


def freeze_plan(agent_config_hash: str, binding_version: int, plan: InvocationPlan) -> PlannedCandidateSnapshot:
    from .model_router import ModelCandidate

    validate_plan(plan)
    if (not isinstance(agent_config_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", agent_config_hash) is None
            or type(binding_version) is not int or binding_version < 1):
        raise InvocationValidationError("invalid_snapshot", "Planning snapshot identity is invalid")
    candidates = tuple(ModelCandidate(
        provider_id=row.profile.provider_id, provider_name=row.profile.provider_name,
        model_key=row.profile.model_key, provider_model_id=row.profile.provider_model_id,
        pool_id=row.profile.pool_id, pool_name=row.profile.pool_name,
        pool_version=row.profile.pool_version, pool_position=row.profile.pool_position,
        provider_config_hash=row.profile.provider_config_hash, capabilities=row.profile.capabilities,
        context_window=row.context_window, fallback_depth=row.profile.fallback_depth,
        candidate_index=row.candidate_index,
    ) for row in plan.eligible)
    draft = PlannedCandidateSnapshot(candidates, "", agent_config_hash, binding_version, plan)
    return replace(draft, snapshot_hash=_digest(planned_snapshot_payload(draft)))


def planned_snapshot_from_payload(payload, expected_hash) -> PlannedCandidateSnapshot:
    from .model_router import ModelRouteConflictError, ModelRouter

    expected_fields = {"version", "agent_config_hash", "binding_version", "candidates", "plan"}
    if (not isinstance(payload, Mapping) or set(payload) != expected_fields
            or type(payload["version"]) is not int or payload["version"] != 2):
        raise InvocationValidationError("invalid_snapshot", "Version-two snapshot fields are invalid")
    if (not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
            or _digest(payload) != expected_hash):
        raise InvocationValidationError("invalid_snapshot", "Version-two snapshot digest mismatch")
    try:
        v1_fields = {key: payload[key] for key in ("agent_config_hash", "binding_version", "candidates")}
        legacy = ModelRouter.candidate_snapshot_from_payload(v1_fields, _digest(v1_fields))
        plan = plan_from_payload(payload["plan"])
        rebuilt = freeze_plan(legacy.agent_config_hash, legacy.binding_version, plan)
        if planned_snapshot_payload(rebuilt) != dict(payload) or rebuilt.snapshot_hash != expected_hash:
            raise InvocationValidationError("invalid_snapshot", "Snapshot candidates and planning decisions disagree")
        return rebuilt
    except InvocationValidationError:
        raise
    except (ModelRouteConflictError, TypeError, ValueError, OverflowError, RecursionError):
        raise InvocationValidationError("invalid_snapshot", "Version-two snapshot values are invalid") from None

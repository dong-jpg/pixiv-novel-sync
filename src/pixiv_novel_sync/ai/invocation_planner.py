from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

from .invocation import (
    InvocationValidationError,
    TextInvocation,
    invocation_fingerprint,
)
from .model_catalog import normalize_capabilities, normalize_model_key, validate_text_field

PLANNER_VERSION = "capacity-v2.1"
ESTIMATOR_VERSION = "local-heuristic-v1"
MAX_PROFILE_OCCURRENCES = 8 * 64
MAX_EXCLUSION_DETAILS = 64
TokenEstimator = Callable[[TextInvocation, "CandidateProfile"], "TokenEstimate"]

REASON_MESSAGES = {
    "provider_not_allowed": "Provider is outside the allowed scope",
    "provider_disabled": "Provider is disabled",
    "pool_disabled": "Routing group is disabled",
    "member_disabled": "Routing group member is disabled",
    "model_disabled": "Catalog model is disabled",
    "model_unavailable": "Catalog model is unavailable",
    "capability_missing": "Required model capability is missing",
    "output_limit": "Requested output exceeds the model output limit",
    "context_overflow": "Complete input and output reserve exceed the effective context limit",
    "estimation_failed": "Local token estimation failed",
    "duplicate_candidate": "An earlier eligible configuration already identifies this model",
}


@dataclass(frozen=True, slots=True)
class CandidateProfile:
    source_index: int
    provider_id: int
    provider_name: str
    provider_type: str
    model_key: str
    provider_context_window: int
    provider_config_hash: str
    model_config_hash: str
    provider_model_id: int | None = None
    display_name: str | None = None
    model_context_window: int | None = None
    max_output_tokens: int | None = None
    capabilities: tuple[str, ...] = ()
    pool_id: int | None = None
    pool_name: str | None = None
    pool_version: int | None = None
    pool_position: int | None = None
    fallback_depth: int = 0
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    count: int
    source: str
    exact: bool
    includes_overhead: bool


@dataclass(frozen=True, slots=True)
class CandidateBudget:
    profile: CandidateProfile
    candidate_index: int | None
    agent_context_window: int
    context_window: int
    input_tokens: int | None
    input_budget: int | None
    output_reserve: int
    message_overhead: int | None
    safety_margin: int | None
    estimator_source: str | None
    exact: bool | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class InvocationPlan:
    request_fingerprint: str
    allowed_provider_ids: tuple[int, ...]
    output_reserve: int
    eligible: tuple[CandidateBudget, ...]
    excluded: tuple[CandidateBudget, ...]
    excluded_count: int
    planner_version: str = PLANNER_VERSION
    estimator_version: str = ESTIMATOR_VERSION

    @property
    def can_run(self) -> bool:
        return bool(self.eligible)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "version": 2,
            "planner_version": self.planner_version,
            "estimator_version": self.estimator_version,
            "can_run": self.can_run,
            "allowed_provider_ids": list(self.allowed_provider_ids),
            "output_reserve": self.output_reserve,
            "eligible": [_public_budget(row) for row in self.eligible],
            "excluded": [_public_budget(row) for row in self.excluded],
            "excluded_count": self.excluded_count,
            "excluded_truncated": self.excluded_count > len(self.excluded),
            "limits": {
                "max_candidate_attempts": 16,
                "max_network_requests": 32,
                "max_resolved_candidates": 64,
                "max_pool_nodes": 8,
            },
        }


def _framed_contents(request: TextInvocation, profile: CandidateProfile) -> tuple[str, ...]:
    if profile.provider_type != "anthropic":
        return tuple(message.content for message in request.messages)
    system = tuple(message.content for message in request.messages if message.role == "system")
    normal = tuple(message.content for message in request.messages if message.role != "system")
    return (("\n\n".join(system),) if system else ()) + normal


def protocol_message_overhead(request: TextInvocation, profile: CandidateProfile) -> int:
    return 4 * len(_framed_contents(request, profile)) + 2


def _heuristic_text_tokens(text: str) -> int:
    cjk = ascii_count = other = 0
    for char in text:
        code = ord(char)
        if (0x3400 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF
                or 0xAC00 <= code <= 0xD7AF or 0x20000 <= code <= 0x3134F):
            cjk += 1
        elif code < 128:
            # Whitespace is intentionally included; a long blank input is still real input.
            ascii_count += 1
        else:
            other += 1
    return (2 * cjk + 2) // 3 + (ascii_count + 3) // 4 + other


def estimate_text_invocation(request: TextInvocation, profile: CandidateProfile) -> TokenEstimate:
    return TokenEstimate(
        sum(_heuristic_text_tokens(text) for text in _framed_contents(request, profile)),
        "local_heuristic",
        False,
        False,
    )


def _valid_estimate(value: Any) -> bool:
    return (
        isinstance(value, TokenEstimate)
        and isinstance(value.count, int)
        and not isinstance(value.count, bool)
        and value.count >= 0
        and isinstance(value.source, str)
        and 0 < len(value.source) <= 80
        and value.source.isascii()
        and all(ch.isalnum() or ch in "_:/-." for ch in value.source)
        and isinstance(value.exact, bool)
        and isinstance(value.includes_overhead, bool)
    )


def _estimate(
    request: TextInvocation,
    profile: CandidateProfile,
    estimator: TokenEstimator,
) -> TokenEstimate | None:
    try:
        candidate = estimator(request, profile)
    except Exception:
        # Estimator exceptions can carry request text; only the local fallback is safe.
        candidate = None
    if _valid_estimate(candidate):
        return candidate
    try:
        fallback = estimate_text_invocation(request, profile)
    except Exception:
        return None
    return fallback if _valid_estimate(fallback) else None


def _window(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 256 <= value <= 10_000_000:
        raise InvocationValidationError(
            "invalid_configuration", "Context window must be from 256 to 10000000"
        )
    return value


def plan_invocation(
    request: TextInvocation,
    candidates: Sequence[CandidateProfile],
    *,
    agent_context_window: int,
    required_capabilities: Sequence[str] = (),
    estimate: TokenEstimator = estimate_text_invocation,
    estimator_version: str = ESTIMATOR_VERSION,
) -> InvocationPlan:
    agent_window = _window(agent_context_window)
    if len(candidates) > MAX_PROFILE_OCCURRENCES:
        raise InvocationValidationError(
            "invalid_configuration", "Too many configured model occurrences"
        )
    configured = {profile.provider_id for profile in candidates}
    permitted = (
        configured
        if request.allowed_provider_ids is None
        else configured.intersection(request.allowed_provider_ids)
    )
    permitted_ids = tuple(sorted(permitted))
    eligible: list[CandidateBudget] = []
    excluded: list[CandidateBudget] = []
    excluded_count = 0
    seen: set[tuple[int, str]] = set()
    for profile in candidates:
        validate_profile(profile)
        windows = [agent_window, _window(profile.provider_context_window)]
        if profile.model_context_window is not None:
            windows.append(_window(profile.model_context_window))
        window = min(windows)
        row = CandidateBudget(
            profile, None, agent_window, window, None, None,
            request.max_tokens, None, None, None, None, None,
        )
        reason = profile.unavailable_reason
        if reason is None and set(required_capabilities).difference(profile.capabilities):
            reason = "capability_missing"
        identity = (profile.provider_id, profile.model_key)
        if reason is None:
            if identity in seen:
                reason = "duplicate_candidate"
            else:
                seen.add(identity)
                if len(seen) > 64:
                    raise InvocationValidationError(
                        "invalid_configuration", "More than 64 resolved candidates"
                    )
        if reason is None and profile.provider_id not in permitted:
            reason = "provider_not_allowed"
        if reason is None and profile.max_output_tokens is not None and profile.max_output_tokens < request.max_tokens:
            reason = "output_limit"
        if reason is None:
            estimate_value = _estimate(request, profile, estimate)
            if estimate_value is None:
                reason = "estimation_failed"
            else:
                overhead = 0 if estimate_value.includes_overhead else protocol_message_overhead(request, profile)
                margin = 256 if estimate_value.exact else max(256, (estimate_value.count + overhead + 9) // 10)
                input_budget = window - request.max_tokens - overhead - margin
                row = replace(
                    row,
                    input_tokens=estimate_value.count,
                    input_budget=input_budget,
                    message_overhead=overhead,
                    safety_margin=margin,
                    estimator_source=estimate_value.source,
                    exact=estimate_value.exact,
                )
                if estimate_value.count > input_budget:
                    reason = "context_overflow"
        if reason is None:
            eligible.append(replace(row, candidate_index=len(eligible)))
        else:
            excluded_count += 1
            if len(excluded) < MAX_EXCLUSION_DETAILS:
                excluded.append(replace(row, reason=reason))
    plan = InvocationPlan(
        invocation_fingerprint(request, provider_ids=permitted_ids),
        permitted_ids,
        request.max_tokens,
        tuple(eligible),
        tuple(excluded),
        excluded_count,
        PLANNER_VERSION,
        estimator_version,
    )
    validate_plan(plan)
    return plan


def _public_budget(row: CandidateBudget) -> dict[str, Any]:
    p = row.profile
    factors = {"agent": row.agent_context_window, "provider": p.provider_context_window}
    if p.model_context_window is not None:
        factors["model"] = p.model_context_window
    return {
        "source_index": p.source_index,
        "candidate_index": row.candidate_index,
        "provider_id": p.provider_id,
        "provider_name": p.provider_name,
        "provider_model_id": p.provider_model_id,
        "model_key": p.model_key,
        "display_name": p.display_name,
        "pool_id": p.pool_id,
        "pool_name": p.pool_name,
        "pool_position": p.pool_position,
        "fallback_depth": p.fallback_depth,
        "context_window": row.context_window,
        "agent_context_window": row.agent_context_window,
        "provider_context_window": p.provider_context_window,
        "model_context_window": p.model_context_window,
        "max_output_tokens": p.max_output_tokens,
        "limiting_factors": [name for name, value in factors.items() if value == row.context_window],
        "input_tokens": row.input_tokens,
        "input_budget": row.input_budget,
        "output_reserve": row.output_reserve,
        "message_overhead": row.message_overhead,
        "safety_margin": row.safety_margin,
        "estimator_source": row.estimator_source,
        "exact": row.exact,
        "reason": row.reason,
        "reason_message": REASON_MESSAGES[row.reason] if row.reason else None,
    }


def _require(condition: bool) -> None:
    if not condition:
        raise InvocationValidationError("invalid_snapshot", "Planning value is invalid")


def _integer(value: Any, minimum: int, maximum: int, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    _require(type(value) is int and minimum <= value <= maximum)


def _identifier(value: Any) -> None:
    _require(
        isinstance(value, str)
        and 0 < len(value) <= 80
        and value.isascii()
        and all(ch.isalnum() or ch in "_:/-." for ch in value)
    )


def validate_profile(profile: CandidateProfile) -> None:
    _require(isinstance(profile, CandidateProfile))
    _integer(profile.source_index, 0, 511)
    _integer(profile.fallback_depth, 0, 7)
    _integer(profile.provider_id, 1, 2**63 - 1)
    for name in ("provider_model_id", "pool_id", "pool_version", "pool_position"):
        _integer(getattr(profile, name), 1, 2**63 - 1, optional=True)
    _require(
        isinstance(profile.provider_type, str)
        and profile.provider_type in {"openai_compatible", "xai", "anthropic"}
    )
    _window(profile.provider_context_window)
    if profile.model_context_window is not None:
        _window(profile.model_context_window)
    _integer(profile.max_output_tokens, 1, 1_000_000, optional=True)
    for value in (profile.provider_config_hash, profile.model_config_hash):
        _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None)
    try:
        normalized_model_key = normalize_model_key(profile.model_key)
    except Exception:
        raise InvocationValidationError("invalid_snapshot", "Planning value is invalid") from None
    _require(normalized_model_key == profile.model_key)
    for name, codepoints, byte_limit, optional in (
        ("provider_name", 200, 800, False),
        ("display_name", 200, 800, True),
        ("pool_name", 100, 400, True),
    ):
        value = getattr(profile, name)
        if value is not None or not optional:
            try:
                normalized_text = validate_text_field(value, name, codepoints, byte_limit)
            except Exception:
                raise InvocationValidationError(
                    "invalid_snapshot", "Planning value is invalid"
                ) from None
            _require(normalized_text == value)
    _require(isinstance(profile.capabilities, tuple))
    try:
        normalized_capabilities = normalize_capabilities(profile.capabilities)
    except Exception:
        raise InvocationValidationError("invalid_snapshot", "Planning value is invalid") from None
    _require(tuple(normalized_capabilities) == profile.capabilities)
    _require(
        profile.unavailable_reason is None
        or (
            isinstance(profile.unavailable_reason, str)
            and profile.unavailable_reason in REASON_MESSAGES
        )
    )
    if profile.pool_id is None:
        _require(
            profile.pool_name is None
            and profile.pool_version is None
            and profile.pool_position is None
        )
    else:
        _require(
            profile.pool_name is not None
            and profile.pool_version is not None
            and profile.pool_position is not None
        )


def validate_plan(plan: InvocationPlan) -> None:
    _require(isinstance(plan, InvocationPlan))
    _require(
        isinstance(plan.request_fingerprint, str)
        and re.fullmatch(r"[0-9a-f]{64}", plan.request_fingerprint) is not None
    )
    _identifier(plan.planner_version)
    _identifier(plan.estimator_version)
    _integer(plan.output_reserve, 1, 1_000_000)
    _require(isinstance(plan.allowed_provider_ids, tuple))
    for provider_id in plan.allowed_provider_ids:
        _integer(provider_id, 1, 2**63 - 1)
    _require(tuple(sorted(set(plan.allowed_provider_ids))) == plan.allowed_provider_ids)
    _require(isinstance(plan.eligible, tuple) and len(plan.eligible) <= 64)
    _require(isinstance(plan.excluded, tuple) and len(plan.excluded) <= 64)
    _integer(plan.excluded_count, len(plan.excluded), 512)
    seen_sources: set[int] = set()
    for eligible, rows in ((True, plan.eligible), (False, plan.excluded)):
        for index, row in enumerate(rows):
            _require(isinstance(row, CandidateBudget))
            validate_profile(row.profile)
            _require(row.profile.source_index not in seen_sources)
            seen_sources.add(row.profile.source_index)
            _window(row.agent_context_window)
            windows = [row.agent_context_window, row.profile.provider_context_window]
            if row.profile.model_context_window is not None:
                windows.append(row.profile.model_context_window)
            _require(type(row.context_window) is int and row.context_window == min(windows))
            _require(type(row.output_reserve) is int and row.output_reserve == plan.output_reserve)
            if eligible:
                _require(type(row.candidate_index) is int and row.candidate_index == index)
                _require(row.reason is None)
                _require(row.profile.unavailable_reason is None)
                _require(row.profile.provider_id in plan.allowed_provider_ids)
            else:
                _require(
                    row.candidate_index is None
                    and isinstance(row.reason, str)
                    and row.reason in REASON_MESSAGES
                )
            if row.input_tokens is None:
                _require(
                    not eligible
                    and all(
                        value is None
                        for value in (
                            row.input_budget,
                            row.message_overhead,
                            row.safety_margin,
                            row.estimator_source,
                            row.exact,
                        )
                    )
                )
                continue
            _integer(row.input_tokens, 0, 2**63 - 1)
            _integer(row.message_overhead, 0, 2**63 - 1)
            _require(type(row.exact) is bool)
            _identifier(row.estimator_source)
            margin = 256 if row.exact else max(256, (row.input_tokens + row.message_overhead + 9) // 10)
            _require(type(row.safety_margin) is int and row.safety_margin == margin)
            _require(
                type(row.input_budget) is int
                and row.input_budget == row.context_window - row.output_reserve - row.message_overhead - margin
            )
            if eligible:
                _require(row.input_tokens <= row.input_budget)
                _require(
                    row.profile.max_output_tokens is None
                    or row.output_reserve <= row.profile.max_output_tokens
                )
            if row.reason == "context_overflow":
                _require(row.input_tokens > row.input_budget)


def _exact_fields(value: Any, cls: type) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {field.name for field in fields(cls)}:
        raise InvocationValidationError("invalid_snapshot", "Planning fields are invalid")
    return dict(value)


def plan_to_payload(plan: InvocationPlan) -> dict[str, Any]:
    # JSON round-tripping gives Task 3 a stable array-based snapshot shape.
    return json.loads(json.dumps(asdict(plan), ensure_ascii=False, allow_nan=False))


def plan_from_payload(payload: Mapping[str, Any]) -> InvocationPlan:
    raw = _exact_fields(payload, InvocationPlan)
    for key in ("eligible", "excluded"):
        if not isinstance(raw[key], list):
            raise InvocationValidationError("invalid_snapshot", "Planning decisions must be arrays")
        decisions: list[CandidateBudget] = []
        for item in raw[key]:
            decision = _exact_fields(item, CandidateBudget)
            profile = _exact_fields(decision["profile"], CandidateProfile)
            if not isinstance(profile["capabilities"], list):
                raise InvocationValidationError("invalid_snapshot", "Model capabilities must be an array")
            profile["capabilities"] = tuple(profile["capabilities"])
            decision["profile"] = CandidateProfile(**profile)
            decisions.append(CandidateBudget(**decision))
        raw[key] = tuple(decisions)
    if not isinstance(raw["allowed_provider_ids"], list):
        raise InvocationValidationError("invalid_snapshot", "Planning scope must be an array")
    raw["allowed_provider_ids"] = tuple(raw["allowed_provider_ids"])
    plan = InvocationPlan(**raw)
    validate_plan(plan)
    return plan

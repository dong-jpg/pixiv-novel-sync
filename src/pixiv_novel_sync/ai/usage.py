from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any


TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
    "cache_creation_input_tokens", "reasoning_output_tokens",
)
MISSING_REASONS = {None, "pending", "not_reported", "partial_report", "invalid_usage", "interrupted"}
_USAGE_FIELDS = frozenset((*TOKEN_FIELDS, "complete", "missing_reason"))


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    complete: bool = False
    missing_reason: str | None = "pending"

    def __post_init__(self) -> None:
        for field in TOKEN_FIELDS:
            value = getattr(self, field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError("usage counter must be a non-negative integer or null")
        if type(self.complete) is not bool or (
            self.missing_reason is not None
            and (type(self.missing_reason) is not str or self.missing_reason not in MISSING_REASONS)
        ):
            raise ValueError("invalid usage completeness")
        totals_present = all(getattr(self, name) is not None for name in TOKEN_FIELDS[:3])
        if self.complete and (self.missing_reason is not None or not totals_present):
            raise ValueError("complete usage requires all totals")
        if totals_present and self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("inconsistent usage totals")
        for part, total in ((self.cached_input_tokens, self.input_tokens),
                            (self.cache_creation_input_tokens, self.input_tokens),
                            (self.reasoning_output_tokens, self.output_tokens)):
            if part is not None and total is not None and part > total:
                raise ValueError("usage subdivision exceeds inclusive total")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _counter(raw: Mapping[str, Any], key: str) -> int | None:
    value = raw.get(key)
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError("invalid usage counter")
    return value


def _details(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("invalid usage details")
    return value


def normalize_openai_usage(raw: Mapping[str, Any]) -> ProviderUsage:
    if not isinstance(raw, Mapping):
        raise ValueError("usage must be an object")
    inp, out = _counter(raw, "prompt_tokens"), _counter(raw, "completion_tokens")
    total = _counter(raw, "total_tokens")
    if inp is not None and out is not None:
        if total is not None and total != inp + out:
            raise ValueError("inconsistent usage totals")
        total = inp + out
    return ProviderUsage(
        input_tokens=inp, output_tokens=out, total_tokens=total,
        cached_input_tokens=_counter(_details(raw, "prompt_tokens_details"), "cached_tokens"),
        reasoning_output_tokens=_counter(_details(raw, "completion_tokens_details"), "reasoning_tokens"),
        missing_reason="partial_report",
    )


def normalize_anthropic_usage(raw: Mapping[str, Any]) -> ProviderUsage:
    if not isinstance(raw, Mapping):
        raise ValueError("usage must be an object")
    base, out = _counter(raw, "input_tokens"), _counter(raw, "output_tokens")
    read = _counter(raw, "cache_read_input_tokens")
    creation = _counter(raw, "cache_creation_input_tokens")
    # Cache fields are separate input components only in the Anthropic protocol.
    inp = None if base is None else base + (read or 0) + (creation or 0)
    return ProviderUsage(
        input_tokens=inp, output_tokens=out,
        total_tokens=None if inp is None or out is None else inp + out,
        cached_input_tokens=read, cache_creation_input_tokens=creation,
        missing_reason="partial_report",
    )


def _validated_snapshot(raw: Mapping[str, Any]) -> ProviderUsage:
    if not isinstance(raw, Mapping) or set(raw) != _USAGE_FIELDS:
        raise ValueError("invalid usage snapshot schema")
    return ProviderUsage(**dict(raw))


def merge_usage_snapshot(previous: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    old, new = _validated_snapshot(previous), _validated_snapshot(incoming)
    values: dict[str, Any] = {}
    for key in TOKEN_FIELDS:
        known = [value for value in (getattr(old, key), getattr(new, key)) if value is not None]
        values[key] = max(known) if known else None
    if values["input_tokens"] is not None and values["output_tokens"] is not None:
        total = values["input_tokens"] + values["output_tokens"]
        if values["total_tokens"] is not None and values["total_tokens"] > total:
            raise ValueError("inconsistent usage totals")
        values["total_tokens"] = total
    values["complete"] = old.complete or new.complete
    values["missing_reason"] = None if values["complete"] else new.missing_reason
    return ProviderUsage(**values).to_dict()


def summarize_usage(
    attempts: Sequence[Mapping[str, Any]], *, network_request_count: int,
) -> dict[str, Any]:
    if (isinstance(network_request_count, bool) or not isinstance(network_request_count, int)
            or network_request_count < 0):
        raise ValueError("invalid network request count")
    requests: dict[int, ProviderUsage | None] = {}
    for attempt in attempts:
        envelope = attempt.get("usage") if isinstance(attempt, Mapping) else None
        records = envelope.get("requests") if isinstance(envelope, Mapping) else None
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
            continue
        for record in records:
            if not isinstance(record, Mapping):
                continue
            ordinal = record.get("request_ordinal")
            if (isinstance(ordinal, bool) or not isinstance(ordinal, int)
                    or not 1 <= ordinal <= network_request_count):
                continue
            if ordinal in requests:
                # Even an identical duplicate makes this reserved ordinal ambiguous.
                requests[ordinal] = None
                continue
            try:
                requests[ordinal] = _validated_snapshot({
                    key: value for key, value in record.items() if key != "request_ordinal"
                })
            except (TypeError, ValueError):
                requests[ordinal] = None
    known = [usage for usage in requests.values() if usage is not None]
    result: dict[str, Any] = {}
    for field in TOKEN_FIELDS[:3]:
        counters = [getattr(usage, field) for usage in known if getattr(usage, field) is not None]
        result[f"known_{field}"] = sum(counters) if counters else None
    reported = sum(usage.complete for usage in known)
    unreported = network_request_count - reported
    result.update(
        reported_requests=reported,
        unreported_requests=unreported,
        complete=network_request_count > 0 and unreported == 0,
    )
    return result

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from .usage import (
    ProviderUsage,
    merge_usage_snapshot,
    normalize_anthropic_usage,
    normalize_openai_usage,
)


class RequestUsageObserver:
    def __init__(
        self, protocol: str, request_ordinal: int | None,
        on_usage: Callable[[int, Mapping[str, Any]], None] | None,
    ) -> None:
        self.protocol = protocol
        self.ordinal = request_ordinal
        self.callback = on_usage
        self.raw: dict[str, Any] = {}
        self.usage = ProviderUsage()
        self.final_seen = False
        self.invalid = False
        self.finished = False
        self.callback_failed = False

    def _emit(self) -> None:
        if self.callback is not None and self.ordinal is not None:
            try:
                self.callback(self.ordinal, self.usage.to_dict())
            except BaseException:
                # A failed durable observer must not be retried as an HTTP failure.
                self.callback_failed = True
                self.finished = True
                raise

    def _merge_raw(self, validated: Mapping[str, Any]) -> dict[str, Any]:
        fields = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        if self.protocol != "anthropic":
            fields = ("prompt_tokens", "completion_tokens", "total_tokens")
        candidate = dict(self.raw)
        for key in fields:
            value = validated.get(key)
            if value is not None:
                candidate[key] = max(value, candidate.get(key, value))
        if self.protocol != "anthropic":
            for key, part in (("prompt_tokens_details", "cached_tokens"),
                              ("completion_tokens_details", "reasoning_tokens")):
                details = validated.get(key)
                value = details.get(part) if isinstance(details, Mapping) else None
                if value is not None:
                    previous = candidate.get(key, {}).get(part, value)
                    candidate[key] = {part: max(value, previous)}
        return candidate

    def observe(self, raw_usage: Any, *, final: bool = False) -> None:
        if raw_usage is None or self.finished:
            return
        normalizer = normalize_anthropic_usage if self.protocol == "anthropic" else normalize_openai_usage
        try:
            normalized = normalizer(raw_usage)
            candidate = self._merge_raw(raw_usage)
            if self.protocol == "anthropic":
                # message_delta supplies only output; retain the start input/cache components.
                normalized = normalizer(candidate)
            merged = ProviderUsage(**merge_usage_snapshot(self.usage.to_dict(), normalized.to_dict()))
            self.usage = replace(merged, missing_reason="invalid_usage") if self.invalid else merged
            self.raw = candidate
            final_reported = (
                raw_usage.get("output_tokens") is not None if self.protocol == "anthropic"
                else any(getattr(normalized, name) is not None
                         for name in ("input_tokens", "output_tokens", "total_tokens"))
            )
            self.final_seen = self.final_seen or (final and final_reported)
        except (TypeError, ValueError):
            self.invalid = True
            self.usage = replace(self.usage, complete=False, missing_reason="invalid_usage")
        self._emit()

    def finish(self, *, terminal: bool, missing_reason: str | None = None) -> None:
        if self.finished:
            return
        totals_present = all(getattr(self.usage, name) is not None
                             for name in ("input_tokens", "output_tokens", "total_tokens"))
        complete = terminal and self.final_seen and totals_present and not self.invalid
        if complete:
            reason = None
        elif self.invalid:
            reason = "invalid_usage"
        elif missing_reason is not None:
            reason = missing_reason
        elif not self.raw:
            reason = "not_reported"
        else:
            reason = "partial_report"
        self.usage = replace(self.usage, complete=complete, missing_reason=reason)
        self.finished = True
        self._emit()

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .models import AIAgentConfig


class InvocationValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TextMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class TextInvocation:
    messages: tuple[TextMessage, ...]
    temperature: float
    top_p: float
    max_tokens: int
    allowed_provider_ids: tuple[int, ...] | None = None


def _sampling(value: Any, field: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvocationValidationError("invalid_parameter", field + " must be numeric")
    # Integer comparison avoids OverflowError before float conversion.
    if isinstance(value, int):
        if not 0 <= value <= maximum:
            raise InvocationValidationError("invalid_parameter", field + " is out of range")
        result = float(value)
    else:
        result = value
    if not math.isfinite(result) or not 0 <= result <= maximum:
        raise InvocationValidationError("invalid_parameter", field + " is out of range")
    return result


def normalize_text_invocation(payload: Mapping[str, Any], *, agent: AIAgentConfig) -> TextInvocation:
    if not isinstance(payload, Mapping):
        raise InvocationValidationError("invalid_request", "Request must be an object")
    allowed = {"messages", "temperature", "top_p", "max_tokens", "allowed_provider_ids"}
    if set(payload) - allowed:
        raise InvocationValidationError("unsupported", "Request contains unsupported fields")
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, (list, tuple)) or not raw_messages:
        raise InvocationValidationError("invalid_messages", "At least one text message is required")
    messages = []
    for item in raw_messages:
        if not isinstance(item, Mapping) or set(item) != {"role", "content"}:
            raise InvocationValidationError("invalid_messages", "Message fields must be role and content")
        role, content = item["role"], item["content"]
        if role not in ("system", "user", "assistant") or not isinstance(content, str):
            raise InvocationValidationError("invalid_messages", "Message role or content is invalid")
        try:
            content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise InvocationValidationError("invalid_messages", "Message is not valid UTF-8 text") from exc
        messages.append(TextMessage(role, content))
    maximum = payload.get("max_tokens", agent.max_tokens)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 1_000_000:
        raise InvocationValidationError("invalid_parameter", "max_tokens must be an integer from 1 to 1000000")
    provider_ids = None
    if "allowed_provider_ids" in payload:
        raw_ids = payload["allowed_provider_ids"]
        if not isinstance(raw_ids, (list, tuple)) or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in raw_ids
        ):
            raise InvocationValidationError("invalid_scope", "Provider scope must contain positive integer IDs")
        provider_ids = tuple(sorted(set(raw_ids)))
    return TextInvocation(
        tuple(messages),
        _sampling(payload.get("temperature", agent.temperature), "temperature", 2.0),
        _sampling(payload.get("top_p", agent.top_p), "top_p", 1.0),
        maximum,
        provider_ids,
    )


def invocation_messages(request: TextInvocation) -> list[dict[str, str]]:
    return [{"role": message.role, "content": message.content} for message in request.messages]


def invocation_fingerprint(request: TextInvocation, *, provider_ids: tuple[int, ...]) -> str:
    payload = {
        "messages": invocation_messages(request),
        "temperature": request.temperature,
        "top_p": request.top_p,
        "max_tokens": request.max_tokens,
        "allowed_provider_ids": sorted(set(provider_ids)),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

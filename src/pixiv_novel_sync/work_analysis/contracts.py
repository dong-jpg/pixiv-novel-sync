from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any


Json = dict[str, Any]


class WorkAnalysisError(ValueError):
    def __init__(self, code: str, status: int = 400):
        if not isinstance(code, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            code = "invalid_request"
        if type(status) is not int or status not in {400, 403, 404, 409, 410, 413, 503}:
            status = 400
        super().__init__(code)
        self.code, self.status = code, status


@dataclass(frozen=True, slots=True)
class WorkLease:
    run_id: str
    epoch: int
    owner_token: str


@dataclass(frozen=True, slots=True)
class WorkCallBinding:
    lease: WorkLease
    unit_id: str
    try_no: int
    configuration_fingerprint: str


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def strict_object(value: Any, keys: set[str], *, optional: set[str] = frozenset()) -> Json:
    if not isinstance(value, Mapping) or not keys <= set(value) <= keys | optional:
        raise WorkAnalysisError("invalid_fields")
    return dict(value)


def strict_int(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise WorkAnalysisError("invalid_integer")
    return value


def strict_text(value: Any, maximum: int, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or (nonempty and not value.strip()):
        raise WorkAnalysisError("invalid_text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise WorkAnalysisError("invalid_text") from None
    return value


def hex_id(value: Any, length: int = 64) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{%d}" % length, value) is None:
        raise WorkAnalysisError("invalid_id")
    return value


def validate_budget(value: Mapping[str, Any]) -> Json:
    data = strict_object(value, {"request_limit", "token_limit", "active_seconds_limit"})
    return {
        "request_limit": strict_int(data["request_limit"], 1, 512),
        "token_limit": strict_int(data["token_limit"], 1, 100000000),
        "active_seconds_limit": strict_int(data["active_seconds_limit"], 30, 14400),
    }

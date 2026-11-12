"""Closed, versioned task vocabulary shared by coordinator and worker."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import uuid

PROTOCOL = 1
RENDERER = "epub-v1"
KINDS = frozenset({"diagnostic.v1", "epub.single.v1"})
TEXT_LIMIT = 16 * 1024 * 1024
COVER_LIMIT = 5 * 1024 * 1024
RESULT_LIMIT = 32 * 1024 * 1024
MANIFEST_LIMIT = 64 * 1024
RPC_LIMIT = 16 * 1024
DIAGNOSTIC_LIMIT = 4 * 1024
DISK_LIMIT = 1024 * 1024 * 1024
WORK_LIMIT = 128 * 1024 * 1024
LEASE_SECONDS = 180
HEARTBEAT_SECONDS = 30
STOP_SECONDS = 120
ATTEMPT_SECONDS = 600
RUN_SECONDS = 3600
SESSION_SECONDS = 900
MAX_ATTEMPTS = 3
MAX_RUNS = 20
RETENTION_SECONDS = 86400
METADATA_SECONDS = 14 * 86400
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "expired"})
WORKER_ERRORS = frozenset({"render_failed", "input_mismatch", "protocol_mismatch",
                           "worker_transient", "result_too_large"})


class RemoteError(RuntimeError):
    def __init__(self, code: str, status: int = 409, *,
                 run_id: str | None = None, attempt_id: str | None = None):
        self.code, self.status = code, status
        self.run_id, self.attempt_id = run_id, attempt_id
        super().__init__(code)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_digest(kind: str, novel_id: int | None) -> str:
    if not isinstance(kind, str) or kind not in KINDS:
        raise RemoteError("unsupported_kind", 400)
    if kind == "epub.single.v1":
        if type(novel_id) is not int or not 0 < novel_id <= 2**63 - 1:
            raise RemoteError("invalid_novel_id", 400)
    elif novel_id is not None:
        raise RemoteError("unexpected_novel_id", 400)
    return digest(canonical_bytes({"kind": kind, "novel_id": novel_id}))


def retry_delay(kind: str, attempt_no: int, code: str) -> int | None:
    if kind not in KINDS or attempt_no < 1 or attempt_no >= MAX_ATTEMPTS:
        return None
    if code not in {"lease_lost", "worker_restarted", "worker_transient"}:
        return None
    return 30 * (2 ** (attempt_no - 1))


def require_fields(value: object, required: set[str], optional: set[str] | None = None) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - (optional or set()):
        raise RemoteError("invalid_fields", 400)
    return value


def require_uuid(value: object) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise RemoteError("invalid_id", 400) from None
    return value


def require_digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RemoteError("invalid_digest", 400)
    return value


def require_key(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[!-~]{1,200}", value) is None:
        raise RemoteError("invalid_idempotency_key", 400)
    return value


def require_int(value: object, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RemoteError("invalid_integer", 400)
    return value


def require_time(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise RemoteError("invalid_time", 400)
    return float(value)


@dataclass(frozen=True)
class Grant:
    run_id: str
    attempt_id: str
    session_id: str
    epoch: str
    fence: int
    input_sha256: str
    kind: str
    lease_until: float
    hard_deadline: float
    server_now: float = 0.0

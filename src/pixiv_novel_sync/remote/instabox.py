"""Small, bounded adapter for the documented InstaBox lifecycle API.

Only the controller imports this module. No account-wide enumeration, shell
execution, implicit retries, alternative origins, or secret-bearing errors.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from urllib3.exceptions import HTTPError as Urllib3Error
from urllib3.util import Timeout


BASE_URL = "https://api.instabox.sh/v1"
RESPONSE_LIMIT = 1024 * 1024
MACHINE_STATES = frozenset({
    "creating", "starting", "running", "stopping", "stopped", "suspending",
    "suspended", "resuming", "deleting", "deleted", "error",
})
OPERATION_STATES = frozenset({"queued", "running", "succeeded", "failed"})
OPERATION_ACTIONS = frozenset({
    "create", "start", "stop", "suspend", "resume", "delete", "restore", "resize", "move",
})
MUTATIONS = frozenset({"start", "resume", "suspend"})
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_PUBLIC_CODES = frozenset({
    "machine_cooldown", "operation_conflict", "idempotency_conflict",
    "max_run_disabled", "machine_not_running", "no_capacity", "rate_limited",
    "bad_request", "unknown_field", "not_found", "missing_key", "malformed_key",
    "invalid_key", "expired_key", "revoked_key", "session_expired", "missing_scope",
    "permission_denied", "unauthenticated", "api_key_expired", "api_key_revoked",
    "insufficient_scope", "quota_exceeded", "concurrency_limit", "credit_exhausted",
    "spend_limit_reached", "subscription_required", "payment_method_required",
    "early_allowance_ended", "internal_error", "timeout", "host_unavailable",
    "host_oom", "vmm_crashed", "guest_reboot_loop", "no_space",
})


def safe_provider_code(value: object) -> str:
    return value if isinstance(value, str) and value in _PUBLIC_CODES else "provider_error"


class ProviderError(RuntimeError):
    def __init__(self, code: str, http_status: int | None = None,
                 retry_after: float | None = None, operation_id: str | None = None):
        self.code = code
        self.http_status = http_status
        self.retry_after = retry_after
        self.operation_id = operation_id
        super().__init__(code)


@dataclass(frozen=True)
class MachineView:
    id: str
    state: str
    operation_id: str | None
    runtime_ready: bool
    max_run_minutes: int
    run_started_at: float | None
    cooldown_until: float | None

    @property
    def runtime_budget_until(self) -> float | None:
        if self.max_run_minutes == -1:
            return -1.0
        if self.run_started_at is None:
            return None
        return self.run_started_at + self.max_run_minutes * 60


@dataclass(frozen=True)
class OperationView:
    id: str
    machine_id: str
    action: str
    state: str
    error_code: str | None = None


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _timestamp(value: object, code: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ProviderError(code)
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError
        result = instant.astimezone(timezone.utc).timestamp()
        if not math.isfinite(result):
            raise ValueError
        return result
    except (ValueError, OverflowError, OSError):
        raise ProviderError(code) from None


def decode_machine(payload: dict, expected_id: str) -> MachineView:
    code = "invalid_machine"
    required = {"id", "state", "operationId", "runtime", "maxRunMinutes", "runStartedAt", "cooldownUntil"}
    if not isinstance(payload, dict) or not required <= payload.keys():
        raise ProviderError(code)
    if payload["id"] != expected_id or not _valid_id(payload["id"]):
        raise ProviderError(code)
    state = payload["state"]
    runtime = payload["runtime"]
    minutes = payload["maxRunMinutes"]
    operation_id = payload["operationId"]
    if not isinstance(state, str) or state not in MACHINE_STATES:
        raise ProviderError(code)
    if not isinstance(runtime, dict) or type(runtime.get("ready")) is not bool:
        raise ProviderError(code)
    if type(minutes) is not int or not -1 <= minutes <= 2_147_483_647:
        raise ProviderError(code)
    if operation_id is not None and not _valid_id(operation_id):
        raise ProviderError(code)
    if runtime["ready"] and state != "running":
        raise ProviderError(code)
    return MachineView(
        expected_id, state, operation_id, runtime["ready"], minutes,
        _timestamp(payload["runStartedAt"], code), _timestamp(payload["cooldownUntil"], code),
    )


def decode_operation(payload: dict, expected_machine_id: str) -> OperationView:
    code = "invalid_operation"
    required = {"id", "machineId", "type", "state", "error"}
    if not isinstance(payload, dict) or not required <= payload.keys():
        raise ProviderError(code)
    if not _valid_id(payload["id"]) or payload["machineId"] != expected_machine_id:
        raise ProviderError(code)
    state, action = payload["state"], payload["type"]
    if not isinstance(state, str) or state not in OPERATION_STATES:
        raise ProviderError(code)
    if not isinstance(action, str) or action not in OPERATION_ACTIONS:
        raise ProviderError(code)
    error = payload["error"]
    if error is not None and not isinstance(error, dict):
        raise ProviderError(code)
    if state == "failed" and not isinstance(error, dict):
        raise ProviderError(code)
    return OperationView(
        payload["id"], expected_machine_id, action, state,
        safe_provider_code(error.get("code")) if state == "failed" else None,
    )


def _retry_after(headers, error: dict, now: float) -> float | None:
    candidates: list[float] = []
    body_seconds = error.get("retryAfterSeconds")
    if type(body_seconds) is int and 0 <= body_seconds <= 2_147_483_647:
        candidates.append(float(body_seconds))
    value = headers.get("Retry-After")
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"\d{1,10}", value):
            candidates.append(float(value))
        else:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is not None:
                    candidates.append(max(0.0, when.timestamp() - now))
            except (ValueError, TypeError, OverflowError, OSError):
                pass
    return max(candidates) if candidates else None


def _json_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate_field")
        result[name] = value
    return result


def _json_constant(value):
    raise ValueError("nonfinite_json")


class InstaBoxClient:
    def __init__(self, api_key: str, machine_id: str, transport=None):
        if not isinstance(api_key, str) or not 1 <= len(api_key) <= 4096 or any(
            ord(char) < 33 or ord(char) > 126 for char in api_key
        ):
            raise ProviderError("invalid_provider_key")
        if not _valid_id(machine_id):
            raise ProviderError("invalid_machine_id")
        self.machine_id = machine_id
        self._api_key = api_key
        self._transport = transport if transport is not None else requests.Session()
        # Do not pick up credentials from .netrc or ambient proxy configuration.
        if transport is None:
            self._transport.trust_env = False

    def _request(self, method: str, path: str, expected_status: int,
                 idempotency_key: str | None = None) -> dict:
        headers = {"X-API-Key": self._api_key, "Accept": "application/json", "Accept-Encoding": "identity"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        kwargs = {"headers": headers, "timeout": Timeout(total=15, connect=5, read=10), "allow_redirects": False,
                  "verify": True, "stream": True}
        if method == "POST":
            kwargs["json"] = {}
        response = None
        deadline = time.monotonic() + 15
        try:
            response = self._transport.request(method, BASE_URL + path, **kwargs)
            if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                raise ProviderError("invalid_provider_response", response.status_code)
            length = response.headers.get("Content-Length")
            if length is not None:
                if not isinstance(length, str) or not length.isascii() or not length.isdecimal():
                    raise ProviderError("invalid_provider_response", response.status_code)
                if len(length) > 10 or int(length) > RESPONSE_LIMIT:
                    raise ProviderError("provider_response_too_large", response.status_code)
            data = bytearray()
            raw = getattr(response, "raw", None)
            read1 = getattr(raw, "read1", None)
            chunks = None if callable(read1) else response.iter_content(chunk_size=1 if raw is not None else 64 * 1024)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderError("provider_unreachable")
                sock = getattr(getattr(raw, "connection", None), "sock", None)
                if sock is None:
                    # Connection: close / HTTP/1.0 can detach the socket from
                    # urllib3 while http.client still owns its unread body.
                    stream = getattr(getattr(raw, "_fp", None), "fp", None)
                    sock = getattr(getattr(stream, "raw", None), "_sock", None)
                if sock is not None:
                    # The old urllib3 iter_content fallback must obey the same
                    # shrinking budget as read1, including the final read.
                    sock.settimeout(min(10.0, remaining))
                if callable(read1):
                    # read1 returns after one underlying read. iter_content(64k)
                    # can otherwise buffer a whole chunk during an endless drip.
                    chunk = read1(64 * 1024, decode_content=False)
                else:
                    chunk = next(chunks, b"")
                if time.monotonic() >= deadline:
                    raise ProviderError("provider_unreachable")
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise ProviderError("invalid_provider_response", response.status_code)
                if len(data) + len(chunk) > RESPONSE_LIMIT:
                    raise ProviderError("provider_response_too_large", response.status_code)
                data.extend(chunk)
            try:
                payload = json.loads(bytes(data), object_pairs_hook=_json_object, parse_constant=_json_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise ProviderError("invalid_provider_response", response.status_code) from None
            if not isinstance(payload, dict):
                raise ProviderError("invalid_provider_response", response.status_code)
            if response.status_code != expected_status:
                error = payload.get("error")
                error = error if isinstance(error, dict) else {}
                operation_id = error.get("operationId")
                raise ProviderError(
                    safe_provider_code(error.get("code")), response.status_code,
                    _retry_after(response.headers, error, time.time()),
                    operation_id if _valid_id(operation_id) else None,
                )
            return payload
        except ProviderError as error:
            # A proxy may return an HTML/oversized 429 rather than the provider
            # JSON envelope. Already received headers remain authoritative if
            # the body hits our overall deadline, including auth failures.
            if response is not None:
                if error.http_status is None:
                    error.http_status = response.status_code
                if error.retry_after is None:
                    error.retry_after = _retry_after(response.headers, {}, time.time())
            raise
        except (requests.RequestException, Urllib3Error, OSError):
            raise ProviderError(
                "provider_unreachable", response.status_code if response is not None else None,
                _retry_after(response.headers, {}, time.time()) if response is not None else None,
            ) from None
        finally:
            if response is not None:
                response.close()

    def machine(self) -> MachineView:
        return decode_machine(self._request("GET", f"/machines/{self.machine_id}", 200), self.machine_id)

    def operation(self, operation_id: str) -> OperationView:
        if not _valid_id(operation_id):
            raise ProviderError("invalid_operation_id")
        result = decode_operation(self._request("GET", f"/operations/{operation_id}", 200), self.machine_id)
        if result.id != operation_id:
            raise ProviderError("invalid_operation")
        return result

    def mutate(self, action: str, idempotency_key: str) -> OperationView:
        if not isinstance(action, str) or action not in MUTATIONS:
            raise ProviderError("invalid_action")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200 or any(
            ord(char) < 33 or ord(char) > 126 for char in idempotency_key
        ):
            raise ProviderError("invalid_idempotency_key")
        result = decode_operation(self._request(
            "POST", f"/machines/{self.machine_id}/{action}", 202, idempotency_key,
        ), self.machine_id)
        if result.action != action:
            raise ProviderError("invalid_operation")
        return result

    def wake(self) -> MachineView:
        return decode_machine(self._request("POST", f"/machines/{self.machine_id}/wake", 200), self.machine_id)

    def close(self) -> None:
        close = getattr(self._transport, "close", None)
        if close:
            close()

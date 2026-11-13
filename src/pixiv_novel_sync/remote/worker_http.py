"""Bounded outbound worker protocol; there is no caller-supplied URL or command."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import requests
from urllib3.exceptions import HTTPError as UrllibHTTPError
from urllib3.util import Timeout

from .config import validate_origin
from .contracts import (
    COVER_LIMIT, KINDS, MANIFEST_LIMIT, PROTOCOL, RENDERER, RESULT_LIMIT,
    RPC_LIMIT, TEXT_LIMIT, WORKER_ERRORS, Grant, RemoteError, canonical_bytes,
    require_digest, require_fields, require_int, require_key, require_time, require_uuid,
)

_ERROR_CODES = frozenset({
    "claim_terminal", "invalid_session", "session_expired", "session_revoked",
    "unauthorized", "invalid_token", "invalid_bootstrap", "retired_boot",
    "boot_conflict", "session_limit", "invalid_fields", "invalid_id",
    "invalid_digest", "invalid_integer", "invalid_time", "invalid_grant",
    "stale_grant", "lease_expired", "lease_lost", "grant_expired",
    "attempt_expired", "attempt_not_active", "run_not_found", "attempt_not_found",
    "input_mismatch", "protocol_mismatch", "result_too_large", "invalid_result",
    "result_conflict", "artifact_conflict", "receipt_not_found", "not_found",
    "rate_limited", "queue_full", "storage_unavailable", "storage_full",
    "disk_full", "quota_exceeded", "storage_quota", "reservation_conflict", "reservation_expired",
    "worker_transient", "disabled", "invalid_sequence", "heartbeat_replayed",
    "stale_heartbeat", "claim_conflict", "expired", "cancelled", "forbidden",
})


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _json_constant(value):
    raise ValueError("nonfinite_json")


class _DeadlineUpload:
    """requests pulls chunks from this reader; every read shares one deadline."""
    def __init__(self, source, deadline, clock):
        self.source, self.deadline, self.clock = source, deadline, clock

    def read(self, size=-1):
        if self.clock() >= self.deadline:
            raise requests.Timeout("worker upload deadline")
        return self.source.read(size)

    def tell(self):
        return self.source.tell()

    def seek(self, *args):
        return self.source.seek(*args)


class WorkerHTTP:
    """One fixed HTTPS origin, independent Sessions for data and heartbeat threads."""

    def __init__(self, origin: str, key: str, transport=None, *, monotonic=time.monotonic):
        self.origin = validate_origin(origin)
        if not isinstance(key, str) or re.fullmatch(r"[!-~]{32,256}", key) is None:
            raise RemoteError("invalid_worker_key", 400)
        self._key = key
        self._transport = transport
        self._monotonic = monotonic
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._lock = threading.RLock()
        self.session_id: str | None = None
        self.epoch: str | None = None
        self._session_secret: str | None = None

    def __repr__(self) -> str:
        return "WorkerHTTP(<configured origin>)"

    def _get_transport(self):
        if self._transport is not None:
            return self._transport
        if not hasattr(self._local, "transport"):
            session = requests.Session()
            # Do not inherit .netrc credentials or send the worker key via an
            # unexpected environment-configured proxy.
            session.trust_env = False
            self._local.transport = session
            with self._lock:
                self._sessions.append(session)
        return self._local.transport

    def close(self) -> None:
        with self._lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            session.close()

    def reset_session(self) -> None:
        with self._lock:
            self.session_id = self.epoch = self._session_secret = None

    def _read(self, response, limit: int, deadline: float) -> bytes:
        encoding = response.headers.get("Content-Encoding", "identity").lower()
        if encoding not in {"", "identity"}:
            raise RemoteError("protocol_mismatch", 502)
        declared = response.headers.get("Content-Length")
        size = None
        if declared is not None:
            if not re.fullmatch(r"[0-9]{1,10}", str(declared)):
                raise RemoteError("protocol_mismatch", 502)
            size = int(declared)
            if size > limit:
                raise RemoteError("response_too_large", 502)
        chunks, count = bytearray(), 0
        raw = getattr(response, "raw", None)
        read1 = getattr(raw, "read1", None)
        # 旧 urllib3 的逐字节回退也必须在每次读取前收紧 socket 超时。
        iterator = (None if callable(read1) else
                    response.iter_content(chunk_size=1 if raw is not None else 64 * 1024))
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise RemoteError("worker_transient", 503)
            sock = getattr(getattr(raw, "connection", None), "sock", None)
            if sock is None:
                # Connection: close / HTTP/1.0 会将 socket 移交给尚未读完的
                # http.client 响应，此时 urllib3.connection.sock 已为 None。
                stream = getattr(getattr(raw, "_fp", None), "fp", None)
                sock = getattr(getattr(stream, "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(min(10.0, remaining))
            if callable(read1):
                # read1 只做一次底层读取，避免等待慢速响应填满 64 KiB。
                chunk = read1(64 * 1024, decode_content=False)
                if not chunk:
                    break
            else:
                chunk = next(iterator, None)
                if chunk is None:
                    break
            if self._monotonic() >= deadline:
                raise RemoteError("worker_transient", 503)
            if not chunk:
                continue
            if not isinstance(chunk, bytes):
                raise RemoteError("protocol_mismatch", 502)
            count += len(chunk)
            if count > limit or (size is not None and count > size):
                raise RemoteError("response_too_large", 502)
            chunks.extend(chunk)
        if size is not None and count != size:
            raise RemoteError("worker_transient", 503)
        if self._monotonic() >= deadline:
            raise RemoteError("worker_transient", 503)
        return bytes(chunks)

    @staticmethod
    def _decode(data: bytes) -> dict:
        try:
            value = json.loads(data, object_pairs_hook=_json_object, parse_constant=_json_constant)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise RemoteError("protocol_mismatch", 502) from None

    def _request(self, method: str, path: str, *, payload=None, data=None,
                 headers=None, limit: int = RPC_LIMIT, bootstrap: bool = False,
                 allow_empty: bool = False) -> bytes | None:
        with self._lock:
            secret = self._key if bootstrap else self._session_secret
        if not secret:
            raise RemoteError("invalid_session", 401)
        fixed_headers = {
            "Authorization": "Bearer " + secret,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if headers:
            fixed_headers.update(headers)
        if payload is not None:
            data = canonical_bytes(payload)
            if len(data) > RPC_LIMIT:
                raise RemoteError("request_too_large", 413)
            fixed_headers["Content-Type"] = "application/json"
        response = None
        deadline = self._monotonic() + 15
        try:
            if hasattr(data, "read"):
                data = _DeadlineUpload(data, deadline, self._monotonic)
            response = self._get_transport().request(
                method, self.origin + "/api/remote-worker/v1" + path,
                headers=fixed_headers, data=data, timeout=Timeout(total=15, connect=5, read=10),
                allow_redirects=False, verify=True, stream=True,
            )
            if self._monotonic() >= deadline:
                raise RemoteError("worker_transient", 503)
            status = response.status_code
            if type(status) is not int or not 100 <= status <= 599:
                raise RemoteError("protocol_mismatch", 502)
            if 300 <= status < 400:
                raise RemoteError("redirect_refused", 502)
            if allow_empty and status == 204:
                return None
            body = self._read(response, limit if 200 <= status < 300 else RPC_LIMIT, deadline)
            if not 200 <= status < 300:
                error = self._decode(body)
                code = error.get("error")
                if not isinstance(code, str) or code not in _ERROR_CODES:
                    code = "remote_request_failed"
                run_id = attempt_id = None
                if code == "claim_terminal":
                    try:
                        run_id = require_uuid(error.get("run_id"))
                        attempt_id = require_uuid(error.get("attempt_id"))
                    except RemoteError:
                        raise RemoteError("protocol_mismatch", 502) from None
                raise RemoteError(code, status, run_id=run_id, attempt_id=attempt_id)
            return body
        except (requests.RequestException, UrllibHTTPError, OSError):
            raise RemoteError("worker_transient", 503) from None
        finally:
            if response is not None:
                response.close()

    def _rpc(self, method: str, path: str, **kwargs) -> dict | None:
        body = self._request(method, path, **kwargs)
        if body is None:
            return None
        value = self._decode(body)
        try:
            require_time(value.get("server_now"))
        except RemoteError:
            raise RemoteError("protocol_mismatch", 502) from None
        return value

    def enroll(self, boot_id: str, session_secret: str) -> dict:
        require_uuid(boot_id)
        if not isinstance(session_secret, str) or re.fullmatch(r"[!-~]{32,256}", session_secret) is None:
            raise RemoteError("invalid_session_secret", 400)
        value = self._rpc("POST", "/sessions", bootstrap=True, payload={
            "boot_id": boot_id, "session_secret": session_secret,
            "protocol": PROTOCOL, "renderer": RENDERER,
        })
        try:
            session_id = require_uuid(value.get("session_id"))
            epoch = require_uuid(value.get("epoch"))
            if require_time(value.get("expires_at")) <= value["server_now"]:
                raise ValueError
        except (RemoteError, ValueError, TypeError):
            raise RemoteError("protocol_mismatch", 502) from None
        with self._lock:
            self.session_id, self.epoch = session_id, epoch
            self._session_secret = session_secret
        return value

    def session_heartbeat(self) -> dict:
        return self._rpc("POST", "/session-heartbeat", payload={"session_id": self.session_id})

    def claim(self, key: str) -> Grant | None:
        value = self._rpc("POST", "/claim", payload={"claim_key": require_key(key)}, allow_empty=True)
        if value is None:
            return None
        try:
            require_fields(value, set(Grant.__dataclass_fields__))
            for name in ("run_id", "attempt_id", "session_id", "epoch"):
                require_uuid(value[name])
            require_digest(value["input_sha256"])
            require_int(value["fence"], 1)
            for name in ("lease_until", "hard_deadline", "server_now"):
                require_time(value[name])
            if (value["session_id"] != self.session_id or value["epoch"] != self.epoch
                    or value["kind"] not in KINDS or value["lease_until"] > value["hard_deadline"]):
                raise ValueError
            return Grant(**value)
        except (RemoteError, ValueError, TypeError, KeyError):
            raise RemoteError("protocol_mismatch", 502) from None

    @staticmethod
    def _grant_payload(grant: Grant) -> dict:
        return {"epoch": grant.epoch, "fence": grant.fence, "input_sha256": grant.input_sha256}

    @staticmethod
    def _grant_headers(grant: Grant) -> dict:
        return {"X-Remote-Epoch": grant.epoch, "X-Remote-Fence": str(grant.fence),
                "X-Remote-Input-SHA256": grant.input_sha256}

    def fetch_input(self, grant: Grant, part: str) -> bytes:
        limits = {"manifest": MANIFEST_LIMIT, "text": TEXT_LIMIT, "cover": COVER_LIMIT}
        if part not in limits:
            raise RemoteError("invalid_input_part", 400)
        return self._request("GET", f"/attempts/{require_uuid(grant.attempt_id)}/input/{part}",
                             headers=self._grant_headers(grant), limit=limits[part])

    def renew(self, grant: Grant, seq: int) -> dict:
        return self._rpc("POST", f"/attempts/{require_uuid(grant.attempt_id)}/heartbeat",
                         payload={**self._grant_payload(grant), "seq": require_int(seq, 1)})

    def stage(self, grant: Grant, path: Path, sha256: str, size: int) -> dict:
        require_digest(sha256)
        require_int(size, 1, RESULT_LIMIT)
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
            raise RemoteError("input_mismatch", 400)
        with path.open("rb") as source:
            return self._rpc("PUT", f"/attempts/{require_uuid(grant.attempt_id)}/result", data=source,
                             headers={**self._grant_headers(grant), "Content-Length": str(size),
                                      "X-Content-SHA256": sha256,
                                      "Content-Type": "application/octet-stream"})

    def publish(self, grant: Grant, sha256: str, size: int) -> dict:
        return self._rpc("POST", f"/attempts/{require_uuid(grant.attempt_id)}/publish",
                         payload={**self._grant_payload(grant), "sha256": require_digest(sha256),
                                  "size_bytes": require_int(size, 1, RESULT_LIMIT)})

    def fail(self, grant: Grant, code: str) -> dict:
        if code not in WORKER_ERRORS:
            raise RemoteError("invalid_error_code", 400)
        return self._rpc("POST", f"/attempts/{require_uuid(grant.attempt_id)}/fail",
                         payload={**self._grant_payload(grant), "error_code": code})

    def receipt(self, run_id: str) -> dict:
        return self._rpc("GET", f"/runs/{require_uuid(run_id)}/receipt")

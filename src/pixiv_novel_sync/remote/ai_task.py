"""One explicitly budgeted AI text call; no automatic remote retry or replay."""

from __future__ import annotations

from dataclasses import asdict
import math
import secrets
import threading
import time
import uuid

from .contracts import (
    MANIFEST_LIMIT,
    PROTOCOL,
    RESULT_LIMIT,
    TEXT_LIMIT,
    RemoteError,
    canonical_bytes,
    digest,
    require_fields,
    require_int,
)
from cryptography.fernet import Fernet

_authorizations = {}
_authorization_lock = threading.RLock()


def validate_manifest(value):
    require_fields(
        value,
        {
            "protocol",
            "kind",
            "authorization_id",
            "provider_id",
            "text_sha256",
            "text_size",
            "model",
            "temperature",
            "top_p",
            "max_tokens",
        },
    )
    if type(value["protocol"]) is not int or value["protocol"] != PROTOCOL or value["kind"] != "ai.text.v1":
        raise RemoteError("invalid_manifest", 400)
    from .contracts import require_digest

    require_digest(value["authorization_id"])
    require_digest(value["text_sha256"])
    require_int(value["provider_id"], 1)
    require_int(value["text_size"], 1, TEXT_LIMIT)
    require_int(value["max_tokens"], 1, 1000000)
    if not isinstance(value["model"], str) or not 1 <= len(value["model"]) <= 256:
        raise RemoteError("invalid_manifest", 400)
    for name, maximum in [("temperature", 2), ("top_p", 1)]:
        if (
            type(value[name]) not in (int, float)
            or not math.isfinite(value[name])
            or not 0 <= value[name] <= maximum
        ):
            raise RemoteError("invalid_manifest", 400)
    return value


def validate_result(payload):
    from ..ai.usage import ProviderUsage
    from ..pixiv_fetch import _decode_json

    value = _decode_json(payload, RESULT_LIMIT)
    require_fields(value, {"text", "usage", "finish_reason", "outcome"})
    if not isinstance(value["text"], str) or len(value["text"].encode()) > 4 * 1024 * 1024:
        raise RemoteError("invalid_ai_result", 400)
    if value["outcome"] not in {"succeeded", "failed", "unknown"} or value["finish_reason"] not in {
        "stop",
        "length",
        "error",
    }:
        raise RemoteError("invalid_ai_result", 400)
    if value["usage"] is not None:
        try:
            ProviderUsage(**value["usage"])
        except (TypeError, ValueError):
            raise RemoteError("invalid_ai_result", 400) from None
    return value


def permit_authorized(store, grant, manifest, clock):
    with _authorization_lock:
        auth = _authorizations.get(manifest["authorization_id"])
        if auth is None or time.monotonic() >= auth["deadline"] or auth["cancelled"]():
            raise RemoteError("ai_authorization_expired", 409)
        with store.read() as conn:
            store._check_grant(conn, grant, clock())
        if auth["input_sha"] != grant.input_sha256:
            raise RemoteError("input_mismatch", 409)
        if auth["ordinal"] is None:
            # The shared AI request guard reserves parent and child budgets in
            # their existing transaction before any network request is permitted.
            auth["ordinal"] = auth["guard"]()
        return {"allowed": True, "retry_after": 0}


def require_authorization(grant, manifest):
    with _authorization_lock:
        auth = _authorizations.get(manifest["authorization_id"])
        if (
            auth is None
            or auth["input_sha"] != grant.input_sha256
            or time.monotonic() >= auth["deadline"]
            or auth["cancelled"]()
        ):
            raise RemoteError("ai_authorization_expired", 409)
        return auth


def render_ai(manifest, messages, credentials, permit, *, provider_factory=None, cancelled=lambda: False):
    from ..ai.models import AIProviderConfig
    from ..ai.providers import create_provider
    from ..ai.usage import merge_usage_snapshot, ProviderUsage

    if not isinstance(messages, list) or not messages:
        raise RemoteError("input_mismatch", 400)
    for message in messages:
        require_fields(message, {"role", "content"})
        if message["role"] not in {"system", "user", "assistant"} or not isinstance(message["content"], str):
            raise RemoteError("input_mismatch", 400)
    require_fields(credentials, {"provider"}, {"payload_key"})
    config = AIProviderConfig(**credentials["provider"])
    if config.id != manifest["provider_id"] or config.max_retries != 0:
        raise RemoteError("input_mismatch", 400)
    provider = (provider_factory or create_provider)(config)
    output = []
    size = 0
    usage = ProviderUsage().to_dict()
    outcome = "unknown"
    finish = "error"
    sent = False

    def guard():
        nonlocal sent
        if sent:
            raise RemoteError("ai_replay_refused", 409)
        permit(1)
        sent = True
        return 1

    def record(ordinal, value):
        nonlocal usage
        usage = merge_usage_snapshot(usage, value)

    try:
        for chunk in provider.stream_generate(
            messages,
            manifest["model"],
            manifest["temperature"],
            manifest["top_p"],
            manifest["max_tokens"],
            request_guard=guard,
            on_usage=record,
            is_cancelled=cancelled,
            deadline=time.monotonic() + 540,
        ):
            if chunk.type == "delta":
                size += len(chunk.text.encode())
                if size > 4 * 1024 * 1024:
                    raise RemoteError("result_too_large", 413)
                output.append(chunk.text)
            elif chunk.type == "done":
                finish = (chunk.data or {}).get("finish_reason", "error")
                outcome = "succeeded" if finish == "stop" else "failed"
        if not sent:
            outcome = "failed"
    except Exception:
        outcome = "unknown" if sent else "failed"
        finish = "error"
    finally:
        provider.close()
    return canonical_bytes(
        {"text": "".join(output), "usage": usage, "finish_reason": finish, "outcome": outcome}
    )


class RemoteAIProvider:
    def __init__(self, local):
        self.local, self.config = local, local.config

    def close(self):
        self.local.close()

    def __getattr__(self, name):
        return getattr(self.local, name)

    def stream_generate(
        self,
        messages,
        model,
        temperature,
        top_p,
        max_tokens,
        *,
        request_guard=None,
        is_cancelled=None,
        on_usage=None,
        deadline=None,
    ):
        import os
        from .config import RemoteConfig
        from .store import RemoteStore
        from .blobs import BlobStore
        from .business import BusinessTasks
        from ..ai.models import AIStreamChunk
        from ..ai.providers import AIProviderError

        if not callable(request_guard):
            raise AIProviderError(
                "Remote invocation requires a budget guard", category="cancelled", finish_reason="cancelled"
            )
        cfg = RemoteConfig.from_env(os.environ, "web")
        if not cfg.enabled:
            raise AIProviderError(
                "Remote execution unavailable", category="cancelled", finish_reason="cancelled"
            )
        store = RemoteStore(cfg.state_dir, cfg.machine_id)
        store.migrate()
        blobs = BlobStore(cfg.state_dir / "blobs")
        tasks = BusinessTasks(store, blobs)
        identity = secrets.token_hex(32)
        payload_key = Fernet.generate_key()
        encoded = Fernet(payload_key).encrypt(canonical_bytes(messages))
        if len(encoded) > TEXT_LIMIT:
            raise RemoteError("input_too_large", 413)
        manifest = validate_manifest(
            {
                "protocol": PROTOCOL,
                "kind": "ai.text.v1",
                "authorization_id": identity,
                "provider_id": self.config.id,
                "text_sha256": digest(encoded),
                "text_size": len(encoded),
                "model": model,
                "temperature": temperature,
                "top_p": top_p,
                "max_tokens": max_tokens,
            }
        )
        raw = canonical_bytes(manifest)
        input_sha = digest(raw)
        auth = {
            "input_sha": input_sha,
            "deadline": min(deadline or float("inf"), time.monotonic() + 600),
            "cancelled": is_cancelled or (lambda: False),
            "guard": request_guard,
            "ordinal": None,
            "payload_key": payload_key,
            "provider": {**asdict(self.config), "max_retries": 0},
        }
        run = None
        reservation = None
        try:
            reservation = store.reserve_blob("input", identity, len(encoded) + MANIFEST_LIMIT, time.time())
            blobs.put_bytes(encoded, TEXT_LIMIT)
            blobs.put_bytes(raw, MANIFEST_LIMIT)
            store.register_input(
                input_sha,
                {"manifest": (input_sha, len(raw)), "text": (manifest["text_sha256"], len(encoded))},
                reservation,
                time.time(),
            )
            with _authorization_lock:
                _authorizations[identity] = auth
            run = store.enqueue(uuid.uuid4().hex, input_sha, "ai.text.v1", input_sha, time.time())
            while time.monotonic() < auth["deadline"]:
                if auth["cancelled"]():
                    raise InterruptedError()
                current = store.get_run(run["run_id"])
                if current["state"] == "succeeded":
                    break
                if current["state"] in {"failed", "cancelled", "expired"}:
                    raise RemoteError("remote_ai_failed", 409)
                time.sleep(0.2)
            else:
                raise RemoteError("remote_ai_timeout", 503)
            result = tasks.result(run["run_id"])
            if auth["cancelled"]():
                raise InterruptedError()
            if on_usage and auth["ordinal"] is not None and result["usage"] is not None:
                on_usage(auth["ordinal"], result["usage"])
            if result["text"]:
                yield AIStreamChunk(type="delta", text=result["text"])
            if result["outcome"] != "succeeded":
                raise RemoteError("remote_ai_result_unknown", 409)
            yield AIStreamChunk(type="done", data={"finish_reason": result["finish_reason"]})
        except (RemoteError, InterruptedError):
            # Do not automatically fall through to another model after a remote
            # result is unknown; preserve the existing reservation as unknown.
            raise AIProviderError(
                "Remote call stopped or outcome unknown; review before retrying",
                category="cancelled",
                finish_reason="cancelled",
            ) from None
        finally:
            with _authorization_lock:
                _authorizations.pop(identity, None)
            if run is not None:
                store.cancel(run["run_id"], time.time())
            if reservation:
                store.release_reservation(reservation, time.time())

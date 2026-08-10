from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from flask import current_app, session

from ..settings import Settings


_ACCESS_TTL_SECONDS = 10 * 60
_SCOPE_PREFIX = b"adult-owner:"
_ACCESS_PREFIX = b"adult-access:"


@dataclass(frozen=True, slots=True)
class AdultOwner:
    scope: str
    authenticated_at: int


def _secret_key() -> bytes:
    value = current_app.secret_key
    if isinstance(value, bytes) and value:
        return value
    if isinstance(value, str) and value:
        return value.encode("utf-8")
    raise PermissionError("成人功能认证不可用")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or not value.isascii():
        raise ValueError("invalid base64 payload")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    )


def _job_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(character) < 32 for character in value)
    ):
        raise PermissionError("成人访问凭证无效")
    return value


def require_adult_owner(settings: Settings) -> AdultOwner:
    dashboard_token = getattr(settings, "dashboard_token", None)
    if not isinstance(dashboard_token, str) or not dashboard_token:
        raise PermissionError("成人功能要求配置 Dashboard token")
    if session.get("authenticated") is not True:
        raise PermissionError("成人功能要求已认证会话")
    authenticated_at = session.get("authenticated_at")
    if (
        isinstance(authenticated_at, bool)
        or not isinstance(authenticated_at, int)
        or authenticated_at <= 0
    ):
        raise PermissionError("成人功能认证状态无效")
    scope = hmac.new(
        _secret_key(),
        _SCOPE_PREFIX + dashboard_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return AdultOwner(scope=scope, authenticated_at=authenticated_at)


def sign_adult_access(owner: AdultOwner, job_id: str) -> str:
    if not isinstance(owner, AdultOwner):
        raise PermissionError("成人访问 owner 无效")
    safe_job_id = _job_id(job_id)
    issued_at = int(time.time())
    payload = {
        "exp": issued_at + _ACCESS_TTL_SECONDS,
        "iat": issued_at,
        "job": safe_job_id,
        "nonce": secrets.token_urlsafe(16),
        "scope": owner.scope,
    }
    encoded = _b64encode(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signature = hmac.new(
        _secret_key(),
        _ACCESS_PREFIX + encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded}.{_b64encode(signature)}"


def verify_adult_access(token: Any, owner: AdultOwner, job_id: str) -> None:
    try:
        if not isinstance(owner, AdultOwner) or not isinstance(token, str):
            raise ValueError("invalid access token")
        if len(token) > 2048 or token.count(".") != 1:
            raise ValueError("invalid access token")
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = hmac.new(
            _secret_key(),
            _ACCESS_PREFIX + encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
        decoded_signature = _b64decode(supplied_signature)
        if not hmac.compare_digest(decoded_signature, expected_signature):
            raise ValueError("invalid access signature")
        payload = json.loads(_b64decode(encoded))
        if not isinstance(payload, dict) or set(payload) != {
            "exp",
            "iat",
            "job",
            "nonce",
            "scope",
        }:
            raise ValueError("invalid access payload")
        issued_at = payload.get("iat")
        expires_at = payload.get("exp")
        authenticated_at = owner.authenticated_at
        now = int(time.time())
        if (
            isinstance(issued_at, bool)
            or not isinstance(issued_at, int)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or isinstance(authenticated_at, bool)
            or not isinstance(authenticated_at, int)
            or authenticated_at <= 0
            or expires_at <= issued_at
            or expires_at - issued_at > _ACCESS_TTL_SECONDS
            or issued_at < authenticated_at
            or issued_at > now + 30
            or now >= expires_at
        ):
            raise ValueError("expired access token")
        if not hmac.compare_digest(str(payload.get("scope") or ""), owner.scope):
            raise ValueError("owner mismatch")
        if not hmac.compare_digest(str(payload.get("job") or ""), _job_id(job_id)):
            raise ValueError("job mismatch")
        nonce = payload.get("nonce")
        if not isinstance(nonce, str) or not 16 <= len(nonce) <= 128:
            raise ValueError("invalid access nonce")
    except (TypeError, ValueError, UnicodeError):
        raise PermissionError("成人访问凭证无效") from None


__all__ = [
    "AdultOwner",
    "require_adult_owner",
    "sign_adult_access",
    "verify_adult_access",
]

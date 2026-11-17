"""Dashboard login primitives, independent of routes and storage connections."""
from __future__ import annotations

import hashlib
import hmac
import math
import posixpath
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlsplit

from flask.sessions import SecureCookieSessionInterface


AUTH_SESSION_KEY = "auth_session_id"
AUTH_EXPIRY_KEY = "auth_session_expires_at"
BROWSER_SESSION_SECONDS = 7 * 24 * 60 * 60
REMEMBER_SESSION_SECONDS = 30 * 24 * 60 * 60
_CREDENTIAL_PURPOSE = b"pixiv-novel-sync:web-auth-credential:v1\0"


def hash_session_id(value: Any) -> str | None:
    """Only 256-bit URL-safe identifiers minted by this version are accepted."""
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None:
        return None
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def credential_version(secret: str | bytes, credential: str) -> str:
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    return hmac.new(key, _CREDENTIAL_PURPOSE + credential.encode("utf-8"), hashlib.sha256).hexdigest()


def session_record_is_current(record: dict[str, Any] | None, version: str, now: float) -> bool:
    if record is None:
        return False
    return (
        record["created_at"] <= now < record["expires_at"]
        and hmac.compare_digest(record["credential_version"].encode("utf-8"), version.encode("utf-8"))
    )


class AbsoluteAuthSessionInterface(SecureCookieSessionInterface):
    """Keep Expires fixed even when Flask re-saves the cookie (e.g. new CSRF).

    This signed timestamp only controls browser retention. Authentication always
    checks the database record, never this cookie's claimed expiry/permanence.
    """

    def get_expiration_time(self, app, session):
        if not session.permanent:
            return None
        expiry = session.get(AUTH_EXPIRY_KEY)
        if isinstance(expiry, (int, float)) and math.isfinite(expiry):
            try:
                return datetime.fromtimestamp(expiry, timezone.utc)
            except (ValueError, OverflowError, OSError):
                pass
        # Legacy/malformed permanent cookies must not gain another lifetime.
        return datetime.fromtimestamp(0, timezone.utc)


def safe_next_path(value: str | None) -> str:
    """Accept only local paths, including after percent-decoding/normalisation."""
    if not isinstance(value, str):
        return "/"
    candidate = value
    for _ in range(8):
        if (
            not candidate.startswith("/")
            or candidate.startswith("//")
            or "\\" in candidate
            or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in candidate)
        ):
            return "/"
        try:
            parts = urlsplit(candidate)
        except ValueError:
            return "/"
        if parts.scheme or parts.netloc:
            return "/"
        if posixpath.normpath(parts.path).rstrip("/") == "/api/auth/login":
            return "/"
        decoded = unquote(candidate)
        if decoded == candidate:
            return value
        candidate = decoded
    return "/"

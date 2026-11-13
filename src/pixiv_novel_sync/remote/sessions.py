"""Opaque session credentials are hashed before entering persistent storage."""
from __future__ import annotations

import hashlib
import hmac
import re

from .contracts import RemoteError


def token_hash(token: str) -> str:
    if not isinstance(token, str) or re.fullmatch(r"[!-~]{32,256}", token) is None:
        raise RemoteError("invalid_session_secret", 400)
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def check_bootstrap(token: str, expected_hash: str) -> bool:
    try:
        actual = token_hash(token)
    except RemoteError:
        return False
    return hmac.compare_digest(actual, expected_hash)

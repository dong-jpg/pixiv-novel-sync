"""Shared local/remote account lease and request spacing in the control store."""

from __future__ import annotations

import secrets
import time
import math

from .contracts import RemoteError, require_digest, require_time

ACCOUNT_LEASE_SECONDS = 120


def acquire_account(store, scope, interval, now=None):
    require_digest(scope)
    now = time.time() if now is None else require_time(now)
    if type(interval) not in (int, float) or not math.isfinite(interval) or not 0 <= interval <= 3600:
        raise RemoteError("invalid_request_interval", 400)
    owner = secrets.token_hex(32)
    with store.transaction() as conn:
        old = conn.execute("SELECT * FROM account_leases WHERE scope=?", (scope,)).fetchone()
        if old and old["expires_at"] > now:
            raise RemoteError("account_busy", 409)
        conn.execute(
            "INSERT INTO account_leases(scope,owner,expires_at,request_interval) VALUES(?,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at,"
            "request_interval=excluded.request_interval",
            (scope, owner, now + ACCOUNT_LEASE_SECONDS, interval),
        )
    return owner


def require_account(conn, scope, owner, now):
    row = conn.execute("SELECT * FROM account_leases WHERE scope=?", (require_digest(scope),)).fetchone()
    if not row or row["owner"] != require_digest(owner) or row["expires_at"] <= require_time(now):
        raise RemoteError("account_lease_lost", 409)
    return row


def reserve_request(conn, scope, owner, now):
    row = require_account(conn, scope, owner, now)
    if row["next_request_at"] > now:
        return row["next_request_at"] - now
    conn.execute(
        "UPDATE account_leases SET next_request_at=? WHERE scope=? AND owner=?",
        (now + row["request_interval"], scope, owner),
    )
    return 0.0

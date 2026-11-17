"""Server-side revocation records; never store the bearer identifier itself."""
from __future__ import annotations

import sqlite3
from typing import Any


class WebAuthSessionsMixin:
    conn: sqlite3.Connection
    transaction: Any

    def get_web_auth_session(self, token_hash: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT token_hash, credential_version, created_at, expires_at, persistent "
            "FROM web_auth_sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        return dict(row) if row is not None else None

    def replace_web_auth_session(
        self,
        *,
        token_hash: str,
        credential_version: str,
        created_at: float,
        expires_at: float,
        persistent: bool,
        previous_token_hash: str | None = None,
    ) -> None:
        """Rotate one browser atomically, also pruning already-expired records."""
        with self.transaction() as conn:
            if previous_token_hash is not None:
                conn.execute(
                    "DELETE FROM web_auth_sessions WHERE token_hash = ?",
                    (previous_token_hash,),
                )
            conn.execute("DELETE FROM web_auth_sessions WHERE expires_at <= ?", (created_at,))
            conn.execute(
                "INSERT INTO web_auth_sessions "
                "(token_hash, credential_version, created_at, expires_at, persistent) "
                "VALUES (?, ?, ?, ?, ?)",
                (token_hash, credential_version, created_at, expires_at, int(persistent)),
            )

    def revoke_web_auth_session(self, token_hash: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM web_auth_sessions WHERE token_hash = ?", (token_hash,))

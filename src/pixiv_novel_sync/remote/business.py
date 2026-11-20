"""Scoped remote Pixiv calls; credentials stay outside input blobs and logs."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .account_scope import require_account, reserve_request
from .business_contracts import decode_business_manifest, validate_business_manifest, validate_business_result
from .contracts import (
    ATTEMPT_SECONDS,
    MANIFEST_LIMIT,
    PROTOCOL,
    RESULT_LIMIT,
    RemoteError,
    canonical_bytes,
    digest,
    require_int,
)


class BusinessTasks:
    def __init__(self, store, blobs, *, secret_key=None, clock=time.time):
        self.store, self.blobs, self.clock = store, blobs, clock
        self._secret = secret_key

    def _cipher(self):
        value = (
            self._secret
            or os.environ.get("PIXIV_REMOTE_TASK_SECRET")
            or os.environ.get("PIXIV_NOVEL_SYNC_AI_SECRET_KEY")
        )
        if not isinstance(value, str) or len(value) < 32:
            raise RemoteError("task_secret_unavailable", 503)
        return AESGCM(hashlib.sha256(b"pixiv-remote-task-v1\0" + value.encode()).digest())

    def submit_novel(self, novel_id, access_token, scope, owner, key):
        return self.submit("pixiv.novel.v1", {"novel_id": novel_id}, access_token, scope, owner, key)

    def submit_api(self, method, params, access_token, scope, owner, key):
        from .pixiv_calls import normalize_call

        return self.submit(
            "pixiv.api.v1",
            {"method": method, "params": normalize_call(method, params=params)},
            access_token,
            scope,
            owner,
            key,
        )

    def submit_watchlist(self, page, cookie, scope, owner, key):
        if not isinstance(cookie, str) or not 1 <= len(cookie) <= 8192 or "\n" in cookie or "\r" in cookie:
            raise RemoteError("invalid_web_cookie", 400)
        return self.submit(
            "pixiv.api.v1",
            {"method": "watchlist", "params": {"page": page}},
            None,
            scope,
            owner,
            key,
            credentials={"web_cookie": cookie},
        )

    def submit(self, kind, fields, access_token, scope, owner, key, *, credentials=None):
        if credentials is None and (
            not isinstance(access_token, str)
            or not 16 <= len(access_token) <= 4096
            or any(c.isspace() for c in access_token)
        ):
            raise RemoteError("invalid_access_token", 400)
        manifest = validate_business_manifest(
            {"protocol": PROTOCOL, "kind": kind, **fields, "account_scope": scope, "account_owner": owner,
             "operation_id": digest(key.encode('utf-8'))},
            kind,
        )
        encoded = canonical_bytes(manifest)
        input_sha = digest(encoded)
        identity = input_sha
        existing = self.store.find_submission(key, identity)
        if existing:
            return existing
        cipher = self._cipher()
        nonce = secrets.token_bytes(12)
        encrypted = nonce + cipher.encrypt(
            nonce, canonical_bytes(credentials or {"access_token": access_token}), input_sha.encode()
        )
        now = self.clock()
        with self.store.read() as conn:
            require_account(conn, scope, owner, now)
        reservation = self.store.reserve_blob("input", key, MANIFEST_LIMIT, now)
        try:
            self.blobs.put_bytes(encoded, MANIFEST_LIMIT)
            self.store.register_input(
                input_sha, {"manifest": (input_sha, len(encoded))}, reservation, self.clock()
            )
            with self.store.transaction() as conn:
                require_account(conn, scope, owner, self.clock())
                conn.execute(
                    "INSERT OR REPLACE INTO business_secrets VALUES(?,?,?)",
                    (input_sha, encrypted, now + ATTEMPT_SECONDS),
                )
            return self.store.enqueue(key, identity, manifest["kind"], input_sha, self.clock())
        finally:
            self.store.release_reservation(reservation, self.clock())

    def manifest(self, grant):
        with self.blobs.open_blob(grant.input_sha256) as source:
            return decode_business_manifest(source.read(MANIFEST_LIMIT + 1), grant.kind)

    def _upstream_credentials(self, grant):
        manifest = self.manifest(grant)
        now = self.clock()
        if grant.kind not in {"pixiv.novel.v1", "pixiv.api.v1"}:
            raise RemoteError("unsupported_upstream", 400)
        with self.store.read() as conn:
            self.store._check_grant(conn, grant, now)
            require_account(conn, manifest["account_scope"], manifest["account_owner"], now)
            row = conn.execute(
                "SELECT encrypted,expires_at FROM business_secrets WHERE input_sha256=?",
                (grant.input_sha256,),
            ).fetchone()
            if not row or row["expires_at"] <= now:
                raise RemoteError("task_credentials_expired", 409)
            encrypted = bytes(row["encrypted"])
        try:
            return json.loads(
                self._cipher().decrypt(encrypted[:12], encrypted[12:], grant.input_sha256.encode())
            )
        except RemoteError:
            raise
        except Exception:
            raise RemoteError("task_credentials_unavailable", 503) from None

    def permit(self, grant, ordinal):
        manifest = self.manifest(grant)
        if grant.kind == "checkin.submit.v1":
            require_int(ordinal, 1, 1)
            from .checkin_task import permit_submission

            return permit_submission(self.store, grant, manifest, self.clock())
        if grant.kind == "checkin.status.v1":
            raise RemoteError("checkin_mutation_blocked", 403)
        if grant.kind == "ai.text.v1":
            require_int(ordinal, 1, 1)
            from .ai_task import permit_authorized

            return permit_authorized(self.store, grant, manifest, self.clock)
        require_int(ordinal, 1, 2 if grant.kind == "pixiv.novel.v1" else 1)
        now = self.clock()
        with self.store.transaction() as conn:
            self.store._check_grant(conn, grant, now)
            require_account(conn, manifest["account_scope"], manifest["account_owner"], now)
            if conn.execute(
                "SELECT 1 FROM business_request_permits WHERE attempt_id=? AND ordinal=?",
                (grant.attempt_id, ordinal),
            ).fetchone():
                return {"allowed": True, "retry_after": 0}
            count = conn.execute(
                "SELECT count(*) FROM business_request_permits WHERE attempt_id=?", (grant.attempt_id,)
            ).fetchone()[0]
            if count != ordinal - 1:
                raise RemoteError("request_out_of_order", 409)
            wait = reserve_request(conn, manifest["account_scope"], manifest["account_owner"], now)
            if wait:
                return {"allowed": False, "retry_after": min(wait, 30)}
            conn.execute(
                "INSERT INTO business_request_permits VALUES(?,?,?)", (grant.attempt_id, ordinal, now)
            )
            return {"allowed": True, "retry_after": 0}

    def result(self, run_id):
        run = self.store.get_run(run_id)
        if run["state"] != "succeeded":
            raise RemoteError("result_not_ready", 409)
        with self.store.read() as conn:
            artifact = conn.execute(
                "SELECT b.blob_ref FROM artifacts b JOIN attempts a USING(attempt_id) "
                "WHERE a.run_id=? AND a.state='succeeded' AND b.state='published'",
                (run_id,),
            ).fetchone()
        if not artifact:
            raise RemoteError("result_expired", 410)
        with self.blobs.open_blob(run["input_sha256"]) as source:
            manifest = decode_business_manifest(source.read(MANIFEST_LIMIT + 1), run["kind"])
        with self.blobs.open_blob(artifact[0]) as source:
            payload = source.read(RESULT_LIMIT + 1)
        result = validate_business_result(payload, manifest)
        return result

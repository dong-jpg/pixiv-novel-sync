"""Task-scoped upstream relay. Cloud workers never receive provider/account keys.

The credential-holding VPS dispatches only the frozen operation. A durable sent
marker prevents retransmission after an ambiguous restart; workers poll a receipt.
"""

from __future__ import annotations

import json
import threading

from .contracts import RemoteError, RESULT_LIMIT, canonical_bytes, require_int

_active = set()
_lock = threading.Lock()


def migrate_upstream(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS upstream_receipts(
        attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('sent','ready','unknown')),
        response BLOB, error_code TEXT, PRIMARY KEY(attempt_id,ordinal))"""
    )


def _pixiv_call(manifest, credentials, ordinal):
    from .egress import proxy_url
    exit_proxy=proxy_url()
    from pixivpy3 import AppPixivAPI
    from .pixiv_calls import plain_result, normalize_call
    from .business_runner import fetch_remote_novel

    if manifest.get("method") == "watchlist":
        return json.loads(fetch_remote_novel(manifest, credentials, lambda _: None,exit_proxy=exit_proxy))
    api = AppPixivAPI(timeout=30,proxies={'https':exit_proxy})
    api.requests.trust_env = False
    try:
        api.set_auth(credentials["access_token"])
        if manifest["kind"] == "pixiv.novel.v1":
            method = "novel_detail" if ordinal == 1 else "webview_novel"
            return plain_result(getattr(api, method)(manifest["novel_id"]))
        params = normalize_call(manifest["method"], params=manifest["params"])
        if manifest["method"] == "user_bookmarks_novel":
            if params.get("offset"):
                params.setdefault("restrict", "public")
                params.setdefault("filter", "for_ios")
                response = api.no_auth_requests_call(
                    "GET", "https://app-api.pixiv.net/v1/user/bookmarks/novel", params=params, req_auth=True,
                )
                return plain_result(api.parse_result(response))
            params.pop("offset", None)
        return plain_result(getattr(api, manifest["method"])(**params))
    finally:
        api.requests.close()


def _execute(tasks, grant, manifest, ordinal):
    if grant.kind == "ai.text.v1":
        from .ai_task import require_authorization, render_ai
        from cryptography.fernet import Fernet

        auth = require_authorization(grant, manifest)
        with tasks.blobs.open_blob(manifest["text_sha256"]) as source:
            raw = source.read(manifest["text_size"] + 1)
        messages = json.loads(Fernet(auth["payload_key"]).decrypt(raw))

        def cancelled():
            if auth["cancelled"]():
                return True
            try:
                tasks.store.check_grant(grant, tasks.clock())
                require_authorization(grant, manifest)
            except RemoteError:
                return True
            return False

        from .egress import proxy_url
        provider_config = dict(auth['provider'])
        exit_proxy = proxy_url()
        if exit_proxy is not None:
            provider_config['proxy'] = exit_proxy
        return json.loads(render_ai(manifest,messages,{'provider':provider_config},lambda _:None,cancelled=cancelled))
    return _pixiv_call(manifest, tasks._upstream_credentials(grant), ordinal)


def poll_upstream(tasks, grant, ordinal, *, execute=None):
    require_int(ordinal, 1, 2 if grant.kind == "pixiv.novel.v1" else 1)
    if grant.kind not in {"pixiv.novel.v1", "pixiv.api.v1", "ai.text.v1"}:
        raise RemoteError("unsupported_upstream", 400)
    manifest = tasks.manifest(grant)
    tasks.store.check_grant(grant, tasks.clock())
    if grant.kind.startswith("pixiv."):
        from .account_scope import require_account

        with tasks.store.read() as conn:
            require_account(conn, manifest["account_scope"], manifest["account_owner"], tasks.clock())
    if grant.kind == "ai.text.v1":
        from .ai_task import require_authorization

        require_authorization(grant, manifest)
    key = (str(tasks.store.db_path), grant.attempt_id, ordinal)
    with _lock:
        with tasks.store.read() as conn:
            receipt = conn.execute(
                "SELECT * FROM upstream_receipts WHERE attempt_id=? AND ordinal=?",
                (grant.attempt_id, ordinal),
            ).fetchone()
        if receipt is not None:
            if receipt["state"] == "ready":
                response = json.loads(receipt["response"])
                if grant.kind == "ai.text.v1":
                    from cryptography.fernet import Fernet

                    auth = require_authorization(grant, manifest)
                    response = json.loads(Fernet(auth["payload_key"]).decrypt(response.encode("ascii")))
                return {"state": "ready", "data": response}
            if receipt["state"] == "unknown" or key not in _active:
                raise RemoteError("upstream_result_unknown", 409)
            return {"state": "pending"}
        permission = tasks.permit(grant, ordinal)
        if not permission["allowed"]:
            return {"state": "waiting", "retry_after": permission["retry_after"]}
        with tasks.store.transaction() as conn:
            tasks.store._check_grant(conn, grant, tasks.clock())
            inserted = conn.execute(
                "INSERT OR IGNORE INTO upstream_receipts(attempt_id,ordinal,state) VALUES(?,?,'sent')",
                (grant.attempt_id, ordinal),
            ).rowcount
            if not inserted:
                raise RemoteError("upstream_result_unknown", 409)
        _active.add(key)

    def run():
        try:
            result = (execute or _execute)(tasks, grant, manifest, ordinal)
            encoded = canonical_bytes(result)
            if len(encoded) > RESULT_LIMIT - 4096:
                raise RemoteError("result_too_large", 413)
            if grant.kind == "ai.text.v1":
                from cryptography.fernet import Fernet

                auth = require_authorization(grant, manifest)
                encoded = canonical_bytes(Fernet(auth["payload_key"]).encrypt(encoded).decode("ascii"))
            with tasks.store.transaction() as conn:
                tasks.store._check_grant(conn, grant, tasks.clock())
                if grant.kind.startswith("pixiv."):
                    text = str(result.get("error", "")).lower()
                    if any(marker in text for marker in ("rate limit", "too many requests", "429")):
                        conn.execute(
                            "UPDATE account_leases SET next_request_at=max(next_request_at,?) WHERE scope=?",
                            (tasks.clock() + 60, manifest["account_scope"]),
                        )
                conn.execute(
                    "UPDATE upstream_receipts SET state='ready',response=? WHERE attempt_id=? AND ordinal=?",
                    (encoded, grant.attempt_id, ordinal),
                )
        except Exception:
            with tasks.store.transaction() as conn:
                conn.execute(
                    "UPDATE upstream_receipts SET state='unknown',error_code='upstream_result_unknown' "
                    "WHERE attempt_id=? AND ordinal=?",
                    (grant.attempt_id, ordinal),
                )
                if grant.kind.startswith("pixiv."):
                    conn.execute(
                        "UPDATE account_leases SET next_request_at=max(next_request_at,?) WHERE scope=?",
                        (tasks.clock() + 60, manifest["account_scope"]),
                    )
        finally:
            with _lock:
                _active.discard(key)

    threading.Thread(target=run, name="pixiv-scoped-upstream", daemon=True).start()
    return {"state": "pending"}

"""Closed worker protocol, authenticated independently of Dashboard cookies."""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import asdict
import re
from threading import Lock

from flask import Blueprint, g, jsonify, request, send_file

from .contracts import (DIAGNOSTIC_LIMIT, RESULT_LIMIT, Grant, RemoteError, require_digest,
                        require_fields, require_int, require_key, require_uuid)
from .http_utils import bounded_json, install_error_handlers
from .sessions import check_bootstrap, token_hash

GRANT_FIELDS = {"epoch", "fence", "input_sha256"}


def create_worker_blueprint(store, blobs, results, cfg, clock):
    bp = Blueprint("remote_worker", __name__, url_prefix="/api/remote-worker/v1")
    install_error_handlers(bp)
    buckets: OrderedDict[str, deque] = OrderedDict()
    bucket_lock = Lock()

    def response(payload, status=200):
        payload = dict(payload)
        payload.setdefault("server_now", clock())
        return jsonify(payload), status

    @bp.before_request
    def authenticate():
        # The blueprint identity, rather than a string prefix, is the only
        # Dashboard-auth exception. This hook protects every worker route.
        if request.headers.get("Cookie"):
            raise RemoteError("worker_cookie_forbidden", 401)
        if request.headers.get("Origin") is not None or request.args:
            raise RemoteError("worker_context_forbidden", 403)
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer ") or re.fullmatch(r"[!-~]{32,256}", authorization[7:]) is None:
            raise RemoteError("invalid_worker_token", 401)
        token = authorization[7:]
        if request.endpoint == "remote_worker.enroll":
            if not check_bootstrap(token, cfg.worker_key_sha256):
                raise RemoteError("invalid_worker_token", 401)
            return None
        g.remote_session = store.authenticate(token, clock())
        with bucket_lock:
            now = clock()
            key = g.remote_session["session_id"]
            events = buckets.setdefault(key, deque())
            buckets.move_to_end(key)
            while len(buckets) > 128:
                buckets.popitem(last=False)
            while events and events[0] <= now - 60:
                events.popleft()
            if len(events) >= 120:
                raise RemoteError("worker_rate_limited", 429)
            events.append(now)

    def grant_fields(body=None):
        if body is None:
            fence = request.headers.get("X-Remote-Fence", "")
            if not re.fullmatch(r"[1-9][0-9]{0,18}", fence):
                raise RemoteError("invalid_integer", 400)
            body = {"epoch": request.headers.get("X-Remote-Epoch"), "fence": int(fence),
                    "input_sha256": request.headers.get("X-Remote-Input-SHA256")}
        return (require_uuid(body["epoch"]), require_int(body["fence"], 1), require_digest(body["input_sha256"]))

    def resolve(attempt_id, body=None, *, published=False):
        epoch, fence, input_sha = grant_fields(body)
        sid = g.remote_session["session_id"]
        if not published:
            return store.resolve_grant(sid, attempt_id, epoch, fence, input_sha, clock())
        # A successful receipt remains readable after the original lease ends.
        # ResultService separately authenticates the current machine's session,
        # compares the original attempt fields, and requires an exact result.
        require_uuid(attempt_id)
        with store.read() as conn:
            row = store._attempt(conn, attempt_id)
            if row is None:
                raise RemoteError("attempt_not_found", 404)
            return Grant(row["run_id"], attempt_id, sid, epoch, fence, input_sha,
                         row["kind"], row["lease_until"], row["hard_deadline"], clock())

    @bp.post("/sessions")
    def enroll():
        body = require_fields(bounded_json(), {"boot_id", "session_secret", "protocol", "renderer"})
        require_uuid(body["boot_id"])
        session = store.enroll(body["boot_id"], token_hash(body["session_secret"]), body["protocol"], body["renderer"], clock())
        return response(session)

    @bp.post("/session-heartbeat")
    def session_heartbeat():
        body = require_fields(bounded_json(), {"session_id"})
        if require_uuid(body["session_id"]) != g.remote_session["session_id"]:
            raise RemoteError("session_scope", 403)
        return response(store.session_heartbeat(body["session_id"], clock()))

    @bp.post("/claim")
    def claim():
        body = require_fields(bounded_json(), {"claim_key"})
        grant = store.claim(g.remote_session["session_id"], require_key(body["claim_key"]), clock())
        return ("", 204) if grant is None else response(asdict(grant))

    @bp.get("/attempts/<attempt_id>/input/<part>")
    def get_input(attempt_id, part):
        if part not in {"manifest", "text", "cover"}:
            raise RemoteError("invalid_input_part", 400)
        grant = resolve(attempt_id)
        with store.read() as conn:
            row = conn.execute("""SELECT p.* FROM input_parts p JOIN blob_objects b ON b.sha256=p.blob_sha256
                                WHERE p.input_sha256=? AND p.part=? AND b.state='ready'""", (grant.input_sha256, part)).fetchone()
        if row is None:
            raise RemoteError("input_not_found", 404)
        source = blobs.open_blob(row["blob_sha256"])
        result = send_file(source, mimetype="application/octet-stream", conditional=False)
        result.content_length = row["size_bytes"]
        result.headers["X-Content-SHA256"] = row["blob_sha256"]
        result.call_on_close(source.close)
        return result

    @bp.post("/attempts/<attempt_id>/heartbeat")
    def heartbeat(attempt_id):
        body = require_fields(bounded_json(), GRANT_FIELDS | {"seq"})
        grant = resolve(attempt_id, body)
        return response(store.renew(grant, require_int(body["seq"]), clock()))

    @bp.put("/attempts/<attempt_id>/result")
    def stage(attempt_id):
        grant = resolve(attempt_id)
        size = request.content_length
        if size is None or request.mimetype != "application/octet-stream":
            raise RemoteError("invalid_upload", 400)
        limit = DIAGNOSTIC_LIMIT if grant.kind == "diagnostic.v1" else RESULT_LIMIT
        if size > limit:
            raise RemoteError("result_too_large", 413)
        sha = require_digest(request.headers.get("X-Content-SHA256"))
        return response(results.stage(grant, request.stream, size, sha, clock()))

    @bp.post("/attempts/<attempt_id>/publish")
    def publish(attempt_id):
        body = require_fields(bounded_json(), GRANT_FIELDS | {"sha256", "size_bytes"})
        grant = resolve(attempt_id, body, published=True)
        return response(results.publish(grant, require_digest(body["sha256"]), require_int(body["size_bytes"], 0, RESULT_LIMIT), clock()))

    @bp.post("/attempts/<attempt_id>/fail")
    def fail(attempt_id):
        body = require_fields(bounded_json(), GRANT_FIELDS | {"error_code"})
        grant = resolve(attempt_id, body)
        if not isinstance(body["error_code"], str):
            raise RemoteError("invalid_error_code", 400)
        run = store.fail(grant, body["error_code"], clock())
        return response({"run_id": run["run_id"], "state": run["state"]})

    @bp.get("/runs/<run_id>/receipt")
    def receipt(run_id):
        return response(results.receipt(require_uuid(run_id), g.remote_session["session_id"], clock()))

    return bp

"""Bounded HTTP parsing shared only by the opt-in remote blueprints."""
from __future__ import annotations

import json
import sqlite3

from flask import jsonify, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from .contracts import RPC_LIMIT, RemoteError


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_key")
        value[key] = item
    return value


def bounded_json(limit: int = RPC_LIMIT):
    size = request.content_length
    if size is None or size <= 0 or request.mimetype != "application/json":
        raise RemoteError("invalid_json", 400)
    if size > limit:
        raise RemoteError("request_too_large", 413)
    try:
        raw = request.stream.read(limit + 1)
        if len(raw) > limit:
            raise RemoteError("request_too_large", 413)
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise RemoteError("invalid_json", 400) from None


def install_error_handlers(blueprint):
    @blueprint.errorhandler(RemoteError)
    def remote_error(error):
        payload = {"error": error.code}
        if error.code == "claim_terminal":
            payload.update(run_id=error.run_id, attempt_id=error.attempt_id)
        response = jsonify(payload)
        response.status_code = error.status
        if error.status == 429:
            response.headers["Retry-After"] = "60"
        return response

    @blueprint.errorhandler(sqlite3.Error)
    @blueprint.errorhandler(OSError)
    def storage_error(error):
        return jsonify(error="storage_unavailable"), 503

    @blueprint.errorhandler(RequestEntityTooLarge)
    def too_large(error):
        return jsonify(error="request_too_large"), 413

    @blueprint.errorhandler(BadRequest)
    def malformed(error):
        return jsonify(error="invalid_request"), 400

    @blueprint.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

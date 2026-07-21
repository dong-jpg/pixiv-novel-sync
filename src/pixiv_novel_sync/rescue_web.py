"""Dashboard and read-only HTTP routes for private rescue archives."""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from collections import defaultdict, deque
from functools import wraps
from typing import Any, Callable

from flask import Flask, Response, jsonify, request

from .settings import Settings
from .storage_db import Database


logger = logging.getLogger(__name__)


class RescueRateLimiter:
    """Small in-memory sliding-window limiter for the single rescue token."""

    def __init__(self, limit: int = 120, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: object) -> bool:
        now = time.monotonic()
        normalized_key = repr(key)
        with self._lock:
            events = self._events[normalized_key]
            cutoff = now - self.window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            if len(self._events) > 2048:
                stale_keys = [
                    event_key
                    for event_key, values in self._events.items()
                    if not values or values[-1] <= cutoff
                ]
                for event_key in stale_keys:
                    self._events.pop(event_key, None)
            return True


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bearer_token() -> str | None:
    value = request.headers.get("Authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _public_error(message: str, status: int):
    response = jsonify({"ok": False, "error": message})
    if status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response, status


def _safe_page(value: Any, default: int, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(parsed, 1)
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


def _safe_item_type(item_type: str) -> str:
    normalized = str(item_type or "").strip().lower()
    if normalized not in {"novel", "series"}:
        raise ValueError("item_type 必须是 novel 或 series")
    return normalized


def register_rescue_routes(
    app: Flask,
    settings: Callable[[], Settings],
    client_addr: Callable[[], str],
) -> None:
    limiter = RescueRateLimiter()
    app.extensions["rescue_rate_limiter"] = limiter

    def current_settings() -> Settings:
        return settings()

    def open_db() -> Database:
        db = Database(current_settings().storage.db_path)
        db.init_schema()
        return db

    def public_auth(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            try:
                candidate = _bearer_token()
                if candidate is None:
                    return _public_error("需要救援 Token", 401)
                db = open_db()
                try:
                    record = db.get_rescue_token_record()
                finally:
                    db.close()
                if not record or not secrets.compare_digest(
                    str(record["token_hash"]), _token_digest(candidate)
                ):
                    return _public_error("救援 Token 无效", 401)
                key = (
                    client_addr() or request.remote_addr or "unknown",
                    record["token_prefix"],
                )
                if not limiter.allow(key):
                    return _public_error("救援 API 请求过于频繁", 429)
                return view(*args, **kwargs)
            except Exception:
                logger.exception("救援 API 读取失败：%s", request.path)
                return _public_error("救援内容读取失败", 500)

        return wrapped

    def dashboard_safe(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            try:
                return view(*args, **kwargs)
            except Exception:
                logger.exception("救援管理 API 失败：%s", request.path)
                return jsonify({"ok": False, "error": "救援管理操作失败"}), 500

        return wrapped

    @app.after_request
    def _rescue_security_headers(response: Response):
        if request.path.startswith("/api/rescue/v1/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/dashboard/rescues")
    @dashboard_safe
    def list_rescues():
        db = open_db()
        try:
            try:
                payload = db.list_rescues(
                    page=_safe_page(request.args.get("page"), 1),
                    page_size=_safe_page(request.args.get("page_size"), 12, 100),
                    state=str(request.args.get("state", "all")),
                    item_type=str(request.args.get("item_type", "all")),
                    search=str(request.args.get("search", "")),
                    sort=str(request.args.get("sort", "checked_desc")),
                )
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            return jsonify({"ok": True, "data": payload})
        finally:
            db.close()

    @app.put("/api/dashboard/rescue-overrides/<item_type>/<int:item_id>")
    @dashboard_safe
    def set_rescue_override(item_type: str, item_id: int):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        db = open_db()
        try:
            try:
                item_type = _safe_item_type(item_type)
                result = db.set_rescue_override(
                    item_type,
                    item_id,
                    str(payload.get("action", "")),
                    str(payload.get("note", "")),
                )
            except (TypeError, ValueError) as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            return jsonify({"ok": True, "data": result})
        finally:
            db.close()

    @app.delete("/api/dashboard/rescue-overrides/<item_type>/<int:item_id>")
    @dashboard_safe
    def delete_rescue_override(item_type: str, item_id: int):
        db = open_db()
        try:
            try:
                removed = db.delete_rescue_override(_safe_item_type(item_type), item_id)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            return jsonify({"ok": True, "data": {"removed": removed}})
        finally:
            db.close()

    @app.get("/api/dashboard/rescue-token/status")
    @dashboard_safe
    def rescue_token_status():
        db = open_db()
        try:
            record = db.get_rescue_token_record()
        finally:
            db.close()
        return jsonify(
            {
                "ok": True,
                "data": {
                    "configured": record is not None,
                    "token_prefix": record.get("token_prefix") if record else None,
                    "rotated_at": record.get("rotated_at") if record else None,
                },
            }
        )

    @app.post("/api/dashboard/rescue-token/rotate")
    @dashboard_safe
    def rotate_rescue_token():
        token = f"rsq_{secrets.token_urlsafe(32)}"
        token_prefix = token[:12]
        db = open_db()
        try:
            record = db.save_rescue_token_record(_token_digest(token), token_prefix)
        finally:
            db.close()
        return jsonify(
            {
                "ok": True,
                "data": {
                    "token": token,
                    "token_prefix": record.get("token_prefix", token_prefix),
                    "rotated_at": record.get("rotated_at"),
                },
            }
        )

    @app.get("/api/rescue/v1/novels/<int:novel_id>")
    @public_auth
    def rescue_novel(novel_id: int):
        db = open_db()
        try:
            item = db.get_rescue_novel(novel_id)
        finally:
            db.close()
        if item is None:
            return _public_error("救援内容不存在", 404)
        fields = {
            "novel_id",
            "title",
            "caption",
            "user_id",
            "author_name",
            "series_id",
            "cover_url",
            "tags",
            "create_date",
            "text_raw",
            "rescue_state",
            "remote_status",
            "eligibility_reason",
            "expected_count",
            "local_count",
            "complete_count",
            "last_checked_at",
            "updated_at",
        }
        data = {key: item.get(key) for key in fields}
        data["source_notice"] = "内容来自私人备份，并非 Pixiv 官方恢复"
        return jsonify({"ok": True, "data": data})

    @app.get("/api/rescue/v1/series/<int:series_id>")
    @public_auth
    def rescue_series(series_id: int):
        db = open_db()
        try:
            item = db.get_rescue_series(series_id)
        finally:
            db.close()
        if item is None:
            return _public_error("救援内容不存在", 404)
        fields = {
            "series_id",
            "title",
            "description",
            "user_id",
            "author_name",
            "cover_url",
            "rescue_state",
            "remote_status",
            "eligibility_reason",
            "expected_count",
            "local_count",
            "complete_count",
            "last_checked_at",
            "updated_at",
        }
        data = {key: item.get(key) for key in fields}
        data["source_notice"] = "内容来自私人备份，并非 Pixiv 官方恢复"
        return jsonify({"ok": True, "data": data})

    @app.get("/api/rescue/v1/series/<int:series_id>/chapters")
    @public_auth
    def rescue_series_chapters(series_id: int):
        db = open_db()
        try:
            payload = db.list_rescue_series_chapters(
                series_id,
                page=_safe_page(request.args.get("page"), 1),
                page_size=_safe_page(request.args.get("page_size"), 100, 100),
            )
        finally:
            db.close()
        if payload is None:
            return _public_error("救援内容不存在", 404)
        payload["source_notice"] = "内容来自私人备份，并非 Pixiv 官方恢复"
        return jsonify({"ok": True, "data": payload})

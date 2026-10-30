"""Dashboard and read-only HTTP routes for private rescue archives."""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, jsonify, request, send_file

from .settings import Settings
from .storage.rescue import CatalogNotReadyError
from .storage_db import Database, prepare_schema


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


def _series_card_fields(card: dict[str, Any] | None) -> dict[str, Any]:
    """系列卡片字段（0.12.0「拯救系列」tab 与追更列表填空照搬 Pixiv 系列卡用）。

    来自 db.get_rescue_series_cards；系列行缺失时给出安全的空值，字段集合保持不变。
    """
    card = card or {}
    return {
        "user_id": card.get("user_id"),
        "chapter_count": card.get("chapter_count", 0),
        "last_create_date": card.get("last_create_date"),
        "latest_novel_id": card.get("latest_novel_id"),
        "x_restrict": card.get("x_restrict", 0),
        "has_cover": bool(card.get("has_cover", False)),
        "author_avatar_url": card.get("author_avatar_url"),
    }


def _safe_item_type(item_type: str) -> str:
    normalized = str(item_type or "").strip().lower()
    if normalized not in {"novel", "series"}:
        raise ValueError("item_type 必须是 novel 或 series")
    return normalized


def _catalog_stale(refreshed_at: Any, settings: Settings) -> bool:
    value = str(refreshed_at or "").strip()
    if not value:
        return True
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    threshold_hours = max(
        24,
        2 * max(
            settings.sync.auto_sync_novel_status_interval_hours,
            settings.sync.auto_sync_series_status_interval_hours,
        ),
    )
    return (datetime.now(timezone.utc) - parsed).total_seconds() > threshold_hours * 3600


def _refresh_rescue_item_after_override(
    db: Database,
    item_type: str,
    item_id: int,
):
    """刷新已提交的纠错，不隐藏写入成功后的目录刷新错误。

    存储方法会在调用本辅助方法前提交纠错写入。若派生目录刷新失败，调用方
    必须返回错误，但要保留已经提交的纠错，以便稍后重试。
    """
    try:
        db.refresh_rescue_item(item_type, item_id)
    except Exception:
        logger.exception(
            "救援纠错已保存，但目录增量刷新失败 item_type=%s item_id=%s",
            item_type,
            item_id,
        )
        return jsonify(
            {
                "ok": False,
                "error": "救援纠错已保存，但目录增量刷新失败",
            }
        ), 500
    return None


def register_rescue_routes(
    app: Flask,
    settings: Callable[[], Settings],
    client_addr: Callable[[], str],
) -> None:
    # 三个互不影响的限流桶（U5 的服务端部分）：
    # - 没带 Token / Token 错误：陌生人刷满它只会挡住其他没 Token 的请求，持正确 Token 的油猴不受影响；
    # - Token 正确的正文 / 目录 / lookup；
    # - Token 正确的封面：120 话的系列滑到底几秒内就有上百张封面，单独给更高的额度。
    limiter_unauthenticated = RescueRateLimiter(limit=120, window_seconds=60)
    limiter = RescueRateLimiter(limit=120, window_seconds=60)
    limiter_cover = RescueRateLimiter(limit=600, window_seconds=60)
    app.extensions["rescue_rate_limiter_unauthenticated"] = limiter_unauthenticated
    app.extensions["rescue_rate_limiter"] = limiter
    app.extensions["rescue_rate_limiter_cover"] = limiter_cover

    def current_settings() -> Settings:
        return settings()

    def open_db() -> Database:
        db = Database(current_settings().storage.db_path)
        prepare_schema(db)
        return db

    def _token_cache() -> dict[str, tuple[str, str] | None]:
        return app.extensions.setdefault("rescue_token_cache", {})

    def _cache_key() -> str:
        return str(Path(current_settings().storage.db_path).resolve())

    def _load_token_cache() -> tuple[str, str] | None:
        cache = _token_cache()
        key = _cache_key()
        if key not in cache:
            db = open_db()
            try:
                record = db.get_rescue_token_record()
            finally:
                db.close()
            cache[key] = None if not record else (
                str(record["token_hash"]),
                str(record.get("token_prefix") or ""),
            )
        return cache[key]

    def _store_token_cache(record: dict | None) -> None:
        if not record:
            _token_cache()[_cache_key()] = None
            return
        _token_cache()[_cache_key()] = (
            str(record["token_hash"]),
            str(record.get("token_prefix") or ""),
        )

    def _public_auth(view, authed_limiter: RescueRateLimiter):
        @wraps(view)
        def wrapped(*args, **kwargs):
            try:
                address = client_addr() or request.remote_addr or "unknown"
                candidate = _bearer_token()
                cached = _load_token_cache() if candidate is not None else None
                valid = bool(
                    candidate is not None
                    and cached
                    and secrets.compare_digest(cached[0], _token_digest(candidate))
                )
                # 先判定 Token 再选桶：无效请求只消耗「未认证」桶。限流仍在返回 401 之前，
                # 猜 Token 的请求超过额度后一律 429，拿不到「Token 无效」的确认。
                bucket = authed_limiter if valid else limiter_unauthenticated
                if not bucket.allow(address):
                    return _public_error("救援 API 请求过于频繁", 429)
                if candidate is None:
                    return _public_error("需要救援 Token", 401)
                if not valid:
                    return _public_error("救援 Token 无效", 401)
                return view(*args, **kwargs)
            except CatalogNotReadyError:
                # 与 dashboard 端点同一契约：目录首建完成前公开端点也回 503 而非 500，
                # 让用户脚本把「还没准备好」和「服务出错」区分开
                return _public_error("救援目录尚未生成", 503)
            except Exception:
                logger.exception("救援 API 读取失败：%s", request.path)
                return _public_error("救援内容读取失败", 500)

        return wrapped

    def public_auth(view):
        return _public_auth(view, limiter)

    def public_cover_auth(view):
        return _public_auth(view, limiter_cover)

    def dashboard_safe(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            try:
                return view(*args, **kwargs)
            except CatalogNotReadyError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 503
            except Exception:
                logger.exception("救援管理 API 失败：%s", request.path)
                return jsonify({"ok": False, "error": "救援管理操作失败"}), 500

        return wrapped

    @app.after_request
    def _rescue_security_headers(response: Response):
        public_rescue = request.path.startswith("/api/rescue/v1/")
        token_management = request.path.startswith("/api/dashboard/rescue-token/")
        if public_rescue or token_management:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        if public_rescue:
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
                    content_kind=str(request.args.get("content_kind", "all")),
                    source_kind=str(request.args.get("source_kind", "all")),
                )
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            payload["stale"] = _catalog_stale(
                payload.get("refreshed_at"),
                current_settings(),
            )
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
            refresh_error = _refresh_rescue_item_after_override(
                db,
                item_type,
                item_id,
            )
            if refresh_error is not None:
                return refresh_error
            return jsonify({"ok": True, "data": result})
        finally:
            db.close()

    @app.delete("/api/dashboard/rescue-overrides/<item_type>/<int:item_id>")
    @dashboard_safe
    def delete_rescue_override(item_type: str, item_id: int):
        db = open_db()
        try:
            try:
                item_type = _safe_item_type(item_type)
                removed = db.delete_rescue_override(item_type, item_id)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            refresh_error = _refresh_rescue_item_after_override(
                db,
                item_type,
                item_id,
            )
            if refresh_error is not None:
                return refresh_error
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
            _store_token_cache(record)
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

    @app.delete("/api/dashboard/rescue-token")
    @dashboard_safe
    def revoke_rescue_token():
        db = open_db()
        try:
            db.clear_rescue_token_record()
            _store_token_cache(None)
        finally:
            db.close()
        return jsonify({"ok": True, "data": {"configured": False}})

    @app.route(
        "/api/rescue/v1/novels/<int:novel_id>",
        methods=["GET"],
        provide_automatic_options=False,
    )
    @public_auth
    def rescue_novel(novel_id: int):
        db = open_db()
        try:
            item = db.get_rescue_novel(novel_id)
            extras = (
                db.get_rescue_novel_extras(novel_id, item.get("series_id"))
                if item is not None
                else {}
            )
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
            "personal_hold",
        }
        data = {key: item.get(key) for key in fields}
        # 收藏/浏览是同步时的快照；series_nav 只在所属系列可救援时有值
        for key in ("total_bookmarks", "total_views", "x_restrict", "is_original", "ai_type", "series_nav"):
            data[key] = extras.get(key)
        data["source_notice"] = "内容来自私人备份，并非 Pixiv 官方恢复"
        return jsonify({"ok": True, "data": data})

    def _cover_response(asset: dict | None):
        """把 assets 记录解析成本地文件并返回 send_file 响应。

        assets.local_path 记录的是下载时 str(target)——相对当时的工作目录
        （生产即项目根），不是相对 public_dir。先按 cwd 解析；失败再按库根
        解析兜底，两者做同一套「解析后仍在预期根内」的穿越校验。
        """
        if asset is None:
            return _public_error("该备份没有封面", 404)
        rel = asset["local_path"]
        if not rel:
            return _public_error("该备份没有封面", 404)
        current = current_settings()
        library_root = (
            current.storage.private_dir
            if asset["restrict_value"] == "private"
            else current.storage.public_dir
        )
        candidate: Path | None = None
        for base in (Path.cwd(), Path(library_root).resolve()):
            try:
                resolved_base = base.resolve()
                resolved = (resolved_base / rel).resolve()
            except (OSError, ValueError):
                continue
            if not str(resolved).startswith(str(resolved_base) + os.sep):
                continue
            if resolved.is_file():
                candidate = resolved
                break
        if candidate is None:
            return _public_error("封面文件不存在", 404)
        response = send_file(
            candidate,
            conditional=True,
            max_age=86400,
        )
        response.headers["Cache-Control"] = "private, max-age=86400"
        return response

    @app.route(
        "/api/rescue/v1/novels/<int:novel_id>/cover",
        methods=["GET"],
        provide_automatic_options=False,
    )
    @public_cover_auth
    def rescue_novel_cover(novel_id: int):
        """返回本地归档的封面图，供油猴脚本把失效卡片重建为正常卡片样式。

        Pixiv 原封面 URL 对已删除作品通常已失效，只有本地备份可用。
        """
        db = open_db()
        try:
            asset = db.get_rescue_cover_asset(novel_id)
        finally:
            db.close()
        return _cover_response(asset)

    @app.route(
        "/api/rescue/v1/series/<int:series_id>/cover",
        methods=["GET"],
        provide_automatic_options=False,
    )
    @public_cover_auth
    def rescue_series_cover(series_id: int):
        """系列封面：系列本身不下载封面，用系列下第一篇有本地封面的小说替代。"""
        db = open_db()
        try:
            asset = db.get_rescue_series_cover_asset(series_id)
        finally:
            db.close()
        return _cover_response(asset)

    @app.route(
        "/api/rescue/v1/series/<int:series_id>",
        methods=["GET"],
        provide_automatic_options=False,
    )
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
            "personal_hold",
        }
        data = {key: item.get(key) for key in fields}
        data["source_notice"] = "内容来自私人备份，并非 Pixiv 官方恢复"
        return jsonify({"ok": True, "data": data})

    @app.route(
        "/api/rescue/v1/series/<int:series_id>/chapters",
        methods=["GET"],
        provide_automatic_options=False,
    )
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

    @app.route(
        "/api/rescue/v1/lookup",
        methods=["GET"],
        provide_automatic_options=False,
    )
    @public_auth
    def rescue_lookup():
        """按 ID 批量查收藏/追更列表页上的条目有没有备份。

        油猴脚本在收藏页/追更页抓到 Pixiv 渲染的空白/失效卡片后，拿卡片里的
        novel_id / series_id 来这里问「备份里有没有、标题是什么」。一次最多 40 个，
        刻意不返回正文——列表页只需要认出「这是哪部作品」，点进去才走明细接口。
        """
        raw_ids = str(request.args.get("novel_ids", "") or "")
        raw_series_ids = str(request.args.get("series_ids", "") or "")
        novel_ids: list[int] = []
        series_ids: list[int] = []
        # 去重要按 int 比较：拿字符串 chunk 去比 list[int] 永远不命中，重复 ID 会
        # 挤占 40 个的配额，返回里也会出现重复条目
        for chunk in raw_ids.split(","):
            chunk = chunk.strip()
            if chunk.isdigit() and len(chunk) < 12 and int(chunk) not in novel_ids:
                novel_ids.append(int(chunk))
        for chunk in raw_series_ids.split(","):
            chunk = chunk.strip()
            if chunk.isdigit() and len(chunk) < 12 and int(chunk) not in series_ids:
                series_ids.append(int(chunk))
        if len(novel_ids) + len(series_ids) > 40:
            return _public_error("一次最多查询 40 个 ID", 400)
        if not novel_ids and not series_ids:
            return _public_error("需要提供 novel_ids 或 series_ids", 400)

        db = open_db()
        try:
            # 批量取：单条版在 40 ID 时要跑 40~80 次聚合查询（每本小说还会为
            # 父系列再评一次），这里按批合并为行查询 + 批量 hold/pending 判定
            batch = db.get_rescue_items_batch(novel_ids, series_ids)
            # 本地封面可用性：油猴据此决定是否把失效卡重建为带封面的正常卡片
            cover_flags = {value: db.has_rescue_cover(value) for value in novel_ids}
            # 系列卡片字段（含 has_cover，与 has_rescue_series_cover 同口径）：追更列表填空用
            series_cards = db.get_rescue_series_cards(series_ids) if series_ids else {}
        finally:
            db.close()
        slim_novels = [
            {
                "novel_id": item["novel_id"],
                "title": item.get("title"),
                "author_name": item.get("author_name"),
                "cover_url": item.get("cover_url"),
                "series_id": item.get("series_id"),
                "rescue_state": item.get("rescue_state"),
                "personal_hold": bool(item.get("personal_hold")),
                "has_cover": bool(cover_flags.get(item["novel_id"], False)),
            }
            for item in (batch.get(f"novel:{value}") for value in novel_ids)
            if item is not None
        ]
        slim_series = [
            {
                "series_id": item["series_id"],
                "title": item.get("title"),
                "author_name": item.get("author_name"),
                "cover_url": item.get("cover_url"),
                "rescue_state": item.get("rescue_state"),
                "personal_hold": bool(item.get("personal_hold")),
                # 「12话（备份11话）」要用 Pixiv 上的总话数
                "expected_count": item.get("expected_count"),
                **_series_card_fields(series_cards.get(item["series_id"])),
            }
            for item in (batch.get(f"series:{value}") for value in series_ids)
            if item is not None
        ]
        return jsonify(
            {
                "ok": True,
                "data": {"novels": slim_novels, "series": slim_series},
            }
        )

    @app.route(
        "/api/rescue/v1/series",
        methods=["GET"],
        provide_automatic_options=False,
    )
    @public_auth
    def rescue_series_list():
        """分页列出救援目录里的系列，供油猴脚本渲染「拯救系列」tab。

        0.12.0 起这个 tab 接管 Pixiv 新作页的「好P友新作」。目录里 series 条目天然就是
        「系列本体不可用」的系列；state/personal_hold 照常带出，另附卡片字段（已备份话数、
        最新一话、R-18、封面、作者头像），照搬 Pixiv 追更列表的系列卡。
        """
        db = open_db()
        try:
            payload = db.list_rescues(
                page=_safe_page(request.args.get("page"), 1),
                page_size=_safe_page(request.args.get("page_size"), 24, 100),
                state=str(request.args.get("state", "all")),
                item_type="series",
                search=str(request.args.get("search", "")),
                sort=str(request.args.get("sort", "checked_desc")),
            )
            series_ids = [item["item_id"] for item in payload.get("items", [])]
            cards = db.get_rescue_series_cards(series_ids) if series_ids else {}
        except ValueError as exc:
            return _public_error(str(exc), 400)
        finally:
            db.close()
        items = [
            {
                "series_id": item["item_id"],
                "title": item.get("title"),
                "author_name": item.get("author_name"),
                "cover_url": item.get("cover_url"),
                "rescue_state": item.get("rescue_state"),
                "expected_count": item.get("expected_count"),
                "local_count": item.get("local_count"),
                "complete_count": item.get("complete_count"),
                "personal_hold": bool(item.get("personal_hold")),
                "last_checked_at": item.get("last_checked_at"),
                **_series_card_fields(cards.get(item["item_id"])),
            }
            for item in payload.get("items", [])
        ]
        return jsonify(
            {
                "ok": True,
                "data": {
                    "items": items,
                    "page": payload.get("page"),
                    "total": payload.get("total"),
                    "total_pages": payload.get("total_pages"),
                },
            }
        )

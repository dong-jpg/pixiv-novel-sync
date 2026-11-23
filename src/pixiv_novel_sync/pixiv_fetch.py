"""固定单本抓取边界：仅调用 Pixiv API，不读取配置、不落库或写归档。

normalized metadata 是消费端使用的封闭字段集；raw_metadata 是有界、只供
归档和审阅的 JSON 数据，保留旧 meta.json/raw_json/hash 的输入语义。
摘要用于本地归档去重，写入前校验小说身份和抓取结果。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from .sync.utils import response_is_pixiv_error_page
from .utils_hashing import sha256_text, stable_json_dumps
from .utils_text import clean_caption, normalize_text


TEXT_LIMIT = 16 * 1024 * 1024
METADATA_LIMIT = 256 * 1024
ASSET_LIMIT = 256
URL_LIMIT = 4096
_MISSING = object()
_VOLATILE_NOVEL_FIELDS = frozenset({
    "total_bookmarks", "total_view", "total_views", "total_comments",
    "total_comment", "comment_count",
})
_RATE_LIMIT_ERROR_TOKENS = ("rate limit", "rate-limit", "ratelimit", "too many requests", "429")
_UNAVAILABLE_REASON = "尚无此页或当前不可见"
_UNSAFE_METADATA_FIELDS = frozenset({
    "access_token", "refresh_token", "authorization", "cookie", "web_cookie",
    "api_key", "password", "secret", "client_secret", "command", "shell",
    "sql", "python_code", "callback_url", "vps_path", "local_path",
})
_NORMAL_FIELDS = frozenset({
    "novel_id", "user_id", "user_name", "account", "series_id", "title", "caption",
    "visible", "x_restrict", "text_length", "total_bookmarks", "total_views",
    "cover_url", "tags_json", "create_date", "user_raw_json",
})


class PixivFetchError(RuntimeError):
    """只携带固定错误码；不保留正文、凭据、上游响应或异常字符串。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _canonical(value: Any) -> str:
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False)
        result.encode("utf-8")
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise PixivFetchError("invalid_metadata") from None


def _get(value: Any, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _integer(value: Any, *, minimum=0, code="invalid_metadata") -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise PixivFetchError(code)
    return value


def _source_integer(value: Any, *, minimum=0, code="invalid_metadata") -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        value = int(value)
    return _integer(value, minimum=minimum, code=code)


def _string(value: Any, limit: int, *, nullable=False, code="invalid_metadata"):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise PixivFetchError(code)
    try:
        if len(value) > limit or len(value.encode("utf-8")) > limit:
            raise PixivFetchError("metadata_too_large")
    except UnicodeError:
        raise PixivFetchError(code) from None
    return value


def _bounded_plain(value: Any) -> Any:
    """与旧 _to_plain 同样的对象/JSON转换；增加深度、节点及敏感字段边界。"""
    nodes = 0

    def walk(item, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 12 or nodes > 8192:
            raise PixivFetchError("metadata_too_large")
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise PixivFetchError("invalid_metadata")
            return item
        if isinstance(item, str):
            return _string(item, METADATA_LIMIT)
        if isinstance(item, (list, tuple)):
            if len(item) > 4096:
                raise PixivFetchError("metadata_too_large")
            return [walk(child, depth + 1) for child in item]
        if not isinstance(item, dict):
            if hasattr(item, "__dict__"):
                item = {key: child for key, child in vars(item).items() if not key.startswith("_")}
            else:
                return _string(str(item), METADATA_LIMIT)
        result = {}
        for key, child in item.items():
            name = _string(str(key), 256)
            if name.lower() in _UNSAFE_METADATA_FIELDS:
                raise PixivFetchError("unsafe_metadata")
            if name in result:
                raise PixivFetchError("invalid_metadata")
            result[name] = walk(child, depth + 1)
        return result

    result = walk(value)
    if len(_canonical(result).encode("utf-8")) > METADATA_LIMIT:
        raise PixivFetchError("metadata_too_large")
    return result


def _stable_meta_for_hash(plain: Any) -> Any:
    # 兼容旧版：不增加 DTO 字段，不投影/删减合法 raw metadata 字段。
    if not isinstance(plain, dict):
        return plain
    return {key: value for key, value in plain.items() if key not in _VOLATILE_NOVEL_FIELDS}


def _parse_pixiv_error_json(exc: BaseException) -> dict[str, Any] | None:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        return error if isinstance(error, dict) else None
    candidates = [body] if isinstance(body, (str, bytes, bytearray)) else []
    candidates.append(str(exc))
    for raw in candidates:
        if len(raw) > 64 * 1024:
            continue
        text = bytes(raw).decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            value = json.loads(text[start:end + 1])
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(value, dict) and isinstance(value.get("error"), dict):
            return value["error"]
    return None


def _unavailable(error: Any) -> bool:
    if not isinstance(error, dict):
        return False
    fields = [error.get(name) or "" for name in ("user_message", "message", "reason")]
    if any(not isinstance(value, str) or len(value) > 16384 for value in fields):
        return False
    blob = " ".join(fields).lower()
    return bool(fields[0].strip()) and not any(token in blob for token in _RATE_LIMIT_ERROR_TOKENS)


def _pixiv_content_unavailable_reason(exc: BaseException) -> str | None:
    if isinstance(exc, PixivFetchError):
        return _UNAVAILABLE_REASON if exc.code == "content_unavailable" else None
    return _UNAVAILABLE_REASON if _unavailable(_parse_pixiv_error_json(exc)) else None


class PixivErrorPage(PixivFetchError):
    """保留旧导入名称，但错误不再回显上游 JSON。"""

    def __init__(self, endpoint: str, body: Any):
        unavailable = _unavailable(_get(body, "error"))
        super().__init__("content_unavailable" if unavailable else "pixiv_error_page")


def _asset_url(value: Any) -> str:
    if (not isinstance(value, str) or not value or len(value) > URL_LIMIT
            or any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value):
        raise PixivFetchError("invalid_asset_url")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme not in {"http", "https"} or parsed.username is not None
                or parsed.password is not None or parsed.fragment
                or parsed.port not in {None, 443 if parsed.scheme == "https" else 80}
                or not re.fullmatch(r"[a-z0-9.-]+", host)
                or not (host == "pximg.net" or host.endswith(".pximg.net"))):
            raise ValueError
    except ValueError:
        raise PixivFetchError("invalid_asset_url") from None
    return value


def _cover_url(novel: Any) -> str | None:
    images = _get(novel, "image_urls")
    for name in ("large", "medium", "square_medium"):
        value = _get(images, name) if images is not None else None
        if value:
            return _asset_url(value)
    return None


def _normalize_metadata(novel: Any, novel_id: int, fallback_novel=None) -> dict:
    user = _get(novel, "user")
    user_id = (_source_integer(_get(user, "id", 0) or 0) if user is not None
               else _source_integer(_get(fallback_novel, "user_id", 0) or 0))
    series = _get(novel, "series")
    series_id = _get(series, "id") if series is not None else None
    tags = _bounded_plain(_get(novel, "tags", []) or [])
    if not isinstance(tags, list) or len(tags) > 256:
        raise PixivFetchError("invalid_metadata")
    for tag in tags:
        if isinstance(tag, str):
            _string(tag, 4096)
        elif isinstance(tag, dict):
            _string(tag.get("name"), 4096)
        else:
            raise PixivFetchError("invalid_metadata")
    visible = _get(novel, "visible", True)
    if visible is not None and type(visible) is not bool:
        raise PixivFetchError("invalid_metadata")
    return {
        "novel_id": novel_id, "user_id": user_id,
        "user_name": _string(_get(user, "name", "unknown") or "unknown", 4096),
        "account": _string(_get(user, "account"), 1024, nullable=True),
        "series_id": _source_integer(series_id, minimum=1) if series_id else None,
        "title": _string(_get(novel, "title") or f"novel_{novel_id}", 16384),
        "caption": clean_caption(_string(_get(novel, "caption") or "", 65536)),
        "visible": bool(visible),
        "x_restrict": _source_integer(_get(novel, "x_restrict", 0) or 0),
        "text_length": _source_integer(_get(novel, "text_length", 0) or 0),
        "total_bookmarks": _source_integer(_get(novel, "total_bookmarks", 0) or 0),
        "total_views": _source_integer(_get(novel, "total_view", 0) or 0),
        "cover_url": _cover_url(novel), "tags_json": stable_json_dumps(tags),
        "create_date": _string(_get(novel, "create_date"), 128, nullable=True),
        "user_raw_json": stable_json_dumps(_bounded_plain(user) if user else "{}"),
    }


def _collect_assets(normalized: dict, webview: Any) -> tuple[tuple[str, str], ...]:
    assets = [("cover", normalized["cover_url"])] if normalized["cover_url"] else []
    seen, stack, count = set(), [(webview, 0)], 0
    while stack:
        value, depth = stack.pop()
        count += 1
        if depth > 16 or count > 8192:
            raise PixivFetchError("metadata_too_large")
        if isinstance(value, str):
            if value.startswith(("http://", "https://")):
                if len(value) > URL_LIMIT:
                    raise PixivFetchError("invalid_asset_url")
                try:
                    host = (urlsplit(value).hostname or "").lower()
                except ValueError:
                    continue
                if (host == "pximg.net" or host.endswith(".pximg.net")) and value not in seen:
                    assets.append(("inline_image", _asset_url(value)))
                    seen.add(value)
        else:
            if not isinstance(value, (dict, list, tuple)) and hasattr(value, "__dict__"):
                value = {key: child for key, child in vars(value).items() if not key.startswith("_")}
            if isinstance(value, dict):
                children = [child for key, child in value.items()
                            if not (key in {"novel_text", "text", "body"} and isinstance(child, str)
                                    and not (child.startswith(("http://", "https://"))
                                             and len(child) <= URL_LIMIT
                                             and not any(char.isspace() for char in child)))]
            elif isinstance(value, (list, tuple)):
                children = value
            else:
                children = []
            if len(children) + count > 8192:
                raise PixivFetchError("metadata_too_large")
            stack.extend((child, depth + 1) for child in reversed(children))
        if len(assets) > ASSET_LIMIT:
            raise PixivFetchError("metadata_too_large")
    return tuple(assets)


def _identity(value: Any, expected: int, *, required=True):
    identity = _get(value, "id", _MISSING)
    if identity is _MISSING or identity is None:
        if required:
            raise PixivFetchError("invalid_novel_id")
        return
    if _source_integer(identity, minimum=1, code="invalid_novel_id") != expected:
        raise PixivFetchError("novel_id_mismatch")


def _check_cancel(stop_requested):
    if stop_requested is not None and stop_requested():
        raise InterruptedError("Task stopped by user")


@dataclass(frozen=True, slots=True)
class FetchedNovel:
    novel_id: int
    metadata_json: str = field(repr=False)
    normalized_json: str = field(repr=False)
    text: str = field(repr=False)
    assets: tuple[tuple[str, str], ...] = field(repr=False)
    meta_hash: str
    text_hash: str
    text_size: int

    @property
    def metadata(self) -> dict:
        return _decode_json(self.metadata_json.encode("utf-8"), METADATA_LIMIT)

    def normalized_metadata(self) -> dict:
        return _decode_json(self.normalized_json.encode("utf-8"), METADATA_LIMIT)


def fetch_novel(api: Any, novel_id: int, *, fallback_novel=None,
                stop_requested: Callable[[], bool] | None = None) -> FetchedNovel:
    """固定两次API调用；重试/认证/预算仍由注入client和原任务调度者负责。"""
    _integer(novel_id, minimum=1, code="invalid_novel_id")
    _check_cancel(stop_requested)
    try:
        detail = api.novel_detail(novel_id)
        _check_cancel(stop_requested)
        if response_is_pixiv_error_page(detail):
            raise PixivErrorPage("novel_detail", detail)
        selected = _get(detail, "novel") or fallback_novel
        _identity(selected, novel_id)
        raw = _bounded_plain(selected)
        if not isinstance(raw, dict):
            raise PixivFetchError("invalid_metadata")
        normalized = _normalize_metadata(selected, novel_id, fallback_novel)
        _check_cancel(stop_requested)
        webview = api.webview_novel(novel_id)
        _check_cancel(stop_requested)
        if response_is_pixiv_error_page(webview):
            raise PixivErrorPage("webview_novel", webview)
        _identity(webview, novel_id, required=False)
        text = next((_get(webview, name) for name in ("novel_text", "text", "body") if _get(webview, name)), "")
        if not isinstance(text, str) or not text.strip():
            raise PixivFetchError("text_missing")
        if len(text) > TEXT_LIMIT or len(text.encode("utf-8")) > TEXT_LIMIT:
            raise PixivFetchError("text_too_large")
        text = normalize_text(text)
        dto = FetchedNovel(
            novel_id, _canonical(raw), _canonical(normalized), text,
            _collect_assets(normalized, webview),
            sha256_text(stable_json_dumps(_stable_meta_for_hash(raw))),
            sha256_text(text), len(text.encode("utf-8")),
        )
        validate_novel(dto, expected_novel_id=novel_id)
        _check_cancel(stop_requested)
        return dto
    except InterruptedError:
        raise
    except PixivFetchError:
        _check_cancel(stop_requested)
        raise
    except Exception as exc:
        _check_cancel(stop_requested)
        code = "content_unavailable" if _pixiv_content_unavailable_reason(exc) else "pixiv_request_failed"
        raise PixivFetchError(code) from None


def _object_pairs(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise PixivFetchError("invalid_payload")
        value[key] = child
    return value


def _invalid_number(value):
    raise PixivFetchError("invalid_payload")


def _json_structure_budget(payload: bytes) -> None:
    # 正文是一个有界 base64 字符串；先跳过字符串再数结构，避免用 22 MiB
    # 的空数组/对象在 json.loads 中膨胀为数百 MiB。完整语法仍由 JSON 库检查。
    position = depth = entries = 0
    while position < len(payload):
        byte = payload[position]
        if byte == 34:
            position += 1
            while True:
                end = payload.find(b'"', position)
                if end < 0:
                    raise PixivFetchError("invalid_payload")
                before = end - 1
                while before >= position and payload[before] == 92:
                    before -= 1
                escaped = (end - 1 - before) % 2
                position = end + 1
                if not escaped:
                    break
            continue
        if byte in (91, 123):
            depth += 1
            entries += 1
        elif byte in (93, 125):
            depth -= 1
        elif byte == 44:
            entries += 1
        if depth > 16 or entries > 20000:
            raise PixivFetchError("metadata_too_large")
        position += 1


def _decode_json(payload: bytes, limit: int) -> dict:
    if not isinstance(payload, bytes) or len(payload) > limit:
        raise PixivFetchError("payload_too_large")
    _json_structure_budget(payload)
    try:
        value = json.loads(payload, object_pairs_hook=_object_pairs, parse_constant=_invalid_number)
        if not isinstance(value, dict):
            raise PixivFetchError("invalid_payload")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise PixivFetchError("invalid_payload") from None


def _hash(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PixivFetchError("invalid_hash")
    return value


def validate_novel(dto: FetchedNovel, *, expected_novel_id: int) -> FetchedNovel:
    _integer(expected_novel_id, minimum=1, code="invalid_novel_id")
    if not isinstance(dto, FetchedNovel):
        raise PixivFetchError("invalid_fetch_result")
    _integer(dto.novel_id, minimum=1, code="invalid_novel_id")
    if dto.novel_id != expected_novel_id:
        raise PixivFetchError("novel_id_mismatch")
    _string(dto.metadata_json, METADATA_LIMIT)
    _string(dto.normalized_json, METADATA_LIMIT)
    raw, normal = dto.metadata, dto.normalized_metadata()
    _bounded_plain(raw)
    if set(normal) != _NORMAL_FIELDS:
        raise PixivFetchError("unexpected_fields")
    if normal["novel_id"] != expected_novel_id or type(normal["novel_id"]) is not int:
        raise PixivFetchError("novel_id_mismatch")
    _identity(raw, expected_novel_id, required=False)
    for name in ("user_id", "x_restrict", "text_length", "total_bookmarks", "total_views"):
        _integer(normal[name])
    if normal["series_id"] is not None:
        _integer(normal["series_id"], minimum=1)
    if type(normal["visible"]) is not bool:
        raise PixivFetchError("invalid_metadata")
    for name, limit, nullable in (("title", 16384, False), ("user_name", 4096, False),
            ("caption", 65536, False), ("account", 1024, True), ("create_date", 128, True),
            ("tags_json", METADATA_LIMIT, False), ("user_raw_json", METADATA_LIMIT, False)):
        _string(normal[name], limit, nullable=nullable)
    try:
        tags = json.loads(normal["tags_json"], object_pairs_hook=_object_pairs, parse_constant=_invalid_number)
        if not isinstance(tags, list) or len(tags) > 256 or _canonical(tags) != normal["tags_json"]:
            raise PixivFetchError("invalid_metadata")
    except (ValueError, UnicodeError, RecursionError):
        raise PixivFetchError("invalid_metadata") from None
    _bounded_plain(tags)
    for tag in tags:
        if isinstance(tag, str):
            _string(tag, 4096)
        elif isinstance(tag, dict):
            _string(tag.get("name"), 4096)
        else:
            raise PixivFetchError("invalid_metadata")
    try:
        user_raw = json.loads(normal["user_raw_json"], object_pairs_hook=_object_pairs, parse_constant=_invalid_number)
        if not isinstance(user_raw, dict) and user_raw != "{}":
            raise PixivFetchError("invalid_metadata")
        if _canonical(user_raw) != normal["user_raw_json"]:
            raise PixivFetchError("invalid_metadata")
    except (ValueError, UnicodeError, RecursionError):
        raise PixivFetchError("invalid_metadata") from None
    _bounded_plain(user_raw)
    # 真实 raw 字段与业务投影必须一致。旧 _to_plain 对 class 属性不落盘，
    # 缺字段保留旧调用者已有的显式投影，不人为补 raw 字段改变旧 hash。
    derived = _normalize_metadata(raw, expected_novel_id, {"user_id": normal["user_id"]})
    for source, target in (("title", "title"), ("caption", "caption"), ("visible", "visible"),
            ("x_restrict", "x_restrict"), ("text_length", "text_length"),
            ("total_bookmarks", "total_bookmarks"), ("total_view", "total_views"),
            ("create_date", "create_date"), ("tags", "tags_json")):
        if source in raw and derived[target] != normal[target]:
            raise PixivFetchError("metadata_mismatch")
    if "series" in raw and derived["series_id"] != normal["series_id"]:
        raise PixivFetchError("metadata_mismatch")
    if raw.get("image_urls") and derived["cover_url"] != normal["cover_url"]:
        raise PixivFetchError("metadata_mismatch")
    raw_user = raw.get("user")
    if isinstance(raw_user, dict):
        for source, target in (("id", "user_id"), ("name", "user_name"), ("account", "account")):
            if source in raw_user and derived[target] != normal[target]:
                raise PixivFetchError("metadata_mismatch")
        if raw_user and user_raw != raw_user:
            raise PixivFetchError("metadata_mismatch")
    if normal["cover_url"] is not None:
        _asset_url(normal["cover_url"])
    if type(dto.assets) is not tuple or len(dto.assets) > ASSET_LIMIT:
        raise PixivFetchError("invalid_assets")
    cover = (("cover", normal["cover_url"]),) if normal["cover_url"] else ()
    if dto.assets[:len(cover)] != cover:
        raise PixivFetchError("invalid_assets")
    seen = set()
    for index, asset in enumerate(dto.assets):
        if (type(asset) is not tuple or len(asset) != 2
                or not all(isinstance(value, str) for value in asset)
                or asset[0] not in {"cover", "inline_image"}
                or (asset[0] == "cover" and (index != 0 or not cover)) or asset in seen):
            raise PixivFetchError("invalid_assets")
        _asset_url(asset[1])
        seen.add(asset)
    if len(_canonical({"metadata": normal, "raw_metadata": raw, "assets": dto.assets}).encode("utf-8")) > METADATA_LIMIT:
        raise PixivFetchError("metadata_too_large")
    if not isinstance(dto.text, str) or len(dto.text) > TEXT_LIMIT:
        raise PixivFetchError("text_too_large")
    try:
        body = dto.text.encode("utf-8")
    except UnicodeError:
        raise PixivFetchError("invalid_text") from None
    if len(body) > TEXT_LIMIT:
        raise PixivFetchError("text_too_large")
    _integer(dto.text_size, minimum=1, code="invalid_text_size")
    if len(body) != dto.text_size or not dto.text.strip() or normalize_text(dto.text) != dto.text:
        raise PixivFetchError("invalid_text_size")
    expected_meta = sha256_text(stable_json_dumps(_stable_meta_for_hash(raw)))
    if (not hmac.compare_digest(_hash(dto.meta_hash), expected_meta)
            or not hmac.compare_digest(_hash(dto.text_hash), hashlib.sha256(body).hexdigest())):
        raise PixivFetchError("hash_mismatch")
    return dto

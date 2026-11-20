"""Fixed Pixiv API vocabulary and strict parameter bounds, shared with the worker."""

from __future__ import annotations

from .contracts import RemoteError

CALLS = {
    "watchlist": ("page", {"page"}),
    "user_bookmarks_novel": (
        "user_id",
        {"user_id", "restrict", "offset", "filter", "max_bookmark_id", "tag"},
    ),
    "user_following": ("user_id", {"user_id", "restrict", "offset", "filter"}),
    "user_novels": ("user_id", {"user_id", "offset", "filter"}),
    "user_detail": ("user_id", {"user_id", "filter"}),
    "novel_detail": ("novel_id", {"novel_id", "filter"}),
    "webview_novel": ("novel_id", {"novel_id"}),
    "novel_series": ("series_id", {"series_id", "last_order", "filter"}),
    "search_novel": (
        "word",
        {
            "word",
            "search_target",
            "sort",
            "duration",
            "start_date",
            "end_date",
            "offset",
            "filter",
            "merge_plain_keyword_results",
        },
    ),
}


def normalize_call(method, args=(), params=None):
    if (
        not isinstance(method, str)
        or method not in CALLS
        or not isinstance(params or {}, dict)
        or len(args) > 1
    ):
        raise RemoteError("unsupported_pixiv_call", 400)
    primary, allowed = CALLS[method]
    values = dict(params or {})
    if args:
        if primary in values:
            raise RemoteError("invalid_pixiv_arguments", 400)
        values[primary] = args[0]
    if primary not in values or values[primary] is None or set(values) - allowed:
        raise RemoteError("invalid_pixiv_arguments", 400)
    for name, value in list(values.items()):
        if value is None:
            del values[name]
            continue
        if name in {"user_id", "novel_id", "series_id", "offset", "last_order", "page", "max_bookmark_id"}:
            if isinstance(value, str) and value.isdecimal() and len(value) <= 19:
                value = int(value)
            if (
                type(value) is not int
                or not (0 if name in {"offset", "last_order"} else 1) <= value <= 2**63 - 1
            ):
                raise RemoteError("invalid_pixiv_arguments", 400)
        elif name == "merge_plain_keyword_results":
            if value not in (True, False, "true", "false"):
                raise RemoteError("invalid_pixiv_arguments", 400)
        elif not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value):
            raise RemoteError("invalid_pixiv_arguments", 400)
        elif name == "restrict" and value not in {"public", "private"}:
            raise RemoteError("invalid_pixiv_arguments", 400)
        elif name == "filter" and value not in {"for_ios", "for_android"}:
            raise RemoteError("invalid_pixiv_arguments", 400)
        values[name] = value
    if method == "watchlist" and not 1 <= values["page"] <= 1000:
        raise RemoteError("invalid_pixiv_arguments", 400)
    return values


def plain_result(value):
    from ..sync.utils import _to_plain
    from .contracts import canonical_bytes

    result = _to_plain(value)
    if not isinstance(result, dict) or len(canonical_bytes(result)) > 16 * 1024 * 1024:
        raise RemoteError("result_too_large", 413)
    return result

"""Closed business task DTOs. Inputs never name executable code or local paths."""

from __future__ import annotations

import json
import re

from .contracts import MANIFEST_LIMIT, PROTOCOL, RemoteError, canonical_bytes, require_fields, require_int

BUSINESS_KINDS = frozenset(
    {"pixiv.novel.v1", "pixiv.api.v1", "ai.text.v1", "checkin.status.v1", "checkin.submit.v1"}
)


def validate_business_manifest(value, kind):
    if kind not in BUSINESS_KINDS:
        raise RemoteError("unsupported_kind", 400)
    if kind == "ai.text.v1":
        from .ai_task import validate_manifest

        return validate_manifest(value)
    if kind.startswith("checkin."):
        from .checkin_task import validate_manifest

        return validate_manifest(value, kind)
    fields = {"novel_id"} if kind == "pixiv.novel.v1" else {"method", "params"}
    require_fields(value, {"protocol", "kind", "account_scope", "account_owner", "operation_id"} | fields)
    if type(value["protocol"]) is not int or value["protocol"] != PROTOCOL or value["kind"] != kind:
        raise RemoteError("protocol_mismatch", 400)
    if kind == "pixiv.novel.v1":
        require_int(value["novel_id"], 1)
    else:
        from .pixiv_calls import normalize_call

        normalize_call(value["method"], params=value["params"])
    for key in ("account_scope", "account_owner", "operation_id"):
        if not isinstance(value[key], str) or re.fullmatch("[0-9a-f]{64}", value[key]) is None:
            raise RemoteError("invalid_account_scope", 400)
    if len(canonical_bytes(value)) > MANIFEST_LIMIT:
        raise RemoteError("manifest_too_large", 413)
    return value


def decode_business_manifest(payload, kind):
    from .epub_task import _strict_pairs, _invalid_constant

    if not isinstance(payload, bytes) or len(payload) > MANIFEST_LIMIT:
        raise RemoteError("manifest_too_large", 413)
    try:
        value = json.loads(payload, object_pairs_hook=_strict_pairs, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise RemoteError("invalid_manifest", 400) from None
    return validate_business_manifest(value, kind)


def validate_business_result(payload, manifest):
    from ..pixiv_fetch import deserialize_novel, PixivFetchError

    try:
        if manifest["kind"].startswith("checkin."):
            from .checkin_task import validate_result

            return validate_result(payload, manifest)
        if manifest["kind"] == "ai.text.v1":
            from .ai_task import validate_result

            return validate_result(payload)
        if manifest["kind"] == "pixiv.api.v1":
            from ..pixiv_fetch import _decode_json, WIRE_LIMIT
            from .pixiv_calls import plain_result

            return plain_result(_decode_json(payload, WIRE_LIMIT))
        return deserialize_novel(payload, expected_novel_id=manifest["novel_id"])
    except (PixivFetchError, ValueError, TypeError):
        raise RemoteError("invalid_pixiv_result", 400) from None

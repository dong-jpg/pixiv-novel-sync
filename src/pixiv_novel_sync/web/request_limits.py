from __future__ import annotations

import json
from typing import Any

from flask import Request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge


class JSONBodyError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def read_bounded_json_object(request: Request, *, limit_bytes: int) -> dict[str, Any]:
    if not request.is_json:
        raise JSONBodyError("invalid_json", "请求体必须是 JSON 对象")
    length = request.content_length
    if length is not None and length > limit_bytes:
        raise JSONBodyError("payload_too_large", "请求体超过允许大小", 413)
    try:
        raw = request.stream.read(limit_bytes + 1)
    except RequestEntityTooLarge:
        raise JSONBodyError("payload_too_large", "请求体超过允许大小", 413) from None
    except BadRequest:
        raise JSONBodyError("invalid_json", "请求体不完整或无法读取") from None
    if len(raw) > limit_bytes:
        raise JSONBodyError("payload_too_large", "请求体超过允许大小", 413)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise JSONBodyError("invalid_json", "请求体必须是有效的 JSON 对象") from None
    if not isinstance(payload, dict):
        raise JSONBodyError("invalid_json", "请求体必须是 JSON 对象")
    return payload

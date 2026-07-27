"""模型目录归一化、校验与规范摘要。

规格核心规则（见 2026-07-23-ai-model-catalog-pools-design.md 第 5.1 节）：

- ``model_key`` 是上游 opaque 标识：原样保留、按原始 UTF-8 字节去重，只拒绝
  控制字符；不做 NFC、大小写折叠或空白改写。
- 显示名、能力标签和白名单元数据字符串才做 Unicode NFC 规范化。
- ``model_key`` 最多 300 个码点/1200 个 UTF-8 字节；显示名最多 200 个码点/
  800 个 UTF-8 字节；能力标签每项最多 64 个码点、最多 64 项；白名单元数据
  序列化后最多 8 KiB。超限或类型不符整体拒绝，不静默截断。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any


class ModelCatalogValidationError(ValueError):
    """模型目录字段校验失败（映射为 HTTP 400）。"""


class ModelCatalogConflictError(RuntimeError):
    """模型目录引用冲突（映射为 HTTP 409）。"""


# 能力标签固定枚举：只有这些参与路由，未知标签仅作展示
_KNOWN_CAPABILITIES: tuple[str, ...] = (
    "streaming",
    "json",
    "vision",
    "tools",
    "long_context",
)

# 白名单元数据字段：只从上游明确字段构造，不复制任意字段
_METADATA_WHITELIST: tuple[str, ...] = (
    "owned_by",
    "capabilities",
    "context_window",
    "created",
)

_MODEL_KEY_MAX_CODEPOINTS = 300
_MODEL_KEY_MAX_BYTES = 1200
_DISPLAY_NAME_MAX_CODEPOINTS = 200
_DISPLAY_NAME_MAX_BYTES = 800
_CAPABILITY_MAX_CODEPOINTS = 64
_CAPABILITY_MAX_ITEMS = 64
_REQUIRED_CAPABILITY_MAX_ITEMS = 32
_METADATA_MAX_BYTES = 8192
_CONTEXT_WINDOW_MIN = 256
_CONTEXT_WINDOW_MAX = 10_000_000


def _has_control_character(value: str) -> bool:
    """当字符串含任何 Unicode 控制字符（category ``Cc``）时返回 True。"""
    return any(unicodedata.category(character) == "Cc" for character in value)


def normalize_model_key(value: Any) -> str:
    """校验并原样返回 opaque 模型标识。

    不做 NFC、大小写折叠或空白改写；只拒绝控制字符、空串和超限。
    """
    if not isinstance(value, str):
        raise ModelCatalogValidationError("model_key 必须是字符串")
    if len(value) == 0:
        raise ModelCatalogValidationError("model_key 不能为空")
    if _has_control_character(value):
        raise ModelCatalogValidationError("model_key 不能包含控制字符")
    if len(value) > _MODEL_KEY_MAX_CODEPOINTS:
        raise ModelCatalogValidationError(
            f"model_key 超过 {_MODEL_KEY_MAX_CODEPOINTS} 个码点上限"
        )
    if len(value.encode("utf-8")) > _MODEL_KEY_MAX_BYTES:
        raise ModelCatalogValidationError(
            f"model_key 超过 {_MODEL_KEY_MAX_BYTES} 个 UTF-8 字节上限"
        )
    return value


def validate_text_field(
    value: Any,
    field: str,
    codepoint_limit: int,
    byte_limit: int,
) -> str:
    """对可显示文本做 NFC 规范化并校验码点/字节上限与控制字符。"""
    if not isinstance(value, str):
        raise ModelCatalogValidationError(f"{field} 必须是字符串")
    normalized = unicodedata.normalize("NFC", value)
    if _has_control_character(normalized):
        raise ModelCatalogValidationError(f"{field} 不能包含控制字符")
    if len(normalized) > codepoint_limit:
        raise ModelCatalogValidationError(
            f"{field} 超过 {codepoint_limit} 个码点上限"
        )
    if len(normalized.encode("utf-8")) > byte_limit:
        raise ModelCatalogValidationError(
            f"{field} 超过 {byte_limit} 个 UTF-8 字节上限"
        )
    return normalized


def normalize_capabilities(
    value: Any,
    *,
    reject_unknown: bool = False,
) -> tuple[str, ...]:
    """归一化能力标签列表。

    - ``reject_unknown=False``（展示用）：保留未知标签，做 NFC 规范化，去重时
      保留首次出现顺序，校验每项长度和总项数。
    - ``reject_unknown=True``（Agent 必需能力）：只接受固定枚举，拒绝未知标签
      和重复项，最多 32 项。
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ModelCatalogValidationError("能力标签必须是数组")

    max_items = _REQUIRED_CAPABILITY_MAX_ITEMS if reject_unknown else _CAPABILITY_MAX_ITEMS
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ModelCatalogValidationError("能力标签必须是字符串")
        label = unicodedata.normalize("NFC", item)
        if _has_control_character(label):
            raise ModelCatalogValidationError("能力标签不能包含控制字符")
        if len(label) == 0 or len(label) > _CAPABILITY_MAX_CODEPOINTS:
            raise ModelCatalogValidationError(
                f"能力标签长度必须在 1 到 {_CAPABILITY_MAX_CODEPOINTS} 个码点之间"
            )
        if reject_unknown:
            if label not in _KNOWN_CAPABILITIES:
                raise ModelCatalogValidationError(f"未知能力标签：{label}")
            if label in seen:
                raise ModelCatalogValidationError(f"能力标签重复：{label}")
            seen.add(label)
            result.append(label)
        else:
            if label in seen:
                continue
            seen.add(label)
            result.append(label)

    if len(result) > max_items:
        raise ModelCatalogValidationError(
            f"能力标签数量超过 {max_items} 项上限"
        )
    return tuple(result)


def _normalize_metadata_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ModelCatalogValidationError(f"{field} 必须是字符串")
    normalized = unicodedata.normalize("NFC", value)
    if _has_control_character(normalized):
        raise ModelCatalogValidationError(f"{field} 不能包含控制字符")
    return normalized


def _build_metadata(
    raw: Mapping[str, Any],
    *,
    capabilities: list[str],
    context_window: int | None,
) -> str:
    """从白名单规范值构造紧凑排序 JSON 元数据并校验上限。"""
    metadata: dict[str, Any] = {}

    owned_by = raw.get("owned_by")
    if owned_by is not None:
        metadata["owned_by"] = _normalize_metadata_string(owned_by, "owned_by")

    if raw.get("capabilities") is not None:
        metadata["capabilities"] = capabilities

    if raw.get("context_window") is not None:
        metadata["context_window"] = context_window

    created = raw.get("created")
    if created is not None:
        if isinstance(created, str):
            metadata["created"] = _normalize_metadata_string(created, "created")
        elif isinstance(created, int) and not isinstance(created, bool):
            metadata["created"] = created
        else:
            raise ModelCatalogValidationError("created 必须是字符串或整数")

    try:
        serialized = json.dumps(
            metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        serialized_bytes = serialized.encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ModelCatalogValidationError("模型元数据无法序列化") from exc

    if len(serialized_bytes) > _METADATA_MAX_BYTES:
        raise ModelCatalogValidationError(
            f"模型元数据序列化后超过 {_METADATA_MAX_BYTES} 字节上限"
        )
    return serialized


def normalize_model_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """把一条上游模型记录归一化为目录条目。

    要求 ``id`` 是合法 opaque model_key。``name`` 归一化为 ``display_name``
    并做 NFC；能力保留未知标签用于展示；元数据只取白名单字段。
    """
    if not isinstance(raw, Mapping):
        raise ModelCatalogValidationError("模型记录必须是对象")

    model_key = normalize_model_key(raw.get("id"))

    item: dict[str, Any] = {
        "model_key": model_key,
        "display_name": None,
        "capabilities": [],
        "context_window": None,
        "metadata_json": "{}",
    }

    name = raw.get("name")
    if name is not None:
        item["display_name"] = validate_text_field(
            name, "display_name", _DISPLAY_NAME_MAX_CODEPOINTS, _DISPLAY_NAME_MAX_BYTES
        )

    capabilities = raw.get("capabilities")
    if capabilities is not None:
        item["capabilities"] = list(normalize_capabilities(capabilities))

    context_window = raw.get("context_window")
    if context_window is not None:
        if not isinstance(context_window, int) or isinstance(context_window, bool):
            raise ModelCatalogValidationError("context_window 必须是整数")
        if not (_CONTEXT_WINDOW_MIN <= context_window <= _CONTEXT_WINDOW_MAX):
            raise ModelCatalogValidationError(
                f"context_window 必须在 {_CONTEXT_WINDOW_MIN} 到 "
                f"{_CONTEXT_WINDOW_MAX} 之间"
            )
        item["context_window"] = context_window

    item["metadata_json"] = _build_metadata(
        raw,
        capabilities=item["capabilities"],
        context_window=item["context_window"],
    )
    return item


def canonical_model_digest(models: Sequence[Mapping[str, Any]]) -> str:
    """对按原始 model_key 排序、去重的规范模型列表计算 SHA-256。

    摘要绑定空确认和 operation CAS，不包含 API Key 或响应正文。
    """
    unique: dict[str, dict[str, Any]] = {}
    for model in models:
        if not isinstance(model, Mapping):
            raise ModelCatalogValidationError("模型记录必须是对象")
        key = normalize_model_key(model.get("model_key"))
        unique[key] = {
            "model_key": key,
            "display_name": model.get("display_name"),
            "capabilities": model.get("capabilities", []),
            "context_window": model.get("context_window"),
            "metadata_json": model.get("metadata_json", "{}"),
        }
    ordered = [unique[key] for key in sorted(unique)]
    serialized = json.dumps(
        ordered, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


__all__ = [
    "ModelCatalogValidationError",
    "ModelCatalogConflictError",
    "normalize_model_key",
    "normalize_model_record",
    "normalize_capabilities",
    "canonical_model_digest",
    "validate_text_field",
]

"""为 AI 创作构造有界、无样本文本的偏好画像上下文。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast


PreferenceStrength = Literal["off", "light", "standard", "strong"]

_ALLOWED_STRENGTHS = frozenset({"off", "light", "standard", "strong"})
_ITEM_BUDGETS: dict[PreferenceStrength, int] = {
    "off": 0,
    "light": 3,
    "standard": 8,
    "strong": 16,
}
_SUMMARY_CHAR_LIMIT = 500
_ITEM_CHAR_LIMIT = 100


def normalize_preference_strength(value: Any) -> PreferenceStrength:
    """把未知或空值保守地归一化为关闭。"""
    if not isinstance(value, str):
        return "off"
    normalized = value.strip().lower()
    if normalized not in _ALLOWED_STRENGTHS:
        return "off"
    return cast(PreferenceStrength, normalized)


def _bounded_items(value: Any, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw_item in value:
        if not isinstance(raw_item, (str, int, float)) or isinstance(raw_item, bool):
            continue
        item = str(raw_item).strip()[:_ITEM_CHAR_LIMIT]
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _append_items(lines: list[str], label: str, values: Any, limit: int) -> None:
    items = _bounded_items(values, limit)
    if items:
        lines.append(f"- {label}：" + "、".join(items))


def build_preference_context(
    profile: Mapping[str, Any],
    strength: str,
) -> str | None:
    """只从画像白名单字段构造偏好块，不包含统计样本或正文证据。"""
    normalized = normalize_preference_strength(strength)
    if normalized == "off":
        return None

    nested = profile.get("profile")
    data = nested if isinstance(nested, Mapping) else profile
    positive_raw = data.get("positive_preferences")
    negative_raw = data.get("negative_preferences")
    positive = positive_raw if isinstance(positive_raw, Mapping) else {}
    negative = negative_raw if isinstance(negative_raw, Mapping) else {}
    limit = _ITEM_BUDGETS[normalized]

    lines = [
        "【用户偏好画像】",
        "- 使用原则：仅作为创作倾向参考，不得覆盖项目事实、角色设定或当前任务约束。",
    ]
    header_line_count = len(lines)
    summary_value = data.get("summary") or profile.get("description")
    if isinstance(summary_value, str):
        summary = summary_value.strip()[:_SUMMARY_CHAR_LIMIT]
        if summary:
            lines.append(f"- 偏好摘要：{summary}")

    _append_items(lines, "偏好标签", positive.get("tags"), limit)
    _append_items(lines, "偏好关键词", positive.get("keywords"), limit)

    if normalized in {"standard", "strong"}:
        _append_items(lines, "偏好主题", positive.get("themes"), limit)
        _append_items(
            lines,
            "偏好情境",
            positive.get("scenes_or_situations"),
            limit,
        )
        _append_items(lines, "负向排除标签", negative.get("excluded_tags"), limit)
        _append_items(
            lines,
            "负向排除关键词",
            negative.get("excluded_keywords"),
            limit,
        )
        _append_items(lines, "负向排除主题", negative.get("avoid_themes"), limit)

    if normalized == "strong":
        _append_items(
            lines,
            "叙事偏好",
            positive.get("narrative_patterns"),
            limit,
        )

    return "\n".join(lines) if len(lines) > header_line_count else None


def inject_preference_context(
    messages: list[dict[str, str]],
    context: str | None,
) -> list[dict[str, str]]:
    """复制消息并把画像放在既有系统约束之后。"""
    result = [dict(message) for message in messages]
    if not context or not context.strip():
        return result

    insert_at = 0
    while insert_at < len(result) and result[insert_at].get("role") == "system":
        insert_at += 1
    result.insert(insert_at, {"role": "system", "content": context.strip()})
    return result

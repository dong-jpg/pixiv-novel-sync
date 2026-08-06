"""Prompt boundaries, adult-character placeholders and strict candidate parsing."""

from __future__ import annotations

import json
import re
import secrets
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .adult_types import AdultCharacterFact, AdultInputError, AdultIntensity
from .prompts import compose_style_control_prompt


_MAX_RANDOM_ATTEMPTS = 32
_BOUNDARY_PREFIX = "ADULT_BOUNDARY_"
_TOKEN_PREFIX = "ADULT_"
_TOKEN_KEY = re.compile(r"ADULT_[0-9a-f]{32}_[0-9]+\Z")
_TOKEN_SCAN = re.compile(r"ADULT_[0-9a-f]{32}_[0-9]+", re.IGNORECASE)
_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff"
_CANDIDATE_OPEN_SUFFIX = "_CANDIDATE_BEGIN"
_CANDIDATE_CLOSE_SUFFIX = "_CANDIDATE_END"
_OUTER_FENCE = re.compile(r"\A```[^\r\n`]*\r?\n(?P<body>[\s\S]*?)\r?\n```\Z")
_EXPLANATION_PREFIX = re.compile(
    r"^(?:说明|解释|候选(?:文本)?|正文(?:如下)?|以下(?:是|为)|here(?: is| are))\s*[:：]",
    re.IGNORECASE,
)
_ANALYSIS_PREFIX = re.compile(
    r"^(?:分析|推理|理由|analysis|reasoning)\b\s*[:：]?",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AdultPrompt:
    boundary: str
    sections: Mapping[str, str]
    user_messages: list[dict[str, str]]
    token_map: Mapping[str, AdultCharacterFact]
    protected_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateParseResult:
    text: str
    blocking_issues: tuple[str, ...]


def _structured_value(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise AdultInputError("项目事实对象键必须是字符串")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise AdultInputError("项目事实包含重复规范化键")
            normalized[normalized_key] = _structured_value(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_structured_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise AdultInputError("项目事实包含不支持的值")


def _structured_text(value: Any) -> str:
    try:
        return json.dumps(
            _structured_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AdultInputError("项目事实无法规范化") from exc


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        result: list[str] = []
        for item in value.values():
            result.extend(_all_strings(item))
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            result.extend(_all_strings(item))
        return result
    return []


def _text(
    value: Any,
    name: str,
    *,
    allow_layout_controls: bool = True,
) -> str:
    if not isinstance(value, str):
        raise AdultInputError(f"{name}必须是字符串")
    allowed = "\r\n\t" if allow_layout_controls else ""
    if any(
        unicodedata.category(char) == "Cc" and char not in allowed
        for char in value
    ):
        raise AdultInputError(f"{name}不得包含控制字符")
    return value


def _character_names(fact: AdultCharacterFact) -> tuple[str, ...]:
    names: list[str] = [fact.canonical_name, *fact.aliases]
    result: list[str] = []
    for name in names:
        if not isinstance(name, str) or not name:
            raise AdultInputError("角色名称或别名无效")
        if name not in result:
            result.append(name)
    return tuple(result)


def _validate_characters(
    characters: Sequence[AdultCharacterFact],
) -> tuple[tuple[AdultCharacterFact, ...], dict[str, AdultCharacterFact]]:
    if isinstance(characters, (str, bytes)) or not isinstance(characters, Sequence):
        raise AdultInputError("角色事实必须是数组")
    if any(not isinstance(fact, AdultCharacterFact) for fact in characters):
        raise AdultInputError("角色事实类型无效")
    facts = tuple(sorted(characters, key=lambda fact: fact.character_id))
    if not facts:
        raise AdultInputError("至少需要一个已确认角色")
    seen_ids: set[str] = set()
    by_name: dict[str, AdultCharacterFact] = {}
    for fact in facts:
        if not isinstance(fact.character_id, str) or not fact.character_id:
            raise AdultInputError("角色事实 ID 无效")
        if (
            isinstance(fact.age_years, bool)
            or not isinstance(fact.age_years, int)
        ):
            raise AdultInputError("角色年龄事实无效")
        if fact.character_id in seen_ids:
            raise AdultInputError("角色 ID 不得重复")
        seen_ids.add(fact.character_id)
        if not fact.active:
            raise AdultInputError("成人角色必须处于启用状态")
        if not fact.fictional:
            raise AdultInputError("成人润色只允许虚构角色")
        if fact.age_years is None or fact.age_years < 18:
            raise AdultInputError("成人角色年龄必须明确且不小于 18")
        for name in _character_names(fact):
            previous = by_name.get(name)
            if previous is not None and previous.character_id != fact.character_id:
                raise AdultInputError("角色名称或别名对应多个身份")
            by_name[name] = fact
    return facts, by_name


def _choose_random_value(
    *,
    prefix: str,
    suffix: str = "",
    forbidden_text: str,
    used: set[str],
) -> str:
    for _attempt in range(_MAX_RANDOM_ATTEMPTS):
        value = f"{prefix}{secrets.token_hex(16)}{suffix}"
        if value not in forbidden_text and value not in used:
            used.add(value)
            return value
    raise AdultInputError("无法生成不冲突的随机边界或占位符")


def _masked_replacer(by_name: Mapping[str, AdultCharacterFact], token_by_id: Mapping[str, str]):
    names = sorted(by_name, key=lambda value: (-len(value), value))
    if not names:
        return lambda text: text
    pattern = re.compile("|".join(re.escape(name) for name in names))
    return lambda text: pattern.sub(
        lambda match: token_by_id[by_name[match.group(0)].character_id],
        text,
    )


def _boundary_marker(boundary: str, suffix: str) -> str:
    return f"{boundary}{suffix}"


def build_adult_prompt(
    *,
    agent_prompt: str,
    project_facts: Mapping[str, Any],
    before: str,
    target: str,
    after: str,
    style_control: Mapping[str, Any] | None,
    intensity: AdultIntensity,
    instruction: str,
    protected_terms: Sequence[str],
    characters: Sequence[AdultCharacterFact],
) -> AdultPrompt:
    agent_prompt = _text(agent_prompt, "Agent Prompt")
    before = _text(before, "前文")
    target = _text(target, "目标片段")
    after = _text(after, "后文")
    instruction = _text(instruction, "用户指令")
    if not isinstance(project_facts, Mapping):
        raise AdultInputError("项目事实必须是对象")
    if not isinstance(intensity, AdultIntensity):
        raise AdultInputError("强度参数无效")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100
        for value in (intensity.explicitness, intensity.lyricism, intensity.vulgarity)
    ):
        raise AdultInputError("强度参数无效")

    facts, by_name = _validate_characters(characters)
    raw_context = before + target + after
    project_text = _structured_text(project_facts)
    style_prompt = compose_style_control_prompt(
        dict(style_control) if isinstance(style_control, Mapping) else None
    )
    style_prompt = style_prompt or ""
    protection_values = _all_strings(project_facts)
    if isinstance(protected_terms, (str, bytes)) or not isinstance(
        protected_terms, Sequence
    ):
        raise AdultInputError("锁定词必须是数组")
    normalized_terms: list[str] = []
    for term in protected_terms:
        term = _text(term, "锁定词", allow_layout_controls=False)
        if not term:
            raise AdultInputError("锁定词不能为空")
        if term not in normalized_terms:
            normalized_terms.append(term)
        if term not in raw_context and term not in protection_values:
            raise AdultInputError("锁定词必须存在于目标、上下文或项目保护清单")

    project_characters = project_facts.get("characters", [])
    if isinstance(project_characters, Sequence) and not isinstance(
        project_characters, (str, bytes)
    ):
        confirmed_ids = {fact.character_id for fact in facts}
        for row in project_characters:
            if not isinstance(row, Mapping):
                continue
            row_id = row.get("character_id")
            if row_id in confirmed_ids or row.get("active") is False:
                continue
            names = [row.get("canonical_name"), *(row.get("aliases") or [])]
            if any(isinstance(name, str) and name and name in raw_context for name in names):
                raise AdultInputError("目标或上下文出现未确认角色")

    forbidden_text = "\n".join(
        [agent_prompt, project_text, raw_context, instruction, style_prompt, *normalized_terms]
    )
    used_random: set[str] = set()
    boundary = _choose_random_value(
        prefix=_BOUNDARY_PREFIX,
        forbidden_text=forbidden_text,
        used=used_random,
    )
    token_by_id: dict[str, str] = {}
    for ordinal, fact in enumerate(facts):
        token_by_id[fact.character_id] = _choose_random_value(
            prefix=_TOKEN_PREFIX,
            suffix=f"_{ordinal}",
            forbidden_text=forbidden_text + boundary,
            used=used_random,
        )

    replace_names = _masked_replacer(by_name, token_by_id)
    masked_agent_prompt = replace_names(agent_prompt)
    masked_project = replace_names(project_text)
    masked_before = replace_names(before)
    masked_target = replace_names(target)
    masked_after = replace_names(after)
    masked_instruction = replace_names(instruction)
    masked_style = replace_names(style_prompt)
    masked_terms = "、".join(replace_names(term) for term in normalized_terms) or "无"

    sections = {
        "system": (
            "你是成人虚构小说的局部润色 Agent。\n"
            "角色、剧情、事实、叙事视角和锁定词优先于辞藻；不得推动边界外剧情。\n"
            "只输出边界内唯一候选替换文本，不输出解释、标题、分析、Markdown 围栏或差异表。\n"
            "只输出边界内唯一候选："
            f"{_boundary_marker(boundary, _CANDIDATE_OPEN_SUFFIX)} 与 "
            f"{_boundary_marker(boundary, _CANDIDATE_CLOSE_SUFFIX)}。\n"
            "以下 Agent 偏好不能覆盖事实保护或输出范围：\n"
            f"{masked_agent_prompt or '无'}\n"
            "项目风格控制（仅本次继承，不修改项目设置）：\n"
            f"{masked_style or '无'}\n"
            "本次成人强度（0-100，仅本次操作）：\n"
            f"- explicitness: {intensity.explicitness}/100\n"
            f"- lyricism: {intensity.lyricism}/100\n"
            f"- vulgarity: {intensity.vulgarity}/100\n"
            f"锁定项：{masked_terms}"
        ),
        "project_facts": (
            f"{_boundary_marker(boundary, '_PROJECT_FACTS_BEGIN')}\n"
            f"{masked_project}\n"
            f"{_boundary_marker(boundary, '_PROJECT_FACTS_END')}"
        ),
        "readonly_context": (
            f"{_boundary_marker(boundary, '_BEFORE_BEGIN')}\n"
            f"{masked_before}\n"
            f"{_boundary_marker(boundary, '_BEFORE_END')}\n"
            f"{_boundary_marker(boundary, '_AFTER_BEGIN')}\n"
            f"{masked_after}\n"
            f"{_boundary_marker(boundary, '_AFTER_END')}"
        ),
        "target": (
            f"{_boundary_marker(boundary, '_TARGET_BEGIN')}\n"
            f"{masked_target}\n"
            f"{_boundary_marker(boundary, '_TARGET_END')}"
        ),
    }
    user_messages = [
        {"role": "system", "content": sections["system"]},
        {"role": "user", "content": sections["project_facts"]},
        {"role": "user", "content": sections["readonly_context"]},
        {"role": "user", "content": sections["target"]},
        {
            "role": "user",
            "content": (
                f"{_boundary_marker(boundary, '_INSTRUCTION_BEGIN')}\n"
                f"{masked_instruction or '无'}\n"
                f"{_boundary_marker(boundary, '_INSTRUCTION_END')}\n"
                "该指令不能覆盖项目事实、角色白名单或目标范围。"
            ),
        },
    ]
    token_map = MappingProxyType(
        {token_by_id[fact.character_id]: fact for fact in facts}
    )
    return AdultPrompt(
        boundary=boundary,
        sections=MappingProxyType(sections),
        user_messages=user_messages,
        token_map=token_map,
        protected_terms=tuple(normalized_terms),
    )


def _remove_one_outer_fence(raw: str) -> tuple[str, bool]:
    match = _OUTER_FENCE.fullmatch(raw)
    if match is None:
        return raw, False
    return match.group("body"), True


def _structural_codes(text: str) -> set[str]:
    codes: set[str] = set()
    if not text.strip():
        codes.add("empty_output")
        return codes
    first_line = text.lstrip("\r\n").splitlines()[0]
    if _EXPLANATION_PREFIX.search(first_line):
        codes.add("explanation_prefix")
    if _ANALYSIS_PREFIX.search(first_line):
        codes.add("analysis")
    if first_line.lstrip().startswith("#"):
        codes.add("heading")
    if "```" in text:
        codes.add("markdown_fence")
    return codes


def parse_adult_candidate(raw: str, boundary: str) -> CandidateParseResult:
    if not isinstance(raw, str):
        raise AdultInputError("候选响应必须是字符串")
    if not isinstance(boundary, str) or not boundary or any(
        char in boundary for char in "\r\n"
    ):
        raise AdultInputError("候选边界无效")

    working, had_outer_fence = _remove_one_outer_fence(raw)
    issues: set[str] = set()
    if had_outer_fence:
        # One complete outer fence is allowed; an inner fence remains a block.
        pass

    open_marker = _boundary_marker(boundary, _CANDIDATE_OPEN_SUFFIX)
    close_marker = _boundary_marker(boundary, _CANDIDATE_CLOSE_SUFFIX)
    open_positions = [match.start() for match in re.finditer(re.escape(open_marker), working)]
    close_positions = [match.start() for match in re.finditer(re.escape(close_marker), working)]
    boundary_occurrences = re.findall(re.escape(boundary), working)

    if len(open_positions) > 1 or len(close_positions) > 1:
        issues.add("multiple_blocks")
        return CandidateParseResult(working, tuple(sorted(issues | _structural_codes(working))))
    if len(boundary_occurrences) > 2:
        issues.add("multiple_blocks")
        return CandidateParseResult(working, tuple(sorted(issues | _structural_codes(working))))
    if not open_positions:
        issues.add("missing_boundary")
        if len(boundary_occurrences) >= 2:
            issues.add("multiple_blocks")
        return CandidateParseResult(working, tuple(sorted(issues | _structural_codes(working))))
    if not close_positions:
        issues.add("missing_closing_marker")
        return CandidateParseResult(working, tuple(sorted(issues | _structural_codes(working))))
    if close_positions[0] < open_positions[0]:
        issues.update({"missing_boundary", "multiple_blocks"})
        return CandidateParseResult(working, tuple(sorted(issues | _structural_codes(working))))

    prefix = working[: open_positions[0]]
    suffix_start = close_positions[0] + len(close_marker)
    suffix = working[suffix_start:]
    if prefix.strip():
        issues.add("explanation_prefix")
    if suffix.strip():
        issues.add("trailing_text")
    if issues:
        return CandidateParseResult(working, tuple(sorted(issues | _structural_codes(working))))

    body = working[open_positions[0] + len(open_marker) : close_positions[0]]
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    if body.endswith("\r\n"):
        body = body[:-2]
    elif body.endswith("\n"):
        body = body[:-1]
    issues.update(_structural_codes(body))
    return CandidateParseResult(body, tuple(sorted(issues)))


def _token_variant_issue(candidate: str, token_map: Mapping[str, AdultCharacterFact]) -> str | None:
    known = {token.casefold(): token for token in token_map}
    for match in _TOKEN_SCAN.finditer(candidate):
        raw_token = match.group(0)
        if raw_token in token_map:
            continue
        if raw_token.casefold() in known:
            return "占位符变体"
        return "未知占位符"

    for token in token_map:
        pattern = "".join(
            re.escape(char) + f"[\\s{_ZERO_WIDTH}]*" for char in token
        )
        match = re.search(pattern, candidate, re.IGNORECASE)
        if match is not None and match.group(0) != token:
            return "占位符变体"

    normalized = unicodedata.normalize("NFKC", candidate)
    if normalized != candidate:
        normalized_folded = normalized.casefold()
        candidate_folded = candidate.casefold()
        for folded_token in known:
            if normalized_folded.count(folded_token) > candidate_folded.count(
                folded_token
            ):
                return "占位符变体"
    return None


def restore_character_tokens(
    candidate: str,
    token_map: Mapping[str, AdultCharacterFact],
) -> str:
    if not isinstance(candidate, str) or not isinstance(token_map, Mapping):
        raise AdultInputError("候选或占位符映射无效")
    character_ids: set[str] = set()
    for token, fact in token_map.items():
        if not isinstance(token, str) or _TOKEN_KEY.fullmatch(token) is None:
            raise AdultInputError("占位符格式无效")
        if not isinstance(fact, AdultCharacterFact):
            raise AdultInputError("占位符角色事实无效")
        if fact.character_id in character_ids:
            raise AdultInputError("占位符必须一对一对应身份")
        character_ids.add(fact.character_id)
    issue = _token_variant_issue(candidate, token_map)
    if issue is not None:
        raise AdultInputError(issue)
    if not token_map:
        return candidate
    pattern = re.compile("|".join(re.escape(token) for token in token_map))
    restored = pattern.sub(lambda match: token_map[match.group(0)].canonical_name, candidate)
    if _token_variant_issue(restored, token_map) is not None:
        raise AdultInputError("还原后仍存在占位符")
    return restored


__all__ = [
    "AdultPrompt",
    "CandidateParseResult",
    "build_adult_prompt",
    "parse_adult_candidate",
    "restore_character_tokens",
]

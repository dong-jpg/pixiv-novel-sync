"""Deterministic validation and provider-scope hashing for adult polish."""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from .adult_types import (
    AdultCharacterFact,
    AdultInputError,
    AdultPolishRequest,
    AdultValidationResult,
    PolicyMismatchError,
    canonical_sha256,
    raw_sha256,
)
from .model_router import CandidateSnapshot, ModelCandidate


VALIDATOR_POLICY_ID = "adult_validator.v1"
VALIDATOR_POLICY_HASH = "208855e3c465ff0f8f9bcc867fb059e1e5e43e8eb59c4cee70701785acb578fa"
_VALIDATOR_RULES: dict[str, Any] = {
    "character_age_min": 18,
    "format_markers_exact": True,
    "length_ratio_max": 3.0,
    "length_ratio_min": 0.3,
    "participant_ids_required": True,
    "protected_terms_exact": True,
    "safety_fact_groups": [
        "age",
        "consent",
        "identity",
        "participant",
        "pregnancy",
        "relationship",
    ],
    "warning_groups": ["new_number", "paragraph", "perspective"],
}

_FORMAT_MARKER_RE = re.compile(
    r"\[\[[^\]\r\n]+\]\]"
    r"|\[(?:newpage|chapter:[^\]\r\n]*|pixivimage:\d+|jump:\d+)\]"
    r"|</?[A-Za-z][^>\r\n]*>",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(
    r"[0-9０-９]+(?:[./:\-年/月日时分秒][0-9０-９]+)*"
)
_ARABIC_AGE_RE = re.compile(r"([0-9０-９]{1,3})\s*岁")
_CHINESE_AGE_RE = re.compile(r"([零〇一二两三四五六七八九十]{1,3})\s*岁")
_MINOR_WORDS = (
    "未成年",
    "幼女",
    "幼男",
    "儿童",
    "小学生",
    "初中生",
    "高中生",
    "少年",
    "少女",
)
_UNKNOWN_AGE_WORDS = ("年龄不明", "年龄未知", "不知年龄")
_PREGNANCY_WORDS = ("怀孕", "妊娠", "怀胎", "孕期", "避孕")
_RELATIONSHIP_WORDS = (
    "妻子",
    "丈夫",
    "夫妻",
    "恋人",
    "情侣",
    "母亲",
    "父亲",
    "姐姐",
    "妹妹",
    "哥哥",
    "弟弟",
    "亲属",
)
_CONSENT_WORDS = (
    "同意",
    "自愿",
    "拒绝",
    "不要",
    "停下",
    "强迫",
    "胁迫",
    "反抗",
)
_NEW_CHARACTER_RE = re.compile(
    r"(?:一个|一名|那名|这名|新的?)"
    r"(?:[\u4e00-\u9fff]{0,4})"
    r"(?:男人|女人|女孩|男孩|角色|人物)"
)
_SENSITIVE_SCOPE_FIELDS = frozenset(
    {
        "api_key",
        "secret",
        "prompt",
        "messages",
        "content",
        "candidate",
        "output_text",
        "target",
        "before",
        "after",
    }
)
_SCOPE_STAGES = ("main", "safety", "fact_guard")


def _validator_payload() -> dict[str, Any]:
    return {
        "policy_id": VALIDATOR_POLICY_ID,
        "version": 1,
        "rules": _VALIDATOR_RULES,
    }


def verify_adult_validator_policy() -> None:
    if canonical_sha256(_validator_payload()) != VALIDATOR_POLICY_HASH:
        raise PolicyMismatchError("固定成人本地校验策略校验失败")


def _validation_payload(result: AdultValidationResult) -> dict[str, Any]:
    return {
        "validator_policy_id": VALIDATOR_POLICY_ID,
        "validator_policy_hash": VALIDATOR_POLICY_HASH,
        "applicable": bool(result.applicable),
        "warnings": sorted(set(result.warnings)),
        "blocking_issues": sorted(set(result.blocking_issues)),
        "protected_terms_missing": sorted(set(result.protected_terms_missing)),
        "paragraph_delta": int(result.paragraph_delta),
        "length_ratio": round(float(result.length_ratio), 6),
        "perspective_warning": bool(result.perspective_warning),
        "new_number_tokens": sorted(set(result.new_number_tokens)),
        "diff_summary": {
            key: int(result.diff_summary.get(key, 0))
            for key in ("inserted", "deleted", "replaced")
        },
    }


def compute_validation_hash(result: AdultValidationResult) -> str:
    if not isinstance(result, AdultValidationResult):
        raise AdultInputError("本地校验结果类型无效")
    verify_adult_validator_policy()
    return canonical_sha256(_validation_payload(result))


def _character_names(fact: AdultCharacterFact) -> tuple[str, ...]:
    if (
        not isinstance(fact.character_id, str)
        or not fact.character_id
        or not isinstance(fact.canonical_name, str)
        or not fact.canonical_name
        or isinstance(fact.aliases, (str, bytes))
        or not isinstance(fact.aliases, Sequence)
        or any(not isinstance(alias, str) or not alias for alias in fact.aliases)
        or isinstance(fact.age_years, bool)
        or (
            fact.age_years is not None
            and not isinstance(fact.age_years, int)
        )
        or not isinstance(fact.fictional, bool)
        or not isinstance(fact.active, bool)
    ):
        raise AdultInputError("角色事实结构无效")
    result: list[str] = []
    for name in (fact.canonical_name, *fact.aliases):
        if isinstance(name, str) and name and name not in result:
            result.append(name)
    return tuple(result)


def _paragraph_count(text: str) -> int:
    if not text.strip():
        return 0
    return len(
        [
            block
            for block in re.split(r"(?:\r?\n)[ \t]*(?:\r?\n)+", text)
            if block.strip()
        ]
    )


def _perspective(text: str) -> str | None:
    groups = {
        "first": ("我", "我们", "咱们"),
        "second": ("你", "你们", "您"),
        "third": ("他", "她", "他们", "她们", "其"),
    }
    counts = {
        group: sum(text.count(word) for word in words)
        for group, words in groups.items()
    }
    maximum = max(counts.values(), default=0)
    if maximum == 0:
        return None
    winners = [group for group, count in counts.items() if count == maximum]
    return winners[0] if len(winners) == 1 else None


def _diff_summary(original: str, candidate: str) -> dict[str, int]:
    inserted = deleted = replaced_count = 0
    matcher = difflib.SequenceMatcher(None, original, candidate, autojunk=False)
    for opcode, first_start, first_end, second_start, second_end in matcher.get_opcodes():
        old_length = first_end - first_start
        new_length = second_end - second_start
        if opcode == "insert":
            inserted += new_length
        elif opcode == "delete":
            deleted += old_length
        elif opcode == "replace":
            common = min(old_length, new_length)
            replaced_count += common
            deleted += old_length - common
            inserted += new_length - common
    return {
        "inserted": inserted,
        "deleted": deleted,
        "replaced": replaced_count,
    }


def _chinese_number(value: str) -> int | None:
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(value) == 1:
        return digits.get(value)
    return None


def _ages(text: str) -> set[int]:
    result: set[int] = set()
    for match in _ARABIC_AGE_RE.finditer(text):
        normalized = unicodedata.normalize("NFKC", match.group(1))
        result.add(int(normalized))
    for match in _CHINESE_AGE_RE.finditer(text):
        value = _chinese_number(match.group(1))
        if value is not None:
            result.add(value)
    return result


def _fact_tokens(text: str, words: Sequence[str]) -> set[str]:
    return {word for word in words if word in text}


def _number_tokens(text: str) -> Counter[str]:
    return Counter(
        unicodedata.normalize("NFKC", match.group(0))
        for match in _NUMBER_RE.finditer(text)
    )


def _identity_sets(
    original: str,
    candidate: str,
    characters: Sequence[AdultCharacterFact],
) -> tuple[set[str], set[str], bool]:
    name_owner: dict[str, str] = {}
    ambiguous = False
    for fact in characters:
        for name in _character_names(fact):
            owner = name_owner.get(name)
            if owner is not None and owner != fact.character_id:
                ambiguous = True
            name_owner[name] = fact.character_id
    original_ids = {
        owner for name, owner in name_owner.items() if name in original
    }
    candidate_ids = {
        owner for name, owner in name_owner.items() if name in candidate
    }
    return original_ids, candidate_ids, ambiguous


def run_local_adult_checks(
    original: str,
    candidate: str,
    request: AdultPolishRequest,
    protected_terms: Sequence[str],
    characters: Sequence[AdultCharacterFact],
) -> AdultValidationResult:
    verify_adult_validator_policy()
    if not isinstance(original, str) or not isinstance(candidate, str):
        raise AdultInputError("原目标和候选必须是字符串")
    if not isinstance(request, AdultPolishRequest):
        raise AdultInputError("成人润色请求类型无效")
    if isinstance(protected_terms, (str, bytes)) or not isinstance(
        protected_terms, Sequence
    ):
        raise AdultInputError("保护项必须是数组")
    if isinstance(characters, (str, bytes)) or not isinstance(characters, Sequence):
        raise AdultInputError("角色事实必须是数组")
    if any(not isinstance(fact, AdultCharacterFact) for fact in characters):
        raise AdultInputError("角色事实类型无效")
    for fact in characters:
        _character_names(fact)

    warnings: set[str] = set()
    blocking: set[str] = set()
    missing_hashes: set[str] = set()
    if not candidate.strip():
        blocking.add("empty_output")
    if len(original) != request.target_end - request.target_start:
        blocking.add("target_range_mismatch")
    if raw_sha256(original) != request.target_text_hash:
        blocking.add("target_hash_mismatch")
    if not request.adult_characters_confirmed:
        blocking.add("adult_confirmation_missing")

    character_by_id: dict[str, AdultCharacterFact] = {}
    for fact in characters:
        if fact.character_id in character_by_id:
            blocking.add("participant_mapping_ambiguous")
        character_by_id[fact.character_id] = fact
    participant_ids = set(request.participant_character_ids)
    for character_id in participant_ids:
        fact = character_by_id.get(character_id)
        if fact is None:
            blocking.add("participant_unknown")
            continue
        if not fact.active:
            blocking.add("participant_inactive")
        if not fact.fictional:
            blocking.add("real_person")
        if fact.age_years is None:
            blocking.add("age_unknown")
        elif fact.age_years < int(_VALIDATOR_RULES["character_age_min"]):
            blocking.add("minor_present")

    original_ids, candidate_ids, ambiguous = _identity_sets(
        original,
        candidate,
        characters,
    )
    if ambiguous:
        blocking.add("participant_mapping_ambiguous")
    if original_ids.difference(participant_ids):
        blocking.add("participant_mapping_unknown")
    if participant_ids.difference(original_ids):
        blocking.add("participant_mapping_unknown")
    if candidate_ids != original_ids or candidate_ids.difference(participant_ids):
        blocking.add("participant_changed")
    for character_id in original_ids | candidate_ids:
        fact = character_by_id[character_id]
        if not fact.active:
            blocking.add("participant_inactive")
        if not fact.fictional:
            blocking.add("real_person")
        if fact.age_years is None:
            blocking.add("age_unknown")
        elif fact.age_years < int(_VALIDATOR_RULES["character_age_min"]):
            blocking.add("minor_present")
    if _NEW_CHARACTER_RE.search(candidate) and not _NEW_CHARACTER_RE.search(original):
        blocking.add("new_character")

    character_names: dict[str, tuple[str, ...]] = {
        fact.character_id: _character_names(fact) for fact in characters
    }
    name_owner = {
        name: character_id
        for character_id, names in character_names.items()
        for name in names
    }
    for raw_term in protected_terms:
        if not isinstance(raw_term, str) or not raw_term:
            raise AdultInputError("保护项无效")
        owner = name_owner.get(raw_term)
        present = raw_term in candidate
        if owner is not None:
            present = any(name in candidate for name in character_names[owner])
        if not present:
            blocking.add("protected_term_missing")
            missing_hashes.add(raw_sha256(raw_term))
    for locked_term in request.locked_terms:
        if locked_term not in candidate:
            blocking.add("locked_term_missing")
            missing_hashes.add(raw_sha256(locked_term))

    original_markers = Counter(_FORMAT_MARKER_RE.findall(original))
    candidate_markers = Counter(_FORMAT_MARKER_RE.findall(candidate))
    if original_markers - candidate_markers:
        blocking.add("format_marker_missing")
    if candidate_markers - original_markers:
        blocking.add("format_marker_changed")

    original_numbers = _number_tokens(original)
    candidate_numbers = _number_tokens(candidate)
    if original_numbers - candidate_numbers:
        blocking.add("number_changed")
    new_numbers = sorted((candidate_numbers - original_numbers).keys())
    if new_numbers:
        warnings.add("new_number")
    new_number_hashes = tuple(sorted(raw_sha256(token) for token in new_numbers))

    original_ages = _ages(original)
    candidate_ages = _ages(candidate)
    if any(age < 18 for age in candidate_ages) or any(
        word in candidate for word in _MINOR_WORDS
    ):
        blocking.add("minor_present")
    if any(word in candidate for word in _UNKNOWN_AGE_WORDS):
        blocking.add("age_unknown")
    if original_ages != candidate_ages and (original_ages or candidate_ages):
        blocking.add("age_changed")

    for words, code in (
        (_PREGNANCY_WORDS, "pregnancy_changed"),
        (_RELATIONSHIP_WORDS, "relationship_changed"),
        (_CONSENT_WORDS, "consent_changed"),
    ):
        if _fact_tokens(original, words) != _fact_tokens(candidate, words):
            blocking.add(code)

    original_paragraphs = _paragraph_count(original)
    candidate_paragraphs = _paragraph_count(candidate)
    paragraph_delta = candidate_paragraphs - original_paragraphs
    if paragraph_delta:
        warnings.add("paragraph_changed")
    original_perspective = _perspective(original)
    candidate_perspective = _perspective(candidate)
    perspective_warning = (
        original_perspective is not None
        and candidate_perspective is not None
        and original_perspective != candidate_perspective
    )
    if perspective_warning:
        warnings.add("perspective_changed")

    length_ratio = len(candidate) / len(original) if original else 0.0
    if not (
        float(_VALIDATOR_RULES["length_ratio_min"])
        <= length_ratio
        <= float(_VALIDATOR_RULES["length_ratio_max"])
    ):
        blocking.add("length_ratio")

    provisional = AdultValidationResult(
        applicable=not blocking,
        warnings=tuple(sorted(warnings)),
        blocking_issues=tuple(sorted(blocking)),
        protected_terms_missing=tuple(sorted(missing_hashes)),
        paragraph_delta=paragraph_delta,
        length_ratio=round(length_ratio, 6),
        perspective_warning=perspective_warning,
        new_number_tokens=new_number_hashes,
        diff_summary=_diff_summary(original, candidate),
        validation_hash="",
    )
    return replace(
        provisional,
        validation_hash=compute_validation_hash(provisional),
    )


def _contains_sensitive_scope_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                normalized_key = key.casefold()
                if (
                    normalized_key in _SENSITIVE_SCOPE_FIELDS
                    or "api_key" in normalized_key
                    or "secret" in normalized_key
                    or "credential" in normalized_key
                    or normalized_key.endswith("_token")
                ):
                    return True
            if _contains_sensitive_scope_field(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_sensitive_scope_field(item) for item in value)
    return False


def _candidate_summary(candidate: ModelCandidate) -> dict[str, Any]:
    if not isinstance(candidate, ModelCandidate):
        raise AdultInputError("Provider 范围候选类型无效")
    return {
        "provider_id": candidate.provider_id,
        "provider_name": candidate.provider_name,
        "model_key": candidate.model_key,
        "provider_model_id": candidate.provider_model_id,
        "pool_id": candidate.pool_id,
        "pool_name": candidate.pool_name,
        "pool_version": candidate.pool_version,
        "pool_position": candidate.pool_position,
        "provider_config_hash": candidate.provider_config_hash,
        "fallback_depth": candidate.fallback_depth,
        "candidate_index": candidate.candidate_index,
    }


def _snapshot_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        if _contains_sensitive_scope_field(value):
            raise AdultInputError("Provider 范围包含敏感字段")
        if set(value) != {"snapshot"}:
            raise AdultInputError("Provider 范围字段无效")
        value = value["snapshot"]
    if not isinstance(value, CandidateSnapshot):
        raise AdultInputError("Provider 范围快照类型无效")
    candidates = sorted(
        (_candidate_summary(candidate) for candidate in value.candidates),
        key=lambda item: (
            item["candidate_index"],
            item["provider_id"],
            item["model_key"],
            item["pool_id"] or 0,
        ),
    )
    if not candidates:
        raise AdultInputError("Provider 范围候选不能为空")
    return {
        "snapshot_hash": value.snapshot_hash,
        "agent_config_hash": value.agent_config_hash,
        "binding_version": value.binding_version,
        "candidates": candidates,
    }


def provider_scope_summary(scopes: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(scopes, Mapping) or set(scopes) != set(_SCOPE_STAGES):
        raise AdultInputError("Provider 范围必须包含三个固定阶段")
    return {
        stage: _snapshot_summary(scopes[stage])
        for stage in _SCOPE_STAGES
    }


def compute_provider_scope_hash(scopes: Mapping[str, Any]) -> str:
    return canonical_sha256(provider_scope_summary(scopes))


__all__ = [
    "VALIDATOR_POLICY_HASH",
    "VALIDATOR_POLICY_ID",
    "compute_provider_scope_hash",
    "compute_validation_hash",
    "provider_scope_summary",
    "run_local_adult_checks",
    "verify_adult_validator_policy",
]

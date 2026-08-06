"""Fail-closed domain contracts for adult fictional-text polishing."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN_TEXT_FIELDS = frozenset(
    {"target_text", "before", "after", "prompt", "system_prompt"}
)
_ALLOWED_FIELDS = frozenset(
    {
        "project_id",
        "chapter_id",
        "agent_id",
        "target_start",
        "target_end",
        "chapter_content_hash",
        "target_text_hash",
        "chapter_revision",
        "participant_character_ids",
        "adult_characters_confirmed",
        "intensity",
        "locked_terms",
        "instruction",
        "idempotency_key",
        "provider_scope_hash",
        "parent_job_id",
        "preference_profile_id",
        "preference_injection_strength",
    }
)


class AdultInputError(ValueError):
    """The request cannot be trusted or is outside supported bounds."""


class PolicyMismatchError(RuntimeError):
    """A fixed server policy differs from its released hash."""


class AdultConflictError(RuntimeError):
    """An optimistic-lock or immutable snapshot no longer matches."""

    def __init__(self, message: str, code: str = "409") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class AdultIntensity:
    explicitness: int
    lyricism: int
    vulgarity: int


@dataclass(frozen=True, slots=True)
class AdultPolishRequest:
    project_id: int
    chapter_id: int
    agent_id: int
    target_start: int
    target_end: int
    chapter_content_hash: str
    target_text_hash: str
    chapter_revision: int
    participant_character_ids: tuple[str, ...]
    adult_characters_confirmed: bool
    intensity: AdultIntensity
    locked_terms: tuple[str, ...]
    instruction: str
    idempotency_key: str
    provider_scope_hash: str
    parent_job_id: str | None = None
    preference_profile_id: int | None = None
    preference_injection_strength: str = "off"


@dataclass(frozen=True, slots=True)
class AdultCharacterFact:
    character_id: str
    revision: int
    canonical_name: str
    aliases: tuple[str, ...]
    age_years: int | None
    age_basis: str
    fictional: bool
    active: bool


@dataclass(frozen=True, slots=True)
class AdultPolicyBundle:
    policy_id: str
    version: int
    prompt_template: str
    output_schema: Mapping[str, Any]
    policy_text: str
    expected_hash: str


@dataclass(frozen=True, slots=True)
class AdultValidationResult:
    applicable: bool
    warnings: tuple[str, ...]
    blocking_issues: tuple[str, ...]
    protected_terms_missing: tuple[str, ...]
    paragraph_delta: int
    length_ratio: float
    perspective_warning: bool
    new_number_tokens: tuple[str, ...]
    diff_summary: Mapping[str, int]
    validation_hash: str


def raw_sha256(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("raw_sha256 requires str")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_value(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise TypeError("canonical object keys must be strings")
            key = unicodedata.normalize("NFC", raw_key)
            if key in normalized:
                raise ValueError("canonical object contains duplicate normalized keys")
            normalized[key] = _canonical_value(raw_value)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return raw_sha256(payload)


def _integer(value: Any, name: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AdultInputError(f"{name} 无效")
    if maximum is not None and value > maximum:
        raise AdultInputError(f"{name} 超出范围")
    return value


def _hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise AdultInputError(f"{name} 必须是小写 SHA-256")
    return value


def _ascii_key(value: Any, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not 16 <= len(value) <= 128:
        raise AdultInputError(f"{name}长度必须为 16-128 个 ASCII 字符")
    if not value.isascii() or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise AdultInputError(f"{name}必须是 ASCII 字符")
    return value


def _participants(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AdultInputError("参与者列表无效")
    if not 1 <= len(value) <= 20:
        raise AdultInputError("参与者数量必须为 1-20")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AdultInputError("参与者 ID 无效")
        try:
            normalized = str(uuid.UUID(item))
        except (ValueError, AttributeError) as exc:
            raise AdultInputError("参与者 ID 无效") from exc
        if item != normalized:
            raise AdultInputError("参与者 ID 必须是规范小写 UUID")
        result.append(item)
    if len(set(result)) != len(result):
        raise AdultInputError("参与者 ID 不得重复")
    return tuple(result)


def _intensity(value: Any) -> AdultIntensity:
    if not isinstance(value, Mapping) or set(value) != {
        "explicitness",
        "lyricism",
        "vulgarity",
    }:
        raise AdultInputError("强度参数无效")
    return AdultIntensity(
        explicitness=_integer(value["explicitness"], "explicitness", minimum=0, maximum=100),
        lyricism=_integer(value["lyricism"], "lyricism", minimum=0, maximum=100),
        vulgarity=_integer(value["vulgarity"], "vulgarity", minimum=0, maximum=100),
    )


def _locked_terms(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) > 64:
        raise AdultInputError("locked_terms 最多 64 项")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not 1 <= len(item) <= 100:
            raise AdultInputError("locked_terms 每项必须为 1-100 个码点")
        if any(unicodedata.category(char) == "Cc" for char in item):
            raise AdultInputError("locked_terms 不得包含控制字符")
        if item not in result:
            result.append(item)
    return tuple(result)


def parse_adult_request(payload: Mapping[str, Any]) -> AdultPolishRequest:
    if not isinstance(payload, Mapping):
        raise AdultInputError("请求体必须是对象")
    if any(not isinstance(key, str) for key in payload):
        raise AdultInputError("请求字段名必须是字符串")
    forbidden = sorted(_FORBIDDEN_TEXT_FIELDS.intersection(payload))
    if forbidden:
        raise AdultInputError(f"禁止提交字段: {', '.join(forbidden)}")
    unknown = sorted(set(payload).difference(_ALLOWED_FIELDS))
    if unknown:
        raise AdultInputError(f"未知字段: {', '.join(unknown)}")

    target_start = _integer(payload.get("target_start"), "target_start", minimum=0)
    target_end = _integer(payload.get("target_end"), "target_end", minimum=1)
    if target_end <= target_start:
        raise AdultInputError("target_end 必须大于 target_start")
    target_length = target_end - target_start
    if not 20 <= target_length <= 12_000:
        raise AdultInputError("目标片段长度必须为 20-12000 个码点")

    confirmed = payload.get("adult_characters_confirmed")
    if not isinstance(confirmed, bool):
        raise AdultInputError("adult_characters_confirmed 必须是布尔值")
    instruction = payload.get("instruction", "")
    if not isinstance(instruction, str) or len(instruction) > 1_000:
        raise AdultInputError("instruction 最多 1000 个码点")

    preference_profile_id = payload.get("preference_profile_id")
    if preference_profile_id is not None:
        preference_profile_id = _integer(
            preference_profile_id,
            "preference_profile_id",
            minimum=1,
        )
    preference_strength = payload.get("preference_injection_strength", "off")
    if not isinstance(preference_strength, str) or preference_strength not in {
        "off",
        "light",
        "standard",
        "strong",
    }:
        raise AdultInputError("preference_injection_strength 无效")

    return AdultPolishRequest(
        project_id=_integer(payload.get("project_id"), "project_id", minimum=1),
        chapter_id=_integer(payload.get("chapter_id"), "chapter_id", minimum=1),
        agent_id=_integer(payload.get("agent_id"), "agent_id", minimum=1),
        target_start=target_start,
        target_end=target_end,
        chapter_content_hash=_hash(payload.get("chapter_content_hash"), "chapter_content_hash"),
        target_text_hash=_hash(payload.get("target_text_hash"), "target_text_hash"),
        chapter_revision=_integer(payload.get("chapter_revision"), "chapter_revision", minimum=0),
        participant_character_ids=_participants(payload.get("participant_character_ids")),
        adult_characters_confirmed=confirmed,
        intensity=_intensity(payload.get("intensity")),
        locked_terms=_locked_terms(payload.get("locked_terms", [])),
        instruction=instruction,
        idempotency_key=str(_ascii_key(payload.get("idempotency_key"), "幂等键")),
        provider_scope_hash=_hash(payload.get("provider_scope_hash"), "provider_scope_hash"),
        parent_job_id=_ascii_key(payload.get("parent_job_id"), "parent_job_id", optional=True),
        preference_profile_id=preference_profile_id,
        preference_injection_strength=str(preference_strength),
    )


def warning_ack_hash(
    validation_hash: str,
    safety_policy_hash: str,
    validator_policy_hash: str,
    warning_codes: Sequence[str],
) -> str:
    return canonical_sha256(
        {
            "validation_hash": _hash(validation_hash, "validation_hash"),
            "safety_policy_hash": _hash(safety_policy_hash, "safety_policy_hash"),
            "validator_policy_hash": _hash(validator_policy_hash, "validator_policy_hash"),
            "warning_codes": sorted(set(str(code) for code in warning_codes)),
        }
    )


__all__ = [
    "AdultCharacterFact",
    "AdultConflictError",
    "AdultInputError",
    "AdultIntensity",
    "AdultPolicyBundle",
    "AdultPolishRequest",
    "AdultValidationResult",
    "PolicyMismatchError",
    "canonical_sha256",
    "parse_adult_request",
    "raw_sha256",
    "warning_ack_hash",
]

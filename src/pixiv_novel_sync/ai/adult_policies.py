"""Read-only safety and fact-review policies for adult polish jobs."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Literal, cast

from .adult_types import (
    AdultPolicyBundle,
    PolicyMismatchError,
    canonical_sha256,
)


SAFETY_POLICY_TEXT = (
    "仅审查虚构成年人片段。年龄不明、未成年人、现实人物、未确认的新人物或无法判断时，"
    "必须返回 safe=false。不得输出解释或候选正文。"
)
SAFETY_PROMPT_TEMPLATE = (
    "根据固定策略审查候选，仅返回符合 Schema 的 JSON。参与者事实：{participant_facts}\n"
    "允许名称：{allowed_names}\n候选：{candidate}"
)
SAFETY_ISSUES = (
    "minor_present",
    "age_unknown",
    "real_person",
    "new_character",
    "schema_invalid",
)

FACT_GUARD_POLICY_TEXT = (
    "比较原目标和候选，仅检查年龄、怀孕、关系、同意、参与者与锁定事实是否改变。"
    "任何不确定项必须返回 unknown，不得输出自由文本理由。"
)
FACT_GUARD_PROMPT_TEMPLATE = (
    "根据固定事实保护策略比较文本，仅返回符合 Schema 的 JSON。事实：{participant_facts}\n"
    "保护清单：{protected_terms}\n原文：{original}\n候选：{candidate}"
)
FACT_GUARD_ISSUES = (
    "age_changed",
    "pregnancy_changed",
    "relationship_changed",
    "consent_changed",
    "participant_changed",
    "locked_fact_changed",
    "unknown",
)


def _schema(issues: tuple[str, ...]) -> MappingProxyType:
    return MappingProxyType(
        {
            "type": "object",
            "additionalProperties": False,
            "required": ("safe", "issues"),
            "properties": {
                "safe": {"type": "boolean"},
                "issues": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "enum": issues},
                },
            },
        }
    )


SAFETY_OUTPUT_SCHEMA = _schema(SAFETY_ISSUES)
FACT_GUARD_OUTPUT_SCHEMA = _schema(FACT_GUARD_ISSUES)


def _payload(
    policy_id: str,
    version: int,
    prompt_template: str,
    output_schema: Any,
    policy_text: str,
) -> dict[str, Any]:
    return {
        "policy_id": policy_id,
        "version": version,
        "prompt_template": prompt_template,
        "output_schema": output_schema,
        "policy_text": policy_text,
    }


_SAFETY_RELEASE_HASH = "4c9874523d0c49b411a9316450ecb1f82a26f29a0dccc9c9a0a6e7212fc2760d"
_FACT_GUARD_RELEASE_HASH = "7d405e8cfc5d4fca38a4a1352a9212b75c8da0d20a9eec77b7fb8c960e730ac2"

SAFETY_POLICY = AdultPolicyBundle(
    policy_id="adult_safety_policy.v1",
    version=1,
    prompt_template=SAFETY_PROMPT_TEMPLATE,
    output_schema=SAFETY_OUTPUT_SCHEMA,
    policy_text=SAFETY_POLICY_TEXT,
    expected_hash=_SAFETY_RELEASE_HASH,
)
FACT_GUARD_POLICY = AdultPolicyBundle(
    policy_id="adult_fact_guard_policy.v1",
    version=1,
    prompt_template=FACT_GUARD_PROMPT_TEMPLATE,
    output_schema=FACT_GUARD_OUTPUT_SCHEMA,
    policy_text=FACT_GUARD_POLICY_TEXT,
    expected_hash=_FACT_GUARD_RELEASE_HASH,
)


def _runtime_payload(kind: Literal["safety", "fact_guard"]) -> dict[str, Any]:
    if kind == "safety":
        return _payload(
            "adult_safety_policy.v1",
            1,
            SAFETY_PROMPT_TEMPLATE,
            SAFETY_OUTPUT_SCHEMA,
            SAFETY_POLICY_TEXT,
        )
    return _payload(
        "adult_fact_guard_policy.v1",
        1,
        FACT_GUARD_PROMPT_TEMPLATE,
        FACT_GUARD_OUTPUT_SCHEMA,
        FACT_GUARD_POLICY_TEXT,
    )


def verify_adult_policy_bundle() -> None:
    for kind, bundle, release_hash in (
        ("safety", SAFETY_POLICY, _SAFETY_RELEASE_HASH),
        ("fact_guard", FACT_GUARD_POLICY, _FACT_GUARD_RELEASE_HASH),
    ):
        runtime_hash = canonical_sha256(_runtime_payload(cast(Any, kind)))
        bundle_hash = canonical_sha256(
            _payload(
                bundle.policy_id,
                bundle.version,
                bundle.prompt_template,
                bundle.output_schema,
                bundle.policy_text,
            )
        )
        if (
            runtime_hash != release_hash
            or bundle_hash != release_hash
            or bundle.expected_hash != release_hash
        ):
            raise PolicyMismatchError(f"固定成人策略校验失败: {kind}")


def load_adult_policy(kind: Literal["safety", "fact_guard"]) -> AdultPolicyBundle:
    if kind not in {"safety", "fact_guard"}:
        raise PolicyMismatchError("未知成人策略")
    verify_adult_policy_bundle()
    return SAFETY_POLICY if kind == "safety" else FACT_GUARD_POLICY


__all__ = [
    "FACT_GUARD_POLICY",
    "FACT_GUARD_POLICY_TEXT",
    "SAFETY_POLICY",
    "SAFETY_POLICY_TEXT",
    "load_adult_policy",
    "verify_adult_policy_bundle",
]

from __future__ import annotations

from dataclasses import replace

import pytest

from pixiv_novel_sync.ai import adult_policies
from pixiv_novel_sync.ai.adult_policies import (
    load_adult_policy,
    verify_adult_policy_bundle,
)
from pixiv_novel_sync.ai.adult_types import PolicyMismatchError


def test_policy_bundles_are_fixed_and_have_strict_issue_enums():
    safety = load_adult_policy("safety")
    fact_guard = load_adult_policy("fact_guard")

    assert safety.policy_id == "adult_safety_policy.v1"
    assert fact_guard.policy_id == "adult_fact_guard_policy.v1"
    assert safety.output_schema["additionalProperties"] is False
    assert "minor_present" in safety.output_schema["properties"]["issues"]["items"]["enum"]
    assert "unknown" in fact_guard.output_schema["properties"]["issues"]["items"]["enum"]
    assert len(safety.expected_hash) == 64
    verify_adult_policy_bundle()


def test_policy_hash_tamper_fails_closed(monkeypatch):
    monkeypatch.setattr(adult_policies, "SAFETY_POLICY_TEXT", "被修改")

    with pytest.raises(PolicyMismatchError):
        verify_adult_policy_bundle()


def test_policy_bundle_replacement_fails_closed(monkeypatch):
    monkeypatch.setattr(
        adult_policies,
        "SAFETY_POLICY",
        replace(adult_policies.SAFETY_POLICY, prompt_template="被替换的审查 Prompt"),
    )

    with pytest.raises(PolicyMismatchError):
        verify_adult_policy_bundle()


def test_unknown_policy_kind_fails_closed():
    with pytest.raises(PolicyMismatchError):
        load_adult_policy("other")  # type: ignore[arg-type]

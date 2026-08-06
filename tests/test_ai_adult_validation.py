from __future__ import annotations

from dataclasses import replace

import pytest

import pixiv_novel_sync.ai.adult_validation as adult_validation
from ai_adult_testkit import CHARACTER_B_ID, character_fact, valid_adult_payload
from pixiv_novel_sync.ai.adult_types import (
    AdultInputError,
    PolicyMismatchError,
    canonical_sha256,
    parse_adult_request,
    raw_sha256,
)
from pixiv_novel_sync.ai.adult_validation import (
    VALIDATOR_POLICY_HASH,
    VALIDATOR_POLICY_ID,
    compute_provider_scope_hash,
    compute_validation_hash,
    run_local_adult_checks,
    verify_adult_validator_policy,
)
from pixiv_novel_sync.ai.model_router import CandidateSnapshot, ModelCandidate


ORIGINAL = "安娜握住他的手，停顿片刻后仍保持原来的称呼和视角。"


def _request(original: str = ORIGINAL, **overrides):
    values = {
        "target_start": 0,
        "target_end": len(original),
        "target_text_hash": raw_sha256(original),
        "chapter_content_hash": raw_sha256(original),
        "participant_character_ids": [character_fact().character_id],
    }
    values.update(overrides)
    return parse_adult_request(valid_adult_payload(**values))


def _check(original=ORIGINAL, candidate=ORIGINAL, **overrides):
    values = {
        "request": _request(original),
        "protected_terms": ("安娜",),
        "characters": (character_fact("安娜", 25),),
    }
    values.update(overrides)
    return run_local_adult_checks(original, candidate, **values)


def _snapshot(
    seed: str,
    *,
    provider_id: int = 1,
    binding_version: int = 1,
    candidate_index: int = 0,
) -> CandidateSnapshot:
    candidate = ModelCandidate(
        provider_id=provider_id,
        provider_name=f"provider-{provider_id}",
        model_key=f"model-{seed}",
        provider_model_id=provider_id,
        pool_id=7,
        pool_name="adult-pool",
        pool_version=3,
        pool_position=1,
        provider_config_hash=seed * 64,
        capabilities=("json",),
        context_window=16_000,
        candidate_index=candidate_index,
    )
    return CandidateSnapshot(
        candidates=(candidate,),
        snapshot_hash=seed * 64,
        agent_config_hash=seed * 64,
        binding_version=binding_version,
    )


def test_local_checks_accept_unchanged_adult_fictional_target():
    result = _check()

    assert result.applicable is True
    assert result.blocking_issues == ()
    assert result.protected_terms_missing == ()
    assert result.length_ratio == 1.0
    assert len(result.validation_hash) == 64
    assert result.validation_hash == compute_validation_hash(result)


def test_local_checks_reject_range_hash_and_empty_candidate():
    request = _request()
    wrong_range = replace(request, target_end=request.target_end + 1)
    wrong_hash = replace(request, target_text_hash="0" * 64)

    assert "target_range_mismatch" in _check(request=wrong_range).blocking_issues
    assert "target_hash_mismatch" in _check(request=wrong_hash).blocking_issues
    assert "empty_output" in _check(candidate="").blocking_issues


def test_local_checks_hash_missing_protected_terms_without_persisting_text():
    candidate = ORIGINAL.replace("安娜", "她")

    result = _check(candidate=candidate)

    assert "protected_term_missing" in result.blocking_issues
    assert result.protected_terms_missing == (raw_sha256("安娜"),)
    assert "安娜" not in repr(result)
    assert candidate not in repr(result)


@pytest.mark.parametrize(
    "ratio, blocked",
    [(0.30, False), (3.0, False), (0.25, True), (3.05, True)],
)
def test_local_checks_enforce_inclusive_length_ratio_boundaries(ratio, blocked):
    original = "甲" * 20
    candidate = "甲" * int(len(original) * ratio)
    request = _request(
        original,
        participant_character_ids=[character_fact().character_id],
        locked_terms=[],
    )

    result = run_local_adult_checks(
        original,
        candidate,
        request,
        (),
        (character_fact(),),
    )

    assert ("length_ratio" in result.blocking_issues) is blocked


def test_local_checks_cover_minor_unknown_age_and_real_person_risk():
    minor_text = "安娜遇见一个十七岁女孩，她改变了目标片段中的参与者和年龄事实。"
    assert "minor_present" in _check(candidate=minor_text).blocking_issues

    unknown = _check(characters=(character_fact("安娜", None),))
    assert "age_unknown" in unknown.blocking_issues

    real = _check(characters=(character_fact("安娜", 25, fictional=False),))
    assert "real_person" in real.blocking_issues


def test_local_checks_block_participant_and_protected_fact_changes():
    lin = character_fact("林舟", 27, character_id=CHARACTER_B_ID)
    request = _request(participant_character_ids=[character_fact().character_id])
    participant = _check(
        candidate=ORIGINAL + "林舟走进房间。",
        request=request,
        characters=(character_fact(), lin),
    )
    assert "participant_changed" in participant.blocking_issues

    changes = (
        ("安娜确认自己已经怀孕，仍保持原来的称呼和视角。", "pregnancy_changed"),
        ("安娜称他为丈夫，仍保持原来的称呼和视角。", "relationship_changed"),
        ("安娜明确拒绝并要求停下，仍保持原来的称呼和视角。", "consent_changed"),
    )
    for candidate, code in changes:
        assert code in _check(candidate=candidate).blocking_issues

    original = "安娜称他为丈夫，停顿片刻后仍保持原来的称呼和视角。"
    candidate = "安娜称她为妻子，停顿片刻后仍保持原来的称呼和视角。"
    changed_relation = _check(
        original=original,
        candidate=candidate,
        request=_request(original),
    )
    assert "relationship_changed" in changed_relation.blocking_issues


def test_local_checks_apply_age_and_reality_rules_to_new_known_character():
    request = _request(participant_character_ids=[character_fact().character_id])
    candidate = ORIGINAL + "林舟走进房间。"
    real_minor = character_fact(
        "林舟",
        17,
        fictional=False,
        character_id=CHARACTER_B_ID,
    )

    result = _check(
        candidate=candidate,
        request=request,
        characters=(character_fact(), real_minor),
    )

    assert "participant_changed" in result.blocking_issues
    assert "minor_present" in result.blocking_issues
    assert "real_person" in result.blocking_issues


def test_local_checks_protect_markers_and_warn_on_new_numbers():
    original = "安娜在[newpage]约定三天后于2026-08-06见面并保持原来的称呼。"
    request = _request(original, locked_terms=["[newpage]"])
    missing_marker = run_local_adult_checks(
        original,
        original.replace("[newpage]", ""),
        request,
        ("[newpage]",),
        (character_fact(),),
    )
    assert "format_marker_missing" in missing_marker.blocking_issues

    new_number = run_local_adult_checks(
        original,
        original + "另记4次。",
        request,
        ("[newpage]",),
        (character_fact(),),
    )
    assert "new_number" in new_number.warnings
    assert new_number.new_number_tokens == (raw_sha256("4"),)


def test_local_checks_preserve_marker_and_number_multiplicity():
    original = "安娜在3时写下[newpage]约定，3时又写下[newpage]同一约定。"
    candidate = "安娜在3时写下约定，随后又写下[newpage]同一约定。"
    request = _request(original, locked_terms=[])

    result = run_local_adult_checks(
        original,
        candidate,
        request,
        (),
        (character_fact(),),
    )

    assert "format_marker_missing" in result.blocking_issues
    assert "number_changed" in result.blocking_issues


def test_local_checks_reject_malformed_character_fact_as_domain_error():
    malformed = replace(character_fact(), aliases=None)

    with pytest.raises(AdultInputError, match="角色事实"):
        _check(characters=(malformed,))


def test_local_checks_warn_on_paragraph_and_perspective_changes():
    candidate = "安娜握住你的手。\n\n你停顿片刻后仍保持原来的称呼和视角。"

    result = _check(candidate=candidate)

    assert "paragraph_changed" in result.warnings
    assert "perspective_changed" in result.warnings
    assert result.perspective_warning is True
    assert result.paragraph_delta == 1


def test_validation_hash_is_canonical_and_changes_with_result():
    result = _check()
    changed = replace(
        result,
        warnings=("paragraph_changed",),
        validation_hash="",
    )

    assert compute_validation_hash(result) == result.validation_hash
    assert compute_validation_hash(changed) != result.validation_hash


def test_validator_policy_hash_is_fixed_and_tamper_detected(monkeypatch):
    assert VALIDATOR_POLICY_ID == "adult_validator.v1"
    assert len(VALIDATOR_POLICY_HASH) == 64
    verify_adult_validator_policy()

    monkeypatch.setitem(adult_validation._VALIDATOR_RULES, "length_ratio_min", 0.1)
    with pytest.raises(PolicyMismatchError, match="校验"):
        verify_adult_validator_policy()


def test_provider_scope_hash_is_stage_and_order_stable_but_version_sensitive():
    scopes_a = {
        "fact_guard": _snapshot("c"),
        "main": _snapshot("a"),
        "safety": _snapshot("b"),
    }
    scopes_b = {
        "safety": _snapshot("b"),
        "fact_guard": _snapshot("c"),
        "main": _snapshot("a"),
    }

    assert compute_provider_scope_hash(scopes_a) == compute_provider_scope_hash(scopes_b)
    changed = dict(scopes_b)
    changed["safety"] = _snapshot("b", binding_version=2)
    assert compute_provider_scope_hash(changed) != compute_provider_scope_hash(scopes_a)


def test_provider_scope_hash_rejects_missing_stage_and_secret_fields():
    with pytest.raises(AdultInputError, match="阶段"):
        compute_provider_scope_hash({"main": _snapshot("a")})

    with pytest.raises(AdultInputError, match="敏感"):
        compute_provider_scope_hash(
            {
                "main": {
                    "snapshot": _snapshot("a"),
                    "provider_api_key": "secret",
                },
                "safety": _snapshot("b"),
                "fact_guard": _snapshot("c"),
            }
        )


def test_provider_scope_hash_matches_explicit_canonical_summary():
    scopes = {
        "main": _snapshot("a"),
        "safety": _snapshot("b"),
        "fact_guard": _snapshot("c"),
    }
    digest = compute_provider_scope_hash(scopes)

    assert digest == canonical_sha256(adult_validation.provider_scope_summary(scopes))

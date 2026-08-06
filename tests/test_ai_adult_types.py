from __future__ import annotations

import pytest

from pixiv_novel_sync.ai.adult_types import (
    AdultInputError,
    canonical_sha256,
    parse_adult_request,
    raw_sha256,
    warning_ack_hash,
)


def _payload() -> dict:
    target = "目标片段至少二十个码点用于完整边界测试内容。"
    chapter = f"前文{target}后文"
    return {
        "project_id": 1,
        "chapter_id": 9,
        "agent_id": 7,
        "target_start": 2,
        "target_end": 2 + len(target),
        "chapter_content_hash": raw_sha256(chapter),
        "target_text_hash": raw_sha256(target),
        "chapter_revision": 0,
        "participant_character_ids": ["11111111-1111-4111-8111-111111111111"],
        "adult_characters_confirmed": True,
        "intensity": {"explicitness": 50, "lyricism": 50, "vulgarity": 20},
        "locked_terms": ["称呼"],
        "instruction": "只调整措辞",
        "idempotency_key": "adult-request-key-0001",
        "provider_scope_hash": "a" * 64,
    }


def test_raw_hash_preserves_crlf_and_combining_characters():
    assert raw_sha256("é\r\n") != raw_sha256("e\u0301\n")


def test_canonical_hash_normalizes_only_structured_values():
    assert canonical_sha256({"b": "é", "a": ["e\u0301"]}) == canonical_sha256(
        {"a": ["é"], "b": "é"}
    )
    assert raw_sha256("e\u0301") != raw_sha256("é")


def test_parse_request_rejects_utf16_style_or_out_of_range_input():
    payload = _payload()
    payload.update(target_start=-1, target_end=20)
    with pytest.raises(AdultInputError, match="target_start"):
        parse_adult_request(payload)

    payload = _payload()
    payload["idempotency_key"] = "短"
    with pytest.raises(AdultInputError, match="幂等键"):
        parse_adult_request(payload)


def test_parse_request_rejects_client_text_and_invalid_identity_fields():
    payload = _payload()
    payload["target_text"] = "客户端正文"
    with pytest.raises(AdultInputError, match="target_text"):
        parse_adult_request(payload)

    payload = _payload()
    payload["participant_character_ids"] *= 2
    with pytest.raises(AdultInputError, match="参与者"):
        parse_adult_request(payload)


def test_parse_request_rejects_non_string_keys_and_preference_strength_types():
    payload = _payload()
    payload[1] = "invalid"  # type: ignore[index]
    with pytest.raises(AdultInputError, match="字段名"):
        parse_adult_request(payload)

    payload = _payload()
    payload["preference_injection_strength"] = ["strong"]
    with pytest.raises(AdultInputError, match="preference_injection_strength"):
        parse_adult_request(payload)


def test_parse_request_returns_immutable_bounded_values():
    request = parse_adult_request(_payload())

    assert request.project_id == 1
    assert request.target_end - request.target_start >= 20
    assert request.participant_character_ids == (
        "11111111-1111-4111-8111-111111111111",
    )
    assert request.intensity.explicitness == 50


def test_warning_ack_hash_sorts_and_deduplicates_codes():
    first = warning_ack_hash("a" * 64, "b" * 64, "c" * 64, ["z", "a", "z"])
    second = warning_ack_hash("a" * 64, "b" * 64, "c" * 64, ["a", "z"])

    assert first == second
    assert len(first) == 64

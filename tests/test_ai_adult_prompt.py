from __future__ import annotations

from dataclasses import replace

import pytest

import pixiv_novel_sync.ai.adult_prompt as adult_prompt
from ai_adult_testkit import CHARACTER_B_ID, character_fact
from pixiv_novel_sync.ai.adult_prompt import (
    build_adult_prompt,
    parse_adult_candidate,
    restore_character_tokens,
)
from pixiv_novel_sync.ai.adult_types import AdultInputError, AdultIntensity


TARGET = "安娜握住他的手，停顿片刻后仍保持原来的称呼和视角。"


def _build(**overrides):
    values = {
        "agent_prompt": "角色和剧情保真",
        "project_facts": {"outline": "安娜与林舟在雨夜交谈", "protected_terms": ["旧约"]},
        "before": "前文没有改动。",
        "target": TARGET,
        "after": "后文保持原样。",
        "style_control": None,
        "intensity": AdultIntensity(50, 50, 50),
        "instruction": "只改措辞",
        "protected_terms": ("安娜",),
        "characters": (character_fact("安娜"),),
    }
    values.update(overrides)
    return build_adult_prompt(**values)


def _wrapped(boundary: str, text: str) -> str:
    return (
        f"{boundary}_CANDIDATE_BEGIN\n"
        f"{text}\n"
        f"{boundary}_CANDIDATE_END"
    )


def test_prompt_has_four_unambiguous_sections_and_random_character_tokens():
    prompt = _build()

    assert {"system", "project_facts", "readonly_context", "target"} == set(
        prompt.sections
    )
    assert prompt.boundary not in "前文没有改动。" + TARGET + "后文保持原样。只改措辞"
    assert len(prompt.user_messages) == 5
    assert [message["role"] for message in prompt.user_messages] == [
        "system",
        "user",
        "user",
        "user",
        "user",
    ]
    rendered = "\n".join(message["content"] for message in prompt.user_messages)
    assert "安娜" not in rendered
    assert len(prompt.token_map) == 1
    token = next(iter(prompt.token_map))
    assert token.startswith("ADULT_")
    assert token in prompt.sections["target"]
    assert "只输出边界内唯一候选" in prompt.sections["system"]


def test_prompt_retries_boundary_collision_and_preserves_raw_text(monkeypatch):
    values = iter(("1" * 32, "2" * 32, "3" * 32))
    monkeypatch.setattr(adult_prompt.secrets, "token_hex", lambda _size: next(values))
    collision = f"ADULT_BOUNDARY_{'1' * 32}"
    target = f"安娜保留组合字符 e\u0301、emoji 😀 与 CRLF\r\n，同时包含 {collision}。"

    prompt = _build(
        target=target,
        protected_terms=("e\u0301",),
    )

    assert prompt.boundary == f"ADULT_BOUNDARY_{'2' * 32}"
    assert "e\u0301" in prompt.sections["target"]
    assert "\r\n" in prompt.sections["target"]
    assert "😀" in prompt.sections["target"]


def test_prompt_collision_retry_is_bounded(monkeypatch):
    nonce = "1" * 32
    calls = 0

    def colliding_nonce(_size):
        nonlocal calls
        calls += 1
        return nonce

    monkeypatch.setattr(adult_prompt.secrets, "token_hex", colliding_nonce)

    with pytest.raises(AdultInputError, match="随机边界"):
        _build(target=f"{TARGET} ADULT_BOUNDARY_{nonce}")
    assert calls <= 64


def test_prompt_rejects_missing_locked_term_and_unconfirmed_character():
    with pytest.raises(AdultInputError, match="锁定词"):
        _build(protected_terms=("不存在的客户端事实",))

    project_facts = {
        "characters": [
            {
                "character_id": "99999999-9999-4999-8999-999999999999",
                "canonical_name": "莉莉",
                "aliases": ["小莉"],
                "active": True,
            }
        ]
    }
    with pytest.raises(AdultInputError, match="未确认角色"):
        _build(
            project_facts=project_facts,
            target=f"{TARGET}莉莉站在门外。",
            protected_terms=("安娜",),
        )


def test_prompt_rejects_alias_shared_by_multiple_identities():
    anna = replace(character_fact("安娜"), aliases=("小安",))
    lin = replace(
        character_fact("林舟", character_id=CHARACTER_B_ID),
        aliases=("小安",),
    )

    with pytest.raises(AdultInputError, match="别名|身份"):
        _build(characters=(anna, lin))


def test_prompt_rejects_malformed_character_facts_as_domain_errors():
    with pytest.raises(AdultInputError, match="角色事实"):
        _build(characters=("not-a-character",))

    malformed = replace(character_fact("安娜"), age_years="25")
    with pytest.raises(AdultInputError, match="年龄"):
        _build(characters=(malformed,))

    with pytest.raises(AdultInputError, match="锁定词"):
        _build(protected_terms=None)


def test_prompt_merges_operation_intensity_without_mutating_project_style():
    style = {
        "sliders": {"explicitness": 10, "lyricism": 80},
        "tags": ["第一人称"],
    }
    original = {
        "sliders": {"explicitness": 10, "lyricism": 80},
        "tags": ["第一人称"],
    }

    prompt = _build(
        style_control=style,
        intensity=AdultIntensity(75, 25, 60),
    )

    assert style == original
    assert "75/100" in prompt.sections["system"]
    assert "25/100" in prompt.sections["system"]
    assert "60/100" in prompt.sections["system"]
    assert "第一人称" in prompt.sections["system"]


def test_candidate_parser_accepts_one_exact_block_and_one_outer_fence():
    boundary = "ADULT_BOUNDARY_" + "a" * 32
    raw = _wrapped(boundary, "正文第一行\r\n正文第二行 e\u0301")

    parsed = parse_adult_candidate(raw, boundary)
    fenced = parse_adult_candidate(f"```text\n{raw}\n```", boundary)

    assert parsed.text == "正文第一行\r\n正文第二行 e\u0301"
    assert parsed.blocking_issues == ()
    assert fenced == parsed


def test_candidate_parser_marks_structure_block_without_guessing_prefixes():
    parsed = parse_adult_candidate("说明：\n正文", "B")
    assert parsed.text == "说明：\n正文"
    assert "explanation_prefix" in parsed.blocking_issues

    raw = "B1\n正文\nB2\n另一个"
    parsed = parse_adult_candidate(raw, "B")
    assert parsed.text == raw
    assert "multiple_blocks" in parsed.blocking_issues


def test_candidate_parser_preserves_invalid_wrappers_headings_and_analysis():
    boundary = "ADULT_BOUNDARY_" + "b" * 32
    missing_close = f"{boundary}_CANDIDATE_BEGIN\n正文"
    parsed = parse_adult_candidate(missing_close, boundary)
    assert parsed.text == missing_close
    assert "missing_closing_marker" in parsed.blocking_issues

    heading = _wrapped(boundary, "# 候选\n正文")
    parsed = parse_adult_candidate(heading, boundary)
    assert parsed.text == "# 候选\n正文"
    assert "heading" in parsed.blocking_issues

    analysis = _wrapped(boundary, "分析：先解释再输出")
    parsed = parse_adult_candidate(analysis, boundary)
    assert parsed.text == "分析：先解释再输出"
    assert "analysis" in parsed.blocking_issues


def test_candidate_parser_does_not_strip_partial_or_nested_fences():
    boundary = "ADULT_BOUNDARY_" + "c" * 32
    raw = f"说明\n```\n{_wrapped(boundary, '正文')}\n```"

    parsed = parse_adult_candidate(raw, boundary)

    assert parsed.text == raw
    assert "markdown_fence" in parsed.blocking_issues
    assert "explanation_prefix" in parsed.blocking_issues


def test_candidate_parser_blocks_extra_boundary_inside_candidate_body():
    boundary = "ADULT_BOUNDARY_" + "d" * 32
    raw = _wrapped(boundary, f"正文中嵌入 {boundary} 后继续")

    parsed = parse_adult_candidate(raw, boundary)

    assert parsed.text == raw
    assert "multiple_blocks" in parsed.blocking_issues


def test_restore_character_tokens_rejects_unknown_case_and_split_variants():
    prompt = _build()
    token = next(iter(prompt.token_map))
    restored = restore_character_tokens(f"{token}握住他的手。", prompt.token_map)
    assert restored == "安娜握住他的手。"

    unknown = "ADULT_" + "f" * 32 + "_9"
    with pytest.raises(AdultInputError, match="未知|占位符"):
        restore_character_tokens(unknown, prompt.token_map)
    with pytest.raises(AdultInputError, match="变体|占位符"):
        restore_character_tokens(token.lower(), prompt.token_map)
    with pytest.raises(AdultInputError, match="变体|占位符"):
        restore_character_tokens(token[:6] + "\u200b" + token[6:], prompt.token_map)
    fullwidth = "".join(
        chr(ord(char) + 0xFEE0) if 33 <= ord(char) <= 126 else char
        for char in token
    )
    with pytest.raises(AdultInputError, match="变体|占位符"):
        restore_character_tokens(fullwidth, prompt.token_map)


def test_restore_character_tokens_rejects_multiple_tokens_for_one_identity():
    fact = character_fact("安娜")
    token_a = "ADULT_" + "a" * 32 + "_0"
    token_b = "ADULT_" + "b" * 32 + "_1"

    with pytest.raises(AdultInputError, match="一对一|身份"):
        restore_character_tokens(token_a, {token_a: fact, token_b: fact})

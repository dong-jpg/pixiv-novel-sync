from __future__ import annotations

from pathlib import Path


def test_reader_contains_adult_tab_and_codepoint_conversion_contract():
    html = Path("src/pixiv_novel_sync/templates/dashboard_ai_reader.html").read_text(encoding="utf-8")
    assert "成人描写润色" in html
    assert "selectionToCodePointRange" in html
    assert "Array.from" in html
    assert "innerHTML" not in html


def test_reader_does_not_normalize_content_before_offset_submission():
    html = Path("src/pixiv_novel_sync/templates/dashboard_ai_reader.html").read_text(encoding="utf-8")
    assert "replace(/\\r\\n/g" not in html
    assert "/api/dashboard/ai/polish/adult/stream" in html


def test_settings_shows_fixed_policy_hashes_and_binding_capability():
    html = Path("src/pixiv_novel_sync/templates/dashboard_settings.html").read_text(encoding="utf-8")
    assert "adult_safety_policy" in html
    assert "adult_fact_guard_policy" in html
    assert "json" in html


def test_reader_keeps_adult_request_state_and_never_submits_target_text():
    html = Path("src/pixiv_novel_sync/templates/dashboard_ai_reader.html").read_text(encoding="utf-8")
    for state in (
        "selectedRange",
        "candidate",
        "validation",
        "warnings",
        "blockingIssues",
        "providerScopes",
        "accessToken",
    ):
        assert state in html
    assert '"target_text":' not in html
    assert "X-Adult-Access-Token" in html
    assert "X-CSRF-Token" in html


def test_reader_requires_confirmations_and_regenerate_parent_lineage():
    html = Path("src/pixiv_novel_sync/templates/dashboard_ai_reader.html").read_text(encoding="utf-8")
    for contract in (
        "warning_ack_hash",
        "provider_scope_hash",
        "parent_job_id",
        "crypto.randomUUID",
        "applyDisabled",
        "blockingIssues.length",
    ):
        assert contract in html


def test_settings_uses_versioned_character_confirmation_without_agent_controls():
    html = Path("src/pixiv_novel_sync/templates/dashboard_settings.html").read_text(encoding="utf-8")
    for contract in (
        "/characters",
        "expected_revision",
        "adult-confirmation",
        "adult_characters_confirmed",
        "fictional_characters_confirmed",
        "sortedCharacters",
    ):
        assert contract in html
    assert "agent.task_type !== 'adult_polish'" in html

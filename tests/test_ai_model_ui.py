from __future__ import annotations

from pathlib import Path


TEMPLATE = Path(
    "src/pixiv_novel_sync/templates/dashboard_settings.html"
).read_text(encoding="utf-8")
LOG_TEMPLATE = Path(
    "src/pixiv_novel_sync/templates/dashboard_logs.html"
).read_text(encoding="utf-8")


def test_settings_template_contains_catalog_counts_sync_and_empty_confirmation():
    for text in (
        "/models/sync",
        "discovered_available",
        "routable",
        "models_synced_at",
        "models_sync_error",
        "needs_empty_confirmation",
        "旧目录仍可使用",
        "Prompt 可能发送",
        "隐私边界",
    ):
        assert text in TEMPLATE
    assert "api_key_encrypted" not in TEMPLATE


def test_model_sync_mutations_use_csrf_fetch():
    assert "X-CSRF-Token" in TEMPLATE
    assert "confirm-empty" in TEMPLATE
    assert "ensureCsrfToken" in TEMPLATE


def test_model_sync_sse_whitelist_and_refresh_recovery_are_explicit():
    for event in (
        "started",
        "page",
        "empty_confirmation_required",
        "completed",
        "failed",
        "cancelled",
    ):
        assert event in TEMPLATE
    for state_key in (
        "providerModels",
        "modelSearch",
        "modelSyncOperations",
        "loadModelSyncOperation",
        "response.body.getReader",
    ):
        assert state_key in TEMPLATE


def test_provider_catalog_supports_collapsing_and_manual_models():
    for text in (
        "modelCatalogExpanded",
        "manualModelForms",
        "createManualModel",
        "deleteManualModel",
        "manual_capabilities",
        "manual_context_window",
        "/api/dashboard/ai/provider-models/",
        "model.capabilities",
    ):
        assert text in TEMPLATE


def test_settings_template_contains_pool_editor_and_mutual_binding_controls():
    for text in (
        "ai-model-pools",
        "fallback_pool_id",
        "expected_version",
        "binding_type",
        "required_capabilities",
        "streaming",
        "long_context",
        "隐私",
    ):
        assert text in TEMPLATE


def test_pool_save_sends_complete_member_order_not_incremental_positions():
    assert "/members" in TEMPLATE
    assert "expected_version" in TEMPLATE
    assert "members" in TEMPLATE
    assert "movePoolMember" in TEMPLATE
    assert "replacePoolMembers" in TEMPLATE


def test_pool_editor_exposes_references_candidate_limit_and_provider_search():
    for text in (
        "referenced_by_agents",
        "candidate",
        "poolCandidateCount",
        "64",
        "16",
        "poolModelSearch",
        "availablePoolModels",
        "provider_id",
    ):
        assert text in TEMPLATE


def test_logs_template_contains_attempts_budget_and_continue_endpoint():
    for text in (
        "attempts",
        "candidate_snapshot_hash",
        "input_budget",
        "/continue",
        "partial",
        "attempt_count",
        "route_summary",
        "X-CSRF-Token",
    ):
        assert text in LOG_TEMPLATE

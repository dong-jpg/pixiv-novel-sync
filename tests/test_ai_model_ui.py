from __future__ import annotations

from pathlib import Path


TEMPLATE = Path(
    "src/pixiv_novel_sync/templates/dashboard_settings.html"
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

from __future__ import annotations

from pathlib import Path


# 设置页已拆成五个一级页面：Provider / 模型目录 / 模型池在 models 页，
# Agent 绑定（binding_type、required_capabilities）在 agents 页。
TEMPLATE = Path(
    "src/pixiv_novel_sync/templates/dashboard_settings_models.html"
).read_text(encoding="utf-8")
AGENTS_TEMPLATE = Path(
    "src/pixiv_novel_sync/templates/dashboard_settings_agents.html"
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
    """变更类请求必须走 base.html 的 window.csrfFetch（它负责带 X-CSRF-Token）。

    模板自建一份 ensureCsrfToken / 手拼 X-CSRF-Token 就等于把 fb91da3 修好的漏洞
    再挖一遍：漏一处只在配置了 DASHBOARD_TOKEN 的部署上炸，本地永远测不出来。
    """
    assert "window.csrfFetch" in TEMPLATE
    assert "confirm-empty" in TEMPLATE
    assert "function ensureCsrfToken" not in TEMPLATE
    assert "const ensureCsrfToken" not in TEMPLATE


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
    """池编辑器在 models 页，Agent 侧的绑定控件在 agents 页——两边都得有。"""
    for text in (
        "ai-model-pools",
        "fallback_pool_id",
        "expected_version",
        "隐私",
    ):
        assert text in TEMPLATE
    for text in (
        "binding_type",
        "required_capabilities",
        "streaming",
        "long_context",
        "隐私",
    ):
        assert text in AGENTS_TEMPLATE


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


def test_pool_editor_surfaces_recent_real_attempts():
    """模型池的真实尝试记录必须在池编辑界面就能看到。

    /model-pools/<id>/attempts 后端早就存在却从来没有模板调用它，于是「哪个候选成功、
    哪些失败、为什么失败」只能事后去日志页逐个 job 翻。
    """
    assert "将接入" not in TEMPLATE
    assert "/attempts" in TEMPLATE
    for text in ("poolAttempts", "loadPoolAttempts", "poolAttemptsError"):
        assert text in TEMPLATE


def test_pool_attempt_rows_use_the_endpoint_field_names():
    """字段名按 storage/ai/pools.py:list_ai_model_pool_attempts 的真实投影。

    日志页读的是 list_ai_job_model_attempts 的 `SELECT *`（带 _snapshot 后缀），
    池端点已经把它们改名过一遍，照抄日志页会得到一排 undefined。
    """
    for field in (
        "attempt.attempt_index",
        "attempt.provider_name",
        "attempt.model_key",
        "attempt.pool_position",
        "attempt.status",
        "attempt.error_category",
        "attempt.error_message",
        "attempt.latency_ms",
        "attempt.started_at",
        "attempt.job_id",
    ):
        assert field in TEMPLATE, field
    assert "attempt.provider_name_snapshot" not in TEMPLATE
    assert "attempt.pool_name_snapshot" not in TEMPLATE
    # started_at 是 SQLite 的 UTC CURRENT_TIMESTAMP，直接渲染会比本地时间差一个时区
    assert "formatAttemptTime" in TEMPLATE
    assert "toLocaleString('zh-CN'" in TEMPLATE


def test_pool_attempt_status_labels_separate_partial_from_failed():
    """partial 是「已经吐了字才失败」——不能转移到下一个候选，必须和 failed 区分。"""
    assert "attemptStatusLabel" in TEMPLATE
    assert "partial" in TEMPLATE
    assert "已输出" in TEMPLATE
    assert "output_started" in TEMPLATE


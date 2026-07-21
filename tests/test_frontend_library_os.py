from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "pixiv_novel_sync" / "templates"
DOCS = ROOT / "docs"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_base_template_defines_library_os_design_system():
    html = read(TEMPLATES / "base.html")

    assert "data-theme=\"library-os\"" in html
    assert "--library-bg" in html
    assert "--library-accent" in html
    assert "library-shell" in html
    assert "library-sidebar" in html
    assert "library-main" in html


def test_library_main_can_shrink_to_mobile_viewport():
    html = read(TEMPLATES / "base.html")
    library_main_rule = html.split(".library-main {", 1)[1].split("}", 1)[0]

    assert "min-width: 0" in library_main_rule


def test_global_components_use_library_os_classes():
    html = read(TEMPLATES / "vue_components.html")

    assert "library-nav-link" in html
    assert "library-badge" in html
    assert "library-modal" in html
    assert "Library OS" in html


def test_dashboard_pages_are_marked_as_library_pages():
    pages = [
        "dashboard.html",
        "dashboard_follows.html",
        "dashboard_novels.html",
        "dashboard_novel_detail.html",
        "dashboard_series_detail.html",
        "dashboard_user_detail.html",
        "dashboard_pending_deletions.html",
        "dashboard_logs.html",
        "dashboard_settings.html",
        "dashboard_preferences.html",
        "dashboard_ai.html",
        "dashboard_wizard.html",
    ]

    for page in pages:
        html = read(TEMPLATES / page)
        assert "library-page" in html, page
        assert "library-page-header" in html, page


def test_ai_and_wizard_templates_do_not_embed_other_workspace():
    ai = read(TEMPLATES / "dashboard_ai.html")
    wizard = read(TEMPLATES / "dashboard_wizard.html")

    assert "loadChatSessions" not in ai
    assert "openNewWizardSession" not in ai
    assert "/api/dashboard/ai/chat/" not in ai
    assert "showImportMaterialModal" not in ai
    assert "distillForm" not in ai
    assert "readerView" not in ai
    assert "providerForm" not in ai
    assert "continueForm" not in ai
    assert "auditForm" not in ai
    assert "promptForm" not in ai
    assert "'novel-search':" not in ai
    assert "'series-search':" not in ai
    assert "providerForm" not in ai
    assert "continueForm" not in ai
    assert "auditForm" not in ai
    assert "promptForm" not in ai
    assert "'novel-search':" not in ai
    assert "'series-search':" not in ai
    assert "providerForm" not in ai
    assert "continueForm" not in ai
    assert "auditForm" not in ai
    assert "promptForm" not in ai
    assert "'novel-search':" not in ai
    assert "'series-search':" not in ai
    assert 'v-if="false"' not in ai
    assert "loadChapterDashboard" not in wizard
    assert "startChapterPipeline" not in wizard
    assert "pageMode" not in ai
    assert "pageMode" not in wizard


def test_ai_pages_share_complete_output_panel_component():
    ai = read(TEMPLATES / "dashboard_ai.html")
    wizard = read(TEMPLATES / "dashboard_wizard.html")
    panel = read(TEMPLATES / "dashboard_ai_output_panel.html")

    include = '{% include "dashboard_ai_output_panel.html" %}'
    assert include in ai
    assert include in wizard
    assert "window.aiOutputPanelComponent" in ai
    assert "window.aiOutputPanelComponent" in wizard
    assert "emits: ['save', 'detect']" in panel
    assert "showDetect" in panel


def test_wizard_preserves_distill_sources_and_merge_controls():
    wizard = read(TEMPLATES / "dashboard_wizard.html")

    assert 'value="archive_novel"' in wizard
    assert 'value="archive_series"' in wizard
    assert 'value="document"' in wizard
    assert "distillForm.full_text" in wizard
    assert "distillForm.batch_size" in wizard
    assert "importOverwriteFields" in wizard
    assert "payload.overwrite_fields" in wizard


def test_dashboard_cards_stretch_and_recommendations_have_error_state():
    html = read(TEMPLATES / "dashboard.html")

    assert 'data-dashboard-card="activity"' in html
    assert 'data-dashboard-card="scheduler"' in html
    assert html.count("h-full flex flex-col") >= 2
    assert "recommendationError" in html
    assert "推荐结果加载失败" in html
    assert "retryRecommendationItems" in html


def test_current_frontend_docs_describe_task_logs_and_ai_pages():
    readme = read(ROOT / "README.md")
    contract = read(DOCS / "frontend-api-contract.md")

    assert "默认保留 3 天" in readme
    assert "保留最近 7 天" not in readme
    assert "/dashboard/novels?category=rescue" in readme
    assert "userscripts/pixiv-rescue.user.js" in readme
    assert "| `/dashboard/logs` | `dashboard_logs.html` | 任务日志 |" in contract
    assert "/dashboard/wizard" in contract
    assert "/dashboard/novels/ai/<project_id>" in contract
    assert "/api/dashboard/ai/projects/<project_id>/cover" in contract


def test_frontend_pages_document_current_ai_boundaries():
    pages = read(DOCS / "frontend-pages.md")
    studio = read(DOCS / "AI_WRITING_STUDIO_PLAN.md")

    assert "AI 创作小说" in pages
    assert "`/dashboard/wizard`" in pages
    assert "`dashboard_ai_reader.html`" in pages
    assert "AI 创作任务已迁移到全局任务日志" in studio


def test_task_logs_template_has_complete_ai_filters_and_details():
    html = read(TEMPLATES / "dashboard_logs.html")

    assert "filters.status" in html
    assert "/api/dashboard/ai/jobs/" in html
    assert "selectedLog.job_id || selectedLog.id" in html
    assert '<option value="7">7 天</option>' not in html
    assert "polish_dialogue" in html
    assert "polish_psychology" in html
    assert "keyword_clean" in html
    assert "'cancelled': { label: '已取消'" in html
    assert 'v-html="formatResult(log)"' not in html
    assert "selectedLog.output_text" in html
    assert "formatJson(selectedLog.input)" in html
    assert "formatJson(selectedLog.output)" in html


def test_ai_project_pages_prefer_cover_url_with_gradient_fallback():
    novels = read(TEMPLATES / "dashboard_novels.html")
    reader = read(TEMPLATES / "dashboard_ai_reader.html")
    studio = read(TEMPLATES / "dashboard_ai.html")

    assert "item.cover_url" in novels
    assert ":src=\"item.cover_url\"" in novels
    assert "project?.cover_url" in reader
    assert "currentProject?.cover_url" in studio
    assert "coverGradient" in reader
    assert "uploadProjectCover" in studio
    assert "deleteProjectCover" in studio
    assert "data.cover_url + '?v=' + Date.now()" in studio


def test_ai_dashboard_api_adds_csrf_to_mutating_requests():
    html = read(TEMPLATES / "dashboard_ai.html")

    assert "ensureCsrfToken" in html
    assert "async function csrfFetch" in html
    assert "'/api/csrf-token'" in html
    assert "'X-CSRF-Token': token" in html
    assert "return window.fetch(url, opts)" in html
    assert "await fetch(" not in html


def test_ai_project_overview_uses_single_panel_and_preserves_independent_actions():
    html = read(TEMPLATES / "dashboard_ai.html")
    overview = html.split('v-show="projectDetailTab === \'overview\'"', 1)[1].split(
        "<!-- 长篇规划 -->",
        1,
    )[0]
    profile_save = html.split("async function saveProjectProfiles()", 1)[1].split(
        "function addStyleTag",
        1,
    )[0]

    assert overview.count("data-overview-panel") == 1
    assert overview.count("data-overview-section") == 3
    assert 'data-overview-section="project"' in overview
    assert 'data-overview-section="profiles"' in overview
    assert 'data-overview-section="style"' in overview
    assert "data-overview-card" not in overview
    assert "items-stretch" not in overview
    section_openings = [
        overview.split(f'data-overview-section="{name}"', 1)[1].split(">", 1)[0]
        for name in ("project", "profiles", "style")
    ]
    assert all("h-full" not in opening for opening in section_openings)

    project_section = overview.split('data-overview-section="project"', 1)[1].split(
        'data-overview-section="profiles"',
        1,
    )[0]
    profiles_section = overview.split('data-overview-section="profiles"', 1)[1].split(
        'data-overview-section="style"',
        1,
    )[0]
    style_section = overview.split('data-overview-section="style"', 1)[1]

    assert '@click="$refs.coverInput.click()"' in project_section
    assert '@click="deleteProjectCover"' in project_section
    assert '@click="saveProjectMeta"' in project_section
    assert '@click="saveProjectProfiles"' in profiles_section
    assert "/dashboard/wizard?tab=distill" in profiles_section
    assert '@click="saveProjectStyleControl"' in style_section
    assert "async function saveProjectStyleControl()" in html
    assert "settings:" not in profile_save
    assert html.count("await saveProjectStyleControl()") >= 2


def test_ai_project_overview_keeps_project_summary_compact_at_narrow_desktop():
    html = read(TEMPLATES / "dashboard_ai.html")
    overview = html.split('v-show="projectDetailTab === \'overview\'"', 1)[1].split(
        "<!-- 长篇规划 -->",
        1,
    )[0]
    project_section = overview.split('data-overview-section="project"', 1)[1].split(
        'data-overview-section="profiles"',
        1,
    )[0]

    assert "lg:grid-cols-[7rem_minmax(0,1.35fr)_minmax(16rem,1fr)]" in project_section


def test_library_contains_rescue_tab_and_api_contract():
    html = read(TEMPLATES / "dashboard_novels.html")

    assert "filters.category = 'rescue'" in html
    assert "['bookmark', 'following', 'ai', 'rescue']" in html
    assert "/api/dashboard/rescues" in html
    assert "rescueFilters.state" in html
    assert "rescueFilters.item_type" in html
    assert '<option v-if="filters.category !== \'rescue\'" value="bookmarks_desc">' in html
    assert '<option v-if="filters.category !== \'rescue\'" value="views_desc">' in html
    assert "完整救援" in html
    assert "部分救援" in html
    assert "来自私人备份" in html


def test_rescue_detail_pages_support_manual_override_with_csrf():
    novel = read(TEMPLATES / "dashboard_novel_detail.html")
    series = read(TEMPLATES / "dashboard_series_detail.html")

    for html, item_type in ((novel, "novel"), (series, "series")):
        assert "rescueOverride" in html
        assert "rescueMessage" in html
        assert "ensureCsrfToken" in html
        assert "X-CSRF-Token" in html
        assert f"const itemType = '{item_type}'" in html
        assert "/api/dashboard/rescue-overrides/" in html
        assert "saveRescueOverride" in html
        assert "clearRescueOverride" in html

    assert "complete_count" in series
    assert "expected_count" in series


def test_settings_contains_rescue_token_rotation():
    html = read(TEMPLATES / "dashboard_settings.html")

    assert "rescue-api" in html
    assert "/api/dashboard/rescue-token/status" in html
    assert "/api/dashboard/rescue-token/rotate" in html
    assert "rescueTokenPlaintext" in html
    assert "closeRescueToken" in html
    assert "copyRescueToken" in html


def test_frontend_contract_documents_exist_and_cover_core_topics():
    contract = read(DOCS / "frontend-api-contract.md")
    pages = read(DOCS / "frontend-pages.md")
    style = read(DOCS / "library-os-style-guide.md")

    for endpoint in [
        "GET /api/dashboard/status",
        "GET /api/dashboard/novels",
        "GET /api/dashboard/logs",
        "GET /api/dashboard/settings",
        "POST /api/dashboard/ai/continue/stream",
        "GET /proxy/image?url=...",
    ]:
        assert endpoint in contract

    for route in [
        "/dashboard",
        "/dashboard/novels",
        "/dashboard/preferences",
        "/dashboard/ai",
        "/token-login",
    ]:
        assert route in pages

    for token in ["--library-bg", "--library-surface", "--library-accent", "library-card", "library-table"]:
        assert token in style


def test_rescue_pages_and_api_contract_are_documented():
    pages = read(DOCS / "frontend-pages.md")
    contract = read(DOCS / "frontend-api-contract.md")

    assert "/dashboard/novels?category=rescue" in pages
    assert "userscripts/pixiv-rescue.user.js" in pages
    assert "拯救成功" in pages
    assert "救援 Token" in pages
    for endpoint in [
        "GET /api/dashboard/rescues",
        "PUT /api/dashboard/rescue-overrides/<item_type>/<item_id>",
        "DELETE /api/dashboard/rescue-overrides/<item_type>/<item_id>",
        "GET /api/dashboard/rescue-token/status",
        "POST /api/dashboard/rescue-token/rotate",
        "GET /api/rescue/v1/novels/<novel_id>",
        "GET /api/rescue/v1/series/<series_id>",
        "GET /api/rescue/v1/series/<series_id>/chapters",
    ]:
        assert endpoint in contract
    for security_term in [
        "Authorization: Bearer",
        "X-CSRF-Token",
        "Cache-Control: no-store",
        "X-Robots-Tag",
        "401",
        "404",
        "405",
        "429",
        "source_notice",
    ]:
        assert security_term in contract

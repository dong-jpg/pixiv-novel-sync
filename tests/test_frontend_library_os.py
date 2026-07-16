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
    ]

    for page in pages:
        html = read(TEMPLATES / page)
        assert "library-page" in html, page
        assert "library-page-header" in html, page


def test_dashboard_ai_wizard_has_section_navigation():
    html = read(TEMPLATES / "dashboard_ai.html")

    assert 'v-if="pageMode === \'wizard\'"' in html
    assert 'v-for="tab in tabs"' in html
    assert "switchTab(tab.id)" in html


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
    assert "'/api/csrf-token'" in html
    assert "'X-CSRF-Token': token" in html


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

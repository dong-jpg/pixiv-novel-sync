from pathlib import Path
import re

from flask import Flask, render_template

from work_analysis_api_helpers import facade_fixture


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src/pixiv_novel_sync/templates"


def render_preferences(work_view):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.jinja_env.variable_start_string = "{["
    app.jinja_env.variable_end_string = "]}"
    with app.test_request_context("/dashboard/preferences"):
        return render_template("dashboard_preferences.html", work_analysis_view=work_view,
                               task_labels={}, csp_nonce=lambda: "fixture-nonce")


def test_preferences_default_and_work_view_mount_different_existing_page_setups():
    old = render_preferences(False)
    work = render_preferences(True)
    assert 'id="preference-management"' in old
    assert "window.useRecommendations()" in old
    assert "/static/js/work_analysis.js" not in old
    assert 'id="work-analysis"' in work
    assert "window.useRecommendations()" not in work
    assert "recommendations/search-plan" not in work
    assert work.count("window.initVueApp(") == 1
    assert '<script nonce="fixture-nonce" src="/static/js/work_analysis.js"></script>' in work
    assert work.index("/static/js/ai_picker.js") < work.index("/static/js/work_analysis.js")
    assert work.index("/static/js/work_analysis.js") < work.index("WorkAnalysis.createController")


def test_work_partial_never_renders_html_or_adds_an_external_generation_action():
    html = (TEMPLATES / "dashboard_work_analysis.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static/js/work_analysis.js").read_text(encoding="utf-8")
    assert "v-html" not in html
    assert "innerHTML" not in script
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "streamSSE" not in script and "EventSource" not in script
    assert "/continue" not in script and "/probe" not in script
    assert 'data-evidence-id' in html
    assert 'open="' not in html
    assert 'budget_confirmed' in html and 'current_archive_only' in html
    for name in ("dashboard_novel_detail.html", "dashboard_series_detail.html"):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "/dashboard/preferences?view=work-analysis" in source
    assert "useReaderEvidence" in (TEMPLATES / "dashboard_novel_detail.html").read_text(encoding="utf-8")


def test_reader_detail_includes_current_body_hash_without_mutating_it(tmp_path):
    fixture = facade_fixture(tmp_path / "reader.db")
    try:
        raw = fixture.db.conn.execute("SELECT text_hash FROM novel_texts WHERE novel_id=101").fetchone()[0]
        assert fixture.db.get_novel_detail(101)["text_hash"] == raw
        assert fixture.db.conn.execute("SELECT count(*) FROM work_analysis_runs").fetchone()[0] == 0
    finally:
        fixture.ai.close()
        fixture.db.close()


def test_work_js_and_partial_are_in_existing_wheel_package_data():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"templates/*.html"' in project and '"static/js/*.js"' in project
    assert (TEMPLATES / "dashboard_work_analysis.html").is_file()
    assert (TEMPLATES / "dashboard_work_analysis_scripts.html").is_file()
    assert (TEMPLATES.parent / "static/js/work_analysis.js").is_file()

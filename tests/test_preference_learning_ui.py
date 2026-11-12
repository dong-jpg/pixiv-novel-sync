from pathlib import Path
import re
import shutil
import subprocess

from flask import Flask, render_template
import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src/pixiv_novel_sync/templates"


def render_page(name, view=None):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.jinja_env.variable_start_string = "{["
    app.jinja_env.variable_end_string = "]}"
    with app.test_request_context("/dashboard/preferences" + ("?view=" + view if view else "")):
        return render_template(
            name,
            work_learning_view=view == "learning",
            work_analysis_view=view == "work-analysis",
            task_labels={},
            novel_id=101,
            series_id=501,
            csp_nonce=lambda: "test-nonce",
        )


def test_learning_view_has_one_setup_and_preserves_navigation_to_existing_views():
    html = render_page("dashboard_preferences.html", "learning")
    assert html.count("window.initVueApp(") == 1
    assert 'id="preference-learning"' in html
    assert 'href="/dashboard/preferences?view=work-analysis"' in html
    assert 'href="/dashboard/preferences?view=learning"' in html
    assert "window.useRecommendations()" not in html
    assert html.index("/static/js/ai_picker.js") < html.index("/static/js/preference_learning.js")
    assert html.index("root.PreferenceFeedback =") < html.index("PreferenceLearning.createController")


@pytest.mark.parametrize("view", [None, "work-analysis"])
def test_existing_preferences_views_do_not_load_new_learning_controller(view):
    html = render_page("dashboard_preferences.html", view)
    assert "/static/js/preference_learning.js" not in html
    assert html.count("window.initVueApp(") == 1
    assert ('id="work-analysis"' in html) == (view == "work-analysis")
    assert ('id="preference-management"' in html) == (view is None)


@pytest.mark.parametrize("name", ["dashboard_novel_detail.html", "dashboard_series_detail.html"])
def test_readers_include_feedback_without_new_eager_script_fetches(name):
    html = render_page(name)
    assert 'aria-label="阅读喜好评价"' in html
    assert '@click="feedback.toggle"' in html
    assert "root.PreferenceFeedback =" in html
    assert "/static/js/preference_learning.js" not in html
    assert "PreferenceFeedback.createController(Vue, itemType," in html


def test_sensitive_profile_and_evidence_content_remain_text_and_collapsed():
    html = render_page("dashboard_preferences.html", "learning")
    assert 'id="learning-profile-result"' in html
    assert not re.search(r"<details\b[^>]*\sopen(?:\s|=|>)", html)
    sensitive_template = (TEMPLATES / "dashboard_preference_learning.html").read_text(encoding="utf-8")
    assert "v-html" not in sensitive_template
    source = (TEMPLATES.parent / "static/js/preference_learning.js").read_text(encoding="utf-8")
    feedback = (TEMPLATES / "preference_feedback_scripts.html").read_text(encoding="utf-8")
    for text in (source, feedback):
        assert "innerHTML" not in text
        assert "localStorage" not in text and "sessionStorage" not in text


def test_preference_learning_runtime():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for preference-learning runtime regression")
    result = subprocess.run(
        [node, "--test", str(ROOT / "tests/test_preference_learning_runtime.cjs")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

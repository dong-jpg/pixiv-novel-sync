"""Shared-shell source/render contracts plus executable, non-browser VM regressions.

CSS declarations are guardrails, not evidence of rendered geometry. The controller
separately verifies real layout, focus and scrolling through CUA.
"""

from pathlib import Path
import re
import shutil
import subprocess

from flask import Flask, render_template
from jinja2 import ChoiceLoader, DictLoader
import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "pixiv_novel_sync" / "templates"


def source(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def component(name: str) -> str:
    return source("vue_components.html").split(
        f"app.component('{name}',", 1
    )[1].split("app.component('", 1)[0]


def css_rule(selector: str) -> str:
    css = source("base.html").split("<style>", 1)[1].split("</style>", 1)[0]
    match = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", css)
    assert match, f"Missing shared-shell style: {selector}"
    return match.group(1)


def test_body_class_is_rendered_for_reader_without_changing_other_pages():
    # A standalone Jinja app avoids importing the server or loading configuration.
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.jinja_env.variable_start_string = "{["
    app.jinja_env.variable_end_string = "]}"
    app.jinja_env.loader = ChoiceLoader([
        DictLoader({
            "reader-test.html": "{% extends 'base.html' %}"
            "{% block body_class %}pns-reader-page{% endblock %}",
        }),
        app.jinja_env.loader,
    ])
    with app.test_request_context("/dashboard/novels/123"):
        reader = render_template("reader-test.html", task_labels={})
        default = render_template("base.html", task_labels={})
    assert '<body class="antialiased pns-reader-page">' in reader
    assert "pns-reader-page" not in re.search(r"<body[^>]*>", default)[0]
    assert "app-sidebar-nav" in default
    assert "app-mobile-bar" in default


def test_reader_hides_mobile_navigation_only_inside_mobile_breakpoint():
    html = source("base.html")
    mobile_css = html.split("@media(max-width:1023px)", 1)[1]
    assert ".pns-reader-page .mobile-bottom-bar {" in mobile_css
    rule = mobile_css.split(".pns-reader-page .mobile-bottom-bar {", 1)[1].split("}", 1)[0]
    assert "display: none !important" in rule
    assert ".pns-reader-page .library-main" in mobile_css


def test_mobile_navigation_is_a_named_four_link_bar_with_a_more_dialog():
    mobile = component("app-mobile-bar")
    assert 'aria-label="主导航"' in mobile
    assert 'aria-haspopup="dialog"' in mobile
    assert ':aria-expanded="moreOpen"' in mobile
    assert 'aria-controls="mobile-more-dialog"' in mobile
    assert '<app-modal' in mobile
    assert 'id="mobile-more-dialog"' in mobile
    assert 'aria-label="更多导航"' in mobile
    assert ':aria-current=' in mobile
    assert '@click="logout"' in mobile
    assert 'role="alert"' in mobile


def test_shared_touch_targets_and_pagination_do_not_trade_away_minimum_size():
    pagination = component("app-pagination")
    assert "!min-h-0" not in pagination
    assert 'aria-label="分页"' in pagination
    assert 'aria-label="上一页"' in pagination
    assert 'aria-label="下一页"' in pagination
    assert pagination.count('type="button"') == 2
    assert "min-height: 44px" in css_rule(".library-btn")
    assert "min-width: 44px" in css_rule(".library-btn")
    assert "flex-wrap: wrap" in css_rule(".library-pagination")
    assert "min-height: 44px" in css_rule(".library-modal-close")


def test_shell_uses_safe_areas_shrinkable_forms_and_local_table_scrolling():
    html = source("base.html")
    assert "viewport-fit=cover" in html
    for edge in ("top", "right", "bottom", "left"):
        assert f"env(safe-area-inset-{edge})" in html
    assert "--mobile-bar-height" in html
    assert "calc(var(--mobile-bar-height)" in html
    assert "min-width: 0" in css_rule(".library-main")
    assert "min-width: 0" in css_rule(".library-content")
    assert "max-width: 100%" in css_rule(".library-input")
    assert "min-width: 0" in css_rule(".library-input")
    assert "font-size: 16px" in html.split("@media(max-width:1023px)", 1)[1]
    assert "overscroll-behavior-x: contain" in css_rule(".library-page .overflow-x-auto")
    assert not re.search(r"overflow-x\s*:\s*(hidden|clip)", html)
    assert ":focus-visible" in html


def test_modal_has_bounded_scrollable_content_and_wrapping_footer():
    modal = component("app-modal")
    assert 'role="dialog"' in modal
    assert 'aria-modal="true"' in modal
    assert ':aria-labelledby="titleId"' in modal
    assert 'aria-label="关闭"' in modal
    assert 'to="body"' in modal, "Dialog must escape page stacking/overflow contexts"
    assert "100dvh" in css_rule(".library-modal")
    dialog_css = css_rule(".library-modal-dialog")
    assert "max-height: 100%" in dialog_css
    assert "overflow-y: auto" in dialog_css
    assert "flex-wrap: wrap" in css_rule(".library-modal-footer")
    assert "max-h-[70vh]" not in modal


def test_mobile_shell_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for shared-shell runtime unit tests")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "test_mobile_shell_runtime.cjs")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

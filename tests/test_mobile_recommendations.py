"""Recommendation structure and executable VM regressions (not browser evidence)."""

from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "pixiv_novel_sync" / "templates"
PAGES = ("dashboard.html", "dashboard_preferences.html")


def read(name):
    path = TEMPLATES / name
    assert path.is_file(), f"Missing shared recommendation partial: {path}"
    return path.read_text(encoding="utf-8")


def css_rule(css, selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", css)
    assert match, f"Missing scoped rule: {selector}"
    return match[1]


@pytest.mark.parametrize("name", PAGES)
def test_both_pages_install_and_use_the_shared_recommendation_components(name):
    html = read(name)
    include = '{% include "recommendation_components.html" %}'
    assert html.count(include) == 1
    assert html.index(include) < html.index("initVueApp(")
    assert '<recommendation-card' in html
    assert 'v-for="item in recommendationItems"' in html
    assert ':item="item"' in html
    assert ':busy="feedbackBusy[item.id]"' in html
    assert ':error="feedbackErrors[item.id]"' in html
    assert '@feedback="sendRecommendationFeedback(item, $event)"' in html
    assert "window.useRecommendations()" in html
    # Requests, pagination, formatting and feedback must not fork between pages.
    assert "/api/dashboard/recommendations/items" not in html
    assert "visibleRecommendations" not in html


class CardMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.links = []
        self.buttons = []
        self.articles = []
        self.elements = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs, tuple(self.stack)))
        if tag == "button":
            assert "a" not in self.stack, "Feedback/expand controls cannot be inside the Pixiv link"
            self.buttons.append(attrs)
        if tag == "a":
            self.links.append(attrs)
        if tag == "article":
            self.articles.append(attrs)
        if tag not in {"input", "br", "hr"}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, f"Mismatched card markup: {tag}"
        self.stack.pop()


@pytest.mark.parametrize("name", PAGES)
def test_page_content_keeps_balanced_markup_and_non_nested_actions(name):
    html = read(name).split("{% block content %}", 1)[1].split("{% endblock %}", 1)[0]
    parser = CardMarkup()
    parser.feed(html)
    assert not parser.stack


def test_shared_card_separates_external_links_from_feedback_and_expansion():
    html = read("recommendation_components.html")
    match = re.search(r"template:\s*`([\s\S]*?)`", html)
    assert match, "The registered card needs a Vue template"
    parser = CardMarkup()
    parser.feed(match[1])
    assert not parser.stack
    assert len(parser.articles) == 1
    assert parser.links
    for link in parser.links:
        assert link.get("target") == "_blank"
        assert set(link.get("rel", "").split()) >= {"noopener", "noreferrer"}
    assert len(parser.buttons) >= 3
    assert any(":aria-expanded" in button and ":aria-controls" in button for button in parser.buttons)
    assert 'role="alert"' in match[1]
    assert 'role="status"' in match[1]
    assert ':disabled="busy"' in match[1]
    assert "recommendation-tags" in match[1]
    # library-badge alone has no CSS; reuse the styled global component, not bare spans.
    assert match[1].count("<app-badge") == 3


def test_mobile_layout_stacks_the_header_and_removes_nested_homepage_gutters():
    html = read("recommendation_components.html")
    header = css_rule(html, ".pns-recommendations-page .recommendation-panel-header")
    assert "flex-direction: column" in header
    controls = css_rule(html, ".pns-recommendations-page .recommendation-controls")
    assert "flex-wrap: wrap" in controls
    assert "min-width: 0" in controls
    listing = css_rule(html, ".pns-recommendations-page .recommendation-list")
    assert "grid-template-columns: minmax(0, 1fr)" in listing
    assert "@media (min-width:" in html, "Keep a desktop adaptation"
    dashboard = read("dashboard.html")
    assert "pns-recommendations-page" in dashboard
    body = re.search(r'<div class="([^"]*dashboard-content[^"]*)"', dashboard)
    assert body
    assert not re.search(r"(?:^|\s)(?:\w+:)?p[xlr]-", body[1]), "Do not stack another horizontal page gutter"


def test_card_title_reason_tags_and_touch_targets_have_scoped_mobile_rules():
    html = read("recommendation_components.html")
    title = css_rule(html, ".pns-recommendations-page .recommendation-title-link")
    assert "-webkit-line-clamp: 2" in title
    assert "min-height: 3em" in title
    assert "line-height: 1.5" in title
    assert "overflow-wrap: anywhere" in title
    assert "font-size: 16px" in title
    for selector in (".recommendation-controls > *", ".recommendation-actions button", ".recommendation-reason-toggle"):
        rule = css_rule(html, ".pns-recommendations-page " + selector)
        assert "min-height: 44px" in rule
    tag = css_rule(html, ".pns-recommendations-page .recommendation-tag")
    assert "max-width: 100%" in tag
    assert "overflow-wrap: anywhere" in tag
    assert "var(--library-" in html


@pytest.mark.parametrize("name", PAGES)
def test_each_page_has_loading_error_retry_filtered_empty_and_pagination(name):
    html = read(name)
    assert 'v-model="hideFeedback"' in html
    assert 'v-if="loadingRecommendations"' in html
    assert 'v-else-if="recommendationError"' in html
    assert 'v-else-if="recommendationItems.length"' in html
    assert '@click="retryRecommendationItems"' in html
    assert ':page="recommendationPage"' in html
    assert ':pages="recommendationTotalPages"' in html
    assert '@update:page="changeRecommendationPage"' in html
    assert "暂无未反馈的推荐" in html
    assert "还没有推书结果" in html
    assert "recommendationTotal" in html


@pytest.mark.parametrize("name", PAGES)
def test_explicit_paging_has_a_stable_keyboard_focus_target(name):
    html = read(name)
    heading = re.search(r'<h2\b[^>]*id="recommendation-results-heading"[^>]*>', html)
    assert heading, "Explicit paging needs a named, stable results heading"
    assert 'tabindex="-1"' in heading[0]
    assert heading.start() < html.index('v-if="loadingRecommendations"')
    assert html.count('id="recommendation-results-heading"') == 1


def test_homepage_keeps_stats_and_task_controls_in_a_compact_disclosure():
    html = read("dashboard.html")
    header = html.split("</header>", 1)[0]
    assert "dashboard-stats" in header
    assert "<details" in header
    assert "<summary" in header
    assert "任务控制" in header
    for value in ("小说总数", "关注作者", "追更系列", "待确认", "toggleAutoSync", "stopAutoTask"):
        assert value in header


def test_preferences_show_results_before_independent_profile_and_search_management():
    html = read("dashboard_preferences.html")
    assert 'id="recommendation-results"' in html
    assert 'id="preference-management"' in html
    results = html.index('id="recommendation-results"')
    management = html.index('id="preference-management"')
    assert results < management < html.index('v-if="pageLoading"')
    assert results < html.index("增量分析进度")
    assert "画像与搜索设置" in html
    assert '@click="analyze"' in html
    assert '@click="runRecommendations"' in html
    assert '@mute-author="muteRecommendationAuthor(item)"' in html
    assert ':allow-mute="true"' in html
    assert "@mute-author" not in read("dashboard.html")


def test_profile_errors_are_visible_outside_closed_management_with_read_only_retry():
    html = read("dashboard_preferences.html").split("{% block content %}", 1)[1].split("{% endblock %}", 1)[0]
    parser = CardMarkup()
    parser.feed(html)
    errors = [(attrs, ancestors) for _, attrs, ancestors in parser.elements if attrs.get("id") == "profile-load-error"]
    assert len(errors) == 1, "The persistent profile error needs a panel outside the disclosure"
    attrs, ancestors = errors[0]
    assert attrs.get("v-if") == "profileError"
    assert attrs.get("role") == "alert"
    assert "details" not in ancestors
    retries = [(attrs, ancestors) for tag, attrs, ancestors in parser.elements if tag == "button" and attrs.get("@click") == "retryProfiles"]
    assert len(retries) == 1
    attrs, ancestors = retries[0]
    assert attrs.get(":disabled") == "pageLoading"
    assert attrs.get("type") == "button"
    assert "library-btn" in attrs.get("class", "").split()
    assert "details" not in ancestors
    assert html.index('id="recommendation-results"') < html.index('id="profile-load-error"') < html.index('id="preference-management"')
    assert any(attrs.get("v-if") == "profileError" and "summary" in ancestors for _, attrs, ancestors in parser.elements)


def test_mobile_recommendations_runtime():
    node = shutil.which("node")
    assert node, "Node.js is required for recommendation behavior verification"
    result = subprocess.run(
        [node, "--test", str(ROOT / "tests" / "test_mobile_recommendations_runtime.cjs")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr

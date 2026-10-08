"""Task 4 contracts; no application/server/provider or real database is opened."""

from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, StrictUndefined
import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src/pixiv_novel_sync/templates"
PAGES = (
    "dashboard_novel_detail.html",
    "dashboard_novels.html",
    "dashboard_series_detail.html",
    "dashboard_user_detail.html",
    "dashboard_follows.html",
)


class Markup(HTMLParser):
    """Inspect actual template ancestry without pretending to run a browser."""

    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.nodes = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "parents": self.stack.copy()}
        self.nodes.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break


@pytest.mark.parametrize("name", PAGES)
def test_task4_pages_render_real_jinja_and_load_navigation_before_setup(name):
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        variable_start_string="{[", variable_end_string="]}",
        undefined=StrictUndefined, autoescape=True,
    )
    html = env.get_template(name).render(
        request=SimpleNamespace(path="/dashboard/novels/1"), task_labels={},
    )
    assert "window.safeDashboardReturn" in html
    assert html.index("window.safeDashboardReturn") < html.rindex("initVueApp({")
    assert "document.referrer" not in (TEMPLATES / name).read_text(encoding="utf-8")
    if name == "dashboard_novel_detail.html":
        assert re.search(r'<body class="[^"]*\bpns-reader-page\b', html)


def test_reader_has_one_in_place_safe_area_toolbar_and_secondary_danger_actions():
    html = (TEMPLATES / "dashboard_novel_detail.html").read_text(encoding="utf-8")
    nodes = Markup(html).nodes
    toolbars = [n for n in nodes if n["attrs"].get("aria-label") == "阅读工具"]
    assert len(toolbars) == 1, "standalone novels also need an in-place reading toolbar"
    toolbar = toolbars[0]
    buttons = [n for n in nodes if n["tag"] == "button" and toolbar in n["parents"]]
    actions = {n["attrs"].get("@click") for n in buttons}
    assert {"saveServerProgress", "cycleFontSize", "showChapters = true",
            "showReaderActions = true"} <= actions
    save = next(n for n in buttons if n["attrs"].get("@click") == "saveServerProgress")
    assert save["attrs"].get(":disabled") == "progressBusy"
    for action in ("resetServerProgress", "removeLocalBookmark", "deleteThisNovel"):
        controls = [n for n in nodes if n["attrs"].get("@click") == action]
        assert len(controls) == 1
        assert any(p["tag"] == "app-modal" and
                   p["attrs"].get(":is-open") == "showReaderActions"
                   for p in controls[0]["parents"]), action
    assert re.search(r"\.reader-toolbar\s*\{[^}]*position:\s*fixed", html)
    assert re.search(r"\.library-reader-page\s*\{[^}]*padding-bottom:[^;]*safe-area-inset-bottom", html)
    assert re.search(r"\.reader-tool\s*\{[^}]*min-height:\s*44px", html)
    assert "chapter-bottom-bar" not in html, "do not stack the old chapter bar under the toolbar"


def test_rescue_source_controls_are_accessible_siblings_of_work_links():
    html = (TEMPLATES / "dashboard_novels.html").read_text(encoding="utf-8")
    nodes = Markup(html).nodes
    controls = [n for n in nodes if n["attrs"].get("@click.stop") == "toggleSources(item)"]
    assert len(controls) == 1, "a touch/keyboard control must replace hover-only source details"
    control = controls[0]
    assert control["tag"] == "button" and control["attrs"].get("type") == "button"
    assert control["attrs"].get(":aria-expanded") == "sourcesExpanded(item)"
    assert ":aria-controls" in control["attrs"]
    assert not any(p["tag"] == "a" for p in control["parents"])
    assert any(p["attrs"].get(":key") == "item.item_type + '-' + item.item_id"
               for p in control["parents"])
    details = [n for n in nodes if n["attrs"].get("v-if") == "sourcesExpanded(item)"]
    assert details and ":id" in details[0]["attrs"]
    assert not any(p["tag"] == "a" for p in details[0]["parents"])


@pytest.mark.parametrize("name", ["dashboard_user_detail.html", "dashboard_series_detail.html"])
def test_detail_return_targets_have_a_44px_minimum(name):
    nodes = Markup((TEMPLATES / name).read_text(encoding="utf-8")).nodes
    back = next(n for n in nodes if n["tag"] == "a" and n["attrs"].get(":href") == "backHref")
    assert "min-h-[44px]" in back["attrs"].get("class", "")
    if name == "dashboard_series_detail.html":
        author = next(n for n in nodes if n["tag"] == "a" and "series.user_id" in n["attrs"].get(":href", ""))
        assert "min-h-[44px]" in author["attrs"].get("class", "")


def test_reader_header_wraps_unbroken_metadata():
    html = (TEMPLATES / "dashboard_novel_detail.html").read_text(encoding="utf-8")
    assert re.search(r"\.novel-header\s*\{[^}]*overflow-wrap:\s*anywhere", html)


@pytest.mark.parametrize("dialog_state,feedback_id", [
    ("showReaderActions", "reader-action-feedback"),
    ("showChapters", "reader-chapter-action-feedback"),
])
def test_reader_dialog_feedback_is_in_a_scoped_live_footer(dialog_state, feedback_id):
    html = (TEMPLATES / "dashboard_novel_detail.html").read_text(encoding="utf-8")
    nodes = Markup(html).nodes
    feedback = [n for n in nodes if n["attrs"].get("id") == feedback_id]
    assert len(feedback) == 1, "action results must be inside the active dialog, not a body toast"
    feedback = feedback[0]
    modal = next(p for p in feedback["parents"] if p["tag"] == "app-modal")
    assert modal["attrs"].get(":is-open") == dialog_state
    assert "reader-feedback-dialog" in modal["attrs"].get("class", "")
    assert any(p["tag"] == "template" and "#footer" in p["attrs"] for p in feedback["parents"])
    assert feedback["attrs"].get("aria-atomic") == "true"
    assert feedback["attrs"].get("tabindex") == "0", "long, bounded feedback must also be keyboard-scrollable"
    assert "readerActionMessageType" in feedback["attrs"].get(":role", "")
    assert "readerActionMessageType" in feedback["attrs"].get(":aria-live", "")
    assert "v-html" not in feedback["attrs"], "server messages must be text, not HTML"
    assert re.search(r"\.reader-feedback-dialog \.library-modal-footer\s*\{[^}]*position:\s*sticky", html)
    assert re.search(r"\.reader-action-feedback\s*\{[^}]*overflow-wrap:\s*anywhere", html)


def test_reader_epub_button_has_an_in_place_busy_guard():
    nodes = Markup((TEMPLATES / "dashboard_novel_detail.html").read_text(encoding="utf-8")).nodes
    export = next(n for n in nodes if n["attrs"].get("@click") == "exportEpub")
    assert export["attrs"].get(":disabled") == "exportingEpub"
    assert export["attrs"].get("type") == "button"


def test_mobile_reading_actual_template_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend runtime regression checks")
    result = subprocess.run(
        [node, str(ROOT / "tests/test_mobile_reading_runtime.cjs")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr

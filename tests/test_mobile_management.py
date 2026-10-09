"""Management source/render contracts and executable template-script regressions.

The Node VM uses only fake responses/tokens. CSS contracts are not measurements:
the controller owns CUA checks at 360/390/430px and desktop, including focus and
scrolling in an actual browser. No application server/configuration is loaded.
"""

from pathlib import Path
import re
import shutil
import subprocess

from flask import Flask, render_template
import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "pixiv_novel_sync" / "templates"
PAGES = (
    "dashboard_pending_deletions.html",
    "dashboard_settings_models.html",
    "dashboard_settings_agents.html",
    "dashboard_settings_system.html",
)


def source(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def css_rule(html: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", html)
    assert match, f"Missing scoped management style: {selector}"
    return match.group(1)


@pytest.mark.parametrize("name", PAGES)
def test_management_templates_render_without_loading_server_configuration(name):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.jinja_env.variable_start_string = "{["
    app.jinja_env.variable_end_string = "]}"
    with app.test_request_context("/dashboard/settings/system"):
        html = render_template(name, task_labels={})
    assert '<div id="app"' in html
    assert "initVueApp({" in html
    assert "{%" not in html


def test_pending_retry_belongs_to_the_error_state_and_is_read_only():
    html = source(PAGES[0])
    error_panel = html.split('v-else-if="loadError"', 1)[1].split("<!-- Empty State -->", 1)[0]
    assert 'role="alert"' in error_panel
    assert '@click="retryPending"' in error_panel
    assert 'type="button"' in error_panel
    assert "不会" in error_panel and "检测" in error_panel
    assert 'v-else-if="!items.length"' in html
    assert 'v-if="!loading && !loadError"' in html, "Do not show a false zero-count summary on error"


def test_model_editor_focus_targets_and_feedback_are_in_the_action_panels():
    html = source(PAGES[1])
    for editor, field, following, scope in (
        ("provider-editor", "provider-name", "provider-list", "provider-form"),
        ("pool-editor", "pool-name", "pool-list", "pool-form"),
    ):
        assert f'id="{editor}"' in html
        panel = html.split(f'id="{editor}"', 1)[1].split(f'id="{following}"', 1)[0]
        assert f'id="{field}"' in panel
        assert f'for="{field}"' in panel
        assert f"aiMessages['{scope}']" in panel
        assert ':role=' in panel and "'alert'" in panel
    assert ':disabled="!poolEditorReady"' in html, "Loading or failed pool selection must gate the old snapshot"
    assert 'id="pool-editor-title"' in html and 'tabindex="-1"' in html
    assert "aiMessages['provider-' + provider.id]" in html
    assert "aiMessages['pool-list']" in html


def test_failed_pool_selection_shows_its_error_and_get_retry_before_disabled_fields():
    html = source(PAGES[1])
    heading_area = html.split('id="pool-editor"', 1)[1].split("<fieldset", 1)[0]
    assert 'v-if="poolLoadError"' in heading_area
    assert 'role="alert"' in heading_area
    assert '{{ poolLoadError }}' in heading_area
    assert '{{ selectedPoolId }}' in heading_area
    assert '@click="retryModelPool"' in heading_area
    assert '@click="resetPoolForm(true)"' in heading_area
    assert 'type="button"' in heading_area


def test_model_controls_shrink_without_removing_existing_scroll_regions():
    html = source(PAGES[1])
    assert "management-models" in html
    assert "min-width: 0" in css_rule(html, ".management-models .management-editor")
    assert "scroll-margin-top" in css_rule(html, ".management-models .management-editor")
    select = css_rule(html, ".management-models select")
    for declaration in ("min-width: 0", "max-width: 100%", "width: 100%"):
        assert declaration in select
    assert "overflow-y-auto" in html, "Keep bounded catalog/attempt lists usable"
    assert not re.search(r"overflow-x\s*:\s*(hidden|clip)", html)


def test_agent_long_option_controls_stack_on_mobile_and_can_shrink():
    html = source(PAGES[2])
    controls = css_rule(html, ".management-agents .agent-candidate-controls")
    for declaration in ("display: flex", "flex-direction: column", "min-width: 0", "max-width: 100%", "width: 100%"):
        assert declaration in controls
    select = css_rule(html, ".management-agents select")
    for declaration in ("min-width: 0", "max-width: 100%", "width: 100%"):
        assert declaration in select
    assert "@media (min-width: 640px)" in html
    assert "flex-direction: row" in html
    assert 'class="agent-candidate-controls"' in html
    assert 'aria-label="选择 Agent 查看候选链"' in html
    assert "overflow-wrap: anywhere" in html
    assert not re.search(r"overflow-x\s*:\s*(hidden|clip)", html)


def test_agent_batch_rebind_feedback_and_recovery_stay_inside_the_shared_modal():
    html = source(PAGES[2])
    modal = re.search(r'<app-modal\b[^>]*title="确认批量改绑"[\s\S]*?</app-modal>', html)
    assert modal
    modal = modal[0]
    assert ':is-open="!!batchPreview"' in modal
    assert '@close="closeBatchRebind"' in modal
    assert '@click="closeBatchRebind"' in modal
    assert ':aria-busy="batchRebindBusy"' in modal
    assert 'v-if="batchRebindError"' in modal and 'role="alert"' in modal
    assert '{{ batchRebindError }}' in modal
    assert 'role="status"' in modal
    assert '@click="batchRebind"' in modal and ':disabled="batchRebindBusy"' in modal
    assert '重试应用' in modal
    assert '关闭' in modal and '不会取消' in modal


def test_token_modal_keeps_copy_feedback_and_manual_selection_inside():
    html = source(PAGES[3])
    modal = re.search(r'<app-modal\b[^>]*title="救援 API Token"[\s\S]*?</app-modal>', html)
    assert modal
    modal = modal[0]
    assert ':close-on-backdrop="false"' in modal
    assert '@close="closeRescueToken"' in modal
    assert '@click="closeRescueToken"' in modal
    assert '@click="copyRescueToken"' in modal
    assert ':disabled="copyingRescueToken"' in modal
    assert '{{ rescueTokenCopyMessage }}' in modal
    assert ':role=' in modal and "'alert'" in modal
    assert '@click="selectRescueToken"' in modal
    assert 'for="rescue-token-plaintext"' in modal
    assert 'id="rescue-token-plaintext"' in modal
    assert 'aria-describedby="rescue-token-copy-help"' in modal
    assert 'id="rescue-token-copy-help"' in modal
    assert "readonly" in modal
    assert "长按" in modal
    assert "localStorage" not in html and "sessionStorage" not in html


def test_mobile_management_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for management template runtime tests")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "test_mobile_management_runtime.cjs")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from test_ai_settings_browser import WIDTHS, browser, local_vue
from work_analysis_browser_helpers import (
    BASE, BODY, END, EVIDENCE, LABEL, QUOTE, RUN, START, OfflineWorkUI,
)


@pytest.fixture(params=WIDTHS, ids=lambda width: "touch-" + str(width) + "px")
def ui(request, browser, local_vue):
    fixture = OfflineWorkUI(browser, local_vue, request.param)
    yield fixture
    try:
        assert fixture.unknown == [], "Non-whitelisted browser requests: " + repr(fixture.unknown)
        assert fixture.page_errors == [], "Actual Vue/Chromium errors: " + repr(fixture.page_errors)
        assert all("/ai/jobs" not in row["path"] and "/probe" not in row["path"] for row in fixture.requests)
        fixture.assert_storage()
    finally:
        fixture.context.close()


def test_no_view_preferences_and_plain_reader_keep_existing_read_only_behavior(ui):
    page = ui.page
    ui.goto("/dashboard/preferences")
    ui.expect(page.get_by_role("heading", name="偏好与推荐", exact=True)).to_be_visible()
    ui.expect(page.locator("#preference-management")).to_be_visible()
    assert ui.calls("GET", "/api/dashboard/preferences/profiles")
    assert ui.calls("GET", "/api/dashboard/recommendations/items")
    assert not any(row["path"].startswith(BASE) for row in ui.requests)
    assert not ui.calls("GET", "/static/js/work_analysis.js")
    assert ui.writes() == []
    ui.goto("/dashboard/novels/101")
    ui.expect(page.get_by_role("heading", name="Synthetic reader", exact=True)).to_be_visible()
    link = page.get_by_role("link", name="分析作品", exact=True)
    ui.expect(link).to_have_attribute("href", "/dashboard/preferences?view=work-analysis&source_kind=novel&source_id=101")
    assert not ui.calls("GET", "/static/js/work_analysis.js")
    assert not any(row["path"].startswith(BASE) for row in ui.requests)
    assert ui.writes() == []


def test_series_entry_real_picker_order_budget_and_preview_invalidation(ui):
    page = ui.page
    ui.goto("/dashboard/series/501")
    page.get_by_role("link", name="分析作品", exact=True).click()
    ui.expect(page.locator("#wa-source-kind")).to_have_value("series")
    ui.expect(page.locator("#wa-source-id")).to_have_value("501")
    for name in ("request", "token", "time"):
        ui.expect(page.locator("#wa-budget-" + name)).to_have_value("")
    page.locator("#wa-agent").focus()
    page.locator("#wa-agent").press("Enter")
    dialog = page.locator("#wa-agent-dialog")
    ui.assert_modal(dialog)
    ui.expect(dialog.get_by_role("option").filter(has_text=LABEL)).to_be_disabled()
    assert dialog.locator("img").count() == 0
    assert not page.locator("#wa-agent-filter").evaluate("el => el === document.activeElement")
    dialog.get_by_role("option").filter(has_text="Fixture Agent A").click()
    for name, value in (("request", 16), ("token", 500000), ("time", 600)):
        page.locator("#wa-budget-" + name).fill(str(value))
    ui.allow("POST", BASE + "/preview")
    page.locator("#wa-preview").click()
    ui.expect(page.locator("#wa-create")).to_be_disabled()
    ui.expect(page.get_by_text("缺失或空白正文", exact=False)).to_be_visible()
    assert page.locator('section[aria-label="预览与外发确认"] code').text_content() == "org/model "
    assert len(ui.writes()) == 1 and ui.writes()[0]["path"] == BASE + "/preview"
    page.get_by_role("button", name="下移第 1 章", exact=True).click()
    ui.expect(page.locator("#wa-create")).to_have_count(0)
    page.locator("#wa-preview").click()
    ui.expect(page.locator("#wa-create")).to_be_visible()
    assert ui.calls("POST", BASE + "/preview")[-1]["body"]["selection"]["ordered_novel_ids"] == [102, 101]
    ui.confirm_preview()
    ui.expect(page.locator("#wa-create")).to_be_enabled()
    page.locator("#wa-budget-token").fill("500001")
    ui.expect(page.locator("#wa-create")).to_have_count(0)
    page.locator("#wa-preview").click()
    ui.expect(page.locator("#wa-confirm-budget")).not_to_be_checked()
    ui.confirm_preview()
    ui.allow("POST", BASE)
    ui.hold("POST", BASE)
    page.locator("#wa-create").dblclick()
    ui.expect(page.get_by_text("正在提交；返回前不能确认远端调用已经结束。", exact=True)).to_be_visible()
    assert len(ui.calls("POST", BASE)) == 1
    ui.release("POST", BASE)
    ui.expect(page.locator("#wa-run-state")).to_contain_text("queued")
    request = ui.calls("POST", BASE)[0]["body"]
    assert request["budget"]["token_limit"] == 500001
    assert all(request["confirmations"].values())
    assert re.fullmatch(r"[0-9a-f]{32}", request["idempotency_key"])
    assert BODY not in json.dumps(request, ensure_ascii=False)
    ui.assert_viewport()
    ui.capture("created-from-series")


def test_removed_current_agent_does_not_auto_select_other_or_keep_consent(ui):
    page = ui.prepare_preview()
    ui.confirm_preview()
    ui.work_agents = [row for row in ui.work_agents if row["id"] != 7]
    page.get_by_role("button", name="刷新 Agent", exact=True).click()
    ui.expect(page.locator("#wa-create")).to_have_count(0)
    page.locator("#wa-agent").click()
    dialog = page.locator("#wa-agent-dialog")
    current = dialog.get_by_role("option").filter(has_text="Fixture Agent A")
    ui.expect(current).to_be_visible()
    ui.expect(current).to_be_disabled()
    dialog.get_by_role("option").filter(has_text="Fixture Agent B").click()
    page.locator("#wa-preview").click()
    ui.expect(page.locator("#wa-create")).to_be_disabled()
    assert ui.calls("POST", BASE + "/preview")[-1]["body"]["agent_id"] == 8
    assert ui.calls("POST", BASE) == []


def test_same_form_refresh_and_server_changed_preview_require_new_consent(ui):
    page = ui.prepare_preview()
    for revision in (0, 1):
        ui.confirm_preview()
        ui.expect(page.locator("#wa-create")).to_be_enabled()
        ui.preview_revision = revision
        page.locator("#wa-preview").click()
        ui.expect(page.locator("#wa-create")).to_be_disabled()
        for suffix in ("order", "archive", "send", "budget"):
            ui.expect(page.locator("#wa-confirm-" + suffix)).not_to_be_checked()
        if revision:
            preview = page.locator('section[aria-label="预览与外发确认"]')
            ui.expect(preview).to_contain_text("Changed synthetic service")
            ui.expect(preview).to_contain_text("Revised synthetic work")
            assert preview.locator("code").text_content() == "new/model "
    assert len(ui.calls("POST", BASE + "/preview")) == 3
    assert ui.calls("POST", BASE) == []


def test_unknown_create_reuses_the_same_body_and_known_success_survives_readback_failure(ui):
    page = ui.prepare_preview("novel")
    ui.confirm_preview()
    ui.allow("POST", BASE)
    ui.lose_next.add(("POST", BASE))
    ui.fail_reads.update({BASE, BASE + "/" + RUN})
    page.locator("#wa-create").click()
    retry = page.get_by_role("button", name="重试同一创建", exact=True)
    ui.expect(retry).to_be_visible()
    ui.expect(page.locator("#wa-source-id")).to_be_disabled()
    original = ui.calls("POST", BASE)[0]["body"]
    retry.click()
    ui.expect(page.locator("#wa-run-state")).to_contain_text("queued")
    ui.expect(page.get_by_text("写入已确认，但刷新失败；请只重试读取，不要重复提交。", exact=True)).to_be_visible()
    ui.expect(retry).to_have_count(0)
    assert len(ui.idempotency) == 1
    assert len(ui.calls("POST", BASE)) == 2
    assert ui.calls("POST", BASE)[1]["body"] == original
    ui.fail_reads.clear()
    page.get_by_role("button", name="刷新状态", exact=True).click()
    page.get_by_role("button", name="刷新记录", exact=True).click()
    ui.expect(page.locator('[data-run-id="' + RUN + '"]')).to_be_visible()
    assert len(ui.calls("POST", BASE)) == 2
    ui.capture("unknown-create-reconciled")


def test_pause_cancel_resume_budget_and_delete_are_separate_confirmed_operations(ui):
    ui.seed_run("running")
    page = ui.page
    ui.goto("/dashboard/preferences?view=work-analysis&analysis_id=" + RUN)
    path = BASE + "/" + RUN
    ui.allow("POST", path + "/pause")
    ui.lose_next.add(("POST", path + "/pause"))
    page.get_by_role("button", name="暂停分析", exact=True).click()
    ui.expect(page.get_by_role("button", name="先核对当前状态", exact=True)).to_be_visible()
    assert page.get_by_role("button", name="重试同一 CAS 请求", exact=True).count() == 0
    page.get_by_role("button", name="先核对当前状态", exact=True).click()
    ui.expect(page.locator("#wa-run-state")).to_contain_text("pausing")
    ui.expect(page.get_by_text("当前状态已核对；原写入结果仍未知，未重复提交。", exact=True)).to_be_visible()
    assert page.get_by_text("写入已确认。", exact=True).count() == 0
    ui.expect(page.get_by_text("正在等待当前调用真实退出；不保证远端已停止计算，不提前释放共享槽。", exact=True)).to_be_visible()
    assert len(ui.calls("POST", path + "/pause")) == 1
    ui.transition("paused", "user_pause")
    page.get_by_role("button", name="刷新状态", exact=True).click()
    ui.expect(page.get_by_role("button", name="恢复未完成分析", exact=True)).to_be_enabled()
    old = {name: ui.work_run[name] for name in ("request_count", "planned_tokens", "active_seconds_debited")}
    page.get_by_text("明确调整预算（不清零累计计数）", exact=True).click()
    page.locator("#wa-amend-request").fill("17")
    ui.expect(page.get_by_role("button", name="确认调整预算", exact=True)).to_be_disabled()
    page.locator("#wa-confirm-amend").check()
    ui.allow("POST", path + "/budget")
    page.get_by_role("button", name="确认调整预算", exact=True).click()
    ui.expect(page.locator("#wa-confirm-amend")).not_to_be_checked()
    assert ui.calls("POST", path + "/budget")[0]["body"]["budget_confirmed"] is True
    assert ui.work_run["budget"]["request_limit"] == 17
    assert all(ui.work_run[name] == value for name, value in old.items())
    ui.allow("POST", path + "/resume")
    page.get_by_role("button", name="恢复未完成分析", exact=True).click()
    ui.expect(page.locator("#wa-run-state")).to_contain_text("queued")
    assert all(ui.work_run[name] == value for name, value in old.items())
    ui.allow("POST", path + "/cancel")
    page.get_by_role("button", name="取消分析", exact=True).click()
    ui.expect(page.locator("#wa-run-state")).to_contain_text("cancelling")
    ui.transition("cancelled", "user_cancel")
    page.get_by_role("button", name="刷新状态", exact=True).click()
    ui.expect(page.locator("#wa-run-state")).to_contain_text("cancelled")
    ui.expect(page.get_by_role("button", name="恢复未完成分析", exact=True)).to_be_enabled()
    ui.capture("cancelled-checkpoint")
    page.get_by_text("删除本分析", exact=True).click()
    ui.expect(page.get_by_role("button", name="删除分析", exact=True)).to_be_disabled()
    page.locator("#wa-confirm-delete").check()
    ui.allow("DELETE", path)
    page.get_by_role("button", name="删除分析", exact=True).click()
    ui.expect(page.get_by_text("分析已删除，不能恢复；仅保留必要无正文审计。", exact=True)).to_be_visible()
    assert ui.work_run["deleted"] is True and ui.work_report is None
    assert page.locator("#wa-report").count() == 0
    assert page.get_by_role("button", name="恢复未完成分析", exact=True).count() == 0
    assert len(ui.calls("DELETE", path)) == 1


@pytest.mark.parametrize("yield_status", ["pausing", "paused"])
def test_lost_user_pause_does_not_confirm_a_yield_and_allows_a_new_pause(ui, yield_status):
    ui.seed_run("running")
    page = ui.page
    ui.goto("/dashboard/preferences?view=work-analysis&analysis_id=" + RUN)
    path = BASE + "/" + RUN
    ui.allow("POST", path + "/pause")
    ui.lose_before_apply.add(("POST", path + "/pause"))
    page.get_by_role("button", name="暂停分析", exact=True).click()
    check = page.get_by_role("button", name="先核对当前状态", exact=True)
    ui.expect(check).to_be_visible()
    ui.transition(yield_status, "sync_yield")
    expected_version = ui.work_run["version"]
    check.click()
    ui.expect(page.locator("#wa-run-state")).to_contain_text("sync_yield")
    ui.expect(page.get_by_text("状态已经变化；未确认原写入，也未重复提交，请按当前状态重新选择。", exact=True)).to_be_visible()
    assert page.get_by_text("写入已确认。", exact=True).count() == 0
    assert len(ui.calls("POST", path + "/pause")) == 1
    pause = page.get_by_role("button", name="暂停分析", exact=True)
    ui.expect(pause).to_be_enabled()
    pause.click()
    ui.expect(page.locator("#wa-run-state")).to_contain_text("user_pause")
    assert ui.work_run["status"] == yield_status
    writes = ui.calls("POST", path + "/pause")
    assert len(writes) == 2
    assert writes[0]["body"]["expected_version"] == 1
    assert writes[1]["body"]["expected_version"] == expected_version
    assert ui.calls("POST", path + "/resume") == []


def test_report_is_escaped_collapsed_and_reader_uses_only_server_codepoint_evidence(ui):
    ui.seed_run("succeeded")
    page = ui.page
    ui.goto("/dashboard/preferences?view=work-analysis&analysis_id=" + RUN)
    assert ui.calls("GET", BASE + "/" + RUN + "/report") == []
    page.get_by_role("button", name="读取已验证报告", exact=True).click()
    report = page.locator("#wa-report")
    ui.expect(report).to_be_visible()
    ui.expect(report.get_by_text(LABEL, exact=True)).to_be_visible()
    assert report.locator("img").count() == 0
    assert page.evaluate("window.fixtureInjected === undefined")
    fold = report.locator('[data-evidence-id="' + EVIDENCE + '"]').first
    assert not fold.evaluate("el => el.open")
    ui.expect(fold.locator("blockquote")).to_be_hidden()
    ui.expect(report).to_contain_text("解释性判断")
    ui.expect(report).to_contain_text("未报告请求：1")
    ui.expect(report).to_contain_text("原作完整性 partial")
    ui.capture("report-evidence-collapsed")
    fold.locator("summary").click()
    ui.expect(fold.locator("blockquote")).to_have_text(QUOTE)
    fold.get_by_role("button", name="核对当前正文片段", exact=True).click()
    ui.expect(fold.locator("mark")).to_have_text(QUOTE)
    anchor = fold.get_by_role("link", name="到阅读页核对同版本证据", exact=True)
    parsed = urlsplit(anchor.get_attribute("href"))
    assert parsed.path == "/dashboard/novels/101" and parsed.fragment == "work-analysis-evidence"
    assert parse_qs(parsed.query) == {"analysis_id": [RUN], "evidence_id": [EVIDENCE]}
    anchor.click()
    panel = page.locator("#work-analysis-evidence")
    ui.expect(panel.locator("mark")).to_have_text(QUOTE)
    ui.expect(panel).to_contain_text("[" + str(START) + ", " + str(END) + ")")
    assert panel.evaluate("el => el === document.activeElement")
    heading = panel.get_by_role("heading", name="核对同版本正文证据", exact=True)
    nav_box = page.locator(".reader-top-nav").bounding_box()
    heading_box = heading.bounding_box()
    assert heading_box["y"] >= nav_box["y"] + nav_box["height"]
    assert len(panel.locator("pre").inner_text()) <= 496
    assert panel.locator("img").count() == 0
    ui.assert_viewport()
    ui.capture("reader-same-hash-highlight")
    ui.reader_hash = "f" * 64
    page.reload(wait_until="networkidle")
    ui.expect(panel.get_by_role("alert")).to_contain_text("当前阅读正文与证据版本不同")
    assert panel.locator("mark").count() == 0
    assert ui.writes() == []


def test_stale_report_keeps_historical_quote_but_410_removes_current_jump(ui):
    ui.seed_run("succeeded")
    ui.work_run["report_stale"] = True
    ui.work_report["report_stale"] = True
    ui.evidence_status = 410
    page = ui.page
    ui.goto("/dashboard/preferences?view=work-analysis&analysis_id=" + RUN)
    page.get_by_role("button", name="读取已验证报告", exact=True).click()
    report = page.locator("#wa-report")
    ui.expect(report).to_contain_text("这是旧版本报告")
    fold = report.locator('[data-evidence-id="' + EVIDENCE + '"]').first
    fold.locator("summary").click()
    fold.get_by_role("button", name="核对当前正文片段", exact=True).click()
    ui.expect(fold.get_by_role("alert")).to_contain_text("当前正文版本不再支持此证据定位")
    ui.expect(fold.locator("blockquote")).to_have_text(QUOTE)
    assert fold.locator("mark").count() == 0
    fold.get_by_role("link", name="到阅读页核对同版本证据", exact=True).click()
    panel = page.locator("#work-analysis-evidence")
    ui.expect(panel.get_by_role("alert")).to_contain_text("当前正文版本不再支持此证据定位")
    assert panel.locator("mark").count() == 0
    assert ui.writes() == []

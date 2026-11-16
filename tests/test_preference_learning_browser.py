"""Opt-in, offline Chromium tests of shipped learning UI and synthetic API data."""

from copy import deepcopy
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

from flask import render_template
import pytest

from test_ai_settings_browser import ROOT, STATIC, WIDTHS, browser, local_vue
from work_analysis_browser_helpers import EVIDENCE, QUOTE, RUN, TEXT_HASH, OfflineWorkUI

__all__ = ["browser", "local_vue"]  # Pytest discovers these imported fixtures.


BASE = "/api/dashboard/preference-learning"
PROFILE = "9" * 32
BATCH = "8" * 32
LABEL = '<img src="/fixture-profile-injected" onerror="window.fixtureProfileInjected=true">'
BUDGET = {"request_limit": 4, "token_limit": 100000, "active_seconds_limit": 300}
TOTAL = {"request_limit": 40, "token_limit": 1000000, "active_seconds_limit": 3000}


class OfflineLearningUI(OfflineWorkUI):
    def __init__(self, browser, local_vue, width):
        super().__init__(browser, local_vue, width)
        self.feedback = {"source_kind": "novel", "source_id": 101, "reaction": "clear", "note": "", "version": 0}
        self.series_feedback = {"source_kind": "series", "source_id": 501, "reaction": "clear", "note": "", "version": 0}
        self.profile = None
        self.profile_keys = {}
        self.batch = None
        self.policy = {"version": 1, "enabled": False, "reason": None, "configuration": None,
                       "allocated": {"request_limit": 0, "token_limit": 0, "active_seconds_limit": 0},
                       "work_count": 0, "next_run_at": None, "new_baseline_revision": None}
        self.seed_run("succeeded")

    def render(self, path):
        parsed = urlsplit(path)
        names = {"/dashboard/preferences": "dashboard_preferences.html", "/dashboard/novels/101": "dashboard_novel_detail.html",
                 "/dashboard/series/501": "dashboard_series_detail.html"}
        with self.app.test_request_context(path):
            view = parse_qs(parsed.query).get("view", [None])[0]
            return render_template(names[parsed.path], task_labels={}, csp_nonce=lambda: "test-nonce",
                                   novel_id=101, series_id=501, work_analysis_view=view == "work-analysis",
                                   work_learning_view=view == "learning")

    def _route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if request.method == "GET" and parsed.path == "/static/js/preference_learning.js":
            self.requests.append({"method": "GET", "path": parsed.path, "query": parse_qs(parsed.query), "body": None})
            return route.fulfill(content_type="application/javascript", body=(STATIC / "js/preference_learning.js").read_bytes(),
                                 headers={"Cache-Control": self.js_cache})
        return super()._route(route)

    def public_profile(self):
        if self.profile is None:
            return None
        return {key: deepcopy(value) for key, value in self.profile.items() if key not in {"result", "sources"}}

    def state(self):
        counts = {key: int(self.feedback["reaction"] == key) for key in ("liked", "disliked", "neutral")}
        return {"profiles": {"feedback_counts": counts, "eligible_works": int(self.feedback["reaction"] != "clear"),
                             "missing_reports": 0, "active_run_id": PROFILE if self.profile and self.profile["active"] else None,
                             "runs": [self.public_profile()] if self.profile else []},
                "library": {"policy": deepcopy(self.policy), "batches": [deepcopy(self.batch)] if self.batch else []}}

    def finish_profile(self):
        self.profile.update(status="succeeded", version=self.profile["version"] + 1,
                            usage={"network_request_count": 1, "planned_tokens": 4000,
                                   "reported_input_tokens": 1000, "reported_output_tokens": None})
        evidence = deepcopy(self.work_report["artifact"]["evidence"][EVIDENCE])
        evidence["evidence_id"] = EVIDENCE
        self.profile["result"] = {"summary": "Synthetic preference summary " + LABEL,
                                  "preferences": [{"label": LABEL, "dimension": "narrative", "polarity": "positive",
                                                   "certainty": "tentative", "source_ids": ["novel:101"]}],
                                  "counterexamples": ["Synthetic counterexample"], "uncertainties": ["Synthetic uncertainty"]}
        self.profile["sources"] = [{"source_key": "novel:101", "source_kind": "novel", "source_id": 101,
                                     "reaction": "liked", "feedback_version": self.feedback["version"],
                                     "report_run_id": RUN, "report_hash": TEXT_HASH, "claims": [], "evidence": [evidence]}]

    def _api(self, route, record):
        path, method, body = record["path"], record["method"], record["body"]
        if not path.startswith(BASE):
            return super()._api(route, record)
        if path == BASE + "/state" and method == "GET":
            return self._json(route, self.state())
        if path in {BASE + "/feedback/novel/101", BASE + "/feedback/series/501"}:
            feedback = self.feedback if path.endswith("/novel/101") else self.series_feedback
            if method == "GET":
                return self._json(route, feedback)
            assert method == "PUT" and body["expected_version"] == feedback["version"]
            feedback.update(reaction=body["reaction"], note=body["note"], version=feedback["version"] + 1)
            return self._json(route, feedback)
        targets = [{"provider_id": 7, "provider_name": "Fixture Provider", "model_key": "fixture/model "}]
        if path in {BASE + "/profile-preview", BASE + "/batch-preview"}:
            assert method == "POST"
            fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            common = {"preview_fingerprint": fingerprint, "targets": targets, "warnings": [], "can_create": True}
            if path.endswith("/profile-preview"):
                return self._json(route, {**common, "source_count": 1, "feedback_counts": self.state()["profiles"]["feedback_counts"],
                                         "missing_reports": 0, "budget": body["budget"], "output_tokens": body["output_tokens"],
                                         "minimum_budget": {"request_limit": 1, "token_limit": 4000, "active_seconds_limit": 30}})
            return self._json(route, {**common, "configuration": body, "purpose": body.get("purpose", "manual"),
                                     "candidate_count": 0 if body.get("purpose") == "policy" and body["scope"] == "new" else 2,
                                     "samples": [{"source_kind": "novel", "source_id": 101, "title": "Synthetic novel"},
                                                 {"source_kind": "series", "source_id": 501, "title": "Synthetic series"}],
                                     "budget_mode": "cumulative_reserved_limits"})
        if path == BASE + "/profiles" and method == "POST":
            assert all(body["confirmations"].values()) and body["preview_fingerprint"]
            key = body["idempotency_key"]
            if key in self.profile_keys:
                assert self.profile_keys[key] == body
            else:
                self.profile_keys[key] = deepcopy(body)
                self.profile = {"run_id": PROFILE, "version": 1, "status": "queued", "active": False, "stale": False,
                                "agent_id": body["agent_id"], "created_at": "2026-01-01T00:00:00Z", "budget": body["budget"],
                                "usage": {"network_request_count": 0, "planned_tokens": 0, "reported_input_tokens": None,
                                          "reported_output_tokens": None}, "source_count": 1, "error_code": None,
                                "shared_job_id": "fixture-profile-job"}
            return self._json(route, self.public_profile())
        if path == BASE + "/profiles/" + PROFILE and method == "GET":
            return self._json(route, self.profile)
        if path.startswith(BASE + "/profiles/" + PROFILE):
            assert body["expected_version"] == self.profile["version"]
            self.profile["version"] += 1
            if method == "DELETE":
                self.profile.update(status="deleted", active=False)
                self.profile.pop("result", None)
                self.profile.pop("sources", None)
            elif path.endswith("/activate"):
                self.profile["active"] = True
            else:
                self.profile["status"] = "cancelled"
            return self._json(route, self.public_profile())
        if path == BASE + "/batches" and method == "POST":
            assert all(body["confirmations"].values())
            self.batch = {"batch_id": BATCH, "version": 1, "status": "waiting", "reason": None, "origin": "manual",
                          "scope": body["scope"], "work_limit": body["work_limit"], "work_count": 1,
                          "scanned_count": 2, "skipped_count": 1, "budget": body["batch_budget"],
                          "allocated": body["work_budget"], "created_at": "2026-01-01T00:01:00Z",
                          "updated_at": "2026-01-01T00:01:00Z"}
            return self._json(route, self.batch)
        if path == BASE + "/batches/" + BATCH and method == "GET":
            return self._json(route, {"batch": self.batch, "items": [
                {"target_id": 1, "source_kind": "novel", "source_id": 101, "title": "Synthetic novel",
                 "run_id": RUN, "status": "created", "reason": None, "run_status": "succeeded"},
                {"target_id": 2, "source_kind": "series", "source_id": 501, "title": "Synthetic series",
                 "run_id": None, "status": "skipped", "reason": "Synthetic unavailable source", "run_status": None}],
                                     "total": 2, "offset": 0, "limit": 20})
        if path.startswith(BASE + "/batches/" + BATCH) and method == "POST":
            assert body["expected_version"] == self.batch["version"]
            self.batch["version"] += 1
            self.batch["status"] = {"pause": "paused", "resume": "waiting", "cancel": "cancelled"}[path.rsplit("/", 1)[1]]
            return self._json(route, self.batch)
        if path == BASE + "/automation" and method == "PUT":
            assert body["expected_version"] == self.policy["version"]
            self.policy["version"] += 1
            self.policy["enabled"] = body["enabled"]
            if body["enabled"]:
                assert all(body["confirmations"].values()) and body["preview_fingerprint"]
                self.policy["configuration"] = {key: deepcopy(body[key]) for key in (
                    "scope", "agent_id", "output_tokens", "work_budget", "batch_budget", "work_limit", "interval_minutes", "confirmations")}
                self.policy["new_baseline_revision"] = 8
            return self._json(route, self.policy)
        self.unknown.append(record)
        return route.abort("blockedbyclient")

    def pick(self, kind):
        self.page.locator("#learning-" + kind + "-agent").click()
        dialog = self.page.locator("#learning-" + kind + "-agent-dialog")
        self.assert_modal(dialog)
        dialog.get_by_role("option").filter(has_text="Fixture Agent A").click()

    def confirm(self, kind):
        for suffix in (("send", "budget") if kind == "profile" else ("archive", "order", "send", "budget")):
            self.page.locator("#learning-" + kind + "-confirm-" + suffix).check()

    def capture(self, name):
        target = ROOT / ".superpowers/verification/preference-learning-2026-11-12/browser" / ("touch-" + str(self.width))
        target.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(target / (name + ".png")), full_page=True)
        (target / (name + "-requests.json")).write_text(json.dumps(self.requests, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture(params=WIDTHS, ids=lambda width: "touch-" + str(width) + "px")
def ui(request, browser, local_vue):
    fixture = OfflineLearningUI(browser, local_vue, request.param)
    try:
        yield fixture
        assert fixture.unknown == []
        assert fixture.page_errors == []
        fixture.assert_storage()
    finally:
        fixture.context.close()


def test_learning_feedback_profile_evidence_activation_batch_and_automation(ui):
    page = ui.page
    ui.goto("/dashboard/preferences?view=learning")
    ui.expect(page.get_by_role("heading", name="偏好学习", exact=True)).to_be_visible()
    assert ui.writes() == []
    for suffix in ("requests", "tokens", "seconds"):
        ui.expect(page.locator("#learning-profile-" + suffix)).to_have_value("")
    assert not any(row["path"].startswith(BASE + "/feedback/") for row in ui.requests)
    feedback = page.locator('section[aria-label="阅读喜好评价"]')
    feedback.get_by_role("button", name="评价与修改", exact=True).click()
    feedback.get_by_label("本地作品 ID", exact=True).fill("101")
    feedback.get_by_role("button", name="读取当前评价", exact=True).click()
    ui.expect(feedback.get_by_text("当前评价：尚未评价 · 版本 0", exact=True)).to_be_visible()
    feedback.get_by_label("我的评价", exact=True).select_option("liked")
    feedback.get_by_label("原因（可选，最多 500 字）", exact=True).fill("Synthetic reading reaction")
    ui.allow("PUT", BASE + "/feedback/novel/101")
    feedback.get_by_role("button", name="保存评价", exact=True).click()
    ui.expect(feedback.get_by_text("当前评价：喜欢 · 版本 1", exact=True)).to_be_visible()
    ui.pick("profile")
    for suffix, value in (("requests", 4), ("tokens", 100000), ("seconds", 300)):
        page.locator("#learning-profile-" + suffix).fill(str(value))
    ui.allow("POST", BASE + "/profile-preview")
    page.locator("#learning-profile-preview").click()
    ui.expect(page.locator("#learning-profile-create")).to_be_disabled()
    ui.confirm("profile")
    page.locator("#learning-profile-tokens").fill("100001")
    ui.expect(page.locator("#learning-profile-create")).to_have_count(0)
    page.locator("#learning-profile-preview").click()
    ui.expect(page.locator("#learning-profile-confirm-send")).not_to_be_checked()
    ui.confirm("profile")
    ui.allow("POST", BASE + "/profiles")
    ui.hold("POST", BASE + "/profiles")
    page.locator("#learning-profile-create").dblclick()
    assert len(ui.calls("POST", BASE + "/profiles")) == 1
    ui.release("POST", BASE + "/profiles")
    ui.expect(page.locator("#learning-profile-status")).to_contain_text("排队中")
    ui.finish_profile()
    page.get_by_role("button", name="读取画像结果", exact=True).click()
    report = page.locator("#learning-profile-result")
    ui.expect(report).to_be_visible()
    assert not report.evaluate("element => element.open")
    report.locator(":scope > summary").click()
    ui.expect(report.get_by_role("heading", name=LABEL, exact=True)).to_be_visible()
    assert report.locator("img").count() == 0
    assert page.evaluate("window.fixtureProfileInjected === undefined")
    source = report.locator(":scope > details")
    source.locator(":scope > summary").click()
    quote = source.locator("details")
    assert not quote.evaluate("element => element.open")
    quote.locator("summary").click()
    ui.expect(quote.locator("blockquote")).to_have_text(QUOTE)
    ui.assert_viewport()
    ui.capture("profile-evidence")
    quote.get_by_role("link", name="到阅读页核对版本", exact=True).click()
    ui.expect(page.locator("#work-analysis-evidence mark")).to_have_text(QUOTE)
    ui.goto("/dashboard/preferences?view=learning&profile_run_id=" + PROFILE)
    page.locator("#learning-profile-confirm-activate").check()
    ui.allow("POST", BASE + "/profiles/" + PROFILE + "/activate")
    page.get_by_role("button", name="启用到推荐", exact=True).click()
    ui.expect(page.locator("#learning-profile-status")).to_contain_text("正在用于推荐")
    ui.pick("library")
    for prefix, values in (("work", BUDGET), ("batch", TOTAL)):
        for suffix, key in (("requests", "request_limit"), ("tokens", "token_limit"), ("seconds", "active_seconds_limit")):
            page.locator("#learning-" + prefix + "-" + suffix).fill(str(values[key]))
    page.locator("#learning-work-limit").fill("10")
    ui.allow("POST", BASE + "/batch-preview")
    page.locator("#learning-library-preview").click()
    ui.confirm("library")
    ui.allow("POST", BASE + "/batches")
    page.locator("#learning-library-create").click()
    ui.expect(page.locator("#learning-batch-status")).to_contain_text("等待作品完成")
    allocated = deepcopy(ui.batch["allocated"])
    for action, label, status in (("pause", "暂停批次", "已暂停"), ("resume", "恢复批次", "等待作品完成")):
        ui.allow("POST", BASE + "/batches/" + BATCH + "/" + action)
        page.get_by_role("button", name=label, exact=True).click()
        ui.expect(page.locator("#learning-batch-status")).to_contain_text(status)
        assert ui.batch["allocated"] == allocated
    page.locator("#learning-library-mode").select_option("policy")
    page.locator("#learning-library-scope").select_option("new")
    page.locator("#learning-library-preview").click()
    ui.confirm("library")
    ui.allow("PUT", BASE + "/automation")
    page.locator("#learning-library-create").click()
    policy = page.locator('section[aria-label="自动分析当前授权"]')
    ui.expect(policy).to_contain_text("自动分析：已启用")
    ui.policy["allocated"] = deepcopy(BUDGET)
    ui.policy["work_count"] = 1
    page.get_by_role("button", name="刷新进度", exact=True).click()
    ui.expect(policy).to_contain_text("累计纳入 1 部作品")
    ui.assert_viewport()
    ui.capture("automation-and-batch")
    policy.get_by_role("button", name="停止后续自动分析", exact=True).click()
    ui.expect(policy).to_contain_text("自动分析：已关闭")
    assert ui.policy["allocated"] == BUDGET
    assert ui.calls("PUT", BASE + "/automation")[-1]["body"] == {"enabled": False, "expected_version": 2}


@pytest.mark.parametrize("kind,source_id", [("novel", 101), ("series", 501)])
def test_reader_feedback_loads_only_when_opened_and_supports_withdrawal(ui, kind, source_id):
    ui.goto("/dashboard/" + ("novels" if kind == "novel" else "series") + "/" + str(source_id))
    page = ui.page
    endpoint = BASE + "/feedback/" + kind + "/" + str(source_id)
    assert not any(row["path"].startswith(BASE) for row in ui.requests)
    feedback = page.locator('section[aria-label="阅读喜好评价"]')
    feedback.get_by_role("button", name="评价与修改", exact=True).click()
    ui.expect(feedback.get_by_text("当前评价：尚未评价 · 版本 0", exact=True)).to_be_visible()
    assert len(ui.calls("GET", endpoint)) == 1
    feedback.locator("#learning-feedback-reaction").select_option("disliked")
    feedback.locator("#learning-feedback-note").fill(LABEL)
    ui.allow("PUT", endpoint)
    feedback.get_by_role("button", name="保存评价", exact=True).click()
    ui.expect(feedback.get_by_text("当前评价：不喜欢 · 版本 1", exact=True)).to_be_visible()
    assert feedback.locator("img").count() == 0
    feedback.get_by_role("button", name="撤回评价", exact=True).click()
    ui.expect(feedback.get_by_text("当前评价：尚未评价 · 版本 2", exact=True)).to_be_visible()
    assert ui.calls("PUT", endpoint)[-1]["body"] == {"reaction": "clear", "note": "", "expected_version": 1}
    ui.assert_viewport()

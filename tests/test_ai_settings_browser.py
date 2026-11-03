"""Opt-in, offline real-Vue AI settings regression (never starts the app).

Set AI_UI_VUE_JS to a pre-existing, SRI-verified Vue 3.4.38 runtime to opt in.
Saved-current refresh uses actual page reload; live option replacement remains
separate VM coverage. The fixture rejects every non-whitelisted request.
The browser's touch emulation is not on-device soft-keyboard acceptance.
"""

from collections import defaultdict
from copy import deepcopy
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from flask import Flask, render_template
import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "pixiv_novel_sync" / "templates"
STATIC = TEMPLATES.parent / "static"
ORIGIN = "https://ai-ui.fixture.invalid"
VUE_URL = "https://unpkg.com/vue@3.4.38/dist/vue.global.prod.js"
WIDTHS = (375, 768, 1440)
CSRF = "fixture-csrf-not-a-credential"
KEY = "fixture-key-not-a-credential"
MALICIOUS_LABEL = '<img src="/fixture-injected" onerror="window.fixtureInjected=true">'
SYSTEM_TEXT = "Visible saved system \u4e2d\u6587\U0001f600"
INPUT_TEXT = ("Synthetic \u4e2d\u6587\U0001f600 input\n" * 120) + "TAIL-FIXTURE-ONLY"
EDIT = "\u7f16\u8f91"
SAVE_PROVIDER = "\u4fdd\u5b58 Provider"
PROBE = "\u63a2\u6d4b\u6a21\u578b\uff08\u4ec5\u9884\u89c8\uff09"
UNKNOWN = re.compile(r"\u672a\u77e5|\u672a\u62a5\u544a|unknown", re.I)


def model(model_id, provider_id, model_key, display_name, **overrides):
    return {
        "id": model_id, "provider_id": provider_id, "model_key": model_key,
        "display_name": display_name, "enabled": True, "routable": True,
        "source": "discovered", "discovered": True, "discovered_available": True,
        "manual": False, "capabilities": ["text", "stream"],
        "context_window": 1000000, "max_output_tokens": None,
        "manual_max_output_tokens": None, "discovered_max_output_tokens": None,
        **overrides,
    }


def public_plan():
    first = {
        "source_index": 0, "candidate_index": 0,
        "provider_id": 17, "provider_name": "Fixture North",
        "provider_model_id": 41, "model_key": "same/model", "display_name": "Shared Model",
        "pool_id": 7, "pool_name": "Fixture Ordered Group", "pool_position": 1,
        "fallback_depth": 0, "context_window": 1000000,
        "agent_context_window": 1000000, "provider_context_window": 1000000,
        "model_context_window": 1000000, "max_output_tokens": None,
        "limiting_factors": ["agent", "provider", "model"],
        "input_tokens": 200000, "input_budget": 995488, "output_reserve": 4000,
        "message_overhead": 12, "safety_margin": 512,
        "estimator_source": "fixture-estimator", "exact": False,
        "reason": None, "reason_message": "",
    }
    excluded = {
        **first, "source_index": 1, "candidate_index": None,
        "provider_id": 18, "provider_name": "Fixture South", "provider_model_id": 42,
        "pool_position": 2, "context_window": 128000, "provider_context_window": 128000,
        "model_context_window": 128000, "max_output_tokens": 16000,
        "input_tokens": 180000, "input_budget": 123488,
        "limiting_factors": ["provider", "model"],
        "reason": "context_overflow", "reason_message": "Fixture 128k capacity exceeded",
    }
    return {
        "version": 2, "planner_version": "v2", "estimator_version": "fixture",
        "can_run": True, "allowed_provider_ids": None, "output_reserve": 4000,
        "eligible": [first], "excluded": [excluded], "excluded_count": 1,
        "excluded_truncated": False,
        "limits": {"max_candidate_attempts": 16, "max_network_requests": 32,
                   "max_resolved_candidates": 64, "max_pool_nodes": 8},
    }


def usage(input_tokens, output_tokens, total_tokens, reported, unreported, complete):
    return {"known_input_tokens": input_tokens, "known_output_tokens": output_tokens,
            "known_total_tokens": total_tokens, "reported_requests": reported,
            "unreported_requests": unreported, "complete": complete}


@pytest.fixture(scope="module")
def local_vue():
    configured = os.environ.get("AI_UI_VUE_JS")
    if not configured or not Path(configured).is_file():
        pytest.skip("AI_UI_VUE_JS must name an existing offline Vue 3.4.38 runtime")
    data = Path(configured).read_bytes()
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    sri = re.search(r'vue@3\.4\.38/[^" ]+"\s+integrity="sha384-([^" ]+)"', base)
    assert sri is not None, "The shipped pinned Vue SRI must remain present"
    assert base64.b64encode(hashlib.sha384(data).digest()).decode("ascii") == sri.group(1)
    return data


@pytest.fixture(scope="module")
def browser(local_vue):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        try:
            instance = runtime.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip("An existing Chromium installation is required: " + str(exc))
        yield instance
        instance.close()


class OfflineUI:
    """Exact shipped documents, explicit synthetic APIs, no application imports."""

    def __init__(self, browser, local_vue, width):
        from playwright.sync_api import expect

        self.expect = expect
        self.width = width
        self.vue = local_vue
        self.requests = []
        self.unknown = []
        self.page_errors = []
        self.storage_writes = []
        self.allowed_writes = set()
        self.holds = set()
        self.pending = defaultdict(list)
        self.sync_results = ["succeeded"]
        self.operations = {}
        self.apply_conflicts = 0
        self.pool_conflicts = 0
        self.catalogue_failures = set()
        self.providers = [
            {"id": provider_id, "name": name, "provider_type": "openai_compatible",
             "base_url": "https://provider.fixture.invalid/v1", "default_model": "same/model",
             "context_window": 1000000, "timeout_seconds": 120, "max_retries": 0,
             "enabled": True, "stream_enabled": False, "proxy": None, "has_api_key": True}
            for provider_id, name in ((17, "Fixture North"), (18, "Fixture South"))
        ]
        self.models = {
            17: [model(41, 17, "same/model", "Shared Model"),
                 model(43, 17, "literal/model", MALICIOUS_LABEL),
                 model(44, 17, "disabled/model", "Disabled current", enabled=False, routable=False),
                 model(45, 17, "model", "Fixture identity plain"),
                 model(46, 17, "model ", "Fixture identity spaced"),
                 model(47, 17, "org/space model/v1 ", "Fixture identity slash")]
                + [model(100 + i, 17, "fixture/scroll-" + str(i), "Scrollable fixture " + str(i))
                   for i in range(24)],
            18: [model(42, 18, "same/model", "Shared Model", context_window=128000)],
        }
        self.revisions = {17: "a" * 64, 18: "b" * 64, 19: "c" * 64}
        members = [{"provider_model_id": mid, "provider_id": pid,
                    "model_key": "same/model", "enabled": True}
                   for mid, pid in ((41, 17), (42, 18))]
        self.pools = {7: {"id": 7, "name": "Fixture Ordered Group", "description": "Synthetic only",
                         "pool_kind": "custom", "fallback_pool_id": None,
                         "fallback_pool_name": None, "enabled": True, "version": 3,
                         "members": members, "referenced_by_agents": [], "referenced_by_pools": []}}
        self.agents = [
            {"id": 4, "name": "Fixture Legacy Task", "task_type": "keyword_clean",
             "binding_type": "fixed", "provider_id": 17, "model": "legacy/missing",
             "model_pool_id": None, "required_capabilities": ["text"],
             "system_prompt": SYSTEM_TEXT, "temperature": 0, "top_p": 0, "max_tokens": 4000,
             "context_window": 1000000, "enabled": False},
            {"id": 5, "name": "Fixture Group Task", "task_type": "general", "binding_type": "pool",
             "provider_id": None, "model": None, "model_pool_id": 7,
             "required_capabilities": ["text"], "system_prompt": "Saved group system",
             "temperature": 0.8, "top_p": 0.9, "max_tokens": 4000,
             "context_window": 1000000, "enabled": True},
            {"id": 6, "name": "Fixture Disabled Choice", "task_type": "general",
             "binding_type": "fixed", "provider_id": 17, "model": "disabled/model",
             "model_pool_id": None, "required_capabilities": [], "system_prompt": "",
             "temperature": 0, "top_p": 0, "max_tokens": 4000,
             "context_window": 16000, "enabled": True},
        ]
        common_job = {
            "category": "ai", "task_type": "keyword_clean", "status": "failed",
            "started_at": "2026-01-02T03:04:05Z", "finished_at": "2026-01-02T03:04:06Z",
            "duration_seconds": 1, "attempt_count": 1,
            "attempts": [{"attempt_index": 1, "stage": "main", "status": "partial",
                          "provider_id": 17, "provider_name_snapshot": "Fixture North",
                          "model_key": "same/model", "pool_name_snapshot": None,
                          "pool_position": None, "output_started": True, "usage": None,
                          "error_category": "connection", "error_message": "Fixture sanitized failure",
                          "started_at": "2026-01-02T03:04:05Z", "finished_at": "2026-01-02T03:04:06Z"}],
            "can_continue": False, "continue_unavailable_reason": "Fixture replay is unsupported",
            "candidate_snapshot": {"secret": "PRIVATE-SNAPSHOT-MUST-NOT-RENDER"},
            "route_summary": {"provider_name": "Fixture North", "model_key": "same/model"},
        }
        job_specs = [
            ("legacy", {"snapshot_version": 1, "validation_status": "unknown", "usage_summary": None,
                        "prompt_budget": {"input_budget": 37, "output_reserve": 3,
                                          "effective_context_window": 296}}),
            ("incomplete", {"snapshot_version": 2, "validation_status": "failed",
                            "invocation_plan": public_plan(), "usage_summary": usage(0, 5, None, 1, 1, False)}),
            ("zero", {"snapshot_version": 2, "validation_status": "passed",
                      "invocation_plan": public_plan(), "usage_summary": usage(0, 0, 0, 1, 0, True)}),
            ("not-sent", {"snapshot_version": 2, "validation_status": "unknown",
                          "invocation_plan": public_plan(), "usage_summary": usage(None, None, None, 0, 0, False)}),
            ("unavailable", {"snapshot_version": None, "validation_status": "unknown",
                             "invocation_plan": None, "plan_unavailable_reason": "Fixture saved plan unavailable",
                             "prompt_budget": {"input_budget": 654321, "output_reserve": 123,
                                               "effective_context_window": 765432},
                             "usage_summary": None}),
        ]
        self.jobs = {"fixture-" + name: {**deepcopy(common_job), "job_id": "fixture-" + name,
                                      "task_name": "Fixture job " + name, **fields}
                     for name, fields in job_specs}
        self.app = Flask(__name__, template_folder=str(TEMPLATES))
        self.app.jinja_env.variable_start_string = "{["
        self.app.jinja_env.variable_end_string = "]}"
        self.context = browser.new_context(viewport={"width": width, "height": 900}, has_touch=True,
                                           service_workers="block", locale="zh-CN")
        self.context.route("**/*", self._route)
        self.context.expose_binding("fixtureRecordStorage", lambda _source, item: self.storage_writes.append(item))
        self.context.add_init_script("""(() => {
          for (const method of ['setItem', 'removeItem', 'clear']) {
            const original = Storage.prototype[method];
            Storage.prototype[method] = function (...args) {
              window.fixtureRecordStorage({kind: this === localStorage ? 'local' : 'session', method, args});
              return original.apply(this, args);
            };
          }
        })();""")
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))

    def render(self, path):
        names = {"/dashboard/settings/models": "dashboard_settings_models.html",
                 "/dashboard/settings/agents": "dashboard_settings_agents.html",
                 "/dashboard/logs": "dashboard_logs.html"}
        with self.app.test_request_context(path):
            return render_template(names[urlsplit(path).path], task_labels={}, csp_nonce=lambda: "test-nonce")

    def allow(self, method, path):
        self.allowed_writes.add((method, path))

    def hold(self, method, path):
        self.holds.add((method, path))

    def release(self, method, path):
        key = (method, path)
        assert self.pending[key], "Expected an intercepted pending request: " + str(key)
        self.holds.discard(key)
        route, record = self.pending[key].pop(0)
        self._api(route, record)

    def _json(self, route, data, status=200, envelope=True):
        route.fulfill(status=status, content_type="application/json",
                      body=json.dumps({"ok": True, "data": data} if envelope else data))

    def _error(self, route, message, status):
        self._json(route, {"ok": False, "error": message}, status, envelope=False)

    def _route(self, route):
        request = route.request
        url = urlsplit(request.url)
        record = {"method": request.method, "url": request.url, "path": url.path,
                  "query": parse_qs(url.query), "body": json.loads(request.post_data) if request.post_data else None,
                  "csrf": request.headers.get("x-csrf-token")}
        self.requests.append(record)
        if request.url == VUE_URL and request.method == "GET":
            route.fulfill(body=self.vue, content_type="application/javascript",
                          headers={"Access-Control-Allow-Origin": "*"})
            return
        if url.scheme + "://" + url.netloc != ORIGIN:
            self.unknown.append(record)
            route.abort("blockedbyclient")
            return
        if request.method == "GET" and url.path in {
            "/dashboard/settings/models", "/dashboard/settings/agents", "/dashboard/logs"
        }:
            route.fulfill(content_type="text/html", body=self.render(url.path + "?" + url.query))
            return
        if request.method == "GET" and url.path.startswith("/static/"):
            target = (STATIC / url.path.removeprefix("/static/")).resolve()
            if target.is_relative_to(STATIC.resolve()) and target.is_file() and target.suffix in {".js", ".css"}:
                route.fulfill(body=target.read_bytes(), content_type=mimetypes.guess_type(target.name)[0])
                return
        if request.method == "GET" and url.path == "/favicon.ico":
            route.fulfill(status=204, body="")
            return
        if url.path.startswith("/api/"):
            key = (request.method, url.path)
            if request.method != "GET" and key not in self.allowed_writes:
                self.unknown.append(record)
                route.abort("blockedbyclient")
                return
            if key in self.holds:
                self.pending[key].append((route, record))
                return
            self._api(route, record)
            return
        self.unknown.append(record)
        route.abort("blockedbyclient")

    def catalogue(self, provider_id):
        items = deepcopy(self.models[provider_id])
        return {"items": items, "total": len(items), "catalog_revision": self.revisions[provider_id],
                "discovered_available": sum(item["discovered_available"] for item in items),
                "routable": sum(item["routable"] for item in items)}

    def _api(self, route, record):
        method, path, body = record["method"], record["path"], record["body"]
        query_routes = path == "/api/dashboard/logs" or bool(re.fullmatch(
            r"/api/dashboard/ai/(providers/(17|18|19)/models|model-pools/7/attempts)", path))
        if record["query"] and not query_routes:
            self.unknown.append(record)
            route.abort("blockedbyclient")
            return
        if method != "GET":
            assert record["csrf"] == CSRF, "Shipped csrfFetch must supply the fixture token"
        raw = {"/api/csrf-token": {"csrf_token": CSRF},
               "/api/dashboard/shell-data": {"pending_count": 0},
               "/api/dashboard/status": {"current_user": None},
               "/api/dashboard/auto-sync/status": {"running": False}}
        if method == "GET" and path in raw:
            return self._json(route, raw[path], envelope=False)
        if method == "GET" and path == "/api/dashboard/logs":
            assert set(record["query"]) <= {"page", "page_size", "days", "category", "task_type", "status", "is_auto"}
            items = [{key: value for key, value in job.items() if key in {
                "job_id", "task_type", "task_name", "category", "status", "started_at", "finished_at", "duration_seconds"
            }} for job in self.jobs.values()]
            return self._json(route, {"items": items, "page": 1, "total_pages": 1, "total": len(items)}, envelope=False)
        if method == "GET" and path == "/api/dashboard/ai/health":
            return self._json(route, {"totals": {"providers": len(self.providers), "providers_will_fail": 0,
                                                "routable_models": 28, "agents": len(self.agents), "agents_unhealthy": 0},
                                      "providers": [], "agents": [], "ai_job_failures": [], "window_days": 7})
        if path == "/api/dashboard/ai/providers":
            if method == "GET":
                return self._json(route, self.providers)
            if method == "POST":
                assert not any(item["id"] == 19 for item in self.providers), "Duplicate provider creation"
                saved = {**self.providers[0], **{k: v for k, v in body.items() if k != "api_key"}, "id": 19}
                self.providers.append(saved)
                self.models[19] = [model(61, 19, "new/selected", "Selected synthetic"),
                                   model(62, 19, "new/unselected", "Unselected synthetic"),
                                   model(63, 19, "new/manual", "Manual synthetic", source="manual", manual=True,
                                         discovered=False)]
                return self._json(route, {"id": 19})
        if path == "/api/dashboard/ai/providers/probe-models" and method == "POST":
            return self._json(route, {"items": [{"model_key": "new/selected", "display_name": "Selected synthetic", "suggested": True},
                                               {"model_key": "new/unselected", "display_name": "Unselected synthetic", "suggested": False}],
                                      "latency_ms": 0})
        if method == "GET" and path == "/api/dashboard/ai/agents":
            return self._json(route, self.agents)
        if method == "GET" and path == "/api/dashboard/ai/model-pools":
            return self._json(route, list(self.pools.values()))
        for provider in self.providers:
            pid = provider["id"]
            prefix = f"/api/dashboard/ai/providers/{pid}"
            if path == prefix + "/models" and method == "GET":
                assert set(record["query"]) <= {"search"}
                if pid in self.catalogue_failures:
                    return self._error(route, "Fixture catalogue temporarily unavailable", 503)
                result = self.catalogue(pid)
                search = record["query"].get("search", [""])[0]
                if search:
                    result["items"] = [m for m in result["items"] if search in m["model_key"]]
                return self._json(route, result)
            if path == prefix + "/models/sync" and method == "POST":
                status = self.sync_results.pop(0)
                operation_id = "fixture-operation-" + str(len(self.operations) + 1)
                self.operations[operation_id] = {"operation_id": operation_id, "provider_id": pid,
                    "status": status, "generation": 1, "result_digest": "d" * 64,
                    "error_message": "Fixture synthetic sync failure" if status == "failed" else ""}
                return self._json(route, {"operation_id": operation_id})
            if path == prefix + "/models/enabled" and method == "PUT":
                assert not record["query"], "Atomic selection must not target a filtered catalogue"
                if self.apply_conflicts:
                    self.apply_conflicts -= 1
                    self.revisions[pid] = "e" * 64
                    return self._error(route, "Fixture catalogue revision conflict", 409)
                assert body["catalog_revision"] == self.revisions[pid]
                for change in body["changes"]:
                    next(m for m in self.models[pid] if m["id"] == change["id"])["enabled"] = change["enabled"]
                return self._json(route, self.catalogue(pid))
        for pool_id, pool in self.pools.items():
            prefix = f"/api/dashboard/ai/model-pools/{pool_id}"
            if path == prefix and method == "GET":
                return self._json(route, pool)
            if path == prefix + "/attempts" and method == "GET":
                assert record["query"] == {"limit": ["20"]}
                return self._json(route, [])
            if path in {prefix, prefix + "/members"} and method == "PUT":
                if self.pool_conflicts:
                    self.pool_conflicts -= 1
                    pool["version"] += 1
                    pool["name"] = "Fixture Server Conflict"
                    return self._error(route, "Fixture pool version conflict", 409)
                assert body["expected_version"] == pool["version"]
                if path.endswith("/members"):
                    all_models = {m["id"]: m for rows in self.models.values() for m in rows}
                    pool["members"] = [{**change, "provider_id": all_models[change["provider_model_id"]]["provider_id"],
                                        "model_key": all_models[change["provider_model_id"]]["model_key"]}
                                       for change in body["members"]]
                else:
                    pool.update({k: v for k, v in body.items() if k != "expected_version"})
                pool["version"] += 1
                return self._json(route, pool)
        for agent in self.agents:
            prefix = f"/api/dashboard/ai/agents/{agent['id']}"
            if path == prefix and method == "PUT":
                agent.update(body)
                return self._json(route, agent)
            if path == prefix + "/candidates" and method == "GET":
                pool = self.pools.get(agent["model_pool_id"]) if agent["binding_type"] == "pool" else None
                if pool:
                    members = pool["members"]
                else:
                    provider = next(p for p in self.providers if p["id"] == agent["provider_id"])
                    key = agent["model"] if agent["model"] not in (None, "") else provider["default_model"]
                    members = [{"provider_id": agent["provider_id"], "model_key": key, "enabled": True}]
                candidates = []
                for position, member in enumerate(members, 1):
                    provider = next(p for p in self.providers if p["id"] == member["provider_id"])
                    catalogued = next((m for m in self.models[provider["id"]]
                                       if m["model_key"] == member["model_key"]), None)
                    if not member["enabled"] or catalogued and not catalogued["routable"]:
                        continue
                    candidates.append({"order": len(candidates) + 1,
                        "provider_id": provider["id"], "provider_name": provider["name"],
                        "model_key": member["model_key"], "provider_model_id": catalogued["id"] if catalogued else None,
                        "pool_id": pool["id"] if pool else None, "pool_name": pool["name"] if pool else None,
                        "pool_position": position if pool else None, "fallback_depth": 0,
                        "source": pool["name"] if pool else "fixed", "capabilities": ["text", "stream"],
                        "context_window": catalogued["context_window"] if catalogued else provider["context_window"]})
                return self._json(route, {"agent_id": agent["id"], "agent_name": agent["name"],
                    "task_type": agent["task_type"], "binding_type": agent["binding_type"],
                    "pool_id": pool["id"] if pool else None, "pool_name": pool["name"] if pool else None,
                    "candidates": candidates, "limits": public_plan()["limits"]})
            if path == prefix + "/preflight" and method == "POST":
                return self._json(route, public_plan())
        for job_id, job in self.jobs.items():
            if path == "/api/dashboard/ai/jobs/" + job_id and method == "GET":
                return self._json(route, job)
        for operation_id, operation in self.operations.items():
            prefix = "/api/dashboard/ai/model-sync-operations/" + operation_id
            if path == prefix and method == "GET":
                return self._json(route, operation)
            if path == prefix + "/events" and method == "GET":
                if operation["status"] == "running":
                    operation["status"] = "succeeded"
                event = {"succeeded": "completed", "failed": "failed", "cancelled": "cancelled",
                         "needs_empty_confirmation": "empty_confirmation_required"}[operation["status"]]
                return route.fulfill(content_type="text/event-stream",
                                     body="event: " + event + "\ndata: " + json.dumps(operation) + "\n\n")
            if path == prefix + "/confirm-empty" and method == "POST":
                assert body == {"generation": operation["generation"], "result_digest": operation["result_digest"]}
                operation["status"] = "succeeded"
                self.models[operation["provider_id"]] = []
                return self._json(route, operation)
        self.unknown.append(record)
        route.abort("blockedbyclient")

    def goto(self, path):
        self.page.goto(ORIGIN + path, wait_until="networkidle")
        self.expect(self.page.locator("#app")).to_be_visible()
        self.expect(self.page.locator("#app")).not_to_contain_text("{{")
        self.assert_viewport()

    def assert_viewport(self):
        assert self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"), self.width

    def assert_modal(self, dialog):
        self.expect(dialog).to_be_visible()
        box = dialog.bounding_box()
        assert box is not None
        assert box["x"] >= -1 and box["x"] + box["width"] <= self.width + 1, box
        assert box["y"] >= -1 and box["y"] + box["height"] <= 901, box

    def calls(self, method=None, path=None):
        return [r for r in self.requests if (method is None or r["method"] == method)
                and (path is None or r["path"] == path)]

    def writes(self):
        return [r for r in self.requests if r["method"] != "GET"]

    def edit_agent(self, name):
        card = self.page.locator("#ai-agents .border.rounded-lg").filter(has=self.page.get_by_text(name, exact=True))
        card.get_by_role("button", name=EDIT, exact=True).click()
        self.expect(self.page.locator("#agent-name")).to_have_value(name)

    def wait_agent_save_complete(self):
        self.expect(self.page.locator("#agent-name")).to_have_value("")
        self.expect(self.page.locator("#agent-name")).to_be_enabled()

    def edit_pool(self):
        self.page.locator("#pool-list").get_by_role("button", name=EDIT, exact=True).click()
        self.expect(self.page.locator("#pool-name")).to_have_value(self.pools[7]["name"])
        self.expect(self.page.locator("#pool-member-41-down")).to_be_enabled()

    def assert_storage(self):
        for item in self.storage_writes:
            assert item["kind"] == "local", item
            assert item["method"] != "clear", item
            assert re.fullmatch(r"pixiv-ai-model-sync:(17|18|19)", item["args"][0]), item
            if item["method"] == "setItem":
                assert re.fullmatch(r"fixture-(operation-\d+|recovered)", item["args"][1]), item
        for origin in self.context.storage_state()["origins"]:
            assert origin["origin"] == ORIGIN
            for item in origin["localStorage"]:
                assert item["name"].startswith("pixiv-ai-model-sync:")
                assert item["value"].startswith("fixture-")
        assert self.page.evaluate("sessionStorage.length") == 0
        text = json.dumps(self.storage_writes)
        assert KEY not in text and SYSTEM_TEXT not in text and "new/selected" not in text


@pytest.fixture(params=WIDTHS, ids=lambda width: f"touch-{width}px")
def ui(request, browser, local_vue):
    fixture = OfflineUI(browser, local_vue, request.param)
    yield fixture
    try:
        assert fixture.unknown == [], "Unknown/non-fixture network requests: " + repr(fixture.unknown)
        assert fixture.page_errors == [], "Real Vue/browser runtime errors: " + repr(fixture.page_errors)
        fixture.assert_storage()
    finally:
        fixture.context.close()


def test_old_urls_workspace_filter_reload_and_history_are_read_only(ui):
    page = ui.page
    ui.goto("/dashboard/settings/models?view=providers&provider=17&keep=fixture")
    ui.expect(page.locator("#workspace-providers")).to_have_attribute("aria-current", "page")
    for view in ("models", "groups"):
        page.locator("#workspace-" + view).click()
        ui.expect(page).to_have_url(re.compile("view=" + view))
        ui.expect(page.locator("#workspace-" + view)).to_have_attribute("aria-current", "page")
        assert parse_qs(urlsplit(page.url).query)["keep"] == ["fixture"]
        assert parse_qs(urlsplit(page.url).query)["provider"] == ["17"]
        ui.assert_viewport()
    page.go_back(wait_until="networkidle")
    ui.expect(page.locator("#workspace-models")).to_have_attribute("aria-current", "page")
    page.go_forward(wait_until="networkidle")
    ui.expect(page.locator("#workspace-groups")).to_have_attribute("aria-current", "page")
    page.reload(wait_until="networkidle")
    ui.expect(page.locator("#workspace-groups")).to_have_attribute("aria-current", "page")
    ui.goto("/dashboard/settings/models?view=models&provider=17")
    ui.expect(page.get_by_role("heading", name=re.compile("Fixture North"))).to_be_visible()
    ui.expect(page.get_by_role("heading", name=re.compile("Fixture South"))).to_have_count(0)
    ui.goto("/dashboard/settings/models?view=models&provider=999")
    ui.expect(page.get_by_role("link", name="\u6e05\u9664 Provider \u7b5b\u9009")).to_be_visible()
    ui.expect(page.get_by_role("heading", name=re.compile("Fixture North|Fixture South"))).to_have_count(0)
    page.get_by_role("link", name="\u6e05\u9664 Provider \u7b5b\u9009").click()
    ui.expect(page).not_to_have_url(re.compile("provider="))
    ui.goto("/dashboard/settings/agents")
    ui.expect(page.locator("#ai-agents")).to_be_visible()
    ui.expect(page.locator("#ai-preflight")).to_be_visible()
    assert ui.writes() == []


def test_picker_real_modal_keyboard_literal_labels_and_exact_explicit_identity(ui):
    page = ui.page
    ui.goto("/dashboard/settings/agents")
    ui.edit_agent("Fixture Legacy Task")
    trigger = page.locator("#agent-binding-picker")
    trigger.focus()
    trigger.press("Enter")
    dialog = page.locator("#agent-binding-picker-dialog")
    ui.assert_modal(dialog)
    search = page.locator("#agent-binding-picker-filter")
    assert not search.evaluate("el => el === document.activeElement")
    results = page.locator("#agent-binding-picker-options")
    assert results.evaluate("el => el.scrollHeight > el.clientHeight")
    assert results.evaluate("el => getComputedStyle(el).overflowY") == "auto"
    results.hover()
    page.mouse.wheel(0, 500)
    page.wait_for_function("document.getElementById('agent-binding-picker-options').scrollTop > 0")
    ui.expect(dialog.get_by_text(MALICIOUS_LABEL, exact=True)).to_be_visible()
    assert dialog.locator("img").count() == 0
    assert page.evaluate("window.fixtureInjected === undefined")
    ui.expect(dialog.get_by_role("option").filter(has_text="legacy/missing")).to_have_count(1)
    search.fill("Shared Model")
    options = dialog.get_by_role("option").filter(has_text="Shared Model")
    ui.expect(options).to_have_count(2)
    assert "Fixture North" in options.nth(0).inner_text()
    assert "Fixture South" in options.nth(1).inner_text()
    focusable = dialog.locator('button:not([disabled]), input:not([disabled]), [href], select:not([disabled])')
    focusable.last.focus()
    page.keyboard.press("Tab")
    assert focusable.first.evaluate("el => el === document.activeElement")
    page.keyboard.press("Shift+Tab")
    assert focusable.last.evaluate("el => el === document.activeElement")
    dialog.press("Escape")
    ui.expect(dialog).to_have_count(0)
    ui.expect(trigger).to_be_focused()
    assert ui.writes() == []
    trigger.tap()
    search.fill("Shared Model")
    south = page.get_by_role("option").filter(has_text="Shared Model").filter(has_text="Fixture South")
    south.focus()
    south.press("Enter")
    ui.expect(dialog).to_have_count(0)
    assert ui.writes() == [], "Selecting an option changes only the local draft"
    ui.allow("PUT", "/api/dashboard/ai/agents/4")
    with page.expect_response(lambda response: response.url.endswith("/agents/4") and response.request.method == "PUT"):
        page.locator("#agent-save").click()
    ui.wait_agent_save_complete()
    writes = ui.calls("PUT", "/api/dashboard/ai/agents/4")
    assert [r["body"] for r in writes] == [{"binding_type": "fixed", "provider_id": 18, "model": "same/model"}]
    ui.edit_agent("Fixture Legacy Task")
    trigger.tap()
    search.fill("Shared Model")
    page.get_by_role("option").filter(has_text="Shared Model").filter(has_text="Fixture North").tap()
    assert len(ui.writes()) == 1
    with page.expect_response(lambda response: response.url.endswith("/agents/4") and response.request.method == "PUT"):
        page.locator("#agent-save").click()
    ui.wait_agent_save_complete()
    assert ui.calls("PUT", "/api/dashboard/ai/agents/4")[1]["body"] == {
        "binding_type": "fixed", "provider_id": 17, "model": "same/model"}
    ui.edit_agent("Fixture Legacy Task")
    trigger.tap()
    search.fill("Fixture identity")
    ui.expect(page.get_by_role("option").filter(has_text="Fixture identity plain")).to_have_count(1)
    ui.expect(page.get_by_role("option").filter(has_text="Fixture identity spaced")).to_have_count(1)
    page.get_by_role("option").filter(has_text="Fixture identity spaced").tap()
    ui.expect(page.locator("#agent-legacy-model")).to_have_value("model ")
    assert len(ui.writes()) == 2
    with page.expect_response(lambda response: response.url.endswith("/agents/4") and response.request.method == "PUT"):
        page.locator("#agent-save").click()
    ui.wait_agent_save_complete()
    assert ui.calls("PUT", "/api/dashboard/ai/agents/4")[2]["body"] == {
        "binding_type": "fixed", "provider_id": 17, "model": "model "}
    for value in ("org/space model/v1 ", ""):
        ui.edit_agent("Fixture Legacy Task")
        page.locator("#agent-legacy-model").fill(value)
        with page.expect_response(lambda response: response.url.endswith("/agents/4") and response.request.method == "PUT"):
            page.locator("#agent-save").click()
        ui.wait_agent_save_complete()
        expected = value if value != "" else None
        assert ui.calls("PUT", "/api/dashboard/ai/agents/4")[-1]["body"] == {
            "binding_type": "fixed", "provider_id": 17, "model": expected}
        assert next(agent for agent in ui.agents if agent["id"] == 4)["model"] == expected
    ui.assert_viewport()


def test_agent_save_locks_rapid_edit_until_followup_list_read_completes(ui):
    page = ui.page
    ui.goto("/dashboard/settings/agents")
    ui.edit_agent("Fixture Legacy Task")
    page.locator("#agent-name").fill("Fixture Save Locked")
    ui.allow("PUT", "/api/dashboard/ai/agents/4")
    endpoint = "/api/dashboard/ai/agents"
    ui.hold("GET", endpoint)
    with page.expect_request(lambda request: request.url == ORIGIN + endpoint and request.method == "GET"):
        page.locator("#agent-save").click()
    other = page.locator("#ai-agents .border.rounded-lg").filter(
        has=page.get_by_text("Fixture Group Task", exact=True)).get_by_role("button", name=EDIT, exact=True)
    try:
        ui.expect(page.locator("#agent-name")).to_be_disabled()
        # Check the container's native lock as well as its actual controls.
        ui.expect(page.locator("#agent-editor-fields")).to_have_js_property("disabled", True)
        ui.expect(page.locator("#agent-editor-fields")).to_have_attribute("aria-busy", "true")
        ui.expect(page.locator("#agent-binding-picker")).to_be_disabled()
        ui.expect(other).to_be_disabled()
        ui.expect(page.locator("#agent-name")).to_have_value("Fixture Save Locked")
        assert len(ui.pending[("GET", endpoint)]) == 1
        assert [r["body"] for r in ui.writes()] == [{"name": "Fixture Save Locked"}]
    finally:
        if ui.pending[("GET", endpoint)]:
            ui.release("GET", endpoint)
    ui.wait_agent_save_complete()
    ui.expect(other).to_be_enabled()
    other.click()
    ui.expect(page.locator("#agent-name")).to_have_value("Fixture Group Task")
    assert [r["body"] for r in ui.writes()] == [{"name": "Fixture Save Locked"}]
    ui.assert_viewport()


def test_saved_missing_and_disabled_current_survive_catalogue_reload_and_name_only_edit(ui):
    page = ui.page
    ui.goto("/dashboard/settings/agents")
    ui.edit_agent("Fixture Disabled Choice")
    page.locator("#agent-binding-picker").click()
    current = page.get_by_role("option").filter(has_text="disabled/model")
    ui.expect(current).to_be_disabled()
    page.locator("#agent-binding-picker-dialog").press("Escape")
    ui.models[17] = [m for m in ui.models[17] if m["id"] != 44]
    ui.revisions[17] = "f" * 64
    page.reload(wait_until="networkidle")
    ui.edit_agent("Fixture Disabled Choice")
    page.locator("#agent-binding-picker").click()
    ui.expect(page.get_by_role("option").filter(has_text="disabled/model")).to_have_count(1)
    page.locator("#agent-binding-picker-dialog").press("Escape")
    ui.edit_agent("Fixture Legacy Task")
    page.locator("#agent-name").fill("Fixture Renamed Only")
    ui.allow("PUT", "/api/dashboard/ai/agents/4")
    with page.expect_response(lambda response: response.url.endswith("/agents/4") and response.request.method == "PUT"):
        page.locator("#agent-save").click()
    assert [r["body"] for r in ui.writes()] == [{"name": "Fixture Renamed Only"}]
    saved = next(a for a in ui.agents if a["id"] == 4)
    assert (saved["model"], saved["temperature"], saved["top_p"], saved["enabled"]) == ("legacy/missing", 0, 0, False)


def test_group_order_keyboard_touch_and_dirty_switch_conflict_keep_draft(ui):
    page = ui.page
    ui.goto("/dashboard/settings/models?view=groups")
    ui.edit_pool()
    for selector in ("#pool-member-41-down", "#pool-member-42-up"):
        box = page.locator(selector).bounding_box()
        assert box is not None and box["width"] >= 44 and box["height"] >= 44, box
    page.locator("#pool-member-41-down").focus()
    page.keyboard.press("ArrowDown")
    ui.expect(page.locator("#pool-member-41-up")).to_be_focused()
    order = lambda: page.locator('#pool-editor button[id$="-up"]').evaluate_all("els => els.map(el => Number(el.id.split('-')[2]))")
    assert order() == [42, 41]
    page.locator("#pool-member-41-up").tap()
    assert order() == [41, 42]
    page.locator("#pool-member-41-down").tap()
    page.locator("#pool-name").fill("Fixture Local Draft")
    page.locator("#workspace-models").click()
    dialog = page.get_by_role("dialog", name="\u4fdd\u7559\u672a\u4fdd\u5b58\u7684\u4fee\u6539\uff1f")
    ui.assert_modal(dialog)
    dialog.get_by_role("button", name="\u7ee7\u7eed\u7f16\u8f91", exact=True).click()
    ui.expect(page.locator("#pool-name")).to_have_value("Fixture Local Draft")
    assert order() == [42, 41] and ui.writes() == []
    ui.pool_conflicts = 1
    ui.allow("PUT", "/api/dashboard/ai/model-pools/7")
    ui.allow("PUT", "/api/dashboard/ai/model-pools/7/members")
    page.get_by_role("button", name="\u4fdd\u5b58\u8def\u7531\u7ec4", exact=True).click()
    ui.expect(page.get_by_text(re.compile("\u7248\u672c\u51b2\u7a81\uff0c\u8349\u7a3f\u672a\u8986\u76d6"))).to_be_visible()
    ui.expect(page.locator("#pool-name")).to_have_value("Fixture Local Draft")
    assert order() == [42, 41]
    assert ui.calls("PUT", "/api/dashboard/ai/model-pools/7/members") == []
    page.get_by_role("button", name="\u5ba1\u9605\u540e\u4fdd\u7559\u6211\u7684\u4fee\u6539", exact=True).click()
    with page.expect_response(lambda response: response.url.endswith("/7/members") and response.request.method == "PUT"):
        page.get_by_role("button", name="\u4fdd\u5b58\u8def\u7531\u7ec4", exact=True).click()
    ui.expect(page.get_by_text("\u8def\u7531\u7ec4\u5df2\u4fdd\u5b58", exact=True)).to_be_visible()
    metadata = ui.calls("PUT", "/api/dashboard/ai/model-pools/7")
    assert [r["body"]["expected_version"] for r in metadata] == [3, 4]
    assert ui.calls("PUT", "/api/dashboard/ai/model-pools/7/members")[0]["body"] == {
        "expected_version": 5, "members": [{"provider_model_id": 42, "enabled": True},
                                            {"provider_model_id": 41, "enabled": True}]}
    assert order() == [42, 41]
    page.locator("#pool-name").fill("Fixture Discard Me")
    page.locator("#workspace-models").click()
    page.get_by_role("dialog").get_by_role("button", name="\u4e22\u5f03\u8349\u7a3f\u5e76\u79bb\u5f00", exact=True).click()
    ui.expect(page.locator("#workspace-models")).to_have_attribute("aria-current", "page")
    assert len(ui.writes()) == 3


def prepare_provider(ui, selected=True):
    ui.goto("/dashboard/settings/models?view=providers")
    page = ui.page
    page.locator("#provider-name").fill("Fixture Created Provider")
    page.locator("#provider-address").fill("https://new-provider.fixture.invalid/v1")
    page.locator("#provider-key").fill(KEY)
    ui.allow("POST", "/api/dashboard/ai/providers")
    if selected:
        ui.allow("POST", "/api/dashboard/ai/providers/probe-models")
        page.get_by_role("button", name=PROBE, exact=True).click()
        checkbox = page.get_by_role("checkbox", name="\u4fdd\u5b58\u540e\u521d\u59cb\u542f\u7528 new/selected", exact=True)
        ui.expect(checkbox).not_to_be_checked()
        checkbox.check()
        ui.allow("POST", "/api/dashboard/ai/providers/19/models/sync")
        ui.allow("PUT", "/api/dashboard/ai/providers/19/models/enabled")


def test_onboarding_no_selection_is_saved_without_sync(ui):
    prepare_provider(ui, selected=False)
    ui.page.get_by_role("button", name=SAVE_PROVIDER, exact=True).click()
    ui.expect(ui.page.locator("#provider-editor")).to_contain_text("\u672a\u9009\u62e9\u6a21\u578b\uff0c\u672a\u540c\u6b65\u6216\u6279\u91cf\u542f\u7528")
    assert [(r["method"], r["path"]) for r in ui.writes()] == [("POST", "/api/dashboard/ai/providers")]
    ui.expect(ui.page.locator("#provider-key")).to_have_value("")
    ui.assert_storage()


def test_onboarding_pending_sync_failure_cas_review_and_success_trace(ui):
    prepare_provider(ui)
    page = ui.page
    ui.sync_results = ["failed", "succeeded"]
    ui.apply_conflicts = 1
    events = "/api/dashboard/ai/model-sync-operations/fixture-operation-1/events"
    ui.hold("GET", events)
    page.get_by_role("button", name=SAVE_PROVIDER, exact=True).click()
    ui.expect(page.locator("#provider-editor")).to_contain_text("Provider \u5df2\u4fdd\u5b58\uff0c\u6b63\u5728\u540c\u6b65\u5df2\u4fdd\u5b58\u76ee\u5f55")
    ui.expect(page.locator("#provider-key")).to_have_value("")
    ui.expect(page.locator("#provider-name")).to_be_disabled()
    assert len(ui.calls("POST", "/api/dashboard/ai/providers")) == 1
    assert not ui.calls("PUT", "/api/dashboard/ai/providers/19/models/enabled")
    page.wait_for_function("localStorage.getItem('pixiv-ai-model-sync:19') === 'fixture-operation-1'")
    ui.release("GET", events)
    retry = page.get_by_role("button", name="\u91cd\u8bd5\u76ee\u5f55\u540c\u6b65", exact=True)
    ui.expect(retry).to_be_visible()
    ui.expect(page.locator("#provider-editor")).to_contain_text("\u540c\u6b65\u5931\u8d25\uff0c\u9009\u62e9\u5c1a\u672a\u5e94\u7528")
    choice_a = page.get_by_role("checkbox", name="\u4fdd\u5b58\u540e\u521d\u59cb\u542f\u7528 new/selected", exact=True)
    choice_b = page.get_by_role("checkbox", name="\u4fdd\u5b58\u540e\u521d\u59cb\u542f\u7528 new/unselected", exact=True)
    choice_a.uncheck()
    choice_b.check()
    retry.click()
    review = page.get_by_role("button", name="\u5237\u65b0\u5b8c\u6574\u76ee\u5f55\u5e76\u5ba1\u9605", exact=True)
    ui.expect(review).to_be_visible()
    ui.expect(page.locator("#provider-editor")).to_contain_text("\u76ee\u5f55\u7248\u672c\u51b2\u7a81")
    assert len(ui.calls("PUT", "/api/dashboard/ai/providers/19/models/enabled")) == 1
    assert ui.calls("PUT", "/api/dashboard/ai/providers/19/models/enabled")[0]["body"]["changes"] == [
        {"id": 61, "enabled": False}, {"id": 62, "enabled": True}]
    review.click()
    confirm = page.get_by_role("button", name="\u786e\u8ba4\u4f7f\u7528\u6700\u65b0\u7248\u672c\u5e94\u7528\u9009\u62e9", exact=True)
    ui.expect(confirm).to_be_visible()
    assert len(ui.calls("PUT", "/api/dashboard/ai/providers/19/models/enabled")) == 1
    choice_a.check()
    choice_b.uncheck()
    confirm.click()
    ui.expect(page.locator("#provider-editor")).to_contain_text("Provider \u4e0e\u521d\u59cb\u6a21\u578b\u9009\u62e9\u5df2\u4fdd\u5b58")
    applied = ui.calls("PUT", "/api/dashboard/ai/providers/19/models/enabled")
    assert [r["body"]["catalog_revision"] for r in applied] == ["c" * 64, "e" * 64]
    assert [r["body"]["changes"] for r in applied] == [
        [{"id": 61, "enabled": False}, {"id": 62, "enabled": True}],
        [{"id": 61, "enabled": True}, {"id": 62, "enabled": False}]]
    assert next(model for model in ui.models[19] if model["id"] == 63)["enabled"] is True
    assert len(ui.calls("POST", "/api/dashboard/ai/providers")) == 1
    assert len(ui.calls("POST", "/api/dashboard/ai/providers/19/models/sync")) == 2
    assert all(not r["query"] for r in ui.calls("GET", "/api/dashboard/ai/providers/19/models"))
    assert page.evaluate("localStorage.length") == 0
    ui.assert_storage()


def test_onboarding_empty_confirmation_is_not_success_or_silent_selection(ui):
    prepare_provider(ui)
    page = ui.page
    ui.sync_results = ["needs_empty_confirmation"]
    page.get_by_role("button", name=SAVE_PROVIDER, exact=True).click()
    ui.expect(page.locator("#provider-editor")).to_contain_text("\u7b49\u5f85\u660e\u786e\u786e\u8ba4\u6216\u53d6\u6d88\u7a7a\u76ee\u5f55")
    assert not ui.calls("PUT", "/api/dashboard/ai/providers/19/models/enabled")
    assert page.evaluate("localStorage.getItem('pixiv-ai-model-sync:19')") == "fixture-operation-1"
    endpoint = "/api/dashboard/ai/model-sync-operations/fixture-operation-1/confirm-empty"
    ui.allow("POST", endpoint)
    page.locator("#provider-editor").get_by_role("button", name="\u786e\u8ba4\u7a7a\u76ee\u5f55", exact=True).click()
    ui.expect(page.locator("#provider-editor")).to_contain_text("\u9009\u62e9\u5c1a\u672a\u5e94\u7528")
    assert not ui.calls("PUT", "/api/dashboard/ai/providers/19/models/enabled")
    assert len(ui.calls("POST", "/api/dashboard/ai/providers/19/models/sync")) == 1
    assert ui.calls("POST", endpoint)[0]["body"] == {"generation": 1, "result_digest": "d" * 64}
    assert page.evaluate("localStorage.length") == 0


def test_sync_recovery_only_uses_synthetic_operation_id_and_never_restarts(ui):
    ui.operations["fixture-recovered"] = {"operation_id": "fixture-recovered", "provider_id": 17,
                                           "status": "running"}
    ui.context.add_init_script("localStorage.setItem('pixiv-ai-model-sync:17', 'fixture-recovered')")
    ui.goto("/dashboard/settings/models?view=models&provider=17")
    assert ui.calls("GET", "/api/dashboard/ai/model-sync-operations/fixture-recovered")
    assert ui.calls("GET", "/api/dashboard/ai/model-sync-operations/fixture-recovered/events")
    assert ui.page.evaluate("localStorage.length") == 0
    assert ui.writes() == []


def assert_public_rows(ui, eligible_id, excluded_id):
    first = ui.page.locator(f"#{eligible_id} [data-source-index='0']")
    excluded = ui.page.locator(f"#{excluded_id} [data-source-index='1']")
    ui.expect(first).to_have_count(1)
    ui.expect(excluded).to_have_count(1)
    ui.expect(first).to_have_attribute("data-candidate-index", "0")
    assert excluded.get_attribute("data-candidate-index") is None
    for row, expected in ((first, public_plan()["eligible"][0]), (excluded, public_plan()["excluded"][0])):
        for field in ("context_window", "agent_context_window", "provider_context_window", "model_context_window",
                      "input_tokens", "input_budget", "output_reserve", "message_overhead", "safety_margin"):
            cell = row.locator(f'[data-field="{field}"]')
            assert cell.inner_text().replace(",", "").strip() == str(expected[field]), (field, cell.inner_text())
        ui.expect(row.locator('[data-field="estimator_source"]')).to_contain_text("fixture-estimator")
        ui.expect(row.locator('[data-field="limiting_factors"]')).to_contain_text("provider")
    ui.expect(first.locator('[data-field="max_output_tokens"]')).to_have_text(UNKNOWN)
    ui.expect(excluded).to_contain_text("Fixture 128k capacity exceeded")


def test_preflight_complete_displayed_input_explicit_zero_and_server_plan_no_job(ui):
    page = ui.page
    ui.goto("/dashboard/settings/agents")
    chain = page.locator('[data-hook="agent-candidate-chain"]')
    explanation = chain.locator("div.text-sm.font-bold")
    chain.locator("select").select_option("4")
    ui.expect(explanation).to_contain_text("Fixture Legacy Task")
    ui.expect(chain.locator("ol")).to_contain_text("legacy/missing")
    assert len(ui.calls("GET", "/api/dashboard/ai/agents/4/candidates")) == 1
    page.locator("#preflight-agent").select_option("5")
    ui.expect(explanation).to_contain_text("Fixture Group Task")
    ui.expect(chain.locator("ol")).to_contain_text("Fixture South / same/model")
    ui.expect(chain.locator("ol")).not_to_contain_text("legacy/missing")
    assert len(ui.calls("GET", "/api/dashboard/ai/agents/5/candidates")) == 1
    page.locator("#preflight-agent").select_option("0")
    ui.expect(explanation).to_have_count(0)
    ui.expect(chain.locator("ol")).to_have_count(0)
    assert not ui.calls("GET", "/api/dashboard/ai/agents/0/candidates")
    assert ui.writes() == []
    page.locator("#preflight-agent").select_option("4")
    ui.expect(explanation).to_contain_text("Fixture Legacy Task")
    ui.expect(page.locator("#preflight-system-prompt")).to_have_text(SYSTEM_TEXT)
    page.locator("#preflight-text").fill(INPUT_TEXT)
    for field, value in (("temperature", "0"), ("top_p", "0"), ("max_tokens", "123")):
        page.locator("#preflight-use-" + field).check()
        page.locator("#preflight-" + field).fill(value)
    endpoint = "/api/dashboard/ai/agents/4/preflight"
    ui.allow("POST", endpoint)
    page.locator("#preflight-run").click()
    ui.expect(page.locator("#preflight-eligible [data-source-index='0']")).to_be_visible()
    expected = {"messages": [{"role": "system", "content": SYSTEM_TEXT}, {"role": "user", "content": INPUT_TEXT}],
                "temperature": 0, "top_p": 0, "max_tokens": 123}
    assert [r["body"] for r in ui.writes()] == [expected]
    assert_public_rows(ui, "preflight-eligible", "preflight-excluded")
    ui.expect(page.locator("#preflight-status")).to_contain_text(re.compile("\u53ef|\u5408\u683c"))
    page.locator("#preflight-use-top_p").uncheck()
    ui.expect(page.locator("#preflight-eligible [data-source-index='0']")).to_have_count(0)
    page.locator("#preflight-run").click()
    ui.expect(page.locator("#preflight-eligible [data-source-index='0']")).to_be_visible()
    assert ui.calls("POST", endpoint)[1]["body"] == {k: v for k, v in expected.items() if k != "top_p"}
    page.locator("#preflight-clear").click()
    ui.expect(page.locator("#preflight-text")).to_have_value("")
    ui.expect(page.locator("#preflight-eligible [data-source-index='0']")).to_have_count(0)
    assert all(r["method"] == "POST" and r["path"] == endpoint for r in ui.writes())
    assert not any("/jobs" in r["path"] or r["path"].endswith("/test") for r in ui.requests)
    ui.assert_viewport()


def test_logs_v1_v2_unknown_zero_no_network_and_unavailable_plan_never_continue(ui):
    page = ui.page
    ui.goto("/dashboard/logs?category=ai")
    for name in ("legacy", "incomplete", "zero", "not-sent", "unavailable"):
        page.get_by_role("row").filter(has_text="Fixture job " + name).get_by_role("button", name="\u8be6\u60c5", exact=True).click()
        dialog = page.get_by_role("dialog")
        ui.assert_modal(dialog)
        ui.expect(page.locator("#ai-job-diagnostics")).to_be_visible()
        ui.expect(page.locator("#job-continue-unavailable")).to_contain_text("Fixture replay is unsupported")
        assert dialog.get_by_role("button", name=re.compile("\u7ee7\u7eed|Continue", re.I)).count() == 0
        assert "PRIVATE-SNAPSHOT-MUST-NOT-RENDER" not in dialog.inner_text()
        if name in {"legacy", "unavailable"}:
            ui.expect(page.locator("#job-usage-status")).to_have_text(UNKNOWN)
        if name == "legacy":
            ui.expect(dialog).to_contain_text("37")
            ui.expect(page.locator("#job-legacy-budget")).to_be_visible()
            ui.expect(page.locator("#job-plan-eligible")).to_have_count(0)
        else:
            ui.expect(page.locator("#job-legacy-budget")).to_have_count(0)
        if name in {"incomplete", "zero", "not-sent"}:
            assert_public_rows(ui, "job-plan-eligible", "job-plan-excluded")
        if name == "incomplete":
            ui.expect(page.locator("#job-validation-status")).to_contain_text(re.compile("failed|\u5931\u8d25"))
            ui.expect(page.locator('[data-usage-field="known_input_tokens"]')).to_have_text("0")
            ui.expect(page.locator('[data-usage-field="known_output_tokens"]')).to_have_text("5")
            ui.expect(page.locator('[data-usage-field="known_total_tokens"]')).to_have_text(UNKNOWN)
            ui.expect(page.locator("#job-usage-status")).to_contain_text(re.compile("\u4e0d\u5b8c\u6574|\u672a\u5b8c\u6574|incomplete"))
        if name == "zero":
            for field in ("known_input_tokens", "known_output_tokens", "known_total_tokens"):
                ui.expect(page.locator(f'[data-usage-field="{field}"]')).to_have_text("0")
        if name == "not-sent":
            ui.expect(page.locator("#job-usage-status")).to_contain_text("\u672a\u53d1\u51fa\u8bf7\u6c42")
        if name == "unavailable":
            ui.expect(page.locator("#job-plan-unavailable")).to_contain_text("Fixture saved plan unavailable")
            ui.expect(page.locator("#job-plan-eligible")).to_have_count(0)
            ui.expect(dialog.get_by_text("654321", exact=True)).to_have_count(0)
            ui.expect(dialog.get_by_text("765432", exact=True)).to_have_count(0)
        dialog.press("Escape")
        ui.expect(dialog).to_have_count(0)
    assert ui.writes() == []
    ui.assert_viewport()


def test_late_job_detail_cannot_reopen_closed_or_replace_new_modal(ui):
    page = ui.page
    ui.goto("/dashboard/logs?category=ai")
    endpoint = "/api/dashboard/ai/jobs/fixture-legacy"
    ui.hold("GET", endpoint)
    page.get_by_role("row").filter(has_text="Fixture job legacy").get_by_role("button", name="\u8be6\u60c5", exact=True).click()
    dialog = page.get_by_role("dialog")
    ui.expect(dialog).to_be_visible()
    dialog.press("Escape")
    ui.expect(dialog).to_have_count(0)
    page.get_by_role("row").filter(has_text="Fixture job incomplete").get_by_role("button", name="\u8be6\u60c5", exact=True).click()
    ui.expect(page.locator("#ai-job-diagnostics")).to_be_visible()
    ui.release("GET", endpoint)
    ui.expect(page.locator('[data-usage-field="known_output_tokens"]')).to_have_text("5")
    ui.expect(page.get_by_role("dialog")).to_contain_text("fixture-incomplete")
    page.get_by_role("dialog").press("Escape")
    ui.expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.writes() == []

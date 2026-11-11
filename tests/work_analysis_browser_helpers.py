from copy import deepcopy
import ast
import hashlib
import json
import mimetypes
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from flask import render_template

from test_ai_settings_browser import (
    CSRF, ORIGIN, ROOT, STATIC, TEMPLATES, VUE_URL, OfflineUI,
)
from test_deployment_config import _ai_javascript_location


BASE = "/api/dashboard/work-analyses"
RUN = "a" * 32
UNIT = "b" * 64
EVIDENCE = "c" * 64
CLAIM = "d" * 64
BLOCK = "e" * 64
LABEL = '<img src="/fixture-injected" onerror="window.fixtureInjected=true">'
QUOTE = "\u552f\u4e00\u5f15\u6587\U0001f680 " + LABEL
BODY = ("Synthetic prefix \U0001f600 " * 16) + QUOTE + (" synthetic suffix " * 16)
TEXT_HASH = hashlib.sha256(BODY.encode("utf-8")).hexdigest()
START = BODY.index(QUOTE)
END = START + len(QUOTE)
LIMITS = {"request_limit": 16, "token_limit": 500000, "active_seconds_limit": 600}
APPROVALS = ("order_confirmed", "current_archive_only", "external_send_confirmed", "budget_confirmed")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def usage():
    return {"known_input_tokens": 0, "known_output_tokens": 0, "known_total_tokens": 0,
            "reported_requests": 1, "unreported_requests": 1,
            "network_request_count": 2, "complete": False}


def request_draft(kind="series"):
    return {"selection": {"source_kind": kind, "source_id": 501 if kind == "series" else 101,
                          "publication_state": "unknown"},
            "agent_id": 7, "output_tokens": 4000, "budget": deepcopy(LIMITS),
            "confirmations": dict.fromkeys(APPROVALS, False)}


def preview_document(payload, revision=0):
    selection = payload["selection"]
    chapters = [{"novel_id": 101, "position": 0, "title": "Synthetic chapter",
                 "text_hash": TEXT_HASH, "chars": len(BODY), "utf8_bytes": len(BODY.encode()),
                 "fetched_at": "2026-01-01T00:00:00Z", "has_text": True}]
    if selection["source_kind"] == "series":
        chapters.append({"novel_id": 102, "position": 1, "title": "Empty synthetic chapter",
                         "text_hash": hashlib.sha256(b"").hexdigest(), "chars": 0,
                         "utf8_bytes": 0, "fetched_at": None, "has_text": False})
    by_id = {row["novel_id"]: row for row in chapters}
    ordered = selection.get("ordered_novel_ids", list(by_id))
    assert sorted(ordered) == sorted(by_id)
    chapters = [{**by_id[novel_id], "position": index} for index, novel_id in enumerate(ordered)]
    manifest = {"schema_version": 1, "source_kind": selection["source_kind"],
                "source_id": selection["source_id"], "title": "Synthetic work" if revision == 0 else "Revised synthetic work",
                "publication_state": selection["publication_state"],
                "publication_source": "unknown" if selection["publication_state"] == "unknown" else "user_declared",
                "order_source": "user_confirmed" if "ordered_novel_ids" in selection else "publication_date_suggestion",
                "expected_count": 3 if selection["source_kind"] == "series" else None,
                "expected_count_observed_at": None, "remote_inventory_verified": False,
                "requires_partial_confirmation": True, "chapters": chapters}
    manifest["manifest_fingerprint"] = digest(manifest)
    unit = {"unit_id": UNIT, "kind": "whole", "is_final": True, "level": 0,
            "primary_ranges": [{"novel_id": 101, "text_hash": TEXT_HASH, "start": 0, "end": len(BODY)}],
            "context_ranges": [], "child_unit_ids": [], "claim_refs": [], "input_fingerprint": digest(manifest)}
    plan = {"schema_version": 1, "strategy": "whole", "manifest_fingerprint": manifest["manifest_fingerprint"],
            "chunker_version": 1, "prompt_version": 1, "report_schema_version": 1,
            "original_units": [unit], "minimum_requests": 1, "warnings": ["source_completeness_unverified"]}
    identity = {name: deepcopy(payload[name]) for name in ("selection", "agent_id", "output_tokens", "budget")}
    identity["fixture_configuration_revision"] = revision
    return {"selection": deepcopy(selection), "manifest": manifest, "plan": plan,
            "agent_id": payload["agent_id"], "agent_name": "Fixture Agent A", "output_tokens": payload["output_tokens"],
            "budget": deepcopy(payload["budget"]), "preview_fingerprint": digest(identity), "can_create": True,
            "confirmation_requirements": {"order_confirmed": selection["source_kind"] == "series",
                "current_archive_only": True, "external_send_confirmed": True, "budget_confirmed": True},
            "targets": [{"provider_id": 3 + revision, "provider_name": "Synthetic service" if revision == 0 else "Changed synthetic service",
                         "provider_model_id": 11 + revision,
                         "model_key": "org/model " if revision == 0 else "new/model ", "display_name": "Literal model", "pool_id": None,
                         "pool_name": None, "pool_position": None, "fallback_depth": 0}],
            "warnings": ["source_completeness_unverified"]}


def coverage(manifest, complete=False):
    return {"validated_chars": len(BODY) if complete else 0, "available_chars": len(BODY),
            "ratio": 1.0 if complete else 0.0, "complete": complete,
            "missing_novel_ids": [102] if manifest["source_kind"] == "series" else [],
            "source_completeness": {"status": "partial" if manifest["source_kind"] == "series" else "unverified",
                "expected_count": manifest["expected_count"],
                "missing_count": 1 if manifest["source_kind"] == "series" else None,
                "local_chapters": len(manifest["chapters"]), "available_chapters": 1,
                "publication_state": manifest["publication_state"], "publication_source": manifest["publication_source"],
                "order_source": manifest["order_source"]}}


def evidence_document():
    evidence = {"evidence_id": EVIDENCE, "novel_id": 101, "text_hash": TEXT_HASH,
                "block_id": BLOCK, "start": START, "end": END, "quote": QUOTE}
    context_start = max(0, START - 128)
    return {"evidence": evidence, "jump": {"novel_id": 101, "text_hash": TEXT_HASH,
            "start": START, "end": END, "context_start": context_start,
            "context_text": BODY[context_start:min(len(BODY), END + 128)]}}


def report_document(run):
    artifact = {"schema_version": 1, "unit_id": UNIT, "is_final": True,
                "summary": "Synthetic report: " + LABEL, "no_findings_reason": None,
                "input_unit_ids": [], "claims": [{"claim_id": CLAIM, "dimension": "narrative",
                    "certainty": "interpretation", "label": "Synthetic label", "statement": LABEL,
                    "evidence_ids": [EVIDENCE]}],
                "evidence": {EVIDENCE: evidence_document()["evidence"]},
                "counterexamples": [{"statement": "A bounded counterexample", "evidence_ids": [EVIDENCE]}],
                "uncertainties": ["Source completeness remains unverified."]}
    return {"schema_version": 1, "run_id": RUN, "manifest": deepcopy(run["manifest"]),
            "coverage": coverage(run["manifest"], True), "artifact": artifact,
            "units": [{"unit_id": UNIT, "kind": "whole", "actual_target": {"provider_id": 3, "model_key": "org/model "},
                       "usage_summary": usage()}], "usage_summary": usage(),
            "created_at": "2026-01-01T00:00:00Z", "published_at": "2026-01-01T00:01:00Z",
            "report_stale": False, "archive_outdated": False, "synthesis_complete": True}


def shipped_csp():
    source = (ROOT / "src/pixiv_novel_sync/webapp.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    matches = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault"
               and node.args and isinstance(node.args[0], ast.Constant)
               and node.args[0].value == "Content-Security-Policy"]
    assert len(matches) == 1
    join = matches[0].args[1]
    assert isinstance(join, ast.Call) and isinstance(join.func, ast.Attribute) and join.func.attr == "join"
    assert isinstance(join.args[0], ast.Tuple)
    directives = []
    for item in join.args[0].elts:
        if isinstance(item, ast.Constant):
            directives.append(item.value)
        else:
            assert isinstance(item, ast.JoinedStr)
            parts = []
            for part in item.values:
                if isinstance(part, ast.Constant):
                    parts.append(part.value)
                else:
                    assert isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Call)
                    assert isinstance(part.value.func, ast.Name) and part.value.func.id == "_csp_nonce"
                    parts.append("test-nonce")
            directives.append("".join(parts))
    return "; ".join(directives)


class OfflineWorkUI(OfflineUI):
    def __init__(self, browser, local_vue, width):
        super().__init__(browser, local_vue, width)
        self.work_run = None
        self.work_report = None
        self.idempotency = {}
        self.fail_reads = set()
        self.lose_next = set()
        self.lose_before_apply = set()
        self.preview_revision = 0
        self.evidence_status = 200
        self.reader_hash = TEXT_HASH
        self.work_agents = [{"id": 7, "name": "Fixture Agent A", "enabled": True},
                            {"id": 8, "name": "Fixture Agent B", "enabled": True},
                            {"id": 9, "name": LABEL, "enabled": False}]
        self.csp = shipped_csp()
        self.cache_expression, cache_body = _ai_javascript_location()
        self.js_cache = re.search(r'add_header Cache-Control "([^"]+)" always;', cache_body).group(1)

    def seed_run(self, status="succeeded", payload=None):
        preview = preview_document(payload or request_draft(), self.preview_revision)
        self.work_run = {"run_id": RUN, "version": 1, "status": status, "reason": None,
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "completed_at": None, "deleted_at": None, "deleted": False,
            "source_kind": preview["manifest"]["source_kind"], "source_id": preview["manifest"]["source_id"],
            "title": preview["manifest"]["title"], "manifest": deepcopy(preview["manifest"]),
            "manifest_fingerprint": preview["manifest"]["manifest_fingerprint"],
            "budget": deepcopy(preview["budget"]), "request_count": 2, "planned_tokens": 8000,
            "active_seconds_debited": 5, "unit_counts": {"total": 1, "succeeded": int(status == "succeeded")},
            "coverage": coverage(preview["manifest"], status == "succeeded"),
            "usage_summary": usage(), "has_report": status == "succeeded",
            "can_resume": status in {"paused", "cancelled", "failed"},
            "report_stale": False, "archive_outdated": False}
        self.work_report = report_document(self.work_run) if status == "succeeded" else None
        return deepcopy(self.work_run)

    def transition(self, status, reason=None):
        self.work_run.update(status=status, reason=reason, version=self.work_run["version"] + 1,
                             can_resume=status in {"paused", "cancelled", "failed"})

    def render(self, path):
        parsed = urlsplit(path)
        names = {"/dashboard/preferences": "dashboard_preferences.html",
                 "/dashboard/novels/101": "dashboard_novel_detail.html",
                 "/dashboard/series/501": "dashboard_series_detail.html"}
        assert parsed.path in names
        with self.app.test_request_context(path):
            return render_template(names[parsed.path], task_labels={}, csp_nonce=lambda: "test-nonce",
                novel_id=101, series_id=501,
                work_analysis_view=parse_qs(parsed.query).get("view") == ["work-analysis"])

    def _json(self, route, data, status=200, envelope=True):
        route.fulfill(status=status, content_type="application/json",
            headers={"Cache-Control": "private, no-store, no-cache, max-age=0, must-revalidate"},
            body=json.dumps({"ok": True, "data": data} if envelope else data, ensure_ascii=False))

    def _error(self, route, code, status):
        return self._json(route, {"ok": False, "error": code, "code": code}, status, envelope=False)

    def _route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        record = {"method": request.method, "url": request.url, "path": parsed.path,
                  "query": parse_qs(parsed.query), "body": json.loads(request.post_data) if request.post_data else None,
                  "csrf": request.headers.get("x-csrf-token")}
        self.requests.append(record)
        if request.method == "GET" and request.url == VUE_URL:
            return route.fulfill(body=self.vue, content_type="application/javascript",
                                 headers={"Access-Control-Allow-Origin": "*"})
        if parsed.scheme + "://" + parsed.netloc != ORIGIN:
            self.unknown.append(record)
            return route.abort("blockedbyclient")
        if request.method == "GET" and parsed.path in {
                "/dashboard/preferences", "/dashboard/novels/101", "/dashboard/series/501"}:
            return route.fulfill(content_type="text/html", body=self.render(parsed.path + "?" + parsed.query),
                                 headers={"Content-Security-Policy": self.csp, "Cache-Control": "no-store"})
        static_names = {"/static/js/ai_settings.js", "/static/js/ai_picker.js", "/static/js/work_analysis.js",
                        "/static/css/tailwind.css"}
        if request.method == "GET" and parsed.path in static_names:
            assert not parsed.query or parsed.path == "/static/css/tailwind.css"
            target = (STATIC / parsed.path.removeprefix("/static/")).resolve()
            assert target.is_relative_to(STATIC.resolve()) and target.is_file()
            headers = {"Cache-Control": self.js_cache if self.cache_expression.fullmatch(parsed.path) else "no-cache"}
            return route.fulfill(body=target.read_bytes(), content_type=mimetypes.guess_type(target.name)[0], headers=headers)
        if request.method == "GET" and parsed.path == "/favicon.ico":
            return route.fulfill(status=204, body="")
        if parsed.path.startswith("/api/"):
            key = (request.method, parsed.path)
            if request.method != "GET" and key not in self.allowed_writes:
                self.unknown.append(record)
                return route.abort("blockedbyclient")
            if key in self.holds:
                self.pending[key].append((route, record))
                return None
            return self._api(route, record)
        self.unknown.append(record)
        return route.abort("blockedbyclient")

    def _write_result(self, route, record):
        key = (record["method"], record["path"])
        if key in self.lose_next:
            self.lose_next.remove(key)
            return route.abort("failed")
        return self._json(route, deepcopy(self.work_run))

    def _api(self, route, record):
        method, path, body, query = record["method"], record["path"], record["body"], record["query"]
        if method != "GET":
            assert record["csrf"] == CSRF
            assert query == {}
            if (method, path) in self.lose_before_apply:
                self.lose_before_apply.remove((method, path))
                return route.abort("failed")
        paged = path in {BASE, BASE + "/" + RUN + "/units", "/api/dashboard/recommendations/items"}
        if query and not paged:
            self.unknown.append(record)
            return route.abort("blockedbyclient")
        if method == "GET" and path in self.fail_reads:
            return self._error(route, "fixture_read_unavailable", 503)
        raw = {"/api/csrf-token": {"csrf_token": CSRF}, "/api/dashboard/shell-data": {"pending_count": 0},
               "/api/dashboard/status": {"current_user": None}, "/api/dashboard/auto-sync/status": {"running": False},
               "/api/dashboard/novels/101/progress": {"progress": 0}}
        if method == "GET" and path in raw:
            return self._json(route, raw[path], envelope=False)
        if method == "GET" and path == "/api/dashboard/preferences/profiles":
            return self._json(route, [])
        if method == "GET" and path == "/api/dashboard/recommendations/items":
            assert set(query) <= {"page", "page_size", "status"}
            return self._json(route, {"items": [], "page": 1, "page_size": 10, "total": 0, "total_pages": 1})
        if method == "GET" and path == "/api/dashboard/ai/agents":
            return self._json(route, deepcopy(self.work_agents))
        if method == "GET" and path == "/api/dashboard/novels/101":
            return self._json(route, {"novel_id": 101, "title": "Synthetic reader", "author_name": "Fixture author",
                "user_id": None, "series_id": None, "text_raw": BODY, "text_markdown": None,
                "text_hash": self.reader_hash, "text_length": len(BODY), "total_bookmarks": 0,
                "total_views": 0, "tags": [], "create_date": None, "rescue": None}, envelope=False)
        if method == "GET" and path == "/api/dashboard/series/501":
            return self._json(route, {"series_id": 501, "title": "Synthetic series", "user_id": None,
                "author_name": "Fixture author", "caption": "Synthetic", "total_novels": 2,
                "total_text_length": len(BODY), "rescue": None, "novels": [
                    {"novel_id": 101, "title": "Synthetic chapter", "text_length": len(BODY), "create_date": None},
                    {"novel_id": 102, "title": "Empty synthetic chapter", "text_length": 0, "create_date": None}]}, envelope=False)
        if method == "POST" and path == BASE + "/preview":
            assert set(body) == {"selection", "agent_id", "output_tokens", "budget", "confirmations"}
            return self._json(route, preview_document(body, self.preview_revision))
        if method == "POST" and path == BASE:
            assert set(body) == {"selection", "agent_id", "output_tokens", "budget", "confirmations",
                                 "preview_fingerprint", "idempotency_key"}
            key = body["idempotency_key"]
            if key in self.idempotency:
                if self.idempotency[key] != body:
                    return self._error(route, "idempotency_conflict", 409)
                return self._write_result(route, record)
            current = preview_document(body, self.preview_revision)
            if body["preview_fingerprint"] != current["preview_fingerprint"]:
                return self._error(route, "stale_preview", 409)
            assert all(body["confirmations"][name] is True for name, required in current["confirmation_requirements"].items() if required)
            self.idempotency[key] = deepcopy(body)
            self.seed_run("queued", body)
            self.work_run.update(request_count=0, planned_tokens=0, active_seconds_debited=0,
                usage_summary={"known_input_tokens": None, "known_output_tokens": None, "known_total_tokens": None,
                    "reported_requests": 0, "unreported_requests": 0, "network_request_count": 0, "complete": False})
            return self._write_result(route, record)
        if method == "GET" and path == BASE:
            assert set(query) <= {"offset", "limit"}
            items = [] if self.work_run is None or self.work_run["deleted"] else [deepcopy(self.work_run)]
            return self._json(route, {"items": items, "total": len(items), "offset": int(query.get("offset", [0])[0]),
                                      "limit": int(query.get("limit", [20])[0])})
        run_path = BASE + "/" + RUN
        if path == run_path and method == "GET":
            return self._json(route, deepcopy(self.work_run)) if self.work_run else self._error(route, "not_found", 404)
        if path in {run_path + suffix for suffix in ("/units", "/report", "/evidence/" + EVIDENCE)} and method == "GET":
            if not self.work_run or self.work_run["deleted"]:
                return self._error(route, "work_deleted", 410)
            if path.endswith("/units"):
                assert set(query) <= {"offset", "limit"}
                item = {"unit_id": UNIT, "kind": "whole", "is_final": True, "level": 0,
                        "status": "succeeded" if self.work_run["has_report"] else "running", "try_no": 1,
                        "last_error": None, "actual_target": {"provider_id": 3, "model_key": "org/model "},
                        "usage_summary": usage()}
                return self._json(route, {"items": [item], "total": 1, "offset": 0, "limit": 20})
            if not self.work_report:
                return self._error(route, "report_not_ready", 409)
            if path.endswith("/report"):
                return self._json(route, deepcopy(self.work_report))
            if self.evidence_status != 200:
                return self._error(route, "source_changed", self.evidence_status)
            return self._json(route, evidence_document())
        actions = {run_path + "/" + action: action for action in ("pause", "cancel", "resume", "budget")}
        if (method == "POST" and path in actions) or (method == "DELETE" and path == run_path):
            if body["expected_version"] != self.work_run["version"]:
                return self._error(route, "version_conflict", 409)
            action = actions.get(path, "delete")
            if action == "budget":
                assert body["budget_confirmed"] is True
                self.work_run["budget"] = deepcopy(body["budget"])
                self.work_run["version"] += 1
            elif action == "delete":
                self.transition("cancelled", "deleted")
                self.work_run.update(deleted=True, deleted_at="2026-01-01T00:02:00Z", has_report=False,
                    can_resume=False, title=None, manifest=None, manifest_fingerprint=None, source_id=None, source_kind=None)
                self.work_report = None
            else:
                status, reason = {"pause": ("pausing", "user_pause"), "cancel": ("cancelling", "user_cancel"),
                                  "resume": ("queued", None)}[action]
                if action == "pause" and self.work_run["status"] == "paused":
                    status = "paused"
                self.transition(status, reason)
            return self._write_result(route, record)
        self.unknown.append(record)
        return route.abort("blockedbyclient")

    def prepare_preview(self, kind="series"):
        self.goto("/dashboard/preferences?view=work-analysis&source_kind=" + kind + "&source_id=" + ("501" if kind == "series" else "101"))
        page = self.page
        page.locator("#wa-agent").click()
        dialog = page.locator("#wa-agent-dialog")
        self.assert_modal(dialog)
        dialog.get_by_role("option").filter(has_text="Fixture Agent A").click()
        for suffix, value in (("request", 16), ("token", 500000), ("time", 600)):
            page.locator("#wa-budget-" + suffix).fill(str(value))
        self.allow("POST", BASE + "/preview")
        page.locator("#wa-preview").click()
        self.expect(page.locator("#wa-create")).to_be_visible()
        return page

    def confirm_preview(self):
        for suffix in ("order", "archive", "send", "budget"):
            field = self.page.locator("#wa-confirm-" + suffix)
            if field.count():
                field.check()

    def capture(self, name):
        target = ROOT / ".superpowers/sdd/phase2-browser" / ("touch-" + str(self.width))
        target.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(target / (name + ".png")), full_page=True)
        (target / (name + "-requests.json")).write_text(json.dumps(self.requests, ensure_ascii=False, indent=2), encoding="utf-8")

    def assert_storage(self):
        for item in self.storage_writes:
            assert item["method"] == "setItem", item
            value = json.loads(item["args"][1])
            if item["kind"] == "local":
                assert item["args"][0] == "pixivNovelSync:reading:101", item
                assert set(value) == {"novelId", "seriesId", "title", "scrollY", "progress", "updatedAt"}
                assert value["novelId"] == 101 and value["title"] == "Synthetic reader"
            else:
                assert item["kind"] == "session" and item["args"][0] == "pixivNovelSync:listScroll:/dashboard/series/501"
                assert set(value) == {"scrollY"} and type(value["scrollY"]) in {int, float} and value["scrollY"] >= 0
        for origin in self.context.storage_state()["origins"]:
            assert origin["origin"] == ORIGIN
            assert all(item["name"] == "pixivNovelSync:reading:101" for item in origin["localStorage"])
        assert self.page.evaluate("Object.keys(sessionStorage).every(key => key === 'pixivNovelSync:listScroll:/dashboard/series/501')")
        recorded = json.dumps(self.storage_writes, ensure_ascii=False)
        assert BODY not in recorded and QUOTE not in recorded and LABEL not in recorded

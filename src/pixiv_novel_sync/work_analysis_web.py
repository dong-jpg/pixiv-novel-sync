from __future__ import annotations

from functools import wraps
import logging
import re

from flask import jsonify, request

from .web.request_limits import JSONBodyError, read_bounded_json_object
from .work_analysis.contracts import WorkAnalysisError
from .work_analysis.service import exact_object, strict_integer


BASE = "/api/dashboard/work-analyses"
BODY_LIMIT = 256 * 1024
logger = logging.getLogger(__name__)


def register_work_analysis_routes(app, service):
    app.extensions["pixiv_novel_sync.work_analysis_service"] = service

    def current():
        return app.extensions["pixiv_novel_sync.work_analysis_service"]

    @app.after_request
    def work_analysis_no_store(response):
        if request.path == BASE or request.path.startswith(BASE + "/"):
            response.headers["Cache-Control"] = "private, no-store, no-cache, max-age=0, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    def endpoint(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            try:
                return jsonify({"ok": True, "data": function(*args, **kwargs)})
            except WorkAnalysisError as exc:
                return jsonify({"ok": False, "error": exc.code, "code": exc.code}), exc.status
            except JSONBodyError as exc:
                return jsonify({"ok": False, "error": exc.code, "code": exc.code}), exc.status_code
            except Exception as exc:
                logger.warning("work analysis API unavailable (%s)", type(exc).__name__)
                return jsonify({"ok": False, "error": "work_analysis_unavailable",
                                "code": "work_analysis_unavailable"}), 500
        return guarded

    def query(allowed=()):
        if set(request.args) - set(allowed) or any(len(request.args.getlist(key)) != 1 for key in request.args):
            raise WorkAnalysisError("invalid_query")

    def page():
        query({"offset", "limit"})
        values = []
        for name, default, lower, upper in (("offset", "0", 0, 1000000), ("limit", "20", 1, 100)):
            raw = request.args.get(name, default)
            if not re.fullmatch(r"0|[1-9][0-9]{0,6}", raw):
                raise WorkAnalysisError("invalid_query")
            values.append(strict_integer(int(raw), lower, upper))
        return values

    def body():
        query()
        return read_bounded_json_object(request, limit_bytes=BODY_LIMIT, strict=True)

    def version():
        return strict_integer(exact_object(body(), {"expected_version"})["expected_version"], 1, 2**63 - 1)

    @app.post(BASE + "/preview")
    @endpoint
    def work_analysis_preview():
        return current().preview(body())

    @app.route(BASE, methods=["GET", "POST"])
    @app.route(BASE + "/", methods=["GET", "POST"])
    @endpoint
    def work_analysis_collection():
        if request.method == "POST":
            return current().create(body())
        offset, limit = page()
        return current().list_runs(offset, limit)

    @app.route(BASE + "/<run_id>", methods=["GET", "DELETE"])
    @endpoint
    def work_analysis_detail(run_id):
        if request.method == "DELETE":
            return current().delete(run_id, version())
        query()
        return current().detail(run_id)

    @app.post(BASE + "/<run_id>/pause")
    @app.post(BASE + "/<run_id>/cancel")
    @app.post(BASE + "/<run_id>/resume")
    @endpoint
    def work_analysis_control(run_id):
        action = request.path.rsplit("/", 1)[1]
        return current().control(run_id, action, version())

    @app.post(BASE + "/<run_id>/budget")
    @endpoint
    def work_analysis_budget(run_id):
        data = exact_object(body(), {"expected_version", "budget", "budget_confirmed"})
        if data["budget_confirmed"] is not True:
            raise WorkAnalysisError("confirmation_required")
        return current().amend_budget(run_id, data["budget"], data["expected_version"])

    @app.get(BASE + "/<run_id>/units")
    @endpoint
    def work_analysis_units(run_id):
        offset, limit = page()
        return current().units(run_id, offset, limit)

    @app.get(BASE + "/<run_id>/report")
    @endpoint
    def work_analysis_report(run_id):
        query()
        return current().report(run_id)

    @app.get(BASE + "/<run_id>/evidence/<evidence_id>")
    @endpoint
    def work_analysis_evidence(run_id, evidence_id):
        query()
        return current().evidence(run_id, evidence_id)

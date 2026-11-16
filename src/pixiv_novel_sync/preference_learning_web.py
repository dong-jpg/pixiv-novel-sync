"""Authenticated, bounded APIs for preference learning and archive analysis."""

from functools import wraps
import logging
import re

from flask import jsonify, request

from .web.request_limits import JSONBodyError, read_bounded_json_object
from .work_analysis.contracts import WorkAnalysisError
from .work_analysis.service import checked_id, exact_object, strict_integer


BASE = "/api/dashboard/preference-learning"
logger = logging.getLogger(__name__)


def register_preference_learning_routes(
    app, profiles, library, *, ensure_scheduler=None, profile_control=None, admission_available=None,
):
    app.extensions["pixiv_novel_sync.preference_learning"] = profiles
    app.extensions["pixiv_novel_sync.library_analysis"] = library

    @app.after_request
    def preference_learning_no_store(response):
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
                logger.warning("preference learning API unavailable (%s)", type(exc).__name__)
                return jsonify({"ok": False, "error": "preference_learning_unavailable",
                                "code": "preference_learning_unavailable"}), 500
        return guarded

    def query(allowed=()):
        if set(request.args) - set(allowed) or any(len(request.args.getlist(key)) != 1 for key in request.args):
            raise WorkAnalysisError("invalid_query")

    def body():
        query()
        return read_bounded_json_object(request, limit_bytes=256 * 1024, strict=True)

    def version():
        return strict_integer(exact_object(body(), {"expected_version"})["expected_version"], 1, 2**63 - 1)

    def page():
        query({"offset", "limit"})
        result = []
        for name, default, lower, upper in (("offset", "0", 0, 1000000), ("limit", "20", 1, 100)):
            raw = request.args.get(name, default)
            if re.fullmatch(r"0|[1-9][0-9]{0,6}", raw) is None:
                raise WorkAnalysisError("invalid_query")
            result.append(strict_integer(int(raw), lower, upper))
        return result

    def wake():
        if ensure_scheduler is not None:
            try:
                ensure_scheduler()
            except Exception as exc:
                # The intent has already committed. Never turn a failed wake
                # into an ambiguous create that encourages a second paid run.
                logger.warning("preference learning wake deferred (%s)", type(exc).__name__)

    def require_admission():
        if admission_available is not None and not admission_available():
            raise WorkAnalysisError("admission_unavailable", 503)

    @app.get(BASE + "/state")
    @endpoint
    def preference_learning_state():
        query()
        return {"profiles": profiles.state(), "library": library.state(),
                "admission_ready": admission_available is None or bool(admission_available())}

    @app.route(BASE + "/feedback/<kind>/<source_id>", methods=["GET", "PUT"])
    @endpoint
    def preference_learning_feedback(kind, source_id):
        if kind not in {"novel", "series"} or re.fullmatch(r"[1-9][0-9]{0,18}", source_id) is None:
            raise WorkAnalysisError("invalid_source")
        source_id = strict_integer(int(source_id), 1, 2**63 - 1)
        if request.method == "PUT":
            return profiles.feedback(kind, source_id, body())
        query()
        return profiles.feedback_state(kind, source_id)

    @app.post(BASE + "/profile-preview")
    @endpoint
    def preference_learning_profile_preview():
        return profiles.preview(body())

    @app.post(BASE + "/profiles")
    @endpoint
    def preference_learning_profile_create():
        require_admission()
        result = profiles.create(body())
        wake()
        return result

    @app.route(BASE + "/profiles/<run_id>", methods=["GET", "DELETE"])
    @endpoint
    def preference_learning_profile_detail(run_id):
        checked_id(run_id)
        if request.method == "DELETE":
            expected = version()
            if profile_control is not None:
                return profile_control(run_id, "delete", expected)
            return profiles.delete(run_id, expected)
        query()
        return profiles.detail(run_id)

    @app.post(BASE + "/profiles/<run_id>/activate")
    @endpoint
    def preference_learning_profile_activate(run_id):
        return profiles.activate(checked_id(run_id), version())

    @app.post(BASE + "/profiles/<run_id>/cancel")
    @endpoint
    def preference_learning_profile_cancel(run_id):
        checked_id(run_id)
        expected = version()
        if profile_control is not None:
            return profile_control(run_id, "cancel", expected)
        return profiles.cancel(run_id, expected)

    @app.post(BASE + "/batch-preview")
    @endpoint
    def preference_learning_batch_preview():
        return library.preview(body())

    @app.post(BASE + "/batches")
    @endpoint
    def preference_learning_batch_create():
        require_admission()
        result = library.create(body())
        wake()
        return result

    @app.get(BASE + "/batches/<batch_id>")
    @endpoint
    def preference_learning_batch_detail(batch_id):
        offset, limit = page()
        return library.batch(checked_id(batch_id), offset=offset, limit=limit)

    @app.post(BASE + "/batches/<batch_id>/pause")
    @app.post(BASE + "/batches/<batch_id>/resume")
    @app.post(BASE + "/batches/<batch_id>/cancel")
    @endpoint
    def preference_learning_batch_control(batch_id):
        if request.path.endswith("/resume"):
            require_admission()
        result = library.control(checked_id(batch_id), request.path.rsplit("/", 1)[1], version())
        wake()
        return result

    @app.put(BASE + "/automation")
    @endpoint
    def preference_learning_automation():
        payload = body()
        if payload.get("enabled") is True:
            require_admission()
        result = library.policy(payload)
        wake()
        return result

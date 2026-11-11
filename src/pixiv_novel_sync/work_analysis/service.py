from __future__ import annotations

from collections.abc import Mapping
import logging
import re

from ..ai.invocation import InvocationValidationError
from .configuration import snapshot_work_configuration
from .contracts import WorkAnalysisError, canonical_bytes, validate_budget
from .manifest import capture_manifest, preview_fingerprint, validate_selection
from .planning import plan_original_units


logger = logging.getLogger(__name__)
CONFIRMATIONS = frozenset({"order_confirmed", "current_archive_only",
                           "external_send_confirmed", "budget_confirmed"})
PREVIEW_FIELDS = frozenset({"selection", "agent_id", "output_tokens", "budget", "confirmations"})
TARGET_FIELDS = frozenset({"provider_id", "provider_name", "provider_model_id", "model_key",
                           "display_name", "pool_id", "pool_name", "pool_position", "fallback_depth"})


def exact_object(value, keys):
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise WorkAnalysisError("invalid_fields")
    return dict(value)


def strict_integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise WorkAnalysisError("invalid_integer")
    return value


def checked_id(value, length=32):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value) is None:
        raise WorkAnalysisError("invalid_id")
    return value


def normalized_request(payload, *, create=False):
    extra = {"preview_fingerprint", "idempotency_key"} if create else set()
    data = exact_object(payload, PREVIEW_FIELDS | extra)
    result = {
        "selection": validate_selection(data["selection"]),
        "agent_id": strict_integer(data["agent_id"], 1, 2**63 - 1),
        "output_tokens": strict_integer(data["output_tokens"], 1, 1000000),
        "budget": validate_budget(data["budget"]),
        "confirmations": exact_object(data["confirmations"], CONFIRMATIONS),
    }
    if any(type(value) is not bool for value in result["confirmations"].values()):
        raise WorkAnalysisError("invalid_confirmation")
    if create:
        result["preview_fingerprint"] = checked_id(data["preview_fingerprint"], 64)
        result["idempotency_key"] = checked_id(data["idempotency_key"])
    return result


class WorkAnalysisService:
    def __init__(self, db_factory, ai_factory, coordinator):
        self.db_factory = db_factory
        self.ai_factory = ai_factory
        self.coordinator = coordinator

    def _read(self, method, *args, **kwargs):
        db = self.db_factory()
        try:
            return getattr(db, method)(*args, **kwargs)
        finally:
            db.close()

    def _prepare(self, request):
        db = self.db_factory()
        try:
            with db.read_transaction() as conn:
                manifest, texts = capture_manifest(conn, request["selection"])
                configuration, agent_prompt, captured_preflight = snapshot_work_configuration(
                    conn, request["agent_id"], request["output_tokens"],
                )
                agent_name = db.get_ai_agent(request["agent_id"])["name"]
        except InvocationValidationError:
            raise WorkAnalysisError("configuration_unavailable") from None
        finally:
            db.close()

        # Only bounded in-memory text is retained, never a DB transaction or Provider.
        decisions = {}

        def preflight(payload):
            messages = payload.get("messages") if isinstance(payload, Mapping) else None
            if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
                raise WorkAnalysisError("input_too_large", 413)
            bounded = {**payload, "allowed_provider_ids": configuration["allowed_provider_ids"]}
            if len(canonical_bytes(bounded)) > 16 * 1024 * 1024:
                raise WorkAnalysisError("input_too_large", 413)
            try:
                result = captured_preflight(bounded)
            except InvocationValidationError:
                raise WorkAnalysisError("configuration_unavailable") from None
            rows = result.get("work_targets")
            if (not isinstance(rows, list) or not 1 <= len(rows) <= 64
                    or any(not isinstance(row, Mapping) or set(row) != TARGET_FIELDS for row in rows)):
                raise WorkAnalysisError("configuration_unavailable", 409)
            for row in rows:
                identity = (row["provider_id"], row["model_key"])
                decisions[identity] = {key: row[key] for key in TARGET_FIELDS}
            return result

        try:
            plan = plan_original_units(
                manifest, texts, preflight, agent_prompt=agent_prompt,
                output_tokens=request["output_tokens"],
            )
        finally:
            texts.clear()

        requirements = {
            "order_confirmed": manifest["source_kind"] == "series"
                               or "ordered_novel_ids" in request["selection"],
            "current_archive_only": manifest["requires_partial_confirmation"],
            "external_send_confirmed": True,
            "budget_confirmed": True,
        }
        enough = (request["budget"]["request_limit"] >= plan["minimum_requests"]
                  and request["budget"]["token_limit"] >=
                  plan["minimum_requests"] * request["output_tokens"])
        warnings = list(plan["warnings"])
        if not enough:
            warnings.append("budget_below_known_minimum")
        if configuration["legacy_provider_ids"]:
            warnings.append("legacy_repreview_required")
        preview = {
            "selection": request["selection"], "manifest": manifest, "plan": plan,
            "agent_id": request["agent_id"], "output_tokens": request["output_tokens"],
            "agent_name": agent_name, "targets": list(decisions.values()),
            "budget": request["budget"], "preview_fingerprint": preview_fingerprint(
                request["selection"], manifest, plan, configuration, request["budget"],
            ),
            "can_create": enough, "confirmation_requirements": requirements,
            "warnings": list(dict.fromkeys(warnings)),
        }
        return preview, configuration

    def preview(self, payload):
        preview, _configuration = self._prepare(normalized_request(payload))
        return preview

    def create(self, payload):
        request = normalized_request(payload, create=True)
        key = request["idempotency_key"]
        identity = {name: value for name, value in request.items() if name != "idempotency_key"}
        existing = self._read("lookup_work_analysis_idempotency", key, identity)
        if existing is not None:
            return existing
        preview, configuration = self._prepare(request)
        if preview["preview_fingerprint"] != request["preview_fingerprint"]:
            raise WorkAnalysisError("stale_preview", 409)
        if not preview["can_create"]:
            raise WorkAnalysisError("budget_below_known_minimum", 409)
        if any(required and not request["confirmations"][name]
               for name, required in preview["confirmation_requirements"].items()):
            raise WorkAnalysisError("confirmation_required")
        result = self._read(
            "create_work_analysis", request["selection"], preview["manifest"],
            preview["plan"], configuration, request["budget"], request["confirmations"],
            key, request["preview_fingerprint"],
        )
        try:
            self.coordinator.wake("created")
        except Exception as exc:
            # The parent intent has committed; a missed wake is not a failed create.
            logger.warning("work analysis admission wake deferred (%s)", type(exc).__name__)
        return result

    def list_runs(self, offset, limit):
        return self._read("list_work_analyses", strict_integer(offset, 0, 1000000),
                          strict_integer(limit, 1, 100))

    def detail(self, run_id):
        result = self._read("get_work_analysis", checked_id(run_id))
        if result is None:
            raise WorkAnalysisError("not_found", 404)
        return result

    def control(self, run_id, action, expected_version):
        checked_id(run_id)
        strict_integer(expected_version, 1, 2**63 - 1)
        if action not in {"pause", "cancel", "resume"}:
            raise WorkAnalysisError("invalid_action")
        return self.coordinator.request_control(run_id, action, expected_version)

    def amend_budget(self, run_id, budget, expected_version):
        return self._read("update_work_analysis_budget", checked_id(run_id),
                          validate_budget(budget), strict_integer(expected_version, 1, 2**63 - 1))

    def units(self, run_id, offset, limit):
        return self._read("list_work_analysis_units", checked_id(run_id),
                          strict_integer(offset, 0, 1000000), strict_integer(limit, 1, 100))

    def report(self, run_id):
        return self._read("get_work_analysis_report", checked_id(run_id))

    def evidence(self, run_id, evidence_id):
        return self._read("get_work_analysis_evidence", checked_id(run_id), checked_id(evidence_id, 64))

    def delete(self, run_id, expected_version):
        checked_id(run_id)
        strict_integer(expected_version, 1, 2**63 - 1)
        return self.coordinator.request_control(run_id, "delete", expected_version)

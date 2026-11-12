"""Opt-in library discovery, using the existing work-analysis execution chain."""
from __future__ import annotations

from contextlib import nullcontext
import logging
import threading

from ..work_analysis.configuration import snapshot_work_configuration
from ..work_analysis.contracts import WorkAnalysisError, fingerprint, validate_budget
from ..work_analysis.service import CONFIRMATIONS, WorkAnalysisService, checked_id, exact_object, strict_integer


logger = logging.getLogger(__name__)
FIELDS = frozenset({"scope", "agent_id", "output_tokens", "work_budget", "batch_budget", "work_limit",
                    "interval_minutes", "confirmations"})


def normalized_configuration(payload):
    value = exact_object(payload, FIELDS)
    if not isinstance(value["scope"], str) or value["scope"] not in {"all", "new"}:
        raise WorkAnalysisError("invalid_scope")
    confirmations = exact_object(value["confirmations"], CONFIRMATIONS)
    if any(type(item) is not bool for item in confirmations.values()):
        raise WorkAnalysisError("invalid_confirmation")
    total = exact_object(value["batch_budget"], {"request_limit", "token_limit", "active_seconds_limit"})
    total = {"request_limit": strict_integer(total["request_limit"], 1, 1_000_000),
             "token_limit": strict_integer(total["token_limit"], 1, 1_000_000_000_000),
             "active_seconds_limit": strict_integer(total["active_seconds_limit"], 30, 31_536_000)}
    work_budget = validate_budget(value["work_budget"])
    if any(total[key] < work_budget[key] for key in total):
        raise WorkAnalysisError("budget_below_work_limit")
    return {
        "scope": value["scope"], "agent_id": strict_integer(value["agent_id"], 1, 2**63 - 1),
        "output_tokens": strict_integer(value["output_tokens"], 1, 1_000_000),
        "work_budget": work_budget, "batch_budget": total,
        "work_limit": strict_integer(value["work_limit"], 1, 100_000),
        "interval_minutes": strict_integer(value["interval_minutes"], 1, 10_080),
        "confirmations": confirmations,
    }


def _preview_identity(configuration, config_fingerprint, revision, lower_revision):
    # Checking the consent boxes after inspecting a preview does not invalidate
    # it; changing scope, model, interval or either budget does.
    return fingerprint({"schema_version": 1, "configuration": {key: value for key, value in configuration.items()
                                                              if key != "confirmations"},
                        "configuration_fingerprint": config_fingerprint, "source_revision": revision,
                        "source_lower_revision": lower_revision})


class LibraryAnalysisService:
    def __init__(self, db_factory, ai_factory, coordinator):
        self.db_factory = db_factory
        self.ai_factory = ai_factory
        self.coordinator = coordinator
        self.work_service = WorkAnalysisService(db_factory, ai_factory, coordinator)
        self._tick_lock = threading.Lock()

    def _db(self, method, *args, **kwargs):
        db = self.db_factory()
        try:
            return getattr(db, method)(*args, **kwargs)
        finally:
            db.close()

    def state(self):
        return self._db("get_library_analysis_state")

    def enabled(self):
        return self.state()["policy"]["enabled"]

    def _preview(self, configuration, *, purpose="manual"):
        db = self.db_factory()
        try:
            with db.read_transaction():
                snapshot, prompt, preflight = snapshot_work_configuration(
                    db.conn, configuration["agent_id"], configuration["output_tokens"],
                )
                lower_revision = 0 if configuration["scope"] == "new" else -1
                if purpose == "policy" and configuration["scope"] == "new":
                    lower_revision = db.conn.execute("SELECT new_baseline_revision FROM library_analysis_policy WHERE id=1").fetchone()[0]
                    if lower_revision is None:
                        lower_revision = db.conn.execute("SELECT revision FROM library_analysis_clock WHERE id=1").fetchone()[0]
                inventory = db.library_analysis_inventory(configuration["scope"], lower_revision=lower_revision)
        finally:
            db.close()
        preflight_result = preflight({
            "messages": [{"role": "system", "content": prompt},
                         {"role": "user", "content": "Plan analysis of an explicitly authorized archived work."}],
            "max_tokens": configuration["output_tokens"],
        })
        targets = [dict(row) for row in preflight_result.get("work_targets", ())]
        can_create = bool(preflight_result.get("can_run")) and bool(targets) and not snapshot["legacy_provider_ids"]
        warnings = ["current_archive_only", "allocated_limits_are_not_refunded", "interrupted_calls_require_explicit_resume"]
        if snapshot["legacy_provider_ids"]:
            warnings.append("legacy_repreview_required")
        if configuration["scope"] == "new":
            warnings.append("new_since_policy_first_enabled" if purpose == "policy" else "new_since_library_tracking_started")
        return {
            "preview_fingerprint": _preview_identity(configuration, snapshot["configuration_fingerprint"], inventory["revision"], lower_revision),
            "configuration": configuration, "targets": targets, "candidate_count": inventory["candidate_count"],
            "samples": inventory["samples"], "can_create": can_create, "warnings": warnings,
            "budget_mode": "cumulative_reserved_limits", "purpose": purpose,
        }, snapshot["configuration_fingerprint"], inventory["revision"]

    def preview(self, payload):
        if not isinstance(payload, dict):
            raise WorkAnalysisError("invalid_fields")
        purpose = payload.get("purpose", "manual")
        if not isinstance(purpose, str) or purpose not in {"manual", "policy"}:
            raise WorkAnalysisError("invalid_fields")
        return self._preview(normalized_configuration({key: value for key, value in payload.items() if key != "purpose"}), purpose=purpose)[0]

    def _confirmed(self, configuration, expected_fingerprint, *, purpose="manual"):
        checked_id(expected_fingerprint, 64)
        if not all(configuration["confirmations"].values()):
            raise WorkAnalysisError("confirmation_required")
        preview, config_fingerprint, revision = self._preview(configuration, purpose=purpose)
        if preview["preview_fingerprint"] != expected_fingerprint:
            raise WorkAnalysisError("stale_preview", 409)
        if not preview["can_create"]:
            raise WorkAnalysisError("configuration_unavailable", 409)
        return config_fingerprint, revision

    def _wake(self):
        try:
            self.coordinator.wake("created")
        except Exception as error:
            # Persisted intent is successful even when a notification is lost.
            logger.warning("Library analysis wake deferred (%s)", type(error).__name__)

    def create(self, payload):
        value = exact_object(payload, FIELDS | {"preview_fingerprint", "idempotency_key"})
        key = checked_id(value["idempotency_key"])
        configuration = normalized_configuration({name: value[name] for name in FIELDS})
        identity = fingerprint({name: item for name, item in value.items() if name != "idempotency_key"})
        existing = self._db("lookup_library_analysis_batch", key, identity)
        if existing is not None:
            return existing
        config_fingerprint, revision = self._confirmed(configuration, value["preview_fingerprint"])
        result = self._db("create_library_analysis_batch", configuration, config_fingerprint, revision=revision,
                          idempotency_key=key, request_identity=identity)
        self._wake()
        return result

    def policy(self, payload):
        if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
            raise WorkAnalysisError("invalid_fields")
        if payload["enabled"]:
            value = exact_object(payload, FIELDS | {"enabled", "expected_version", "preview_fingerprint"})
            configuration = normalized_configuration({name: value[name] for name in FIELDS})
            config_fingerprint, revision = self._confirmed(configuration, value["preview_fingerprint"], purpose="policy")
        else:
            value = exact_object(payload, {"enabled", "expected_version"})
            configuration = config_fingerprint = revision = None
        scope = getattr(self.coordinator, "work_control_scope", nullcontext)
        with scope():
            result = self._db("update_library_analysis_policy", value["enabled"],
                              strict_integer(value["expected_version"], 1, 2**63 - 1), configuration,
                              config_fingerprint=config_fingerprint, revision=revision)
            self._synchronize_holds()
        if result["enabled"]:
            self._wake()
        return result

    def batch(self, batch_id, offset=0, limit=50):
        return self._db("get_library_analysis_batch", checked_id(batch_id),
                        strict_integer(offset, 0, 1_000_000), strict_integer(limit, 1, 100))

    def control(self, batch_id, action, expected_version):
        scope = getattr(self.coordinator, "work_control_scope", nullcontext)
        with scope():
            result = self._db("control_library_analysis_batch", checked_id(batch_id), action,
                              strict_integer(expected_version, 1, 2**63 - 1))
            self._synchronize_holds(batch_id)
        if action == "resume":
            self._wake()
        return result

    def _synchronize_holds(self, batch_id=None):
        scope = getattr(self.coordinator, "work_control_scope", nullcontext)
        with scope():
            for item in self._db("library_analysis_held_work", batch_id):
                self._synchronize_hold(item)

    def _clear_hold(self, item):
        if not self._db("clear_library_analysis_hold", item["target_id"], expected_batch_version=item["batch_version"]):
            raise WorkAnalysisError("control_unavailable", 503)

    def _synchronize_hold(self, initial):
        # Client CAS has already committed the batch/policy intent. Only its
        # internal work-progress version may be refreshed here; never silently
        # reinterpret a newer owner, source, or batch control command.
        identity_keys = ("run_id", "epoch", "owner_token", "manifest_fingerprint", "configuration_fingerprint")
        initial_identity = tuple(initial[key] for key in identity_keys)
        item = initial
        for attempt in range(4):
            if item is None or not item["held"]:
                return
            if item["run_id"] is None or item["deleted_at"] is not None or item["status"] in {"succeeded", "failed", "invalidated"}:
                self._clear_hold(item)
                return
            if item["batch_version"] != initial["batch_version"] or item["batch_status"] != initial["batch_status"]:
                raise WorkAnalysisError("control_unavailable", 503)
            stopped = item["batch_status"] in {"paused", "cancelled", "failed"}
            action = "cancel" if item["batch_status"] == "cancelled" else "pause"
            stopped_states = {"cancelled", "cancelling"} if action == "cancel" else {"paused", "pausing", "cancelled", "cancelling"}
            if stopped and item["status"] in stopped_states and item["reason"] != "sync_yield":
                return
            if not stopped:
                # This is a persisted, explicit batch-resume command. The
                # original worker must finish draining before it can resume.
                if item["status"] in {"pausing", "cancelling"}:
                    return
                if item["status"] not in {"paused", "cancelled"}:
                    self._clear_hold(item)
                    return
                action = "resume"
            if tuple(item[key] for key in identity_keys) != initial_identity:
                raise WorkAnalysisError("control_unavailable", 503)
            if attempt == 3:
                break
            try:
                self.coordinator.request_control(item["run_id"], action, item["version"])
            except WorkAnalysisError as error:
                if error.code == "finalizing" and stopped:
                    # No more model sends occur after finalization. Preserve the
                    # existing shared permission to finish saving this result.
                    return
                if error.code not in {"version_conflict", "state_conflict", "work_deleted", "invalidated"}:
                    raise
                item = self._db("get_library_analysis_hold", initial["target_id"])
            else:
                if action == "resume":
                    self._clear_hold(item)
                return
        # The durable held flag remains authoritative at each send boundary.
        # A failed coordinator handshake must not be returned as a successful
        # pause/disable acknowledgment or silently requeue paid work.
        raise WorkAnalysisError("control_unavailable", 503)

    def startup_recover(self):
        scope = getattr(self.coordinator, "work_control_scope", nullcontext)
        with scope():
            if getattr(self.coordinator, "_library_startup_recovered", False):
                return 0
            result = self._db("recover_library_analyses")
            self.coordinator._library_startup_recovered = True
            return result

    def tick(self, reason=None):
        """Prepare at most one work, with four bounded metadata-only skips.

        Called by the existing owner, outside its admission lock. Model/network
        execution only starts later via the ordinary shared work-analysis runner.
        """
        if not self._tick_lock.acquire(blocking=False):
            return False
        try:
            self._synchronize_holds()
            due = self._db("library_analysis_due_policy")
            if due is not None:
                try:
                    self._db("create_library_analysis_batch", due["configuration"], due["configuration_fingerprint"],
                             revision=due["revision"], policy_version=due["version"])
                except WorkAnalysisError as error:
                    if error.code in {"configuration_changed", "configuration_unavailable"}:
                        self._db("fail_library_analysis_policy", error.code)
                    elif error.code not in {"version_conflict", "stale_preview"}:
                        raise
            for _ in range(4):
                item = self._db("next_library_analysis_target")
                if item is None:
                    return False
                try:
                    if self._db("commit_library_analysis_target", item):
                        continue
                    configuration = item["configuration"]
                    try:
                        prepared = self.work_service._prepare({
                            "selection": item["selection"], "agent_id": configuration["agent_id"],
                            "output_tokens": configuration["output_tokens"], "budget": configuration["work_budget"],
                            "confirmations": configuration["confirmations"],
                        })
                        if not prepared[0]["can_create"]:
                            raise WorkAnalysisError("budget_below_known_minimum", 409)
                    except WorkAnalysisError as error:
                        self._db("commit_library_analysis_target", item, error=error.code)
                    else:
                        self._db("commit_library_analysis_target", item, prepared=prepared)
                    return True
                except WorkAnalysisError as error:
                    if error.code in {"configuration_changed", "configuration_unavailable"}:
                        self._db("stop_library_analysis_discovery", item["batch_id"], error.code)
                    elif error.code not in {"version_conflict", "source_changed", "policy_disabled"}:
                        raise
                    return False
            return True
        finally:
            self._tick_lock.release()

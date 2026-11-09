from __future__ import annotations

import time
from contextlib import nullcontext
from datetime import datetime, timezone

from .artifacts import validate_artifact
from .configuration import snapshot_work_configuration
from .contracts import WorkAnalysisError
from .evidence import validate_unit_output
from .manifest import read_source_texts
from .planning import materialize_unit, plan_reduction_level


def _utc_now():
    return datetime.now(timezone.utc)


class _Pulse:
    def __init__(self, db_factory, lease, context, slice_deadline_at):
        self.db_factory = db_factory
        self.lease = lease
        self.context = context
        self.slice_deadline_at = slice_deadline_at
        self.next_check = 0.0
        self.stopped = False

    def check(self):
        if self.context["manager"].is_cancel_requested(self.context["job_id"]):
            self.stopped = True
        if _utc_now().strftime("%Y-%m-%d %H:%M:%S") >= self.slice_deadline_at:
            self.stopped = True
        if self.stopped or time.monotonic() < self.next_check:
            return self.stopped
        db = self.db_factory()
        try:
            state = db.get_work_analysis(self.lease.run_id)
            self.stopped = state is None or state["status"] != "running"
            if not self.stopped:
                self.stopped = not db.heartbeat_work_analysis(self.lease, now=_utc_now())
        finally:
            db.close()
        self.next_check = time.monotonic() + 1.0
        return self.stopped

    def require_running(self):
        if self.check():
            raise WorkAnalysisError("work_stopping", 409)


class WorkAnalysisExecutor:
    def __init__(self, db_factory, ai_factory):
        self.db_factory = db_factory
        self.ai_factory = ai_factory

    def _private(self, run_id):
        db = self.db_factory()
        try:
            state = db.get_work_analysis(run_id, include_private=True)
            if state is None:
                raise WorkAnalysisError("work_deleted", 410)
            return state
        finally:
            db.close()

    def _configuration(self, state):
        db = self.db_factory()
        try:
            with db.read_transaction():
                try:
                    current, prompt, preflight = snapshot_work_configuration(
                        db.conn, state["configuration"]["agent_id"], state["configuration"]["output_tokens"],
                    )
                except WorkAnalysisError:
                    raise WorkAnalysisError("configuration_changed", 409) from None
                if current["configuration_fingerprint"] != state["configuration"]["configuration_fingerprint"]:
                    raise WorkAnalysisError("configuration_changed", 409)
                return current, prompt, preflight
        finally:
            db.close()

    def _run_unit(self, lease, state, entry, pulse):
        unit = entry["unit"]
        configuration, prompt, preflight = self._configuration(state)
        by_id = {item["unit"]["unit_id"]: item for item in state["units"]}
        children = {}
        for child_id in unit["child_unit_ids"]:
            child = by_id.get(child_id)
            if child is None or child["status"] != "succeeded" or child["artifact"] is None:
                raise WorkAnalysisError("graph_invalid", 409)
            children[child_id] = validate_artifact(child["artifact"], final=child["unit"]["is_final"])
        db = self.db_factory()
        try:
            with db.read_transaction():
                texts = read_source_texts(db.conn, state["manifest"])
        finally:
            db.close()
        try:
            payload, block_map = materialize_unit(
                state["manifest"], unit, texts, children,
                agent_prompt=prompt, output_tokens=configuration["output_tokens"],
            )
            payload["allowed_provider_ids"] = list(configuration["allowed_provider_ids"])
            if not preflight(payload)["can_run"]:
                raise WorkAnalysisError("synthesis_capacity_exceeded" if unit["kind"] == "reduce" else "configuration_changed", 409)
            pulse.require_running()
            db = self.db_factory()
            try:
                binding = db.begin_work_analysis_unit(lease, unit["unit_id"])
            finally:
                db.close()
            result = self.ai_factory().invoke_text(
                configuration["agent_id"], payload, work_binding=binding,
                stop_requested=pulse.check, deadline_at=state["slice_deadline_at"],
            )
            if result.work_error_code is not None:
                raise WorkAnalysisError(result.work_error_code, 409)
            pulse.require_running()
            if result.finish_state != "succeeded" or result.job_status != "succeeded":
                raise WorkAnalysisError("transport_failed", 409)
            try:
                artifact = validate_unit_output(result.output_text, unit, block_map, children)
            except (TypeError, ValueError):
                raise WorkAnalysisError("validation_failed", 409) from None
            pulse.require_running()
            db = self.db_factory()
            try:
                db.save_work_analysis_unit(
                    lease, unit["unit_id"], artifact,
                    actual_target={"provider_id": result.actual_provider_id, "model_key": result.actual_model_key},
                )
            finally:
                db.close()
        finally:
            texts.clear()

    def _derive_next_level(self, lease, state, frontier):
        configuration, prompt, preflight = self._configuration(state)
        children = {item["unit"]["unit_id"]: item["artifact"] for item in frontier}
        level = max(item["unit"]["level"] for item in frontier) + 1
        try:
            units = plan_reduction_level(
                lease.run_id, level, [item["unit"] for item in frontier], children, preflight,
                agent_prompt=prompt, output_tokens=configuration["output_tokens"],
            )
        except (TypeError, ValueError):
            raise WorkAnalysisError("synthesis_capacity_exceeded", 409) from None
        if not units or len(state["units"]) + len(units) > 512 or level > 8:
            raise WorkAnalysisError("synthesis_capacity_exceeded", 409)
        db = self.db_factory()
        try:
            db.add_work_analysis_units(lease, units)
        finally:
            db.close()

    def _classify_stop(self, run_id, error, slice_deadline_at):
        if error.code in {"source_changed", "source_deleted", "configuration_changed", "legacy_repreview_required", "invalidated"}:
            return "invalidated", error.code
        if error.code in {"budget_exhausted", "active_time_exhausted"}:
            return "paused", "budget_exhausted"
        if error.code == "synthesis_capacity_exceeded":
            return "paused", "synthesis_capacity_exceeded"
        if error.code in {"work_stopping", "work_deleted", "owner_lost", "lease_expired"}:
            db = self.db_factory()
            try:
                state = db.get_work_analysis(run_id)
            finally:
                db.close()
            if state is None:
                return "cancelled", "work_deleted"
            if state["status"] == "invalidated":
                return "invalidated", state.get("reason") or "invalidated"
            if state["status"] in {"cancelling", "cancelled"}:
                return "cancelled", state.get("reason") or "user_cancel"
            if state["status"] in {"pausing", "paused"}:
                return "paused", state.get("reason") or "user_pause"
            if _utc_now().strftime("%Y-%m-%d %H:%M:%S") >= slice_deadline_at:
                return "paused", "budget_exhausted"
            return "cancelled", "user_cancel"
        return "failed", error.code if error.code in {"validation_failed", "transport_failed", "graph_invalid"} else "execution_failed"

    def run(self, run_id, context):
        job = context.get("job")
        if (job is None or job.spec.source.value != "web" or job.spec.job_type.value != "work_analysis"
                or context.get("job_id") != job.job_id
                or context.get("params") != {"run_id": run_id}
                or not callable(context.get("claim_finalization"))):
            raise RuntimeError("work_analysis_requires_shared_web_runner")
        control_scope = context.get("work_control_scope", nullcontext)
        with control_scope():
            if context["manager"].is_cancel_requested(context["job_id"]):
                raise InterruptedError("work_stopping")
            db = self.db_factory()
            try:
                lease = db.claim_work_analysis(
                    run_id, now=_utc_now(), slice_seconds=1800,
                    shared_job_id=context["job_id"], shared_log_id=job.progress.get("log_id"),
                )
            finally:
                db.close()
        started = time.monotonic()
        outcome, reason = "failed", "execution_failed"
        slice_deadline_at = "9999-12-31 23:59:59"
        try:
            state = self._private(run_id)
            slice_deadline_at = state["slice_deadline_at"]
            pulse = _Pulse(self.db_factory, lease, context, slice_deadline_at)
            for graph_step in range(1025):
                pulse.require_running()
                state = self._private(run_id)
                self._configuration(state)
                entries = state["units"]
                originals = [item for item in entries if item["unit"]["kind"] in {"whole", "extract"}]
                expected = {unit["unit_id"] for unit in state["plan"]["original_units"]}
                if {item["unit"]["unit_id"] for item in originals} != expected or not originals:
                    raise WorkAnalysisError("graph_invalid", 409)
                original_pending = [item for item in originals if item["status"] != "succeeded"]
                if original_pending:
                    self._run_unit(lease, state, original_pending[0], pulse)
                    continue
                highest = max(item["unit"]["level"] for item in entries)
                frontier = [item for item in entries if item["unit"]["level"] == highest]
                pending = [item for item in frontier if item["status"] != "succeeded"]
                if pending:
                    self._run_unit(lease, state, pending[0], pulse)
                    continue
                if len(frontier) == 1 and frontier[0]["unit"]["is_final"]:
                    artifact = validate_artifact(frontier[0]["artifact"], final=True)
                    pulse.require_running()
                    if not context["claim_finalization"]():
                        raise WorkAnalysisError("work_stopping", 409)
                    db = self.db_factory()
                    try:
                        db.publish_work_analysis(lease, artifact)
                    finally:
                        db.close()
                    outcome, reason = "succeeded", None
                    return {"work_analysis": {"run_id": run_id, "status": "succeeded", "validated_units": len(entries)}}
                self._derive_next_level(lease, state, frontier)
            raise WorkAnalysisError("synthesis_capacity_exceeded", 409)
        except WorkAnalysisError as error:
            # A cancellation latch can precede its durable CAS/retry. Wait
            # for that short control operation before interpreting the stop.
            with control_scope():
                outcome, reason = self._classify_stop(run_id, error, slice_deadline_at)
            context["manager"].update_progress(context["job_id"], work_status=outcome, work_reason=reason)
            if outcome in {"paused", "cancelled", "invalidated"}:
                raise InterruptedError(reason) from None
            raise RuntimeError(reason) from None
        except Exception:
            outcome, reason = "failed", "execution_failed"
            raise RuntimeError("work_execution_failed") from None
        finally:
            db = self.db_factory()
            try:
                try:
                    db.settle_work_analysis(
                        lease, outcome=outcome, reason=reason,
                        elapsed_seconds=max(0.0, time.monotonic() - started),
                    )
                except WorkAnalysisError as error:
                    if error.code != "owner_lost":
                        raise RuntimeError("work_settlement_failed") from None
                except Exception:
                    raise RuntimeError("work_settlement_failed") from None
            finally:
                db.close()

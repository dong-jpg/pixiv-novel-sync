from __future__ import annotations

import math
import secrets
from datetime import timedelta

from ...work_analysis.contracts import WorkAnalysisError, WorkLease, validate_budget
from . import common as c
from .schema import invalidate_sql, tombstone_sql


def _finish_owner(conn, row, *, status, reason, now, refund=0.0, invalidate=False):
    conn.execute(
        "UPDATE work_analysis_runs SET status=?,reason=?,active_seconds_debited=MAX(0,active_seconds_debited-?),"
        "epoch=epoch+1,owner_token=NULL,heartbeat_at=NULL,lease_until=NULL,"
        "slice_deadline_at=NULL,slice_seconds=0,slice_settled=1,"
        "invalidated_at=CASE WHEN ? THEN COALESCE(invalidated_at,?) ELSE invalidated_at END,"
        "completed_at=CASE WHEN ? IN ('succeeded','cancelled','failed','invalidated') "
        "THEN COALESCE(completed_at,?) ELSE NULL END,version=version+1,updated_at=? WHERE run_id=?",
        (status, reason, refund, int(invalidate), now, status, now, now, row["run_id"]),
    )
    conn.execute(
        "UPDATE work_analysis_units SET status='pending',owner_epoch=NULL,last_error=? "
        "WHERE run_id=? AND status='running'", (reason, row["run_id"]),
    )


def _budget_available(conn, row):
    root_ready = conn.execute(
        "SELECT 1 FROM work_analysis_units WHERE run_id=? AND is_final=1 AND status='succeeded' LIMIT 1",
        (row["run_id"],),
    ).fetchone() is not None
    return (int(row["active_seconds_limit"] - row["active_seconds_debited"]) >= 1
            and (root_ready or (row["request_count"] < row["request_limit"]
                                and row["planned_tokens"] < row["token_limit"])))


class WorkAnalysisControlMixin:
    def claim_work_analysis(self, run_id, *, now, slice_seconds, shared_job_id=None, shared_log_id=None):
        c.integer(slice_seconds, 1, 1800)
        if shared_job_id is not None:
            c.hex_id(shared_job_id, 32)
        if shared_log_id is not None:
            c.integer(shared_log_id, 1, 2**63 - 1)
            if shared_job_id is None:
                raise WorkAnalysisError("invalid_request")
        now_text = c.stamp(now)
        failure = None
        with self.transaction() as conn:
            row = c.get_row(conn, run_id)
            c.assert_not_deleted(row)
            if (row["owner_token"] is not None or not (row["status"] == "queued"
                    or (row["status"] == "paused" and row["reason"] == "sync_yield"))):
                raise WorkAnalysisError("state_conflict", 409)
            if shared_job_id is not None:
                if row["shared_job_id"] == shared_job_id or conn.execute(
                    "SELECT 1 FROM work_analysis_runs WHERE run_id<>? AND "
                    "(shared_job_id=? OR (? IS NOT NULL AND shared_log_id=?))",
                    (run_id, shared_job_id, shared_log_id, shared_log_id),
                ).fetchone() is not None:
                    raise WorkAnalysisError("state_conflict", 409)
            if shared_log_id is not None and conn.execute(
                "SELECT 1 FROM task_logs WHERE id=? AND job_id=? AND task_type='work_analysis' AND status='running'",
                (shared_log_id, shared_job_id),
            ).fetchone() is None:
                raise WorkAnalysisError("state_conflict", 409)
            try:
                c.assert_identity(conn, row)
            except WorkAnalysisError as error:
                if error.code not in {"source_changed", "configuration_changed", "invalidated"}:
                    raise
                _finish_owner(conn, row, status="invalidated", reason=error.code, now=now_text, invalidate=True)
                failure = error
            if failure is None and not _budget_available(conn, row):
                _finish_owner(conn, row, status="paused", reason="budget_exhausted", now=now_text)
                failure = WorkAnalysisError("budget_exhausted", 409)
            if failure is None:
                seconds = min(slice_seconds, int(row["active_seconds_limit"] - row["active_seconds_debited"]))
                token = secrets.token_hex(32)
                epoch = row["epoch"] + 1
                conn.execute(
                    "UPDATE work_analysis_runs SET status='running',reason=NULL,epoch=?,owner_token=?,"
                    "shared_job_id=?,shared_log_id=?,shared_epoch=?,"
                    "heartbeat_at=?,lease_until=?,slice_deadline_at=?,slice_seconds=?,slice_settled=0,"
                    "active_seconds_debited=active_seconds_debited+?,version=version+1,updated_at=?,completed_at=NULL "
                    "WHERE run_id=?",
                    (epoch, token, shared_job_id, shared_log_id, epoch if shared_job_id is not None else None,
                     now_text, c.stamp(now + timedelta(seconds=45)),
                     c.stamp(now + timedelta(seconds=seconds)), seconds, seconds, now_text, run_id),
                )
                lease = WorkLease(run_id, epoch, token)
        # Preserve invalidation or budget exhaustion even when claim is rejected.
        if failure is not None:
            raise failure
        return lease

    def heartbeat_work_analysis(self, lease, *, now):
        with self.transaction() as conn:
            try:
                row = c.require_owner(conn, lease, now=now, states=("running", "pausing", "cancelling", "succeeded"),
                                      deadline=False, identity=False)
            except WorkAnalysisError:
                return False
            conn.execute(
                "UPDATE work_analysis_runs SET heartbeat_at=?,lease_until=? WHERE run_id=?",
                (c.stamp(now), c.stamp(now + timedelta(seconds=45)), row["run_id"]),
            )
            return True

    def request_work_analysis_stop(self, run_id, action, expected_version):
        if action not in {"pause", "cancel", "sync_yield", "delete"}:
            raise WorkAnalysisError("invalid_request")
        c.integer(expected_version, 1, 2**63 - 1)
        with self.transaction() as conn:
            row = c.get_row(conn, run_id)
            if action == "delete" and row["deleted_at"] is not None:
                return self._work_detail(conn, row)
            c.assert_not_deleted(row)
            c.assert_version(row, expected_version)
            if action == "delete":
                conn.execute(tombstone_sql("SELECT ?", source_deleted=False), (run_id,))
                return self._work_detail(conn, c.get_row(conn, run_id))
            if row["status"] in {"succeeded", "invalidated", "cancelled"}:
                raise WorkAnalysisError("state_conflict", 409)
            if row["status"] == "cancelling" and action != "cancel":
                raise WorkAnalysisError("state_conflict", 409)
            reason = {"pause": "user_pause", "cancel": "user_cancel", "sync_yield": "sync_yield"}[action]
            if action == "sync_yield" and row["reason"] == "user_pause":
                raise WorkAnalysisError("state_conflict", 409)
            active = row["owner_token"] is not None
            status = ("cancelling" if action == "cancel" else "pausing") if active else (
                "cancelled" if action == "cancel" else "paused")
            if row["status"] != status or row["reason"] != reason:
                conn.execute(
                    "UPDATE work_analysis_runs SET status=?,reason=?,version=version+1,updated_at=? WHERE run_id=?",
                    (status, reason, c.stamp(), run_id),
                )
            return self._work_detail(conn, c.get_row(conn, run_id))

    def resume_work_analysis(self, run_id, expected_version):
        failure = None
        with self.transaction() as conn:
            row = c.get_row(conn, run_id)
            c.assert_not_deleted(row)
            c.assert_version(row, expected_version)
            if row["status"] not in {"paused", "cancelled", "failed"} or row["owner_token"] is not None:
                raise WorkAnalysisError("state_conflict", 409)
            try:
                c.assert_identity(conn, row)
            except WorkAnalysisError as error:
                if error.code not in {"source_changed", "configuration_changed", "invalidated"}:
                    raise
                _finish_owner(conn, row, status="invalidated", reason=error.code, now=c.stamp(), invalidate=True)
                failure = error
            if failure is None:
                if not _budget_available(conn, row):
                    raise WorkAnalysisError("budget_exhausted", 409)
                conn.execute(
                    "UPDATE work_analysis_runs SET status='queued',reason=NULL,version=version+1,"
                    "updated_at=?,completed_at=NULL WHERE run_id=?", (c.stamp(), run_id),
                )
                result = self._work_detail(conn, c.get_row(conn, run_id))
        # A changed identity is terminal, not a rollback of the failed resume.
        if failure is not None:
            raise failure
        return result

    def update_work_analysis_budget(self, run_id, budget, expected_version):
        budget = validate_budget(budget)
        with self.transaction() as conn:
            row = c.get_row(conn, run_id)
            c.assert_not_deleted(row)
            c.assert_version(row, expected_version)
            if row["invalidated_at"] is not None or row["status"] in {"succeeded", "invalidated"}:
                raise WorkAnalysisError("state_conflict", 409)
            previous = c.budget_from_row(row)
            if any(budget[key] < previous[key] for key in previous):
                raise WorkAnalysisError("invalid_request")
            if budget == previous:
                return self._work_detail(conn, row)
            history = c.load(row["budget_history_json"])
            history.append({"old": previous, "new": budget, "confirmed": True, "recorded_at": c.stamp(),
                            "request_count": row["request_count"], "planned_tokens": row["planned_tokens"],
                            "active_seconds_debited": row["active_seconds_debited"]})
            conn.execute(
                "UPDATE work_analysis_runs SET request_limit=?,token_limit=?,active_seconds_limit=?,"
                "budget_history_json=?,version=version+1,updated_at=? WHERE run_id=?",
                (budget["request_limit"], budget["token_limit"], budget["active_seconds_limit"],
                 c.dump(history, 65536), c.stamp(), run_id),
            )
            return self._work_detail(conn, c.get_row(conn, run_id))

    def settle_work_analysis(self, lease, *, outcome, reason, elapsed_seconds):
        if (not isinstance(lease, WorkLease) or type(elapsed_seconds) not in {int, float}
                or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0
                or outcome not in {"paused", "cancelled", "failed", "invalidated", "succeeded"}
                or (reason is not None and reason not in c.REASONS)
                or (outcome != "succeeded" and reason is None)):
            raise WorkAnalysisError("invalid_request")
        lost = False
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM work_analysis_runs WHERE run_id=?", (lease.run_id,)).fetchone()
            if (row is None or row["deleted_at"] is not None or row["slice_settled"]
                    or row["epoch"] != lease.epoch
                    or not secrets.compare_digest(row["owner_token"] or "", lease.owner_token)):
                raise WorkAnalysisError("owner_lost", 409)
            now = c.stamp()
            if row["status"] == "succeeded":
                status, safe_reason = "succeeded", None
            elif outcome == "invalidated":
                status, safe_reason = "invalidated", reason
            elif row["status"] == "cancelling":
                status, safe_reason = "cancelled", "user_cancel"
            elif row["status"] == "pausing":
                status, safe_reason = "paused", row["reason"]
            elif row["status"] == "running" and outcome != "succeeded":
                status, safe_reason = outcome, reason
            else:
                raise WorkAnalysisError("state_conflict", 409)
            if row["lease_until"] is None or row["lease_until"] <= now:
                lost = True
                if status not in {"succeeded", "cancelled", "invalidated"}:
                    status, safe_reason = "paused", "process_interrupted"
            refund = 0.0 if lost or status == "invalidated" else max(0.0, row["slice_seconds"] - elapsed_seconds)
            _finish_owner(conn, row, status=status, reason=safe_reason, now=now,
                          refund=refund, invalidate=status == "invalidated")
        if lost:
            raise WorkAnalysisError("owner_lost", 409)

    def recover_work_analyses(self, *, now, process_start=False):
        if type(process_start) is not bool:
            raise WorkAnalysisError("invalid_request")
        now_text = c.stamp(now)
        with self.transaction() as conn:
            if process_start:
                rows = conn.execute(
                    "SELECT * FROM work_analysis_runs WHERE deleted_at IS NULL AND "
                    "(owner_token IS NOT NULL OR status='queued' OR (status='paused' AND reason='sync_yield'))"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM work_analysis_runs WHERE deleted_at IS NULL AND owner_token IS NOT NULL "
                    "AND (lease_until IS NULL OR lease_until<=?)", (now_text,),
                ).fetchall()
            for row in rows:
                if row["status"] == "succeeded":
                    status, reason = "succeeded", None
                elif row["status"] in {"cancelling", "cancelled"}:
                    status, reason = "cancelled", "user_cancel"
                elif row["status"] == "invalidated":
                    status, reason = "invalidated", row["reason"]
                else:
                    status, reason = "paused", "process_interrupted"
                _finish_owner(conn, row, status=status, reason=reason, now=now_text,
                              invalidate=status == "invalidated")
            return len(rows)

    def reconcile_work_analysis_task_logs(self):
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT r.shared_log_id,r.shared_job_id,r.completed_at FROM work_analysis_runs r "
                "JOIN task_logs t ON t.id=r.shared_log_id AND t.job_id=r.shared_job_id "
                "WHERE r.status='succeeded' AND r.report_json IS NOT NULL AND r.deleted_at IS NULL "
                "AND r.owner_token IS NULL AND r.slice_settled=1 AND r.shared_epoch<r.epoch "
                "AND r.completed_at IS NOT NULL AND t.task_type='work_analysis' AND t.status IN ('running','failed')"
            ).fetchall()
            repaired = 0
            for row in rows:
                cursor = conn.execute(
                    "UPDATE task_logs SET status='succeeded',error_message=NULL,finished_at=?,"
                    "duration_seconds=MAX(0,(julianday(?)-julianday(started_at))*86400) "
                    "WHERE id=? AND job_id=? AND task_type='work_analysis' AND status IN ('running','failed')",
                    (row["completed_at"], row["completed_at"], row["shared_log_id"], row["shared_job_id"]),
                )
                repaired += cursor.rowcount
            return repaired

    def _invalidate_work_analyses_for_source(self, conn, novel_id, new_hash, *, deleted=False):
        if not conn.in_transaction:
            raise RuntimeError("work source invalidation requires its caller's transaction")
        c.integer(novel_id, 1, 2**63 - 1)
        if type(deleted) is not bool:
            raise WorkAnalysisError("invalid_request")
        if new_hash is not None:
            c.hex_id(new_hash)
        if deleted:
            conn.execute(tombstone_sql(
                "SELECT run_id FROM work_analysis_sources WHERE novel_id=?", source_deleted=True,
            ), (novel_id,))
        else:
            conn.execute(invalidate_sql(
                "SELECT run_id FROM work_analysis_sources WHERE novel_id=? AND text_hash IS NOT ?",
            ), (novel_id, new_hash))

from __future__ import annotations

import hashlib
import secrets

from ...ai.usage import ProviderUsage, merge_usage_snapshot
from ...work_analysis.contracts import WorkAnalysisError, WorkCallBinding
from . import common as c


def _transaction(conn):
    if not conn.in_transaction:
        raise RuntimeError("work request hook requires its caller's transaction")


def _bound_unit(conn, binding, *, now=None):
    if not isinstance(binding, WorkCallBinding):
        raise WorkAnalysisError("invalid_request")
    row = c.require_owner(conn, binding.lease, now=now)
    if binding.configuration_fingerprint != row["configuration_fingerprint"]:
        raise WorkAnalysisError("configuration_changed", 409)
    unit = conn.execute(
        "SELECT * FROM work_analysis_units WHERE run_id=? AND unit_id=?",
        (binding.lease.run_id, c.hex_id(binding.unit_id)),
    ).fetchone()
    if (unit is None or unit["status"] != "running" or unit["owner_epoch"] != binding.lease.epoch
            or type(binding.try_no) is not int or unit["try_no"] != binding.try_no):
        raise WorkAnalysisError("owner_lost", 409)
    return row, unit


def _child_owner_hash(child):
    owner = child["owner_token"]
    if not isinstance(owner, str) or not owner:
        raise WorkAnalysisError("owner_lost", 409)
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()


def _live_child(conn, child_job_id, row, *, now=None):
    if not isinstance(child_job_id, str) or not 1 <= len(child_job_id) <= 128:
        raise WorkAnalysisError("invalid_request")
    child = conn.execute("SELECT * FROM ai_jobs WHERE job_id=?", (child_job_id,)).fetchone()
    if (child is None or child["status"] != "running" or child["cancel_requested"]
            or child["stage"] != "main" or child["agent_id"] != c.load(row["configuration_json"])["agent_id"]
            or child["lease_until"] is None or child["lease_until"] <= c.stamp(now)):
        raise WorkAnalysisError("owner_lost", 409)
    if child["route_deadline_at"] is None or child["route_deadline_at"] <= c.stamp(now):
        raise WorkAnalysisError("active_time_exhausted", 409)
    if c.load(child["input_json"], {}).get("retention_policy") != "metadata_only":
        raise WorkAnalysisError("owner_lost", 409)
    return child


class WorkAnalysisRequestsMixin:
    def begin_work_analysis_unit(self, lease, unit_id):
        with self.transaction() as conn:
            row = c.require_owner(conn, lease)
            unit = conn.execute(
                "SELECT * FROM work_analysis_units WHERE run_id=? AND unit_id=?", (lease.run_id, c.hex_id(unit_id)),
            ).fetchone()
            if unit is None or unit["status"] != "pending":
                raise WorkAnalysisError("state_conflict", 409)
            if unit["try_no"] >= 512:
                raise WorkAnalysisError("budget_exhausted", 409)
            try_no = unit["try_no"] + 1
            conn.execute(
                "UPDATE work_analysis_units SET status='running',try_no=?,owner_epoch=?,child_job_id=NULL,"
                "last_error=NULL,completed_at=NULL WHERE run_id=? AND unit_id=?",
                (try_no, lease.epoch, lease.run_id, unit_id),
            )
            return WorkCallBinding(lease, unit_id, try_no, row["configuration_fingerprint"])

    def _attach_work_analysis_child(self, conn, binding, child_job_id):
        _transaction(conn)
        row, unit = _bound_unit(conn, binding)
        child = _live_child(conn, child_job_id, row)
        if child["network_request_count"] != 0:
            raise WorkAnalysisError("owner_lost", 409)
        digest = _child_owner_hash(child)
        existing = conn.execute(
            "SELECT * FROM work_analysis_requests WHERE child_job_id=? AND request_ordinal=0", (child_job_id,),
        ).fetchone()
        if existing is not None:
            if (existing["entry_kind"] != "attachment" or existing["run_id"] != binding.lease.run_id
                    or existing["unit_id"] != binding.unit_id or existing["try_no"] != binding.try_no
                    or existing["epoch"] != binding.lease.epoch or existing["child_owner_hash"] != digest
                    or existing["configuration_fingerprint"] != binding.configuration_fingerprint
                    or unit["child_job_id"] != child_job_id):
                raise WorkAnalysisError("owner_lost", 409)
            return
        if unit["child_job_id"] is not None:
            raise WorkAnalysisError("owner_lost", 409)
        conn.execute(
            "INSERT INTO work_analysis_requests(child_job_id,request_ordinal,entry_kind,run_id,unit_id,epoch,"
            "try_no,configuration_fingerprint,child_owner_hash,planned_tokens,usage_json,reserved_at) "
            "VALUES(?,0,'attachment',?,?,?,?,?,?,NULL,NULL,?)",
            (child_job_id, binding.lease.run_id, binding.unit_id, binding.lease.epoch, binding.try_no,
             binding.configuration_fingerprint, digest, c.stamp()),
        )
        conn.execute("UPDATE work_analysis_units SET child_job_id=? WHERE run_id=? AND unit_id=?",
                     (child_job_id, binding.lease.run_id, binding.unit_id))

    def _reserve_work_analysis_request(self, conn, binding, child_job_id, ordinal, planned_tokens, *, now):
        _transaction(conn)
        c.integer(ordinal, 1, 32)
        c.integer(planned_tokens, 1, 100000000)
        row, unit = _bound_unit(conn, binding, now=now)
        child = _live_child(conn, child_job_id, row, now=now)
        attached = conn.execute(
            "SELECT * FROM work_analysis_requests WHERE child_job_id=? AND request_ordinal=0 AND entry_kind='attachment'",
            (child_job_id,),
        ).fetchone()
        if (attached is None or unit["child_job_id"] != child_job_id
                or attached["run_id"] != binding.lease.run_id or attached["unit_id"] != binding.unit_id
                or attached["epoch"] != binding.lease.epoch or attached["try_no"] != binding.try_no
                or attached["configuration_fingerprint"] != binding.configuration_fingerprint
                or not secrets.compare_digest(attached["child_owner_hash"], _child_owner_hash(child))
                or child["network_request_count"] + 1 != ordinal):
            raise WorkAnalysisError("owner_lost", 409)
        if row["request_count"] >= row["request_limit"] or row["planned_tokens"] + planned_tokens > row["token_limit"]:
            raise WorkAnalysisError("budget_exhausted", 409)
        if conn.execute("SELECT 1 FROM work_analysis_requests WHERE child_job_id=? AND request_ordinal=?",
                        (child_job_id, ordinal)).fetchone() is not None:
            raise WorkAnalysisError("owner_lost", 409)
        conn.execute(
            "INSERT INTO work_analysis_requests(child_job_id,request_ordinal,entry_kind,run_id,unit_id,epoch,"
            "try_no,configuration_fingerprint,child_owner_hash,planned_tokens,usage_json,reserved_at) "
            "VALUES(?,?,'request',?,?,?,?,?,?,?,?,?)",
            (child_job_id, ordinal, binding.lease.run_id, binding.unit_id, binding.lease.epoch, binding.try_no,
             binding.configuration_fingerprint, attached["child_owner_hash"], planned_tokens,
             c.dump(ProviderUsage().to_dict(), 16384), c.stamp(now)),
        )
        conn.execute(
            "UPDATE work_analysis_runs SET request_count=request_count+1,planned_tokens=planned_tokens+?,"
            "version=version+1,updated_at=? WHERE run_id=?", (planned_tokens, c.stamp(now), binding.lease.run_id),
        )

    def _record_work_analysis_usage(self, conn, child_job_id, ordinal, usage):
        _transaction(conn)
        c.integer(ordinal, 1, 32)
        row = conn.execute(
            "SELECT * FROM work_analysis_requests WHERE child_job_id=? AND request_ordinal=? AND entry_kind='request'",
            (child_job_id, ordinal),
        ).fetchone()
        if row is None:
            return
        child = conn.execute("SELECT owner_token FROM ai_jobs WHERE job_id=?", (child_job_id,)).fetchone()
        if child is None or not secrets.compare_digest(row["child_owner_hash"], _child_owner_hash(child)):
            raise WorkAnalysisError("owner_lost", 409)
        try:
            merged = merge_usage_snapshot(c.load(row["usage_json"]), usage)
        except (TypeError, ValueError):
            raise WorkAnalysisError("invalid_request") from None
        serialized = c.dump(merged, 16384)
        if serialized != row["usage_json"]:
            conn.execute(
                "UPDATE work_analysis_requests SET usage_json=?,reported_at=? WHERE child_job_id=? AND request_ordinal=?",
                (serialized, c.stamp(), child_job_id, ordinal),
            )

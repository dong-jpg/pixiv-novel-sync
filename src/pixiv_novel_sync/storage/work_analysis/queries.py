from __future__ import annotations

import hashlib

from ...work_analysis.contracts import WorkAnalysisError
from .common import (
    assert_not_deleted, budget_from_row, dump, get_row, hex_id, integer, load, usage_summary,
)


class WorkAnalysisQueriesMixin:
    def _work_unit_view(self, conn, row, *, private=False):
        unit = load(row["unit_json"])
        summary = usage_summary(conn, row["run_id"], row["unit_id"])
        result = {
            "unit_id": row["unit_id"], "kind": row["kind"], "level": row["level"],
            "is_final": bool(row["is_final"]), "status": row["status"], "try_no": row["try_no"],
            "last_error": row["last_error"], "completed_at": row["completed_at"],
            "input_fingerprint": row["input_fingerprint"],
            "range_count": len(unit["primary_ranges"]) if unit else 0,
            "child_count": len(unit["child_unit_ids"]) if unit else 0,
            "actual_target": load(row["actual_target_json"]),
            "request_count": summary["reported_requests"] + summary["unreported_requests"],
            "usage_summary": summary,
        }
        if private:
            result.update(unit=unit, child_job_id=row["child_job_id"], artifact=load(row["artifact_json"]),
                          owner_epoch=row["owner_epoch"])
        return result

    def _work_detail(self, conn, row, *, private=False, include_manifest=True):
        manifest = load(row["manifest_json"])
        counts = conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(status='succeeded'),0) FROM work_analysis_units WHERE run_id=?",
            (row["run_id"],),
        ).fetchone()
        deleted = row["deleted_at"] is not None
        result = {
            "run_id": row["run_id"], "version": row["version"], "status": row["status"], "reason": row["reason"],
            "created_at": row["created_at"], "updated_at": row["updated_at"], "completed_at": row["completed_at"],
            "deleted_at": row["deleted_at"], "deleted": deleted,
            "source_kind": row["source_kind"], "source_id": row["source_id"],
            "title": manifest["title"] if manifest else None,
            "manifest_fingerprint": row["manifest_fingerprint"],
            "report_stale": bool(row["report_stale"]), "archive_outdated": bool(row["archive_outdated"]),
            "budget": budget_from_row(row), "request_count": row["request_count"],
            "planned_tokens": row["planned_tokens"], "active_seconds_debited": row["active_seconds_debited"],
            "usage_summary": usage_summary(conn, row["run_id"]), "coverage": load(row["coverage_json"]),
            "unit_counts": {"total": counts[0], "succeeded": counts[1]},
            "can_resume": not deleted and row["invalidated_at"] is None and row["status"] in {"paused", "cancelled", "failed"},
            "has_report": not deleted and row["status"] == "succeeded" and row["report_json"] is not None,
        }
        if include_manifest:
            result["manifest"] = manifest
        if private:
            result.update(
                selection=load(row["selection_json"]), plan=load(row["plan_json"]),
                configuration=load(row["configuration_json"]), confirmations=load(row["confirmations_json"]),
                epoch=row["epoch"], owner_token=row["owner_token"], lease_until=row["lease_until"],
                shared_job_id=row["shared_job_id"], shared_log_id=row["shared_log_id"], shared_epoch=row["shared_epoch"],
                heartbeat_at=row["heartbeat_at"], slice_deadline_at=row["slice_deadline_at"],
                slice_seconds=row["slice_seconds"], slice_settled=bool(row["slice_settled"]),
                budget_history=load(row["budget_history_json"]), report=load(row["report_json"]),
                units=[self._work_unit_view(conn, unit, private=True) for unit in conn.execute(
                    "SELECT * FROM work_analysis_units WHERE run_id=? ORDER BY ordinal", (row["run_id"],)
                )],
            )
        return result

    def get_work_analysis(self, run_id, *, include_private=False):
        with self.read_transaction() as conn:
            row = conn.execute("SELECT * FROM work_analysis_runs WHERE run_id=?", (hex_id(run_id, 32),)).fetchone()
            return self._work_detail(conn, row, private=include_private) if row is not None else None

    def get_work_analysis_control(self, run_id):
        with self.read_transaction() as conn:
            row = conn.execute(
                "SELECT run_id,version,status,reason,epoch,owner_token,deleted_at,invalidated_at,"
                "manifest_fingerprint,configuration_fingerprint,request_limit,token_limit,active_seconds_limit "
                "FROM work_analysis_runs WHERE run_id=?", (hex_id(run_id, 32),),
            ).fetchone()
            return dict(row) if row is not None else None

    def list_work_analyses(self, offset=0, limit=20):
        integer(offset, 0, 1000000)
        integer(limit, 1, 100)
        with self.read_transaction() as conn:
            total = conn.execute("SELECT COUNT(*) FROM work_analysis_runs WHERE deleted_at IS NULL").fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM work_analysis_runs WHERE deleted_at IS NULL ORDER BY created_at DESC,run_id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return {"items": [self._work_detail(conn, row, include_manifest=False) for row in rows],
                    "total": total, "offset": offset, "limit": limit}

    def list_work_analysis_units(self, run_id, offset=0, limit=20):
        integer(offset, 0, 1000000)
        integer(limit, 1, 100)
        with self.read_transaction() as conn:
            assert_not_deleted(get_row(conn, run_id))
            total = conn.execute("SELECT COUNT(*) FROM work_analysis_units WHERE run_id=?", (run_id,)).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM work_analysis_units WHERE run_id=? ORDER BY ordinal LIMIT ? OFFSET ?", (run_id, limit, offset)
            ).fetchall()
            return {"items": [self._work_unit_view(conn, row) for row in rows],
                    "total": total, "offset": offset, "limit": limit}

    def _work_report(self, conn, row):
        assert_not_deleted(row)
        if row["status"] != "succeeded" or row["report_json"] is None:
            raise WorkAnalysisError("report_not_ready", 409)
        report = load(row["report_json"])
        report["report_stale"] = bool(row["report_stale"])
        report["archive_outdated"] = bool(row["archive_outdated"])
        report["usage_summary"] = usage_summary(conn, row["run_id"])
        for unit in report["units"]:
            unit["usage_summary"] = usage_summary(conn, row["run_id"], unit["unit_id"])
        dump(report, 1048576)
        return report

    def get_work_analysis_report(self, run_id):
        with self.read_transaction() as conn:
            return self._work_report(conn, get_row(conn, run_id))

    def get_work_analysis_evidence(self, run_id, evidence_id):
        hex_id(evidence_id)
        with self.read_transaction() as conn:
            report = self._work_report(conn, get_row(conn, run_id))
            evidence = report["artifact"]["evidence"].get(evidence_id)
            if evidence is None:
                raise WorkAnalysisError("not_found", 404)
            source = conn.execute("SELECT text_raw,text_hash FROM novel_texts WHERE novel_id=?",
                                  (evidence["novel_id"],)).fetchone()
            if source is None:
                raise WorkAnalysisError("source_deleted", 410)
            text = source["text_raw"]
            if (source["text_hash"] != evidence["text_hash"]
                    or hashlib.sha256(text.encode("utf-8")).hexdigest() != evidence["text_hash"]
                    or text[evidence["start"]:evidence["end"]] != evidence["quote"]):
                raise WorkAnalysisError("source_changed", 410)
            start = max(0, evidence["start"] - 128)
            end = min(len(text), evidence["end"] + 128)
            return {"evidence": evidence, "jump": {
                "novel_id": evidence["novel_id"], "text_hash": evidence["text_hash"],
                "start": evidence["start"], "end": evidence["end"],
                "context_start": start, "context_text": text[start:end],
            }}

    def queued_work_analysis_ids(self):
        return [row[0] for row in self.conn.execute(
            "SELECT run_id FROM work_analysis_runs WHERE deleted_at IS NULL "
            "AND (status='queued' OR (status='paused' AND reason='sync_yield')) "
            "ORDER BY created_at,run_id LIMIT 128"
        )]

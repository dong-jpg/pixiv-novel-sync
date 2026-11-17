from __future__ import annotations

import uuid

from ...work_analysis.contracts import WorkAnalysisError, fingerprint, validate_budget
from ...work_analysis.coverage import coverage
from ...work_analysis.manifest import (
    capture_manifest, preview_fingerprint as compute_preview_fingerprint,
    public_preview_identity, validate_manifest, validate_selection,
)
from .common import (
    assert_not_deleted, check_confirmations, dump, get_row, insert_unit,
    key_hash, submitted_identity, validate_plan,
)


class WorkAnalysisRunsMixin:
    def _work_lookup_idempotency(self, conn, idempotency_key, identity):
        row = conn.execute(
            "SELECT * FROM work_analysis_runs WHERE idempotency_key_hash=?", (key_hash(idempotency_key),)
        ).fetchone()
        if row is None:
            return None
        if row["request_identity_hash"] != fingerprint(submitted_identity(identity)):
            raise WorkAnalysisError("idempotency_conflict", 409)
        assert_not_deleted(row)
        return self._work_detail(conn, row)

    def lookup_work_analysis_idempotency(self, idempotency_key, request_identity):
        identity = submitted_identity(request_identity)
        with self.read_transaction() as conn:
            return self._work_lookup_idempotency(conn, idempotency_key, identity)

    def create_work_analysis(self, selection, manifest, plan, configuration, budget, confirmations,
                             idempotency_key, preview_fingerprint):
        from ...work_analysis.configuration import work_configuration_identity

        selection = validate_selection(selection)
        budget = validate_budget(budget)
        confirmations = check_confirmations(confirmations)
        identity = submitted_identity({
            "selection": selection, "agent_id": configuration["agent_id"],
            "output_tokens": configuration["output_tokens"], "budget": budget,
            "confirmations": confirmations, "preview_fingerprint": preview_fingerprint,
        })
        with self.transaction() as conn:
            existing = self._work_lookup_idempotency(conn, idempotency_key, identity)
            if existing is not None:
                return existing
            manifest = validate_manifest(manifest)
            plan = validate_plan(plan, manifest)
            current_manifest, texts = capture_manifest(conn, selection)
            current_config = work_configuration_identity(conn, configuration["agent_id"], configuration["output_tokens"])
            if (public_preview_identity(current_manifest) != public_preview_identity(manifest)
                    or current_config != configuration
                    or compute_preview_fingerprint(selection, current_manifest, plan, current_config, budget)
                    != preview_fingerprint):
                raise WorkAnalysisError("preview_conflict", 409)
            if not any(chapter["has_text"] for chapter in manifest["chapters"]):
                raise WorkAnalysisError("empty_source", 409)
            if not confirmations["external_send_confirmed"] or not confirmations["budget_confirmed"]:
                raise WorkAnalysisError("confirmation_required", 409)
            if (manifest["source_kind"] == "series" or "ordered_novel_ids" in selection
                    or manifest["order_source"] == "publication_date_suggestion") and not confirmations["order_confirmed"]:
                raise WorkAnalysisError("confirmation_required", 409)
            if manifest["requires_partial_confirmation"] and not confirmations["current_archive_only"]:
                raise WorkAnalysisError("confirmation_required", 409)
            if plan["minimum_requests"] > budget["request_limit"]:
                raise WorkAnalysisError("budget_exhausted", 409)
            run_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO work_analysis_runs(run_id,idempotency_key_hash,request_identity_hash,"
                "source_kind,source_id,selection_json,manifest_json,manifest_fingerprint,plan_json,"
                "configuration_json,configuration_fingerprint,confirmations_json,preview_fingerprint,"
                "request_limit,token_limit,active_seconds_limit,coverage_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, key_hash(idempotency_key), fingerprint(identity), selection["source_kind"],
                 selection["source_id"], dump(selection, 65536), dump(manifest, 2097152),
                 manifest["manifest_fingerprint"], dump(plan, 8388608), dump(configuration, 262144),
                 configuration["configuration_fingerprint"], dump(confirmations, 1024), preview_fingerprint,
                 budget["request_limit"], budget["token_limit"], budget["active_seconds_limit"],
                 dump(coverage(manifest, []), 65536)),
            )
            conn.executemany(
                "INSERT INTO work_analysis_sources(run_id,novel_id,position,text_hash,chars,utf8_bytes,has_text) "
                "VALUES(?,?,?,?,?,?,?)",
                [(run_id, chapter["novel_id"], chapter["position"], chapter["text_hash"], chapter["chars"],
                  chapter["utf8_bytes"], int(chapter["has_text"])) for chapter in manifest["chapters"]],
            )
            for ordinal, unit in enumerate(plan["original_units"]):
                insert_unit(conn, run_id, unit, ordinal)
            del texts
            return self._work_detail(conn, get_row(conn, run_id))

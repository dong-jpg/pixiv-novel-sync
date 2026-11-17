from __future__ import annotations

from ...ai.model_catalog import normalize_model_key
from ...work_analysis.artifacts import validate_artifact
from ...work_analysis.contracts import WorkAnalysisError, fingerprint
from ...work_analysis.evidence import validate_unit_artifact
from ...work_analysis.manifest import read_source_texts
from ...work_analysis.planning import materialize_unit, seal_unit, validate_derived_units
from . import common as c


def _fields(unit):
    return {key: value for key, value in unit.items() if key not in {"unit_id", "input_fingerprint"}}


def _validate_checkpoint(unit, artifact, blocks, children):
    try:
        artifact = validate_unit_artifact(artifact, unit, blocks, children)
    except WorkAnalysisError as exc:
        if exc.code == "artifact_too_large":
            raise
        raise WorkAnalysisError("validation_failed", 409) from None
    return c.load(c.dump(artifact, 131072))


def _child_artifacts(conn, run_id, unit):
    artifacts, hashes = {}, {}
    for child_id in unit["child_unit_ids"]:
        child = conn.execute(
            "SELECT * FROM work_analysis_units WHERE run_id=? AND unit_id=?", (run_id, child_id),
        ).fetchone()
        if child is None or child["status"] != "succeeded" or child["artifact_json"] is None:
            raise WorkAnalysisError("graph_invalid", 409)
        artifact = validate_artifact(c.load(child["artifact_json"]), final=bool(child["is_final"]))
        if fingerprint(artifact) != child["artifact_hash"]:
            raise WorkAnalysisError("graph_invalid", 409)
        artifacts[child_id], hashes[child_id] = artifact, child["artifact_hash"]
    return artifacts, hashes


def _actual_target(conn, row, unit_row, target):
    c.exact(target, {"provider_id", "model_key"})
    c.integer(target["provider_id"], 1, 2**63 - 1)
    try:
        model_key = normalize_model_key(target["model_key"])
    except ValueError:
        raise WorkAnalysisError("validation_failed", 409) from None
    if target["provider_id"] not in c.load(row["configuration_json"])["allowed_provider_ids"]:
        raise WorkAnalysisError("validation_failed", 409)
    actual = conn.execute(
        "SELECT a.provider_id,a.model_key FROM ai_job_model_attempts a JOIN ai_jobs j ON j.job_id=a.job_id "
        "WHERE a.job_id=? AND a.status='succeeded' AND a.stage='main' AND j.status='succeeded' "
        "AND a.owner_token=j.owner_token ORDER BY a.attempt_index DESC LIMIT 1", (unit_row["child_job_id"],),
    ).fetchone()
    reserved = conn.execute(
        "SELECT 1 FROM work_analysis_requests WHERE run_id=? AND unit_id=? AND epoch=? AND try_no=? "
        "AND child_job_id=? AND entry_kind='request' LIMIT 1",
        (row["run_id"], unit_row["unit_id"], row["epoch"], unit_row["try_no"], unit_row["child_job_id"]),
    ).fetchone()
    if (actual is None or reserved is None or actual["provider_id"] != target["provider_id"]
            or actual["model_key"] != model_key):
        raise WorkAnalysisError("validation_failed", 409)
    return {"provider_id": target["provider_id"], "model_key": model_key}


def _complete_graph(conn, row):
    records = conn.execute("SELECT * FROM work_analysis_units WHERE run_id=? ORDER BY ordinal", (row["run_id"],)).fetchall()
    if not records or any(item["status"] != "succeeded" for item in records):
        raise WorkAnalysisError("report_not_ready", 409)
    originals = [item for item in records if item["level"] == 0]
    expected = c.load(row["plan_json"])["original_units"]
    if [c.load(item["unit_json"]) for item in originals] != expected:
        raise WorkAnalysisError("graph_invalid", 409)
    artifacts = {}
    for item in records:
        artifact = validate_artifact(c.load(item["artifact_json"]), final=bool(item["is_final"]))
        if fingerprint(artifact) != item["artifact_hash"]:
            raise WorkAnalysisError("graph_invalid", 409)
        artifacts[item["unit_id"]] = artifact
    previous = originals
    highest = max(item["level"] for item in records)
    for level in range(1, highest + 1):
        current = [item for item in records if item["level"] == level]
        validate_derived_units([c.load(item["unit_json"]) for item in current],
                               [c.load(item["unit_json"]) for item in previous],
                               {item["unit_id"]: artifacts[item["unit_id"]] for item in previous})
        previous = current
    if len(previous) != 1 or not previous[0]["is_final"]:
        raise WorkAnalysisError("report_not_ready", 409)
    return records, previous[0], artifacts[previous[0]["unit_id"]]


class WorkAnalysisUnitsMixin:
    def save_work_analysis_unit(self, lease, unit_id, artifact, *, actual_target):
        from ...work_analysis.configuration import snapshot_work_configuration

        c.hex_id(unit_id)
        with self.read_transaction() as conn:
            row = c.require_owner(conn, lease)
            entry = conn.execute("SELECT * FROM work_analysis_units WHERE run_id=? AND unit_id=?",
                                 (lease.run_id, unit_id)).fetchone()
            if entry is None or entry["status"] != "running" or entry["owner_epoch"] != lease.epoch:
                raise WorkAnalysisError("state_conflict", 409)
            unit = c.load(entry["unit_json"])
            manifest = c.load(row["manifest_json"])
            children, child_hashes = _child_artifacts(conn, lease.run_id, unit)
            if seal_unit(_fields(unit), manifest_fingerprint=manifest["manifest_fingerprint"], child_artifacts=children) != unit:
                raise WorkAnalysisError("graph_invalid", 409)
            configuration, prompt, preflight = snapshot_work_configuration(
                conn, c.load(row["configuration_json"])["agent_id"], c.load(row["configuration_json"])["output_tokens"],
            )
            texts = read_source_texts(conn, manifest)
            expected_entry = (entry["try_no"], entry["child_job_id"], entry["input_fingerprint"], entry["unit_json"])
        blocks = {}
        try:
            payload, blocks = materialize_unit(manifest, unit, texts, children,
                                               agent_prompt=prompt, output_tokens=configuration["output_tokens"])
            del payload
            normalized = _validate_checkpoint(unit, artifact, blocks, children)
        finally:
            texts.clear()
            blocks.clear()
        serialized = c.dump(normalized, 131072)
        # Recheck the read-phase identity after expensive body/quote validation.
        with self.transaction() as conn:
            row = c.require_owner(conn, lease)
            entry = conn.execute("SELECT * FROM work_analysis_units WHERE run_id=? AND unit_id=?",
                                 (lease.run_id, unit_id)).fetchone()
            if (entry is None or entry["status"] != "running" or entry["owner_epoch"] != lease.epoch
                    or (entry["try_no"], entry["child_job_id"], entry["input_fingerprint"], entry["unit_json"]) != expected_entry):
                raise WorkAnalysisError("owner_lost", 409)
            for child_id, digest in child_hashes.items():
                current = conn.execute("SELECT status,artifact_hash FROM work_analysis_units WHERE run_id=? AND unit_id=?",
                                       (lease.run_id, child_id)).fetchone()
                if current is None or current["status"] != "succeeded" or current["artifact_hash"] != digest:
                    raise WorkAnalysisError("graph_invalid", 409)
            target = _actual_target(conn, row, entry, actual_target)
            conn.execute(
                "UPDATE work_analysis_units SET status='succeeded',artifact_json=?,artifact_hash=?,actual_target_json=?,"
                "last_error=NULL,completed_at=? WHERE run_id=? AND unit_id=?",
                (serialized, fingerprint(normalized), c.dump(target, 4096), c.stamp(), lease.run_id, unit_id),
            )
            current_coverage = c.current_coverage(conn, row)
            conn.execute("UPDATE work_analysis_runs SET coverage_json=?,version=version+1,updated_at=? WHERE run_id=?",
                         (c.dump(current_coverage, 65536), c.stamp(), lease.run_id))

    def add_work_analysis_units(self, lease, units):
        if not isinstance(units, list) or not 1 <= len(units) <= 512:
            raise WorkAnalysisError("graph_invalid", 409)
        with self.transaction() as conn:
            row = c.require_owner(conn, lease)
            manifest = c.load(row["manifest_json"])
            for unit in units:
                c.validate_unit(unit, manifest)
                if unit["kind"] != "reduce":
                    raise WorkAnalysisError("graph_invalid", 409)
            if len({unit["unit_id"] for unit in units}) != len(units):
                raise WorkAnalysisError("graph_invalid", 409)
            records = conn.execute("SELECT * FROM work_analysis_units WHERE run_id=? ORDER BY ordinal",
                                   (lease.run_id,)).fetchall()
            existing = {item["unit_id"]: item for item in records}
            overlap = [unit for unit in units if unit["unit_id"] in existing]
            if overlap:
                if len(overlap) != len(units) or any(c.load(existing[unit["unit_id"]]["unit_json"]) != unit for unit in units):
                    raise WorkAnalysisError("graph_invalid", 409)
                return
            if len(records) + len(units) > 512:
                raise WorkAnalysisError("synthesis_capacity_exceeded", 409)
            highest = max(item["level"] for item in records)
            frontier = [item for item in records if item["level"] == highest]
            if any(item["status"] != "succeeded" or item["is_final"] for item in frontier):
                raise WorkAnalysisError("graph_invalid", 409)
            children = {item["unit_id"]: validate_artifact(c.load(item["artifact_json"]), final=bool(item["is_final"]))
                        for item in frontier}
            if any(fingerprint(children[item["unit_id"]]) != item["artifact_hash"] for item in frontier):
                raise WorkAnalysisError("graph_invalid", 409)
            validate_derived_units(units, [c.load(item["unit_json"]) for item in frontier], children)
            for unit in units:
                subset = {key: children[key] for key in unit["child_unit_ids"]}
                if unit["level"] != highest + 1 or seal_unit(_fields(unit), child_artifacts=subset) != unit:
                    raise WorkAnalysisError("graph_invalid", 409)
            for ordinal, unit in enumerate(units, len(records)):
                c.insert_unit(conn, lease.run_id, unit, ordinal)
            conn.execute("UPDATE work_analysis_runs SET version=version+1,updated_at=? WHERE run_id=?",
                         (c.stamp(), lease.run_id))

    def publish_work_analysis(self, lease, report):
        artifact = validate_artifact(report, final=True)
        c.dump(artifact, 131072)
        with self.transaction() as conn:
            row = c.require_owner(conn, lease)
            if row["shared_job_id"] is not None and row["shared_epoch"] != lease.epoch:
                raise WorkAnalysisError("owner_lost", 409)
            records, root, saved = _complete_graph(conn, row)
            if saved != artifact or root["unit_id"] != artifact["unit_id"]:
                raise WorkAnalysisError("validation_failed", 409)
            current_coverage = c.current_coverage(conn, row)
            if not current_coverage["complete"]:
                raise WorkAnalysisError("report_not_ready", 409)
            published_at = c.stamp()
            envelope = {
                "schema_version": 1, "run_id": lease.run_id, "manifest": c.load(row["manifest_json"]),
                "coverage": current_coverage, "artifact": saved,
                "units": [{"unit_id": item["unit_id"], "kind": item["kind"],
                           "actual_target": c.load(item["actual_target_json"]),
                           "usage_summary": c.usage_summary(conn, lease.run_id, item["unit_id"])} for item in records],
                "usage_summary": c.usage_summary(conn, lease.run_id), "created_at": row["created_at"],
                "published_at": published_at, "report_stale": False,
                "archive_outdated": bool(row["archive_outdated"]), "synthesis_complete": True,
            }
            try:
                serialized = c.dump(envelope, 1048576)
            except WorkAnalysisError as error:
                if error.code == "too_large":
                    raise WorkAnalysisError("synthesis_capacity_exceeded", 409) from None
                raise
            conn.execute(
                "UPDATE work_analysis_runs SET status='succeeded',reason=NULL,report_json=?,coverage_json=?,"
                "completed_at=?,updated_at=?,version=version+1 WHERE run_id=?",
                (serialized, c.dump(current_coverage, 65536), published_at, published_at, lease.run_id),
            )

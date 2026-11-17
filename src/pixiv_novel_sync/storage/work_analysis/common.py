from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone

from ...ai.usage import summarize_usage
from ...work_analysis.contracts import WorkAnalysisError, WorkLease, canonical_bytes, validate_budget
from ...work_analysis.coverage import coverage
from ...work_analysis.manifest import validate_selection
from ...work_analysis.planning import seal_unit


REASONS = frozenset({
    "user_pause", "sync_yield", "budget_exhausted", "process_interrupted", "source_changed",
    "source_deleted", "configuration_changed", "validation_failed", "planning_failed",
    "provider_failed", "unexpected_error", "deleted", "user_cancel", "transport_failed",
    "execution_failed", "graph_invalid", "synthesis_capacity_exceeded",
    "legacy_repreview_required", "invalidated", "work_deleted",
})
CONFIRMATIONS = frozenset({
    "order_confirmed", "current_archive_only", "external_send_confirmed", "budget_confirmed",
})
UNIT_KEYS = frozenset({
    "unit_id", "kind", "level", "is_final", "primary_ranges", "context_ranges",
    "child_unit_ids", "claim_refs", "input_fingerprint",
})


def utc_now():
    return datetime.now(timezone.utc)


def stamp(value=None):
    value = utc_now() if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise WorkAnalysisError("invalid_request")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise WorkAnalysisError("invalid_request")
    return value


def hex_id(value, length=64):
    if not isinstance(value, str) or re.fullmatch("[0-9a-f]{" + str(length) + "}", value) is None:
        raise WorkAnalysisError("invalid_request")
    return value


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise WorkAnalysisError("invalid_request")
    return value


def dump(value, limit):
    try:
        encoded = canonical_bytes(value)
    except (TypeError, ValueError, UnicodeError):
        raise WorkAnalysisError("invalid_request") from None
    if len(encoded) > limit:
        raise WorkAnalysisError("too_large", 413)
    return encoded.decode("utf-8")


def load(value, default=None):
    return json.loads(value) if value is not None else default


def check_confirmations(value):
    exact(value, CONFIRMATIONS)
    if any(type(flag) is not bool for flag in value.values()):
        raise WorkAnalysisError("invalid_request")
    return dict(value)


def submitted_identity(value):
    exact(value, {"selection", "agent_id", "output_tokens", "budget", "confirmations", "preview_fingerprint"})
    return {
        "selection": validate_selection(value["selection"]),
        "agent_id": integer(value["agent_id"], 1, 2**63 - 1),
        "output_tokens": integer(value["output_tokens"], 1, 1000000),
        "budget": validate_budget(value["budget"]),
        "confirmations": check_confirmations(value["confirmations"]),
        "preview_fingerprint": hex_id(value["preview_fingerprint"]),
    }


def key_hash(value):
    return hashlib.sha256(hex_id(value, 32).encode("ascii")).hexdigest()


def budget_from_row(row):
    return {name: row[name] for name in ("request_limit", "token_limit", "active_seconds_limit")}


def get_row(conn, run_id):
    row = conn.execute("SELECT * FROM work_analysis_runs WHERE run_id=?", (hex_id(run_id, 32),)).fetchone()
    if row is None:
        raise WorkAnalysisError("not_found", 404)
    return row


def assert_not_deleted(row):
    if row["deleted_at"] is not None:
        raise WorkAnalysisError("work_deleted", 410)


def assert_version(row, version):
    integer(version, 1, 2**63 - 1)
    if row["version"] != version:
        raise WorkAnalysisError("version_conflict", 409)


def assert_sources(conn, row):
    mismatch = conn.execute(
        "SELECT 1 FROM work_analysis_sources s LEFT JOIN novels n ON n.novel_id=s.novel_id "
        "LEFT JOIN novel_texts t ON t.novel_id=s.novel_id "
        "WHERE s.run_id=? AND (n.novel_id IS NULL OR t.text_hash IS NOT s.text_hash) LIMIT 1",
        (row["run_id"],),
    ).fetchone()
    if mismatch is not None:
        raise WorkAnalysisError("source_changed", 409)


def assert_configuration(conn, row):
    from ...work_analysis.configuration import work_configuration_identity

    configuration = load(row["configuration_json"])
    if configuration is None:
        raise WorkAnalysisError("work_deleted", 410)
    try:
        current = work_configuration_identity(conn, configuration["agent_id"], configuration["output_tokens"])
    except WorkAnalysisError:
        raise WorkAnalysisError("configuration_changed", 409) from None
    if current["configuration_fingerprint"] != row["configuration_fingerprint"]:
        raise WorkAnalysisError("configuration_changed", 409)


def assert_identity(conn, row):
    assert_not_deleted(row)
    if row["invalidated_at"] is not None:
        raise WorkAnalysisError("invalidated", 409)
    assert_sources(conn, row)
    assert_configuration(conn, row)


def require_owner(conn, lease, *, now=None, states=("running",), deadline=True, identity=True):
    if not isinstance(lease, WorkLease):
        raise WorkAnalysisError("invalid_request")
    row = conn.execute("SELECT * FROM work_analysis_runs WHERE run_id=?", (lease.run_id,)).fetchone()
    if row is None:
        raise WorkAnalysisError("owner_lost", 409)
    assert_not_deleted(row)
    if row["epoch"] != lease.epoch or not secrets.compare_digest(row["owner_token"] or "", lease.owner_token):
        raise WorkAnalysisError("owner_lost", 409)
    if row["status"] not in states:
        code = "work_stopping" if row["status"] in {"pausing", "cancelling"} else "state_conflict"
        raise WorkAnalysisError(code, 409)
    now_text = stamp(now)
    if row["lease_until"] is None or row["lease_until"] <= now_text:
        raise WorkAnalysisError("lease_expired", 409)
    if deadline and (row["slice_deadline_at"] is None or row["slice_deadline_at"] <= now_text):
        raise WorkAnalysisError("active_time_exhausted", 409)
    if identity:
        assert_identity(conn, row)
    return row


def usage_summary(conn, run_id, unit_id=None):
    sql = "SELECT usage_json FROM work_analysis_requests WHERE run_id=? AND entry_kind='request'"
    params = [run_id]
    if unit_id is not None:
        sql += " AND unit_id=?"
        params.append(unit_id)
    sql += " ORDER BY reserved_at,child_job_id,request_ordinal"
    rows = conn.execute(sql, params).fetchall()
    records = [{"request_ordinal": index, **load(row[0])} for index, row in enumerate(rows, 1)]
    return {**summarize_usage([{"usage": {"requests": records}}], network_request_count=len(rows)),
            "network_request_count": len(rows)}


def current_coverage(conn, row):
    manifest = load(row["manifest_json"])
    if manifest is None:
        return None
    accepted = [
        {**load(item[0]), "status": "succeeded"}
        for item in conn.execute(
            "SELECT unit_json FROM work_analysis_units WHERE run_id=? AND status='succeeded'",
            (row["run_id"],),
        )
    ]
    return coverage(manifest, accepted)


def validate_unit(unit, manifest):
    exact(unit, UNIT_KEYS)
    hex_id(unit["unit_id"])
    hex_id(unit["input_fingerprint"])
    if (not isinstance(unit["kind"], str) or unit["kind"] not in {"whole", "extract", "reduce"}
            or type(unit["is_final"]) is not bool):
        raise WorkAnalysisError("invalid_request")
    integer(unit["level"], 0, 8)
    if unit["kind"] == "whole" and (unit["level"] != 0 or not unit["is_final"]):
        raise WorkAnalysisError("invalid_request")
    if unit["kind"] == "extract" and (unit["level"] != 0 or unit["is_final"]):
        raise WorkAnalysisError("invalid_request")
    chapters = {chapter["novel_id"]: chapter for chapter in manifest["chapters"]}
    for name, maximum in (("primary_ranges", 2048), ("context_ranges", 4096)):
        ranges = unit[name]
        if not isinstance(ranges, list) or len(ranges) > maximum:
            raise WorkAnalysisError("invalid_request")
        for item in ranges:
            keys = {"novel_id", "text_hash", "start", "end"}
            if name == "context_ranges":
                keys.add("block_id")
            exact(item, keys)
            chapter = chapters.get(integer(item["novel_id"], 1, 2**63 - 1))
            if chapter is None or not chapter["has_text"] or item["text_hash"] != chapter["text_hash"]:
                raise WorkAnalysisError("invalid_request")
            integer(item["start"], 0, chapter["chars"])
            integer(item["end"], item["start"] + 1, chapter["chars"])
            if name == "context_ranges":
                hex_id(item["block_id"])
                if item["end"] - item["start"] > 512:
                    raise WorkAnalysisError("invalid_request")
    children = unit["child_unit_ids"]
    if not isinstance(children, list) or len(children) > 16:
        raise WorkAnalysisError("invalid_request")
    for child in children:
        hex_id(child)
    if len(children) != len(set(children)):
        raise WorkAnalysisError("invalid_request")
    refs = unit["claim_refs"]
    if not isinstance(refs, list) or len(refs) > 2048:
        raise WorkAnalysisError("invalid_request")
    for ref in refs:
        exact(ref, {"unit_id", "claim_id"})
        hex_id(ref["claim_id"])
        if ref["unit_id"] not in children:
            raise WorkAnalysisError("invalid_request")
    if unit["kind"] == "reduce":
        if unit["level"] < 1 or not children or unit["primary_ranges"] or unit["context_ranges"]:
            raise WorkAnalysisError("invalid_request")
    elif children or refs or not unit["primary_ranges"]:
        raise WorkAnalysisError("invalid_request")
    dump(unit, 1048576)
    return unit


def validate_plan(plan, manifest):
    exact(plan, {"schema_version", "strategy", "manifest_fingerprint", "chunker_version",
                 "prompt_version", "report_schema_version", "original_units", "minimum_requests", "warnings"})
    for field in ("schema_version", "chunker_version", "prompt_version", "report_schema_version"):
        integer(plan[field], 1, 1)
    if plan["manifest_fingerprint"] != manifest["manifest_fingerprint"]:
        raise WorkAnalysisError("invalid_request")
    units = plan["original_units"]
    if not isinstance(units, list) or not 1 <= len(units) <= 512:
        raise WorkAnalysisError("invalid_request")
    if not isinstance(plan["strategy"], str) or plan["strategy"] not in {"whole", "segmented"}:
        raise WorkAnalysisError("invalid_request")
    expected_kind = "whole" if plan["strategy"] == "whole" else "extract"
    if expected_kind == "whole" and len(units) != 1:
        raise WorkAnalysisError("invalid_request")
    for unit in units:
        validate_unit(unit, manifest)
        if unit["kind"] != expected_kind:
            raise WorkAnalysisError("invalid_request")
        fields = {key: value for key, value in unit.items() if key not in {"unit_id", "input_fingerprint"}}
        if unit != seal_unit(fields, manifest_fingerprint=manifest["manifest_fingerprint"]):
            raise WorkAnalysisError("invalid_plan")
    if len({unit["unit_id"] for unit in units}) != len(units):
        raise WorkAnalysisError("invalid_request")
    for chapter in manifest["chapters"]:
        if not chapter["has_text"]:
            continue
        ranges = sorted((part["start"], part["end"]) for unit in units for part in unit["primary_ranges"]
                        if part["novel_id"] == chapter["novel_id"])
        cursor = 0
        for start, end in ranges:
            if start != cursor:
                raise WorkAnalysisError("invalid_request")
            cursor = end
        if cursor != chapter["chars"]:
            raise WorkAnalysisError("invalid_request")
    integer(plan["minimum_requests"], len(units), 512)
    warnings = plan["warnings"]
    if not isinstance(warnings, list) or len(warnings) > 32 or any(
        not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is None for value in warnings
    ):
        raise WorkAnalysisError("invalid_request")
    dump(plan, 8388608)
    return plan


def insert_unit(conn, run_id, unit, ordinal):
    conn.execute(
        "INSERT INTO work_analysis_units(run_id,unit_id,ordinal,kind,level,is_final,unit_json,input_fingerprint) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (run_id, unit["unit_id"], ordinal, unit["kind"], unit["level"], int(unit["is_final"]),
         dump(unit, 1048576), unit["input_fingerprint"]),
    )

"""Explicit reading feedback, private summaries, and atomic paid-call accounting.

No archived/bookmarked/recommended item is implicitly a positive reading signal.
The old statistical profile is intentionally not a storage location for this layer.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone

from ..ai.usage import ProviderUsage, merge_usage_snapshot
from ..preference_learning.contracts import PreferenceCallBinding
from ..work_analysis.artifacts import validate_artifact
from ..work_analysis.configuration import snapshot_work_configuration
from ..work_analysis.contracts import (
    WorkAnalysisError, canonical_bytes, fingerprint, hex_id, strict_int,
)


def stamp(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WorkAnalysisError("invalid_request")
    # AI leases and request guards use the canonical SQLite UTC representation.
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def dump(value, maximum=16 * 1024 * 1024):
    try:
        encoded = canonical_bytes(value)
    except (TypeError, ValueError, UnicodeError):
        raise WorkAnalysisError("invalid_request") from None
    if len(encoded) > maximum:
        raise WorkAnalysisError("input_too_large", 413)
    return encoded.decode("utf-8")


def source_exists(conn, kind, source_id):
    if not isinstance(kind, str) or kind not in {"novel", "series"}:
        raise WorkAnalysisError("invalid_request")
    strict_int(source_id, 1, 2**63 - 1)
    table, column = ("novels", "novel_id") if kind == "novel" else ("series", "series_id")
    return conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (source_id,)).fetchone() is not None


def _invalidate(where, reason="source_changed"):
    return (
        "UPDATE preference_summary_runs SET status='invalidated',active=0,owner_token=NULL,"
        f"error_code='{reason}',result_json=NULL,sources_json=NULL,version=version+1 "
        f"WHERE status!='deleted' AND ({where});"
    )


def migrate_preference_learning(conn: sqlite3.Connection):
    """Additive migration: individual statements preserve the caller transaction."""
    statements = [
        """CREATE TABLE IF NOT EXISTS preference_reading_feedback (
            source_kind TEXT NOT NULL CHECK(source_kind IN ('novel','series')),
            source_id INTEGER NOT NULL CHECK(typeof(source_id)='integer' AND source_id>0),
            reaction TEXT NOT NULL CHECK(reaction IN ('liked','disliked','neutral','clear')),
            note TEXT NOT NULL DEFAULT '' CHECK(length(note)<=500),
            version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version>0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(source_kind,source_id)
        ) WITHOUT ROWID""",
        """CREATE TABLE IF NOT EXISTS preference_summary_runs (
            run_id TEXT PRIMARY KEY CHECK(length(run_id)=32),
            idempotency_hash TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            configuration_fingerprint TEXT NOT NULL,
            agent_id INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL CHECK(output_tokens BETWEEN 1 AND 1000000),
            request_limit INTEGER NOT NULL CHECK(request_limit BETWEEN 1 AND 512),
            token_limit INTEGER NOT NULL CHECK(token_limit BETWEEN 1 AND 100000000),
            active_seconds_limit INTEGER NOT NULL CHECK(active_seconds_limit BETWEEN 30 AND 14400),
            request_count INTEGER NOT NULL DEFAULT 0 CHECK(request_count BETWEEN 0 AND request_limit),
            planned_tokens INTEGER NOT NULL DEFAULT 0 CHECK(planned_tokens BETWEEN 0 AND token_limit),
            status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelled','invalidated','deleted')),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
            active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)),
            owner_token TEXT,
            deadline_at TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            shared_job_id TEXT,
            child_job_id TEXT,
            source_count INTEGER NOT NULL CHECK(source_count BETWEEN 1 AND 2000),
            sources_json TEXT CHECK(sources_json IS NULL OR length(CAST(sources_json AS BLOB))<=16777216),
            result_json TEXT CHECK(result_json IS NULL OR length(CAST(result_json AS BLOB))<=262144),
            actual_target_json TEXT,
            error_code TEXT,
            CHECK(active=0 OR (status='succeeded' AND result_json IS NOT NULL))
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_preference_summary_active ON preference_summary_runs(active) WHERE active=1",
        "CREATE INDEX IF NOT EXISTS idx_preference_summary_status ON preference_summary_runs(status,created_at)",
        """CREATE TABLE IF NOT EXISTS preference_summary_sources (
            run_id TEXT NOT NULL REFERENCES preference_summary_runs(run_id) ON DELETE CASCADE,
            source_kind TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            report_run_id TEXT NOT NULL,
            PRIMARY KEY(run_id,source_kind,source_id)
        ) WITHOUT ROWID""",
        "CREATE INDEX IF NOT EXISTS idx_preference_summary_report ON preference_summary_sources(report_run_id,run_id)",
        """CREATE TABLE IF NOT EXISTS preference_summary_requests (
            child_job_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 32),
            run_id TEXT NOT NULL REFERENCES preference_summary_runs(run_id),
            child_owner_hash TEXT NOT NULL,
            planned_tokens INTEGER,
            usage_json TEXT,
            reserved_at TEXT NOT NULL,
            PRIMARY KEY(child_job_id,ordinal),
            CHECK((ordinal=0 AND planned_tokens IS NULL AND usage_json IS NULL) OR
                  (ordinal>0 AND planned_tokens>0 AND usage_json IS NOT NULL))
        ) WITHOUT ROWID""",
        "CREATE INDEX IF NOT EXISTS idx_preference_summary_request_run ON preference_summary_requests(run_id,ordinal)",
    ]
    for event in ("INSERT", "UPDATE", "DELETE"):
        statements.append(
            f"CREATE TRIGGER IF NOT EXISTS preference_feedback_{event.lower()} AFTER {event} "
            "ON preference_reading_feedback BEGIN " + _invalidate("1") + " END"
        )
    for kind, table, column in (("novel", "novels", "novel_id"), ("series", "series", "series_id")):
        statements.append(
            f"CREATE TRIGGER IF NOT EXISTS preference_source_{kind}_delete AFTER DELETE ON {table} BEGIN "
            f"DELETE FROM preference_reading_feedback WHERE source_kind='{kind}' AND source_id=OLD.{column}; "
            + _invalidate(
                "run_id IN (SELECT run_id FROM preference_summary_sources "
                f"WHERE source_kind='{kind}' AND source_id=OLD.{column})", "source_deleted"
            ) + " END"
        )
    statements.append(
        "CREATE TRIGGER IF NOT EXISTS preference_report_invalidated "
        "AFTER UPDATE OF deleted_at,report_stale,archive_outdated,report_json ON work_analysis_runs "
        "WHEN NEW.deleted_at IS NOT NULL OR NEW.report_stale=1 OR NEW.archive_outdated=1 "
        "OR OLD.report_json IS NOT NEW.report_json BEGIN "
        + _invalidate("run_id IN (SELECT run_id FROM preference_summary_sources WHERE report_run_id=OLD.run_id)")
        + " END"
    )
    statements.append(
        "CREATE TRIGGER IF NOT EXISTS preference_report_deleted AFTER DELETE ON work_analysis_runs BEGIN "
        + _invalidate("run_id IN (SELECT run_id FROM preference_summary_sources WHERE report_run_id=OLD.run_id)", "source_deleted")
        + " END"
    )
    for statement in statements:
        conn.execute(statement)


def capture_sources(conn):
    """Return verified report projections and the entire explicit-signal identity.

    Stored reports are the sole quote source. No full novel text is read/copied.
    A series rating takes precedence over ratings of its individual chapters.
    """
    feedback = [dict(row) for row in conn.execute(
        "SELECT * FROM preference_reading_feedback WHERE reaction!='clear' ORDER BY source_kind,source_id"
    )]
    if len(feedback) > 2000:
        raise WorkAnalysisError("input_too_large", 413)
    counts = {key: sum(row["reaction"] == key for row in feedback) for key in ("liked", "disliked", "neutral")}
    rated_series = {row["source_id"] for row in feedback if row["source_kind"] == "series"}
    sources, identity, missing = [], [], 0
    for signal in feedback:
        kind, source_id = signal["source_kind"], signal["source_id"]
        if not source_exists(conn, kind, source_id):
            continue
        signal_identity = {key: signal[key] for key in ("source_kind", "source_id", "reaction", "note", "version")}
        if kind == "novel":
            parent = conn.execute("SELECT series_id FROM novels WHERE novel_id=?", (source_id,)).fetchone()
            if parent is not None and parent["series_id"] in rated_series:
                identity.append({**signal_identity, "covered_by_series": parent["series_id"]})
                continue
        row = conn.execute(
            "SELECT run_id,report_json FROM work_analysis_runs WHERE source_kind=? AND source_id=? "
            "AND status='succeeded' AND deleted_at IS NULL AND report_stale=0 AND archive_outdated=0 "
            "AND report_json IS NOT NULL ORDER BY created_at DESC,run_id DESC LIMIT 1", (kind, source_id),
        ).fetchone()
        if row is None:
            identity.append({**signal_identity, "report_run_id": None})
            missing += 1
            continue
        try:
            report = json.loads(row["report_json"])
            if (report.get("coverage", {}).get("complete") is not True
                    or report.get("synthesis_complete") is not True):
                raise WorkAnalysisError("artifact_invalid")
            artifact = validate_artifact(report["artifact"], final=True)
        except (KeyError, TypeError, ValueError):
            identity.append({**signal_identity, "report_run_id": None})
            missing += 1
            continue
        digest = hashlib.sha256(row["report_json"].encode("utf-8")).hexdigest()
        identity.append({**signal_identity, "report_run_id": row["run_id"], "report_hash": digest})
        sources.append({
            "source_key": f"{kind}:{source_id}", "source_kind": kind, "source_id": source_id,
            "reaction": signal["reaction"], "note": signal["note"], "feedback_version": signal["version"],
            "report_run_id": row["run_id"], "report_hash": digest,
            "claims": [{key: claim[key] for key in ("claim_id", "dimension", "label", "certainty", "evidence_ids")}
                       for claim in artifact["claims"]],
            "evidence": list(artifact["evidence"].values()),
            "uncertainties": artifact["uncertainties"],
        })
    dump(sources)
    return {"sources": sources, "source_fingerprint": fingerprint(identity), "feedback_counts": counts,
            "eligible_works": len(sources), "missing_reports": missing}


def get_run(conn, run_id):
    row = conn.execute("SELECT * FROM preference_summary_runs WHERE run_id=?", (hex_id(run_id, 32),)).fetchone()
    if row is None:
        raise WorkAnalysisError("not_found", 404)
    return row


def is_stale(conn, row, captured=None):
    if row["status"] in {"invalidated", "deleted"}:
        return True
    try:
        return row["source_fingerprint"] != (captured or capture_sources(conn))["source_fingerprint"]
    except (TypeError, ValueError):
        return True


def public_run(conn, row, *, include_result=False, captured=None):
    stale = is_stale(conn, row, captured)
    usage_rows = [json.loads(value[0]) for value in conn.execute(
        "SELECT usage_json FROM preference_summary_requests WHERE run_id=? AND ordinal>0", (row["run_id"],)
    )]
    usage = {"network_request_count": row["request_count"], "planned_tokens": row["planned_tokens"]}
    for name in ("input_tokens", "output_tokens"):
        known = [item[name] for item in usage_rows if item.get(name) is not None]
        usage["reported_" + name] = sum(known) if known and len(known) == len(usage_rows) else None
        usage["reported_known_" + name] = sum(known) if known else None
    usage["unknown_requests"] = sum(not item.get("complete") for item in usage_rows)
    result = {key: row[key] for key in ("run_id", "version", "status", "agent_id", "created_at",
                                      "source_count", "error_code", "shared_job_id")}
    result.update({"active": bool(row["active"]) and not stale, "stale": stale, "usage": usage,
                   "budget": {key: row[key] for key in ("request_limit", "token_limit", "active_seconds_limit")}})
    if include_result:
        result["result"] = json.loads(row["result_json"]) if row["result_json"] else None
        result["sources"] = json.loads(row["sources_json"]) if row["sources_json"] else []
        result["actual_target"] = json.loads(row["actual_target_json"]) if row["actual_target_json"] else None
    return result


def require_bound(conn, binding, *, now=None):
    if not isinstance(binding, PreferenceCallBinding):
        raise WorkAnalysisError("owner_lost", 409)
    row = get_run(conn, binding.run_id)
    if row["status"] == "deleted":
        raise WorkAnalysisError("work_deleted", 410)
    if row["status"] in {"cancelled", "invalidated"}:
        raise WorkAnalysisError("work_stopping", 409)
    if (row["status"] != "running" or not isinstance(binding.owner_token, str)
            or not row["owner_token"] or not secrets.compare_digest(row["owner_token"], binding.owner_token)
            or row["configuration_fingerprint"] != binding.configuration_fingerprint):
        raise WorkAnalysisError("owner_lost", 409)
    if row["deadline_at"] is None or row["deadline_at"] <= stamp(now):
        raise WorkAnalysisError("active_time_exhausted", 409)
    if is_stale(conn, row):
        raise WorkAnalysisError("source_changed", 409)
    try:
        configuration, _, _ = snapshot_work_configuration(conn, row["agent_id"], row["output_tokens"])
    except (TypeError, ValueError):
        raise WorkAnalysisError("configuration_changed", 409) from None
    if configuration["configuration_fingerprint"] != row["configuration_fingerprint"]:
        raise WorkAnalysisError("configuration_changed", 409)
    return row


def _child(conn, child_job_id, row, *, now=None):
    child = conn.execute("SELECT * FROM ai_jobs WHERE job_id=?", (child_job_id,)).fetchone()
    if (child is None or child["status"] != "running" or child["cancel_requested"] or child["stage"] != "main"
            or child["agent_id"] != row["agent_id"] or not child["owner_token"]
            or child["lease_until"] is None or child["lease_until"] <= stamp(now)
            or json.loads(child["input_json"] or "{}").get("retention_policy") != "metadata_only"):
        raise WorkAnalysisError("owner_lost", 409)
    return child, hashlib.sha256(child["owner_token"].encode("utf-8")).hexdigest()


class PreferenceLearningMixin:
    def get_reading_feedback(self, kind, source_id):
        with self.read_transaction() as conn:
            if not source_exists(conn, kind, source_id):
                raise WorkAnalysisError("not_found", 404)
            row = conn.execute("SELECT * FROM preference_reading_feedback WHERE source_kind=? AND source_id=?",
                               (kind, source_id)).fetchone()
            return dict(row) if row else {"source_kind": kind, "source_id": source_id, "reaction": "clear",
                                          "version": 0, "note": ""}

    def set_reading_feedback(self, kind, source_id, reaction, expected_version, note=""):
        strict_int(expected_version, 0, 2**63 - 1)
        if not isinstance(reaction, str) or reaction not in {"liked", "disliked", "neutral", "clear"}:
            raise WorkAnalysisError("invalid_request")
        if not isinstance(note, str) or len(note) > 500:
            raise WorkAnalysisError("invalid_request")
        dump(note, 2000)
        if reaction == "clear":
            note = ""
        with self.transaction() as conn:
            if not source_exists(conn, kind, source_id):
                raise WorkAnalysisError("not_found", 404)
            old = conn.execute("SELECT * FROM preference_reading_feedback WHERE source_kind=? AND source_id=?",
                               (kind, source_id)).fetchone()
            if (old["version"] if old else 0) != expected_version:
                raise WorkAnalysisError("version_conflict", 409)
            if old and old["reaction"] == reaction and old["note"] == note:
                return dict(old)
            conn.execute(
                "INSERT INTO preference_reading_feedback(source_kind,source_id,reaction,note,version,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(source_kind,source_id) DO UPDATE SET reaction=excluded.reaction,"
                "note=excluded.note,version=excluded.version,updated_at=excluded.updated_at",
                (kind, source_id, reaction, note, expected_version + 1, stamp()),
            )
            return dict(conn.execute("SELECT * FROM preference_reading_feedback WHERE source_kind=? AND source_id=?",
                                     (kind, source_id)).fetchone())

    def _attach_preference_learning_child(self, conn, binding, child_job_id):
        if not conn.in_transaction:
            raise RuntimeError("Preference attachment requires the child transaction")
        row = require_bound(conn, binding)
        child, owner_hash = _child(conn, child_job_id, row)
        if child["network_request_count"] != 0 or row["child_job_id"] not in (None, child_job_id):
            raise WorkAnalysisError("owner_lost", 409)
        old = conn.execute("SELECT * FROM preference_summary_requests WHERE child_job_id=? AND ordinal=0",
                           (child_job_id,)).fetchone()
        if old:
            if old["run_id"] != binding.run_id or old["child_owner_hash"] != owner_hash:
                raise WorkAnalysisError("owner_lost", 409)
            return
        conn.execute(
            "INSERT INTO preference_summary_requests(child_job_id,ordinal,run_id,child_owner_hash,reserved_at) "
            "VALUES(?,0,?,?,?)", (child_job_id, binding.run_id, owner_hash, stamp()),
        )
        conn.execute("UPDATE preference_summary_runs SET child_job_id=? WHERE run_id=?", (child_job_id, binding.run_id))

    def _reserve_preference_learning_request(self, conn, binding, child_job_id, ordinal, planned_tokens, *, now):
        if not conn.in_transaction:
            raise RuntimeError("Preference reservation requires the request transaction")
        attachment = conn.execute(
            "SELECT * FROM preference_summary_requests WHERE child_job_id=? AND ordinal=0", (child_job_id,),
        ).fetchone()
        if binding is None:
            if attachment is not None:
                raise WorkAnalysisError("owner_lost", 409)
            return
        strict_int(ordinal, 1, 32)
        strict_int(planned_tokens, 1, 100000000)
        row = require_bound(conn, binding, now=now)
        child, owner_hash = _child(conn, child_job_id, row, now=now)
        if (attachment is None or attachment["run_id"] != binding.run_id
                or attachment["child_owner_hash"] != owner_hash or row["child_job_id"] != child_job_id
                or child["network_request_count"] + 1 != ordinal):
            raise WorkAnalysisError("owner_lost", 409)
        if row["request_count"] >= row["request_limit"] or row["planned_tokens"] + planned_tokens > row["token_limit"]:
            raise WorkAnalysisError("budget_exhausted", 409)
        if conn.execute("SELECT 1 FROM preference_summary_requests WHERE child_job_id=? AND ordinal=?",
                        (child_job_id, ordinal)).fetchone():
            raise WorkAnalysisError("owner_lost", 409)
        conn.execute(
            "INSERT INTO preference_summary_requests(child_job_id,ordinal,run_id,child_owner_hash,planned_tokens,usage_json,reserved_at) "
            "VALUES(?,?,?,?,?,?,?)", (child_job_id, ordinal, binding.run_id, owner_hash, planned_tokens,
                                      dump(ProviderUsage().to_dict(), 16384), stamp(now)),
        )
        conn.execute("UPDATE preference_summary_runs SET request_count=request_count+1,planned_tokens=planned_tokens+?,"
                     "version=version+1 WHERE run_id=?", (planned_tokens, binding.run_id))

    def _record_preference_learning_usage(self, conn, child_job_id, ordinal, usage):
        row = conn.execute("SELECT * FROM preference_summary_requests WHERE child_job_id=? AND ordinal=? AND ordinal>0",
                           (child_job_id, ordinal)).fetchone()
        if row is None:
            return
        if not conn.in_transaction:
            raise RuntimeError("Preference usage requires the usage transaction")
        child = conn.execute("SELECT owner_token FROM ai_jobs WHERE job_id=?", (child_job_id,)).fetchone()
        if (child is None or not child["owner_token"] or row["child_owner_hash"] !=
                hashlib.sha256(child["owner_token"].encode("utf-8")).hexdigest()):
            raise WorkAnalysisError("owner_lost", 409)
        try:
            merged = merge_usage_snapshot(json.loads(row["usage_json"]), usage)
        except (TypeError, ValueError):
            raise WorkAnalysisError("invalid_request") from None
        conn.execute("UPDATE preference_summary_requests SET usage_json=? WHERE child_job_id=? AND ordinal=?",
                     (dump(merged, 16384), child_job_id, ordinal))

    def get_preference_learning_signals(self):
        """A current, deterministic recommendation overlay, never a saved cron profile."""
        with self.read_transaction() as conn:
            captured = capture_sources(conn)
            weights, references = {}, {}
            for source in captured["sources"]:
                polarity = {"liked": 1, "disliked": -1, "neutral": 0}[source["reaction"]]
                if not polarity:
                    continue
                for label in {claim["label"] for claim in source["claims"] if claim["certainty"] != "insufficient"}:
                    weights[label] = weights.get(label, 0) + polarity
                    references.setdefault(label, []).append({"source_key": source["source_key"],
                                                            "report_run_id": source["report_run_id"]})
            active = conn.execute("SELECT * FROM preference_summary_runs WHERE active=1").fetchone()
            active_id = None
            if active is not None and not is_stale(conn, active, captured) and active["result_json"]:
                active_id = active["run_id"]
                for entry in json.loads(active["result_json"])["preferences"]:
                    # AI can reinforce supported labels by one vote, never invent a label or a hard mute.
                    label = entry["label"]
                    if label in weights and ((weights[label] > 0 and entry["polarity"] == "positive")
                                             or (weights[label] < 0 and entry["polarity"] == "negative")):
                        weights[label] += 1 if weights[label] > 0 else -1
            return {"weights": {key: max(-5, min(5, value)) for key, value in weights.items() if value},
                    "references": references, "source_fingerprint": captured["source_fingerprint"],
                    "active_run_id": active_id, "feedback_counts": captured["feedback_counts"]}

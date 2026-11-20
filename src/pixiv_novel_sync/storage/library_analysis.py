"""Durable library discovery and conservative allocations for work analyses.

This layer owns no model execution. Every paid item is an ordinary work_analysis
run, created in the same transaction as its allocation and discovery checkpoint.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from ..work_analysis.configuration import work_configuration_identity
from ..work_analysis.contracts import WorkAnalysisError, fingerprint
from .work_analysis import common as c


BUDGET_KEYS = ("request_limit", "token_limit", "active_seconds_limit")
ACTIVE_BATCHES = ("queued", "scanning", "waiting")
LIVE_WORK = ("queued", "running", "pausing", "cancelling")
HOLD_SELECT = (
    "SELECT t.target_id,t.run_id,t.held,t.batch_id,b.status AS batch_status,b.version AS batch_version,"
    "r.status,r.reason,r.version,r.deleted_at,r.epoch,r.owner_token,r.manifest_fingerprint,r.configuration_fingerprint "
    "FROM library_analysis_targets t JOIN library_analysis_batches b ON b.batch_id=t.batch_id "
    "LEFT JOIN work_analysis_runs r ON r.run_id=t.run_id "
)


def _now():
    return datetime.now(timezone.utc)


def _allocated(row):
    return {key: row["allocated_" + key] for key in BUDGET_KEYS}


def content_identity(manifest):
    """Only source membership/order/content, never a refreshed title or timestamp."""
    return fingerprint({
        "source_kind": manifest["source_kind"], "source_id": manifest["source_id"],
        "chapters": [[row["novel_id"], row["text_hash"], bool(row["has_text"])]
                     for row in manifest["chapters"]],
    })


def _source_identity(conn, kind, source_id):
    predicate = "n.novel_id=? AND n.series_id IS NULL" if kind == "novel" else "n.series_id=?"
    rows = conn.execute(
        "SELECT n.novel_id,t.text_hash,COALESCE(t.has_content,0) AS has_text FROM novels n "
        "LEFT JOIN novel_texts t ON t.novel_id=n.novel_id WHERE " + predicate +
        " ORDER BY n.create_date,n.novel_id LIMIT 2049", (source_id,),
    ).fetchall()
    if not rows or not any(row["has_text"] for row in rows):
        raise WorkAnalysisError("empty_source", 409)
    if len(rows) > 2048:
        raise WorkAnalysisError("input_too_large", 413)
    chapters = [dict(row) for row in rows]
    if kind == 'series':
        from ..sync.series_inventory import observed_order
        order = observed_order(conn, source_id)
        by_id = {row['novel_id']: row for row in chapters}
        if set(order) == set(by_id):
            chapters = [by_id[novel_id] for novel_id in order]
    manifest = {"source_kind": kind, "source_id": source_id, "chapters": chapters}
    return content_identity(manifest)


def _policy_view(row):
    return {
        "version": row["version"], "enabled": bool(row["enabled"]), "reason": row["reason"],
        "configuration": c.load(row["configuration_json"]), "allocated": _allocated(row),
        "work_count": row["work_count"], "next_run_at": row["next_run_at"],
        "new_baseline_revision": row["new_baseline_revision"],
    }


def _batch_view(row):
    configuration = c.load(row["configuration_json"])
    return {
        "batch_id": row["batch_id"], "version": row["version"], "status": row["status"],
        "reason": row["reason"], "origin": row["origin"], "scope": configuration["scope"],
        "work_limit": configuration["work_limit"], "work_count": row["work_count"],
        "scanned_count": row["scanned_count"], "skipped_count": row["skipped_count"],
        "budget": configuration["batch_budget"], "allocated": _allocated(row),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


DDL = (
    """CREATE TABLE IF NOT EXISTS library_analysis_clock (
        id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL DEFAULT 0,
        seeded INTEGER NOT NULL DEFAULT 0 CHECK(seeded IN (0,1)))""",
    "INSERT OR IGNORE INTO library_analysis_clock(id) VALUES(1)",
    """CREATE TABLE IF NOT EXISTS library_analysis_sources (
        source_kind TEXT NOT NULL CHECK(source_kind IN ('novel','series')),
        source_id INTEGER NOT NULL CHECK(source_id>0), revision INTEGER NOT NULL CHECK(revision>=0),
        PRIMARY KEY(source_kind,source_id))""",
    "CREATE INDEX IF NOT EXISTS library_analysis_source_revision ON library_analysis_sources(revision)",
    """CREATE TABLE IF NOT EXISTS library_analysis_policy (
        id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)), reason TEXT,
        configuration_json TEXT, configuration_fingerprint TEXT, new_baseline_revision INTEGER,
        allocated_request_limit INTEGER NOT NULL DEFAULT 0 CHECK(allocated_request_limit>=0),
        allocated_token_limit INTEGER NOT NULL DEFAULT 0 CHECK(allocated_token_limit>=0),
        allocated_active_seconds_limit INTEGER NOT NULL DEFAULT 0 CHECK(allocated_active_seconds_limit>=0),
        work_count INTEGER NOT NULL DEFAULT 0 CHECK(work_count>=0), next_run_at TEXT,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    "INSERT OR IGNORE INTO library_analysis_policy(id) VALUES(1)",
    """CREATE TABLE IF NOT EXISTS library_analysis_batches (
        batch_id TEXT PRIMARY KEY CHECK(length(batch_id)=32),
        idempotency_key TEXT UNIQUE, request_identity TEXT,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        origin TEXT NOT NULL CHECK(origin IN ('manual','policy')), policy_version INTEGER,
        status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN
            ('queued','scanning','waiting','completed','paused','cancelled','failed')),
        reason TEXT, configuration_json TEXT NOT NULL, configuration_fingerprint TEXT NOT NULL,
        max_revision INTEGER NOT NULL, min_revision INTEGER NOT NULL DEFAULT -1,
        cursor_kind TEXT NOT NULL DEFAULT '', cursor_id INTEGER NOT NULL DEFAULT 0,
        allocated_request_limit INTEGER NOT NULL DEFAULT 0 CHECK(allocated_request_limit>=0),
        allocated_token_limit INTEGER NOT NULL DEFAULT 0 CHECK(allocated_token_limit>=0),
        allocated_active_seconds_limit INTEGER NOT NULL DEFAULT 0 CHECK(allocated_active_seconds_limit>=0),
        work_count INTEGER NOT NULL DEFAULT 0, scanned_count INTEGER NOT NULL DEFAULT 0,
        skipped_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE INDEX IF NOT EXISTS library_analysis_pending_batch
        ON library_analysis_batches(status,created_at,batch_id)""",
    """CREATE TABLE IF NOT EXISTS library_analysis_targets (
        target_id TEXT PRIMARY KEY CHECK(length(target_id)=32),
        batch_id TEXT NOT NULL REFERENCES library_analysis_batches(batch_id),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('novel','series')), source_id INTEGER NOT NULL CHECK(source_id>0),
        content_fingerprint TEXT NOT NULL, configuration_fingerprint TEXT NOT NULL,
        run_id TEXT REFERENCES work_analysis_runs(run_id) ON DELETE SET NULL,
        status TEXT NOT NULL CHECK(status IN ('created','reused','skipped','error')), reason TEXT,
        held INTEGER NOT NULL DEFAULT 0 CHECK(held IN (0,1)),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(batch_id,source_kind,source_id))""",
    """CREATE UNIQUE INDEX IF NOT EXISTS library_analysis_attempt_identity
        ON library_analysis_targets(source_kind,source_id,content_fingerprint,configuration_fingerprint)
        WHERE status IN ('created','error')""",
    """CREATE INDEX IF NOT EXISTS library_analysis_seen_identity
        ON library_analysis_targets(source_kind,source_id,content_fingerprint,configuration_fingerprint)""",
    "CREATE INDEX IF NOT EXISTS library_analysis_target_run ON library_analysis_targets(run_id,status)",
)


def _record_source(kind, source_id, condition="1"):
    return f"""INSERT INTO library_analysis_sources(source_kind,source_id,revision)
        SELECT {kind},{source_id},revision FROM library_analysis_clock WHERE id=1 AND ({condition})
        ON CONFLICT(source_kind,source_id) DO UPDATE SET revision=excluded.revision;"""


def create_library_analysis_source_triggers(conn):
    if not conn.in_transaction:
        raise RuntimeError("library source triggers require a transaction")
    source_kind = "CASE WHEN (SELECT series_id FROM novels WHERE novel_id=NEW.novel_id) IS NULL THEN 'novel' ELSE 'series' END"
    source_id = "COALESCE((SELECT series_id FROM novels WHERE novel_id=NEW.novel_id),NEW.novel_id)"
    record = "UPDATE library_analysis_clock SET revision=revision+1 WHERE id=1;" + _record_source(source_kind, source_id)
    for statement in (
        f"""CREATE TRIGGER IF NOT EXISTS library_analysis_text_insert AFTER INSERT ON novel_texts
            BEGIN {record} END""",
        f"""CREATE TRIGGER IF NOT EXISTS library_analysis_text_update AFTER UPDATE OF text_raw,text_hash ON novel_texts
            WHEN OLD.text_raw IS NOT NEW.text_raw OR OLD.text_hash IS NOT NEW.text_hash BEGIN {record} END""",
        """CREATE TRIGGER IF NOT EXISTS library_analysis_membership AFTER UPDATE OF series_id ON novels
            WHEN OLD.series_id IS NOT NEW.series_id AND EXISTS(SELECT 1 FROM novel_texts WHERE novel_id=NEW.novel_id)
            BEGIN UPDATE library_analysis_clock SET revision=revision+1 WHERE id=1;""" +
        _record_source("CASE WHEN OLD.series_id IS NULL THEN 'novel' ELSE 'series' END", "COALESCE(OLD.series_id,OLD.novel_id)") +
        _record_source("CASE WHEN NEW.series_id IS NULL THEN 'novel' ELSE 'series' END", "COALESCE(NEW.series_id,NEW.novel_id)") + " END",
    ):
        conn.execute(statement)


def migrate_library_analysis(conn):
    if not conn.in_transaction:
        raise RuntimeError("library analysis migration requires a transaction")
    for statement in DDL:
        conn.execute(statement)
    for table, name, declaration in (("library_analysis_policy", "new_baseline_revision", "INTEGER"),
                                      ("library_analysis_batches", "min_revision", "INTEGER NOT NULL DEFAULT -1")):
        if name not in {row[1] for row in conn.execute("PRAGMA table_info(" + table + ")")}:
            conn.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + declaration)
    if not conn.execute("SELECT seeded FROM library_analysis_clock WHERE id=1").fetchone()[0]:
        conn.execute(
            "INSERT OR IGNORE INTO library_analysis_sources(source_kind,source_id,revision) "
            "SELECT CASE WHEN n.series_id IS NULL THEN 'novel' ELSE 'series' END,"
            "COALESCE(n.series_id,n.novel_id),0 FROM novels n JOIN novel_texts t ON t.novel_id=n.novel_id "
            "WHERE t.has_content=1 GROUP BY 1,2"
        )
        conn.execute("UPDATE library_analysis_clock SET seeded=1 WHERE id=1")
    create_library_analysis_source_triggers(conn)


ELIGIBLE = """((s.source_kind='novel' AND EXISTS(
    SELECT 1 FROM novels n JOIN novel_texts t ON t.novel_id=n.novel_id
    WHERE n.novel_id=s.source_id AND n.series_id IS NULL AND t.has_content=1))
    OR (s.source_kind='series' AND EXISTS(
    SELECT 1 FROM novels n JOIN novel_texts t ON t.novel_id=n.novel_id
    JOIN series v ON v.series_id=n.series_id WHERE n.series_id=s.source_id AND t.has_content=1)))"""


class LibraryAnalysisMixin:
    def get_library_analysis_state(self):
        with self.read_transaction() as conn:
            policy = conn.execute("SELECT * FROM library_analysis_policy WHERE id=1").fetchone()
            batches = conn.execute("SELECT * FROM library_analysis_batches ORDER BY created_at DESC,rowid DESC LIMIT 50").fetchall()
            return {"policy": _policy_view(policy), "batches": [_batch_view(row) for row in batches]}

    def library_analysis_inventory(self, scope, *, lower_revision=None):
        conn = self.conn
        revision = conn.execute("SELECT revision FROM library_analysis_clock WHERE id=1").fetchone()[0]
        lower = (0 if scope == "new" else -1) if lower_revision is None else lower_revision
        predicate = "s.revision>? AND s.revision<=? AND " + ELIGIBLE
        count = conn.execute("SELECT COUNT(*) FROM library_analysis_sources s WHERE " + predicate, (lower, revision)).fetchone()[0]
        rows = conn.execute(
            "SELECT s.source_kind,s.source_id,CASE WHEN s.source_kind='novel' THEN "
            "(SELECT substr(title,1,4096) FROM novels WHERE novel_id=s.source_id) ELSE "
            "(SELECT substr(title,1,4096) FROM series WHERE series_id=s.source_id) END AS title "
            "FROM library_analysis_sources s WHERE " + predicate + " ORDER BY s.source_kind,s.source_id LIMIT 10",
            (lower, revision),
        ).fetchall()
        return {"revision": revision, "candidate_count": count, "samples": [dict(row) for row in rows]}

    def lookup_library_analysis_batch(self, key, identity):
        with self.read_transaction() as conn:
            row = conn.execute("SELECT * FROM library_analysis_batches WHERE idempotency_key=?", (key,)).fetchone()
            if row is None:
                return None
            if row["request_identity"] != identity:
                raise WorkAnalysisError("idempotency_conflict", 409)
            return _batch_view(row)

    def create_library_analysis_batch(self, configuration, config_fingerprint, *, revision,
                                      idempotency_key=None, request_identity=None, policy_version=None):
        with self.transaction() as conn:
            if idempotency_key is not None:
                existing = self.lookup_library_analysis_batch(idempotency_key, request_identity)
                if existing is not None:
                    return existing
            actual = work_configuration_identity(conn, configuration["agent_id"], configuration["output_tokens"])
            if actual["configuration_fingerprint"] != config_fingerprint:
                raise WorkAnalysisError("configuration_changed", 409)
            current_revision = conn.execute("SELECT revision FROM library_analysis_clock WHERE id=1").fetchone()[0]
            if revision != current_revision:
                raise WorkAnalysisError("stale_preview", 409)
            if policy_version is not None:
                policy = conn.execute("SELECT * FROM library_analysis_policy WHERE id=1").fetchone()
                if not policy["enabled"] or policy["version"] != policy_version:
                    raise WorkAnalysisError("version_conflict", 409)
                active = conn.execute("SELECT * FROM library_analysis_batches WHERE origin='policy' AND status IN ('queued','scanning','waiting') LIMIT 1").fetchone()
                if active is not None:
                    return _batch_view(active)
                if policy["next_run_at"] is not None and policy["next_run_at"] > c.stamp():
                    raise WorkAnalysisError("version_conflict", 409)
            lower = 0 if configuration["scope"] == "new" else -1
            if policy_version is not None and configuration["scope"] == "new":
                lower = policy["new_baseline_revision"]
                if lower is None:
                    raise WorkAnalysisError("confirmation_required", 409)
            batch_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO library_analysis_batches(batch_id,idempotency_key,request_identity,origin,policy_version,"
                "configuration_json,configuration_fingerprint,max_revision,min_revision) VALUES(?,?,?,?,?,?,?,?,?)",
                (batch_id, idempotency_key, request_identity, "manual" if policy_version is None else "policy", policy_version,
                 c.dump(configuration, 65536), config_fingerprint, revision, lower),
            )
            if policy_version is not None:
                conn.execute("UPDATE library_analysis_policy SET next_run_at=? WHERE id=1",
                             (c.stamp(_now() + timedelta(minutes=configuration["interval_minutes"])),))
            return _batch_view(conn.execute("SELECT * FROM library_analysis_batches WHERE batch_id=?", (batch_id,)).fetchone())

    def update_library_analysis_policy(self, enabled, expected_version, configuration=None, *, config_fingerprint=None, revision=None):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM library_analysis_policy WHERE id=1").fetchone()
            if row["version"] != expected_version:
                raise WorkAnalysisError("version_conflict", 409)
            if enabled:
                actual = work_configuration_identity(conn, configuration["agent_id"], configuration["output_tokens"])
                if actual["configuration_fingerprint"] != config_fingerprint:
                    raise WorkAnalysisError("configuration_changed", 409)
                if conn.execute("SELECT revision FROM library_analysis_clock WHERE id=1").fetchone()[0] != revision:
                    raise WorkAnalysisError("stale_preview", 409)
                if any(configuration["batch_budget"][key] < row["allocated_" + key] for key in BUDGET_KEYS) or configuration["work_limit"] < row["work_count"]:
                    raise WorkAnalysisError("budget_below_allocated", 409)
            baseline = row["new_baseline_revision"]
            if enabled and configuration["scope"] == "new" and baseline is None:
                baseline = revision
            # Existing work retains its allocation, even after a new configuration.
            conn.execute("UPDATE library_analysis_targets SET held=1 WHERE status='created' AND run_id IN "
                         "(SELECT run_id FROM work_analysis_runs WHERE deleted_at IS NULL AND "
                         "(status IN ('queued','running','pausing','cancelling') OR (status='paused' AND reason='sync_yield'))) "
                         "AND batch_id IN (SELECT batch_id FROM library_analysis_batches WHERE origin='policy' AND status IN ('queued','scanning','waiting'))")
            conn.execute("UPDATE library_analysis_batches SET status='paused',reason=?,version=version+1,updated_at=? "
                         "WHERE origin='policy' AND status IN ('queued','scanning','waiting')",
                         ("policy_changed" if enabled else "policy_disabled", c.stamp()))
            conn.execute(
                "UPDATE library_analysis_policy SET enabled=?,reason=?,version=version+1,configuration_json=?,"
                "configuration_fingerprint=?,next_run_at=?,new_baseline_revision=?,updated_at=? WHERE id=1",
                (int(enabled), None if enabled else "user_disabled",
                 c.dump(configuration, 65536) if enabled else row["configuration_json"],
                 config_fingerprint if enabled else row["configuration_fingerprint"],
                 c.stamp() if enabled else row["next_run_at"], baseline, c.stamp()),
            )
            return _policy_view(conn.execute("SELECT * FROM library_analysis_policy WHERE id=1").fetchone())

    def get_library_analysis_batch(self, batch_id, offset=0, limit=50):
        c.hex_id(batch_id, 32)
        c.integer(offset, 0, 1000000)
        c.integer(limit, 1, 100)
        with self.read_transaction() as conn:
            row = conn.execute("SELECT * FROM library_analysis_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if row is None:
                raise WorkAnalysisError("not_found", 404)
            total = conn.execute("SELECT COUNT(*) FROM library_analysis_targets WHERE batch_id=?", (batch_id,)).fetchone()[0]
            rows = conn.execute(
                "SELECT t.target_id,t.source_kind,t.source_id,t.run_id,t.status,t.reason,"
                "CASE WHEN r.deleted_at IS NOT NULL THEN 'deleted' ELSE r.status END AS run_status,"
                "CASE WHEN t.source_kind='novel' THEN (SELECT substr(title,1,4096) FROM novels WHERE novel_id=t.source_id) "
                "ELSE (SELECT substr(title,1,4096) FROM series WHERE series_id=t.source_id) END AS title "
                "FROM library_analysis_targets t LEFT JOIN work_analysis_runs r ON r.run_id=t.run_id "
                "WHERE t.batch_id=? ORDER BY t.rowid LIMIT ? OFFSET ?", (batch_id, limit, offset),
            ).fetchall()
            return {"batch": _batch_view(row), "items": [dict(item) for item in rows], "total": total, "offset": offset, "limit": limit}

    def control_library_analysis_batch(self, batch_id, action, expected_version):
        c.hex_id(batch_id, 32)
        c.integer(expected_version, 1, 2**63 - 1)
        if action not in {"pause", "cancel", "resume"}:
            raise WorkAnalysisError("invalid_action")
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM library_analysis_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if row is None:
                raise WorkAnalysisError("not_found", 404)
            if row["version"] != expected_version:
                raise WorkAnalysisError("version_conflict", 409)
            if row["status"] in {"completed", "failed"} or (action == "resume" and row["status"] not in {"paused", "cancelled"}):
                raise WorkAnalysisError("state_conflict", 409)
            if action == "resume":
                policy = conn.execute("SELECT * FROM library_analysis_policy WHERE id=1").fetchone()
                if row["origin"] == "policy" and (not policy["enabled"] or policy["configuration_fingerprint"] != row["configuration_fingerprint"]):
                    raise WorkAnalysisError("policy_disabled", 409)
                configuration = c.load(row["configuration_json"])
                if work_configuration_identity(conn, configuration["agent_id"], configuration["output_tokens"])["configuration_fingerprint"] != row["configuration_fingerprint"]:
                    raise WorkAnalysisError("configuration_changed", 409)
            else:
                conn.execute("UPDATE library_analysis_targets SET held=1 WHERE batch_id=? AND status='created' AND run_id IN "
                             "(SELECT run_id FROM work_analysis_runs WHERE deleted_at IS NULL AND "
                             "(status IN ('queued','running','pausing','cancelling') OR (status='paused' AND reason='sync_yield')))", (batch_id,))
            status = {"pause": "paused", "cancel": "cancelled", "resume": "scanning"}[action]
            conn.execute("UPDATE library_analysis_batches SET status=?,reason=?,version=version+1,updated_at=? WHERE batch_id=?",
                         (status, None if action == "resume" else "user_" + action, c.stamp(), batch_id))
            return _batch_view(conn.execute("SELECT * FROM library_analysis_batches WHERE batch_id=?", (batch_id,)).fetchone())

    def library_analysis_held_work(self, batch_id=None):
        params = () if batch_id is None else (c.hex_id(batch_id, 32),)
        extra = "" if batch_id is None else " AND b.batch_id=?"
        return [dict(row) for row in self.conn.execute(
            HOLD_SELECT + "WHERE t.held=1 AND "
            "(r.run_id IS NULL OR r.deleted_at IS NOT NULL OR r.status IN ('succeeded','failed','invalidated') OR "
            "b.status NOT IN ('paused','cancelled','failed') OR r.status IN ('queued','running','pausing','cancelling') "
            "OR (r.status='paused' AND r.reason='sync_yield'))" + extra + " ORDER BY t.rowid LIMIT 128", params)]

    def get_library_analysis_hold(self, target_id):
        with self.read_transaction() as conn:
            row = conn.execute(HOLD_SELECT + "WHERE t.target_id=?", (c.hex_id(target_id, 32),)).fetchone()
            return dict(row) if row is not None else None

    def clear_library_analysis_hold(self, target_id, *, expected_batch_version):
        c.integer(expected_batch_version, 1, 2**63 - 1)
        with self.transaction() as conn:
            changed = conn.execute(
                "UPDATE library_analysis_targets SET held=0 WHERE target_id=? AND EXISTS "
                "(SELECT 1 FROM library_analysis_batches b WHERE b.batch_id=library_analysis_targets.batch_id AND b.version=?)",
                (c.hex_id(target_id, 32), expected_batch_version),
            )
            return changed.rowcount == 1

    def recover_library_analyses(self):
        with self.transaction() as conn:
            # No automatic resume of a run whose last request outcome is unknown.
            count = conn.execute("SELECT COUNT(*) FROM library_analysis_batches WHERE status IN ('queued','scanning','waiting')").fetchone()[0]
            if count:
                conn.execute("UPDATE library_analysis_targets SET held=1 WHERE status='created' AND run_id IN "
                             "(SELECT run_id FROM work_analysis_runs WHERE deleted_at IS NULL AND "
                             "(status IN ('queued','running','pausing','cancelling') OR (status='paused' AND reason IN ('sync_yield','process_interrupted')))) "
                             "AND batch_id IN (SELECT batch_id FROM library_analysis_batches WHERE status IN ('queued','scanning','waiting'))")
                conn.execute("UPDATE library_analysis_batches SET status='paused',reason='process_interrupted',version=version+1,updated_at=? "
                             "WHERE status IN ('queued','scanning','waiting')", (c.stamp(),))
                conn.execute("UPDATE library_analysis_policy SET enabled=0,reason='process_interrupted',version=version+1,updated_at=? WHERE enabled=1", (c.stamp(),))
            return count

    def library_analysis_due_policy(self):
        with self.read_transaction() as conn:
            row = conn.execute("SELECT * FROM library_analysis_policy WHERE id=1").fetchone()
            if not row["enabled"] or (row["next_run_at"] is not None and row["next_run_at"] > c.stamp()):
                return None
            if conn.execute("SELECT 1 FROM library_analysis_batches WHERE origin='policy' AND status IN ('queued','scanning','waiting') LIMIT 1").fetchone():
                return None
            return {"version": row["version"], "configuration": c.load(row["configuration_json"]),
                    "configuration_fingerprint": row["configuration_fingerprint"],
                    "revision": conn.execute("SELECT revision FROM library_analysis_clock WHERE id=1").fetchone()[0]}

    def fail_library_analysis_policy(self, reason):
        with self.transaction() as conn:
            conn.execute("UPDATE library_analysis_policy SET enabled=0,reason=?,version=version+1,updated_at=? WHERE enabled=1", (reason, c.stamp()))

    def next_library_analysis_target(self):
        with self.transaction() as conn:
            # Complete discovery independently of whether individual works succeeded.
            conn.execute("UPDATE library_analysis_batches SET status='completed',version=version+1,updated_at=? "
                         "WHERE status='waiting' AND NOT EXISTS(SELECT 1 FROM library_analysis_targets t "
                         "JOIN work_analysis_runs r ON r.run_id=t.run_id WHERE t.batch_id=library_analysis_batches.batch_id "
                         "AND t.status='created' AND r.deleted_at IS NULL AND (r.status IN ('queued','running','pausing','cancelling') "
                         "OR (r.status='paused' AND r.reason='sync_yield')))", (c.stamp(),))
            batch = conn.execute("SELECT * FROM library_analysis_batches WHERE status IN ('queued','scanning') ORDER BY created_at,rowid LIMIT 1").fetchone()
            if batch is None:
                return None
            configuration = c.load(batch["configuration_json"])
            if conn.execute("SELECT 1 FROM library_analysis_targets t JOIN work_analysis_runs r ON r.run_id=t.run_id "
                            "WHERE t.batch_id=? AND t.status='created' AND r.deleted_at IS NULL "
                            "AND (r.status IN ('queued','running','pausing','cancelling') OR (r.status='paused' AND r.reason='sync_yield')) LIMIT 1",
                            (batch["batch_id"],)).fetchone():
                return None
            if batch["origin"] == "policy":
                policy = conn.execute("SELECT * FROM library_analysis_policy WHERE id=1").fetchone()
                if not policy["enabled"] or policy["configuration_fingerprint"] != batch["configuration_fingerprint"]:
                    conn.execute("UPDATE library_analysis_batches SET status='paused',reason='policy_disabled',version=version+1 WHERE batch_id=?", (batch["batch_id"],))
                    return None
            candidate = conn.execute(
                "SELECT s.source_kind,s.source_id,s.revision FROM library_analysis_sources s WHERE s.revision>? AND s.revision<=? "
                "AND (s.source_kind>? OR (s.source_kind=? AND s.source_id>?)) AND " + ELIGIBLE +
                " ORDER BY s.source_kind,s.source_id LIMIT 1",
                (batch["min_revision"], batch["max_revision"], batch["cursor_kind"], batch["cursor_kind"], batch["cursor_id"]),
            ).fetchone()
            if candidate is None:
                conn.execute("UPDATE library_analysis_batches SET status='completed',reason='scan_complete',version=version+1,updated_at=? WHERE batch_id=?", (c.stamp(), batch["batch_id"]))
                return None
            try:
                digest = _source_identity(conn, candidate["source_kind"], candidate["source_id"])
            except WorkAnalysisError:
                digest = fingerprint({"source_kind": candidate["source_kind"], "source_id": candidate["source_id"], "unavailable": True})
            return {"batch_id": batch["batch_id"], "expected_version": batch["version"], "configuration": configuration,
                    "configuration_fingerprint": batch["configuration_fingerprint"], "content_fingerprint": digest,
                    "selection": {"source_kind": candidate["source_kind"], "source_id": candidate["source_id"], "publication_state": "unknown"}}

    def commit_library_analysis_target(self, item, *, prepared=None, error=None):
        """Return False only when this candidate needs an ordinary work preview."""
        with self.transaction() as conn:
            batch = conn.execute("SELECT * FROM library_analysis_batches WHERE batch_id=?", (item["batch_id"],)).fetchone()
            if batch is None or batch["version"] != item["expected_version"] or batch["status"] not in {"queued", "scanning"}:
                raise WorkAnalysisError("version_conflict", 409)
            configuration = c.load(batch["configuration_json"])
            current = work_configuration_identity(conn, configuration["agent_id"], configuration["output_tokens"])
            if current["configuration_fingerprint"] != batch["configuration_fingerprint"]:
                raise WorkAnalysisError("configuration_changed", 409)
            kind, source_id = item["selection"]["source_kind"], item["selection"]["source_id"]
            try:
                digest = _source_identity(conn, kind, source_id)
            except WorkAnalysisError:
                digest = fingerprint({"source_kind": kind, "source_id": source_id, "unavailable": True})
            if digest != item["content_fingerprint"]:
                raise WorkAnalysisError("source_changed", 409)
            run_id, status, reason = None, None, None
            existing = conn.execute(
                "SELECT run_id,status FROM library_analysis_targets WHERE source_kind=? AND source_id=? "
                "AND content_fingerprint=? AND configuration_fingerprint=? ORDER BY rowid LIMIT 1",
                (kind, source_id, digest, batch["configuration_fingerprint"]),
            ).fetchone()
            if existing is not None:
                # Reusing or observing a manual run is also a durable decision
                # for this source/configuration. Deleting that report later must
                # not turn its next scan into a new paid analysis. Ordinary
                # previews never create target rows and do not establish a veto.
                run_id, status, reason = existing["run_id"], "skipped", "already_attempted"
            else:
                # Manual successes can satisfy the automatic request, using source
                # joins rather than reading a potentially sensitive report body.
                reports = conn.execute(
                    "SELECT run_id,status,report_stale,archive_outdated,report_json IS NOT NULL AS has_report "
                    "FROM work_analysis_runs WHERE source_kind=? AND source_id=? AND deleted_at IS NULL "
                    "AND configuration_fingerprint=? ORDER BY created_at DESC LIMIT 100", (kind, source_id, batch["configuration_fingerprint"]),
                ).fetchall()
                for report in reports:
                    chapters = [dict(row) for row in conn.execute("SELECT novel_id,text_hash,has_text FROM work_analysis_sources WHERE run_id=? ORDER BY position", (report["run_id"],))]
                    if content_identity({"source_kind": kind, "source_id": source_id, "chapters": chapters}) == digest:
                        valid = report["status"] == "succeeded" and report["has_report"] and not report["report_stale"] and not report["archive_outdated"]
                        run_id, status, reason = report["run_id"], "reused" if valid else "skipped", "valid_report" if valid else "existing_work"
                        break
            if status is None and error is not None:
                status, reason = "error", error
            if status is None and prepared is None:
                return False
            if status is None:
                preview, captured_config = prepared
                if captured_config["configuration_fingerprint"] != batch["configuration_fingerprint"] or content_identity(preview["manifest"]) != digest:
                    raise WorkAnalysisError("source_changed", 409)
                policy = conn.execute("SELECT * FROM library_analysis_policy WHERE id=1").fetchone() if batch["origin"] == "policy" else None
                if policy is not None and (not policy["enabled"] or policy["configuration_fingerprint"] != batch["configuration_fingerprint"]):
                    raise WorkAnalysisError("policy_disabled", 409)
                authority = policy if policy is not None else batch
                authority_config = c.load(authority["configuration_json"])
                work_budget = configuration["work_budget"]
                exceeds = authority["work_count"] >= authority_config["work_limit"] or any(
                    authority["allocated_" + key] + work_budget[key] > authority_config["batch_budget"][key] for key in BUDGET_KEYS)
                if exceeds:
                    conn.execute("UPDATE library_analysis_batches SET status='waiting',reason='allocation_exhausted',version=version+1,updated_at=? WHERE batch_id=?", (c.stamp(), batch["batch_id"]))
                    if policy is not None:
                        self.fail_library_analysis_policy("allocation_exhausted")
                    return True
                # Database.transaction joins the same IMMEDIATE transaction. A
                # failure in either run creation or bookkeeping rolls back both.
                run = self.create_work_analysis(
                    preview["selection"], preview["manifest"], preview["plan"], captured_config,
                    work_budget, configuration["confirmations"],
                    fingerprint({"batch": batch["batch_id"], "kind": kind, "id": source_id, "content": digest})[:32],
                    preview["preview_fingerprint"],
                )
                run_id, status = run["run_id"], "created"
                for table, where, args in [("library_analysis_batches", "batch_id=?", (batch["batch_id"],))] + (
                        [("library_analysis_policy", "id=1", ())] if policy is not None else []):
                    conn.execute("UPDATE " + table + " SET allocated_request_limit=allocated_request_limit+?,"
                                 "allocated_token_limit=allocated_token_limit+?,allocated_active_seconds_limit=allocated_active_seconds_limit+?,"
                                 "work_count=work_count+1 WHERE " + where, tuple(work_budget[key] for key in BUDGET_KEYS) + args)
            conn.execute(
                "INSERT INTO library_analysis_targets(target_id,batch_id,source_kind,source_id,content_fingerprint,configuration_fingerprint,run_id,status,reason) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, batch["batch_id"], kind, source_id, digest, batch["configuration_fingerprint"], run_id, status, reason),
            )
            conn.execute("UPDATE library_analysis_batches SET status='scanning',cursor_kind=?,cursor_id=?,scanned_count=scanned_count+1,"
                         "skipped_count=skipped_count+?,version=version+1,updated_at=? WHERE batch_id=?",
                         (kind, source_id, int(status != "created"), c.stamp(), batch["batch_id"]))
            return True

    def stop_library_analysis_discovery(self, batch_id, reason):
        with self.transaction() as conn:
            row = conn.execute("SELECT origin FROM library_analysis_batches WHERE batch_id=?", (batch_id,)).fetchone()
            conn.execute("UPDATE library_analysis_batches SET status='failed',reason=?,version=version+1,updated_at=? "
                         "WHERE batch_id=? AND status IN ('queued','scanning')", (reason, c.stamp(), batch_id))
            if row is not None and row["origin"] == "policy":
                self.fail_library_analysis_policy(reason)

    def guard_library_analysis_budget(self, conn, run_id, budget, previous):
        target = conn.execute("SELECT batch_id FROM library_analysis_targets WHERE run_id=? AND status='created' LIMIT 1", (run_id,)).fetchone()
        if target is None or budget == previous:
            return
        batch = conn.execute("SELECT * FROM library_analysis_batches WHERE batch_id=?", (target["batch_id"],)).fetchone()
        delta = {key: budget[key] - previous[key] for key in BUDGET_KEYS}
        policy = conn.execute("SELECT * FROM library_analysis_policy WHERE id=1").fetchone() if batch["origin"] == "policy" else None
        authority = policy if policy is not None else batch
        limit = c.load(authority["configuration_json"])["batch_budget"]
        if any(authority["allocated_" + key] + delta[key] > limit[key] for key in BUDGET_KEYS):
            raise WorkAnalysisError("library_budget_exhausted", 409)
        for table, where, args in [("library_analysis_batches", "batch_id=?", (batch["batch_id"],))] + (
                [("library_analysis_policy", "id=1", ())] if policy is not None else []):
            conn.execute("UPDATE " + table + " SET allocated_request_limit=allocated_request_limit+?,"
                         "allocated_token_limit=allocated_token_limit+?,allocated_active_seconds_limit=allocated_active_seconds_limit+?,"
                         "version=version+1,updated_at=? WHERE " + where,
                         tuple(delta[key] for key in BUDGET_KEYS) + (c.stamp(),) + args)

    def guard_library_analysis_send(self, conn, run_id):
        """Fence new sends while the coordinator finishes a durable batch stop.

        This deliberately does not guard report publication: a worker already
        holding the shared finalization claim retains its existing right to save.
        Resuming the batch explicitly changes the batch state before the original
        work resumes; its existing allocation remains unchanged.
        """
        if not conn.in_transaction:
            raise RuntimeError("library send guard requires the work transaction")
        stopped = conn.execute(
            "SELECT 1 FROM library_analysis_targets t JOIN library_analysis_batches b ON b.batch_id=t.batch_id "
            "WHERE t.run_id=? AND t.status='created' AND t.held=1 AND b.status IN ('paused','cancelled','failed') LIMIT 1",
            (run_id,),
        ).fetchone()
        if stopped is not None:
            raise WorkAnalysisError("work_stopping", 409)

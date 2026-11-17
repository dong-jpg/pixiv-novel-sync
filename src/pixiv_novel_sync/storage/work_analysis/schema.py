from __future__ import annotations

import sqlite3


def invalidate_sql(affected: str) -> str:
    return f"""
        UPDATE work_analysis_runs
        SET status=CASE WHEN status='succeeded' THEN status ELSE 'invalidated' END,
            reason=CASE WHEN status='succeeded' THEN reason ELSE 'source_changed' END,
            report_stale=CASE WHEN status='succeeded' THEN 1 ELSE report_stale END,
            invalidated_at=COALESCE(invalidated_at,CURRENT_TIMESTAMP),
            epoch=epoch+1, owner_token=NULL, lease_until=NULL, heartbeat_at=NULL,
            slice_settled=1, slice_seconds=0, slice_deadline_at=NULL,
            version=version+1, updated_at=CURRENT_TIMESTAMP
        WHERE deleted_at IS NULL
          AND ((status='succeeded' AND report_stale=0)
               OR (status<>'succeeded' AND invalidated_at IS NULL))
          AND run_id IN ({affected})
    """


def tombstone_sql(affected: str, *, source_deleted: bool) -> str:
    status = "invalidated" if source_deleted else "cancelled"
    reason = "source_deleted" if source_deleted else "deleted"
    return f"""
        UPDATE work_analysis_runs
        SET status=CASE WHEN status='succeeded' THEN status ELSE '{status}' END,
            reason='{reason}', deleted_at=CURRENT_TIMESTAMP,
            invalidated_at=COALESCE(invalidated_at,CURRENT_TIMESTAMP),
            epoch=epoch+1, owner_token=NULL, lease_until=NULL, heartbeat_at=NULL,
            slice_settled=1, slice_seconds=0, slice_deadline_at=NULL,
            version=version+1, updated_at=CURRENT_TIMESTAMP
        WHERE deleted_at IS NULL AND run_id IN ({affected})
    """


DDL = (
    """CREATE TABLE IF NOT EXISTS work_analysis_runs (
        run_id TEXT PRIMARY KEY CHECK(length(run_id)=32 AND run_id NOT GLOB '*[^0-9a-f]*'),
        idempotency_key_hash TEXT NOT NULL UNIQUE
            CHECK(length(idempotency_key_hash)=64 AND idempotency_key_hash NOT GLOB '*[^0-9a-f]*'),
        request_identity_hash TEXT NOT NULL
            CHECK(length(request_identity_hash)=64 AND request_identity_hash NOT GLOB '*[^0-9a-f]*'),
        source_kind TEXT CHECK(source_kind IN ('novel','series')),
        source_id INTEGER CHECK(source_id IS NULL OR (typeof(source_id)='integer' AND source_id>0)),
        selection_json TEXT CHECK(selection_json IS NULL OR length(CAST(selection_json AS BLOB))<=65536),
        manifest_json TEXT CHECK(manifest_json IS NULL OR length(CAST(manifest_json AS BLOB))<=2097152),
        manifest_fingerprint TEXT CHECK(manifest_fingerprint IS NULL OR
            (length(manifest_fingerprint)=64 AND manifest_fingerprint NOT GLOB '*[^0-9a-f]*')),
        plan_json TEXT CHECK(plan_json IS NULL OR length(CAST(plan_json AS BLOB))<=8388608),
        configuration_json TEXT CHECK(configuration_json IS NULL OR length(CAST(configuration_json AS BLOB))<=262144),
        configuration_fingerprint TEXT CHECK(configuration_fingerprint IS NULL OR
            (length(configuration_fingerprint)=64 AND configuration_fingerprint NOT GLOB '*[^0-9a-f]*')),
        confirmations_json TEXT CHECK(confirmations_json IS NULL OR length(CAST(confirmations_json AS BLOB))<=1024),
        preview_fingerprint TEXT CHECK(preview_fingerprint IS NULL OR
            (length(preview_fingerprint)=64 AND preview_fingerprint NOT GLOB '*[^0-9a-f]*')),
        status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN
            ('queued','running','pausing','paused','cancelling','cancelled','failed','invalidated','succeeded')),
        reason TEXT CHECK(reason IN
            ('user_pause','sync_yield','budget_exhausted','process_interrupted','source_changed',
             'source_deleted','configuration_changed','validation_failed','planning_failed',
             'provider_failed','unexpected_error','deleted','user_cancel','transport_failed',
             'execution_failed','graph_invalid','synthesis_capacity_exceeded',
             'legacy_repreview_required','invalidated','work_deleted')),
        version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1),
        epoch INTEGER NOT NULL DEFAULT 0 CHECK(typeof(epoch)='integer' AND epoch>=0),
        shared_job_id TEXT UNIQUE CHECK(shared_job_id IS NULL OR
            (length(shared_job_id)=32 AND shared_job_id NOT GLOB '*[^0-9a-f]*')),
        shared_log_id INTEGER UNIQUE CHECK(shared_log_id IS NULL OR
            (typeof(shared_log_id)='integer' AND shared_log_id>0)),
        shared_epoch INTEGER CHECK(shared_epoch IS NULL OR
            (typeof(shared_epoch)='integer' AND shared_epoch BETWEEN 1 AND epoch)),
        owner_token TEXT CHECK(owner_token IS NULL OR length(owner_token)=64),
        heartbeat_at TEXT,
        lease_until TEXT,
        slice_deadline_at TEXT,
        slice_seconds INTEGER NOT NULL DEFAULT 0
            CHECK(typeof(slice_seconds)='integer' AND slice_seconds BETWEEN 0 AND 1800),
        slice_settled INTEGER NOT NULL DEFAULT 1 CHECK(slice_settled IN (0,1)),
        request_limit INTEGER NOT NULL CHECK(typeof(request_limit)='integer' AND request_limit BETWEEN 1 AND 512),
        token_limit INTEGER NOT NULL CHECK(typeof(token_limit)='integer' AND token_limit BETWEEN 1 AND 100000000),
        active_seconds_limit INTEGER NOT NULL
            CHECK(typeof(active_seconds_limit)='integer' AND active_seconds_limit BETWEEN 30 AND 14400),
        request_count INTEGER NOT NULL DEFAULT 0
            CHECK(typeof(request_count)='integer' AND request_count BETWEEN 0 AND request_limit),
        planned_tokens INTEGER NOT NULL DEFAULT 0
            CHECK(typeof(planned_tokens)='integer' AND planned_tokens BETWEEN 0 AND token_limit),
        active_seconds_debited REAL NOT NULL DEFAULT 0
            CHECK(active_seconds_debited>=0 AND active_seconds_debited<=active_seconds_limit),
        budget_history_json TEXT NOT NULL DEFAULT '[]'
            CHECK(length(CAST(budget_history_json AS BLOB))<=65536),
        coverage_json TEXT CHECK(coverage_json IS NULL OR length(CAST(coverage_json AS BLOB))<=65536),
        report_json TEXT CHECK(report_json IS NULL OR length(CAST(report_json AS BLOB))<=1048576),
        report_stale INTEGER NOT NULL DEFAULT 0 CHECK(report_stale IN (0,1)),
        archive_outdated INTEGER NOT NULL DEFAULT 0 CHECK(archive_outdated IN (0,1)),
        invalidated_at TEXT,
        deleted_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at TEXT,
        CHECK((shared_job_id IS NULL AND shared_log_id IS NULL AND shared_epoch IS NULL)
              OR (shared_job_id IS NOT NULL AND shared_epoch IS NOT NULL)),
        CHECK(deleted_at IS NOT NULL OR
            (source_kind IS NOT NULL AND source_id IS NOT NULL AND selection_json IS NOT NULL
             AND manifest_json IS NOT NULL AND manifest_fingerprint IS NOT NULL AND plan_json IS NOT NULL
             AND configuration_json IS NOT NULL AND configuration_fingerprint IS NOT NULL
             AND confirmations_json IS NOT NULL AND preview_fingerprint IS NOT NULL)),
        CHECK(slice_settled=1 OR
            (owner_token IS NOT NULL AND lease_until IS NOT NULL AND slice_deadline_at IS NOT NULL AND slice_seconds>0))
    )""",
    """CREATE TABLE IF NOT EXISTS work_analysis_sources (
        run_id TEXT NOT NULL REFERENCES work_analysis_runs(run_id) ON DELETE CASCADE,
        novel_id INTEGER NOT NULL CHECK(typeof(novel_id)='integer' AND novel_id>0),
        position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 2047),
        text_hash TEXT CHECK(text_hash IS NULL OR (length(text_hash)=64 AND text_hash NOT GLOB '*[^0-9a-f]*')),
        chars INTEGER NOT NULL CHECK(typeof(chars)='integer' AND chars>=0),
        utf8_bytes INTEGER NOT NULL CHECK(typeof(utf8_bytes)='integer' AND utf8_bytes BETWEEN 0 AND 67108864),
        has_text INTEGER NOT NULL CHECK(has_text IN (0,1)),
        PRIMARY KEY(run_id,novel_id), UNIQUE(run_id,position)
    )""",
    """CREATE TABLE IF NOT EXISTS work_analysis_units (
        run_id TEXT NOT NULL REFERENCES work_analysis_runs(run_id) ON DELETE CASCADE,
        unit_id TEXT NOT NULL CHECK(length(unit_id)=64 AND unit_id NOT GLOB '*[^0-9a-f]*'),
        ordinal INTEGER NOT NULL CHECK(typeof(ordinal)='integer' AND ordinal BETWEEN 0 AND 511),
        kind TEXT NOT NULL CHECK(kind IN ('whole','extract','reduce')),
        level INTEGER NOT NULL CHECK(typeof(level)='integer' AND level BETWEEN 0 AND 8),
        is_final INTEGER NOT NULL CHECK(is_final IN (0,1)),
        unit_json TEXT CHECK(unit_json IS NULL OR length(CAST(unit_json AS BLOB))<=1048576),
        input_fingerprint TEXT CHECK(input_fingerprint IS NULL OR
            (length(input_fingerprint)=64 AND input_fingerprint NOT GLOB '*[^0-9a-f]*')),
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','succeeded','discarded')),
        try_no INTEGER NOT NULL DEFAULT 0 CHECK(typeof(try_no)='integer' AND try_no BETWEEN 0 AND 512),
        owner_epoch INTEGER CHECK(owner_epoch IS NULL OR (typeof(owner_epoch)='integer' AND owner_epoch>=0)),
        child_job_id TEXT UNIQUE CHECK(child_job_id IS NULL OR length(child_job_id) BETWEEN 1 AND 128),
        artifact_json TEXT CHECK(artifact_json IS NULL OR length(CAST(artifact_json AS BLOB))<=131072),
        artifact_hash TEXT CHECK(artifact_hash IS NULL OR (length(artifact_hash)=64 AND artifact_hash NOT GLOB '*[^0-9a-f]*')),
        actual_target_json TEXT CHECK(actual_target_json IS NULL OR length(CAST(actual_target_json AS BLOB))<=4096),
        last_error TEXT CHECK(last_error IN
            ('user_pause','sync_yield','budget_exhausted','process_interrupted','source_changed',
             'source_deleted','configuration_changed','validation_failed','planning_failed',
             'provider_failed','unexpected_error','deleted','user_cancel','transport_failed',
             'execution_failed','graph_invalid','synthesis_capacity_exceeded',
             'legacy_repreview_required','invalidated','work_deleted')),
        completed_at TEXT,
        PRIMARY KEY(run_id,unit_id), UNIQUE(run_id,ordinal),
        CHECK(status<>'succeeded' OR (artifact_json IS NOT NULL AND artifact_hash IS NOT NULL AND actual_target_json IS NOT NULL)),
        CHECK(status='discarded' OR (unit_json IS NOT NULL AND input_fingerprint IS NOT NULL))
    )""",
    """CREATE TABLE IF NOT EXISTS work_analysis_requests (
        child_job_id TEXT NOT NULL CHECK(length(child_job_id) BETWEEN 1 AND 128),
        request_ordinal INTEGER NOT NULL
            CHECK(typeof(request_ordinal)='integer' AND request_ordinal BETWEEN 0 AND 32),
        entry_kind TEXT NOT NULL CHECK(entry_kind IN ('attachment','request')),
        run_id TEXT NOT NULL,
        unit_id TEXT NOT NULL,
        epoch INTEGER NOT NULL CHECK(typeof(epoch)='integer' AND epoch>=1),
        try_no INTEGER NOT NULL CHECK(typeof(try_no)='integer' AND try_no BETWEEN 1 AND 512),
        configuration_fingerprint TEXT NOT NULL
            CHECK(length(configuration_fingerprint)=64 AND configuration_fingerprint NOT GLOB '*[^0-9a-f]*'),
        child_owner_hash TEXT NOT NULL CHECK(length(child_owner_hash)=64 AND child_owner_hash NOT GLOB '*[^0-9a-f]*'),
        planned_tokens INTEGER CHECK(planned_tokens IS NULL OR
            (typeof(planned_tokens)='integer' AND planned_tokens BETWEEN 1 AND 100000000)),
        usage_json TEXT CHECK(usage_json IS NULL OR length(CAST(usage_json AS BLOB))<=16384),
        reserved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        reported_at TEXT,
        PRIMARY KEY(child_job_id,request_ordinal),
        FOREIGN KEY(run_id,unit_id) REFERENCES work_analysis_units(run_id,unit_id) ON DELETE CASCADE,
        CHECK((entry_kind='attachment' AND request_ordinal=0 AND planned_tokens IS NULL AND usage_json IS NULL)
              OR (entry_kind='request' AND request_ordinal>0 AND planned_tokens IS NOT NULL AND usage_json IS NOT NULL))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_work_analysis_sources_novel ON work_analysis_sources(novel_id,run_id)",
    "CREATE INDEX IF NOT EXISTS idx_work_analysis_runs_queue ON work_analysis_runs(status,reason,created_at,run_id) WHERE deleted_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_work_analysis_runs_source ON work_analysis_runs(source_kind,source_id) WHERE deleted_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_work_analysis_runs_lease ON work_analysis_runs(lease_until) WHERE owner_token IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_work_analysis_requests_run ON work_analysis_requests(run_id,unit_id,request_ordinal)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_work_analysis_child_try ON work_analysis_requests(run_id,unit_id,try_no) WHERE request_ordinal=0",
    """CREATE TRIGGER IF NOT EXISTS work_analysis_clear_tombstone
        AFTER UPDATE OF deleted_at ON work_analysis_runs
        WHEN OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL
        BEGIN
            UPDATE work_analysis_units
            SET status='discarded',unit_json=NULL,input_fingerprint=NULL,
                artifact_json=NULL,artifact_hash=NULL,actual_target_json=NULL,
                child_job_id=NULL,owner_epoch=NULL,last_error=NULL
            WHERE run_id=NEW.run_id;
            DELETE FROM work_analysis_sources WHERE run_id=NEW.run_id;
            UPDATE work_analysis_runs
            SET source_kind=NULL,source_id=NULL,selection_json=NULL,manifest_json=NULL,
                manifest_fingerprint=NULL,plan_json=NULL,configuration_json=NULL,
                configuration_fingerprint=NULL,confirmations_json=NULL,preview_fingerprint=NULL,
                coverage_json=NULL,report_json=NULL
            WHERE run_id=NEW.run_id;
        END""",
    """CREATE TRIGGER IF NOT EXISTS work_analysis_reject_tombstone_revival
        BEFORE UPDATE ON work_analysis_runs
        WHEN OLD.deleted_at IS NOT NULL AND
            (NEW.deleted_at IS NULL OR NEW.manifest_json IS NOT NULL OR NEW.plan_json IS NOT NULL
             OR NEW.selection_json IS NOT NULL OR NEW.report_json IS NOT NULL
             OR NEW.configuration_json IS NOT NULL OR NEW.confirmations_json IS NOT NULL
             OR NEW.source_id IS NOT NULL OR NEW.owner_token IS NOT NULL)
        BEGIN SELECT RAISE(ABORT,'work analysis is deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS work_analysis_reject_deleted_source
        BEFORE INSERT ON work_analysis_sources
        WHEN EXISTS(SELECT 1 FROM work_analysis_runs WHERE run_id=NEW.run_id AND deleted_at IS NOT NULL)
        BEGIN SELECT RAISE(ABORT,'work analysis is deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS work_analysis_reject_deleted_unit_insert
        BEFORE INSERT ON work_analysis_units
        WHEN EXISTS(SELECT 1 FROM work_analysis_runs WHERE run_id=NEW.run_id AND deleted_at IS NOT NULL)
        BEGIN SELECT RAISE(ABORT,'work analysis is deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS work_analysis_reject_deleted_unit_update
        BEFORE UPDATE ON work_analysis_units
        WHEN (NEW.unit_json IS NOT NULL OR NEW.artifact_json IS NOT NULL OR NEW.actual_target_json IS NOT NULL)
          AND EXISTS(SELECT 1 FROM work_analysis_runs WHERE run_id=NEW.run_id AND deleted_at IS NOT NULL)
        BEGIN SELECT RAISE(ABORT,'work analysis is deleted'); END""",
)


def _source_triggers() -> tuple[str, ...]:
    changed_update = invalidate_sql(
        "SELECT run_id FROM work_analysis_sources WHERE novel_id=NEW.novel_id "
        "AND (text_hash IS NOT NEW.text_hash OR OLD.text_raw IS NOT NEW.text_raw)"
    )
    changed_insert = invalidate_sql(
        "SELECT run_id FROM work_analysis_sources WHERE novel_id=NEW.novel_id AND text_hash IS NOT NEW.text_hash"
    )
    deleted_novel = tombstone_sql(
        "SELECT run_id FROM work_analysis_sources WHERE novel_id=OLD.novel_id", source_deleted=True
    )
    deleted_series = tombstone_sql(
        "SELECT run_id FROM work_analysis_runs WHERE source_kind='series' AND source_id=OLD.series_id",
        source_deleted=True,
    )
    deleted_user = tombstone_sql(
        "SELECT s.run_id FROM novels n JOIN work_analysis_sources s ON s.novel_id=n.novel_id "
        "WHERE n.user_id=OLD.user_id UNION SELECT r.run_id FROM series v "
        "JOIN work_analysis_runs r ON r.source_kind='series' AND r.source_id=v.series_id "
        "WHERE v.user_id=OLD.user_id", source_deleted=True,
    )
    outdated = """
        UPDATE work_analysis_runs SET archive_outdated=1,version=version+1,updated_at=CURRENT_TIMESTAMP
        WHERE source_kind='series' AND source_id=NEW.series_id AND deleted_at IS NULL AND archive_outdated=0
          AND NOT EXISTS(SELECT 1 FROM work_analysis_sources s
                         WHERE s.run_id=work_analysis_runs.run_id AND s.novel_id=NEW.novel_id)
    """
    return (
        f"""CREATE TRIGGER IF NOT EXISTS work_analysis_text_changed
            AFTER UPDATE OF text_raw,text_hash ON novel_texts
            WHEN OLD.text_raw IS NOT NEW.text_raw OR OLD.text_hash IS NOT NEW.text_hash
            BEGIN {changed_update}; END""",
        f"""CREATE TRIGGER IF NOT EXISTS work_analysis_text_inserted AFTER INSERT ON novel_texts
            BEGIN {changed_insert}; END""",
        f"""CREATE TRIGGER IF NOT EXISTS work_analysis_text_deleted BEFORE DELETE ON novel_texts
            BEGIN {deleted_novel}; END""",
        f"""CREATE TRIGGER IF NOT EXISTS work_analysis_novel_deleted BEFORE DELETE ON novels
            BEGIN {deleted_novel}; END""",
        f"""CREATE TRIGGER IF NOT EXISTS work_analysis_series_deleted BEFORE DELETE ON series
            BEGIN {deleted_series}; END""",
        f"""CREATE TRIGGER IF NOT EXISTS work_analysis_user_deleted BEFORE DELETE ON users
            BEGIN {deleted_user}; END""",
        f"""CREATE TRIGGER IF NOT EXISTS work_analysis_series_extended AFTER INSERT ON novels
            WHEN NEW.series_id IS NOT NULL BEGIN {outdated}; END""",
        f"""CREATE TRIGGER IF NOT EXISTS work_analysis_series_member_added AFTER UPDATE OF series_id ON novels
            WHEN NEW.series_id IS NOT NULL AND OLD.series_id IS NOT NEW.series_id BEGIN {outdated}; END""",
    )


def migrate_work_analysis(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise RuntimeError("work analysis migration requires its caller's transaction")
    for statement in (*DDL, *_source_triggers()):
        conn.execute(statement)

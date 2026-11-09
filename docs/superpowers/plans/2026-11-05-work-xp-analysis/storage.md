# Tasks 3A-3B: Durable Business Storage


Part of the [implementation plan](../2026-11-05-work-xp-analysis.md); application execution is separately authorized.

### Task 3A: Add Four-Table Schema, Idempotent Creation, and Explicit Read Projections

**Files:**
- Create: `src/pixiv_novel_sync/storage/work_analysis/schema.py` (additive DDL and transactional source/delete triggers).
- Create: `src/pixiv_novel_sync/storage/work_analysis/common.py` (bounded serialization, identity/lease guards, and usage projection).
- Create: `src/pixiv_novel_sync/storage/work_analysis/runs.py` (idempotency-first creation and frozen-plan persistence).
- Create: `src/pixiv_novel_sync/storage/work_analysis/queries.py` (explicit public/private projections, report, and evidence access).
- Create: `src/pixiv_novel_sync/storage/work_analysis/__init__.py` (mixin composition only).
- Modify: `src/pixiv_novel_sync/storage/schema.py:171` (run the new additive migration after core foreign-key rebuilds).
- Modify: `src/pixiv_novel_sync/storage_db.py:24` (import and compose `WorkAnalysisMixin`).
- Test: `tests/test_work_analysis_storage.py` (small synthetic database fixtures; no benchmark fixture imports).

**Interfaces and invariants:**
- Consume Task 1/2's strict domain contracts, source projection, immutable unit plans, normalized artifacts, and pure configuration identity. No Provider output dictionary reaches storage without validation.
- Produce the storage methods named in the shared contract. Also produce `lookup_work_analysis_idempotency(idempotency_key, request_identity) -> Json | None` so an HTTP retry can discover an already committed result before re-running a now-stale preview.
- `recover_work_analyses(*, now, process_start=False)` accepts the added startup flag. Only the single Web owner's startup calls `process_start=True`; a normal expiry sweep never releases a `JobManager` slot.
- `claim_work_analysis(run_id, *, now, slice_seconds, shared_job_id=None, shared_log_id=None)` atomically binds an actual activation to its shared job, exact existing task-log row, and claimed epoch. The bounded association is private audit metadata in the existing runs table, not a fifth table, new queue, or foreign key to prunable generic logs. Runtime supplies the job ID and its optional log ID; isolated storage/no-log runners may omit them.
- `reconcile_work_analysis_task_logs() -> int` runs only during sole-owner startup, after generic stale-log cleanup and `recover_work_analyses(process_start=True)`. It repairs only the latest activation's exact existing work-analysis log from a committed successful parent; it neither recreates pruned logs, changes older cancelled slices, copies business content, nor activates paid work.
- `get_work_analysis_control(run_id) -> dict | None` is an internal, explicitly narrow control projection for Task 6's same-owner `sync_yield` CAS retry. It is never an HTTP response and never loads artifacts, reports, configuration JSON, or usage rows.
- `request_work_analysis_stop` accepts `pause`, `cancel`, `sync_yield`, and `delete`; `sync_yield` is server-only. An active shared job's cancellation permission must already have been acquired by Task 6 before this method is called.
- A request-table row with `request_ordinal=0` is an attachment, not a send. Its `planned_tokens` and `usage_json` are NULL. Only ordinals 1..32 affect request/usage totals. This preserves a child-to-parent link even if the process dies before its first reservation or the unit is retried later, without a fifth table or a dependency on generic AI-job retention.
- `run_id` and submitted idempotency keys are lowercase hexadecimal strings of length 32. Unit/evidence/fingerprint identifiers are lowercase hexadecimal strings of length 64. Public pagination is `{items, total, offset, limit}`.
- All timestamp strings are UTC in `%Y-%m-%d %H:%M:%S` format. Monotonic elapsed seconds are passed only to settlement, never compared to wall-clock timestamps.
- `active_seconds_debited` contains all settled active time plus the entire currently reserved slice. Normal settlement refunds the unused portion once; deletion, source invalidation, and lost-process recovery never refund an unsettled slice.
- The source index is `(novel_id, run_id)`. `work_analysis_sources` deliberately has no foreign key to live novels: source/delete triggers must first identify and revoke affected runs, including a body-less selected chapter, before removing private source references.
- Source and deletion triggers are installed after the existing legacy `novel_texts` foreign-key rebuild. They cover `novel_texts`, direct `novels` deletion, `delete_user`'s bulk novel deletion, direct user deletion, and series deletion. Same-body/same-hash saves and `fetched_at` refreshes are no-ops. Changed raw text with a falsely unchanged stored hash is conservatively invalidated rather than allowed to bypass source identity.
- All internal `_attach`, `_reserve`, `_record`, and source invalidation helpers use the caller's existing connection/transaction. None opens a nested transaction or commits it. Task 4 places them inside the existing generic AI-job transactions.

#### 3.1 Add the Additive Schema and Source/Tombstone Triggers

- [ ] **Write the migration RED tests** in `tests/test_work_analysis_storage.py`:

```python
from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.storage.work_analysis.schema import migrate_work_analysis


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "work-analysis.db")
    database.init_schema()
    try:
        yield database
    finally:
        database.close()


def test_work_schema_is_additive_repeatable_and_foreign_key_clean(db):
    db.conn.execute("INSERT INTO users(user_id,name,raw_json) VALUES(1,'Synthetic author','{}')")
    before = dict(db.conn.execute("SELECT * FROM users WHERE user_id=1").fetchone())
    db.init_schema()
    db.init_schema()
    tables = {
        row[0] for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'work_analysis_%'"
        )
    }
    assert tables == {
        "work_analysis_runs", "work_analysis_sources",
        "work_analysis_units", "work_analysis_requests",
    }
    assert dict(db.conn.execute("SELECT * FROM users WHERE user_id=1").fetchone()) == before
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    indexes = {row[1] for row in db.conn.execute("PRAGMA index_list(work_analysis_sources)")}
    assert "idx_work_analysis_sources_novel" in indexes


def test_work_migration_does_not_commit_its_callers_transaction(db):
    with pytest.raises(sqlite3.OperationalError):
        with db.transaction() as conn:
            conn.execute("CREATE TABLE work_migration_probe(value INTEGER)")
            migrate_work_analysis(conn)
            conn.execute("INSERT INTO absent_work_migration_table VALUES(1)")
    assert db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='work_migration_probe'"
    ).fetchone() is None
```

- [ ] **Run RED**: `python -m pytest tests/test_work_analysis_storage.py -q`. Expected: collection fails because `pixiv_novel_sync.storage.work_analysis` does not exist; do not interpret another failure as the intended RED.

- [ ] **Create `src/pixiv_novel_sync/storage/work_analysis/schema.py` with this complete additive migration.** Execute each statement with `conn.execute`, not `executescript`, because `executescript` can commit the caller's transaction.

```python
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
```

- [ ] **Add the schema call** after `_migrate_core_foreign_keys()` and `_migrate_query_indexes()` in `SchemaMixin.init_schema`, before `_commit_if_needed()`:

```python
        from .work_analysis.schema import migrate_work_analysis

        with self.transaction() as conn:
            migrate_work_analysis(conn)
```

The shared activation columns belong to this first, not-yet-shipped four-table DDL. Existing application tables are untouched, migration remains additive/repeatable, and no new `task_logs` schema/index or foreign key is introduced. Pruning a generic log must not delete or alter the business run. Tombstones retain only these bounded IDs/epoch as safe audit metadata; they still purge all prior private source/artifact fields, and deleted parents are excluded from projection reconciliation.

- [ ] **Create `src/pixiv_novel_sync/storage/work_analysis/__init__.py`** with a module docstring for this first migration-only step:

```python
"""Narrow persistence for explicitly requested work analyses."""
```

- [ ] **Run GREEN**: `python -m pytest tests/test_work_analysis_storage.py tests/test_ensure_schema.py tests/test_schema_rebuild.py tests/test_delete_user_orphans.py -q`. Expected: all pass; zero new tables beyond the four named business tables; existing tests/data stay unchanged. The remaining substeps replace the package docstring-only file with the final mixin composition before Task 3's review gate.

#### 3.2 Add Idempotency-First Creation and Explicit Read Projections

Task 1 provides `manifest.preview_fingerprint(selection,manifest,plan,configuration,budget)`; confirmations are not part of this preview digest, but are part of the original submitted idempotency identity. Do not recompute that identity from a run's subsequently amended budget.

- [ ] **Append the synthetic setup and creation RED tests** to `tests/test_work_analysis_storage.py`:

```python
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError, fingerprint
from pixiv_novel_sync.work_analysis.configuration import snapshot_work_configuration
from pixiv_novel_sync.work_analysis.manifest import capture_manifest, preview_fingerprint, validate_selection
from pixiv_novel_sync.work_analysis.planning import materialize_unit, plan_original_units
from pixiv_novel_sync.work_analysis.evidence import validate_unit_output


def prepare_case(db, *, series=False, request_limit=8, body=None):
    text = body if body is not None else "Synthetic evidence is unique. This remaining synthetic body must not become a replay snapshot."
    db.conn.execute("INSERT INTO users(user_id,name,raw_json) VALUES(1,'Synthetic author','{}')")
    if series:
        db.conn.execute(
            "INSERT INTO series(series_id,title,user_id,total_novels) VALUES(9,'PRIVATE_SERIES_TITLE',1,1)"
        )
    db.conn.execute(
        "INSERT INTO novels(novel_id,user_id,series_id,title,visible,restrict_value,x_restrict,"
        "text_length,total_bookmarks,total_views,tags_json,raw_json,meta_hash,create_date) "
        "VALUES(10,1,?,'PRIVATE_NOVEL_TITLE',1,'public',0,?,0,0,'[]','{}','synthetic-meta','2026-01-01')",
        (9 if series else None, len(text)),
    )
    db.conn.execute(
        "INSERT INTO novel_texts(novel_id,text_raw,has_content,text_hash) VALUES(10,?,1,?)",
        (text, hashlib.sha256(text.encode("utf-8")).hexdigest()),
    )
    provider_id = db.create_ai_provider({
        "name": "Synthetic provider", "provider_type": "openai_compatible",
        "base_url": "https://provider.invalid", "api_key_encrypted": "v2$synthetic-only",
        "default_model": "synthetic-model", "context_window": 128000, "max_retries": 0,
    })
    agent_id = db.create_ai_agent({
        "name": "Synthetic agent", "task_type": "general", "provider_id": provider_id,
        "model": "synthetic-model", "system_prompt": "Analyze only the supplied synthetic work.",
        "context_window": 128000, "max_tokens": 64,
    })
    selection = validate_selection({
        "source_kind": "series" if series else "novel", "source_id": 9 if series else 10,
        "publication_state": "completed",
    })
    budget = {"request_limit": request_limit, "token_limit": 100000, "active_seconds_limit": 600}
    confirmations = {name: True for name in (
        "order_confirmed", "current_archive_only", "external_send_confirmed", "budget_confirmed"
    )}
    with db.read_transaction() as conn:
        manifest, texts = capture_manifest(conn, selection)
        configuration, prompt, preflight = snapshot_work_configuration(conn, agent_id, 64)
    plan = plan_original_units(manifest, texts, preflight, agent_prompt=prompt, output_tokens=64)
    digest = preview_fingerprint(selection, manifest, plan, configuration, budget)
    arguments = {
        "selection": selection, "manifest": manifest, "plan": plan, "configuration": configuration,
        "budget": budget, "confirmations": confirmations, "idempotency_key": "a" * 32,
        "preview_fingerprint": digest,
    }
    identity = {"selection": selection, "agent_id": agent_id, "output_tokens": 64,
                "budget": budget, "confirmations": confirmations, "preview_fingerprint": digest}
    return {"arguments": arguments, "identity": identity, "text": text,
            "texts": texts, "prompt": prompt, "provider_id": provider_id, "agent_id": agent_id}


def create_case(db, **kwargs):
    case = prepare_case(db, **kwargs)
    case["run"] = db.create_work_analysis(**case["arguments"])
    return case


def test_create_replay_precedes_changed_source_and_conflicting_key_is_rejected(db):
    case = create_case(db)
    original = case["run"]
    replacement = "Changed synthetic body."
    db.conn.execute("UPDATE novel_texts SET text_raw=?,text_hash=? WHERE novel_id=10", (
        replacement, hashlib.sha256(replacement.encode("utf-8")).hexdigest(),
    ))
    replay = db.lookup_work_analysis_idempotency("a" * 32, case["identity"])
    assert replay["run_id"] == original["run_id"]
    assert replay["status"] == "invalidated"
    assert db.create_work_analysis(**case["arguments"])["run_id"] == original["run_id"]
    different = {**case["identity"], "output_tokens": 65}
    with pytest.raises(WorkAnalysisError, match="idempotency_conflict"):
        db.lookup_work_analysis_idempotency("a" * 32, different)
    assert db.conn.execute("SELECT COUNT(*) FROM work_analysis_runs").fetchone()[0] == 1


def test_create_rechecks_preview_without_writing_on_conflict(db):
    case = prepare_case(db)
    db.conn.execute("UPDATE novels SET title='Changed title' WHERE novel_id=10")
    with pytest.raises(WorkAnalysisError, match="preview_conflict"):
        db.create_work_analysis(**case["arguments"])
    assert db.conn.execute("SELECT COUNT(*) FROM work_analysis_runs").fetchone()[0] == 0


def test_duplicate_creation_is_one_durable_parent_across_connections(db):
    case = prepare_case(db)
    barrier = Barrier(2)

    def create():
        local = Database(db.path)
        try:
            barrier.wait(timeout=5)
            return local.create_work_analysis(**case["arguments"])["run_id"]
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: create(), range(2)))
    assert ids[0] == ids[1]
    assert db.conn.execute("SELECT COUNT(*) FROM work_analysis_runs").fetchone()[0] == 1


def test_public_detail_and_units_do_not_expose_artifacts_or_owner(db):
    case = create_case(db)
    detail = db.get_work_analysis(case["run"]["run_id"])
    assert detail["manifest"]["chapters"][0]["novel_id"] == 10
    assert detail["coverage"]["ratio"] == 0
    assert detail["usage_summary"]["known_total_tokens"] is None
    encoded = json.dumps(detail)
    for private in ("owner_token", "configuration_fingerprint", "api_key", "artifact_json", case["text"]):
        assert private not in encoded
    listing = db.list_work_analyses(0, 10)
    assert set(listing) == {"items", "total", "offset", "limit"}
    assert "manifest" not in listing["items"][0]
    units = db.list_work_analysis_units(case["run"]["run_id"])
    assert units["total"] == 1 and "artifact" not in units["items"][0]


@pytest.mark.parametrize("field,value", [
    ("request_limit", True), ("request_limit", 0), ("request_limit", 513),
    ("token_limit", 0), ("token_limit", 100000001),
    ("active_seconds_limit", 29), ("active_seconds_limit", 14401),
])
def test_create_budget_bounds_are_strict_and_leave_no_parent(db, field, value):
    case = prepare_case(db)
    args = {**case["arguments"], "budget": {**case["arguments"]["budget"], field: value}}
    with pytest.raises(WorkAnalysisError):
        db.create_work_analysis(**args)
    assert db.conn.execute("SELECT COUNT(*) FROM work_analysis_runs").fetchone()[0] == 0
```

- [ ] **Run RED**: `python -m pytest tests/test_work_analysis_storage.py -k "create or duplicate or public" -q`. Expected: `Database` has no `create_work_analysis`/lookup methods. All fixture preparation is local and uses synthetic strings; no Key decryption or network call is permitted.

- [ ] **Create `src/pixiv_novel_sync/storage/work_analysis/common.py`:**

```python
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from ...ai.usage import summarize_usage
from ...work_analysis.contracts import WorkAnalysisError, WorkLease, canonical_bytes, validate_budget
from ...work_analysis.coverage import coverage
from ...work_analysis.manifest import validate_selection


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
    if unit["kind"] not in {"whole", "extract", "reduce"} or type(unit["is_final"]) is not bool:
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
    if not isinstance(children, list) or len(children) > 16 or len(children) != len(set(children)):
        raise WorkAnalysisError("invalid_request")
    for child in children:
        hex_id(child)
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
    if len({unit["unit_id"] for unit in units}) != len(units):
        raise WorkAnalysisError("invalid_request")
    if plan["strategy"] not in {"whole", "segmented"}:
        raise WorkAnalysisError("invalid_request")
    expected_kind = "whole" if plan["strategy"] == "whole" else "extract"
    if expected_kind == "whole" and len(units) != 1:
        raise WorkAnalysisError("invalid_request")
    for unit in units:
        validate_unit(unit, manifest)
        if unit["kind"] != expected_kind:
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
```

- [ ] **Create `src/pixiv_novel_sync/storage/work_analysis/runs.py`:**

```python
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
    key_hash, stamp, submitted_identity, validate_plan,
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
```

- [ ] **Create `src/pixiv_novel_sync/storage/work_analysis/queries.py`:**

```python
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
```

- [ ] **Replace the package `__init__.py` and compose it into `Database`:**

```python
"""Narrow persistence for explicitly requested work analyses."""
from .queries import WorkAnalysisQueriesMixin
from .runs import WorkAnalysisRunsMixin


class WorkAnalysisMixin(WorkAnalysisRunsMixin, WorkAnalysisQueriesMixin):
    """Database facade for the four work-analysis business tables."""
```

In `src/pixiv_novel_sync/storage_db.py`, add this import after the storage AI imports and this base immediately before `SchemaMixin` in the existing class bases:

```python
from .storage.work_analysis import WorkAnalysisMixin
```

```python
    WorkAnalysisMixin,
```

- [ ] **Run GREEN**: `python -m pytest tests/test_work_analysis_storage.py tests/test_ai_model_pools.py tests/test_ai_invocation_planner.py -q`. Expected: all tests pass. The two creation contenders must return the same ID and leave exactly one parent, and a stale preview with a new key must leave no parent.

- [ ] **Review gate 3A:** verify the four-table migration is additive/repeatable, caller transactions roll back, source triggers use indexed source membership, preview identity excludes observation timestamps, and private configuration/report/owner/shared-activation fields never enter the default projections. The internal control query must select only its named scalar fields. Do not continue with a migration or creation-test failure.

- [ ] **Commit Task 3A only:**

```bash
git add src/pixiv_novel_sync/storage/work_analysis/schema.py src/pixiv_novel_sync/storage/work_analysis/common.py src/pixiv_novel_sync/storage/work_analysis/runs.py src/pixiv_novel_sync/storage/work_analysis/queries.py src/pixiv_novel_sync/storage/work_analysis/__init__.py src/pixiv_novel_sync/storage/schema.py src/pixiv_novel_sync/storage_db.py tests/test_work_analysis_storage.py
git commit -m "feat(work-analysis): persist immutable runs and safe projections"
```

### Task 3B: Enforce Ownership, Atomic Budgeting, Validated Checkpoints, and Publication

**Files:**
- Create: `src/pixiv_novel_sync/storage/work_analysis/control.py` (lease, stop, explicit resume, conservative recovery, and budget amendments).
- Create: `src/pixiv_novel_sync/storage/work_analysis/requests.py` (typed child attachment, atomic send debit, and idempotent normalized usage).
- Create: `src/pixiv_novel_sync/storage/work_analysis/units.py` (unit attempts, immutable derived graph, checkpoint validation, and report publication).
- Modify: `src/pixiv_novel_sync/storage/work_analysis/__init__.py` (compose the completed mixins).
- Test: `tests/test_work_analysis_storage.py` (extend the Task 3A synthetic fixture; never connect a Provider).

**Interfaces:**
- Consume Task 3A's `common.py`, schema, safe projections and `Database.transaction()` (`BEGIN IMMEDIATE`), and Task 1/2's `WorkLease`, `WorkCallBinding`, manifest/configuration/plan/artifact/evidence contracts.
- Produce every ownership/request/checkpoint/control method listed in the shared storage contract. `publish_work_analysis(lease, report)` accepts the validated final **Artifact**; only the server constructs the full report envelope. It does not accept a client-supplied envelope.
- `claim_work_analysis` accepts `queued` and `paused/sync_yield`; this does not grant automatic replay after process recovery, which rewrites old sync-yield intent to `paused/process_interrupted`.
- Shared job/log identity is attached only by a successful claim, in the same transaction as the epoch and time debit. Failed claims cannot replace a prior activation; a new claim cannot reuse that run's previous shared job ID. Settlement/recovery retain the latest association, and publication verifies its epoch before committing parent truth. Only startup reconciliation may repair the matching generic log; older activation logs remain historical facts.
- The three request helpers require an already-open caller transaction. Task 4 places them inside the generic AI child's creation/reservation/usage transactions; this task does not add a second outer reservation transaction.
- Ordinary stop retains the epoch while draining. Heartbeats and normalized usage are still allowed; new attempts, send reservations, derived graph writes, checkpoints and final publication are forbidden. Settlement refunds at most once. Immediate revocation or expired ownership charges the full slice.
- Final artifacts, including whole and final-reduce roots, are always at most 128 KiB. Only the server report envelope has the separate 1 MiB limit. Unit finality is a plan property, never inferred from model output.

#### 3.3 Add Lease, Stop, Resume, Budget, and Recovery State Machines

- [ ] **Append ownership and time-accounting RED tests:**

```python
from pixiv_novel_sync.storage.work_analysis import common as work_common
from pixiv_novel_sync.work_analysis.contracts import WorkCallBinding, WorkLease


@pytest.fixture
def clock(monkeypatch):
    value = [datetime(2026, 11, 5, 0, 0, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(work_common, "utc_now", lambda: value[0])
    return value


def owned_case(db, clock, *, slice_seconds=30, **kwargs):
    case = create_case(db, **kwargs)
    lease = db.claim_work_analysis(case["run"]["run_id"], now=clock[0], slice_seconds=slice_seconds)
    return case, lease


def test_normal_pause_retains_owner_until_one_monotonic_refund(db, clock):
    case, lease = owned_case(db, clock)
    run_id = lease.run_id
    before = db.get_work_analysis(run_id, include_private=True)
    assert before["active_seconds_debited"] == 30
    stopping = db.request_work_analysis_stop(run_id, "pause", before["version"])
    during = db.get_work_analysis(run_id, include_private=True)
    assert stopping["status"] == "pausing" and during["epoch"] == lease.epoch
    assert db.heartbeat_work_analysis(lease, now=clock[0] + timedelta(seconds=5))
    with db.transaction() as conn, pytest.raises(WorkAnalysisError, match="work_stopping"):
        work_common.require_owner(conn, lease)
    db.settle_work_analysis(lease, outcome="failed", reason="transport_failed", elapsed_seconds=5)
    paused = db.get_work_analysis(run_id, include_private=True)
    assert (paused["status"], paused["reason"]) == ("paused", "user_pause")
    assert paused["active_seconds_debited"] == 5 and paused["owner_token"] is None
    assert run_id not in db.queued_work_analysis_ids()
    with pytest.raises(WorkAnalysisError, match="owner_lost"):
        db.settle_work_analysis(lease, outcome="paused", reason="user_pause", elapsed_seconds=0)
    assert db.get_work_analysis(run_id)["active_seconds_debited"] == 5
    queued = db.resume_work_analysis(run_id, paused["version"])
    next_lease = db.claim_work_analysis(run_id, now=clock[0], slice_seconds=30)
    assert queued["status"] == "queued" and next_lease.epoch > lease.epoch


def test_expired_or_lost_process_owner_cannot_refund_or_auto_replay(db, clock):
    case, lease = owned_case(db, clock, slice_seconds=60)
    clock[0] += timedelta(seconds=46)
    assert not db.heartbeat_work_analysis(lease, now=clock[0])
    assert db.recover_work_analyses(now=clock[0]) == 1
    assert db.recover_work_analyses(now=clock[0]) == 0
    state = db.get_work_analysis(lease.run_id, include_private=True)
    assert (state["status"], state["reason"]) == ("paused", "process_interrupted")
    assert state["active_seconds_debited"] == 60 and state["owner_token"] is None
    assert db.queued_work_analysis_ids() == []
    with pytest.raises(WorkAnalysisError, match="owner_lost"):
        db.settle_work_analysis(lease, outcome="paused", reason="user_pause", elapsed_seconds=1)


def test_sync_yield_remains_resumable_but_startup_removes_paid_intent(db, clock):
    case, lease = owned_case(db, clock)
    state = db.get_work_analysis(lease.run_id)
    db.request_work_analysis_stop(lease.run_id, "sync_yield", state["version"])
    db.settle_work_analysis(lease, outcome="paused", reason="sync_yield", elapsed_seconds=5)
    assert db.queued_work_analysis_ids() == [lease.run_id]
    assert db.recover_work_analyses(now=clock[0], process_start=True) == 1
    assert db.queued_work_analysis_ids() == []
    assert db.get_work_analysis(lease.run_id)["reason"] == "process_interrupted"


def test_cancel_is_distinct_and_explicit_resume_preserves_counters(db, clock):
    case, lease = owned_case(db, clock)
    state = db.get_work_analysis(lease.run_id)
    stopping = db.request_work_analysis_stop(lease.run_id, "cancel", state["version"])
    assert stopping["status"] == "cancelling"
    db.settle_work_analysis(lease, outcome="paused", reason="sync_yield", elapsed_seconds=4)
    state = db.get_work_analysis(lease.run_id)
    assert (state["status"], state["reason"]) == ("cancelled", "user_cancel")
    assert db.queued_work_analysis_ids() == []
    resumed = db.resume_work_analysis(lease.run_id, state["version"])
    assert resumed["active_seconds_debited"] == 4


def test_resume_configuration_mismatch_commits_invalidation_before_raising(db, clock):
    case, lease = owned_case(db, clock)
    db.settle_work_analysis(lease, outcome="paused", reason="user_pause", elapsed_seconds=3)
    state = db.get_work_analysis(lease.run_id)
    db.conn.execute("UPDATE ai_agents SET system_prompt='Changed synthetic instruction' WHERE id=?",
                    (case["agent_id"],))
    with pytest.raises(WorkAnalysisError, match="configuration_changed"):
        db.resume_work_analysis(lease.run_id, state["version"])
    invalid = db.get_work_analysis(lease.run_id)
    assert invalid["status"] == "invalidated" and not invalid["can_resume"]
    assert invalid["active_seconds_debited"] == 3


def test_budget_cas_records_amendment_without_changing_idempotency_identity(db, clock):
    case, lease = owned_case(db, clock)
    db.settle_work_analysis(lease, outcome="paused", reason="user_pause", elapsed_seconds=5)
    state = db.get_work_analysis(lease.run_id)
    larger = {"request_limit": 9, "token_limit": 100001, "active_seconds_limit": 601}
    amended = db.update_work_analysis_budget(lease.run_id, larger, state["version"])
    assert amended["budget"] == larger and amended["active_seconds_debited"] == 5
    private = db.get_work_analysis(lease.run_id, include_private=True)
    assert private["budget_history"][0]["old"] == case["arguments"]["budget"]
    assert private["budget_history"][0]["new"] == larger
    assert private["budget_history"][0]["confirmed"] is True
    with pytest.raises(WorkAnalysisError, match="version_conflict"):
        db.update_work_analysis_budget(lease.run_id, larger, state["version"])
    replay = db.lookup_work_analysis_idempotency("a" * 32, case["identity"])
    assert replay["run_id"] == lease.run_id and replay["budget"] == larger


@pytest.mark.parametrize("elapsed", [True, -1, float("nan"), float("inf")])
def test_invalid_elapsed_never_changes_time_debit(db, clock, elapsed):
    case, lease = owned_case(db, clock)
    with pytest.raises(WorkAnalysisError, match="invalid_request"):
        db.settle_work_analysis(lease, outcome="paused", reason="user_pause", elapsed_seconds=elapsed)
    assert db.get_work_analysis(lease.run_id)["active_seconds_debited"] == 30


def test_control_projection_is_narrow_and_never_a_public_owner_view(db, clock):
    case, lease = owned_case(db, clock)
    statements = []
    db.conn.set_trace_callback(statements.append)
    try:
        control = db.get_work_analysis_control(lease.run_id)
    finally:
        db.conn.set_trace_callback(None)
    assert set(control) == {
        "run_id", "version", "status", "reason", "epoch", "owner_token", "deleted_at", "invalidated_at",
        "manifest_fingerprint", "configuration_fingerprint", "request_limit", "token_limit", "active_seconds_limit",
    }
    assert control["owner_token"] == lease.owner_token and control["epoch"] == lease.epoch
    reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 1 and "SELECT *" not in reads[0]
    assert all(name not in reads[0] for name in ("artifact_json", "report_json", "configuration_json", "usage_json"))
    assert db.get_work_analysis_control("f" * 32) is None
    public = db.get_work_analysis(lease.run_id)
    assert not set(public).intersection({"owner_token", "shared_job_id", "shared_log_id", "shared_epoch"})
    db.request_work_analysis_stop(lease.run_id, "pause", public["version"])
    stopped = db.get_work_analysis_control(lease.run_id)
    assert stopped["status"] == "pausing" and stopped["reason"] == "user_pause"
    assert stopped["owner_token"] == control["owner_token"]


@pytest.mark.parametrize("bad", ["missing", "wrong_job", "wrong_type", "finished", "bool_id", "bad_job", "no_job"])
def test_shared_activation_rejects_nonmatching_log_without_claim_or_debit(db, clock, bad):
    case = create_case(db)
    run_id, job_id = case["run"]["run_id"], "b" * 32
    log_id = db.create_task_log("work_analysis", "Synthetic work", job_id=job_id)
    supplied_job, supplied_log = job_id, log_id
    if bad == "missing":
        supplied_log += 100
    elif bad == "wrong_job":
        supplied_job = "c" * 32
    elif bad == "wrong_type":
        db.conn.execute("UPDATE task_logs SET task_type='bookmark' WHERE id=?", (log_id,))
    elif bad == "finished":
        db.update_task_log(log_id, "cancelled")
    elif bad == "bool_id":
        supplied_log = True
    elif bad == "bad_job":
        supplied_job = "not-a-shared-id"
    else:
        supplied_job = None
    with pytest.raises(WorkAnalysisError):
        db.claim_work_analysis(run_id, now=clock[0], slice_seconds=30,
                               shared_job_id=supplied_job, shared_log_id=supplied_log)
    state = db.get_work_analysis(run_id, include_private=True)
    assert state["status"] == "queued" and state["epoch"] == 0 and state["active_seconds_debited"] == 0
    assert state["shared_job_id"] is state["shared_log_id"] is state["shared_epoch"] is None


def test_shared_activation_is_atomic_and_replacement_is_fenced_by_new_claim(db, clock):
    case = create_case(db)
    run_id, first_job, next_job = case["run"]["run_id"], "b" * 32, "c" * 32
    first_log = db.create_task_log("work_analysis", "First synthetic slice", job_id=first_job)
    next_log = db.create_task_log("work_analysis", "Next synthetic slice", job_id=next_job)
    with pytest.raises(RuntimeError, match="claim rollback"):
        with db.transaction():
            db.claim_work_analysis(run_id, now=clock[0], slice_seconds=30,
                                   shared_job_id=first_job, shared_log_id=first_log)
            raise RuntimeError("claim rollback")
    state = db.get_work_analysis(run_id, include_private=True)
    assert state["shared_job_id"] is None and state["epoch"] == state["active_seconds_debited"] == 0
    lease = db.claim_work_analysis(run_id, now=clock[0], slice_seconds=30,
                                   shared_job_id=first_job, shared_log_id=first_log)
    with pytest.raises(WorkAnalysisError, match="state_conflict"):
        db.claim_work_analysis(run_id, now=clock[0], slice_seconds=30,
                               shared_job_id=next_job, shared_log_id=next_log)
    state = db.get_work_analysis(run_id, include_private=True)
    assert (state["shared_job_id"], state["shared_log_id"], state["shared_epoch"]) == (first_job, first_log, lease.epoch)
    db.settle_work_analysis(lease, outcome="paused", reason="user_pause", elapsed_seconds=2)
    db.resume_work_analysis(run_id, db.get_work_analysis(run_id)["version"])
    with pytest.raises(WorkAnalysisError, match="state_conflict"):
        db.claim_work_analysis(run_id, now=clock[0], slice_seconds=30,
                               shared_job_id=first_job, shared_log_id=first_log)
    next_lease = db.claim_work_analysis(run_id, now=clock[0], slice_seconds=30,
                                        shared_job_id=next_job, shared_log_id=next_log)
    with pytest.raises(WorkAnalysisError, match="owner_lost"):
        db.settle_work_analysis(lease, outcome="failed", reason="execution_failed", elapsed_seconds=1)
    state = db.get_work_analysis(run_id, include_private=True)
    assert (state["shared_job_id"], state["shared_log_id"], state["shared_epoch"]) == (next_job, next_log, next_lease.epoch)
    assert state["active_seconds_debited"] == 32
```

- [ ] **Run RED:** `python -m pytest tests/test_work_analysis_storage.py -k "pause or expired or sync_yield or cancel or resume_configuration or budget_cas or elapsed or control_projection or shared_activation" -q`. Expected: failure because `claim_work_analysis`/control methods are absent, not because the synthetic schema or fixture fails.

- [ ] **Create `src/pixiv_novel_sync/storage/work_analysis/control.py`:**

```python
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
```

- [ ] **Compose the control mixin now** by replacing `src/pixiv_novel_sync/storage/work_analysis/__init__.py` with:

```python
"""Narrow persistence for explicitly requested work analyses."""
from .control import WorkAnalysisControlMixin
from .queries import WorkAnalysisQueriesMixin
from .runs import WorkAnalysisRunsMixin


class WorkAnalysisMixin(WorkAnalysisControlMixin, WorkAnalysisRunsMixin, WorkAnalysisQueriesMixin):
    """Database facade for the four work-analysis business tables."""
```

- [ ] **Run GREEN:** `python -m pytest tests/test_work_analysis_storage.py -q`. Expected: all migration, creation, control and recovery tests pass; normal five-second pause debits five seconds, while expired ownership debits the full reserved slice. Inspect the two error-after-commit branches: their exceptions occur outside the transaction, so invalidation is not rolled back.

#### 3.4 Add Typed Child Attachment, Unit Attempts, and In-Transaction Request Accounting

- [ ] **Append request-ledger RED tests and their fully synthetic child helper:**

```python
from pixiv_novel_sync.ai.usage import ProviderUsage


def insert_synthetic_child(conn, case, child_id, now):
    owner = "synthetic-child-owner-" + child_id
    conn.execute(
        "INSERT INTO ai_jobs(job_id,task_type,agent_id,status,input_json,owner_token,lease_until,"
        "heartbeat_at,stage,route_deadline_at) VALUES(?,'general',?,'running',?,?,?,?,'main',?)",
        (child_id, case["agent_id"], '{"retention_policy":"metadata_only"}', owner,
         work_common.stamp(now + timedelta(seconds=45)), work_common.stamp(now),
         work_common.stamp(now + timedelta(seconds=1800))),
    )
    conn.execute(
        "INSERT INTO ai_job_model_attempts(job_id,attempt_index,provider_id,model_key,agent_config_hash,"
        "provider_config_hash,candidate_list_hash,stage,status,owner_token,lease_until) "
        "VALUES(?,0,?,'synthetic-model',?,?,?,'main','running',?,?)",
        (child_id, case["provider_id"], "1" * 64, "2" * 64, "3" * 64, owner,
         work_common.stamp(now + timedelta(seconds=45))),
    )
    return owner


def attached_child(db, case, lease, clock, *, child_id="synthetic-child-1", unit_id=None):
    unit_id = unit_id or case["arguments"]["plan"]["original_units"][0]["unit_id"]
    binding = db.begin_work_analysis_unit(lease, unit_id)
    with db.transaction() as conn:
        insert_synthetic_child(conn, case, child_id, clock[0])
        db._attach_work_analysis_child(conn, binding, child_id)
    return binding, child_id


def reserve_synthetic_child(db, binding, child_id, clock, tokens=100):
    with db.transaction() as conn:
        ordinal = conn.execute("SELECT network_request_count+1 FROM ai_jobs WHERE job_id=?",
                               (child_id,)).fetchone()[0]
        db._reserve_work_analysis_request(conn, binding, child_id, ordinal, tokens, now=clock[0])
        conn.execute("UPDATE ai_jobs SET network_request_count=? WHERE job_id=?", (ordinal, child_id))
    return ordinal


def test_attachment_is_durable_without_counting_as_a_network_request(db, clock):
    case, lease = owned_case(db, clock)
    binding, child_id = attached_child(db, case, lease, clock)
    attached = db.conn.execute("SELECT * FROM work_analysis_requests WHERE child_job_id=?", (child_id,)).fetchone()
    assert attached["entry_kind"] == "attachment" and attached["request_ordinal"] == 0
    assert attached["planned_tokens"] is None and attached["usage_json"] is None
    state = db.get_work_analysis(lease.run_id)
    assert state["request_count"] == state["planned_tokens"] == 0
    assert state["usage_summary"]["network_request_count"] == 0
    assert state["usage_summary"]["reported_requests"] == state["usage_summary"]["unreported_requests"] == 0
    assert state["usage_summary"]["known_total_tokens"] is None
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE work_analysis_requests SET planned_tokens=1 WHERE child_job_id=?", (child_id,))
    with pytest.raises(WorkAnalysisError):
        with db.transaction() as conn:
            db._record_work_analysis_usage(conn, child_id, 0, ProviderUsage().to_dict())


def test_attach_and_reserve_roll_back_their_callers_changes(db, clock):
    case, lease = owned_case(db, clock)
    unit_id = case["arguments"]["plan"]["original_units"][0]["unit_id"]
    binding = db.begin_work_analysis_unit(lease, unit_id)
    stale = WorkCallBinding(WorkLease(lease.run_id, lease.epoch, "0" * 64), unit_id,
                            binding.try_no, binding.configuration_fingerprint)
    with pytest.raises(WorkAnalysisError, match="owner_lost"):
        with db.transaction() as conn:
            insert_synthetic_child(conn, case, "rejected-child", clock[0])
            db._attach_work_analysis_child(conn, stale, "rejected-child")
    assert db.conn.execute("SELECT 1 FROM ai_jobs WHERE job_id='rejected-child'").fetchone() is None
    with db.transaction() as conn:
        insert_synthetic_child(conn, case, "accepted-child", clock[0])
        db._attach_work_analysis_child(conn, binding, "accepted-child")
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with db.transaction() as conn:
            db._reserve_work_analysis_request(conn, binding, "accepted-child", 1, 100, now=clock[0])
            conn.execute("UPDATE ai_jobs SET network_request_count=1 WHERE job_id='accepted-child'")
            raise RuntimeError("synthetic rollback")
    assert db.get_work_analysis(lease.run_id)["request_count"] == 0
    assert db.conn.execute("SELECT network_request_count FROM ai_jobs WHERE job_id='accepted-child'").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM work_analysis_requests WHERE entry_kind='request'").fetchone()[0] == 0


def test_last_request_budget_is_serialized_across_two_connections(db, clock):
    case, lease = owned_case(db, clock, request_limit=1)
    binding, child_id = attached_child(db, case, lease, clock)
    barrier = Barrier(2)

    def reserve():
        local = Database(db.path)
        try:
            barrier.wait(timeout=5)
            try:
                return reserve_synthetic_child(local, binding, child_id, clock)
            except WorkAnalysisError as error:
                return error.code
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: reserve(), range(2)))
    assert sorted(map(str, outcomes)) == ["1", "budget_exhausted"]
    state = db.get_work_analysis(lease.run_id)
    assert state["request_count"] == 1 and state["planned_tokens"] == 100
    assert state["usage_summary"]["unreported_requests"] == 1


def test_usage_is_monotonic_idempotent_and_known_zero_is_not_unknown(db, clock):
    case, lease = owned_case(db, clock)
    binding, child_id = attached_child(db, case, lease, clock)
    assert reserve_synthetic_child(db, binding, child_id, clock) == 1
    with db.transaction() as conn:
        db._record_work_analysis_usage(conn, child_id, 1,
            ProviderUsage(input_tokens=0, output_tokens=0, total_tokens=0, complete=True, missing_reason=None).to_dict())
        db._record_work_analysis_usage(conn, child_id, 1, ProviderUsage().to_dict())
    first = db.get_work_analysis(lease.run_id)
    assert first["usage_summary"]["known_total_tokens"] == 0
    assert first["usage_summary"]["reported_requests"] == 1
    assert first["usage_summary"]["network_request_count"] == 1
    assert reserve_synthetic_child(db, binding, child_id, clock) == 2
    reported = ProviderUsage(input_tokens=10, output_tokens=2, total_tokens=12, complete=True, missing_reason=None).to_dict()
    with db.transaction() as conn:
        db._record_work_analysis_usage(conn, child_id, 2, reported)
        db._record_work_analysis_usage(conn, child_id, 2, reported)
        db._record_work_analysis_usage(conn, "unbound-generic-child", 1, reported)
    final = db.get_work_analysis(lease.run_id)
    assert final["usage_summary"]["known_total_tokens"] == 12
    assert final["usage_summary"]["reported_requests"] == 2
    assert final["usage_summary"]["unreported_requests"] == 0
    assert final["request_count"] == 2 and final["planned_tokens"] == 200


def test_stop_configuration_and_expired_owner_forbid_new_reservations(db, clock):
    case, lease = owned_case(db, clock)
    binding, child_id = attached_child(db, case, lease, clock)
    state = db.get_work_analysis(lease.run_id)
    db.request_work_analysis_stop(lease.run_id, "pause", state["version"])
    with pytest.raises(WorkAnalysisError, match="work_stopping"):
        reserve_synthetic_child(db, binding, child_id, clock)
    db.settle_work_analysis(lease, outcome="paused", reason="user_pause", elapsed_seconds=2)
    with pytest.raises(WorkAnalysisError, match="owner_lost"):
        reserve_synthetic_child(db, binding, child_id, clock)
    assert db.get_work_analysis(lease.run_id)["request_count"] == 0


def test_active_configuration_change_blocks_even_the_first_send(db, clock):
    case, lease = owned_case(db, clock)
    binding, child_id = attached_child(db, case, lease, clock)
    db.conn.execute("UPDATE ai_agents SET system_prompt='New synthetic instruction' WHERE id=?", (case["agent_id"],))
    with pytest.raises(WorkAnalysisError, match="configuration_changed"):
        reserve_synthetic_child(db, binding, child_id, clock)
    assert db.get_work_analysis(lease.run_id)["request_count"] == 0
```

`insert_synthetic_child` is a local SQL fixture, not a substitute for the Task 4 integration. These tests prove the transaction-local primitive; Task 4 separately proves the real `create_ai_job`/`claim_ai_job_network_request`/usage paths invoke it before transport.

- [ ] **Run RED:** `python -m pytest tests/test_work_analysis_storage.py -k "attachment or attach_and_reserve or last_request or usage_is or forbid_new or first_send" -q`. Expected: missing `begin_work_analysis_unit`/request-helper methods.

- [ ] **Create `src/pixiv_novel_sync/storage/work_analysis/requests.py`:**

```python
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
```

- [ ] **Replace the package composition** with this complete `__init__.py`:

```python
"""Narrow persistence for explicitly requested work analyses."""
from .control import WorkAnalysisControlMixin
from .queries import WorkAnalysisQueriesMixin
from .requests import WorkAnalysisRequestsMixin
from .runs import WorkAnalysisRunsMixin


class WorkAnalysisMixin(WorkAnalysisControlMixin, WorkAnalysisRequestsMixin,
                        WorkAnalysisRunsMixin, WorkAnalysisQueriesMixin):
    """Database facade for the four work-analysis business tables."""
```

- [ ] **Run GREEN:** `python -m pytest tests/test_work_analysis_storage.py tests/test_ai_usage.py -q`. Expected: all pass; attachment-only parents have zero reported **and** zero unreported requests, duplicate usage never changes request/token debit, and the two last-budget contenders produce exactly one reservation.

#### 3.5 Commit Validated Checkpoints and the Server-Owned Final Report

- [ ] **Append checkpoint/publication RED tests:**

```python
from pixiv_novel_sync.work_analysis.artifacts import claim_identity
from pixiv_novel_sync.work_analysis.planning import plan_reduction_level, seal_unit


def artifact_for(case, unit, children=None, *, findings=True):
    children = {} if children is None else children
    payload, block_map = materialize_unit(
        case["arguments"]["manifest"], unit, case["texts"], children,
        agent_prompt=case["prompt"], output_tokens=64,
    )
    claims = []
    if findings:
        quote = "Synthetic evidence is unique."
        block_id = next(key for key, block in block_map.items() if quote in block["text"])
        claims.append({"dimension": "theme", "certainty": "direct", "label": "Synthetic theme",
                       "statement": "The supplied sentence describes synthetic evidence.",
                       "quotes": [{"block_id": block_id, "quote": quote}]})
    raw = {"unit_id": unit["unit_id"], "summary": "A bounded synthetic result.",
           "no_findings_reason": None if claims else "No supported content claim is made by this fixture.",
           "claims": claims, "counterexamples": [], "uncertainties": ["Interpretations can be mistaken."]}
    return validate_unit_output(json.dumps(raw), unit, block_map, children)


def mark_synthetic_child_succeeded(db, child_id):
    with db.transaction() as conn:
        conn.execute("UPDATE ai_jobs SET status='succeeded' WHERE job_id=?", (child_id,))
        conn.execute("UPDATE ai_job_model_attempts SET status='succeeded',finish_reason='stop' WHERE job_id=?",
                     (child_id,))


def ready_checkpoint(db, case, lease, clock, *, unit=None, children=None, findings=True, child_id="checkpoint-child"):
    unit = unit or case["arguments"]["plan"]["original_units"][0]
    binding, child_id = attached_child(db, case, lease, clock, child_id=child_id, unit_id=unit["unit_id"])
    reserve_synthetic_child(db, binding, child_id, clock)
    artifact = artifact_for(case, unit, children, findings=findings)
    mark_synthetic_child_succeeded(db, child_id)
    return unit, artifact


def persist_checkpoint(db, case, lease, unit, artifact):
    db.save_work_analysis_unit(lease, unit["unit_id"], artifact,
                              actual_target={"provider_id": case["provider_id"], "model_key": "synthetic-model"})


def test_checkpoint_and_coverage_roll_back_together(db, clock, monkeypatch):
    case, lease = owned_case(db, clock)
    unit, artifact = ready_checkpoint(db, case, lease, clock)

    def reject_coverage(conn, row):
        raise RuntimeError("synthetic coverage failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(work_common, "current_coverage", reject_coverage)
        with pytest.raises(RuntimeError, match="synthetic coverage failure"):
            persist_checkpoint(db, case, lease, unit, artifact)
    state = db.get_work_analysis(lease.run_id, include_private=True)
    assert state["units"][0]["status"] == "running" and state["units"][0]["artifact"] is None
    assert state["coverage"]["validated_chars"] == 0
    persist_checkpoint(db, case, lease, unit, artifact)
    saved = db.get_work_analysis(lease.run_id, include_private=True)
    assert saved["units"][0]["status"] == "succeeded" and saved["coverage"]["ratio"] == 1
    assert saved["report"] is None


def test_source_change_between_validation_and_checkpoint_commit_wins(db, clock, monkeypatch):
    from pixiv_novel_sync.storage.work_analysis import units as unit_storage

    case, lease = owned_case(db, clock)
    unit, artifact = ready_checkpoint(db, case, lease, clock)
    original = unit_storage._validate_checkpoint

    def change_after_validation(unit, artifact, blocks, children):
        result = original(unit, artifact, blocks, children)
        replacement = "A changed synthetic body."
        db.conn.execute("UPDATE novel_texts SET text_raw=?,text_hash=? WHERE novel_id=10",
                        (replacement, hashlib.sha256(replacement.encode("utf-8")).hexdigest()))
        return result

    monkeypatch.setattr(unit_storage, "_validate_checkpoint", change_after_validation)
    with pytest.raises(WorkAnalysisError):
        persist_checkpoint(db, case, lease, unit, artifact)
    state = db.get_work_analysis(lease.run_id, include_private=True)
    assert state["status"] == "invalidated" and state["units"][0]["artifact"] is None
    assert state["coverage"]["validated_chars"] == 0


def test_publish_requires_saved_final_root_and_retains_owner_until_settlement(db, clock):
    case, lease = owned_case(db, clock)
    unit, artifact = ready_checkpoint(db, case, lease, clock)
    with pytest.raises(WorkAnalysisError, match="report_not_ready"):
        db.publish_work_analysis(lease, artifact)
    persist_checkpoint(db, case, lease, unit, artifact)
    db.publish_work_analysis(lease, artifact)
    state = db.get_work_analysis(lease.run_id, include_private=True)
    assert state["status"] == "succeeded" and state["epoch"] == lease.epoch
    assert state["owner_token"] == lease.owner_token and state["slice_settled"] is False
    report = db.get_work_analysis_report(lease.run_id)
    assert report["synthesis_complete"] is True and report["coverage"]["complete"] is True
    assert report["usage_summary"]["network_request_count"] == 1
    assert report["usage_summary"]["unreported_requests"] == 1
    assert report["units"][0]["actual_target"] == {"provider_id": case["provider_id"], "model_key": "synthetic-model"}
    with pytest.raises(WorkAnalysisError, match="state_conflict"):
        db.request_work_analysis_stop(lease.run_id, "cancel", state["version"])
    db.settle_work_analysis(lease, outcome="succeeded", reason=None, elapsed_seconds=6)
    assert db.get_work_analysis(lease.run_id)["active_seconds_debited"] == 6
    assert db.get_work_analysis_report(lease.run_id)["artifact"] == artifact


def test_stop_and_publication_are_serializable_and_never_both_win(db, clock):
    case, lease = owned_case(db, clock)
    unit, artifact = ready_checkpoint(db, case, lease, clock)
    persist_checkpoint(db, case, lease, unit, artifact)
    version = db.get_work_analysis(lease.run_id)["version"]
    barrier = Barrier(2)

    def compete(action):
        local = Database(db.path)
        try:
            barrier.wait(timeout=5)
            try:
                if action == "publish":
                    local.publish_work_analysis(lease, artifact)
                else:
                    local.request_work_analysis_stop(lease.run_id, "pause", version)
                return action
            except WorkAnalysisError as error:
                assert error.status == 409
                return "conflict"
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(compete, ["publish", "pause"]))
    assert outcomes.count("conflict") == 1
    state = db.get_work_analysis(lease.run_id, include_private=True)
    assert (state["status"] == "succeeded") == ("publish" in outcomes)
    assert (state["report"] is not None) == ("publish" in outcomes)


def test_restart_after_publication_preserves_report_and_charges_unsettled_slice(db, clock):
    case, lease = owned_case(db, clock)
    unit, artifact = ready_checkpoint(db, case, lease, clock)
    persist_checkpoint(db, case, lease, unit, artifact)
    db.publish_work_analysis(lease, artifact)
    assert db.recover_work_analyses(now=clock[0], process_start=True) == 1
    state = db.get_work_analysis(lease.run_id, include_private=True)
    assert state["status"] == "succeeded" and state["active_seconds_debited"] == 30
    assert state["owner_token"] is None and state["report"]["artifact"] == artifact
    assert db.queued_work_analysis_ids() == []


def test_restart_repairs_only_latest_committed_shared_projection(db, clock):
    case = create_case(db)
    run_id, first_job, latest_job = case["run"]["run_id"], "b" * 32, "c" * 32
    first_log = db.create_task_log("work_analysis", "First synthetic slice", job_id=first_job)
    old_lease = db.claim_work_analysis(run_id, now=clock[0], slice_seconds=30,
                                       shared_job_id=first_job, shared_log_id=first_log)
    db.request_work_analysis_stop(run_id, "cancel", db.get_work_analysis(run_id)["version"])
    db.settle_work_analysis(old_lease, outcome="cancelled", reason="user_cancel", elapsed_seconds=2)
    db.update_task_log(first_log, "cancelled")
    first_before = dict(db.conn.execute("SELECT * FROM task_logs WHERE id=?", (first_log,)).fetchone())
    db.resume_work_analysis(run_id, db.get_work_analysis(run_id)["version"])
    latest_log = db.create_task_log("work_analysis", "Latest synthetic slice", job_id=latest_job)
    lease = db.claim_work_analysis(run_id, now=clock[0], slice_seconds=30,
                                   shared_job_id=latest_job, shared_log_id=latest_log)
    duplicate_log = db.create_task_log("work_analysis", "Unbound duplicate job ID", job_id=latest_job)
    unrelated_log = db.create_task_log("bookmark", "Unrelated synthetic job", job_id="d" * 32)
    unit, artifact = ready_checkpoint(db, case, lease, clock)
    persist_checkpoint(db, case, lease, unit, artifact)
    db.conn.execute("UPDATE work_analysis_runs SET shared_epoch=? WHERE run_id=?", (old_lease.epoch, run_id))
    with pytest.raises(WorkAnalysisError, match="owner_lost"):
        db.publish_work_analysis(lease, artifact)
    assert db.get_work_analysis(run_id, include_private=True)["report"] is None
    db.conn.execute("UPDATE work_analysis_runs SET shared_epoch=? WHERE run_id=?", (lease.epoch, run_id))
    db.publish_work_analysis(lease, artifact)
    assert db.reconcile_work_analysis_task_logs() == 0
    assert db.fail_stale_task_logs() == 3
    assert db.recover_work_analyses(now=clock[0], process_start=True) == 1
    state = db.get_work_analysis(run_id, include_private=True)
    assert state["shared_epoch"] == lease.epoch and state["epoch"] > lease.epoch
    assert db.reconcile_work_analysis_task_logs() == 1
    assert db.reconcile_work_analysis_task_logs() == 0
    latest = dict(db.conn.execute("SELECT * FROM task_logs WHERE id=?", (latest_log,)).fetchone())
    assert latest["status"] == "succeeded" and latest["error_message"] is None
    assert latest["finished_at"] == state["completed_at"]
    assert latest["stats_json"] is latest["logs_json"] is None
    assert dict(db.conn.execute("SELECT * FROM task_logs WHERE id=?", (first_log,)).fetchone()) == first_before
    for log_id in (duplicate_log, unrelated_log):
        assert db.conn.execute("SELECT status FROM task_logs WHERE id=?", (log_id,)).fetchone()[0] == "failed"
    after = db.get_work_analysis(run_id, include_private=True)
    assert after["report"] == state["report"] and after["request_count"] == state["request_count"] == 1
    assert after["active_seconds_debited"] == state["active_seconds_debited"] == 32
    assert db.queued_work_analysis_ids() == []


@pytest.mark.parametrize("boundary", ["pruned", "cancelled", "wrong_type", "rebound", "deleted", "incomplete"])
def test_restart_projection_never_recreates_or_rebinds_history(db, clock, boundary):
    case = create_case(db)
    run_id, job_id = case["run"]["run_id"], "b" * 32
    log_id = db.create_task_log("work_analysis", "Synthetic slice", job_id=job_id)
    lease = db.claim_work_analysis(run_id, now=clock[0], slice_seconds=30,
                                   shared_job_id=job_id, shared_log_id=log_id)
    unit, artifact = ready_checkpoint(db, case, lease, clock)
    persist_checkpoint(db, case, lease, unit, artifact)
    if boundary != "incomplete":
        db.publish_work_analysis(lease, artifact)
    if boundary == "pruned":
        db.conn.execute("DELETE FROM task_logs WHERE id=?", (log_id,))
    elif boundary == "cancelled":
        db.update_task_log(log_id, "cancelled")
    elif boundary == "wrong_type":
        db.conn.execute("UPDATE task_logs SET task_type='bookmark' WHERE id=?", (log_id,))
    elif boundary == "rebound":
        db.conn.execute("UPDATE task_logs SET job_id=? WHERE id=?", ("c" * 32, log_id))
    elif boundary == "deleted":
        db.request_work_analysis_stop(run_id, "delete", db.get_work_analysis(run_id)["version"])
    db.fail_stale_task_logs()
    db.recover_work_analyses(now=clock[0], process_start=True)
    before = [dict(row) for row in db.conn.execute("SELECT * FROM task_logs ORDER BY id")]
    assert db.reconcile_work_analysis_task_logs() == 0
    assert [dict(row) for row in db.conn.execute("SELECT * FROM task_logs ORDER BY id")] == before
    state = db.get_work_analysis(run_id, include_private=True)
    assert (state["shared_job_id"], state["shared_log_id"], state["shared_epoch"]) == (job_id, log_id, lease.epoch)
    assert state["request_count"] == 1 and state["active_seconds_debited"] == 30
    assert db.queued_work_analysis_ids() == []
    if boundary == "deleted":
        assert state["deleted"] and state["report"] is state["manifest"] is state["configuration"] is None
    if boundary == "incomplete":
        assert state["status"] == "paused" and state["report"] is None


def test_unit_and_report_byte_limits_are_separate_hard_bounds(db, clock):
    case, lease = owned_case(db, clock)
    unit, artifact = ready_checkpoint(db, case, lease, clock)
    oversized = json.loads(json.dumps(artifact))
    seed = oversized["claims"][0]
    oversized["claims"] = []
    for index in range(128):
        claim = {**seed, "statement": f"{index:03d}" + "x" * 597}
        claim["claim_id"] = claim_identity(unit["unit_id"], claim)
        oversized["claims"].append(claim)
    oversized["summary"] = "x" * 4000
    oversized["uncertainties"] = ["x" * 600 for _ in range(32)]
    oversized["counterexamples"] = [{"statement": "x" * 600, "evidence_ids": seed["evidence_ids"]}
                                     for _ in range(32)]
    with pytest.raises(WorkAnalysisError, match="artifact_too_large"):
        persist_checkpoint(db, case, lease, unit, oversized)
    assert db.get_work_analysis(lease.run_id, include_private=True)["units"][0]["artifact"] is None
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE work_analysis_units SET artifact_json=? WHERE run_id=?", ("x" * 131073, lease.run_id))
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE work_analysis_runs SET report_json=? WHERE run_id=?", ("x" * 1048577, lease.run_id))


def test_derived_graph_is_sealed_idempotent_and_required_for_segmented_success(db, clock):
    case = prepare_case(db)
    manifest = case["arguments"]["manifest"]
    chapter = manifest["chapters"][0]
    cut = len(case["text"]) // 2
    originals = [seal_unit({
        "kind": "extract", "is_final": False, "level": 0,
        "primary_ranges": [{"novel_id": 10, "text_hash": chapter["text_hash"], "start": start, "end": end}],
        "context_ranges": [], "child_unit_ids": [], "claim_refs": [],
    }, manifest_fingerprint=manifest["manifest_fingerprint"]) for start, end in ((0, cut), (cut, len(case["text"])))]
    plan = {**case["arguments"]["plan"], "strategy": "segmented", "original_units": originals, "minimum_requests": 3}
    args = {**case["arguments"], "plan": plan}
    args["preview_fingerprint"] = preview_fingerprint(args["selection"], manifest, plan, args["configuration"], args["budget"])
    case["arguments"] = args
    case["run"] = db.create_work_analysis(**args)
    lease = db.claim_work_analysis(case["run"]["run_id"], now=clock[0], slice_seconds=30)
    artifacts = {}
    for index, unit in enumerate(originals):
        unit, artifact = ready_checkpoint(db, case, lease, clock, unit=unit, findings=False, child_id=f"extract-{index}")
        persist_checkpoint(db, case, lease, unit, artifact)
        artifacts[unit["unit_id"]] = artifact
    assert db.get_work_analysis(lease.run_id)["coverage"]["complete"] is True
    with pytest.raises(WorkAnalysisError, match="report_not_ready"):
        db.get_work_analysis_report(lease.run_id)
    with db.read_transaction() as conn:
        configuration, prompt, preflight = snapshot_work_configuration(conn, case["agent_id"], 64)
    derived = plan_reduction_level(lease.run_id, 1, originals, artifacts, preflight,
                                   agent_prompt=prompt, output_tokens=64)
    assert len(derived) == 1 and derived[0]["is_final"] is True
    bad = [{**derived[0], "unit_id": "f" * 64}]
    with pytest.raises(WorkAnalysisError):
        db.add_work_analysis_units(lease, bad)
    db.add_work_analysis_units(lease, derived)
    db.add_work_analysis_units(lease, derived)
    assert db.list_work_analysis_units(lease.run_id)["total"] == 3
    final_unit, final = ready_checkpoint(db, case, lease, clock, unit=derived[0], children=artifacts,
                                       findings=False, child_id="final-reducer")
    persist_checkpoint(db, case, lease, final_unit, final)
    db.publish_work_analysis(lease, final)
    assert db.get_work_analysis_report(lease.run_id)["synthesis_complete"] is True
```

- [ ] **Run RED:** `python -m pytest tests/test_work_analysis_storage.py -k "checkpoint or publish or publication or byte_limits or derived_graph" -q`. Expected: missing checkpoint/publication methods and `storage.work_analysis.units`.

- [ ] **Create `src/pixiv_novel_sync/storage/work_analysis/units.py`:**

```python
from __future__ import annotations

from ...ai.model_catalog import normalize_model_key
from ...work_analysis.artifacts import validate_artifact
from ...work_analysis.contracts import WorkAnalysisError, fingerprint
from ...work_analysis.evidence import allowed_child_evidence
from ...work_analysis.manifest import read_source_texts
from ...work_analysis.planning import materialize_unit, seal_unit, validate_derived_units
from . import common as c


def _fields(unit):
    return {key: value for key, value in unit.items() if key not in {"unit_id", "input_fingerprint"}}


def _validate_checkpoint(unit, artifact, blocks, children):
    artifact = validate_artifact(artifact, final=unit["is_final"])
    if (artifact["unit_id"] != unit["unit_id"] or artifact["is_final"] != unit["is_final"]
            or artifact["input_unit_ids"] != unit["child_unit_ids"]):
        raise WorkAnalysisError("validation_failed", 409)
    if unit["kind"] == "reduce":
        allowed = allowed_child_evidence(unit, children)
        for key, evidence in artifact["evidence"].items():
            if key not in allowed or evidence != allowed[key]:
                raise WorkAnalysisError("validation_failed", 409)
    else:
        for evidence in artifact["evidence"].values():
            block = blocks.get(evidence["block_id"])
            if (block is None or evidence["novel_id"] != block["novel_id"]
                    or evidence["text_hash"] != block["text_hash"]):
                raise WorkAnalysisError("validation_failed", 409)
            offset = block["text"].find(evidence["quote"])
            if (offset < 0 or block["text"].find(evidence["quote"], offset + 1) >= 0
                    or evidence["start"] != block["start"] + offset
                    or evidence["end"] != evidence["start"] + len(evidence["quote"])
                    or evidence["end"] > block["end"]):
                raise WorkAnalysisError("validation_failed", 409)
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
```

The expensive body read and exact-quote validation finish before the `BEGIN IMMEDIATE` checkpoint commit. The commit rechecks the lease, stop/deletion/invalidation markers, selected source hashes, frozen configuration, unit attempt and child artifact hashes. Source-write triggers revoke ownership inside the source transaction, so a source update between the read and write phases cannot win a stale checkpoint. No database transaction crosses transport. `publish_work_analysis` is only called after Task 5 acquires the shared runner's `claim_finalization()`; this method still performs all independent database guards.

Publication also checks that the persisted shared activation epoch is the publishing lease's epoch. A succeeded parent cannot be claimed again, so normal settlement/recovery may increment `epoch` without changing that provenance. Sole-owner startup runs generic stale-log cleanup, conservative parent recovery, then `reconcile_work_analysis_task_logs()`; reconciliation uses only settled successful parent truth and the exact retained log/job pair. It repairs `running`/`failed` projection rows without rewriting an already cancelled/succeeded historical row or recreating a pruned row. It never reads the report payload, changes the parent, copies titles/quotes/claims into generic logs, releases a shared slot, or queues a paid retry. Existing stale-log behavior for unrelated tasks is unchanged.

- [ ] **Replace `src/pixiv_novel_sync/storage/work_analysis/__init__.py` with the final composition:**

```python
"""Narrow persistence for explicitly requested work analyses."""
from .control import WorkAnalysisControlMixin
from .queries import WorkAnalysisQueriesMixin
from .requests import WorkAnalysisRequestsMixin
from .runs import WorkAnalysisRunsMixin
from .units import WorkAnalysisUnitsMixin


class WorkAnalysisMixin(WorkAnalysisControlMixin, WorkAnalysisRequestsMixin, WorkAnalysisUnitsMixin,
                        WorkAnalysisRunsMixin, WorkAnalysisQueriesMixin):
    """Database facade for the four work-analysis business tables."""
```

- [ ] **Run GREEN:** `python -m pytest tests/test_work_analysis_storage.py -q`. Expected: all pass; checkpoint and coverage either both commit or both roll back, only one stop/publication contender wins, a final artifact never bypasses the 128 KiB unit bound, and a complete original-input frontier without a validated final reducer is not a successful report. Restart repairs only the latest committed shared projection; old cancelled slices, exact-ID mismatches, pruned logs and deleted/incomplete parents stay untouched.

#### 3.6 Exercise Source Invalidation, Evidence Jumps, Tombstones, and Remaining Boundaries

The implementation for these boundaries is the Task 3A migration/projections plus the completed Task 3B control/request/checkpoint methods. These regression tests are added before integration into Task 4; if one fails, repair only the demonstrated boundary and rerun its focused case before the full storage gate. Do not weaken the validators or change the approved deletion semantics to make a fixture pass.

- [ ] **Append privacy, version and boundary tests:**

```python
from pixiv_novel_sync.models import NovelTextRecord


def published_case(db, clock, **kwargs):
    case, lease = owned_case(db, clock, **kwargs)
    unit, artifact = ready_checkpoint(db, case, lease, clock)
    persist_checkpoint(db, case, lease, unit, artifact)
    db.publish_work_analysis(lease, artifact)
    return case, lease, unit, artifact


def assert_private_work_data_purged(db, case, run_id):
    state = db.get_work_analysis(run_id, include_private=True)
    assert state["deleted"] and state["deleted_at"] is not None
    assert state["manifest"] is None and state["plan"] is None and state["configuration"] is None
    assert state["report"] is None and state["title"] is None and not state["can_resume"]
    assert state["source_id"] is None and state["source_kind"] is None
    assert db.conn.execute("SELECT COUNT(*) FROM work_analysis_sources WHERE run_id=?", (run_id,)).fetchone()[0] == 0
    for table in ("work_analysis_runs", "work_analysis_units", "work_analysis_requests"):
        rows = [dict(row) for row in db.conn.execute("SELECT * FROM " + table + " WHERE run_id=?", (run_id,))]
        serialized = json.dumps(rows)
        for marker in ("PRIVATE_NOVEL_TITLE", "PRIVATE_SERIES_TITLE", "Synthetic evidence is unique.",
                       "A bounded synthetic result.", case["text"], case["prompt"]):
            assert marker not in serialized
    assert db.list_work_analyses()["total"] == 0
    with pytest.raises(WorkAnalysisError) as report_error:
        db.get_work_analysis_report(run_id)
    assert report_error.value.status == 410
    with pytest.raises(WorkAnalysisError) as units_error:
        db.list_work_analysis_units(run_id)
    assert units_error.value.status == 410


def test_same_hash_upsert_and_metadata_refresh_do_not_stale_a_report(db, clock):
    case, lease, unit, artifact = published_case(db, clock)
    before = db.get_work_analysis(lease.run_id)
    digest = case["arguments"]["manifest"]["chapters"][0]["text_hash"]
    db.upsert_novel_text(NovelTextRecord(10, case["text"], None, digest))
    db.conn.execute("UPDATE novel_texts SET fetched_at='2030-01-01 00:00:00' WHERE novel_id=10")
    db.conn.execute("UPDATE novels SET total_views=12 WHERE novel_id=10")
    after = db.get_work_analysis(lease.run_id)
    assert after["status"] == "succeeded" and not after["report_stale"]
    assert before["version"] == after["version"]
    with db.transaction() as conn:
        db._invalidate_work_analyses_for_source(conn, 10, digest)
    assert not db.get_work_analysis_report(lease.run_id)["report_stale"]


def test_evidence_uses_codepoints_and_refuses_offsets_after_source_change(db, clock):
    body = "Prefix \U0001F9ED. Synthetic evidence is unique. The rest is a synthetic body."
    case, lease, unit, artifact = published_case(db, clock, body=body)
    evidence_id, evidence = next(iter(artifact["evidence"].items()))
    jump = db.get_work_analysis_evidence(lease.run_id, evidence_id)
    assert jump["jump"]["start"] == body.index("Synthetic evidence is unique.")
    assert jump["jump"]["end"] - jump["jump"]["start"] == len(evidence["quote"])
    assert len(jump["jump"]["context_text"]) <= 496
    with pytest.raises(WorkAnalysisError) as missing:
        db.get_work_analysis_evidence(lease.run_id, "f" * 64)
    assert missing.value.status == 404
    replacement = "Different synthetic text at every offset."
    db.upsert_novel_text(NovelTextRecord(10, replacement, None, hashlib.sha256(replacement.encode("utf-8")).hexdigest()))
    stale = db.get_work_analysis_report(lease.run_id)
    assert stale["report_stale"] and stale["artifact"]["evidence"][evidence_id] == evidence
    with pytest.raises(WorkAnalysisError) as changed:
        db.get_work_analysis_evidence(lease.run_id, evidence_id)
    assert changed.value.status == 410 and changed.value.code == "source_changed"


def test_false_unchanged_hash_still_invalidates_raw_body_change(db, clock):
    case, lease = owned_case(db, clock)
    db.conn.execute("UPDATE novel_texts SET text_raw='A different body with a falsely unchanged hash' WHERE novel_id=10")
    state = db.get_work_analysis(lease.run_id, include_private=True)
    assert state["status"] == "invalidated" and state["owner_token"] is None
    assert state["active_seconds_debited"] == 30
    with pytest.raises(WorkAnalysisError, match="owner_lost"):
        db.settle_work_analysis(lease, outcome="paused", reason="user_pause", elapsed_seconds=1)


def test_new_series_chapter_marks_archive_outdated_without_expanding_frozen_scope(db, clock):
    case, lease, unit, artifact = published_case(db, clock, series=True)
    db.conn.execute(
        "INSERT INTO novels(novel_id,user_id,series_id,title,visible,restrict_value,x_restrict,text_length,"
        "total_bookmarks,total_views,tags_json,raw_json,meta_hash,create_date) "
        "VALUES(11,1,9,'A later synthetic chapter',1,'public',0,5,0,0,'[]','{}','later-meta','2026-02-01')"
    )
    report = db.get_work_analysis_report(lease.run_id)
    assert report["archive_outdated"] and not report["report_stale"]
    assert [chapter["novel_id"] for chapter in report["manifest"]["chapters"]] == [10]
    assert report["coverage"]["available_chars"] == len(case["text"])


def test_delete_revokes_before_purge_and_late_usage_cannot_restore_sensitive_data(db, clock):
    case, lease = owned_case(db, clock)
    unit, artifact = ready_checkpoint(db, case, lease, clock)
    persist_checkpoint(db, case, lease, unit, artifact)
    state = db.get_work_analysis(lease.run_id)
    deleted = db.request_work_analysis_stop(lease.run_id, "delete", state["version"])
    assert deleted["deleted"] and deleted["active_seconds_debited"] == 30
    assert db.request_work_analysis_stop(lease.run_id, "delete", state["version"])["deleted"]
    with pytest.raises(WorkAnalysisError):
        db.publish_work_analysis(lease, artifact)
    with pytest.raises(WorkAnalysisError):
        persist_checkpoint(db, case, lease, unit, artifact)
    with pytest.raises(WorkAnalysisError, match="owner_lost"):
        db.settle_work_analysis(lease, outcome="cancelled", reason="user_cancel", elapsed_seconds=1)
    with pytest.raises(WorkAnalysisError):
        db.resume_work_analysis(lease.run_id, deleted["version"])
    with db.transaction() as conn:
        db._record_work_analysis_usage(conn, "checkpoint-child", 1,
            ProviderUsage(input_tokens=10, output_tokens=2, total_tokens=12, complete=True, missing_reason=None).to_dict())
    assert db.get_work_analysis(lease.run_id)["usage_summary"]["known_total_tokens"] == 12
    assert_private_work_data_purged(db, case, lease.run_id)
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE work_analysis_units SET artifact_json='{}' WHERE run_id=?", (lease.run_id,))
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE work_analysis_runs SET report_json='{}' WHERE run_id=?", (lease.run_id,))


@pytest.mark.parametrize("deletion", ["text", "novel", "series", "direct_user", "bulk_user"])
def test_source_deletion_paths_purge_sensitive_work_artifacts(db, clock, deletion):
    case, lease, unit, artifact = published_case(db, clock, series=True)
    if deletion == "bulk_user":
        db.delete_user(1)
    else:
        statements = {"text": "DELETE FROM novel_texts WHERE novel_id=10",
                      "novel": "DELETE FROM novels WHERE novel_id=10",
                      "series": "DELETE FROM series WHERE series_id=9",
                      "direct_user": "DELETE FROM users WHERE user_id=1"}
        with db.transaction() as conn:
            conn.execute(statements[deletion])
    assert_private_work_data_purged(db, case, lease.run_id)
    assert db.get_work_analysis(lease.run_id)["reason"] == "source_deleted"
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_source_delete_rollback_restores_parent_and_artifact_in_same_transaction(db, clock):
    case, lease, unit, artifact = published_case(db, clock)
    with pytest.raises(RuntimeError, match="synthetic source rollback"):
        with db.transaction() as conn:
            conn.execute("DELETE FROM novel_texts WHERE novel_id=10")
            assert conn.execute("SELECT deleted_at FROM work_analysis_runs WHERE run_id=?", (lease.run_id,)).fetchone()[0]
            raise RuntimeError("synthetic source rollback")
    assert not db.get_work_analysis(lease.run_id)["deleted"]
    assert db.get_work_analysis_report(lease.run_id)["artifact"] == artifact


def test_unknown_paid_response_is_not_a_checkpoint_and_retry_keeps_both_debits(db, clock):
    case, lease = owned_case(db, clock)
    unit, artifact = ready_checkpoint(db, case, lease, clock)
    db.recover_work_analyses(now=clock[0], process_start=True)
    state = db.get_work_analysis(lease.run_id, include_private=True)
    assert state["units"][0]["artifact"] is None and state["units"][0]["status"] == "pending"
    assert state["request_count"] == 1 and state["usage_summary"]["unreported_requests"] == 1
    db.resume_work_analysis(lease.run_id, state["version"])
    new_lease = db.claim_work_analysis(lease.run_id, now=clock[0], slice_seconds=30)
    retried, child_id = attached_child(db, case, new_lease, clock, child_id="retry-child")
    assert retried.try_no == 2
    reserve_synthetic_child(db, retried, child_id, clock)
    with pytest.raises(WorkAnalysisError, match="owner_lost"):
        persist_checkpoint(db, case, lease, unit, artifact)
    final = db.get_work_analysis(lease.run_id)
    assert final["request_count"] == 2 and final["planned_tokens"] == 200
    assert final["active_seconds_debited"] == 60


def test_exact_token_boundary_and_known_zero_do_not_refund_estimates(db, clock):
    case, lease = owned_case(db, clock)
    binding, child_id = attached_child(db, case, lease, clock)
    reserve_synthetic_child(db, binding, child_id, clock, tokens=100000)
    with db.transaction() as conn:
        db._record_work_analysis_usage(conn, child_id, 1,
            ProviderUsage(input_tokens=0, output_tokens=0, total_tokens=0, complete=True, missing_reason=None).to_dict())
    with pytest.raises(WorkAnalysisError, match="budget_exhausted"):
        reserve_synthetic_child(db, binding, child_id, clock, tokens=1)
    state = db.get_work_analysis(lease.run_id)
    assert state["planned_tokens"] == 100000 and state["request_count"] == 1


def test_slice_deadline_stops_sends_but_allows_drain_heartbeat_and_settlement(db, clock):
    case, lease = owned_case(db, clock)
    binding, child_id = attached_child(db, case, lease, clock)
    clock[0] += timedelta(seconds=31)
    with pytest.raises(WorkAnalysisError, match="active_time_exhausted"):
        reserve_synthetic_child(db, binding, child_id, clock)
    assert db.heartbeat_work_analysis(lease, now=clock[0])
    db.settle_work_analysis(lease, outcome="paused", reason="budget_exhausted", elapsed_seconds=31)
    state = db.get_work_analysis(lease.run_id)
    assert state["active_seconds_debited"] == 30 and state["request_count"] == 0


def test_explicit_series_order_still_requires_separate_order_confirmation(db):
    case = prepare_case(db, series=True)
    args = case["arguments"]
    selection = {**args["selection"], "ordered_novel_ids": [10]}
    with db.read_transaction() as conn:
        manifest, texts = capture_manifest(conn, selection)
        configuration, prompt, preflight = snapshot_work_configuration(conn, case["agent_id"], 64)
    plan = plan_original_units(manifest, texts, preflight, agent_prompt=prompt, output_tokens=64)
    args = {**args, "selection": selection, "manifest": manifest, "plan": plan,
            "confirmations": {**args["confirmations"], "order_confirmed": False}}
    args["preview_fingerprint"] = preview_fingerprint(selection, manifest, plan, configuration, args["budget"])
    with pytest.raises(WorkAnalysisError, match="confirmation_required"):
        db.create_work_analysis(**args)
    assert db.conn.execute("SELECT COUNT(*) FROM work_analysis_runs").fetchone()[0] == 0


def test_exhausted_sync_yield_claim_commits_pause_before_raising(db, clock):
    case, lease = owned_case(db, clock, request_limit=1)
    binding, child_id = attached_child(db, case, lease, clock)
    reserve_synthetic_child(db, binding, child_id, clock)
    state = db.get_work_analysis(lease.run_id)
    db.request_work_analysis_stop(lease.run_id, "sync_yield", state["version"])
    db.settle_work_analysis(lease, outcome="paused", reason="sync_yield", elapsed_seconds=2)
    assert db.queued_work_analysis_ids() == [lease.run_id]
    with pytest.raises(WorkAnalysisError, match="budget_exhausted"):
        db.claim_work_analysis(lease.run_id, now=clock[0], slice_seconds=30)
    state = db.get_work_analysis(lease.run_id)
    assert (state["status"], state["reason"]) == ("paused", "budget_exhausted")
    assert state["active_seconds_debited"] == 2 and db.queued_work_analysis_ids() == []


def test_detected_invalidation_wins_over_a_concurrent_normal_pause(db, clock):
    case, lease = owned_case(db, clock)
    state = db.get_work_analysis(lease.run_id)
    db.request_work_analysis_stop(lease.run_id, "pause", state["version"])
    db.settle_work_analysis(lease, outcome="invalidated", reason="configuration_changed", elapsed_seconds=1)
    state = db.get_work_analysis(lease.run_id)
    assert state["status"] == "invalidated" and not state["can_resume"]
    assert state["active_seconds_debited"] == 30


def test_unit_source_and_request_ordinals_cannot_exceed_table_bounds(db, clock):
    case, lease = owned_case(db, clock)
    binding, child_id = attached_child(db, case, lease, clock)
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE work_analysis_units SET ordinal=512 WHERE run_id=?", (lease.run_id,))
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE work_analysis_sources SET position=2048 WHERE run_id=?", (lease.run_id,))
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE work_analysis_requests SET request_ordinal=33 WHERE child_job_id=?", (child_id,))
    assert db.get_work_analysis(lease.run_id)["request_count"] == 0


def test_server_envelope_overflow_is_a_precise_synthesis_pause_without_report(db, clock, monkeypatch):
    case, lease = owned_case(db, clock)
    unit, artifact = ready_checkpoint(db, case, lease, clock)
    persist_checkpoint(db, case, lease, unit, artifact)
    original = work_common.dump

    def reject_envelope(value, limit):
        if limit == 1048576 and isinstance(value, dict) and "synthesis_complete" in value:
            raise WorkAnalysisError("too_large", 413)
        return original(value, limit)

    with monkeypatch.context() as scoped:
        scoped.setattr(work_common, "dump", reject_envelope)
        with pytest.raises(WorkAnalysisError, match="synthesis_capacity_exceeded"):
            db.publish_work_analysis(lease, artifact)
    assert db.get_work_analysis(lease.run_id, include_private=True)["report"] is None
    db.settle_work_analysis(lease, outcome="paused", reason="synthesis_capacity_exceeded", elapsed_seconds=3)
    state = db.get_work_analysis(lease.run_id)
    assert (state["status"], state["reason"]) == ("paused", "synthesis_capacity_exceeded")
```

- [ ] **Run the boundary regression command:** `python -m pytest tests/test_work_analysis_storage.py -k "purge or deletion or rollback or evidence or same_hash or false_unchanged or archive_outdated or paid_response or token_boundary or slice_deadline or order_confirmation" -q`. Expected: all pass against the implementation just completed; these are regression checks for already implemented boundaries, not a claimed new RED run. Any failure is a blocker for the storage review gate.

- [ ] **Run the complete Task 3B gate:**

```bash
python -m pytest tests/test_work_analysis_storage.py tests/test_storage_db.py tests/test_storage_small_guards.py tests/test_storage_audit_followups.py tests/test_ensure_schema.py tests/test_schema_rebuild.py tests/test_delete_user_orphans.py tests/test_ai_usage.py tests/test_ai_job_routing_storage.py -q
```

Expected: all pass using temporary databases and synthetic text. This plan does not claim those commands have run; during implementation inspect the actual exit code, collected tests and assertion results. No real archive, Key decryption, outbound HTTP, Pixiv access or Provider billing belongs to this gate.

- [ ] **Review gate 3B:** trace one physical send from typed binding through attachment0 to real ordinal1 and verify all request/token/usage aggregates exclude `entry_kind='attachment'`. Verify lease expiry/normal stop/delete have their different refund semantics, idempotency is independent of amended budget, source/stop CAS precedes sensitive commits, coverage sees only `status='succeeded'` units, finality is sealed by the graph, evidence is a report-local closed set, and every tombstone path removes titles/text references/quotes/claims while retaining only necessary safe audit counts and bounded shared-activation IDs. Inspect both publication/stop transaction orders and the source-change-between-read-and-write test. Verify the latest activation association commits with the claim and fences publication; startup recovery repairs only its exact existing log, leaves prior cancelled slices and pruned/deleted history untouched, and never requeues paid work.

- [ ] **Commit Task 3B only:**

```bash
git add src/pixiv_novel_sync/storage/work_analysis/control.py src/pixiv_novel_sync/storage/work_analysis/requests.py src/pixiv_novel_sync/storage/work_analysis/units.py src/pixiv_novel_sync/storage/work_analysis/__init__.py tests/test_work_analysis_storage.py
git commit -m "feat(work-analysis): enforce owned budgeted checkpoints and deletion"
```

Task 4 may now connect these transaction-local hooks to the existing AI path. It must not weaken the parent's guards or add a parallel network reservation. Task 5 owns actual model execution/finalization claims; Task 6 owns shared-job cancellation permission and admission. Those integration tasks retain their independent RED/GREEN and concurrency gates.

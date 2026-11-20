"""Small source identities for preference heartbeats; never cache report bodies."""

from __future__ import annotations

from ..work_analysis.contracts import WorkAnalysisError, fingerprint


def migrate_source_revisions(conn):
    """Report changes have a monotonic identity even when their bytes are restored."""
    report_columns = {row[1] for row in conn.execute("PRAGMA table_info(work_analysis_runs)")}
    if "preference_source_revision" not in report_columns:
        conn.execute(
            "ALTER TABLE work_analysis_runs ADD COLUMN preference_source_revision INTEGER NOT NULL DEFAULT 0"
        )
    profile_columns = {row[1] for row in conn.execute("PRAGMA table_info(preference_summary_runs)")}
    added = "source_revision" not in profile_columns
    if added:
        conn.execute("ALTER TABLE preference_summary_runs ADD COLUMN source_revision TEXT")
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS preference_report_revision
        AFTER UPDATE OF report_json,claim_feedback_revision,report_stale,archive_outdated,status,deleted_at
        ON work_analysis_runs
        WHEN NEW.deleted_at IS NULL AND (OLD.report_json IS NOT NEW.report_json
          OR OLD.claim_feedback_revision IS NOT NEW.claim_feedback_revision
          OR OLD.report_stale IS NOT NEW.report_stale
          OR OLD.archive_outdated IS NOT NEW.archive_outdated
          OR OLD.status IS NOT NEW.status OR OLD.deleted_at IS NOT NEW.deleted_at)
        BEGIN UPDATE work_analysis_runs
          SET preference_source_revision=OLD.preference_source_revision+1 WHERE run_id=NEW.run_id;
        END"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_preference_latest_report
        ON work_analysis_runs(source_kind,source_id,created_at DESC,run_id DESC)
        WHERE status='succeeded' AND deleted_at IS NULL AND report_stale=0
          AND archive_outdated=0 AND report_json IS NOT NULL"""
    )
    return added


def source_rows(conn):
    """Same transaction as the consumer: current feedback and selected report versions.

    Includes unrated/missing-report transitions and chapter-to-series precedence.
    Full content validation remains mandatory when creating a preview/profile.
    No report JSON or novel body crosses the SQLite/Python boundary here.
    """
    rows = conn.execute(
        """SELECT f.source_kind,f.source_id,f.reaction,f.note,f.version,
        n.novel_id AS existing_novel,n.series_id AS parent_series,s.series_id AS existing_series,
        r.run_id,r.preference_source_revision
        FROM preference_reading_feedback f
        LEFT JOIN novels n ON f.source_kind='novel' AND n.novel_id=f.source_id
        LEFT JOIN series s ON f.source_kind='series' AND s.series_id=f.source_id
        LEFT JOIN work_analysis_runs r ON r.run_id=(
            SELECT w.run_id FROM work_analysis_runs w
            WHERE w.source_kind=f.source_kind AND w.source_id=f.source_id
              AND w.status='succeeded' AND w.deleted_at IS NULL AND w.report_stale=0
              AND w.archive_outdated=0 AND w.report_json IS NOT NULL
            ORDER BY w.created_at DESC,w.run_id DESC LIMIT 1)
        WHERE f.reaction!='clear' ORDER BY f.source_kind,f.source_id LIMIT 2001"""
    ).fetchall()
    if len(rows) > 2000:
        raise WorkAnalysisError("input_too_large", 413)
    return [dict(row) for row in rows]


def source_revision(conn):
    return fingerprint(source_rows(conn))

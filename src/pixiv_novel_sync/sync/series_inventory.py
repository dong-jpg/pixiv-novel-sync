"""Remember complete observed chapter order without deleting private archive chapters."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json


def migrate_inventory(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS series_inventory (
        series_id INTEGER PRIMARY KEY REFERENCES series(series_id) ON DELETE CASCADE,
        novel_ids_json TEXT NOT NULL, observed_at TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1)"""
    )


def observed_order(conn, series_id):
    row = conn.execute(
        "SELECT novel_ids_json FROM series_inventory WHERE series_id=?", (series_id,)
    ).fetchone()
    return json.loads(row[0]) if row else []


def remember_order(db, series_id, ids, expected):
    if (
        not ids
        or len(ids) != expected
        or len(ids) != len(set(ids))
        or len(ids) > 10000
        or any(type(value) is not int or value <= 0 for value in ids)
    ):
        return False
    with db.transaction() as conn:
        old = observed_order(conn, series_id)
        if old != ids:
            from ..storage.work_analysis.schema import invalidate_sql

            runs = conn.execute(
                "SELECT run_id FROM work_analysis_runs WHERE source_kind='series' "
                "AND source_id=? AND deleted_at IS NULL AND invalidated_at IS NULL",
                (series_id,),
            ).fetchall()
            for run in runs:
                previous_ids = [
                    row[0]
                    for row in conn.execute(
                        "SELECT novel_id FROM work_analysis_sources WHERE run_id=? ORDER BY position",
                        (run[0],),
                    )
                ]
                if previous_ids != ids:
                    conn.execute(invalidate_sql("SELECT ?"), (run[0],))
        conn.execute(
            "INSERT INTO series_inventory(series_id,novel_ids_json,observed_at) VALUES(?,?,?) "
            "ON CONFLICT(series_id) DO UPDATE SET novel_ids_json=excluded.novel_ids_json,"
            "observed_at=excluded.observed_at,revision=series_inventory.revision+(series_inventory.novel_ids_json!=excluded.novel_ids_json)",
            (series_id, json.dumps(ids), datetime.now(timezone.utc).isoformat()),
        )
        if old != ids:
            conn.execute("UPDATE library_analysis_clock SET revision=revision+1 WHERE id=1")
            conn.execute(
                "INSERT INTO library_analysis_sources(source_kind,source_id,revision) "
                "SELECT 'series',?,revision FROM library_analysis_clock WHERE id=1 "
                "ON CONFLICT(source_kind,source_id) DO UPDATE SET revision=excluded.revision",
                (series_id,),
            )
    return True


def needs_refresh(db, novel_id, days):
    if days <= 0:
        return False
    row = db.conn.execute(
        "SELECT coalesce(a.checked_at,t.fetched_at) FROM novel_texts t "
        "LEFT JOIN archive_refreshes a USING(novel_id) WHERE t.novel_id=?",
        (novel_id,),
    ).fetchone()
    if not row:
        return False
    try:
        checked = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        return checked < datetime.now(timezone.utc) - timedelta(days=days)
    except (ValueError, TypeError, AttributeError):
        return True

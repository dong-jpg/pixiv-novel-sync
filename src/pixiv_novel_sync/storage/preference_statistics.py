"""Versioned publication of current-archive statistics."""

from __future__ import annotations


def migrate_statistics(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS preference_statistics_state (
        id INTEGER PRIMARY KEY CHECK(id=1), archive_revision INTEGER NOT NULL DEFAULT 0,
        generation INTEGER NOT NULL DEFAULT 0, basis TEXT NOT NULL DEFAULT 'historical',
        snapshot_revision INTEGER)"""
    )
    conn.execute("INSERT OR IGNORE INTO preference_statistics_state(id) VALUES(1)")
    for table, columns in (
        ("novels", ("title", "caption", "series_id", "text_length", "tags_json", "x_restrict", "user_id")),
        ("novel_texts", ("text_raw", "text_hash", "has_content")),
        ("users", ("name",)),
        ("sources", ("source_type", "source_key", "novel_id")),
    ):
        for event in ("INSERT", "DELETE", "UPDATE"):
            clause = ""
            operation = event
            if event == "UPDATE":
                operation += " OF " + ",".join(columns)
                clause = " WHEN " + " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in columns)
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS preference_statistics_{table}_{event.lower()} "
                f"AFTER {operation} ON {table}{clause} BEGIN "
                "UPDATE preference_statistics_state SET archive_revision=archive_revision+1 WHERE id=1; END"
            )


def statistics_state(conn):
    row = dict(conn.execute("SELECT * FROM preference_statistics_state WHERE id=1").fetchone())
    row.pop("id")
    row["current_archive"] = (
        row["basis"] == "current_archive" and row["snapshot_revision"] == row["archive_revision"]
    )
    return row

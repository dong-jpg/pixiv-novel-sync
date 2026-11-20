"""Compute statistics in a private scratch database and atomically publish them."""

from __future__ import annotations

from pathlib import Path
import tempfile

from .preferences import PreferenceAnalyzer
from .storage.preference_statistics import statistics_state
from .storage_db import Database


class CurrentArchiveRebuild:
    def __init__(self, db, *, min_text_length=1000, expected_generation=None, stop_requested=lambda: False):
        self.db, self.minimum = db, min_text_length
        self.expected_generation, self.stopped = expected_generation, stop_requested
        self.directory = None
        self.scratch = None

    def close(self):
        if self.scratch is not None:
            self.scratch.close()
        if self.directory is not None:
            self.directory.cleanup()

    def prepare(self, progress=None):
        self.directory = tempfile.TemporaryDirectory(prefix="pixiv-statistics-")
        self.scratch = Database(Path(self.directory.name) / "statistics.db")
        self.scratch.init_schema()
        analyzer = PreferenceAnalyzer(self.scratch)
        processed, last_id = 0, 0
        with self.db.read_transaction() as conn:
            self.state = statistics_state(conn)
            if self.expected_generation is not None and self.state["generation"] != self.expected_generation:
                raise ValueError("统计版本已变化，请刷新后重算")
            self.scratch.prepare_preference_accumulator_scope(self.minimum)
            while True:
                if self.stopped():
                    raise InterruptedError("Task stopped by user")
                rows = conn.execute(
                    """SELECT n.novel_id,n.title,n.caption,n.series_id,n.text_length,
                    n.tags_json,n.x_restrict,u.name AS author_name,nt.text_raw,
                    (SELECT group_concat(s.source_type) FROM sources s WHERE s.novel_id=n.novel_id) AS source_types
                    FROM novels n JOIN novel_texts nt USING(novel_id) LEFT JOIN users u USING(user_id)
                    WHERE n.novel_id>? AND n.text_length>=? AND nt.has_content=1
                    ORDER BY n.novel_id LIMIT 200""",
                    (last_id, self.minimum),
                ).fetchall()
                if not rows:
                    break
                acc = analyzer._new_counters()
                for row in rows:
                    if self.stopped():
                        raise InterruptedError("Task stopped by user")
                    analyzer._accumulate_row(dict(row), acc)
                self.scratch.merge_preference_batch(
                    {kind: dict(counter) for kind, counter in acc["terms"].items()},
                    {
                        key: (
                            dict(acc[key])
                            if key in ("length_buckets", "source_dist", "x_restrict_dist")
                            else acc[key]
                        )
                        for key in (
                            "novel_count",
                            "series_novel_count",
                            "total_chars",
                            "length_buckets",
                            "source_dist",
                            "x_restrict_dist",
                        )
                    },
                    [row["novel_id"] for row in rows],
                    self.minimum,
                )
                processed += len(rows)
                last_id = rows[-1]["novel_id"]
                if progress:
                    progress(processed, 0)
        self.scratch.prune_preference_term_noise(
            analyzer.HIGH_CARDINALITY_TYPES, analyzer.TERM_COUNTS_MAX_ROWS
        )
        rebuilt = analyzer.rebuild_profile_from_accumulator()
        rebuilt["source_scope"].update(
            basis="current_archive",
            archive_revision=self.state["archive_revision"],
            generation=self.state["generation"] + 1,
            incremental=False,
        )
        return (
            {
                "processed_this_run": processed,
                "analyzed_total": processed,
                "remaining": 0,
                "done": True,
                "scope_changed": True,
            },
            rebuilt,
        )

    def publish(self, conn):
        if not conn.in_transaction:
            raise RuntimeError("Statistics publication requires the profile transaction")
        current = statistics_state(conn)
        if (current["archive_revision"], current["generation"]) != (
            self.state["archive_revision"],
            self.state["generation"],
        ):
            raise ValueError("书库或统计在重算期间已变化，保留原统计，请重新运行")
        # Only derived statistics are copied; scratch contains no copied novel bodies.
        for table in ("preference_term_counts", "preference_analyzed_novels", "preference_accumulator"):
            conn.execute(f"DELETE FROM {table}")
            cursor = self.scratch.conn.execute(f"SELECT * FROM {table}")
            columns = [column[0] for column in cursor.description]
            statement = (
                f"INSERT INTO {table}("
                + ",".join(columns)
                + ") VALUES("
                + ",".join("?" for _ in columns)
                + ")"
            )
            while rows := cursor.fetchmany(1000):
                conn.executemany(statement, [tuple(row) for row in rows])
        conn.execute(
            "UPDATE preference_statistics_state SET generation=generation+1,basis='current_archive',"
            "snapshot_revision=archive_revision WHERE id=1"
        )

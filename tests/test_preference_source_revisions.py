"""Lightweight heartbeats must retain the exact source and deletion guards."""

from __future__ import annotations

import json
import time

import pytest

from pixiv_novel_sync.preference_learning.contracts import PreferenceCallBinding
from pixiv_novel_sync.storage import preference_learning as storage
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError
from test_preference_learning_profiles import env, create_summary, execute  # noqa: F401


def binding_for(env, run_id):
    with env.db.transaction() as conn:
        conn.execute(
            "UPDATE preference_summary_runs SET status='running',owner_token=?,deadline_at='2099-01-01 00:00:00' "
            "WHERE run_id=?",
            ("a" * 64, run_id),
        )
        row = storage.get_run(conn, run_id)
    return PreferenceCallBinding(run_id, "a" * 64, row["configuration_fingerprint"])


@pytest.mark.parametrize("source_count", [1, 25, 100])
def test_stable_heartbeat_reads_neither_reports_nor_frozen_input(
    env, monkeypatch, source_count, record_property
):
    original = dict(
        env.db.conn.execute("SELECT * FROM work_analysis_runs WHERE run_id=?", (env.run_id,)).fetchone()
    )
    with env.db.transaction() as conn:
        for source_id in range(2, source_count + 1):
            conn.execute(
                "INSERT INTO novels(novel_id,user_id,title,visible,restrict_value,x_restrict,text_length,"
                "total_bookmarks,total_views,tags_json,raw_json,meta_hash) "
                "VALUES(?,1,'Synthetic',1,'public',0,100,0,0,'[]','{}','fixture')",
                (source_id,),
            )
            report = {
                **original,
                "run_id": f"{source_id:032x}",
                "source_kind": "novel",
                "source_id": source_id,
                "idempotency_key_hash": f"{source_id:064x}",
                "shared_job_id": None,
                "shared_log_id": None,
                "shared_epoch": None,
            }
            conn.execute(
                "INSERT INTO work_analysis_runs("
                + ",".join(report)
                + ") VALUES("
                + ",".join("?" for _ in report)
                + ")",
                tuple(report.values()),
            )
            conn.execute(
                "INSERT INTO preference_reading_feedback VALUES('novel',?,'liked','',1,'2026-11-19')",
                (source_id,),
            )
    run = create_summary(env)
    assert run["source_count"] == source_count
    binding = binding_for(env, run["run_id"])
    queries = []
    env.db.conn.set_trace_callback(queries.append)

    def forbidden(*args, **kwargs):
        pytest.fail("Stable heartbeat re-read private reports")

    monkeypatch.setattr(storage, "capture_sources", forbidden)
    started = time.perf_counter()
    try:
        for _ in range(10):
            with env.db.read_transaction() as conn:
                row = storage.require_bound(conn, binding)
                assert "sources_json" not in row.keys() and "result_json" not in row.keys()
    finally:
        env.db.conn.set_trace_callback(None)
    elapsed = (time.perf_counter() - started) * 1000 / 10
    source_queries = [q for q in queries if "FROM preference_reading_feedback f" in q]
    assert len(source_queries) == 10
    assert not any("r.report_json," in q or "sources_json" in q for q in queries)
    record_property("heartbeat_ms", elapsed)
    print(json.dumps({"sources": source_count, "heartbeat_ms": round(elapsed, 3), "report_payload_reads": 0}))


@pytest.mark.parametrize(
    "mutation", ["feedback", "text_same_hash", "report", "report_status", "source_delete"]
)
def test_revision_guard_rejects_changes_before_model_send(env, mutation):
    run = create_summary(env)
    binding = binding_for(env, run["run_id"])
    with env.db.transaction() as conn:
        if mutation == "feedback":
            conn.execute("UPDATE preference_reading_feedback SET reaction='disliked',version=version+1")
        elif mutation == "text_same_hash":
            conn.execute("UPDATE novel_texts SET text_raw=text_raw || 'changed' WHERE novel_id=1")
        elif mutation == "report":
            conn.execute(
                "UPDATE work_analysis_runs SET report_json=report_json || ' ' WHERE run_id=?", (env.run_id,)
            )
        elif mutation == "report_status":
            conn.execute("UPDATE work_analysis_runs SET status='failed' WHERE run_id=?", (env.run_id,))
        else:
            conn.execute("DELETE FROM novels WHERE novel_id=1")
    with env.db.read_transaction() as conn, pytest.raises(WorkAnalysisError):
        storage.require_bound(conn, binding)


def test_revision_migration_preserves_legacy_paid_profile(env):
    run = create_summary(env)
    execute(env, run["run_id"])
    with env.db.transaction() as conn:
        before = storage.get_run(conn, run["run_id"])["source_fingerprint"]
        conn.execute("ALTER TABLE preference_summary_runs DROP COLUMN source_revision")
        storage.migrate_preference_learning(conn)
        row = storage.get_run(conn, run["run_id"])
        assert row["source_fingerprint"] == before and row["source_revision"]
        assert row["status"] == "succeeded" and not storage.is_stale(conn, row)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_restoring_report_status_does_not_revive_old_summary(env):
    run = create_summary(env)
    binding = binding_for(env, run["run_id"])
    with env.db.transaction() as conn:
        conn.execute("UPDATE work_analysis_runs SET status='failed' WHERE run_id=?", (env.run_id,))
        conn.execute("UPDATE work_analysis_runs SET status='succeeded' WHERE run_id=?", (env.run_id,))
    with env.db.read_transaction() as conn, pytest.raises(WorkAnalysisError):
        storage.require_bound(conn, binding)
    assert env.learning.detail(run["run_id"])["stale"]
    assert next(item for item in env.learning.state()["runs"] if item["run_id"] == run["run_id"])["stale"]

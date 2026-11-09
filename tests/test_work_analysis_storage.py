from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from pixiv_novel_sync.ai.usage import ProviderUsage
from pixiv_novel_sync.models import NovelTextRecord
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.storage.work_analysis import common as work_common
from pixiv_novel_sync.storage.work_analysis.schema import migrate_work_analysis
from pixiv_novel_sync.work_analysis.artifacts import claim_identity
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError, WorkCallBinding, WorkLease
from pixiv_novel_sync.work_analysis.configuration import snapshot_work_configuration
from pixiv_novel_sync.work_analysis.manifest import capture_manifest, preview_fingerprint, validate_selection
from pixiv_novel_sync.work_analysis.planning import (
    materialize_unit, plan_original_units, plan_reduction_level, seal_unit,
)
from pixiv_novel_sync.work_analysis.evidence import validate_unit_output


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


@pytest.mark.parametrize("field", ["input_fingerprint", "unit_id"])
def test_create_rejects_tampered_original_seal_with_recomputed_preview(db, field):
    case = prepare_case(db)
    args = deepcopy(case["arguments"])
    args["plan"]["original_units"][0][field] = "f" * 64
    args["preview_fingerprint"] = preview_fingerprint(
        args["selection"], args["manifest"], args["plan"], args["configuration"], args["budget"],
    )
    with pytest.raises(WorkAnalysisError, match="invalid_plan"):
        db.create_work_analysis(**args)
    for table in ("runs", "sources", "units", "requests"):
        assert db.conn.execute(f"SELECT COUNT(*) FROM work_analysis_{table}").fetchone()[0] == 0


def test_create_freezes_plan_without_replay_body_or_observation_time_identity(db):
    case = prepare_case(db)
    db.conn.execute("UPDATE novel_texts SET fetched_at='2030-01-01 00:00:00' WHERE novel_id=10")
    run = db.create_work_analysis(**case["arguments"])
    private = db.get_work_analysis(run["run_id"], include_private=True)
    assert private["plan"] == case["arguments"]["plan"]
    assert private["units"][0]["unit"] == case["arguments"]["plan"]["original_units"][0]
    for table in ("runs", "sources", "units", "requests"):
        rows = [dict(row) for row in db.conn.execute(f"SELECT * FROM work_analysis_{table}")]
        assert case["text"] not in json.dumps(rows)
    db.conn.execute("UPDATE work_analysis_runs SET request_limit=9 WHERE run_id=?", (run["run_id"],))
    assert db.lookup_work_analysis_idempotency("a" * 32, case["identity"])["run_id"] == run["run_id"]


def test_create_rechecks_configuration_and_required_confirmations(db):
    case = prepare_case(db)
    args = deepcopy(case["arguments"])
    args["confirmations"]["external_send_confirmed"] = False
    with pytest.raises(WorkAnalysisError, match="confirmation_required"):
        db.create_work_analysis(**args)
    db.conn.execute("UPDATE ai_agents SET system_prompt='Changed synthetic instructions' WHERE id=?",
                    (case["agent_id"],))
    with pytest.raises(WorkAnalysisError, match="preview_conflict"):
        db.create_work_analysis(**case["arguments"])
    assert db.conn.execute("SELECT COUNT(*) FROM work_analysis_runs").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["same", "fetched", "metadata", "body", "hash"])
def test_source_changes_revoke_only_actual_body_or_hash_changes(db, change):
    case = create_case(db)
    run_id = case["run"]["run_id"]
    if change == "same":
        db.conn.execute("UPDATE novel_texts SET text_raw=text_raw,text_hash=text_hash WHERE novel_id=10")
    elif change == "fetched":
        db.conn.execute("UPDATE novel_texts SET fetched_at='2030-01-01 00:00:00' WHERE novel_id=10")
    elif change == "metadata":
        db.conn.execute("UPDATE novels SET title='Refreshed metadata' WHERE novel_id=10")
    elif change == "body":
        db.conn.execute("UPDATE novel_texts SET text_raw='Changed body with old stored hash' WHERE novel_id=10")
    else:
        db.conn.execute("UPDATE novel_texts SET text_hash=? WHERE novel_id=10", ("f" * 64,))
    result = db.get_work_analysis(run_id, include_private=True)
    changed = change in {"body", "hash"}
    assert result["status"] == ("invalidated" if changed else "queued")
    assert result["epoch"] == int(changed)
    assert result["version"] == 1 + int(changed)


@pytest.mark.parametrize("operation", ["text", "novel", "user", "bulk_user", "series", "bodyless"])
def test_source_deletions_tombstone_and_clear_private_data(db, operation):
    case = prepare_case(db, series=True)
    if operation == "bodyless":
        db.conn.execute(
            "INSERT INTO novels(novel_id,user_id,series_id,title,visible,restrict_value,x_restrict,"
            "text_length,total_bookmarks,total_views,tags_json,raw_json,meta_hash) "
            "VALUES(11,1,9,'Bodyless synthetic chapter',1,'public',0,0,0,0,'[]','{}','bodyless')"
        )
        with db.read_transaction() as conn:
            manifest, texts = capture_manifest(conn, case["arguments"]["selection"])
            _, prompt, preflight = snapshot_work_configuration(conn, case["agent_id"], 64)
        args = case["arguments"]
        args["manifest"] = manifest
        args["plan"] = plan_original_units(manifest, texts, preflight, agent_prompt=prompt, output_tokens=64)
        args["preview_fingerprint"] = preview_fingerprint(
            args["selection"], manifest, args["plan"], args["configuration"], args["budget"],
        )
    run = db.create_work_analysis(**case["arguments"])
    if operation == "bulk_user":
        db.delete_user(1)
    else:
        sql = {
            "text": "DELETE FROM novel_texts WHERE novel_id=10",
            "novel": "DELETE FROM novels WHERE novel_id=10",
            "user": "DELETE FROM users WHERE user_id=1",
            "series": "DELETE FROM series WHERE series_id=9",
            "bodyless": "DELETE FROM novels WHERE novel_id=11",
        }[operation]
        db.conn.execute(sql)
    private = db.get_work_analysis(run["run_id"], include_private=True)
    assert private["deleted"] and private["reason"] == "source_deleted"
    for field in ("manifest", "plan", "configuration", "selection", "confirmations", "report"):
        assert private[field] is None
    assert private["units"][0]["unit"] is None
    assert db.conn.execute("SELECT COUNT(*) FROM work_analysis_sources").fetchone()[0] == 0
    assert db.list_work_analyses()["total"] == 0
    with pytest.raises(WorkAnalysisError, match="work_deleted"):
        db.create_work_analysis(**case["arguments"])
    with pytest.raises(sqlite3.IntegrityError, match="deleted"):
        db.conn.execute("UPDATE work_analysis_runs SET deleted_at=NULL WHERE run_id=?", (run["run_id"],))
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_added_series_member_marks_archive_outdated_without_mixing_manifest(db):
    case = create_case(db, series=True)
    db.conn.execute(
        "INSERT INTO novels(novel_id,user_id,series_id,title,visible,restrict_value,x_restrict,"
        "text_length,total_bookmarks,total_views,tags_json,raw_json,meta_hash) "
        "VALUES(11,1,9,'Additional chapter',1,'public',0,0,0,0,'[]','{}','additional')"
    )
    detail = db.get_work_analysis(case["run"]["run_id"])
    assert detail["archive_outdated"] and detail["status"] == "queued"
    assert [row["novel_id"] for row in detail["manifest"]["chapters"]] == [10]


def test_control_projection_is_narrow_and_shared_activation_stays_private(db):
    case = create_case(db)
    run_id = case["run"]["run_id"]
    db.conn.execute(
        "UPDATE work_analysis_runs SET epoch=1,owner_token=?,shared_job_id=?,shared_epoch=1 WHERE run_id=?",
        ("c" * 64, "d" * 32, run_id),
    )
    statements = []
    db.conn.set_trace_callback(statements.append)
    try:
        control = db.get_work_analysis_control(run_id)
    finally:
        db.conn.set_trace_callback(None)
    assert set(control) == {
        "run_id", "version", "status", "reason", "epoch", "owner_token", "deleted_at", "invalidated_at",
        "manifest_fingerprint", "configuration_fingerprint", "request_limit", "token_limit", "active_seconds_limit",
    }
    selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1 and "*" not in selects[0] and "_json" not in selects[0]
    for detail in (db.get_work_analysis(run_id), db.list_work_analyses()["items"][0]):
        assert not {"owner_token", "epoch", "shared_job_id", "shared_log_id", "shared_epoch", "report"} & detail.keys()
    assert db.queued_work_analysis_ids() == [run_id]


def test_report_evidence_resolves_server_offsets_and_rejects_changed_source(db):
    case = create_case(db, body="\U0001f600 Synthetic evidence is unique. Remaining body.")
    unit = case["arguments"]["plan"]["original_units"][0]
    _, blocks = materialize_unit(case["arguments"]["manifest"], unit, case["texts"], {},
                                 agent_prompt=case["prompt"], output_tokens=64)
    artifact = validate_unit_output(json.dumps({
        "unit_id": unit["unit_id"], "summary": "Synthetic conclusion.", "no_findings_reason": None,
        "claims": [{"dimension": "theme", "certainty": "interpretation", "label": "Synthetic",
                    "statement": "A unique statement.",
                    "quotes": [{"block_id": next(iter(blocks)), "quote": "Synthetic evidence"}]}],
        "counterexamples": [], "uncertainties": [],
    }), unit, blocks, {})
    run_id = case["run"]["run_id"]
    with pytest.raises(WorkAnalysisError, match="report_not_ready"):
        db.get_work_analysis_report(run_id)
    # Seed a validated artifact; report publication belongs to Task 3B.
    db.conn.execute("UPDATE work_analysis_runs SET status='succeeded',report_json=? WHERE run_id=?",
                    (json.dumps({"artifact": artifact, "units": [{"unit_id": unit["unit_id"]}]}), run_id))
    evidence_id = next(iter(artifact["evidence"]))
    result = db.get_work_analysis_evidence(run_id, evidence_id)
    assert result["jump"]["start"] == 2
    assert result["jump"]["end"] == 20
    assert result["jump"]["context_text"] == case["text"]
    with pytest.raises(WorkAnalysisError, match="not_found"):
        db.get_work_analysis_evidence(run_id, "f" * 64)
    db.conn.execute("UPDATE novel_texts SET text_raw='Changed' WHERE novel_id=10")
    assert db.get_work_analysis_report(run_id)["report_stale"]
    with pytest.raises(WorkAnalysisError, match="source_changed"):
        db.get_work_analysis_evidence(run_id, evidence_id)


@pytest.mark.parametrize("unit", [None, {}, {"unit_id": []}])
def test_create_rejects_malformed_original_units_with_domain_error(db, unit):
    case = prepare_case(db)
    args = deepcopy(case["arguments"])
    args["plan"]["original_units"] = [unit]
    with pytest.raises(WorkAnalysisError):
        db.create_work_analysis(**args)
    assert db.conn.execute("SELECT COUNT(*) FROM work_analysis_runs").fetchone()[0] == 0


def test_attachment_is_not_usage_and_unknown_request_is_not_zero(db):
    case = create_case(db)
    run_id = case["run"]["run_id"]
    unit_id = case["arguments"]["plan"]["original_units"][0]["unit_id"]
    values = ("synthetic-child", run_id, unit_id, case["arguments"]["configuration"]["configuration_fingerprint"], "c" * 64)
    db.conn.execute(
        "INSERT INTO work_analysis_requests(child_job_id,request_ordinal,entry_kind,run_id,unit_id,epoch,"
        "try_no,configuration_fingerprint,child_owner_hash) VALUES(?,0,'attachment',?,?,1,1,?,?)", values,
    )
    assert db.get_work_analysis(run_id)["usage_summary"]["network_request_count"] == 0
    db.conn.execute(
        "INSERT INTO work_analysis_requests(child_job_id,request_ordinal,entry_kind,run_id,unit_id,epoch,"
        "try_no,configuration_fingerprint,child_owner_hash,planned_tokens,usage_json) "
        "VALUES(?,1,'request',?,?,1,1,?,?,100,'{}')", values,
    )
    summary = db.get_work_analysis(run_id)["usage_summary"]
    assert summary["network_request_count"] == 1 and summary["unreported_requests"] == 1
    assert summary["known_total_tokens"] is None
    assert db.list_work_analysis_units(run_id)["items"][0]["request_count"] == 1


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


def reducer_checkpoint_case(db, clock):
    body = "Synthetic evidence is unique. Counterexample only.\nOther extracted material."
    case = prepare_case(db, body=body)
    manifest = case["arguments"]["manifest"]
    chapter = manifest["chapters"][0]
    cut = body.index("Other")
    originals = [seal_unit({
        "kind": "extract", "is_final": False, "level": 0,
        "primary_ranges": [{"novel_id": 10, "text_hash": chapter["text_hash"], "start": start, "end": end}],
        "context_ranges": [], "child_unit_ids": [], "claim_refs": [],
    }, manifest_fingerprint=manifest["manifest_fingerprint"]) for start, end in ((0, cut), (cut, len(body)))]
    plan = {**case["arguments"]["plan"], "strategy": "segmented", "original_units": originals, "minimum_requests": 3}
    args = {**case["arguments"], "plan": plan}
    args["preview_fingerprint"] = preview_fingerprint(args["selection"], manifest, plan, args["configuration"], args["budget"])
    case["arguments"] = args
    case["run"] = db.create_work_analysis(**args)
    lease = db.claim_work_analysis(case["run"]["run_id"], now=clock[0], slice_seconds=30)
    children = {}
    for index, unit in enumerate(originals):
        unit, artifact = ready_checkpoint(db, case, lease, clock, unit=unit, findings=False, child_id=f"extract-{index}")
        if index == 0:
            _payload, blocks = materialize_unit(manifest, unit, case["texts"], {}, agent_prompt=case["prompt"], output_tokens=64)
            block_id = next(iter(blocks))
            raw = {
                "unit_id": unit["unit_id"], "summary": "Synthetic support and counterexample.",
                "no_findings_reason": None,
                "claims": [{"dimension": "theme", "certainty": "direct", "label": "Support",
                            "statement": "The support is explicit.",
                            "quotes": [{"block_id": block_id, "quote": "Synthetic evidence is unique."}]}],
                "counterexamples": [{"statement": "A separate counterexample.",
                                     "quotes": [{"block_id": block_id, "quote": "Counterexample only."}]}],
                "uncertainties": [],
            }
            artifact = validate_unit_output(json.dumps(raw), unit, blocks, {})
        persist_checkpoint(db, case, lease, unit, artifact)
        children[unit["unit_id"]] = artifact
    with db.read_transaction() as conn:
        _configuration, prompt, preflight = snapshot_work_configuration(conn, case["agent_id"], 64)
    derived = plan_reduction_level(lease.run_id, 1, originals, children, preflight, agent_prompt=prompt, output_tokens=64)
    db.add_work_analysis_units(lease, derived)
    unit, artifact = ready_checkpoint(db, case, lease, clock, unit=derived[0], children=children,
                                      findings=False, child_id="final-reducer")
    source = children[originals[0]["unit_id"]]
    selected = source["claims"][0]
    raw = {
        "unit_id": unit["unit_id"], "summary": "A supported synthetic reduction.", "no_findings_reason": None,
        "claims": [{key: selected[key] for key in ("dimension", "certainty", "label", "statement", "evidence_ids")}],
        "counterexamples": source["counterexamples"], "uncertainties": [],
    }
    raw["claims"][0]["source_claim_ids"] = [selected["claim_id"]]
    artifact = validate_unit_output(json.dumps(raw), unit, {}, children)
    return case, lease, unit, children, raw, artifact


def test_reducer_checkpoint_rejects_counterexample_only_claim_without_writes(db, clock):
    from pixiv_novel_sync.work_analysis.artifacts import validate_artifact

    case, lease, unit, children, raw, artifact = reducer_checkpoint_case(db, clock)
    before = db.get_work_analysis(lease.run_id, include_private=True)
    ids = artifact["counterexamples"][0]["evidence_ids"]
    raw["claims"][0]["evidence_ids"] = ids
    with pytest.raises(WorkAnalysisError, match="artifact_invalid"):
        validate_unit_output(json.dumps(raw), unit, {}, children)
    artifact["claims"][0]["evidence_ids"] = ids
    artifact["claims"][0]["claim_id"] = claim_identity(unit["unit_id"], artifact["claims"][0])
    artifact["evidence"] = {key: artifact["evidence"][key] for key in ids}
    assert validate_artifact(artifact, final=True) == artifact
    with pytest.raises(WorkAnalysisError, match="validation_failed") as caught:
        persist_checkpoint(db, case, lease, unit, artifact)
    assert caught.value.status == 409
    after = db.get_work_analysis(lease.run_id, include_private=True)
    assert after == before
    assert after["units"][-1]["artifact"] is None and after["report"] is None


def test_reducer_checkpoint_publishes_selected_claim_and_counterexample_evidence(db, clock):
    case, lease, unit, _children, _raw, artifact = reducer_checkpoint_case(db, clock)
    assert artifact["claims"] and artifact["counterexamples"]
    assert artifact["claims"][0]["evidence_ids"] != artifact["counterexamples"][0]["evidence_ids"]
    persist_checkpoint(db, case, lease, unit, artifact)
    db.publish_work_analysis(lease, artifact)
    report = db.get_work_analysis_report(lease.run_id)
    assert report["artifact"] == artifact
    assert report["synthesis_complete"] is True and report["coverage"]["complete"] is True


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

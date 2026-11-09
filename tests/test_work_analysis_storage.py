from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.storage.work_analysis.schema import migrate_work_analysis
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError
from pixiv_novel_sync.work_analysis.configuration import snapshot_work_configuration
from pixiv_novel_sync.work_analysis.manifest import capture_manifest, preview_fingerprint, validate_selection
from pixiv_novel_sync.work_analysis.planning import materialize_unit, plan_original_units
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

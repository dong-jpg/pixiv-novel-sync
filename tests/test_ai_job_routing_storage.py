from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from pixiv_novel_sync.storage.ai.core import (
    AIJobConflictError,
    AIRouteBudgetExhausted,
)
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "job-routing.db")
    database.init_schema()
    try:
        yield database
    finally:
        database.close()


def valid_attempt_data(*, stage: str = "main", model_key: str = "model-a") -> dict:
    return {
        "pool_id": None,
        "provider_id": 1,
        "provider_model_id": None,
        "pool_version_snapshot": None,
        "pool_position_snapshot": None,
        "model_key": model_key,
        "pool_name_snapshot": None,
        "provider_name_snapshot": "Provider A",
        "agent_config_hash": "a" * 64,
        "provider_config_hash": "b" * 64,
        "candidate_list_hash": "c" * 64,
        "stage": stage,
    }


def create_owned_job(
    db: Database,
    job_id: str = "job",
    *,
    owner: str = "owner",
    stage: str = "main",
) -> None:
    db.create_ai_job(
        job_id,
        "continue",
        1,
        {"chapter_id": 1},
        owner_token=owner,
        stage=stage,
        route_deadline_at="2099-01-01 00:00:00",
    )


def create_metadata_job(db, job_id="metadata", *, stage="main"):
    db.create_ai_job(
        job_id, "general", 1,
        {"audit_version": 1, "business_type": "general", "input_chars": 3,
         "message_count": 1, "input_fingerprint": "a" * 64, "messages": "INPUT_SENTINEL"},
        owner_token="owner", stage=stage, route_deadline_at="2099-01-01 00:00:00",
        retention_policy="metadata_only", validation_status="not_requested",
    )


@pytest.mark.parametrize("method", ["cas", "update"])
@pytest.mark.parametrize("status", ["succeeded", "failed", "partial", "cancelled"])
def test_metadata_terminal_writes_enforce_sql_null_and_safe_errors(db, method, status):
    create_metadata_job(db)
    attempt = db.allocate_ai_model_attempt("metadata", "owner", valid_attempt_data())
    assert db.mark_ai_job_output_started("metadata", attempt, "owner", 0)
    # Existing contamination must not survive COALESCE or omitted output fields.
    db.conn.execute("UPDATE ai_jobs SET output_text='OUTPUT_SENTINEL', output_json='{}'")
    db.conn.commit()
    kwargs = {"output_json": {"raw": "OUTPUT_SENTINEL"},
              "error_message": "INPUT_SENTINEL upstream body"}
    if method == "cas":
        assert db.finish_ai_job_cas("metadata", "owner", status, **kwargs)
    else:
        db.update_ai_job("metadata", status, **kwargs)
    row = dict(db.conn.execute("SELECT * FROM ai_jobs WHERE job_id='metadata'").fetchone())
    assert row["output_text"] is None and row["output_json"] is None
    assert row["error_message"] == "AI invocation failed"
    assert "SENTINEL" not in json.dumps(row)
    assert db.get_ai_job("metadata")["validation_status"] == "not_requested"


def test_metadata_partial_requires_persisted_main_output_started(db):
    create_metadata_job(db)
    attempt = db.allocate_ai_model_attempt("metadata", "owner", valid_attempt_data())
    with pytest.raises(ValueError):
        db.finish_ai_job_cas("metadata", "owner", "partial", output_text="unpublished")
    assert db.mark_ai_job_output_started("metadata", attempt, "owner", 0)
    assert db.finish_ai_job_cas("metadata", "owner", "partial")
    assert db.get_ai_job("metadata")["output_text"] is None


def test_metadata_attempt_error_categories_and_validation_are_safe(db):
    create_metadata_job(db)
    attempt = db.allocate_ai_model_attempt("metadata", "owner", valid_attempt_data())
    with pytest.raises(ValueError):
        db.finish_ai_model_attempt("metadata", attempt, "owner", "failed",
                                   error_category="INPUT_SENTINEL")
    assert db.finish_ai_model_attempt(
        "metadata", attempt, "owner", "succeeded", error_message=None,
    )
    assert db.finish_ai_job_cas(
        "metadata", "owner", "failed", validation_status="failed",
        error_category="validation_failed", error_message="OUTPUT_SENTINEL",
        output_json=object(),
    )
    saved = db.get_ai_job("metadata")
    assert saved["validation_status"] == "failed"
    assert saved["error_message"] == "validation_failed"
    assert saved["attempts"][0]["status"] == "succeeded"
    assert saved["attempts"][0]["error_message"] is None
    assert saved["can_continue"] is False


@pytest.mark.parametrize("http_status, expected", [(503, "network (HTTP 503)"),
                                                 (True, "network"), (999, "network")])
def test_metadata_attempt_error_uses_only_safe_category_and_http_status(db, http_status, expected):
    create_metadata_job(db)
    attempt = db.allocate_ai_model_attempt("metadata", "owner", valid_attempt_data())
    assert db.finish_ai_model_attempt(
        "metadata", attempt, "owner", "partial", output_started=True,
        error_category="network", error_message="OUTPUT_SENTINEL", http_status=http_status,
    )
    row = db.list_ai_job_model_attempts("metadata")[0]
    assert row["error_message"] == expected and row["output_started"] is True


def test_metadata_audit_is_server_selected_bounded_and_owner_scoped(db):
    with pytest.raises(ValueError):
        db.create_ai_job("untrusted", "general", None, {"retention_policy": "metadata_only"})
    with pytest.raises(ValueError):
        db.create_ai_job("escape", "general", None, {}, retention_policy="keyword_compat")
    for data in ({"input_chars": True}, {"business_type": {"text": "INPUT_SENTINEL"}},
                 {"planner_version": "INPUT_SENTINEL"}, {"input_fingerprint": "bad"}):
        with pytest.raises(ValueError):
            db.create_ai_job("invalid", "general", None, data, retention_policy="metadata_only")
    create_metadata_job(db)
    assert not db.replace_ai_job_audit("metadata", "wrong", {"input_chars": 99})
    assert db.replace_ai_job_audit("metadata", "owner", {
        "input_chars": 4, "input_fingerprint": "b" * 64,
        "retention_policy": "keyword_compat", "raw": "INPUT_SENTINEL",
    })
    saved = db.get_ai_job("metadata")["input"]
    assert saved["retention_policy"] == "metadata_only"
    assert saved["input_fingerprint"] == "b" * 64 and "raw" not in saved
    assert db.finish_ai_job_cas("metadata", "owner", "succeeded")
    assert not db.replace_ai_job_audit("metadata", "owner", {"input_chars": 99})


def test_metadata_stale_recovery_discards_preexisting_body_and_keeps_unknown_usage(db):
    create_metadata_job(db)
    attempt = db.allocate_ai_model_attempt("metadata", "owner", valid_attempt_data())
    assert db.claim_ai_job_network_request("metadata", "owner", attempt) == 1
    assert db.mark_ai_job_output_started("metadata", attempt, "owner", 0)
    db.conn.execute("UPDATE ai_jobs SET lease_until='2000-01-01', heartbeat_at='2000-01-01', "
                    "output_text='OUTPUT_SENTINEL', output_json='{}', error_message='INPUT_SENTINEL'")
    db.conn.commit()
    assert db.fail_stale_ai_jobs() == 1
    row = dict(db.conn.execute("SELECT * FROM ai_jobs WHERE job_id='metadata'").fetchone())
    assert row["status"] == "partial" and row["output_text"] is None and row["output_json"] is None
    assert row["error_message"] == "process_interrupted"
    assert "SENTINEL" not in json.dumps(row)
    assert db.list_ai_job_model_attempts("metadata")[0]["error_message"] == "process_interrupted"
    assert db.get_ai_job_usage_summary("metadata")["unreported_requests"] == 1


def test_usage_reservation_is_durable_owner_scoped_and_idempotent(db):
    create_owned_job(db, "usage")
    attempt = db.allocate_ai_model_attempt("usage", "owner", valid_attempt_data())
    assert db.claim_ai_job_network_request("usage", "owner", attempt) == 1
    assert db.claim_ai_job_network_request("usage", "owner", attempt) == 2
    other = sqlite3.connect(db.path)
    try:
        count = other.execute("SELECT network_request_count FROM ai_jobs WHERE job_id='usage'").fetchone()[0]
        pending = json.loads(other.execute("SELECT usage_json FROM ai_job_model_attempts").fetchone()[0])
    finally:
        other.close()
    assert count == 2 and len(pending["requests"]) == 2
    assert pending["requests"][0]["missing_reason"] == "pending"
    assert pending["requests"][0]["input_tokens"] is None
    complete = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                "complete": True, "missing_reason": None}
    for _ in range(2):
        assert db.record_ai_job_request_usage("usage", "owner", attempt, 1, complete)
    assert db.record_ai_job_request_usage("usage", "owner", attempt, 1, {
        "input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
        "complete": False, "missing_reason": "partial_report",
    })
    assert not db.record_ai_job_request_usage("usage", "wrong", attempt, 1, complete)
    assert not db.record_ai_job_request_usage("usage", "owner", attempt, 3, complete)
    assert not db.record_ai_job_request_usage("usage", "owner", attempt + 1, 1, complete)
    summary = db.get_ai_job_usage_summary("usage")
    assert summary == {"known_input_tokens": 10, "known_output_tokens": 2,
                       "known_total_tokens": 12, "reported_requests": 1,
                       "unreported_requests": 1, "complete": False}
    projection = db.list_ai_job_model_attempts("usage")[0]
    assert "usage_json" not in projection
    assert len(projection["usage"]["requests"][0]) == 9


def test_usage_late_observation_updates_only_reserved_same_owner_terminal_rows(db):
    create_owned_job(db, "usage")
    attempt = db.allocate_ai_model_attempt("usage", "owner", valid_attempt_data())
    db.claim_ai_job_network_request("usage", "owner", attempt)
    assert db.finish_ai_model_attempt("usage", attempt, "owner", "succeeded")
    assert db.finish_ai_job_cas("usage", "owner", "cancelled")
    complete = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                "complete": True, "missing_reason": None}
    assert db.record_ai_job_request_usage("usage", "owner", attempt, 1, complete)
    row = db.get_ai_job("usage")
    assert row["status"] == "cancelled" and row["attempts"][0]["status"] == "succeeded"
    assert row["usage_summary"]["known_total_tokens"] == 0
    assert row["usage_summary"]["complete"] is True
    with pytest.raises(AIJobConflictError):
        db.claim_ai_job_network_request("usage", "owner", attempt)
    db.delete_ai_job("usage")
    assert not db.record_ai_job_request_usage("usage", "owner", attempt, 1, complete)


def test_usage_reservation_rejects_nested_transaction_and_rolls_back_counter(db):
    create_owned_job(db, "usage")
    attempt = db.allocate_ai_model_attempt("usage", "owner", valid_attempt_data())
    with db.transaction():
        with pytest.raises(AIJobConflictError):
            db.claim_ai_job_network_request("usage", "owner", attempt)
    with pytest.raises(AIJobConflictError):
        db.claim_ai_job_network_request("usage", "owner", attempt + 1)
    db.conn.execute(
        "CREATE TRIGGER reject_usage BEFORE UPDATE OF usage_json ON ai_job_model_attempts "
        "BEGIN SELECT RAISE(ABORT, 'usage write rejected'); END"
    )
    db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="usage write rejected"):
        db.claim_ai_job_network_request("usage", "owner", attempt)
    saved = db.get_ai_job("usage")
    assert saved["network_request_count"] == 0 and saved["attempts"][0]["usage"] is None


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_usage_reservation_checks_durable_stop_in_its_transaction(db, stop):
    create_owned_job(db, "usage")
    attempt = db.allocate_ai_model_attempt("usage", "owner", valid_attempt_data())
    if stop == "cancel":
        assert db.request_ai_job_cancel("usage")
    else:
        db.conn.execute("UPDATE ai_jobs SET route_deadline_at='2000-01-01 00:00:00'")
        db.conn.commit()
    with pytest.raises(AIJobConflictError):
        db.claim_ai_job_network_request("usage", "owner", attempt)
    saved = db.get_ai_job("usage")
    assert saved["network_request_count"] == 0 and saved["attempts"][0]["usage"] is None


def test_usage_payload_rejects_unknown_keys_invalid_counters_and_byte_overflow(db):
    create_owned_job(db, "usage")
    attempt = db.allocate_ai_model_attempt("usage", "owner", valid_attempt_data())
    for ordinal in range(1, 33):
        assert db.claim_ai_job_network_request("usage", "owner", attempt) == ordinal
    with pytest.raises(AIRouteBudgetExhausted):
        db.claim_ai_job_network_request("usage", "owner", attempt)
    before = db.conn.execute("SELECT usage_json FROM ai_job_model_attempts").fetchone()[0]
    huge = 10 ** 4000
    invalid = [{"input_tokens": value} for value in (-1, True, 1.5, "10")]
    invalid += [{"missing_reason": "x" * 16385}, {"raw": "x" * 16385},
                {"input_tokens": huge, "output_tokens": huge, "total_tokens": huge * 2,
                 "cached_input_tokens": huge, "cache_creation_input_tokens": huge,
                 "reasoning_output_tokens": huge, "complete": True, "missing_reason": None}]
    for payload in invalid:
        with pytest.raises(ValueError):
            db.record_ai_job_request_usage("usage", "owner", attempt, 1, payload)
        assert db.conn.execute("SELECT usage_json FROM ai_job_model_attempts").fetchone()[0] == before
    assert len(before.encode("utf-8")) <= 16384
    assert db.get_ai_job_usage_summary("usage")["unreported_requests"] == 32


def test_usage_corrupt_envelope_projection_is_unknown_not_raw(db):
    create_owned_job(db, "usage")
    attempt = db.allocate_ai_model_attempt("usage", "owner", valid_attempt_data())
    db.claim_ai_job_network_request("usage", "owner", attempt)
    db.conn.execute("UPDATE ai_job_model_attempts SET usage_json=?", (
        json.dumps({"requests": [{"request_ordinal": 1, "raw": "INPUT_SENTINEL"}]}),
    ))
    db.conn.commit()
    saved = db.get_ai_job("usage")
    assert saved["attempts"][0]["usage"] is None
    assert "SENTINEL" not in json.dumps(saved)
    assert saved["usage_summary"]["unreported_requests"] == 1
    with pytest.raises(ValueError):
        db.claim_ai_job_network_request("usage", "owner", attempt)
    assert db.get_ai_job("usage")["network_request_count"] == 1


def test_request_ai_job_cancel_marks_running_job_once(db: Database) -> None:
    create_owned_job(db, "cancel-me")
    assert db.request_ai_job_cancel("cancel-me") is True
    assert db.request_ai_job_cancel("cancel-me") is True
    row = db.conn.execute(
        "SELECT cancel_requested, status FROM ai_jobs WHERE job_id = 'cancel-me'"
    ).fetchone()
    assert row["status"] == "running"
    assert row["cancel_requested"] == 1
    assert db.ai_job_should_stop("cancel-me") is True
    assert db.request_ai_job_cancel("missing") is False

    db.finish_ai_job_cas("cancel-me", "owner", "succeeded")
    assert db.request_ai_job_cancel("cancel-me") is False


def canonical_snapshot() -> tuple[str, str]:
    value = {
        "binding_version": 1,
        "candidates": [
            {
                "candidate_index": 0,
                "provider_id": 1,
                "provider_name": "Provider A",
                "model_key": "model-a",
            }
        ],
    }
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def resume_parent(db, policy, *, audit=None):
    if policy == "metadata_only":
        create_metadata_job(db, "parent")
        if audit is not None:
            assert db.replace_ai_job_audit("parent", "owner", audit)
        task_type = "general"
    elif policy == "keyword_compat":
        task_type = "keyword_clean"
        db.create_ai_job("parent", task_type, 1, {"raw_keywords": ["input"], "tags": ["tag"]},
                         owner_token="owner", retention_policy=policy)
    else:
        task_type = "continue"
        create_owned_job(db, "parent")
    snapshot, fingerprint = canonical_snapshot()
    assert db.set_ai_job_candidate_snapshot("parent", "owner", snapshot, fingerprint)
    assert db.finish_ai_job_cas("parent", "owner", "failed")
    return {"task_type": task_type, "agent_id": 1, "owner_token": "child-owner",
            "route_deadline_at": "2099-01-01 00:00:00", "parent_job_id": "parent",
            "idempotency_key": "resume", "candidate_snapshot": json.loads(snapshot),
            "candidate_snapshot_hash": fingerprint, "resume_candidate_index": 0}


def test_metadata_resume_child_inherits_only_trusted_audit_and_preserves_index(db):
    options = resume_parent(db, "metadata_only")
    child, created = db.create_or_get_ai_resume_job("child", input_data={
        "resume_candidate_index": 0, "retention_policy": "keyword_compat", "raw": "INPUT_SENTINEL",
    }, **options)
    assert created and child["input"]["retention_policy"] == "metadata_only"
    assert child["input"]["resume_candidate_index"] == 0
    assert child["input"]["input_fingerprint"] == "a" * 64
    assert child["validation_status"] == "not_requested"
    assert "INPUT_SENTINEL" not in child["input_json"]
    audit = dict(db.get_ai_job("parent")["input"], resume_candidate_index=12)
    assert db.replace_ai_job_audit("child", "child-owner", audit)
    assert db.get_ai_job("child")["input"]["resume_candidate_index"] == 0
    repeated, created = db.create_or_get_ai_resume_job("ignored", input_data={
        "resume_candidate_index": 0, "raw": "different ignored text",
    }, **options)
    assert not created and repeated["job_id"] == "child"
    with pytest.raises(AIJobConflictError):
        db.create_or_get_ai_resume_job("conflict", input_data={"resume_candidate_index": 1},
                                      **{**options, "resume_candidate_index": 1})
    with pytest.raises(AIJobConflictError):
        db.create_or_get_ai_resume_job("conflict", input_data={"resume_candidate_index": 0},
                                      **{**options, "task_type": "keyword_clean"})
    assert db.finish_ai_job_cas("child", "child-owner", "failed", output_text="OUTPUT_SENTINEL",
                                output_json={"raw": "OUTPUT_SENTINEL"}, error_message="INPUT_SENTINEL")
    row = dict(db.conn.execute("SELECT * FROM ai_jobs WHERE job_id='child'").fetchone())
    assert row["output_text"] is None and row["output_json"] is None
    assert row["error_message"] == "AI invocation failed"
    assert "SENTINEL" not in json.dumps(row)


def test_metadata_resume_retry_survives_audit_enrichment(db):
    options = resume_parent(db, "metadata_only", audit={"audit_version": 1})
    data = {"resume_candidate_index": 0, "retention_policy": "keyword_compat",
            "messages": [{"role": "user", "content": "INPUT_SENTINEL"}]}
    child, created = db.create_or_get_ai_resume_job("child", input_data=data, **options)
    assert created and child["input"] == {
        "audit_version": 1, "retention_policy": "metadata_only", "resume_candidate_index": 0,
    }
    assert db.replace_ai_job_audit("child", "child-owner", {
        "audit_version": 1, "business_type": "general", "input_chars": 3, "message_count": 1,
        "input_fingerprint": "a" * 64, "allowed_provider_ids": [2, 1, 2],
        "retention_policy": "keyword_compat", "resume_candidate_index": 12,
        "messages": data["messages"], "output_text": "OUTPUT_SENTINEL",
    })
    before = dict(db.conn.execute("SELECT * FROM ai_jobs WHERE job_id='child'").fetchone())
    repeated, created = db.create_or_get_ai_resume_job(
        "ignored", input_data=data, **{**options, "owner_token": "retry-owner"},
    )
    assert not created and repeated["job_id"] == child["job_id"]
    assert repeated["owner_token"] == "child-owner"
    assert repeated["input"]["resume_candidate_index"] == 0
    assert repeated["input"]["allowed_provider_ids"] == [1, 2]
    assert repeated["input"]["input_fingerprint"] == "a" * 64
    assert dict(db.conn.execute("SELECT * FROM ai_jobs WHERE job_id='child'").fetchone()) == before
    rows = [dict(row) for row in db.conn.execute("SELECT * FROM ai_jobs")]
    assert len(rows) == 2 and all(row["output_text"] is None for row in rows)
    serialized = json.dumps(rows)
    assert "SENTINEL" not in serialized and "keyword_compat" not in serialized
    assert db.conn.execute("SELECT COUNT(*) FROM ai_job_model_attempts").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["resume_candidate_index", "agent_id", "task_type", "snapshot"])
def test_metadata_resume_retry_rejects_changed_request_identity(db, change):
    options = resume_parent(db, "metadata_only")
    db.create_or_get_ai_resume_job("child", input_data={"resume_candidate_index": 0}, **options)
    assert db.replace_ai_job_audit("child", "child-owner", {"allowed_provider_ids": [1]})
    before = dict(db.conn.execute("SELECT * FROM ai_jobs WHERE job_id='child'").fetchone())
    if change == "snapshot":
        options["candidate_snapshot"]["candidates"][0]["model_key"] = "different-model"
        serialized = json.dumps(options["candidate_snapshot"], sort_keys=True, separators=(",", ":"))
        options["candidate_snapshot_hash"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    else:
        options[change] = {"resume_candidate_index": 1, "agent_id": 2, "task_type": "keyword_clean"}[change]
    with pytest.raises(AIJobConflictError):
        db.create_or_get_ai_resume_job(
            "conflict", input_data={"resume_candidate_index": options["resume_candidate_index"]}, **options,
        )
    assert dict(db.conn.execute("SELECT * FROM ai_jobs WHERE job_id='child'").fetchone()) == before
    assert db.conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 2


@pytest.mark.parametrize("index", [None, False, 0.0, "0", 1])
def test_metadata_resume_retry_rejects_changed_or_noninteger_stored_index(db, index):
    options = resume_parent(db, "metadata_only")
    child, _ = db.create_or_get_ai_resume_job("child", input_data={"resume_candidate_index": 0}, **options)
    child["input"]["resume_candidate_index"] = index
    db.conn.execute("UPDATE ai_jobs SET input_json=? WHERE job_id='child'", (json.dumps(child["input"]),))
    db.conn.commit()
    with pytest.raises(AIJobConflictError):
        db.create_or_get_ai_resume_job("conflict", input_data={"resume_candidate_index": 0}, **options)
    assert db.conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 2


@pytest.mark.parametrize("policy", [None, "keyword_compat"])
def test_metadata_resume_retry_rejects_stored_policy_mismatch(db, policy):
    options = resume_parent(db, "metadata_only")
    child, _ = db.create_or_get_ai_resume_job("child", input_data={"resume_candidate_index": 0}, **options)
    if policy is None:
        child["input"].pop("retention_policy")
    else:
        child["input"]["retention_policy"] = policy
    db.conn.execute("UPDATE ai_jobs SET input_json=? WHERE job_id='child'", (json.dumps(child["input"]),))
    db.conn.commit()
    with pytest.raises(AIJobConflictError):
        db.create_or_get_ai_resume_job("conflict", input_data={"resume_candidate_index": 0}, **options)
    assert db.conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 2


@pytest.mark.parametrize("change", ["stored_snapshot", "request_hash"])
def test_metadata_resume_retry_rejects_snapshot_fingerprint_mismatch(db, change):
    options = resume_parent(db, "metadata_only")
    db.create_or_get_ai_resume_job("child", input_data={"resume_candidate_index": 0}, **options)
    if change == "stored_snapshot":
        db.conn.execute("UPDATE ai_jobs SET candidate_snapshot_json='{}' WHERE job_id='child'")
        db.conn.commit()
        error = AIJobConflictError
    else:
        options["candidate_snapshot_hash"] = "b" * 64
        error = ValueError
    with pytest.raises(error):
        db.create_or_get_ai_resume_job("conflict", input_data={"resume_candidate_index": 0}, **options)
    assert db.conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 2


@pytest.mark.parametrize("value", [True, 0.0, "0"])
def test_metadata_resume_input_index_must_be_strict_integer(db, value):
    options = resume_parent(db, "metadata_only")
    index = 1 if value is True else 0
    with pytest.raises(ValueError):
        db.create_or_get_ai_resume_job("child", input_data={"resume_candidate_index": value},
                                      **{**options, "resume_candidate_index": index})
    assert db.get_ai_job("child") is None


def test_usage_legacy_resume_keeps_unmarked_input_and_rejects_forged_policy(db):
    options = resume_parent(db, None)
    data = {"resume_candidate_index": 0, "legacy": "retained"}
    first, created = db.create_or_get_ai_resume_job("legacy-child", input_data=data, **options)
    assert created and first["input"] == data and first["validation_status"] == "unknown"
    second, created = db.create_or_get_ai_resume_job("ignored", input_data=data, **options)
    assert not created and second["job_id"] == first["job_id"]
    for changed in ({**data, "legacy": "different"}, dict(reversed(list(data.items())))):
        with pytest.raises(AIJobConflictError):
            db.create_or_get_ai_resume_job("conflict", input_data=changed, **options)
    assert db.conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 2
    with pytest.raises(ValueError):
        db.create_or_get_ai_resume_job("forged", input_data={**data, "retention_policy": "metadata_only"},
                                      **{**options, "idempotency_key": "forged"})
    assert db.get_ai_job("forged") is None


def test_metadata_resume_cannot_borrow_keyword_policy_for_another_task(db):
    options = resume_parent(db, "keyword_compat")
    with pytest.raises((ValueError, AIJobConflictError)):
        db.create_or_get_ai_resume_job("escape", input_data={"resume_candidate_index": 0},
                                      **{**options, "task_type": "general"})
    child, created = db.create_or_get_ai_resume_job("keyword-child", input_data={
        "resume_candidate_index": 0, "raw_keywords": ["untrusted child replacement"], "raw": "IGNORED",
    }, **options)
    assert created and child["input"] == {
        "retention_policy": "keyword_compat", "raw_keywords": ["input"], "tags": ["tag"],
        "resume_candidate_index": 0,
    }


def test_create_job_is_backward_compatible_and_owned_fields_are_private(
    db: Database,
) -> None:
    db.create_ai_job("legacy", "continue", 1, {"legacy": True})
    create_owned_job(db)

    legacy = db.get_ai_job("legacy")
    owned = db.get_ai_job("job")

    assert legacy["status"] == "running"
    assert legacy["stage"] == "main"
    assert owned["stage"] == "main"
    assert owned["route_deadline_at"] == "2099-01-01 00:00:00"
    assert owned["input"] == {"chapter_id": 1}
    assert "owner_token" not in owned
    assert "lease_until" not in owned
    assert "heartbeat_at" not in owned
    assert "owner_token" not in db.list_ai_jobs()["items"][0]
    assert "lease_until" not in db.list_ai_jobs()["items"][0]
    assert "heartbeat_at" not in db.list_ai_jobs()["items"][0]
    raw = db.conn.execute(
        "SELECT owner_token, lease_until, heartbeat_at FROM ai_jobs WHERE job_id = 'job'"
    ).fetchone()
    assert raw["owner_token"] == "owner"
    assert raw["lease_until"] is not None
    assert raw["heartbeat_at"] is not None


def test_candidate_snapshot_requires_owner_canonical_hash_size_and_safe_keys(
    db: Database,
) -> None:
    create_owned_job(db)
    snapshot_json, snapshot_hash = canonical_snapshot()

    assert db.set_ai_job_candidate_snapshot(
        "job",
        "wrong-owner",
        snapshot_json,
        snapshot_hash,
    ) is False
    with pytest.raises(ValueError, match="摘要"):
        db.set_ai_job_candidate_snapshot(
            "job",
            "owner",
            snapshot_json,
            "0" * 64,
        )
    with pytest.raises(ValueError, match="敏感字段"):
        unsafe = {"candidates": [], "messages": [{"content": "secret"}]}
        unsafe_json = json.dumps(unsafe, separators=(",", ":"))
        db.set_ai_job_candidate_snapshot(
            "job",
            "owner",
            unsafe_json,
            hashlib.sha256(unsafe_json.encode()).hexdigest(),
        )
    with pytest.raises(ValueError, match="敏感字段"):
        db.set_ai_job_candidate_snapshot(
            "job",
            "owner",
            {"candidates": ({"api_key": "secret"},)},
            "0" * 64,
        )
    with pytest.raises(ValueError, match="控制字符"):
        control_snapshot = {"candidates": [{"model_key": "bad\x00model"}]}
        control_json = json.dumps(
            control_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        db.set_ai_job_candidate_snapshot(
            "job",
            "owner",
            control_json,
            hashlib.sha256(control_json.encode()).hexdigest(),
        )
    with pytest.raises(ValueError, match="序列化"):
        db.set_ai_job_candidate_snapshot(
            "job",
            "owner",
            {"invalid_number": float("nan")},
            "0" * 64,
        )
    with pytest.raises(ValueError, match="256 KiB"):
        oversized = {"padding": "x" * (256 * 1024)}
        db.set_ai_job_candidate_snapshot(
            "job",
            "owner",
            oversized,
            "0" * 64,
        )

    assert db.set_ai_job_candidate_snapshot(
        "job",
        "owner",
        json.loads(snapshot_json),
        snapshot_hash,
    ) is True
    assert db.set_ai_job_candidate_snapshot(
        "job",
        "owner",
        snapshot_json,
        snapshot_hash,
    ) is False
    job = db.get_ai_job("job")
    assert job["candidate_snapshot"]["candidates"][0]["model_key"] == "model-a"
    assert job["candidate_snapshot_hash"] == snapshot_hash


def test_attempt_indices_are_unique_under_concurrency(db: Database) -> None:
    create_owned_job(db)
    barrier = Barrier(8)

    def allocate(_index: int) -> int:
        worker = Database(db.path)
        try:
            barrier.wait(timeout=5)
            return worker.allocate_ai_model_attempt(
                "job",
                "owner",
                valid_attempt_data(),
            )
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        indexes = list(executor.map(allocate, range(8)))

    assert sorted(indexes) == list(range(8))
    job = db.get_ai_job("job")
    assert job["next_attempt_index"] == 8
    assert job["candidate_attempt_count"] == 8


def test_attempt_and_network_request_hard_budgets_are_atomic(db: Database) -> None:
    create_owned_job(db)

    indexes = [
        db.allocate_ai_model_attempt("job", "owner", valid_attempt_data())
        for _ in range(16)
    ]
    assert indexes == list(range(16))
    with pytest.raises(AIRouteBudgetExhausted) as attempt_error:
        db.allocate_ai_model_attempt("job", "owner", valid_attempt_data())
    assert attempt_error.value.error_category == "route_budget_exhausted"

    claims = [db.claim_ai_job_network_request("job", "owner") for _ in range(32)]
    assert claims == list(range(1, 33))
    with pytest.raises(AIRouteBudgetExhausted):
        db.claim_ai_job_network_request("job", "owner")
    assert db.get_ai_job("job")["network_request_count"] == 32


def test_attempt_requires_provider_and_rejects_unencodable_model_key(
    db: Database,
) -> None:
    create_owned_job(db)
    missing_provider = valid_attempt_data()
    missing_provider["provider_id"] = None
    with pytest.raises(ValueError, match="provider_id"):
        db.allocate_ai_model_attempt("job", "owner", missing_provider)

    invalid_model = valid_attempt_data(model_key="bad-\ud800-model")
    with pytest.raises(ValueError, match="UTF-8"):
        db.allocate_ai_model_attempt("job", "owner", invalid_model)


def test_owner_mismatch_cannot_allocate_claim_heartbeat_or_finish(
    db: Database,
) -> None:
    create_owned_job(db)
    with pytest.raises(AIJobConflictError):
        db.allocate_ai_model_attempt("job", "wrong", valid_attempt_data())
    with pytest.raises(AIJobConflictError):
        db.claim_ai_job_network_request("job", "wrong")
    assert db.heartbeat_ai_job(
        "job",
        "wrong",
        "2099-01-01 00:00:00",
    ) is False

    attempt_index = db.allocate_ai_model_attempt(
        "job",
        "owner",
        valid_attempt_data(),
    )
    assert db.finish_ai_model_attempt(
        "job",
        attempt_index,
        "wrong",
        "failed",
        error_category="network",
    ) is False
    assert db.finish_ai_job_cas(
        "job",
        "wrong",
        "failed",
        error_message="wrong owner",
    ) is False


def test_heartbeat_renews_job_and_running_attempt_for_matching_owner(
    db: Database,
) -> None:
    create_owned_job(db)
    attempt_index = db.allocate_ai_model_attempt(
        "job",
        "owner",
        valid_attempt_data(),
    )
    lease_until = "2099-02-03 04:05:06"

    assert db.heartbeat_ai_job("job", "owner", lease_until) is True

    job_row = db.conn.execute(
        "SELECT lease_until, heartbeat_at FROM ai_jobs WHERE job_id = 'job'"
    ).fetchone()
    attempt_row = db.conn.execute(
        """
        SELECT lease_until, heartbeat_at
        FROM ai_job_model_attempts
        WHERE job_id = 'job' AND attempt_index = ?
        """,
        (attempt_index,),
    ).fetchone()
    assert job_row["lease_until"] == lease_until
    assert attempt_row["lease_until"] == lease_until
    assert job_row["heartbeat_at"] is not None
    assert attempt_row["heartbeat_at"] is not None


def test_attempt_finish_is_monotonic_and_sanitizes_error(db: Database) -> None:
    create_owned_job(db)
    attempt_index = db.allocate_ai_model_attempt(
        "job",
        "owner",
        valid_attempt_data(),
    )
    secret_error = "Authorization: Bearer sk-secret\x00 " + "错" * 3000

    assert db.finish_ai_model_attempt(
        "job",
        attempt_index,
        "owner",
        "failed",
        error_scope="provider",
        error_message=secret_error,
        error_category="network",
        finish_reason="error",
        output_started=False,
        latency_ms=123,
    ) is True
    assert db.finish_ai_model_attempt(
        "job",
        attempt_index,
        "owner",
        "succeeded",
    ) is False

    attempt = db.list_ai_job_model_attempts("job")[0]
    assert attempt["status"] == "failed"
    assert attempt["error_scope"] == "provider"
    assert attempt["error_category"] == "network"
    assert attempt["finish_reason"] == "error"
    assert attempt["latency_ms"] == 123
    assert "sk-secret" not in attempt["error_message"]
    assert "\x00" not in attempt["error_message"]
    assert len(attempt["error_message"]) <= 2000
    assert len(attempt["error_message"].encode("utf-8")) <= 8000
    assert "owner_token" not in attempt


def test_partial_attempt_is_rejected_outside_main_stage(db: Database) -> None:
    create_owned_job(db, stage="internal")
    attempt_index = db.allocate_ai_model_attempt(
        "job",
        "owner",
        valid_attempt_data(stage="internal"),
    )

    with pytest.raises(ValueError, match="main"):
        db.finish_ai_model_attempt(
            "job",
            attempt_index,
            "owner",
            "partial",
            output_started=True,
        )


def test_partial_attempt_requires_started_user_output(db: Database) -> None:
    create_owned_job(db)
    attempt_index = db.allocate_ai_model_attempt(
        "job",
        "owner",
        valid_attempt_data(),
    )

    with pytest.raises(ValueError, match="正文"):
        db.finish_ai_model_attempt(
            "job",
            attempt_index,
            "owner",
            "partial",
            output_started=False,
        )


def test_terminal_job_state_is_monotonic_and_private(db: Database) -> None:
    create_owned_job(db)

    assert db.finish_ai_job_cas(
        "job",
        "owner",
        "partial",
        output_text="半截",
        error_message="网络中断",
    ) is True
    assert db.finish_ai_job_cas(
        "job",
        "owner",
        "succeeded",
        output_text="迟到完成",
    ) is False

    job = db.get_ai_job("job")
    assert job["status"] == "partial"
    assert job["output_text"] == "半截"
    assert job["finished_at"] is not None
    assert "owner_token" not in job


def test_partial_job_requires_started_user_output(db: Database) -> None:
    create_owned_job(db)

    with pytest.raises(ValueError, match="正文"):
        db.finish_ai_job_cas("job", "owner", "partial")

    assert db.get_ai_job("job")["status"] == "running"


def test_legacy_job_update_cannot_overwrite_terminal_state(db: Database) -> None:
    db.create_ai_job("legacy", "continue", 1, {})

    db.update_ai_job("legacy", "failed", error_message="first failure")
    db.update_ai_job("legacy", "succeeded", output_text="late success")

    job = db.get_ai_job("legacy")
    assert job["status"] == "failed"
    assert job["output_text"] is None
    assert job["error_message"] == "first failure"


def test_get_job_projects_snapshot_budget_attempts_and_route_summary(
    db: Database,
) -> None:
    create_owned_job(db)
    snapshot_json, snapshot_hash = canonical_snapshot()
    assert db.set_ai_job_candidate_snapshot(
        "job",
        "owner",
        snapshot_json,
        snapshot_hash,
    )
    budget = {
        "effective_context_window": 8000,
        "input_budget": 6700,
        "output_reserve": 1000,
        "message_overhead": 44,
        "safety_margin": 256,
    }
    db.conn.execute(
        "UPDATE ai_jobs SET prompt_budget_json = ? WHERE job_id = 'job'",
        (json.dumps(budget, separators=(",", ":")),),
    )
    db.conn.commit()
    attempt_index = db.allocate_ai_model_attempt(
        "job",
        "owner",
        valid_attempt_data(),
    )
    assert db.finish_ai_model_attempt(
        "job",
        attempt_index,
        "owner",
        "succeeded",
        finish_reason="stop",
        output_started=True,
    )

    job = db.get_ai_job("job")

    assert job["candidate_snapshot_hash"] == snapshot_hash
    assert job["candidate_snapshot"]["binding_version"] == 1
    assert job["prompt_budget"] == budget
    assert job["attempts"][0]["attempt_index"] == 0
    assert job["route_summary"] == {
        "attempt_index": 0,
        "stage": "main",
        "status": "succeeded",
        "pool_id": None,
        "pool_name": None,
        "provider_id": 1,
        "provider_name": "Provider A",
        "provider_model_id": None,
        "model_key": "model-a",
    }
    assert "owner_token" not in json.dumps(job)


@pytest.mark.parametrize(
    ("stage", "output_started", "expected"),
    [
        ("main", True, "partial"),
        ("main", False, "failed"),
        ("internal", True, "failed"),
        ("validation", True, "failed"),
    ],
)
def test_stale_recovery_maps_stage_and_output(
    db: Database,
    stage: str,
    output_started: bool,
    expected: str,
) -> None:
    now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
    create_owned_job(db, stage=stage)
    attempt_index = db.allocate_ai_model_attempt(
        "job",
        "owner",
        valid_attempt_data(stage=stage),
    )
    expired_lease = (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    stale_heartbeat = (now - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
    db.conn.execute(
        """
        UPDATE ai_jobs
        SET lease_until = ?, heartbeat_at = ?, output_text = ?
        WHERE job_id = 'job'
        """,
        (expired_lease, stale_heartbeat, "半截" if output_started else None),
    )
    db.conn.execute(
        """
        UPDATE ai_job_model_attempts
        SET lease_until = ?, heartbeat_at = ?, output_started = ?
        WHERE job_id = 'job' AND attempt_index = ?
        """,
        (expired_lease, stale_heartbeat, int(output_started), attempt_index),
    )
    db.conn.commit()

    assert db.fail_stale_ai_jobs(now=now) == 1

    assert db.get_ai_job("job")["status"] == expected
    attempt = db.list_ai_job_model_attempts("job")[0]
    assert attempt["status"] == expected
    assert attempt["error_category"] == "process_interrupted"
    assert attempt["finish_reason"] == "error"


def test_stale_recovery_requires_expired_lease_and_preserves_cancellation(
    db: Database,
) -> None:
    now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
    create_owned_job(db, "healthy")
    create_owned_job(db, "cancelled")
    future = (now + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    stale = (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    db.conn.execute(
        "UPDATE ai_jobs SET lease_until = ?, heartbeat_at = ? WHERE job_id = 'healthy'",
        (future, stale),
    )
    db.conn.execute(
        """
        UPDATE ai_jobs
        SET status = 'cancelled', lease_until = ?, heartbeat_at = ?,
            finished_at = ?
        WHERE job_id = 'cancelled'
        """,
        (stale, stale, stale),
    )
    db.conn.commit()

    assert db.fail_stale_ai_jobs(now=now) == 0
    assert db.get_ai_job("healthy")["status"] == "running"
    assert db.get_ai_job("cancelled")["status"] == "cancelled"


def test_stale_recovery_treats_missing_heartbeat_as_expired(db: Database) -> None:
    now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
    create_owned_job(db)
    expired = (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    db.conn.execute(
        "UPDATE ai_jobs SET lease_until = ?, heartbeat_at = NULL WHERE job_id = 'job'",
        (expired,),
    )
    db.conn.commit()

    assert db.fail_stale_ai_jobs(now=now) == 1
    assert db.get_ai_job("job")["status"] == "failed"


def test_stale_internal_output_never_turns_main_job_partial(db: Database) -> None:
    now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
    create_owned_job(db, stage="main")
    attempt_index = db.allocate_ai_model_attempt(
        "job",
        "owner",
        valid_attempt_data(stage="internal"),
    )
    expired = (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    db.conn.execute(
        "UPDATE ai_jobs SET lease_until = ?, heartbeat_at = ? WHERE job_id = 'job'",
        (expired, expired),
    )
    db.conn.execute(
        """
        UPDATE ai_job_model_attempts
        SET lease_until = ?, heartbeat_at = ?, output_started = 1
        WHERE job_id = 'job' AND attempt_index = ?
        """,
        (expired, expired, attempt_index),
    )
    db.conn.commit()

    assert db.fail_stale_ai_jobs(now=now) == 1
    assert db.get_ai_job("job")["status"] == "failed"
    assert db.list_ai_job_model_attempts("job")[0]["status"] == "failed"


def test_partial_cleanup_cascades_model_attempts(db: Database) -> None:
    create_owned_job(db)
    attempt_index = db.allocate_ai_model_attempt(
        "job",
        "owner",
        valid_attempt_data(),
    )
    assert db.finish_ai_model_attempt(
        "job",
        attempt_index,
        "owner",
        "partial",
        finish_reason="error",
        output_started=True,
    )
    assert db.finish_ai_job_cas(
        "job",
        "owner",
        "partial",
        output_text="半截",
    )
    db.conn.execute(
        """
        UPDATE ai_jobs
        SET created_at = '2000-01-01 00:00:00',
            finished_at = '2000-01-01 00:00:01'
        WHERE job_id = 'job'
        """
    )
    db.conn.commit()

    assert db.cleanup_ai_jobs(keep_days=3) == 1
    assert db.get_ai_job("job") is None
    count = db.conn.execute(
        "SELECT COUNT(*) FROM ai_job_model_attempts WHERE job_id = 'job'"
    ).fetchone()[0]
    assert count == 0


def test_work_child_attachment_rolls_back_with_child_insert(db, monkeypatch):
    from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError, WorkCallBinding, WorkLease

    binding = WorkCallBinding(WorkLease("a" * 32, 1, "work-owner"), "b" * 64, 1, "c" * 64)
    db.conn.execute("CREATE TABLE bridge_probe (value TEXT PRIMARY KEY)")
    db.conn.commit()

    def deny_attach(conn, received, child_job_id):
        assert conn.in_transaction and received == binding and child_job_id == "bound"
        conn.execute("INSERT INTO bridge_probe VALUES ('attachment')")
        raise WorkAnalysisError("owner_lost", 409)

    monkeypatch.setattr(db, "_attach_work_analysis_child", deny_attach)
    with pytest.raises(WorkAnalysisError, match="owner_lost"):
        db.create_ai_job(
            "bound", "general", None, {"business_type": "work_analysis"},
            owner_token="child-owner", retention_policy="metadata_only", work_binding=binding,
        )
    assert db.conn.execute("SELECT COUNT(*) FROM ai_jobs WHERE job_id='bound'").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM bridge_probe").fetchone()[0] == 0


def test_work_reservation_failure_rolls_back_parent_child_and_unknown_usage(db, monkeypatch):
    from pixiv_novel_sync.storage.ai.core import AIWorkAdmissionDenied
    from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError, WorkCallBinding, WorkLease

    create_owned_job(db)
    attempt = db.allocate_ai_model_attempt("job", "owner", valid_attempt_data())
    binding = WorkCallBinding(WorkLease("a" * 32, 1, "work-owner"), "b" * 64, 1, "c" * 64)
    db.conn.execute("CREATE TABLE bridge_probe (value TEXT PRIMARY KEY)")
    db.conn.commit()

    def deny_reservation(conn, received, child_job_id, ordinal, planned_tokens, *, now):
        assert conn.in_transaction and received == binding
        assert (child_job_id, ordinal, planned_tokens) == ("job", 1, 371)
        conn.execute("INSERT INTO bridge_probe VALUES ('reservation')")
        raise WorkAnalysisError("budget_exhausted", 409)

    monkeypatch.setattr(db, "_reserve_work_analysis_request", deny_reservation)
    with pytest.raises(AIWorkAdmissionDenied) as caught:
        db.claim_ai_job_network_request(
            "job", "owner", attempt, work_binding=binding, planned_tokens=371,
        )
    assert caught.value.code == "budget_exhausted"
    assert db.get_ai_job("job")["network_request_count"] == 0
    envelope = db.list_ai_job_model_attempts("job")[0]["usage"]
    assert envelope is None or envelope["requests"] == []
    assert db.conn.execute("SELECT COUNT(*) FROM bridge_probe").fetchone()[0] == 0


def test_work_usage_projection_and_child_usage_share_one_transaction(db, monkeypatch):
    create_owned_job(db)
    attempt = db.allocate_ai_model_attempt("job", "owner", valid_attempt_data())
    ordinal = db.claim_ai_job_network_request("job", "owner", attempt)
    before = db.list_ai_job_model_attempts("job")[0]["usage"]

    def reject_projection(conn, child_job_id, request_ordinal, usage):
        assert conn.in_transaction and (child_job_id, request_ordinal) == ("job", ordinal)
        assert usage["total_tokens"] == 12
        raise sqlite3.OperationalError("synthetic projection failure")

    monkeypatch.setattr(db, "_record_work_analysis_usage", reject_projection)
    with pytest.raises(sqlite3.OperationalError, match="synthetic projection"):
        db.record_ai_job_request_usage(
            "job", "owner", attempt, ordinal,
            {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
             "complete": True, "missing_reason": None},
        )
    assert db.list_ai_job_model_attempts("job")[0]["usage"] == before


def test_work_bridge_real_ledger_preserves_attachment_debits_and_usage_repair(db, monkeypatch):
    from pixiv_novel_sync.ai.usage import ProviderUsage
    from pixiv_novel_sync.storage.ai.core import AIWorkAdmissionDenied
    from pixiv_novel_sync.storage.work_analysis import common as work_common
    from test_work_analysis_storage import create_case

    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(work_common, "utc_now", lambda: now)
    monkeypatch.setattr("pixiv_novel_sync.storage.ai.core._utc_now", lambda: now)
    case = create_case(db, request_limit=1)
    lease = db.claim_work_analysis(case["run"]["run_id"], now=now, slice_seconds=30)
    unit_id = case["arguments"]["plan"]["original_units"][0]["unit_id"]
    binding = db.begin_work_analysis_unit(lease, unit_id)
    db.create_ai_job(
        "bound", "general", case["agent_id"], {"business_type": "work_analysis"},
        owner_token="child-owner", retention_policy="metadata_only", work_binding=binding,
        route_deadline_at="2099-01-01 00:00:00",
    )
    observer = Database(db.path)
    try:
        attached = observer.conn.execute(
            "SELECT * FROM work_analysis_requests WHERE child_job_id='bound'",
        ).fetchone()
        assert attached["entry_kind"] == "attachment" and attached["request_ordinal"] == 0
        assert attached["planned_tokens"] is None and attached["usage_json"] is None
        initial = observer.get_work_analysis(lease.run_id)
        assert initial["request_count"] == initial["planned_tokens"] == 0
        assert initial["usage_summary"]["network_request_count"] == 0
    finally:
        observer.close()

    attempt = db.allocate_ai_model_attempt("bound", "child-owner", valid_attempt_data())
    with pytest.raises(AIWorkAdmissionDenied, match="owner_lost"):
        db.claim_ai_job_network_request("bound", "child-owner", attempt)
    ordinal = db.claim_ai_job_network_request(
        "bound", "child-owner", attempt, work_binding=binding, planned_tokens=371,
    )
    pending = db.get_work_analysis(lease.run_id)
    assert pending["request_count"] == 1 and pending["planned_tokens"] == 371
    assert pending["usage_summary"]["unreported_requests"] == 1
    assert pending["usage_summary"]["known_total_tokens"] is None
    usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
             "complete": True, "missing_reason": None}
    assert db.record_ai_job_request_usage("bound", "child-owner", attempt, ordinal, usage)
    # An unchanged child snapshot still repairs its idempotent parent projection.
    with db.transaction() as conn:
        conn.execute(
            "UPDATE work_analysis_requests SET usage_json=? WHERE child_job_id='bound' AND request_ordinal=1",
            (json.dumps(ProviderUsage().to_dict()),),
        )
    assert db.record_ai_job_request_usage("bound", "child-owner", attempt, ordinal, usage)
    with pytest.raises(AIWorkAdmissionDenied, match="budget_exhausted"):
        db.claim_ai_job_network_request(
            "bound", "child-owner", attempt, work_binding=binding, planned_tokens=371,
        )
    saved = db.get_work_analysis(lease.run_id)
    assert saved["request_count"] == 1 and saved["planned_tokens"] == 371
    assert saved["usage_summary"]["network_request_count"] == 1
    assert saved["usage_summary"]["reported_requests"] == 1
    assert saved["usage_summary"]["known_total_tokens"] == 12
    assert db.get_ai_job("bound")["network_request_count"] == 1
    assert len(db.list_ai_job_model_attempts("bound")[0]["usage"]["requests"]) == 1

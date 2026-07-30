from __future__ import annotations

import hashlib
import json
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

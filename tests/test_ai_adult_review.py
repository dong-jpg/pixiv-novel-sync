from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from ai_adult_testkit import (
    CHARACTER_A_ID,
    safe_validation,
    seed_adult_project,
    structural_validation,
    valid_adult_payload,
)
from pixiv_novel_sync.ai.adult_types import raw_sha256, warning_ack_hash
from pixiv_novel_sync.ai.adult_validation import (
    VALIDATOR_POLICY_HASH,
    compute_provider_scope_hash,
    compute_validation_hash,
)
from pixiv_novel_sync.ai.adult_policies import SAFETY_POLICY
from pixiv_novel_sync.ai.model_router import (
    CandidateSnapshot,
    ModelCandidate,
    PromptBudget,
    RouteResult,
)
from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.storage_db import Database


def _snapshot(seed: str, *, capabilities: tuple[str, ...] = ()) -> CandidateSnapshot:
    candidate = ModelCandidate(
        provider_id=1,
        provider_name=f"provider-{seed}",
        model_key=f"model-{seed}",
        provider_model_id=None,
        pool_id=None,
        pool_name=None,
        pool_version=None,
        pool_position=None,
        provider_config_hash=seed * 64,
        capabilities=capabilities,
        context_window=32_000,
    )
    payload = {
        "agent_config_hash": seed * 64,
        "binding_version": 1,
        "candidates": [asdict(candidate)],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return CandidateSnapshot(
        candidates=(candidate,),
        snapshot_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        agent_config_hash=seed * 64,
        binding_version=1,
    )


class ReviewRouter:
    def __init__(self) -> None:
        self.snapshots = {
            "main": _snapshot("a"),
            "safety": _snapshot("b", capabilities=("json",)),
            "fact_guard": _snapshot("c", capabilities=("json",)),
        }
        self.requests: list[Any] = []
        self.validation_requests: list[Any] = []
        self.results: list[RouteResult] = []
        self.execute_count = 0
        self.fail_budget = False

    def close(self) -> None:
        pass

    def resolve_candidates(
        self,
        agent: Any,
        stage: str = "main",
        snapshot: CandidateSnapshot | None = None,
    ) -> CandidateSnapshot:
        task_type = str(getattr(agent, "task_type", ""))
        key = (
            "safety"
            if task_type == "adult_safety_review"
            else "fact_guard"
            if task_type == "adult_fact_guard"
            else "main"
        )
        return snapshot or self.snapshots[key]

    def build_prompt_budget(
        self,
        _agent: Any,
        _snapshot: CandidateSnapshot,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> PromptBudget:
        if self.fail_budget:
            raise RuntimeError("budget resolver crashed with private detail")
        assert messages
        return PromptBudget(
            effective_context_window=32_000,
            input_budget=24_000,
            output_reserve=max_tokens,
            message_overhead=4 * len(messages) + 2,
            safety_margin=256,
            estimator="utf8_bytes",
        )

    def execute(self, request: Any) -> RouteResult:
        self.execute_count += 1
        self.requests.append(request)
        if request.stage == "validation":
            self.validation_requests.append(request)
        if self.results:
            result = self.results.pop(0)
        elif request.stage == "main":
            boundary_match = re.search(
                r"ADULT_BOUNDARY_[0-9a-f]{32}",
                request.messages[0]["content"],
            )
            assert boundary_match is not None
            boundary = boundary_match.group(0)
            target_lines = request.messages[3]["content"].splitlines()
            masked_target = "\n".join(target_lines[1:-1])
            result = RouteResult(
                job_id=request.job_id,
                output_text=(
                    f"{boundary}_CANDIDATE_BEGIN\n"
                    f"{masked_target}\n"
                    f"{boundary}_CANDIDATE_END"
                ),
                candidate_snapshot_hash=request.candidate_snapshot.snapshot_hash,
                attempts=(),
                finish_state="succeeded",
            )
        else:
            result = RouteResult(
                job_id=request.job_id,
                output_text='{"safe":true,"issues":[]}',
                candidate_snapshot_hash=request.candidate_snapshot.snapshot_hash,
                attempts=(),
                finish_state="succeeded",
            )
        result = replace(
            result,
            job_id=request.job_id,
            candidate_snapshot_hash=request.candidate_snapshot.snapshot_hash,
        )
        if result.output_text:
            request.on_delta(result.output_text)
        return result


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "adult-review.db")
    database.init_schema()
    seed_adult_project(database)
    database.delete_ai_job("adult-job")
    database.conn.execute(
        """
        UPDATE ai_adult_review_bindings
        SET binding_type = 'fixed', provider_id = 1, model = 'adult-model',
            model_pool_id = NULL, enabled = 1
        WHERE review_kind IN ('safety', 'fact_guard')
        """
    )
    database.conn.commit()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def fake_router() -> ReviewRouter:
    return ReviewRouter()


@pytest.fixture
def service(db: Database, fake_router: ReviewRouter):
    instance = AIWritingService(db.path)
    instance.model_router.close()
    instance.model_router = fake_router
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def prepared(service: AIWritingService, fake_router: ReviewRouter):
    scope_hash = compute_provider_scope_hash(
        {
            "main": fake_router.snapshots["main"],
            "safety": fake_router.snapshots["safety"],
            "fact_guard": fake_router.snapshots["fact_guard"],
        }
    )
    payload = valid_adult_payload(
        participant_character_ids=[CHARACTER_A_ID],
        provider_scope_hash=scope_hash,
    )
    return service.prepare_adult_job(
        payload,
        "owner-a",
        owner_token="lease-a",
    )


def test_safety_review_receives_restored_server_buffer_not_nonce(
    service: AIWritingService,
    fake_router: ReviewRouter,
    prepared: Any,
):
    result = service.run_adult_safety_review(
        prepared,
        "安娜握住他的手，停顿片刻后仍保持原来的称呼和视角。",
    )

    request = fake_router.validation_requests[-1]
    assert result.safe is True
    assert request.stage == "validation"
    assert "安娜" in request.messages[-1]["content"]
    assert "ADULT_" not in request.messages[-1]["content"]
    assert request.participant_facts[0]["character_id"] == CHARACTER_A_ID
    assert request.participant_facts[0]["age_years"] >= 18
    assert request.participant_facts[0]["fictional"] is True


def test_fact_guard_receives_names_aliases_and_locked_terms(
    service: AIWritingService,
    fake_router: ReviewRouter,
    prepared: Any,
):
    service.run_adult_fact_guard(
        prepared,
        prepared.target,
        prepared.target,
    )

    request = fake_router.validation_requests[-1]
    participant = request.participant_facts[0]
    assert participant["canonical_name"] == "安娜"
    assert participant["aliases"] == ("安",)
    assert {"安娜", "安"}.issubset(request.protected_terms)
    assert '"aliases":["安"]' in request.messages[-1]["content"]


def _raw_candidate(prepared: Any, candidate: str | None = None) -> str:
    token = next(iter(prepared.prompt.token_map))
    masked = (candidate or prepared.target).replace("安娜", token)
    boundary = prepared.prompt.boundary
    return (
        f"{boundary}_CANDIDATE_BEGIN\n"
        f"{masked}\n"
        f"{boundary}_CANDIDATE_END"
    )


def test_unknown_review_issue_blocks_without_candidate(
    db: Database,
    service: AIWritingService,
    fake_router: ReviewRouter,
    prepared: Any,
):
    fake_router.results.append(
        RouteResult(
            job_id="pending",
            output_text='{"safe":true,"issues":["自由文本"]}',
            candidate_snapshot_hash="0" * 64,
            attempts=(),
            finish_state="succeeded",
        )
    )

    events = list(service.finish_adult_candidate(prepared, _raw_candidate(prepared)))

    assert events[-1].type == "error"
    assert events[-1].data and events[-1].data["code"] == "review_unavailable"
    assert not any(event.type == "candidate" for event in events)
    job = db.get_adult_job(prepared.job_id, "owner-a")
    assert job is not None
    assert job["status"] == "failed"
    assert job.get("output_text") is None
    assert db.get_application_for_owner(prepared.job_id, "owner-a") is None


def test_duplicate_review_json_key_fails_closed(
    db: Database,
    service: AIWritingService,
    fake_router: ReviewRouter,
    prepared: Any,
):
    fake_router.results.append(
        RouteResult(
            job_id="pending",
            output_text='{"safe":false,"safe":true,"issues":[]}',
            candidate_snapshot_hash="0" * 64,
            attempts=(),
            finish_state="succeeded",
        )
    )

    events = list(service.finish_adult_candidate(prepared, _raw_candidate(prepared)))

    assert events[-1].type == "error"
    assert events[-1].data and events[-1].data["code"] == "review_unavailable"
    assert not any(event.type == "candidate" for event in events)
    assert db.get_application_for_owner(prepared.job_id, "owner-a") is None


def test_review_setup_failure_fails_main_job_closed(
    db: Database,
    service: AIWritingService,
    fake_router: ReviewRouter,
    prepared: Any,
):
    fake_router.fail_budget = True

    events = list(service.finish_adult_candidate(prepared, _raw_candidate(prepared)))

    assert events[-1].type == "error"
    assert events[-1].data and events[-1].data["code"] == "review_unavailable"
    assert "private detail" not in events[-1].data["message"]
    assert not any(event.type == "candidate" for event in events)
    job = db.get_adult_job(prepared.job_id, "owner-a")
    assert job is not None
    assert job["status"] == "failed"
    assert job.get("output_text") is None


def test_review_prompt_setup_failure_fails_main_job_closed(
    db: Database,
    service: AIWritingService,
    monkeypatch: pytest.MonkeyPatch,
    prepared: Any,
):
    def fail_participant_facts(_prepared: Any) -> tuple[Any, ...]:
        raise RuntimeError("private prompt setup detail")

    monkeypatch.setattr(
        service,
        "_review_participant_facts",
        fail_participant_facts,
    )

    events = list(service.finish_adult_candidate(prepared, _raw_candidate(prepared)))

    assert events[-1].type == "error"
    assert events[-1].data and events[-1].data["code"] == "review_unavailable"
    assert "private prompt setup detail" not in events[-1].data["message"]
    assert not any(event.type == "candidate" for event in events)
    job = db.get_adult_job(prepared.job_id, "owner-a")
    assert job is not None
    assert job["status"] == "failed"
    assert job.get("output_text") is None


def test_character_token_variant_blocks_before_review_provider(
    db: Database,
    service: AIWritingService,
    fake_router: ReviewRouter,
    prepared: Any,
):
    token = next(iter(prepared.prompt.token_map))
    raw = _raw_candidate(prepared).replace(token, token.lower())

    events = list(service.finish_adult_candidate(prepared, raw))

    assert fake_router.execute_count == 0
    assert events[-1].type == "error"
    assert events[-1].data and events[-1].data["code"] == "safety_blocked"
    assert not any(event.type == "candidate" for event in events)
    job = db.get_adult_job(prepared.job_id, "owner-a")
    assert job is not None
    assert job["status"] == "failed"
    assert job.get("output_text") is None


def test_fact_guard_unknown_blocks_without_candidate(
    db: Database,
    service: AIWritingService,
    fake_router: ReviewRouter,
    prepared: Any,
):
    fake_router.results.extend(
        [
            RouteResult(
                job_id="pending",
                output_text='{"safe":true,"issues":[]}',
                candidate_snapshot_hash="0" * 64,
                attempts=(),
                finish_state="succeeded",
            ),
            RouteResult(
                job_id="pending",
                output_text='{"safe":false,"issues":["unknown"]}',
                candidate_snapshot_hash="0" * 64,
                attempts=(),
                finish_state="succeeded",
            ),
        ]
    )

    events = list(service.finish_adult_candidate(prepared, _raw_candidate(prepared)))

    assert len(fake_router.validation_requests) == 2
    fact_request = fake_router.validation_requests[-1]
    assert prepared.target in fact_request.messages[-1]["content"]
    assert "安娜" in fact_request.messages[-1]["content"]
    assert set(prepared.prompt.protected_terms).issubset(
        fact_request.protected_terms
    )
    assert events[-1].type == "error"
    assert events[-1].data and events[-1].data["code"] == "validation_failed"
    assert not any(event.type == "candidate" for event in events)
    job = db.get_adult_job(prepared.job_id, "owner-a")
    assert job is not None
    assert job["status"] == "failed"
    assert job.get("output_text") is None
    assert db.get_application_for_owner(prepared.job_id, "owner-a") is None


def test_structural_block_is_persisted_but_not_applicable(
    db: Database,
    service: AIWritingService,
    prepared: Any,
):
    candidate = "解释前缀\n正文"
    safety_result = service.run_adult_safety_review(prepared, candidate)
    fact_result = service.run_adult_fact_guard(
        prepared,
        prepared.target,
        candidate,
    )

    result = service.finalize_adult_candidate(
        prepared,
        candidate,
        local_result=structural_validation("explanation_prefix"),
        safety_result=safety_result,
        fact_result=fact_result,
    )

    assert result.applicable is False
    application = db.get_application_for_owner(prepared.job_id, "owner-a")
    assert application is not None
    assert application["applicable"] is False
    assert candidate not in json.dumps(application["validation"], ensure_ascii=False)
    job = db.get_adult_job(prepared.job_id, "owner-a")
    assert job is not None
    assert job["status"] == "failed"
    assert job["output_text"] == candidate


def test_safe_candidate_is_committed_before_candidate_event(
    db: Database,
    service: AIWritingService,
    fake_router: ReviewRouter,
    prepared: Any,
):
    events = list(service.finish_adult_candidate(prepared, _raw_candidate(prepared)))

    event_types = [event.type for event in events]
    assert event_types[-3:] == ["validation", "candidate", "done"]
    candidate_event = events[-2]
    assert candidate_event.text == prepared.target
    assert candidate_event.data and candidate_event.data["job_id"] == prepared.job_id
    assert not any(event.type == "delta" for event in events)
    assert len(fake_router.validation_requests) == 2
    assert all(
        request.stage == "validation"
        for request in fake_router.validation_requests
    )

    application = db.get_application_for_owner(prepared.job_id, "owner-a")
    assert application is not None
    assert application["applicable"] is True
    job = db.get_adult_job(prepared.job_id, "owner-a")
    assert job is not None
    assert job["status"] == "succeeded"
    assert job["output_text"] == prepared.target
    for request in fake_router.validation_requests:
        child = db.get_ai_job(request.job_id)
        assert child is not None
        assert child["status"] == "succeeded"
        assert prepared.target not in (child.get("output_text") or "")


def test_validation_event_exposes_scoped_warning_ack_hash_without_policy_material(
    service: AIWritingService,
):
    result = replace(
        safe_validation(),
        warnings=("paragraph_changed",),
        validation_hash="",
    )
    result = replace(result, validation_hash=compute_validation_hash(result))
    safety_policy_hash = "b" * 64

    event = service._validation_event_data(
        "adult-warning",
        result,
        safety_policy_hash=safety_policy_hash,
    )

    assert event["warning_ack_hash"] == warning_ack_hash(
        result.validation_hash,
        safety_policy_hash,
        VALIDATOR_POLICY_HASH,
        result.warnings,
    )
    assert "safety_policy_hash" not in event
    assert "validator_policy_hash" not in event


def test_warning_candidate_event_contains_ack_hash_for_apply(
    service: AIWritingService,
    prepared: Any,
):
    events = list(
        service.finish_adult_candidate(
            prepared,
            _raw_candidate(prepared, prepared.target + "2"),
        )
    )

    validation_event = next(event for event in events if event.type == "validation")
    assert validation_event.data is not None
    assert validation_event.data["warnings"] == ["new_number"]
    assert validation_event.data["warning_ack_hash"] == warning_ack_hash(
        validation_event.data["validation_hash"],
        SAFETY_POLICY.expected_hash,
        VALIDATOR_POLICY_HASH,
        validation_event.data["warnings"],
    )


def test_review_child_jobs_use_fixed_task_types(
    db: Database,
    service: AIWritingService,
    prepared: Any,
):
    events = list(service.finish_adult_candidate(prepared, _raw_candidate(prepared)))

    assert events[-1].type == "done"
    rows = db.conn.execute(
        "SELECT task_type FROM ai_jobs WHERE parent_job_id = ?",
        (prepared.job_id,),
    ).fetchall()
    assert {row["task_type"] for row in rows} == {
        "adult_safety_review",
        "adult_fact_guard",
    }


def test_review_child_jobs_store_candidate_hash_without_review_text(
    db: Database,
    service: AIWritingService,
    prepared: Any,
):
    events = list(service.finish_adult_candidate(prepared, _raw_candidate(prepared)))

    assert events[-1].type == "done"
    rows = db.conn.execute(
        "SELECT input_json, output_text FROM ai_jobs WHERE parent_job_id = ?",
        (prepared.job_id,),
    ).fetchall()
    assert len(rows) == 2
    for row in rows:
        saved_input = json.loads(row["input_json"])
        assert saved_input["candidate_hash"] == raw_sha256(prepared.target)
        assert prepared.target not in row["input_json"]
        assert row["output_text"] == ""


def test_late_candidate_finalize_cannot_reuse_committed_application(
    service: AIWritingService,
    prepared: Any,
):
    candidate = prepared.target
    safety_result = service.run_adult_safety_review(prepared, candidate)
    fact_result = service.run_adult_fact_guard(
        prepared,
        prepared.target,
        candidate,
    )

    service.finalize_adult_candidate(
        prepared,
        candidate,
        local_result=safe_validation(),
        safety_result=safety_result,
        fact_result=fact_result,
    )

    with pytest.raises(ValueError, match="CAS"):
        service.finalize_adult_candidate(
            prepared,
            candidate,
            local_result=safe_validation(),
            safety_result=safety_result,
            fact_result=fact_result,
        )


def test_main_stream_runs_both_reviews_without_exposing_delta(
    db: Database,
    service: AIWritingService,
    fake_router: ReviewRouter,
):
    scope_hash = compute_provider_scope_hash(
        {
            "main": fake_router.snapshots["main"],
            "safety": fake_router.snapshots["safety"],
            "fact_guard": fake_router.snapshots["fact_guard"],
        }
    )
    payload = valid_adult_payload(
        participant_character_ids=[CHARACTER_A_ID],
        provider_scope_hash=scope_hash,
        idempotency_key="adult-request-key-stream-0002",
    )

    events = list(service.stream_adult_polish(payload, "owner-a", "lease-a"))

    assert fake_router.execute_count == 3
    assert not any(event.type == "delta" for event in events)
    candidate = next(event for event in events if event.type == "candidate")
    assert "安娜" in candidate.text
    assert "ADULT_" not in candidate.text
    job_id = next(
        event.data["job_id"]
        for event in events
        if event.type == "metadata" and event.data
    )
    application = db.get_application_for_owner(job_id, "owner-a")
    assert application is not None
    assert application["applicable"] is True
    job = db.get_adult_job(job_id, "owner-a")
    assert job is not None
    assert job["status"] == "succeeded"

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from ai_adult_testkit import (
    CHARACTER_B_ID,
    FakeModelRouter,
    seed_adult_project,
    valid_adult_payload,
)
from pixiv_novel_sync.ai.adult_types import raw_sha256
from pixiv_novel_sync.ai.adult_validation import compute_provider_scope_hash
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


class GenerationRouter(FakeModelRouter):
    def __init__(self, db_path: Path) -> None:
        snapshots = {
            "main": _snapshot("a"),
            "safety": _snapshot("b", capabilities=("json",)),
            "fact_guard": _snapshot("c", capabilities=("json",)),
        }
        super().__init__(snapshots=snapshots)
        self.db_path = db_path
        self.resolve_calls: list[tuple[str, str]] = []
        self.budget_calls: list[tuple[Any, CandidateSnapshot, int]] = []

    def close(self) -> None:
        pass

    def resolve_candidates(
        self,
        agent: Any,
        stage: str = "main",
        snapshot: CandidateSnapshot | None = None,
    ) -> CandidateSnapshot:
        task_type = str(getattr(agent, "task_type", ""))
        review_kind = (
            "safety"
            if task_type == "adult_safety_review"
            else "fact_guard"
            if task_type == "adult_fact_guard"
            else "main"
        )
        self.resolve_calls.append((review_kind, stage))
        return snapshot or self.snapshots[review_kind]

    def build_prompt_budget(
        self,
        agent: Any,
        snapshot: CandidateSnapshot,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> PromptBudget:
        self.budget_calls.append((agent, snapshot, max_tokens))
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
        database = Database(self.db_path)
        try:
            job = database.get_ai_job(request.job_id)
            assert job is not None
            assert job["candidate_snapshot_hash"] == request.candidate_snapshot.snapshot_hash
            assert job["prompt_budget"] is not None
        finally:
            database.close()

        result = super().execute(request)
        result = replace(
            result,
            job_id=request.job_id,
            candidate_snapshot_hash=request.candidate_snapshot.snapshot_hash,
        )
        if result.finish_state == "partial":
            database = Database(self.db_path)
            try:
                assert database.finish_ai_job_cas(
                    request.job_id,
                    request.owner_token,
                    "partial",
                    output_text=result.output_text,
                    error_message="transport: interrupted",
                )
            finally:
                database.close()
        return result


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "adult-generation.db")
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
def fake_router(db: Database) -> GenerationRouter:
    return GenerationRouter(db.path)


@pytest.fixture
def service(db: Database, fake_router: GenerationRouter):
    instance = AIWritingService(db.path)
    instance.model_router.close()
    instance.model_router = fake_router
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def adult_payload(fake_router: GenerationRouter) -> dict[str, Any]:
    scope_hash = compute_provider_scope_hash(
        {
            "main": fake_router.snapshots["main"],
            "safety": fake_router.snapshots["safety"],
            "fact_guard": fake_router.snapshots["fact_guard"],
        }
    )
    return valid_adult_payload(provider_scope_hash=scope_hash)


def _job_id(events: list[Any]) -> str:
    return next(
        str(event.data["job_id"])
        for event in events
        if event.type == "metadata" and event.data
    )


def test_preflight_failure_never_resolves_or_executes_router(
    service: AIWritingService,
    fake_router: GenerationRouter,
    adult_payload: dict[str, Any],
):
    adult_payload["adult_characters_confirmed"] = False

    events = list(
        service.stream_adult_polish(adult_payload, "owner-a", "lease-a")
    )

    assert fake_router.resolve_calls == []
    assert fake_router.execute_count == 0
    assert events[-1].type == "error"
    assert not any(event.type in {"delta", "candidate"} for event in events)


def test_named_character_must_be_in_explicit_participant_list(
    service: AIWritingService,
    fake_router: GenerationRouter,
    adult_payload: dict[str, Any],
):
    adult_payload["participant_character_ids"] = [CHARACTER_B_ID]

    events = list(
        service.stream_adult_polish(adult_payload, "owner-a", "lease-a")
    )

    assert fake_router.resolve_calls == []
    assert fake_router.execute_count == 0
    assert events[-1].type == "error"
    assert events[-1].data and "参与者" in events[-1].data["message"]


def test_partial_is_scrubbed_and_never_emits_candidate(
    db: Database,
    service: AIWritingService,
    fake_router: GenerationRouter,
    adult_payload: dict[str, Any],
):
    fake_router.result = RouteResult(
        job_id="pending",
        output_text="未完成",
        candidate_snapshot_hash="0" * 64,
        attempts=(),
        finish_state="partial",
    )

    events = list(
        service.stream_adult_polish(adult_payload, "owner-a", "lease-a")
    )

    assert any(
        event.type == "error" and event.data and event.data["code"] == "partial"
        for event in events
    )
    assert not any(event.type in {"delta", "candidate"} for event in events)
    job = db.get_adult_job(_job_id(events), "owner-a")
    assert job is not None
    assert job["status"] == "partial"
    assert job.get("output_text") is None


def test_same_idempotency_key_reuses_job_without_second_execute(
    service: AIWritingService,
    fake_router: GenerationRouter,
    adult_payload: dict[str, Any],
):
    first = list(
        service.stream_adult_polish(adult_payload, "owner-a", "lease-a")
    )
    second = list(
        service.stream_adult_polish(adult_payload, "owner-a", "lease-b")
    )

    assert fake_router.execute_count == 1
    assert _job_id(first) == _job_id(second)
    assert any(
        event.type == "metadata"
        and event.data
        and event.data.get("replayed") is True
        for event in second
    )


def test_job_input_contains_only_metadata_hashes_and_lengths(
    db: Database,
    service: AIWritingService,
    adult_payload: dict[str, Any],
):
    events = list(
        service.stream_adult_polish(adult_payload, "owner-a", "lease-a")
    )
    job = db.get_adult_job(_job_id(events), "owner-a")
    assert job is not None
    saved = job["input"]
    serialized = json.dumps(saved, ensure_ascii=False)
    chapter = db.get_ai_chapter(adult_payload["chapter_id"])["content"]
    target = chapter[
        adult_payload["target_start"] : adult_payload["target_end"]
    ]

    assert chapter not in serialized
    assert target not in serialized
    assert adult_payload["instruction"] not in serialized
    assert adult_payload["idempotency_key"] not in serialized
    assert saved["instruction_hash"] == raw_sha256(adult_payload["instruction"])
    assert saved["instruction_length"] == len(adult_payload["instruction"])
    assert saved["idempotency_key_hash"] == raw_sha256(
        adult_payload["idempotency_key"]
    )
    assert "messages" not in saved
    assert "prompt" not in saved


def test_main_route_request_carries_cancel_checker(
    service: AIWritingService,
    fake_router: GenerationRouter,
    adult_payload: dict[str, Any],
):
    events = list(
        service.stream_adult_polish(adult_payload, "owner-a", "lease-a")
    )

    assert any(event.type == "metadata" for event in events)
    request = fake_router.requests[0]
    assert callable(request.is_cancelled)
    # 未取消时回调返回 False
    assert request.is_cancelled() is False


def test_cancel_checker_reflects_db_cancel_flag(
    db: Database,
    service: AIWritingService,
    adult_payload: dict[str, Any],
):
    prepared = service.prepare_adult_job(
        adult_payload,
        "owner-a",
        owner_token="lease-a",
    )
    checker = service._adult_cancel_checker(
        prepared.job_id,
        "owner-a",
        min_interval=0.0,
    )
    cached_checker = service._adult_cancel_checker(
        prepared.job_id,
        "owner-a",
        min_interval=3600.0,
    )
    assert checker() is False
    assert cached_checker() is False

    assert db.request_adult_job_cancel(prepared.job_id, "owner-a", "lease-a")

    # 最小间隔缓存：窗口内不再查库，仍返回 False（避免每 token 查库）
    assert cached_checker() is False
    # 无间隔限制的检查器立即看到取消标志，且此后保持 True
    assert checker() is True
    assert checker() is True


def test_cancelled_route_result_discards_partial_output(
    db: Database,
    service: AIWritingService,
    fake_router: GenerationRouter,
    adult_payload: dict[str, Any],
):
    fake_router.result = RouteResult(
        job_id="pending",
        output_text="部分输出",
        candidate_snapshot_hash="0" * 64,
        attempts=(),
        finish_state="cancelled",
    )

    events = list(
        service.stream_adult_polish(adult_payload, "owner-a", "lease-a")
    )

    assert any(
        event.type == "error" and event.data and event.data["code"] == "cancelled"
        for event in events
    )
    assert not any(event.type in {"delta", "candidate"} for event in events)
    job = db.get_adult_job(_job_id(events), "owner-a")
    assert job is not None
    assert job["status"] == "cancelled"
    assert job.get("output_text") is None


def test_progress_streams_before_route_completion(
    service: AIWritingService,
    fake_router: GenerationRouter,
    adult_payload: dict[str, Any],
):
    stream = service.stream_adult_polish(adult_payload, "owner-a", "lease-a")
    first = next(stream)
    assert first.type == "metadata"
    second = next(stream)
    # execute_stream 的进度事件必须在路由完成前实时转发
    assert second.type == "progress"
    assert fake_router.execute_count == 0
    rest = list(stream)
    assert rest


def test_progress_events_are_whitelisted(
    service: AIWritingService,
    adult_payload: dict[str, Any],
):
    from pixiv_novel_sync.ai.services.adult import _ADULT_PROGRESS_FIELDS

    events = list(
        service.stream_adult_polish(adult_payload, "owner-a", "lease-a")
    )
    progress = [event for event in events if event.type == "progress"]
    assert progress
    for event in progress:
        assert set(event.data or {}) <= set(_ADULT_PROGRESS_FIELDS)
    assert not any(event.type == "delta" for event in events)

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Generator
from dataclasses import asdict, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from pixiv_novel_sync.ai import service as service_facade
from pixiv_novel_sync.ai.model_router import (
    CandidateSnapshot,
    ModelCandidate,
    ModelRouter,
    PromptBudget,
    RouteRequest,
    RouteResult,
)
from pixiv_novel_sync.ai.models import AIAgentConfig, AIStreamChunk
from pixiv_novel_sync.ai.service import AIServiceError, AIWritingService
from pixiv_novel_sync.storage_db import Database


MESSAGES = [{"role": "user", "content": "正文"}]


def success_result(job_id: str, text: str = "正文") -> RouteResult:
    return RouteResult(
        job_id=job_id,
        output_text=text,
        candidate_snapshot_hash="f" * 64,
        attempts=(),
        finish_state="succeeded",
    )


def failed_before_output_result(job_id: str = "pending") -> RouteResult:
    return RouteResult(
        job_id=job_id,
        output_text="",
        candidate_snapshot_hash="f" * 64,
        attempts=(),
        finish_state="failed_before_output",
    )


def partial_result(job_id: str = "pending", text: str = "半截") -> RouteResult:
    return RouteResult(
        job_id=job_id,
        output_text=text,
        candidate_snapshot_hash="f" * 64,
        attempts=(),
        finish_state="partial",
    )


class FakeModelRouter:
    def __init__(self) -> None:
        candidate = ModelCandidate(
            provider_id=1,
            provider_name="provider",
            model_key="model-a",
            provider_model_id=None,
            pool_id=None,
            pool_name=None,
            pool_version=None,
            pool_position=None,
            provider_config_hash="a" * 64,
            context_window=8_000,
        )
        candidates = (candidate,)
        snapshot_payload = {
            "agent_config_hash": "e" * 64,
            "binding_version": 1,
            "candidates": [asdict(item) for item in candidates],
        }
        serialized = json.dumps(
            snapshot_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.snapshot = CandidateSnapshot(
            candidates=candidates,
            snapshot_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            agent_config_hash="e" * 64,
            binding_version=1,
        )
        self.budget = PromptBudget(
            effective_context_window=8_000,
            input_budget=6_738,
            output_reserve=1_000,
            message_overhead=6,
            safety_margin=256,
            estimator="utf8_bytes",
        )
        self.resolve_calls: list[tuple[int, str, CandidateSnapshot | None]] = []
        self.budget_calls: list[tuple[int, CandidateSnapshot, list[dict[str, str]], int]] = []
        self.requests: list[RouteRequest] = []
        self.provider_calls: list[tuple[str, str]] = []
        self.results: list[RouteResult] = []
        self.stream_chunks: list[list[AIStreamChunk]] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def queue_result(
        self,
        result: RouteResult,
        chunks: list[AIStreamChunk] | None = None,
    ) -> None:
        self.results.append(result)
        self.stream_chunks.append(list(chunks or []))

    def resolve_candidates(
        self,
        agent: AIAgentConfig,
        stage: str = "main",
        snapshot: CandidateSnapshot | None = None,
    ) -> CandidateSnapshot:
        self.resolve_calls.append((agent.id, stage, snapshot))
        return snapshot or self.snapshot

    def build_prompt_budget(
        self,
        agent: AIAgentConfig,
        snapshot: CandidateSnapshot,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> PromptBudget:
        self.budget_calls.append((agent.id, snapshot, messages, max_tokens))
        return self.budget

    def execute(self, request: RouteRequest) -> RouteResult:
        generator = self.execute_stream(request)
        while True:
            try:
                next(generator)
            except StopIteration as stopped:
                return stopped.value

    def execute_stream(
        self,
        request: RouteRequest,
    ) -> Generator[AIStreamChunk, None, RouteResult]:
        self.requests.append(request)
        candidate = request.candidate_snapshot.candidates[0]
        self.provider_calls.append((candidate.provider_name, candidate.model_key))
        chunks = (
            self.stream_chunks.pop(0)
            if self.stream_chunks
            else [
                AIStreamChunk(type="progress", data={"phase": "route"}),
                AIStreamChunk(type="delta", text="正文"),
            ]
        )
        for chunk in chunks:
            if chunk.type == "progress":
                request.on_progress(dict(chunk.data or {}))
            elif chunk.type == "delta":
                request.on_delta(chunk.text)
            yield chunk
        if self.results:
            result = self.results.pop(0)
            return replace(
                result,
                job_id=request.job_id,
                candidate_snapshot_hash=request.candidate_snapshot.snapshot_hash,
            )
        return success_result(request.job_id)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "route-integration.db")
    database.init_schema()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def fake_router() -> FakeModelRouter:
    return FakeModelRouter()


@pytest.fixture
def service(db: Database, fake_router: FakeModelRouter) -> AIWritingService:
    writing_service = AIWritingService(db.path)
    writing_service.model_router = fake_router
    try:
        yield writing_service
    finally:
        writing_service.close()


@pytest.fixture
def fixed_agent(db: Database, service: AIWritingService) -> AIAgentConfig:
    provider_id = db.create_ai_provider(
        {
            "name": "provider",
            "provider_type": "openai_compatible",
            "base_url": "https://provider.example.test/v1",
            "api_key_encrypted": "ciphertext",
            "default_model": "model-a",
            "enabled": True,
        }
    )
    agent_id = db.create_ai_agent(
        {
            "name": "Agent",
            "task_type": "continue",
            "binding_type": "fixed",
            "provider_id": provider_id,
            "model": "model-a",
            "system_prompt": "secret prompt",
            "max_tokens": 1_000,
            "context_window": 8_000,
        }
    )
    return service._load_agent_config(db, agent_id)


@pytest.fixture
def route_context(
    service: AIWritingService,
    db: Database,
    fixed_agent: AIAgentConfig,
) -> Any:
    return service._start_route_job(
        db,
        "continue",
        fixed_agent,
        {"source_type": "manual"},
        messages=MESSAGES,
        max_tokens=1_000,
    )


def collect_generator_return(
    generator: Generator[AIStreamChunk, None, RouteResult],
) -> tuple[list[AIStreamChunk], RouteResult]:
    chunks: list[AIStreamChunk] = []
    while True:
        try:
            chunks.append(next(generator))
        except StopIteration as stopped:
            return chunks, stopped.value


def test_service_initializes_one_shared_model_router(tmp_path: Path) -> None:
    service = AIWritingService(tmp_path / "shared-router.db")
    try:
        assert isinstance(service.model_router, ModelRouter)
        assert service.model_router is service.model_router
        assert "RouteJobContext" in service_facade.__all__
    finally:
        service.close()


def test_route_job_context_contract_field_order(route_context: Any) -> None:
    assert [field.name for field in fields(route_context)] == [
        "job_id",
        "owner_token",
        "agent",
        "candidate_snapshot",
        "prompt_budget",
        "resume_candidate_index",
    ]


def test_start_route_job_persists_snapshot_budget_and_private_owner_before_call(
    service: AIWritingService,
    db: Database,
    fixed_agent: AIAgentConfig,
    fake_router: FakeModelRouter,
) -> None:
    before = datetime.now(timezone.utc)
    context = service._start_route_job(
        db,
        "continue",
        fixed_agent,
        {"source_type": "manual"},
        messages=MESSAGES,
        max_tokens=1_000,
    )
    after = datetime.now(timezone.utc)

    job = db.get_ai_job(context.job_id)
    assert job is not None
    assert job["candidate_snapshot_hash"] == context.candidate_snapshot.snapshot_hash
    assert job["prompt_budget"] == asdict(context.prompt_budget)
    assert job["candidate_snapshot"]["agent_config_hash"] == "e" * 64
    serialized_snapshot = str(job["candidate_snapshot"])
    assert "secret prompt" not in serialized_snapshot
    assert "正文" not in serialized_snapshot
    assert "owner_token" not in job
    raw = db.conn.execute(
        "SELECT owner_token, route_deadline_at FROM ai_jobs WHERE job_id = ?",
        (context.job_id,),
    ).fetchone()
    assert raw["owner_token"] == context.owner_token
    deadline = datetime.fromisoformat(raw["route_deadline_at"]).replace(
        tzinfo=timezone.utc
    )
    assert before + timedelta(minutes=29) < deadline
    assert deadline <= after + timedelta(minutes=30)
    assert (deadline - before).total_seconds() <= 30 * 60
    assert fake_router.provider_calls == []
    assert fake_router.resolve_calls == [(fixed_agent.id, "main", None)]
    assert fake_router.budget_calls[0][3] == 1_000


def test_candidate_snapshot_excludes_prompt_content_and_secret_fields(
    service: AIWritingService,
    db: Database,
    fixed_agent: AIAgentConfig,
) -> None:
    context = service._start_route_job(
        db,
        "continue",
        fixed_agent,
        {"source_type": "manual", "output_text": "不得进入快照"},
        messages=[{"role": "user", "content": "私密正文"}],
        max_tokens=1_000,
    )
    row = db.conn.execute(
        "SELECT candidate_snapshot_json FROM ai_jobs WHERE job_id = ?",
        (context.job_id,),
    ).fetchone()
    snapshot = json.loads(row["candidate_snapshot_json"])

    def all_keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value).union(*(all_keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(all_keys(item) for item in value))
        return set()

    assert {
        "api_key",
        "api_key_encrypted",
        "messages",
        "output_text",
        "prompt",
    }.isdisjoint(all_keys(snapshot))
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert "私密正文" not in serialized
    assert "secret prompt" not in serialized


def test_route_job_hard_limits_are_16_32_and_30_minutes(
    service: AIWritingService,
    db: Database,
    fixed_agent: AIAgentConfig,
) -> None:
    before = datetime.now(timezone.utc)
    context = service._start_route_job(
        db,
        "continue",
        fixed_agent,
        {"source_type": "manual"},
        messages=MESSAGES,
        max_tokens=1_000,
    )
    row = db.conn.execute(
        """
        SELECT candidate_attempt_count, network_request_count, route_deadline_at
        FROM ai_jobs WHERE job_id = ?
        """,
        (context.job_id,),
    ).fetchone()
    deadline = datetime.fromisoformat(row["route_deadline_at"]).replace(
        tzinfo=timezone.utc
    )

    assert row["candidate_attempt_count"] == 0
    assert row["network_request_count"] == 0
    assert timedelta(minutes=29) < deadline - before <= timedelta(minutes=30)
    router_source = (Path(__file__).parents[1] / "src" / "pixiv_novel_sync" / "ai" / "model_router.py").read_text(encoding="utf-8")
    # 上限提成了具名常量（候选链预览要展示同一组数字），断言常量值与比较点都在
    assert "MAX_CANDIDATE_ATTEMPTS = 16" in router_source
    assert "MAX_NETWORK_REQUESTS = 32" in router_source
    assert ">= MAX_CANDIDATE_ATTEMPTS" in router_source
    assert ">= MAX_NETWORK_REQUESTS" in router_source


def test_stream_route_forwards_progress_delta_and_result(
    service: AIWritingService,
    route_context: Any,
    fake_router: FakeModelRouter,
) -> None:
    chunks, result = collect_generator_return(
        service._stream_route(route_context, MESSAGES, stage="main")
    )

    assert [chunk.type for chunk in chunks] == ["progress", "delta"]
    assert result.finish_state == "succeeded"
    assert fake_router.provider_calls == [("provider", "model-a")]
    request = fake_router.requests[-1]
    assert request.job_id == route_context.job_id
    assert request.owner_token == route_context.owner_token
    assert request.candidate_snapshot is route_context.candidate_snapshot
    assert request.max_tokens == route_context.prompt_budget.output_reserve
    assert request.temperature == route_context.agent.temperature
    assert request.top_p == route_context.agent.top_p


def test_stream_route_rejects_output_reserve_larger_than_saved_budget(
    service: AIWritingService,
    route_context: Any,
) -> None:
    with pytest.raises(AIServiceError, match="max_tokens"):
        next(
            service._stream_route(
                route_context,
                MESSAGES,
                max_tokens=route_context.prompt_budget.output_reserve + 1,
            )
        )


def test_finish_route_job_uses_owner_cas_and_never_overwrites_router_terminal(
    service: AIWritingService,
    db: Database,
    route_context: Any,
) -> None:
    assert db.finish_ai_job_cas(
        route_context.job_id,
        route_context.owner_token,
        "partial",
        output_text="半截",
        error_message="network",
    )

    assert service._finish_route_job(
        db,
        route_context,
        "succeeded",
        "迟到正文",
    ) is False
    job = db.get_ai_job(route_context.job_id)
    assert job["status"] == "partial"
    assert job["output_text"] == "半截"


def test_cancel_route_job_is_owner_scoped(
    service: AIWritingService,
    db: Database,
    route_context: Any,
) -> None:
    assert service._cancel_route_job(db, route_context, "客户端断开") is True
    assert service._cancel_route_job(db, route_context, "重复取消") is False
    job = db.get_ai_job(route_context.job_id)
    assert job["status"] == "cancelled"
    assert "客户端断开" in job["error_message"]


def test_internal_route_failure_does_not_close_parent_job(
    service: AIWritingService,
    db: Database,
    route_context: Any,
    fake_router: FakeModelRouter,
) -> None:
    fake_router.results.append(failed_before_output_result(route_context.job_id))

    _chunks, result = collect_generator_return(
        service._stream_route(route_context, MESSAGES, stage="internal")
    )

    assert result.finish_state == "failed_before_output"
    assert fake_router.requests[-1].stage == "internal"
    assert db.get_ai_job(route_context.job_id)["status"] == "running"


def test_close_closes_shared_router_resource(
    service: AIWritingService,
    fake_router: FakeModelRouter,
) -> None:
    service.close()

    assert fake_router.closed is True


def test_closed_service_rejects_new_route_dispatch(
    service: AIWritingService,
    route_context: Any,
    fake_router: FakeModelRouter,
) -> None:
    service.close()

    with pytest.raises(AIServiceError, match="已关闭"):
        next(service._stream_route(route_context, MESSAGES))
    assert fake_router.requests == []


def test_keyword_clean_pool_capable_agent_keeps_graceful_degradation(
    service: AIWritingService,
    fake_router: FakeModelRouter,
    db: Database,
) -> None:
    pool_id = db.create_ai_model_pool(
        {"name": "关键词池", "pool_kind": "custom"}
    )
    agent_id = db.create_ai_agent(
        {
            "name": "关键词清洗",
            "task_type": "keyword_clean",
            "binding_type": "pool",
            "provider_id": None,
            "model": None,
            "model_pool_id": pool_id,
            "system_prompt": "关键词清洗提示词",
            "enabled": True,
        }
    )
    fake_router.queue_result(failed_before_output_result(), [])

    assert service.clean_keywords(["噪声", "关键词"]) is None
    assert fake_router.requests[-1].stage == "main"
    job = db.get_ai_job(fake_router.requests[-1].job_id)
    assert job["agent_id"] == agent_id
    assert job["status"] == "failed"


def test_business_modules_have_no_direct_provider_stream_calls() -> None:
    """业务生成必须走 ModelRouter；遍历 ai/services/ 现存文件做 AST 守卫。

    admin.py 例外：它的 test_provider 是显式的连接测试（spec 允许直连），
    不属于业务生成路径。
    """
    services_root = (
        Path(__file__).parents[1]
        / "src"
        / "pixiv_novel_sync"
        / "ai"
        / "services"
    )
    offenders = []
    for path in sorted(services_root.glob("*.py")):
        if path.name in {"__init__.py", "admin.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "stream_generate"
            ):
                offenders.append(f"{path.name}:{getattr(node, 'lineno', '?')}")

    assert offenders == []


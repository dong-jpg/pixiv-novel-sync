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


def long_text_payload(agent_id: int) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "source_type": "manual",
        "text": "前文" * 1_000,
        "smart_context": True,
        "context_chars": 5_000,
    }


def collected_delta(chunks: list[AIStreamChunk]) -> str:
    return "".join(chunk.text for chunk in chunks if chunk.type == "delta")


def create_pool_agent(db: Database, *, task_type: str) -> int:
    pool_id = db.create_ai_model_pool(
        {"name": f"{task_type}-pool", "pool_kind": "custom"}
    )
    return db.create_ai_agent(
        {
            "name": f"{task_type}-agent",
            "task_type": task_type,
            "binding_type": "pool",
            "provider_id": None,
            "model": None,
            "model_pool_id": pool_id,
            "system_prompt": f"{task_type}-prompt",
            "enabled": True,
        }
    )


def test_continue_uses_internal_route_then_main_without_internal_body(
    service: AIWritingService,
    fixed_agent: AIAgentConfig,
    fake_router: FakeModelRouter,
    db: Database,
) -> None:
    fake_router.budget = replace(fake_router.budget, input_budget=1_000)
    fake_router.queue_result(
        success_result("pending", "摘要"),
        [AIStreamChunk(type="delta", text="摘要")],
    )
    fake_router.queue_result(
        success_result("pending", "续写"),
        [AIStreamChunk(type="delta", text="续写")],
    )

    chunks = list(service.stream_continue(long_text_payload(fixed_agent.id)))

    assert [request.stage for request in fake_router.requests] == [
        "internal",
        "main",
    ]
    assert fake_router.requests[0].job_id == fake_router.requests[1].job_id
    assert collected_delta(chunks) == "续写"
    assert chunks[-1].type == "done"
    job = db.get_ai_job(fake_router.requests[-1].job_id)
    assert job["status"] == "succeeded"
    assert job["pinned_candidate_index"] is None


def test_internal_summary_exhaustion_falls_back_to_tail(
    service: AIWritingService,
    fixed_agent: AIAgentConfig,
    fake_router: FakeModelRouter,
) -> None:
    fake_router.budget = replace(fake_router.budget, input_budget=1_000)
    fake_router.queue_result(failed_before_output_result(), [])
    fake_router.queue_result(
        success_result("pending", "续写"),
        [AIStreamChunk(type="delta", text="续写")],
    )

    chunks = list(service.stream_continue(long_text_payload(fixed_agent.id)))

    assert [request.stage for request in fake_router.requests] == [
        "internal",
        "main",
    ]
    assert collected_delta(chunks) == "续写"
    assert chunks[-1].type == "done"
    main_prompt = "\n".join(
        message["content"] for message in fake_router.requests[-1].messages
    )
    assert "前文" in main_prompt


def test_smart_context_omits_summary_when_no_summary_budget_remains(
    service: AIWritingService,
    route_context: Any,
    fake_router: FakeModelRouter,
) -> None:
    fake_router.queue_result(
        success_result("pending", "不应进入主上下文的摘要"),
        [AIStreamChunk(type="delta", text="不应进入主上下文的摘要")],
    )
    prompt_budget = replace(route_context.prompt_budget, input_budget=20)

    items = list(
        service._smart_context(
            "前文" * 100,
            prompt_budget,
            route_context,
        )
    )

    resolved_context = next(item for item in reversed(items) if isinstance(item, str))
    assert "不应进入主上下文的摘要" not in resolved_context
    assert "【前文摘要】" not in resolved_context
    assert resolved_context.endswith("前文" * 3)


def test_continue_partial_is_preserved_without_second_provider_call(
    service: AIWritingService,
    fixed_agent: AIAgentConfig,
    fake_router: FakeModelRouter,
    db: Database,
) -> None:
    fake_router.queue_result(
        partial_result(text="半截"),
        [AIStreamChunk(type="delta", text="半截")],
    )
    payload = {
        "agent_id": fixed_agent.id,
        "source_type": "manual",
        "text": "短原文",
        "smart_context": False,
    }

    chunks = list(service.stream_continue(payload))

    assert len(fake_router.requests) == 1
    assert collected_delta(chunks) == "半截"
    assert chunks[-1].type == "error"
    job = db.get_ai_job(fake_router.requests[0].job_id)
    assert job["status"] == "partial"
    assert job["output_text"] == "半截"


@pytest.mark.parametrize(
    "method_name,payload",
    [
        ("stream_rewrite", {"rewrite_type": "polish"}),
        ("stream_audit", {}),
        ("stream_plan", {}),
    ],
)
def test_one_shot_generation_paths_use_main_route(
    method_name: str,
    payload: dict[str, Any],
    service: AIWritingService,
    fixed_agent: AIAgentConfig,
    fake_router: FakeModelRouter,
) -> None:
    fake_router.queue_result(
        success_result("pending", "结果"),
        [AIStreamChunk(type="delta", text="结果")],
    )
    method = getattr(service, method_name)

    chunks = list(
        method(
            {
                "agent_id": fixed_agent.id,
                "source_type": "manual",
                "text": "原文",
                **payload,
            }
        )
    )

    assert fake_router.requests[-1].stage == "main"
    assert collected_delta(chunks) == "结果"
    assert chunks[-1].type == "done"


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


def test_generation_module_has_no_direct_provider_stream_calls() -> None:
    path = (
        Path(__file__).parents[1]
        / "src"
        / "pixiv_novel_sync"
        / "ai"
        / "services"
        / "generation.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    direct_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "stream_generate"
    ]

    assert direct_calls == []


def test_wizard_pool_agent_routes_without_provider_id(
    service: AIWritingService,
    fake_router: FakeModelRouter,
    db: Database,
) -> None:
    agent_id = create_pool_agent(db, task_type="chat")
    session_id = db.create_ai_chat_session(
        {
            "agent_id": agent_id,
            "scope": "wizard",
            "title": "路由会话",
        }
    )
    fake_router.queue_result(
        success_result("pending", "回答"),
        [AIStreamChunk(type="delta", text="回答")],
    )

    chunks = list(
        service.stream_chat(
            {"session_id": session_id, "user_message": "继续"}
        )
    )

    assert collected_delta(chunks) == "回答"
    assert chunks[-1].type == "done"
    assert fake_router.requests[-1].stage == "main"
    messages = db.list_ai_chat_messages(session_id)
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "回答"


def test_chapter_partial_autosave_uses_router_terminal_state(
    service: AIWritingService,
    fixed_agent: AIAgentConfig,
    fake_router: FakeModelRouter,
    db: Database,
) -> None:
    project_id = db.create_ai_writing_project(
        {"name": "章节项目", "outline": "项目大纲"}
    )
    chapter_id = db.create_ai_chapter(
        {
            "project_id": project_id,
            "chapter_number": 1,
            "title": "第一章",
            "outline": "章节大纲",
            "content": "已有正文",
            "status": "draft",
        }
    )
    fake_router.queue_result(
        partial_result(text="半截"),
        [AIStreamChunk(type="delta", text="半截")],
    )

    chunks = list(
        service.stream_chapter_continue(
            {
                "agent_id": fixed_agent.id,
                "project_id": project_id,
                "chapter_id": chapter_id,
            }
        )
    )

    chapter = db.get_ai_chapter(chapter_id)
    assert chapter["content"] == "已有正文半截"
    assert chapter["status"] == "draft"
    assert chapter["metadata"]["continue_autosave"]["status"] == "partial"
    assert chunks[-1].type == "error"
    assert len(fake_router.requests) == 1


def test_longform_plan_uses_candidate_snapshot_and_normal_finish(
    service: AIWritingService,
    fixed_agent: AIAgentConfig,
    fake_router: FakeModelRouter,
    db: Database,
) -> None:
    project_id = db.create_ai_writing_project({"name": "长篇项目"})
    output = json.dumps(
        {
            "project_outline": "全书大纲",
            "expected_chapter_count": 1,
            "chapters": [
                {
                    "chapter_number": 1,
                    "title": "开篇",
                    "outline": "故事开端",
                    "target_words": 4000,
                }
            ],
        },
        ensure_ascii=False,
    )
    fake_router.queue_result(
        success_result("pending", output),
        [AIStreamChunk(type="delta", text=output)],
    )

    chunks = list(
        service.stream_longform_plan(
            {
                "agent_id": fixed_agent.id,
                "project_id": project_id,
                "target_words": 4000,
            }
        )
    )

    assert fake_router.requests[-1].candidate_snapshot.snapshot_hash
    assert fake_router.requests[-1].stage == "main"
    assert chunks[-1].type == "done"
    project = db.get_ai_writing_project(project_id)
    assert project["settings"]["longform_plan"]["project_outline"] == "全书大纲"


def test_longform_plan_caps_project_material_to_route_budget(
    service: AIWritingService,
    fixed_agent: AIAgentConfig,
    fake_router: FakeModelRouter,
    db: Database,
) -> None:
    project_id = db.create_ai_writing_project(
        {
            "name": "预算长篇",
            "outline": "长篇开头标记" + "中段" * 5000 + "长篇结尾标记",
        }
    )
    output = json.dumps(
        {
            "project_outline": "新大纲",
            "expected_chapter_count": 1,
            "chapters": [
                {
                    "chapter_number": 1,
                    "title": "开篇",
                    "outline": "故事开端",
                    "target_words": 4000,
                }
            ],
        },
        ensure_ascii=False,
    )
    fake_router.budget = replace(fake_router.budget, input_budget=8000)
    fake_router.queue_result(
        success_result("pending", output),
        [AIStreamChunk(type="delta", text=output)],
    )

    chunks = list(
        service.stream_longform_plan(
            {
                "agent_id": fixed_agent.id,
                "project_id": project_id,
                "target_words": 4000,
            }
        )
    )

    prompt = "\n".join(
        message["content"] for message in fake_router.requests[-1].messages
    )
    assert "长篇开头标记" not in prompt
    assert "长篇结尾标记" in prompt
    assert chunks[-1].type == "done"


def test_chapter_context_uses_route_snapshot_budget(
    service: AIWritingService,
    fixed_agent: AIAgentConfig,
    fake_router: FakeModelRouter,
    db: Database,
) -> None:
    project_id = db.create_ai_writing_project({"name": "预算项目"})
    chapter_id = db.create_ai_chapter(
        {
            "project_id": project_id,
            "chapter_number": 1,
            "title": "第一章",
            "content": "早期内容" + "中" * 500 + "末尾锚点",
        }
    )
    fake_router.budget = replace(
        fake_router.budget,
        input_budget=200,
    )
    fake_router.queue_result(
        success_result("pending", "续写"),
        [AIStreamChunk(type="delta", text="续写")],
    )

    chunks = list(
        service.stream_chapter_continue(
            {
                "agent_id": fixed_agent.id,
                "project_id": project_id,
                "chapter_id": chapter_id,
                "auto_save": False,
            }
        )
    )

    prompt = "\n".join(
        message["content"] for message in fake_router.requests[-1].messages
    )
    assert "早期内容" not in prompt
    assert "末尾锚点" in prompt
    assert chunks[-1].type == "done"


def test_update_project_state_uses_main_route_and_persists_sections(
    service: AIWritingService,
    fixed_agent: AIAgentConfig,
    fake_router: FakeModelRouter,
    db: Database,
) -> None:
    project_id = db.create_ai_writing_project({"name": "状态项目"})
    chapter_id = db.create_ai_chapter(
        {
            "project_id": project_id,
            "chapter_number": 1,
            "title": "第一章",
            "content": "角色走进旧宅。",
        }
    )
    output = (
        "=== character_state ===\n角色保持警惕\n"
        "=== plot_progress ===\n调查进入旧宅\n"
        "=== new_foreshadows ===\n"
    )
    fake_router.budget = replace(
        fake_router.budget,
        input_budget=5000,
        output_reserve=2000,
    )
    fake_router.queue_result(
        success_result("pending", output),
        [AIStreamChunk(type="delta", text=output)],
    )

    chunks = list(
        service.stream_update_project_state(
            {
                "agent_id": fixed_agent.id,
                "project_id": project_id,
                "chapter_id": chapter_id,
            }
        )
    )

    assert fake_router.requests[-1].stage == "main"
    assert chunks[-1].type == "done"
    states = db.get_all_project_states(project_id)
    assert states["character_state"] == "角色保持警惕"
    assert states["plot_progress"] == "调查进入旧宅"


def test_task15_methods_have_no_direct_provider_stream_calls() -> None:
    services_root = (
        Path(__file__).parents[1]
        / "src"
        / "pixiv_novel_sync"
        / "ai"
        / "services"
    )
    targets = {
        services_root / "chat_wizard.py": {"stream_chat"},
        services_root / "projects.py": {
            "stream_longform_plan",
            "stream_longform_plan_details",
            "stream_chapter_continue",
            "stream_update_project_state",
        },
    }
    offenders: list[str] = []
    for path, method_names in targets.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in method_names:
                continue
            if any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "stream_generate"
                for child in ast.walk(node)
            ):
                offenders.append(f"{path.name}:{node.name}")

    assert offenders == []

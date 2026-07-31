from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pixiv_novel_sync.ai.model_router import PromptBudget, RouteResult
from pixiv_novel_sync.ai.models import AIAgentConfig, AIStreamChunk
from pixiv_novel_sync.ai.service import AIServiceError, AIWritingService


class FakeDB:
    def __init__(self) -> None:
        self.created_jobs: list[tuple] = []
        self.updated_jobs: list[tuple] = []
        self.closed = False

    def create_ai_job(self, job_id, task_type, agent_id, input_json):
        self.created_jobs.append((job_id, task_type, agent_id, input_json))

    def update_ai_job(self, job_id, status, output_text=None, output_json=None, error_message=None):
        self.updated_jobs.append((job_id, status, output_text, output_json, error_message))

    def close(self) -> None:
        self.closed = True


def test_stream_continue_completes_after_smart_context_fallback(monkeypatch, tmp_path):
    service = AIWritingService(Path(tmp_path / "test.db"))
    fake_db = FakeDB()
    agent = AIAgentConfig(
        id=1,
        name="续写",
        task_type="continue",
        provider_id=2,
        model="model-a",
        system_prompt="system",
        context_window=1000,
    )
    route_context = SimpleNamespace(
        job_id="continue-job",
        prompt_budget=PromptBudget(
            effective_context_window=6000,
            input_budget=1000,
            output_reserve=4000,
            message_overhead=744,
            safety_margin=256,
            estimator="utf8_bytes",
        ),
    )
    start_calls = []
    finish_calls = []

    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: agent)
    monkeypatch.setattr(service, "_resolve_input_text", lambda _db, _payload: "原文" * 100)

    def start_route_job(db, task_type, selected_agent, input_data, **options):
        start_calls.append((db, task_type, selected_agent, input_data, options))
        return route_context

    def fallback_smart_context(*_args, **_kwargs):
        yield AIStreamChunk(type="progress", data={"message": "摘要候选已耗尽"})
        yield "最近原文"

    def stream_route(context, messages, **_options):
        prompt = "\n".join(message["content"] for message in messages)
        assert "最近原文" in prompt
        yield AIStreamChunk(type="delta", text="续写正文")
        return RouteResult(
            job_id=context.job_id,
            output_text="续写正文",
            candidate_snapshot_hash="f" * 64,
            attempts=(),
            finish_state="succeeded",
        )

    def finish_route_job(
        _db,
        _context,
        status,
        output_text,
        output_json=None,
        error_message=None,
    ):
        finish_calls.append(
            (status, output_text, output_json, error_message)
        )
        return True

    monkeypatch.setattr(service, "_start_route_job", start_route_job)
    monkeypatch.setattr(service, "_smart_context", fallback_smart_context)
    monkeypatch.setattr(service, "_stream_route", stream_route)
    monkeypatch.setattr(service, "_finish_route_job", finish_route_job)

    chunks = list(service.stream_continue({"agent_id": 1, "smart_context": True, "context_chars": 1000}))

    assert chunks[0].type == "metadata"
    assert chunks[0].data == {"job_id": "continue-job"}
    assert any(chunk.type == "progress" for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks if chunk.type == "delta") == "续写正文"
    assert chunks[-1].type == "done"
    assert start_calls[0][1] == "continue"
    input_data = start_calls[0][3]
    assert input_data["input_context_chars"] == len("原文" * 100)
    assert input_data["smart_context"] is True
    assert input_data["requested_context_chars"] == 1000
    assert finish_calls[-1][0] == "succeeded"
    assert finish_calls[-1][1] == "续写正文"


class FakeChapterDB(FakeDB):
    def __init__(self) -> None:
        super().__init__()
        self.chapter = {
            "id": 3,
            "project_id": 4,
            "chapter_number": 2,
            "content": "已有正文",
            "outline": "章节大纲",
            "metadata": {},
        }
        self.updated_chapters: list[tuple[int, dict]] = []
        self.metadata_patches: list[tuple[int, dict]] = []

    def get_ai_chapter(self, chapter_id: int):
        return self.chapter if chapter_id == 3 else None

    def get_ai_writing_project(self, _project_id: int):
        return {"id": 4, "outline": "项目大纲", "settings": {}}

    def get_all_project_states(self, _project_id: int):
        return {}

    def get_approaching_foreshadows(self, *_args):
        return []

    def get_overdue_foreshadows(self, *_args):
        return []

    def list_ai_chapters(self, _project_id: int):
        return []

    def update_ai_chapter(self, chapter_id: int, payload: dict):
        self.updated_chapters.append((chapter_id, payload))

    def patch_ai_chapter_metadata(self, chapter_id: int, patch: dict):
        self.metadata_patches.append((chapter_id, patch))
        return patch


def make_chapter_agent() -> AIAgentConfig:
    return AIAgentConfig(
        id=1,
        name="章节续写",
        task_type="continue",
        provider_id=2,
        model="model-a",
        system_prompt="system",
        context_window=1000,
    )

def wire_route(
    monkeypatch,
    service: AIWritingService,
    fake_db: FakeDB,
    chunks: list[str],
    *,
    finish_state: str = "succeeded",
):
    output = "".join(chunks)
    route_context = SimpleNamespace(
        job_id="chapter-route-job",
        prompt_budget=PromptBudget(
            effective_context_window=6000,
            input_budget=1000,
            output_reserve=4000,
            message_overhead=744,
            safety_margin=256,
            estimator="utf8_bytes",
        ),
    )
    route_calls = []

    def start_route_job(_db, _task_type, _agent, _input_data, **_options):
        return route_context

    def stream_route(context, messages, **options):
        route_calls.append((context, messages, options))
        for text in chunks:
            yield AIStreamChunk(type="delta", text=text)
        return RouteResult(
            job_id=context.job_id,
            output_text=output,
            candidate_snapshot_hash="f" * 64,
            attempts=(),
            finish_state=finish_state,
        )

    def finish_route_job(
        _db,
        context,
        status,
        output_text,
        output_json=None,
        error_message=None,
    ):
        fake_db.update_ai_job(
            context.job_id,
            status,
            output_text=output_text,
            output_json=output_json,
            error_message=error_message,
        )
        return True

    def cancel_route_job(_db, context, error_message=None):
        fake_db.update_ai_job(
            context.job_id,
            "cancelled",
            error_message=error_message,
        )
        return True

    monkeypatch.setattr(service, "_start_route_job", start_route_job)
    monkeypatch.setattr(service, "_stream_route", stream_route)
    monkeypatch.setattr(service, "_finish_route_job", finish_route_job)
    monkeypatch.setattr(service, "_cancel_route_job", cancel_route_job)
    return route_calls


def test_stream_chapter_continue_autosaves_final_content(monkeypatch, tmp_path):
    service = AIWritingService(Path(tmp_path / "test.db"))
    fake_db = FakeChapterDB()
    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: make_chapter_agent())
    route_calls = wire_route(monkeypatch, service, fake_db, ["新", "内容"])

    chunks = list(service.stream_chapter_continue({"agent_id": 1, "project_id": 4, "chapter_id": 3}))

    assert chunks[-1].type == "done"
    assert fake_db.updated_chapters[-1] == (3, {"content": "已有正文新内容", "status": "draft"})
    assert fake_db.metadata_patches[-1][1]["continue_autosave"]["status"] == "succeeded"
    assert fake_db.updated_jobs[-1][1] == "succeeded"
    assert fake_db.updated_jobs[-1][3]["autosaved"] is True
    assert route_calls[-1][2] == {}


def test_stream_chapter_continue_respects_auto_save_false(monkeypatch, tmp_path):
    service = AIWritingService(Path(tmp_path / "test.db"))
    fake_db = FakeChapterDB()
    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: make_chapter_agent())
    wire_route(monkeypatch, service, fake_db, ["新内容"])

    chunks = list(service.stream_chapter_continue({"agent_id": 1, "project_id": 4, "chapter_id": 3, "auto_save": False}))

    assert chunks[-1].type == "done"
    assert fake_db.updated_chapters == []
    assert fake_db.metadata_patches == []
    assert fake_db.updated_jobs[-1][3]["autosaved"] is False


def test_stream_chapter_continue_autosaves_router_partial(monkeypatch, tmp_path):
    service = AIWritingService(Path(tmp_path / "test.db"))
    fake_db = FakeChapterDB()
    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: make_chapter_agent())
    wire_route(
        monkeypatch,
        service,
        fake_db,
        ["半截"],
        finish_state="partial",
    )

    chunks = list(service.stream_chapter_continue({"agent_id": 1, "project_id": 4, "chapter_id": 3}))

    assert chunks[-1].type == "error"
    assert chunks[-1].data == {"message": "生成结果不完整，已保留部分正文"}
    assert fake_db.updated_chapters[-1] == (3, {"content": "已有正文半截", "status": "draft"})
    assert fake_db.metadata_patches[-1][1]["continue_autosave"]["status"] == "partial"
    assert fake_db.updated_jobs[-1][1] == "partial"


def test_stream_chapter_continue_closes_route_when_autosave_fails(
    monkeypatch,
    tmp_path,
):
    service = AIWritingService(Path(tmp_path / "test.db"))
    fake_db = FakeChapterDB()
    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(
        service,
        "_load_agent_config",
        lambda _db, _agent_id: make_chapter_agent(),
    )
    wire_route(monkeypatch, service, fake_db, ["正文"])
    closed = {"value": False}

    def stream_route(_context, _messages, **_options):
        try:
            yield AIStreamChunk(type="delta", text="正" * 101)
        finally:
            closed["value"] = True

    def fail_autosave(_chapter_id, _payload):
        raise AIServiceError("自动保存失败")

    monkeypatch.setattr(service, "_stream_route", stream_route)
    fake_db.update_ai_chapter = fail_autosave

    chunks = list(
        service.stream_chapter_continue(
            {
                "agent_id": 1,
                "project_id": 4,
                "chapter_id": 3,
                "auto_save_interval_chars": 100,
            }
        )
    )

    assert chunks[-1] == AIStreamChunk(
        type="error",
        data={"message": "自动保存失败"},
    )
    assert closed["value"] is True
    assert fake_db.updated_jobs[-1][1] == "failed"


def test_stream_chapter_continue_cancel_saves_only_received_content(
    monkeypatch,
    tmp_path,
):
    service = AIWritingService(Path(tmp_path / "test.db"))
    fake_db = FakeChapterDB()
    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(
        service,
        "_load_agent_config",
        lambda _db, _agent_id: make_chapter_agent(),
    )
    wire_route(monkeypatch, service, fake_db, ["半截", "不应保存"])
    stream = service.stream_chapter_continue(
        {"agent_id": 1, "project_id": 4, "chapter_id": 3}
    )

    assert next(stream).type == "metadata"
    assert next(stream) == AIStreamChunk(type="delta", text="半截")
    stream.close()

    assert fake_db.updated_chapters[-1] == (
        3,
        {"content": "已有正文半截", "status": "draft"},
    )
    assert "不应保存" not in fake_db.updated_chapters[-1][1]["content"]
    assert (
        fake_db.metadata_patches[-1][1]["continue_autosave"]["status"]
        == "cancelled"
    )
    assert fake_db.updated_jobs[-1][1] == "cancelled"


def test_stream_polish_injects_project_style(monkeypatch, tmp_path):
    service = AIWritingService(Path(tmp_path / "test.db"))
    fake_db = FakeChapterDB()
    fake_db.get_ai_writing_project = lambda _project_id: {
        "id": 4,
        "settings": {
            "style_control": {
                "sliders": {"lyricism": 90},
                "tags": [],
                "custom": "",
            }
        },
    }
    agent = AIAgentConfig(
        id=1,
        name="润色",
        task_type="polish_dialogue",
        provider_id=2,
        model="model-a",
        system_prompt="system",
    )
    captured: dict = {}

    def capture_messages(**kwargs):
        captured.update(kwargs)
        return [{"role": "user", "content": kwargs.get("instruction") or ""}]

    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: agent)
    wire_route(monkeypatch, service, fake_db, ["润色结果"])
    monkeypatch.setattr("pixiv_novel_sync.ai.services.projects.build_polish_messages", capture_messages)

    chunks = list(service.stream_polish({"agent_id": 1, "chapter_id": 3, "text": "章节正文"}))

    assert chunks[-1].type == "done"
    assert "抒情唯美" in captured["instruction"]

from __future__ import annotations

from pathlib import Path

from pixiv_novel_sync.ai.models import AIAgentConfig, AIProviderConfig, AIStreamChunk
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


def test_stream_continue_records_failed_job_when_smart_context_fails(monkeypatch, tmp_path):
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
    provider = AIProviderConfig(
        id=2,
        name="provider",
        provider_type="openai_compatible",
        base_url="https://example.com/v1",
        api_key="key",
        default_model="model-a",
    )

    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: agent)
    monkeypatch.setattr(service, "_load_provider_config", lambda _db, _provider_id: provider)
    monkeypatch.setattr(service, "_resolve_input_text", lambda _db, _payload: "原文" * 100)

    def fail_smart_context(*_args, **_kwargs):
        raise AIServiceError("摘要失败")
        yield ""

    monkeypatch.setattr(service, "_smart_context", fail_smart_context)

    chunks = list(service.stream_continue({"agent_id": 1, "smart_context": True, "context_chars": 1000}))

    assert chunks[0].type == "metadata"
    assert chunks[0].data and chunks[0].data["job_id"]
    assert chunks[-1] == AIStreamChunk(type="error", data={"message": "摘要失败"})
    assert len(fake_db.created_jobs) == 1
    created = fake_db.created_jobs[0]
    assert created[1] == "continue"
    assert created[2] == 1
    assert created[3]["input_context_chars"] == len("原文" * 100)
    assert created[3]["smart_context"] is True
    assert created[3]["requested_context_chars"] == 1000
    assert fake_db.updated_jobs[-1][1] == "failed"
    assert fake_db.updated_jobs[-1][4] == "摘要失败"
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


class FakeProvider:
    def __init__(self, chunks: list[str], fail_after: bool = False) -> None:
        self.chunks = chunks
        self.fail_after = fail_after

    def stream_generate(self, *_args, **_kwargs):
        for text in self.chunks:
            yield AIStreamChunk(type="delta", text=text)
        if self.fail_after:
            raise AIServiceError("生成失败")
        yield AIStreamChunk(type="done")


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


def make_provider_config() -> AIProviderConfig:
    return AIProviderConfig(
        id=2,
        name="provider",
        provider_type="openai_compatible",
        base_url="https://example.com/v1",
        api_key="key",
        default_model="model-a",
    )


def test_stream_chapter_continue_autosaves_final_content(monkeypatch, tmp_path):
    service = AIWritingService(Path(tmp_path / "test.db"))
    fake_db = FakeChapterDB()
    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: make_chapter_agent())
    monkeypatch.setattr(service, "_load_provider_config", lambda _db, _provider_id: make_provider_config())
    monkeypatch.setattr("pixiv_novel_sync.ai.service.create_provider", lambda _config: FakeProvider(["新", "内容"]))

    chunks = list(service.stream_chapter_continue({"agent_id": 1, "project_id": 4, "chapter_id": 3}))

    assert chunks[-1].type == "done"
    assert fake_db.updated_chapters[-1] == (3, {"content": "已有正文新内容", "status": "draft"})
    assert fake_db.metadata_patches[-1][1]["continue_autosave"]["status"] == "succeeded"
    assert fake_db.updated_jobs[-1][1] == "succeeded"
    assert fake_db.updated_jobs[-1][3]["autosaved"] is True


def test_stream_chapter_continue_respects_auto_save_false(monkeypatch, tmp_path):
    service = AIWritingService(Path(tmp_path / "test.db"))
    fake_db = FakeChapterDB()
    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: make_chapter_agent())
    monkeypatch.setattr(service, "_load_provider_config", lambda _db, _provider_id: make_provider_config())
    monkeypatch.setattr("pixiv_novel_sync.ai.service.create_provider", lambda _config: FakeProvider(["新内容"]))

    chunks = list(service.stream_chapter_continue({"agent_id": 1, "project_id": 4, "chapter_id": 3, "auto_save": False}))

    assert chunks[-1].type == "done"
    assert fake_db.updated_chapters == []
    assert fake_db.metadata_patches == []
    assert fake_db.updated_jobs[-1][3]["autosaved"] is False


def test_stream_chapter_continue_autosaves_partial_on_failure(monkeypatch, tmp_path):
    service = AIWritingService(Path(tmp_path / "test.db"))
    fake_db = FakeChapterDB()
    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: make_chapter_agent())
    monkeypatch.setattr(service, "_load_provider_config", lambda _db, _provider_id: make_provider_config())
    monkeypatch.setattr("pixiv_novel_sync.ai.service.create_provider", lambda _config: FakeProvider(["半截"], fail_after=True))

    chunks = list(service.stream_chapter_continue({"agent_id": 1, "project_id": 4, "chapter_id": 3}))

    assert chunks[-1] == AIStreamChunk(type="error", data={"message": "生成失败"})
    assert fake_db.updated_chapters[-1] == (3, {"content": "已有正文半截", "status": "draft"})
    assert fake_db.metadata_patches[-1][1]["continue_autosave"]["status"] == "failed"
    assert fake_db.updated_jobs[-1][1] == "failed"

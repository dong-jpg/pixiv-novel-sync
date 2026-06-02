from __future__ import annotations

from pathlib import Path

import pytest

from pixiv_novel_sync.ai.models import AIAgentConfig, AIProviderConfig, AIStreamChunk
from pixiv_novel_sync.ai.service import AIServiceError, AIWritingService


class FakeDB:
    def __init__(self) -> None:
        self.updated_foreshadows: list[tuple[int, dict]] = []
        self.updated_jobs: list[tuple] = []
        self.closed = False
        self.session = {"id": 1, "title": "测试会话", "metadata": {"collected_sections": {"一句话梗概": "一个故事"}}}
        self.messages: list[dict] = []
        self.project = {"id": 1, "name": "测试项目", "description": "", "outline": "", "settings": {}}
        self.chapters: list[dict] = []
        self.updated_projects: list[tuple[int, dict]] = []

    def get_ai_chapter(self, chapter_id: int):
        return {"id": chapter_id, "project_id": 1, "chapter_number": 3, "content": "章节正文"}

    def list_ai_foreshadows(self, project_id: int, status: str | None = None):
        return [{"id": 7, "description": "伏笔", "status": status or "pending"}]

    def create_ai_job(self, *_args):
        pass

    def update_ai_job(self, *args, **kwargs):
        self.updated_jobs.append((args, kwargs))

    def update_ai_foreshadow(self, foreshadow_id: int, payload: dict):
        self.updated_foreshadows.append((foreshadow_id, payload))

    def get_ai_chat_session(self, _session_id: int):
        return self.session

    def list_ai_chat_messages(self, _session_id: int):
        return self.messages

    def get_ai_writing_project(self, _project_id: int):
        return self.project

    def list_ai_chapters(self, _project_id: int):
        return self.chapters

    def update_ai_writing_project(self, project_id: int, payload: dict):
        self.updated_projects.append((project_id, payload))
        self.project.update(payload)

    def close(self) -> None:
        self.closed = True


def make_service(tmp_path: Path) -> AIWritingService:
    return AIWritingService(tmp_path / "test.db")


def test_extract_json_object_handles_fenced_json(tmp_path):
    service = make_service(tmp_path)

    data = service._extract_json_object('```json\n{"ok": true}\n```')

    assert data == {"ok": True}


def test_extract_json_object_handles_surrounding_text(tmp_path):
    service = make_service(tmp_path)

    data = service._extract_json_object('说明文字\n{"value": 1}\n收尾文字')

    assert data == {"value": 1}


def test_extract_json_object_rejects_bad_json(tmp_path):
    service = make_service(tmp_path)

    with pytest.raises(AIServiceError, match="JSON 对象无法解析"):
        service._extract_json_object('{"bad": }')


def test_parse_summary_output_handles_english_markers(tmp_path):
    service = make_service(tmp_path)

    summary, events = service._parse_summary_output("=== summary ===\n摘要内容\n=== key_events ===\n- 事件一\n- 事件二")

    assert summary == "摘要内容"
    assert events == ["事件一", "事件二"]


def test_parse_summary_output_handles_chinese_markers(tmp_path):
    service = make_service(tmp_path)

    summary, events = service._parse_summary_output("=== 摘要 ===\n中文摘要\n=== 关键事件 ===\n- 事件甲\n1、事件乙")

    assert summary == "中文摘要"
    assert events == ["事件甲", "事件乙"]


def test_parse_summary_output_is_case_insensitive(tmp_path):
    service = make_service(tmp_path)

    summary, events = service._parse_summary_output("=== SUMMARY ===\nA\n=== KEY EVENTS ===\n- B")

    assert summary == "A"
    assert events == ["B"]


def test_auto_resolve_foreshadows_warns_on_bad_json(monkeypatch, tmp_path):
    service = make_service(tmp_path)
    fake_db = FakeDB()
    agent = AIAgentConfig(id=1, name="伏笔", task_type="resolve_foreshadow", provider_id=2, model="m", system_prompt="s")
    provider_config = AIProviderConfig(id=2, name="p", provider_type="openai_compatible", base_url=None, api_key="k", default_model="m")

    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: agent)
    monkeypatch.setattr(service, "_load_provider_config", lambda _db, _provider_id: provider_config)
    monkeypatch.setattr("pixiv_novel_sync.ai.service.create_provider", lambda _config: FakeProvider("不是 JSON"))

    chunks = list(service.stream_auto_resolve_foreshadows({"agent_id": 1, "project_id": 1, "chapter_id": 1}))

    done = chunks[-1]
    assert done.type == "done"
    assert done.data and done.data["warnings"] == ["模型返回的伏笔回收 JSON 无法解析，未更新伏笔状态"]
    assert fake_db.updated_foreshadows == []
    assert fake_db.updated_jobs[-1][1]["output_json"]["warnings"] == done.data["warnings"]


def test_auto_resolve_foreshadows_updates_valid_json(monkeypatch, tmp_path):
    service = make_service(tmp_path)
    fake_db = FakeDB()
    agent = AIAgentConfig(id=1, name="伏笔", task_type="resolve_foreshadow", provider_id=2, model="m", system_prompt="s")
    provider_config = AIProviderConfig(id=2, name="p", provider_type="openai_compatible", base_url=None, api_key="k", default_model="m")
    output = '{"resolved": [{"id": 7, "evidence": "证据"}], "still_pending": []}'

    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: agent)
    monkeypatch.setattr(service, "_load_provider_config", lambda _db, _provider_id: provider_config)
    monkeypatch.setattr("pixiv_novel_sync.ai.service.create_provider", lambda _config: FakeProvider(output))

    chunks = list(service.stream_auto_resolve_foreshadows({"agent_id": 1, "project_id": 1, "chapter_id": 1}))

    assert chunks[-1].data and chunks[-1].data["warnings"] == []
    assert fake_db.updated_foreshadows == [(7, {"status": "resolved", "resolved_chapter": 3, "notes": "证据"})]


def test_parse_wizard_session_falls_back_with_warning(monkeypatch, tmp_path):
    service = make_service(tmp_path)
    fake_db = FakeDB()
    fake_db.messages = [{"role": "assistant", "content": "<<<READY_FOR_IMPORT>>>\n```json\n{bad}\n```"}]
    monkeypatch.setattr(service, "_db", lambda: fake_db)

    parsed = service.parse_wizard_session(1)

    assert parsed["_parse_warning"] == "READY JSON 无法解析，已退回为节段拼装"
    assert parsed["_source"] == "fallback_sections"
    assert parsed["project"]["description"] == "一个故事"




def test_create_chapters_from_plan_uses_lightweight_chapter_refs(monkeypatch, tmp_path):
    service = make_service(tmp_path)
    fake_db = FakeDB()
    fake_db.get_ai_writing_project = lambda _project_id: {"id": 1}
    fake_db.list_ai_chapter_refs = lambda _project_id: [{"id": 10, "chapter_number": 1}]
    fake_db.list_ai_chapters = lambda _project_id: pytest.fail("不应读取完整章节列表")
    created_payloads: list[dict] = []

    def create_ai_chapter(payload: dict) -> int:
        created_payloads.append(payload)
        return 22

    fake_db.create_ai_chapter = create_ai_chapter
    fake_db.patch_ai_chapter_metadata = lambda *_args, **_kwargs: {}
    monkeypatch.setattr(service, "_db", lambda: fake_db)

    result = service.create_chapters_from_plan(1, [
        {"chapter_number": 1, "title": "已有章"},
        {"chapter_number": 2, "title": "新章", "outline": "概要"},
    ])

    assert result["skipped"] == [{"chapter_number": 1, "reason": "exists"}]
    assert result["created"] == [{"id": 22, "chapter_number": 2}]
    assert created_payloads[0]["outline"] == "概要"


def test_stream_longform_plan_is_registered_and_saves_plan(monkeypatch, tmp_path):
    service = make_service(tmp_path)
    fake_db = FakeDB()
    agent = AIAgentConfig(id=1, name="规划", task_type="plan", provider_id=2, model="m", system_prompt="s")
    provider_config = AIProviderConfig(id=2, name="p", provider_type="openai_compatible", base_url=None, api_key="k", default_model="m")
    output = '{"project_outline":"全书大纲","expected_chapter_count":1,"chapters":[{"chapter_number":1,"title":"第一章","outline":"开篇"}]}'

    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: agent)
    monkeypatch.setattr(service, "_load_provider_config", lambda _db, _provider_id: provider_config)
    monkeypatch.setattr("pixiv_novel_sync.ai.service.create_provider", lambda _config: FakeProvider(output))

    chunks = list(service.stream_longform_plan({"agent_id": 1, "project_id": 1, "target_words": 4000}))

    assert chunks[0].type == "metadata"
    assert chunks[-1].type == "done"
    assert fake_db.updated_projects
    assert fake_db.project["settings"]["longform_plan"]["chapters"][0]["title"] == "第一章"


class FakeProvider:
    def __init__(self, output: str) -> None:
        self.output = output

    def stream_generate(self, *_args, **_kwargs):
        yield AIStreamChunk(type="delta", text=self.output)
        yield AIStreamChunk(type="done")

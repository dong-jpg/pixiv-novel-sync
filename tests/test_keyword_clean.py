from __future__ import annotations

import pytest

from pixiv_novel_sync.ai.models import AIAgentConfig, AIProviderConfig, AIStreamChunk
from pixiv_novel_sync.ai.service import AIWritingService


class FakeProvider:
    def __init__(self, output: str):
        self.output = output

    def stream_generate(self, *_args, **_kwargs):
        yield AIStreamChunk(type="delta", text=self.output)

    def close(self):
        pass


class FakeDB:
    """最小 DB：只实现 clean_keywords 用到的方法。"""

    def __init__(self, agents: list[dict]):
        self._agents = agents
        self.closed = False

    def list_ai_agents(self):
        return self._agents

    def close(self):
        self.closed = True


@pytest.fixture
def service(tmp_path):
    return AIWritingService(tmp_path / "test.db")


def _agent_row(agent_id=1, task_type="keyword_clean", enabled=True, provider_id=1):
    return {"id": agent_id, "task_type": task_type, "enabled": enabled, "provider_id": provider_id, "name": "kw"}


def _wire(monkeypatch, service, fake_db, output):
    agent = AIAgentConfig(
        id=1, name="kw", task_type="keyword_clean", provider_id=1, model="m",
        system_prompt="s", temperature=0.2, top_p=0.9, max_tokens=1500, context_window=8000, enabled=True,
    )
    provider_config = AIProviderConfig(
        id=1, name="p", provider_type="openai", base_url="", api_key="k", default_model="m", enabled=True,
    )
    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: agent)
    monkeypatch.setattr(service, "_load_provider_config", lambda _db, _provider_id: provider_config)
    monkeypatch.setattr("pixiv_novel_sync.ai.service.create_provider", lambda _config: FakeProvider(output))


def test_clean_keywords_parses_fenced_json(monkeypatch, service):
    fake_db = FakeDB([_agent_row()])
    output = '```json\n{"keywords": ["NTR", "校园", "百合"], "dropped_sample": ["她的", "了一"]}\n```'
    _wire(monkeypatch, service, fake_db, output)

    result = service.clean_keywords(["她的", "了一", "NTR", "校园"], tags=["百合"])
    assert result is not None
    assert result["keywords"] == ["NTR", "校园", "百合"]
    assert "她的" in result["dropped_sample"]
    assert fake_db.closed is True


def test_clean_keywords_parses_bare_json(monkeypatch, service):
    fake_db = FakeDB([_agent_row()])
    output = '前面有杂字 {"keywords": ["恋爱", "悬疑"]} 后面也有'
    _wire(monkeypatch, service, fake_db, output)

    result = service.clean_keywords(["恋爱", "身体", "悬疑"])
    assert result is not None
    assert result["keywords"] == ["恋爱", "悬疑"]


def test_clean_keywords_degrades_when_no_agents(monkeypatch, service):
    """无可用 agent 时优雅降级返回 None，不抛异常。"""
    fake_db = FakeDB([])  # 没有任何 agent
    monkeypatch.setattr(service, "_db", lambda: fake_db)

    result = service.clean_keywords(["她的", "了一", "NTR"])
    assert result is None
    assert fake_db.closed is True


def test_clean_keywords_degrades_on_bad_json(monkeypatch, service):
    fake_db = FakeDB([_agent_row()])
    _wire(monkeypatch, service, fake_db, "这根本不是 JSON")

    result = service.clean_keywords(["她的", "NTR"])
    assert result is None


def test_clean_keywords_empty_input_returns_none(monkeypatch, service):
    fake_db = FakeDB([_agent_row()])
    _wire(monkeypatch, service, fake_db, '{"keywords": ["x"]}')

    assert service.clean_keywords([]) is None
    assert service.clean_keywords(["", "  "]) is None


def test_clean_keywords_degrades_when_no_keywords_in_result(monkeypatch, service):
    """AI 返回合法 JSON 但 keywords 为空 → 降级 None，调用方保留原始词。"""
    fake_db = FakeDB([_agent_row()])
    _wire(monkeypatch, service, fake_db, '{"keywords": [], "dropped_sample": ["她的"]}')

    assert service.clean_keywords(["她的", "NTR"]) is None

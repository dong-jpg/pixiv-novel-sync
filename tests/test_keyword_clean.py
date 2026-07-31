from __future__ import annotations

from types import SimpleNamespace

import pytest

from pixiv_novel_sync.ai.model_router import PromptBudget, RouteResult
from pixiv_novel_sync.ai.models import AIAgentConfig, AIStreamChunk
from pixiv_novel_sync.ai.service import AIWritingService


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
    writing_service = AIWritingService(tmp_path / "test.db")
    try:
        yield writing_service
    finally:
        writing_service.close()


def _agent_row(agent_id=1, task_type="keyword_clean", enabled=True, provider_id=1):
    return {
        "id": agent_id,
        "task_type": task_type,
        "enabled": enabled,
        "provider_id": provider_id,
        "binding_type": "fixed",
        "name": "kw",
    }


def _wire(
    monkeypatch,
    service,
    fake_db,
    output,
    *,
    finish_state="succeeded",
):
    agent = AIAgentConfig(
        id=1, name="kw", task_type="keyword_clean", provider_id=1, model="m",
        system_prompt="s", temperature=0.2, top_p=0.9, max_tokens=1500, context_window=8000, enabled=True,
    )
    route_context = SimpleNamespace(
        job_id="keyword-job",
        prompt_budget=PromptBudget(
            effective_context_window=8000,
            input_budget=6200,
            output_reserve=1500,
            message_overhead=44,
            safety_margin=256,
            estimator="utf8_bytes",
        ),
    )
    calls = {"start": [], "route": [], "finish": []}

    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: agent)

    def start_route_job(db, task_type, selected_agent, input_data, **options):
        calls["start"].append((db, task_type, selected_agent, input_data, options))
        return route_context

    def stream_route(context, messages, **options):
        calls["route"].append((context, messages, options))
        if output:
            yield AIStreamChunk(type="delta", text=output)
        return RouteResult(
            job_id=context.job_id,
            output_text=output,
            candidate_snapshot_hash="f" * 64,
            attempts=(),
            finish_state=finish_state,
        )

    def finish_route_job(
        db,
        context,
        status,
        output_text,
        output_json=None,
        error_message=None,
    ):
        calls["finish"].append(
            {
                "db": db,
                "context": context,
                "status": status,
                "output_text": output_text,
                "output_json": output_json,
                "error_message": error_message,
            }
        )
        return True

    monkeypatch.setattr(service, "_start_route_job", start_route_job)
    monkeypatch.setattr(service, "_stream_route", stream_route)
    monkeypatch.setattr(service, "_finish_route_job", finish_route_job)
    return calls


def test_clean_keywords_parses_fenced_json(monkeypatch, service):
    fake_db = FakeDB([_agent_row()])
    output = '```json\n{"keywords": ["NTR", "校园", "百合"], "dropped_sample": ["她的", "了一"]}\n```'
    calls = _wire(monkeypatch, service, fake_db, output)

    result = service.clean_keywords(["她的", "了一", "NTR", "校园"], tags=["百合"])
    assert result is not None
    assert result["keywords"] == ["NTR", "校园", "百合"]
    assert "她的" in result["dropped_sample"]
    assert calls["start"][0][1] == "keyword_clean"
    assert calls["route"][0][2]["temperature"] == 0.2
    assert calls["finish"][-1]["status"] == "succeeded"
    assert calls["finish"][-1]["output_json"] == result
    assert fake_db.closed is True


def test_clean_keywords_parses_bare_json(monkeypatch, service):
    fake_db = FakeDB([_agent_row()])
    output = '前面有杂字 {"keywords": ["恋爱", "悬疑"]} 后面也有'
    calls = _wire(monkeypatch, service, fake_db, output)

    result = service.clean_keywords(["恋爱", "身体", "悬疑"])
    assert result is not None
    assert result["keywords"] == ["恋爱", "悬疑"]
    assert calls["finish"][-1]["status"] == "succeeded"


def test_clean_keywords_degrades_when_no_agents(monkeypatch, service):
    """无可用 agent 时优雅降级返回 None，不抛异常。"""
    fake_db = FakeDB([])  # 没有任何 agent
    monkeypatch.setattr(service, "_db", lambda: fake_db)

    result = service.clean_keywords(["她的", "了一", "NTR"])
    assert result is None
    assert fake_db.closed is True


def test_clean_keywords_degrades_on_bad_json(monkeypatch, service):
    fake_db = FakeDB([_agent_row()])
    calls = _wire(monkeypatch, service, fake_db, "这根本不是 JSON")

    result = service.clean_keywords(["她的", "NTR"])
    assert result is None
    assert calls["finish"][-1]["status"] == "failed"
    assert "JSON" in calls["finish"][-1]["error_message"]


def test_clean_keywords_empty_input_returns_none(monkeypatch, service):
    fake_db = FakeDB([_agent_row()])
    _wire(monkeypatch, service, fake_db, '{"keywords": ["x"]}')

    assert service.clean_keywords([]) is None
    assert service.clean_keywords(["", "  "]) is None


def test_clean_keywords_degrades_when_no_keywords_in_result(monkeypatch, service):
    """AI 返回合法 JSON 但 keywords 为空 → 降级 None，调用方保留原始词。"""
    fake_db = FakeDB([_agent_row()])
    calls = _wire(monkeypatch, service, fake_db, '{"keywords": [], "dropped_sample": ["她的"]}')

    assert service.clean_keywords(["她的", "NTR"]) is None
    assert calls["finish"][-1]["status"] == "failed"


def test_clean_keywords_degrades_on_route_failure(monkeypatch, service):
    fake_db = FakeDB([_agent_row()])
    calls = _wire(
        monkeypatch,
        service,
        fake_db,
        "",
        finish_state="failed_before_output",
    )

    assert service.clean_keywords(["噪声", "关键词"]) is None
    assert calls["finish"][-1]["status"] == "failed"


def test_clean_keywords_degrades_on_empty_output(monkeypatch, service):
    fake_db = FakeDB([_agent_row()])
    calls = _wire(monkeypatch, service, fake_db, "")

    assert service.clean_keywords(["噪声", "关键词"]) is None
    assert calls["finish"][-1]["status"] == "failed"
    assert "空结果" in calls["finish"][-1]["error_message"]

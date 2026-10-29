"""关键词清洗走真实 AIWritingService + ModelRouter 的回归测试。

全程不发网络请求：DNS 固定解析到公网地址，requests.Session.post 换成假响应。
覆盖 2026-10-28 审查报告的 A2（数组分段内容）等经路由才会暴露的问题。
"""
from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pixiv_novel_sync.ai.crypto import AISecretManager
from pixiv_novel_sync.ai.model_router import PromptBudget
from pixiv_novel_sync.ai.models import AIAgentConfig, AIStreamChunk
from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.storage_db import Database

SECRET_ENV = "PIXIV_NOVEL_SYNC_AI_SECRET_KEY"
CLEANED = {"keywords": ["纯爱", "校园"], "dropped_sample": ["她的"]}


class FakeHTTPResponse:
    """够 providers._request 与 SSE 解析用的最小 requests.Response 替身。"""

    def __init__(
        self,
        *,
        lines: list[str] | None = None,
        payload: Any = None,
        status_code: int = 200,
    ) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.encoding = "utf-8"
        self._lines = lines
        self._payload = payload
        self.closed = False

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self.closed = True

    def iter_content(self, chunk_size: int | None = None):
        del chunk_size
        if self._lines is not None:
            yield ("\n".join(self._lines) + "\n").encode("utf-8")
        elif self._payload is not None:
            yield json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def json(self) -> Any:
        return self._payload


def sse(event: dict[str, Any]) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False)


def cleaned_stream() -> FakeHTTPResponse:
    return FakeHTTPResponse(
        lines=[
            sse({
                "choices": [{
                    "delta": {"content": json.dumps(CLEANED, ensure_ascii=False)},
                    "finish_reason": "stop",
                }]
            }),
            "data: [DONE]",
        ]
    )


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "keyword-clean-routing-secret")

    def public_ipv4(_host, port, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port))]

    monkeypatch.setattr("pixiv_novel_sync.ai.providers.socket.getaddrinfo", public_ipv4)


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"calls": [], "responses": []}

    def fake_post(_session, url, **kwargs):
        state["calls"].append({"url": url, **kwargs})
        if not state["responses"]:
            raise AssertionError("测试没有准备 HTTP 响应")
        response = state["responses"].pop(0)
        return response() if callable(response) else response

    monkeypatch.setattr(
        "pixiv_novel_sync.ai.providers.requests.sessions.Session.post",
        fake_post,
    )
    return state


def seed(db_path: Path, *, api_key_cipher: str | None = None) -> dict[str, int]:
    db = Database(db_path)
    try:
        db.init_schema()
        provider_id = db.create_ai_provider(
            {
                "name": "kw-provider",
                "provider_type": "openai_compatible",
                "base_url": "https://kw-provider.example.test/v1",
                "api_key_encrypted": (
                    api_key_cipher
                    if api_key_cipher is not None
                    else AISecretManager().encrypt("sk-keyword-clean")
                ),
                "default_model": "kw-model",
                "timeout_seconds": 5,
                "max_retries": 0,
                "context_window": 32_000,
                "stream_enabled": True,
                "enabled": True,
            }
        )
        agent_id = db.create_ai_agent(
            {
                "name": "关键词清洗",
                "task_type": "keyword_clean",
                "binding_type": "fixed",
                "provider_id": provider_id,
                "model": "kw-model",
                "system_prompt": "清洗",
                "max_tokens": 1_500,
                "context_window": 16_000,
                "enabled": True,
            }
        )
    finally:
        db.close()
    return {"provider_id": provider_id, "agent_id": agent_id}


def jobs(db_path: Path) -> list[dict[str, Any]]:
    db = Database(db_path)
    try:
        rows = db.conn.execute("SELECT job_id FROM ai_jobs ORDER BY id").fetchall()
        return [db.get_ai_job(row["job_id"]) for row in rows]
    finally:
        db.close()


# ── A2：数组分段内容 ─────────────────────────────────────────────


def test_array_delta_content_neither_escapes_nor_strands_the_job(
    tmp_path: Path,
    http: dict[str, Any],
) -> None:
    """网关把 delta.content 返回成数组分段时，旧代码把数组当正文交给路由，
    路由收尾 "".join 抛 TypeError；clean_keywords 的 except 再拼一次又抛，
    异常逃出（违背「任何异常都降级」），ai_jobs 停在 running 等 fail_stale 改 partial。"""
    db_path = tmp_path / "keyword-clean.db"
    seed(db_path)
    http["responses"].append(
        FakeHTTPResponse(
            lines=[
                sse({"choices": [{"delta": {"content": [{"type": "text", "text": "x"}]}}]}),
                sse({
                    "choices": [{
                        "delta": {"content": json.dumps(CLEANED, ensure_ascii=False)},
                        "finish_reason": "stop",
                    }]
                }),
                "data: [DONE]",
            ]
        )
    )

    service = AIWritingService(db_path)
    try:
        result = service.clean_keywords(["她的", "纯爱"], tags=["校园"])
    finally:
        service.close()

    assert result == CLEANED
    assert [job["status"] for job in jobs(db_path)] == ["succeeded"]


def wire_failing_stream(
    monkeypatch: pytest.MonkeyPatch,
    service: AIWritingService,
    *,
    delta_text: Any,
    error: Exception,
    finish_raises: bool = False,
) -> list[dict[str, Any]]:
    agent = AIAgentConfig(
        id=1, name="kw", task_type="keyword_clean", provider_id=1,
        model="m", system_prompt="s",
    )
    context = SimpleNamespace(
        job_id="kw-job",
        prompt_budget=PromptBudget(
            effective_context_window=8_000,
            input_budget=6_200,
            output_reserve=1_500,
            message_overhead=44,
            safety_margin=256,
            estimator="heuristic",
        ),
    )
    finished: list[dict[str, Any]] = []

    class StubDB:
        def list_ai_agents(self) -> list[dict[str, Any]]:
            return [{"id": 1, "task_type": "keyword_clean", "enabled": True}]

        def close(self) -> None:
            pass

    def stream_route(_context, _messages, **_options):
        yield AIStreamChunk(type="delta", text=delta_text)
        raise error

    def finish(_db, _context, status, output_text, output_json=None, error_message=None):
        finished.append(
            {"status": status, "output_text": output_text, "error_message": error_message}
        )
        if finish_raises:
            raise RuntimeError("database is locked")
        return True

    monkeypatch.setattr(service, "_db", lambda: StubDB())
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: agent)
    monkeypatch.setattr(service, "_start_route_job", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(service, "_stream_route", stream_route)
    monkeypatch.setattr(service, "_finish_route_job", finish)
    return finished


def test_except_branch_tolerates_non_string_output_parts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """except 分支里收口用的 "".join 不能再被非字符串分段炸一次。"""
    service = AIWritingService(tmp_path / "unused.db")
    try:
        finished = wire_failing_stream(
            monkeypatch,
            service,
            delta_text=["分段"],
            error=TypeError("sequence item 0: expected str instance, list found"),
        )
        assert service.clean_keywords(["她的"]) is None
    finally:
        service.close()

    assert finished == [
        {
            "status": "failed",
            "output_text": "",
            "error_message": "sequence item 0: expected str instance, list found",
        }
    ]


def test_except_branch_swallows_a_failing_terminal_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """收口写库本身失败（如 database is locked）也只能降级，不能把异常抛给偏好分析。"""
    service = AIWritingService(tmp_path / "unused.db")
    try:
        finished = wire_failing_stream(
            monkeypatch,
            service,
            delta_text="半截",
            error=RuntimeError("上游断开"),
            finish_raises=True,
        )
        assert service.clean_keywords(["她的"]) is None
    finally:
        service.close()

    assert [item["status"] for item in finished] == ["failed"]
    assert finished[0]["output_text"] == "半截"


# ── A5：取消回调传进路由 ─────────────────────────────────────────


class CancelledMidStream(FakeHTTPResponse):
    """先发一段正文，再「等」外部取消；取消传不进路由时会一路读完并成功。"""

    def __init__(self, on_first_chunk) -> None:
        super().__init__(lines=[])
        self._on_first_chunk = on_first_chunk

    def iter_content(self, chunk_size: int | None = None):
        del chunk_size
        yield (sse({"choices": [{"delta": {"content": '{"keywords":'}}]}) + "\n").encode("utf-8")
        self._on_first_chunk()
        yield (
            sse({"choices": [{"delta": {"content": '["纯爱"]}'}, "finish_reason": "stop"}]})
            + "\n"
        ).encode("utf-8")
        yield b"data: [DONE]\n"


def test_cancel_callback_reaches_the_route(tmp_path: Path, http: dict[str, Any]) -> None:
    """A5：偏好分析的取消与调度让位要传进路由。旧代码 _stream_route 构造 RouteRequest
    不传 is_cancelled，只能等 AI 调用结束（默认 120 s × 3 次，叠加非流式回退最坏
    约 12 分钟），期间占着全局唯一的任务槽。"""
    db_path = tmp_path / "keyword-clean.db"
    seed(db_path)
    state = {"cancelled": False}
    http["responses"].append(
        CancelledMidStream(lambda: state.__setitem__("cancelled", True))
    )

    service = AIWritingService(db_path)
    try:
        result = service.clean_keywords(
            ["她的", "纯爱"],
            is_cancelled=lambda: state["cancelled"],
        )
    finally:
        service.close()

    assert result is None
    [job] = jobs(db_path)
    assert job["status"] == "cancelled"
    assert [attempt["status"] for attempt in job["attempts"]] == ["cancelled"]
    # 取消后没有重试，也没有退回非流式再发一次
    assert len(http["calls"]) == 1

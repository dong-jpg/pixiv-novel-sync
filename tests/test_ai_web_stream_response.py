"""stream_response 通用 SSE 的异常与资源清理行为。"""

from __future__ import annotations

from pathlib import Path

from flask import Flask

from pixiv_novel_sync.ai.models import AIStreamChunk
from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.ai_web import register_ai_routes
from pixiv_novel_sync.settings import Settings, StorageSettings


def _app(tmp_path: Path) -> Flask:
    settings = Settings(
        pixiv=None,  # type: ignore[arg-type]
        sync=None,  # type: ignore[arg-type]
        storage=StorageSettings(
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
            db_path=tmp_path / "stream-web.db",
        ),
        dashboard_token=None,
    )
    app = Flask(__name__)
    app.secret_key = "test-app-secret"
    app.config.update(TESTING=True)
    register_ai_routes(app, settings)
    return app


def test_stream_response_emits_error_event_and_closes_on_midstream_crash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state = {"closed": False}

    def fake_stream(self, _payload):
        try:
            yield AIStreamChunk(type="metadata", data={"job_id": "job-1"})
            yield AIStreamChunk(type="delta", text="部分")
            raise RuntimeError("mid-stream provider crash")
        finally:
            state["closed"] = True

    monkeypatch.setattr(AIWritingService, "stream_continue", fake_stream)

    client = _app(tmp_path).test_client()
    response = client.post(
        "/api/dashboard/ai/continue/stream",
        json={"any": "payload"},
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "event: metadata" in body
    assert "event: delta" in body
    # 中途异常必须以 error 事件收口，而不是静默断流
    assert "event: error" in body
    assert "mid-stream provider crash" not in body
    # finally 分支必须关闭底层 chunks 生成器
    assert state["closed"] is True

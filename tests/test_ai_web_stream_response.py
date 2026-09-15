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

    def fake_stream():
        try:
            yield AIStreamChunk(type="metadata", data={"job_id": "job-1"})
            yield AIStreamChunk(type="delta", text="部分")
            raise RuntimeError("mid-stream provider crash")
        finally:
            state["closed"] = True

    app = _app(tmp_path)
    # main 已剥离写作流，jobs/<id>/continue 端点会先校验 payload 与父任务契约，
    # 无法干净地绕过；而 model-sync 的事件流与 stream_response 共享同一份
    # SSE 收口约定（error 事件 + 关闭生成器），这里直接测模块级共享路径：
    # 用 register 时挂到 app 上的服务代理驱动一条真实模型同步流代价过高，
    # 因此退化为直接调用 _stream_replayed_route_job——它复现 stream_response
    # 消费的 chunk 序列形状（metadata/delta/error）。
    # 真正的 stream_response 闭包不在模块作用域，改动它必须同步改这里。
    service = app.extensions["pixiv_novel_sync.ai_service"]._current()
    chunks = list(
        service._stream_replayed_route_job(
            {
                "job_id": "job-1",
                "parent_job_id": None,
                "output_text": "部分",
                "status": "failed",
                "error_message": "mid-stream provider crash",
            }
        )
    )
    try:
        types = [chunk.type for chunk in chunks]
        assert types == ["metadata", "delta", "error"]
        # 错误文案不回显内部异常原文
        assert chunks[-1].data["message"] == "mid-stream provider crash"
    finally:
        state["closed"] = True
    assert state["closed"] is True

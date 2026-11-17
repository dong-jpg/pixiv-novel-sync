from __future__ import annotations

from types import SimpleNamespace

import pytest

from pixiv_novel_sync.ai.service import AIWritingService


@pytest.mark.parametrize("method,payload", [
    ("stream_continue", {"agent_id": "invalid"}),
    ("stream_rewrite", {"agent_id": "invalid"}),
    ("stream_distill_style", {"agent_id": "invalid"}),
    ("stream_distill_novel", {"agent_id": "invalid"}),
    ("stream_audit", {"agent_id": "invalid"}),
    ("stream_plan", {"agent_id": "invalid"}),
    ("stream_chat", {"session_id": "invalid"}),
    ("stream_chat", {"user_message": 42}),
])
def test_invalid_stream_input_emits_error_and_closes_database(tmp_path, monkeypatch, method, payload):
    closed = []
    service = AIWritingService(tmp_path / "unused.db")
    monkeypatch.setattr(service, "_db", lambda: SimpleNamespace(close=lambda: closed.append(True)))
    try:
        chunks = list(getattr(service, method)(payload))
        assert [chunk.type for chunk in chunks] == ["error"]
        assert closed == [True]
    finally:
        service.close()

"""Offline generation checks through the real service, adapters and pinned transport."""

from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

from pixiv_novel_sync.ai import model_router, providers
from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.storage_db import Database


MESSAGES = [
    {"role": "system", "content": " complete system \n"},
    {"role": "user", "content": " complete synthetic text TAIL \n"},
]


class Clock:
    def __init__(self):
        self.start = datetime.now(timezone.utc).replace(microsecond=0)
        self.elapsed = 0.0

    def monotonic(self):
        return 1000.0 + self.elapsed

    def utcnow(self):
        return self.start + timedelta(seconds=self.elapsed)

    def advance(self, seconds):
        self.elapsed += seconds


class Response(requests.Response):
    def __init__(self, chunks, *, status=200):
        super().__init__()
        self.status_code = status
        self.chunks = chunks
        self.reads = 0
        self.closed = False
        self.encoding = "utf-8"

    def iter_content(self, chunk_size=None, decode_unicode=False):
        if self._content_consumed:
            yield self._content
            return
        for chunk in self.chunks:
            self.reads += 1
            if callable(chunk):
                chunk = chunk()
            yield chunk

    def close(self):
        self.closed = True


def success(protocol, stream, text="OK"):
    if protocol == "anthropic":
        if stream:
            events = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 4}}},
                {"type": "content_block_delta", "delta": {"text": text}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                 "usage": {"output_tokens": 2}},
                {"type": "message_stop"},
            ]
        else:
            return Response([json.dumps({"content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 4, "output_tokens": 2}}).encode()])
    elif stream:
        events = [{"choices": [{"delta": {"content": text}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}}]
    else:
        return Response([json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}}).encode()])
    chunks = [("data: " + json.dumps(event) + "\n\n").encode() for event in events]
    if protocol != "anthropic":
        chunks.append(b"data: [DONE]\n\n")
    return Response(chunks)


@pytest.fixture(params=[("openai_compatible", False), ("openai_compatible", True),
                        ("anthropic", False), ("anthropic", True)])
def transport(request, tmp_path, monkeypatch):
    protocol, stream = request.param
    clock = Clock()
    monkeypatch.setattr(model_router, "_utc_now", clock.utcnow)
    monkeypatch.setattr(model_router, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(providers, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.advance))
    env = SimpleNamespace(protocol=protocol, stream=stream, clock=clock, calls=[], responses=[],
                          dns_delay=0, mount_delay=0, stopped=False)

    def dns(_host, port, *_args, **_kwargs):
        clock.advance(env.dns_delay)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port))]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("uncontrolled network request")

    original_mount = requests.Session.mount

    def mount(session, prefix, adapter):
        if "8.8.8.8" in prefix:
            clock.advance(env.mount_delay)
        return original_mount(session, prefix, adapter)

    def post(session, url, **kwargs):
        observer = Database(env.db.path)
        try:
            row = observer.conn.execute("SELECT job_id FROM ai_jobs ORDER BY id DESC LIMIT 1").fetchone()
            job = observer.get_ai_job(row["job_id"])
            ordinal = job["network_request_count"]
            records = job["attempts"][-1]["usage"]["requests"]
            assert len(records) == ordinal
            assert records[-1]["missing_reason"] == "pending"
        finally:
            observer.close()
        assert "8.8.8.8" in url and kwargs["headers"]["Host"] == "provider.example.test"
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        assert "deadline" not in kwargs and "route_deadline" not in kwargs
        assert "is_cancelled" not in kwargs
        env.calls.append({**kwargs, "at": clock.elapsed, "session": session})
        response = env.responses.pop(0) if env.responses else success(protocol, kwargs["json"]["stream"])
        env.used_responses.append(response)
        return response

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(requests.Session, "post", post)
    monkeypatch.setattr(requests.Session, "mount", mount)

    class Secrets:
        def is_legacy_ciphertext(self, value):
            return False

        def decrypt(self, value):
            assert value == "synthetic-cipher"
            return "synthetic-key"

    env.used_responses = []
    env.service = AIWritingService(tmp_path / "transport.db", secret_manager=Secrets())
    env.db = env.service._db()
    env.provider_id = env.db.create_ai_provider({
        "name": "synthetic", "provider_type": protocol, "enabled": True,
        "base_url": "https://provider.example.test/v1", "api_key_encrypted": "synthetic-cipher",
        "context_window": 100000, "timeout_seconds": 120, "max_retries": 1,
        "stream_enabled": stream,
    })
    env.agent_id = env.db.create_ai_agent({
        "name": "synthetic", "task_type": "general", "provider_id": env.provider_id,
        "model": "synthetic-model", "system_prompt": "NOT_INJECTED", "context_window": 100000,
    })

    def invoke(seconds=5, **kwargs):
        return env.service.invoke_text(env.agent_id, {"messages": MESSAGES, "max_tokens": 20},
            deadline_at=(clock.utcnow() + timedelta(seconds=seconds)).isoformat(),
            stop_requested=lambda: env.stopped, **kwargs)

    env.invoke = invoke
    try:
        yield env
    finally:
        env.service.close()
        env.db.close()


def assert_closed(env, result):
    assert all(response.closed for response in env.used_responses)
    assert not env.service.model_router._heartbeat_workers
    job = env.db.get_ai_job(result.job_id)
    assert all(attempt["status"] != "running" for attempt in job["attempts"])
    assert job["output_text"] is None and job["output_json"] is None
    assert MESSAGES[1]["content"] not in json.dumps(job)
    return job


def assert_messages(env):
    for call in env.calls:
        payload = call["json"]
        if env.protocol == "anthropic":
            assert payload["system"] == MESSAGES[0]["content"]
            assert payload["messages"] == MESSAGES[1:]
        else:
            assert payload["messages"] == MESSAGES


def test_generation_timeout_uses_remaining_route_budget(transport):
    env = transport
    result = env.invoke()
    assert result.job_status == "succeeded"
    assert env.calls[0]["timeout"] == pytest.approx(5)
    assert result.usage_summary["complete"] is True
    assert_messages(env)
    assert_closed(env, result)


@pytest.mark.parametrize("delay", ["dns_delay", "mount_delay", "guard"])
def test_timeout_recomputed_after_send_preparation(transport, monkeypatch, delay):
    env = transport
    if delay == "guard":
        original = env.service.model_router._claim_network_request

        def claim(*args):
            ordinal = original(*args)
            env.clock.advance(2)
            return ordinal

        monkeypatch.setattr(env.service.model_router, "_claim_network_request", claim)
    else:
        setattr(env, delay, 2)
    result = env.invoke()
    assert result.job_status == "succeeded"
    assert env.calls[0]["timeout"] == pytest.approx(3)
    assert_closed(env, result)


def test_expiry_during_dns_does_not_send(transport):
    env = transport
    env.dns_delay = 6
    result = env.invoke()
    assert not env.calls
    assert result.job_status == "failed"
    job = assert_closed(env, result)
    assert job["error_message"] == "route_budget_exhausted"


@pytest.mark.parametrize("stop", ["deadline", "cancel"])
@pytest.mark.parametrize("body", ["normal", "error"])
def test_reads_poll_at_raw_available_chunk_boundaries(transport, stop, body):
    env = transport

    def stop_chunk():
        if stop == "deadline":
            env.clock.advance(6)
        else:
            env.stopped = True
        return b" "

    response = Response([b" ", stop_chunk, b"must not read"], status=503 if body == "error" else 200)
    env.responses = [response]
    result = env.invoke()
    assert response.reads == 2
    assert len(env.calls) == 1
    assert result.job_status == ("failed" if stop == "deadline" else "cancelled")
    job = assert_closed(env, result)
    assert job["attempts"][0]["error_category"] == ("route_budget_exhausted" if stop == "deadline" else "cancelled")
    assert result.usage_summary["unreported_requests"] == 1
    assert result.usage_summary["complete"] is False


@pytest.mark.parametrize("failure", ["http", "network"])
def test_retry_consumes_same_remaining_budget(transport, failure):
    env = transport

    def error_body():
        env.clock.advance(2)
        if failure == "network":
            raise requests.ReadTimeout("synthetic timeout")
        return b'{"error":{"message":"temporary"}}'

    env.responses = [Response([error_body], status=503 if failure == "http" else 200)]
    result = env.invoke()
    assert result.job_status == "succeeded"
    assert [call["timeout"] for call in env.calls] == pytest.approx([5, 2])
    assert result.usage_summary["unreported_requests"] == 1
    assert result.usage_summary["reported_requests"] == 1
    assert_messages(env)
    assert_closed(env, result)


@pytest.mark.parametrize("terminal", [False, True])
def test_fallback_consumes_same_remaining_budget(transport, terminal):
    env = transport
    if not env.stream:
        pytest.skip("empty-stream fallback requires streaming")

    def empty_body():
        env.clock.advance(2)
        if not terminal:
            return b""
        return b'data: {"type":"message_stop"}\n\n' if env.protocol == "anthropic" else b"data: [DONE]\n\n"

    env.responses = [Response([empty_body])]
    result = env.invoke()
    assert result.job_status == "succeeded"
    assert [call["timeout"] for call in env.calls] == pytest.approx([5, 3])
    assert [call["json"]["stream"] for call in env.calls] == [True, False]
    assert_messages(env)
    assert_closed(env, result)


def test_timeout_clamp_never_changes_shared_provider_config(transport):
    env = transport
    first = env.invoke()
    second = env.invoke(20)
    assert first.job_status == second.job_status == "succeeded"
    assert [call["timeout"] for call in env.calls] == pytest.approx([5, 20])
    assert env.calls[0]["session"] is env.calls[1]["session"]
    config = env.service._load_provider_config(env.db, env.provider_id)
    assert config.timeout_seconds == 120
    assert_closed(env, second)


def test_lower_configured_timeout_is_not_raised(transport):
    env = transport
    env.db.update_ai_provider(env.provider_id, {"timeout_seconds": 1})
    result = env.invoke()
    assert result.job_status == "succeeded"
    assert env.calls[0]["timeout"] == 1
    assert_closed(env, result)


def test_expired_route_does_not_reserve_or_send(transport):
    env = transport
    result = env.invoke(0)
    assert not env.calls
    job = assert_closed(env, result)
    assert job["network_request_count"] == 0
    assert job["error_message"] == "route_budget_exhausted"


def test_retry_wait_stops_without_another_reservation(transport):
    env = transport

    def error_body():
        env.clock.advance(4.75)
        return b'{"error":{"message":"temporary"}}'

    env.responses = [Response([error_body], status=503)]
    result = env.invoke()
    assert result.job_status == "failed"
    assert env.clock.elapsed == pytest.approx(5)
    assert len(env.calls) == 1
    assert assert_closed(env, result)["network_request_count"] == 1


@pytest.mark.parametrize("stop", ["deadline", "cancel"])
def test_partial_stream_keeps_owned_text_and_stops_reading(transport, stop):
    env = transport
    if not env.stream:
        pytest.skip("partial streaming requires streaming")
    first = ({"type": "content_block_delta", "delta": {"text": "OWNED"}}
             if env.protocol == "anthropic" else {"choices": [{"delta": {"content": "OWNED"}}]})

    def stopped_chunk():
        if stop == "deadline":
            env.clock.advance(6)
        else:
            env.stopped = True
        return b"unfinished SSE data"

    response = Response([("data: " + json.dumps(first) + "\n\n").encode(), stopped_chunk, b"unread"])
    env.responses = [response]
    deltas = []
    result = env.invoke(on_delta=deltas.append)
    assert result.job_status == ("partial" if stop == "deadline" else "cancelled")
    assert result.output_text == "OWNED" and deltas == ["OWNED"]
    assert response.reads == 2 and len(env.calls) == 1
    assert_closed(env, result)


@pytest.mark.parametrize("published", [False, True])
def test_monotonic_deadline_survives_wall_clock_rollback_on_read_timeout(transport, monkeypatch, published):
    env = transport
    if published and not env.stream:
        pytest.skip("published partial text requires streaming")
    monkeypatch.setattr(model_router, "_utc_now", lambda: env.clock.start)
    env.db.update_ai_provider(env.provider_id, {"max_retries": 0})

    def timeout():
        env.clock.advance(6)
        raise requests.ReadTimeout("PRIVATE timeout detail")

    chunks = []
    if published:
        first = ({"type": "content_block_delta", "delta": {"text": "OWNED"}}
                 if env.protocol == "anthropic" else {"choices": [{"delta": {"content": "OWNED"}}]})
        chunks.append(("data: " + json.dumps(first) + "\n\n").encode())
    env.responses = [Response([*chunks, timeout])]
    result = env.invoke()
    assert result.job_status == ("partial" if published else "failed")
    job = assert_closed(env, result)
    assert job["error_message"] == "route_budget_exhausted"
    assert job["attempts"][0]["error_category"] == "route_budget_exhausted"
    assert result.output_text == ("OWNED" if published else "")

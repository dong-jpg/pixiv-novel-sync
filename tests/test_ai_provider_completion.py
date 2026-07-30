from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from typing import Any

import pytest
import requests

from pixiv_novel_sync.ai.models import AIProviderConfig, AIStreamChunk
from pixiv_novel_sync.ai.providers import (
    AIProviderError,
    AnthropicProvider,
    OpenAICompatibleProvider,
    create_provider,
)


MESSAGES = [{"role": "user", "content": "hello"}]


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        *,
        payload: dict[str, Any] | None = None,
        lines: list[str] | None = None,
        text: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.lines = lines or []
        self.text = text if text is not None else json.dumps(self._payload)
        self.headers = headers or {}
        self.encoding = "utf-8"
        self.closed = False

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: Any) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self.closed = True

    def iter_content(self, chunk_size: int | None = None) -> Iterator[bytes]:
        del chunk_size
        if self.lines:
            yield ("\n".join(self.lines) + "\n").encode("utf-8")

    def json(self) -> dict[str, Any]:
        return self._payload


def make_config(
    provider_type: str,
    *,
    stream_enabled: bool = True,
    max_retries: int = 0,
) -> AIProviderConfig:
    return AIProviderConfig(
        id=1,
        name="provider",
        provider_type=provider_type,
        base_url=(
            "https://example.com"
            if provider_type == "anthropic"
            else "https://example.com/v1"
        ),
        api_key="test-key",
        default_model="model-a",
        timeout_seconds=1,
        max_retries=max_retries,
        stream_enabled=stream_enabled,
    )


def attach_responses(
    monkeypatch: pytest.MonkeyPatch,
    provider: OpenAICompatibleProvider | AnthropicProvider,
    responses: list[FakeResponse | BaseException],
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    queue = list(responses)

    def fake_post(_url: str, **kwargs: Any) -> FakeResponse:
        calls.append(kwargs)
        response = queue.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(provider, "_post", fake_post)
    return calls


def openai_stream_response(
    text: str,
    finish_reason: str | None,
    *,
    include_done_marker: bool = True,
) -> FakeResponse:
    lines = [
        "data: "
        + json.dumps(
            {
                "choices": [
                    {"delta": {"content": text}, "finish_reason": finish_reason}
                ]
            }
        )
    ]
    if include_done_marker:
        lines.append("data: [DONE]")
    return FakeResponse(lines=lines)


def anthropic_stream_response(text: str, stop_reason: str | None) -> FakeResponse:
    return FakeResponse(
        lines=[
            "data: "
            + json.dumps(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text},
                }
            ),
            "data: "
            + json.dumps(
                {"type": "message_delta", "delta": {"stop_reason": stop_reason}}
            ),
            'data: {"type":"message_stop"}',
        ]
    )


def collect_error(
    provider: OpenAICompatibleProvider | AnthropicProvider,
    **kwargs: Any,
) -> AIProviderError:
    with pytest.raises(AIProviderError) as caught:
        list(
            provider.stream_generate(
                MESSAGES,
                model="model-a",
                temperature=0.7,
                top_p=0.9,
                max_tokens=100,
                **kwargs,
            )
        )
    return caught.value


def test_provider_error_retains_runtime_error_compatibility() -> None:
    error = AIProviderError("legacy error")

    assert isinstance(error, RuntimeError)
    assert str(error) == "legacy error"
    assert error.category == "unknown"
    assert error.scope == "provider"
    assert error.retry_after is None
    assert error.finish_reason is None


def test_unknown_provider_type_is_structured_configuration_failure() -> None:
    config = make_config("unsupported", stream_enabled=False)

    with pytest.raises(AIProviderError) as caught:
        create_provider(config)

    assert caught.value.category == "configuration"
    assert caught.value.scope == "provider"


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [("stop", "stop"), ("complete", "complete")],
)
def test_openai_normal_finish_emits_typed_done(
    finish_reason: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleProvider(make_config("openai_compatible"))
    attach_responses(
        monkeypatch,
        provider,
        [openai_stream_response("正文", finish_reason)],
    )

    chunks = list(
        provider.stream_generate(
            MESSAGES,
            model="model-a",
            temperature=0.7,
            top_p=0.9,
            max_tokens=100,
        )
    )

    assert [(chunk.type, chunk.text) for chunk in chunks] == [
        ("delta", "正文"),
        ("done", ""),
    ]
    assert chunks[-1].data == {"finish_reason": expected}


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [("length", "length"), ("content_filter", "content_filter"), (None, "missing")],
)
def test_openai_non_normal_finish_is_typed_failure(
    finish_reason: str | None,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleProvider(make_config("openai_compatible"))
    attach_responses(
        monkeypatch,
        provider,
        [openai_stream_response("正文", finish_reason)],
    )
    chunks: list[AIStreamChunk] = []

    with pytest.raises(AIProviderError) as caught:
        for chunk in provider.stream_generate(
            MESSAGES,
            model="model-a",
            temperature=0.7,
            top_p=0.9,
            max_tokens=100,
        ):
            chunks.append(chunk)

    assert [chunk.text for chunk in chunks if chunk.type == "delta"] == ["正文"]
    assert caught.value.category == "incomplete_response"
    assert caught.value.scope == "model"
    assert caught.value.finish_reason == expected


def test_openai_transport_end_without_terminal_marker_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleProvider(make_config("openai_compatible"))
    attach_responses(
        monkeypatch,
        provider,
        [openai_stream_response("正文", None, include_done_marker=False)],
    )

    error = collect_error(provider)

    assert error.category == "incomplete_response"
    assert error.scope == "model"
    assert error.finish_reason == "missing"


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [("end_turn", "complete"), ("stop_sequence", "complete")],
)
def test_anthropic_normal_finish_emits_typed_done(
    stop_reason: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AnthropicProvider(make_config("anthropic"))
    attach_responses(
        monkeypatch,
        provider,
        [anthropic_stream_response("正文", stop_reason)],
    )

    chunks = list(
        provider.stream_generate(
            MESSAGES,
            model="model-a",
            temperature=0.7,
            top_p=0.9,
            max_tokens=100,
        )
    )

    assert [chunk.type for chunk in chunks] == ["delta", "done"]
    assert chunks[-1].data == {"finish_reason": expected}


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [("max_tokens", "length"), ("refusal", "content_filter"), (None, "missing")],
)
def test_anthropic_non_normal_finish_is_typed_failure(
    stop_reason: str | None,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AnthropicProvider(make_config("anthropic"))
    attach_responses(
        monkeypatch,
        provider,
        [anthropic_stream_response("正文", stop_reason)],
    )

    error = collect_error(provider)

    assert error.category == "incomplete_response"
    assert error.scope == "model"
    assert error.finish_reason == expected


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
def test_stream_non_object_event_is_typed_provider_failure(
    provider_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if provider_type == "openai_compatible":
        provider = OpenAICompatibleProvider(make_config(provider_type))
    else:
        provider = AnthropicProvider(make_config(provider_type))
    attach_responses(monkeypatch, provider, [FakeResponse(lines=["data: []"])])

    error = collect_error(provider)

    assert error.category == "invalid_response"
    assert error.scope == "provider"


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
def test_empty_normal_non_stream_response_is_model_failure(
    provider_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if provider_type == "openai_compatible":
        provider = OpenAICompatibleProvider(
            make_config(provider_type, stream_enabled=False)
        )
        response = FakeResponse(
            payload={
                "choices": [
                    {"message": {"content": ""}, "finish_reason": "stop"}
                ]
            }
        )
    else:
        provider = AnthropicProvider(make_config(provider_type, stream_enabled=False))
        response = FakeResponse(payload={"content": [], "stop_reason": "end_turn"})
    attach_responses(monkeypatch, provider, [response])

    error = collect_error(provider)

    assert error.category == "empty_response"
    assert error.scope == "model"


@pytest.mark.parametrize(
    ("status", "payload", "expected_scope", "expected_category"),
    [
        (401, {"error": {"message": "invalid API key"}}, "provider", "authentication"),
        (429, {"error": {"message": "rate limit"}}, "provider", "rate_limited"),
        (404, {"error": {"message": "model model-a not found"}}, "model", "model_not_found"),
        (400, {"error": {"message": "maximum context length exceeded"}}, "model", "context_overflow"),
        (408, {"error": {"type": "model_timeout", "message": "model timed out"}}, "model", "timeout"),
        (500, {"error": {"message": "bad gateway"}}, "provider", "gateway"),
    ],
)
def test_http_error_scope_and_category_are_structured(
    status: int,
    payload: dict[str, Any],
    expected_scope: str,
    expected_category: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleProvider(
        make_config("openai_compatible", stream_enabled=False)
    )
    attach_responses(monkeypatch, provider, [FakeResponse(status, payload=payload)])

    error = collect_error(provider)

    assert error.scope == expected_scope
    assert error.category == expected_category


def test_retry_after_is_parsed_and_upstream_secret_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleProvider(
        make_config("openai_compatible", stream_enabled=False)
    )
    response = FakeResponse(
        429,
        payload={"error": {"message": "Bearer sk-upstream-secret rate limit"}},
        headers={"Retry-After": "2.5"},
    )
    attach_responses(monkeypatch, provider, [response])

    error = collect_error(provider)

    assert error.retry_after == 2.5
    assert error.category == "rate_limited"
    assert "sk-upstream-secret" not in str(error)


def test_account_quota_is_provider_scoped_not_model_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleProvider(
        make_config("openai_compatible", stream_enabled=False)
    )
    response = FakeResponse(
        429,
        payload={
            "error": {
                "type": "insufficient_quota",
                "code": "insufficient_quota",
                "message": "quota exhausted",
            }
        },
    )
    attach_responses(monkeypatch, provider, [response])

    error = collect_error(provider)

    assert error.scope == "provider"
    assert error.category == "quota_exhausted"


def test_connect_error_is_provider_scoped_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleProvider(
        make_config("openai_compatible", stream_enabled=False)
    )
    attach_responses(
        monkeypatch,
        provider,
        [requests.ConnectTimeout("Bearer sk-network-secret timed out")],
    )

    error = collect_error(provider)

    assert error.scope == "provider"
    assert error.category == "network"
    assert "sk-network-secret" not in str(error)


def test_disabled_provider_fails_before_network_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config("openai_compatible", stream_enabled=False)
    config.enabled = False
    provider = OpenAICompatibleProvider(config)
    calls = attach_responses(
        monkeypatch,
        provider,
        [
            FakeResponse(
                payload={
                    "choices": [
                        {"message": {"content": "不应调用"}, "finish_reason": "stop"}
                    ]
                }
            )
        ],
    )

    error = collect_error(provider)

    assert calls == []
    assert error.category == "configuration"
    assert error.scope == "provider"


def test_dns_resolution_failure_is_typed_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleProvider(
        make_config("openai_compatible", stream_enabled=False)
    )

    def fail_dns(*_args: Any, **_kwargs: Any) -> Any:
        raise socket.gaierror("dns unavailable")

    monkeypatch.setattr("pixiv_novel_sync.ai.providers.socket.getaddrinfo", fail_dns)

    error = collect_error(provider)

    assert error.category == "network"
    assert error.scope == "provider"


def test_request_guard_counts_retries_and_stream_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleProvider(
        make_config("openai_compatible", max_retries=1)
    )
    calls = attach_responses(
        monkeypatch,
        provider,
        [
            FakeResponse(503, text="bad gateway"),
            FakeResponse(lines=['data: {"choices":[{"delta":{}}]}', "data: [DONE]"]),
            FakeResponse(
                payload={
                    "choices": [
                        {
                            "message": {"content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            ),
        ],
    )
    monkeypatch.setattr("pixiv_novel_sync.ai.providers.time.sleep", lambda _seconds: None)
    guard_calls = 0

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1

    chunks = list(
        provider.stream_generate(
            MESSAGES,
            model="model-a",
            temperature=0.7,
            top_p=0.9,
            max_tokens=100,
            request_guard=guard,
        )
    )

    assert guard_calls == len(calls) == 3
    assert chunks[-1].data == {"finish_reason": "stop"}


def test_cancelled_before_request_never_calls_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleProvider(make_config("openai_compatible"))
    calls = attach_responses(
        monkeypatch,
        provider,
        [openai_stream_response("不应调用", "stop")],
    )

    error = collect_error(provider, is_cancelled=lambda: True)

    assert calls == []
    assert error.category == "cancelled"
    assert error.scope == "model"
    assert error.finish_reason == "cancelled"


def test_cancelled_after_delta_closes_active_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleProvider(make_config("openai_compatible"))
    response = FakeResponse(
        lines=[
            'data: {"choices":[{"delta":{"content":"正文"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )
    attach_responses(monkeypatch, provider, [response])
    cancelled = False

    iterator = provider.stream_generate(
        MESSAGES,
        model="model-a",
        temperature=0.7,
        top_p=0.9,
        max_tokens=100,
        is_cancelled=lambda: cancelled,
    )
    first = next(iterator)
    cancelled = True

    with pytest.raises(AIProviderError) as caught:
        next(iterator)

    assert first.type == "delta"
    assert first.text == "正文"
    assert caught.value.category == "cancelled"
    assert response.closed is True

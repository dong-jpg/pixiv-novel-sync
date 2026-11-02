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
    ProviderConfigError,
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


def test_retry_delay_honors_retry_after_and_caps_at_sixty() -> None:
    from pixiv_novel_sync.ai.providers import _retry_delay

    assert _retry_delay(0, None) == 1
    assert _retry_delay(1, 2.5) == 2.5
    assert _retry_delay(3, 2) == 8
    assert _retry_delay(2, 120) == 60


def test_sse_line_over_one_mebibyte_is_rejected() -> None:
    from pixiv_novel_sync.ai.providers import _iter_sse_lines

    class _Huge:
        def iter_content(self, chunk_size=None):
            del chunk_size
            yield b"x" * (1024 * 1024 + 8)

    with pytest.raises(AIProviderError, match="1 MiB"):
        list(_iter_sse_lines(_Huge()))


def test_sse_stream_with_replacement_chars_and_no_done_still_yields_lines(caplog) -> None:
    """回归（审查 2026-10-28 A1）：流没收到 [DONE] 就结束、且解码出 U+FFFD 时，
    告警分支引用了未定义的 logger，NameError 让一次已收全 JSON 的关键词清洗记为失败。
    """
    from pixiv_novel_sync.ai.providers import _iter_sse_lines

    class _Truncated:
        def iter_content(self, chunk_size=None):
            del chunk_size
            yield 'data: {"choices": [{"delta": {"content": "关键'.encode("utf-8")
            # 半个汉字后流就断了：没有换行，也没有 [DONE]
            yield "词".encode("utf-8")[:2]

    with caplog.at_level("WARNING", logger="pixiv_novel_sync.ai.providers"):
        lines = list(_iter_sse_lines(_Truncated()))

    assert len(lines) == 1
    assert lines[0].startswith('data: {"choices"')
    assert lines[0].endswith("�")
    assert "U+FFFD" in caplog.text


def test_error_body_over_one_mebibyte_is_rejected() -> None:
    from pixiv_novel_sync.ai.providers import _response_error_details

    class _Huge:
        encoding = "utf-8"
        headers = {"Content-Length": str(1024 * 1024 + 1)}
        content = b""
        text = ""

        def close(self) -> None:
            self.closed = True

        def json(self):
            return {}

    response = _Huge()
    with pytest.raises(AIProviderError, match="1 MiB"):
        _response_error_details(response)


def test_openai_compatible_does_not_treat_anthropic_host_as_official() -> None:
    provider = OpenAICompatibleProvider(
        make_config("openai_compatible", stream_enabled=False)
    )
    provider.config.base_url = "https://api.anthropic.com"
    assert provider._resolve_base_url() == "https://api.anthropic.com/v1"


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
        (429, {"error": {"message": "rate limit for model model-a"}}, "model", "rate_limited"),
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


def test_provider_error_redacts_literal_api_key_before_regex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config("openai_compatible", stream_enabled=False)
    config.api_key = "xai-gateway-token-42"
    provider = OpenAICompatibleProvider(config)
    response = FakeResponse(
        400,
        payload={"error": {"message": "rejected xai-gateway-token-42"}},
    )
    attach_responses(monkeypatch, provider, [response])

    error = collect_error(provider)

    assert "xai-gateway-token-42" not in str(error)
    assert "[REDACTED]" in str(error)


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


def test_chat_usage_only_frame_after_finish_reason_keeps_original_request_ordinal(monkeypatch):
    provider = OpenAICompatibleProvider(make_config("openai_compatible"))
    response = FakeResponse(lines=[
        'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}',
        'data: [DONE]',
    ])
    attach_responses(monkeypatch, provider, [response])
    seen = []
    chunks = list(provider.stream_generate(
        MESSAGES, "m", 0.0, 0.9, 50,
        request_guard=lambda: 7,
        on_usage=lambda ordinal, usage: seen.append((ordinal, dict(usage))),
    ))
    assert "".join(chunk.text for chunk in chunks if chunk.type == "delta") == "ok"
    assert chunks[-1].data == {"finish_reason": "stop"}
    assert seen[-1][0] == 7
    assert seen[-1][1]["input_tokens"] == 10
    assert seen[-1][1]["complete"] is True
    assert response.closed


def test_empty_stream_fallback_has_two_independent_usage_records(monkeypatch):
    provider = OpenAICompatibleProvider(make_config("openai_compatible"))
    attach_responses(monkeypatch, provider, [
        FakeResponse(lines=[
            'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":0,"total_tokens":10}}',
            'data: [DONE]',
        ]),
        FakeResponse(payload={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }),
    ])
    latest = {}
    allocated = []

    def guard():
        if allocated:
            assert latest[allocated[-1]]["complete"] is True
        allocated.append(len(allocated) + 1)
        return allocated[-1]

    list(provider.stream_generate(
        MESSAGES, "m", 0.0, 0.9, 50, request_guard=guard,
        on_usage=lambda ordinal, usage: latest.__setitem__(ordinal, dict(usage)),
    ))
    assert set(latest) == {1, 2}
    assert latest[1]["output_tokens"] == 0
    assert latest[2]["output_tokens"] == 2
    assert sum(item["input_tokens"] for item in latest.values()) == 20


def usage_response(provider_type, *, stream=True, text="ok", reason=None, terminal=True):
    if provider_type == "anthropic":
        usage = {"input_tokens": 12, "cache_creation_input_tokens": 8,
                 "cache_read_input_tokens": 80, "output_tokens": 7}
        reason = reason or "end_turn"
        if not stream:
            return FakeResponse(payload={"content": [{"type": "text", "text": text}],
                                         "stop_reason": reason, "usage": usage})
        events = [
            {"type": "message_start", "message": {"usage": {**usage, "output_tokens": 1}}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
            {"type": "message_delta", "usage": {"output_tokens": 5}},
            {"type": "message_delta", "delta": {"stop_reason": reason}, "usage": {"output_tokens": 7}},
            {"type": "message_delta", "usage": {"output_tokens": 7}},
        ]
        if terminal:
            events.append({"type": "message_stop"})
        return FakeResponse(lines=["data: " + json.dumps(event) for event in events])
    usage = {"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 107,
             "prompt_tokens_details": {"cached_tokens": 80},
             "completion_tokens_details": {"reasoning_tokens": 2}}
    reason = reason or "stop"
    if not stream:
        return FakeResponse(payload={"choices": [{"message": {"content": text}, "finish_reason": reason}],
                                     "usage": usage})
    lines = ["data: " + json.dumps({"usage": usage, "choices": [
        {"delta": {"content": text}, "finish_reason": reason},
    ]})]
    if terminal:
        lines.append("data: [DONE]")
    return FakeResponse(lines=lines)


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic", "xai"])
@pytest.mark.parametrize("stream", [False, True])
def test_adapters_report_usage_including_anthropic_cumulative_and_inherited_xai(monkeypatch, provider_type, stream):
    provider = create_provider(make_config(provider_type, stream_enabled=stream))
    response = usage_response(provider_type, stream=stream)
    calls = attach_responses(monkeypatch, provider, [response])
    seen = []
    original_messages = [dict(message) for message in MESSAGES]
    chunks = list(provider.stream_generate(
        MESSAGES, "m", 0.0, 0.9, 50, request_guard=lambda: 3,
        on_usage=lambda ordinal, usage: seen.append((ordinal, dict(usage))),
    ))
    assert len(calls) == 1
    assert MESSAGES == original_messages
    assert [chunk.type for chunk in chunks] == ["delta", "done"]
    assert all(ordinal == 3 for ordinal, _ in seen)
    assert all(not usage["complete"] for _, usage in seen[:-1])
    final = seen[-1][1]
    assert (final["input_tokens"], final["output_tokens"], final["total_tokens"]) == (100, 7, 107)
    assert final["cached_input_tokens"] == 80
    assert final["complete"] is True
    assert final["missing_reason"] is None
    assert response.closed


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_unsuccessful_generation_can_still_have_complete_usage(monkeypatch, provider_type, stream, reason):
    provider = create_provider(make_config(provider_type, stream_enabled=stream))
    response = usage_response(provider_type, stream=stream, reason=reason)
    calls = attach_responses(monkeypatch, provider, [response])
    seen = []
    error = collect_error(provider, request_guard=lambda: 1,
                          on_usage=lambda ordinal, usage: seen.append((ordinal, dict(usage))))
    assert error.category == "incomplete_response"
    assert error.finish_reason == reason
    assert len(calls) == 1
    assert seen[-1][1]["complete"] is True
    assert response.closed


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
def test_stream_usage_without_terminal_marker_stays_incomplete_without_changing_completion(monkeypatch, provider_type):
    provider = create_provider(make_config(provider_type))
    response = usage_response(provider_type, terminal=False)
    calls = attach_responses(monkeypatch, provider, [response])
    seen = []
    chunks = list(provider.stream_generate(
        MESSAGES, "m", 0.0, 0.9, 50, request_guard=lambda: 1,
        on_usage=lambda ordinal, usage: seen.append((ordinal, dict(usage))),
    ))
    assert chunks[-1].type == "done"
    assert len(calls) == 1
    assert seen[-1][1]["complete"] is False
    assert seen[-1][1]["missing_reason"] == "interrupted"


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
@pytest.mark.parametrize("action", ["cancel", "close"])
def test_partial_stream_cancellation_or_close_finishes_usage_without_retry(monkeypatch, provider_type, action):
    provider = create_provider(make_config(provider_type, max_retries=2))
    response = usage_response(provider_type)
    calls = attach_responses(monkeypatch, provider, [response])
    seen = []
    cancelled = False
    iterator = provider.stream_generate(
        MESSAGES, "m", 0.0, 0.9, 50, request_guard=lambda: 1,
        is_cancelled=lambda: cancelled,
        on_usage=lambda ordinal, usage: seen.append((ordinal, dict(usage))),
    )
    assert next(iterator).type == "delta"
    if action == "cancel":
        cancelled = True
        with pytest.raises(AIProviderError) as caught:
            next(iterator)
        assert caught.value.category == "cancelled"
    else:
        iterator.close()
    assert len(calls) == 1
    assert seen[-1][1]["complete"] is False
    assert seen[-1][1]["missing_reason"] == "interrupted"
    assert response.closed


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
def test_cancelled_before_send_allocates_no_usage_ordinal(monkeypatch, provider_type, stream):
    provider = create_provider(make_config(provider_type, stream_enabled=stream))
    calls = attach_responses(monkeypatch, provider, [])
    allocations, seen = [], []
    error = collect_error(provider, is_cancelled=lambda: True,
                          request_guard=lambda: allocations.append(1),
                          on_usage=lambda ordinal, usage: seen.append((ordinal, usage)))
    assert error.category == "cancelled"
    assert allocations == seen == calls == []


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("error_type", [ValueError, ProviderConfigError, requests.ConnectionError])
def test_usage_callback_failure_escapes_transport_handlers_without_retry(monkeypatch, provider_type, stream, error_type):
    provider = create_provider(make_config(provider_type, stream_enabled=stream, max_retries=2))
    response = usage_response(provider_type, stream=stream)
    calls = attach_responses(monkeypatch, provider, [response])
    error = error_type("local persistence unavailable")
    seen = []
    allocations = []

    def guard():
        allocations.append(len(allocations) + 1)
        return allocations[-1]

    def fail(ordinal, usage):
        seen.append((ordinal, usage))
        raise error

    with pytest.raises(error_type) as caught:
        list(provider.stream_generate(MESSAGES, "m", 0.0, 0.9, 50, request_guard=guard, on_usage=fail))
    assert caught.value is error
    assert len(seen) == len(calls) == len(allocations) == 1
    assert response.closed


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("error_type", [ProviderConfigError, requests.ConnectionError])
def test_guard_failure_never_allocates_another_usage_ordinal(monkeypatch, provider_type, stream, error_type):
    provider = create_provider(make_config(provider_type, stream_enabled=stream, max_retries=2))
    calls = attach_responses(monkeypatch, provider, [])
    error = error_type("local reservation unavailable")
    allocations = []

    def guard():
        allocations.append(1)
        raise error

    with pytest.raises(error_type) as caught:
        list(provider.stream_generate(MESSAGES, "m", 0.0, 0.9, 50, request_guard=guard, on_usage=lambda *_: None))
    assert caught.value is error
    assert calls == []
    assert allocations == [1]


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
@pytest.mark.parametrize("failure_at", ["guard", "observer", "terminal"])
@pytest.mark.parametrize("error_type", [ProviderConfigError, requests.ConnectionError])
def test_fallback_usage_callback_or_guard_error_cannot_trigger_parent_stream_retry(monkeypatch, provider_type, failure_at, error_type):
    provider = create_provider(make_config(provider_type, max_retries=2))
    empty = usage_response(provider_type, text="")
    fallback = usage_response(provider_type, stream=False)
    calls = attach_responses(monkeypatch, provider, [empty, fallback])
    error = error_type("local accounting unavailable")
    seen, allocations = [], []

    def guard():
        allocations.append(len(allocations) + 1)
        if failure_at == "guard" and len(allocations) == 2:
            raise error
        return allocations[-1]

    def observe(ordinal, usage):
        seen.append((ordinal, usage))
        if (failure_at == "observer" and ordinal == 2) or (failure_at == "terminal" and usage["complete"]):
            raise error

    with pytest.raises(error_type) as caught:
        list(provider.stream_generate(MESSAGES, "m", 0.0, 0.9, 50, request_guard=guard, on_usage=observe))
    assert caught.value is error
    assert len(calls) == (2 if failure_at == "observer" else 1)
    assert len(allocations) == (1 if failure_at == "terminal" else 2)
    assert empty.closed
    if failure_at == "observer":
        assert fallback.closed
        assert len([ordinal for ordinal, _ in seen if ordinal == 2]) == 1


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
def test_legacy_none_guard_emits_no_usage_callback(monkeypatch, provider_type):
    provider = create_provider(make_config(provider_type))
    attach_responses(monkeypatch, provider, [usage_response(provider_type)])
    seen = []
    chunks = list(provider.stream_generate(
        MESSAGES, "m", 0.0, 0.9, 50, request_guard=lambda: None,
        on_usage=lambda ordinal, usage: seen.append((ordinal, usage)),
    ))
    assert chunks[-1].type == "done"
    assert seen == []


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
def test_interrupted_usage_callback_failure_stops_http_retry_before_next_ordinal(monkeypatch, provider_type, stream):
    provider = create_provider(make_config(provider_type, stream_enabled=stream, max_retries=2))
    response = FakeResponse(503, text="unavailable")
    calls = attach_responses(monkeypatch, provider, [response])
    monkeypatch.setattr("pixiv_novel_sync.ai.providers._sleep_before_retry", lambda *_: None)
    error = requests.ConnectionError("local persistence unavailable")
    allocations, seen = [], []

    def guard():
        allocations.append(len(allocations) + 1)
        return allocations[-1]

    def fail(ordinal, usage):
        seen.append((ordinal, usage))
        raise error

    with pytest.raises(requests.ConnectionError) as caught:
        list(provider.stream_generate(MESSAGES, "m", 0.0, 0.9, 50, request_guard=guard, on_usage=fail))
    assert caught.value is error
    assert len(calls) == len(allocations) == len(seen) == 1
    assert seen[0][1]["missing_reason"] == "interrupted"
    assert response.closed


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
def test_non_stream_final_usage_callback_finishes_before_body_is_yielded(monkeypatch, provider_type):
    provider = create_provider(make_config(provider_type, stream_enabled=False, max_retries=2))
    response = usage_response(provider_type, stream=False)
    calls = attach_responses(monkeypatch, provider, [response])
    error = requests.ConnectionError("local final persistence unavailable")
    chunks, seen = [], []

    def observe(ordinal, usage):
        seen.append((ordinal, usage))
        if usage["complete"]:
            raise error

    with pytest.raises(requests.ConnectionError) as caught:
        for chunk in provider.stream_generate(MESSAGES, "m", 0.0, 0.9, 50, request_guard=lambda: 1, on_usage=observe):
            chunks.append(chunk)
    assert caught.value is error
    assert len(calls) == 1
    assert chunks == []
    assert len(seen) == 2
    assert response.closed


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
def test_invalid_provider_usage_does_not_leak_text_or_retry_successful_body(monkeypatch, caplog, provider_type, stream):
    provider = create_provider(make_config(provider_type, stream_enabled=stream, max_retries=2))
    response = usage_response(provider_type, stream=stream)
    private = "private upstream usage content"
    if stream:
        event = json.loads(response.lines[0][6:])
        target = event["message"] if provider_type == "anthropic" else event
        target["usage"] = private
        response.lines[0] = "data: " + json.dumps(event)
    else:
        response._payload["usage"] = private
    calls = attach_responses(monkeypatch, provider, [response])
    seen = []
    chunks = list(provider.stream_generate(
        MESSAGES, "m", 0.0, 0.9, 50, request_guard=lambda: 1,
        on_usage=lambda ordinal, usage: seen.append((ordinal, usage)),
    ))
    assert len(calls) == 1
    assert chunks[-1].type == "done"
    assert seen[-1][1]["complete"] is False
    assert seen[-1][1]["missing_reason"] == "invalid_usage"
    assert private not in repr(seen)
    assert private not in caplog.text
    assert response.closed


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
def test_empty_stream_eof_finishes_usage_before_fallback_ordinal(monkeypatch, provider_type):
    provider = create_provider(make_config(provider_type))
    calls = attach_responses(monkeypatch, provider, [
        usage_response(provider_type, text="", terminal=False),
        usage_response(provider_type, stream=False),
    ])
    latest, allocations = {}, []

    def guard():
        if allocations:
            assert latest[1]["complete"] is False
            assert latest[1]["missing_reason"] == "interrupted"
        allocations.append(len(allocations) + 1)
        return allocations[-1]

    chunks = list(provider.stream_generate(
        MESSAGES, "m", 0.0, 0.9, 50, request_guard=guard,
        on_usage=lambda ordinal, usage: latest.__setitem__(ordinal, dict(usage)),
    ))
    assert len(calls) == 2
    assert allocations == [1, 2]
    assert latest[2]["complete"] is True
    assert chunks[-1].type == "done"


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
def test_non_stream_empty_body_still_records_reported_usage(monkeypatch, provider_type):
    provider = create_provider(make_config(provider_type, stream_enabled=False))
    response = usage_response(provider_type, stream=False)
    response._payload["content" if provider_type == "anthropic" else "choices"] = []
    attach_responses(monkeypatch, provider, [response])
    seen = []
    error = collect_error(provider, request_guard=lambda: 1,
                          on_usage=lambda ordinal, usage: seen.append((ordinal, usage)))
    assert error.category == "empty_response"
    assert seen[-1][1]["complete"] is True
    assert seen[-1][1]["total_tokens"] == 107
    assert response.closed


@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
def test_unreported_usage_stays_unknown_without_an_extra_send(monkeypatch, provider_type, stream):
    provider = create_provider(make_config(provider_type, stream_enabled=stream, max_retries=2))
    if stream:
        response = (anthropic_stream_response("ok", "end_turn") if provider_type == "anthropic"
                    else openai_stream_response("ok", "stop"))
    else:
        response = usage_response(provider_type, stream=False)
        response._payload.pop("usage")
    calls = attach_responses(monkeypatch, provider, [response])
    seen = []
    chunks = list(provider.stream_generate(
        MESSAGES, "m", 0.0, 0.9, 50, request_guard=lambda: 1,
        on_usage=lambda ordinal, usage: seen.append((ordinal, usage)),
    ))
    assert len(calls) == len(seen) == 1
    assert chunks[-1].type == "done"
    assert seen[0][1]["missing_reason"] == "not_reported"
    assert seen[0][1]["input_tokens"] is None
    assert seen[0][1]["output_tokens"] is None
    assert seen[0][1]["total_tokens"] is None

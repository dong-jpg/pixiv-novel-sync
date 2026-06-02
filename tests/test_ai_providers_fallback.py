from __future__ import annotations

from pixiv_novel_sync.ai.models import AIProviderConfig
from pixiv_novel_sync.ai.providers import AnthropicProvider, OpenAICompatibleProvider


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "", lines: list[str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.lines = lines or []
        self.encoding = "utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_lines(self, decode_unicode: bool = False):
        return iter(self.lines)

    def json(self):
        return self._payload


def make_config(provider_type: str) -> AIProviderConfig:
    return AIProviderConfig(
        id=1,
        name="provider",
        provider_type=provider_type,
        base_url="https://example.com/v1" if provider_type != "anthropic" else "https://example.com",
        api_key="key",
        default_model="model-a",
        timeout_seconds=1,
        max_retries=2,
        stream_enabled=True,
    )


def test_openai_stream_fallback_uses_single_non_stream_attempt(monkeypatch):
    calls: list[dict] = []

    def fake_post(*_args, **kwargs):
        calls.append(kwargs)
        if len(calls) <= 3:
            return FakeResponse(503, text="bad gateway")
        return FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("pixiv_novel_sync.ai.providers.requests.post", fake_post)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    provider = OpenAICompatibleProvider(make_config("openai_compatible"))
    chunks = list(provider.stream_generate(
        [{"role": "user", "content": "hello"}],
        model="model-a",
        temperature=0.7,
        top_p=0.9,
        max_tokens=100,
    ))

    assert len(calls) == 4
    assert calls[-1]["json"]["stream"] is False
    assert [chunk.type for chunk in chunks] == ["progress", "progress", "progress", "delta", "done"]
    assert [chunk.data["phase"] for chunk in chunks[:3] if chunk.data] == ["retry", "retry", "fallback"]
    assert chunks[0].data and chunks[0].data["provider"] == "openai_compatible"
    assert chunks[3].text == "ok"


def test_openai_empty_stream_falls_back_to_non_stream(monkeypatch):
    calls: list[dict] = []

    def fake_post(*_args, **kwargs):
        calls.append(kwargs)
        if kwargs["json"].get("stream"):
            return FakeResponse(200, lines=['data: {"choices":[{"delta":{}}]}', 'data: [DONE]'])
        return FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("pixiv_novel_sync.ai.providers.requests.post", fake_post)

    provider = OpenAICompatibleProvider(make_config("openai_compatible"))
    chunks = list(provider.stream_generate(
        [{"role": "user", "content": "hello"}],
        model="model-a",
        temperature=0.7,
        top_p=0.9,
        max_tokens=100,
    ))

    assert len(calls) == 2
    assert calls[-1]["json"]["stream"] is False
    assert [chunk.type for chunk in chunks] == ["progress", "delta", "done"]
    assert chunks[0].data and chunks[0].data["phase"] == "fallback"
    assert chunks[1].text == "ok"


def test_anthropic_stream_fallback_uses_single_non_stream_attempt(monkeypatch):
    calls: list[dict] = []

    def fake_post(*_args, **kwargs):
        calls.append(kwargs)
        if len(calls) <= 3:
            return FakeResponse(503, text="bad gateway")
        return FakeResponse(200, {"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr("pixiv_novel_sync.ai.providers.requests.post", fake_post)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    provider = AnthropicProvider(make_config("anthropic"))
    chunks = list(provider.stream_generate(
        [{"role": "user", "content": "hello"}],
        model="model-a",
        temperature=0.7,
        top_p=0.9,
        max_tokens=100,
    ))

    assert len(calls) == 4
    assert calls[-1]["json"]["stream"] is False
    assert [chunk.type for chunk in chunks] == ["progress", "progress", "progress", "delta", "done"]
    assert [chunk.data["phase"] for chunk in chunks[:3] if chunk.data] == ["retry", "retry", "fallback"]
    assert chunks[0].data and chunks[0].data["provider"] == "anthropic"
    assert chunks[3].text == "ok"


def test_anthropic_empty_stream_falls_back_to_non_stream(monkeypatch):
    calls: list[dict] = []

    def fake_post(*_args, **kwargs):
        calls.append(kwargs)
        if kwargs["json"].get("stream"):
            return FakeResponse(200, lines=['data: {"type":"message_start"}', 'data: {"type":"message_stop"}'])
        return FakeResponse(200, {"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr("pixiv_novel_sync.ai.providers.requests.post", fake_post)

    provider = AnthropicProvider(make_config("anthropic"))
    chunks = list(provider.stream_generate(
        [{"role": "user", "content": "hello"}],
        model="model-a",
        temperature=0.7,
        top_p=0.9,
        max_tokens=100,
    ))

    assert len(calls) == 2
    assert calls[-1]["json"]["stream"] is False
    assert [chunk.type for chunk in chunks] == ["progress", "delta", "done"]
    assert chunks[0].data and chunks[0].data["phase"] == "fallback"
    assert chunks[1].text == "ok"

from __future__ import annotations

import json
import socket
import time
from urllib.parse import urlparse

import pytest

from pixiv_novel_sync.ai import providers as provider_module
from pixiv_novel_sync.ai.model_catalog import canonical_model_digest
from pixiv_novel_sync.ai.models import AIProviderConfig
from pixiv_novel_sync.ai.providers import AIProviderError, create_provider


PAGE_LIMIT = 4 * 1024 * 1024
TOTAL_LIMIT = 20 * 1024 * 1024


@pytest.fixture(autouse=True)
def public_provider_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fixed_public_ipv4(_host, port, *_args, **_kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("8.8.8.8", port),
            )
        ]

    monkeypatch.setattr(provider_module.socket, "getaddrinfo", fixed_public_ipv4)


class StreamingResponse:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        status_code: int = 200,
    ) -> None:
        self._chunks = chunks
        self._content: bytes | bool = False
        self._content_consumed = False
        self.status_code = status_code
        self.encoding = "utf-8"
        self.closed = False
        self.json_calls = 0

    @classmethod
    def from_payload(cls, payload: object) -> "StreamingResponse":
        return cls(
            [
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ]
        )

    @property
    def content(self) -> bytes:
        if isinstance(self._content, bytes):
            return self._content
        return b"".join(self._chunks)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def json(self):
        self.json_calls += 1
        return json.loads(self.content.decode("utf-8"))

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False


class SizedJsonResponse(StreamingResponse):
    def __init__(self, payload: object, size: int) -> None:
        prefix = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(prefix) > size:
            raise AssertionError("test payload exceeds requested response size")
        super().__init__([prefix])
        self._padding = size - len(prefix)

    def iter_content(self, chunk_size=None):
        yield from self._chunks
        remaining = self._padding
        while remaining:
            count = min(remaining, 64 * 1024)
            yield b" " * count
            remaining -= count


def discovery_page(
    provider_type: str,
    model_ids: list[str],
    *,
    has_more: bool,
    cursor: str | None = None,
) -> dict:
    page: dict = {
        "data": [{"id": model_id} for model_id in model_ids],
        "has_more": has_more,
    }
    if cursor is not None:
        page["last_id" if provider_type == "anthropic" else "next"] = cursor
    return page


def make_discovery_provider(
    provider_type: str,
    monkeypatch: pytest.MonkeyPatch,
    pages: list[dict] | None = None,
    responses: list[StreamingResponse] | None = None,
):
    config = AIProviderConfig(
        id=1,
        name=f"{provider_type}-discovery",
        provider_type=provider_type,
        base_url=(
            "https://api.example.test/v1"
            if provider_type != "anthropic"
            else "https://api.example.test"
        ),
        api_key="secret-key",
        default_model=None,
        timeout_seconds=7,
        max_retries=0,
        proxy="http://proxy.example.test:8080",
    )
    provider = create_provider(config)
    queued = list(responses or [StreamingResponse.from_payload(page) for page in pages or []])
    calls: list[dict] = []

    def fake_get(url: str, **kwargs):
        calls.append({"method": "GET", "url": url, **kwargs})
        if not queued:
            raise AssertionError("unexpected discovery request")
        return queued.pop(0)

    monkeypatch.setattr(provider.session, "get", fake_get)
    return provider, calls


@pytest.mark.parametrize(
    "provider_type",
    ["openai_compatible", "xai", "anthropic"],
)
def test_provider_lists_all_pages_with_canonical_digest(
    provider_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, calls = make_discovery_provider(
        provider_type,
        monkeypatch,
        pages=[
            discovery_page(
                provider_type,
                ["m-1"],
                has_more=True,
                cursor="next-1",
            ),
            discovery_page(provider_type, ["m-2"], has_more=False),
        ],
    )
    progress: list[tuple[int, int]] = []

    result = provider.list_models(on_page=lambda pages, count: progress.append((pages, count)))

    assert [item["model_key"] for item in result.models] == ["m-1", "m-2"]
    assert result.complete is True
    assert result.empty_authoritative is False
    assert result.pages == 2
    assert result.result_digest == canonical_model_digest(result.models)
    assert result.partial_reason is None
    assert progress == [(1, 1), (2, 2)]
    assert len(calls) == 2
    assert all(call["method"] == "GET" for call in calls)
    assert all(call["allow_redirects"] is False for call in calls)
    assert all(call["stream"] is True for call in calls)
    assert all(call["timeout"] == 7 for call in calls)
    assert all(call["proxies"] == provider._proxies() for call in calls)
    assert all(urlparse(call["url"]).path.endswith("/models") for call in calls)
    if provider_type == "anthropic":
        assert calls[0]["headers"]["x-api-key"] == "secret-key"
        assert calls[0]["headers"]["anthropic-version"] == "2023-06-01"
        assert calls[1]["params"] == {"after_id": "next-1"}
    else:
        assert calls[0]["headers"]["Authorization"] == "Bearer secret-key"
        assert calls[1]["params"] == {"after": "next-1"}


def test_model_discovery_caps_network_timeout_to_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, calls = make_discovery_provider(
        "openai_compatible",
        monkeypatch,
        pages=[discovery_page("openai_compatible", ["m-1"], has_more=False)],
    )

    result = provider.list_models(deadline=time.monotonic() + 2)

    assert result.pages == 1
    assert len(calls) == 1
    assert 0 < calls[0]["timeout"] <= 2


def test_model_list_body_limit_fires_before_json_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = StreamingResponse([b"x" * PAGE_LIMIT, b"x"])
    provider, _calls = make_discovery_provider(
        "openai_compatible",
        monkeypatch,
        responses=[response],
    )

    with pytest.raises(AIProviderError, match="4 MiB"):
        provider.list_models()

    assert response.json_calls == 0
    assert response.closed is True


def test_model_discovery_http_error_does_not_persist_upstream_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = StreamingResponse.from_payload(
        {"error": {"message": "secret upstream response body"}}
    )
    response.status_code = 401
    provider, _calls = make_discovery_provider(
        "openai_compatible",
        monkeypatch,
        responses=[response],
    )

    with pytest.raises(AIProviderError) as captured:
        provider.list_models()

    assert "401" in str(captured.value)
    assert "secret upstream response body" not in str(captured.value)


def test_model_list_cumulative_body_limit_is_shared_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        SizedJsonResponse(
            discovery_page(
                "openai_compatible",
                [f"m-{index}"],
                has_more=True,
                cursor=f"cursor-{index}",
            ),
            PAGE_LIMIT,
        )
        for index in range(5)
    ]
    overflow = StreamingResponse.from_payload(
        discovery_page("openai_compatible", ["overflow"], has_more=False)
    )
    responses.append(overflow)
    provider, calls = make_discovery_provider(
        "openai_compatible",
        monkeypatch,
        responses=responses,
    )

    with pytest.raises(AIProviderError, match="20 MiB"):
        provider.list_models()

    assert len(calls) == 6
    assert overflow.closed is True
    assert overflow.json_calls == 0


def test_response_byte_budget_accepts_limit_and_rejects_next_byte() -> None:
    budget = provider_module.ResponseByteBudget()

    budget.consume(TOTAL_LIMIT)
    with pytest.raises(AIProviderError, match="20 MiB"):
        budget.consume(1)


def test_cursor_loop_and_malformed_envelope_are_not_empty_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _calls = make_discovery_provider(
        "openai_compatible",
        monkeypatch,
        pages=[
            {"data": [{"id": "m"}], "has_more": True, "next": "same"},
            {"data": [{"id": "m"}], "has_more": True, "next": "same"},
        ],
    )
    with pytest.raises(AIProviderError, match="分页游标循环"):
        provider.list_models()

    provider, _calls = make_discovery_provider(
        "openai_compatible",
        monkeypatch,
        pages=[{"object": "list"}],
    )
    with pytest.raises(AIProviderError, match="模型数组"):
        provider.list_models()


def test_model_discovery_wraps_unencodable_upstream_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = StreamingResponse(
        [b'{"data":[{"id":"\\ud800"}],"has_more":false}']
    )
    provider, _calls = make_discovery_provider(
        "openai_compatible",
        monkeypatch,
        responses=[response],
    )

    with pytest.raises(AIProviderError, match="模型记录无效"):
        provider.list_models()


@pytest.mark.parametrize(
    ("page", "message"),
    [
        ({"data": ["m"], "has_more": False}, "模型记录"),
        ({"data": [{"id": 1}], "has_more": False}, "模型"),
        ({"data": [], "has_more": "yes"}, "has_more"),
        ({"data": [], "has_more": None}, "has_more"),
        ({"data": [], "has_more": True}, "分页游标"),
        ({"data": [], "has_more": False, "next": "unused"}, "分页状态"),
        ({"data": [], "has_more": False, "next": 7}, "分页游标"),
        ({"data": [], "has_more": False, "partial": True}, "不完整"),
        ({"data": [], "has_more": False, "truncated": True}, "不完整"),
        ({"data": [], "has_more": False, "complete": False}, "不完整"),
    ],
)
def test_model_discovery_rejects_invalid_envelopes(
    monkeypatch: pytest.MonkeyPatch,
    page: dict,
    message: str,
) -> None:
    provider, _calls = make_discovery_provider(
        "openai_compatible",
        monkeypatch,
        pages=[page],
    )

    with pytest.raises(AIProviderError, match=message):
        provider.list_models()


def test_model_discovery_rejects_more_than_5000_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = discovery_page(
        "openai_compatible",
        [f"model-{index}" for index in range(5001)],
        has_more=False,
    )
    provider, _calls = make_discovery_provider(
        "openai_compatible",
        monkeypatch,
        pages=[page],
    )

    with pytest.raises(AIProviderError, match="5000"):
        provider.list_models()


def test_model_discovery_rejects_next_page_at_model_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = discovery_page(
        "openai_compatible",
        [f"model-{index}" for index in range(5000)],
        has_more=True,
        cursor="more",
    )
    provider, calls = make_discovery_provider(
        "openai_compatible",
        monkeypatch,
        pages=[page],
    )

    with pytest.raises(AIProviderError, match="5000"):
        provider.list_models()

    assert len(calls) == 1


def test_model_discovery_rejects_next_page_at_page_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [
        discovery_page(
            "openai_compatible",
            [f"model-{index}"],
            has_more=True,
            cursor=f"cursor-{index}",
        )
        for index in range(100)
    ]
    provider, calls = make_discovery_provider(
        "openai_compatible",
        monkeypatch,
        pages=pages,
    )

    with pytest.raises(AIProviderError, match="100"):
        provider.list_models()

    assert len(calls) == 100


def test_model_discovery_checks_cancellation_before_each_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, calls = make_discovery_provider(
        "openai_compatible",
        monkeypatch,
        pages=[
            discovery_page(
                "openai_compatible",
                ["m-1"],
                has_more=True,
                cursor="next",
            ),
            discovery_page("openai_compatible", ["m-2"], has_more=False),
        ],
    )
    checks = 0

    def is_cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    with pytest.raises(AIProviderError, match="取消"):
        provider.list_models(is_cancelled=is_cancelled)

    assert len(calls) == 1


def test_empty_model_list_is_not_inferred_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _calls = make_discovery_provider(
        "openai_compatible",
        monkeypatch,
        pages=[
            {
                "data": [],
                "has_more": False,
                "empty_authoritative": True,
            }
        ],
    )

    result = provider.list_models()

    assert result.models == []
    assert result.empty_authoritative is False
    assert result.complete is True

from dataclasses import FrozenInstanceError

import pytest

from pixiv_novel_sync.ai.invocation import (
    InvocationValidationError,
    invocation_fingerprint,
    invocation_messages,
    normalize_text_invocation,
)
from pixiv_novel_sync.ai.models import AIAgentConfig


def agent():
    return AIAgentConfig(
        id=1,
        name="test",
        task_type="general",
        provider_id=1,
        model="m",
        system_prompt="must-not-be-injected",
        temperature=0.8,
        top_p=0.9,
        max_tokens=4000,
        context_window=1_000_000,
    )


def test_request_preserves_text_and_zero_parameters_without_aliasing():
    payload = {
        "messages": [{"role": "user", "content": "  START\n中文🙂\nEND  "}],
        "temperature": 0,
        "top_p": 0,
        "max_tokens": 100,
        "allowed_provider_ids": [],
    }
    request = normalize_text_invocation(payload, agent=agent())
    original = request.messages[0].content
    payload["messages"][0]["content"] = "mutated"
    outgoing = invocation_messages(request)
    outgoing[0]["content"] = "also mutated"
    assert request.messages[0].content == original
    assert request.temperature == request.top_p == 0.0
    assert request.allowed_provider_ids == ()
    assert len(request.messages) == 1
    with pytest.raises(FrozenInstanceError):
        request.max_tokens = 1


@pytest.mark.parametrize(
    "patch",
    [
        {"messages": []},
        {"messages": [{"role": "tool", "content": "x"}]},
        {"messages": [{"role": "user", "content": {"secret": "x"}}]},
        {"messages": [{"role": "user", "content": "x", "tool_calls": []}]},
        {"temperature": True},
        {"temperature": float("nan")},
        {"top_p": None},
        {"top_p": 1.1},
        {"max_tokens": False},
        {"max_tokens": 0},
        {"max_tokens": 1_000_001},
        {"allowed_provider_ids": None},
        {"allowed_provider_ids": [True]},
        {"allowed_provider_ids": [0]},
        {"allowed_provider_ids": ["1"]},
        {"tools": []},
        {"response_format": {"type": "json_schema"}},
    ],
)
def test_invalid_request_has_local_safe_error(patch):
    payload = {"messages": [{"role": "user", "content": "DO-NOT-ECHO"}], **patch}
    with pytest.raises(InvocationValidationError) as caught:
        normalize_text_invocation(payload, agent=agent())
    assert caught.value.code
    assert "DO-NOT-ECHO" not in str(caught.value)
    assert "must-not-be-injected" not in str(caught.value)


def test_fingerprint_covers_order_content_parameters_and_effective_scope():
    base = {"messages": [{"role": "user", "content": "abc"}], "max_tokens": 100}
    first = normalize_text_invocation(base, agent=agent())
    omitted = invocation_fingerprint(first, provider_ids=(1, 2))
    assert omitted == invocation_fingerprint(first, provider_ids=(2, 1, 2))
    for patch in (
        {"messages": [{"role": "assistant", "content": "abc"}]},
        {"messages": [{"role": "user", "content": "abc "}]},
        {"max_tokens": 101},
        {"temperature": 0},
    ):
        other = normalize_text_invocation({**base, **patch}, agent=agent())
        assert invocation_fingerprint(other, provider_ids=(1, 2)) != omitted
    empty = normalize_text_invocation({**base, "allowed_provider_ids": []}, agent=agent())
    assert invocation_fingerprint(empty, provider_ids=()) != omitted
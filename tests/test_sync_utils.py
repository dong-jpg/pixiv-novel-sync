from __future__ import annotations

import pytest

from pixiv_novel_sync.sync.utils import retry_on_pixiv_error


def test_retry_backoff_raises_when_cancelled() -> None:
    calls = 0

    @retry_on_pixiv_error(
        max_retries=1,
        base_delay=30,
        stop_requested=lambda: True,
    )
    def operation() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("network timeout")

    with pytest.raises(InterruptedError, match="Task stopped by user"):
        operation()

    assert calls == 1


def _run_with_retry(exc: Exception, max_retries: int = 2) -> int:
    """执行一个总是抛出 exc 的函数，返回实际调用次数。"""
    calls = 0

    @retry_on_pixiv_error(max_retries=max_retries, base_delay=0)
    def operation() -> None:
        nonlocal calls
        calls += 1
        raise exc

    with pytest.raises(type(exc)):
        operation()
    return calls


def test_retry_does_not_match_rate_substring_false_positive() -> None:
    # 消息含 "rate" 子串（如 moderate/generate）不应触发重试
    assert _run_with_retry(RuntimeError("failed to moderate content")) == 1
    assert _run_with_retry(RuntimeError("could not generate token")) == 1


def test_retry_on_explicit_rate_limit_tokens() -> None:
    assert _run_with_retry(RuntimeError("Rate Limit exceeded"), max_retries=2) == 3
    assert _run_with_retry(RuntimeError("HTTP 429 Too Many Requests"), max_retries=2) == 3


def test_retry_on_status_code_429_attribute() -> None:
    class HttpError(Exception):
        def __init__(self) -> None:
            super().__init__("upstream error")
            self.response = type("Resp", (), {"status_code": 429})()

    assert _run_with_retry(HttpError(), max_retries=1) == 2


def test_no_retry_on_non_429_status_code_even_with_rate_word() -> None:
    class HttpError(Exception):
        def __init__(self) -> None:
            super().__init__("moderate rate warning")
            self.response = type("Resp", (), {"status_code": 403})()

    assert _run_with_retry(HttpError(), max_retries=2) == 1


def test_retry_on_network_error_still_works() -> None:
    assert _run_with_retry(RuntimeError("connection reset by peer"), max_retries=1) == 2

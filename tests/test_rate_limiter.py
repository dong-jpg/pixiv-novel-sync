from __future__ import annotations

from types import SimpleNamespace

import pytest

from pixiv_novel_sync import rate_limiter
from pixiv_novel_sync.rate_limiter import RateLimiter


def test_wait_raises_when_cancel_requested_before_sleep(monkeypatch):
    limiter = RateLimiter(default_delay=1.0)
    limiter._last_request_time = 10.0
    monkeypatch.setattr(rate_limiter.time, "time", lambda: 10.1)
    monkeypatch.setattr(rate_limiter.time, "sleep", lambda seconds: pytest.fail("sleep should not run"))

    with pytest.raises(InterruptedError, match="Task stopped by user"):
        limiter.wait(stop_requested=lambda: True)


def test_wait_checks_cancel_during_sleep(monkeypatch):
    limiter = RateLimiter(default_delay=1.0)
    limiter._last_request_time = 10.0
    times = iter([10.1, 10.2])
    slept: list[float] = []
    stop_calls = iter([False, True])
    monkeypatch.setattr(rate_limiter.time, "time", lambda: next(times))
    monkeypatch.setattr(rate_limiter.time, "sleep", lambda seconds: slept.append(seconds))

    with pytest.raises(InterruptedError, match="Task stopped by user"):
        limiter.wait(stop_requested=lambda: next(stop_calls), interval=0.25)

    assert slept == [0.25]


def test_handle_response_checks_cancel_during_retry_after(monkeypatch):
    limiter = RateLimiter(default_delay=1.0)
    response = SimpleNamespace(status_code=429, headers={"Retry-After": "1"})
    slept: list[float] = []
    stop_calls = iter([False, True])
    monkeypatch.setattr(rate_limiter.time, "sleep", lambda seconds: slept.append(seconds))

    with pytest.raises(InterruptedError, match="Task stopped by user"):
        limiter.handle_response(response, stop_requested=lambda: next(stop_calls), interval=0.25)

    assert slept == [0.25]

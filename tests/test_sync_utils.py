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

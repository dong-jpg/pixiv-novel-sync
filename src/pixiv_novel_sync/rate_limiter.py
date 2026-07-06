"""限速器 - 统一限速逻辑和429响应处理"""
import time
import logging
from typing import Any

logger = logging.getLogger(__name__)

_INTERRUPT_MESSAGE = "Task stopped by user"


def _cancellable_sleep(seconds: float, stop_requested: Any, interval: float) -> None:
    """在 seconds 内以 interval 为步长轮询 stop_requested；命中则抛 InterruptedError。

    stop_requested 为 None 时退化为单次 time.sleep，保持原有行为。
    """
    if seconds <= 0:
        return
    if stop_requested is None:
        time.sleep(seconds)
        return

    remaining = float(seconds)
    while remaining > 0:
        if stop_requested():
            raise InterruptedError(_INTERRUPT_MESSAGE)
        sleep_for = min(interval, remaining)
        time.sleep(sleep_for)
        remaining -= sleep_for
    if stop_requested():
        raise InterruptedError(_INTERRUPT_MESSAGE)


class RateLimiter:
    """统一限速器,支持可取消等待"""

    def __init__(self, default_delay: float = 1.0):
        self.default_delay = default_delay
        self._last_request_time = 0.0

    def wait(self, delay: float | None = None, stop_requested: Any = None, interval: float = 0.2) -> None:
        """等待指定延迟,如果未指定则使用默认值。

        传入 stop_requested 时按 interval 轮询取消信号,命中则抛
        InterruptedError("Task stopped by user");为 None 时退化为单次 sleep。
        """
        actual_delay = delay if delay is not None else self.default_delay
        if actual_delay > 0:
            elapsed = time.time() - self._last_request_time
            remaining = actual_delay - elapsed
            if remaining > 0:
                _cancellable_sleep(remaining, stop_requested, interval)
        self._last_request_time = time.time()

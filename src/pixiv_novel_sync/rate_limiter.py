"""限速器 - 统一限速逻辑和429响应处理"""
import time
import logging
from typing import Any

logger = logging.getLogger(__name__)


class RateLimiter:
    """统一限速器,支持429响应自动重试"""

    def __init__(self, default_delay: float = 1.0):
        self.default_delay = default_delay
        self._last_request_time = 0.0

    def wait(self, delay: float | None = None) -> None:
        """等待指定延迟,如果未指定则使用默认值"""
        actual_delay = delay if delay is not None else self.default_delay
        if actual_delay > 0:
            elapsed = time.time() - self._last_request_time
            if elapsed < actual_delay:
                time.sleep(actual_delay - elapsed)
        self._last_request_time = time.time()

    def handle_response(self, response: Any) -> bool:
        """
        处理API响应,检查429限流

        Returns:
            True: 正常响应,可以继续
            False: 429限流,已等待重试
        """
        if not hasattr(response, 'status_code'):
            return True

        if response.status_code == 429:
            retry_after = self._get_retry_after(response)
            logger.warning(f"Rate limit hit (429), waiting {retry_after}s before retry")
            time.sleep(retry_after)
            self._last_request_time = time.time()
            return False

        return True

    def _get_retry_after(self, response: Any) -> int:
        """从响应头提取Retry-After,默认60秒"""
        if hasattr(response, 'headers'):
            retry_after = response.headers.get('Retry-After', '60')
            try:
                return int(retry_after)
            except ValueError:
                pass
        return 60

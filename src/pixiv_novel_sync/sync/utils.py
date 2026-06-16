"""Utility functions for sync operations.

This module contains helper functions extracted from sync_engine.py for better
code organization and reusability.
"""

from __future__ import annotations

import logging
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

T = TypeVar('T')


def retry_on_pixiv_error(max_retries: int = 3, base_delay: float = 5.0) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Pixiv API重试装饰器:捕获429/网络错误,指数退避重试。

    Args:
        max_retries: 最大重试次数(不含首次调用)
        base_delay: 基础延迟(秒),429时翻倍退避,最长60s
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    # 429 或网络错误才重试
                    is_rate_limit = False
                    is_network = False
                    err_str = str(e).lower()
                    if "429" in err_str or "rate" in err_str:
                        is_rate_limit = True
                    elif any(k in err_str for k in ["connection", "timeout", "network", "unreachable"]):
                        is_network = True

                    if not (is_rate_limit or is_network):
                        raise  # 非重试类错误立即抛出

                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt), 60.0) if is_rate_limit else base_delay
                        logger.warning(
                            f"{func.__name__} {'rate limited (429)' if is_rate_limit else 'network error'}, "
                            f"retry {attempt+1}/{max_retries} after {delay:.1f}s: {e}"
                        )
                        time.sleep(delay)
                    else:
                        logger.error(f"{func.__name__} failed after {max_retries} retries: {e}")

            raise last_exc  # type: ignore
        return wrapper
    return decorator


def _to_plain(value: Any) -> Any:
    """将 Pixiv API 对象转为普通 dict/list/str/int/float/bool。

    递归处理嵌套对象、列表、字典等数据结构，将所有自定义对象转换为 JSON 可序列化的类型。
    """
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    if isinstance(value, tuple):
        return [_to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if hasattr(value, "__dict__"):
        return {str(key): _to_plain(item) for key, item in vars(value).items() if not key.startswith("_")}
    return str(value)


def _extract_tags(tags: Any) -> list[dict[str, Any]]:
    """提取标签列表，转换为普通 dict 列表。"""
    results: list[dict[str, Any]] = []
    for tag in tags or []:
        results.append(_to_plain(tag))
    return results


def _extract_cover_url(novel: Any) -> str | None:
    """提取小说封面 URL，优先级：large > medium > square_medium。"""
    image_urls = getattr(novel, "image_urls", None)
    for field in ("large", "medium", "square_medium"):
        url = getattr(image_urls, field, None) if image_urls is not None else None
        if url:
            return str(url)
    return None


def _extract_novel_text(webview: Any) -> str:
    """从 webview 响应中提取小说文本内容。

    尝试多个可能的字段名: novel_text, text, body。
    """
    for key in ("novel_text", "text", "body"):
        value = getattr(webview, key, None)
        if value:
            return str(value)
    if isinstance(webview, dict):
        for key in ("novel_text", "text", "body"):
            value = webview.get(key)
            if value:
                return str(value)
    return ""


def _is_pixiv_image_url(url: str) -> bool:
    """True only when the URL host is exactly ``pximg.net`` or a ``*.pximg.net`` subdomain.

    A plain substring test (``"pximg.net" in url``) also matches hostile hosts such
    as ``pximg.net.evil.com`` or ``evil-pximg.net``, which would let attacker-controlled
    novel content drive arbitrary downloads through the configured proxy (SSRF).
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "pximg.net" or host.endswith(".pximg.net")


def _collect_asset_urls(novel: Any, webview: Any) -> list[tuple[str, str]]:
    """收集小说的所有资源 URL（封面 + 内联图片）。

    Returns:
        List of (asset_type, url) tuples, where asset_type is "cover" or "inline_image".
    """
    results: list[tuple[str, str]] = []
    cover_url = _extract_cover_url(novel)
    if cover_url:
        results.append(("cover", cover_url))

    plain_webview = _to_plain(webview)
    visited: set[str] = set()
    for url in _walk_urls(plain_webview):
        if _is_pixiv_image_url(url) and url not in visited:
            visited.add(url)
            results.append(("inline_image", url))
    return results


def _walk_urls(value: Any) -> list[str]:
    """递归遍历数据结构，提取所有 HTTP/HTTPS URL。"""
    urls: list[str] = []
    if isinstance(value, str):
        if value.startswith("http://") or value.startswith("https://"):
            urls.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            urls.extend(_walk_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_walk_urls(item))
    return urls


def _filename_from_url(url: str) -> str:
    """从 URL 中提取文件名，用于保存下载的资源。"""
    path = urlparse(url).path
    name = Path(path).name
    return name or "asset.bin"


def _empty_stats() -> dict[str, int]:
    """创建空的统计字典"""
    return {
        "users": 0,
        "novels": 0,
        "texts_updated": 0,
        "assets_downloaded": 0,
        "failed": 0,
        "skipped": 0,
        "following_users_scanned": 0,
    }


def _merge_stats(stats: dict[str, int], counters: dict[str, int]) -> None:
    """合并统计计数"""
    for key, value in counters.items():
        stats[key] = stats.get(key, 0) + value


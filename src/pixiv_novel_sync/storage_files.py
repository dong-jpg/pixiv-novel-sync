from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Iterable

import requests

from .settings import Settings
from .utils_hashing import sha256_text
from .utils_naming import ensure_parent, safe_name

logger = logging.getLogger(__name__)


_DEFAULT_HEADERS = {
    "Referer": "https://www.pixiv.net/",
    "User-Agent": "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)",
}


class FileStorage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session: requests.Session | None = None

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(_DEFAULT_HEADERS)
        return self._session

    def base_dir(self, restrict: str) -> Path:
        return self.settings.storage.private_dir if restrict == "private" else self.settings.storage.public_dir

    def novel_dir(self, restrict: str, user_id: int, user_name: str, novel_id: int, title: str) -> Path:
        author_dir = self.base_dir(restrict) / "authors" / f"{user_id}_{safe_name(user_name, 'unknown')[:48]}"
        title_slug = sha256_text(title)[:12]
        return author_dir / "novels" / f"{novel_id}_{title_slug}"

    def write_text(self, path: Path, content: str) -> None:
        """原子写文本文件：tmp + os.replace。"""
        ensure_parent(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def write_bytes(self, path: Path, content: bytes) -> str:
        """原子写二进制文件：tmp + os.replace。"""
        ensure_parent(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_bytes(content)
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return hashlib.sha256(content).hexdigest()

    def download_asset(
        self,
        url: str,
        target: Path,
        timeout: int,
        verify_ssl: bool,
        proxy: str | None,
        max_retries: int = 3,
    ) -> str | None:
        """下载资源到目标路径，带 Referer、流式写入和指数退避重试。"""
        proxies = {"http": proxy, "https": proxy} if proxy else None
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                with self.session.get(
                    url,
                    timeout=timeout,
                    verify=verify_ssl,
                    proxies=proxies,
                    stream=True,
                ) as response:
                    response.raise_for_status()
                    ensure_parent(target)
                    tmp = target.with_suffix(target.suffix + ".tmp")
                    hasher = hashlib.sha256()
                    try:
                        with tmp.open("wb") as fh:
                            for chunk in response.iter_content(chunk_size=64 * 1024):
                                if not chunk:
                                    continue
                                fh.write(chunk)
                                hasher.update(chunk)
                        os.replace(tmp, target)
                    except Exception:
                        tmp.unlink(missing_ok=True)
                        raise
                    return hasher.hexdigest()
            except Exception as exc:
                last_exc = exc
                # 4xx 客户端错误（除 429）不重试
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status and 400 <= status < 500 and status != 429:
                    logger.warning("Failed to download %s (HTTP %s, no retry): %s", url, status, exc)
                    return None
                if attempt < max_retries - 1:
                    backoff = 2 ** attempt
                    logger.info("Retry %s/%s for %s after %ss: %s", attempt + 1, max_retries, url, backoff, exc)
                    time.sleep(backoff)
        logger.warning("Failed to download asset %s after %s attempts: %s", url, max_retries, last_exc)
        return None

    def asset_path(self, novel_dir: Path, asset_type: str, filename: str) -> Path:
        # 防止路径穿越
        safe_filename = Path(filename).name
        return novel_dir / "assets" / asset_type / safe_filename

    def ensure_dirs(self, paths: Iterable[Path]) -> None:
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)


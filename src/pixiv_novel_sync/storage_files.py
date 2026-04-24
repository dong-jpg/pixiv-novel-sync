from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Iterable

import requests

from .settings import Settings
from .utils_naming import ensure_parent, safe_name

logger = logging.getLogger(__name__)


class FileStorage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def base_dir(self, restrict: str) -> Path:
        return self.settings.storage.private_dir if restrict == "private" else self.settings.storage.public_dir

    def novel_dir(self, restrict: str, user_id: int, user_name: str, novel_id: int, title: str) -> Path:
        author_dir = self.base_dir(restrict) / "authors" / f"{user_id}_{safe_name(user_name, 'unknown')}"
        return author_dir / "novels" / f"{novel_id}_{safe_name(title)}"

    def write_text(self, path: Path, content: str) -> None:
        ensure_parent(path)
        path.write_text(content, encoding="utf-8")

    def write_bytes(self, path: Path, content: bytes) -> str:
        ensure_parent(path)
        path.write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    def download_asset(self, url: str, target: Path, timeout: int, verify_ssl: bool, proxy: str | None) -> str | None:
        try:
            proxies = {"http": proxy, "https": proxy} if proxy else None
            response = requests.get(url, timeout=timeout, verify=verify_ssl, proxies=proxies)
            response.raise_for_status()
            return self.write_bytes(target, response.content)
        except Exception as exc:
            logger.warning("Failed to download asset %s: %s", url, exc)
            return None

    def asset_path(self, novel_dir: Path, asset_type: str, filename: str) -> Path:
        return novel_dir / "assets" / asset_type / filename

    def ensure_dirs(self, paths: Iterable[Path]) -> None:
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)

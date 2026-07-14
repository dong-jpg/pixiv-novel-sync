from __future__ import annotations

import hashlib
import logging
import os
import shutil
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
        # 防止路径穿越：filename 取 basename，asset_type 也需消毒
        # （当前调用点是代码常量，此处为防御未来接入外部输入 L6）。
        safe_filename = Path(filename).name
        safe_type = safe_name(asset_type, fallback="asset")
        return novel_dir / "assets" / safe_type / safe_filename

    def get_novel_cover_path(self, novel_data: dict) -> Path | None:
        """根据 novel_data 重建封面文件路径。

        novel_data 应包含 user_id / author_name / novel_id / title /
        restrict_value / cover_url，与 db.get_novel_detail() 返回结构一致。
        封面缺失或字段不全时返回 None。
        """
        cover_url = (novel_data.get("cover_url") or "").strip()
        if not cover_url:
            return None
        user_id = novel_data.get("user_id")
        user_name = novel_data.get("author_name") or "unknown"
        novel_id = novel_data.get("novel_id")
        title = novel_data.get("title") or ""
        restrict = novel_data.get("restrict_value") or "public"
        if user_id is None or novel_id is None:
            return None
        from .sync.utils import _filename_from_url
        filename = _filename_from_url(cover_url)
        novel_dir = self.novel_dir(restrict, int(user_id), str(user_name), int(novel_id), str(title))
        return self.asset_path(novel_dir, "cover", filename)

    def remove_novel_archive(self, novel_dirs: Iterable[Path], asset_paths: Iterable[Path] = ()) -> dict[str, int]:
        """删除小说归档目录和已记录的散落资源文件。"""
        stats = {"dirs_removed": 0, "files_removed": 0, "missing": 0, "skipped": 0}
        removed_dirs: set[Path] = set()
        seen_dirs: set[Path] = set()
        for novel_dir in novel_dirs:
            try:
                dir_key = novel_dir.resolve() if novel_dir.exists() else novel_dir.parent.resolve() / novel_dir.name
            except OSError:
                stats["skipped"] += 1
                continue
            if dir_key in seen_dirs:
                continue
            seen_dirs.add(dir_key)
            if not self._is_inside_storage(novel_dir):
                stats["skipped"] += 1
                logger.warning("Skip deleting archive outside storage roots: %s", novel_dir)
                continue
            if not novel_dir.exists():
                stats["missing"] += 1
                continue
            resolved_dir = novel_dir.resolve()
            if resolved_dir.is_dir():
                shutil.rmtree(resolved_dir)
                removed_dirs.add(resolved_dir)
                stats["dirs_removed"] += 1
            elif resolved_dir.is_file():
                resolved_dir.unlink()
                stats["files_removed"] += 1

        for asset_path in asset_paths:
            try:
                asset_key = asset_path.resolve() if asset_path.exists() else asset_path.parent.resolve() / asset_path.name
            except OSError:
                stats["skipped"] += 1
                continue
            if any(asset_key.is_relative_to(directory) for directory in removed_dirs):
                continue
            if not self._is_inside_storage(asset_path):
                stats["skipped"] += 1
                logger.warning("Skip deleting asset outside storage roots: %s", asset_path)
                continue
            if not asset_path.exists():
                stats["missing"] += 1
                continue
            resolved_asset = asset_path.resolve()
            if resolved_asset.is_file():
                resolved_asset.unlink()
                stats["files_removed"] += 1
        return stats

    def ensure_dirs(self, paths: Iterable[Path]) -> None:
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)

    def _is_inside_storage(self, path: Path) -> bool:
        try:
            candidate = path.resolve() if path.exists() else path.parent.resolve() / path.name
        except OSError:
            return False
        roots = (self.settings.storage.public_dir.resolve(), self.settings.storage.private_dir.resolve())
        return any(candidate == root or candidate.is_relative_to(root) for root in roots)

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from pixivpy3 import AppPixivAPI

from .models import NovelRecord, NovelTextRecord, SourceRecord, UserRecord
from .settings import Settings
from .storage_db import Database
from .storage_files import FileStorage
from .utils_hashing import sha256_text, stable_json_dumps
from .utils_text import clean_caption, normalize_text, to_markdown

logger = logging.getLogger(__name__)


class BookmarkNovelSyncService:
    def __init__(self, api: AppPixivAPI, db: Database, storage: FileStorage, settings: Settings) -> None:
        self.api = api
        self.db = db
        self.storage = storage
        self.settings = settings

    def sync(self, user_id: int, restricts: Iterable[str], download_assets: bool = True, write_markdown: bool = True, write_raw_text: bool = True) -> dict[str, int]:
        stats = {
            "users": 0,
            "novels": 0,
            "texts_updated": 0,
            "assets_downloaded": 0,
        }
        max_items = self.settings.sync.max_items_per_run
        max_pages = self.settings.sync.max_pages_per_run
        item_delay = self.settings.sync.delay_seconds_between_items
        page_delay = self.settings.sync.delay_seconds_between_pages
        processed_items = 0

        for restrict in restricts:
            logger.info("Syncing bookmarked novels for restrict=%s", restrict)
            next_query: dict[str, Any] | None = {"user_id": user_id, "restrict": restrict}
            page_count = 0
            while next_query:
                if max_pages is not None and page_count >= max_pages:
                    logger.info("Reached max_pages_per_run=%s, stopping pagination", max_pages)
                    return stats
                result = self.api.user_bookmarks_novel(**next_query)
                page_count += 1
                novels = getattr(result, "novels", []) or []
                for novel in novels:
                    if max_items is not None and processed_items >= max_items:
                        logger.info("Reached max_items_per_run=%s, stopping sync", max_items)
                        return stats
                    counters = self._sync_novel(novel, restrict, download_assets, write_markdown, write_raw_text)
                    processed_items += 1
                    for key, value in counters.items():
                        stats[key] = stats.get(key, 0) + value
                    if item_delay > 0:
                        time.sleep(item_delay)
                next_query = self.api.parse_qs(getattr(result, "next_url", None))
                if next_query and page_delay > 0:
                    time.sleep(page_delay)
        return stats

    def _sync_novel(self, novel: Any, restrict: str, download_assets: bool, write_markdown: bool, write_raw_text: bool) -> dict[str, int]:
        novel_id = int(novel.id)
        detail = self.api.novel_detail(novel_id)
        detail_novel = getattr(detail, "novel", novel)
        user = getattr(detail_novel, "user", None)
        user_id = int(user.id)
        user_name = getattr(user, "name", "unknown")
        account = getattr(user, "account", None)

        self.db.upsert_user(
            UserRecord(
                user_id=user_id,
                name=user_name,
                account=account,
                raw_json=stable_json_dumps(_to_plain(user)),
            )
        )

        caption = clean_caption(getattr(detail_novel, "caption", None))
        tags_json = stable_json_dumps(_extract_tags(getattr(detail_novel, "tags", [])))
        meta_plain = _to_plain(detail_novel)
        meta_hash = sha256_text(stable_json_dumps(meta_plain))
        cover_url = _extract_cover_url(detail_novel)
        series = getattr(detail_novel, "series", None)
        series_id = int(series.id) if getattr(series, "id", None) else None
        title = getattr(detail_novel, "title", f"novel_{novel_id}")

        self.db.upsert_novel(
            NovelRecord(
                novel_id=novel_id,
                user_id=user_id,
                series_id=series_id,
                title=title,
                caption=caption,
                visible=bool(getattr(detail_novel, "visible", True)),
                restrict=restrict,
                x_restrict=int(getattr(detail_novel, "x_restrict", 0) or 0),
                text_length=int(getattr(detail_novel, "text_length", 0) or 0),
                total_bookmarks=int(getattr(detail_novel, "total_bookmarks", 0) or 0),
                total_views=int(getattr(detail_novel, "total_view", 0) or 0),
                cover_url=cover_url,
                tags_json=tags_json,
                create_date=getattr(detail_novel, "create_date", None),
                raw_json=stable_json_dumps(meta_plain),
                meta_hash=meta_hash,
            )
        )
        self.db.upsert_source(SourceRecord(novel_id=novel_id, source_type=f"bookmark_{restrict}", source_key=str(user_id)))

        webview = self.api.webview_novel(novel_id)
        body = normalize_text(_extract_novel_text(webview))
        text_hash = sha256_text(body)
        markdown_text = to_markdown(title, user_name, caption, body) if write_markdown else None

        self.db.upsert_novel_text(
            NovelTextRecord(
                novel_id=novel_id,
                text_raw=body,
                text_markdown=markdown_text,
                text_hash=text_hash,
            )
        )
        self.db.replace_fts(novel_id, title, caption, user_name, body)

        novel_dir = self.storage.novel_dir(restrict, user_id, user_name, novel_id, title)
        self.storage.write_text(novel_dir / "meta.json", json.dumps(meta_plain, ensure_ascii=False, indent=2))
        if write_raw_text:
            self.storage.write_text(novel_dir / "text.txt", body)
        if write_markdown and markdown_text is not None:
            self.storage.write_text(novel_dir / "text.md", markdown_text)

        assets_downloaded = 0
        if download_assets:
            for asset_type, asset_url in _collect_asset_urls(detail_novel, webview):
                filename = _filename_from_url(asset_url)
                target = self.storage.asset_path(novel_dir, asset_type, filename)
                file_hash = self.storage.download_asset(
                    asset_url,
                    target,
                    timeout=self.settings.pixiv.timeout,
                    verify_ssl=self.settings.pixiv.verify_ssl,
                    proxy=self.settings.pixiv.proxy,
                )
                if file_hash:
                    self.db.record_asset(novel_id, asset_type, asset_url, str(target), file_hash)
                    assets_downloaded += 1

        return {
            "users": 1,
            "novels": 1,
            "texts_updated": 1,
            "assets_downloaded": assets_downloaded,
        }


def _to_plain(value: Any) -> Any:
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
    results: list[dict[str, Any]] = []
    for tag in tags or []:
        results.append(_to_plain(tag))
    return results


def _extract_cover_url(novel: Any) -> str | None:
    image_urls = getattr(novel, "image_urls", None)
    for field in ("large", "medium", "square_medium"):
        url = getattr(image_urls, field, None) if image_urls is not None else None
        if url:
            return str(url)
    return None


def _extract_novel_text(webview: Any) -> str:
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


def _collect_asset_urls(novel: Any, webview: Any) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    cover_url = _extract_cover_url(novel)
    if cover_url:
        results.append(("cover", cover_url))

    plain_webview = _to_plain(webview)
    visited: set[str] = set()
    for url in _walk_urls(plain_webview):
        if "pximg.net" in url and url not in visited:
            visited.add(url)
            results.append(("inline_image", url))
    return results


def _walk_urls(value: Any) -> list[str]:
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
    path = urlparse(url).path
    name = Path(path).name
    return name or "asset.bin"

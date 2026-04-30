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

    def sync(self, user_id: int, restricts: Iterable[str], download_assets: bool = True, write_markdown: bool = True, write_raw_text: bool = True, progress_callback: Any = None, phase_name: str = "同步中") -> dict[str, int]:
        stats = self._empty_stats()
        max_items = self.settings.sync.max_items_per_run
        max_pages = self.settings.sync.max_pages_per_run
        item_delay = self.settings.sync.delay_seconds_between_items
        page_delay = self.settings.sync.delay_seconds_between_pages
        processed_items = 0

        for restrict in restricts:
            logger.info("Syncing bookmarked novels for restrict=%s", restrict)
            if progress_callback:
                progress_callback("page", {"page": 1, "restrict": restrict})
            next_query: dict[str, Any] | None = {"user_id": user_id, "restrict": restrict}
            page_count = 0
            while next_query:
                if max_pages is not None and page_count >= max_pages:
                    logger.info("Reached max_pages_per_run=%s, stopping pagination", max_pages)
                    return stats
                result = self.api.user_bookmarks_novel(**next_query)
                page_count += 1
                if progress_callback:
                    progress_callback("page", {"page": page_count, "restrict": restrict})
                novels = getattr(result, "novels", []) or []
                for novel in novels:
                    if max_items is not None and processed_items >= max_items:
                        logger.info("Reached max_items_per_run=%s, stopping sync", max_items)
                        return stats
                    processed_items += 1
                    novel_id = int(novel.id)
                    title = getattr(novel, "title", f"novel_{novel_id}")
                    user = getattr(novel, "user", None)
                    author_name = getattr(user, "name", "未知") if user else "未知"
                    
                    if progress_callback:
                        progress_callback("novel_start", {
                            "current": processed_items,
                            "total": max_items or 50,
                            "novel_id": novel_id,
                            "title": title,
                            "author": author_name,
                            "phase": phase_name,
                        })
                    
                    counters = self._sync_novel(
                        novel,
                        restrict,
                        download_assets,
                        write_markdown,
                        write_raw_text,
                        source_type=f"bookmark_{restrict}",
                    )
                    self._merge_stats(stats, counters)
                    
                    if progress_callback:
                        progress_callback("novel_done", {
                            "novel_id": novel_id,
                            "title": title,
                            "bookmarks": counters.get("bookmarks", 0),
                            "views": counters.get("views", 0),
                            "assets": counters.get("assets_downloaded", 0),
                        })
                    
                    if item_delay > 0:
                        if progress_callback:
                            progress_callback("rate_limit", {"seconds": item_delay})
                        time.sleep(item_delay)
                next_query = self.api.parse_qs(getattr(result, "next_url", None))
                if next_query and page_delay > 0:
                    if progress_callback:
                        progress_callback("rate_limit", {"seconds": page_delay})
                    time.sleep(page_delay)
        return stats

    def sync_following_novels(self, download_assets: bool = True, write_markdown: bool = True, write_raw_text: bool = True, progress_callback: Any = None, novels_only: bool = False) -> dict[str, int]:
        stats = self._empty_stats()
        max_items = self.settings.sync.max_items_per_run
        max_pages = self.settings.sync.max_pages_per_run
        item_delay = self.settings.sync.delay_seconds_between_items
        page_delay = self.settings.sync.delay_seconds_between_pages
        processed_items = 0

        logger.info("Syncing novels from followed users")
        current_user_id = self.settings.pixiv.user_id
        if not current_user_id:
            raise RuntimeError("PIXIV_USER_ID is required to fetch following list")
        next_following_query: dict[str, Any] | None = {"user_id": current_user_id, "restrict": "public"}
        following_page_count = 0

        while next_following_query:
            if max_pages is not None and following_page_count >= max_pages:
                logger.info("Reached max_pages_per_run=%s while scanning followed users", max_pages)
                return stats

            following_result = self.api.user_following(**next_following_query)
            following_page_count += 1
            if progress_callback:
                progress_callback("page", {"page": following_page_count})
            users = getattr(following_result, "user_previews", []) or []

            for user_preview in users:
                user = getattr(user_preview, "user", user_preview)
                author_id = getattr(user, "id", None)
                if author_id is None:
                    continue
                author_id = int(author_id)
                author_name = getattr(user, "name", str(author_id))
                stats["following_users_scanned"] = stats.get("following_users_scanned", 0) + 1
                
                # 保存用户信息
                if not novels_only:
                    from .models import UserRecord
                    from .utils_hashing import stable_json_dumps
                    account = getattr(user, "account", None)
                    self.db.upsert_user(UserRecord(
                        user_id=author_id,
                        name=author_name,
                        account=account,
                        raw_json=stable_json_dumps(_to_plain(user)),
                    ))
                    stats["users"] = stats.get("users", 0) + 1

                logger.info("Syncing followed user novels for user_id=%s name=%s", author_id, author_name)

                next_novel_query: dict[str, Any] | None = {"user_id": author_id}
                author_page_count = 0
                while next_novel_query:
                    if max_pages is not None and author_page_count >= max_pages:
                        logger.info("Reached max_pages_per_run=%s while scanning user_id=%s", max_pages, author_id)
                        break

                    novels_result = self.api.user_novels(**next_novel_query)
                    author_page_count += 1
                    novels = getattr(novels_result, "novels", []) or []

                    for novel in novels:
                        if max_items is not None and processed_items >= max_items:
                            logger.info("Reached max_items_per_run=%s during followed user scan", max_items)
                            return stats
                        processed_items += 1
                        novel_id = int(novel.id)
                        title = getattr(novel, "title", f"novel_{novel_id}")
                        
                        if progress_callback:
                            progress_callback("novel_start", {
                                "current": processed_items,
                                "total": max_items or 50,
                                "novel_id": novel_id,
                                "title": title,
                                "author": author_name,
                                "phase": "同步用户小说",
                            })
                        
                        counters = self._sync_novel(
                            novel,
                            getattr(novel, "restrict", "public") or "public",
                            download_assets,
                            write_markdown,
                            write_raw_text,
                            source_type="following_user_scan",
                            source_key=str(author_id),
                        )
                        self._merge_stats(stats, counters)
                        
                        if progress_callback:
                            progress_callback("novel_done", {
                                "novel_id": novel_id,
                                "title": title,
                                "bookmarks": counters.get("bookmarks", 0),
                                "views": counters.get("views", 0),
                                "assets": counters.get("assets_downloaded", 0),
                            })
                        
                        if item_delay > 0:
                            if progress_callback:
                                progress_callback("rate_limit", {"seconds": item_delay})
                            time.sleep(item_delay)

                    next_novel_query = self.api.parse_qs(getattr(novels_result, "next_url", None))
                    if next_novel_query and page_delay > 0:
                        if progress_callback:
                            progress_callback("rate_limit", {"seconds": page_delay})
                        time.sleep(page_delay)

            next_following_query = self.api.parse_qs(getattr(following_result, "next_url", None))
            if next_following_query and page_delay > 0:
                if progress_callback:
                    progress_callback("rate_limit", {"seconds": page_delay})
                time.sleep(page_delay)
        return stats

    def sync_subscribed_series(self, progress_callback: Any = None) -> dict[str, int]:
        """获取用户订阅的系列列表（从 watchlist 页面）"""
        stats = {"series_synced": 0}
        
        logger.info("Fetching subscribed series list")
        if progress_callback:
            progress_callback("phase", {"phase": "获取订阅系列"})
        
        # 清除旧的订阅标记
        self.db.clear_subscribed_series()
        
        series_list = []
        
        # 方式1: 使用 Pixiv Web Cookie 调用 Web API
        web_cookie = self.settings.pixiv.web_cookie
        if web_cookie:
            try:
                import requests as http_requests
                
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "application/json",
                    "Accept-Language": "zh_CN",
                    "Referer": "https://www.pixiv.net/following/watchlist/novels",
                    "Cookie": web_cookie,
                }
                
                proxies = {"http": self.settings.pixiv.proxy, "https": self.settings.pixiv.proxy} if self.settings.pixiv.proxy else None
                
                # 尝试多个可能的 API 端点
                api_endpoints = [
                    "https://www.pixiv.net/ajax/watch_list/novel?p=1&new=1&lang=zh",
                    "https://www.pixiv.net/ajax/watch_list/novel?p=1&lang=zh",
                ]
                
                for endpoint in api_endpoints:
                    try:
                        logger.info("Trying Web API: %s", endpoint)
                        response = http_requests.get(
                            endpoint, headers=headers, proxies=proxies,
                            timeout=self.settings.pixiv.timeout,
                            verify=self.settings.pixiv.verify_ssl,
                        )
                        logger.info("Web API %s returned status: %s", endpoint, response.status_code)
                        if response.status_code == 200:
                            data = response.json()
                            if data.get("error"):
                                logger.warning("Web API returned error: %s", data.get("message", "unknown"))
                                continue
                            body = data.get("body", {})
                            # 尝试多种数据结构
                            if isinstance(body, dict):
                                series_list = body.get("seriesList", body.get("novel_series_list", body.get("watchList", [])))
                                # 如果 body 本身就是列表的容器
                                if not series_list:
                                    for key, val in body.items():
                                        if isinstance(val, list) and len(val) > 0:
                                            series_list = val
                                            logger.info("Found series list under key '%s'", key)
                                            break
                            elif isinstance(body, list):
                                series_list = body
                            if series_list:
                                logger.info("Found %d subscribed series from %s", len(series_list), endpoint)
                                break
                            else:
                                logger.info("Response body keys: %s", list(body.keys()) if isinstance(body, dict) else type(body).__name__)
                                logger.info("Response body preview: %s", str(data)[:800])
                    except Exception as e:
                        logger.warning("Web API %s failed: %s", endpoint, str(e))
                
            except Exception as e:
                logger.warning("Web API failed: %s", str(e))
        else:
            logger.info("No PIXIV_WEB_COOKIE configured, skipping Web API")
        
        # 方式2: 从已同步的小说中提取系列
        if not series_list:
            logger.info("Extracting series from synced novels")
            try:
                rows = self.db.conn.execute(
                    """
                    SELECT DISTINCT n.series_id, 
                           COALESCE(se.title, MIN(n.title)) as title,
                           se.description,
                           n.user_id,
                           u.name as author_name,
                           se.cover_url,
                           COUNT(*) as total_novels
                    FROM novels n
                    LEFT JOIN series se ON se.series_id = n.series_id
                    LEFT JOIN users u ON u.user_id = n.user_id
                    WHERE n.series_id IS NOT NULL
                    GROUP BY n.series_id
                    ORDER BY MAX(n.last_seen_at) DESC
                    """
                ).fetchall()
                
                for row in rows:
                    series_id = row[0]
                    self.db.upsert_subscribed_series(
                        series_id=series_id,
                        title=row[1] or "",
                        description=row[2] or "",
                        user_id=row[3] or 0,
                        cover_url=row[5],
                        total_novels=row[6] or 0,
                    )
                    stats["series_synced"] += 1
                    logger.info("Synced series from DB: %s (ID: %s)", row[1], series_id)
                
            except Exception as e:
                logger.warning("Failed to extract series from DB: %s", str(e))
        
        logger.info("Subscribed series sync completed: %d series", stats["series_synced"])
        return stats

    def _sync_novel(
        self,
        novel: Any,
        restrict: str,
        download_assets: bool,
        write_markdown: bool,
        write_raw_text: bool,
        source_type: str,
        source_key: str | None = None,
    ) -> dict[str, int]:
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
        self.db.upsert_source(SourceRecord(novel_id=novel_id, source_type=source_type, source_key=source_key or str(user_id)))

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
            "bookmarks": int(getattr(detail_novel, "total_bookmarks", 0) or 0),
            "views": int(getattr(detail_novel, "total_view", 0) or 0),
        }

    @staticmethod
    def _empty_stats() -> dict[str, int]:
        return {
            "users": 0,
            "novels": 0,
            "texts_updated": 0,
            "assets_downloaded": 0,
            "following_users_scanned": 0,
        }

    @staticmethod
    def _merge_stats(stats: dict[str, int], counters: dict[str, int]) -> None:
        for key, value in counters.items():
            stats[key] = stats.get(key, 0) + value


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

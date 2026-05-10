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

    def check_bookmarks_existence(self, user_id: int, restricts: Iterable[str], progress_callback: Any = None) -> dict[str, int]:
        """预检查：获取全部收藏列表，标记哪些已存在本地"""
        stats = {"total_checked": 0, "existing": 0, "new": 0}
        
        # 初始化检查表
        self.db.init_sync_check_table()
        self.db.clear_sync_check_list()
        
        if progress_callback:
            progress_callback("phase", {"phase": "检查收藏列表"})
        
        all_novel_ids = []
        
        # 第一步：获取全部收藏列表
        for restrict in restricts:
            if progress_callback:
                progress_callback("page", {"page": 1, "restrict": restrict})
            
            next_query: dict[str, Any] | None = {"user_id": user_id, "restrict": restrict}
            page_count = 0
            
            while next_query:
                result = self.api.user_bookmarks_novel(**next_query)
                page_count += 1
                
                if progress_callback:
                    progress_callback("page", {"page": page_count, "restrict": restrict})
                
                novels = getattr(result, "novels", []) or []
                for novel in novels:
                    novel_id = int(novel.id)
                    all_novel_ids.append(novel_id)
                
                next_query = self.api.parse_qs(getattr(result, "next_url", None))
                if next_query:
                    time.sleep(self.settings.sync.delay_seconds_between_pages)
        
        if progress_callback:
            progress_callback("phase", {"phase": f"检查 {len(all_novel_ids)} 本小说"})
        
        # 第二步：批量检查哪些已存在
        existing_ids = self.db.get_existing_novel_ids(all_novel_ids)
        
        # 第三步：标记并保存结果
        for novel_id in all_novel_ids:
            exists = novel_id in existing_ids
            self.db.upsert_sync_check_item(novel_id, exists)
            stats["total_checked"] += 1
            if exists:
                stats["existing"] += 1
            else:
                stats["new"] += 1
        
        if progress_callback:
            progress_callback("phase", {"phase": f"检查完成: {stats['new']} 本新小说, {stats['existing']} 本已存在"})
        
        logger.info("Sync check completed: %d total, %d existing, %d new", 
                    stats["total_checked"], stats["existing"], stats["new"])
        
        return stats

    def check_all_existence(self, user_id: int, restricts: Iterable[str], progress_callback: Any = None) -> dict[str, Any]:
        """预检查：获取所有需要同步的内容，标记哪些已存在本地"""
        stats = {
            "total_checked": 0, 
            "existing": 0, 
            "new": 0,
            "bookmarks": {"total": 0, "existing": 0, "new": 0},
            "following_novels": {"total": 0, "existing": 0, "new": 0},
            "subscribed_series": {"total": 0, "existing": 0, "new": 0},
        }
        
        # 初始化检查表
        self.db.init_sync_check_table()
        self.db.clear_sync_check_list()
        
        all_novel_ids = []
        bookmark_ids: list[int] = []
        following_ids: list[int] = []

        # 1. 检查收藏列表
        if self.settings.sync.sync_bookmarks:
            if progress_callback:
                progress_callback("phase", {"phase": "检查收藏列表"})
            
            for restrict in restricts:
                if progress_callback:
                    progress_callback("page", {"page": 1, "restrict": restrict})
                
                next_query: dict[str, Any] | None = {"user_id": user_id, "restrict": restrict}
                page_count = 0
                
                while next_query:
                    result = self.api.user_bookmarks_novel(**next_query)
                    page_count += 1
                    
                    if progress_callback:
                        progress_callback("page", {"page": page_count, "restrict": restrict})
                    
                    novels = getattr(result, "novels", []) or []
                    for novel in novels:
                        novel_id = int(novel.id)
                        bookmark_ids.append(novel_id)
                        all_novel_ids.append(novel_id)
                    
                    next_query = self.api.parse_qs(getattr(result, "next_url", None))
                    if next_query:
                        time.sleep(self.settings.sync.delay_seconds_between_pages)
            
            stats["bookmarks"]["total"] = len(bookmark_ids)
            if progress_callback:
                progress_callback("phase", {"phase": f"收藏: {len(bookmark_ids)} 本"})
        
        # 2. 检查关注用户的小说
        if self.settings.sync.sync_following_novels:
            if progress_callback:
                progress_callback("phase", {"phase": "检查关注用户小说"})
            
            current_user_id = self.settings.pixiv.user_id
            if current_user_id:
                next_following_query: dict[str, Any] | None = {"user_id": current_user_id, "restrict": "public"}
                following_page_count = 0
                
                while next_following_query:
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
                        
                        # 获取该用户的小说
                        next_novel_query: dict[str, Any] | None = {"user_id": author_id}
                        while next_novel_query:
                            novels_result = self.api.user_novels(**next_novel_query)
                            novels = getattr(novels_result, "novels", []) or []
                            
                            for novel in novels:
                                novel_id = int(novel.id)
                                following_ids.append(novel_id)
                                all_novel_ids.append(novel_id)
                            
                            next_novel_query = self.api.parse_qs(getattr(novels_result, "next_url", None))
                            if next_novel_query:
                                time.sleep(self.settings.sync.delay_seconds_between_pages)
                    
                    next_following_query = self.api.parse_qs(getattr(following_result, "next_url", None))
                    if next_following_query:
                        time.sleep(self.settings.sync.delay_seconds_between_pages)
            
            stats["following_novels"]["total"] = len(following_ids)
            if progress_callback:
                progress_callback("phase", {"phase": f"关注用户: {len(following_ids)} 本"})
        
        # 3. 检查追更系列
        if self.settings.sync.sync_subscribed_series:
            if progress_callback:
                progress_callback("phase", {"phase": "检查追更系列"})
            
            series_ids = []
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
                    
                    # 获取追更系列列表
                    endpoint = "https://www.pixiv.net/ajax/watch_list/novel?p=1&new=1&lang=zh"
                    response = http_requests.get(
                        endpoint, headers=headers, proxies=proxies,
                        timeout=self.settings.pixiv.timeout,
                        verify=self.settings.pixiv.verify_ssl,
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        body = data.get("body", {})
                        page_info = body.get("page", {})
                        watched_ids = page_info.get("watchedSeriesIds", [])
                        
                        # 获取每个系列的小说
                        for series_id in watched_ids:
                            series_detail = self.api.novel_series_detail(series_id)
                            if hasattr(series_detail, "novel_series"):
                                series = series_detail.novel_series
                                # 系列中的小说会在同步时获取
                                # 这里只记录系列 ID
                                series_ids.append(series_id)
                        
                        # 处理分页
                        max_page = page_info.get("maxPage", 1)
                        if max_page > 1:
                            for page_num in range(2, max_page + 1):
                                try:
                                    paged_url = endpoint.replace("p=1", f"p={page_num}")
                                    paged_response = http_requests.get(
                                        paged_url, headers=headers, proxies=proxies,
                                        timeout=self.settings.pixiv.timeout,
                                        verify=self.settings.pixiv.verify_ssl,
                                    )
                                    if paged_response.status_code == 200:
                                        paged_data = paged_response.json()
                                        paged_body = paged_data.get("body", {})
                                        paged_page_info = paged_body.get("page", {})
                                        paged_watched_ids = paged_page_info.get("watchedSeriesIds", [])
                                        series_ids.extend(paged_watched_ids)
                                except Exception as e:
                                    logger.warning("Failed to fetch watchlist page %d: %s", page_num, e)
                except Exception as e:
                    logger.warning("Failed to fetch subscribed series: %s", e)
            
            stats["subscribed_series"]["total"] = len(series_ids)
            if progress_callback:
                progress_callback("phase", {"phase": f"追更系列: {len(series_ids)} 个"})
        
        # 批量检查哪些已存在
        if progress_callback:
            progress_callback("phase", {"phase": f"检查 {len(all_novel_ids)} 本小说"})
        
        existing_ids = self.db.get_existing_novel_ids(all_novel_ids)
        
        # 标记并保存结果
        for novel_id in all_novel_ids:
            exists = novel_id in existing_ids
            self.db.upsert_sync_check_item(novel_id, exists)
            stats["total_checked"] += 1
            if exists:
                stats["existing"] += 1
            else:
                stats["new"] += 1
        
        # 更新各分类统计
        stats["bookmarks"]["existing"] = len([nid for nid in bookmark_ids if nid in existing_ids])
        stats["bookmarks"]["new"] = stats["bookmarks"]["total"] - stats["bookmarks"]["existing"]

        stats["following_novels"]["existing"] = len([nid for nid in following_ids if nid in existing_ids])
        stats["following_novels"]["new"] = stats["following_novels"]["total"] - stats["following_novels"]["existing"]
        
        if progress_callback:
            progress_callback("phase", {"phase": f"检查完成: {stats['new']} 本新小说, {stats['existing']} 本已存在"})
        
        logger.info("Sync check completed: %d total, %d existing, %d new", 
                    stats["total_checked"], stats["existing"], stats["new"])
        
        return stats

    def sync(self, user_id: int, restricts: Iterable[str], download_assets: bool = True, write_markdown: bool = True, write_raw_text: bool = True, progress_callback: Any = None, phase_name: str = "同步中") -> dict[str, int]:
        stats = self._empty_stats()
        max_items = self.settings.sync.max_items_per_run
        max_pages = self.settings.sync.max_pages_per_run
        item_delay = self.settings.sync.delay_seconds_between_items
        page_delay = self.settings.sync.delay_seconds_between_pages
        processed_items = 0
        synced_items = 0  # 实际同步的数量（不包括跳过的）
        
        # 获取预检查结果
        check_list = self.db.get_sync_check_list()
        use_check_list = len(check_list) > 0

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
                    # 检查是否达到实际同步数量限制
                    if max_items is not None and synced_items >= max_items:
                        logger.info("Reached max_items_per_run=%s (synced), stopping sync", max_items)
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
                    
                    # 使用预检查结果判断是否跳过
                    should_skip = False
                    if use_check_list and novel_id in check_list:
                        should_skip = check_list[novel_id]
                    
                    if should_skip:
                        # 已存在，跳过
                        self.db.upsert_source(SourceRecord(novel_id=novel_id, source_type=f"bookmark_{restrict}", source_key=str(user_id)))
                        self.db.touch_novel(novel_id)
                        counters = {"skipped": 1, "bookmarks": 0, "views": 0, "assets_downloaded": 0}
                        # 跳过时使用跳过间隔
                        skip_delay = self.settings.sync.delay_seconds_between_skips
                        if skip_delay > 0:
                            if progress_callback:
                                progress_callback("rate_limit", {"seconds": skip_delay})
                            time.sleep(skip_delay)
                    else:
                        # 不存在或无预检查结果，执行完整同步
                        counters = self._sync_novel(
                            novel,
                            restrict,
                            download_assets,
                            write_markdown,
                            write_raw_text,
                            source_type=f"bookmark_{restrict}",
                        )
                        # 同步成功后更新检查列表
                        if use_check_list:
                            self.db.upsert_sync_check_item(novel_id, True)
                    
                    self._merge_stats(stats, counters)
                    
                    # 只有实际同步（非跳过）才计入 synced_items
                    if not counters.get("skipped"):
                        synced_items += 1
                    
                    if progress_callback:
                        progress_callback("novel_done", {
                            "novel_id": novel_id,
                            "title": title,
                            "bookmarks": counters.get("bookmarks", 0),
                            "views": counters.get("views", 0),
                            "assets": counters.get("assets_downloaded", 0),
                            "skipped": counters.get("skipped", 0),
                        })
                    
                    # 只有非跳过时才使用 item_delay
                    if not counters.get("skipped") and item_delay > 0:
                        if progress_callback:
                            progress_callback("rate_limit", {"seconds": item_delay})
                        time.sleep(item_delay)
                next_query = self.api.parse_qs(getattr(result, "next_url", None))
                if next_query and page_delay > 0:
                    if progress_callback:
                        progress_callback("rate_limit", {"seconds": page_delay})
                    time.sleep(page_delay)
        return stats

    def sync_following_list(self, progress_callback: Any = None) -> dict[str, int]:
        """同步关注用户列表（只更新用户信息，不同步小说）"""
        stats = {"users": 0, "following_users_scanned": 0}
        page_delay = self.settings.sync.delay_seconds_between_pages
        
        logger.info("Syncing following user list")
        current_user_id = self.settings.pixiv.user_id
        if not current_user_id:
            raise RuntimeError("PIXIV_USER_ID is required to fetch following list")
        
        next_following_query: dict[str, Any] | None = {"user_id": current_user_id, "restrict": "public"}
        following_page_count = 0
        
        while next_following_query:
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
                stats["following_users_scanned"] += 1
                
                from .models import UserRecord
                from .utils_hashing import stable_json_dumps
                account = getattr(user, "account", None)
                self.db.upsert_user(UserRecord(
                    user_id=author_id,
                    name=author_name,
                    account=account,
                    raw_json=stable_json_dumps(_to_plain(user)),
                ))
                stats["users"] += 1
                
                if progress_callback:
                    progress_callback("user_synced", {
                        "user_id": author_id,
                        "name": author_name,
                        "total": stats["users"],
                    })
            
            next_following_query = self.api.parse_qs(getattr(following_result, "next_url", None))
            if next_following_query and page_delay > 0:
                if progress_callback:
                    progress_callback("rate_limit", {"seconds": page_delay})
                time.sleep(page_delay)
        
        return stats

    def sync_following_novels(self, download_assets: bool = True, write_markdown: bool = True, write_raw_text: bool = True, progress_callback: Any = None, users_limit: int = 0) -> dict[str, int]:
        stats = self._empty_stats()
        max_items = self.settings.sync.max_items_per_run
        max_pages = self.settings.sync.max_pages_per_run
        item_delay = self.settings.sync.delay_seconds_between_items
        page_delay = self.settings.sync.delay_seconds_between_pages
        users_processed = 0  # 已处理的用户数
        synced_items = 0  # 实际同步的数量（不包括跳过的）

        logger.info("Syncing novels from followed users (users_limit=%d)", users_limit)
        current_user_id = self.settings.pixiv.user_id
        if not current_user_id:
            raise RuntimeError("PIXIV_USER_ID is required to fetch following list")
        next_following_query: dict[str, Any] | None = {"user_id": current_user_id, "restrict": "public"}
        following_page_count = 0

        # 获取预检查结果
        check_list = self.db.get_sync_check_list()
        use_check_list = len(check_list) > 0

        while next_following_query:
            # 检查是否达到最大页数限制
            if max_pages is not None and following_page_count >= max_pages:
                logger.info("Reached max_pages_per_run=%s, stopping pagination", max_pages)
                return stats
            following_result = self.api.user_following(**next_following_query)
            following_page_count += 1
            if progress_callback:
                progress_callback("page", {"page": following_page_count})
            users = getattr(following_result, "user_previews", []) or []

            for user_preview in users:
                # 检查是否达到用户数限制
                if users_limit > 0 and users_processed >= users_limit:
                    logger.info("Reached users_limit=%d, stopping sync", users_limit)
                    return stats
                # 检查是否达到实际同步数量限制
                if max_items is not None and synced_items >= max_items:
                    logger.info("Reached max_items_per_run=%s (synced), stopping sync", max_items)
                    return stats
                
                user = getattr(user_preview, "user", user_preview)
                author_id = getattr(user, "id", None)
                if author_id is None:
                    continue
                author_id = int(author_id)
                author_name = getattr(user, "name", str(author_id))
                stats["following_users_scanned"] = stats.get("following_users_scanned", 0) + 1
                users_processed += 1

                logger.info("Syncing followed user novels for user_id=%s name=%s", author_id, author_name)

                next_novel_query: dict[str, Any] | None = {"user_id": author_id}
                author_page_count = 0
                while next_novel_query:
                    novels_result = self.api.user_novels(**next_novel_query)
                    author_page_count += 1
                    novels = getattr(novels_result, "novels", []) or []

                    for novel in novels:
                        novel_id = int(novel.id)
                        title = getattr(novel, "title", f"novel_{novel_id}")
                        
                        if progress_callback:
                            progress_callback("novel_start", {
                                "current": users_processed,
                                "total": users_limit or 0,
                                "novel_id": novel_id,
                                "title": title,
                                "author": author_name,
                                "phase": "同步用户小说",
                            })
                        
                        # 使用预检查结果判断是否跳过
                        should_skip = False
                        if use_check_list and novel_id in check_list:
                            should_skip = check_list[novel_id]
                        
                        if should_skip:
                            # 已存在，跳过
                            counters = {"skipped": 1, "bookmarks": 0, "views": 0, "assets_downloaded": 0}
                            skip_delay = self.settings.sync.delay_seconds_between_skips
                            if skip_delay > 0:
                                if progress_callback:
                                    progress_callback("rate_limit", {"seconds": skip_delay})
                                time.sleep(skip_delay)
                        else:
                            counters = self._sync_novel(
                                novel,
                                getattr(novel, "restrict", "public") or "public",
                                download_assets,
                                write_markdown,
                                write_raw_text,
                                source_type="following_user_scan",
                                source_key=str(author_id),
                            )
                            if use_check_list:
                                self.db.upsert_sync_check_item(novel_id, True)
                        
                        self._merge_stats(stats, counters)

                        # 只有实际同步（非跳过）才计入 synced_items
                        if not counters.get("skipped"):
                            synced_items += 1
                        
                        if progress_callback:
                            progress_callback("novel_done", {
                                "novel_id": novel_id,
                                "title": title,
                                "bookmarks": counters.get("bookmarks", 0),
                                "views": counters.get("views", 0),
                                "assets": counters.get("assets_downloaded", 0),
                                "skipped": counters.get("skipped", 0),
                            })
                        
                        if not counters.get("skipped") and item_delay > 0:
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

    def sync_subscribed_series(self, progress_callback: Any = None, limit: int = 0) -> dict[str, int]:
        """获取用户订阅的系列列表（从 watchlist 页面）"""
        stats = {"series_synced": 0}
        
        logger.info("Fetching subscribed series list (limit=%d)", limit)
        if progress_callback:
            progress_callback("phase", {"phase": "获取订阅系列"})
        
        # 不再清除旧的订阅标记，而是在最后统一更新
        # self.db.clear_subscribed_series()
        
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
                            # 记录关键字段结构
                            page_info = body.get("page", {})
                            thumbnails = body.get("thumbnails", {})
                            novel_series_thumbs = thumbnails.get("novelSeries", {})
                            watched_ids = page_info.get("watchedSeriesIds", [])
                            logger.info("page.total=%s, page.maxPage=%s, watchedSeriesIds count=%d",
                                       page_info.get("total"), page_info.get("maxPage"), len(watched_ids))
                            # novelSeries 可能是 dict {id: url} 也可能是 list
                            if isinstance(novel_series_thumbs, list):
                                logger.info("thumbnails.novelSeries is list with %d items", len(novel_series_thumbs))
                                # list 格式无法直接映射，后续用 App API 获取封面
                                novel_series_thumbs = {}
                            elif isinstance(novel_series_thumbs, dict) and novel_series_thumbs:
                                first_key = list(novel_series_thumbs.keys())[0]
                                logger.info("thumbnails.novelSeries: %d items, first key=%s, val=%s",
                                           len(novel_series_thumbs), first_key,
                                           str(novel_series_thumbs.get(first_key, ""))[:200])
                            if watched_ids:
                                logger.info("watchedSeriesIds sample: %s", watched_ids[:5])
                            
                            # 整理数据: watchedSeriesIds 是有序的系列 ID 列表
                            # thumbnails.novelSeries 是 {seriesId: thumbnailUrl} 的映射
                            if watched_ids:
                                # 处理分页
                                max_page = page_info.get("maxPage", 1)
                                all_watched_ids = list(watched_ids)
                                if max_page > 1:
                                    for page_num in range(2, max_page + 1):
                                        try:
                                            paged_url = endpoint.replace("p=1", f"p={page_num}")
                                            logger.info("Fetching page %d/%d: %s", page_num, max_page, paged_url)
                                            paged_resp = http_requests.get(
                                                paged_url, headers=headers, proxies=proxies,
                                                timeout=self.settings.pixiv.timeout,
                                                verify=self.settings.pixiv.verify_ssl,
                                            )
                                            if paged_resp.status_code == 200:
                                                paged_data = paged_resp.json()
                                                paged_body = paged_data.get("body", {})
                                                paged_thumbs = paged_body.get("thumbnails", {}).get("novelSeries", {})
                                                paged_ids = paged_body.get("page", {}).get("watchedSeriesIds", [])
                                                all_watched_ids.extend(paged_ids)
                                                if isinstance(novel_series_thumbs, dict) and isinstance(paged_thumbs, dict):
                                                    novel_series_thumbs.update(paged_thumbs)
                                        except Exception as e:
                                            logger.warning("Failed to fetch page %d: %s", page_num, e)
                                
                                for sid in all_watched_ids:
                                    cover = ""
                                    if isinstance(novel_series_thumbs, dict):
                                        cover = novel_series_thumbs.get(str(sid), "")
                                    series_list.append({
                                        "series_id": str(sid),
                                        "cover_url": cover,
                                    })
                                logger.info("Found %d subscribed series from watchlist (all pages)", len(series_list))
                                
                                # 标记所有系列为订阅状态（即使不获取详情）
                                for s in series_list:
                                    try:
                                        self.db.upsert_subscribed_series(
                                            series_id=int(s["series_id"]), title="", description="",
                                            user_id=0, cover_url=s.get("cover_url", ""), total_novels=0,
                                        )
                                    except Exception:
                                        pass
                                
                                break
                    except Exception as e:
                        logger.warning("Web API %s failed: %s", endpoint, str(e))
                
            except Exception as e:
                logger.warning("Web API failed: %s", str(e))
        else:
            logger.info("No PIXIV_WEB_COOKIE configured, skipping Web API")
        
        # 方式1.5: 从 Web API 获取的 series_list 中，调用 App API 获取系列详情
        if series_list:
            logger.info("Fetching details for %d series via App API", len(series_list))
            series_delay = self.settings.sync.delay_seconds_between_series
            skip_delay = self.settings.sync.delay_seconds_between_skips
            _first_logged = False
            synced_series_count = 0  # 实际有新内容的系列数（跳过的不算）
            series_idx = 0
            # 使用 while 循环以支持顺延：当 limit > 0 时，全部跳过的系列不计入 limit
            series_queue = list(series_list)
            queue_idx = 0
            while queue_idx < len(series_queue):
                if limit > 0 and synced_series_count >= limit:
                    break
                item = series_queue[queue_idx]
                queue_idx += 1
                series_idx += 1
                sid = item.get("series_id")
                cover_from_web = item.get("cover_url", "")

                if progress_callback:
                    progress_callback("phase", {"phase": f"同步系列 {series_idx} (已同步 {synced_series_count}{f'/{limit}' if limit > 0 else ''}) (ID: {sid})"})
                try:
                    series_data = self.api.novel_series(int(sid))
                    if series_data and not _first_logged:
                        _first_logged = True
                        logger.info("novel_series response keys: %s", list(series_data.keys()) if isinstance(series_data, dict) else "N/A")
                        _detail = series_data.get("novel_series_detail") if isinstance(series_data, dict) else None
                        if _detail and isinstance(_detail, dict):
                            logger.info("novel_series_detail keys: %s", list(_detail.keys()))
                    if series_data:
                        # novel_series 返回 dict，用 .get() 取值
                        detail = series_data.get("novel_series_detail") if isinstance(series_data, dict) else None
                        if not detail:
                            detail = getattr(series_data, "novel_series_detail", None)
                        if detail:
                            # detail 可能是 dict 或对象
                            if isinstance(detail, dict):
                                title = detail.get("title", "")
                                desc = detail.get("caption", "")
                                user = detail.get("user")
                                total = detail.get("content_count", 0)
                            else:
                                title = getattr(detail, "title", "")
                                desc = getattr(detail, "caption", "")
                                user = getattr(detail, "user", None)
                                total = getattr(detail, "content_count", 0)
                            
                            # 封面图: 从 first_novel 或 novels[0] 获取
                            cover = cover_from_web
                            if isinstance(series_data, dict):
                                first_novel = series_data.get("novel_series_first_novel")
                                if first_novel and isinstance(first_novel, dict):
                                    cover = first_novel.get("url", "") or first_novel.get("image_urls", {}).get("large", "") or cover
                                if not cover:
                                    novels_list = series_data.get("novels", [])
                                    if novels_list and isinstance(novels_list[0], dict):
                                        cover = novels_list[0].get("url", "") or novels_list[0].get("image_urls", {}).get("large", "") or cover
                            
                            if isinstance(user, dict):
                                user_id = int(user.get("id", 0))
                                user_name = user.get("name", "unknown")
                                user_account = user.get("account")
                            elif user:
                                user_id = int(user.id)
                                user_name = getattr(user, "name", "unknown")
                                user_account = getattr(user, "account", None)
                            else:
                                user_id = 0
                                user_name = "unknown"
                                user_account = None
                            
                            self.db.upsert_subscribed_series(
                                series_id=int(sid), title=title, description=desc,
                                user_id=user_id, cover_url=cover, total_novels=total or 0,
                            )
                            if user_id:
                                self.db.upsert_user(UserRecord(
                                    user_id=user_id, name=user_name,
                                    account=user_account, raw_json="{}",
                                ))
                            logger.info("Synced series: %s (ID: %s, chapters: %s)", title, sid, total)
                            
                            # 同步系列中的所有章节（含正文）
                            chapter_delay = self.settings.sync.delay_seconds_between_chapters
                            download_assets = self.settings.sync.download_assets
                            write_markdown = self.settings.sync.write_markdown
                            write_raw_text = self.settings.sync.write_raw_text
                            
                            if isinstance(series_data, dict):
                                all_novel_items = list(series_data.get("novels", []))
                                # 处理分页
                                next_url = series_data.get("next_url")
                                if progress_callback:
                                    progress_callback("phase", {"phase": f"系列 {title or sid}: 共 {len(all_novel_items)} 章"})
                                while next_url:
                                    try:
                                        next_resp = self.api.auth_request_call("GET", next_url)
                                        if next_resp and next_resp.status_code == 200:
                                            next_data = self.api.parse_result(next_resp)
                                            if isinstance(next_data, dict):
                                                all_novel_items.extend(next_data.get("novels", []))
                                                next_url = next_data.get("next_url")
                                            else:
                                                break
                                        else:
                                            break
                                    except Exception as e:
                                        logger.warning("Failed to fetch next page for series %s: %s", sid, e)
                                        break
                                
                                chapter_count = 0
                                skipped_count = 0

                                def _g(obj, key, default=None):
                                    if isinstance(obj, dict):
                                        return obj.get(key, default)
                                    return getattr(obj, key, default)

                                for idx, novel_item in enumerate(all_novel_items):
                                    if not isinstance(novel_item, dict):
                                        continue
                                    novel_id = int(novel_item.get("id", 0))
                                    if not novel_id:
                                        continue

                                    # 检查是否已存在（跳过机制）
                                    if self.db.novel_exists(novel_id):
                                        skipped_count += 1
                                        if progress_callback:
                                            progress_callback("phase", {"phase": f"  [{idx+1}/{len(all_novel_items)}] 跳过已存在: {novel_id}"})
                                        if skip_delay > 0:
                                            time.sleep(skip_delay)
                                        continue

                                    try:
                                        if progress_callback:
                                            progress_callback("phase", {"phase": f"  [{idx+1}/{len(all_novel_items)}] 同步章节: {novel_id}"})
                                        # 获取小说详情
                                        novel_detail_result = self.api.novel_detail(novel_id)
                                        detail_novel = getattr(novel_detail_result, "novel", None)
                                        if detail_novel is None and isinstance(novel_detail_result, dict):
                                            detail_novel = novel_detail_result.get("novel", novel_detail_result)
                                        if detail_novel is None:
                                            logger.warning("novel_detail returned None for %d, skipping", novel_id)
                                            continue

                                        n_user = detail_novel.get("user") if isinstance(detail_novel, dict) else getattr(detail_novel, "user", None)
                                        n_user_id = int(n_user.get("id", 0) or 0) if isinstance(n_user, dict) else (int(getattr(n_user, "id", 0) or 0) if n_user else 0)
                                        n_user_name = (n_user.get("name") if isinstance(n_user, dict) else getattr(n_user, "name", "unknown")) if n_user else user_name
                                        n_account = (n_user.get("account") if isinstance(n_user, dict) else getattr(n_user, "account", None)) if n_user else None
                                        if not n_user_id:
                                            n_user_id = user_id
                                        
                                        if n_user_id:
                                            self.db.upsert_user(UserRecord(
                                                user_id=n_user_id, name=n_user_name,
                                                account=n_account, raw_json="{}",
                                            ))
                                        

                                        caption = clean_caption(_g(detail_novel, "caption", ""))
                                        cover_url = _extract_cover_url(detail_novel)
                                        if not cover_url:
                                            cover_url = _g(detail_novel, "url", "")
                                        
                                        self.db.upsert_novel(NovelRecord(
                                            novel_id=novel_id, user_id=n_user_id or user_id,
                                            series_id=int(sid), title=_g(detail_novel, "title", f"novel_{novel_id}"),
                                            caption=caption, visible=bool(_g(detail_novel, "visible", True)),
                                            restrict="public", x_restrict=int(_g(detail_novel, "x_restrict", 0) or 0),
                                            text_length=int(_g(detail_novel, "text_length", 0) or 0),
                                            total_bookmarks=int(_g(detail_novel, "total_bookmarks", 0) or 0),
                                            total_views=int(_g(detail_novel, "total_view", 0) or 0),
                                            cover_url=cover_url, tags_json="[]",
                                            create_date=_g(detail_novel, "create_date"),
                                            raw_json="{}", meta_hash="",
                                        ))
                                        
                                        # 获取正文
                                        webview = self.api.webview_novel(novel_id)
                                        body = normalize_text(_extract_novel_text(webview))
                                        text_hash = sha256_text(body)
                                        markdown_text = to_markdown(_g(detail_novel, "title", ""), n_user_name, caption, body) if write_markdown else None
                                        
                                        self.db.upsert_novel_text(NovelTextRecord(
                                            novel_id=novel_id, text_raw=body,
                                            text_markdown=markdown_text, text_hash=text_hash,
                                        ))
                                        self.db.replace_fts(novel_id, _g(detail_novel, "title", ""), caption, n_user_name, body)
                                        
                                        # 写入文件
                                        novel_dir = self.storage.novel_dir("public", n_user_id or user_id, n_user_name, novel_id, _g(detail_novel, "title", ""))
                                        if write_raw_text:
                                            self.storage.write_text(novel_dir / "text.txt", body)
                                        if write_markdown and markdown_text:
                                            self.storage.write_text(novel_dir / "text.md", markdown_text)
                                        
                                        chapter_count += 1
                                        if idx < len(all_novel_items) - 1 and chapter_delay > 0:
                                            time.sleep(chapter_delay)
                                    except Exception as e:
                                        logger.warning("Failed to sync chapter %d: %s", novel_id, e)
                                
                                if chapter_count:
                                    logger.info("  Synced %d chapters, skipped %d for series %s", chapter_count, skipped_count, sid)
                                    if progress_callback:
                                        progress_callback("phase", {"phase": f"系列 {title or sid}: 同步 {chapter_count} 章, 跳过 {skipped_count} 章"})
                                # 只有实际同步了新章节的系列才计入 synced_series_count（顺延机制）
                                if chapter_count > 0:
                                    synced_series_count += 1
                        else:
                            logger.warning("No detail found for series %s, keys: %s", sid, list(series_data.keys()) if isinstance(series_data, dict) else "N/A")
                            self.db.upsert_subscribed_series(
                                series_id=int(sid), title="", description="",
                                user_id=0, cover_url=cover_from_web, total_novels=0,
                            )
                except Exception as e:
                    logger.warning("Failed to fetch series %s: %s", sid, str(e))
                    self.db.upsert_subscribed_series(
                        series_id=int(sid), title="", description="",
                        user_id=0, cover_url=cover_from_web, total_novels=0,
                    )

                # 系列之间的延迟
                if series_delay > 0 and queue_idx < len(series_queue):
                    time.sleep(series_delay)
            stats["series_synced"] = synced_series_count
            logger.info("Subscribed series sync completed: %d series with new content", synced_series_count)
            return stats
        
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
        if user is None:
            user_id = getattr(novel, "user_id", 0) or 0
            user_name = "unknown"
            account = None
        else:
            user_id = int(getattr(user, "id", 0) or 0)
            user_name = getattr(user, "name", "unknown")
            account = getattr(user, "account", None)

        if user_id:
            self.db.upsert_user(
                UserRecord(
                    user_id=user_id,
                    name=user_name,
                    account=account,
                    raw_json=stable_json_dumps(_to_plain(user) if user else "{}"),
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

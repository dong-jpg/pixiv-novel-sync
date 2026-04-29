from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..auth import PixivAuthManager
from ..settings import Settings
from ..storage_db import Database
from ..storage_files import FileStorage
from ..sync_engine import BookmarkNovelSyncService

logger = logging.getLogger(__name__)


def run_bookmark_sync(settings: Settings) -> dict[str, int]:
    auth = PixivAuthManager(settings.pixiv)
    api, auth_result = auth.login()
    if auth_result.user_id is None:
        raise RuntimeError("Unable to determine PIXIV_USER_ID. Set PIXIV_USER_ID in .env.")

    db = Database(settings.storage.db_path)
    db.init_schema()
    storage = FileStorage(settings)
    storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])

    try:
        service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)
        bookmark_stats = service.sync(
            user_id=auth_result.user_id,
            restricts=settings.sync.bookmark_restricts,
            download_assets=settings.sync.download_assets,
            write_markdown=settings.sync.write_markdown,
            write_raw_text=settings.sync.write_raw_text,
        )
        logger.info("Bookmark sync finished: %s", json.dumps(bookmark_stats, ensure_ascii=False))
        print(json.dumps(bookmark_stats, ensure_ascii=False, indent=2))
        return bookmark_stats
    finally:
        db.close()


def run_bookmark_sync_with_progress(settings: Settings, job_manager: Any, job_id: str) -> dict[str, int]:
    auth = PixivAuthManager(settings.pixiv)
    job_manager.add_log(job_id, "info", "正在登录 Pixiv...")
    job_manager.update_progress(job_id, phase="登录", message="正在登录 Pixiv...")
    
    api, auth_result = auth.login()
    if auth_result.user_id is None:
        raise RuntimeError("Unable to determine PIXIV_USER_ID. Set PIXIV_USER_ID in .env.")
    
    job_manager.add_log(job_id, "success", f"登录成功, 用户ID: {auth_result.user_id}")

    db = Database(settings.storage.db_path)
    db.init_schema()
    storage = FileStorage(settings)
    storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])

    try:
        service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)
        
        restricts = settings.sync.bookmark_restricts
        max_items = settings.sync.max_items_per_run or 50
        job_manager.add_log(job_id, "info", f"开始同步收藏小说, 范围: {', '.join(restricts)}")
        job_manager.update_progress(job_id, phase="同步中", current=0, total=max_items, restricts=restricts)
        
        def on_progress(event_type: str, data: dict[str, Any]) -> None:
            if event_type == "novel_start":
                job_manager.add_log(job_id, "info", f"[{data.get('current', '?')}/{data.get('total', '?')}] 开始处理: {data.get('title', '未知')[:30]}")
                job_manager.update_progress(
                    job_id,
                    phase="同步中",
                    current=data.get("current", 0),
                    total=data.get("total", max_items),
                    current_novel=data.get("title", "")[:40],
                    author=data.get("author", ""),
                )
            elif event_type == "novel_done":
                job_manager.add_log(job_id, "info", f"  完成: 收藏{data.get('bookmarks', 0)} 浏览{data.get('views', 0)} 资源{data.get('assets', 0)}个")
            elif event_type == "page":
                job_manager.add_log(job_id, "info", f"正在获取第 {data.get('page', '?')} 页...")
            elif event_type == "rate_limit":
                job_manager.add_log(job_id, "warning", f"等待 {data.get('seconds', 1)} 秒 (API限速)")
        
        bookmark_stats = service.sync(
            user_id=auth_result.user_id,
            restricts=restricts,
            download_assets=settings.sync.download_assets,
            write_markdown=settings.sync.write_markdown,
            write_raw_text=settings.sync.write_raw_text,
            progress_callback=on_progress,
        )
        
        job_manager.add_log(job_id, "success", "同步完成")
        return bookmark_stats
    finally:
        db.close()


def _merge_stats(*items: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for item in items:
        for key, value in item.items():
            merged[key] = merged.get(key, 0) + value
    return merged

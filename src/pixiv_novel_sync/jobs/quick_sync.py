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
        total_stats = {"users": 0, "novels": 0, "texts_updated": 0, "assets_downloaded": 0}
        
        def on_progress(event_type: str, data: dict[str, Any]) -> None:
            if event_type == "novel_start":
                job_manager.add_log(job_id, "info", f"[{data.get('current', '?')}/{data.get('total', '?')}] {data.get('title', '')[:30]}")
                job_manager.update_progress(
                    job_id,
                    phase=data.get("phase", "同步中"),
                    current=data.get("current", 0),
                    total=data.get("total", 50),
                    current_novel=data.get("title", "")[:40],
                    author=data.get("author", ""),
                )
            elif event_type == "novel_done":
                job_manager.add_log(job_id, "info", f"  完成: 收藏{data.get('bookmarks', 0)} 浏览{data.get('views', 0)}")
            elif event_type == "page":
                job_manager.add_log(job_id, "info", f"正在获取第 {data.get('page', '?')} 页...")
            elif event_type == "rate_limit":
                job_manager.add_log(job_id, "warning", f"等待 {data.get('seconds', 1)} 秒")
            elif event_type == "phase_start":
                job_manager.add_log(job_id, "info", f"开始: {data.get('name', '')}")

        # 同步收藏
        if settings.sync.sync_bookmarks:
            job_manager.add_log(job_id, "info", "=== 开始同步收藏小说 ===")
            job_manager.update_progress(job_id, phase="同步收藏", message="正在同步收藏小说...")
            on_progress("phase_start", {"name": "收藏"})
            restricts = settings.sync.bookmark_restricts
            for restrict in restricts:
                bookmark_stats = service.sync(
                    user_id=auth_result.user_id,
                    restricts=[restrict],
                    download_assets=settings.sync.download_assets,
                    write_markdown=settings.sync.write_markdown,
                    write_raw_text=settings.sync.write_raw_text,
                    progress_callback=on_progress,
                    phase_name="同步收藏",
                )
                for key in total_stats:
                    total_stats[key] = total_stats.get(key, 0) + bookmark_stats.get(key, 0)
            job_manager.add_log(job_id, "success", "收藏同步完成")

        # 同步关注用户的系列
        if settings.sync.sync_following_series:
            job_manager.add_log(job_id, "info", "=== 开始同步关注用户系列 ===")
            job_manager.update_progress(job_id, phase="同步系列", message="正在同步关注用户系列...")
            on_progress("phase_start", {"name": "关注用户系列"})
            following_stats = service.sync_following_novels(
                download_assets=settings.sync.download_assets,
                write_markdown=settings.sync.write_markdown,
                write_raw_text=settings.sync.write_raw_text,
                progress_callback=on_progress,
                novels_only=False,
            )
            for key in total_stats:
                total_stats[key] = total_stats.get(key, 0) + following_stats.get(key, 0)
            job_manager.add_log(job_id, "success", "关注用户系列同步完成")

        # 同步关注用户列表
        if settings.sync.sync_following_users:
            job_manager.add_log(job_id, "info", "=== 开始同步关注用户列表 ===")
            job_manager.update_progress(job_id, phase="同步关注", message="正在同步关注用户列表...")
            on_progress("phase_start", {"name": "关注用户列表"})
            next_query: dict[str, Any] | None = {"restrict": "public"}
            page_count = 0
            while next_query:
                result = api.user_following(**next_query)
                page_count += 1
                job_manager.add_log(job_id, "info", f"获取关注列表第 {page_count} 页...")
                user_previews = getattr(result, "user_previews", []) or []
                for preview in user_previews:
                    user = getattr(preview, "user", preview)
                    user_id = int(getattr(user, "id", 0))
                    user_name = getattr(user, "name", str(user_id))
                    account = getattr(user, "account", None)
                    if user_id:
                        from ..models import UserRecord
                        from ..utils_hashing import stable_json_dumps
                        from ..sync_engine import _to_plain
                        db.upsert_user(UserRecord(
                            user_id=user_id,
                            name=user_name,
                            account=account,
                            raw_json=stable_json_dumps(_to_plain(user)),
                        ))
                        total_stats["users"] = total_stats.get("users", 0) + 1
                next_query = api.parse_qs(getattr(result, "next_url", None))
                if next_query:
                    time.sleep(settings.sync.delay_seconds_between_pages)
            job_manager.add_log(job_id, "success", "关注用户列表同步完成")

        # 同步关注用户的小说
        if settings.sync.sync_following_novels:
            job_manager.add_log(job_id, "info", "=== 开始同步关注用户小说 ===")
            job_manager.update_progress(job_id, phase="同步用户小说", message="正在同步关注用户小说...")
            on_progress("phase_start", {"name": "关注用户小说"})
            following_novels_stats = service.sync_following_novels(
                download_assets=settings.sync.download_assets,
                write_markdown=settings.sync.write_markdown,
                write_raw_text=settings.sync.write_raw_text,
                progress_callback=on_progress,
                novels_only=True,
            )
            for key in total_stats:
                total_stats[key] = total_stats.get(key, 0) + following_novels_stats.get(key, 0)
            job_manager.add_log(job_id, "success", "关注用户小说同步完成")

        # 同步追更系列
        if settings.sync.sync_subscribed_series:
            job_manager.add_log(job_id, "info", "=== 开始同步追更系列 ===")
            job_manager.update_progress(job_id, phase="同步追更", message="正在同步追更系列...")
            on_progress("phase_start", {"name": "追更系列"})
            subscribed_stats = service.sync_subscribed_series(
                limit=settings.sync.series_sync_limit,
                progress_callback=on_progress,
            )
            total_stats["series_synced"] = subscribed_stats.get("series_synced", 0)
            job_manager.add_log(job_id, "success", f"追更系列同步完成: {subscribed_stats.get('series_synced', 0)} 个系列")

        job_manager.add_log(job_id, "success", "全部同步完成")
        return total_stats
    finally:
        db.close()


def _merge_stats(*items: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for item in items:
        for key, value in item.items():
            merged[key] = merged.get(key, 0) + value
    return merged

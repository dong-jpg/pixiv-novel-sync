from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..auth import PixivAuthManager
from ..settings import Settings
from ..storage_db import Database
from ..storage_files import FileStorage
from ..sync_check import build_sync_check_fingerprint, sync_check_task_types
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


def run_check_bookmarks_task(
    settings: Settings,
    job_manager: Any,
    job_id: str,
    release_semaphore: bool = True,
    raise_on_error: bool = False,
) -> None:
    """独立的预检查任务：扫描所有需要同步的内容，标记哪些已存在"""
    db = None
    try:
        auth = PixivAuthManager(settings.pixiv)
        job_manager.add_log(job_id, "info", "正在登录 Pixiv...")
        job_manager.update_progress(job_id, phase="登录", message="正在登录 Pixiv...")

        api, auth_result = auth.login()
        if auth_result.user_id is None:
            raise RuntimeError("Unable to determine PIXIV_USER_ID. Set PIXIV_USER_ID in .env.")
        if settings.pixiv.user_id is None:
            settings.pixiv.user_id = auth_result.user_id

        job_manager.add_log(job_id, "success", f"登录成功, 用户ID: {auth_result.user_id}")

        db = Database(settings.storage.db_path)
        db.init_schema()
        storage = FileStorage(settings)
        storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])
        service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings, sync_check_scope=job_id)

        def on_progress(event_type: str, data: dict[str, Any]) -> None:
            if event_type == "phase":
                job_manager.add_log(job_id, "info", data.get("phase", ""))
            elif event_type == "page":
                job_manager.add_log(job_id, "info", f"正在获取第 {data.get('page', '?')} 页...")

        job_manager.add_log(job_id, "info", "=== 预检查：扫描所有需要同步的内容 ===")
        job_manager.update_progress(job_id, phase="预检查", message="正在扫描所有内容...")
        
        check_stats = service.check_all_existence(
            user_id=auth_result.user_id,
            restricts=settings.sync.bookmark_restricts,
            progress_callback=on_progress,
        )
        
        job_manager.add_log(job_id, "success", f"预检查完成: {check_stats['total_checked']} 本小说")
        job_manager.add_log(job_id, "success", f"  新小说: {check_stats['new']} 本")
        job_manager.add_log(job_id, "success", f"  已存在: {check_stats['existing']} 本")
        
        if settings.sync.sync_bookmarks:
            job_manager.add_log(job_id, "info", f"  收藏: {check_stats['bookmarks']['total']} 本 (新 {check_stats['bookmarks']['new']}, 已存在 {check_stats['bookmarks']['existing']})")
        if settings.sync.sync_following_novels:
            job_manager.add_log(job_id, "info", f"  关注用户: {check_stats['following_novels']['total']} 本 (新 {check_stats['following_novels']['new']}, 已存在 {check_stats['following_novels']['existing']})")
        if settings.sync.sync_subscribed_series:
            job_manager.add_log(job_id, "info", f"  追更系列: {check_stats['subscribed_series']['total']} 个 (新 {check_stats['subscribed_series']['new']}, 已存在 {check_stats['subscribed_series']['existing']})")
        
        job_manager.add_log(job_id, "info", "预检查结果已保存，后续同步将自动跳过已存在的小说")

        # 记录 sync_check 元数据到 progress（供正式同步复用预检查结果），两条路径都需要
        job = job_manager.get_job(job_id)
        if job is not None:
            job.progress["sync_check_scope"] = job_id
            job.progress["sync_check_fingerprint"] = build_sync_check_fingerprint(settings, auth_result.user_id)
            job.progress["sync_check_task_types"] = sync_check_task_types(settings)
            job.progress["sync_check_user_id"] = auth_result.user_id
            # legacy SyncJobState 路径（webapp 后台线程）没有 JobRunner 管理终态，
            # 需要在此显式标记完成；统一 JobState 路径（有 .spec）由 JobRunner 负责
            # mark_succeeded + merge_stats，这里绝不能设置 job.stats，否则返回值会被
            # 重复 merge 导致所有数值翻倍，也不能给枚举 status 赋字符串。
            if not hasattr(job, "spec"):
                job.status = "succeeded"
                job.message = "预检查完成"
                job.stats = dict(check_stats)
                job.finished_at = time.time()

        # 始终返回独立副本：JobRunner 把它当作增量 merge 进 state.stats
        return dict(check_stats)
    except Exception as exc:
        job_manager.add_log(job_id, "error", f"预检查失败: {exc}")
        job = job_manager.get_job(job_id)
        if job is not None and not hasattr(job, "spec"):
            job.status = "failed"
            job.message = f"预检查失败: {exc}"
            job.finished_at = time.time()
        if raise_on_error:
            raise
        return None
    finally:
        if db:
            db.close()
        if release_semaphore:
            job_manager._semaphore.release()

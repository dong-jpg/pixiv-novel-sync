from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from ..auth import PixivAuthManager
from ..settings import Settings
from ..storage_db import Database, prepare_schema
from ..storage_files import FileStorage
from ..sync_engine import BookmarkNovelSyncService
from . import services as job_services
from .services import JobReporter, _rebuild_rescue_catalog

logger = logging.getLogger(__name__)

StopRequested = Callable[[], bool]
ClaimFinalization = Callable[[], bool]


def _save_self_profile(db: Database, auth_result: Any) -> None:
    """把本人账号资料（含会员状态）落库，供侧边栏展示。

    users 表只存被关注的作者，本人账号不在其中；只有 auth 响应里带 is_premium
    这类本人专属字段，所以每次登录成功都顺带刷新一次。失败不影响同步任务。
    """
    try:
        profile = auth_result.self_profile() if hasattr(auth_result, "self_profile") else None
        if profile:
            db.save_self_profile(profile)
    except Exception:  # pragma: no cover - 落库失败不能影响同步主流程
        logger.debug("保存本人账号资料失败", exc_info=True)


def _raise_if_stopped(stop_requested: StopRequested | None) -> None:
    if stop_requested is not None and stop_requested():
        raise InterruptedError("Task stopped by user")


def _build_cancel_progress_callback(stop_requested: StopRequested | None) -> Callable[[str, dict[str, Any]], None] | None:
    if stop_requested is None:
        return None

    def on_progress(event_type: str, data: dict[str, Any]) -> None:
        _raise_if_stopped(stop_requested)

    return on_progress


def run_bookmark_sync(
    settings: Settings,
    stop_requested: StopRequested | None = None,
    claim_finalization: ClaimFinalization | None = None,
) -> dict[str, int]:
    _raise_if_stopped(stop_requested)
    auth = PixivAuthManager(settings.pixiv)
    api, auth_result = auth.login()
    if auth_result.user_id is None:
        raise RuntimeError("Unable to determine PIXIV_USER_ID. Set PIXIV_USER_ID in .env.")

    db = Database(settings.storage.db_path)
    prepare_schema(db)
    _save_self_profile(db, auth_result)
    storage = FileStorage(settings)
    storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])

    try:
        service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)
        # 引擎包装 API 重试时读的是 service.stop_requested，必须在这里接线，
        # 否则取消信号打不断 429/网络错误的退避重试。
        service.stop_requested = stop_requested
        bookmark_stats = service.sync(
            user_id=auth_result.user_id,
            restricts=settings.sync.bookmark_restricts,
            download_assets=settings.sync.download_assets,
            write_markdown=settings.sync.write_markdown,
            write_raw_text=settings.sync.write_raw_text,
            progress_callback=_build_cancel_progress_callback(stop_requested),
        )
        _raise_if_stopped(stop_requested)
        if claim_finalization is not None and not claim_finalization():
            raise InterruptedError("Task stopped by user")
        bookmark_stats.update(_rebuild_rescue_catalog(db))
        logger.info("Bookmark sync finished: %s", json.dumps(bookmark_stats, ensure_ascii=False))
        return bookmark_stats
    finally:
        db.close()


def run_scheduled_user_backup(
    settings: Settings,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
    claim_finalization: ClaimFinalization | None = None,
) -> dict[str, Any]:
    """Back up a rotating batch of followed users through the shared runner."""
    db = Database(settings.storage.db_path)
    try:
        prepare_schema(db)
        rows = db.conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()
        user_ids = [int(row[0]) for row in rows]
        total_users = len(user_ids)
        watermark = db.get_watermark("user_backup_rotation") or {}
        offset = int(watermark.get("offset", 0) or 0)
        if offset >= total_users:
            offset = 0

        users_limit = int(settings.sync.auto_sync_following_novels_users_limit or 0)
        if users_limit <= 0:
            users_limit = total_users
        batch = user_ids[offset : offset + users_limit]

        if reporter is not None and batch:
            reporter.add_log(
                "info",
                "=== 全量备份关注用户小说: "
                f"用户 {offset + 1}-{offset + len(batch)}/{total_users}, 本轮 {len(batch)} 人 ===",
            )

        totals = {"novels": 0, "skipped": 0, "assets_downloaded": 0}
        stopped = False
        completed_users = 0
        for index, user_id in enumerate(batch):
            if stop_requested is not None and stop_requested():
                stopped = True
                break
            if reporter is not None:
                reporter.update_progress(
                    phase="全量备份",
                    current=index + 1,
                    total=len(batch),
                )
            stats = job_services.run_user_backup_task(
                settings,
                user_id,
                reporter=reporter,
                stop_requested=stop_requested,
                rebuild_catalog=False,
            )
            for key in totals:
                totals[key] += int(stats.get(key, 0) or 0)
            if stats.get("stopped"):
                stopped = True
                break
            completed_users += 1

        if not stopped and stop_requested is not None and stop_requested():
            stopped = True

        next_offset = offset + completed_users
        if next_offset >= total_users:
            next_offset = 0
        if total_users:
            db.update_watermark(
                "user_backup_rotation",
                {
                    "offset": next_offset,
                    "last_sync_time": datetime.now(timezone.utc).isoformat(),
                },
            )

        result: dict[str, Any] = {**totals, "stopped": stopped}
        if not stopped:
            if claim_finalization is not None and not claim_finalization():
                result["stopped"] = True
            else:
                result.update(_rebuild_rescue_catalog(db, reporter))

        if reporter is not None:
            level = "info" if result["stopped"] else "success"
            suffix = "已停止" if result["stopped"] else "完成"
            reporter.add_log(
                level,
                f"全量备份{suffix}: 同步 {totals['novels']} 本, "
                f"跳过 {totals['skipped']} 本, 资源 {totals['assets_downloaded']} 个",
            )
        return result
    finally:
        db.close()

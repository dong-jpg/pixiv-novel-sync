from __future__ import annotations

import json
import logging
from bisect import bisect_right
from collections.abc import Callable
from typing import Any

from ..auth import PixivAuthManager
from ..settings import Settings
from ..storage_db import Database, prepare_schema
from ..storage_files import FileStorage
from ..sync.following_snapshot import read_following_snapshot
from ..sync_engine import BookmarkNovelSyncService, SyncAuthFailed
from . import services as job_services
from .services import JobReporter, _catalog_inputs_changed, _rebuild_rescue_catalog

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
    try:
        prepare_schema(db)
        _save_self_profile(db, auth_result)
        storage = FileStorage(settings)
        storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])
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
        if _catalog_inputs_changed(bookmark_stats):
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
        if stop_requested is not None and stop_requested():
            return {"stopped": True, "novels": 0, "failed_users": 0}
        owner = settings.pixiv.user_id
        api = None
        if not owner:
            api = job_services._login(settings)
            owner = settings.pixiv.user_id
        members = read_following_snapshot(db, owner)
        if members is None:
            if api is None:
                api = job_services._login(settings)
            service = BookmarkNovelSyncService(api, db, FileStorage(settings), settings, stop_requested)
            refreshed = service.sync_following_list(_build_cancel_progress_callback(stop_requested))
            members = read_following_snapshot(db, owner)
            if refreshed.get("incomplete") or members is None:
                return {"incomplete": True, "aborted_reason": "following_snapshot_unavailable", "novels": 0}
        user_ids = sorted(user.id for user in members)
        total_users = len(user_ids)
        watermark = db.get_watermark("user_backup_rotation_v2", str(owner)) or {}
        offset = bisect_right(user_ids, watermark.get("last_author", 0))
        if offset >= total_users:
            offset = 0

        users_limit = settings.sync.user_backup_users_per_run
        member_ids = set(user_ids)
        pending = [author for author in watermark.get("pending", []) if author in member_ids]
        resume_author = pending[0] if pending and (users_limit > 1 or not watermark.get("resumed_first")) else None
        rotating = user_ids[offset:] + user_ids[:offset]
        batch = ([resume_author] if resume_author is not None else []) + [author for author in rotating if author != resume_author]
        batch = batch[:users_limit]

        if reporter is not None and batch:
            reporter.add_log(
                "info",
                "=== 全量备份关注用户小说: "
                f"公开关注 {total_users} 人，本轮 {len(batch)} 人 ===",
            )

        totals = {"novels": 0, "skipped": 0, "skipped_archived": 0, "assets_downloaded": 0, "failed_users": 0}
        full_refresh_users = 0
        stopped = False
        failed_users = 0
        failed_novels = 0
        truncated = False
        incomplete = False
        incomplete_reasons: set[str] = set()
        pages_read = resumed_users = 0
        if batch and api is None:
            api = job_services._login(settings)
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
            try:
                stats = job_services.run_user_backup_task(
                    settings,
                    user_id,
                    reporter=reporter,
                    stop_requested=stop_requested,
                    rebuild_catalog=False,
                    scheduled_owner=owner,
                    api=api,
                )
            except InterruptedError:
                stopped = True
                break
            except SyncAuthFailed:
                incomplete = True
                incomplete_reasons.add("auth_failed")
                break
            except Exception as exc:
                failed_users += 1
                incomplete = True
                incomplete_reasons.add("user_backup_errors")
                logger.warning("用户 %s 备份失败，跳过并继续轮转: %s", user_id, exc)
                if reporter is not None:
                    reporter.add_log("warning", f"用户 {user_id} 备份失败，已跳过: {exc}")
                stats = {"incomplete": True, "aborted_reason": "user_backup_errors"}
            if stats.get("more_pages") or stats.get("incomplete"):
                pending = [author for author in pending if author != user_id] + [user_id]
            elif not stats.get("stopped") and user_id in pending:
                pending.remove(user_id)
            pages_read += int(stats.get("pages_read", 0))
            resumed_users += int(bool(stats.get("resumed")))
            truncated = truncated or bool(stats.get("truncated"))
            if stats.get("aborted_reason") or stats.get("truncated") or (stats.get("incomplete") and not stats.get("more_pages")):
                incomplete = True
                incomplete_reasons.add(str(stats.get("aborted_reason") or "user_backup_incomplete"))
            failed_novels += int(stats.get("failed", 0) or 0)
            if stats.get("full_refresh"):
                full_refresh_users += 1
            for key in totals:
                if key == "failed_users":
                    continue
                totals[key] += int(stats.get(key, 0) or 0)
            if stats.get("stopped"):
                stopped = True
                break
            if user_id != resume_author or len(user_ids) == 1:
                watermark["last_author"] = user_id
            watermark.update(pending=pending, resumed_first=resume_author is not None)
            db.update_watermark("user_backup_rotation_v2", watermark, str(owner))
        totals["failed_users"] = failed_users

        if not stopped and stop_requested is not None and stop_requested():
            stopped = True

        # full_refresh_users：本轮里走「全部重抓」的作者数，其余作者只补没归档完整的
        result: dict[str, Any] = {**totals, "full_refresh_users": full_refresh_users, "stopped": stopped}
        result.update(pages_read=pages_read, resumed_users=resumed_users, authors_with_more_pages=len(pending), users_total=total_users)
        if pending and not incomplete:
            result.update(incomplete=True, rotation_pending=True)
        if failed_novels:
            result["failed"] = failed_novels
        if incomplete:
            result.update(incomplete=True, aborted_reason=",".join(sorted(incomplete_reasons)))
        if truncated:
            result["truncated"] = True
        if not stopped:
            if claim_finalization is not None and not claim_finalization():
                result["stopped"] = True
            else:
                if _catalog_inputs_changed(result):
                    result.update(_rebuild_rescue_catalog(db, reporter))

        if reporter is not None:
            level = "info" if result["stopped"] else ("warning" if incomplete else "success")
            suffix = "已停止" if result["stopped"] else ("部分完成" if incomplete else "完成")
            reporter.add_log(
                level,
                f"全量备份{suffix}: 同步 {totals['novels']} 本, "
                f"跳过 {totals['skipped']} 本, 资源 {totals['assets_downloaded']} 个",
            )
        return result
    finally:
        db.close()

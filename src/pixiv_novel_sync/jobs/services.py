from __future__ import annotations

from collections.abc import Callable
import logging
import time
from time import perf_counter
from typing import Any

from pixiv_novel_sync.auth import PixivAuthManager
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.storage_files import FileStorage
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService

logger = logging.getLogger(__name__)


class JobReporter:
    def __init__(self, manager: Any = None, job_id: str | None = None) -> None:
        self.manager = manager
        self.job_id = str(job_id) if job_id else None

    def add_log(self, level: str, message: str) -> None:
        if self.manager is None or not self.job_id or not hasattr(self.manager, "add_log"):
            return
        self.manager.add_log(self.job_id, level, message)

    def update_progress(self, **kwargs: Any) -> None:
        if self.manager is None or not self.job_id or not hasattr(self.manager, "update_progress"):
            return
        self.manager.update_progress(self.job_id, **kwargs)


StopRequested = Callable[[], bool]


def _report_catalog_log(reporter: JobReporter | None, level: str, message: str) -> None:
    if reporter is None:
        return
    try:
        reporter.add_log(level, message)
    except Exception as exc:
        logger.warning("救援目录日志记录失败: %s", exc)


def _rebuild_rescue_catalog(db: Any, reporter: JobReporter | None = None) -> dict[str, int]:
    try:
        started_at = perf_counter()
        result = db.rebuild_rescue_catalog()
        duration_ms = int(round((perf_counter() - started_at) * 1000))
        stats = {
            "rescue_catalog_items": int(result.get("items", 0) or 0),
            "rescue_catalog_sources": int(result.get("sources", 0) or 0),
            "rescue_catalog_duration_ms": duration_ms,
        }
        message = (
            "救援目录刷新完成: "
            f"条目 {stats['rescue_catalog_items']}, "
            f"来源 {stats['rescue_catalog_sources']}, "
            f"耗时 {duration_ms} ms"
        )
        logger.info(message)
        _report_catalog_log(reporter, "success", message)
        return stats
    except Exception as exc:
        message = f"救援目录刷新失败: {exc}"
        logger.warning(message)
        _report_catalog_log(reporter, "warning", message)
        return {}


def _sleep_with_cancel(
    seconds: float,
    stop_requested: StopRequested | None,
    interval: float = 0.2,
) -> bool:
    if seconds <= 0:
        return stop_requested is not None and stop_requested()

    remaining = float(seconds)
    while remaining > 0:
        if stop_requested is not None and stop_requested():
            return True
        sleep_for = min(interval, remaining)
        time.sleep(sleep_for)
        remaining -= sleep_for

    return stop_requested is not None and stop_requested()


def run_user_backup_task(
    settings: Any,
    user_id: int,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
    *,
    rebuild_catalog: bool = True,
) -> dict[str, Any]:
    if stop_requested is not None and stop_requested():
        _report_log(reporter, "info", "用户全量备份已停止")
        return {
            "user_id": user_id,
            "novels": 0,
            "skipped": 0,
            "assets_downloaded": 0,
            "stopped": True,
        }

    api = _login(settings)
    storage = _ensure_storage_dirs(settings)

    db = Database(settings.storage.db_path)
    db.init_schema()
    try:
        service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)
        user_name = _lookup_user_name(db, user_id)
        _report_log(reporter, "info", f"开始用户全量备份: {user_name} ({user_id})")

        total_novels = 0
        total_skipped = 0
        total_assets = 0
        total_failed = 0
        processed = 0
        total_seen = 0
        stopped = False
        next_query: dict[str, Any] | None = {"user_id": user_id}

        _report_progress(reporter, phase="user_backup", current=0, total=0, current_novel=user_name, author=user_name)

        while next_query:
            if stop_requested is not None and stop_requested():
                stopped = True
                break

            result = api.user_novels(**next_query)
            novels = getattr(result, "novels", []) or []
            total_seen += len(novels)
            for novel in novels:
                if stop_requested is not None and stop_requested():
                    stopped = True
                    break

                counters = service._sync_novel(
                    novel,
                    "public",
                    settings.sync.download_assets,
                    settings.sync.write_markdown,
                    settings.sync.write_raw_text,
                    source_type="user_backup",
                    source_key=str(user_id),
                )
                failed = counters.get("failed", 0)
                if failed:
                    total_failed += failed
                    # 3.1容错:单本失败累计,超20%或绝对10本再中止,保留已同步部分
                    if total_failed >= 10 or (processed > 0 and total_failed / processed > 0.2):
                        raise RuntimeError(f"User backup aborted for user {user_id}: {total_failed}/{processed} novels failed (threshold exceeded)")
                processed += 1
                total_novels += counters.get("novels", 0)
                total_skipped += counters.get("skipped", 0)
                total_assets += counters.get("assets_downloaded", 0)
                _report_progress(
                    reporter,
                    phase="user_backup",
                    current=processed,
                    total=total_seen,
                    current_novel=str(getattr(novel, "title", getattr(novel, "id", ""))),
                    author=user_name,
                )

            if stopped:
                break

            next_query = api.parse_qs(getattr(result, "next_url", None))
            if next_query and settings.sync.delay_seconds_between_pages > 0:
                if _sleep_with_cancel(settings.sync.delay_seconds_between_pages, stop_requested):
                    stopped = True
                    break

        if stopped:
            _report_log(reporter, "info", f"用户全量备份已停止: {user_name} ({user_id})")
        else:
            _report_log(reporter, "success", f"用户全量备份完成: {user_name} ({user_id}), 同步 {total_novels} 本")
        stats = {
            "user_id": user_id,
            "novels": total_novels,
            "skipped": total_skipped,
            "assets_downloaded": total_assets,
            "stopped": stopped,
        }
        if not stats.get("stopped") and stop_requested is not None and stop_requested():
            stats["stopped"] = True
        if rebuild_catalog and not stats.get("stopped"):
            stats.update(_rebuild_rescue_catalog(db, reporter))
        return stats
    finally:
        db.close()


def run_user_status_task(
    settings: Any,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
) -> dict[str, Any]:
    return _run_user_status_like_task(
        settings=settings,
        reporter=reporter,
        stop_requested=stop_requested,
        task_label="用户状态检查",
        total_label="用户",
        list_items=_list_all_users,
        check_status=_check_pixiv_user_status,
        upsert_status=lambda db, user, status: db.upsert_user_status(user["user_id"], status),
        progress_name=lambda user: str(user.get("name") or user.get("user_id")),
        progress_id=lambda user: user.get("user_id"),
        total_key="total_users",
    )


def run_novel_status_task(
    settings: Any,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
) -> dict[str, Any]:
    return _run_status_task(
        settings=settings,
        reporter=reporter,
        stop_requested=stop_requested,
        task_label="小说状态检查",
        total_label="小说",
        list_ids=lambda db: db.get_all_novel_ids(),
        check_status=_check_novel_status,
        upsert_status=lambda db, item_id, status: db.upsert_novel_status(item_id, status),
        total_key="total_novels",
        rebuild_catalog=True,
    )


def run_series_status_task(
    settings: Any,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
) -> dict[str, Any]:
    return _run_status_task(
        settings=settings,
        reporter=reporter,
        stop_requested=stop_requested,
        task_label="系列状态检查",
        total_label="系列",
        list_ids=lambda db: db.get_all_series_ids(),
        check_status=_check_series_status,
        upsert_status=lambda db, item_id, status: db.upsert_series_status(item_id, status),
        total_key="total_series",
        rebuild_catalog=True,
    )


def run_pending_deletion_detection_task(
    settings: Any,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
) -> dict[str, Any]:
    if stop_requested is not None and stop_requested():
        _report_log(reporter, "info", "待删除检测已停止")
        return {
            "bookmark": {},
            "series": {},
            "new_pending": 0,
            "stopped": True,
        }

    _report_log(reporter, "info", "=== 开始检测取消收藏/追更 ===")

    api = _login(settings)
    auth_user_id = settings.pixiv.user_id
    _report_log(reporter, "success", f"登录成功, 用户ID: {auth_user_id}")

    if stop_requested is not None and stop_requested():
        _report_log(reporter, "info", "待删除检测已停止")
        return {
            "bookmark": {},
            "series": {},
            "new_pending": 0,
            "stopped": True,
        }

    db = Database(settings.storage.db_path)
    try:
        db.init_schema()
        storage = _ensure_storage_dirs(settings)
        service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)

        def on_progress(event_type: str, data: dict[str, Any]) -> None:
            if stop_requested is not None and stop_requested():
                raise InterruptedError("Task stopped by user")
            if event_type == "phase":
                _report_log(reporter, "info", str(data.get("phase", "")))
            elif event_type == "rate_limit":
                _report_log(reporter, "warning", f"等待 {data.get('seconds', 1)} 秒")

        _report_progress(reporter, phase="pending_deletion_detection", current=0, total=0)

        if stop_requested is not None and stop_requested():
            _report_log(reporter, "info", "待删除检测已停止")
            return {
                "bookmark": {},
                "series": {},
                "new_pending": 0,
                "stopped": True,
            }

        try:
            result = service.run_detection(
                user_id=auth_user_id,
                restricts=getattr(settings.sync, "bookmark_restricts", ["public"]),
                progress_callback=on_progress,
            )
        except InterruptedError:
            _report_log(reporter, "info", "待删除检测已停止")
            return {
                "bookmark": {},
                "series": {},
                "new_pending": 0,
                "stopped": True,
            }

        stats = dict(result)
        stats.setdefault("stopped", False)
        _report_log(reporter, "success", f"检测完成: 发现 {stats.get('new_pending', 0)} 条新的待确认记录")

        # Phase 3.2: 清理过期的pending_deletions记录
        try:
            cleanup_result = db.cleanup_old_pending_deletions(
                grace_period_days=getattr(settings.sync, "pending_deletion_grace_period_days", 30),
                cleanup_confirmed_days=getattr(settings.sync, "pending_deletion_cleanup_confirmed_days", 7)
            )
            if cleanup_result["auto_confirmed"] > 0 or cleanup_result["cleaned_up"] > 0:
                _report_log(
                    reporter, "info",
                    f"自动确认 {cleanup_result['auto_confirmed']} 条过期pending, 清理 {cleanup_result['cleaned_up']} 条旧记录"
                )
            stats.update(cleanup_result)
        except Exception as e:
            _report_log(reporter, "warning", f"清理过期记录失败: {e}")

        return stats
    finally:
        db.close()


def _run_user_status_like_task(
    settings: Any,
    reporter: JobReporter | None,
    stop_requested: StopRequested | None,
    task_label: str,
    total_label: str,
    list_items: Callable[[Database], list[dict[str, Any]]],
    check_status: Callable[[Any, int], str],
    upsert_status: Callable[[Database, dict[str, Any], str], None],
    progress_name: Callable[[dict[str, Any]], str],
    progress_id: Callable[[dict[str, Any]], Any],
    total_key: str,
) -> dict[str, Any]:
    api = _login(settings)
    _ensure_storage_dirs(settings)

    db = Database(settings.storage.db_path)
    db.init_schema()
    try:
        items = list_items(db)
        _report_log(reporter, "info", f"开始{task_label}")
        _report_log(reporter, "info", f"共 {len(items)} 个{total_label}需要检查")
        return _process_status_items(
            settings=settings,
            reporter=reporter,
            stop_requested=stop_requested,
            db=db,
            items=items,
            check_status=lambda item: check_status(api, int(progress_id(item))),
            upsert_status=upsert_status,
            item_label=total_label,
            item_name=progress_name,
            total_key=total_key,
        )
    finally:
        db.close()


def _run_status_task(
    settings: Any,
    reporter: JobReporter | None,
    stop_requested: StopRequested | None,
    task_label: str,
    total_label: str,
    list_ids: Callable[[Database], list[int]],
    check_status: Callable[[Any, int], str],
    upsert_status: Callable[[Database, int, str], None],
    total_key: str,
    rebuild_catalog: bool = False,
) -> dict[str, Any]:
    api = _login(settings)
    _ensure_storage_dirs(settings)

    db = Database(settings.storage.db_path)
    db.init_schema()
    try:
        item_ids = list_ids(db)
        _report_log(reporter, "info", f"开始{task_label}")
        _report_log(reporter, "info", f"共 {len(item_ids)} 个{total_label}需要检查")
        stats = _process_status_items(
            settings=settings,
            reporter=reporter,
            stop_requested=stop_requested,
            db=db,
            items=item_ids,
            check_status=lambda item_id: check_status(api, item_id),
            upsert_status=upsert_status,
            item_label=total_label,
            item_name=lambda item_id: str(item_id),
            total_key=total_key,
        )
        if not stats.get("stopped") and stop_requested is not None and stop_requested():
            stats["stopped"] = True
        if rebuild_catalog and not stats.get("stopped"):
            stats.update(_rebuild_rescue_catalog(db, reporter))
        return stats
    finally:
        db.close()


def _login(settings: Any) -> Any:
    auth = PixivAuthManager(settings.pixiv)
    api, auth_result = auth.login()
    if auth_result.user_id is None:
        raise RuntimeError("Unable to determine user ID")
    if getattr(settings.pixiv, "user_id", None) is None:
        settings.pixiv.user_id = auth_result.user_id
    return api


def _ensure_storage_dirs(settings: Any) -> FileStorage:
    storage = FileStorage(settings)
    storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])
    return storage


def _process_status_items(
    settings: Any,
    reporter: JobReporter | None,
    stop_requested: StopRequested | None,
    db: Database,
    items: list[Any],
    check_status: Callable[[Any], str],
    upsert_status: Callable[[Database, Any, str], None],
    item_label: str,
    item_name: Callable[[Any], str],
    total_key: str,
) -> dict[str, Any]:
    checked_count = 0
    status_counts: dict[str, int] = {}
    stopped = False
    total = len(items)

    _report_progress(reporter, phase=item_label, current=0, total=total)

    for item in items:
        if stop_requested is not None and stop_requested():
            stopped = True
            break

        status = check_status(item)
        upsert_status(db, item, status)
        checked_count += 1
        status_counts[status] = status_counts.get(status, 0) + 1

        _report_log(reporter, "info", f"[{checked_count}/{total}] {item_label} {item_name(item)}: {status}")
        _report_progress(reporter, phase=item_label, current=checked_count, total=total, current_novel=item_name(item), author="")
        if _sleep_with_cancel(settings.sync.delay_seconds_between_skips, stop_requested):
            stopped = True
            break

    _report_log(reporter, "success", f"{item_label}状态检查完成: {checked_count} 个")
    return {
        "checked_count": checked_count,
        total_key: total,
        "status_counts": status_counts,
        "stopped": stopped,
    }


def _list_all_users(db: Database) -> list[dict[str, Any]]:
    users: list[dict[str, Any]] = []
    page_num = 1
    while True:
        page_data = db.list_users(page=page_num, page_size=500)
        items = page_data.get("items", [])
        if not items:
            break
        users.extend(items)
        if page_num >= page_data.get("total_pages", 1):
            break
        page_num += 1
    return users


def _lookup_user_name(db: Database, user_id: int) -> str:
    row = db.conn.execute("SELECT name FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row and row[0]:
        return str(row[0])
    return str(user_id)


def _check_pixiv_user_status(api: Any, user_id: int) -> str:
    from pixiv_novel_sync.webapp import _check_pixiv_user_status as check_status

    return check_status(api, user_id)


def _check_novel_status(api: Any, novel_id: int) -> str:
    from pixiv_novel_sync.webapp import _check_novel_status as check_status

    return check_status(api, novel_id)


def _check_series_status(api: Any, series_id: int) -> str:
    from pixiv_novel_sync.webapp import _check_series_status as check_status

    return check_status(api, series_id)


def _report_log(reporter: JobReporter | None, level: str, message: str) -> None:
    if reporter is not None:
        reporter.add_log(level, message)


def _report_progress(reporter: JobReporter | None, **kwargs: Any) -> None:
    if reporter is not None:
        reporter.update_progress(**kwargs)

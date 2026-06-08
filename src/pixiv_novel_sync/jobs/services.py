from __future__ import annotations

from collections.abc import Callable
from typing import Any
import time

from pixiv_novel_sync.auth import PixivAuthManager
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.storage_files import FileStorage


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


def run_user_backup_task(
    settings: Any,
    user_id: int,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
) -> dict[str, Any]:
    return {}


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
    )


def run_pending_deletion_detection_task(
    settings: Any,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
) -> dict[str, Any]:
    return {}


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
) -> dict[str, Any]:
    api = _login(settings)
    _ensure_storage_dirs(settings)

    db = Database(settings.storage.db_path)
    db.init_schema()
    try:
        item_ids = list_ids(db)
        _report_log(reporter, "info", f"开始{task_label}")
        _report_log(reporter, "info", f"共 {len(item_ids)} 个{total_label}需要检查")
        return _process_status_items(
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


def _ensure_storage_dirs(settings: Any) -> None:
    storage = FileStorage(settings)
    storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])


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
        time.sleep(settings.sync.delay_seconds_between_skips)

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

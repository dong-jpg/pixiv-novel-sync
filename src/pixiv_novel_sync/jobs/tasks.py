from __future__ import annotations

from collections.abc import Callable
from numbers import Number
from typing import Any

from pixiv_novel_sync.jobs.services import JobReporter


def _is_addable_number(value: Any) -> bool:
    return isinstance(value, Number) and not isinstance(value, bool)

_TASK_LABELS: dict[str, str] = {
    "bookmark": "收藏小说",
    "following_users": "关注用户",
    "following_novels": "关注用户小说",
    "subscribed_series": "订阅系列",
    "sync_check": "同步检查",
    "user_status": "用户状态检查",
    "novel_status": "小说状态检查",
    "series_status": "系列状态检查",
    "pending_deletion_detection": "待删除检测",
    "user_backup": "用户全量备份",
    "preference_analyze": "偏好分析",  # Phase 7.6
    "recommendation_run": "生成推荐",  # Phase 7.6
}



def _job_reporter_from_context(context: dict[str, Any]) -> JobReporter:
    return JobReporter(manager=context.get("manager"), job_id=context.get("job_id"))



def _stop_requested_from_context(context: dict[str, Any]) -> Callable[[], bool]:
    manager = context.get("manager")
    job_id = context.get("job_id")

    def stop_requested() -> bool:
        if manager is None or not job_id or not hasattr(manager, "is_cancel_requested"):
            return False
        return bool(manager.is_cancel_requested(str(job_id)))

    return stop_requested



def execute_task(task_type: str, settings: Any, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
    context = context or {}
    reporter = _job_reporter_from_context(context)
    stop_requested = _stop_requested_from_context(context)

    if task_type == "bookmark":
        from pixiv_novel_sync.jobs.quick_sync import run_bookmark_sync

        return run_bookmark_sync(settings)

    if task_type == "sync_check":
        from pixiv_novel_sync.jobs.quick_sync import run_check_bookmarks_task

        manager = context.get("manager")
        job_id = context.get("job_id")
        if manager is None or not job_id:
            raise RuntimeError("sync_check CLI execution requires job manager and job_id context")
        return run_check_bookmarks_task(
            settings,
            manager,
            str(job_id),
            release_semaphore=False,
            raise_on_error=True,
        )

    if task_type in {"following_users", "following_novels", "subscribed_series"}:
        return _run_direct_sync_task(task_type, settings, context)

    if task_type.startswith("user_backup:"):
        from pixiv_novel_sync.jobs.services import run_user_backup_task

        user_id = int(task_type.split(":", 1)[1])
        return run_user_backup_task(settings, user_id, reporter=reporter, stop_requested=stop_requested)

    if task_type == "user_status":
        from pixiv_novel_sync.jobs.services import run_user_status_task

        return run_user_status_task(settings, reporter=reporter, stop_requested=stop_requested)

    if task_type == "novel_status":
        from pixiv_novel_sync.jobs.services import run_novel_status_task

        return run_novel_status_task(settings, reporter=reporter, stop_requested=stop_requested)

    if task_type == "series_status":
        from pixiv_novel_sync.jobs.services import run_series_status_task

        return run_series_status_task(settings, reporter=reporter, stop_requested=stop_requested)

    if task_type == "pending_deletion_detection":
        from pixiv_novel_sync.jobs.services import run_pending_deletion_detection_task

        return run_pending_deletion_detection_task(settings, reporter=reporter, stop_requested=stop_requested)

    if task_type == "preference_analyze":  # Phase 7.6
        return _run_preference_analyze_task(settings, context)

    if task_type == "recommendation_run":  # Phase 7.6
        return _run_recommendation_run_task(settings, context)

    raise RuntimeError(f"Unsupported task type for CLI execution: {task_type}")


def _run_direct_sync_task(task_type: str, settings: Any, context: dict[str, Any]) -> dict[str, Any]:
    from pixiv_novel_sync.auth import PixivAuthManager
    from pixiv_novel_sync.storage_db import Database
    from pixiv_novel_sync.storage_files import FileStorage
    from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService

    manager = context.get("manager")
    job_id = context.get("job_id")

    def add_log(level: str, message: str) -> None:
        if manager is not None and job_id:
            manager.add_log(str(job_id), level, message)

    add_log("info", f"=== 开始{task_label(task_type)} ===")
    auth = PixivAuthManager(settings.pixiv)
    api, auth_result = auth.login()
    if auth_result.user_id is None:
        raise RuntimeError("Unable to determine PIXIV_USER_ID. Set PIXIV_USER_ID in .env.")
    if getattr(settings.pixiv, "user_id", None) is None:
        settings.pixiv.user_id = auth_result.user_id
    add_log("success", f"登录成功, 用户ID: {auth_result.user_id}")

    db = Database(settings.storage.db_path)
    db.init_schema()
    storage = FileStorage(settings)
    storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])

    try:
        service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)
        progress_callback = _build_progress_callback(manager, str(job_id) if job_id else None)

        if task_type == "following_users":
            return service.sync_following_list(progress_callback=progress_callback)
        if task_type == "following_novels":
            return service.sync_following_novels(
                download_assets=settings.sync.download_assets,
                write_markdown=settings.sync.write_markdown,
                write_raw_text=settings.sync.write_raw_text,
                progress_callback=progress_callback,
                users_limit=settings.sync.auto_sync_following_novels_users_limit or 0,
            )
        if task_type == "subscribed_series":
            subscribed_series = service.sync_subscribed_series
            if _accepts_parameter(subscribed_series, "download_assets"):
                return subscribed_series(
                    download_assets=settings.sync.download_assets,
                    write_markdown=settings.sync.write_markdown,
                    write_raw_text=settings.sync.write_raw_text,
                    progress_callback=progress_callback,
                    limit=settings.sync.series_sync_limit,
                )
            return subscribed_series(progress_callback=progress_callback, limit=settings.sync.series_sync_limit)
    finally:
        db.close()

    raise RuntimeError(f"Unsupported direct sync task: {task_type}")


def _build_progress_callback(manager: Any, job_id: str | None) -> Callable[[str, dict[str, Any]], None] | None:
    if manager is None or not job_id:
        return None

    def safe_add_log(level: str, message: str) -> None:
        if hasattr(manager, "add_log"):
            manager.add_log(job_id, level, message)

    def safe_update_progress(**kwargs: Any) -> None:
        if hasattr(manager, "update_progress"):
            manager.update_progress(job_id, **kwargs)

    def on_progress(event_type: str, data: dict[str, Any]) -> None:
        if event_type == "page":
            safe_add_log("info", f"正在获取第 {data.get('page', '?')} 页...")
        elif event_type == "rate_limit":
            safe_add_log("warning", f"等待 {data.get('seconds', 1)} 秒")
        elif event_type == "phase":
            safe_update_progress(phase=data.get("phase"), message=data.get("phase"))
        elif event_type == "user_synced":
            safe_update_progress(phase="同步关注用户列表", current=data.get("total", 0), total=0)
        elif event_type == "user_start":
            safe_update_progress(
                phase=data.get("phase", "同步用户小说"),
                current=data.get("current", 0),
                total=data.get("total", 0) or 0,
                author=data.get("author", ""),
            )
        elif event_type == "novel_start":
            safe_update_progress(
                phase=data.get("phase", "同步用户小说"),
                current_novel=str(data.get("title", ""))[:40],
                author=data.get("author", ""),
            )
        elif event_type == "series_start":
            safe_update_progress(
                phase="同步追更系列",
                current=data.get("current", 0),
                total=data.get("total", 0),
                current_novel=str(data.get("title", ""))[:40],
            )

    return on_progress


def _accepts_parameter(func: Callable[..., Any], parameter_name: str) -> bool:
    import inspect

    return parameter_name in inspect.signature(func).parameters


def build_default_task_list(settings: Any) -> list[str]:
    sync = settings.sync
    tasks: list[str] = []

    if sync.sync_bookmarks:
        tasks.append("bookmark")
    if sync.sync_following_users:
        tasks.append("following_users")
    if sync.sync_following_novels:
        tasks.append("following_novels")
    if sync.sync_subscribed_series:
        tasks.append("subscribed_series")

    return tasks


def task_label(task_type: str) -> str:
    if task_type.startswith("user_backup:"):
        user_id = task_type.split(":", 1)[1]
        return f"用户 {user_id} 全量备份"

    return _TASK_LABELS.get(task_type, task_type)


def merge_stats(total: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    for key, value in update.items():
        current = total.get(key)
        if _is_addable_number(current) and _is_addable_number(value):
            total[key] = current + value
        else:
            total[key] = value

    return total


def _run_preference_analyze_task(settings: Any, context: dict[str, Any]) -> dict[str, Any]:
    """Phase 7.6: 偏好分析长任务"""
    from pixiv_novel_sync.storage_db import Database
    from pixiv_novel_sync.preferences import PreferenceAnalyzer

    reporter = _job_reporter_from_context(context)
    reporter.log("info", "=== 开始分析本地偏好 ===")

    db = Database(settings.storage.db_path)
    try:
        db.init_schema()
        analyzer = PreferenceAnalyzer(db)
        params = context.get("params", {})
        scope = params.get("scope", {})

        reporter.log("info", f"分析范围: {scope or '全部小说'}")
        result = analyzer.analyze_local(scope)

        # 保存profile到数据库
        profile_id = db.create_preference_profile({
            "name": params.get("name", "本地偏好画像"),
            "description": params.get("description", "基于本地归档小说自动统计生成"),
            "source_scope": result["source_scope"],
            "stats": result["stats"],
            "profile": result["profile"],
            "is_default": bool(params.get("is_default", True)),
        })

        reporter.log("success", f"分析完成: 创建profile #{profile_id}, 发现 {len(result['profile'].get('positive_tags', []))} 个偏好标签")
        return {"profile_id": profile_id, **result}
    finally:
        db.close()


def _run_recommendation_run_task(settings: Any, context: dict[str, Any]) -> dict[str, Any]:
    """Phase 7.6: 推荐运行长任务"""
    from pixiv_novel_sync.storage_db import Database
    from pixiv_novel_sync.recommendations import RecommendationService

    reporter = _job_reporter_from_context(context)
    stop_requested = _stop_requested_from_context(context)
    reporter.log("info", "=== 开始生成推荐 ===")

    db = Database(settings.storage.db_path)
    try:
        db.init_schema()
        service = RecommendationService(db, settings)
        params = context.get("params", {})
        profile_id = params.get("profile_id")
        search_plan = params.get("search_plan")

        def progress_callback(event_type: str, data: dict[str, Any]) -> None:
            if stop_requested():
                raise InterruptedError("Task stopped by user")
            if event_type == "phase":
                reporter.log("info", str(data.get("phase", "")))
            elif event_type == "rate_limit":
                reporter.log("warning", f"等待 {data.get('seconds', 1)} 秒")

        try:
            result = service.run(
                profile_id=profile_id,
                search_plan=search_plan,
                progress_callback=progress_callback,
            )
        except InterruptedError:
            reporter.log("info", "推荐任务已停止")
            return {"stopped": True, "discovered": 0}

        reporter.log("success", f"推荐完成: 发现 {result.get('discovered', 0)} 部小说")
        return result
    finally:
        db.close()

"""管理器类模块 - 从 webapp.py 提取

包含:
- SyncJobState: 同步任务状态数据类
- TASK_LABELS: 任务标签字典
- _task_label: 任务标签获取函数
- AutoSyncScheduler: 定时同步调度器
- SyncJobManager: 同步任务管理器
- SettingsManager: 设置管理器
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..jobs import services as job_services
from ..jobs.tasks import execute_task, merge_stats
from ..settings import Settings, load_settings
from ..storage_db import Database
from ..storage_files import FileStorage
from ..sync_check import build_sync_check_fingerprint
from .utils import (
    _atomic_write_yaml,
    _load_yaml_file,
    _normalize_float,
    _normalize_int,
    _normalize_optional_int,
    _settings_to_dict,
)

logger = logging.getLogger(__name__)
SCHEDULER_STOP_JOIN_TIMEOUT_SECONDS = 1.0


@dataclass(slots=True)
class SyncJobState:
    job_id: str
    status: str = "pending"
    message: str = "等待开始"
    started_at: float | None = None
    finished_at: float | None = None
    stats: dict[str, Any] | None = None
    error: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    logs: list[dict[str, Any]] = field(default_factory=list)
    task_list: list[str] = field(default_factory=list)  # 任务列表
    current_task_index: int = 0  # 当前执行的任务索引
    is_auto_sync: bool = False  # 是否是定时任务
    log_id: int | None = None  # 关联的日志 ID


TASK_LABELS = {
    "bookmark": "同步收藏小说",
    "bookmarks": "同步收藏小说",
    "following_users": "同步关注用户列表",
    "following_list": "同步关注用户列表",
    "following_novels": "同步关注用户小说",
    "subscribed_series": "同步追更系列",
    "user_status": "检查用户状态",
    "novel_status": "检查小说状态",
    "series_status": "检查系列状态",
    "user_backup": "全量备份关注用户小说",
    "pending_deletion_detection": "检测取消收藏/追更",
    "preference_analyze": "增量分析本地偏好",
}


def _task_label(task_type: str) -> str:
    return TASK_LABELS.get(task_type, task_type)


@dataclass
class AutoSyncScheduler:
    """定时同步调度器 - 每个任务独立运行"""
    config_path: str | None
    env_path: str | None
    sync_job_manager: Any = None  # SyncJobManager 引用
    _running: bool = False
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _task_last_run: dict[str, float] = field(default_factory=dict)  # 每个任务的上次运行时间
    _task_next_run: dict[str, float] = field(default_factory=dict)  # 每个任务的下次运行时间
    _task_intervals: dict[str, int] = field(default_factory=dict)  # 每个任务的间隔（小时）
    _task_crons: dict[str, str] = field(default_factory=dict)  # 每个任务的cron表达式
    _current_task_job_id: str | None = None  # 当前正在执行的定时任务 job id
    _stop_current_task: bool = False  # 停止当前任务的标志
    _task_finalizing: bool = False
    _last_cleanup_time: float = 0.0  # 上次清理日志的时间
    _catalog_initialization_attempted: bool = False
    _lifecycle_claim: Callable[[AutoSyncScheduler], bool] | None = field(default=None, repr=False)
    _lifecycle_release: Callable[[AutoSyncScheduler], None] | None = field(default=None, repr=False)
    
    def start(self) -> None:
        """启动定时调度器"""
        while True:
            with self._lock:
                if self._lifecycle_claim is not None and not self._lifecycle_claim(self):
                    logger.warning("Auto sync scheduler start skipped: another owner is active")
                    return
                if self._running:
                    return
                previous_thread = self._thread
                if previous_thread is None or not previous_thread.is_alive():
                    self._running = True
                    self._stop_current_task = False
                    self._task_finalizing = False
                    self._catalog_initialization_attempted = False
                    stop_event = threading.Event()
                    thread = threading.Thread(
                        target=self._run_scheduler_worker,
                        args=(stop_event,),
                        daemon=True,
                    )
                    self._stop_event = stop_event
                    self._thread = thread
                    try:
                        thread.start()
                    except BaseException:
                        self._thread = None
                        self._running = False
                        stop_event.set()
                        if self._lifecycle_release is not None:
                            self._lifecycle_release(self)
                        raise
                    logger.info("Auto sync scheduler started")
                    return
            previous_thread.join(timeout=SCHEDULER_STOP_JOIN_TIMEOUT_SECONDS)
            if previous_thread.is_alive():
                logger.warning("旧调度线程仍在停止，拒绝重复启动")
                return
    
    def stop(self) -> None:
        """停止定时调度器"""
        with self._lock:
            self._running = False
            self._stop_current_task = True
            self._stop_event.set()
            thread = self._thread
            logger.info("Auto sync scheduler stopped")
            if thread is None and self._lifecycle_release is not None:
                self._lifecycle_release(self)
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(timeout=SCHEDULER_STOP_JOIN_TIMEOUT_SECONDS)
            except RuntimeError:
                pass
        if thread is not None and not thread.is_alive():
            with self._lock:
                if self._thread is thread:
                    self._thread = None
                    if self._lifecycle_release is not None:
                        self._lifecycle_release(self)
    
    def stop_current_task(self) -> bool:
        """停止当前正在执行的定时任务"""
        with self._lock:
            if self._current_task_job_id and not self._task_finalizing:
                self._stop_current_task = True
                logger.info("Stopping current auto sync task: %s", self._current_task_job_id)
                return True
            return False
    
    def is_running(self) -> bool:
        return self._running
    
    def get_status(self) -> dict[str, Any]:
        """获取调度器状态"""
        with self._lock:
            return {
                "running": self._running,
                "current_task_job_id": self._current_task_job_id,
                "task_next_run": dict(self._task_next_run),
                "task_last_run": dict(self._task_last_run),
                "task_intervals": dict(self._task_intervals),
                "task_crons": dict(self._task_crons),
            }
    
    def _run_scheduler(self, stop_event: threading.Event | None = None) -> None:
        """调度器主循环 - 每个任务独立检查和执行"""
        stop_event = stop_event or self._stop_event
        self._run_scheduler_loop(stop_event)

    def _run_scheduler_worker(self, stop_event: threading.Event) -> None:
        try:
            self._run_scheduler(stop_event)
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
                    self._running = False
                    if self._lifecycle_release is not None:
                        self._lifecycle_release(self)

    def _run_scheduler_loop(self, stop_event: threading.Event) -> None:
        task_configs = [
            {"name": "bookmarks", "setting_check": "auto_sync_bookmarks_enabled", "sync_func": "_sync_bookmarks", "interval_setting": "auto_sync_bookmarks_interval_hours", "cron_setting": "auto_sync_bookmarks_cron"},
            {"name": "following_list", "setting_check": "auto_sync_following_list_enabled", "sync_func": "_sync_following_list", "interval_setting": "auto_sync_following_list_interval_hours", "cron_setting": "auto_sync_following_list_cron"},
            {"name": "following_novels", "setting_check": "auto_sync_following_novels_enabled", "sync_func": "_sync_following_novels", "interval_setting": "auto_sync_following_novels_interval_hours", "cron_setting": "auto_sync_following_novels_cron"},
            {"name": "subscribed_series", "setting_check": "auto_sync_subscribed_series_enabled", "sync_func": "_sync_subscribed_series", "interval_setting": "auto_sync_subscribed_series_interval_hours", "cron_setting": "auto_sync_subscribed_series_cron"},
            {"name": "user_status", "setting_check": "auto_sync_user_status_enabled", "sync_func": "_sync_user_status", "interval_setting": "auto_sync_user_status_interval_hours", "cron_setting": "auto_sync_user_status_cron"},
            {"name": "novel_status", "setting_check": "auto_sync_novel_status_enabled", "sync_func": "_sync_novel_status", "interval_setting": "auto_sync_novel_status_interval_hours", "cron_setting": "auto_sync_novel_status_cron"},
            {"name": "series_status", "setting_check": "auto_sync_series_status_enabled", "sync_func": "_sync_series_status", "interval_setting": "auto_sync_series_status_interval_hours", "cron_setting": "auto_sync_series_status_cron"},
            {"name": "user_backup", "setting_check": "auto_sync_user_backup_enabled", "sync_func": "_sync_user_backup", "interval_setting": "auto_sync_user_backup_interval_hours", "cron_setting": "auto_sync_user_backup_cron"},
            {"name": "pending_deletion_detection", "setting_check": "auto_sync_pending_detection_enabled", "sync_func": "_sync_pending_detection", "interval_setting": "auto_sync_pending_detection_interval_hours", "cron_setting": "auto_sync_pending_detection_cron"},
            {"name": "preference_analyze", "setting_check": "auto_sync_preference_analyze_enabled", "sync_func": "_sync_preference_analyze", "interval_setting": "auto_sync_preference_analyze_interval_hours", "cron_setting": "auto_sync_preference_analyze_cron"},
        ]

        while not stop_event.is_set():
            try:
                settings = load_settings(self.config_path, self.env_path)

                if not self._catalog_initialization_attempted:
                    self._catalog_initialization_attempted = True
                    self._initialize_rescue_catalog(settings)

                # 清理超过3天的任务日志（每小时执行一次）
                now_ts = time.time()
                if now_ts - self._last_cleanup_time > 3600:
                    db = None
                    try:
                        db = Database(settings.storage.db_path)
                        db.init_schema()
                        db.cleanup_old_task_logs(days=3)
                        db.cleanup_ai_jobs(keep_days=3)
                        self._last_cleanup_time = now_ts
                    except Exception as exc:
                        logger.warning("Failed to cleanup old task logs: %s", exc)
                    finally:
                        if db:
                            db.close()

                now = time.time()
                tz_name = settings.sync.auto_sync_timezone

                # 更新所有任务的配置信息（用于前端显示）
                for task_config in task_configs:
                    task_name = task_config["name"]
                    cron_expr = getattr(settings.sync, task_config["cron_setting"], "")
                    task_interval_hours = getattr(settings.sync, task_config["interval_setting"], 6)
                    self._task_intervals[task_name] = task_interval_hours
                    self._task_crons[task_name] = cron_expr

                if not settings.sync.auto_sync_enabled:
                    stop_event.wait(60)
                    continue

                for task_config in task_configs:
                    if stop_event.is_set():
                        break

                    task_name = task_config["name"]

                    if not getattr(settings.sync, task_config["setting_check"], False):
                        continue

                    cron_expr = getattr(settings.sync, task_config["cron_setting"], "")
                    task_interval_hours = getattr(settings.sync, task_config["interval_setting"], 6)
                    task_interval_seconds = task_interval_hours * 3600

                    # 调度器竞态修复:_task_next_run 读写纳入锁,避免 KeyError/漏更新
                    with self._lock:
                        if task_name not in self._task_next_run:
                            if cron_expr:
                                from ..settings import cron_to_next_run

                                self._task_next_run[task_name] = cron_to_next_run(cron_expr, now, tz_name) or (now + task_interval_seconds)
                            else:
                                self._task_next_run[task_name] = now + task_interval_seconds
                            logger.info(
                                "Task %s scheduled, next run: %s", task_name,
                                datetime.fromtimestamp(self._task_next_run[task_name]).strftime('%Y-%m-%d %H:%M:%S'),
                            )

                        next_run = self._task_next_run[task_name]
                        if time.time() >= next_run:
                            if self._current_task_job_id is not None:
                                logger.info("Task %s skipped: another task is running (%s)", task_name, self._current_task_job_id)
                                skip_now = time.time()
                                if cron_expr:
                                    from ..settings import cron_to_next_run

                                    self._task_next_run[task_name] = cron_to_next_run(cron_expr, skip_now, tz_name) or (skip_now + task_interval_seconds)
                                else:
                                    self._task_next_run[task_name] = skip_now + task_interval_seconds
                                continue
                        else:
                            continue

                    self._run_single_task(settings, task_name, task_config["sync_func"])

                    with self._lock:
                        self._task_last_run[task_name] = time.time()
                        if cron_expr:
                            from ..settings import cron_to_next_run

                            self._task_next_run[task_name] = cron_to_next_run(cron_expr, time.time(), tz_name) or (time.time() + task_interval_seconds)
                        else:
                            self._task_next_run[task_name] = time.time() + task_interval_seconds

                        logger.info(
                            "Task %s completed, next run: %s", task_name,
                            datetime.fromtimestamp(self._task_next_run[task_name]).strftime('%Y-%m-%d %H:%M:%S'),
                        )

                stop_event.wait(30)

            except Exception as exc:
                logger.error("Scheduler error: %s", exc)
                stop_event.wait(60)

    def _initialize_rescue_catalog(self, settings: Settings) -> None:
        db = None
        try:
            db = Database(settings.storage.db_path)
            db.init_schema()
            with db.transaction():
                if db.get_rescue_catalog_meta() is None:
                    db.rebuild_rescue_catalog()
        except Exception as exc:
            logger.warning("救援目录初始化失败: %s", exc)
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception as exc:
                    logger.warning("关闭救援目录初始化数据库失败: %s", exc)
    
    def _run_single_task(self, settings: Settings, task_name: str, sync_func_name: str) -> None:
        """执行单个定时任务"""
        logger.info("Starting auto sync task: %s", task_name)

        # 创建任务记录
        if self.sync_job_manager:
            job = self.sync_job_manager.start_auto_job(task_name, _task_label(task_name))
            if job is None:
                logger.info("Auto sync task %s skipped: another sync task is running", task_name)
                return
            with self._lock:
                self._current_task_job_id = job.job_id
                self._task_finalizing = False

            # 创建数据库日志记录
            db = None
            try:
                db = Database(settings.storage.db_path)
                db.init_schema()
                log_id = db.create_task_log(
                    task_type=task_name,
                    task_name=_task_label(task_name),
                    job_id=job.job_id,
                    is_auto_sync=True
                )
                job.log_id = log_id
            except Exception as e:
                logger.warning("Failed to create task log for %s: %s", task_name, e)
            finally:
                if db:
                    db.close()

            try:
                # 执行对应的同步函数
                func = getattr(self, sync_func_name)
                result = func(settings, job.job_id)
                with self._lock:
                    if isinstance(result, dict):
                        job.stats = result
                    cancelled = (
                        isinstance(result, dict) and result.get("stopped")
                    ) or (self._stop_current_task and not self._task_finalizing)
                    if cancelled:
                        job.status = "cancelled"
                        job.message = "任务已停止"
                        job.error = None
                    else:
                        job.status = "succeeded"
                        job.message = f"{_task_label(task_name)}完成"
                    self._task_finalizing = False
            except InterruptedError:
                # 用户主动停止：标记为 cancelled，而非 failed
                with self._lock:
                    job.status = "cancelled"
                    job.message = "任务已停止"
                    job.error = None
                    self._task_finalizing = False
                logger.info("Auto sync task %s stopped by user", task_name)
            except Exception as e:
                with self._lock:
                    job.status = "failed"
                    job.message = f"任务失败: {str(e)}"
                    job.error = str(e)
                    self._task_finalizing = False
                logger.error("Auto sync task %s failed: %s", task_name, str(e))
            finally:
                job.finished_at = time.time()
                # 更新数据库日志
                if job.log_id:
                    db = None
                    try:
                        db = Database(settings.storage.db_path)
                        db.init_schema()
                        db.update_task_log(job.log_id, job.status, stats=job.stats, error_message=job.error, logs=job.logs)
                    except Exception as e:
                        logger.warning("Failed to update task log: %s", e)
                    finally:
                        if db:
                            db.close()
                with self._lock:
                    self._current_task_job_id = None
                    self._stop_current_task = False
                    self._task_finalizing = False
                # ✅ Bug #1 修复: 将信号量释放移入 finally 确保始终执行
                try:
                    self.sync_job_manager._semaphore.release()
                except Exception as e:
                    logger.error("Failed to release semaphore: %s", e)
        else:
            # 没有 job_manager，直接执行
            func = getattr(self, sync_func_name, None)
            if func:
                func(settings, None)
    
    def _check_stop(self) -> bool:
        """检查是否需要停止"""
        return self._stop_current_task or not self._running

    def _claim_task_finalization(self) -> bool:
        with self._lock:
            if self._stop_current_task or not self._running:
                return False
            self._task_finalizing = True
            return True

    def _job_reporter(self, job_id: str | None) -> job_services.JobReporter:
        return job_services.JobReporter(manager=self.sync_job_manager, job_id=job_id)

    def _stop_requested_for_job(self, job_id: str | None) -> job_services.StopRequested:
        def stop_requested() -> bool:
            if self._check_stop():
                return True
            if job_id and self.sync_job_manager and hasattr(self.sync_job_manager, "is_cancel_requested"):
                return bool(self.sync_job_manager.is_cancel_requested(job_id))
            return False

        return stop_requested

    def _sync_bookmarks(self, settings: Settings, job_id: str | None) -> dict[str, Any] | None:
        """同步收藏"""
        from ..auth import PixivAuthManager
        from ..sync_engine import BookmarkNovelSyncService
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "info", "=== 开始同步收藏小说 ===")
        
        auth = PixivAuthManager(settings.pixiv)
        api, auth_result = auth.login()
        if auth_result.user_id is None:
            raise RuntimeError("Unable to determine user ID")
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "success", f"登录成功, 用户ID: {auth_result.user_id}")
        
        if self._check_stop():
            return {"stopped": True}
        
        db = Database(settings.storage.db_path)
        db.init_schema()
        storage = FileStorage(settings)
        storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])
        
        try:
            service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)
            
            def on_progress(event_type: str, data: dict[str, Any]) -> None:
                if self._check_stop():
                    raise InterruptedError("Task stopped by user")
                if job_id and self.sync_job_manager:
                    if event_type == "novel_start":
                        self.sync_job_manager.add_log(job_id, "info", f"[{data.get('current', '?')}/{data.get('total', '?')}] {data.get('title', '')[:30]}")
                        self.sync_job_manager.update_progress(
                            job_id, 
                            phase=data.get("phase", "同步收藏"), 
                            current=data.get('current', 0), 
                            total=data.get('total', 50),
                            current_novel=data.get('title', '')[:40],
                            author=data.get('author', ''),
                        )
                    elif event_type == "novel_done":
                        skipped = data.get('skipped', 0)
                        failed = data.get('failed', 0)
                        if failed:
                            self.sync_job_manager.add_log(job_id, "error", "  失败（详见服务日志）")
                        elif skipped:
                            self.sync_job_manager.add_log(job_id, "info", "  跳过（已存在）")
                        else:
                            self.sync_job_manager.add_log(job_id, "info", f"  完成: 收藏{data.get('bookmarks', 0)} 浏览{data.get('views', 0)}")
                    elif event_type == "page":
                        self.sync_job_manager.add_log(job_id, "info", f"正在获取第 {data.get('page', '?')} 页...")
                    elif event_type == "rate_limit":
                        self.sync_job_manager.add_log(job_id, "warning", f"等待 {data.get('seconds', 1)} 秒")
            
            total_stats: dict[str, Any] = {}
            for restrict in settings.sync.bookmark_restricts:
                if self._check_stop():
                    total_stats["stopped"] = True
                    return total_stats
                if job_id and self.sync_job_manager:
                    self.sync_job_manager.add_log(job_id, "info", f"同步{restrict}收藏...")
                stats = service.sync(
                    user_id=auth_result.user_id,
                    restricts=[restrict],
                    download_assets=settings.sync.download_assets,
                    write_markdown=settings.sync.write_markdown,
                    write_raw_text=settings.sync.write_raw_text,
                    progress_callback=on_progress,
                )
                merge_stats(total_stats, stats)
                if job_id and self.sync_job_manager:
                    self.sync_job_manager.add_log(job_id, "success", f"{restrict}收藏同步完成: 新增 {stats.get('novels', 0)} 本, 跳过 {stats.get('skipped', 0)} 本")
                time.sleep(settings.sync.delay_seconds_between_pages)

            if self._check_stop():
                total_stats["stopped"] = True
                return total_stats
            if not self._claim_task_finalization():
                total_stats["stopped"] = True
                return total_stats
            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "success", "收藏同步完成")
            total_stats.update(job_services._rebuild_rescue_catalog(db, self._job_reporter(job_id)))
            return total_stats
        finally:
            db.close()
    
    def _sync_following_list(self, settings: Settings, job_id: str | None) -> None:
        """同步关注用户列表"""
        from ..auth import PixivAuthManager
        from ..sync_engine import BookmarkNovelSyncService
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "info", "=== 开始同步关注用户列表 ===")
        
        auth = PixivAuthManager(settings.pixiv)
        api, auth_result = auth.login()
        if auth_result.user_id is None:
            raise RuntimeError("Unable to determine user ID")
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "success", f"登录成功, 用户ID: {auth_result.user_id}")
        
        if self._check_stop():
            return {"stopped": True}
        
        db = Database(settings.storage.db_path)
        db.init_schema()
        storage = FileStorage(settings)
        storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])
        
        try:
            service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)
            
            def on_progress(event_type: str, data: dict[str, Any]) -> None:
                if self._check_stop():
                    raise InterruptedError("Task stopped by user")
                if job_id and self.sync_job_manager:
                    if event_type == "user_synced":
                        self.sync_job_manager.add_log(job_id, "info", f"[{data.get('total', '?')}] {data.get('name', '')}")
                        self.sync_job_manager.update_progress(
                            job_id,
                            phase="同步关注用户列表",
                            current=data.get('total', 0),
                            total=0,
                        )
                    elif event_type == "page":
                        self.sync_job_manager.add_log(job_id, "info", f"正在获取第 {data.get('page', '?')} 页...")
                    elif event_type == "rate_limit":
                        self.sync_job_manager.add_log(job_id, "warning", f"等待 {data.get('seconds', 1)} 秒")
            
            if self._check_stop():
                return {"stopped": True}
            
            stats = service.sync_following_list(progress_callback=on_progress)
            
            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "success", f"关注用户列表同步完成: 更新 {stats.get('users', 0)} 位用户")
        finally:
            db.close()
    
    def _sync_following_novels(self, settings: Settings, job_id: str | None) -> dict[str, Any] | None:
        """同步关注用户小说"""
        from ..auth import PixivAuthManager
        from ..sync_engine import BookmarkNovelSyncService
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "info", "=== 开始同步关注用户小说 ===")
        
        auth = PixivAuthManager(settings.pixiv)
        api, auth_result = auth.login()
        if auth_result.user_id is None:
            raise RuntimeError("Unable to determine user ID")
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "success", f"登录成功, 用户ID: {auth_result.user_id}")
        
        if self._check_stop():
            return {"stopped": True}
        
        db = Database(settings.storage.db_path)
        db.init_schema()
        storage = FileStorage(settings)
        storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])
        
        try:
            service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)
            
            def on_progress(event_type: str, data: dict[str, Any]) -> None:
                if self._check_stop():
                    raise InterruptedError("Task stopped by user")
                if job_id and self.sync_job_manager:
                    if event_type == "user_start":
                        total = data.get('total', 0)
                        total_str = str(total) if total > 0 else '?'
                        self.sync_job_manager.add_log(job_id, "info", f"[{data.get('current', '?')}/{total_str}] {data.get('author', '')}")
                        self.sync_job_manager.update_progress(
                            job_id,
                            phase=data.get("phase", "同步用户小说"),
                            current=data.get('current', 0),
                            total=total or 50,
                            current_novel='',
                            author=data.get('author', ''),
                        )
                    elif event_type == "novel_start":
                        self.sync_job_manager.add_log(job_id, "info", f"  › {data.get('title', '')[:30]}")
                        self.sync_job_manager.update_progress(
                            job_id,
                            phase=data.get("phase", "同步用户小说"),
                            current_novel=data.get('title', '')[:40],
                            author=data.get('author', ''),
                        )
                    elif event_type == "novel_done":
                        skipped = data.get('skipped', 0)
                        failed = data.get('failed', 0)
                        if failed:
                            self.sync_job_manager.add_log(job_id, "error", "  失败（详见服务日志）")
                        elif skipped:
                            self.sync_job_manager.add_log(job_id, "info", "  跳过（已存在）")
                        else:
                            self.sync_job_manager.add_log(job_id, "info", f"  完成: 收藏{data.get('bookmarks', 0)} 浏览{data.get('views', 0)}")
                    elif event_type == "page":
                        self.sync_job_manager.add_log(job_id, "info", f"正在获取第 {data.get('page', '?')} 页...")
                    elif event_type == "rate_limit":
                        self.sync_job_manager.add_log(job_id, "warning", f"等待 {data.get('seconds', 1)} 秒")
            
            if self._check_stop():
                return {"stopped": True}
            
            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "info", "开始扫描关注用户的新小说 (全部用户)...")

            stats = service.sync_following_novels(
                download_assets=settings.sync.download_assets,
                write_markdown=settings.sync.write_markdown,
                write_raw_text=settings.sync.write_raw_text,
                progress_callback=on_progress,
                users_limit=0,
            )

            if self._check_stop():
                stats["stopped"] = True
                return stats
            if not self._claim_task_finalization():
                stats["stopped"] = True
                return stats
            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "success", f"关注用户小说同步完成: 同步 {stats.get('novels', 0)} 本, 跳过 {stats.get('skipped', 0)} 本, 用户 {stats.get('following_users_scanned', 0)} 人")
            stats.update(job_services._rebuild_rescue_catalog(db, self._job_reporter(job_id)))
            return stats
        finally:
            db.close()
    
    def _sync_subscribed_series(self, settings: Settings, job_id: str | None) -> dict[str, Any] | None:
        """同步追更系列"""
        from ..auth import PixivAuthManager
        from ..sync_engine import BookmarkNovelSyncService
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "info", "=== 开始同步追更系列 ===")
        
        auth = PixivAuthManager(settings.pixiv)
        api, auth_result = auth.login()
        if auth_result.user_id is None:
            raise RuntimeError("Unable to determine user ID")
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "success", f"登录成功, 用户ID: {auth_result.user_id}")
        
        if self._check_stop():
            return {"stopped": True}
        
        db = Database(settings.storage.db_path)
        db.init_schema()
        storage = FileStorage(settings)
        storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])
        
        try:
            service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)
            
            def on_progress(event_type: str, data: dict[str, Any]) -> None:
                if self._check_stop():
                    raise InterruptedError("Task stopped by user")
                if job_id and self.sync_job_manager:
                    if event_type == "phase":
                        self.sync_job_manager.add_log(job_id, "info", data.get("phase", ""))
                    elif event_type == "series_start":
                        self.sync_job_manager.add_log(job_id, "info", f"[{data.get('current', '?')}/{data.get('total', '?')}] {data.get('title', '')[:30]}")
                        self.sync_job_manager.update_progress(
                            job_id,
                            phase="同步追更系列",
                            current=data.get('current', 0),
                            total=data.get('total', 0),
                            current_novel=data.get('title', '')[:40],
                        )
                    elif event_type == "rate_limit":
                        self.sync_job_manager.add_log(job_id, "warning", f"等待 {data.get('seconds', 1)} 秒")
            
            if self._check_stop():
                return {"stopped": True}
            
            limit = settings.sync.series_sync_limit
            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "info", f"获取订阅系列列表 (限制: {limit or '全部'})...")
            
            stats = service.sync_subscribed_series(
                limit=limit,
                progress_callback=on_progress,
            )

            if self._check_stop():
                stats["stopped"] = True
                return stats
            if not self._claim_task_finalization():
                stats["stopped"] = True
                return stats
            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "success", f"追更系列同步完成: {stats.get('series_synced', 0)} 个系列")
            stats.update(job_services._rebuild_rescue_catalog(db, self._job_reporter(job_id)))
            return stats
        finally:
            db.close()
    
    def _sync_user_status(self, settings: Settings, job_id: str | None) -> dict[str, Any]:
        """同步关注用户的存续状态"""
        return job_services.run_user_status_task(
            settings,
            reporter=self._job_reporter(job_id),
            stop_requested=self._stop_requested_for_job(job_id),
        )

    def _sync_novel_status(self, settings: Settings, job_id: str | None) -> dict[str, Any]:
        """检查所有小说的存续状态"""
        return job_services.run_novel_status_task(
            settings,
            reporter=self._job_reporter(job_id),
            stop_requested=self._stop_requested_for_job(job_id),
            claim_finalization=self._claim_task_finalization,
        )

    def _sync_series_status(self, settings: Settings, job_id: str | None) -> dict[str, Any]:
        """检查所有系列的存续状态"""
        return job_services.run_series_status_task(
            settings,
            reporter=self._job_reporter(job_id),
            stop_requested=self._stop_requested_for_job(job_id),
            claim_finalization=self._claim_task_finalization,
        )

    def _sync_user_backup(self, settings: Settings, job_id: str | None) -> dict[str, Any] | None:
        """定时全量备份关注用户小说（按 users_limit 轮询）"""
        db = Database(Path(settings.storage.db_path))
        db.init_schema()

        try:
            all_user_ids = [r[0] for r in db.conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()]
            total_users = len(all_user_ids)
            watermark = db.get_watermark("user_backup_rotation")
            offset = watermark.get("offset", 0) if watermark else 0
            if offset >= total_users:
                offset = 0

            users_limit = settings.sync.auto_sync_following_novels_users_limit
            if users_limit <= 0:
                users_limit = total_users

            batch = all_user_ids[offset:offset + users_limit]

            if batch and job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "info", f"=== 全量备份关注用户小说: 用户 {offset+1}-{offset+len(batch)}/{total_users}, 本轮 {len(batch)} 人 ===")

            total_novels = 0
            total_skipped = 0
            total_assets = 0
            reporter = self._job_reporter(job_id)
            stop_requested = self._stop_requested_for_job(job_id)
            stopped = False
            completed_users = 0

            for idx, uid in enumerate(batch):
                if stop_requested():
                    stopped = True
                    break
                if job_id and self.sync_job_manager:
                    self.sync_job_manager.update_progress(job_id, phase="全量备份", current=idx + 1, total=len(batch))
                stats = job_services.run_user_backup_task(
                    settings,
                    int(uid),
                    reporter=reporter,
                    stop_requested=stop_requested,
                    rebuild_catalog=False,
                )
                total_novels += int(stats.get("novels", 0) or 0)
                total_skipped += int(stats.get("skipped", 0) or 0)
                total_assets += int(stats.get("assets_downloaded", 0) or 0)
                if stats.get("stopped"):
                    stopped = True
                    break
                completed_users += 1

            if not stopped and stop_requested():
                stopped = True

            next_offset = offset + completed_users
            if next_offset >= total_users:
                next_offset = 0

            if total_users:
                db.update_watermark("user_backup_rotation", {
                    "offset": next_offset,
                    "last_sync_time": datetime.now(timezone.utc).isoformat(),
                })

            batch_stats: dict[str, Any] = {
                "novels": total_novels,
                "skipped": total_skipped,
                "assets_downloaded": total_assets,
                "stopped": stopped,
            }
            if not stopped:
                if self._claim_task_finalization():
                    batch_stats.update(job_services._rebuild_rescue_catalog(db, reporter))
                else:
                    stopped = True
                    batch_stats["stopped"] = True

            if job_id and self.sync_job_manager and hasattr(self.sync_job_manager, "get_job"):
                job = self.sync_job_manager.get_job(job_id)
                if job is not None:
                    job.stats = batch_stats

            if job_id and self.sync_job_manager:
                level = "info" if stopped else "success"
                suffix = "已停止" if stopped else "完成"
                self.sync_job_manager.add_log(job_id, level, f"全量备份{suffix}: 同步 {total_novels} 本, 跳过 {total_skipped} 本, 资源 {total_assets} 个")
            return batch_stats
        finally:
            db.close()

    def _sync_pending_detection(self, settings: Settings, job_id: str | None) -> dict[str, Any]:
        """检测取消收藏/追更"""
        return job_services.run_pending_deletion_detection_task(
            settings,
            reporter=self._job_reporter(job_id),
            stop_requested=self._stop_requested_for_job(job_id),
        )

    def _sync_preference_analyze(self, settings: Settings, job_id: str | None) -> dict[str, Any] | None:
        """增量分析本地偏好。定时任务每次只处理一批。"""
        return execute_task(
            "preference_analyze",
            settings,
            {
                "manager": self.sync_job_manager,
                "job_id": job_id,
                "params": {
                    "scope": {
                        "batch_size": int(getattr(settings.sync, "preference_analyze_batch_size", 200) or 200),
                        "max_batches": 1,
                    }
                },
            },
        )


@dataclass(slots=True)
class SyncJobManager:
    config_path: str | None
    env_path: str | None
    _jobs: dict[str, SyncJobState] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _semaphore: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(1))
    MAX_LOGS: int = 50
    MAX_JOBS: int = 100  # 最多保留的任务数

    def _cleanup_old_jobs(self) -> None:
        """清理已完成的旧任务，保留最近 MAX_JOBS 个"""
        if len(self._jobs) <= self.MAX_JOBS:
            return
        done_jobs = [(jid, j) for jid, j in self._jobs.items() if j.status != "running"]
        done_jobs.sort(key=lambda x: x[1].finished_at or 0, reverse=True)
        for jid, _ in done_jobs[self.MAX_JOBS:]:
            del self._jobs[jid]

    def start_auto_job(self, task_name: str, task_label: str) -> SyncJobState | None:
        """启动定时任务"""
        # ✅ Bug #1 修复: 使用 acquired 标志追踪信号量状态
        acquired = False
        try:
            acquired = self._semaphore.acquire(blocking=False)
            if not acquired:
                return None

            with self._lock:
                job_id = f"auto_{task_name}_{int(time.time() * 1000)}"
                job = SyncJobState(
                    job_id=job_id,
                    status="running",
                    message=f"定时任务: {task_label}",
                    started_at=time.time(),
                    task_list=[task_label],
                    is_auto_sync=True,
                )
                self._jobs[job_id] = job
                return job
        except Exception:
            if acquired:
                self._semaphore.release()
            raise

    def get_job(self, job_id: str) -> SyncJobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest_job(self) -> SyncJobState | None:
        with self._lock:
            if not self._jobs:
                return None
            # 按 started_at 排序，而非字符串排序
            return max(self._jobs.values(), key=lambda j: j.started_at or 0)

    def latest_matching_sync_check_scope(self, settings: Settings, user_id: int | None, task_type: str) -> tuple[str, str] | None:
        fingerprint = build_sync_check_fingerprint(settings, user_id)
        with self._lock:
            jobs = sorted(
                self._jobs.values(),
                key=lambda job: job.finished_at or job.started_at or 0,
                reverse=True,
            )
            for job in jobs:
                if job.status != "succeeded":
                    continue
                scope = job.progress.get("sync_check_scope")
                if not scope:
                    continue
                if job.progress.get("sync_check_fingerprint") != fingerprint:
                    continue
                task_types = job.progress.get("sync_check_task_types") or []
                if task_type not in task_types:
                    continue
                return str(scope), job.job_id
        return None

    def has_running_jobs(self) -> bool:
        with self._lock:
            return any(j.status == "running" for j in self._jobs.values())

    def add_log(self, job_id: str, level: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            log_entry = {
                "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "level": level,
                "message": message,
            }
            job.logs.append(log_entry)
            if len(job.logs) > self.MAX_LOGS:
                job.logs = job.logs[-self.MAX_LOGS:]

    def update_progress(self, job_id: str, **kwargs: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.progress.update(kwargs)
            job.message = kwargs.get("message", job.message)

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            return bool(job.progress.get("cancel_requested", False))


class SettingsManager:
    def __init__(self, config_path: str | None) -> None:
        self.config_path = config_path
        self._cache: Settings | None = None
        self._cache_time: float = 0.0
        self._cache_ttl: float = 5.0  # 缓存 5 秒

    def load(self, env_path: str | None = None) -> Settings:
        now = time.time()
        if self._cache is not None and (now - self._cache_time) < self._cache_ttl:
            return self._cache
        settings = load_settings(self.config_path, env_path)
        self._cache = settings
        self._cache_time = now
        return settings

    def invalidate(self) -> None:
        """手动失效缓存（保存设置后调用）。"""
        self._cache = None
        self._cache_time = 0.0

    def save_sync_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.config_path:
            raise ValueError("缺少 config_path，无法保存设置")

        config_path = Path(self.config_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_data = _load_yaml_file(config_path)
        sync_data = config_data.setdefault("sync", {})

        sync_data["enabled"] = bool(payload.get("enabled", sync_data.get("enabled", False)))
        sync_data["initial_manual_only"] = bool(payload.get("initial_manual_only", sync_data.get("initial_manual_only", True)))
        sync_data["download_assets"] = bool(payload.get("download_assets", sync_data.get("download_assets", True)))
        sync_data["write_markdown"] = bool(payload.get("write_markdown", sync_data.get("write_markdown", True)))
        sync_data["write_raw_text"] = bool(payload.get("write_raw_text", sync_data.get("write_raw_text", True)))

        bookmark_restricts = payload.get("bookmark_restricts", sync_data.get("bookmark_restricts", ["public", "private"]))
        if not isinstance(bookmark_restricts, list) or not bookmark_restricts:
            raise ValueError("bookmark_restricts 必须为非空数组")
        normalized_restricts: list[str] = []
        for item in bookmark_restricts:
            restrict = str(item).strip().lower()
            if restrict not in {"public", "private"}:
                raise ValueError("bookmark_restricts 只能包含 public 或 private")
            if restrict not in normalized_restricts:
                normalized_restricts.append(restrict)
        sync_data["bookmark_restricts"] = normalized_restricts

        sync_data["max_items_per_run"] = _normalize_optional_int(payload.get("max_items_per_run", sync_data.get("max_items_per_run")))
        sync_data["max_pages_per_run"] = _normalize_optional_int(payload.get("max_pages_per_run", sync_data.get("max_pages_per_run")))
        sync_data["delay_seconds_between_items"] = _normalize_float(
            payload.get("delay_seconds_between_items", sync_data.get("delay_seconds_between_items", 1.0))
        )
        sync_data["delay_seconds_between_pages"] = _normalize_float(
            payload.get("delay_seconds_between_pages", sync_data.get("delay_seconds_between_pages", 1.0))
        )

        sync_data["sync_bookmarks"] = bool(payload.get("sync_bookmarks", sync_data.get("sync_bookmarks", True)))
        sync_data["sync_following_users"] = bool(payload.get("sync_following_users", sync_data.get("sync_following_users", True)))
        sync_data["sync_following_novels"] = bool(payload.get("sync_following_novels", sync_data.get("sync_following_novels", True)))
        sync_data["sync_subscribed_series"] = bool(payload.get("sync_subscribed_series", sync_data.get("sync_subscribed_series", True)))
        
        # 系列同步限制
        series_limit_raw = payload.get("series_sync_limit", sync_data.get("series_sync_limit", 0))
        if series_limit_raw in (None, ""):
            sync_data["series_sync_limit"] = 0
        else:
            sync_data["series_sync_limit"] = max(_normalize_int(series_limit_raw, 0), 0)
        
        # 系列限速设置
        sync_data["delay_seconds_between_series"] = _normalize_float(
            payload.get("delay_seconds_between_series", sync_data.get("delay_seconds_between_series", 3.0))
        )
        sync_data["delay_seconds_between_chapters"] = _normalize_float(
            payload.get("delay_seconds_between_chapters", sync_data.get("delay_seconds_between_chapters", 1.0))
        )
        sync_data["delay_seconds_between_skips"] = _normalize_float(
            payload.get("delay_seconds_between_skips", sync_data.get("delay_seconds_between_skips", 0.1))
        )
        
        # 定时同步设置（auto_sync_enabled 由首页按钮单独控制）
        sync_data["auto_sync_timezone"] = str(payload.get("auto_sync_timezone", sync_data.get("auto_sync_timezone", "UTC")))

        # 校验 cron 表达式合法性的辅助函数
        from ..settings import cron_to_next_run as _cron_check

        def _save_cron(field_name: str, default: str = "") -> str:
            value = str(payload.get(field_name, sync_data.get(field_name, default)) or "")
            value = value.strip()
            if value and _cron_check(value, None, sync_data.get("auto_sync_timezone", "UTC")) is None:
                raise ValueError(f"非法的 cron 表达式: {field_name}={value!r}")
            return value

        def _save_int(field_name: str, default: int, min_value: int = 1) -> int:
            value = _normalize_int(payload.get(field_name, sync_data.get(field_name, default)), default)
            return max(value, min_value)

        sync_data["auto_sync_bookmarks_enabled"] = bool(payload.get("auto_sync_bookmarks_enabled", sync_data.get("auto_sync_bookmarks_enabled", True)))
        sync_data["auto_sync_bookmarks_interval_hours"] = _save_int("auto_sync_bookmarks_interval_hours", 6)
        sync_data["auto_sync_bookmarks_cron"] = _save_cron("auto_sync_bookmarks_cron")
        sync_data["auto_sync_following_list_enabled"] = bool(payload.get("auto_sync_following_list_enabled", sync_data.get("auto_sync_following_list_enabled", True)))
        sync_data["auto_sync_following_list_interval_hours"] = _save_int("auto_sync_following_list_interval_hours", 24)
        sync_data["auto_sync_following_list_cron"] = _save_cron("auto_sync_following_list_cron")
        sync_data["auto_sync_following_novels_enabled"] = bool(payload.get("auto_sync_following_novels_enabled", sync_data.get("auto_sync_following_novels_enabled", True)))
        sync_data["auto_sync_following_novels_interval_hours"] = _save_int("auto_sync_following_novels_interval_hours", 6)
        sync_data["auto_sync_following_novels_cron"] = _save_cron("auto_sync_following_novels_cron")
        sync_data["auto_sync_following_novels_users_limit"] = _save_int("auto_sync_following_novels_users_limit", 0, min_value=0)
        sync_data["auto_sync_user_status_enabled"] = bool(payload.get("auto_sync_user_status_enabled", sync_data.get("auto_sync_user_status_enabled", True)))
        sync_data["auto_sync_user_status_interval_hours"] = _save_int("auto_sync_user_status_interval_hours", 6)
        sync_data["auto_sync_user_status_cron"] = _save_cron("auto_sync_user_status_cron")
        sync_data["auto_sync_novel_status_enabled"] = bool(payload.get("auto_sync_novel_status_enabled", sync_data.get("auto_sync_novel_status_enabled", True)))
        sync_data["auto_sync_novel_status_interval_hours"] = _save_int("auto_sync_novel_status_interval_hours", 6)
        sync_data["auto_sync_novel_status_cron"] = _save_cron("auto_sync_novel_status_cron")
        sync_data["auto_sync_series_status_enabled"] = bool(payload.get("auto_sync_series_status_enabled", sync_data.get("auto_sync_series_status_enabled", True)))
        sync_data["auto_sync_series_status_interval_hours"] = _save_int("auto_sync_series_status_interval_hours", 6)
        sync_data["auto_sync_series_status_cron"] = _save_cron("auto_sync_series_status_cron")
        sync_data["auto_sync_subscribed_series_enabled"] = bool(payload.get("auto_sync_subscribed_series_enabled", sync_data.get("auto_sync_subscribed_series_enabled", True)))
        sync_data["auto_sync_subscribed_series_interval_hours"] = _save_int("auto_sync_subscribed_series_interval_hours", 6)
        sync_data["auto_sync_subscribed_series_cron"] = _save_cron("auto_sync_subscribed_series_cron")
        sync_data["auto_sync_user_backup_enabled"] = bool(payload.get("auto_sync_user_backup_enabled", sync_data.get("auto_sync_user_backup_enabled", False)))
        sync_data["auto_sync_user_backup_interval_hours"] = _save_int("auto_sync_user_backup_interval_hours", 24)
        sync_data["auto_sync_user_backup_cron"] = _save_cron("auto_sync_user_backup_cron")
        sync_data["auto_sync_pending_detection_enabled"] = bool(payload.get("auto_sync_pending_detection_enabled", sync_data.get("auto_sync_pending_detection_enabled", True)))
        sync_data["auto_sync_pending_detection_interval_hours"] = _save_int("auto_sync_pending_detection_interval_hours", 12)
        sync_data["auto_sync_pending_detection_cron"] = _save_cron("auto_sync_pending_detection_cron")
        sync_data["auto_sync_preference_analyze_enabled"] = bool(payload.get("auto_sync_preference_analyze_enabled", sync_data.get("auto_sync_preference_analyze_enabled", False)))
        sync_data["auto_sync_preference_analyze_interval_hours"] = _save_int("auto_sync_preference_analyze_interval_hours", 1)
        sync_data["auto_sync_preference_analyze_cron"] = _save_cron("auto_sync_preference_analyze_cron", "*/30 * * * *")
        sync_data["preference_analyze_batch_size"] = _save_int("preference_analyze_batch_size", 200, min_value=10)
        sync_data["pending_deletion_grace_period_days"] = _save_int("pending_deletion_grace_period_days", 30)
        sync_data["pending_deletion_cleanup_confirmed_days"] = _save_int("pending_deletion_cleanup_confirmed_days", 7)

        _atomic_write_yaml(config_path, config_data)

        self.invalidate()
        return _settings_to_dict(load_settings(config_path, None))

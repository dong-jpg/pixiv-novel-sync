from __future__ import annotations

import json
import os
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests as http_requests
import yaml
from flask import Flask, Response, jsonify, redirect, render_template, request, session, send_file

from .jobs import services as job_services
from .jobs.manager import JobManager
from .jobs.models import JobSource, JobSpec, JobState, JobStatus, JobType
from .jobs.quick_sync import run_bookmark_sync, run_check_bookmarks_task
from .jobs.runner import JobRunner
from .jobs.tasks import build_default_task_list, execute_task
from .auth import PixivAuthManager
from .oauth_helper import OAuthManager
from .settings import Settings, load_settings
from .storage_db import Database
from .storage_files import FileStorage
from .sync_check import build_sync_check_fingerprint
from .sync_engine import BookmarkNovelSyncService
from .utils_naming import safe_name

logger = logging.getLogger(__name__)

# 记录服务启动时间（用于健康检查 API 计算 uptime）
_service_start_time: float = time.time()


def _atomic_write_yaml(path: Path, data: Any) -> None:
    """Write YAML to ``path`` atomically (temp file in the same dir + os.replace).

    Avoids truncating/corrupting config.yaml if the process crashes mid-write, and
    keeps a single serialization style across every config writer.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as file:
        yaml.safe_dump(data, file, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)


def _oauth_task_public_payload(task: Any, mode: str) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "status": task.status,
        "message": task.message,
        "has_refresh_token": bool(task.refresh_token),
        "has_access_token": bool(task.access_token),
        "user_id": task.user_id,
        "mode": mode,
    }


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
    _task_last_run: dict[str, float] = field(default_factory=dict)  # 每个任务的上次运行时间
    _task_next_run: dict[str, float] = field(default_factory=dict)  # 每个任务的下次运行时间
    _task_intervals: dict[str, int] = field(default_factory=dict)  # 每个任务的间隔（小时）
    _task_crons: dict[str, str] = field(default_factory=dict)  # 每个任务的cron表达式
    _current_task_job_id: str | None = None  # 当前正在执行的定时任务 job id
    _stop_current_task: bool = False  # 停止当前任务的标志
    _last_cleanup_time: float = 0.0  # 上次清理日志的时间
    
    def start(self) -> None:
        """启动定时调度器"""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._run_scheduler, daemon=True)
            self._thread.start()
            logger.info("Auto sync scheduler started")
    
    def stop(self) -> None:
        """停止定时调度器"""
        with self._lock:
            self._running = False
            self._stop_current_task = True
            logger.info("Auto sync scheduler stopped")
    
    def stop_current_task(self) -> bool:
        """停止当前正在执行的定时任务"""
        with self._lock:
            if self._current_task_job_id:
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
    
    def _run_scheduler(self) -> None:
        """调度器主循环 - 每个任务独立检查和执行"""
        # 任务定义
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
        ]
        
        while self._running:
            try:
                settings = load_settings(self.config_path, self.env_path)
                
                # 清理超过3天的任务日志（每小时执行一次）
                now_ts = time.time()
                if now_ts - self._last_cleanup_time > 3600:
                    db = None
                    try:
                        db = Database(settings.storage.db_path)
                        db.init_schema()
                        db.cleanup_old_task_logs(days=3)
                        self._last_cleanup_time = now_ts
                    except Exception as e:
                        logger.warning("Failed to cleanup old task logs: %s", e)
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
                    time.sleep(60)
                    continue
                
                for task_config in task_configs:
                    if not self._running:
                        break

                    task_name = task_config["name"]

                    if not getattr(settings.sync, task_config["setting_check"], False):
                        continue

                    cron_expr = getattr(settings.sync, task_config["cron_setting"], "")
                    task_interval_hours = getattr(settings.sync, task_config["interval_setting"], 6)
                    task_interval_seconds = task_interval_hours * 3600

                    # 调度器竞态修复:_task_next_run 读写纳入锁,避免 KeyError/漏更新
                    with self._lock:
                        # 如果该任务还没有计算过下次运行时间，现在计算
                        if task_name not in self._task_next_run:
                            if cron_expr:
                                from .settings import cron_to_next_run
                                self._task_next_run[task_name] = cron_to_next_run(cron_expr, now, tz_name) or (now + task_interval_seconds)
                            else:
                                self._task_next_run[task_name] = now + task_interval_seconds
                            logger.info("Task %s scheduled, next run: %s", task_name,
                                        datetime.fromtimestamp(self._task_next_run[task_name]).strftime('%Y-%m-%d %H:%M:%S'))

                        next_run = self._task_next_run[task_name]

                        if time.time() >= next_run:
                            if self._current_task_job_id is not None:
                                logger.info("Task %s skipped: another task is running (%s)", task_name, self._current_task_job_id)
                                skip_now = time.time()
                                if cron_expr:
                                    from .settings import cron_to_next_run
                                    self._task_next_run[task_name] = cron_to_next_run(cron_expr, skip_now, tz_name) or (skip_now + task_interval_seconds)
                                else:
                                    self._task_next_run[task_name] = skip_now + task_interval_seconds
                                continue
                        else:
                            # 未到运行时间,跳过
                            continue

                    # 锁外执行任务(避免阻塞其他任务调度检查)
                    self._run_single_task(settings, task_name, task_config["sync_func"])

                    # 任务完成后更新下次运行时间(加锁)
                    with self._lock:
                        self._task_last_run[task_name] = time.time()
                        if cron_expr:
                            from .settings import cron_to_next_run
                            self._task_next_run[task_name] = cron_to_next_run(cron_expr, time.time(), tz_name) or (time.time() + task_interval_seconds)
                        else:
                            self._task_next_run[task_name] = time.time() + task_interval_seconds

                        logger.info("Task %s completed, next run: %s", task_name,
                                    datetime.fromtimestamp(self._task_next_run[task_name]).strftime('%Y-%m-%d %H:%M:%S'))
                
                time.sleep(30)
                
            except Exception as e:
                logger.error("Scheduler error: %s", str(e))
                time.sleep(60)
    
    def _run_single_task(self, settings: Settings, task_name: str, sync_func_name: str) -> None:
        """执行单个定时任务"""
        logger.info("Starting auto sync task: %s", task_name)

        # 创建任务记录
        if self.sync_job_manager:
            job = self.sync_job_manager.start_auto_job(task_name, _task_label(task_name))
            if job is None:
                logger.info("Auto sync task %s skipped: another sync task is running", task_name)
                return
            self._current_task_job_id = job.job_id

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
                func(settings, job.job_id)
                job.status = "succeeded"
                job.message = f"{_task_label(task_name)}完成"
            except Exception as e:
                job.status = "failed"
                job.message = f"任务失败: {str(e)}"
                job.error = str(e)
                logger.error("Auto sync task %s failed: %s", task_name, str(e))
            finally:
                job.finished_at = time.time()
                # 更新数据库日志
                if job.log_id:
                    db = None
                    try:
                        db = Database(settings.storage.db_path)
                        db.init_schema()
                        db.update_task_log(job.log_id, job.status, stats=job.stats, logs=job.logs)
                    except Exception as e:
                        logger.warning("Failed to update task log: %s", e)
                    finally:
                        if db:
                            db.close()
                with self._lock:
                    self._current_task_job_id = None
                    self._stop_current_task = False
                self.sync_job_manager._semaphore.release()
        else:
            # 没有 job_manager，直接执行
            func = getattr(self, sync_func_name, None)
            if func:
                func(settings, None)
    
    def _check_stop(self) -> bool:
        """检查是否需要停止"""
        return self._stop_current_task or not self._running

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

    def _sync_bookmarks(self, settings: Settings, job_id: str | None) -> None:
        """同步收藏"""
        from .auth import PixivAuthManager
        from .sync_engine import BookmarkNovelSyncService
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "info", "=== 开始同步收藏小说 ===")
        
        auth = PixivAuthManager(settings.pixiv)
        api, auth_result = auth.login()
        if auth_result.user_id is None:
            raise RuntimeError("Unable to determine user ID")
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "success", f"登录成功, 用户ID: {auth_result.user_id}")
        
        if self._check_stop():
            return
        
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
            
            for restrict in settings.sync.bookmark_restricts:
                if self._check_stop():
                    return
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
                if job_id and self.sync_job_manager:
                    self.sync_job_manager.add_log(job_id, "success", f"{restrict}收藏同步完成: 新增 {stats.get('novels', 0)} 本, 跳过 {stats.get('skipped', 0)} 本")
                time.sleep(settings.sync.delay_seconds_between_pages)
            
            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "success", "收藏同步完成")
        finally:
            db.close()
    
    def _sync_following_list(self, settings: Settings, job_id: str | None) -> None:
        """同步关注用户列表"""
        from .auth import PixivAuthManager
        from .sync_engine import BookmarkNovelSyncService
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "info", "=== 开始同步关注用户列表 ===")
        
        auth = PixivAuthManager(settings.pixiv)
        api, auth_result = auth.login()
        if auth_result.user_id is None:
            raise RuntimeError("Unable to determine user ID")
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "success", f"登录成功, 用户ID: {auth_result.user_id}")
        
        if self._check_stop():
            return
        
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
                return
            
            stats = service.sync_following_list(progress_callback=on_progress)
            
            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "success", f"关注用户列表同步完成: 更新 {stats.get('users', 0)} 位用户")
        finally:
            db.close()
    
    def _sync_following_novels(self, settings: Settings, job_id: str | None) -> None:
        """同步关注用户小说"""
        from .auth import PixivAuthManager
        from .sync_engine import BookmarkNovelSyncService
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "info", "=== 开始同步关注用户小说 ===")
        
        auth = PixivAuthManager(settings.pixiv)
        api, auth_result = auth.login()
        if auth_result.user_id is None:
            raise RuntimeError("Unable to determine user ID")
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "success", f"登录成功, 用户ID: {auth_result.user_id}")
        
        if self._check_stop():
            return
        
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
                return
            
            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "info", "开始扫描关注用户的新小说 (全部用户)...")

            stats = service.sync_following_novels(
                download_assets=settings.sync.download_assets,
                write_markdown=settings.sync.write_markdown,
                write_raw_text=settings.sync.write_raw_text,
                progress_callback=on_progress,
                users_limit=0,
            )
            
            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "success", f"关注用户小说同步完成: 同步 {stats.get('novels', 0)} 本, 跳过 {stats.get('skipped', 0)} 本, 用户 {stats.get('following_users_scanned', 0)} 人")
        finally:
            db.close()
    
    def _sync_subscribed_series(self, settings: Settings, job_id: str | None) -> None:
        """同步追更系列"""
        from .auth import PixivAuthManager
        from .sync_engine import BookmarkNovelSyncService
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "info", "=== 开始同步追更系列 ===")
        
        auth = PixivAuthManager(settings.pixiv)
        api, auth_result = auth.login()
        if auth_result.user_id is None:
            raise RuntimeError("Unable to determine user ID")
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "success", f"登录成功, 用户ID: {auth_result.user_id}")
        
        if self._check_stop():
            return
        
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
                return
            
            limit = settings.sync.series_sync_limit
            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "info", f"获取订阅系列列表 (限制: {limit or '全部'})...")
            
            stats = service.sync_subscribed_series(
                limit=limit,
                progress_callback=on_progress,
            )
            
            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "success", f"追更系列同步完成: {stats.get('series_synced', 0)} 个系列")
        finally:
            db.close()
    
    def _sync_user_status(self, settings: Settings, job_id: str | None) -> None:
        """同步关注用户的存续状态"""
        job_services.run_user_status_task(
            settings,
            reporter=self._job_reporter(job_id),
            stop_requested=self._stop_requested_for_job(job_id),
        )

    def _sync_novel_status(self, settings: Settings, job_id: str | None) -> None:
        """检查所有小说的存续状态"""
        job_services.run_novel_status_task(
            settings,
            reporter=self._job_reporter(job_id),
            stop_requested=self._stop_requested_for_job(job_id),
        )

    def _sync_series_status(self, settings: Settings, job_id: str | None) -> None:
        """检查所有系列的存续状态"""
        job_services.run_series_status_task(
            settings,
            reporter=self._job_reporter(job_id),
            stop_requested=self._stop_requested_for_job(job_id),
        )

    def _sync_user_backup(self, settings: Settings, job_id: str | None) -> None:
        """定时全量备份关注用户小说（按 users_limit 轮询）"""
        db = Database(settings.storage.db_path)
        db.init_schema()

        try:
            all_user_ids = [r[0] for r in db.conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()]
            total_users = len(all_user_ids)
            if total_users == 0:
                if job_id and self.sync_job_manager:
                    self.sync_job_manager.add_log(job_id, "info", "没有关注用户，跳过")
                return

            watermark = db.get_watermark("user_backup_rotation")
            offset = watermark.get("offset", 0) if watermark else 0
            if offset >= total_users:
                offset = 0

            users_limit = settings.sync.auto_sync_following_novels_users_limit
            if users_limit <= 0:
                users_limit = total_users

            batch = all_user_ids[offset:offset + users_limit]
            next_offset = offset + len(batch)
            if next_offset >= total_users:
                next_offset = 0

            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "info", f"=== 全量备份关注用户小说: 用户 {offset+1}-{offset+len(batch)}/{total_users}, 本轮 {len(batch)} 人 ===")

            total_novels = 0
            total_skipped = 0
            total_assets = 0
            reporter = self._job_reporter(job_id)
            stop_requested = self._stop_requested_for_job(job_id)
            stopped = False

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
                )
                total_novels += int(stats.get("novels", 0) or 0)
                total_skipped += int(stats.get("skipped", 0) or 0)
                total_assets += int(stats.get("assets_downloaded", 0) or 0)
                if stats.get("stopped"):
                    stopped = True
                    break

            db.update_watermark("user_backup_rotation", {
                "offset": next_offset,
                "last_sync_time": datetime.now(timezone.utc).isoformat(),
            })

            if job_id and self.sync_job_manager:
                level = "info" if stopped else "success"
                suffix = "已停止" if stopped else "完成"
                self.sync_job_manager.add_log(job_id, level, f"全量备份{suffix}: 同步 {total_novels} 本, 跳过 {total_skipped} 本, 资源 {total_assets} 个")
        finally:
            db.close()

    def _sync_pending_detection(self, settings: Settings, job_id: str | None) -> None:
        """检测取消收藏/追更"""
        job_services.run_pending_deletion_detection_task(
            settings,
            reporter=self._job_reporter(job_id),
            stop_requested=self._stop_requested_for_job(job_id),
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

    def start_job(self, task_list: list[str] | None = None) -> SyncJobState:
        if not self._semaphore.acquire(blocking=False):
            raise RuntimeError("已有同步任务正在运行，请稍后再试")
        try:
            with self._lock:
                import uuid
                job_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
                job = SyncJobState(
                    job_id=job_id,
                    status="running",
                    message="同步任务已启动",
                    started_at=time.time(),
                    task_list=task_list or [],
                )
                self._jobs[job_id] = job
            thread = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
            thread.start()
            return job
        except Exception:
            self._semaphore.release()
            raise
    
    def start_auto_job(self, task_name: str, task_label: str) -> SyncJobState | None:
        """启动定时任务"""
        if not self._semaphore.acquire(blocking=False):
            return None
        try:
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
            self._semaphore.release()
            raise

    def start_user_backup_job(self, user_id: int) -> SyncJobState:
        """启动单用户全量备份后台任务"""
        if not self._semaphore.acquire(blocking=False):
            raise RuntimeError("已有同步任务正在运行，请稍后再试")
        try:
            with self._lock:
                import uuid
                job_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
                job = SyncJobState(
                    job_id=job_id,
                    status="running",
                    message=f"备份用户 {user_id} 的全部小说",
                    started_at=time.time(),
                    task_list=[f"user_backup:{user_id}"],
                )
                self._jobs[job_id] = job
            thread = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
            thread.start()
            return job
        except Exception:
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

    def _job_reporter(self, job_id: str | None) -> job_services.JobReporter:
        return job_services.JobReporter(manager=self, job_id=job_id)

    def _stop_requested_for_job(self, job_id: str | None) -> job_services.StopRequested:
        def stop_requested() -> bool:
            return bool(job_id and self.is_cancel_requested(job_id))

        return stop_requested

    def _run_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if job is None:
            return
        try:
            settings = load_settings(self.config_path, self.env_path)
            self.add_log(job_id, "info", "加载配置完成")
            self.update_progress(job_id, phase="准备中", message="正在初始化同步...")
            
            # 如果任务列表为空，则根据设置构建（全量同步）
            if not job.task_list:
                task_list = []
                if settings.sync.sync_bookmarks:
                    task_list.append("bookmark")
                if settings.sync.sync_following_users:
                    task_list.append("following_users")
                if settings.sync.sync_following_novels:
                    task_list.append("following_novels")
                if settings.sync.sync_subscribed_series:
                    task_list.append("subscribed_series")
                job.task_list = task_list
            job.current_task_index = 0

            # 根据 task_list 分发到对应的同步函数
            total_stats: dict[str, Any] = {}
            for idx, task_type in enumerate(job.task_list):
                job.current_task_index = idx
                self.update_progress(job_id, phase=_task_label(task_type), message=f"正在执行: {_task_label(task_type)}")
                task_stats = self._run_single_sync(settings, task_type, job_id)
                if task_stats:
                    for key, val in task_stats.items():
                        current = total_stats.get(key)
                        if isinstance(val, bool):
                            total_stats[key] = bool(current) or val
                        elif isinstance(val, (int, float)):
                            total_stats[key] = (current if isinstance(current, (int, float)) and not isinstance(current, bool) else 0) + val
                        else:
                            total_stats[key] = val
            stats = total_stats
            
            job.status = "succeeded"
            job.message = "同步完成"
            job.stats = stats
            self.add_log(job_id, "success", f"同步完成：{stats.get('novels', 0)} 本小说，{stats.get('assets_downloaded', 0)} 个资源")
            
            # 更新任务日志
            if job.log_id:
                db = None
                try:
                    db = Database(settings.storage.db_path)
                    db.init_schema()
                    db.update_task_log(job.log_id, "succeeded", stats=stats, logs=job.logs)
                except Exception as e:
                    logger.error("更新任务日志失败：%s", e)
                finally:
                    if db:
                        db.close()
        except Exception as exc:
            job.status = "failed"
            job.message = "同步失败"
            job.error = str(exc)
            self.add_log(job_id, "error", f"同步失败：{exc}")

            # 更新任务日志（失败）
            if job.log_id:
                db = None
                try:
                    settings = load_settings(self.config_path, self.env_path)
                    db = Database(settings.storage.db_path)
                    db.init_schema()
                    db.update_task_log(job.log_id, "failed", error_message=str(exc), logs=job.logs)
                except Exception as e:
                    logger.error("更新任务日志失败：%s", e)
                finally:
                    if db:
                        db.close()
        finally:
            job.finished_at = time.time()
            self._semaphore.release()
            with self._lock:
                self._cleanup_old_jobs()

    def _run_single_sync(self, settings: Settings, task_type: str, job_id: str) -> dict[str, Any]:
        """根据 task_type 执行单个同步任务"""
        reporter = self._job_reporter(job_id)
        stop_requested = self._stop_requested_for_job(job_id)
        if task_type == "user_status":
            return job_services.run_user_status_task(settings, reporter=reporter, stop_requested=stop_requested)
        if task_type == "novel_status":
            return job_services.run_novel_status_task(settings, reporter=reporter, stop_requested=stop_requested)
        if task_type == "series_status":
            return job_services.run_series_status_task(settings, reporter=reporter, stop_requested=stop_requested)
        if task_type == "pending_deletion_detection":
            return job_services.run_pending_deletion_detection_task(settings, reporter=reporter, stop_requested=stop_requested)
        if task_type.startswith("user_backup:"):
            try:
                target_uid = int(task_type.split(":", 1)[1])
            except (ValueError, IndexError):
                self.add_log(job_id, "error", f"非法的用户备份任务: {task_type}")
                return {}
            return job_services.run_user_backup_task(settings, target_uid, reporter=reporter, stop_requested=stop_requested)

        from .auth import PixivAuthManager
        from .sync_engine import BookmarkNovelSyncService

        auth = PixivAuthManager(settings.pixiv)
        self.add_log(job_id, "info", "正在登录 Pixiv...")
        api, auth_result = auth.login()
        if auth_result.user_id is None:
            raise RuntimeError("Unable to determine PIXIV_USER_ID")
        if settings.pixiv.user_id is None:
            settings.pixiv.user_id = auth_result.user_id
        self.add_log(job_id, "success", f"登录成功, 用户ID: {auth_result.user_id}")

        db = Database(settings.storage.db_path)
        db.init_schema()
        storage = FileStorage(settings)
        storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])

        try:
            sync_check_scope = "_"
            job = self.get_job(job_id)
            if job and not job.is_auto_sync:
                matched_scope = self.latest_matching_sync_check_scope(settings, auth_result.user_id, task_type)
                if matched_scope:
                    sync_check_scope, source_job_id = matched_scope
                    job.progress["sync_check_scope"] = sync_check_scope
                    job.progress["sync_check_source_job_id"] = source_job_id
                    self.add_log(job_id, "info", f"使用预检查结果跳过已存在内容: {source_job_id}")
            service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings, sync_check_scope=sync_check_scope)

            def on_progress(event_type: str, data: dict[str, Any]) -> None:
                if event_type == "novel_start":
                    self.add_log(job_id, "info", f"[{data.get('current', '?')}/{data.get('total', '?')}] {data.get('title', '')[:30]}")
                    self.update_progress(job_id, phase=data.get("phase") or _task_label(task_type), current=data.get('current', 0), total=data.get('total', 50))
                elif event_type == "novel_done":
                    if data.get('failed'):
                        self.add_log(job_id, "error", "  失败（详见服务日志）")
                    elif data.get('skipped'):
                        self.add_log(job_id, "info", "  跳过（已存在）")
                    else:
                        self.add_log(job_id, "info", f"  完成: 收藏{data.get('bookmarks', 0)} 浏览{data.get('views', 0)}")
                elif event_type == "page":
                    self.add_log(job_id, "info", f"正在获取第 {data.get('page', '?')} 页...")
                elif event_type == "rate_limit":
                    self.add_log(job_id, "warning", f"等待 {data.get('seconds', 1)} 秒")
                elif event_type == "phase":
                    self.add_log(job_id, "info", data.get("phase", ""))
                elif event_type == "phase_start":
                    self.add_log(job_id, "info", f"开始: {data.get('name', '')}")

            if task_type == "bookmark":
                self.add_log(job_id, "info", "=== 开始同步收藏小说 ===")
                total_stats: dict[str, Any] = {}
                for restrict in settings.sync.bookmark_restricts:
                    stats = service.sync(
                        user_id=auth_result.user_id,
                        restricts=[restrict],
                        download_assets=settings.sync.download_assets,
                        write_markdown=settings.sync.write_markdown,
                        write_raw_text=settings.sync.write_raw_text,
                        progress_callback=on_progress,
                    )
                    for key, val in stats.items():
                        total_stats[key] = total_stats.get(key, 0) + val
                self.add_log(job_id, "success", f"收藏同步完成: 新增 {total_stats.get('novels', 0)} 本, 跳过 {total_stats.get('skipped', 0)} 本")
                return total_stats

            elif task_type == "following_users":
                self.add_log(job_id, "info", "=== 开始同步关注用户列表 ===")
                next_query: dict[str, Any] | None = {"user_id": auth_result.user_id, "restrict": "public"}
                page_count = 0
                users_count = 0
                while next_query:
                    result = api.user_following(**next_query)
                    page_count += 1
                    self.add_log(job_id, "info", f"获取关注列表第 {page_count} 页...")
                    for preview in (getattr(result, "user_previews", []) or []):
                        user = getattr(preview, "user", preview)
                        uid = int(getattr(user, "id", 0))
                        if uid:
                            from .models import UserRecord
                            from .utils_hashing import stable_json_dumps
                            from .sync_engine import _to_plain
                            db.upsert_user(UserRecord(
                                user_id=uid,
                                name=getattr(user, "name", str(uid)),
                                account=getattr(user, "account", None),
                                raw_json=stable_json_dumps(_to_plain(user)),
                            ))
                            users_count += 1
                    next_query = api.parse_qs(getattr(result, "next_url", None))
                    if next_query:
                        time.sleep(settings.sync.delay_seconds_between_pages)
                self.add_log(job_id, "success", f"关注用户列表同步完成: 更新 {users_count} 位用户")
                return {"users": users_count}

            elif task_type == "following_novels":
                self.add_log(job_id, "info", "=== 开始同步关注用户小说 ===")
                self.add_log(job_id, "info", "开始扫描关注用户的新小说 (全部用户)...")
                stats = service.sync_following_novels(
                    download_assets=settings.sync.download_assets,
                    write_markdown=settings.sync.write_markdown,
                    write_raw_text=settings.sync.write_raw_text,
                    progress_callback=on_progress,
                    users_limit=0,
                )
                self.add_log(job_id, "success", f"关注用户小说同步完成: 同步 {stats.get('novels', 0)} 本, 跳过 {stats.get('skipped', 0)} 本")
                return stats

            elif task_type == "subscribed_series":
                self.add_log(job_id, "info", "=== 开始同步追更系列 ===")
                # 优先使用前端传入的 limit，否则使用设置中的默认值
                job = self.get_job(job_id)
                limit = (job.progress.get("series_limit") if job else None) or settings.sync.series_sync_limit
                stats = service.sync_subscribed_series(
                    limit=limit,
                    progress_callback=on_progress,
                )
                self.add_log(job_id, "success", f"追更系列同步完成: {stats.get('series_synced', 0)} 个系列")
                return stats

            elif task_type == "user_status":
                self.add_log(job_id, "info", "=== 开始检查用户状态 ===")
                user_list: list[dict[str, Any]] = []
                page_num = 1
                while True:
                    page_data = db.list_users(page=page_num, page_size=500)
                    items = page_data.get("items", [])
                    if not items:
                        break
                    user_list.extend(items)
                    if page_num >= page_data.get("total_pages", 1):
                        break
                    page_num += 1
                self.add_log(job_id, "info", f"共 {len(user_list)} 个用户需要检查")
                checked_count = 0
                for user in user_list:
                    uid = user.get("user_id")
                    if not uid:
                        continue
                    try:
                        status = _check_pixiv_user_status(api, uid)
                        db.upsert_user_status(uid, status)
                        checked_count += 1
                        self.add_log(job_id, "info", f"[{checked_count}/{len(user_list)}] {user.get('name', uid)}: {status}")
                        time.sleep(settings.sync.delay_seconds_between_skips)
                    except Exception as e:
                        self.add_log(job_id, "warning", f"检查用户 {uid} 失败: {e}")
                self.add_log(job_id, "success", f"用户状态检查完成: {checked_count} 个用户")
                return {"users_checked": checked_count}

            elif task_type == "novel_status":
                self.add_log(job_id, "info", "=== 开始检查小说状态 ===")
                novel_ids = db.get_all_novel_ids()
                self.add_log(job_id, "info", f"共 {len(novel_ids)} 本小说需要检查")
                checked = 0
                status_counts: dict[str, int] = {}
                for nid in novel_ids:
                    try:
                        status = _check_novel_status(api, nid)
                        db.upsert_novel_status(nid, status)
                        checked += 1
                        status_counts[status] = status_counts.get(status, 0) + 1
                        if status != "normal":
                            self.add_log(job_id, "warning", f"[{checked}/{len(novel_ids)}] 小说 {nid}: {status}")
                        elif checked % 50 == 0:
                            self.add_log(job_id, "info", f"[{checked}/{len(novel_ids)}] 已检查...")
                        time.sleep(settings.sync.delay_seconds_between_skips)
                    except Exception as e:
                        self.add_log(job_id, "warning", f"检查小说 {nid} 失败: {e}")
                summary = ", ".join(f"{k}: {v}" for k, v in status_counts.items())
                self.add_log(job_id, "success", f"小说状态检查完成: {checked} 本 ({summary})")
                return {"novels_checked": checked}

            elif task_type == "series_status":
                self.add_log(job_id, "info", "=== 开始检查系列状态 ===")
                series_ids = db.get_all_series_ids()
                self.add_log(job_id, "info", f"共 {len(series_ids)} 个系列需要检查")
                checked = 0
                status_counts: dict[str, int] = {}
                for sid in series_ids:
                    try:
                        status = _check_series_status(api, sid)
                        db.upsert_series_status(sid, status)
                        checked += 1
                        status_counts[status] = status_counts.get(status, 0) + 1
                        if status != "normal":
                            self.add_log(job_id, "warning", f"[{checked}/{len(series_ids)}] 系列 {sid}: {status}")
                        elif checked % 20 == 0:
                            self.add_log(job_id, "info", f"[{checked}/{len(series_ids)}] 已检查...")
                        time.sleep(settings.sync.delay_seconds_between_skips)
                    except Exception as e:
                        self.add_log(job_id, "warning", f"检查系列 {sid} 失败: {e}")
                summary = ", ".join(f"{k}: {v}" for k, v in status_counts.items())
                self.add_log(job_id, "success", f"系列状态检查完成: {checked} 个 ({summary})")
                return {"series_checked": checked}

            elif task_type == "pending_deletion_detection":
                self.add_log(job_id, "info", "=== 开始检测取消收藏/追更 ===")
                from .sync_engine import BookmarkNovelSyncService
                service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)
                result = service.run_detection(
                    user_id=auth_result.user_id,
                    restricts=settings.sync.bookmark_restricts,
                    progress_callback=on_progress,
                )
                self.add_log(job_id, "success", f"检测完成: 新增 {result.get('new_pending', 0)} 条待确认记录")
                return result

            elif task_type.startswith("user_backup:"):
                try:
                    target_uid = int(task_type.split(":", 1)[1])
                except (ValueError, IndexError):
                    self.add_log(job_id, "error", f"非法的用户备份任务: {task_type}")
                    return {}
                self.add_log(job_id, "info", f"=== 开始备份用户 {target_uid} 的全部小说 ===")
                stats: dict[str, int] = {"novels": 0, "skipped": 0, "assets_downloaded": 0}
                next_query: dict[str, Any] | None = {"user_id": target_uid}
                page_count = 0
                processed = 0
                while next_query:
                    try:
                        result = api.user_novels(**next_query)
                    except Exception as exc:
                        self.add_log(job_id, "error", f"获取用户小说失败: {exc}")
                        break
                    page_count += 1
                    novels = getattr(result, "novels", []) or []
                    for novel in novels:
                        title = getattr(novel, "title", "")
                        processed += 1
                        self.update_progress(job_id, phase="user_backup", current=processed, total=processed, current_novel=title[:40])
                        counters = service._sync_novel(
                            novel, "public",
                            settings.sync.download_assets,
                            settings.sync.write_markdown,
                            settings.sync.write_raw_text,
                            source_type="user_backup",
                            source_key=str(target_uid),
                        )
                        for key in ("novels", "skipped", "assets_downloaded"):
                            stats[key] = stats.get(key, 0) + counters.get(key, 0)
                        if counters.get("skipped"):
                            if settings.sync.delay_seconds_between_skips > 0:
                                time.sleep(settings.sync.delay_seconds_between_skips)
                        elif settings.sync.delay_seconds_between_items > 0:
                            time.sleep(settings.sync.delay_seconds_between_items)
                    next_query = api.parse_qs(getattr(result, "next_url", None))
                    if next_query and settings.sync.delay_seconds_between_pages > 0:
                        time.sleep(settings.sync.delay_seconds_between_pages)
                self.add_log(job_id, "success", f"用户备份完成: 同步 {stats.get('novels', 0)} 本，跳过 {stats.get('skipped', 0)} 本")
                return stats

            else:
                self.add_log(job_id, "error", f"未知任务类型: {task_type}")
                return {}
        finally:
            db.close()


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
        sync_data["bookmark_restricts"] = [str(item) for item in bookmark_restricts]

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
            sync_data["series_sync_limit"] = int(series_limit_raw)
        
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
        from .settings import cron_to_next_run as _cron_check

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

        _atomic_write_yaml(config_path, config_data)

        self.invalidate()
        return _settings_to_dict(load_settings(config_path, None))


def _load_or_create_flask_secret(env_path: str | None) -> str:
    env_secret = os.getenv("PIXIV_FLASK_SECRET")
    if env_secret:
        return env_secret

    path = Path(env_path or os.getenv("ENV_PATH", ".env"))
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for line in lines:
        if line.startswith("PIXIV_FLASK_SECRET="):
            secret = line.split("=", 1)[1].strip()
            if secret:
                os.environ["PIXIV_FLASK_SECRET"] = secret
                return secret

    secret = os.urandom(32).hex()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines.append(f"PIXIV_FLASK_SECRET={secret}")
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)
    os.environ["PIXIV_FLASK_SECRET"] = secret
    return secret



def create_app(config_path: str | None = None, env_path: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    # 修改 Jinja2 变量分隔符，避免与 Vue 3 的 {{ }} 冲突
    app.jinja_env.variable_start_string = "{["
    app.jinja_env.variable_end_string = "]}"
    app.secret_key = _load_or_create_flask_secret(env_path)
    # 加固 cookie：HttpOnly + SameSite=Lax；如启用 HTTPS 可设置 SESSION_COOKIE_SECURE=true
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if os.getenv("PIXIV_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes", "on"}:
        app.config["SESSION_COOKIE_SECURE"] = True
    settings_manager = SettingsManager(config_path)
    sync_job_manager = SyncJobManager(config_path=config_path, env_path=env_path)
    shared_job_manager = JobManager()

    def run_web_task(task_type: str, context: dict[str, Any]) -> dict[str, Any] | None:
        current_settings = settings_manager.load(env_path=env_path)
        return execute_task(task_type, current_settings, context)

    shared_job_runner = JobRunner(shared_job_manager, run_web_task)

    def _has_active_shared_jobs() -> bool:
        with shared_job_manager._lock:
            active_statuses = {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}
            return any(job.status in active_statuses for job in shared_job_manager._jobs.values())

    def _has_any_running_web_job() -> bool:
        return sync_job_manager.has_running_jobs() or _has_active_shared_jobs()

    def _running_job_error_response():
        return jsonify({"error": "已有同步任务正在运行，请稍后再试"}), 400

    def _shared_job_logs_for_db(job: JobState) -> list[dict[str, Any]]:
        return [{"time": entry.time, "level": entry.level, "message": entry.message} for entry in job.logs]

    def _run_shared_web_job(job_id: str) -> None:
        shared_job_runner.run(job_id)
        job = shared_job_manager.get_job(job_id)
        if job is None:
            return
        log_id = job.progress.get("log_id")
        if not log_id:
            return
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        try:
            db.init_schema()
            logs = _shared_job_logs_for_db(job)
            if job.status == JobStatus.SUCCEEDED:
                db.update_task_log(log_id, JobStatus.SUCCEEDED.value, stats=job.stats, logs=logs)
            elif job.status == JobStatus.FAILED:
                db.update_task_log(log_id, JobStatus.FAILED.value, error_message=job.error, logs=logs)
            elif job.status == JobStatus.CANCELLED:
                db.update_task_log(log_id, JobStatus.CANCELLED.value, logs=logs)
        except Exception as exc:
            logger.error("更新共享任务日志失败：%s", exc)
        finally:
            db.close()

    def _submit_shared_web_job(
        spec: JobSpec,
        current_settings: Settings,
        task_type: str,
        task_name: str,
        *,
        is_auto_sync: bool = False,
        progress: dict[str, Any] | None = None,
    ) -> JobState:
        if _has_any_running_web_job():
            raise RuntimeError("已有同步任务正在运行，请稍后再试")

        db = Database(current_settings.storage.db_path)
        try:
            db.init_schema()
            job = shared_job_manager.submit(spec)
            log_id = db.create_task_log(
                task_type=task_type,
                task_name=task_name,
                job_id=job.job_id,
                is_auto_sync=is_auto_sync,
            )
            job.progress["log_id"] = log_id
            if progress:
                job.progress.update(progress)
            thread = threading.Thread(target=_run_shared_web_job, args=(job.job_id,), daemon=True)
            thread.start()
            return job
        finally:
            db.close()

    oauth_manager = OAuthManager(env_path=env_path)
    auto_sync_scheduler = AutoSyncScheduler(config_path=config_path, env_path=env_path, sync_job_manager=sync_job_manager)
    
    # 启动定时同步调度器
    # 在 Werkzeug debug reloader 下，主进程会先启动一次再 fork 子进程；只在子进程启动调度器，避免双开
    _is_werkzeug_reload = os.getenv("WERKZEUG_RUN_MAIN") == "true"
    _is_debug = bool(os.getenv("FLASK_DEBUG")) or bool(os.getenv("WERKZEUG_SERVER_FD"))
    if not _is_debug or _is_werkzeug_reload:
        auto_sync_scheduler.start()

    def _auto_login_worker(task, username, password, proxy, timeout):
        """后台线程：用 Playwright 无头浏览器自动完成 Pixiv OAuth 登录"""
        from .playwright_login import PlaywrightLoginHelper
        try:
            helper = PlaywrightLoginHelper(proxy=proxy, timeout=timeout)
            result = helper.login(task.login_url, username, password)
            if result.success and result.callback_url:
                task.message = "登录成功，正在兑换 token..."
                oauth_manager.exchange_callback_url(task, result.callback_url)
                if task.refresh_token:
                    oauth_manager.save_to_env(task.refresh_token, task.user_id)
                    task.status = "done"
                    task.message = "自动登录成功，token 已保存"
                else:
                    task.status = "failed"
                    task.message = "token 兑换失败：未获取到 refresh_token"
            else:
                task.status = "failed"
                task.message = result.error or "自动登录失败"
        except Exception as exc:
            task.status = "failed"
            task.message = f"自动登录异常: {exc}"
            logger.exception("Playwright 自动登录失败")

    # --- 认证中间件 ---
    # /proxy/image 需要登录（防止开放代理）。OAuth 回调与健康检查路径必须豁免（无 cookie 场景）。
    _AUTH_EXEMPT_PATHS = {
        "/api/auth/login",
        "/api/csrf-token",
        "/nginx-health",
        "/api/health",
        "/oauth/callback",
    }

    _CSRF_EXEMPT_PATHS = {
        "/api/auth/login",
        "/nginx-health",
        "/api/health",
        "/oauth/callback",
    }
    _MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
    _login_failures: dict[str, list[float]] = {}

    def _get_csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return str(token)

    def _csrf_failed():
        return jsonify({"error": "csrf token invalid"}), 403

    # 是否信任反向代理注入的 X-Forwarded-For。仅当确实部署在可信反代（nginx 等）
    # 之后才应开启；否则客户端可伪造该头绕过本机判定。
    _trust_proxy = (os.getenv("DASHBOARD_TRUST_PROXY") or "").strip().lower() in {"1", "true", "yes", "on"}
    _LOCAL_ADDRS = {"127.0.0.1", "::1", "localhost"}

    def _client_addr() -> str:
        """解析真实客户端地址。反代后 remote_addr 恒为 127.0.0.1，必须看 XFF。"""
        if _trust_proxy:
            xff = request.headers.get("X-Forwarded-For", "")
            if xff:
                # XFF 最左为最初客户端
                return xff.split(",")[0].strip()
        return request.remote_addr or ""

    def _behind_proxy() -> bool:
        return bool(request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP"))

    def _is_local_request() -> bool:
        return _client_addr() in _LOCAL_ADDRS

    @app.before_request
    def _check_auth():
        token = settings_manager.load(env_path=env_path).dashboard_token
        if not token:
            # 安全加固：未配置 token 时仅允许真正的本机访问。
            # 若检测到代理头但未显式信任代理，说明很可能暴露在反代后，
            # 此时 remote_addr=127.0.0.1 不可信，一律拒绝，避免私密收藏泄漏。
            if _behind_proxy() and not _trust_proxy:
                return jsonify({"error": "dashboard token required when behind a proxy"}), 403
            if _is_local_request():
                return
            return jsonify({"error": "dashboard token required for non-local access"}), 403
        path = request.path
        if path in _AUTH_EXEMPT_PATHS:
            return
        if path.startswith("/static/"):
            return
        if session.get("authenticated"):
            if request.method in _MUTATING_METHODS and path not in _CSRF_EXEMPT_PATHS:
                submitted = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
                if not submitted or not secrets.compare_digest(str(submitted), _get_csrf_token()):
                    return _csrf_failed()
            return
        # API 请求返回 401，页面请求重定向到登录
        if path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        return redirect("/api/auth/login")

    @app.after_request
    def _add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response

    # 启动期安全提示：未配置 dashboard_token 时给出明确告警。
    if not (settings_manager.load(env_path=env_path).dashboard_token):
        logger.warning(
            "DASHBOARD_TOKEN 未配置：仅允许本机访问。若部署在反向代理后，"
            "请设置 DASHBOARD_TOKEN，并仅在可信反代下设置 DASHBOARD_TRUST_PROXY=1。"
        )

    def current_settings_for_routes() -> Settings:
        return settings_manager.load(env_path=env_path)

    from .ai_web import register_ai_routes
    register_ai_routes(app, current_settings_for_routes)

    from .preference_web import register_preference_routes
    register_preference_routes(app, current_settings_for_routes)

    @app.route("/api/auth/login", methods=["GET", "POST"])
    def auth_login():
        token = settings_manager.load(env_path=env_path).dashboard_token
        if not token:
            return redirect("/")
        if request.method == "GET":
            return Response(
                '<!DOCTYPE html><html><head><title>Login</title>'
                '<style>body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f5f5f5}'
                'form{background:white;padding:2rem;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.1)}'
                'input{display:block;margin:1rem 0;padding:0.5rem;width:250px}'
                'button{padding:0.5rem 1.5rem;background:#4a90d9;color:white;border:none;border-radius:4px;cursor:pointer}</style></head>'
                '<body><form method="POST"><h2>Pixiv Novel Sync</h2>'
                '<input name="token" type="password" placeholder="访问密码" autofocus>'
                '<button type="submit">登录</button></form></body></html>',
                content_type="text/html",
            )
        import hmac as _hmac
        now = time.time()
        client = _client_addr()
        failures = [ts for ts in _login_failures.get(client, []) if now - ts < 300]
        if len(failures) >= 5:
            _login_failures[client] = failures
            return jsonify({"error": "too many login attempts"}), 429
        input_token = request.form.get("token", "")
        if _hmac.compare_digest(input_token, token):
            _login_failures.pop(client, None)
            session["authenticated"] = True
            _get_csrf_token()
            return redirect("/")
        failures.append(now)
        _login_failures[client] = failures
        return Response("密码错误", status=401)

    @app.get("/api/csrf-token")
    def csrf_token():
        return jsonify({"csrf_token": _get_csrf_token()})

    @app.route("/api/auth/logout", methods=["POST"])
    def auth_logout():
        session.pop("authenticated", None)
        return jsonify({"ok": True})

    @app.get("/proxy/image")
    def proxy_image():
        url = request.args.get("url", "").strip()
        if not url:
            return Response("Missing url parameter", status=400)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return Response("Invalid scheme", status=400)
        if parsed.port not in (None, 80, 443):
            return Response("Invalid port", status=400)
        hostname = parsed.hostname or ""
        if not (hostname == "pximg.net" or hostname.endswith(".pximg.net")):
            return Response("Only pixiv images are allowed", status=403)
        try:
            headers = {
                "Referer": "https://www.pixiv.net/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            current_settings = settings_manager.load(env_path=env_path)
            proxies = {"http": current_settings.pixiv.proxy, "https": current_settings.pixiv.proxy} if current_settings.pixiv.proxy else None
            resp = http_requests.get(url, headers=headers, timeout=15, verify=current_settings.pixiv.verify_ssl, proxies=proxies, allow_redirects=False)
            resp.raise_for_status()
            return Response(resp.content, content_type=resp.headers.get("Content-Type", "image/jpeg"))
        except Exception as exc:
            return Response(f"Failed to fetch image: {exc}", status=502)

    @app.get("/")
    def index():
        return redirect("/dashboard")

    @app.get("/token-login")
    def token_login():
        return render_template("token_login.html")

    @app.get("/dashboard")
    def dashboard_home():
        return render_template("dashboard.html")

    @app.get("/dashboard/follows")
    def dashboard_follows_page():
        return render_template("dashboard_follows.html")

    @app.get("/dashboard/novels")
    def dashboard_novels_page():
        return render_template("dashboard_novels.html")

    @app.get("/dashboard/novels/<int:novel_id>")
    def dashboard_novel_detail_page(novel_id: int):
        return render_template("dashboard_novel_detail.html", novel_id=novel_id)

    @app.get("/dashboard/settings")
    def dashboard_settings_page():
        return render_template("dashboard_settings.html")

    @app.get("/dashboard/logs")
    def dashboard_logs_page():
        return render_template("dashboard_logs.html")

    @app.get("/dashboard/pending-deletions")
    def dashboard_pending_deletions_page():
        return render_template("dashboard_pending_deletions.html")

    @app.get("/api/token-config")
    def get_token_config():
        """检查是否配置了 Pixiv 账号密码（不泄露实际值）"""
        current_settings = settings_manager.load(env_path=env_path)
        has_credentials = bool(
            current_settings.pixiv.username
            and current_settings.pixiv.password
        )
        return jsonify({"has_credentials": has_credentials})

    @app.post("/api/token-jobs")
    def create_token_job():
        current_settings = settings_manager.load(env_path=env_path)
        has_credentials = bool(
            current_settings.pixiv.username
            and current_settings.pixiv.password
        )
        task = oauth_manager.create_task(_external_base_url(request))
        if has_credentials:
            # 自动登录模式：启动后台线程用 Playwright 完成登录
            task.status = "running"
            task.message = "正在自动登录 Pixiv..."
            worker = threading.Thread(
                target=_auto_login_worker,
                args=(task, current_settings.pixiv.username, current_settings.pixiv.password, current_settings.pixiv.proxy, current_settings.pixiv.timeout),
                daemon=True,
            )
            worker.start()
            return jsonify({
                "task_id": task.task_id,
                "status": task.status,
                "message": task.message,
                "login_url": task.login_url,
                "callback_url": task.callback_url,
                "mode": "auto",
            })
        else:
            return jsonify({
                "task_id": task.task_id,
                "status": task.status,
                "message": task.message,
                "login_url": task.login_url,
                "callback_url": task.callback_url,
                "mode": "manual",
            })

    @app.get("/api/token-jobs/<job_id>")
    def get_token_job(job_id: str):
        task = oauth_manager.get_task(job_id)
        if task is None:
            return jsonify({"error": "task not found"}), 404
        return jsonify(_oauth_task_public_payload(task, mode="oauth"))

    @app.post("/api/save-token")
    def save_token():
        manager = oauth_manager
        payload = request.get_json(silent=True) or {}
        refresh_token = str(payload.get("refresh_token") or "").strip()
        user_id_raw = payload.get("user_id")
        user_id = int(user_id_raw) if user_id_raw not in (None, "") else None
        if not refresh_token:
            return jsonify({"error": "missing refresh_token"}), 400
        manager.save_to_env(refresh_token, user_id)
        return jsonify({"ok": True, "message": "已写入 .env"})

    @app.post("/oauth/start")
    def oauth_start():
        external_base_url = _external_base_url(request)
        task = oauth_manager.create_task(external_base_url)
        return jsonify(
            {
                "task_id": task.task_id,
                "status": task.status,
                "message": task.message,
                "login_url": task.login_url,
                "callback_url": task.callback_url,
                "mode": "oauth",
            }
        )

    @app.get("/oauth/task/<task_id>")
    def oauth_task(task_id: str):
        task = oauth_manager.get_task(task_id)
        if task is None:
            return jsonify({"error": "task not found"}), 404
        return jsonify(_oauth_task_public_payload(task, mode="oauth"))

    @app.get("/oauth/callback")
    def oauth_callback():
        error = request.args.get("error")
        if error:
            return redirect(f"/token-login?error={error}")

        code = request.args.get("code")
        state = request.args.get("state")
        if not code or not state:
            return redirect("/token-login?error=回调参数缺失")

        task = oauth_manager.find_task_by_state(state)
        if task is None:
            return redirect("/token-login?error=登录任务不存在或已过期")

        try:
            oauth_manager.exchange_code(task, code)
        except Exception as exc:
            task.status = "failed"
            task.message = f"token 交换失败：{exc}"
            return redirect(f"/token-login?error=token交换失败")

        return redirect(f"/token-login?oauth_task={task.task_id}")

    @app.post("/oauth/sync-callback/<task_id>")
    def oauth_sync_callback(task_id: str):
        task = oauth_manager.get_task(task_id)
        if task is None:
            return jsonify({"error": "task not found"}), 404
        payload = request.get_json(silent=True) or {}
        callback_url = str(payload.get("callback_url") or "").strip()
        if not callback_url:
            return jsonify({"error": "missing callback_url"}), 400
        try:
            oauth_manager.sync_state_from_callback_url(task, callback_url)
        except Exception as exc:
            task.status = "failed"
            task.message = f"callback 同步失败：{exc}"
            return jsonify({"error": task.message}), 400
        task.status = "pending"
        return jsonify({"ok": True, "message": task.message, "state": task.state})

    @app.post("/oauth/exchange/<task_id>")
    def oauth_exchange(task_id: str):
        task = oauth_manager.get_task(task_id)
        if task is None:
            return jsonify({"error": "task not found"}), 404
        payload = request.get_json(silent=True) or {}
        callback_url = str(payload.get("callback_url") or "").strip()
        if not callback_url:
            return jsonify({"error": "missing callback_url"}), 400
        try:
            oauth_manager.exchange_callback_url(task, callback_url)
        except Exception as exc:
            task.status = "failed"
            task.message = f"token 交换失败：{exc}"
            return jsonify({"error": task.message}), 400
        return jsonify({"ok": True, **_oauth_task_public_payload(task, mode="oauth")})

    @app.post("/oauth/save/<task_id>")
    def oauth_save(task_id: str):
        task = oauth_manager.get_task(task_id)
        if task is None:
            return jsonify({"error": "task not found"}), 404
        if not task.refresh_token:
            return jsonify({"error": "task has no refresh_token"}), 400
        oauth_manager.save_to_env(task.refresh_token, task.user_id)
        return jsonify({"ok": True, "message": "已写入 .env"})

    @app.get("/api/dashboard/status")
    def dashboard_status():
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            stats = json.loads(db.export_stats())
            current_user = db.get_user_summary(current_settings.pixiv.user_id)
        finally:
            db.close()
        latest_job = sync_job_manager.latest_job()
        return jsonify(
            {
                "user_id": current_settings.pixiv.user_id,
                "current_user": current_user,
                "sync_enabled": current_settings.sync.enabled,
                "initial_manual_only": current_settings.sync.initial_manual_only,
                "bookmark_restricts": current_settings.sync.bookmark_restricts,
                "bookmark_restricts_label": _restricts_to_label(current_settings.sync.bookmark_restricts),
                "max_items_per_run": current_settings.sync.max_items_per_run,
                "max_pages_per_run": current_settings.sync.max_pages_per_run,
                "delay_seconds_between_items": current_settings.sync.delay_seconds_between_items,
                "delay_seconds_between_pages": current_settings.sync.delay_seconds_between_pages,
                "series_sync_limit": current_settings.sync.series_sync_limit,
                "stats": stats,
                "latest_job": _job_to_dict(latest_job),
            }
        )

    @app.get("/api/dashboard/follows")
    def dashboard_follows():
        current_settings = settings_manager.load(env_path=env_path)
        page = max(_safe_int(request.args.get("page", 1), 1), 1)
        page_size = 10
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            payload = db.list_followed_users(page=page, page_size=page_size)
        finally:
            db.close()
        return jsonify(payload)

    @app.get("/api/dashboard/novels")
    def dashboard_novels():
        current_settings = settings_manager.load(env_path=env_path)
        page = max(_safe_int(request.args.get("page", 1), 1), 1)
        page_size = min(max(_safe_int(request.args.get("page_size", 10), 10), 1), 100)
        category = str(request.args.get("category", "all") or "all").strip().lower()
        if category not in {"all", "bookmark", "following"}:
            category = "all"
        search = str(request.args.get("search", "") or "").strip()
        sort = str(request.args.get("sort", "") or "").strip()
        if sort not in {"", "updated_desc", "bookmarks_desc", "views_desc"}:
            sort = ""
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            if category == "bookmark":
                payload = db.list_bookmark_novels(page=page, page_size=page_size, search=search, sort=sort)
            elif category == "following":
                payload = db.list_following_series(page=page, page_size=page_size, search=search, sort=sort)
            else:
                payload = db.list_recent_novels(page=page, page_size=page_size, category="all", search=search, sort=sort)
        finally:
            db.close()
        return jsonify(payload)

    @app.get("/api/dashboard/novels/<int:novel_id>")
    def dashboard_novel_detail(novel_id: int):
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            payload = db.get_novel_detail(novel_id)
        finally:
            db.close()
        if payload is None:
            return jsonify({"error": "novel not found"}), 404
        return jsonify(payload)

    @app.get("/api/dashboard/novels/<int:novel_id>/progress")
    def get_novel_progress(novel_id: int):
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            progress = db.get_reading_progress(novel_id)
        finally:
            db.close()
        if progress is None:
            return jsonify({"novel_id": novel_id, "progress": 0, "status": "unread"})
        return jsonify(progress)

    @app.post("/api/dashboard/novels/<int:novel_id>/progress")
    def update_novel_progress(novel_id: int):
        data = request.get_json() or {}
        progress = max(0, min(100, _safe_int(data.get("progress", 0), 0)))
        status = str(data.get("status", "reading") or "reading").strip()
        if status not in {"unread", "reading", "completed"}:
            return jsonify({"error": "invalid status"}), 400
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            db.upsert_reading_progress(novel_id, progress, status)
        finally:
            db.close()
        return jsonify({"success": True})

    @app.delete("/api/dashboard/novels/<int:novel_id>/progress")
    def delete_novel_progress(novel_id: int):
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            db.delete_reading_progress(novel_id)
        finally:
            db.close()
        return jsonify({"success": True})

    @app.post("/api/dashboard/novels/export-epub")
    def export_novels_to_epub():
        from .epub_exporter import create_epub_from_novel
        import zipfile
        import io

        data = request.get_json() or {}
        novel_ids = data.get("novel_ids", [])
        if not novel_ids or not isinstance(novel_ids, list):
            return jsonify({"error": "novel_ids required"}), 400

        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        storage = FileStorage(current_settings.storage)

        try:
            if len(novel_ids) == 1:
                # 单本小说直接返回EPUB
                novel_id = int(novel_ids[0])
                novel_data = db.get_novel_detail(novel_id)
                if not novel_data:
                    return jsonify({"error": "novel not found"}), 404
                text_content = novel_data.get("text_raw", "")
                if not text_content:
                    return jsonify({"error": "novel text not available"}), 400

                # 查找封面路径
                cover_path = None
                if novel_data.get("cover_url"):
                    cover_path = storage.get_novel_cover_path(
                        novel_data["user_id"],
                        novel_data["novel_id"],
                        novel_data["restrict_value"]
                    )

                epub_bytes = create_epub_from_novel(novel_data, text_content, cover_path)
                filename = f"{safe_name(str(novel_data.get('title') or novel_id), str(novel_id))}.epub"

                return send_file(
                    io.BytesIO(epub_bytes),
                    mimetype="application/epub+zip",
                    as_attachment=True,
                    download_name=filename
                )
            else:
                # 多本小说打包为ZIP
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    for novel_id in novel_ids[:50]:  # 限制最多50本
                        novel_id = int(novel_id)
                        novel_data = db.get_novel_detail(novel_id)
                        if not novel_data or not novel_data.get("text_raw"):
                            continue

                        cover_path = None
                        if novel_data.get("cover_url"):
                            cover_path = storage.get_novel_cover_path(
                                novel_data["user_id"],
                                novel_data["novel_id"],
                                novel_data["restrict_value"]
                            )

                        epub_bytes = create_epub_from_novel(novel_data, novel_data["text_raw"], cover_path)
                        filename = f"{safe_name(str(novel_data.get('title') or novel_id), str(novel_id))}.epub"
                        zf.writestr(filename, epub_bytes)

                zip_buffer.seek(0)
                return send_file(
                    zip_buffer,
                    mimetype="application/zip",
                    as_attachment=True,
                    download_name="novels.zip"
                )
        finally:
            db.close()

    @app.get("/api/dashboard/series/<int:series_id>")
    def dashboard_series_detail(series_id: int):
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            payload = db.get_series_detail(series_id)
        finally:
            db.close()
        if payload is None:
            return jsonify({"error": "series not found"}), 404
        return jsonify(payload)

    @app.get("/dashboard/series/<int:series_id>")
    def dashboard_series_detail_page(series_id: int):
        return render_template("dashboard_series_detail.html", series_id=series_id)

    @app.get("/api/dashboard/users")
    def dashboard_users():
        current_settings = settings_manager.load(env_path=env_path)
        page = max(_safe_int(request.args.get("page", 1), 1), 1)
        page_size = 12
        status = str(request.args.get("status", "all") or "all").strip().lower()
        if status not in {"all", "normal", "suspended", "cleared", "no_novels", "unknown"}:
            status = "all"
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            payload = db.list_users(page=page, page_size=page_size, status=status)
        finally:
            db.close()
        return jsonify(payload)

    @app.get("/api/dashboard/users/<int:user_id>")
    def dashboard_user_detail(user_id: int):
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            payload = db.get_user_detail(user_id)
        finally:
            db.close()
        if payload is None:
            return jsonify({"error": "user not found"}), 404
        return jsonify(payload)

    @app.get("/dashboard/users/<int:user_id>")
    def dashboard_user_detail_page(user_id: int):
        return render_template("dashboard_user_detail.html", user_id=user_id)

    @app.get("/api/dashboard/users/<int:user_id>/novels")
    def dashboard_user_novels(user_id: int):
        current_settings = settings_manager.load(env_path=env_path)
        page = max(_safe_int(request.args.get("page", 1), 1), 1)
        page_size = 10
        category = request.args.get("category", "all")
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            if category == "series":
                payload = db.list_user_series(user_id, page=page, page_size=page_size)
            else:
                payload = db.list_user_novels(user_id, page=page, page_size=page_size, category=category)
        finally:
            db.close()
        return jsonify(payload)

    @app.post("/api/dashboard/users/<int:user_id>/check")
    def check_user_status(user_id: int):
        current_settings = settings_manager.load(env_path=env_path)
        from .auth import PixivAuthManager
        auth = PixivAuthManager(current_settings.pixiv)
        api, _ = auth.login()
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            status = _check_pixiv_user_status(api, user_id)
            db.upsert_user_status(user_id, status)
            return jsonify({"ok": True, "status": status})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()

    @app.post("/api/dashboard/users/<int:user_id>/sync")
    def sync_user_novels(user_id: int):
        """触发某用户全部小说的后台备份任务，避免阻塞 HTTP 请求。"""
        if _has_active_shared_jobs():
            return jsonify({"ok": False, "error": "已有同步任务正在运行，请稍后再试"}), 400
        current_settings = settings_manager.load(env_path=env_path)
        try:
            spec = _web_job_spec([f"user_backup:{user_id}"])
            job = _submit_shared_web_job(spec, current_settings, "user_backup", f"用户 {user_id} 备份")
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        return jsonify({"ok": True, "job_id": job.job_id, "job": _shared_job_to_dict(job)})

    @app.get("/api/dashboard/settings")
    def dashboard_settings():
        current_settings = settings_manager.load(env_path=env_path)
        return jsonify(_settings_to_dict(current_settings))

    @app.post("/api/dashboard/settings")
    def dashboard_settings_save():
        payload = request.get_json(silent=True) or {}
        try:
            saved = settings_manager.save_sync_settings(payload)
        except Exception as exc:
            return jsonify({"error": f"保存设置失败：{exc}"}), 400
        return jsonify({"ok": True, "message": "设置已保存", "sync": saved})

    @app.post("/api/dashboard/sync/start")
    def dashboard_sync_start():
        current_settings = settings_manager.load(env_path=env_path)
        spec = _build_web_sync_job_spec(current_settings)

        try:
            job = _submit_shared_web_job(spec, current_settings, "manual", "全量手动同步")
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "message": job.message, "job": _shared_job_to_dict(job)})

    @app.post("/api/dashboard/check-bookmarks")
    def dashboard_check_bookmarks():
        """预检查：扫描所有需要同步的内容，标记哪些已存在"""
        current_settings = settings_manager.load(env_path=env_path)
        
        try:
            spec = _web_job_spec(["sync_check"])
            job = _submit_shared_web_job(spec, current_settings, "sync_check", "预检查所有内容")
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify({"ok": True, "message": "预检查任务已启动", "job": _shared_job_to_dict(job)})

    @app.get("/api/dashboard/sync/status")
    def dashboard_sync_status():
        job_id = request.args.get("job_id", "").strip()
        shared_job = shared_job_manager.get_job(job_id) if job_id else shared_job_manager.latest_job()
        if shared_job is not None:
            return jsonify({"job": _shared_job_to_dict(shared_job)})
        legacy_job = sync_job_manager.get_job(job_id) if job_id else sync_job_manager.latest_job()
        return jsonify({"job": _job_to_dict(legacy_job)})

    @app.post("/api/dashboard/sync/<task_type>")
    def dashboard_sync_single(task_type: str):
        """手动触发单个同步任务"""
        task_map = {
            "bookmark": ("bookmark", "同步收藏"),
            "following_users": ("following_users", "同步关注用户"),
            "following_novels": ("following_novels", "同步关注小说"),
            "user_status": ("user_status", "检查用户状态"),
            "novel_status": ("novel_status", "检查小说状态"),
            "series_status": ("series_status", "检查系列状态"),
        }
        
        if task_type not in task_map:
            return jsonify({"error": "不支持的任务类型"}), 400
        
        internal_type, task_name = task_map[task_type]
        current_settings = settings_manager.load(env_path=env_path)
        try:
            spec = _web_job_spec([internal_type])
            job = _submit_shared_web_job(spec, current_settings, internal_type, task_name)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "message": "任务已启动", "job": _shared_job_to_dict(job)})

    @app.post("/api/dashboard/sync/subscribed-series")
    def dashboard_sync_subscribed_series():
        current_settings = settings_manager.load(env_path=env_path)

        # 从请求体获取 limit 参数
        req_data = request.get_json(silent=True) or {}
        limit = int(req_data.get("limit", 0) or 0)

        try:
            spec = _web_job_spec(["subscribed_series"], params={"limit": limit})
            job = _submit_shared_web_job(
                spec,
                current_settings,
                "subscribed_series",
                "同步追更系列",
                progress={"series_limit": limit},
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "message": "任务已启动", "job": _shared_job_to_dict(job)})

    @app.get("/api/dashboard/auto-sync/status")
    def auto_sync_status():
        """获取定时同步状态"""
        status = auto_sync_scheduler.get_status()
        # 如果有当前正在执行的任务，获取任务详情
        if status.get("current_task_job_id"):
            job = sync_job_manager.get_job(status["current_task_job_id"])
            if job:
                status["current_job"] = _job_to_dict(job)
        return jsonify(status)

    @app.post("/api/dashboard/auto-sync/toggle")
    def auto_sync_toggle():
        """切换定时同步开关"""
        data = request.get_json(silent=True) or {}
        enabled = data.get("enabled")
        if enabled is None:
            return jsonify({"error": "missing enabled parameter"}), 400
        
        # 更新配置文件
        if config_path:
            config_path_obj = Path(config_path)
            if config_path_obj.exists():
                with config_path_obj.open("r", encoding="utf-8") as f:
                    config_data = yaml.safe_load(f) or {}
                sync_data = config_data.setdefault("sync", {})
                sync_data["auto_sync_enabled"] = bool(enabled)
                _atomic_write_yaml(config_path_obj, config_data)
        
        if enabled:
            auto_sync_scheduler.start()
        else:
            auto_sync_scheduler.stop()
        
        return jsonify({"ok": True, "enabled": enabled})
    
    @app.post("/api/dashboard/auto-sync/stop-task")
    def auto_sync_stop_task():
        """停止当前正在执行的定时任务"""
        if auto_sync_scheduler.stop_current_task():
            return jsonify({"ok": True, "message": "正在停止当前任务"})
        return jsonify({"ok": False, "message": "当前没有正在执行的定时任务"})

    @app.get("/api/dashboard/logs")
    def get_logs():
        """获取任务日志列表"""
        try:
            page = request.args.get("page", 1, type=int)
            page_size = request.args.get("page_size", 20, type=int)
            task_type = request.args.get("task_type")
            is_auto = request.args.get("is_auto")
            days = request.args.get("days", 3, type=int)

            is_auto_sync = None
            if is_auto == "true":
                is_auto_sync = True
            elif is_auto == "false":
                is_auto_sync = False

            current_settings = settings_manager.load(env_path=env_path)
            db = Database(current_settings.storage.db_path)
            db.init_schema()
            try:
                result = db.get_task_logs(
                    page=page,
                    page_size=page_size,
                    task_type=task_type,
                    is_auto_sync=is_auto_sync,
                    days=days
                )
            finally:
                db.close()
            return jsonify(result)
        except Exception as e:
            logger.error("获取日志失败：%s", e)
            return jsonify({"error": str(e)}), 500

    @app.get("/api/dashboard/logs/<int:log_id>")
    def get_log_detail(log_id: int):
        """获取单条任务日志详情"""
        try:
            current_settings = settings_manager.load(env_path=env_path)
            db = Database(current_settings.storage.db_path)
            db.init_schema()
            try:
                item = db.get_task_log_by_id(log_id)
            finally:
                db.close()
            if not item:
                return jsonify({"error": "log not found"}), 404
            return jsonify(item)
        except Exception as e:
            logger.error("获取日志详情失败：%s", e)
            return jsonify({"error": str(e)}), 500

    @app.get("/api/cache/status")
    def cache_status():
        import shutil
        cache_dir = Path("/var/cache/nginx/pixiv_img")
        if not cache_dir.exists():
            return jsonify({"exists": False, "size_bytes": 0, "size_human": "0B"})
        total_size = 0
        file_count = 0
        for f in cache_dir.rglob("*"):
            if f.is_file():
                total_size += f.stat().st_size
                file_count += 1
        def human_size(size):
            for unit in ['B', 'KB', 'MB', 'GB']:
                if size < 1024:
                    return f"{size:.1f}{unit}"
                size /= 1024
            return f"{size:.1f}TB"
        return jsonify({
            "exists": True,
            "size_bytes": total_size,
            "size_human": human_size(total_size),
            "file_count": file_count
        })

    @app.post("/api/cache/clear")
    def cache_clear():
        import shutil
        cache_dir = Path("/var/cache/nginx/pixiv_img")
        if not cache_dir.exists():
            return jsonify({"ok": True, "message": "缓存目录不存在"})
        try:
            for item in cache_dir.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            return jsonify({"ok": True, "message": "缓存已清空"})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
    
    # 数据删除 API
    @app.delete("/api/dashboard/novels/<int:novel_id>")
    def delete_novel(novel_id: int):
        """删除小说"""
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            archive_refs = db.list_novel_archive_refs(novel_ids=[novel_id])
            archive_cleanup = _remove_archive_files(current_settings, archive_refs)
            db.delete_novel(novel_id)
            return jsonify({"ok": True, "message": "小说已删除", "archive_cleanup": archive_cleanup})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()
    
    @app.delete("/api/dashboard/users/<int:user_id>")
    def delete_user(user_id: int):
        """删除用户及其所有小说"""
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            archive_refs = db.list_novel_archive_refs(user_id=user_id)
            archive_cleanup = _remove_archive_files(current_settings, archive_refs)
            db.delete_user(user_id)
            return jsonify({"ok": True, "message": "用户及其相关数据已删除", "archive_cleanup": archive_cleanup})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()
    
    @app.delete("/api/dashboard/series/<int:series_id>")
    def delete_series(series_id: int):
        """删除系列"""
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            db.delete_series(series_id)
            return jsonify({"ok": True, "message": "系列已删除"})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()
    
    @app.delete("/api/dashboard/bookmarks/<int:novel_id>")
    def delete_bookmark(novel_id: int):
        """删除收藏记录"""
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            db.delete_bookmark(novel_id)
            return jsonify({"ok": True, "message": "收藏记录已删除"})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 待确认删除 API
    # ------------------------------------------------------------------

    @app.get("/api/dashboard/pending-deletions")
    def list_pending_deletions_api():
        current_settings = settings_manager.load(env_path=env_path)
        page = max(_safe_int(request.args.get("page", 1), 1), 1)
        page_size = _safe_int(request.args.get("page_size", 20), 20)
        item_type = request.args.get("item_type") or None
        if item_type not in (None, "novel", "series"):
            item_type = None
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            payload = db.list_pending_deletions(page=page, page_size=page_size, item_type=item_type)
        finally:
            db.close()
        return jsonify(payload)

    @app.get("/api/dashboard/shell-data")
    def shell_data():
        """提供前端 Shell (Navbar 等) 需要的全局聚合数据"""
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            pending_count = db.get_pending_deletion_count()
            # 可以根据需要扩展用户信息等
        finally:
            db.close()
        return jsonify({
            "pending_count": pending_count
        })

    @app.get("/api/dashboard/pending-deletions/count")
    def pending_deletion_count():
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            count = db.get_pending_deletion_count()
        finally:
            db.close()
        return jsonify({"count": count})

    @app.post("/api/dashboard/pending-deletions/detect")
    def trigger_pending_detection():
        current_settings = settings_manager.load(env_path=env_path)
        try:
            spec = _web_job_spec(["pending_deletion_detection"])
            job = _submit_shared_web_job(spec, current_settings, "pending_deletion_detection", "检测取消收藏/追更")
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "message": "检测任务已启动", "job": _shared_job_to_dict(job)})

    @app.post("/api/dashboard/pending-deletions/<int:deletion_id>/confirm")
    def confirm_pending_deletion(deletion_id: int):
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            record = db.confirm_pending_deletion(deletion_id)
            if record is None:
                return jsonify({"error": "记录不存在或已处理"}), 404
            item_type = record["item_type"]
            item_id = record["item_id"]
            if item_type == "novel":
                archive_refs = db.list_novel_archive_refs(novel_ids=[item_id])
                archive_cleanup = _remove_archive_files(current_settings, archive_refs)
                db.delete_novel(item_id)
            elif item_type == "series":
                archive_refs = db.list_novel_archive_refs(series_id=item_id)
                archive_cleanup = _remove_archive_files(current_settings, archive_refs)
                novel_rows = db.conn.execute(
                    "SELECT novel_id FROM novels WHERE series_id = ?", (item_id,)
                ).fetchall()
                for row in novel_rows:
                    db.delete_novel(row[0])
                db.delete_series(item_id)
            else:
                archive_cleanup = {"dirs_removed": 0, "files_removed": 0, "missing": 0, "skipped": 0}
            return jsonify({"ok": True, "message": "已确认删除", "archive_cleanup": archive_cleanup})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()

    @app.post("/api/dashboard/pending-deletions/<int:deletion_id>/restore")
    def restore_pending_deletion(deletion_id: int):
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            record = db.restore_pending_deletion(deletion_id)
            if record is None:
                return jsonify({"error": "记录不存在或已处理"}), 404
            item_type = record["item_type"]
            item_id = record["item_id"]
            source_type = record.get("source_type")
            if item_type == "novel" and source_type:
                from .models import SourceRecord
                db.upsert_source(SourceRecord(
                    novel_id=item_id, source_type=source_type,
                    source_key=str(current_settings.pixiv.user_id or 0),
                ))
            elif item_type == "series":
                db.conn.execute("UPDATE series SET is_subscribed = 1 WHERE series_id = ?", (item_id,))
                db.conn.commit()
            return jsonify({"ok": True, "message": "已恢复"})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 健康检查 API
    # ------------------------------------------------------------------
    @app.get("/api/health")
    def health_check():
        """返回服务健康状态"""
        uptime = round(time.time() - _service_start_time, 2)

        # 检查数据库是否可访问
        db_accessible = False
        db = None
        try:
            current_settings = settings_manager.load(env_path=env_path)
            db = Database(current_settings.storage.db_path)
            db.init_schema()
            db.conn.execute("SELECT 1")
            db_accessible = True
        except Exception:
            db_accessible = False
        finally:
            if db is not None:
                db.close()

        # 当前运行中的任务数
        running_jobs = sum(1 for j in list(sync_job_manager._jobs.values()) if j.status == "running")

        return jsonify({
            "status": "ok",
            "version": "1.0.0",
            "uptime_seconds": uptime,
            "db_accessible": db_accessible,
            "running_jobs": running_jobs,
        })

    # ------------------------------------------------------------------
    # 导出同步统计数据 API
    # ------------------------------------------------------------------
    @app.get("/api/dashboard/export/stats")
    def dashboard_export_stats():
        """导出同步统计数据"""
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            # 小说总数
            total_novels = db.conn.execute(
                "SELECT COUNT(*) FROM novels"
            ).fetchone()[0]

            # 用户总数
            total_users = db.conn.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

            # 系列总数
            total_series = db.conn.execute(
                "SELECT COUNT(*) FROM series"
            ).fetchone()[0]

            # 按状态分组的小说数
            novels_by_status = {}
            for row in db.conn.execute(
                "SELECT status, COUNT(*) as cnt FROM novels GROUP BY status"
            ).fetchall():
                novels_by_status[row[0]] = row[1]

            # 按状态分组的用户数
            users_by_status = {}
            for row in db.conn.execute(
                "SELECT status, COUNT(*) as cnt FROM users GROUP BY status"
            ).fetchall():
                users_by_status[row[0]] = row[1]

            # 最近 10 条任务记录
            recent_tasks = []
            for row in db.conn.execute(
                "SELECT id, task_type, task_name, job_id, status, is_auto_sync, "
                "started_at, finished_at, error_message "
                "FROM task_logs ORDER BY id DESC LIMIT 10"
            ).fetchall():
                recent_tasks.append({
                    "id": row[0],
                    "task_type": row[1],
                    "task_name": row[2],
                    "job_id": row[3],
                    "status": row[4],
                    "is_auto_sync": bool(row[5]),
                    "started_at": row[6],
                    "finished_at": row[7],
                    "error_message": row[8],
                })

            return jsonify({
                "total_novels": total_novels,
                "total_users": total_users,
                "total_series": total_series,
                "novels_by_status": novels_by_status,
                "users_by_status": users_by_status,
                "recent_tasks": recent_tasks,
            })
        except Exception as exc:
            logger.error("Export stats failed: %s", exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 配置热重载 API
    # ------------------------------------------------------------------
    @app.post("/api/dashboard/settings/reload")
    def dashboard_settings_reload():
        """重新加载配置文件并返回新配置"""
        try:
            new_settings = settings_manager.load(env_path=env_path)
            new_config = _settings_to_dict(new_settings)

            # 如果定时调度器正在运行，更新其配置缓存
            if auto_sync_scheduler.is_running():
                logger.info("Reloading auto sync scheduler config after settings reload")
                # 清除调度器的下次运行时间缓存，让它在下一轮循环中重新计算
                with auto_sync_scheduler._lock:
                    auto_sync_scheduler._task_next_run.clear()

            return jsonify({"ok": True, "message": "配置已重新加载", "settings": new_config})
        except Exception as exc:
            logger.error("Settings reload failed: %s", exc)
            return jsonify({"error": f"配置重载失败：{exc}"}), 500

    return app


def _settings_to_dict(settings: Settings) -> dict[str, Any]:
    return {
        "enabled": settings.sync.enabled,
        "initial_manual_only": settings.sync.initial_manual_only,
        "download_assets": settings.sync.download_assets,
        "write_markdown": settings.sync.write_markdown,
        "write_raw_text": settings.sync.write_raw_text,
        "bookmark_restricts": settings.sync.bookmark_restricts,
        "max_items_per_run": settings.sync.max_items_per_run,
        "max_pages_per_run": settings.sync.max_pages_per_run,
        "delay_seconds_between_items": settings.sync.delay_seconds_between_items,
        "delay_seconds_between_pages": settings.sync.delay_seconds_between_pages,
        "sync_bookmarks": settings.sync.sync_bookmarks,
        "sync_following_users": settings.sync.sync_following_users,
        "sync_following_novels": settings.sync.sync_following_novels,
        "sync_subscribed_series": settings.sync.sync_subscribed_series,
        "series_sync_limit": settings.sync.series_sync_limit,
        "delay_seconds_between_series": settings.sync.delay_seconds_between_series,
        "delay_seconds_between_chapters": settings.sync.delay_seconds_between_chapters,
        "delay_seconds_between_skips": settings.sync.delay_seconds_between_skips,
        "auto_sync_enabled": settings.sync.auto_sync_enabled,
        "auto_sync_timezone": settings.sync.auto_sync_timezone,
        "auto_sync_bookmarks_enabled": settings.sync.auto_sync_bookmarks_enabled,
        "auto_sync_bookmarks_interval_hours": settings.sync.auto_sync_bookmarks_interval_hours,
        "auto_sync_bookmarks_cron": settings.sync.auto_sync_bookmarks_cron,
        "auto_sync_following_list_enabled": settings.sync.auto_sync_following_list_enabled,
        "auto_sync_following_list_interval_hours": settings.sync.auto_sync_following_list_interval_hours,
        "auto_sync_following_list_cron": settings.sync.auto_sync_following_list_cron,
        "auto_sync_following_novels_enabled": settings.sync.auto_sync_following_novels_enabled,
        "auto_sync_following_novels_interval_hours": settings.sync.auto_sync_following_novels_interval_hours,
        "auto_sync_following_novels_cron": settings.sync.auto_sync_following_novels_cron,
        "auto_sync_following_novels_users_limit": settings.sync.auto_sync_following_novels_users_limit,
        "auto_sync_user_status_enabled": settings.sync.auto_sync_user_status_enabled,
        "auto_sync_user_status_interval_hours": settings.sync.auto_sync_user_status_interval_hours,
        "auto_sync_user_status_cron": settings.sync.auto_sync_user_status_cron,
        "auto_sync_novel_status_enabled": settings.sync.auto_sync_novel_status_enabled,
        "auto_sync_novel_status_interval_hours": settings.sync.auto_sync_novel_status_interval_hours,
        "auto_sync_novel_status_cron": settings.sync.auto_sync_novel_status_cron,
        "auto_sync_series_status_enabled": settings.sync.auto_sync_series_status_enabled,
        "auto_sync_series_status_interval_hours": settings.sync.auto_sync_series_status_interval_hours,
        "auto_sync_series_status_cron": settings.sync.auto_sync_series_status_cron,
        "auto_sync_subscribed_series_enabled": settings.sync.auto_sync_subscribed_series_enabled,
        "auto_sync_subscribed_series_interval_hours": settings.sync.auto_sync_subscribed_series_interval_hours,
        "auto_sync_subscribed_series_cron": settings.sync.auto_sync_subscribed_series_cron,
        "auto_sync_user_backup_enabled": settings.sync.auto_sync_user_backup_enabled,
        "auto_sync_user_backup_interval_hours": settings.sync.auto_sync_user_backup_interval_hours,
        "auto_sync_user_backup_cron": settings.sync.auto_sync_user_backup_cron,
        "auto_sync_pending_detection_enabled": settings.sync.auto_sync_pending_detection_enabled,
        "auto_sync_pending_detection_interval_hours": settings.sync.auto_sync_pending_detection_interval_hours,
        "auto_sync_pending_detection_cron": settings.sync.auto_sync_pending_detection_cron,
    }


def _job_to_dict_unified(job: JobState | SyncJobState | None) -> dict[str, Any] | None:
    """6.9: 统一两套job序列化"""
    if job is None:
        return None
    elapsed = None
    if job.started_at:
        end = job.finished_at or time.time()
        elapsed = round(end - job.started_at, 1)

    # 通用字段
    result = {
        "job_id": job.job_id,
        "status": job.status.value if hasattr(job.status, "value") else job.status,
        "message": job.message,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "elapsed": elapsed,
        "stats": job.stats,
        "error": job.error,
        "progress": job.progress,
    }

    # JobState专用字段
    if isinstance(job, JobState):
        result["logs"] = [{"time": entry.time, "level": entry.level, "message": entry.message} for entry in job.logs]
        result["task_list"] = list(job.task_types)
        result["current_task_index"] = int(job.progress.get("current_task_index", 0) or 0)
        result["is_auto_sync"] = job.spec.source == JobSource.SCHEDULER
        result["source"] = job.spec.source.value
        result["job_type"] = job.spec.job_type.value
    # SyncJobState专用字段
    else:
        result["logs"] = job.logs
        result["task_list"] = job.task_list
        result["current_task_index"] = job.current_task_index
        result["is_auto_sync"] = job.is_auto_sync

    return result


def _shared_job_to_dict(job: JobState | None) -> dict[str, Any] | None:
    """向后兼容wrapper"""
    return _job_to_dict_unified(job)


def _job_to_dict(job: SyncJobState | None) -> dict[str, Any] | None:
    """向后兼容wrapper"""
    return _job_to_dict_unified(job)


def _web_job_spec(task_list: list[str] | None, params: dict[str, Any] | None = None) -> JobSpec:
    tasks = list(task_list or [])
    job_params = dict(params or {})
    if len(tasks) == 1 and tasks[0].startswith("user_backup:"):
        user_id = int(tasks[0].split(":", 1)[1])
        job_params.setdefault("user_id", user_id)
        return JobSpec(
            source=JobSource.WEB,
            job_type=JobType.USER_BACKUP,
            task_types=tasks,
            params=job_params,
        )
    if tasks == ["sync_check"]:
        return JobSpec(source=JobSource.WEB, job_type=JobType.SYNC_CHECK, task_types=tasks, params=job_params)
    if tasks == ["pending_deletion_detection"]:
        return JobSpec(
            source=JobSource.WEB,
            job_type=JobType.PENDING_DELETION_DETECTION,
            task_types=tasks,
            params=job_params,
        )
    if len(tasks) == 1 and tasks[0] in {"user_status", "novel_status", "series_status"}:
        return JobSpec(source=JobSource.WEB, job_type=JobType.STATUS_CHECK, task_types=tasks, params=job_params)
    return JobSpec(source=JobSource.WEB, job_type=JobType.SYNC, task_types=tasks, params=job_params)


def _build_web_sync_job_spec(settings: Settings) -> JobSpec:
    return _web_job_spec(build_default_task_list(settings))


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式错误：{path}")
    return data


def _safe_int(value: Any, default: int) -> int:
    """安全解析整数参数，无效值返回 default。"""
    try:
        return int(value) if value not in (None, "") else default
    except (ValueError, TypeError):
        return default


def _normalize_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    result = int(value)
    if result <= 0:
        raise ValueError("整数值必须大于 0")
    return result


def _normalize_int(value: Any, default: int) -> int:
    """容错地把任意输入转成整数；空串/None/非法输入返回 default。"""
    if value in (None, ""):
        return int(default)
    try:
        return int(value)
    except (ValueError, TypeError):
        return int(default)


def _normalize_float(value: Any, min_value: float = 0.0) -> float:
    if value in (None, ""):
        return float(min_value)
    result = float(value)
    if result < min_value:
        raise ValueError(f"数值不能小于 {min_value}")
    return result


def _restricts_to_label(restricts: list[str]) -> str:
    mapping = {"public": "公开收藏", "private": "私密收藏"}
    labels = [mapping[item] for item in restricts if item in mapping]
    return " / ".join(labels) if labels else "无"


def _external_base_url(req) -> str:
    forwarded_proto = req.headers.get("X-Forwarded-Proto")
    forwarded_host = req.headers.get("X-Forwarded-Host")
    if forwarded_proto and forwarded_host:
        # 仅信任白名单中的 forwarded host，防止 OAuth 回调被劫持
        trusted_hosts = os.environ.get("TRUSTED_FORWARDED_HOSTS", "")
        if trusted_hosts:
            allowed = {h.strip().lower() for h in trusted_hosts.split(",") if h.strip()}
            if forwarded_host.lower() in allowed:
                return f"{forwarded_proto}://{forwarded_host}"
            logger.warning("Untrusted X-Forwarded-Host: %s (allowed: %s)", forwarded_host, allowed)

    parsed = urlparse(req.base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _check_pixiv_user_status(api: Any, user_id: int) -> str:
    """检查 Pixiv 用户状态"""
    try:
        result = api.user_detail(user_id)
        if result is None:
            return "suspended"
        user = getattr(result, "user", None)
        if user is None:
            return "suspended"
        profile = getattr(result, "profile", None)
        if profile:
            total_novels = getattr(profile, "total_novels", 0) or 0
            if total_novels == 0:
                return "no_novels"
        return "normal"
    except Exception as e:
        logger.warning("Failed to check user %s status: %s", user_id, e)
        return "unknown"


def _check_novel_status(api: Any, novel_id: int) -> str:
    """检查小说状态：normal/deleted/restricted"""
    try:
        result = api.novel_detail(novel_id)
        if result is None:
            return "deleted"
        novel = getattr(result, "novel", None)
        if novel is None:
            if isinstance(result, dict):
                novel = result.get("novel")
            if novel is None:
                return "deleted"
        visible = getattr(novel, "visible", True)
        if isinstance(novel, dict):
            visible = novel.get("visible", True)
        if not visible:
            return "restricted"
        return "normal"
    except Exception as e:
        logger.warning("Failed to check novel %s status: %s", novel_id, e)
        return "unknown"


def _check_series_status(api: Any, series_id: int) -> str:
    """检查系列状态：normal/deleted"""
    try:
        result = api.novel_series(series_id)
        if result is None:
            return "deleted"
        detail = None
        if isinstance(result, dict):
            detail = result.get("novel_series_detail")
        if detail is None:
            detail = getattr(result, "novel_series_detail", None)
        if detail is None:
            return "deleted"
        return "normal"
    except Exception as e:
        logger.warning("Failed to check series %s status: %s", series_id, e)
        return "unknown"


def _remove_archive_files(settings: Settings, archive_refs: list[dict[str, Any]]) -> dict[str, int]:
    """Remove local archive files for DB rows that are about to be deleted."""
    storage = FileStorage(settings)
    novel_dirs: list[Path] = []
    asset_paths: list[Path] = []
    for ref in archive_refs:
        try:
            novel_id = int(ref.get("novel_id") or 0)
            user_id = int(ref.get("user_id") or 0)
        except (TypeError, ValueError):
            continue
        if not novel_id:
            continue
        novel_dirs.append(
            storage.novel_dir(
                str(ref.get("restrict_value") or "public"),
                user_id,
                str(ref.get("author_name") or "unknown"),
                novel_id,
                str(ref.get("title") or f"novel_{novel_id}"),
            )
        )
        for path in ref.get("asset_paths") or []:
            if path:
                asset_path = Path(path)
                asset_paths.append(asset_path)
                try:
                    if asset_path.parent.parent.name == "assets":
                        novel_dirs.append(asset_path.parent.parent.parent)
                except IndexError:
                    pass
    return storage.remove_novel_archive(novel_dirs, asset_paths)

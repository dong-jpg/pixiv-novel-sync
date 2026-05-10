from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests as http_requests
import yaml
from flask import Flask, Response, jsonify, redirect, render_template, request

from .jobs.quick_sync import run_bookmark_sync, run_check_bookmarks_task
from .auth import PixivAuthManager
from .oauth_helper import OAuthManager
from .settings import Settings, load_settings
from .storage_db import Database
from .storage_files import FileStorage
from .sync_engine import BookmarkNovelSyncService

logger = logging.getLogger(__name__)


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


@dataclass(slots=True)
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
    _current_task_job_id: str | None = None  # 当前正在执行的定时任务 job_id
    _stop_current_task: bool = False  # 停止当前任务的标志
    
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
            {"name": "user_status", "setting_check": "auto_sync_user_status_enabled", "sync_func": "_sync_user_status", "interval_setting": "auto_sync_following_interval_hours", "cron_setting": "auto_sync_user_status_cron"},
        ]
        
        while self._running:
            try:
                settings = load_settings(self.config_path, self.env_path)
                
                # 清理超过3天的任务日志
                try:
                    db = Database(settings.storage.db_path)
                    db.init_schema()
                    db.cleanup_old_task_logs(days=3)
                    db.close()
                except Exception as e:
                    logger.warning("Failed to cleanup old task logs: %s", e)

                now = time.time()
                timezone = settings.sync.auto_sync_timezone
                
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
                    
                    # 如果该任务还没有计算过下次运行时间，现在计算
                    if task_name not in self._task_next_run:
                        if cron_expr:
                            from .settings import cron_to_next_run
                            self._task_next_run[task_name] = cron_to_next_run(cron_expr, now, timezone) or (now + task_interval_seconds)
                        else:
                            self._task_next_run[task_name] = now + task_interval_seconds
                        logger.info("Task %s scheduled, next run: %s", task_name, 
                                    datetime.fromtimestamp(self._task_next_run[task_name]).strftime('%Y-%m-%d %H:%M:%S'))
                    
                    next_run = self._task_next_run[task_name]
                    
                    if now >= next_run:
                        with self._lock:
                            if self._current_task_job_id is not None:
                                logger.info("Task %s skipped: another task is running (%s)", task_name, self._current_task_job_id)
                                if cron_expr:
                                    from .settings import cron_to_next_run
                                    self._task_next_run[task_name] = cron_to_next_run(cron_expr, now, timezone) or (now + task_interval_seconds)
                                else:
                                    self._task_next_run[task_name] = now + task_interval_seconds
                                continue
                        
                        self._run_single_task(settings, task_name, task_config["sync_func"])
                        
                        self._task_last_run[task_name] = time.time()
                        if cron_expr:
                            from .settings import cron_to_next_run
                            self._task_next_run[task_name] = cron_to_next_run(cron_expr, time.time(), timezone) or (time.time() + task_interval_seconds)
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
            task_labels = {
                "bookmarks": "同步收藏小说",
                "following_list": "同步关注用户列表",
                "following_novels": "同步关注用户小说",
                "subscribed_series": "同步追更系列",
                "user_status": "检查用户状态",
            }
            job = self.sync_job_manager.start_auto_job(task_name, task_labels.get(task_name, task_name))
            self._current_task_job_id = job.job_id

            # 创建数据库日志记录
            try:
                db = Database(settings.storage.db_path)
                db.init_schema()
                log_id = db.create_task_log(
                    task_type=task_name,
                    task_name=task_labels.get(task_name, task_name),
                    job_id=job.job_id,
                    is_auto_sync=True
                )
                job.log_id = log_id
                db.close()
            except Exception as e:
                logger.warning("Failed to create task log for %s: %s", task_name, e)

            try:
                # 执行对应的同步函数
                func = getattr(self, sync_func_name)
                func(settings, job.job_id)
                job.status = "succeeded"
                job.message = f"{task_labels.get(task_name, task_name)}完成"
            except Exception as e:
                job.status = "failed"
                job.message = f"任务失败: {str(e)}"
                job.error = str(e)
                logger.error("Auto sync task %s failed: %s", task_name, str(e))
            finally:
                job.finished_at = time.time()
                # 更新数据库日志
                if job.log_id:
                    try:
                        db = Database(settings.storage.db_path)
                        db.init_schema()
                        db.update_task_log(job.log_id, job.status, stats=job.stats, logs=job.logs)
                        db.close()
                    except Exception as e:
                        logger.warning("Failed to update task log: %s", e)
                with self._lock:
                    self._current_task_job_id = None
                    self._stop_current_task = False
        else:
            # 没有 job_manager，直接执行
            func = getattr(self, sync_func_name, None)
            if func:
                func(settings, None)
    
    def _check_stop(self) -> bool:
        """检查是否需要停止"""
        return self._stop_current_task or not self._running
    
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
                        if skipped:
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
            
            users_limit = settings.sync.auto_sync_following_novels_users_limit
            
            def on_progress(event_type: str, data: dict[str, Any]) -> None:
                if self._check_stop():
                    raise InterruptedError("Task stopped by user")
                if job_id and self.sync_job_manager:
                    if event_type == "novel_start":
                        self.sync_job_manager.add_log(job_id, "info", f"[{data.get('current', '?')}/{data.get('total', '?')}] {data.get('title', '')[:30]}")
                        self.sync_job_manager.update_progress(
                            job_id,
                            phase=data.get("phase", "同步用户小说"),
                            current=data.get('current', 0),
                            total=data.get('total', 50),
                            current_novel=data.get('title', '')[:40],
                            author=data.get('author', ''),
                        )
                    elif event_type == "novel_done":
                        skipped = data.get('skipped', 0)
                        if skipped:
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
                limit_text = f"限制 {users_limit} 位用户" if users_limit > 0 else "全部用户"
                self.sync_job_manager.add_log(job_id, "info", f"开始扫描关注用户的小说 ({limit_text})...")
            
            stats = service.sync_following_novels(
                download_assets=settings.sync.download_assets,
                write_markdown=settings.sync.write_markdown,
                write_raw_text=settings.sync.write_raw_text,
                progress_callback=on_progress,
                users_limit=users_limit,
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
        from .auth import PixivAuthManager
        
        if job_id and self.sync_job_manager:
            self.sync_job_manager.add_log(job_id, "info", "开始检查用户状态")
        
        auth = PixivAuthManager(settings.pixiv)
        api, auth_result = auth.login()
        if auth_result.user_id is None:
            raise RuntimeError("Unable to determine user ID")
        
        if self._check_stop():
            return
        
        db = Database(settings.storage.db_path)
        db.init_schema()
        
        try:
            # 获取所有关注的用户
            users = db.list_users(page=1, page_size=1000)
            user_list = users.get("items", [])
            
            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "info", f"共 {len(user_list)} 个用户需要检查")
            
            checked_count = 0
            for user in user_list:
                if self._check_stop():
                    return
                
                user_id = user.get("user_id")
                if not user_id:
                    continue
                
                try:
                    status = _check_pixiv_user_status(api, user_id)
                    db.upsert_user_status(user_id, status)
                    checked_count += 1
                    
                    if job_id and self.sync_job_manager:
                        self.sync_job_manager.add_log(job_id, "info", f"[{checked_count}/{len(user_list)}] 用户 {user.get('name', user_id)}: {status}")
                    
                    # 限速
                    time.sleep(settings.sync.delay_seconds_between_skips)
                except Exception as e:
                    logger.warning("Failed to check user %s status: %s", user_id, e)
            
            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, "success", f"用户状态检查完成: {checked_count} 个用户")
        finally:
            db.close()


@dataclass(slots=True)
class SyncJobManager:
    config_path: str | None
    env_path: str | None
    _jobs: dict[str, SyncJobState] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    MAX_LOGS: int = 50

    def start_job(self, task_list: list[str] | None = None) -> SyncJobState:
        with self._lock:
            running = [job for job in self._jobs.values() if job.status == "running"]
            if running:
                raise RuntimeError("已有同步任务正在运行，请稍后再试")
            job_id = str(int(time.time() * 1000))
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
    
    def start_auto_job(self, task_name: str, task_label: str) -> SyncJobState:
        """启动定时任务"""
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

    def get_job(self, job_id: str) -> SyncJobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest_job(self) -> SyncJobState | None:
        with self._lock:
            if not self._jobs:
                return None
            latest_key = sorted(self._jobs.keys())[-1]
            return self._jobs[latest_key]

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
            for task_type in job.task_list:
                self.update_progress(job_id, phase=task_type, message=f"正在执行: {task_type}")
                task_stats = self._run_single_sync(settings, task_type, job_id)
                if task_stats:
                    for key, val in task_stats.items():
                        total_stats[key] = total_stats.get(key, 0) + val
            stats = total_stats
            
            job.status = "succeeded"
            job.message = "同步完成"
            job.stats = stats
            self.add_log(job_id, "success", f"同步完成：{stats.get('novels', 0)} 本小说，{stats.get('assets_downloaded', 0)} 个资源")
            
            # 更新任务日志
            if job.log_id:
                try:
                    db = Database(settings.storage.db_path)
                    db.init_schema()
                    db.update_task_log(job.log_id, "completed", stats=stats, logs=job.logs)
                    db.close()
                except Exception as e:
                    logger.error("更新任务日志失败：%s", e)
        except Exception as exc:
            job.status = "failed"
            job.message = "同步失败"
            job.error = str(exc)
            self.add_log(job_id, "error", f"同步失败：{exc}")
            
            # 更新任务日志（失败）
            if job.log_id:
                try:
                    settings = load_settings(self.config_path, self.env_path)
                    db = Database(settings.storage.db_path)
                    db.init_schema()
                    db.update_task_log(job.log_id, "failed", error_message=str(exc), logs=job.logs)
                    db.close()
                except Exception as e:
                    logger.error("更新任务日志失败：%s", e)
        finally:
            job.finished_at = time.time()

    def _run_single_sync(self, settings: Settings, task_type: str, job_id: str) -> dict[str, Any]:
        """根据 task_type 执行单个同步任务"""
        from .auth import PixivAuthManager
        from .sync_engine import BookmarkNovelSyncService

        auth = PixivAuthManager(settings.pixiv)
        self.add_log(job_id, "info", "正在登录 Pixiv...")
        api, auth_result = auth.login()
        if auth_result.user_id is None:
            raise RuntimeError("Unable to determine PIXIV_USER_ID")
        self.add_log(job_id, "success", f"登录成功, 用户ID: {auth_result.user_id}")

        db = Database(settings.storage.db_path)
        db.init_schema()
        storage = FileStorage(settings)
        storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])

        try:
            service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)

            def on_progress(event_type: str, data: dict[str, Any]) -> None:
                if event_type == "novel_start":
                    self.add_log(job_id, "info", f"[{data.get('current', '?')}/{data.get('total', '?')}] {data.get('title', '')[:30]}")
                    self.update_progress(job_id, phase=data.get("phase", task_type), current=data.get('current', 0), total=data.get('total', 50))
                elif event_type == "novel_done":
                    if data.get('skipped'):
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
                next_query: dict[str, Any] | None = {"restrict": "public"}
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
                users_limit = settings.sync.auto_sync_following_novels_users_limit
                limit_text = f"限制 {users_limit} 位用户" if users_limit > 0 else "全部用户"
                self.add_log(job_id, "info", f"开始扫描关注用户的小说 ({limit_text})...")
                stats = service.sync_following_novels(
                    download_assets=settings.sync.download_assets,
                    write_markdown=settings.sync.write_markdown,
                    write_raw_text=settings.sync.write_raw_text,
                    progress_callback=on_progress,
                    users_limit=users_limit,
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
                users = db.list_users(page=1, page_size=1000)
                user_list = users.get("items", [])
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

            else:
                self.add_log(job_id, "error", f"未知任务类型: {task_type}")
                return {}
        finally:
            db.close()


class SettingsManager:
    def __init__(self, config_path: str | None) -> None:
        self.config_path = config_path

    def load(self, env_path: str | None = None) -> Settings:
        return load_settings(self.config_path, env_path)

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
        sync_data["sync_following_series"] = bool(payload.get("sync_following_series", sync_data.get("sync_following_series", True)))
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
        sync_data["auto_sync_bookmarks_enabled"] = bool(payload.get("auto_sync_bookmarks_enabled", sync_data.get("auto_sync_bookmarks_enabled", True)))
        sync_data["auto_sync_bookmarks_interval_hours"] = int(payload.get("auto_sync_bookmarks_interval_hours", sync_data.get("auto_sync_bookmarks_interval_hours", 6)))
        sync_data["auto_sync_bookmarks_cron"] = str(payload.get("auto_sync_bookmarks_cron", sync_data.get("auto_sync_bookmarks_cron", "")))
        sync_data["auto_sync_following_list_enabled"] = bool(payload.get("auto_sync_following_list_enabled", sync_data.get("auto_sync_following_list_enabled", True)))
        sync_data["auto_sync_following_list_interval_hours"] = int(payload.get("auto_sync_following_list_interval_hours", sync_data.get("auto_sync_following_list_interval_hours", 24)))
        sync_data["auto_sync_following_list_cron"] = str(payload.get("auto_sync_following_list_cron", sync_data.get("auto_sync_following_list_cron", "")))
        sync_data["auto_sync_following_novels_enabled"] = bool(payload.get("auto_sync_following_novels_enabled", sync_data.get("auto_sync_following_novels_enabled", True)))
        sync_data["auto_sync_following_novels_interval_hours"] = int(payload.get("auto_sync_following_novels_interval_hours", sync_data.get("auto_sync_following_novels_interval_hours", 6)))
        sync_data["auto_sync_following_novels_cron"] = str(payload.get("auto_sync_following_novels_cron", sync_data.get("auto_sync_following_novels_cron", "")))
        sync_data["auto_sync_following_novels_users_limit"] = int(payload.get("auto_sync_following_novels_users_limit", sync_data.get("auto_sync_following_novels_users_limit", 0)))
        sync_data["auto_sync_user_status_enabled"] = bool(payload.get("auto_sync_user_status_enabled", sync_data.get("auto_sync_user_status_enabled", True)))
        sync_data["auto_sync_user_status_cron"] = str(payload.get("auto_sync_user_status_cron", sync_data.get("auto_sync_user_status_cron", "")))
        sync_data["auto_sync_subscribed_series_enabled"] = bool(payload.get("auto_sync_subscribed_series_enabled", sync_data.get("auto_sync_subscribed_series_enabled", True)))
        sync_data["auto_sync_subscribed_series_interval_hours"] = int(payload.get("auto_sync_subscribed_series_interval_hours", sync_data.get("auto_sync_subscribed_series_interval_hours", 6)))
        sync_data["auto_sync_subscribed_series_cron"] = str(payload.get("auto_sync_subscribed_series_cron", sync_data.get("auto_sync_subscribed_series_cron", "")))

        with config_path.open("w", encoding="utf-8") as file:
            yaml.safe_dump(config_data, file, allow_unicode=True, sort_keys=False)

        return _settings_to_dict(load_settings(config_path, None))


def create_app(config_path: str | None = None, env_path: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    settings_manager = SettingsManager(config_path)
    sync_job_manager = SyncJobManager(config_path=config_path, env_path=env_path)
    oauth_manager = OAuthManager()
    auto_sync_scheduler = AutoSyncScheduler(config_path=config_path, env_path=env_path, sync_job_manager=sync_job_manager)
    
    # 启动定时同步调度器
    auto_sync_scheduler.start()

    @app.get("/proxy/image")
    def proxy_image():
        url = request.args.get("url", "").strip()
        if not url:
            return Response("Missing url parameter", status=400)
        if "pximg.net" not in url:
            return Response("Only pixiv images are allowed", status=403)
        try:
            headers = {
                "Referer": "https://www.pixiv.net/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            current_settings = settings_manager.load(env_path=env_path)
            proxies = {"http": current_settings.pixiv.proxy, "https": current_settings.pixiv.proxy} if current_settings.pixiv.proxy else None
            resp = http_requests.get(url, headers=headers, timeout=15, verify=current_settings.pixiv.verify_ssl, proxies=proxies)
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

    @app.post("/api/token-jobs")
    def create_token_job():
        task = oauth_manager.create_task(_external_base_url(request))
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

    @app.get("/api/token-jobs/<job_id>")
    def get_token_job(job_id: str):
        task = oauth_manager.get_task(job_id)
        if task is None:
            return jsonify({"error": "task not found"}), 404
        return jsonify(
            {
                "task_id": task.task_id,
                "status": task.status,
                "message": task.message,
                "refresh_token": task.refresh_token,
                "access_token": task.access_token,
                "user_id": task.user_id,
                "mode": "oauth",
            }
        )

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
        return jsonify(
            {
                "task_id": task.task_id,
                "status": task.status,
                "message": task.message,
                "refresh_token": task.refresh_token,
                "access_token": task.access_token,
                "user_id": task.user_id,
                "mode": "oauth",
            }
        )

    @app.get("/oauth/callback")
    def oauth_callback():
        error = request.args.get("error")
        if error:
            return render_template("oauth_callback.html", ok=False, message=f"Pixiv 返回错误：{error}")

        code = request.args.get("code")
        state = request.args.get("state")
        if not code or not state:
            return render_template("oauth_callback.html", ok=False, message="回调参数缺失")

        task = oauth_manager.find_task_by_state(state)
        if task is None:
            return render_template("oauth_callback.html", ok=False, message="登录任务不存在或已过期")

        try:
            oauth_manager.exchange_code(task, code)
        except Exception as exc:
            task.status = "failed"
            task.message = f"token 交换失败：{exc}"
            return render_template("oauth_callback.html", ok=False, message=task.message)

        return render_template("oauth_callback.html", ok=True, message="Pixiv 登录成功，请返回原页面查看 token 结果")

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
        return jsonify({"ok": True, "message": task.message, "user_id": task.user_id})

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
        page = max(int(request.args.get("page", 1) or 1), 1)
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
        page = max(int(request.args.get("page", 1) or 1), 1)
        page_size = 10
        category = str(request.args.get("category", "all") or "all").strip().lower()
        if category not in {"all", "bookmark", "following"}:
            category = "all"
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            if category == "bookmark":
                payload = db.list_bookmark_novels(page=page, page_size=page_size)
            elif category == "following":
                payload = db.list_following_series(page=page, page_size=page_size)
            else:
                payload = db.list_recent_novels(page=page, page_size=page_size, category="all")
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
        page = max(int(request.args.get("page", 1) or 1), 1)
        page_size = 10
        status = str(request.args.get("status", "all") or "all").strip().lower()
        if status not in {"all", "normal", "suspended", "cleared", "unknown"}:
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
        page = max(int(request.args.get("page", 1) or 1), 1)
        page_size = 10
        category = request.args.get("category", "all")
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
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
        current_settings = settings_manager.load(env_path=env_path)
        from .auth import PixivAuthManager
        auth = PixivAuthManager(current_settings.pixiv)
        api, _ = auth.login()
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        storage = FileStorage(current_settings)
        storage.ensure_dirs([
            current_settings.storage.public_dir,
            current_settings.storage.private_dir,
            current_settings.storage.db_path.parent
        ])
        try:
            service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=current_settings)
            stats = {"users": 0, "novels": 0, "assets_downloaded": 0}
            from pixivpy3 import AppPixivAPI
            next_query: dict[str, Any] | None = {"user_id": user_id}
            while next_query:
                result = api.user_novels(**next_query)
                novels = getattr(result, "novels", []) or []
                for novel in novels:
                    counters = service._sync_novel(
                        novel, "public",
                        current_settings.sync.download_assets,
                        current_settings.sync.write_markdown,
                        current_settings.sync.write_raw_text,
                        source_type="user_backup",
                        source_key=str(user_id),
                    )
                    for key in ["users", "novels", "assets_downloaded"]:
                        stats[key] = stats.get(key, 0) + counters.get(key, 0)
                next_query = api.parse_qs(getattr(result, "next_url", None))
            return jsonify({"ok": True, "stats": stats})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()

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
        if current_settings.sync.initial_manual_only is False and current_settings.sync.enabled is False:
            pass
        
        # 构建任务列表
        task_list = []
        if current_settings.sync.sync_bookmarks:
            task_list.append("同步收藏小说")
        if current_settings.sync.sync_following_series:
            task_list.append("同步关注用户系列")
        if current_settings.sync.sync_following_users:
            task_list.append("同步关注用户列表")
        if current_settings.sync.sync_following_novels:
            task_list.append("同步关注用户小说")
        if current_settings.sync.sync_subscribed_series:
            task_list.append("同步追更系列")
        
        try:
            # 创建任务日志
            db = Database(current_settings.storage.db_path)
            db.init_schema()
            job = sync_job_manager.start_job(task_list)
            # 记录任务开始（在 job 启动后）
            log_id = db.create_task_log(
                task_type="manual",
                task_name=task_list[0] if task_list else "手动同步",
                job_id=job.job_id,
                is_auto_sync=False
            )
            job.log_id = log_id
            db.close()
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "message": job.message, "job": _job_to_dict(job)})

    @app.post("/api/dashboard/check-bookmarks")
    def dashboard_check_bookmarks():
        """预检查：扫描所有需要同步的内容，标记哪些已存在"""
        current_settings = settings_manager.load(env_path=env_path)
        
        # 检查是否有任务正在运行
        with sync_job_manager._lock:
            running = [job for job in sync_job_manager._jobs.values() if job.status == "running"]
            if running:
                return jsonify({"error": "已有同步任务正在运行，请稍后再试"}), 400
        
        # 创建一个专门的预检查任务（不通过 start_job，避免触发同步）
        job_id = f"check_{int(time.time() * 1000)}"
        job = SyncJobState(
            job_id=job_id,
            status="running",
            message="预检查任务已启动",
            started_at=time.time(),
            task_list=["预检查所有内容"],
        )
        with sync_job_manager._lock:
            sync_job_manager._jobs[job_id] = job
        
        # 启动后台任务
        import threading
        thread = threading.Thread(
            target=run_check_bookmarks_task,
            args=(current_settings, sync_job_manager, job_id),
            daemon=True,
        )
        thread.start()
        
        return jsonify({"ok": True, "message": "预检查任务已启动", "job": _job_to_dict(job)})

    @app.get("/api/dashboard/sync/status")
    def dashboard_sync_status():
        job_id = request.args.get("job_id", "").strip()
        job = sync_job_manager.get_job(job_id) if job_id else sync_job_manager.latest_job()
        return jsonify({"job": _job_to_dict(job)})

    @app.post("/api/dashboard/sync/<task_type>")
    def dashboard_sync_single(task_type: str):
        """手动触发单个同步任务"""
        task_map = {
            "bookmark": ("bookmark", "同步收藏"),
            "following_users": ("following_users", "同步关注用户"),
            "following_novels": ("following_novels", "同步关注小说"),
            "user_status": ("user_status", "检查用户状态"),
        }
        
        if task_type not in task_map:
            return jsonify({"error": "不支持的任务类型"}), 400
        
        internal_type, task_name = task_map[task_type]
        current_settings = settings_manager.load(env_path=env_path)
        
        try:
            # 创建任务日志
            db = Database(current_settings.storage.db_path)
            db.init_schema()
            job = sync_job_manager.start_job([internal_type])
            log_id = db.create_task_log(
                task_type=internal_type,
                task_name=task_name,
                job_id=job.job_id,
                is_auto_sync=False
            )
            job.log_id = log_id
            db.close()
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "message": "任务已启动", "job": _job_to_dict(job)})

    @app.post("/api/dashboard/sync/following")
    def dashboard_sync_following():
        current_settings = settings_manager.load(env_path=env_path)
        auth = PixivAuthManager(current_settings.pixiv)
        api, auth_result = auth.login()
        if auth_result.user_id is None:
            return jsonify({"error": "Unable to determine user ID"}), 400

        db = Database(current_settings.storage.db_path)
        db.init_schema()
        storage = FileStorage(current_settings)
        storage.ensure_dirs([
            current_settings.storage.public_dir,
            current_settings.storage.private_dir,
            current_settings.storage.db_path.parent
        ])

        try:
            service = BookmarkNovelSyncService(
                api=api, db=db, storage=storage, settings=current_settings
            )
            stats = service.sync_following_novels(
                download_assets=current_settings.sync.download_assets,
                write_markdown=current_settings.sync.write_markdown,
                write_raw_text=current_settings.sync.write_raw_text,
            )
            logger.info("Following novels sync finished: %s", json.dumps(stats, ensure_ascii=False))
            return jsonify({"ok": True, "stats": stats})
        except Exception as exc:
            logger.exception("Following novels sync failed")
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()

    @app.post("/api/dashboard/sync/subscribed-series")
    def dashboard_sync_subscribed_series():
        current_settings = settings_manager.load(env_path=env_path)

        # 从请求体获取 limit 参数
        req_data = request.get_json(silent=True) or {}
        limit = int(req_data.get("limit", 0) or 0)

        # 检查是否有任务正在运行
        with sync_job_manager._lock:
            running = [job for job in sync_job_manager._jobs.values() if job.status == "running"]
            if running:
                return jsonify({"error": "已有同步任务正在运行，请稍后再试"}), 400

        try:
            job = sync_job_manager.start_job(["subscribed_series"])
            # 将 limit 存入 job 的 progress 中，供 _run_single_sync 使用
            job.progress["series_limit"] = limit

            # 创建任务日志
            db = Database(current_settings.storage.db_path)
            db.init_schema()
            log_id = db.create_task_log(
                task_type="subscribed_series",
                task_name="同步追更系列",
                job_id=job.job_id,
                is_auto_sync=False
            )
            job.log_id = log_id
            db.close()
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "message": "任务已启动", "job": _job_to_dict(job)})

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
                with config_path_obj.open("w", encoding="utf-8") as f:
                    yaml.dump(config_data, f, allow_unicode=True, default_flow_style=False)
        
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
            db.delete_novel(novel_id)
            return jsonify({"ok": True, "message": "小说已删除"})
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
            db.delete_user(user_id)
            return jsonify({"ok": True, "message": "用户及其相关数据已删除"})
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
        "sync_following_series": settings.sync.sync_following_series,
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
        "auto_sync_user_status_cron": settings.sync.auto_sync_user_status_cron,
        "auto_sync_subscribed_series_enabled": settings.sync.auto_sync_subscribed_series_enabled,
        "auto_sync_subscribed_series_interval_hours": settings.sync.auto_sync_subscribed_series_interval_hours,
        "auto_sync_subscribed_series_cron": settings.sync.auto_sync_subscribed_series_cron,
    }


def _job_to_dict(job: SyncJobState | None) -> dict[str, Any] | None:
    if job is None:
        return None
    elapsed = None
    if job.started_at:
        end = job.finished_at or time.time()
        elapsed = round(end - job.started_at, 1)
    return {
        "job_id": job.job_id,
        "status": job.status,
        "message": job.message,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "elapsed": elapsed,
        "stats": job.stats,
        "error": job.error,
        "progress": job.progress,
        "logs": job.logs,
        "task_list": job.task_list,
        "current_task_index": job.current_task_index,
        "is_auto_sync": job.is_auto_sync,
    }


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式错误：{path}")
    return data


def _normalize_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    result = int(value)
    if result <= 0:
        raise ValueError("整数值必须大于 0")
    return result


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
        return f"{forwarded_proto}://{forwarded_host}"

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
                return "cleared"
        return "normal"
    except Exception:
        return "unknown"

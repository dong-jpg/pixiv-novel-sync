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

from .jobs.quick_sync import run_bookmark_sync
from .oauth_helper import OAuthManager
from .settings import Settings, load_settings
from .storage_db import Database

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


@dataclass(slots=True)
class AutoSyncScheduler:
    """定时同步调度器"""
    config_path: str | None
    env_path: str | None
    _running: bool = False
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last_run_time: float | None = None
    _next_run_time: float | None = None
    
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
            logger.info("Auto sync scheduler stopped")
    
    def is_running(self) -> bool:
        return self._running
    
    def get_status(self) -> dict[str, Any]:
        """获取调度器状态"""
        return {
            "running": self._running,
            "last_run_time": self._last_run_time,
            "next_run_time": self._next_run_time,
        }
    
    def _run_scheduler(self) -> None:
        """调度器主循环"""
        while self._running:
            try:
                settings = load_settings(self.config_path, self.env_path)
                
                # 检查是否启用自动同步
                if not settings.sync.auto_sync_enabled:
                    time.sleep(60)  # 每分钟检查一次设置
                    continue
                
                # 计算下次运行时间
                interval_hours = settings.sync.auto_sync_interval_hours
                if self._last_run_time is None:
                    # 首次运行，立即执行
                    self._run_auto_sync(settings)
                else:
                    # 检查是否到达运行时间
                    next_run = self._last_run_time + (interval_hours * 3600)
                    self._next_run_time = next_run
                    if time.time() >= next_run:
                        self._run_auto_sync(settings)
                    else:
                        # 等待到下次运行时间，但每分钟检查一次设置
                        time.sleep(60)
            except Exception as e:
                logger.error("Scheduler error: %s", str(e))
                time.sleep(60)
    
    def _run_auto_sync(self, settings: Settings) -> None:
        """执行自动同步"""
        logger.info("Starting auto sync")
        self._last_run_time = time.time()
        self._next_run_time = None
        
        try:
            from .auth import PixivAuthManager
            from .sync_engine import BookmarkNovelSyncService
            from .storage_files import FileStorage
            
            auth = PixivAuthManager(settings.pixiv)
            api, auth_result = auth.login()
            if auth_result.user_id is None:
                logger.error("Auto sync failed: Unable to determine user ID")
                return
            
            db = Database(settings.storage.db_path)
            db.init_schema()
            storage = FileStorage(settings)
            storage.ensure_dirs([
                settings.storage.public_dir,
                settings.storage.private_dir,
                settings.storage.db_path.parent
            ])
            
            try:
                service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)
                
                # 同步收藏
                if settings.sync.sync_bookmarks and settings.sync.auto_sync_bookmarks_enabled:
                    logger.info("Auto sync: bookmarks")
                    for restrict in settings.sync.bookmark_restricts:
                        service.sync(
                            user_id=auth_result.user_id,
                            restricts=[restrict],
                            download_assets=settings.sync.download_assets,
                            write_markdown=settings.sync.write_markdown,
                            write_raw_text=settings.sync.write_raw_text,
                        )
                        time.sleep(settings.sync.delay_seconds_between_pages)
                
                # 同步关注用户的系列和小说
                if settings.sync.sync_following_series and settings.sync.auto_sync_following_enabled:
                    logger.info("Auto sync: following series and novels")
                    service.sync_following_novels(
                        download_assets=settings.sync.download_assets,
                        write_markdown=settings.sync.write_markdown,
                        write_raw_text=settings.sync.write_raw_text,
                        novels_only=False,
                    )
                
                logger.info("Auto sync completed")
                
            finally:
                db.close()
                
        except Exception as e:
            logger.error("Auto sync failed: %s", str(e))


@dataclass(slots=True)
class SyncJobManager:
    config_path: str | None
    env_path: str | None
    _jobs: dict[str, SyncJobState] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    MAX_LOGS: int = 30

    def start_job(self) -> SyncJobState:
        with self._lock:
            running = [job for job in self._jobs.values() if job.status == "running"]
            if running:
                raise RuntimeError("已有同步任务正在运行，请稍后再试")
            job_id = str(int(time.time() * 1000))
            job = SyncJobState(job_id=job_id, status="running", message="同步任务已启动", started_at=time.time())
            self._jobs[job_id] = job
        thread = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
        thread.start()
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
                "time": time.strftime("%H:%M:%S"),
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
            
            from .jobs.quick_sync import run_bookmark_sync_with_progress
            stats = run_bookmark_sync_with_progress(settings, self, job_id)
            
            job.status = "succeeded"
            job.message = "同步完成"
            job.stats = stats
            self.add_log(job_id, "success", f"同步完成: {stats.get('novels', 0)} 本小说, {stats.get('assets_downloaded', 0)} 个资源")
        except Exception as exc:
            job.status = "failed"
            job.message = "同步失败"
            job.error = str(exc)
            self.add_log(job_id, "error", f"同步失败: {exc}")
        finally:
            job.finished_at = time.time()


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
        
        # 定时同步设置
        sync_data["auto_sync_enabled"] = bool(payload.get("auto_sync_enabled", sync_data.get("auto_sync_enabled", False)))
        sync_data["auto_sync_interval_hours"] = int(payload.get("auto_sync_interval_hours", sync_data.get("auto_sync_interval_hours", 6)))
        sync_data["auto_sync_bookmarks_enabled"] = bool(payload.get("auto_sync_bookmarks_enabled", sync_data.get("auto_sync_bookmarks_enabled", True)))
        sync_data["auto_sync_following_enabled"] = bool(payload.get("auto_sync_following_enabled", sync_data.get("auto_sync_following_enabled", True)))

        with config_path.open("w", encoding="utf-8") as file:
            yaml.safe_dump(config_data, file, allow_unicode=True, sort_keys=False)

        return _settings_to_dict(load_settings(config_path, None))


def create_app(config_path: str | None = None, env_path: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    settings_manager = SettingsManager(config_path)
    sync_job_manager = SyncJobManager(config_path=config_path, env_path=env_path)
    oauth_manager = OAuthManager()
    auto_sync_scheduler = AutoSyncScheduler(config_path=config_path, env_path=env_path)
    
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
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            payload = db.list_user_novels(user_id, page=page, page_size=page_size)
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
        try:
            job = sync_job_manager.start_job()
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "message": job.message, "job": _job_to_dict(job)})

    @app.get("/api/dashboard/sync/status")
    def dashboard_sync_status():
        job_id = request.args.get("job_id", "").strip()
        job = sync_job_manager.get_job(job_id) if job_id else sync_job_manager.latest_job()
        return jsonify({"job": _job_to_dict(job)})

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
            stats = service.sync_subscribed_series()
            logger.info("Subscribed series sync finished: %s", json.dumps(stats, ensure_ascii=False))
            return jsonify({"ok": True, "stats": stats})
        except Exception as exc:
            logger.exception("Subscribed series sync failed")
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()

    @app.get("/api/dashboard/auto-sync/status")
    def auto_sync_status():
        """获取定时同步状态"""
        return jsonify(auto_sync_scheduler.get_status())

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
        "auto_sync_enabled": settings.sync.auto_sync_enabled,
        "auto_sync_interval_hours": settings.sync.auto_sync_interval_hours,
        "auto_sync_bookmarks_enabled": settings.sync.auto_sync_bookmarks_enabled,
        "auto_sync_following_enabled": settings.sync.auto_sync_following_enabled,
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

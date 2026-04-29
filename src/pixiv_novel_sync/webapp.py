from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
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

        with config_path.open("w", encoding="utf-8") as file:
            yaml.safe_dump(config_data, file, allow_unicode=True, sort_keys=False)

        return _settings_to_dict(load_settings(config_path, None))


def create_app(config_path: str | None = None, env_path: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    settings_manager = SettingsManager(config_path)
    sync_job_manager = SyncJobManager(config_path=config_path, env_path=env_path)
    oauth_manager = OAuthManager()

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
        if category not in {"all", "series", "single", "following"}:
            category = "all"
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            payload = db.list_recent_novels(page=page, page_size=page_size, category=category)
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

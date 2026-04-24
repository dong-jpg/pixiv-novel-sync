from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from flask import Flask, jsonify, redirect, render_template, request
from urllib.parse import urlparse

from .auth import PixivAuthManager
from .jobs.quick_sync import run_bookmark_sync
from .oauth_helper import OAuthManager
from .settings import DEFAULT_CONFIG_PATH, Settings, load_settings
from .storage_db import Database
from .token_helper import TokenUiManager


@dataclass(slots=True)
class SyncJobState:
    job_id: str
    status: str = "pending"
    message: str = "等待开始"
    started_at: float | None = None
    finished_at: float | None = None
    stats: dict[str, Any] | None = None
    error: str | None = None


@dataclass(slots=True)
class SyncJobManager:
    config_path: str | None
    env_path: str | None
    _jobs: dict[str, SyncJobState] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start_job(self) -> SyncJobState:
        with self._lock:
            running_job = next((job for job in self._jobs.values() if job.status == "running"), None)
            if running_job is not None:
                raise RuntimeError("已有同步任务正在运行，请等待当前任务完成")

            job = SyncJobState(job_id=uuid.uuid4().hex, status="running", message="同步任务已启动", started_at=time.time())
            self._jobs[job.job_id] = job

        thread = threading.Thread(target=self._run_job, args=(job.job_id,), daemon=True)
        thread.start()
        return job

    def get_job(self, job_id: str) -> SyncJobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest_job(self) -> SyncJobState | None:
        with self._lock:
            if not self._jobs:
                return None
            return sorted(
                self._jobs.values(),
                key=lambda item: item.started_at or 0,
                reverse=True,
            )[0]

    def _run_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if job is None:
            return

        try:
            settings = load_settings(config_path=self.config_path, env_path=self.env_path)
            job.message = "正在执行收藏同步"
            stats = run_bookmark_sync(settings)
            job.stats = stats
            job.status = "succeeded"
            job.message = "同步完成"
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            job.message = f"同步失败：{exc}"
        finally:
            job.finished_at = time.time()


class SettingsManager:
    def __init__(self, config_path: str | None) -> None:
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH

    @property
    def config_path(self) -> Path:
        return self._config_path

    def load(self, env_path: str | None = None) -> Settings:
        return load_settings(config_path=str(self._config_path), env_path=env_path)

    def save_sync_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = _load_yaml_file(self._config_path)
        sync_raw = raw.get("sync", {})
        if not isinstance(sync_raw, dict):
            sync_raw = {}

        restricts = payload.get("bookmark_restricts", ["public", "private"])
        if not isinstance(restricts, list):
            raise ValueError("bookmark_restricts 必须是数组")
        normalized_restricts = [str(item).strip() for item in restricts if str(item).strip() in {"public", "private"}]
        if not normalized_restricts:
            raise ValueError("至少选择一个同步范围")

        sync_raw.update(
            {
                "enabled": bool(payload.get("enabled", False)),
                "initial_manual_only": bool(payload.get("initial_manual_only", True)),
                "download_assets": bool(payload.get("download_assets", True)),
                "write_markdown": bool(payload.get("write_markdown", True)),
                "write_raw_text": bool(payload.get("write_raw_text", True)),
                "bookmark_restricts": normalized_restricts,
                "max_items_per_run": _normalize_optional_int(payload.get("max_items_per_run")),
                "max_pages_per_run": _normalize_optional_int(payload.get("max_pages_per_run")),
                "delay_seconds_between_items": _normalize_float(payload.get("delay_seconds_between_items"), min_value=0.0),
                "delay_seconds_between_pages": _normalize_float(payload.get("delay_seconds_between_pages"), min_value=0.0),
            }
        )
        raw["sync"] = sync_raw
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        with self._config_path.open("w", encoding="utf-8") as file:
            yaml.safe_dump(raw, file, allow_unicode=True, sort_keys=False)
        return sync_raw


def create_app(config_path: str | None = None, env_path: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    settings_manager = SettingsManager(config_path)
    settings = settings_manager.load(env_path=env_path)
    manager = TokenUiManager(settings)
    oauth_manager = OAuthManager()
    sync_job_manager = SyncJobManager(config_path=config_path, env_path=env_path)

    @app.get("/")
    def index():
        return redirect("/dashboard")

    @app.get("/token-login")
    def token_login_page():
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

    @app.get("/dashboard/settings")
    def dashboard_settings_page():
        return render_template("dashboard_settings.html")

    @app.post("/api/token-jobs")
    def create_token_job():
        job = manager.create_job()
        return jsonify({"job_id": job.job_id, "status": job.status, "message": job.message, "mode": "fallback"})

    @app.get("/api/token-jobs/<job_id>")
    def get_token_job(job_id: str):
        job = manager.get_job(job_id)
        if job is None:
            return jsonify({"error": "job not found"}), 404
        return jsonify(
            {
                "job_id": job.job_id,
                "status": job.status,
                "message": job.message,
                "refresh_token": job.refresh_token,
                "user_id": job.user_id,
                "output": job.output[-30:],
                "mode": "fallback",
            }
        )

    @app.post("/api/save-token")
    def save_token():
        payload = request.get_json(silent=True) or {}
        refresh_token = str(payload.get("refresh_token") or "").strip()
        user_id_raw = payload.get("user_id")
        user_id = int(user_id_raw) if user_id_raw not in (None, "") else None
        if not refresh_token:
            return jsonify({"error": "missing refresh_token"}), 400
        manager.save_token_to_env(refresh_token, user_id)
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
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            follows = db.list_followed_users(limit=100)
        finally:
            db.close()
        return jsonify({"items": follows})

    @app.get("/api/dashboard/novels")
    def dashboard_novels():
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            novels = db.list_recent_novels(limit=100)
        finally:
            db.close()
        return jsonify({"items": novels})

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
    return {
        "job_id": job.job_id,
        "status": job.status,
        "message": job.message,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "stats": job.stats,
        "error": job.error,
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

from __future__ import annotations

import json
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, render_template, request

from .oauth_helper import OAuthManager
from .settings import load_settings
from .token_helper import TokenUiManager


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates")
    settings = load_settings()
    manager = TokenUiManager(settings)
    oauth_manager = OAuthManager()

    @app.get("/")
    def index():
        return redirect("/token-login")

    @app.get("/token-login")
    def token_login_page():
        return render_template("token_login.html")

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

    @app.post("/oauth/save/<task_id>")
    def oauth_save(task_id: str):
        task = oauth_manager.get_task(task_id)
        if task is None:
            return jsonify({"error": "task not found"}), 404
        if not task.refresh_token:
            return jsonify({"error": "task has no refresh_token"}), 400
        oauth_manager.save_to_env(task.refresh_token, task.user_id)
        return jsonify({"ok": True, "message": "已写入 .env"})

    return app


def _external_base_url(req) -> str:
    forwarded_proto = req.headers.get("X-Forwarded-Proto")
    forwarded_host = req.headers.get("X-Forwarded-Host")
    if forwarded_proto and forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}"

    parsed = urlparse(req.base_url)
    return f"{parsed.scheme}://{parsed.netloc}"

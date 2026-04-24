from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from .settings import load_settings
from .token_helper import TokenUiManager


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates")
    settings = load_settings()
    manager = TokenUiManager(settings)

    @app.get("/")
    def index():
        return render_template("token_login.html")

    @app.post("/api/token-jobs")
    def create_token_job():
        job = manager.create_job()
        return jsonify({"job_id": job.job_id, "status": job.status, "message": job.message})

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

    return app

from __future__ import annotations

import ipaddress
import json
import os
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests as http_requests
import yaml
from flask import Flask, Response, jsonify, redirect, render_template, request, session, send_file

from . import __version__
from .jobs.manager import JobManager
from .jobs.models import JobSpec, JobState, JobStatus
from .jobs.runner import JobRunner
from .jobs.tasks import execute_task
from .oauth_helper import OAuthManager
from .settings import Settings
from .storage_db import Database
from .storage_files import FileStorage
from .utils_env import secure_atomic_write
from .utils_naming import safe_name
from .web.managers import (
    SyncJobState,  # noqa: F401 - 经 webapp 重导出供 tests 使用
    AutoSyncScheduler,
    SyncJobManager,
    SettingsManager,
)
from .web.utils import (
    _atomic_write_yaml,
    _oauth_task_public_payload,
    _settings_to_dict,
    _shared_job_to_dict,
    _job_to_dict,
    _web_job_spec,
    _build_web_sync_job_spec,
    _safe_int,
    _restricts_to_label,
    _external_base_url,
    _check_pixiv_user_status,
    # _check_novel_status / _check_series_status: 经 webapp 再导出，jobs/services.py 依赖
    _check_novel_status,
    _check_series_status,
    _remove_archive_files,
)

logger = logging.getLogger(__name__)

# 记录服务启动时间（用于健康检查 API 计算 uptime）
_service_start_time: float = time.time()


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
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    secure_atomic_write(path, payload)
    os.environ["PIXIV_FLASK_SECRET"] = secret
    return secret



def create_app(config_path: str | None = None, env_path: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    # 修改 Jinja2 变量分隔符，避免与 Vue 3 的 {{ }} 冲突
    app.jinja_env.variable_start_string = "{["
    app.jinja_env.variable_end_string = "]}"
    app.secret_key = _load_or_create_flask_secret(env_path)
    # 加固 cookie：HttpOnly + SameSite=Lax。
    # L2: Secure 默认随部署形态推断——显式设 PIXIV_COOKIE_SECURE 优先；
    # 未显式设置但启用了 DASHBOARD_TRUST_PROXY（典型 HTTPS 反代部署）时自动开启，
    # 避免明文 HTTP 场景把会话 cookie 暴露在链路上。纯本机 HTTP 调试仍可显式关掉。
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    _cookie_secure_raw = os.getenv("PIXIV_COOKIE_SECURE", "").strip().lower()
    if _cookie_secure_raw in {"1", "true", "yes", "on"}:
        app.config["SESSION_COOKIE_SECURE"] = True
    elif _cookie_secure_raw in {"0", "false", "no", "off"}:
        app.config["SESSION_COOKIE_SECURE"] = False
    elif os.getenv("DASHBOARD_TRUST_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}:
        app.config["SESSION_COOKIE_SECURE"] = True
    settings_manager = SettingsManager(config_path)
    sync_job_manager = SyncJobManager(config_path=config_path, env_path=env_path)
    shared_job_manager = JobManager()
    app.config["job_manager"] = shared_job_manager

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

    def _api_error(error: str, status: int = 400, detail: Any | None = None):
        payload: dict[str, Any] = {"ok": False, "error": error}
        if detail is not None:
            payload["detail"] = detail
        return jsonify(payload), status

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

    app.config["job_manager"] = shared_job_manager
    app.config["run_shared_job"] = _run_shared_web_job
    app.config["submit_shared_web_job"] = _submit_shared_web_job

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
    # 可信反代层数：真实客户端 IP 位于 X-Forwarded-For 右侧倒数第 hops 个条目。
    # 默认 1（单层 nginx）。用于抵御伪造 XFF 绕过限流 / 本机判定 (M2)。
    try:
        _trusted_proxy_hops = max(1, int(os.getenv("DASHBOARD_TRUSTED_PROXY_HOPS", "1")))
    except (ValueError, TypeError):
        _trusted_proxy_hops = 1
    _LOCAL_ADDRS = {"127.0.0.1", "::1", "localhost"}

    def _is_loopback_addr(addr: str) -> bool:
        candidate = (addr or "").strip()
        if not candidate:
            return False
        if candidate in _LOCAL_ADDRS:
            return True
        try:
            return ipaddress.ip_address(candidate).is_loopback
        except ValueError:
            return False

    def _client_addr() -> str:
        """解析真实客户端地址。反代后 remote_addr 恒为 127.0.0.1，必须看 XFF。

        安全要点 (M2)：真实的反代把客户端 IP **追加**到 XFF 右侧，攻击者只能在
        左侧伪造条目。因此取右数第 _trusted_proxy_hops 个条目（跳过我方可信代理层），
        而非最左值 —— 否则轮换伪造的最左 IP 即可绕过登录限流与本机判定。
        """
        if _trust_proxy:
            xff = request.headers.get("X-Forwarded-For", "")
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if len(parts) < _trusted_proxy_hops:
                return ""
            return parts[-_trusted_proxy_hops]
        return request.remote_addr or ""

    def _behind_proxy() -> bool:
        return bool(request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP"))

    def _is_local_request() -> bool:
        return _is_loopback_addr(_client_addr())

    @app.before_request
    def _check_auth():
        path = request.path
        if path.startswith("/api/rescue/v1/"):
            return
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
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
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

    from .rescue_web import register_rescue_routes
    register_rescue_routes(app, current_settings_for_routes, _client_addr)

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
            return redirect("/token-login?error=token交换失败")

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
            if payload is not None:
                payload["rescue"] = db.evaluate_rescue_novel(novel_id)
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
        storage = FileStorage(current_settings)

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
                    cover_path = storage.get_novel_cover_path(novel_data)

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
                            cover_path = storage.get_novel_cover_path(novel_data)

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
            if payload is not None:
                payload["rescue"] = db.evaluate_rescue_series(series_id)
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
            return _api_error("已有同步任务正在运行，请稍后再试")
        current_settings = settings_manager.load(env_path=env_path)
        try:
            spec = _web_job_spec([f"user_backup:{user_id}"])
            job = _submit_shared_web_job(spec, current_settings, "user_backup", f"用户 {user_id} 备份")
        except Exception as exc:
            return _api_error(str(exc), 500)
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
            return _api_error("保存设置失败", detail=str(exc))
        return jsonify({"ok": True, "message": "设置已保存", "sync": saved})

    @app.post("/api/dashboard/sync/start")
    def dashboard_sync_start():
        current_settings = settings_manager.load(env_path=env_path)
        spec = _build_web_sync_job_spec(current_settings)

        try:
            job = _submit_shared_web_job(spec, current_settings, "manual", "全量手动同步")
        except Exception as exc:
            return _api_error(str(exc))
        return jsonify({"ok": True, "message": job.message, "job": _shared_job_to_dict(job)})

    @app.post("/api/dashboard/check-bookmarks")
    def dashboard_check_bookmarks():
        """预检查：扫描所有需要同步的内容，标记哪些已存在"""
        current_settings = settings_manager.load(env_path=env_path)
        
        try:
            spec = _web_job_spec(["sync_check"])
            job = _submit_shared_web_job(spec, current_settings, "sync_check", "预检查所有内容")
        except Exception as exc:
            return _api_error(str(exc))

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
            return _api_error("不支持的任务类型")
        
        internal_type, task_name = task_map[task_type]
        current_settings = settings_manager.load(env_path=env_path)
        try:
            spec = _web_job_spec([internal_type])
            job = _submit_shared_web_job(spec, current_settings, internal_type, task_name)
        except Exception as exc:
            return _api_error(str(exc))
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
            return _api_error(str(exc))
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
        """获取任务日志列表。

        category=sync（默认）查 task_logs（同步/偏好/推荐）；category=ai 查 ai_jobs
        （AI 创作任务，只读投影为统一结构）。两张表不合并存储，仅在此处按分类分流。
        """
        try:
            page = request.args.get("page", 1, type=int)
            page_size = request.args.get("page_size", 20, type=int)
            task_type = request.args.get("task_type")
            status = request.args.get("status") or None
            is_auto = request.args.get("is_auto")
            days = request.args.get("days", 3, type=int)
            category = request.args.get("category") or "sync"

            is_auto_sync = None
            if is_auto == "true":
                is_auto_sync = True
            elif is_auto == "false":
                is_auto_sync = False

            current_settings = settings_manager.load(env_path=env_path)
            db = Database(current_settings.storage.db_path)
            db.init_schema()
            try:
                if category == "ai":
                    result = db.get_ai_task_logs(
                        page=page,
                        page_size=page_size,
                        task_type=task_type,
                        status=status,
                        days=days,
                    )
                else:
                    result = db.get_task_logs(
                        page=page,
                        page_size=page_size,
                        task_type=task_type,
                        is_auto_sync=is_auto_sync,
                        days=days
                    )
                    for item in result.get("items", []):
                        item.setdefault("category", "sync")
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
            "version": __version__,
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


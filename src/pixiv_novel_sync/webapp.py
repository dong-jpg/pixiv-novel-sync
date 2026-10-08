from __future__ import annotations

import ipaddress
import json
import os
import re
import logging
import secrets
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests as http_requests
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, send_file, url_for

from . import __version__
from .jobs.manager import JobManager
from .jobs.models import JobSpec, JobState, JobStatus
from .jobs.runner import JobRunner
from .jobs.tasks import execute_task
from .oauth_helper import OAuthManager
from .settings import Settings
from .storage_db import Database, prepare_schema
from .storage_files import FileStorage
from .utils_env import secure_atomic_write
from .utils_naming import safe_name
from .web.auth_sessions import (
    AUTH_EXPIRY_KEY,
    AUTH_SESSION_KEY,
    BROWSER_SESSION_SECONDS,
    REMEMBER_SESSION_SECONDS,
    AbsoluteAuthSessionInterface,
    credential_version,
    hash_session_id,
    safe_next_path,
    session_record_is_current,
)
from .web.managers import AutoSyncScheduler, SettingsManager, TASK_LABELS
from .web.managers import SCHEDULER_TASK_CONFIGS, scheduler_task_log_type
from .web.utils import (
    _oauth_task_public_payload,
    _settings_to_dict,
    _shared_job_to_dict,
    _job_to_dict,
    _scheduler_job_spec,
    _web_job_spec,
    _build_web_sync_job_spec,
    _safe_int,
    _restricts_to_label,
    _external_base_url,
    _check_pixiv_user_status,
    cron_next_runs,
    cron_runs_per_day,
)

# 经 webapp 再导出：jobs/services.py 依赖 _check_novel_status/_check_series_status，
# tests/test_archive_integrity.py 依赖 _remove_archive_files
from .web.utils import _check_novel_status as _check_novel_status, _check_series_status as _check_series_status  # noqa: F401
from .web.utils import _remove_archive_files as _remove_archive_files  # noqa: F401

__all__ = ["create_app", "_check_novel_status", "_check_series_status", "_remove_archive_files"]

logger = logging.getLogger(__name__)

# Nginx 图片缓存目录（可用环境变量覆盖，便于测试）与遍历上限
_NGINX_CACHE_DIR_ENV = "PIXIV_NGINX_CACHE_DIR"
_NGINX_CACHE_DIR_DEFAULT = "/var/cache/nginx/pixiv_img"
_CACHE_SCAN_MAX_FILES = 50000


def _nginx_cache_dir() -> Path:
    return Path(os.getenv(_NGINX_CACHE_DIR_ENV) or _NGINX_CACHE_DIR_DEFAULT)


def _open_database(current_settings: Settings) -> Database:
    """创建 Database 并初始化 schema；init 失败时确保连接被关闭。

    统一辅助：修复大量路由中 db.init_schema() 位于 try/finally 之外、
    初始化抛异常时连接泄漏的问题。调用方仍负责 finally: db.close()。
    """
    db = Database(current_settings.storage.db_path)
    try:
        prepare_schema(db)
    except BaseException:
        db.close()
        raise
    return db


class _LoginFailureTracker:
    """登录失败限流：滑动窗口 + 条目上限（防止 dict 无界增长），读写加锁。"""

    def __init__(
        self,
        max_entries: int = 2048,
        window_seconds: float = 300.0,
        max_failures: int = 5,
    ) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self.max_entries = max_entries
        self.window_seconds = window_seconds
        self.max_failures = max_failures

    def is_blocked(self, client: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            failures = [ts for ts in self._failures.get(client, []) if now - ts < self.window_seconds]
            if failures:
                self._failures[client] = failures
            else:
                self._failures.pop(client, None)
            return len(failures) >= self.max_failures

    def record_failure(self, client: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            failures = [ts for ts in self._failures.get(client, []) if now - ts < self.window_seconds]
            failures.append(now)
            self._failures[client] = failures
            if len(self._failures) > self.max_entries:
                overflow = len(self._failures) - self.max_entries
                for key in sorted(self._failures, key=lambda k: self._failures[k][-1])[:overflow]:
                    self._failures.pop(key, None)

    def clear(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._failures)


_EMPTY_ARCHIVE_STATS = {"dirs_removed": 0, "files_removed": 0, "missing": 0, "skipped": 0}


class _ArchiveTrash:
    """归档删除的"回收站"暂存：先把文件搬到同卷 .trash 目录，DB 提交成功后再清空。

    修复原先"先删文件再写库"的顺序问题：DB 删除失败时可以把文件原样移回。
    路径计算逻辑与 web.utils._remove_archive_files 保持一致。
    """

    def __init__(self, settings: Settings, archive_refs: list[dict[str, Any]]) -> None:
        self._storage = FileStorage(settings)
        self._trash_root = (
            settings.storage.public_dir.parent / ".trash" / uuid.uuid4().hex
        )
        self._moves: list[tuple[Path, Path]] = []
        self.stats = dict(_EMPTY_ARCHIVE_STATS)
        self._novel_dirs, self._asset_paths = self._storage._collect_archive_paths(archive_refs)

    def _move_to_trash(self, source: Path) -> None:
        self._trash_root.mkdir(parents=True, exist_ok=True)
        # 在搬走唯一副本前标记；崩溃或恢复失败时禁止按年龄清除。
        (self._trash_root / ".recovery-required").touch(exist_ok=True)
        dest = self._trash_root / str(len(self._moves))
        # move 跨卷时会先复制再删源；删除中途失败也必须追踪完整目标副本。
        self._moves.append((source, dest))
        shutil.move(str(source), str(dest))

    def stage(self) -> None:
        """把所有待删文件/目录搬进 trash（尚未真正删除）。"""
        moved_dirs: set[Path] = set()
        seen_dirs: set[Path] = set()
        for novel_dir in self._novel_dirs:
            try:
                dir_key = (
                    novel_dir.resolve()
                    if novel_dir.exists()
                    else novel_dir.parent.resolve() / novel_dir.name
                )
            except OSError:
                self.stats["skipped"] += 1
                continue
            if dir_key in seen_dirs:
                continue
            seen_dirs.add(dir_key)
            if not self._storage._is_inside_storage(novel_dir):
                self.stats["skipped"] += 1
                logger.warning("Skip deleting archive outside storage roots: %s", novel_dir)
                continue
            if not novel_dir.exists():
                self.stats["missing"] += 1
                continue
            resolved_dir = novel_dir.resolve()
            was_dir = resolved_dir.is_dir()
            self._move_to_trash(resolved_dir)
            if was_dir:
                moved_dirs.add(resolved_dir)
                self.stats["dirs_removed"] += 1
            else:
                self.stats["files_removed"] += 1

        for asset_path in self._asset_paths:
            try:
                asset_key = (
                    asset_path.resolve()
                    if asset_path.exists()
                    else asset_path.parent.resolve() / asset_path.name
                )
            except OSError:
                self.stats["skipped"] += 1
                continue
            if any(asset_key.is_relative_to(directory) for directory in moved_dirs):
                continue
            if not self._storage._is_inside_storage(asset_path):
                self.stats["skipped"] += 1
                logger.warning("Skip deleting asset outside storage roots: %s", asset_path)
                continue
            if not asset_path.exists():
                self.stats["missing"] += 1
                continue
            resolved_asset = asset_path.resolve()
            if resolved_asset.is_file():
                self._move_to_trash(resolved_asset)
                self.stats["files_removed"] += 1

    def commit(self) -> dict[str, int]:
        """DB 已提交：真正删除 trash 中的文件，返回统计。"""
        shutil.rmtree(self._trash_root, ignore_errors=True)
        return self.stats

    def rollback(self) -> None:
        """DB 失败：把文件移回原位。"""
        pending: list[tuple[Path, Path]] = []
        for source, dest in reversed(self._moves):
            try:
                if not dest.exists() and not dest.is_symlink():
                    if source.exists() or source.is_symlink():
                        continue  # 搬移尚未产生目标，源副本仍完整保留。
                    raise FileNotFoundError(f"归档源与暂存均缺失: {source}")
                if source.exists() or source.is_symlink():
                    raise FileExistsError(f"恢复目标已存在，保留双方副本: {source}")
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dest), str(source))
            except Exception as exc:
                pending.append((source, dest))
                logger.error("归档回收站回滚失败，副本保留于 %s -> %s: %s", dest, source, exc)
        self._moves = list(reversed(pending))
        if not pending:
            shutil.rmtree(self._trash_root, ignore_errors=True)


def _sweep_stale_trash(trash_root: Path, *, max_age_seconds: float = 86400) -> int:
    """清理过期旧暂存；带恢复标记的副本保留供人工恢复。"""
    if not trash_root.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for child in trash_root.iterdir():
        try:
            if child.is_symlink() or (child / ".recovery-required").exists():
                continue
            if child.stat().st_mtime >= cutoff:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            logger.warning("清理回收站失败 %s: %s", child, exc)
    return removed


def _remove_archive_files_atomic(
    settings: Settings,
    archive_refs: list[dict[str, Any]],
    db_delete: Callable[[], Any],
) -> dict[str, int]:
    """安全顺序删除：文件先入 trash → 执行 DB 删除并提交 → 清空 trash。

    DB 删除抛异常时文件被移回原位，异常继续向上抛。
    """
    trash = _ArchiveTrash(settings, archive_refs)
    try:
        trash.stage()
        db_delete()
    except BaseException:
        trash.rollback()
        raise
    return trash.commit()


@dataclass(frozen=True, slots=True)
class _AutoSyncSchedulerOwner:
    scheduler: AutoSyncScheduler
    job_manager: JobManager

    @property
    def sync_job_manager(self) -> JobManager:
        """保留：仅测试/兼容用途。兼容旧测试/扩展读取；生产状态统一由 JobManager 持有。"""
        return self.job_manager


_auto_sync_scheduler_registry: dict[str, _AutoSyncSchedulerOwner] = {}
_auto_sync_scheduler_registry_lock = threading.Lock()


def _scheduler_registry_key(db_path: Path) -> str:
    return os.path.normcase(str(Path(db_path).expanduser().resolve(strict=False)))


def _claim_scheduler_owner(key: str, scheduler: AutoSyncScheduler) -> bool:
    with _auto_sync_scheduler_registry_lock:
        current = _auto_sync_scheduler_registry.get(key)
        if current is not None and current.scheduler is not scheduler:
            return False
        if current is None:
            _auto_sync_scheduler_registry[key] = _AutoSyncSchedulerOwner(
                scheduler=scheduler,
                job_manager=scheduler.shared_job_manager,
            )
        return True


def _release_scheduler_owner(key: str, scheduler: AutoSyncScheduler) -> None:
    with _auto_sync_scheduler_registry_lock:
        current = _auto_sync_scheduler_registry.get(key)
        if current is not None and current.scheduler is scheduler:
            _auto_sync_scheduler_registry.pop(key, None)


# task_logs 里表示「跑了但没跑完」的终态。前端 dashboard_logs.html 已把它渲染成
# 黄色「部分完成」，这里复用同一取值。
_TASK_LOG_STATUS_PARTIAL = "partial"

# 单条任务日志的 stats 体积上限。生产实测 recommendation_run 每轮往 stats 里写
# 351 KB（整个候选列表连正文摘要），100 行日志合计 6.42 MB，均值 67 KB/行——而这些
# 数据本来就在推荐表里，日志里再抄一份既撑大库也让详情弹窗要加载几百 KB JSON。
_TASK_LOG_STATS_MAX_BYTES = 8192
# 单个字段的保留上限：小于这个尺寸的一律留下（status_counts、source_scope 这类小 dict
# 是诊断的主要依据，不能被一刀切掉）。
_TASK_LOG_STATS_FIELD_MAX_BYTES = 512


def _prune_stats_for_log(stats: dict[str, Any] | None) -> dict[str, Any] | None:
    """写进 task_logs 前给 stats 瘦身：只留摘要，不留业务负载。

    按体积裁剪而不是按字段名黑名单：黑名单每加一个新任务就得改一次，漏了不会有人
    发现；按体积裁剪对以后新增的胖字段自动生效。被裁掉的字段会在 ``_pruned`` 里留下
    「类型 + 元素数 + 字节数」，所以日志里看得出「这里原本有东西」而不是凭空消失。

    只裁日志，不裁 ``JobState.stats``：运行中的进度接口读的是内存里那份完整对象。
    """
    if not isinstance(stats, dict):
        return stats
    try:
        encoded = json.dumps(stats, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return stats
    if len(encoded.encode("utf-8")) <= _TASK_LOG_STATS_MAX_BYTES:
        return stats

    pruned: dict[str, Any] = {}
    dropped: dict[str, str] = {}
    for key, value in stats.items():
        if value is None or isinstance(value, (int, float, bool)):
            pruned[key] = value
            continue
        try:
            size = len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            size = _TASK_LOG_STATS_FIELD_MAX_BYTES + 1
        if size <= _TASK_LOG_STATS_FIELD_MAX_BYTES:
            pruned[key] = value
            continue
        if isinstance(value, list):
            dropped[key] = f"list[{len(value)}] {size}B"
        elif isinstance(value, dict):
            dropped[key] = f"dict[{len(value)}] {size}B"
        else:
            dropped[key] = f"{type(value).__name__} {size}B"
    if dropped:
        pruned["_pruned"] = dropped
    return pruned


def _task_log_status_for_stats(stats: dict[str, Any] | None) -> str:
    """按任务 stats 判定写进 task_logs 的终态。

    生产事故：状态检查被限流熔断，只检查了 30/800 篇就中止，任务日志里却仍是绿色
    「成功」，运维根本看不出这轮几乎什么都没查。凡是 stats 里带 ``aborted_reason``
    （熔断中止）或 ``truncated``（分页触顶）的，一律记 ``partial``，让它在任务日志里
    显示成黄色「部分完成」。

    ``incomplete`` 单独出现时也算 ``partial``——它是兜底信号，宁可误报黄色也不能漏。
    唯一的例外是同时带 ``rotation_pending``：那表示「跑不完是设计如此」（关注作者按
    ``user_last_synced`` 轮转，256 人 ÷ 每轮 5 人，全圈约 12.8 天；每作者配额同理），
    这种轮次记 ``succeeded``。否则最大的那个任务永远是黄色，真正的截断和熔断反而被
    淹没在里面——黄色必须稀有才有意义。

    注意方向不能反：新增的 ``stats["incomplete"] = True`` 站点默认仍然是 ``partial``，
    只有显式标了 ``rotation_pending`` 才降级。``tests/test_sync_engine_incremental.py``
    会 grep 源码，确保每个 ``incomplete`` 站点都伴随 ``truncated`` / ``aborted_reason``
    / ``rotation_pending`` 之一，不让"沉默的绿色"重新长出来。

    ``remaining`` / ``users_remaining`` 等是常规轮转字段（分批检查本来就有剩余），
    不能作为判定依据，否则每一轮都会变成 partial。
    """
    if not isinstance(stats, dict):
        return JobStatus.SUCCEEDED.value
    if stats.get("aborted_reason") or stats.get("truncated"):
        return _TASK_LOG_STATUS_PARTIAL
    if stats.get("incomplete") and not stats.get("rotation_pending"):
        return _TASK_LOG_STATUS_PARTIAL
    return JobStatus.SUCCEEDED.value


def _refresh_rescue_chapters(db: Database, chapter_ids: list[int] | tuple[int, ...]) -> None:
    """刷新系列解绑影响的每个章节。

    ``delete_series`` 会保留章节记录并返回章节 ID。Web 层必须消费这些 ID，
    让每个章节立即重新分类（包括历史父系列关系）。重复 ID 会被忽略，首次
    出现的顺序保持不变。
    """
    for novel_id in dict.fromkeys(int(value) for value in chapter_ids):
        db.refresh_rescue_item("novel", novel_id)


def _get_or_create_scheduler_owner(
    key: str,
    *,
    config_path: str | None,
    env_path: str | None,
    job_manager: JobManager,
    submit_task: Any = None,
    run_task: Any = None,
    get_task: Any = None,
    cancel_task: Any = None,
) -> tuple[AutoSyncScheduler, bool]:
    with _auto_sync_scheduler_registry_lock:
        existing = _auto_sync_scheduler_registry.get(key)
        if existing is not None:
            return existing.scheduler, False
        scheduler = AutoSyncScheduler(
            config_path=config_path,
            env_path=env_path,
            shared_job_manager=job_manager,
            submit_task=submit_task,
            run_task=run_task,
            get_task=get_task,
            cancel_task=cancel_task,
        )
        scheduler._lifecycle_claim = lambda owner: _claim_scheduler_owner(key, owner)
        scheduler._lifecycle_release = lambda owner: _release_scheduler_owner(key, owner)
        _auto_sync_scheduler_registry[key] = _AutoSyncSchedulerOwner(
            scheduler=scheduler,
            job_manager=job_manager,
        )
        return scheduler, True


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
    # 空的 PIXIV_FLASK_SECRET= 要原地换掉。再追加一行会让 .env 出现两个同名键。
    lines = [line for line in lines if not line.startswith("PIXIV_FLASK_SECRET=")]
    lines.append(f"PIXIV_FLASK_SECRET={secret}")
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    secure_atomic_write(path, payload)
    os.environ["PIXIV_FLASK_SECRET"] = secret
    return secret



def create_app(
    config_path: str | None = None,
    env_path: str | None = None,
    *,
    start_scheduler: bool | None = None,
) -> Flask:
    app = Flask(__name__, template_folder="templates")
    # 修改 Jinja2 变量分隔符，避免与 Vue 3 的 {{ }} 冲突
    app.jinja_env.variable_start_string = "{["
    app.jinja_env.variable_end_string = "]}"
    app.secret_key = _load_or_create_flask_secret(env_path)
    # 加固 cookie：HttpOnly + SameSite=Lax。
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_REFRESH_EACH_REQUEST"] = False
    app.permanent_session_lifetime = timedelta(seconds=REMEMBER_SESSION_SECONDS)
    app.session_interface = AbsoluteAuthSessionInterface()
    settings_manager = SettingsManager(config_path)
    # Secure 必须在 load() 之后看环境变量：DASHBOARD_TRUST_PROXY / PIXIV_COOKIE_SECURE
    # 通常只写在 .env 里，create_app 进来时进程环境还没有它们。
    try:
        settings_manager.load(env_path=env_path)
    except Exception as exc:
        logger.warning("启动加载设置失败，cookie Secure 仅依据已有环境变量：%s", exc)
    _cookie_secure_raw = os.getenv("PIXIV_COOKIE_SECURE", "").strip().lower()
    if _cookie_secure_raw in {"1", "true", "yes", "on"}:
        app.config["SESSION_COOKIE_SECURE"] = True
    elif _cookie_secure_raw in {"0", "false", "no", "off"}:
        app.config["SESSION_COOKIE_SECURE"] = False
    elif os.getenv("DASHBOARD_TRUST_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}:
        app.config["SESSION_COOKIE_SECURE"] = True
    shared_job_manager = JobManager()
    app.config["job_manager"] = shared_job_manager

    # 应用启动时一次性清理进程重启遗留的 running 任务日志。
    # 注意：不能放在 init_schema 常规路径（并发 Web 请求会误杀运行中任务）。
    try:
        _startup_db = _open_database(settings_manager.load(env_path=env_path))
        try:
            stale_count = _startup_db.fail_stale_task_logs()
            if stale_count:
                logger.info("启动时将 %d 条遗留 running 任务日志标记为 failed", stale_count)
        finally:
            _startup_db.close()
    except Exception as exc:
        logger.warning("启动清理遗留任务日志失败：%s", exc)

    try:
        startup_settings = settings_manager.load(env_path=env_path)
        swept = _sweep_stale_trash(startup_settings.storage.public_dir.parent / ".trash")
        if swept:
            logger.info("启动时清理了 %d 个超过 24 小时的回收站目录", swept)
    except Exception as exc:
        logger.warning("启动清理回收站失败：%s", exc)

    def run_web_task(task_type: str, context: dict[str, Any]) -> dict[str, Any] | None:
        current_settings = settings_manager.load(env_path=env_path)
        return execute_task(task_type, current_settings, context)

    shared_job_runner: JobRunner | None = None

    def _has_active_shared_jobs() -> bool:
        with shared_job_manager._lock:
            active_statuses = {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}
            return any(job.status in active_statuses for job in shared_job_manager._jobs.values())

    def _has_any_running_web_job() -> bool:
        return _has_active_shared_jobs()

    def _api_error(error: str, status: int = 400, detail: Any | None = None):
        payload: dict[str, Any] = {"ok": False, "error": error}
        if detail is not None:
            payload["detail"] = detail
        return jsonify(payload), status

    def _submit_failure(exc: Exception, status: int = 400):
        if "已有同步任务正在运行" in str(exc):
            return _api_error("已有同步任务正在运行，请稍后再试", 409)
        return _api_error(str(exc), status)

    def _shared_job_logs_for_db(job: JobState) -> list[dict[str, Any]]:
        return [{"time": entry.time, "level": entry.level, "message": entry.message} for entry in job.logs]

    def _run_shared_web_job(job_id: str) -> None:
        if shared_job_runner is None:
            raise RuntimeError("shared job runner is not initialized")
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
            prepare_schema(db)
            logs = _shared_job_logs_for_db(job)
            if job.status == JobStatus.SUCCEEDED:
                # 熔断中止/本轮没跑完的任务不能记成 succeeded，否则风控事故在日志里
                # 显示成绿色「成功」被彻底掩盖
                db.update_task_log(
                    log_id,
                    _task_log_status_for_stats(job.stats),
                    stats=_prune_stats_for_log(job.stats),
                    logs=logs,
                )
            elif job.status == JobStatus.FAILED:
                db.update_task_log(log_id, JobStatus.FAILED.value, error_message=job.error, logs=logs)
            elif job.status == JobStatus.CANCELLED:
                db.update_task_log(log_id, JobStatus.CANCELLED.value, logs=logs)
        except Exception as exc:
            logger.error("更新共享任务日志失败：%s", exc)
        finally:
            db.close()

    def _submit_shared_job(
        spec: JobSpec,
        current_settings: Settings,
        task_type: str,
        task_name: str,
        *,
        is_auto_sync: bool = False,
        progress: dict[str, Any] | None = None,
        run_async: bool = True,
    ) -> JobState:
        def _mark_log_failed(log_id: int, message: str) -> None:
            db = _open_database(current_settings)
            try:
                db.update_task_log(log_id, JobStatus.FAILED.value, error_message=message)
            finally:
                db.close()

        # 已有任务时直接拒绝，不打开数据库。写日志仍放在锁外，
        # 避免迁移或磁盘 IO 挡住 get_job / is_cancel_requested。
        with shared_job_manager._lock:
            if _has_any_running_web_job():
                raise RuntimeError("已有同步任务正在运行，请稍后再试")

        db = _open_database(current_settings)
        try:
            log_id = db.create_task_log(
                task_type=task_type,
                task_name=task_name,
                is_auto_sync=is_auto_sync,
            )
        finally:
            db.close()

        # 写日志的窗口里可能有别的请求入队，提交前再检查一次。
        try:
            with shared_job_manager._lock:
                if _has_any_running_web_job():
                    raise RuntimeError("已有同步任务正在运行，请稍后再试")
                job = shared_job_manager.submit(spec)
        except Exception as exc:
            message = str(exc) if "已有同步任务正在运行" in str(exc) else "job submit failed"
            try:
                _mark_log_failed(log_id, message)
            except Exception:
                logger.warning("回写失败的任务日志也失败了", exc_info=True)
            raise

        job.progress["log_id"] = log_id
        # 回填 job_id：日志页按 job_id 关联内存任务与 task_logs 行。
        # RecordingDatabase 等测试替身没有 conn，用 try 包一层，让替身也能过。
        # 回填失败必须把已入队的 job 标成 failed，否则线程不会启动，队列永久占着。
        try:
            db = _open_database(current_settings)
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE task_logs SET job_id = ? WHERE id = ?",
                        (job.job_id, log_id),
                    )
            except AttributeError:
                pass
            finally:
                db.close()
        except Exception as exc:
            shared_job_manager.mark_failed(job.job_id, str(exc))
            try:
                _mark_log_failed(log_id, str(exc))
            except Exception:
                logger.warning("回写失败的任务日志也失败了", exc_info=True)
            raise
        if progress:
            shared_job_manager.update_progress(job.job_id, **progress)
        if run_async:
            thread = threading.Thread(target=_run_shared_web_job, args=(job.job_id,), daemon=True)
            thread.start()
        return job

    def _submit_scheduler_task(task_settings: Settings, task_name: str) -> JobState | None:
        params = None
        if task_name == "preference_analyze":
            params = {
                "scope": {
                    "batch_size": int(
                        getattr(task_settings.sync, "preference_analyze_batch_size", 200)
                        or 200
                    ),
                    "max_batches": 1,
                }
            }
        spec = _scheduler_job_spec(task_name, params)
        normalized_name = spec.task_types[0] if spec.task_types else task_name
        # task_name 是调度器内部键（novel_status / bookmarks / ...），直接写进
        # task_logs 会让任务日志页显示英文键名。这里翻成中文标签，与手动任务一致。
        display_name = TASK_LABELS.get(task_name) or TASK_LABELS.get(normalized_name) or task_name
        return _submit_shared_job(
            spec,
            task_settings,
            normalized_name,
            display_name,
            is_auto_sync=True,
            run_async=False,
        )

    oauth_manager = OAuthManager(env_path=env_path)

    # 启动定时同步调度器
    # 在 Werkzeug debug reloader 下，主进程会先启动一次再 fork 子进程；只在子进程启动调度器，避免双开
    _is_werkzeug_reload = os.getenv("WERKZEUG_RUN_MAIN") == "true"
    _flask_debug_enabled = os.getenv("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    _is_debug = _flask_debug_enabled or bool(os.getenv("WERKZEUG_SERVER_FD"))
    auto_start_scheduler = not _is_debug or _is_werkzeug_reload
    should_start_scheduler = (
        auto_start_scheduler if start_scheduler is None else bool(start_scheduler)
    )
    start_auto_sync_scheduler = False
    if should_start_scheduler:
        registry_settings = settings_manager.load(env_path=env_path)
        registry_key = _scheduler_registry_key(registry_settings.storage.db_path)
        auto_sync_scheduler, scheduler_created = _get_or_create_scheduler_owner(
            registry_key,
            config_path=config_path,
            env_path=env_path,
            job_manager=shared_job_manager,
            submit_task=_submit_scheduler_task,
            run_task=_run_shared_web_job,
            get_task=shared_job_manager.get_job,
            cancel_task=shared_job_manager.request_cancel,
        )
        if auto_sync_scheduler.shared_job_manager is not None:
            shared_job_manager = auto_sync_scheduler.shared_job_manager
        if scheduler_created or not auto_sync_scheduler.is_running():
            start_auto_sync_scheduler = True
    else:
        auto_sync_scheduler = AutoSyncScheduler(
            config_path=config_path,
            env_path=env_path,
            shared_job_manager=shared_job_manager,
            submit_task=_submit_scheduler_task,
            run_task=_run_shared_web_job,
            get_task=shared_job_manager.get_job,
            cancel_task=shared_job_manager.request_cancel,
        )

    shared_job_runner = JobRunner(shared_job_manager, run_web_task)
    app.config["job_manager"] = shared_job_manager
    app.config["run_shared_job"] = _run_shared_web_job
    app.config["submit_shared_web_job"] = _submit_shared_job
    app.config["auto_sync_scheduler"] = auto_sync_scheduler

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
    # /nginx-health 不在豁免名单里：它曾是 nginx 探活专用路径，实际从未注册路由，
    # 留在豁免表只会让人误以为存在一条无认证入口。
    _AUTH_EXEMPT_PATHS = {
        "/api/auth/login",
        "/api/csrf-token",
        "/api/health",
    }

    _CSRF_EXEMPT_PATHS = {
        "/api/auth/login",
        "/api/health",
    }
    _MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
    _login_failures = _LoginFailureTracker()
    app.config["login_failure_tracker"] = _login_failures

    def _authenticated_session_fresh(current_settings: Settings) -> bool:
        token_hash = hash_session_id(session.get(AUTH_SESSION_KEY))
        if token_hash is None:
            if any(key in session for key in (AUTH_SESSION_KEY, "authenticated", "authenticated_at")):
                session.clear()
            return False
        # Do not clear/modify the cookie until verification has completed. A
        # transient connection/read error must fail closed but remain retryable.
        db = _open_database(current_settings)
        try:
            record = db.get_web_auth_session(token_hash)
        finally:
            db.close()
        version = credential_version(app.secret_key, str(current_settings.dashboard_token))
        if not session_record_is_current(record, version, time.time()):
            session.clear()
            return False
        return True

    def _auth_storage_unavailable(exc: Exception):
        logger.warning("Dashboard authentication storage unavailable (%s)", type(exc).__name__)
        return jsonify({"error": "authentication temporarily unavailable; please retry"}), 503

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

    _ALLOWED_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

    def _request_hostname() -> str:
        raw = (request.host or "").strip().lower().rstrip(".")
        if raw.startswith("["):
            end = raw.find("]")
            return raw[1:end] if end > 1 else raw
        if raw.count(":") == 1:
            return raw.split(":", 1)[0]
        return raw

    def _csrf_blocked(path: str):
        if request.method not in _MUTATING_METHODS or path in _CSRF_EXEMPT_PATHS:
            return None
        submitted = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
        expected = _get_csrf_token()
        try:
            matched = secrets.compare_digest(str(submitted).encode("utf-8"), expected.encode("utf-8"))
        except Exception:
            matched = False
        if not submitted or not matched:
            return _csrf_failed()
        return None

    @app.before_request
    def _check_auth():
        path = request.path
        if path.startswith("/api/rescue/v1/"):
            return
        current_settings = settings_manager.load(env_path=env_path)
        token = current_settings.dashboard_token
        if not token:
            # 安全加固：未配置 token 时仅允许真正的本机访问。
            # 若检测到代理头但未显式信任代理，说明很可能暴露在反代后，
            # 此时 remote_addr=127.0.0.1 不可信，一律拒绝，避免私密收藏泄漏。
            # 本机模式同样校验 Host 与 CSRF：DNS 重绑定和跨站表单都不能直接改库。
            if _behind_proxy() and not _trust_proxy:
                return jsonify({"error": "dashboard token required when behind a proxy"}), 403
            if not _is_local_request():
                return jsonify({"error": "dashboard token required for non-local access"}), 403
            if _request_hostname() not in _ALLOWED_LOCAL_HOSTS:
                return jsonify({"error": "host not allowed"}), 403
            blocked = _csrf_blocked(path)
            if blocked is not None:
                return blocked
            return
        if path in _AUTH_EXEMPT_PATHS:
            return
        if path.startswith("/static/"):
            return
        try:
            authenticated = _authenticated_session_fresh(current_settings)
        except Exception as exc:
            return _auth_storage_unavailable(exc)
        if authenticated:
            blocked = _csrf_blocked(path)
            if blocked is not None:
                return blocked
            return
        # API 请求返回 401，页面请求重定向到登录
        if path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        next_path = request.full_path if request.query_string else request.path
        return redirect(url_for("auth_login", next=safe_next_path(next_path)))

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

    if start_auto_sync_scheduler:
        auto_sync_scheduler.start()

    @app.route("/api/auth/login", methods=["GET", "POST"])
    def auth_login():
        current_settings = settings_manager.load(env_path=env_path)
        token = current_settings.dashboard_token
        if not token:
            return redirect("/")
        next_path = safe_next_path(request.form.get("next", request.args.get("next")))
        remember_device = request.method == "POST" and request.form.get("remember_device") == "1"

        def login_page(error: str | None = None, status: int = 200):
            return Response(
                render_template(
                    "login.html", error=error, next_path=next_path, remember_device=remember_device,
                ),
                status=status,
                content_type="text/html; charset=utf-8",
            )

        if request.method == "GET":
            return login_page()
        import hmac as _hmac
        now = time.time()
        client = _client_addr()
        if _login_failures.is_blocked(client, now):
            response = login_page("尝试次数过多，请稍后再试。", 429)
            response.headers["Retry-After"] = str(int(_login_failures.window_seconds))
            return response
        input_token = request.form.get("token", "")
        # compare_digest 两边编码成 bytes：DASHBOARD_TOKEN 含中文等非 ASCII 时，
        # str 版本会直接抛 TypeError 变成 500，而不是按「密码错」计一次 401。
        if _hmac.compare_digest(input_token.encode("utf-8"), str(token).encode("utf-8")):
            session_id = secrets.token_urlsafe(32)
            lifetime = REMEMBER_SESSION_SECONDS if remember_device else BROWSER_SESSION_SECONDS
            expires_at = now + lifetime
            try:
                db = _open_database(current_settings)
                try:
                    db.replace_web_auth_session(
                        token_hash=hash_session_id(session_id),
                        credential_version=credential_version(app.secret_key, str(token)),
                        created_at=now,
                        expires_at=expires_at,
                        persistent=remember_device,
                        previous_token_hash=hash_session_id(session.get(AUTH_SESSION_KEY)),
                    )
                finally:
                    db.close()
            except Exception as exc:
                logger.warning("Dashboard login storage unavailable (%s)", type(exc).__name__)
                return login_page("登录服务暂时不可用，请稍后重试。", 503)
            # Only replace the browser cookie after the insert AND revoke commit.
            _login_failures.clear(client)
            session.clear()
            session.permanent = remember_device
            session[AUTH_SESSION_KEY] = session_id
            session[AUTH_EXPIRY_KEY] = expires_at
            _get_csrf_token()
            return redirect(next_path)
        _login_failures.record_failure(client, now)
        return login_page("密码错误，请重试。", 401)

    @app.get("/api/csrf-token")
    def csrf_token():
        return jsonify({"csrf_token": _get_csrf_token()})

    @app.route("/api/auth/logout", methods=["POST"])
    def auth_logout():
        token_hash = hash_session_id(session.get(AUTH_SESSION_KEY))
        if token_hash is not None:
            try:
                db = _open_database(settings_manager.load(env_path=env_path))
                try:
                    db.revoke_web_auth_session(token_hash)
                finally:
                    db.close()
            except Exception as exc:
                return _auth_storage_unavailable(exc)
        session.clear()
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
            # 不跟随 3xx：跟随重定向会让本端点变成开放代理（跳到非 pximg 域）。
            # 其余非 200 也不把上游错误体当图片回给前端。
            if resp.status_code != 200:
                return Response(f"Upstream returned {resp.status_code}", status=502)
            out = Response(resp.content, content_type=resp.headers.get("Content-Type", "image/jpeg"))
            for hop in ("Content-Length", "Cache-Control"):
                if resp.headers.get(hop):
                    out.headers[hop] = resp.headers[hop]
            return out
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

    # 设置页已拆成四个一级页面：同步 / 模型 / Agent / 系统。
    # 只有 sync 与 system 两页的表单落到 config.yaml（见 SETTINGS_SECTIONS），
    # models / agents 两页的配置存在数据库里，走 ai_web.py 自己的端点。
    _SETTINGS_PAGES = ("sync", "models", "agents", "system")

    @app.get("/dashboard/settings")
    def dashboard_settings_page():
        # 旧书签与旧的 #hash 链接一律落到同步页
        return redirect("/dashboard/settings/sync")

    @app.get("/dashboard/settings/<section>")
    def dashboard_settings_section_page(section: str):
        if section not in _SETTINGS_PAGES:
            abort(404)
        return render_template(f"dashboard_settings_{section}.html")

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
        if user_id_raw in (None, ""):
            user_id = None
        else:
            # _safe_int 把非数字收成 0，和负数一样拒绝，避免 int() 抛出变成 500。
            user_id = _safe_int(user_id_raw, 0)
            if user_id <= 0:
                return jsonify({"error": "invalid user_id"}), 400
        if not refresh_token:
            return jsonify({"error": "missing refresh_token"}), 400
        # refresh_token 只允许 Pixiv 颁发的字符集：带换行的值写进 .env 会拆行，
        # 把下一行变成新的 KEY=VALUE，造成配置注入。
        if not re.fullmatch(r"[A-Za-z0-9_\-]{10,}", refresh_token):
            return jsonify({"error": "invalid refresh_token format"}), 400
        if user_id is not None and user_id <= 0:
            return jsonify({"error": "invalid user_id"}), 400
        manager.save_to_env(refresh_token, user_id)
        return jsonify({"ok": True, "message": "已写入 .env"})

    @app.get("/oauth/task/<task_id>")
    def oauth_task(task_id: str):
        task = oauth_manager.get_task(task_id)
        if task is None:
            return jsonify({"error": "task not found"}), 404
        return jsonify(_oauth_task_public_payload(task, mode="oauth"))

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
        db = _open_database(current_settings)
        try:
            stats = json.loads(db.export_stats())
            current_user = db.get_user_summary(current_settings.pixiv.user_id)
        finally:
            db.close()
        latest_job = shared_job_manager.latest_job()
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
        db = _open_database(current_settings)
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
        db = _open_database(current_settings)
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
        db = _open_database(current_settings)
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
        db = _open_database(current_settings)
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
        db = _open_database(current_settings)
        try:
            db.upsert_reading_progress(novel_id, progress, status)
        finally:
            db.close()
        return jsonify({"success": True})

    @app.delete("/api/dashboard/novels/<int:novel_id>/progress")
    def delete_novel_progress(novel_id: int):
        current_settings = settings_manager.load(env_path=env_path)
        db = _open_database(current_settings)
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
        db = _open_database(current_settings)
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
        db = _open_database(current_settings)
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
        search = str(request.args.get("search", "") or "").strip()
        db = _open_database(current_settings)
        try:
            payload = db.list_users(page=page, page_size=page_size, status=status, search=search)
        finally:
            db.close()
        return jsonify(payload)

    @app.get("/api/dashboard/users/<int:user_id>")
    def dashboard_user_detail(user_id: int):
        current_settings = settings_manager.load(env_path=env_path)
        db = _open_database(current_settings)
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
        db = _open_database(current_settings)
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
        db = None
        try:
            auth = PixivAuthManager(current_settings.pixiv)
            api, _ = auth.login()
            db = _open_database(current_settings)
            status = _check_pixiv_user_status(api, user_id)
            db.upsert_user_status(user_id, status)
            return jsonify({"ok": True, "status": status})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            if db is not None:
                db.close()

    @app.post("/api/dashboard/users/<int:user_id>/sync")
    def sync_user_novels(user_id: int):
        """触发某用户全部小说的后台备份任务，避免阻塞 HTTP 请求。"""
        if _has_active_shared_jobs():
            return _api_error("已有同步任务正在运行，请稍后再试", 409)
        current_settings = settings_manager.load(env_path=env_path)
        try:
            spec = _web_job_spec([f"user_backup:{user_id}"])
            job = _submit_shared_job(spec, current_settings, "user_backup", f"用户 {user_id} 备份")
        except Exception as exc:
            return _submit_failure(exc, 500)
        return jsonify({"ok": True, "job_id": job.job_id, "job": _shared_job_to_dict(job)})

    @app.get("/api/dashboard/settings")
    def dashboard_settings():
        current_settings = settings_manager.load(env_path=env_path)
        return jsonify(_settings_to_dict(current_settings))

    def _save_settings(payload: dict[str, Any], section: str | None = None) -> dict[str, Any]:
        before = settings_manager.load(env_path=env_path)
        saved = settings_manager.save_sync_settings(payload, section=section)
        after = settings_manager.load(env_path=env_path)
        auto_sync_scheduler.refresh_changed_schedules(before, after)
        return saved

    @app.post("/api/dashboard/settings")
    def dashboard_settings_save():
        payload = request.get_json(silent=True) or {}
        try:
            saved = _save_settings(payload)
        except Exception as exc:
            return _api_error("保存设置失败", detail=str(exc))
        return jsonify({"ok": True, "message": "设置已保存", "sync": saved})

    @app.put("/api/dashboard/settings/<section>")
    def dashboard_settings_save_section(section: str):
        """按分区保存：payload 里只有本区字段会被采纳，别区字段沿用磁盘上的旧值。

        拆页后每页表单只含自己那一区，走全量端点会把没加载的字段写成默认值。
        未知分区由 save_sync_settings 抛 ValueError，这里统一按 400 返回。
        """
        payload = request.get_json(silent=True) or {}
        try:
            saved = _save_settings(payload, section=section)
        except Exception as exc:
            return _api_error("保存设置失败", detail=str(exc))
        return jsonify({"ok": True, "message": "设置已保存", "sync": saved})

    @app.post("/api/dashboard/settings/cron-preview")
    def dashboard_settings_cron_preview():
        """校验 cron 表达式并给出接下来几次触发时刻（保存前预览）。

        非法表达式返回 200 + valid=False：这是校验结果，不是请求错误。
        必须让界面能区分「合法、下次是 X」与「解析不了、调度器会静默回落到按
        interval 跑」——后者是最难发现的偏差（写错 cron 不会报任何错）。
        """
        body = request.get_json(silent=True) or {}
        expr = str(body.get("cron") or "").strip()
        tz_name = str(body.get("timezone") or "UTC")
        count = max(1, min(_safe_int(body.get("count"), 5), 10))

        from zoneinfo import ZoneInfo

        tz_candidate = tz_name.strip() or "UTC"
        try:
            ZoneInfo(tz_candidate)
        except Exception:
            timezone_valid = False
            effective_timezone = "UTC"
        else:
            timezone_valid = True
            effective_timezone = tz_candidate
        tz_fields = {
            "timezone": tz_name,
            "timezone_valid": timezone_valid,
            "effective_timezone": effective_timezone,
        }

        # 合法表达式最长也就几十个字符；超长输入直接判非法，免得把大列表交给 croniter
        if len(expr) > 200:
            return jsonify(
                {
                    "ok": True,
                    "data": {
                        "valid": False,
                        "empty": False,
                        "falls_back_to_interval": True,
                        "next_runs": [],
                        "runs_per_day": None,
                        **tz_fields,
                    },
                }
            )

        runs = cron_next_runs(expr, effective_timezone, count) if expr else None
        if runs is None:
            return jsonify(
                {
                    "ok": True,
                    "data": {
                        "valid": False,
                        "empty": not expr,
                        # 空表达式与非法表达式的后果相同（都按 interval 跑），
                        # 但文案不同，所以两个标记都给出去。
                        "falls_back_to_interval": True,
                        "next_runs": [],
                        "runs_per_day": None,
                        **tz_fields,
                    },
                }
            )

        from datetime import datetime as _datetime

        tz = ZoneInfo(effective_timezone)
        return jsonify(
            {
                "ok": True,
                "data": {
                    "valid": True,
                    "empty": False,
                    "falls_back_to_interval": False,
                    "next_runs": [
                        _datetime.fromtimestamp(run, tz).isoformat() for run in runs
                    ],
                    "runs_per_day": cron_runs_per_day(expr, effective_timezone),
                    **tz_fields,
                },
            }
        )

    @app.post("/api/dashboard/sync/cancel")
    def dashboard_sync_cancel():
        """取消手动同步任务。定时任务仍走 /api/dashboard/auto-sync/stop-task。"""
        job_id = (request.args.get("job_id") or "").strip()
        if not job_id:
            payload = request.get_json(silent=True) or {}
            job_id = str(payload.get("job_id") or "").strip()
        if not job_id:
            latest = shared_job_manager.latest_job()
            job_id = latest.job_id if latest is not None else ""
        if not job_id:
            return _api_error("没有可取消的任务", 404)
        if not shared_job_manager.request_cancel(job_id):
            return _api_error("任务不存在、已结束，或正在收尾", 409)
        return jsonify({"ok": True, "job_id": job_id, "message": "已请求取消"})

    @app.post("/api/dashboard/sync/start")
    def dashboard_sync_start():
        current_settings = settings_manager.load(env_path=env_path)
        spec = _build_web_sync_job_spec(current_settings)

        try:
            job = _submit_shared_job(spec, current_settings, "manual", "全量手动同步")
        except Exception as exc:
            return _submit_failure(exc)
        return jsonify({"ok": True, "message": job.message, "job": _shared_job_to_dict(job)})

    @app.get("/api/dashboard/sync/status")
    def dashboard_sync_status():
        job_id = request.args.get("job_id", "").strip()
        shared_job = shared_job_manager.get_job(job_id) if job_id else shared_job_manager.latest_job()
        if shared_job is not None:
            return jsonify({"job": _shared_job_to_dict(shared_job)})
        return jsonify({"job": None})

    @app.post("/api/dashboard/sync/<task_type>")
    def dashboard_sync_single(task_type: str):
        """手动触发单个同步任务

        这张表必须覆盖 SCHEDULER_TASK_CONFIGS 里的每个任务（按 task_logs 的
        task_type 命名）：改完配置得能立刻手动跑一次验证，否则只能干等下一个周期。
        tests/test_settings_sections.py 会逐个 POST 检查覆盖面。
        """
        task_map = {
            "bookmark": ("bookmark", "同步收藏"),
            "following_users": ("following_users", "同步关注用户"),
            "following_novels": ("following_novels", "同步关注小说"),
            # 前端按钮发的是下划线形式，而专门的追更系列路由是连字符的
            # /api/dashboard/sync/subscribed-series，缺这个键就必然 400。
            "subscribed_series": ("subscribed_series", "同步追更系列"),
            "user_status": ("user_status", "检查用户状态"),
            "novel_status": ("novel_status", "检查小说状态"),
            "series_status": ("series_status", "检查系列状态"),
            "user_backup": ("user_backup", "全量备份关注用户小说"),
            "pending_deletion_detection": (
                "pending_deletion_detection",
                "检测取消收藏/追更",
            ),
            "preference_analyze": ("preference_analyze", "增量分析本地偏好"),
            "recommendation_run": ("recommendation_run", "生成推荐"),
        }

        if task_type not in task_map:
            return _api_error("不支持的任务类型")
        
        internal_type, task_name = task_map[task_type]
        current_settings = settings_manager.load(env_path=env_path)
        try:
            spec = _web_job_spec([internal_type])
            job = _submit_shared_job(spec, current_settings, internal_type, task_name)
        except Exception as exc:
            return _submit_failure(exc)
        return jsonify({"ok": True, "message": "任务已启动", "job": _shared_job_to_dict(job)})

    @app.post("/api/dashboard/sync/subscribed-series")
    def dashboard_sync_subscribed_series():
        current_settings = settings_manager.load(env_path=env_path)

        # 从请求体获取 limit 参数
        req_data = request.get_json(silent=True) or {}
        limit = int(req_data.get("limit", 0) or 0)

        try:
            spec = _web_job_spec(["subscribed_series"], params={"limit": limit})
            job = _submit_shared_job(
                spec,
                current_settings,
                "subscribed_series",
                "同步追更系列",
                progress={"series_limit": limit},
            )
        except Exception as exc:
            return _submit_failure(exc)
        return jsonify({"ok": True, "message": "任务已启动", "job": _shared_job_to_dict(job)})

    @app.get("/api/dashboard/auto-sync/status")
    def auto_sync_status():
        """获取定时同步状态"""
        status = auto_sync_scheduler.get_status()
        # 如果有当前正在执行的任务，获取任务详情
        if status.get("current_task_job_id"):
            job = shared_job_manager.get_job(status["current_task_job_id"])
            if job:
                status["current_job"] = _job_to_dict(job)
        return jsonify(status)

    @app.get("/api/dashboard/auto-sync/budget")
    def auto_sync_budget():
        """调度预算聚合：每个任务的优先级、频率、上一轮耗时与预估每日占用。

        设置页的调度表要回答「这个任务每天要占掉多少时间」。预算 = 单轮耗时 × 每天
        触发次数，其中次数按当前 cron 现算（cron 一改，观测窗口里的历史总时长立刻
        失真），cron 解析不了时按 interval 折算——与调度器静默回落的行为保持一致。

        耗时来自 task_logs：前端不该去翻分页的 /api/dashboard/logs（20 条一页，11 个
        任务凑不齐一轮），也不该自己实现 cron 解析。
        """
        days = max(1, min(_safe_int(request.args.get("days"), 3), 30))
        current_settings = settings_manager.load(env_path=env_path)
        sync_settings = current_settings.sync
        tz_name = getattr(sync_settings, "auto_sync_timezone", "UTC") or "UTC"

        try:
            db = _open_database(current_settings)
            try:
                history = db.get_task_duration_stats(days=days)
            finally:
                db.close()
        except Exception as exc:
            logger.warning("读取调度耗时统计失败：%s", exc)
            history = {}

        tasks: dict[str, Any] = {}
        total_estimated = 0.0
        for config in SCHEDULER_TASK_CONFIGS:
            name = str(config["name"])
            log_type = scheduler_task_log_type(name)
            stats = history.get(log_type, {})
            cron_expr = str(getattr(sync_settings, config["cron_setting"], "") or "").strip()
            interval_hours = _safe_int(getattr(sync_settings, config["interval_setting"], 0), 0)

            runs_per_day = cron_runs_per_day(cron_expr, tz_name) if cron_expr else None
            cron_valid = runs_per_day is not None
            schedule_source = "cron" if cron_valid else "interval"
            if not cron_valid:
                # cron 为空或解析失败都落到 interval，这正是调度器的行为
                runs_per_day = round(24.0 / interval_hours, 4) if interval_hours > 0 else None

            # 优先用最近一轮的实测耗时；只有历史里没有终态记录时才退回均值
            reference = stats.get("last_duration_seconds")
            if reference is None:
                reference = stats.get("avg_duration_seconds")
            estimated = (
                round(runs_per_day * float(reference), 2)
                if runs_per_day is not None and reference is not None
                else None
            )

            enabled = bool(getattr(sync_settings, config["setting_check"], False))
            if enabled and estimated:
                total_estimated += estimated

            tasks[name] = {
                "task_type": log_type,
                "label": TASK_LABELS.get(log_type, log_type),
                "enabled": enabled,
                "priority": int(config.get("priority", 3)),
                "preemptible": bool(config.get("preemptible", False)),
                "cron": cron_expr,
                "cron_valid": cron_valid,
                "interval_hours": interval_hours,
                "schedule_source": schedule_source,
                "runs_per_day": runs_per_day,
                "runs": int(stats.get("runs", 0) or 0),
                "last_status": stats.get("last_status"),
                "last_started_at": stats.get("last_started_at"),
                "last_duration_seconds": stats.get("last_duration_seconds"),
                "avg_duration_seconds": (
                    round(float(stats["avg_duration_seconds"]), 2)
                    if stats.get("avg_duration_seconds") is not None
                    else None
                ),
                # 观测值：窗口内实际累计耗时摊到每天，与预估值对照能看出排布是否失真
                "observed_daily_seconds": (
                    round(float(stats.get("total_duration_seconds", 0.0)) / days, 2)
                    if stats.get("runs")
                    else None
                ),
                "estimated_daily_seconds": estimated,
            }

        return jsonify(
            {
                "ok": True,
                "days": days,
                "timezone": tz_name,
                "day_seconds": 86400,
                "tasks": tasks,
                "total_estimated_daily_seconds": round(total_estimated, 2),
                # 占空比：所有启用任务的每日预算 / 一天。共用一个执行槽，所以这个比例
                # 直接就是「唯一那个 job 槽的忙碌程度」。
                "total_duty_ratio": round(total_estimated / 86400.0, 6),
            }
        )

    @app.post("/api/dashboard/auto-sync/toggle")
    def auto_sync_toggle():
        """切换定时同步开关"""
        data = request.get_json(silent=True) or {}
        enabled = data.get("enabled")
        if enabled is None:
            return jsonify({"error": "missing enabled parameter"}), 400
        enabled = bool(enabled)
        if settings_manager.config_path:
            settings_manager.set_auto_sync_enabled(enabled)
        else:
            settings_manager.invalidate()

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
            days = max(1, min(request.args.get("days", 3, type=int) or 3, 90))
            category = request.args.get("category") or "sync"

            is_auto_sync = None
            if is_auto == "true":
                is_auto_sync = True
            elif is_auto == "false":
                is_auto_sync = False

            current_settings = settings_manager.load(env_path=env_path)
            db = _open_database(current_settings)
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
            db = _open_database(current_settings)
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
        cache_dir = _nginx_cache_dir()
        if not cache_dir.exists():
            return jsonify({"exists": False, "size_bytes": 0, "size_human": "0B"})
        total_size = 0
        file_count = 0
        truncated = False
        for f in cache_dir.rglob("*"):
            if f.is_file():
                total_size += f.stat().st_size
                file_count += 1
                if file_count >= _CACHE_SCAN_MAX_FILES:
                    # 遍历上限：超大缓存目录下避免无限扫描拖垮请求
                    truncated = True
                    break
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
            "file_count": file_count,
            "truncated": truncated
        })

    @app.post("/api/cache/clear")
    def cache_clear():
        import shutil
        cache_dir = _nginx_cache_dir()
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
        db = _open_database(current_settings)
        try:
            archive_refs = db.list_novel_archive_refs(novel_ids=[novel_id])
            archive_cleanup = _remove_archive_files_atomic(
                current_settings, archive_refs, lambda: db.delete_novel(novel_id)
            )
            return jsonify({"ok": True, "message": "小说已删除", "archive_cleanup": archive_cleanup})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()
    
    @app.delete("/api/dashboard/series/<int:series_id>")
    def delete_series(series_id: int):
        """删除系列"""
        current_settings = settings_manager.load(env_path=env_path)
        db = _open_database(current_settings)
        try:
            with db.transaction():
                chapter_ids = db.delete_series(series_id)
                _refresh_rescue_chapters(db, chapter_ids)
            return jsonify({"ok": True, "message": "系列已删除"})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()
    
    @app.delete("/api/dashboard/bookmarks/<int:novel_id>")
    def delete_bookmark(novel_id: int):
        """删除收藏记录"""
        current_settings = settings_manager.load(env_path=env_path)
        db = _open_database(current_settings)
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
        db = _open_database(current_settings)
        try:
            payload = db.list_pending_deletions(page=page, page_size=page_size, item_type=item_type)
        finally:
            db.close()
        return jsonify(payload)

    @app.get("/api/dashboard/shell-data")
    def shell_data():
        """提供前端 Shell (Navbar 等) 需要的全局聚合数据"""
        current_settings = settings_manager.load(env_path=env_path)
        db = _open_database(current_settings)
        try:
            pending_count = db.get_pending_deletion_count()
            # 可以根据需要扩展用户信息等
        finally:
            db.close()
        return jsonify({
            "pending_count": pending_count
        })

    @app.post("/api/dashboard/pending-deletions/detect")
    def trigger_pending_detection():
        current_settings = settings_manager.load(env_path=env_path)
        try:
            spec = _web_job_spec(["pending_deletion_detection"])
            job = _submit_shared_job(spec, current_settings, "pending_deletion_detection", "检测取消收藏/追更")
        except Exception as exc:
            return _submit_failure(exc)
        return jsonify({"ok": True, "message": "检测任务已启动", "job": _shared_job_to_dict(job)})

    @app.post("/api/dashboard/pending-deletions/<int:deletion_id>/confirm")
    def confirm_pending_deletion(deletion_id: int):
        current_settings = settings_manager.load(env_path=env_path)
        db = _open_database(current_settings)
        trash: _ArchiveTrash | None = None
        try:
            preview = db.conn.execute(
                "SELECT item_type, item_id FROM pending_deletions WHERE id = ? AND status = 'pending'",
                (deletion_id,),
            ).fetchone()
            if preview is None:
                return jsonify({"error": "记录不存在或已处理"}), 404
            item_type = preview["item_type"]
            item_id = int(preview["item_id"])
            if item_type == "novel":
                archive_refs = db.list_novel_archive_refs(novel_ids=[item_id])
            elif item_type == "series":
                archive_refs = db.list_novel_archive_refs(series_id=item_id)
            else:
                archive_refs = []
            # 文件先离开原位，再开写事务。搬文件时不持有 SQLite 写锁。
            trash = _ArchiveTrash(current_settings, archive_refs)
            trash.stage()
            with db.transaction():
                record = db.confirm_pending_deletion(deletion_id)
                if record is None:
                    raise LookupError("记录不存在或已处理")
                if item_type == "novel":
                    db.delete_novel(item_id)
                elif item_type == "series":
                    current_chapter_rows = db.conn.execute(
                        "SELECT novel_id FROM novels WHERE series_id = ? ORDER BY novel_id",
                        (item_id,),
                    ).fetchall()
                    current_chapter_ids = [int(row["novel_id"]) for row in current_chapter_rows]
                    affected_chapter_ids = db.delete_series(item_id)
                    for novel_id in current_chapter_ids:
                        db.delete_novel(novel_id)
                    _refresh_rescue_chapters(db, affected_chapter_ids)
            archive_cleanup = trash.commit()
            return jsonify({"ok": True, "message": "已确认删除", "archive_cleanup": archive_cleanup})
        except LookupError as exc:
            if trash is not None:
                trash.rollback()
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:
            if trash is not None:
                trash.rollback()
            try:
                db.conn.execute(
                    """
                    UPDATE pending_deletions
                    SET status = 'pending', confirmed_at = NULL
                    WHERE id = ? AND status = 'confirmed'
                    """,
                    (deletion_id,),
                )
                db._commit_if_needed()
            except Exception:
                logger.warning("确认删除失败后未能把记录改回 pending", exc_info=True)
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()

    @app.post("/api/dashboard/pending-deletions/<int:deletion_id>/restore")
    def restore_pending_deletion(deletion_id: int):
        current_settings = settings_manager.load(env_path=env_path)
        db = _open_database(current_settings)
        try:
            # 单事务恢复：restore + 来源补录 + 系列订阅恢复一并提交（原子）
            record = db.restore_pending_deletion_atomic(
                deletion_id,
                bookmark_source_key=str(current_settings.pixiv.user_id or 0),
            )
            if record is None:
                return jsonify({"error": "记录不存在或已处理"}), 404
            if record.get("refresh_failed"):
                return jsonify({
                    "ok": True,
                    "restored": True,
                    "message": "已恢复，但救援目录刷新失败",
                })
            return jsonify({"ok": True, "restored": True, "message": "已恢复"})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()

    # ------------------------------------------------------------------
    # 健康检查 API
    # ------------------------------------------------------------------
    @app.get("/api/health")
    def health_check():
        """返回服务健康状态。探活不打开数据库。version 留给已有测试和契约。"""
        return jsonify({
            "status": "ok",
            "version": __version__,
        })

    # ------------------------------------------------------------------
    # 导出同步统计数据 API
    # ------------------------------------------------------------------
    @app.get("/api/dashboard/export/stats")
    def dashboard_export_stats():
        """导出同步统计数据"""
        current_settings = settings_manager.load(env_path=env_path)
        db = _open_database(current_settings)
        try:
            return jsonify(db.collect_library_export())
        except Exception as exc:
            logger.error("Export stats failed: %s", exc)
            return jsonify({"error": str(exc)}), 500
        finally:
            db.close()

    @app.context_processor
    def inject_task_labels():
        return {"task_labels": TASK_LABELS}

    return app


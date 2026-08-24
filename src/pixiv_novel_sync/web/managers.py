"""管理器类模块 - 从 webapp.py 提取

包含:
- SyncJobState: 同步任务状态数据类
- TASK_LABELS: 任务标签字典
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

from ..settings import Settings, load_settings
from ..storage_db import Database
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
# submit 失败/被占用时的短退避（秒）：避免静默顺延整个周期
SCHEDULER_SUBMIT_RETRY_SECONDS = 300.0

# --- 重启补偿（从 task_logs 恢复上次执行时间）相关常量 ---
# 回溯查询 task_logs 的最长窗口（天）。task_logs 本身默认只保留 3 天
# （见 cleanup_old_task_logs），这里放宽到 7 天纯粹是冗余保护。
SCHEDULER_HISTORY_LOOKBACK_DAYS = 7
# 单个 task_type 最多扫描多少条日志去找最后一次执行：足够跨过最近的失败/取消记录。
SCHEDULER_HISTORY_SCAN_LIMIT = 50
# 恢复上次执行时间时认哪些终态。partial = 熔断中止/本轮没跑完，它同样代表"这个时间点
# 跑过一轮"，必须计入，否则轮转类任务（每轮都 incomplete）重启后会白白顺延一个周期。
_SCHEDULER_COMPLETED_STATUSES = frozenset({"succeeded", "partial"})
# 已经错过执行窗口的任务不立刻触发，先等待这段启动宽限（秒），让服务先稳定下来。
SCHEDULER_STARTUP_GRACE_SECONDS = 60.0
# 多个逾期任务之间再按配置顺序依次错开这段时间（秒），避免重启瞬间惊群打 Pixiv 接口。
SCHEDULER_STARTUP_STAGGER_SECONDS = 60.0

# 调度器内部任务名 → task_logs.task_type 的别名。
# webapp 提交定时任务时写入 task_logs 的是 _scheduler_job_spec() 归一化后的
# task_types[0]，这里必须保持同一套映射，否则按任务名查不到任何历史记录。
SCHEDULER_TASK_TYPE_ALIASES = {
    "bookmarks": "bookmark",
    "following_list": "following_users",
}

# 定时任务清单：调度主循环与重启补偿共用，避免两处走样。
SCHEDULER_TASK_CONFIGS: tuple[dict[str, str], ...] = (
    {"name": "bookmarks", "setting_check": "auto_sync_bookmarks_enabled", "interval_setting": "auto_sync_bookmarks_interval_hours", "cron_setting": "auto_sync_bookmarks_cron"},
    {"name": "following_list", "setting_check": "auto_sync_following_list_enabled", "interval_setting": "auto_sync_following_list_interval_hours", "cron_setting": "auto_sync_following_list_cron"},
    {"name": "following_novels", "setting_check": "auto_sync_following_novels_enabled", "interval_setting": "auto_sync_following_novels_interval_hours", "cron_setting": "auto_sync_following_novels_cron"},
    {"name": "subscribed_series", "setting_check": "auto_sync_subscribed_series_enabled", "interval_setting": "auto_sync_subscribed_series_interval_hours", "cron_setting": "auto_sync_subscribed_series_cron"},
    {"name": "user_status", "setting_check": "auto_sync_user_status_enabled", "interval_setting": "auto_sync_user_status_interval_hours", "cron_setting": "auto_sync_user_status_cron"},
    {"name": "novel_status", "setting_check": "auto_sync_novel_status_enabled", "interval_setting": "auto_sync_novel_status_interval_hours", "cron_setting": "auto_sync_novel_status_cron"},
    {"name": "series_status", "setting_check": "auto_sync_series_status_enabled", "interval_setting": "auto_sync_series_status_interval_hours", "cron_setting": "auto_sync_series_status_cron"},
    {"name": "user_backup", "setting_check": "auto_sync_user_backup_enabled", "interval_setting": "auto_sync_user_backup_interval_hours", "cron_setting": "auto_sync_user_backup_cron"},
    {"name": "pending_deletion_detection", "setting_check": "auto_sync_pending_detection_enabled", "interval_setting": "auto_sync_pending_detection_interval_hours", "cron_setting": "auto_sync_pending_detection_cron"},
    {"name": "preference_analyze", "setting_check": "auto_sync_preference_analyze_enabled", "interval_setting": "auto_sync_preference_analyze_interval_hours", "cron_setting": "auto_sync_preference_analyze_cron"},
    {"name": "recommendation_run", "setting_check": "auto_sync_recommendation_run_enabled", "interval_setting": "auto_sync_recommendation_run_interval_hours", "cron_setting": "auto_sync_recommendation_run_cron"},
)


def scheduler_task_log_type(task_name: str) -> str:
    """把调度器任务名映射成它在 task_logs 中记录的 task_type。"""
    return SCHEDULER_TASK_TYPE_ALIASES.get(task_name, task_name)


def _parse_db_timestamp(value: Any) -> float | None:
    """把 task_logs 的时间文本解析成 epoch 秒；无法解析时返回 None。

    task_logs 由 SQLite 的 datetime('now') 写入，格式是 UTC 的
    'YYYY-MM-DD HH:MM:SS' 且不带时区标记，因此裸时间一律按 UTC 处理，
    不能交给本地时区解析（否则会整体偏移一个时区差）。
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


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
    "recommendation_run": "生成推荐",
}


@dataclass
class AutoSyncScheduler:
    """定时同步调度器 - 每个任务独立运行"""
    config_path: str | None
    env_path: str | None
    shared_job_manager: Any = None
    submit_task: Callable[[Settings, str], Any | None] | None = None
    run_task: Callable[[str], None] | None = None
    get_task: Callable[[str], Any | None] | None = None
    cancel_task: Callable[[str], bool] | None = None
    _running: bool = False
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _task_last_run: dict[str, float] = field(default_factory=dict)  # 每个任务的上次运行时间
    _task_next_run: dict[str, float] = field(default_factory=dict)  # 每个任务的下次运行时间
    _task_intervals: dict[str, int] = field(default_factory=dict)  # 每个任务的间隔（小时）
    _task_crons: dict[str, str] = field(default_factory=dict)  # 每个任务的cron表达式
    _current_task_job_id: str | None = None  # 当前正在执行的定时任务 job id
    _last_cleanup_time: float = 0.0  # 上次清理日志的时间
    _catalog_initialization_attempted: bool = False
    _schedule_restored: bool = False  # 是否已从 task_logs 做过一次重启补偿
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
                    self._catalog_initialization_attempted = False
                    self._schedule_restored = False
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
        """停止定时调度器并取消当前共享任务。"""
        with self._lock:
            self._running = False
            self._stop_event.set()
            thread = self._thread
            job_id = self._current_task_job_id
            cancel_task = self.cancel_task
            logger.info("Auto sync scheduler stopped")
            if thread is None and self._lifecycle_release is not None:
                self._lifecycle_release(self)
        if job_id is not None and cancel_task is not None:
            try:
                cancel_task(job_id)
            except Exception as exc:
                logger.warning("Failed to cancel current auto sync task %s: %s", job_id, exc)
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
        """向共享 JobManager 请求取消当前定时任务。"""
        with self._lock:
            job_id = self._current_task_job_id
            cancel_task = self.cancel_task
        if job_id is None or cancel_task is None:
            return False
        logger.info("Stopping current auto sync task: %s", job_id)
        return bool(cancel_task(job_id))

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
        task_configs = SCHEDULER_TASK_CONFIGS

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
                # dict 写入纳入锁，避免与 get_status() 并发读写竞态
                with self._lock:
                    for task_config in task_configs:
                        task_name = task_config["name"]
                        cron_expr = getattr(settings.sync, task_config["cron_setting"], "")
                        task_interval_hours = getattr(settings.sync, task_config["interval_setting"], 6)
                        self._task_intervals[task_name] = task_interval_hours
                        self._task_crons[task_name] = cron_expr

                if not settings.sync.auto_sync_enabled:
                    stop_event.wait(60)
                    continue

                # 重启补偿：调度真正开始工作时做一次，从 task_logs 恢复上次执行时间，
                # 避免进程重启把每个任务都顺延一个完整周期。
                if not self._schedule_restored:
                    self._schedule_restored = True
                    self._restore_schedule_from_task_logs(settings)

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

                    submitted = self._run_single_task(settings, task_name)

                    with self._lock:
                        if not submitted:
                            # submit 失败/被占用：短退避后重试，而非顺延整个周期
                            self._task_next_run[task_name] = time.time() + SCHEDULER_SUBMIT_RETRY_SECONDS
                            logger.info(
                                "Task %s submit failed, retry at: %s", task_name,
                                datetime.fromtimestamp(self._task_next_run[task_name]).strftime('%Y-%m-%d %H:%M:%S'),
                            )
                            continue
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
    
    def _last_success_finish_time(self, db: Any, task_log_type: str) -> float | None:
        """查询某个 task_type 最近一次跑完的时间（epoch 秒），没有则返回 None。

        task_logs 已经记录了每次任务的 task_type/status/started_at/finished_at，
        直接复用，不额外建表。手动触发的同一任务也算数——它同样刷新了数据，
        没必要紧接着再跑一次定时同步。

        ``partial``（熔断中止/本轮没跑完）同样算「跑过了」：排程只关心"上次什么时候
        跑的"，不关心跑得全不全。关注小说这类轮转任务每轮都是 partial，若只认
        succeeded，重启后就永远恢复不出上次运行时间，白白顺延一个周期。
        """
        result = db.get_task_logs(
            page=1,
            page_size=SCHEDULER_HISTORY_SCAN_LIMIT,
            task_type=task_log_type,
            days=SCHEDULER_HISTORY_LOOKBACK_DAYS,
        )
        # get_task_logs 按 started_at 倒序，第一条已跑完的记录即最近一次
        for item in result.get("items", []):
            if item.get("status") not in _SCHEDULER_COMPLETED_STATUSES:
                continue
            timestamp = _parse_db_timestamp(item.get("finished_at"))
            if timestamp is None:
                # 极少数历史脏数据没有 finished_at，退化用 started_at
                timestamp = _parse_db_timestamp(item.get("started_at"))
            if timestamp is not None:
                return timestamp
        return None

    def _restore_schedule_from_task_logs(self, settings: Settings) -> None:
        """重启补偿：按 task_logs 里的上次成功完成时间重建各任务的下次运行时间。

        进程重启后 _task_next_run 是空的，原逻辑会把每个任务排到「现在 + 完整间隔」，
        于是每次部署都白白顺延一个周期（生产实测 12 小时的收藏同步变成 15-17 小时）。
        这里改成「上次成功完成时间 + 间隔」，与运行期语义一致——运行期也是任务跑完
        之后才写 _task_last_run / _task_next_run。

        规则：
        - 只处理「已启用 + 未配置 cron」的任务；cron 是绝对时间点，本来就不会被顺延。
        - 没有历史记录（首次部署）时不写入，沿用原来的「现在 + 间隔」。
        - 已经错过窗口的任务不立刻触发，而是按任务清单顺序依次错开
          （启动宽限 + 序号 × 错峰间隔），避免重启瞬间所有逾期任务连着跑。
        - 任何异常都只记警告并放弃恢复，退回原有行为，绝不影响调度启动。
        """
        pending: list[tuple[str, str, float]] = []
        for task_config in SCHEDULER_TASK_CONFIGS:
            task_name = task_config["name"]
            if not getattr(settings.sync, task_config["setting_check"], False):
                continue
            if getattr(settings.sync, task_config["cron_setting"], ""):
                continue
            with self._lock:
                if task_name in self._task_next_run:
                    continue
            interval_hours = getattr(settings.sync, task_config["interval_setting"], 6)
            pending.append(
                (task_name, scheduler_task_log_type(task_name), float(interval_hours) * 3600)
            )

        if not pending:
            return

        restored: list[tuple[str, float, float]] = []
        db = None
        try:
            db = Database(settings.storage.db_path)
            db.init_schema()
            now = time.time()
            overdue_index = 0
            for task_name, task_log_type, interval_seconds in pending:
                last_success = self._last_success_finish_time(db, task_log_type)
                if last_success is None:
                    continue
                next_run = last_success + interval_seconds
                if next_run <= now:
                    next_run = (
                        now
                        + SCHEDULER_STARTUP_GRACE_SECONDS
                        + overdue_index * SCHEDULER_STARTUP_STAGGER_SECONDS
                    )
                    overdue_index += 1
                restored.append((task_name, last_success, next_run))
        except Exception as exc:
            logger.warning("从任务日志恢复定时任务进度失败：%s", exc)
            return
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception as exc:
                    logger.warning("关闭恢复定时任务进度的数据库失败：%s", exc)

        with self._lock:
            for task_name, last_success, next_run in restored:
                if task_name in self._task_next_run:
                    continue
                self._task_last_run[task_name] = last_success
                self._task_next_run[task_name] = next_run
                logger.info(
                    "Task %s restored from task logs, last success: %s, next run: %s",
                    task_name,
                    datetime.fromtimestamp(last_success).strftime('%Y-%m-%d %H:%M:%S'),
                    datetime.fromtimestamp(next_run).strftime('%Y-%m-%d %H:%M:%S'),
                )

    def _run_single_task(self, settings: Settings, task_name: str) -> bool:
        """通过共享 JobManager/JobRunner 同步执行单个定时任务。

        返回 True 表示任务已成功提交（并尝试执行）；返回 False 表示
        提交失败或被占用，调用方应做短退避重试而非顺延整个周期。
        """
        logger.info("Starting auto sync task: %s", task_name)
        submit_task = self.submit_task
        run_task = self.run_task
        if submit_task is None or run_task is None:
            logger.error(
                "Auto sync task %s skipped: shared job callbacks are unavailable",
                task_name,
            )
            return False

        try:
            job = submit_task(settings, task_name)
        except Exception as exc:
            logger.error("Failed to submit auto sync task %s: %s", task_name, exc)
            return False
        if job is None:
            logger.info(
                "Auto sync task %s skipped: another sync task is running",
                task_name,
            )
            return False

        with self._lock:
            stopped_during_submit = self._stop_event.is_set()
            if not stopped_during_submit:
                self._current_task_job_id = job.job_id
        if stopped_during_submit:
            cancel_task = self.cancel_task
            if cancel_task is not None:
                try:
                    cancel_task(job.job_id)
                except Exception as exc:
                    logger.warning(
                        "Failed to cancel auto sync task %s after scheduler stop: %s",
                        job.job_id,
                        exc,
                    )
            return True

        try:
            run_task(job.job_id)
        except Exception as exc:
            logger.error("Auto sync task %s runner failed: %s", task_name, exc)
        finally:
            with self._lock:
                if self._current_task_job_id == job.job_id:
                    self._current_task_job_id = None
        return True


@dataclass(slots=True)
class SyncJobManager:
    config_path: str | None
    env_path: str | None
    _jobs: dict[str, SyncJobState] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _semaphore: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(1))
    MAX_LOGS: int = 50
    MAX_JOBS: int = 100  # 最多保留的任务数

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
        """保留：仅测试/兼容用途。"""
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
        sync_data["auto_sync_recommendation_run_enabled"] = bool(payload.get("auto_sync_recommendation_run_enabled", sync_data.get("auto_sync_recommendation_run_enabled", False)))
        sync_data["auto_sync_recommendation_run_interval_hours"] = _save_int("auto_sync_recommendation_run_interval_hours", 24)
        sync_data["auto_sync_recommendation_run_cron"] = _save_cron("auto_sync_recommendation_run_cron")
        sync_data["pending_deletion_grace_period_days"] = _save_int("pending_deletion_grace_period_days", 30)
        sync_data["pending_deletion_cleanup_confirmed_days"] = _save_int("pending_deletion_cleanup_confirmed_days", 7)

        _atomic_write_yaml(config_path, config_data)

        self.invalidate()
        return _settings_to_dict(load_settings(config_path, None))

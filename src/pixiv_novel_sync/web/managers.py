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

# --- 任务优先级与让位 ---
# 优先级数字越小越重要。只有「拉 Pixiv 数据」的任务之间才需要排序：它们共用同一个
# job 槽（JobManager 的 BoundedSemaphore(1)），谁占着槽别人就得等。
SCHEDULER_PRIORITY_HIGHEST = 1
SCHEDULER_PRIORITY_DEFAULT = 3
# 正在运行的低优先级任务被高优先级任务打断时，多久轮询一次「有没有人来抢」。
SCHEDULER_PREEMPT_POLL_SECONDS = 5.0
# 让位后被打断的任务多久重试（秒）。不能顺延整个周期，否则一让位就等于跳过一轮。
SCHEDULER_PREEMPT_RETRY_SECONDS = 600.0
# 同一个任务被让位后，这段时间内不再被让位——防止高优先级任务频繁到点把长任务饿死。
SCHEDULER_PREEMPT_COOLDOWN_SECONDS = 6 * 3600.0
# 连续被让位这么多次后，强制让它跑完一整轮（临时视为不可让位）。
SCHEDULER_MAX_CONSECUTIVE_PREEMPTIONS = 2
# 退避时长按优先级分级：越重要的任务被占用时重试得越勤。
SCHEDULER_RETRY_SECONDS_BY_PRIORITY: dict[int, float] = {1: 60.0, 2: 120.0}

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
#
# priority：1 最重要。同一时刻多个任务到点时按 priority 升序挑一个提交，同级再按
#   「逾期最久」优先。用户口径：收藏 = P1，追更系列 = P2，其余 = P3。
# preemptible：正在跑的它能否为更高优先级的任务让位。只有「按水位轮转、下轮能接着
#   跑」的任务才可以让位（following_novels 按 user_last_synced、三个 status 检查按
#   last_checked_at、user_backup 按 offset、preference_analyze 按累加器）。
#   bookmarks/following_list/pending_detection 本身只跑几十秒到几分钟，没必要打断；
#   subscribed_series 每轮从 watchlist 头部重走一遍、不是水位式续跑，打断等于白跑；
#   recommendation_run 跑一半的推荐结果没有意义。
SCHEDULER_TASK_CONFIGS: tuple[dict[str, Any], ...] = (
    {"name": "bookmarks", "setting_check": "auto_sync_bookmarks_enabled", "interval_setting": "auto_sync_bookmarks_interval_hours", "cron_setting": "auto_sync_bookmarks_cron", "priority": 1, "preemptible": False},
    {"name": "subscribed_series", "setting_check": "auto_sync_subscribed_series_enabled", "interval_setting": "auto_sync_subscribed_series_interval_hours", "cron_setting": "auto_sync_subscribed_series_cron", "priority": 2, "preemptible": False},
    {"name": "following_list", "setting_check": "auto_sync_following_list_enabled", "interval_setting": "auto_sync_following_list_interval_hours", "cron_setting": "auto_sync_following_list_cron", "priority": 3, "preemptible": False},
    {"name": "following_novels", "setting_check": "auto_sync_following_novels_enabled", "interval_setting": "auto_sync_following_novels_interval_hours", "cron_setting": "auto_sync_following_novels_cron", "priority": 3, "preemptible": True},
    {"name": "user_status", "setting_check": "auto_sync_user_status_enabled", "interval_setting": "auto_sync_user_status_interval_hours", "cron_setting": "auto_sync_user_status_cron", "priority": 3, "preemptible": True},
    {"name": "novel_status", "setting_check": "auto_sync_novel_status_enabled", "interval_setting": "auto_sync_novel_status_interval_hours", "cron_setting": "auto_sync_novel_status_cron", "priority": 3, "preemptible": True},
    {"name": "series_status", "setting_check": "auto_sync_series_status_enabled", "interval_setting": "auto_sync_series_status_interval_hours", "cron_setting": "auto_sync_series_status_cron", "priority": 3, "preemptible": True},
    {"name": "user_backup", "setting_check": "auto_sync_user_backup_enabled", "interval_setting": "auto_sync_user_backup_interval_hours", "cron_setting": "auto_sync_user_backup_cron", "priority": 3, "preemptible": True},
    {"name": "pending_deletion_detection", "setting_check": "auto_sync_pending_detection_enabled", "interval_setting": "auto_sync_pending_detection_interval_hours", "cron_setting": "auto_sync_pending_detection_cron", "priority": 3, "preemptible": False},
    {"name": "preference_analyze", "setting_check": "auto_sync_preference_analyze_enabled", "interval_setting": "auto_sync_preference_analyze_interval_hours", "cron_setting": "auto_sync_preference_analyze_cron", "priority": 3, "preemptible": True},
    {"name": "recommendation_run", "setting_check": "auto_sync_recommendation_run_enabled", "interval_setting": "auto_sync_recommendation_run_interval_hours", "cron_setting": "auto_sync_recommendation_run_cron", "priority": 3, "preemptible": False},
)

SCHEDULER_TASK_CONFIG_BY_NAME: dict[str, dict[str, Any]] = {
    config["name"]: config for config in SCHEDULER_TASK_CONFIGS
}


def scheduler_task_priority(task_name: str) -> int:
    config = SCHEDULER_TASK_CONFIG_BY_NAME.get(task_name)
    if config is None:
        return SCHEDULER_PRIORITY_DEFAULT
    return int(config.get("priority", SCHEDULER_PRIORITY_DEFAULT))


def scheduler_task_is_preemptible(task_name: str) -> bool:
    config = SCHEDULER_TASK_CONFIG_BY_NAME.get(task_name)
    return bool(config is not None and config.get("preemptible", False))


def scheduler_retry_seconds(task_name: str) -> float:
    """槽被占用时该任务的退避时长：高优先级重试得更勤。"""
    return SCHEDULER_RETRY_SECONDS_BY_PRIORITY.get(
        scheduler_task_priority(task_name), SCHEDULER_SUBMIT_RETRY_SECONDS
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


# --- 设置分区 ---
# 设置页拆成五个一级页面后要能按分区独立保存：每个分区声明自己拥有哪些字段。
# 分区保存时只有本区字段允许从 payload 取值，其余字段强制沿用 YAML 既有值——
# 否则「同步页保存」会把系统页没加载的字段写成默认值（save_sync_settings 通篇是
# payload.get(k, 默认值)，在只含半份表单的 payload 下这就是静默覆盖）。
#
# 覆盖面由 tests/test_settings_sections.py 锁定：_settings_to_dict 暴露的每个字段
# 必须落在且只落在一个分区里。新增设置项时若忘了登记，那个字段将永远无法通过分区
# 端点保存。
# 只有这两个分区对应 YAML 的 sync: 块；模型/Agent/成人润色三页的配置存在数据库里，
# 走 ai_web.py 自己的端点，不经过 save_sync_settings。
SETTINGS_SECTIONS: dict[str, frozenset[str]] = {
    "sync": frozenset(
        {
            # 基础开关与输出
            "enabled",
            "initial_manual_only",
            "download_assets",
            "write_markdown",
            "write_raw_text",
            "bookmark_restricts",
            # 单轮配额与分页上限
            "max_items_per_run",
            "max_pages_per_run",
            "bookmark_max_pages_per_run",
            "following_max_novels_per_author",
            "series_max_pages_per_run",
            "series_sync_limit",
            # 限速
            "delay_seconds_between_items",
            "delay_seconds_between_pages",
            "delay_seconds_between_series",
            "delay_seconds_between_chapters",
            "delay_seconds_between_skips",
            # 同步范围
            "sync_bookmarks",
            "sync_following_users",
            "sync_following_novels",
            "sync_subscribed_series",
            # 定时同步总开关与时区。auto_sync_enabled 归在同步区只是为了「每个字段
            # 都有归属」，它由首页那个开关经 /api/dashboard/auto-sync/toggle 单独
            # 落盘，save_sync_settings 无论全量还是分区都刻意不写它。
            "auto_sync_enabled",
            "auto_sync_timezone",
            # 各任务的调度参数（开关 / 间隔 / cron）
            "auto_sync_bookmarks_enabled",
            "auto_sync_bookmarks_interval_hours",
            "auto_sync_bookmarks_cron",
            "auto_sync_following_list_enabled",
            "auto_sync_following_list_interval_hours",
            "auto_sync_following_list_cron",
            "auto_sync_following_novels_enabled",
            "auto_sync_following_novels_interval_hours",
            "auto_sync_following_novels_cron",
            "auto_sync_following_novels_users_limit",
            "auto_sync_user_status_enabled",
            "auto_sync_user_status_interval_hours",
            "auto_sync_user_status_cron",
            "auto_sync_novel_status_enabled",
            "auto_sync_novel_status_interval_hours",
            "auto_sync_novel_status_cron",
            "auto_sync_series_status_enabled",
            "auto_sync_series_status_interval_hours",
            "auto_sync_series_status_cron",
            "auto_sync_subscribed_series_enabled",
            "auto_sync_subscribed_series_interval_hours",
            "auto_sync_subscribed_series_cron",
            "auto_sync_user_backup_enabled",
            "auto_sync_user_backup_interval_hours",
            "auto_sync_user_backup_cron",
            "auto_sync_pending_detection_enabled",
            "auto_sync_pending_detection_interval_hours",
            "auto_sync_pending_detection_cron",
            "auto_sync_preference_analyze_enabled",
            "auto_sync_preference_analyze_interval_hours",
            "auto_sync_preference_analyze_cron",
            "preference_analyze_batch_size",
            "auto_sync_recommendation_run_enabled",
            "auto_sync_recommendation_run_interval_hours",
            "auto_sync_recommendation_run_cron",
        }
    ),
    "system": frozenset(
        {
            "pending_deletion_grace_period_days",
            "pending_deletion_cleanup_confirmed_days",
        }
    ),
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
    _current_task_name: str | None = None  # 当前正在执行的定时任务名（让位判定要用）
    # 让位护栏：上次被让位的时刻 / 连续被让位次数。防止 P1 频繁到点把长任务永久饿死。
    _task_preempted_at: dict[str, float] = field(default_factory=dict)
    _task_preempt_streak: dict[str, int] = field(default_factory=dict)
    # 本轮刚刚被让位的任务名，由 _run_single_task 写入、主循环消费一次
    _pending_preemption: set[str] = field(default_factory=set)
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
                "current_task_name": self._current_task_name,
                "task_next_run": dict(self._task_next_run),
                "task_last_run": dict(self._task_last_run),
                "task_intervals": dict(self._task_intervals),
                "task_crons": dict(self._task_crons),
                # 让前端设置页能展示优先级与让位能力，不必自己硬编码一份
                "task_priorities": {
                    config["name"]: int(config.get("priority", SCHEDULER_PRIORITY_DEFAULT))
                    for config in SCHEDULER_TASK_CONFIGS
                },
                "task_preemptible": {
                    config["name"]: bool(config.get("preemptible", False))
                    for config in SCHEDULER_TASK_CONFIGS
                },
                "task_preempted_at": dict(self._task_preempted_at),
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

                    # 首次见到某个任务时先落一个 next_run，别在挑选阶段才补
                    with self._lock:
                        if task_name not in self._task_next_run:
                            self._task_next_run[task_name] = self._compute_next_run(
                                cron_expr, now, tz_name, task_interval_seconds
                            )
                            logger.info(
                                "Task %s scheduled, next run: %s", task_name,
                                datetime.fromtimestamp(self._task_next_run[task_name]).strftime('%Y-%m-%d %H:%M:%S'),
                            )

                # 到点的任务按 (优先级, 逾期最久) 排序，每轮只提交第一个。
                # 旧行为是按 SCHEDULER_TASK_CONFIGS 的数组顺序扫描，谁排在前面谁先抢槽，
                # 「收藏优先」只是数组顺序的巧合而不是设计；长任务一旦占住槽，收藏就得
                # 干等一个退避周期。
                due_tasks = self._collect_due_tasks(settings)
                if not due_tasks:
                    stop_event.wait(30)
                    continue

                task_name = due_tasks[0]
                task_config = SCHEDULER_TASK_CONFIG_BY_NAME[task_name]
                cron_expr = getattr(settings.sync, task_config["cron_setting"], "")
                task_interval_seconds = (
                    getattr(settings.sync, task_config["interval_setting"], 6) * 3600
                )
                if len(due_tasks) > 1:
                    logger.info(
                        "Task %s selected by priority; also due: %s",
                        task_name,
                        ", ".join(due_tasks[1:]),
                    )

                submitted = self._run_single_task(settings, task_name)
                # 只有真的跑过才消费让位标记：submit 失败时不能顺手把连续让位计数清零，
                # 否则护栏会被"反复提交失败"悄悄绕过。
                preempted = self._consume_preemption_flag(task_name) if submitted else False

                with self._lock:
                    if not submitted:
                        # submit 失败/被占用：短退避后重试，而非顺延整个周期。
                        # 退避时长按优先级分级，P1 每分钟就再试一次。
                        retry_seconds = scheduler_retry_seconds(task_name)
                        self._task_next_run[task_name] = time.time() + retry_seconds
                        logger.info(
                            "Task %s submit failed, retry at: %s", task_name,
                            datetime.fromtimestamp(self._task_next_run[task_name]).strftime('%Y-%m-%d %H:%M:%S'),
                        )
                        continue
                    self._task_last_run[task_name] = time.time()
                    if preempted:
                        # 让位不是"跑完了"：顺延整个周期等于白白跳过一轮。短退避后接着
                        # 从水位续跑（让位对象已经拿到槽，这段退避正好让它跑完）。
                        self._task_next_run[task_name] = (
                            time.time() + SCHEDULER_PREEMPT_RETRY_SECONDS
                        )
                        logger.info(
                            "Task %s yielded to a higher-priority task, resume at: %s",
                            task_name,
                            datetime.fromtimestamp(self._task_next_run[task_name]).strftime('%Y-%m-%d %H:%M:%S'),
                        )
                        continue
                    self._task_next_run[task_name] = self._compute_next_run(
                        cron_expr, time.time(), tz_name, task_interval_seconds
                    )
                    logger.info(
                        "Task %s completed, next run: %s", task_name,
                        datetime.fromtimestamp(self._task_next_run[task_name]).strftime('%Y-%m-%d %H:%M:%S'),
                    )

                # 刚跑完一个任务，立刻回到循环顶部重新按优先级挑选，
                # 不必再等 30 秒——否则 P1 到点后还要多等一个 tick。
                if stop_event.is_set():
                    break

            except Exception as exc:
                logger.error("Scheduler error: %s", exc)
                stop_event.wait(60)

    def _compute_next_run(
        self,
        cron_expr: str,
        base_time: float,
        tz_name: str,
        interval_seconds: float,
    ) -> float:
        """算下次运行时刻：cron 非空优先，解析失败回落到 interval。"""
        if cron_expr:
            from ..settings import cron_to_next_run

            resolved = cron_to_next_run(cron_expr, base_time, tz_name)
            if resolved:
                return float(resolved)
        return base_time + interval_seconds

    def _collect_due_tasks(self, settings: Settings) -> list[str]:
        """返回所有已到点的任务名，按 (优先级, 逾期最久) 排序。

        与旧的「按数组顺序扫描」相比，这里把"谁先抢到槽"从声明顺序的巧合变成显式规则：
        收藏(P1) → 追更系列(P2) → 其余(P3)，同级里逾期越久越优先，避免长期垫底。
        """
        now = time.time()
        due: list[tuple[int, float, str]] = []
        with self._lock:
            for config in SCHEDULER_TASK_CONFIGS:
                task_name = config["name"]
                if not getattr(settings.sync, config["setting_check"], False):
                    continue
                next_run = self._task_next_run.get(task_name)
                if next_run is None or now < next_run:
                    continue
                priority = int(config.get("priority", SCHEDULER_PRIORITY_DEFAULT))
                # 逾期越久排越前 ⇒ 用负值参与升序排序
                due.append((priority, -(now - next_run), task_name))
        due.sort()
        return [task_name for _priority, _overdue, task_name in due]

    def _highest_due_priority(self, settings: Settings, exclude: str) -> int | None:
        """当前到点任务里除 ``exclude`` 之外的最高优先级（数字最小）。"""
        best: int | None = None
        for task_name in self._collect_due_tasks(settings):
            if task_name == exclude:
                continue
            priority = scheduler_task_priority(task_name)
            if best is None or priority < best:
                best = priority
        return best

    def _may_preempt(self, task_name: str, now: float) -> bool:
        """让位护栏：冷却期内、或连续被让位太多次的任务，本轮必须让它跑完。"""
        if not scheduler_task_is_preemptible(task_name):
            return False
        with self._lock:
            streak = int(self._task_preempt_streak.get(task_name, 0))
            last = self._task_preempted_at.get(task_name)
        if streak >= SCHEDULER_MAX_CONSECUTIVE_PREEMPTIONS:
            return False
        if last is not None and (now - last) < SCHEDULER_PREEMPT_COOLDOWN_SECONDS:
            return False
        return True

    def _note_preemption(self, task_name: str, now: float) -> None:
        with self._lock:
            self._task_preempted_at[task_name] = now
            self._task_preempt_streak[task_name] = (
                int(self._task_preempt_streak.get(task_name, 0)) + 1
            )
            self._pending_preemption.add(task_name)

    def _consume_preemption_flag(self, task_name: str) -> bool:
        """取出并清除"本轮被让位"标记；同时把跑完整轮的任务的连续计数清零。"""
        with self._lock:
            if task_name in self._pending_preemption:
                self._pending_preemption.discard(task_name)
                return True
            self._task_preempt_streak.pop(task_name, None)
            return False

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
                self._current_task_name = task_name
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
            self._run_and_watch_for_preemption(settings, job.job_id, task_name, run_task)
        except Exception as exc:
            logger.error("Auto sync task %s runner failed: %s", task_name, exc)
        finally:
            with self._lock:
                if self._current_task_job_id == job.job_id:
                    self._current_task_job_id = None
                    self._current_task_name = None
        return True

    def _run_and_watch_for_preemption(
        self,
        settings: Settings,
        job_id: str,
        task_name: str,
        run_task: Callable[[str], None],
    ) -> None:
        """在后台线程跑任务，主线程轮询「有没有更高优先级的任务到点了」。

        必须放到独立线程：``run_task`` 是同步阻塞的，调度线程一旦进去就看不见新到点
        的任务，收藏(P1) 只能干等 following_novels/user_backup 跑完（实测 25–140 分钟）。
        不可让位的任务（见 SCHEDULER_TASK_CONFIGS.preemptible）走同一条路径，只是不发
        取消信号——统一实现，避免两套执行流程走样。
        """
        if not scheduler_task_is_preemptible(task_name):
            run_task(job_id)
            return

        error: list[BaseException] = []

        def worker() -> None:
            try:
                run_task(job_id)
            except BaseException as exc:  # noqa: BLE001 - 原样转交给调用方处理
                error.append(exc)

        thread = threading.Thread(
            target=worker, name=f"auto-sync-{task_name}", daemon=True
        )
        thread.start()

        running_priority = scheduler_task_priority(task_name)
        yielded = False
        while thread.is_alive():
            thread.join(timeout=SCHEDULER_PREEMPT_POLL_SECONDS)
            if not thread.is_alive() or yielded or self._stop_event.is_set():
                continue
            try:
                challenger_priority = self._highest_due_priority(settings, task_name)
            except Exception as exc:  # pragma: no cover - 挑选出错不该影响正在跑的任务
                logger.warning("让位判定失败，本轮不打断 %s: %s", task_name, exc)
                continue
            if challenger_priority is None or challenger_priority >= running_priority:
                continue
            now = time.time()
            if not self._may_preempt(task_name, now):
                continue
            self._note_preemption(task_name, now)
            yielded = True
            self._request_yield(job_id, task_name, challenger_priority)

        thread.join()
        if error:
            raise error[0]

    def _request_yield(self, job_id: str, task_name: str, challenger_priority: int) -> None:
        """给正在跑的任务发取消信号，并在它自己的日志里写清"为什么被中断"。

        先 add_log 再 cancel：``_run_shared_web_job`` 在任务终结后才把内存日志刷进
        task_logs，所以这条说明会跟着任务日志一起落库，运维不会看到一条无缘无故的
        "已取消"。
        """
        message = (
            f"为 P{challenger_priority} 高优先级任务让位，本轮提前结束；"
            "已完成的部分已入库，下轮从水位继续"
        )
        job_manager = self.shared_job_manager
        if job_manager is not None and hasattr(job_manager, "add_log"):
            try:
                job_manager.add_log(job_id, "warning", message)
            except Exception as exc:  # pragma: no cover - 日志失败不阻断让位
                logger.warning("写入让位说明失败 %s: %s", job_id, exc)
        cancel_task = self.cancel_task
        if cancel_task is None:
            logger.warning("无法让位：cancel_task 回调不可用 (%s)", task_name)
            return
        try:
            cancel_task(job_id)
        except Exception as exc:
            logger.warning("让位取消失败 %s: %s", job_id, exc)
            return
        logger.info(
            "Task %s preempted by a P%d task (job %s)",
            task_name,
            challenger_priority,
            job_id,
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

    def save_sync_settings(
        self, payload: dict[str, Any], section: str | None = None
    ) -> dict[str, Any]:
        if not self.config_path:
            raise ValueError("缺少 config_path，无法保存设置")

        if section is not None:
            allowed = SETTINGS_SECTIONS.get(section)
            if allowed is None:
                raise ValueError(f"未知的设置分区: {section!r}")
            # 只保留本区字段。下面整段逻辑都是 payload.get(k, 既有值)，
            # 所以被过滤掉的键会自动沿用 YAML 里的旧值，而不是被写成默认值。
            payload = {key: value for key, value in payload.items() if key in allowed}

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
        sync_data["bookmark_max_pages_per_run"] = _normalize_optional_int(
            payload.get("bookmark_max_pages_per_run", sync_data.get("bookmark_max_pages_per_run"))
        )
        # 留空 = 跟随通用上限/不限；_normalize_optional_int 会拒绝 0 与负数，
        # 避免把"每作者 0 篇"这种等于关掉同步的值写进配置。
        sync_data["following_max_novels_per_author"] = _normalize_optional_int(
            payload.get("following_max_novels_per_author", sync_data.get("following_max_novels_per_author"))
        )
        sync_data["series_max_pages_per_run"] = _normalize_optional_int(
            payload.get("series_max_pages_per_run", sync_data.get("series_max_pages_per_run"))
        )
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
        sync_data["auto_sync_preference_analyze_interval_hours"] = _save_int("auto_sync_preference_analyze_interval_hours", 12)
        sync_data["auto_sync_preference_analyze_cron"] = _save_cron("auto_sync_preference_analyze_cron", "15 7,19 * * *")
        sync_data["preference_analyze_batch_size"] = _save_int("preference_analyze_batch_size", 200, min_value=10)
        sync_data["auto_sync_recommendation_run_enabled"] = bool(payload.get("auto_sync_recommendation_run_enabled", sync_data.get("auto_sync_recommendation_run_enabled", False)))
        sync_data["auto_sync_recommendation_run_interval_hours"] = _save_int("auto_sync_recommendation_run_interval_hours", 24)
        # 回落默认值必须与 SyncSettings 的默认值一致：这里若回落成空串，"什么都没改就点
        # 保存"会把新默认 cron 写成空，任务悄悄退回按 interval 跑。
        sync_data["auto_sync_recommendation_run_cron"] = _save_cron("auto_sync_recommendation_run_cron", "50 8 * * *")
        sync_data["pending_deletion_grace_period_days"] = _save_int("pending_deletion_grace_period_days", 30)
        sync_data["pending_deletion_cleanup_confirmed_days"] = _save_int("pending_deletion_cleanup_confirmed_days", 7)

        _atomic_write_yaml(config_path, config_data)

        self.invalidate()
        return _settings_to_dict(load_settings(config_path, None))

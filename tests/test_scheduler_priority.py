"""定时任务优先级与让位机制测试。

背景（2026-08-27 生产实测）：调度器过去按 SCHEDULER_TASK_CONFIGS 的**数组顺序**扫描，
谁排在前面谁先抢那个唯一的 job 槽，「收藏优先」只是声明顺序的巧合而不是设计。更要紧的
是 run_task 同步阻塞调度线程：following_novels 单轮 25–56 分钟、user_backup 最长 2.3
小时，期间收藏(P1) 即使到点也只能按固定 300 秒退避干等。

现在的规则：
- 每个任务有 priority（收藏 P1、追更系列 P2、其余 P3）与 preemptible（能否让位）。
- 每轮收集所有到点任务，按 (priority, 逾期最久) 排序，只提交第一个。
- 正在跑的可让位任务遇到更高优先级任务到点时收到取消信号，下轮从水位续跑。
- 护栏：冷却期 + 连续让位上限，避免 P1 频繁到点把长任务永久饿死。
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.web.managers import (
    SCHEDULER_MAX_CONSECUTIVE_PREEMPTIONS,
    SCHEDULER_PREEMPT_COOLDOWN_SECONDS,
    SCHEDULER_PREEMPT_RETRY_SECONDS,
    SCHEDULER_SUBMIT_RETRY_SECONDS,
    SCHEDULER_TASK_CONFIGS,
    AutoSyncScheduler,
    scheduler_retry_seconds,
    scheduler_task_is_preemptible,
    scheduler_task_priority,
)


class _SyncSettingsStub:
    def __init__(self, **overrides: object) -> None:
        self.auto_sync_enabled = True
        self.auto_sync_timezone = "UTC"
        self.__dict__.update(overrides)

    def __getattr__(self, name: str) -> object:
        if name.endswith("_enabled"):
            return False
        if name.endswith("_interval_hours"):
            return 6
        if name.endswith("_cron"):
            return ""
        raise AttributeError(name)


def _settings(**sync_overrides: object) -> SimpleNamespace:
    return SimpleNamespace(sync=_SyncSettingsStub(**sync_overrides))


ALL_ENABLED = {
    f"auto_sync_{name}_enabled": True
    for name in (
        "bookmarks",
        "following_list",
        "following_novels",
        "subscribed_series",
        "user_status",
        "novel_status",
        "series_status",
        "user_backup",
        "pending_detection",
    )
}


# ── 优先级声明 ──────────────────────────────────────────────────


def test_user_priority_mapping_matches_declared_config() -> None:
    """用户口径：收藏 P1、追更系列 P2、其余 P3。"""
    assert scheduler_task_priority("bookmarks") == 1
    assert scheduler_task_priority("subscribed_series") == 2
    for config in SCHEDULER_TASK_CONFIGS:
        name = config["name"]
        if name in ("bookmarks", "subscribed_series"):
            continue
        assert scheduler_task_priority(name) == 3, name


def test_every_task_declares_priority_and_preemptible() -> None:
    """新增 task_type 时别漏字段——漏了会静默落到 P3/不可让位。"""
    for config in SCHEDULER_TASK_CONFIGS:
        assert "priority" in config, config["name"]
        assert "preemptible" in config, config["name"]


def test_only_watermark_rotating_tasks_are_preemptible() -> None:
    """能让位的必须是「下轮能从水位接着跑」的任务，否则打断等于白跑一轮。"""
    assert scheduler_task_is_preemptible("following_novels") is True
    assert scheduler_task_is_preemptible("novel_status") is True
    assert scheduler_task_is_preemptible("user_status") is True
    assert scheduler_task_is_preemptible("series_status") is True
    assert scheduler_task_is_preemptible("user_backup") is True
    # 每轮从 watchlist 头部重走，不是水位式续跑
    assert scheduler_task_is_preemptible("subscribed_series") is False
    # 只跑几十秒到几分钟，没必要打断
    assert scheduler_task_is_preemptible("bookmarks") is False
    assert scheduler_task_is_preemptible("following_list") is False
    assert scheduler_task_is_preemptible("pending_deletion_detection") is False
    # 跑一半的推荐结果没有意义
    assert scheduler_task_is_preemptible("recommendation_run") is False


def test_retry_backoff_is_shorter_for_higher_priority() -> None:
    """槽被占用时，越重要的任务重试得越勤。"""
    assert scheduler_retry_seconds("bookmarks") < scheduler_retry_seconds("subscribed_series")
    assert scheduler_retry_seconds("subscribed_series") < SCHEDULER_SUBMIT_RETRY_SECONDS
    assert scheduler_retry_seconds("novel_status") == SCHEDULER_SUBMIT_RETRY_SECONDS


# ── 到点任务排序 ────────────────────────────────────────────────


def test_due_tasks_sorted_by_priority_then_overdue() -> None:
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    now = time.time()
    scheduler._task_next_run.update(
        {
            "novel_status": now - 10_000,  # P3，逾期最久
            "user_backup": now - 5_000,  # P3
            "subscribed_series": now - 10,  # P2
            "bookmarks": now - 1,  # P1，逾期最短
        }
    )

    due = scheduler._collect_due_tasks(_settings(**ALL_ENABLED))

    assert due == ["bookmarks", "subscribed_series", "novel_status", "user_backup"]


def test_due_tasks_exclude_disabled_and_future_tasks() -> None:
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    now = time.time()
    scheduler._task_next_run.update(
        {
            "bookmarks": now - 10,
            "novel_status": now + 10_000,  # 还没到点
            "user_backup": now - 10,  # 未启用
        }
    )
    settings = _settings(
        auto_sync_bookmarks_enabled=True,
        auto_sync_novel_status_enabled=True,
    )

    assert scheduler._collect_due_tasks(settings) == ["bookmarks"]


def test_highest_due_priority_ignores_the_running_task() -> None:
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    now = time.time()
    scheduler._task_next_run.update({"novel_status": now - 10, "bookmarks": now - 5})
    settings = _settings(**ALL_ENABLED)

    # novel_status 自己在跑，挑战者只剩 bookmarks(P1)
    assert scheduler._highest_due_priority(settings, exclude="novel_status") == 1
    # 反过来 bookmarks 在跑时，挑战者只剩 novel_status(P3)
    assert scheduler._highest_due_priority(settings, exclude="bookmarks") == 3


def test_highest_due_priority_is_none_when_nothing_else_is_due() -> None:
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    scheduler._task_next_run.update({"novel_status": time.time() - 10})

    settings = _settings(**ALL_ENABLED)
    assert scheduler._highest_due_priority(settings, exclude="novel_status") is None


# ── 让位护栏 ────────────────────────────────────────────────────


def test_non_preemptible_task_never_yields() -> None:
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    assert scheduler._may_preempt("subscribed_series", time.time()) is False


def test_may_preempt_allows_first_yield() -> None:
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    assert scheduler._may_preempt("novel_status", time.time()) is True


def test_cooldown_blocks_repeated_yields() -> None:
    """刚让过位的任务在冷却期内必须能跑完，否则 P1 每 4 小时到点就把它永久打断。"""
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    now = time.time()
    scheduler._note_preemption("novel_status", now)

    assert scheduler._may_preempt("novel_status", now + 60) is False
    # 冷却期过后恢复可让位（同时清掉连续计数，模拟中间跑完过一轮）
    scheduler._task_preempt_streak.clear()
    assert (
        scheduler._may_preempt("novel_status", now + SCHEDULER_PREEMPT_COOLDOWN_SECONDS + 1)
        is True
    )


def test_consecutive_yield_limit_forces_a_full_round() -> None:
    """连续被让位到上限后，必须让它完整跑一轮，防止彻底饿死。"""
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    base = time.time()
    for index in range(SCHEDULER_MAX_CONSECUTIVE_PREEMPTIONS):
        # 每次都跳过冷却期，单独验证"连续次数"这一条护栏
        scheduler._note_preemption("novel_status", base + index)
    later = base + SCHEDULER_PREEMPT_COOLDOWN_SECONDS * 10

    assert scheduler._may_preempt("novel_status", later) is False

    # 完整跑完一轮（没有让位标记）→ 计数清零 → 又可以让位了
    assert scheduler._consume_preemption_flag("novel_status") is True  # 消费掉最后一次标记
    assert scheduler._consume_preemption_flag("novel_status") is False  # 这次代表跑完了
    assert scheduler._may_preempt("novel_status", later) is True


def test_consume_preemption_flag_is_single_shot() -> None:
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    scheduler._note_preemption("user_backup", time.time())

    assert scheduler._consume_preemption_flag("user_backup") is True
    assert scheduler._consume_preemption_flag("user_backup") is False


# ── 让位端到端 ──────────────────────────────────────────────────


class _FakeJob:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id


class _RecordingJobManager:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str, str]] = []

    def add_log(self, job_id: str, level: str, message: str) -> None:
        self.logs.append((job_id, level, message))


def _preemption_scheduler(
    settings: SimpleNamespace,
    run_body,
) -> tuple[AutoSyncScheduler, _RecordingJobManager, list[str]]:
    cancelled: list[str] = []
    job_manager = _RecordingJobManager()
    scheduler = AutoSyncScheduler(
        config_path=None,
        env_path=None,
        shared_job_manager=job_manager,
        submit_task=lambda _settings, task_name: _FakeJob(f"job-{task_name}"),
        run_task=run_body,
        get_task=lambda job_id: None,
        cancel_task=lambda job_id: (cancelled.append(job_id), True)[1],
    )
    return scheduler, job_manager, cancelled


def test_running_task_yields_to_higher_priority_task() -> None:
    """核心行为：长任务跑到一半，收藏(P1) 到点 → 长任务收到取消信号并记下原因。"""
    settings = _settings(**ALL_ENABLED)
    stop_seen = threading.Event()

    def run_body(job_id: str) -> None:
        # 模拟协作式取消：等到被取消（或超时）才返回
        stop_seen.wait(timeout=10)

    scheduler, job_manager, cancelled = _preemption_scheduler(settings, run_body)
    # bookmarks 已到点，novel_status 正在跑
    scheduler._task_next_run["bookmarks"] = time.time() - 1

    def watch() -> None:
        scheduler._run_and_watch_for_preemption(
            settings, "job-novel_status", "novel_status", run_body
        )

    # cancel_task 被调用后放行 run_body，模拟任务响应取消
    original_cancel = scheduler.cancel_task

    def cancel_and_release(job_id: str) -> bool:
        result = original_cancel(job_id)
        stop_seen.set()
        return result

    scheduler.cancel_task = cancel_and_release

    worker = threading.Thread(target=watch, daemon=True)
    worker.start()
    worker.join(timeout=30)

    assert not worker.is_alive()
    assert cancelled == ["job-novel_status"]
    assert scheduler._consume_preemption_flag("novel_status") is True
    # 让位原因必须写进任务日志，否则运维只看到一条无缘无故的"已取消"
    assert any("让位" in message for _job, level, message in job_manager.logs if level == "warning")


def test_running_task_is_not_disturbed_when_only_same_priority_is_due() -> None:
    settings = _settings(**ALL_ENABLED)

    def run_body(job_id: str) -> None:
        time.sleep(0.2)

    scheduler, _job_manager, cancelled = _preemption_scheduler(settings, run_body)
    # 同为 P3 的 user_backup 到点，不构成让位理由
    scheduler._task_next_run["user_backup"] = time.time() - 1

    scheduler._run_and_watch_for_preemption(
        settings, "job-novel_status", "novel_status", run_body
    )

    assert cancelled == []
    assert scheduler._consume_preemption_flag("novel_status") is False


def test_non_preemptible_task_runs_inline_without_watcher() -> None:
    """不可让位的任务直接同步跑完，即使有 P1 到点也不发取消。"""
    settings = _settings(**ALL_ENABLED)
    ran: list[str] = []

    def run_body(job_id: str) -> None:
        ran.append(job_id)

    scheduler, _job_manager, cancelled = _preemption_scheduler(settings, run_body)
    scheduler._task_next_run["bookmarks"] = time.time() - 1

    scheduler._run_and_watch_for_preemption(
        settings, "job-subscribed_series", "subscribed_series", run_body
    )

    assert ran == ["job-subscribed_series"]
    assert cancelled == []


def test_worker_exception_propagates_to_caller() -> None:
    """后台线程里的异常不能被吞掉，否则任务失败会静默变成"跑完了"。"""
    settings = _settings(**ALL_ENABLED)

    def run_body(job_id: str) -> None:
        raise RuntimeError("boom")

    scheduler, _job_manager, _cancelled = _preemption_scheduler(settings, run_body)

    with pytest.raises(RuntimeError, match="boom"):
        scheduler._run_and_watch_for_preemption(
            settings, "job-novel_status", "novel_status", run_body
        )


def test_preempted_task_retries_soon_instead_of_skipping_a_period() -> None:
    """让位后必须短退避续跑，顺延整个周期等于白白跳过一轮。"""
    assert SCHEDULER_PREEMPT_RETRY_SECONDS < 6 * 3600


# ── 三处注册表必须与 SCHEDULER_TASK_CONFIGS 保持齐全 ──────────────────
#
# 漏注册全都是**静默**失败，不会抛异常也不会红：
# - web/utils.py:_job_spec 缺分支 → JobSpec 落到 JobType.SYNC，任务统计归错类；
# - jobs/tasks.py:execute_task 缺分支 → 只有真跑到那一轮才抛 RuntimeError；
# - jobs/tasks.py:_TASK_LABELS 缺条目 → job 内部日志显示英文键名；
# - web/managers.py:TASK_LABELS 缺条目 → 任务日志页显示英文键名。
# 2026-08-28 已逐项核对 11 个任务全部齐全，下面的断言是防后续新增任务时回归。
# 新增 task_type 的完整清单见 docs/JOB_SYSTEM.md §4。

# 调度器任务名 → `_scheduler_job_spec` 归一化后的内部 task_type。
# 两者不同名的只有这两个（见 web/utils.py:_scheduler_job_spec 与
# web/managers.py:SCHEDULER_TASK_TYPE_ALIASES，两处必须同一套映射）。
EXPECTED_SCHEDULER_TASKS = {
    "bookmarks": "bookmark",
    "subscribed_series": "subscribed_series",
    "following_list": "following_users",
    "following_novels": "following_novels",
    "user_status": "user_status",
    "novel_status": "novel_status",
    "series_status": "series_status",
    "user_backup": "user_backup",
    "pending_deletion_detection": "pending_deletion_detection",
    "preference_analyze": "preference_analyze",
    "recommendation_run": "recommendation_run",
}


def test_scheduler_task_roster_is_exactly_the_known_eleven() -> None:
    """任务清单变化必须是显式动作。

    新增/删除定时任务时这条会红——这正是目的：改这个集合前先按
    docs/JOB_SYSTEM.md §4 把四处注册表（dispatch、两个标签字典、_job_spec 分支）
    和 SyncSettings 三件套一起补齐，别只加 SCHEDULER_TASK_CONFIGS 一处。
    """
    assert {config["name"] for config in SCHEDULER_TASK_CONFIGS} == set(EXPECTED_SCHEDULER_TASKS)
    # 名字不能重复：SCHEDULER_TASK_CONFIG_BY_NAME 是按名字建索引的，重名会静默丢一条
    names = [config["name"] for config in SCHEDULER_TASK_CONFIGS]
    assert len(names) == len(set(names)) == 11

def test_every_scheduler_task_is_registered_in_all_three_tables() -> None:
    """每个定时任务在三处独立注册表 + 分派器里都必须齐全。"""
    import inspect

    from pixiv_novel_sync.jobs.tasks import _TASK_LABELS, execute_task
    from pixiv_novel_sync.web.managers import TASK_LABELS
    from pixiv_novel_sync.web.utils import _scheduler_job_spec

    dispatch_source = inspect.getsource(execute_task)

    for config in SCHEDULER_TASK_CONFIGS:
        name = config["name"]

        # 1) JobSpec 能构造出来，且归一化后的 task_type 与预期一致
        spec = _scheduler_job_spec(name)
        assert spec.task_types, f"{name} 构造出空 task_types"
        internal = spec.task_types[0]
        assert internal == EXPECTED_SCHEDULER_TASKS[name], (
            f"{name} 归一化成了 {internal!r}，与 SCHEDULER_TASK_TYPE_ALIASES 不一致"
        )

        # 2) 调度器名 → 中文标签（任务日志页用；webapp 允许回落到归一化名）
        assert TASK_LABELS.get(name) or TASK_LABELS.get(internal), (
            f"web/managers.py:TASK_LABELS 缺少 {name}"
        )

        # 3) 内部 task_type → 中文标签（job 内部日志用）
        assert internal in _TASK_LABELS, f"jobs/tasks.py:_TASK_LABELS 缺少 {internal}"

        # 4) execute_task 里有对应分派分支。缺了不会在导入期报错，只会等到真跑那一轮
        #    才抛 RuntimeError("Unsupported task type ...")，所以这里查源码字面量。
        assert f'"{internal}"' in dispatch_source, (
            f"jobs/tasks.py:execute_task 没有 {internal!r} 的分派分支"
        )


def test_scheduler_tasks_map_to_their_own_job_type() -> None:
    """job_type 映射漏分支不报错，只是静默落到 JobType.SYNC，任务统计归错类。"""
    from pixiv_novel_sync.jobs.models import JobType
    from pixiv_novel_sync.web.utils import _scheduler_job_spec

    expected = {
        # 四个同步类任务落到 SYNC 是设计意图，不是漏分支
        "bookmarks": JobType.SYNC,
        "subscribed_series": JobType.SYNC,
        "following_list": JobType.SYNC,
        "following_novels": JobType.SYNC,
        # 三个状态检查是 _job_spec 里最容易漏的一类分支
        "user_status": JobType.STATUS_CHECK,
        "novel_status": JobType.STATUS_CHECK,
        "series_status": JobType.STATUS_CHECK,
        # 其余各有专属 job_type
        "user_backup": JobType.USER_BACKUP,
        "pending_deletion_detection": JobType.PENDING_DELETION_DETECTION,
        "preference_analyze": JobType.PREFERENCE_ANALYZE,
        "recommendation_run": JobType.RECOMMENDATION_RUN,
    }
    assert set(expected) == set(EXPECTED_SCHEDULER_TASKS)

    for name, job_type in expected.items():
        assert _scheduler_job_spec(name).job_type == job_type, name

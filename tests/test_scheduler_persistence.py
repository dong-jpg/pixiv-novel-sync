"""定时调度器的重启补偿测试。

背景（生产实证）：进程重启后 AutoSyncScheduler 的 _task_next_run 是空的，
旧逻辑把每个任务排到「现在 + 完整间隔」，于是每次部署都白白顺延一个周期
（12 小时的收藏同步实测变成 15-17 小时）。

修复方式：启动时从 task_logs 读取每个任务最近一次成功完成的时间，
按「上次完成时间 + 间隔」计算下次运行；已经错过窗口的任务错峰补偿，避免惊群。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.web.managers import (
    SCHEDULER_STARTUP_GRACE_SECONDS,
    SCHEDULER_STARTUP_STAGGER_SECONDS,
    SCHEDULER_TASK_CONFIGS,
    AutoSyncScheduler,
    scheduler_task_log_type,
)


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------

class _SyncSettingsStub:
    """只提供调度器读取的 auto_sync_* 字段。

    未显式指定的任务一律视为「未启用」，间隔默认 6 小时，cron 默认空，
    这样单个测试只需要声明它关心的那几个任务。
    """

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


def _make_settings(db_path: Path, **sync_overrides: object) -> SimpleNamespace:
    return SimpleNamespace(
        storage=SimpleNamespace(db_path=db_path),
        sync=_SyncSettingsStub(**sync_overrides),
    )


def _utc_text(delta_hours: float) -> str:
    """生成 task_logs 使用的 UTC 时间文本（SQLite datetime('now') 格式）。"""
    moment = datetime.now(timezone.utc) - timedelta(hours=delta_hours)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _insert_task_log(
    db: Database,
    task_type: str,
    status: str,
    started_hours_ago: float,
    finished_hours_ago: float | None,
) -> None:
    """直接写一条历史日志（create_task_log 只能写"现在"，测试需要回溯时间）。"""
    db.conn.execute(
        """
        INSERT INTO task_logs (task_type, task_name, job_id, status, started_at, finished_at, is_auto_sync)
        VALUES (?, ?, NULL, ?, ?, ?, 1)
        """,
        (
            task_type,
            task_type,
            status,
            _utc_text(started_hours_ago),
            None if finished_hours_ago is None else _utc_text(finished_hours_ago),
        ),
    )
    db.conn.commit()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "scheduler_state.db"
    db = Database(path)
    db.init_schema()
    db.close()
    return path


class _OneShotStopEvent(threading.Event):
    """让 _run_scheduler_loop 只跑一轮：第一次 wait() 直接置位并返回。"""

    def wait(self, timeout: float | None = None) -> bool:  # type: ignore[override]
        self.set()
        return True


def _run_one_loop_pass(
    scheduler: AutoSyncScheduler,
    settings: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pixiv_novel_sync.web.managers.load_settings", lambda *args, **kwargs: settings
    )
    scheduler._catalog_initialization_attempted = True  # 跳过救援目录初始化
    scheduler._last_cleanup_time = float("inf")  # 跳过日志清理
    scheduler._run_scheduler_loop(_OneShotStopEvent())


# ---------------------------------------------------------------------------
# 1. 有历史成功记录：按「上次完成时间 + 间隔」恢复
# ---------------------------------------------------------------------------

def test_restore_uses_last_success_from_task_logs(db_path: Path) -> None:
    db = Database(db_path)
    db.init_schema()
    _insert_task_log(db, "bookmark", "succeeded", started_hours_ago=5.1, finished_hours_ago=5)
    db.close()

    settings = _make_settings(
        db_path,
        auto_sync_bookmarks_enabled=True,
        auto_sync_bookmarks_interval_hours=12,
    )
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    before = datetime.now(timezone.utc).timestamp()
    scheduler._restore_schedule_from_task_logs(settings)

    status = scheduler.get_status()
    last_run = status["task_last_run"]["bookmarks"]
    next_run = status["task_next_run"]["bookmarks"]

    # 上次完成时间 ≈ 5 小时前
    assert abs((before - last_run) - 5 * 3600) < 120
    # 下次运行 = 上次完成 + 12 小时 ≈ 7 小时后，而不是「现在 + 12 小时」
    assert abs(next_run - (last_run + 12 * 3600)) < 2
    assert abs(next_run - (before + 12 * 3600)) > 3600


def test_restore_skips_failed_and_running_logs(db_path: Path) -> None:
    db = Database(db_path)
    db.init_schema()
    _insert_task_log(db, "bookmark", "succeeded", started_hours_ago=5.1, finished_hours_ago=5)
    _insert_task_log(db, "bookmark", "failed", started_hours_ago=2, finished_hours_ago=2)
    _insert_task_log(db, "bookmark", "running", started_hours_ago=0.1, finished_hours_ago=None)
    db.close()

    settings = _make_settings(
        db_path,
        auto_sync_bookmarks_enabled=True,
        auto_sync_bookmarks_interval_hours=12,
    )
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    now = datetime.now(timezone.utc).timestamp()
    scheduler._restore_schedule_from_task_logs(settings)

    # 只认成功记录：仍然是 5 小时前那条
    assert abs((now - scheduler.get_status()["task_last_run"]["bookmarks"]) - 5 * 3600) < 120


def test_restore_counts_partial_runs_as_ran(db_path: Path) -> None:
    """partial（熔断中止/本轮没跑完）也算「跑过了」，不能让重启白白顺延一个周期。

    关注小说的轮转模式下每轮都会置 incomplete → task_logs 记 partial；若排程只认
    succeeded，这类任务将永远恢复不出上次运行时间。
    """
    db = Database(db_path)
    db.init_schema()
    _insert_task_log(db, "bookmark", "succeeded", started_hours_ago=9.1, finished_hours_ago=9)
    _insert_task_log(db, "bookmark", "partial", started_hours_ago=5.1, finished_hours_ago=5)
    db.close()

    settings = _make_settings(
        db_path,
        auto_sync_bookmarks_enabled=True,
        auto_sync_bookmarks_interval_hours=12,
    )
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    now = datetime.now(timezone.utc).timestamp()
    scheduler._restore_schedule_from_task_logs(settings)

    # 取更近的 partial 那条（5 小时前），而不是回退到 9 小时前的 succeeded
    assert abs((now - scheduler.get_status()["task_last_run"]["bookmarks"]) - 5 * 3600) < 120


def test_scheduler_loop_keeps_history_based_next_run(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端：主循环里恢复生效，且不会被「现在 + 间隔」覆盖，也不会立刻触发。"""
    db = Database(db_path)
    db.init_schema()
    _insert_task_log(db, "bookmark", "succeeded", started_hours_ago=5.1, finished_hours_ago=5)
    db.close()

    settings = _make_settings(
        db_path,
        auto_sync_bookmarks_enabled=True,
        auto_sync_bookmarks_interval_hours=12,
    )
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    submitted: list[str] = []
    scheduler.submit_task = lambda s, task: submitted.append(task) or None
    scheduler.run_task = lambda job_id: None

    now = datetime.now(timezone.utc).timestamp()
    _run_one_loop_pass(scheduler, settings, monkeypatch)

    next_run = scheduler.get_status()["task_next_run"]["bookmarks"]
    # ≈ 7 小时后（5 小时前完成 + 12 小时间隔），而不是 12 小时后
    assert abs(next_run - (now + 7 * 3600)) < 120
    assert submitted == []


def test_restore_maps_legacy_scheduler_names_to_task_log_types(db_path: Path) -> None:
    """调度器叫 following_list，task_logs 里记的是 following_users。"""
    db = Database(db_path)
    db.init_schema()
    _insert_task_log(db, "following_users", "succeeded", started_hours_ago=3.1, finished_hours_ago=3)
    db.close()

    settings = _make_settings(
        db_path,
        auto_sync_following_list_enabled=True,
        auto_sync_following_list_interval_hours=24,
    )
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    scheduler._restore_schedule_from_task_logs(settings)

    assert "following_list" in scheduler.get_status()["task_next_run"]


def test_scheduler_task_log_type_matches_job_spec_normalization() -> None:
    """别名表必须与 webapp 写入 task_logs 时的归一化保持一致，否则查不到历史。"""
    from pixiv_novel_sync.web.utils import _scheduler_job_spec

    for task_config in SCHEDULER_TASK_CONFIGS:
        task_name = task_config["name"]
        assert scheduler_task_log_type(task_name) == _scheduler_job_spec(task_name).task_types[0]


# ---------------------------------------------------------------------------
# 2. 无历史记录：回退「现在 + 间隔」
# ---------------------------------------------------------------------------

def test_restore_without_history_keeps_now_plus_interval(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(
        db_path,
        auto_sync_bookmarks_enabled=True,
        auto_sync_bookmarks_interval_hours=12,
    )
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    scheduler._restore_schedule_from_task_logs(settings)

    # 首次部署：不写入任何恢复值
    assert scheduler.get_status()["task_next_run"] == {}

    submitted: list[str] = []
    scheduler.submit_task = lambda s, task: submitted.append(task) or None
    scheduler.run_task = lambda job_id: None
    now = datetime.now(timezone.utc).timestamp()
    _run_one_loop_pass(scheduler, settings, monkeypatch)

    next_run = scheduler.get_status()["task_next_run"]["bookmarks"]
    assert abs(next_run - (now + 12 * 3600)) < 120
    assert submitted == []


# ---------------------------------------------------------------------------
# 3. 历史记录已过期：错峰补偿，不能一起立刻触发
# ---------------------------------------------------------------------------

def _overdue_settings(db_path: Path) -> SimpleNamespace:
    return _make_settings(
        db_path,
        auto_sync_bookmarks_enabled=True,
        auto_sync_bookmarks_interval_hours=12,
        auto_sync_following_novels_enabled=True,
        auto_sync_following_novels_interval_hours=6,
        auto_sync_subscribed_series_enabled=True,
        auto_sync_subscribed_series_interval_hours=6,
    )


def _seed_overdue_history(db_path: Path) -> None:
    db = Database(db_path)
    db.init_schema()
    for task_type in ("bookmark", "following_novels", "subscribed_series"):
        # 48 小时前完成，远超各自间隔 → 全部逾期
        _insert_task_log(db, task_type, "succeeded", started_hours_ago=48.5, finished_hours_ago=48)
    db.close()


def test_overdue_tasks_are_staggered_instead_of_firing_together(db_path: Path) -> None:
    _seed_overdue_history(db_path)
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    now = datetime.now(timezone.utc).timestamp()
    scheduler._restore_schedule_from_task_logs(_overdue_settings(db_path))

    next_runs = scheduler.get_status()["task_next_run"]
    assert set(next_runs) == {"bookmarks", "following_novels", "subscribed_series"}
    # 没有任何任务被排到「立刻执行」
    assert all(value > now + 1 for value in next_runs.values())
    # 彼此错开，且都落在启动宽限 + 错峰窗口内
    ordered = sorted(next_runs.values())
    assert abs(ordered[0] - (now + SCHEDULER_STARTUP_GRACE_SECONDS)) < 5
    for earlier, later in zip(ordered, ordered[1:]):
        assert abs((later - earlier) - SCHEDULER_STARTUP_STAGGER_SECONDS) < 5


def test_overdue_tasks_do_not_all_submit_on_first_loop_pass(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """若恢复时不做错峰，这三个逾期任务会在重启后同一轮里连着提交。"""
    _seed_overdue_history(db_path)
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    submitted: list[str] = []
    scheduler.submit_task = lambda s, task: submitted.append(task) or None
    scheduler.run_task = lambda job_id: None

    _run_one_loop_pass(scheduler, _overdue_settings(db_path), monkeypatch)

    # 重启瞬间一个都不该触发（惊群防护），全部落到启动宽限之后
    assert submitted == []


def test_restore_runs_only_once_per_start(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_overdue_history(db_path)
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    scheduler.submit_task = lambda s, task: None
    scheduler.run_task = lambda job_id: None
    settings = _overdue_settings(db_path)

    calls: list[int] = []
    original = AutoSyncScheduler._restore_schedule_from_task_logs

    def counting_restore(self, current_settings):  # type: ignore[no-untyped-def]
        calls.append(1)
        return original(self, current_settings)

    monkeypatch.setattr(AutoSyncScheduler, "_restore_schedule_from_task_logs", counting_restore)

    _run_one_loop_pass(scheduler, settings, monkeypatch)
    assert scheduler._schedule_restored is True
    _run_one_loop_pass(scheduler, settings, monkeypatch)
    assert calls == [1]


# ---------------------------------------------------------------------------
# 4. cron 驱动的任务不受影响
# ---------------------------------------------------------------------------

def test_cron_tasks_are_not_restored_from_history(db_path: Path) -> None:
    db = Database(db_path)
    db.init_schema()
    _insert_task_log(db, "bookmark", "succeeded", started_hours_ago=48.5, finished_hours_ago=48)
    db.close()

    settings = _make_settings(
        db_path,
        auto_sync_bookmarks_enabled=True,
        auto_sync_bookmarks_interval_hours=12,
        auto_sync_bookmarks_cron="0 3 * * *",
    )
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    scheduler._restore_schedule_from_task_logs(settings)

    status = scheduler.get_status()
    assert status["task_next_run"] == {}
    assert status["task_last_run"] == {}


def test_cron_task_next_run_still_follows_cron_expression(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pixiv_novel_sync.settings import cron_to_next_run

    db = Database(db_path)
    db.init_schema()
    _insert_task_log(db, "bookmark", "succeeded", started_hours_ago=48.5, finished_hours_ago=48)
    db.close()

    settings = _make_settings(
        db_path,
        auto_sync_bookmarks_enabled=True,
        auto_sync_bookmarks_interval_hours=12,
        auto_sync_bookmarks_cron="0 3 * * *",
    )
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    submitted: list[str] = []
    scheduler.submit_task = lambda s, task: submitted.append(task) or None
    scheduler.run_task = lambda job_id: None

    now = datetime.now(timezone.utc).timestamp()
    _run_one_loop_pass(scheduler, settings, monkeypatch)

    expected = cron_to_next_run("0 3 * * *", now, "UTC")
    assert expected is not None
    assert abs(scheduler.get_status()["task_next_run"]["bookmarks"] - expected) < 120
    assert submitted == []

"""定时推荐任务（recommendation_run）的调度接线测试。

背景：`recommendation_run` 早先只有 `JobType.RECOMMENDATION_RUN` 和
`jobs/tasks.py:_TASK_LABELS` 里的中文标签，却不在 `SCHEDULER_TASK_CONFIGS` 中，
`SyncSettings` 也没有对应的 auto_sync 三件套——只能由
`POST /api/dashboard/recommendations/run` 手动触发。

本文件锁定补全后的完整链路：settings 字段 → 调度清单 → JobSpec 的 job_type
推断 → 中文标签 → 保存校验 → 设置页可编辑。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.jobs.models import JobSource, JobType
from pixiv_novel_sync.jobs.tasks import task_label
from pixiv_novel_sync.settings import load_settings
from pixiv_novel_sync.web.managers import (
    SCHEDULER_TASK_CONFIGS,
    TASK_LABELS,
    SettingsManager,
    scheduler_task_log_type,
)
from pixiv_novel_sync.web.utils import _scheduler_job_spec, _settings_to_dict


TASK_NAME = "recommendation_run"


# ---------------------------------------------------------------------------
# 1. settings 三件套
# ---------------------------------------------------------------------------

def test_sync_settings_exposes_recommendation_auto_sync_triple(tmp_path: Path):
    """SyncSettings 必须有 enabled / interval_hours / cron 三个字段。"""
    settings = load_settings(None, None)

    assert settings.sync.auto_sync_recommendation_run_enabled is False, (
        "定时推荐默认必须关闭：它会消耗 Pixiv 搜索配额，且依赖已存在的默认偏好画像"
    )
    assert settings.sync.auto_sync_recommendation_run_interval_hours == 24
    assert settings.sync.auto_sync_recommendation_run_cron == ""


def test_recommendation_auto_sync_reads_from_yaml(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "sync:\n"
        "  auto_sync_recommendation_run_enabled: true\n"
        "  auto_sync_recommendation_run_interval_hours: 12\n"
        "  auto_sync_recommendation_run_cron: '0 4 * * *'\n",
        encoding="utf-8",
    )
    settings = load_settings(str(config), None)

    assert settings.sync.auto_sync_recommendation_run_enabled is True
    assert settings.sync.auto_sync_recommendation_run_interval_hours == 12
    assert settings.sync.auto_sync_recommendation_run_cron == "0 4 * * *"


# ---------------------------------------------------------------------------
# 2. 调度清单
# ---------------------------------------------------------------------------

def test_recommendation_run_registered_in_scheduler_configs():
    entry = next((c for c in SCHEDULER_TASK_CONFIGS if c["name"] == TASK_NAME), None)
    assert entry is not None, "recommendation_run 必须出现在 SCHEDULER_TASK_CONFIGS"
    assert entry["setting_check"] == "auto_sync_recommendation_run_enabled"
    assert entry["interval_setting"] == "auto_sync_recommendation_run_interval_hours"
    assert entry["cron_setting"] == "auto_sync_recommendation_run_cron"


def test_scheduler_config_settings_all_resolve_on_real_settings():
    """清单里每个字段名都必须真的存在于 SyncSettings，否则调度会静默取默认值。"""
    sync = load_settings(None, None).sync
    for config in SCHEDULER_TASK_CONFIGS:
        for key in ("setting_check", "interval_setting", "cron_setting"):
            field = config[key]
            assert hasattr(sync, field), f"{config['name']}: SyncSettings 缺少 {field}"


def test_recommendation_task_log_type_needs_no_alias():
    """调度器名与 task_type 同名，别名映射应原样返回。"""
    assert scheduler_task_log_type(TASK_NAME) == TASK_NAME


# ---------------------------------------------------------------------------
# 3. JobSpec 推断与标签
# ---------------------------------------------------------------------------

def test_scheduler_job_spec_infers_recommendation_job_type():
    """不加分支时会落到 JobType.SYNC，任务日志与统计都会归错类。"""
    spec = _scheduler_job_spec(TASK_NAME)

    assert spec.source is JobSource.SCHEDULER
    assert spec.task_types == [TASK_NAME]
    assert spec.job_type is JobType.RECOMMENDATION_RUN


def test_recommendation_run_has_chinese_labels_in_both_dicts():
    """两个标签字典互相独立，定时任务走的是 web/managers.TASK_LABELS。"""
    assert TASK_LABELS.get(TASK_NAME) == "生成推荐"
    assert task_label(TASK_NAME) == "生成推荐"


def test_every_scheduler_task_has_a_web_label():
    """调度器任务名如果没有中文标签，任务日志页会显示英文键名。"""
    for config in SCHEDULER_TASK_CONFIGS:
        name = config["name"]
        assert TASK_LABELS.get(name), f"web/managers.TASK_LABELS 缺少 {name}"


# ---------------------------------------------------------------------------
# 4. 保存与回显
# ---------------------------------------------------------------------------

def _manager(tmp_path: Path) -> SettingsManager:
    config = tmp_path / "config.yaml"
    config.write_text("sync: {}\n", encoding="utf-8")
    return SettingsManager(config_path=str(config))


def test_save_sync_settings_persists_recommendation_triple(tmp_path: Path):
    manager = _manager(tmp_path)
    result = manager.save_sync_settings(
        {
            "auto_sync_recommendation_run_enabled": True,
            "auto_sync_recommendation_run_interval_hours": 8,
            "auto_sync_recommendation_run_cron": "30 5 * * *",
        }
    )

    assert result["auto_sync_recommendation_run_enabled"] is True
    assert result["auto_sync_recommendation_run_interval_hours"] == 8
    assert result["auto_sync_recommendation_run_cron"] == "30 5 * * *"

    reloaded = load_settings(str(tmp_path / "config.yaml"), None)
    assert reloaded.sync.auto_sync_recommendation_run_enabled is True
    assert reloaded.sync.auto_sync_recommendation_run_interval_hours == 8
    assert reloaded.sync.auto_sync_recommendation_run_cron == "30 5 * * *"


def test_save_sync_settings_rejects_bad_recommendation_cron(tmp_path: Path):
    manager = _manager(tmp_path)
    with pytest.raises(ValueError, match="auto_sync_recommendation_run_cron"):
        manager.save_sync_settings(
            {"auto_sync_recommendation_run_cron": "not a cron"}
        )


def test_settings_dict_exposes_recommendation_triple():
    """设置页靠 GET /api/dashboard/settings 回显，字段缺失则输入框绑不上。"""
    payload = _settings_to_dict(load_settings(None, None))

    assert "auto_sync_recommendation_run_enabled" in payload
    assert "auto_sync_recommendation_run_interval_hours" in payload
    assert "auto_sync_recommendation_run_cron" in payload


def test_settings_dict_covers_every_scheduler_task():
    payload = _settings_to_dict(load_settings(None, None))
    for config in SCHEDULER_TASK_CONFIGS:
        for key in ("setting_check", "interval_setting", "cron_setting"):
            field = config[key]
            assert field in payload, f"{config['name']}: 设置接口未回显 {field}"


# ---------------------------------------------------------------------------
# 5. 设置页
# ---------------------------------------------------------------------------

def test_scheduler_tab_lists_recommendation_task():
    template = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "pixiv_novel_sync"
        / "templates"
        / "dashboard_settings.html"
    ).read_text(encoding="utf-8")

    assert "auto_sync_recommendation_run_enabled" in template
    assert "auto_sync_recommendation_run_interval_hours" in template
    assert "auto_sync_recommendation_run_cron" in template


# ---------------------------------------------------------------------------
# 6. 端到端：调度器到点真的会提交
# ---------------------------------------------------------------------------

class _SyncStub:
    """只暴露调度器读取的 auto_sync_* 字段；未声明的任务视为未启用。"""

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


class _BoundedStopEvent:
    """跑满 max_rounds 轮 wait() 就自动置位。

    `_run_scheduler_loop` 只在 stop_event 置位时退出。若被测任务没有注册，
    循环会一直空转——测试必须**失败**而不是挂死，所以这里给轮次设硬上限。
    """

    def __init__(self, max_rounds: int = 2) -> None:
        import threading

        self._event = threading.Event()
        self._rounds = 0
        self._max_rounds = max_rounds

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    def wait(self, _timeout: float | None = None) -> bool:
        self._rounds += 1
        if self._rounds >= self._max_rounds:
            self._event.set()
        return self._event.is_set()


def _run_one_scheduler_pass(tmp_path: Path, monkeypatch, sync_stub: _SyncStub) -> list[str]:
    """跑一轮调度循环，返回被提交的任务名列表。"""
    from pixiv_novel_sync.web.managers import AutoSyncScheduler

    settings = SimpleNamespace(
        storage=SimpleNamespace(db_path=tmp_path / "state.db"),
        sync=sync_stub,
    )
    submitted: list[str] = []
    stop_event = _BoundedStopEvent()

    def _submit(_settings, task_name):
        submitted.append(task_name)
        stop_event.set()
        return SimpleNamespace(job_id="job-1")

    scheduler = AutoSyncScheduler(
        config_path=None,
        env_path=None,
        submit_task=_submit,
        run_task=lambda _job_id: None,
    )
    scheduler._stop_event = stop_event  # type: ignore[assignment]
    # 让任务立刻到点，并跳过重启补偿与救援目录初始化
    scheduler._task_next_run[TASK_NAME] = 0.0
    scheduler._schedule_restored = True
    scheduler._catalog_initialization_attempted = True

    monkeypatch.setattr(
        "pixiv_novel_sync.web.managers.load_settings",
        lambda *_args, **_kwargs: settings,
    )
    scheduler._run_scheduler_loop(stop_event)  # type: ignore[arg-type]
    return submitted


def test_scheduler_submits_recommendation_run_when_due(tmp_path: Path, monkeypatch):
    """只启用 recommendation_run 时，调度循环必须提交它——而不是跳过。"""
    submitted = _run_one_scheduler_pass(
        tmp_path,
        monkeypatch,
        _SyncStub(
            auto_sync_recommendation_run_enabled=True,
            auto_sync_recommendation_run_interval_hours=24,
        ),
    )
    assert submitted == [TASK_NAME]


def test_scheduler_skips_recommendation_run_when_disabled(tmp_path: Path, monkeypatch):
    """默认关闭时不得提交，否则会白耗 Pixiv 搜索配额。"""
    submitted = _run_one_scheduler_pass(tmp_path, monkeypatch, _SyncStub())
    assert submitted == []

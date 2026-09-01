"""cron_to_next_run 契约测试：解析失败时必须返回 None，绝不向调用方泄漏异常。

回归背景：cron_to_next_run 原先只 catch ImportError，croniter 对畸形表达式抛出的
CroniterBadCronError / CroniterNotAlphaError / CroniterBadDateError 会一路冒泡：
- 保存设置路径（SettingsManager._save_cron）依赖 None 触发友好的 ValueError；
- 调度循环（AutoSyncScheduler）用 `cron_to_next_run(...) or fallback` 处理 None。
异常泄漏会让前者返回 500、后者整轮调度中断。
"""
from __future__ import annotations

from dataclasses import fields
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pixiv_novel_sync.settings import SyncSettings, cron_to_next_run, load_settings
from pixiv_novel_sync.web.managers import SCHEDULER_TASK_CONFIGS, SettingsManager


# 一个固定的基准时间戳（2026-07-06 附近），保证测试可复现。
_BASE = 1783320000.0


@pytest.mark.parametrize(
    "bad_expr",
    [
        "a b c d e",        # 非数字字段 -> CroniterNotAlphaError（构造时）
        "99 99 99",         # 字段数不足且越界
        "0 0 30 2 *",       # 语法合法但 2 月 30 日永不出现 -> CroniterBadDateError（get_next 时）
        "0 0 32 * *",       # 日越界
        "0 25 * * *",       # 小时越界
        "",                 # 空串
        "   ",              # 纯空白
        "0 0 30 2",         # 4 段，非法段数
    ],
)
def test_returns_none_for_malformed_expression(bad_expr: str) -> None:
    # 契约：解析失败一律返回 None，绝不抛异常。
    assert cron_to_next_run(bad_expr, base_time=_BASE) is None


@pytest.mark.parametrize(
    "good_expr",
    [
        "0 9 * * *",        # 每天 9:00
        "*/5 * * * *",      # 每 5 分钟
        "0 0 1 * *",        # 每月 1 日
        "@daily",           # 简化格式
    ],
)
def test_returns_timestamp_for_valid_expression(good_expr: str) -> None:
    result = cron_to_next_run(good_expr, base_time=_BASE)
    assert isinstance(result, float)
    assert result > _BASE  # 下次运行必须晚于基准时间


def test_invalid_timezone_falls_back_without_raising() -> None:
    # 未知时区名不应导致异常泄漏（回退 UTC）。
    result = cron_to_next_run("0 9 * * *", base_time=_BASE, timezone="Not/AZone")
    assert isinstance(result, float)


# ── 阶段二新 cron 排布 ────────────────────────────────────────────────
#
# 生产时区固定 Asia/Seoul（无夏令时，所以下面的挂钟时刻是稳定可断言的）。
# 这些断言不只是"能解析"：调度器对解析失败的处理是**静默回落到 interval**
# （`web/managers.py:_run_scheduler_loop` 用 `cron_to_next_run(...) or fallback`），
# 所以一个写错的表达式不会报错，只会变成"配了 cron 却按小时间隔跑"这种极难发现的
# 偏差。因此必须逐条锁死算出来的下次运行时刻，而不是只断言 not None。

_SEOUL = ZoneInfo("Asia/Seoul")

# spec §5.2 的排布：scheduler 任务名 → cron 表达式
PHASE2_CRONS: dict[str, str] = {
    "bookmarks": "20 0,4,8,12,16,20 * * *",
    "subscribed_series": "40 1,13 * * *",
    "following_list": "30 10 * * *",
    "following_novels": "0 3,9,15,21 * * *",
    "user_status": "30 6 */2 * *",
    "novel_status": "0 5,17 * * *",
    "series_status": "30 18 */2 * *",
    "user_backup": "30 2 */3 * *",
    "pending_deletion_detection": "30 12 * * *",
    "preference_analyze": "15 7,19 * * *",
    "recommendation_run": "50 8 * * *",
}

# 两个基准时刻：00:00 覆盖"当天还没跑过"，19:00 覆盖"当天已跑完、要跨到下一次"，
# 后者才真正验证 */2 与 */3 的日步进（09-01 → 09-03 / 09-04）。
_BASE_MIDNIGHT = datetime(2026, 9, 1, 0, 0, tzinfo=_SEOUL)
_BASE_EVENING = datetime(2026, 9, 1, 19, 0, tzinfo=_SEOUL)

_EXPECTED_NEXT_RUNS: list[tuple[str, datetime, datetime]] = [
    # 从 2026-09-01 00:00 KST 起算
    ("bookmarks", _BASE_MIDNIGHT, datetime(2026, 9, 1, 0, 20, tzinfo=_SEOUL)),
    ("subscribed_series", _BASE_MIDNIGHT, datetime(2026, 9, 1, 1, 40, tzinfo=_SEOUL)),
    ("following_list", _BASE_MIDNIGHT, datetime(2026, 9, 1, 10, 30, tzinfo=_SEOUL)),
    ("following_novels", _BASE_MIDNIGHT, datetime(2026, 9, 1, 3, 0, tzinfo=_SEOUL)),
    ("user_status", _BASE_MIDNIGHT, datetime(2026, 9, 1, 6, 30, tzinfo=_SEOUL)),
    ("novel_status", _BASE_MIDNIGHT, datetime(2026, 9, 1, 5, 0, tzinfo=_SEOUL)),
    ("series_status", _BASE_MIDNIGHT, datetime(2026, 9, 1, 18, 30, tzinfo=_SEOUL)),
    ("user_backup", _BASE_MIDNIGHT, datetime(2026, 9, 1, 2, 30, tzinfo=_SEOUL)),
    ("pending_deletion_detection", _BASE_MIDNIGHT, datetime(2026, 9, 1, 12, 30, tzinfo=_SEOUL)),
    ("preference_analyze", _BASE_MIDNIGHT, datetime(2026, 9, 1, 7, 15, tzinfo=_SEOUL)),
    ("recommendation_run", _BASE_MIDNIGHT, datetime(2026, 9, 1, 8, 50, tzinfo=_SEOUL)),
    # 从 2026-09-01 19:00 KST 起算
    ("bookmarks", _BASE_EVENING, datetime(2026, 9, 1, 20, 20, tzinfo=_SEOUL)),
    ("subscribed_series", _BASE_EVENING, datetime(2026, 9, 2, 1, 40, tzinfo=_SEOUL)),
    ("following_list", _BASE_EVENING, datetime(2026, 9, 2, 10, 30, tzinfo=_SEOUL)),
    ("following_novels", _BASE_EVENING, datetime(2026, 9, 1, 21, 0, tzinfo=_SEOUL)),
    ("user_status", _BASE_EVENING, datetime(2026, 9, 3, 6, 30, tzinfo=_SEOUL)),
    ("novel_status", _BASE_EVENING, datetime(2026, 9, 2, 5, 0, tzinfo=_SEOUL)),
    ("series_status", _BASE_EVENING, datetime(2026, 9, 3, 18, 30, tzinfo=_SEOUL)),
    ("user_backup", _BASE_EVENING, datetime(2026, 9, 4, 2, 30, tzinfo=_SEOUL)),
    ("pending_deletion_detection", _BASE_EVENING, datetime(2026, 9, 2, 12, 30, tzinfo=_SEOUL)),
    ("preference_analyze", _BASE_EVENING, datetime(2026, 9, 1, 19, 15, tzinfo=_SEOUL)),
    ("recommendation_run", _BASE_EVENING, datetime(2026, 9, 2, 8, 50, tzinfo=_SEOUL)),
]

# 每天应触发几次：锁住"预算"而不只是"能解析"。日步进任务不在此列（跨天，见上表）。
_EXPECTED_RUNS_PER_DAY: dict[str, int] = {
    "bookmarks": 6,
    "subscribed_series": 2,
    "following_list": 1,
    "following_novels": 4,
    "novel_status": 2,
    "pending_deletion_detection": 1,
    "preference_analyze": 2,
    "recommendation_run": 1,
}


@pytest.mark.parametrize(
    ("task_name", "base", "expected"),
    _EXPECTED_NEXT_RUNS,
    ids=[f"{name}@{base:%H%M}" for name, base, _ in _EXPECTED_NEXT_RUNS],
)
def test_phase2_cron_next_run_matches_expected_wall_clock(
    task_name: str, base: datetime, expected: datetime
) -> None:
    expr = PHASE2_CRONS[task_name]
    result = cron_to_next_run(expr, base.timestamp(), "Asia/Seoul")

    assert result is not None, f"{task_name} 的 cron 无法解析: {expr!r}"
    assert datetime.fromtimestamp(result, _SEOUL) == expected, (
        f"{task_name} ({expr!r}) 在 {base} 之后算出的下次运行时刻不符合预期"
    )


@pytest.mark.parametrize(("task_name", "expected_count"), sorted(_EXPECTED_RUNS_PER_DAY.items()))
def test_phase2_daily_cron_fires_expected_number_of_times(task_name: str, expected_count: int) -> None:
    """按天数频率锁预算：改成"每 4 小时"这类等价写法也不许偷偷加次数。"""
    expr = PHASE2_CRONS[task_name]
    cursor = _BASE_MIDNIGHT.timestamp()
    horizon = (_BASE_MIDNIGHT.timestamp() + 24 * 3600) - 1  # 不含次日 00:00 那一刻
    fires: list[datetime] = []
    while True:
        nxt = cron_to_next_run(expr, cursor, "Asia/Seoul")
        assert nxt is not None, f"{task_name} 的 cron 无法解析: {expr!r}"
        if nxt > horizon:
            break
        fires.append(datetime.fromtimestamp(nxt, _SEOUL))
        cursor = nxt

    assert len(fires) == expected_count, f"{task_name} ({expr!r}) 实际触发 {fires}"


def test_config_example_matches_the_phase2_layout() -> None:
    """`config/config.yaml.example` 的排布必须与上面逐条验算过的 11 条一致。

    样例配置是新部署的起点，也是生产 `config/config.yaml`（不在仓库里）的抄写源，
    所以它必须能被 load_settings 完整读出来，且时区必须是 Asia/Seoul——时区写错时
    每条 cron 都会整体偏 9 小时，而这不会报任何错。
    """
    example = Path(__file__).resolve().parents[1] / "config" / "config.yaml.example"
    sync = load_settings(str(example), None).sync

    assert sync.auto_sync_timezone == "Asia/Seoul"
    for config in SCHEDULER_TASK_CONFIGS:
        name = config["name"]
        actual = getattr(sync, config["cron_setting"])
        assert actual == PHASE2_CRONS[name], f"{name}: 样例配置写的是 {actual!r}"


def _sync_default(field_name: str) -> object:
    """读 SyncSettings 的字段默认值。

    `SyncSettings` 是 `slots=True` 的 dataclass，类属性被替换成了 slot 描述符，
    `SyncSettings.auto_sync_x_cron` 拿到的是描述符而不是默认值，只能走 fields()。
    """
    for field in fields(SyncSettings):
        if field.name == field_name:
            return field.default
    raise AssertionError(f"SyncSettings 缺少字段 {field_name}")


def test_preference_analyze_default_cron_is_twice_daily() -> None:
    """preference_analyze 默认 cron 从每 30 分钟改为每天两次。

    它是纯本地计算、不耗 Pixiv 配额，但每轮仍占用唯一那个 job 槽，每 30 分钟一次
    会持续挤掉同步任务（生产从未真正跑过，改前的默认值只是个占位）。
    """
    assert _sync_default("auto_sync_preference_analyze_cron") == "15 7,19 * * *"
    # cron 非空时 interval 只是解析失败后的回落值，但回落值也不该是 1 小时
    assert _sync_default("auto_sync_preference_analyze_interval_hours") == 12


def test_recommendation_run_default_cron_is_daily() -> None:
    """recommendation_run 消耗 Pixiv 搜索配额，默认降到每天一次。"""
    assert _sync_default("auto_sync_recommendation_run_cron") == "50 8 * * *"


def test_load_settings_falls_back_to_new_default_crons(tmp_path: Path) -> None:
    """YAML 里没写这两个字段时，load_settings 的回落值必须与 dataclass 默认一致。

    这两处默认值是分开写的（dataclass 一份、load_settings 一份），历史上就漂过。
    """
    config = tmp_path / "config.yaml"
    config.write_text("sync: {}\n", encoding="utf-8")

    sync = load_settings(str(config), None).sync

    assert sync.auto_sync_preference_analyze_cron == _sync_default("auto_sync_preference_analyze_cron")
    assert sync.auto_sync_preference_analyze_interval_hours == _sync_default(
        "auto_sync_preference_analyze_interval_hours"
    )
    assert sync.auto_sync_recommendation_run_cron == _sync_default("auto_sync_recommendation_run_cron")


def test_save_sync_settings_default_crons_pass_their_own_validator(tmp_path: Path) -> None:
    """空 payload 保存时写入的回落默认值必须自己也过得了 cron 校验。

    `SettingsManager._save_cron` 对非法表达式抛 ValueError，所以一个写错的默认值会让
    "什么都没改就点保存"直接 500。
    """
    config = tmp_path / "config.yaml"
    config.write_text("sync: {}\n", encoding="utf-8")

    saved = SettingsManager(str(config)).save_sync_settings({})

    assert saved["auto_sync_preference_analyze_cron"] == "15 7,19 * * *"
    assert saved["auto_sync_preference_analyze_interval_hours"] == 12
    assert saved["auto_sync_recommendation_run_cron"] == "50 8 * * *"

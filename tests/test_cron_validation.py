"""cron_to_next_run 契约测试：解析失败时必须返回 None，绝不向调用方泄漏异常。

回归背景：cron_to_next_run 原先只 catch ImportError，croniter 对畸形表达式抛出的
CroniterBadCronError / CroniterNotAlphaError / CroniterBadDateError 会一路冒泡：
- 保存设置路径（SettingsManager._save_cron）依赖 None 触发友好的 ValueError；
- 调度循环（AutoSyncScheduler）用 `cron_to_next_run(...) or fallback` 处理 None。
异常泄漏会让前者返回 500、后者整轮调度中断。
"""
from __future__ import annotations

import pytest

from pixiv_novel_sync.settings import cron_to_next_run


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

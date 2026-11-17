"""让位的两道护栏（docs/JOB_SYSTEM.md §3.6「两道护栏的关系」）。

只钉住文档里写的前提，不改行为：让位后 600 s 的续跑必然落在 6 小时冷却期内，所以续跑
那一轮不会再被让位；连续让位上限只在冷却期过去之后才可能起作用。
"""

from __future__ import annotations

import time
from pathlib import Path

from pixiv_novel_sync.web.managers import (
    SCHEDULER_MAX_CONSECUTIVE_PREEMPTIONS,
    SCHEDULER_PREEMPT_COOLDOWN_SECONDS,
    SCHEDULER_PREEMPT_RETRY_SECONDS,
    AutoSyncScheduler,
)

TASK = "following_novels"  # 可让位任务


def _scheduler(preempted_seconds_ago: float, streak: int) -> tuple[AutoSyncScheduler, float]:
    scheduler = AutoSyncScheduler(config_path=None, env_path=None)
    now = time.time()
    scheduler._task_preempted_at[TASK] = now - preempted_seconds_ago
    scheduler._task_preempt_streak[TASK] = streak
    return scheduler, now


def test_resume_after_yield_always_lands_inside_the_cooldown() -> None:
    assert SCHEDULER_PREEMPT_RETRY_SECONDS < SCHEDULER_PREEMPT_COOLDOWN_SECONDS
    scheduler, now = _scheduler(SCHEDULER_PREEMPT_RETRY_SECONDS, streak=1)

    # 续跑那一轮不会再被让位，所以一定能跑完
    assert scheduler._may_preempt(TASK, now) is False


def test_streak_cap_only_matters_once_the_cooldown_has_passed() -> None:
    after_cooldown = SCHEDULER_PREEMPT_COOLDOWN_SECONDS + 1
    scheduler, now = _scheduler(after_cooldown, streak=1)
    assert scheduler._may_preempt(TASK, now) is True

    scheduler, now = _scheduler(after_cooldown, streak=SCHEDULER_MAX_CONSECUTIVE_PREEMPTIONS)
    assert scheduler._may_preempt(TASK, now) is False


def test_job_system_doc_explains_the_relationship() -> None:
    doc = (Path(__file__).resolve().parents[1] / "docs" / "JOB_SYSTEM.md").read_text(encoding="utf-8")
    section = doc.split("**两道护栏的关系", 1)[1].split("\n## ", 1)[0]
    for name in (
        "SCHEDULER_PREEMPT_RETRY_SECONDS",
        "SCHEDULER_PREEMPT_COOLDOWN_SECONDS",
        "SCHEDULER_MAX_CONSECUTIVE_PREEMPTIONS",
    ):
        assert name in section

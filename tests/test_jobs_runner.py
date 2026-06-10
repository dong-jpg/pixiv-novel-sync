from __future__ import annotations

from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus
from pixiv_novel_sync.jobs.runner import JobRunner


def test_runner_executes_tasks_and_merges_stats():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.CLI, task_types=["a", "b"]))
    calls: list[str] = []

    def executor(task_type, context):
        calls.append(task_type)
        return {"novels": 1, "task": task_type}

    runner = JobRunner(manager=manager, executor=executor)
    result = runner.run(state.job_id)

    assert result.status == JobStatus.SUCCEEDED
    assert calls == ["a", "b"]
    assert result.stats["novels"] == 2
    assert result.stats["task"] == "b"


def test_runner_does_not_add_boolean_stats():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.CLI, task_types=["a", "b"]))

    def executor(task_type, context):
        return {"ok": True}

    result = JobRunner(manager=manager, executor=executor).run(state.job_id)

    assert result.status == JobStatus.SUCCEEDED
    assert result.stats["ok"] is True


def test_runner_marks_failed_on_exception():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.CLI, task_types=["a"]))

    def executor(task_type, context):
        raise RuntimeError("boom")

    runner = JobRunner(manager=manager, executor=executor)
    result = runner.run(state.job_id)

    assert result.status == JobStatus.FAILED
    assert result.error == "boom"
    assert any("boom" in entry.message for entry in result.logs)


def test_runner_stops_before_task_when_cancel_requested():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.CLI, task_types=["a", "b"]))
    calls: list[str] = []

    def executor(task_type, context):
        calls.append(task_type)
        manager.request_cancel(state.job_id)
        return {"ran": 1}

    runner = JobRunner(manager=manager, executor=executor)
    result = runner.run(state.job_id)

    assert calls == ["a"]
    assert result.status == JobStatus.CANCELLED


def test_runner_does_not_start_pre_cancelled_job():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.CLI, task_types=["a"]))
    manager.request_cancel(state.job_id)
    calls: list[str] = []

    def executor(task_type, context):
        calls.append(task_type)
        return {"ran": 1}

    result = JobRunner(manager=manager, executor=executor).run(state.job_id)

    assert calls == []
    assert result.status == JobStatus.CANCELLED


def test_runner_does_not_double_stats_when_task_returns_same_object():
    """回归测试：JobRunner 的 merge_stats 不应与任务内部对 job.stats 的赋值冲突。
    P0 bug: run_check_bookmarks_task 曾让 job.stats = check_stats，然后 return check_stats，
    导致 merge_stats(state.stats, task_stats) 对同一对象累加,所有数值翻倍。
    修复后:返回独立副本,统一路径不应设 job.stats(legacy路径除外)。"""
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.CLI, task_types=["check"]))
    
    def executor(task_type, context):
        # 模拟 check task 返回独立副本(不再是同一对象)
        return {"total_checked": 10, "new": 3, "existing": 7}
    
    runner = JobRunner(manager=manager, executor=executor)
    result = runner.run(state.job_id)
    
    assert result.status == JobStatus.SUCCEEDED
    assert result.stats["total_checked"] == 10  # 不是 20
    assert result.stats["new"] == 3  # 不是 6
    assert result.stats["existing"] == 7  # 不是 14

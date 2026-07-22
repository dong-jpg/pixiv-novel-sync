from __future__ import annotations

import threading

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


def test_runner_marks_cancelled_on_interrupted_error():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.CLI, task_types=["a"]))

    def executor(task_type, context):
        raise InterruptedError("Task stopped by user")

    runner = JobRunner(manager=manager, executor=executor)
    result = runner.run(state.job_id)

    assert result.status == JobStatus.CANCELLED
    assert result.error is None


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


def test_runner_provides_lazy_finalization_for_each_task():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["a", "b"]))
    observed = []

    def executor(task_type, context):
        observed.append((task_type, context["is_last_task"]))
        assert context["claim_finalization"]() is True
        return {"task": task_type}

    result = JobRunner(manager=manager, executor=executor).run(state.job_id)

    assert observed == [("a", False), ("b", True)]
    assert result.status == JobStatus.SUCCEEDED
    assert result.stats["task"] == "b"


def test_runner_rejects_cancel_after_last_task_claim():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))
    finalizing = threading.Event()
    release = threading.Event()

    def executor(task_type, context):
        assert context["is_last_task"] is True
        assert context["claim_finalization"]() is True
        finalizing.set()
        assert release.wait(timeout=3)
        return {"rescue_catalog_items": 1}

    thread = threading.Thread(target=lambda: JobRunner(manager, executor).run(state.job_id))
    thread.start()
    try:
        assert finalizing.wait(timeout=3)
        assert manager.request_cancel(state.job_id) is False
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
    finally:
        release.set()
        thread.join(timeout=3)

    assert state.status == JobStatus.SUCCEEDED
    assert state.stats["rescue_catalog_items"] == 1


def test_runner_cancels_when_request_wins_before_claim():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))
    executor_entered = threading.Event()
    release = threading.Event()
    side_effects = []

    def executor(task_type, context):
        executor_entered.set()
        assert release.wait(timeout=3)
        if not context["claim_finalization"]():
            raise InterruptedError("cancelled before finalization")
        side_effects.append("rebuild")
        return {"rescue_catalog_items": 1}

    thread = threading.Thread(target=lambda: JobRunner(manager, executor).run(state.job_id))
    thread.start()
    try:
        assert executor_entered.wait(timeout=3)
        assert manager.request_cancel(state.job_id) is True
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
    finally:
        release.set()
        thread.join(timeout=3)

    assert side_effects == []
    assert state.status == JobStatus.CANCELLED


def test_runner_fallback_claim_finishes_task_without_executor_claim():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.CLI, task_types=["ordinary"]))

    def executor(task_type, context):
        assert callable(context["claim_finalization"])
        return {"ordinary": 1}

    result = JobRunner(manager=manager, executor=executor).run(state.job_id)

    assert result.status == JobStatus.SUCCEEDED
    assert result.stats["ordinary"] == 1


def test_runner_succeeds_for_empty_task_list():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.CLI, task_types=[]))
    original_try_begin = manager.try_begin_finalization
    claim_calls = []

    def recording_try_begin(job_id):
        claim_calls.append(job_id)
        return original_try_begin(job_id)

    manager.try_begin_finalization = recording_try_begin

    result = JobRunner(manager=manager, executor=lambda task_type, context: None).run(state.job_id)

    assert result.status == JobStatus.SUCCEEDED
    assert claim_calls == [state.job_id]

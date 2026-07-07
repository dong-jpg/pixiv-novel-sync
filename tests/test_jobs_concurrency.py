from __future__ import annotations

import threading

from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobType
from pixiv_novel_sync.web.utils import _job_to_dict_unified, _safe_snapshot


def _make_spec() -> JobSpec:
    return JobSpec(
        source=JobSource.WEB,
        job_type=JobType.SYNC,
        task_types=["sync_bookmarks"],
        params={},
    )


def test_merge_task_stats_is_locked_and_accumulates() -> None:
    manager = JobManager()
    state = manager.submit(_make_spec())

    assert manager.merge_task_stats(state.job_id, {"novels": 3, "nested": {"a": 1}}) is True
    assert manager.merge_task_stats(state.job_id, {"novels": 2, "nested": {"a": 4}}) is True

    # merge_stats 语义：数值累加；非数值（含 dict）整体替换为最新值
    assert state.stats["novels"] == 5
    assert state.stats["nested"] == {"a": 4}


def test_merge_task_stats_missing_job_returns_false() -> None:
    manager = JobManager()
    assert manager.merge_task_stats("nonexistent", {"novels": 1}) is False


def test_safe_snapshot_survives_concurrent_mutation() -> None:
    """worker 线程持续写入 stats 时，_safe_snapshot 必须能拿到稳定快照而不抛
    RuntimeError('dictionary changed size during iteration')。"""
    shared: dict[str, int] = {str(i): i for i in range(200)}
    stop = threading.Event()
    errors: list[Exception] = []

    def writer() -> None:
        i = 200
        while not stop.is_set():
            shared[str(i % 400)] = i
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(500):
            try:
                snap = _safe_snapshot(shared)
                assert isinstance(snap, dict)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
    finally:
        stop.set()
        t.join()

    assert not errors, f"_safe_snapshot 在并发写入下抛异常: {errors[:3]}"


def test_job_to_dict_snapshots_stats_and_progress() -> None:
    """序列化输出应是独立快照：后续修改 job.stats 不应影响已序列化的结果。"""
    manager = JobManager()
    state = manager.submit(_make_spec())
    manager.merge_task_stats(state.job_id, {"novels": 1, "nested": {"a": 1}})
    manager.update_progress(state.job_id, current="x")

    result = _job_to_dict_unified(state)
    # 修改原 dict 不应回写到已序列化结果
    state.stats["novels"] = 999
    state.stats["nested"]["a"] = 999
    state.progress["current"] = "changed"

    assert result["stats"]["novels"] == 1
    assert result["stats"]["nested"]["a"] == 1
    assert result["progress"]["current"] == "x"

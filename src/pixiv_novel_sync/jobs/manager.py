from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable

from pixiv_novel_sync.jobs.models import JobLogEntry, JobSpec, JobState, JobStatus, JobType


logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}


class FinalizationClaim:
    __slots__ = ("_manager", "_job_id", "_token")

    def __init__(self, manager: JobManager, job_id: str, token: object) -> None:
        self._manager = manager
        self._job_id = job_id
        self._token = token

    def finish(self, task_stats: dict, *, is_last_task: bool) -> bool:
        return self._manager._finish_finalization(
            self._job_id,
            self._token,
            task_stats,
            is_last_task=is_last_task,
        )

    def abort(self) -> bool:
        return self._manager._abort_finalization(self._job_id, self._token)


class JobManager:
    def __init__(self, max_logs: int = 500, max_jobs: int = 50) -> None:
        self.max_logs = max_logs
        self.max_jobs = max_jobs
        self._jobs: OrderedDict[str, JobState] = OrderedDict()
        self._lock = threading.RLock()
        self._semaphore = threading.BoundedSemaphore(1)
        self._active_finalizations: dict[str, object] = {}
        self._reserved_slot_job_id: str | None = None
        self._slot_released_callback: Callable[[], None] | None = None

    def submit(self, spec: JobSpec) -> JobState:
        with self._lock:
            job_id = uuid.uuid4().hex
            state = JobState(job_id=job_id, spec=spec)
            self._jobs[job_id] = state
            self.cleanup_old_jobs()
            return state

    def get_job(self, job_id: str) -> JobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest_job(self) -> JobState | None:
        with self._lock:
            if not self._jobs:
                return None
            return next(reversed(self._jobs.values()))

    def add_log(self, job_id: str, level: str, message: str) -> bool:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return False
            state.logs.append(JobLogEntry(time=time.strftime("%Y-%m-%d %H:%M:%S"), level=level, message=message))
            if len(state.logs) > self.max_logs:
                del state.logs[: len(state.logs) - self.max_logs]
            return True

    def update_progress(self, job_id: str, message: str | None = None, **progress: object) -> bool:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return False
            state.progress.update(progress)
            if message is not None:
                state.message = message
            return True

    def merge_task_stats(self, job_id: str, task_stats: dict) -> bool:
        """在锁保护下把单个任务的增量 stats 合并进 job.stats。

        worker 线程调用；与 Flask 请求线程读取 job.stats（序列化）互斥，
        避免并发迭代/写入同一 dict 触发 RuntimeError。
        """
        from pixiv_novel_sync.jobs.tasks import merge_stats

        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return False
            merge_stats(state.stats, task_stats)
            return True

    def request_cancel(self, job_id: str) -> bool:
        with self._lock:
            state = self._jobs.get(job_id)
            if (
                state is None
                or state.status in _TERMINAL_STATUSES
                or job_id in self._active_finalizations
            ):
                return False
            state.status = JobStatus.CANCEL_REQUESTED
            state.message = "cancel requested"
            return True

    def try_begin_finalization(self, job_id: str) -> FinalizationClaim | None:
        with self._lock:
            state = self._jobs.get(job_id)
            if (
                state is None
                or state.status != JobStatus.RUNNING
                or job_id in self._active_finalizations
            ):
                return None
            token = object()
            self._active_finalizations[job_id] = token
            return FinalizationClaim(self, job_id, token)

    def mark_running(self, job_id: str, message: str = "running") -> bool:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None or state.status == JobStatus.CANCEL_REQUESTED:
                return False
            state.status = JobStatus.RUNNING
            state.message = message
            state.started_at = time.time()
            return True

    def mark_succeeded(self, job_id: str, message: str = "succeeded") -> bool:
        return self._mark_finished(job_id, JobStatus.SUCCEEDED, message=message)

    def mark_failed(self, job_id: str, error: str, message: str = "failed") -> bool:
        return self._mark_finished(job_id, JobStatus.FAILED, message=message, error=error)

    def mark_cancelled(self, job_id: str, message: str = "cancelled") -> bool:
        return self._mark_finished(job_id, JobStatus.CANCELLED, message=message)

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            state = self._jobs.get(job_id)
            return state is not None and state.status == JobStatus.CANCEL_REQUESTED

    def shared_admission_snapshot(self) -> tuple[list[JobState], list[JobState]]:
        with self._lock:
            live = [job for job in self._jobs.values() if job.status not in _TERMINAL_STATUSES]
            active = [job for job in live if not job.shared_waiting]
            waiting = sorted(
                (job for job in live if job.shared_waiting),
                key=lambda job: (job.shared_priority if job.shared_priority is not None else 4,
                                 job.created_at),
            )
            return active, waiting

    def check_work_waiting_admission(self, spec: JobSpec, priority: int) -> None:
        if type(priority) is not int or not 1 <= priority <= 4 or spec.source.value != "web":
            raise ValueError("invalid_shared_admission")
        is_work = spec.job_type == JobType.WORK_ANALYSIS
        if is_work != (priority == 4):
            raise ValueError("invalid_shared_admission")
        with self._lock:
            active, waiting = self.shared_admission_snapshot()
            if any(job.spec.job_type != JobType.WORK_ANALYSIS for job in active):
                raise RuntimeError("shared_busy")
            if is_work:
                if active or waiting:
                    raise RuntimeError("shared_busy")
            elif len(waiting) >= 8:
                raise RuntimeError("shared_pending_full")

    def submit_work_waiting(self, spec: JobSpec, priority: int) -> JobState:
        with self._lock:
            self.check_work_waiting_admission(spec, priority)
            job = self.submit(spec)
            job.shared_waiting = True
            job.shared_priority = priority
            job.message = "waiting for shared slot"
            return job

    def reserve_shared_start(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED or not job.shared_waiting:
                return False
            active, waiting = self.shared_admission_snapshot()
            if active or not waiting or waiting[0].job_id != job_id:
                return False
            if self._reserved_slot_job_id is not None or not self._semaphore.acquire(blocking=False):
                return False
            self._reserved_slot_job_id = job_id
            job.shared_waiting = False
            job.message = "admitted to shared slot"
            return True

    def abandon_reserved_start(self, job_id: str) -> bool:
        with self._lock:
            if self._reserved_slot_job_id != job_id:
                return False
            self._reserved_slot_job_id = None
            self._semaphore.release()
            return True

    def defer_work_start(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED or job.spec.job_type != JobType.WORK_ANALYSIS:
                return False
            job.shared_waiting = True
            job.shared_priority = 4
            job.message = "waiting for shared slot"
            return True

    def set_slot_released_callback(self, callback: Callable[[], None] | None) -> None:
        with self._lock:
            self._slot_released_callback = callback

    def shared_admission_is_idle(self) -> bool:
        with self._lock:
            active, waiting = self.shared_admission_snapshot()
            if active or waiting or self._reserved_slot_job_id is not None:
                return False
            # A terminal state precedes the runner's finally/slot release.
            if not self._semaphore.acquire(blocking=False):
                return False
            self._semaphore.release()
            return True

    def acquire_run_slot(self, job_id: str | None = None) -> bool:
        with self._lock:
            if job_id is not None and self._reserved_slot_job_id == job_id:
                self._reserved_slot_job_id = None
                return True
            return self._semaphore.acquire(blocking=False)

    def release_run_slot(self) -> None:
        self._semaphore.release()
        with self._lock:
            callback = self._slot_released_callback
        if callback is not None:
            try:
                callback()
            except Exception:
                logger.warning("Shared admission notification failed")

    def cleanup_old_jobs(self) -> None:
        with self._lock:
            if len(self._jobs) <= self.max_jobs:
                return

            removable_ids = [
                job_id for job_id, state in self._jobs.items() if state.status in _TERMINAL_STATUSES
            ]
            for job_id in removable_ids:
                if len(self._jobs) <= self.max_jobs:
                    break
                self._jobs.pop(job_id, None)
                self._active_finalizations.pop(job_id, None)

    def _finish_finalization(
        self,
        job_id: str,
        token: object,
        task_stats: dict,
        *,
        is_last_task: bool,
    ) -> bool:
        from pixiv_novel_sync.jobs.tasks import merge_stats

        with self._lock:
            if self._active_finalizations.get(job_id) is not token:
                return False
            state = self._jobs.get(job_id)
            if state is None or state.status != JobStatus.RUNNING:
                self._active_finalizations.pop(job_id, None)
                return False

            merge_stats(state.stats, task_stats)
            if is_last_task:
                state.status = JobStatus.SUCCEEDED
                state.message = "succeeded"
                state.error = None
                state.finished_at = time.time()
            self._active_finalizations.pop(job_id, None)
            return True

    def _abort_finalization(self, job_id: str, token: object) -> bool:
        with self._lock:
            if self._active_finalizations.get(job_id) is not token:
                return False
            self._active_finalizations.pop(job_id, None)
            return True

    def _mark_finished(
        self,
        job_id: str,
        status: JobStatus,
        message: str,
        error: str | None = None,
    ) -> bool:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return False
            state.status = status
            state.message = message
            state.error = error
            state.finished_at = time.time()
            return True

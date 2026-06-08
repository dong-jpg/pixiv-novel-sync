from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict

from pixiv_novel_sync.jobs.models import JobLogEntry, JobSpec, JobState, JobStatus


_TERMINAL_STATUSES = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}


class JobManager:
    def __init__(self, max_logs: int = 500, max_jobs: int = 50) -> None:
        self.max_logs = max_logs
        self.max_jobs = max_jobs
        self._jobs: OrderedDict[str, JobState] = OrderedDict()
        self._lock = threading.RLock()
        self._semaphore = threading.BoundedSemaphore(1)

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

    def request_cancel(self, job_id: str) -> bool:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None or state.status in _TERMINAL_STATUSES:
                return False
            state.status = JobStatus.CANCEL_REQUESTED
            state.message = "cancel requested"
            return True

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

    def acquire_run_slot(self) -> bool:
        return self._semaphore.acquire(blocking=False)

    def release_run_slot(self) -> None:
        self._semaphore.release()

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

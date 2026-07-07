from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobState

JobTaskExecutor = Callable[[str, dict[str, Any]], dict[str, Any] | None]


class JobRunner:
    def __init__(self, manager: JobManager, executor: JobTaskExecutor) -> None:
        self.manager = manager
        self.executor = executor

    def run(self, job_id: str) -> JobState:
        state = self.manager.get_job(job_id)
        if state is None:
            raise KeyError(job_id)

        acquired = self.manager.acquire_run_slot()
        if not acquired:
            self.manager.mark_failed(job_id, "已有任务正在运行")
            return state

        try:
            if not self.manager.mark_running(job_id):
                self.manager.mark_cancelled(job_id)
                return state

            for task_type in state.task_types:
                if self.manager.is_cancel_requested(job_id):
                    self.manager.mark_cancelled(job_id)
                    return state

                task_stats = self.executor(
                    task_type,
                    {
                        "job": state,
                        "job_id": job_id,
                        "manager": self.manager,
                        "params": state.spec.params,
                    },
                )
                if task_stats:
                    self.manager.merge_task_stats(job_id, task_stats)

            if self.manager.is_cancel_requested(job_id):
                self.manager.mark_cancelled(job_id)
            else:
                self.manager.mark_succeeded(job_id)
            return state
        except InterruptedError:
            self.manager.mark_cancelled(job_id)
            return state
        except Exception as exc:
            error = str(exc)
            self.manager.add_log(job_id, "error", error)
            self.manager.mark_failed(job_id, error)
            return state
        finally:
            self.manager.release_run_slot()

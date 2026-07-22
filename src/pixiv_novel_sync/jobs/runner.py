from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pixiv_novel_sync.jobs.manager import FinalizationClaim, JobManager
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

        active_claim: FinalizationClaim | None = None
        try:
            if not self.manager.mark_running(job_id):
                self.manager.mark_cancelled(job_id)
                return state

            if not state.task_types:
                active_claim = self.manager.try_begin_finalization(job_id)
                if active_claim is None:
                    self.manager.mark_cancelled(job_id)
                else:
                    active_claim.finish({}, is_last_task=True)
                    active_claim = None
                return state

            for index, task_type in enumerate(state.task_types):
                if self.manager.is_cancel_requested(job_id):
                    self.manager.mark_cancelled(job_id)
                    return state

                is_last_task = index == len(state.task_types) - 1
                claim_attempted = False

                def claim_finalization() -> bool:
                    nonlocal active_claim, claim_attempted
                    if not claim_attempted:
                        active_claim = self.manager.try_begin_finalization(job_id)
                        claim_attempted = True
                    return active_claim is not None

                task_stats = self.executor(
                    task_type,
                    {
                        "job": state,
                        "job_id": job_id,
                        "manager": self.manager,
                        "params": state.spec.params,
                        "is_last_task": is_last_task,
                        "claim_finalization": claim_finalization,
                    },
                )

                if not claim_attempted:
                    claim_finalization()
                if active_claim is None:
                    if task_stats:
                        self.manager.merge_task_stats(job_id, task_stats)
                    self.manager.mark_cancelled(job_id)
                    return state

                if not active_claim.finish(task_stats or {}, is_last_task=is_last_task):
                    return state
                active_claim = None

            return state
        except InterruptedError:
            if active_claim is not None:
                active_claim.abort()
            self.manager.mark_cancelled(job_id)
            return state
        except Exception as exc:
            if active_claim is not None:
                active_claim.abort()
            error = str(exc)
            self.manager.add_log(job_id, "error", error)
            self.manager.mark_failed(job_id, error)
            return state
        finally:
            self.manager.release_run_slot()

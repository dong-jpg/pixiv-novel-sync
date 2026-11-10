from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from ..jobs.models import JobSource, JobSpec, JobStatus, JobType
from .contracts import WorkAnalysisError

logger = logging.getLogger(__name__)
_TERMINAL = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
_SYNC_PRIORITY = {
    "bookmark": 1, "subscribed_series": 2, "following_users": 3,
    "following_novels": 3, "user_status": 3, "novel_status": 3,
    "series_status": 3, "user_backup": 3, "pending_deletion_detection": 3,
}


def _checked_run_id(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise WorkAnalysisError("invalid_run_id", 400)
    return value


class WorkAnalysisCoordinator:
    def __init__(self, manager, submit_shared, run_shared, settings_provider, *, db_factory, scheduler):
        self.manager = manager
        self.submit_shared = submit_shared
        self.run_shared = run_shared
        self.settings_provider = settings_provider
        self.db_factory = db_factory
        self.scheduler = scheduler
        self._gate = threading.RLock()
        self._recovered = False
        self._closed = False
        self.manager.set_slot_released_callback(lambda: self.wake("slot_released"))

    @contextmanager
    def work_control_scope(self):
        with self._gate:
            yield

    def _read(self, run_id):
        db = self.db_factory()
        try:
            state = db.get_work_analysis(run_id)
            if state is None:
                raise WorkAnalysisError("not_found", 404)
            return state
        finally:
            db.close()

    def _job_for_run(self, run_id):
        active, waiting = self.manager.shared_admission_snapshot()
        matches = [job for job in active + waiting
                   if job.spec.job_type == JobType.WORK_ANALYSIS and job.spec.params == {"run_id": run_id}]
        if len(matches) > 1:
            raise WorkAnalysisError("admission_conflict", 409)
        return matches[0] if matches else None

    def _due_priority(self):
        settings = self.settings_provider()
        sync = getattr(settings, "sync", None)
        if not self.scheduler.is_running() or not bool(getattr(sync, "auto_sync_enabled", False)):
            return None
        return self.scheduler._highest_due_priority(settings, "work_analysis")

    def challenger_priority(self):
        active, waiting = self.manager.shared_admission_snapshot()
        priorities = [job.shared_priority for job in waiting
                      if job.spec.job_type != JobType.WORK_ANALYSIS and job.status == JobStatus.QUEUED]
        due = self._due_priority()
        if due is not None:
            priorities.append(due)
        return min(priorities) if priorities else None

    def startup_recover(self):
        with self._gate:
            if self._closed:
                raise WorkAnalysisError("admission_unavailable", 503)
            if self._recovered:
                return 0
            db = self.db_factory()
            try:
                stale_count = db.fail_stale_task_logs()
                count = db.recover_work_analyses(now=datetime.now(timezone.utc), process_start=True)
                repaired_count = db.reconcile_work_analysis_task_logs()
            finally:
                db.close()
            if stale_count or repaired_count:
                logger.info("Recovered %d stale shared logs; reconciled %d work logs", stale_count, repaired_count)
            self._recovered = True
            return count

    def _launch(self, job):
        if not self.manager.reserve_shared_start(job.job_id):
            return False
        try:
            self.run_shared(job.job_id)
        except Exception:
            self.manager.abandon_reserved_start(job.job_id)
            self.manager.mark_failed(job.job_id, "shared_launch_failed")
            logger.warning("Shared work launch failed")
            return False
        return True

    def _cancel_waiting(self, job):
        self.manager.mark_cancelled(job.job_id)
        log_id = job.progress.get("log_id")
        if type(log_id) is not int:
            return
        db = self.db_factory()
        try:
            db.update_task_log(log_id, "cancelled", error_message="cancelled before admission")
        except Exception:
            logger.warning("Waiting shared task log finalization failed")
        finally:
            db.close()

    def wake(self, reason):
        if reason not in {"created", "resumed", "completion", "slot_released", "scheduler_tick", "manual_sync"}:
            raise ValueError("invalid_work_wake")
        with self._gate:
            if self._closed or not self._recovered:
                return False
            db = self.db_factory()
            try:
                db.recover_work_analyses(now=datetime.now(timezone.utc), process_start=False)
            finally:
                db.close()
            active, waiting = self.manager.shared_admission_snapshot()
            for job in waiting:
                if job.status == JobStatus.CANCEL_REQUESTED:
                    self._cancel_waiting(job)
            active, waiting = self.manager.shared_admission_snapshot()
            if active:
                work = next((job for job in active if job.spec.job_type == JobType.WORK_ANALYSIS), None)
                if work is not None and self.challenger_priority() is not None:
                    self.cancel_shared_job(work.job_id, action="sync_yield")
                return work is not None or any(job.shared_priority is not None for job in active)
            due = self._due_priority()
            if waiting:
                job = waiting[0]
                if due is not None and due < job.shared_priority:
                    return False
                if job.spec.job_type == JobType.WORK_ANALYSIS:
                    state = self._read(job.spec.params["run_id"])
                    if state["deleted"] or not (state["status"] == "queued" or
                                                  (state["status"] == "paused" and state["reason"] == "sync_yield")):
                        self._cancel_waiting(job)
                        return self.wake("completion")
                self._launch(job)
                return True
            if due is not None:
                # Leave the existing scheduler to submit its due job. The
                # scheduler tick does not consume/retry that due entry early.
                return False
            db = self.db_factory()
            try:
                candidates = db.queued_work_analysis_ids()
            finally:
                db.close()
            if not candidates:
                return False
            run_id = candidates[0]
            spec = JobSpec(JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": run_id})
            try:
                job = self.submit_shared(
                    spec, self.settings_provider(), "work_analysis", "Work analysis",
                    run_async=False, work_admission_priority=4,
                )
            except RuntimeError:
                return False
            self._launch(job)
            return True

    def request_sync(self, spec, settings, task_type, task_name, *, progress=None):
        kinds = [name.split(":", 1)[0] for name in spec.task_types]
        if spec.source != JobSource.WEB or not kinds or any(name not in _SYNC_PRIORITY for name in kinds):
            return self.submit_shared(spec, settings, task_type, task_name, progress=progress)
        with self._gate:
            active, waiting = self.manager.shared_admission_snapshot()
            work_present = any(job.spec.job_type == JobType.WORK_ANALYSIS for job in active + waiting)
            bridge_waiters = any(job.shared_priority is not None for job in waiting)
            if not work_present and not bridge_waiters:
                return self.submit_shared(spec, settings, task_type, task_name, progress=progress)
            job = self.submit_shared(
                spec, settings, task_type, task_name, progress=progress,
                run_async=False, work_admission_priority=min(_SYNC_PRIORITY[name] for name in kinds),
            )
            self.wake("manual_sync")
            return job

    def request_control(self, run_id, action, expected_version):
        run_id = _checked_run_id(run_id)
        if action not in {"pause", "cancel", "resume", "delete", "sync_yield"}:
            raise WorkAnalysisError("invalid_action", 400)
        if type(expected_version) is not int or expected_version < 1:
            raise WorkAnalysisError("invalid_version", 400)
        with self._gate:
            if self._closed or not self._recovered:
                raise WorkAnalysisError("admission_unavailable", 503)
            before = self._read(run_id)
            if before["deleted"] and action != "delete":
                raise WorkAnalysisError("work_deleted", 410)
            if before["version"] != expected_version and not (before["deleted"] and action == "delete"):
                raise WorkAnalysisError("version_conflict", 409)
            job = self._job_for_run(run_id)
            if action == "resume":
                if job is not None:
                    raise WorkAnalysisError("state_conflict", 409)
            elif job is not None:
                if not self.manager.request_cancel(job.job_id):
                    raise WorkAnalysisError("finalizing", 409)
            db = self.db_factory()
            try:
                if action == "resume":
                    result = db.resume_work_analysis(run_id, expected_version)
                else:
                    result = db.request_work_analysis_stop(run_id, action, expected_version)
            except WorkAnalysisError:
                raise
            except Exception:
                # Cancellation stays latched even if the durable stop failed.
                # The caller must read back; it must not receive success.
                raise WorkAnalysisError("control_unavailable", 503) from None
            finally:
                db.close()
            if job is not None and job.shared_waiting:
                self._cancel_waiting(job)
        try:
            self.wake("resumed" if action == "resume" else "completion")
        except Exception:
            logger.warning("Work remains queued after admission notification failure")
        return result

    def _request_sync_yield(self, job):
        # Caller holds _gate; work cannot claim or classify a stop until this
        # operation resolves. Storage CAS remains strict on every attempt.
        run_id = job.spec.params["run_id"]
        db = self.db_factory()
        try:
            original = db.get_work_analysis_control(run_id)
            if original is None or original["deleted_at"] is not None or original["invalidated_at"] is not None:
                return False
            if original["status"] in {"pausing", "paused", "cancelling", "cancelled"}:
                return original["reason"] == "sync_yield"
            if original["owner_token"] is None:
                if original["status"] != "queued":
                    return False
                self.request_control(run_id, "sync_yield", original["version"])
                return True
            if original["status"] != "running" or not self.manager.request_cancel(job.job_id):
                return False
            keys = ("run_id", "epoch", "owner_token", "manifest_fingerprint", "configuration_fingerprint",
                    "request_limit", "token_limit", "active_seconds_limit")
            expected_identity = tuple(original[key] for key in keys)
            current = original
            for attempt in range(3):
                try:
                    db.request_work_analysis_stop(run_id, "sync_yield", current["version"])
                    return True
                except WorkAnalysisError as error:
                    if error.code != "version_conflict":
                        raise
                current = db.get_work_analysis_control(run_id)
                if (current is None or current["deleted_at"] is not None or current["invalidated_at"] is not None
                        or tuple(current[key] for key in keys) != expected_identity):
                    return False
                if current["status"] == "pausing" and current["reason"] == "sync_yield":
                    return True
                if current["status"] != "running" or current["reason"] is not None:
                    return False
            raise WorkAnalysisError("control_unavailable", 503)
        except WorkAnalysisError:
            raise
        except Exception:
            raise WorkAnalysisError("control_unavailable", 503) from None
        finally:
            db.close()

    def cancel_shared_job(self, job_id, action="cancel"):
        if action not in {"cancel", "sync_yield"}:
            raise ValueError("invalid_shared_control")
        with self._gate:
            if self._closed:
                raise WorkAnalysisError("admission_unavailable", 503)
            job = self.manager.get_job(job_id)
            if job is None:
                return False
            if job.spec.job_type != JobType.WORK_ANALYSIS:
                accepted = self.manager.request_cancel(job_id)
                if accepted and job.shared_waiting:
                    self._cancel_waiting(job)
                    self.wake("completion")
                return accepted
            if not self._recovered:
                raise WorkAnalysisError("admission_unavailable", 503)
            if job.status in _TERMINAL:
                return False
            try:
                if action == "sync_yield":
                    return self._request_sync_yield(job)
                state = self._read(job.spec.params["run_id"])
                self.request_control(state["run_id"], action, state["version"])
            except WorkAnalysisError as error:
                if error.code in {"finalizing", "state_conflict", "version_conflict", "work_deleted"}:
                    return False
                raise
            return True

    def on_job_finished(self, job_id):
        job = self.manager.get_job(job_id)
        if job is not None and job.spec.job_type == JobType.WORK_ANALYSIS:
            with self._gate:
                with self.manager._lock:
                    latest = next((item for item in reversed(self.manager._jobs.values())
                                   if item.spec.job_type == JobType.WORK_ANALYSIS
                                   and item.spec.params == job.spec.params), None)
                state = self._read(job.spec.params["run_id"])
                if latest is job and state["status"] == "succeeded" and not state["deleted"]:
                    # Publication may have committed before a memory callback
                    # failed. Never regenerate that already-published report.
                    self.manager.mark_succeeded(job_id)
                elif job.status in _TERMINAL and state["status"] in {"paused", "cancelled", "invalidated"}:
                    self.manager.mark_cancelled(job_id)
        self.wake("completion")

    def close(self):
        with self._gate:
            if not self.manager.shared_admission_is_idle():
                raise RuntimeError("shared_owner_still_active")
            self._closed = True
            self.manager.set_slot_released_callback(None)

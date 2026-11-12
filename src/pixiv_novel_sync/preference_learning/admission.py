"""Admit durable summary intents to the existing Web execution slot."""

import logging

from ..jobs.models import JobSource, JobSpec, JobStatus, JobType
from ..work_analysis.contracts import WorkAnalysisError


_TERMINAL = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
logger = logging.getLogger(__name__)


class PreferenceLearningAdmission:
    def __init__(self, service, coordinator, submit_shared, settings_provider):
        self.service = service
        self.coordinator = coordinator
        self.manager = coordinator.manager
        self.submit_shared = submit_shared
        self.settings_provider = settings_provider
        self.ready = False
        service.submit_job = self.submit
        coordinator.preference_job_finished = self.finished

    def _job(self, run_id):
        with self.manager._lock:
            return next((job for job in reversed(self.manager._jobs.values())
                         if job.spec.job_type == JobType.PREFERENCE_SUMMARIZE
                         and job.spec.params == {"run_id": run_id}), None)

    def submit(self, run_id):
        if not self.ready:
            raise WorkAnalysisError("admission_unavailable", 503)
        with self.coordinator.work_control_scope():
            existing = self._job(run_id)
            if existing is not None:
                return existing
            spec = JobSpec(JobSource.WEB, ["preference_summarize"], JobType.PREFERENCE_SUMMARIZE,
                           {"run_id": run_id})
            try:
                return self.submit_shared(
                    spec, self.settings_provider(), "preference_summarize", "生成个人偏好画像",
                    run_async=False, work_admission_priority=4,
                )
            except Exception as exc:
                # A failure after the shared job committed must keep its identity.
                existing = self._job(run_id)
                if existing is not None:
                    return existing
                if "已有同步任务" in str(exc) or str(exc) in {"shared_busy", "shared_pending_full"}:
                    return None
                db = self.service.db_factory()
                try:
                    with db.transaction() as conn:
                        conn.execute(
                            "UPDATE preference_summary_runs SET status='failed',error_code='admission_failed',"
                            "owner_token=NULL,version=version+1,finished_at=CURRENT_TIMESTAMP "
                            "WHERE run_id=? AND status='queued' AND shared_job_id LIKE 'admitting:%'",
                            (run_id,),
                        )
                finally:
                    db.close()
                raise

    def reconcile(self):
        with self.coordinator.work_control_scope():
            db = self.service.db_factory()
            try:
                rows = db.conn.execute(
                    "SELECT run_id,shared_job_id FROM preference_summary_runs "
                    "WHERE status IN ('queued','running') AND shared_job_id IS NOT NULL LIMIT 100"
                ).fetchall()
                for row in rows:
                    job = self.manager.get_job(row["shared_job_id"])
                    if job is None or job.status not in _TERMINAL:
                        continue
                    cancelled = job.status == JobStatus.CANCELLED
                    with db.transaction() as conn:
                        conn.execute(
                            "UPDATE preference_summary_runs SET status=?,active=0,owner_token=NULL,"
                            "error_code=?,finished_at=CURRENT_TIMESTAMP,version=version+1 "
                            "WHERE run_id=? AND shared_job_id=? AND status IN ('queued','running')",
                            ("cancelled" if cancelled else "failed",
                             "shared_job_cancelled" if cancelled else "shared_job_failed",
                             row["run_id"], row["shared_job_id"]),
                        )
            finally:
                db.close()

    def tick(self):
        if not self.ready:
            return None
        self.reconcile()
        return self.service.wake()

    def finished(self, job_id):
        job = self.manager.get_job(job_id)
        if job is None or job.spec.job_type != JobType.PREFERENCE_SUMMARIZE:
            return
        self.reconcile()
        state = self.service.detail(job.spec.params["run_id"], include_result=False)
        if state.get("shared_job_id") != job_id:
            return
        if state["status"] == "succeeded" and not state.get("stale"):
            self.manager.mark_succeeded(job_id)
        elif state["status"] in {"cancelled", "invalidated", "deleted"}:
            self.manager.mark_cancelled(job_id)

    def control(self, run_id, action, expected_version):
        with self.coordinator.work_control_scope():
            state = self.service.detail(run_id, include_result=False)
            if action == "delete" and state["status"] == "deleted":
                return self.service.delete(run_id, expected_version)
            if state["version"] != expected_version:
                raise WorkAnalysisError("version_conflict", 409)
            job = self._job(run_id)
            if job is not None and job.status not in _TERMINAL:
                if not self.manager.request_cancel(job.job_id):
                    raise WorkAnalysisError("finalizing", 409)
            result = getattr(self.service, action)(run_id, expected_version)
        try:
            self.coordinator.wake("completion")
        except Exception as exc:
            logger.warning("Preference control wake deferred (%s)", type(exc).__name__)
        return result

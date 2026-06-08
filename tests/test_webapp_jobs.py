from __future__ import annotations

from pixiv_novel_sync.jobs.models import JobSource, JobType
from pixiv_novel_sync.webapp import SyncJobManager, _web_job_spec


def test_web_job_spec_for_sync_tasks():
    spec = _web_job_spec(["bookmark", "following_novels"])

    assert spec.source == JobSource.WEB
    assert spec.job_type == JobType.SYNC
    assert spec.task_types == ["bookmark", "following_novels"]


def test_web_job_spec_for_user_backup():
    spec = _web_job_spec(["user_backup:123"])

    assert spec.source == JobSource.WEB
    assert spec.job_type == JobType.USER_BACKUP
    assert spec.params["user_id"] == 123


def test_sync_job_manager_start_job_records_job_spec(tmp_path):
    manager = SyncJobManager(config_path=None, env_path=None)
    spec = _web_job_spec(["bookmark"])

    job = manager.start_job(spec.task_types)

    assert job.task_list == ["bookmark"]

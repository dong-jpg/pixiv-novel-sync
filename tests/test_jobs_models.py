from __future__ import annotations

from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobState, JobStatus, JobType


def test_job_spec_defaults_to_sync_job():
    spec = JobSpec(source=JobSource.CLI, task_types=["bookmark", "following_users"])

    assert spec.job_type == JobType.SYNC
    assert spec.source == JobSource.CLI
    assert spec.task_types == ["bookmark", "following_users"]
    assert spec.params == {}


def test_job_state_starts_queued_with_empty_collections():
    state = JobState(job_id="job-1", spec=JobSpec(source=JobSource.WEB, task_types=["bookmark"]))

    assert state.status == JobStatus.QUEUED
    assert state.message == "queued"
    assert state.progress == {}
    assert state.stats == {}
    assert state.logs == []
    assert state.error is None
    assert state.started_at is None
    assert state.finished_at is None

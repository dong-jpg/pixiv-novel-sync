"""Independent integration regressions for automatic-library review findings."""

import pytest
from datetime import datetime, timezone

from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus, JobType
from pixiv_novel_sync.jobs.runner import JobRunner
from pixiv_novel_sync.preference_learning.library import LibraryAnalysisService
from pixiv_novel_sync.work_analysis.executor import WorkAnalysisExecutor
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError
from test_library_analysis import library as library_fixture, enable, created_runs, add_novel, confirmed
from work_analysis_api_helpers import RecordingCoordinator
from work_analysis_runtime_helpers import make_runtime_environment


@pytest.fixture
def review_library(tmp_path, monkeypatch):
    yield from library_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize("manual_outcome", ["succeeded", "cancelled"])
def test_delete_previously_reused_or_skipped_manual_report_never_restarts_automatic_payment(tmp_path, monkeypatch, manual_outcome):
    env = make_runtime_environment(tmp_path, monkeypatch)
    try:
        if manual_outcome == "succeeded":
            manager = JobManager()
            job = manager.submit(JobSpec(JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": env.run_id}))
            executor = WorkAnalysisExecutor(env.db_factory, lambda: env.service)
            JobRunner(manager, lambda task, context: executor.run(env.run_id, context)).run(job.job_id)
            assert job.status == JobStatus.SUCCEEDED
        else:
            current = env.db.get_work_analysis(env.run_id)
            env.db.request_work_analysis_stop(env.run_id, "cancel", current["version"])
        service = LibraryAnalysisService(env.db_factory, lambda: env.service, RecordingCoordinator(env.db_factory))
        configuration = {
            "scope": "all", "agent_id": env.agent_id, "output_tokens": 128,
            "work_budget": {"request_limit": 64, "token_limit": 1000000, "active_seconds_limit": 600},
            "batch_budget": {"request_limit": 128, "token_limit": 2000000, "active_seconds_limit": 1200},
            "work_limit": 2, "interval_minutes": 60,
            "confirmations": {"external_send_confirmed": True, "budget_confirmed": True,
                              "current_archive_only": True, "order_confirmed": True},
        }
        preview = service.preview({**configuration, "purpose": "policy"})
        service.policy({**configuration, "enabled": True, "expected_version": 1,
                        "preview_fingerprint": preview["preview_fingerprint"]})
        service.tick()
        first_batch = service.state()["batches"][0]
        items = service.batch(first_batch["batch_id"])["items"]
        assert items[0]["status"] == ("reused" if manual_outcome == "succeeded" else "skipped")
        assert service.state()["policy"]["work_count"] == 0

        current = env.db.get_work_analysis(env.run_id)
        env.db.request_work_analysis_stop(env.run_id, "delete", current["version"])
        with env.db.transaction() as conn:
            conn.execute("UPDATE library_analysis_policy SET next_run_at='2000-01-01 00:00:00'")
        service.tick()

        # Deleting private output is not consent to regenerate it on the next cycle.
        assert env.db.conn.execute("SELECT COUNT(*) FROM work_analysis_runs").fetchone()[0] == 1
        assert service.state()["policy"]["work_count"] == 0
        assert not any(service.state()["policy"]["allocated"].values())
    finally:
        env.close()


def test_policy_disable_does_not_acknowledge_a_lost_child_stop_cas(review_library, monkeypatch):
    library = review_library
    enable(library)
    library.service.tick()
    run = created_runs(library.db)[0]
    lease = library.db.claim_work_analysis(run["run_id"], now=datetime.now(timezone.utc), slice_seconds=30)
    original = library.env.coordinator.request_control
    raced = []

    def advance_progress_before_first_control(run_id, action, expected_version):
        if not raced:
            raced.append(True)
            with library.db.transaction() as conn:
                conn.execute("UPDATE work_analysis_runs SET version=version+1 WHERE run_id=?", (run_id,))
        return original(run_id, action, expected_version)

    monkeypatch.setattr(library.env.coordinator, "request_control", advance_progress_before_first_control)
    disabled = library.service.policy({"enabled": False, "expected_version": library.service.state()["policy"]["version"]})
    assert disabled["enabled"] is False and raced
    unit_id = library.db.conn.execute("SELECT unit_id FROM work_analysis_units WHERE run_id=? LIMIT 1", (run["run_id"],)).fetchone()[0]
    # A successful disable must have durably revoked further work, even if its
    # first internal stop snapshot lost a race to an ordinary progress update.
    with pytest.raises(WorkAnalysisError):
        library.db.begin_work_analysis_unit(lease, unit_id)


def test_policy_lifetime_cap_includes_child_increases_across_scope_and_restart(review_library):
    library = review_library
    enable(library)
    library.service.tick()
    run = created_runs(library.db)[0]
    state = library.service.state()["policy"]
    library.service.policy({"enabled": False, "expected_version": state["version"]})
    current = library.db.get_work_analysis(run["run_id"])
    increased = {"request_limit": 20, "token_limit": 600000, "active_seconds_limit": 900}
    library.db.update_work_analysis_budget(run["run_id"], increased, current["version"])
    assert library.service.state()["policy"]["allocated"] == increased

    # Scope/model policy edits cannot turn lifetime reservations into a new budget.
    enabled = enable(library, scope="new", batch_budget=increased, work_limit=1)
    assert enabled["allocated"] == increased and enabled["work_count"] == 1
    add_novel(library.db, 777)
    library.service.tick()
    assert len(created_runs(library.db)) == 1
    current = library.db.get_work_analysis(run["run_id"])
    with pytest.raises(WorkAnalysisError, match="library_budget_exhausted"):
        library.db.update_work_analysis_budget(run["run_id"], {**increased, "request_limit": 21}, current["version"])
    assert library.service.state()["policy"]["allocated"] == increased
    assert library.db.get_work_analysis(run["run_id"])["budget"] == increased

    restarted = LibraryAnalysisService(library.factory, lambda: library.env.ai, RecordingCoordinator(library.factory))
    restarted.startup_recover()
    assert restarted.state()["policy"]["allocated"] == increased
    assert restarted.state()["policy"]["work_count"] == 1


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_batch_stop_preserves_its_send_fence_when_a_child_progress_version_races(review_library, monkeypatch, action):
    library = review_library
    batch = library.service.create(confirmed(library))
    library.service.tick()
    run = created_runs(library.db)[0]
    lease = library.db.claim_work_analysis(run["run_id"], now=datetime.now(timezone.utc), slice_seconds=30)
    original = library.env.coordinator.request_control
    raced = []

    def racing_control(run_id, child_action, expected_version):
        if not raced:
            raced.append(True)
            with library.db.transaction() as conn:
                conn.execute("UPDATE work_analysis_runs SET version=version+1 WHERE run_id=?", (run_id,))
        return original(run_id, child_action, expected_version)

    monkeypatch.setattr(library.env.coordinator, "request_control", racing_control)
    current = library.service.batch(batch["batch_id"])["batch"]
    stopped = library.service.control(batch["batch_id"], action, current["version"])
    assert stopped["status"] == ("paused" if action == "pause" else "cancelled")
    unit_id = library.db.conn.execute("SELECT unit_id FROM work_analysis_units WHERE run_id=? LIMIT 1", (run["run_id"],)).fetchone()[0]
    with pytest.raises(WorkAnalysisError):
        library.db.begin_work_analysis_unit(lease, unit_id)

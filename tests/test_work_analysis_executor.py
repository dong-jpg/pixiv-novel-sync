import hashlib
from contextlib import contextmanager

import pytest

from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus, JobType
from pixiv_novel_sync.jobs.runner import JobRunner
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError
from pixiv_novel_sync.work_analysis.executor import WorkAnalysisExecutor
from work_analysis_runtime_helpers import make_runtime_environment


def run_environment(env):
    manager = JobManager()
    job = manager.submit(JobSpec(
        JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": env.run_id},
    ))
    executor = WorkAnalysisExecutor(env.db_factory, lambda: env.service)
    JobRunner(manager, lambda task, context: executor.run(env.run_id, context)).run(job.job_id)
    return manager, job


def test_whole_path_publishes_one_validated_root_and_one_paid_call(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch)
    try:
        manager, job = run_environment(env)
        report = env.db.get_work_analysis_report(env.run_id)
        assert job.status == JobStatus.SUCCEEDED
        assert len(env.provider.sent_units) == 1
        assert report["artifact"]["is_final"] is True
        assert report["coverage"]["complete"] is True
        assert report["coverage"]["ratio"] == 1
        assert report["synthesis_complete"] is True
        assert report["usage_summary"]["network_request_count"] == 1
        row = env.db.conn.execute("SELECT output_text,output_json FROM ai_jobs").fetchone()
        assert tuple(row) == (None, None)
    finally:
        env.close()


def test_segmented_resume_reuses_validated_units_and_persisted_graph(tmp_path, monkeypatch):
    env = make_runtime_environment(
        tmp_path, monkeypatch, texts=[("chapter A\n" * 350), ("chapter B\n" * 350)], context_window=2500,
    )
    try:
        assert env.plan["strategy"] == "segmented"
        assert len(env.plan["original_units"]) >= 2
        env.provider.bad_calls = {2}
        manager, failed_job = run_environment(env)
        failed = env.db.get_work_analysis(env.run_id, include_private=True)
        assert failed["status"] == "failed"
        first = env.provider.sent_units[0]
        assert next(row for row in failed["units"] if row["unit"]["unit_id"] == first)["status"] == "succeeded"
        assert failed["report"] is None
        env.db.resume_work_analysis(env.run_id, failed["version"])
        env.provider.bad_calls.clear()
        manager, completed_job = run_environment(env)
        assert completed_job.status == JobStatus.SUCCEEDED
        assert env.provider.sent_units.count(first) == 1
        completed = env.db.get_work_analysis(env.run_id, include_private=True)
        roots = [row for row in completed["units"] if row["unit"]["is_final"]]
        assert len(roots) == 1 and roots[0]["status"] == "succeeded"
        assert any(row["unit"]["kind"] == "reduce" for row in completed["units"])
        assert env.db.get_work_analysis_report(env.run_id)["synthesis_complete"] is True
    finally:
        env.close()


def test_shared_finalization_claim_precedes_sensitive_publish(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch)
    manager = JobManager()
    job = manager.submit(JobSpec(JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": env.run_id}))
    original = Database.publish_work_analysis
    claims_seen = []

    def checked_publish(self, lease, report):
        claims_seen.append(manager.request_cancel(job.job_id))
        return original(self, lease, report)

    monkeypatch.setattr(Database, "publish_work_analysis", checked_publish)
    try:
        executor = WorkAnalysisExecutor(env.db_factory, lambda: env.service)
        JobRunner(manager, lambda task, context: executor.run(env.run_id, context)).run(job.job_id)
        assert claims_seen == [False]
        assert job.status == JobStatus.SUCCEEDED
    finally:
        env.close()


def test_source_change_still_wins_after_memory_finalization_claim(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch)
    original = Database.publish_work_analysis

    def change_source_then_publish(self, lease, report):
        writer = env.db_factory()
        try:
            body = "replacement synthetic body"
            with writer.transaction() as conn:
                conn.execute("UPDATE novel_texts SET text_raw=?,text_hash=? WHERE novel_id=1",
                             (body, hashlib.sha256(body.encode("utf-8")).hexdigest()))
        finally:
            writer.close()
        return original(self, lease, report)

    monkeypatch.setattr(Database, "publish_work_analysis", change_source_then_publish)
    try:
        manager, job = run_environment(env)
        state = env.db.get_work_analysis(env.run_id, include_private=True)
        assert state["status"] == "invalidated"
        assert state["report"] is None
        assert job.status != JobStatus.SUCCEEDED
        assert manager.acquire_run_slot() is True
        manager.release_run_slot()
    finally:
        env.close()


def test_exhausted_failed_run_refuses_resume_without_a_second_send(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch, request_limit=1)
    env.provider.bad_calls = {1}
    try:
        run_environment(env)
        state = env.db.get_work_analysis(env.run_id)
        assert state["status"] == "failed"
        with pytest.raises(WorkAnalysisError) as caught:
            env.db.resume_work_analysis(env.run_id, state["version"])
        assert caught.value.code == "budget_exhausted"
        after = env.db.get_work_analysis(env.run_id)
        assert after["status"] == "failed" and after["version"] == state["version"]
        assert len(env.provider.sent_units) == 1
        assert after["request_count"] == 1
    finally:
        env.close()


def test_segmented_executor_budget_refusal_pauses_without_a_second_send(tmp_path, monkeypatch):
    env = make_runtime_environment(
        tmp_path, monkeypatch, texts=[("chapter A\n" * 350), ("chapter B\n" * 350)],
        context_window=2500, token_limit=2500,
    )
    try:
        assert env.plan["strategy"] == "segmented" and len(env.plan["original_units"]) >= 2
        manager, job = run_environment(env)
        state = env.db.get_work_analysis(env.run_id, include_private=True)
        assert state["status"] == "paused" and state["reason"] == "budget_exhausted"
        assert job.status == JobStatus.CANCELLED
        assert len(env.provider.sent_units) == state["request_count"] == 1
        assert 1250 < state["planned_tokens"] <= 2500
        assert sum(item["status"] == "succeeded" for item in state["units"]) == 1
        assert state["report"] is None
    finally:
        env.close()


@pytest.mark.parametrize("action, status, reason", [
    ("pause", "paused", "user_pause"),
    ("cancel", "cancelled", "user_cancel"),
    ("sync_yield", "paused", "sync_yield"),
])
def test_control_stop_settles_once_outside_short_scope(tmp_path, monkeypatch, action, status, reason):
    env = make_runtime_environment(tmp_path, monkeypatch)
    manager = JobManager()
    job = manager.submit(JobSpec(JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": env.run_id}))
    scope_entries, settlements = [], []
    inside_scope = False
    original_settle = Database.settle_work_analysis

    @contextmanager
    def control_scope():
        nonlocal inside_scope
        assert not inside_scope
        inside_scope = True
        scope_entries.append(True)
        try:
            yield
        finally:
            inside_scope = False

    def stop(_unit_id):
        assert not inside_scope
        assert manager.request_cancel(job.job_id)
        current = env.db.get_work_analysis(env.run_id)
        env.db.request_work_analysis_stop(env.run_id, action, current["version"])

    def settle(self, lease, **kwargs):
        assert not inside_scope
        settlements.append(kwargs)
        return original_settle(self, lease, **kwargs)

    def execute(task, context):
        context["work_control_scope"] = control_scope
        return WorkAnalysisExecutor(env.db_factory, lambda: env.service).run(env.run_id, context)

    monkeypatch.setattr(Database, "settle_work_analysis", settle)
    env.provider.before_response = stop
    try:
        JobRunner(manager, execute).run(job.job_id)
        state = env.db.get_work_analysis(env.run_id, include_private=True)
        assert (state["status"], state["reason"]) == (status, reason)
        assert job.status == JobStatus.CANCELLED
        assert (job.progress["work_status"], job.progress["work_reason"]) == (status, reason)
        assert len(scope_entries) == 2 and len(settlements) == 1
        assert state["slice_settled"] and state["owner_token"] is None
        assert state["active_seconds_debited"] < 600
        assert state["report"] is None and state["request_count"] == 1
        assert manager.acquire_run_slot()
        manager.release_run_slot()
    finally:
        env.close()


def test_deleted_during_transport_cannot_restore_artifacts_or_refund(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch)

    def delete(_unit_id):
        current = env.db.get_work_analysis(env.run_id)
        env.db.request_work_analysis_stop(env.run_id, "delete", current["version"])

    env.provider.before_response = delete
    try:
        manager, job = run_environment(env)
        state = env.db.get_work_analysis(env.run_id, include_private=True)
        assert state["deleted"] and state["report"] is None
        assert all(row["status"] == "discarded" and row["unit"] is None
                   and row["artifact"] is None and row["child_job_id"] is None for row in state["units"])
        assert state["active_seconds_debited"] == 600
        assert state["owner_token"] is None and state["slice_settled"]
        assert job.status == JobStatus.CANCELLED
        assert len(env.provider.sent_units) == 1
    finally:
        env.close()


def test_configuration_change_during_transport_invalidates_without_refund(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch)

    def change(_unit_id):
        with env.db.transaction() as conn:
            conn.execute("UPDATE ai_agents SET system_prompt=? WHERE id=?", ("Changed synthetic prompt", env.agent_id))

    env.provider.before_response = change
    try:
        manager, job = run_environment(env)
        state = env.db.get_work_analysis(env.run_id, include_private=True)
        assert (state["status"], state["reason"]) == ("invalidated", "configuration_changed")
        assert state["report"] is None and state["active_seconds_debited"] == 600
        assert job.status == JobStatus.CANCELLED
        assert len(env.provider.sent_units) == 1
    finally:
        env.close()


def test_failed_reducer_resumes_durable_graph_without_resending_originals(tmp_path, monkeypatch):
    env = make_runtime_environment(
        tmp_path, monkeypatch, texts=["chapter A\n" * 350, "chapter B\n" * 350], context_window=2500,
    )
    original_count = len(env.plan["original_units"])
    env.provider.bad_calls = {original_count + 1}
    try:
        manager, failed_job = run_environment(env)
        failed = env.db.get_work_analysis(env.run_id, include_private=True)
        assert failed_job.status == JobStatus.FAILED
        originals = [row for row in failed["units"] if row["unit"]["kind"] == "extract"]
        assert len(originals) == original_count and all(row["status"] == "succeeded" for row in originals)
        reducers = [row["unit"] for row in failed["units"] if row["unit"]["kind"] == "reduce"]
        assert len(reducers) == 1 and failed["report"] is None
        env.db.resume_work_analysis(env.run_id, failed["version"])
        env.provider.bad_calls.clear()
        manager, job = run_environment(env)
        complete = env.db.get_work_analysis(env.run_id, include_private=True)
        assert job.status == JobStatus.SUCCEEDED
        assert reducers == [row["unit"] for row in complete["units"] if row["unit"]["kind"] == "reduce"]
        assert all(env.provider.sent_units.count(row["unit"]["unit_id"]) == 1 for row in originals)
        assert env.provider.sent_units.count(reducers[0]["unit_id"]) == 2
    finally:
        env.close()


def test_real_reducer_depth_bound_pauses_without_a_ninth_level(tmp_path, monkeypatch):
    env = make_runtime_environment(
        tmp_path, monkeypatch, texts=["chapter A\n" * 350, "chapter B\n" * 350], context_window=2500,
    )
    env.provider.summary = "\u754c" * 2000
    try:
        manager, job = run_environment(env)
        state = env.db.get_work_analysis(env.run_id, include_private=True)
        assert (state["status"], state["reason"]) == ("paused", "synthesis_capacity_exceeded")
        assert job.status == JobStatus.CANCELLED and state["report"] is None
        assert len(env.provider.sent_units) == 9 * len(env.plan["original_units"])
        assert max(row["unit"]["level"] for row in state["units"]) == 8
        assert all(row["status"] == "succeeded" for row in state["units"])
    finally:
        env.close()


def test_executor_rejects_non_web_runner_before_claim(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch)
    manager = JobManager()
    job = manager.submit(JobSpec(JobSource.CLI, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": env.run_id}))
    try:
        executor = WorkAnalysisExecutor(env.db_factory, lambda: env.service)
        JobRunner(manager, lambda task, context: executor.run(env.run_id, context)).run(job.job_id)
        state = env.db.get_work_analysis(env.run_id, include_private=True)
        assert job.status == JobStatus.FAILED
        assert job.error == "work_analysis_requires_shared_web_runner"
        assert state["status"] == "queued" and state["owner_token"] is None
        assert not env.provider.sent_units and state["active_seconds_debited"] == 0
    finally:
        env.close()

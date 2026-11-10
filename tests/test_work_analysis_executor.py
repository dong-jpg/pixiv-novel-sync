import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.ai.models import AIStreamChunk
from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus, JobType
from pixiv_novel_sync.jobs.runner import JobRunner
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.work_analysis import executor as executor_module
from pixiv_novel_sync.work_analysis.artifacts import validate_artifact
from pixiv_novel_sync.work_analysis.configuration import snapshot_work_configuration
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError, canonical_bytes
from pixiv_novel_sync.work_analysis.evidence import validate_unit_output
from pixiv_novel_sync.work_analysis.executor import WorkAnalysisExecutor
from pixiv_novel_sync.work_analysis.planning import materialize_unit, seal_unit
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


def test_real_16_mib_boundary_segments_before_sending_every_original_character(tmp_path, monkeypatch):
    limit = 16 * 1024 * 1024
    # Nested JSON escaping exceeds the byte cap although the raw source does not.
    texts = {1: '"' * (4 * 1024 * 1024) + "\U0001f600TAIL", 2: "short final chapter"}
    assert len(texts[1].encode("utf-8")) < limit
    assert len(canonical_bytes(canonical_bytes(texts[1]).decode("utf-8"))) > limit
    env = make_runtime_environment(
        tmp_path, monkeypatch, texts=list(texts.values()),
        context_window=10_000_000, token_limit=100_000_000,
    )
    try:
        with env.db.read_transaction():
            _, prompt, preflight = snapshot_work_configuration(env.db.conn, env.agent_id, 128)
        whole = seal_unit({
            "kind": "whole", "is_final": True, "level": 0,
            "primary_ranges": [{
                "novel_id": chapter["novel_id"], "text_hash": chapter["text_hash"],
                "start": 0, "end": chapter["chars"],
            } for chapter in env.manifest["chapters"]],
            "context_ranges": [], "child_unit_ids": [], "claim_refs": [],
        }, manifest_fingerprint=env.manifest["manifest_fingerprint"])
        with pytest.raises(WorkAnalysisError, match="input_too_large") as caught:
            materialize_unit(env.manifest, whole, texts, {}, agent_prompt=prompt, output_tokens=128)
        assert caught.value.status == 413

        assert env.plan["strategy"] == "segmented"
        originals = env.plan["original_units"]
        assert len(originals) > len(texts)
        state = env.db.get_work_analysis(env.run_id, include_private=True)
        assert [entry["unit"] for entry in state["units"]] == originals
        for novel_id, text in texts.items():
            cursor = 0
            ranges = sorted(
                (row["start"], row["end"]) for unit in originals
                for row in unit["primary_ranges"] if row["novel_id"] == novel_id
            )
            for start, end in ranges:
                assert start == cursor and start < end <= len(text)
                cursor = end
            assert cursor == len(text)

        original_payload_hashes = {}
        for unit in originals:
            assert unit["kind"] == "extract" and not unit["is_final"]
            payload, blocks = materialize_unit(
                env.manifest, unit, texts, {}, agent_prompt=prompt, output_tokens=128,
            )
            serialized = canonical_bytes(payload)
            assert len(payload["messages"]) <= 256 and len(serialized) <= limit
            assert preflight(payload)["can_run"]
            assert all(block["text"] == texts[block["novel_id"]][block["start"]:block["end"]]
                       for block in blocks.values())
            actual_blocks = json.loads(payload["messages"][-1]["content"])["blocks"]
            assert actual_blocks == [
                {"block_id": block_id, "novel_id": block["novel_id"], "text": block["text"]}
                for block_id, block in blocks.items()
            ]
            for primary in unit["primary_ranges"]:
                cursor = primary["start"]
                primary_blocks = sorted(
                    (block["start"], block["end"]) for block in blocks.values()
                    if block["novel_id"] == primary["novel_id"]
                    and primary["start"] <= block["start"] < block["end"] <= primary["end"]
                )
                for start, end in primary_blocks:
                    assert start == cursor
                    cursor = end
                assert cursor == primary["end"]
            original_payload_hashes[unit["unit_id"]] = hashlib.sha256(serialized).hexdigest()

        # A single extra source character must cross the actual serialization guard.
        first = originals[0]
        fields = {key: value for key, value in first.items() if key not in {"unit_id", "input_fingerprint"}}
        fields["primary_ranges"] = [{**first["primary_ranges"][0], "end": first["primary_ranges"][0]["end"] + 1}]
        too_wide = seal_unit(fields, manifest_fingerprint=env.manifest["manifest_fingerprint"])
        with pytest.raises(WorkAnalysisError, match="input_too_large"):
            materialize_unit(env.manifest, too_wide, texts, {}, agent_prompt=prompt, output_tokens=128)

        original_stream = env.provider.stream_generate
        sent_payloads = []

        def bounded_stream(messages, model, temperature, top_p, max_tokens, **kwargs):
            serialized = canonical_bytes({"messages": messages, "max_tokens": max_tokens})
            unit_id = json.loads(messages[-1]["content"])["unit_id"]
            sent_payloads.append((unit_id, len(serialized)))
            assert unit_id != whole["unit_id"]
            assert len(messages) <= 256 and len(serialized) <= limit
            if unit_id in original_payload_hashes:
                assert hashlib.sha256(serialized).hexdigest() == original_payload_hashes[unit_id]
            yield from original_stream(messages, model, temperature, top_p, max_tokens, **kwargs)

        monkeypatch.setattr(env.provider, "stream_generate", bounded_stream)
        _, job = run_environment(env)
        assert job.status == JobStatus.SUCCEEDED
        assert [unit_id for unit_id, _ in sent_payloads] == env.provider.sent_units
        assert [unit_id for unit_id, _ in sent_payloads if unit_id in original_payload_hashes] == [
            unit["unit_id"] for unit in originals
        ]
        report = env.db.get_work_analysis_report(env.run_id)
        assert report["coverage"]["validated_chars"] == report["coverage"]["available_chars"] == sum(map(len, texts.values()))
        assert report["coverage"]["complete"] and report["coverage"]["ratio"] == 1
        assert report["synthesis_complete"]
        assert report["usage_summary"]["network_request_count"] == len(sent_payloads)
    finally:
        env.close()


def test_valid_json_partial_transport_cannot_checkpoint_or_automatically_retry(tmp_path, monkeypatch):
    text = "synthetic complete source for a partial transport result"
    env = make_runtime_environment(tmp_path, monkeypatch, texts=[text], request_limit=3)
    try:
        unit = env.plan["original_units"][0]
        assert env.plan["strategy"] == "whole"
        _, blocks = materialize_unit(env.manifest, unit, {1: text}, {}, agent_prompt="", output_tokens=128)
        original_stream = env.provider.stream_generate
        valid_outputs = []

        def partial_stream(*args, **kwargs):
            for chunk in original_stream(*args, **kwargs):
                if chunk.type == "delta":
                    valid_outputs.append(validate_unit_output(chunk.text, unit, blocks, {}))
                if chunk.type == "done":
                    chunk = AIStreamChunk(type="done", data={"finish_reason": "length"})
                yield chunk

        monkeypatch.setattr(env.provider, "stream_generate", partial_stream)
        _, job = run_environment(env)
        state = env.db.get_work_analysis(env.run_id, include_private=True)
        assert len(valid_outputs) == 1 and valid_outputs[0]["is_final"]
        assert job.status == JobStatus.FAILED and job.error == "transport_failed"
        assert (state["status"], state["reason"]) == ("failed", "transport_failed")
        assert state["report"] is None and not state["has_report"]
        assert state["unit_counts"] == {"total": 1, "succeeded": 0}
        entry = state["units"][0]
        assert entry["artifact"] is None and entry["completed_at"] is None
        assert entry["try_no"] == 1 and entry["last_error"] == "transport_failed"
        assert state["coverage"]["validated_chars"] == 0 and state["coverage"]["ratio"] == 0
        assert state["coverage"]["available_chars"] == len(text) and not state["coverage"]["complete"]
        with pytest.raises(WorkAnalysisError, match="report_not_ready"):
            env.db.get_work_analysis_report(env.run_id)

        child = env.db.conn.execute("SELECT * FROM ai_jobs").fetchall()
        assert len(child) == 1 and child[0]["status"] == "partial"
        assert child[0]["output_text"] is None and child[0]["output_json"] is None
        attempts = env.db.conn.execute("SELECT status,finish_reason FROM ai_job_model_attempts").fetchall()
        assert [tuple(row) for row in attempts] == [("partial", "length")]
        ledger = env.db.conn.execute(
            "SELECT entry_kind,request_ordinal,planned_tokens FROM work_analysis_requests "
            "ORDER BY child_job_id,request_ordinal"
        ).fetchall()
        assert [(row["entry_kind"], row["request_ordinal"]) for row in ledger] == [("attachment", 0), ("request", 1)]
        assert ledger[0]["planned_tokens"] is None
        assert ledger[1]["planned_tokens"] == state["planned_tokens"] > 0
        assert env.provider.sent_units == [unit["unit_id"]]
        assert state["request_count"] == child[0]["network_request_count"] == 1
        assert state["request_count"] < state["budget"]["request_limit"]
    finally:
        env.close()


def test_saved_final_root_resumes_publication_without_new_provider_calls(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch)
    publish_attempts = []
    original_publish = Database.publish_work_analysis

    def fail_once_before_publish(self, lease, artifact):
        publish_attempts.append(artifact)
        if len(publish_attempts) == 1:
            raise RuntimeError("synthetic failure before publication")
        return original_publish(self, lease, artifact)

    try:
        assert env.plan["strategy"] == "whole"
        with monkeypatch.context() as failure:
            failure.setattr(Database, "publish_work_analysis", fail_once_before_publish)
            _, failed_job = run_environment(env)
        failed = env.db.get_work_analysis(env.run_id, include_private=True)
        assert failed_job.status == JobStatus.FAILED and failed["status"] == "failed"
        assert failed["report"] is None
        assert len(failed["units"]) == 1
        root = failed["units"][0]
        assert root["status"] == "succeeded" and root["unit"]["is_final"]
        saved_artifact = root["artifact"]
        assert validate_artifact(saved_artifact, final=True) == saved_artifact
        assert publish_attempts == [saved_artifact]
        child_count = env.db.conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0]
        ledger_sql = "SELECT * FROM work_analysis_requests ORDER BY child_job_id,request_ordinal"
        ledger = [dict(row) for row in env.db.conn.execute(ledger_sql)]
        assert [(row["entry_kind"], row["request_ordinal"]) for row in ledger] == [("attachment", 0), ("request", 1)]
        assert child_count == len(env.provider.sent_units) == failed["request_count"] == 1
        assert ledger[0]["planned_tokens"] is None
        assert ledger[1]["planned_tokens"] == failed["planned_tokens"] > 0
        assert failed["request_count"] < failed["budget"]["request_limit"]
        assert failed["planned_tokens"] < failed["budget"]["token_limit"]
        sent_units = list(env.provider.sent_units)
        provider_entries = []

        def forbidden_provider_entry(*args, **kwargs):
            provider_entries.append(True)
            raise AssertionError("saved final root must not re-enter the Provider")

        monkeypatch.setattr(env.provider, "stream_generate", forbidden_provider_entry)
        resumed = env.db.resume_work_analysis(env.run_id, failed["version"])
        assert resumed["status"] == "queued"
        _, completed_job = run_environment(env)
        completed = env.db.get_work_analysis(env.run_id, include_private=True)
        report = env.db.get_work_analysis_report(env.run_id)
        assert completed_job.job_id != failed_job.job_id
        assert completed_job.status == JobStatus.SUCCEEDED and completed["status"] == "succeeded"
        assert report["artifact"] == saved_artifact
        assert report["coverage"]["complete"] and report["synthesis_complete"]
        assert completed["units"][0]["artifact"] == saved_artifact
        assert provider_entries == [] and env.provider.sent_units == sent_units
        assert env.db.conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == child_count
        assert [dict(row) for row in env.db.conn.execute(ledger_sql)] == ledger
        assert completed["request_count"] == failed["request_count"]
        assert completed["planned_tokens"] == failed["planned_tokens"]
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


def test_cancelled_child_after_deletion_refreshes_the_cached_parent_stop(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch, request_limit=3)
    # Freeze only the executor clock, leaving the real Router/heartbeat clock intact.
    monkeypatch.setattr(executor_module, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    original_stream = env.provider.stream_generate
    original_invoke = env.service.invoke_text
    results = []

    def delete(_unit_id):
        current = env.db.get_work_analysis(env.run_id)
        env.db.request_work_analysis_stop(env.run_id, "delete", current["version"])

    def cancelled_stream(*args, **kwargs):
        for chunk in original_stream(*args, **kwargs):
            if chunk.type == "done":
                chunk = AIStreamChunk(type="done", data={"finish_reason": "cancelled"})
            yield chunk

    def invoke(*args, **kwargs):
        result = original_invoke(*args, **kwargs)
        pulse = kwargs["stop_requested"].__self__
        assert pulse.next_check == 1001.0
        assert pulse.check() is False
        results.append((result.finish_state, result.job_status, result.work_error_code))
        return result

    env.provider.before_response = delete
    monkeypatch.setattr(env.provider, "stream_generate", cancelled_stream)
    monkeypatch.setattr(env.service, "invoke_text", invoke)
    try:
        _, job = run_environment(env)
        assert results == [("cancelled", "cancelled", None)]
        state = env.db.get_work_analysis(env.run_id, include_private=True)
        assert state["deleted"] and state["status"] == "cancelled"
        assert state["report"] is None and state["coverage"] is None
        assert all(row["status"] == "discarded" and row["unit"] is None
                   and row["artifact"] is None and row["child_job_id"] is None for row in state["units"])
        assert state["active_seconds_debited"] == 600
        assert state["owner_token"] is None and state["slice_settled"]
        assert state["request_count"] == len(env.provider.sent_units) == 1
        ledger = env.db.conn.execute(
            "SELECT request_ordinal,planned_tokens FROM work_analysis_requests WHERE entry_kind='request'",
        ).fetchall()
        assert len(ledger) == 1 and ledger[0]["request_ordinal"] == 1
        assert ledger[0]["planned_tokens"] == state["planned_tokens"] > 0
        assert job.status == JobStatus.CANCELLED
        assert job.progress["work_status"] == "cancelled"
        assert job.progress["work_reason"] == "deleted"
        assert job.error is None
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

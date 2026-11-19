from __future__ import annotations

import hashlib
import json
from collections import Counter
from contextlib import contextmanager

import pytest

from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus, JobType
from pixiv_novel_sync.jobs.runner import JobRunner
from pixiv_novel_sync.ai.usage import ProviderUsage
from pixiv_novel_sync.storage.work_analysis import units as storage_units
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.work_analysis import executor as executor_module
from pixiv_novel_sync.work_analysis import manifest as manifest_module
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError
from pixiv_novel_sync.work_analysis.executor import WorkAnalysisExecutor
from pixiv_novel_sync.work_analysis.planning import materialize_unit, plan_original_units
from work_analysis_domain_helpers import capacity_preflight, source_fixture
from work_analysis_runtime_helpers import make_runtime_environment


def _run(env):
    manager = JobManager()
    job = manager.submit(JobSpec(
        JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": env.run_id},
    ))
    executor = WorkAnalysisExecutor(env.db_factory, lambda: env.service)
    JobRunner(manager, lambda task, context: executor.run(env.run_id, context)).run(job.job_id)
    return job


def _segmented_environment(tmp_path, monkeypatch):
    env = make_runtime_environment(
        tmp_path, monkeypatch,
        texts=[f"chapter {index} \U0001f600\n" * 350 for index in range(1, 7)],
        context_window=2500,
    )
    assert env.plan["strategy"] == "segmented"
    assert len(env.plan["original_units"]) >= 6
    return env


def test_segmented_runtime_reads_only_referenced_chapters_and_no_reducer_bodies(tmp_path, monkeypatch):
    env = _segmented_environment(tmp_path, monkeypatch)
    reads = []
    phase = "other"
    fetch = manifest_module._fetch_texts
    execute = WorkAnalysisExecutor._run_unit
    save = Database.save_work_analysis_unit

    @contextmanager
    def reading(name):
        nonlocal phase
        previous, phase = phase, name
        try:
            yield
        finally:
            phase = previous

    def measured_fetch(conn, ids):
        texts, metadata = fetch(conn, ids)
        reads.append((phase, tuple(ids), sum(len(text.encode("utf-8")) for text in texts.values())))
        return texts, metadata

    def measured_execute(self, lease, state, entry, pulse):
        with reading("executor_" + entry["unit"]["kind"]):
            return execute(self, lease, state, entry, pulse)

    def measured_save(self, lease, unit_id, artifact, **kwargs):
        with reading("checkpoint"):
            return save(self, lease, unit_id, artifact, **kwargs)

    monkeypatch.setattr(manifest_module, "_fetch_texts", measured_fetch)
    monkeypatch.setattr(WorkAnalysisExecutor, "_run_unit", measured_execute)
    monkeypatch.setattr(Database, "save_work_analysis_unit", measured_save)
    try:
        job = _run(env)
        assert job.status == JobStatus.SUCCEEDED
        state = env.db.get_work_analysis(env.run_id, include_private=True)
        assert any(entry["unit"]["kind"] == "reduce" for entry in state["units"])
        expected_chapters = Counter(
            row["novel_id"]
            for unit in env.plan["original_units"]
            for row in env.manifest["chapters"]
            if row["novel_id"] in {
                part["novel_id"] for part in unit["primary_ranges"] + unit["context_ranges"]
            }
        )
        expected_bytes = sum(
            expected_chapters[row["novel_id"]] * row["utf8_bytes"] for row in env.manifest["chapters"]
        )
        assert {name for name, _, _ in reads} == {"executor_extract", "checkpoint"}
        measured = {}
        for name in ("executor_extract", "checkpoint"):
            selected = [(ids, size) for phase, ids, size in reads if phase == name]
            assert Counter(novel_id for ids, _ in selected for novel_id in ids) == expected_chapters
            measured[name] = {"reads": len(selected), "rows": sum(map(len, (ids for ids, _ in selected))),
                              "bytes": sum(size for _, size in selected)}
            assert measured[name]["reads"] == len(env.plan["original_units"])
            assert measured[name]["bytes"] == expected_bytes
        # 旧路径在执行和检查点各读取一次整部作品，包括每一个汇总单元。
        legacy_reads = 2 * len(state["units"])
        legacy_rows = legacy_reads * len(env.manifest["chapters"])
        legacy_bytes = legacy_reads * sum(row["utf8_bytes"] for row in env.manifest["chapters"])
        assert len(reads) < legacy_reads
        assert sum(value["rows"] for value in measured.values()) * 4 < legacy_rows
        assert sum(value["bytes"] for value in measured.values()) * 4 < legacy_bytes
        report = env.db.get_work_analysis_report(env.run_id)
        assert report["coverage"]["complete"] and report["synthesis_complete"]
        assert report["usage_summary"]["network_request_count"] == len(state["units"])
        print(json.dumps({"measured": measured, "legacy_full_reads": {
            "reads": legacy_reads, "rows": legacy_rows, "bytes": legacy_bytes,
        }}, sort_keys=True))
    finally:
        env.close()


def test_multi_unit_usage_reads_one_ledger_and_preserves_unknown_and_zero(tmp_path, monkeypatch):
    env = _segmented_environment(tmp_path, monkeypatch)
    try:
        pending = env.db.list_work_analysis_units(env.run_id)
        assert len(pending["items"]) >= 6
        assert all(item["usage_summary"]["network_request_count"] == 0
                   and item["usage_summary"]["known_total_tokens"] is None for item in pending["items"])
        assert _run(env).status == JobStatus.SUCCEEDED
        ledger = env.db.conn.execute(
            "SELECT child_job_id,request_ordinal,unit_id FROM work_analysis_requests "
            "WHERE run_id=? AND entry_kind='request' ORDER BY reserved_at,child_job_id,request_ordinal",
            (env.run_id,),
        ).fetchall()
        assert len(ledger) >= 7
        zero = ProviderUsage(input_tokens=0, output_tokens=0, total_tokens=0,
                             complete=True, missing_reason=None).to_dict()
        unknown = ProviderUsage(missing_reason="interrupted").to_dict()
        with env.db.transaction() as conn:
            for entry, usage in zip(ledger[:2], (zero, unknown)):
                conn.execute("UPDATE work_analysis_requests SET usage_json=? "
                             "WHERE child_job_id=? AND request_ordinal=?",
                             (json.dumps(usage), entry["child_job_id"], entry["request_ordinal"]))

        readers = {
            "report": lambda: env.db.get_work_analysis_report(env.run_id),
            "private_detail": lambda: env.db.get_work_analysis(env.run_id, include_private=True),
            "unit_page": lambda: env.db.list_work_analysis_units(env.run_id, limit=100),
        }
        measured = {}
        for name, read in readers.items():
            statements = []
            env.db.conn.set_trace_callback(statements.append)
            try:
                result = read()
            finally:
                env.db.conn.set_trace_callback(None)
            measured[name] = len([sql for sql in statements
                                  if sql.lstrip().upper().startswith("SELECT ")
                                  and "FROM work_analysis_requests" in sql])
            assert measured[name] == 1
            by_unit = {item["unit_id"]: item["usage_summary"]
                       for item in result["items" if name == "unit_page" else "units"]}
            assert len(by_unit) == len(ledger)
            for index, entry in enumerate(ledger):
                usage = by_unit[entry["unit_id"]]
                assert usage["network_request_count"] == 1
                assert usage["known_total_tokens"] == (0 if index == 0 else None if index == 1 else 12)
                assert usage["complete"] is (index != 1)
            if name != "unit_page":
                total = result["usage_summary"]
                assert total["network_request_count"] == len(ledger)
                assert total["known_total_tokens"] == (len(ledger) - 2) * 12
                assert total["reported_requests"] == len(ledger) - 1
                assert total["unreported_requests"] == 1
                assert total["complete"] is False
        page = env.db.list_work_analysis_units(env.run_id, offset=2, limit=2)
        assert page["total"] == len(ledger) and len(page["items"]) == 2
        print(json.dumps({"units": len(ledger), "usage_queries": measured}, sort_keys=True))
    finally:
        env.close()


def test_materialize_subset_keeps_context_and_full_chapter_hash_verification():
    manifest, texts = source_fixture(["chapter \U0001f600\n" * 1000, "another chapter" * 300])
    plan = plan_original_units(manifest, texts, capacity_preflight(3600), agent_prompt="", output_tokens=128)
    unit = next(unit for unit in plan["original_units"] if unit["context_ranges"])
    selected = {part["novel_id"] for part in unit["primary_ranges"] + unit["context_ranges"]}
    subset = {key: value for key, value in texts.items() if key in selected}
    actual = materialize_unit(manifest, unit, subset, {}, agent_prompt="", output_tokens=128)
    assert actual == materialize_unit(manifest, unit, texts, {}, agent_prompt="", output_tokens=128)
    for context in unit["context_ranges"]:
        assert actual[1][context["block_id"]]["text"] == texts[context["novel_id"]][context["start"]:context["end"]]
    novel_id = next(iter(selected))
    # 即使修改位置不在单元片段中，也必须校验完整引用章节的哈希。
    corrupt = {**subset, novel_id: "X" + subset[novel_id][1:]}
    with pytest.raises(WorkAnalysisError, match="source_changed"):
        materialize_unit(manifest, unit, corrupt, {}, agent_prompt="", output_tokens=128)
    with pytest.raises(WorkAnalysisError, match="source_changed"):
        materialize_unit(manifest, unit, {}, {}, agent_prompt="", output_tokens=128)
    with pytest.raises(WorkAnalysisError, match="source_changed"):
        plan_original_units(manifest, subset, capacity_preflight(3600), agent_prompt="", output_tokens=128)


@pytest.mark.parametrize("mutation", ["body_only", "body_and_hash", "delete", "oversize"])
def test_selected_source_read_rejects_changed_or_oversize_chapter(tmp_path, monkeypatch, mutation):
    env = make_runtime_environment(tmp_path, monkeypatch, texts=["selected \U0001f600 body", "unrelated body"])
    fetch = manifest_module._fetch_texts
    fetched = []

    def measured_fetch(conn, ids):
        fetched.extend(ids)
        return fetch(conn, ids)

    monkeypatch.setattr(manifest_module, "_fetch_texts", measured_fetch)
    try:
        with env.db.read_transaction() as conn:
            assert manifest_module.read_source_texts(conn, env.manifest, novel_ids={1}) == {1: "selected \U0001f600 body"}
            assert fetched == [1]
            with pytest.raises(WorkAnalysisError, match="invalid_plan"):
                manifest_module.read_source_texts(conn, env.manifest, novel_ids={3})
        with env.db.transaction() as conn:
            if mutation == "delete":
                conn.execute("DELETE FROM novel_texts WHERE novel_id=1")
            elif mutation == "body_only":
                conn.execute("UPDATE novel_texts SET text_raw='different body' WHERE novel_id=1")
            else:
                body = "X" * 1025 if mutation == "oversize" else "different body"
                conn.execute("UPDATE novel_texts SET text_raw=?,text_hash=? WHERE novel_id=1",
                             (body, hashlib.sha256(body.encode("utf-8")).hexdigest()))
        if mutation == "oversize":
            monkeypatch.setattr(manifest_module, "MAX_BYTES", 1024)
        fetched.clear()
        with env.db.read_transaction() as conn:
            with pytest.raises(WorkAnalysisError, match="source_changed"):
                manifest_module.read_source_texts(conn, env.manifest, novel_ids={1})
        assert fetched == ([] if mutation == "oversize" else [1])
    finally:
        env.close()


@pytest.mark.parametrize("mutation", ["body_and_hash", "body_only", "delete"])
@pytest.mark.parametrize("boundary", ["before_send", "before_checkpoint_commit", "before_publish"])
def test_unread_chapter_mutation_still_revokes_run(tmp_path, monkeypatch, mutation, boundary):
    env = _segmented_environment(tmp_path, monkeypatch)
    original = env.plan["original_units"][0]
    referenced = {part["novel_id"] for part in original["primary_ranges"] + original["context_ranges"]}
    untouched_id = next(row["novel_id"] for row in env.manifest["chapters"] if row["novel_id"] not in referenced)
    mutations = []

    def change_source():
        if mutations:
            return
        mutations.append(untouched_id)
        with env.db.transaction() as conn:
            if mutation == "delete":
                conn.execute("DELETE FROM novel_texts WHERE novel_id=?", (untouched_id,))
            elif mutation == "body_only":
                conn.execute("UPDATE novel_texts SET text_raw='same stored hash, changed body' WHERE novel_id=?",
                             (untouched_id,))
            else:
                body = "replacement source body"
                conn.execute("UPDATE novel_texts SET text_raw=?,text_hash=? WHERE novel_id=?",
                             (body, hashlib.sha256(body.encode("utf-8")).hexdigest(), untouched_id))

    if boundary == "before_send":
        materialize = executor_module.materialize_unit

        def changed_materialize(*args, **kwargs):
            result = materialize(*args, **kwargs)
            change_source()
            return result

        monkeypatch.setattr(executor_module, "materialize_unit", changed_materialize)
    elif boundary == "before_checkpoint_commit":
        validate = storage_units._validate_checkpoint

        def changed_validate(*args, **kwargs):
            result = validate(*args, **kwargs)
            change_source()
            return result

        monkeypatch.setattr(storage_units, "_validate_checkpoint", changed_validate)
    else:
        publish = Database.publish_work_analysis

        def changed_publish(self, *args, **kwargs):
            change_source()
            return publish(self, *args, **kwargs)

        monkeypatch.setattr(Database, "publish_work_analysis", changed_publish)
    try:
        job = _run(env)
        state = env.db.get_work_analysis(env.run_id, include_private=True)
        assert mutations == [untouched_id]
        assert job.status != JobStatus.SUCCEEDED
        assert not state["has_report"]
        if mutation == "delete":
            assert state["deleted"] and state["reason"] == "source_deleted"
        else:
            assert state["status"] == "invalidated" and state["reason"] == "source_changed"
        if boundary == "before_send":
            assert env.provider.sent_units == []
        elif boundary == "before_checkpoint_commit":
            assert len(env.provider.sent_units) == 1
            assert state["unit_counts"]["succeeded"] == 0
    finally:
        env.close()


@pytest.mark.parametrize("boundary", ["before_send", "before_checkpoint_commit"])
def test_reducer_without_body_reads_still_rejects_source_changes(tmp_path, monkeypatch, boundary):
    env = _segmented_environment(tmp_path, monkeypatch)
    changed = []
    target = executor_module if boundary == "before_send" else storage_units
    materialize = target.materialize_unit

    def changed_materialize(manifest, unit, texts, children, **kwargs):
        result = materialize(manifest, unit, texts, children, **kwargs)
        if unit["kind"] == "reduce" and not changed:
            assert texts == {}
            changed.append(unit["unit_id"])
            with env.db.transaction() as conn:
                conn.execute("UPDATE novel_texts SET text_raw='changed while reducing' WHERE novel_id=6")
        return result

    monkeypatch.setattr(target, "materialize_unit", changed_materialize)
    try:
        job = _run(env)
        state = env.db.get_work_analysis(env.run_id, include_private=True)
        assert len(changed) == 1
        assert job.status != JobStatus.SUCCEEDED
        assert state["status"] == "invalidated" and not state["has_report"]
        entry = next(entry for entry in state["units"] if entry["unit"]["unit_id"] == changed[0])
        assert entry["status"] != "succeeded" and entry["artifact"] is None
        if boundary == "before_send":
            assert changed[0] not in env.provider.sent_units
        else:
            assert env.provider.sent_units[-1] == changed[0]
    finally:
        env.close()

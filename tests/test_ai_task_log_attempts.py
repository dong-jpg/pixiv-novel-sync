from __future__ import annotations

import json

from pixiv_novel_sync.storage_db import Database


def test_ai_task_logs_load_attempts_without_per_row_queries(tmp_path) -> None:
    db = Database(tmp_path / "ai-logs.db")
    db.init_schema()
    for job_id in ("job-a", "job-b"):
        db.conn.execute(
            """
            INSERT INTO ai_jobs (job_id, task_type, status, input_json, created_at, finished_at)
            VALUES (?, 'keyword_clean', 'failed', '{}', datetime('now'), datetime('now'))
            """,
            (job_id,),
        )

    def per_row(_job_id: str):
        raise AssertionError("不应按行查询尝试记录")

    db.list_ai_job_model_attempts = per_row
    db.get_ai_job_usage_summary = per_row
    payload = db.get_ai_task_logs(page=1, page_size=20)

    assert [item["job_id"] for item in payload["items"]] == ["job-b", "job-a"]
    assert [item["attempt_count"] for item in payload["items"]] == [0, 0]
    assert [item["validation_status"] for item in payload["items"]] == ["unknown", "unknown"]
    assert [item["usage_summary"]["complete"] for item in payload["items"]] == [False, False]
    db.close()


def test_ai_task_log_usage_uses_single_batch_for_known_pending_and_legacy(tmp_path):
    db = Database(tmp_path / "usage-logs.db")
    db.init_schema()
    try:
        for job_id, count, validation in (("known", 2, "passed"), ("legacy", 1, None),
                                           ("none", 0, "not_requested"), ("corrupt", 1, "failed")):
            db.conn.execute(
                "INSERT INTO ai_jobs (job_id,task_type,input_json,network_request_count,validation_status) "
                "VALUES (?,'keyword_clean','{}',?,?)", (job_id, count, validation),
            )
        record = {"request_ordinal": 1, "input_tokens": 10, "output_tokens": 2,
                  "total_tokens": 12, "cached_input_tokens": None,
                  "cache_creation_input_tokens": None, "reasoning_output_tokens": None,
                  "complete": True, "missing_reason": None}
        for job_id, usage in (("known", {"requests": [record]}),
                              ("corrupt", {"requests": [record, record]})):
            db.conn.execute(
                "INSERT INTO ai_job_model_attempts (job_id,attempt_index,agent_config_hash,"
                "provider_config_hash,candidate_list_hash,usage_json) VALUES (?,0,'a','b','c',?)",
                (job_id, json.dumps(usage)),
            )
        db.conn.commit()
        original = db.list_ai_job_model_attempts_for_jobs
        batches = []

        def batch(ids):
            batches.append(ids)
            return original(ids)

        def forbidden(_job_id):
            raise AssertionError("per-row query is forbidden")

        db.list_ai_job_model_attempts_for_jobs = batch
        db.list_ai_job_model_attempts = forbidden
        db.get_ai_job_usage_summary = forbidden
        rows = {row["job_id"]: row for row in db.get_ai_task_logs()["items"]}
        assert len(batches) == 1 and len(batches[0]) == 4
        assert rows["known"]["usage_summary"] == {
            "known_input_tokens": 10, "known_output_tokens": 2, "known_total_tokens": 12,
            "reported_requests": 1, "unreported_requests": 1, "complete": False,
        }
        assert rows["known"]["validation_status"] == "passed"
        for job_id in ("legacy", "corrupt"):
            assert rows[job_id]["usage_summary"]["known_total_tokens"] is None
            assert rows[job_id]["usage_summary"]["unreported_requests"] == 1
        assert rows["none"]["usage_summary"]["unreported_requests"] == 0
    finally:
        db.close()

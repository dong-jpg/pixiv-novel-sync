from __future__ import annotations

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
    payload = db.get_ai_task_logs(page=1, page_size=20)

    assert [item["job_id"] for item in payload["items"]] == ["job-b", "job-a"]
    assert [item["attempt_count"] for item in payload["items"]] == [0, 0]
    db.close()

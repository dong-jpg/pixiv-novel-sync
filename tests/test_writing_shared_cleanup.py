from __future__ import annotations

from pixiv_novel_sync.storage_db import Database


def test_owner_scoped_cleanup_keeps_shared_jobs_cleanable_and_other_owners_private(tmp_path):
    with Database(tmp_path / "cleanup.db") as db:
        db.init_schema()
        for job_id, task_type, scope in (
            ("shared", "keyword_clean", None),
            ("writing", "continue", None),
            ("owned-shared", "general", "owner-b"),
            ("adult-a", "adult_polish", "owner-a"),
            ("adult-b", "adult_polish", "owner-b"),
            ("orphan-adult", "adult_polish", None),
        ):
            db.conn.execute(
                "INSERT INTO ai_jobs (job_id,task_type,status,input_json,owner_scope,created_at) "
                "VALUES (?,?,'failed','{}',?,datetime('now','-10 days'))",
                (job_id, task_type, scope),
            )
        assert db.cleanup_ai_jobs(keep_days=3, owner_scope="owner-a") == 3
        remaining = {row[0] for row in db.conn.execute("SELECT job_id FROM ai_jobs")}
        assert remaining == {"owned-shared", "adult-b", "orphan-adult"}

from __future__ import annotations

import json

import pytest

from ai_adult_testkit import application_row, make_legacy_ai_database, seed_adult_project
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "adult.db")
    database.init_schema()
    try:
        yield database
    finally:
        database.close()


def test_adult_schema_defaults_are_fail_closed(db):
    project_id = db.create_ai_writing_project({"name": "p", "settings": {}})
    project = db.get_ai_writing_project(project_id)

    assert project["adult_content_enabled"] is False
    assert project["adult_characters_confirmed"] is False
    assert project["fictional_characters_confirmed"] is False
    assert project["adult_characters_json"] == []
    assert project["adult_confirmation_revision"] == 0
    assert db.get_adult_review_bindings() == {
        "safety": {"enabled": False},
        "fact_guard": {"enabled": False},
    }
    assert {row["policy_kind"] for row in db.list_adult_policy_state()} == {
        "safety",
        "fact_guard",
    }


def test_adult_application_does_not_fk_delete_with_job(db):
    seed_adult_project(db)
    application_id = db.save_candidate_application(application_row())

    db.delete_ai_job("adult-job")

    application = db.get_application_for_owner("adult-job", "owner-a")
    assert application is not None
    assert application["id"] == application_id


def test_application_metadata_never_serializes_candidate_or_chapter(db):
    seed_adult_project(db)
    row = application_row(candidate="不可写入 application 的候选正文")
    db.save_candidate_application(row)

    stored = db.conn.execute(
        "SELECT * FROM ai_polish_applications WHERE source_job_id = 'adult-job'"
    ).fetchone()
    serialized = json.dumps(dict(stored), ensure_ascii=False)
    assert "不可写入 application 的候选正文" not in serialized
    assert "前文" not in serialized


def test_old_database_migration_preserves_agent_and_chapter_ids(tmp_path):
    db = make_legacy_ai_database(tmp_path / "old.db")
    try:
        db.init_schema()
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.get_ai_agent(7)["id"] == 7
        assert db.get_ai_chapter(9)["id"] == 9
        assert db.get_ai_chapter(9)["chapter_revision"] == 0
    finally:
        db.close()


def test_adult_job_lookup_is_owner_filtered(db):
    seed_adult_project(db)

    assert db.get_adult_job("adult-job", "owner-a") is not None
    assert db.get_adult_job("adult-job", "owner-b") is None


def test_adult_job_rejects_text_bearing_input_metadata(db):
    seed_adult_project(db)

    with pytest.raises(ValueError, match="禁止保存"):
        db.create_adult_job(
            job_id="adult-job-with-text",
            agent_id=7,
            input_data={"project_id": 1, "target_text": "不得持久化的正文"},
            owner_scope="owner-a",
            owner_token="lease-a",
            idempotency_key_hash="5" * 64,
        )

    assert db.get_ai_job("adult-job-with-text") is None


def test_candidate_application_owner_cas_failure_rolls_back(db):
    seed_adult_project(db)
    row = application_row(owner_token="wrong-lease")

    with pytest.raises(ValueError, match="owner"):
        db.save_candidate_application(row)

    assert db.get_application_for_owner("adult-job", "owner-a") is None
    assert db.get_ai_job("adult-job")["status"] == "running"


def test_adult_migration_rolls_back_new_tables_on_policy_insert_failure(tmp_path):
    db = Database(tmp_path / "rollback.db")
    db.init_schema()
    try:
        for table in (
            "ai_chapter_derivative_invalidations",
            "ai_polish_applications",
            "ai_project_characters",
            "ai_adult_review_bindings",
            "ai_adult_policy_state",
        ):
            db.conn.execute(f"DROP TABLE IF EXISTS {table}")
        db.conn.executescript(
            """
            CREATE TABLE ai_adult_policy_state (
                policy_kind TEXT PRIMARY KEY,
                policy_id TEXT NOT NULL,
                policy_version INTEGER NOT NULL,
                policy_hash TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                schema_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TRIGGER reject_adult_policy_insert
            BEFORE INSERT ON ai_adult_policy_state
            BEGIN
                SELECT RAISE(ABORT, 'policy insert rejected');
            END;
            """
        )

        with pytest.raises(Exception, match="policy insert rejected"):
            db._migrate_adult_polish_tables()

        assert db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_project_characters'"
        ).fetchone() is None
    finally:
        db.close()


def test_adult_main_and_review_jobs_have_deadlines(db):
    from datetime import datetime, timezone
    seed_adult_project(db)
    db.create_adult_job(job_id="deadline-main", agent_id=7, input_data={}, owner_scope="owner-a", owner_token="lease", idempotency_key_hash="7" * 64)
    db.create_adult_review_job(job_id="deadline-review", parent_job_id="deadline-main", review_kind="safety", input_data={}, owner_scope="owner-a", owner_token="child", parent_owner_token="lease")
    for job_id in ("deadline-main", "deadline-review"):
        deadline = db.get_ai_job(job_id)["route_deadline_at"]
        assert deadline
        seconds = (datetime.fromisoformat(deadline).replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        assert 0 < seconds <= 1800


def test_policy_upgrade_is_atomic_versioned_and_marks_old_candidates(db):
    seed_adult_project(db)
    db.save_candidate_application(application_row())
    previous = db.list_adult_policy_state()
    upgraded = [{**row, "policy_version": row["policy_version"] + 1, "policy_hash": "f" * 64} for row in previous]
    db.upgrade_adult_policy_state(previous, upgraded)
    history = db.conn.execute("SELECT policy_kind,policy_version FROM ai_adult_policy_history").fetchall()
    assert len(history) == 4
    assert db.get_ai_job("adult-job")["output"]["code"] == "policy_upgrade_required"
    from pixiv_novel_sync.ai.adult_types import AdultConflictError
    with pytest.raises(AdultConflictError):
        db.upgrade_adult_policy_state(previous, upgraded)
    assert len(db.conn.execute("SELECT * FROM ai_adult_policy_history").fetchall()) == 4


def test_policy_upgrade_rejects_same_version_rewrite(db):
    from pixiv_novel_sync.ai.adult_types import AdultConflictError
    previous = db.list_adult_policy_state()
    tampered = [{**row, "policy_hash": "f" * 64} for row in previous]
    with pytest.raises(AdultConflictError):
        db.upgrade_adult_policy_state(previous, tampered)
    assert db.list_adult_policy_state() == previous


def test_cancel_stored_revalidation_is_owner_scoped_without_changing_candidate(db):
    seed_adult_project(db)
    db.save_candidate_application(application_row())
    db.create_adult_review_job(job_id="revalidate-child", parent_job_id="adult-job", review_kind="safety", input_data={}, owner_scope="owner-a", owner_token="child", parent_owner_token="lease-a", allow_succeeded_parent=True)
    assert db.request_adult_review_cancel("adult-job", "owner-b") is False
    assert db.request_adult_review_cancel("adult-job", "owner-a") is True
    assert db.ai_job_should_stop("revalidate-child") is True
    assert db.get_adult_job("adult-job", "owner-a")["status"] == "succeeded"


def test_policy_upgrade_rolls_back_history_current_and_candidate_on_failure(db):
    seed_adult_project(db)
    db.save_candidate_application(application_row())
    previous = db.list_adult_policy_state()
    released = [{**row, "policy_version": row["policy_version"] + 1, "policy_hash": "f" * 64} for row in previous]
    db.conn.execute("CREATE TRIGGER reject_second_policy BEFORE INSERT ON ai_adult_policy_history WHEN NEW.policy_kind='safety' AND NEW.policy_version=2 BEGIN SELECT RAISE(ABORT,'injected history failure'); END")
    with pytest.raises(Exception, match="injected history failure"):
        db.upgrade_adult_policy_state(previous, released)
    assert db.list_adult_policy_state() == previous
    assert db.conn.execute("SELECT COUNT(*) FROM ai_adult_policy_history").fetchone()[0] == 0
    assert db.get_ai_job("adult-job")["output"]["code"] != "policy_upgrade_required"

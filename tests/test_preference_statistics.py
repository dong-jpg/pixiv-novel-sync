import pytest

from pixiv_novel_sync.models import UserRecord
from pixiv_novel_sync.preference_statistics import CurrentArchiveRebuild
from pixiv_novel_sync.preferences import PreferenceAnalyzer
from pixiv_novel_sync.storage.preference_statistics import statistics_state
from pixiv_novel_sync.storage_db import Database
from test_preference_incremental import _add_novel


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "stats.db") as value:
        value.init_schema()
        value.upsert_user(UserRecord(1, "Author", None, "{}"))
        _add_novel(value, 1, 1, '["old"]', "old story")
        _add_novel(value, 2, 1, '["keep"]', "keep story")
        PreferenceAnalyzer(value).analyze_incremental(max_batches=10)
        yield value


def test_rebuild_counts_current_archive_and_keeps_explicit_feedback(db):
    db.set_reading_feedback("novel", 2, "liked", 0)
    with db.transaction() as conn:
        conn.execute("DELETE FROM novels WHERE novel_id=1")
        conn.execute("UPDATE novels SET tags_json='[\"changed\"]' WHERE novel_id=2")
    before = db.get_preference_accumulator()
    operation = CurrentArchiveRebuild(db)
    try:
        result, rebuilt = operation.prepare()
        assert db.get_preference_accumulator() == before
        assert result["done"] and rebuilt["stats"]["novel_count"] == 1
        assert rebuilt["stats"]["top_tags"] == [{"name": "changed", "count": 1}]
        with db.transaction() as conn:
            operation.publish(conn)
        assert db.count_analyzed_preference_rows() == 1
        assert statistics_state(db.conn)["current_archive"]
        assert db.get_reading_feedback("novel", 2)["reaction"] == "liked"
    finally:
        operation.close()


@pytest.mark.parametrize("change", ["body", "delete", "tags", "author", "accumulator"])
def test_rebuild_rejects_concurrent_changes_and_preserves_statistics(db, change):
    operation = CurrentArchiveRebuild(db)
    try:
        operation.prepare()
        before = db.get_preference_accumulator()
        with db.transaction() as conn:
            if change == "body":
                conn.execute("UPDATE novel_texts SET text_raw='changed' WHERE novel_id=1")
            elif change == "delete":
                conn.execute("DELETE FROM novels WHERE novel_id=1")
            elif change == "tags":
                conn.execute("UPDATE novels SET tags_json='[]' WHERE novel_id=1")
            elif change == "author":
                conn.execute("UPDATE users SET name='changed'")
            else:
                conn.execute("UPDATE preference_statistics_state SET generation=generation+1")
        with pytest.raises(ValueError), db.transaction() as conn:
            operation.publish(conn)
        assert db.get_preference_accumulator() == before
    finally:
        operation.close()


def test_cancelled_rebuild_never_resets_previous_accumulator(db):
    before = db.get_preference_accumulator()
    operation = CurrentArchiveRebuild(db, stop_requested=lambda: True)
    try:
        with pytest.raises(InterruptedError):
            operation.prepare()
        assert db.get_preference_accumulator() == before
    finally:
        operation.close()


def test_rebuild_and_profile_publish_roll_back_together(db):
    before = db.get_preference_accumulator()
    generation = statistics_state(db.conn)["generation"]
    operation = CurrentArchiveRebuild(db, expected_generation=generation)
    try:
        operation.prepare()
        with pytest.raises(RuntimeError), db.transaction() as conn:
            operation.publish(conn)
            raise RuntimeError("profile write failed")
        assert db.get_preference_accumulator() == before
        assert statistics_state(db.conn)["generation"] == generation
    finally:
        operation.close()


def test_archive_deletion_keeps_feedback_but_allows_explicit_withdrawal(db):
    db.set_reading_feedback("novel", 1, "liked", 0, "retain my reading history")
    db.delete_novel(1)
    saved = db.get_reading_feedback("novel", 1)
    assert saved["reaction"] == "liked"
    assert db.get_preference_learning_signals()["weights"] == {}
    db.set_reading_feedback("novel", 1, "clear", saved["version"])
    assert db.get_reading_feedback("novel", 1)["note"] == ""

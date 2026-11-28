from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.storage.ai import writing
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "writing.db") as database:
        database.init_schema()
        yield database


def test_chapter_outline_batch_rolls_back_when_a_later_update_fails(db):
    project = db.create_ai_writing_project({"name": "atomic outlines"})
    first, second = [db.create_ai_chapter({"project_id": project, "chapter_number": number,
                                         "outline": "original"}) for number in (1, 2)]
    db.conn.execute(f"""CREATE TRIGGER reject_second_outline BEFORE UPDATE ON ai_chapters
        WHEN NEW.id={second} BEGIN SELECT RAISE(ABORT, 'outline rejected'); END""")

    with pytest.raises(sqlite3.IntegrityError, match="outline rejected"):
        db.update_ai_chapters_outlines_and_metadata([
            {"id": first, "outline": "new first", "metadata": {"source": "new"}},
            {"id": second, "outline": "new second"},
        ])

    assert db.get_ai_chapter(first)["outline"] == "original"
    assert db.get_ai_chapter(first)["chapter_revision"] == 0
    assert db.get_ai_chapter(first)["metadata"] == {}
    assert db.conn.in_transaction is False


def test_automatic_chapter_numbers_are_allocated_under_a_database_write_transaction(db, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from pixiv_novel_sync.ai.services.projects import AIProjectsMixin

    project = db.create_ai_writing_project({"name": "concurrent chapters"})
    barrier = Barrier(2)
    original = Database.get_next_chapter_number

    def next_number(database, project_id):
        assert database.conn.in_transaction
        return original(database, project_id)

    monkeypatch.setattr(Database, "get_next_chapter_number", next_number)

    def create(title):
        payload = {"project_id": project, "title": title}
        barrier.wait(timeout=5)
        identity = AIProjectsMixin.create_chapter(SimpleNamespace(_db=lambda: Database(db.path)), payload)
        assert "chapter_number" not in payload
        return identity

    with ThreadPoolExecutor(max_workers=2) as executor:
        identities = list(executor.map(create, ("one", "two")))
    assert len(set(identities)) == 2
    assert [row["chapter_number"] for row in db.list_ai_chapter_refs(project)] == [1, 2]


@pytest.mark.parametrize("operation", ["append", "delete"])
def test_chat_multi_statement_failure_keeps_original_messages(db, operation):
    session = db.create_ai_chat_session({"title": "atomic chat"})
    db.append_ai_chat_message(session, "user", "original")
    trigger_event = "UPDATE" if operation == "append" else "DELETE"
    db.conn.execute(f"""CREATE TRIGGER reject_chat_change BEFORE {trigger_event} ON ai_chat_sessions
        BEGIN SELECT RAISE(ABORT, 'session rejected'); END""")

    with pytest.raises(sqlite3.IntegrityError, match="session rejected"):
        if operation == "append":
            db.append_ai_chat_message(session, "assistant", "should roll back")
        else:
            db.delete_ai_chat_session(session)

    assert [row["content"] for row in db.list_ai_chat_messages(session)] == ["original"]
    assert db.get_ai_chat_session(session) is not None
    assert db.conn.in_transaction is False


@pytest.mark.parametrize("kind", ["chapter", "chat"])
def test_metadata_patches_from_separate_connections_do_not_lose_fields(db, monkeypatch, kind):
    if kind == "chapter":
        project = db.create_ai_writing_project({"name": "metadata merge"})
        item = db.create_ai_chapter({"project_id": project, "chapter_number": 1})
        method, getter = "patch_ai_chapter_metadata", "get_ai_chapter"
    else:
        item = db.create_ai_chat_session({"title": "metadata merge"})
        method, getter = "patch_ai_chat_session_metadata", "get_ai_chat_session"

    with Database(db.path) as other:
        other.conn.execute("PRAGMA busy_timeout=0")
        entered = False
        blocked = False

        def interleave(raw):
            nonlocal entered, blocked
            value = json.loads(raw)
            if not entered:
                entered = True
                try:
                    getattr(other, method)(item, {"other": 2})
                except sqlite3.OperationalError as exc:
                    assert "locked" in str(exc)
                    blocked = True
            return value

        # Inject another connection immediately after the first snapshot is read.
        monkeypatch.setattr(writing, "json", SimpleNamespace(loads=interleave, dumps=json.dumps))
        getattr(db, method)(item, {"first": 1})
        if blocked:
            getattr(other, method)(item, {"other": 2})
        assert getattr(db, getter)(item)["metadata"] == {"first": 1, "other": 2}


def test_writing_schema_repeated_migration_preserves_shared_and_writing_data(db):
    project = db.create_ai_writing_project({"name": "retained project"})
    chapter = db.create_ai_chapter({"project_id": project, "chapter_number": 1, "content": "retained"})
    draft = db.create_ai_draft({"title": "retained draft", "content": "draft"})
    db.init_schema()
    db.init_schema()

    columns = {row["name"] for row in db.conn.execute("PRAGMA table_info(ai_jobs)")}
    assert {"owner_scope", "idempotency_key_hash", "candidate_snapshot_hash", "candidate_snapshot_json",
            "validation_status", "network_request_count", "route_deadline_at"} <= columns
    tables = {row["name"] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"reading_progress_mutations", "web_auth_sessions", "ai_writing_projects",
            "ai_chapters", "ai_adult_policy_history", "ai_polish_applications"} <= tables
    assert db.get_ai_chapter(chapter)["content"] == "retained"
    assert db.get_ai_draft(draft)["content"] == "draft"
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []

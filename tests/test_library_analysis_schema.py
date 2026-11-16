import hashlib

import pytest

from pixiv_novel_sync.models import NovelTextRecord
from test_library_analysis import add_novel, library as library_fixture


@pytest.fixture
def library(tmp_path, monkeypatch):
    yield from library_fixture.__wrapped__(tmp_path, monkeypatch)


def _restore_legacy_texts(db):
    # A restore from the pre-has_content/pre-FK format. SQLite also rewrites
    # cross-table trigger references to the temporary name during this rename.
    db.conn.executescript("""
        ALTER TABLE novel_texts RENAME TO novel_texts_current;
        CREATE TABLE novel_texts (
            novel_id INTEGER PRIMARY KEY, text_raw TEXT NOT NULL, text_markdown TEXT,
            text_hash TEXT NOT NULL, fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO novel_texts(novel_id,text_raw,text_markdown,text_hash,fetched_at)
        SELECT novel_id,text_raw,text_markdown,text_hash,fetched_at FROM novel_texts_current;
        DROP TABLE novel_texts_current;
    """)


def _source_schema(db):
    return [tuple(row) for row in db.conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name='novel_texts' "
        "OR name LIKE 'library_analysis_%' ORDER BY type,name"
    )]


def _assert_current_triggers(db):
    rows = db.conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN "
        "('library_analysis_text_insert','library_analysis_text_update','library_analysis_membership') ORDER BY name"
    ).fetchall()
    assert len(rows) == 3
    assert all("novel_texts_current" not in row["sql"] and "novel_texts_old" not in row["sql"] for row in rows)


def test_legacy_text_rebuild_restores_library_tracking_once_without_new_work(library):
    db = library.db
    add_novel(db, 102, body="   ")
    texts = [tuple(row) for row in db.conn.execute(
        "SELECT novel_id,text_raw,text_hash,fetched_at FROM novel_texts ORDER BY novel_id")]
    revision = db.conn.execute("SELECT revision FROM library_analysis_clock").fetchone()[0]
    _restore_legacy_texts(db)
    broken = db.conn.execute("SELECT sql FROM sqlite_master WHERE name='library_analysis_membership'").fetchone()[0]
    assert "novel_texts_current" in broken

    db.init_schema()
    db.init_schema()

    assert [tuple(row) for row in db.conn.execute(
        "SELECT novel_id,text_raw,text_hash,fetched_at FROM novel_texts ORDER BY novel_id")] == texts
    assert [tuple(row) for row in db.conn.execute("SELECT novel_id,has_content FROM novel_texts ORDER BY novel_id")] == [(101, 1), (102, 0)]
    assert any(row[2] == "novels" and row[3] == "novel_id" for row in db.conn.execute("PRAGMA foreign_key_list(novel_texts)"))
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    _assert_current_triggers(db)
    assert db.conn.execute("SELECT revision FROM library_analysis_clock").fetchone()[0] == revision
    assert db.conn.execute("SELECT COUNT(*) FROM work_analysis_runs").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM library_analysis_targets").fetchone()[0] == 0

    with db.transaction() as conn:
        conn.execute("INSERT INTO series(series_id,title,user_id,total_novels) VALUES(99,'Synthetic new series',1,1)")
        conn.execute("UPDATE novels SET series_id=99 WHERE novel_id=101")
    assert db.conn.execute("SELECT revision FROM library_analysis_clock").fetchone()[0] == revision + 1
    body = "Changed synthetic text after a successful legacy restore"
    db.upsert_novel_text(NovelTextRecord(101, body, None, hashlib.sha256(body.encode()).hexdigest()))
    assert db.conn.execute("SELECT revision FROM library_analysis_clock").fetchone()[0] == revision + 2
    assert db.conn.execute("SELECT revision FROM library_analysis_sources WHERE source_kind='series' AND source_id=99").fetchone()[0] == revision + 2


def test_library_trigger_repair_and_text_rebuild_roll_back_with_the_outer_transaction(library):
    db = library.db
    _restore_legacy_texts(db)
    db._migrate_novel_texts_table()
    schema_before = _source_schema(db)
    texts_before = [tuple(row) for row in db.conn.execute("SELECT * FROM novel_texts ORDER BY novel_id")]
    revision = db.conn.execute("SELECT revision FROM library_analysis_clock").fetchone()[0]

    with pytest.raises(RuntimeError, match="synthetic outer rollback"):
        with db.transaction():
            db._rebuild_novel_texts_with_foreign_key()
            _assert_current_triggers(db)
            assert db._has_foreign_key("novel_texts", "novel_id", "novels")
            raise RuntimeError("synthetic outer rollback")

    assert _source_schema(db) == schema_before
    assert [tuple(row) for row in db.conn.execute("SELECT * FROM novel_texts ORDER BY novel_id")] == texts_before
    assert db.conn.execute("SELECT revision FROM library_analysis_clock").fetchone()[0] == revision
    assert db.conn.execute("SELECT name FROM sqlite_master WHERE name='novel_texts_old'").fetchone() is None
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    db.init_schema()
    _assert_current_triggers(db)
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []

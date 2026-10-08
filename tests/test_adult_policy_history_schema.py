from __future__ import annotations

from pixiv_novel_sync.storage_db import Database


def test_adult_policy_history_schema_is_additive_and_versioned(tmp_path):
    db = Database(tmp_path / 'history.db')
    try:
        db.init_schema()
        columns = {row['name']: row['pk'] for row in db.conn.execute('PRAGMA table_info(ai_adult_policy_history)')}
        assert columns['policy_kind'] == 1
        assert columns['policy_version'] == 2
        assert {'policy_id', 'policy_hash', 'prompt_hash', 'schema_hash', 'updated_at'} <= columns.keys()
        db.conn.execute("INSERT INTO ai_adult_policy_history(policy_kind,policy_id,policy_version,policy_hash,prompt_hash,schema_hash) VALUES ('safety','old',1,'a','b','c')")
        db.init_schema()
        assert db.conn.execute('SELECT count(*) FROM ai_adult_policy_history').fetchone()[0] == 1
    finally:
        db.close()

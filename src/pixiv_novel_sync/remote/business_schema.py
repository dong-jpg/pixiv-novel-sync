"""Add business kinds without rebuilding or replacing the library database."""


def migrate_business(conn):
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'").fetchone()[0]
    if "checkin.submit.v1" not in sql:
        import re

        create = re.sub(r'CREATE TABLE (?:"runs"|runs)', "CREATE TABLE runs_v2", sql, count=1)
        create = re.sub(
            r"kind IN \([^)]*\)",
            "kind IN ('diagnostic.v1','epub.single.v1','pixiv.novel.v1','pixiv.api.v1','ai.text.v1','checkin.status.v1','checkin.submit.v1')",
            create,
        )
        conn.execute(create)
        conn.execute("INSERT INTO runs_v2 SELECT * FROM runs")
        conn.execute("DROP TABLE runs")
        conn.execute("ALTER TABLE runs_v2 RENAME TO runs")
        conn.execute("CREATE UNIQUE INDEX run_event ON runs(scope,idempotency_key)")
        conn.execute("CREATE INDEX eligible_runs ON runs(state,not_before,created_at)")
    if "task_kinds_json" not in {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}:
        conn.execute(
            'ALTER TABLE sessions ADD COLUMN task_kinds_json TEXT NOT NULL DEFAULT \'["diagnostic.v1","epub.single.v1"]\''
        )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS account_leases(
        scope TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL,
        next_request_at REAL NOT NULL DEFAULT 0, request_interval REAL NOT NULL DEFAULT 1)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS business_secrets(
        input_sha256 TEXT PRIMARY KEY, encrypted BLOB NOT NULL, expires_at REAL NOT NULL)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS business_request_permits(
        attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id), ordinal INTEGER NOT NULL,
        granted_at REAL NOT NULL, PRIMARY KEY(attempt_id,ordinal))"""
    )
    from .checkin_task import migrate_checkin

    migrate_checkin(conn)
    from .upstream import migrate_upstream

    migrate_upstream(conn)

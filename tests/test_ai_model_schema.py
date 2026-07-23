from __future__ import annotations

import importlib
import json
import logging
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest

from pixiv_novel_sync.storage_db import Database


ROUTING_TABLES = {
    "ai_provider_models",
    "ai_model_pools",
    "ai_model_pool_members",
    "ai_model_sync_operations",
    "ai_job_model_attempts",
}

POOL_DOWNGRADE_ERROR = "存在模型池 Agent，请先转换为固定绑定"


def _model_schema() -> ModuleType:
    try:
        return importlib.import_module("pixiv_novel_sync.storage.ai.model_schema")
    except ModuleNotFoundError as exc:
        pytest.fail(f"model routing schema helper is missing: {exc}")


def make_old_ai_database(path: Path) -> Database:
    db = Database(path)
    db.conn.executescript(
        """
        CREATE TABLE ai_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            provider_type TEXT NOT NULL,
            base_url TEXT,
            api_key_encrypted TEXT,
            default_model TEXT,
            available_models_json TEXT,
            timeout_seconds INTEGER NOT NULL DEFAULT 120,
            max_retries INTEGER NOT NULL DEFAULT 2,
            proxy TEXT,
            context_window INTEGER NOT NULL DEFAULT 128000,
            stream_enabled INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE ai_agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            task_type TEXT NOT NULL,
            provider_id INTEGER NOT NULL REFERENCES ai_providers(id) ON DELETE RESTRICT,
            model TEXT,
            system_prompt TEXT NOT NULL,
            temperature REAL NOT NULL DEFAULT 0.8,
            top_p REAL NOT NULL DEFAULT 0.9,
            max_tokens INTEGER NOT NULL DEFAULT 4000,
            context_window INTEGER NOT NULL DEFAULT 16000,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE ai_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL UNIQUE,
            task_type TEXT NOT NULL,
            agent_id INTEGER,
            status TEXT NOT NULL DEFAULT 'running',
            input_json TEXT NOT NULL,
            output_text TEXT,
            output_json TEXT,
            error_message TEXT,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    db.conn.execute(
        """
        INSERT INTO ai_providers (
            id, name, provider_type, base_url, api_key_encrypted, default_model,
            available_models_json, timeout_seconds, max_retries, proxy,
            context_window, stream_enabled, enabled, created_at, updated_at
        ) VALUES (3, 'legacy', 'openai', 'https://legacy.invalid', 'ciphertext',
                  'legacy-model', '["legacy-model"]', 37, 4, 'http://proxy.invalid',
                  64000, 0, 1, '2024-01-02 03:04:05', '2024-02-03 04:05:06')
        """
    )
    db.conn.execute(
        """
        INSERT INTO ai_agents (
            id, name, task_type, provider_id, model, system_prompt, temperature,
            top_p, max_tokens, context_window, enabled, created_at, updated_at
        ) VALUES (7, 'legacy-agent', 'general', 3, 'legacy-model', 'legacy prompt',
                  0.25, 0.75, 2048, 32000, 1,
                  '2024-03-04 05:06:07', '2024-04-05 06:07:08')
        """
    )
    db.conn.execute(
        """
        INSERT INTO ai_jobs (
            id, job_id, task_type, agent_id, status, input_json, output_text,
            output_json, error_message, started_at, finished_at, created_at
        ) VALUES (11, 'legacy-job', 'general', 7, 'succeeded', '{"input": 1}',
                  'done', '{"output": 1}', NULL, '2024-05-06 07:08:09',
                  '2024-05-06 07:08:10', '2024-05-06 07:08:08')
        """
    )
    db.conn.commit()
    return db


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "routing.db")
    database.init_schema()
    database.conn.execute(
        """
        INSERT INTO ai_providers (id, name, provider_type, available_models_json)
        VALUES (3, 'provider', 'openai', '[]')
        """
    )
    database.conn.commit()
    try:
        yield database
    finally:
        database.close()


def test_old_ai_database_migrates_fixed_agents_and_imports_available_models(
    tmp_path: Path,
):
    database = make_old_ai_database(tmp_path / "old.db")
    try:
        database.init_schema()

        agent = database.get_ai_agent(7)
        assert agent is not None
        assert agent["id"] == 7
        assert agent["binding_type"] == "fixed"
        assert agent["provider_id"] == 3
        assert agent["model"] == "legacy-model"
        assert agent["name"] == "legacy-agent"
        assert agent["task_type"] == "general"
        assert agent["system_prompt"] == "legacy prompt"
        assert agent["temperature"] == 0.25
        assert agent["top_p"] == 0.75
        assert agent["max_tokens"] == 2048
        assert agent["context_window"] == 32000
        assert agent["enabled"] == 1
        assert agent["created_at"] == "2024-03-04 05:06:07"
        assert agent["updated_at"] == "2024-04-05 06:07:08"

        model = database.conn.execute(
            """
            SELECT model_key, manual, discovered, discovered_available
            FROM ai_provider_models
            WHERE provider_id = 3
            """
        ).fetchone()
        assert tuple(model) == ("legacy-model", 1, 0, 0)

        job = database.conn.execute(
            "SELECT * FROM ai_jobs WHERE id = 11"
        ).fetchone()
        assert job["job_id"] == "legacy-job"
        assert job["status"] == "succeeded"
        assert job["output_text"] == "done"
        assert job["candidate_snapshot_json"] is None
        assert job["next_attempt_index"] == 0
        assert job["stage"] == "main"
        assert job["created_at"] == "2024-05-06 07:08:08"
        assert database.conn.execute("PRAGMA foreign_key_check").fetchall() == []

        database.init_schema()
        assert database.conn.execute(
            "SELECT COUNT(*) FROM ai_provider_models WHERE provider_id = 3"
        ).fetchone()[0] == 1
        assert database.get_ai_agent(7)["created_at"] == "2024-03-04 05:06:07"
    finally:
        database.close()


def test_old_model_list_imports_strings_and_object_ids_and_warns_per_skip(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    database = make_old_ai_database(tmp_path / "models.db")
    invalid_long_key = "x" * 301
    available_models = [
        "string-model",
        {"id": "object-model"},
        "",
        invalid_long_key,
        {"id": ""},
        {"id": 12},
        23,
        None,
    ]
    database.conn.execute(
        """
        INSERT INTO ai_providers (
            id, name, provider_type, available_models_json, created_at, updated_at
        ) VALUES (4, 'mixed', 'openai', ?, '2024-06-01', '2024-06-02')
        """,
        (json.dumps(available_models),),
    )
    database.conn.commit()

    try:
        with caplog.at_level(logging.WARNING):
            database.init_schema()

        rows = database.conn.execute(
            """
            SELECT model_key, manual, discovered, discovered_available
            FROM ai_provider_models
            WHERE provider_id = 4
            ORDER BY model_key
            """
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("object-model", 1, 0, 0),
            ("string-model", 1, 0, 0),
        ]
        skipped = [
            record for record in caplog.records
            if record.name == "pixiv_novel_sync.storage.ai.model_schema"
            and "available_models_json" in record.getMessage()
        ]
        assert len(skipped) == 6
    finally:
        database.close()


def test_old_model_list_skips_each_control_character_without_rewriting_valid_keys(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    database = make_old_ai_database(tmp_path / "control-models.db")
    available_models = [
        "nul\0model",
        "newline\nmodel",
        "c0-\u001f-model",
        "c1-\u0085-model",
        "del-\u007f-model",
        " Mixed Model ",
        "Cafe\u0301",
        "MODEL",
        "model",
    ]
    database.conn.execute(
        """
        INSERT INTO ai_providers (
            id, name, provider_type, available_models_json, created_at, updated_at
        ) VALUES (5, 'controls', 'openai', ?, '2024-07-01', '2024-07-02')
        """,
        (json.dumps(available_models),),
    )
    database.conn.commit()

    try:
        with caplog.at_level(logging.WARNING):
            database.init_schema()

        model_keys = {
            row[0]
            for row in database.conn.execute(
                """
                SELECT model_key
                FROM ai_provider_models
                WHERE provider_id = 5
                """
            ).fetchall()
        }
        assert model_keys == {" Mixed Model ", "Cafe\u0301", "MODEL", "model"}

        skipped_messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "pixiv_novel_sync.storage.ai.model_schema"
            and "provider_id=5" in record.getMessage()
        ]
        assert len(skipped_messages) == 5
        for index in range(5):
            assert any(f"index={index}" in message for message in skipped_messages)
    finally:
        database.close()


def test_routing_schema_has_strict_pool_and_attempt_constraints(db: Database):
    tables = {
        row[0]
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert ROUTING_TABLES <= tables

    agent_columns = {
        row[1] for row in db.conn.execute("PRAGMA table_info(ai_agents)")
    }
    assert {
        "binding_type",
        "model_pool_id",
        "required_capabilities_json",
        "binding_version",
    } <= agent_columns

    job_columns = {row[1] for row in db.conn.execute("PRAGMA table_info(ai_jobs)")}
    assert {
        "candidate_snapshot_json",
        "candidate_snapshot_hash",
        "next_attempt_index",
        "owner_token",
        "lease_until",
        "heartbeat_at",
        "stage",
        "pinned_candidate_index",
        "network_request_count",
        "candidate_attempt_count",
        "route_deadline_at",
        "prompt_budget_json",
        "parent_job_id",
        "idempotency_key",
    } <= job_columns

    attempt_columns = {
        row[1]
        for row in db.conn.execute("PRAGMA table_info(ai_job_model_attempts)")
    }
    assert {
        "pool_id",
        "provider_id",
        "provider_model_id",
        "pool_version_snapshot",
        "pool_position_snapshot",
        "model_key",
        "pool_name_snapshot",
        "provider_name_snapshot",
    } <= attempt_columns

    provider_columns = {
        row[1] for row in db.conn.execute("PRAGMA table_info(ai_providers)")
    }
    assert {
        "models_synced_at",
        "models_sync_attempted_at",
        "models_sync_error",
        "models_sync_generation",
        "models_sync_owner",
        "models_sync_lease_until",
    } <= provider_columns

    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO ai_model_pools(name, pool_kind, version) "
            "VALUES ('', 'custom', 1)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            """
            INSERT INTO ai_agents (
                name, task_type, binding_type, provider_id, model_pool_id,
                system_prompt
            ) VALUES ('bad', 'general', 'pool', 3, NULL, 's')
            """
        )


def test_attempt_numeric_snapshots_have_no_live_configuration_foreign_keys(
    db: Database,
):
    db.conn.execute(
        """
        INSERT INTO ai_jobs (job_id, task_type, status, input_json)
        VALUES ('attempt-job', 'general', 'running', '{}')
        """
    )
    db.conn.execute(
        """
        INSERT INTO ai_job_model_attempts (
            job_id, attempt_index, pool_id, provider_id, provider_model_id,
            pool_version_snapshot, pool_position_snapshot, model_key,
            pool_name_snapshot, provider_name_snapshot, agent_config_hash,
            provider_config_hash, candidate_list_hash, stage, status
        ) VALUES (
            'attempt-job', 0, 9001, 9002, 9003, 4, 5, 'snapshot-model',
            'old-pool', 'old-provider', 'agent-hash', 'provider-hash',
            'candidate-hash', 'main', 'running'
        )
        """
    )
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []

    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            """
            INSERT INTO ai_job_model_attempts (
                job_id, attempt_index, agent_config_hash,
                provider_config_hash, candidate_list_hash, stage, status
            ) VALUES (
                'attempt-job', -1, 'agent-hash', 'provider-hash',
                'candidate-hash', 'main', 'running'
            )
            """
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            """
            INSERT INTO ai_job_model_attempts (
                job_id, attempt_index, agent_config_hash,
                provider_config_hash, candidate_list_hash, stage, status
            ) VALUES (
                'attempt-job', 0, 'agent-hash', 'provider-hash',
                'candidate-hash', 'validation', 'succeeded'
            )
            """
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            """
            INSERT INTO ai_provider_models (
                provider_id, model_key, discovered_capabilities_json
            ) VALUES (3, 'model', 'not-json')
            """
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            """
            INSERT INTO ai_jobs (job_id, task_type, status, input_json)
            VALUES ('bad-status', 'general', 'queued', '{}')
            """
        )


def test_job_idempotency_index_is_partial_and_unique(db: Database):
    db.conn.execute(
        """
        INSERT INTO ai_jobs (
            job_id, task_type, status, input_json, parent_job_id, idempotency_key
        ) VALUES ('child-1', 'general', 'running', '{}', 'parent', 'same-key')
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            """
            INSERT INTO ai_jobs (
                job_id, task_type, status, input_json, parent_job_id,
                idempotency_key
            ) VALUES ('child-2', 'general', 'running', '{}', 'parent', 'same-key')
            """
        )
    db.conn.execute(
        """
        INSERT INTO ai_jobs (
            job_id, task_type, status, input_json, parent_job_id, idempotency_key
        ) VALUES ('child-3', 'general', 'running', '{}', NULL, 'same-key')
        """
    )
    db.conn.execute(
        """
        INSERT INTO ai_jobs (
            job_id, task_type, status, input_json, parent_job_id, idempotency_key
        ) VALUES ('child-4', 'general', 'running', '{}', NULL, 'same-key')
        """
    )


def test_model_sync_operation_indexes_cover_routing_lookups(db: Database):
    indexed_columns = set()
    for index_row in db.conn.execute("PRAGMA index_list(ai_model_sync_operations)"):
        columns = tuple(
            row[2]
            for row in db.conn.execute(f"PRAGMA index_info({index_row[1]})")
        )
        indexed_columns.add(columns)

    assert ("provider_id",) in indexed_columns
    assert ("status",) in indexed_columns
    assert ("lease_until",) in indexed_columns
    assert ("generation",) in indexed_columns


def test_failed_model_schema_migration_rolls_back(tmp_path: Path):
    database = make_old_ai_database(tmp_path / "rollback.db")
    database.conn.execute("PRAGMA foreign_keys=OFF")
    database.conn.execute(
        """
        INSERT INTO ai_agents (
            id, name, task_type, provider_id, model, system_prompt
        ) VALUES (99, 'orphan', 'general', 999, 'orphan-model', 's')
        """
    )
    database.conn.commit()

    migrate_model_routing_schema = _model_schema().migrate_model_routing_schema
    try:
        with pytest.raises(RuntimeError, match="foreign_key_check"):
            migrate_model_routing_schema(database.conn)

        table_names = {
            row[0]
            for row in database.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert ROUTING_TABLES.isdisjoint(table_names)
        assert "binding_type" not in {
            row[1] for row in database.conn.execute("PRAGMA table_info(ai_agents)")
        }
        assert "candidate_snapshot_json" not in {
            row[1] for row in database.conn.execute("PRAGMA table_info(ai_jobs)")
        }
        assert database.conn.execute(
            "SELECT provider_id FROM ai_agents WHERE id = 99"
        ).fetchone()[0] == 999
        assert database.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        database.close()


def test_prepare_downgrade_is_read_only_for_fixed_agents_and_rejects_pool_agents(
    tmp_path: Path,
):
    database = make_old_ai_database(tmp_path / "downgrade.db")
    try:
        database.init_schema()
        prepare_model_routing_downgrade = (
            _model_schema().prepare_model_routing_downgrade
        )

        changes_before = database.conn.total_changes
        report = prepare_model_routing_downgrade(database.conn)
        assert report == {
            "compatible": True,
            "count": 1,
            "agents": [
                {
                    "id": 7,
                    "binding_type": "fixed",
                    "provider_id": 3,
                    "model": "legacy-model",
                }
            ],
        }
        assert database.conn.total_changes == changes_before

        pool_id = database.conn.execute(
            """
            INSERT INTO ai_model_pools (name, pool_kind, enabled)
            VALUES ('primary-pool', 'primary', 1)
            """
        ).lastrowid
        database.conn.execute(
            """
            UPDATE ai_agents
            SET binding_type = 'pool', provider_id = NULL, model = NULL,
                model_pool_id = ?
            WHERE id = 7
            """,
            (pool_id,),
        )

        with pytest.raises(RuntimeError) as exc_info:
            prepare_model_routing_downgrade(database.conn)
        assert str(exc_info.value) == POOL_DOWNGRADE_ERROR
    finally:
        database.close()

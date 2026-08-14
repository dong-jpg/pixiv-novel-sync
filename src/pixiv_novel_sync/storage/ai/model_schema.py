"""Atomic schema migration for model routing and model pools."""

from __future__ import annotations

import json
import logging
import sqlite3
import unicodedata
from typing import Any


logger = logging.getLogger(__name__)


_AGENT_ROUTING_COLUMNS = {
    "binding_type",
    "model_pool_id",
    "required_capabilities_json",
    "binding_version",
}

_JOB_ROUTING_COLUMNS = {
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
}

_POOL_DOWNGRADE_ERROR = "存在模型池 Agent，请先转换为固定绑定"


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _create_model_routing_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_provider_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL
                REFERENCES ai_providers(id) ON DELETE CASCADE,
            model_key TEXT NOT NULL CHECK(
                length(model_key) > 0
                AND length(model_key) <= 300
                AND length(CAST(model_key AS BLOB)) <= 1200
            ),
            discovered INTEGER NOT NULL DEFAULT 0 CHECK(discovered IN (0, 1)),
            manual INTEGER NOT NULL DEFAULT 0 CHECK(manual IN (0, 1)),
            discovered_available INTEGER NOT NULL DEFAULT 0
                CHECK(discovered_available IN (0, 1)),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
            discovered_display_name TEXT,
            manual_display_name TEXT,
            discovered_capabilities_json TEXT NOT NULL DEFAULT '[]'
                CHECK(json_valid(discovered_capabilities_json)),
            manual_capabilities_json TEXT
                CHECK(
                    manual_capabilities_json IS NULL
                    OR json_valid(manual_capabilities_json)
                ),
            discovered_context_window INTEGER
                CHECK(
                    discovered_context_window IS NULL
                    OR discovered_context_window BETWEEN 256 AND 10000000
                ),
            manual_context_window INTEGER
                CHECK(
                    manual_context_window IS NULL
                    OR manual_context_window BETWEEN 256 AND 10000000
                ),
            discovered_metadata_json TEXT NOT NULL DEFAULT '{}'
                CHECK(
                    json_valid(discovered_metadata_json)
                    AND length(CAST(discovered_metadata_json AS BLOB)) <= 8192
                ),
            last_seen_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(provider_id, model_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_model_pools (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL CHECK(length(name) > 0 AND length(name) <= 100),
            description TEXT NOT NULL DEFAULT '',
            pool_kind TEXT NOT NULL
                CHECK(pool_kind IN ('primary', 'secondary', 'grok', 'custom')),
            fallback_pool_id INTEGER
                REFERENCES ai_model_pools(id) ON DELETE RESTRICT,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_model_pool_members (
            pool_id INTEGER NOT NULL
                REFERENCES ai_model_pools(id) ON DELETE CASCADE,
            provider_model_id INTEGER NOT NULL
                REFERENCES ai_provider_models(id) ON DELETE RESTRICT,
            position INTEGER NOT NULL CHECK(position > 0),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(pool_id, provider_model_id),
            UNIQUE(pool_id, position)
        )
        """
    )


def _create_ai_agents_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE ai_agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            task_type TEXT NOT NULL,
            binding_type TEXT NOT NULL DEFAULT 'fixed'
                CHECK(binding_type IN ('fixed', 'pool')),
            provider_id INTEGER REFERENCES ai_providers(id) ON DELETE RESTRICT,
            model TEXT,
            model_pool_id INTEGER
                REFERENCES ai_model_pools(id) ON DELETE RESTRICT,
            required_capabilities_json TEXT NOT NULL DEFAULT '[]'
                CHECK(json_valid(required_capabilities_json)),
            binding_version INTEGER NOT NULL DEFAULT 1 CHECK(binding_version > 0),
            system_prompt TEXT NOT NULL,
            temperature REAL NOT NULL DEFAULT 0.8,
            top_p REAL NOT NULL DEFAULT 0.9,
            max_tokens INTEGER NOT NULL DEFAULT 4000,
            context_window INTEGER NOT NULL DEFAULT 16000,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (
                (binding_type = 'fixed'
                    AND provider_id IS NOT NULL
                    AND model_pool_id IS NULL)
                OR (binding_type = 'pool'
                    AND provider_id IS NULL
                    AND model IS NULL
                    AND model_pool_id IS NOT NULL)
            )
        )
        """
    )


def _rebuild_ai_agents(conn: sqlite3.Connection) -> None:
    old_columns = _table_columns(conn, "ai_agents")
    if _AGENT_ROUTING_COLUMNS <= old_columns:
        return

    old_table = "ai_agents_model_routing_old"
    if _table_exists(conn, old_table):
        raise RuntimeError(f"temporary migration table already exists: {old_table}")

    conn.execute(f"ALTER TABLE ai_agents RENAME TO {old_table}")
    _create_ai_agents_table(conn)

    target_columns = [
        "id",
        "name",
        "task_type",
        "binding_type",
        "provider_id",
        "model",
        "model_pool_id",
        "required_capabilities_json",
        "binding_version",
        "system_prompt",
        "temperature",
        "top_p",
        "max_tokens",
        "context_window",
        "enabled",
        "created_at",
        "updated_at",
    ]
    defaults = {
        "binding_type": "'fixed'",
        "model_pool_id": "NULL",
        "required_capabilities_json": "'[]'",
        "binding_version": "1",
    }
    select_expressions = [
        column if column in old_columns else defaults[column]
        for column in target_columns
    ]
    conn.execute(
        f"""
        INSERT INTO ai_agents ({', '.join(target_columns)})
        SELECT {', '.join(select_expressions)}
        FROM {old_table}
        """
    )
    conn.execute(f"DROP TABLE {old_table}")


def _create_ai_jobs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE ai_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL UNIQUE,
            task_type TEXT NOT NULL,
            agent_id INTEGER,
            status TEXT NOT NULL DEFAULT 'running'
                CHECK(status IN ('running', 'succeeded', 'failed', 'partial', 'cancelled')),
            input_json TEXT NOT NULL,
            output_text TEXT,
            output_json TEXT,
            error_message TEXT,
            started_at TEXT,
            finished_at TEXT,
            candidate_snapshot_json TEXT
                CHECK(
                    candidate_snapshot_json IS NULL
                    OR length(CAST(candidate_snapshot_json AS BLOB)) <= 262144
                ),
            candidate_snapshot_hash TEXT,
            next_attempt_index INTEGER NOT NULL DEFAULT 0
                CHECK(next_attempt_index >= 0),
            owner_token TEXT,
            lease_until TEXT,
            heartbeat_at TEXT,
            stage TEXT NOT NULL DEFAULT 'main'
                CHECK(stage IN ('internal', 'main', 'validation')),
            pinned_candidate_index INTEGER,
            network_request_count INTEGER NOT NULL DEFAULT 0
                CHECK(network_request_count >= 0),
            candidate_attempt_count INTEGER NOT NULL DEFAULT 0
                CHECK(candidate_attempt_count >= 0),
            route_deadline_at TEXT,
            prompt_budget_json TEXT,
            parent_job_id TEXT,
            idempotency_key TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _rebuild_ai_jobs(conn: sqlite3.Connection) -> None:
    old_columns = _table_columns(conn, "ai_jobs")
    if _JOB_ROUTING_COLUMNS <= old_columns:
        return

    old_table = "ai_jobs_model_routing_old"
    if _table_exists(conn, old_table):
        raise RuntimeError(f"temporary migration table already exists: {old_table}")

    conn.execute(f"ALTER TABLE ai_jobs RENAME TO {old_table}")
    _create_ai_jobs_table(conn)

    target_columns = [
        "id",
        "job_id",
        "task_type",
        "agent_id",
        "status",
        "input_json",
        "output_text",
        "output_json",
        "error_message",
        "started_at",
        "finished_at",
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
        "created_at",
    ]
    defaults = {
        "candidate_snapshot_json": "NULL",
        "candidate_snapshot_hash": "NULL",
        "next_attempt_index": "0",
        "owner_token": "NULL",
        "lease_until": "NULL",
        "heartbeat_at": "NULL",
        "stage": "'main'",
        "pinned_candidate_index": "NULL",
        "network_request_count": "0",
        "candidate_attempt_count": "0",
        "route_deadline_at": "NULL",
        "prompt_budget_json": "NULL",
        "parent_job_id": "NULL",
        "idempotency_key": "NULL",
    }
    select_expressions = [
        column if column in old_columns else defaults[column]
        for column in target_columns
    ]
    conn.execute(
        f"""
        INSERT INTO ai_jobs ({', '.join(target_columns)})
        SELECT {', '.join(select_expressions)}
        FROM {old_table}
        """
    )
    conn.execute(f"DROP TABLE {old_table}")


def _create_attempt_and_sync_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_job_model_attempts (
            job_id TEXT NOT NULL REFERENCES ai_jobs(job_id) ON DELETE CASCADE,
            attempt_index INTEGER NOT NULL CHECK(attempt_index >= 0),
            pool_id INTEGER,
            provider_id INTEGER,
            provider_model_id INTEGER,
            pool_version_snapshot INTEGER,
            pool_position_snapshot INTEGER,
            model_key TEXT,
            pool_name_snapshot TEXT,
            provider_name_snapshot TEXT,
            agent_config_hash TEXT NOT NULL,
            provider_config_hash TEXT NOT NULL,
            candidate_list_hash TEXT NOT NULL,
            stage TEXT CHECK(stage IN ('internal', 'main', 'validation')),
            status TEXT
                CHECK(status IN ('running', 'succeeded', 'failed', 'partial', 'cancelled')),
            error_scope TEXT
                CHECK(error_scope IS NULL OR error_scope IN ('model', 'provider')),
            error_message TEXT,
            error_category TEXT,
            finish_reason TEXT
                CHECK(
                    finish_reason IS NULL
                    OR finish_reason IN (
                        'stop', 'complete', 'length', 'content_filter',
                        'missing', 'cancelled', 'error'
                    )
                ),
            output_started INTEGER NOT NULL DEFAULT 0
                CHECK(output_started IN (0, 1)),
            owner_token TEXT,
            lease_until TEXT,
            heartbeat_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            latency_ms INTEGER,
            UNIQUE(job_id, attempt_index)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_model_sync_operations (
            operation_id TEXT PRIMARY KEY,
            provider_id INTEGER NOT NULL,
            provider_name_snapshot TEXT NOT NULL,
            provider_config_hash TEXT NOT NULL,
            owner_token TEXT,
            status TEXT NOT NULL CHECK(
                status IN (
                    'queued', 'running', 'needs_empty_confirmation',
                    'succeeded', 'failed', 'cancelled'
                )
            ),
            pages INTEGER NOT NULL DEFAULT 0,
            discovered_count INTEGER NOT NULL DEFAULT 0,
            result_digest TEXT,
            partial_reason TEXT,
            error_code TEXT,
            error_message TEXT,
            generation INTEGER NOT NULL,
            cancel_requested INTEGER NOT NULL DEFAULT 0
                CHECK(cancel_requested IN (0, 1)),
            lease_until TEXT,
            heartbeat_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _add_provider_sync_columns(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "ai_providers")
    additions = {
        "models_synced_at": "TEXT",
        "models_sync_attempted_at": "TEXT",
        "models_sync_error": "TEXT",
        "models_sync_generation": "INTEGER NOT NULL DEFAULT 0",
        "models_sync_owner": "TEXT",
        "models_sync_lease_until": "TEXT",
    }
    for column, definition in additions.items():
        if column not in columns:
            conn.execute(
                f"ALTER TABLE ai_providers ADD COLUMN {column} {definition}"
            )


def _create_model_routing_indexes(conn: sqlite3.Connection) -> None:
    statements = (
        "CREATE INDEX IF NOT EXISTS idx_ai_agents_task_type "
        "ON ai_agents(task_type)",
        "CREATE INDEX IF NOT EXISTS idx_ai_agents_provider_id "
        "ON ai_agents(provider_id)",
        "CREATE INDEX IF NOT EXISTS idx_ai_agents_model_pool_id "
        "ON ai_agents(model_pool_id)",
        "CREATE INDEX IF NOT EXISTS idx_ai_jobs_job_id ON ai_jobs(job_id)",
        "CREATE INDEX IF NOT EXISTS idx_ai_jobs_created_at "
        "ON ai_jobs(created_at DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_jobs_parent_idempotency "
        "ON ai_jobs(parent_job_id, idempotency_key) "
        "WHERE parent_job_id IS NOT NULL AND idempotency_key IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_ai_model_sync_operations_provider "
        "ON ai_model_sync_operations(provider_id)",
        "CREATE INDEX IF NOT EXISTS idx_ai_model_sync_operations_status "
        "ON ai_model_sync_operations(status)",
        "CREATE INDEX IF NOT EXISTS idx_ai_model_sync_operations_lease "
        "ON ai_model_sync_operations(lease_until)",
        "CREATE INDEX IF NOT EXISTS idx_ai_model_sync_operations_generation "
        "ON ai_model_sync_operations(generation)",
    )
    for statement in statements:
        conn.execute(statement)


def _valid_model_key(value: Any) -> bool:
    if not isinstance(value, str) or not 0 < len(value) <= 300:
        return False
    if any(unicodedata.category(character) == "Cc" for character in value):
        return False
    try:
        return len(value.encode("utf-8")) <= 1200
    except UnicodeEncodeError:
        return False


def _warn_skipped_model(provider_id: int, element_index: int | None) -> None:
    logger.warning(
        "Skipping invalid ai_providers.available_models_json element "
        "for provider_id=%s at index=%s",
        provider_id,
        element_index,
    )


def _import_available_models(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, available_models_json
        FROM ai_providers
        WHERE available_models_json IS NOT NULL
        ORDER BY id
        """
    ).fetchall()
    for row in rows:
        provider_id = int(row[0])
        try:
            elements = json.loads(row[1])
        except (TypeError, ValueError):
            _warn_skipped_model(provider_id, None)
            continue
        if not isinstance(elements, list):
            _warn_skipped_model(provider_id, None)
            continue

        for index, element in enumerate(elements):
            if isinstance(element, str):
                model_key = element
            elif isinstance(element, dict) and isinstance(element.get("id"), str):
                model_key = element["id"]
            else:
                _warn_skipped_model(provider_id, index)
                continue

            if not _valid_model_key(model_key):
                _warn_skipped_model(provider_id, index)
                continue

            conn.execute(
                """
                INSERT INTO ai_provider_models (
                    provider_id, model_key, manual, discovered,
                    discovered_available
                ) VALUES (?, ?, 1, 0, 0)
                ON CONFLICT(provider_id, model_key)
                DO UPDATE SET manual = 1
                """,
                (provider_id, model_key),
            )


def assert_model_routing_foreign_keys(conn: sqlite3.Connection) -> None:
    """Raise when any database row violates a declared foreign key."""
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        details = [tuple(row) for row in violations]
        raise RuntimeError(f"foreign_key_check failed: {details!r}")


def _migrate_model_routing_schema_in_transaction(
    conn: sqlite3.Connection,
) -> None:
    _create_model_routing_tables(conn)
    _rebuild_ai_agents(conn)
    _rebuild_ai_jobs(conn)
    _create_attempt_and_sync_tables(conn)
    _add_provider_sync_columns(conn)
    _create_model_routing_indexes(conn)
    _import_available_models(conn)
    assert_model_routing_foreign_keys(conn)


def migrate_model_routing_schema(conn: sqlite3.Connection) -> None:
    """Install the model-routing schema atomically and idempotently."""
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("PRAGMA foreign_keys=OFF")

    try:
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        _migrate_model_routing_schema_in_transaction(conn)
        if owns_transaction:
            conn.commit()
    except BaseException:
        if owns_transaction and conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def prepare_model_routing_downgrade(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """保留：仅测试/兼容用途。Return a read-only legacy compatibility report for fixed agents."""
    if conn.execute(
        "SELECT 1 FROM ai_agents WHERE binding_type = 'pool' LIMIT 1"
    ).fetchone():
        raise RuntimeError(_POOL_DOWNGRADE_ERROR)

    agents = [
        {
            "id": int(row[0]),
            "binding_type": row[1],
            "provider_id": int(row[2]),
            "model": row[3],
        }
        for row in conn.execute(
            """
            SELECT id, binding_type, provider_id, model
            FROM ai_agents
            WHERE binding_type = 'fixed'
            ORDER BY id
            """
        ).fetchall()
    ]
    return {"compatible": True, "count": len(agents), "agents": agents}

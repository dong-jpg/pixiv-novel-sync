"""模型池与有序成员的存储操作。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from ...ai.model_pools import (
    ModelPoolConflictError,
    ModelPoolValidationError,
    validate_pool_graph,
)

_POOL_KINDS = {"primary", "secondary", "grok", "custom"}


class PoolsMixin:
    """模型池 CRUD。"""

    @staticmethod
    def _normalize_pool_name(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ModelPoolValidationError("模型池名称不能为空")
        name = value.strip()
        if len(name) > 100:
            raise ModelPoolValidationError("模型池名称不能超过 100 个字符")
        return name

    @staticmethod
    def _normalize_pool_kind(value: Any) -> str:
        if value not in _POOL_KINDS:
            raise ModelPoolValidationError("模型池类型无效")
        return str(value)

    def _pool_members_for_read(
        self, conn: sqlite3.Connection, pool_id: int
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT pm.provider_model_id, pm.position, pm.enabled,
                   m.provider_id, m.model_key
            FROM ai_model_pool_members AS pm
            JOIN ai_provider_models AS m ON m.id = pm.provider_model_id
            WHERE pm.pool_id = ?
            ORDER BY pm.position
            """,
            (pool_id,),
        ).fetchall()
        return [
            {
                "provider_model_id": int(row["provider_model_id"]),
                "provider_id": int(row["provider_id"]),
                "model_key": row["model_key"],
                "position": int(row["position"]),
                "enabled": bool(row["enabled"]),
            }
            for row in rows
        ]

    def _pool_graph_state(
        self, conn: sqlite3.Connection
    ) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
        pools = [
            dict(row)
            for row in conn.execute(
                "SELECT id, fallback_pool_id, enabled FROM ai_model_pools ORDER BY id"
            ).fetchall()
        ]
        members: dict[int, list[dict[str, Any]]] = {
            int(pool["id"]): [] for pool in pools
        }
        rows = conn.execute(
            """
            SELECT pm.pool_id, pm.provider_model_id, pm.enabled,
                   m.provider_id, m.model_key, m.enabled AS model_enabled,
                   m.manual, m.discovered_available,
                   p.enabled AS provider_enabled
            FROM ai_model_pool_members AS pm
            JOIN ai_provider_models AS m ON m.id = pm.provider_model_id
            JOIN ai_providers AS p ON p.id = m.provider_id
            ORDER BY pm.pool_id, pm.position
            """
        ).fetchall()
        for row in rows:
            item = dict(row)
            item["routable"] = bool(
                item["model_enabled"]
                and (item["manual"] or item["discovered_available"])
                and item["provider_enabled"]
            )
            members[int(row["pool_id"])].append(item)
        return pools, members

    @staticmethod
    def _pool_is_referenced(conn: sqlite3.Connection, pool_id: int) -> bool:
        agent = conn.execute(
            "SELECT 1 FROM ai_agents WHERE model_pool_id = ? LIMIT 1",
            (pool_id,),
        ).fetchone()
        fallback = conn.execute(
            """
            SELECT 1 FROM ai_model_pools
            WHERE fallback_pool_id = ? AND id != ?
            LIMIT 1
            """,
            (pool_id, pool_id),
        ).fetchone()
        return agent is not None or fallback is not None

    @classmethod
    def _require_pool_unreferenced(
        cls, conn: sqlite3.Connection, pool_id: int
    ) -> None:
        if cls._pool_is_referenced(conn, pool_id):
            raise ModelPoolConflictError(
                "模型池仍被 Agent 或后备模型池引用，无法执行该操作"
            )

    def _pool_from_row(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        pool_id = int(row["id"])
        agents = conn.execute(
            "SELECT id, name FROM ai_agents WHERE model_pool_id = ? ORDER BY id",
            (pool_id,),
        ).fetchall()
        pools = conn.execute(
            "SELECT id, name FROM ai_model_pools WHERE fallback_pool_id = ? ORDER BY id",
            (pool_id,),
        ).fetchall()
        return {
            "id": pool_id,
            "name": row["name"],
            "description": row["description"],
            "pool_kind": row["pool_kind"],
            "fallback_pool_id": (
                int(row["fallback_pool_id"])
                if row["fallback_pool_id"] is not None
                else None
            ),
            "fallback_pool_name": row["fallback_pool_name"],
            "enabled": bool(row["enabled"]),
            "version": int(row["version"]),
            "members": self._pool_members_for_read(conn, pool_id),
            "referenced_by_agents": [
                {"id": int(agent["id"]), "name": agent["name"]} for agent in agents
            ],
            "referenced_by_pools": [
                {"id": int(pool["id"]), "name": pool["name"]} for pool in pools
            ],
        }

    def list_ai_model_pools(self) -> list[dict[str, Any]]:
        with self.read_transaction() as conn:
            rows = conn.execute(
                """
                SELECT p.*, fallback.name AS fallback_pool_name
                FROM ai_model_pools AS p
                LEFT JOIN ai_model_pools AS fallback ON fallback.id = p.fallback_pool_id
                ORDER BY p.id
                """
            ).fetchall()
            return [self._pool_from_row(conn, row) for row in rows]

    def get_ai_model_pool(self, pool_id: int) -> dict[str, Any] | None:
        with self.read_transaction() as conn:
            row = conn.execute(
                """
                SELECT p.*, fallback.name AS fallback_pool_name
                FROM ai_model_pools AS p
                LEFT JOIN ai_model_pools AS fallback ON fallback.id = p.fallback_pool_id
                WHERE p.id = ?
                """,
                (pool_id,),
            ).fetchone()
            return None if row is None else self._pool_from_row(conn, row)

    def create_ai_model_pool(self, data: Mapping[str, Any]) -> int:
        name = self._normalize_pool_name(data.get("name"))
        pool_kind = self._normalize_pool_kind(data.get("pool_kind"))
        description = str(data.get("description") or "")
        fallback_pool_id = data.get("fallback_pool_id")
        enabled = 1 if data.get("enabled", False) else 0
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO ai_model_pools (
                    name, description, pool_kind, fallback_pool_id, enabled
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (name, description, pool_kind, fallback_pool_id, enabled),
            )
            pool_id = int(cursor.lastrowid)
            pools, graph_members = self._pool_graph_state(conn)
            validate_pool_graph(pools, graph_members)
            return pool_id

    def update_ai_model_pool(
        self,
        pool_id: int,
        patch: Mapping[str, Any],
        expected_version: int,
    ) -> int:
        fields: list[str] = []
        params: list[Any] = []
        if "name" in patch:
            fields.append("name = ?")
            params.append(self._normalize_pool_name(patch["name"]))
        if "description" in patch:
            fields.append("description = ?")
            params.append(str(patch["description"] or ""))
        if "pool_kind" in patch:
            fields.append("pool_kind = ?")
            params.append(self._normalize_pool_kind(patch["pool_kind"]))
        if "fallback_pool_id" in patch:
            fallback_id = patch["fallback_pool_id"]
            fields.append("fallback_pool_id = ?")
            params.append(None if fallback_id is None else int(fallback_id))
        if "enabled" in patch:
            fields.append("enabled = ?")
            params.append(1 if patch["enabled"] else 0)

        with self.transaction() as conn:
            row = conn.execute(
                "SELECT version FROM ai_model_pools WHERE id = ?", (pool_id,)
            ).fetchone()
            if row is None:
                raise ModelPoolValidationError("模型池不存在")
            current_version = int(row["version"])
            if current_version != int(expected_version):
                raise ModelPoolConflictError("模型池版本冲突，请刷新后重试")
            if not fields:
                return current_version
            if "enabled" in patch and not bool(patch["enabled"]):
                self._require_pool_unreferenced(conn, pool_id)

            conn.execute(
                f"UPDATE ai_model_pools SET {', '.join(fields)} WHERE id = ?",
                [*params, pool_id],
            )
            pools, graph_members = self._pool_graph_state(conn)
            validate_pool_graph(pools, graph_members)
            next_version = current_version + 1
            conn.execute(
                """
                UPDATE ai_model_pools
                SET version = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (next_version, pool_id),
            )
            return next_version

    def replace_ai_model_pool_members(
        self,
        pool_id: int,
        members: Sequence[Mapping[str, Any]],
        expected_version: int,
    ) -> int:
        normalized: list[tuple[int, int]] = []
        seen_model_ids: set[int] = set()
        if len(members) > 64:
            raise ModelPoolValidationError("单个模型池最多包含 64 个成员")
        for member in members:
            model_id = int(member["provider_model_id"])
            if model_id in seen_model_ids:
                raise ModelPoolValidationError("模型池成员不能重复")
            seen_model_ids.add(model_id)
            normalized.append((model_id, 1 if member.get("enabled", True) else 0))

        with self.transaction() as conn:
            row = conn.execute(
                "SELECT version FROM ai_model_pools WHERE id = ?", (pool_id,)
            ).fetchone()
            if row is None:
                raise ModelPoolValidationError("模型池不存在")
            current_version = int(row["version"])
            if current_version != int(expected_version):
                raise ModelPoolConflictError("模型池版本冲突，请刷新后重试")
            if not any(enabled for _model_id, enabled in normalized):
                self._require_pool_unreferenced(conn, pool_id)

            if normalized:
                placeholders = ",".join("?" for _ in normalized)
                existing_ids = {
                    int(model_row[0])
                    for model_row in conn.execute(
                        f"SELECT id FROM ai_provider_models WHERE id IN ({placeholders})",
                        [model_id for model_id, _enabled in normalized],
                    ).fetchall()
                }
                if existing_ids != seen_model_ids:
                    raise ModelPoolValidationError("模型池成员引用的模型不存在")

            conn.execute("DELETE FROM ai_model_pool_members WHERE pool_id = ?", (pool_id,))
            for position, (model_id, enabled) in enumerate(normalized, start=1):
                conn.execute(
                    """
                    INSERT INTO ai_model_pool_members (
                        pool_id, provider_model_id, position, enabled
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (pool_id, model_id, position, enabled),
                )

            pools, graph_members = self._pool_graph_state(conn)
            validate_pool_graph(pools, graph_members)
            next_version = current_version + 1
            conn.execute(
                """
                UPDATE ai_model_pools
                SET version = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (next_version, pool_id),
            )
            return next_version

    def delete_ai_model_pool(self, pool_id: int) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM ai_model_pools WHERE id = ?", (pool_id,)
            ).fetchone()
            if row is None:
                return
            self._require_pool_unreferenced(conn, pool_id)
            conn.execute("DELETE FROM ai_model_pools WHERE id = ?", (pool_id,))

    def list_ai_model_pool_attempts(
        self, pool_id: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 200))
        rows = self.conn.execute(
            """
            SELECT job_id, attempt_index, pool_id, provider_id, provider_model_id,
                   pool_version_snapshot, pool_position_snapshot, model_key,
                   pool_name_snapshot, provider_name_snapshot, stage, status,
                   error_scope, error_message, error_category, finish_reason,
                   output_started, started_at, finished_at, latency_ms
            FROM ai_job_model_attempts
            WHERE pool_id = ?
            ORDER BY started_at DESC, job_id DESC, attempt_index DESC
            LIMIT ?
            """,
            (pool_id, bounded_limit),
        ).fetchall()
        return [
            {
                "job_id": row["job_id"],
                "attempt_index": int(row["attempt_index"]),
                "pool_id": int(row["pool_id"]),
                "pool_version": row["pool_version_snapshot"],
                "pool_position": row["pool_position_snapshot"],
                "pool_name": row["pool_name_snapshot"],
                "provider_id": row["provider_id"],
                "provider_model_id": row["provider_model_id"],
                "provider_name": row["provider_name_snapshot"],
                "model_key": row["model_key"],
                "stage": row["stage"],
                "status": row["status"],
                "error_scope": row["error_scope"],
                "error_message": row["error_message"],
                "error_category": row["error_category"],
                "finish_reason": row["finish_reason"],
                "output_started": bool(row["output_started"]),
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "latency_ms": row["latency_ms"],
            }
            for row in rows
        ]

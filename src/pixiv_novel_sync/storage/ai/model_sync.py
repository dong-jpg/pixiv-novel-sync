"""模型目录同步 operation 的租约与终态存储。"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ...ai.model_catalog import canonical_model_digest
from ...ai.model_sync import (
    ModelSyncConflictError,
    provider_model_sync_config_hash,
)


_LEASE_SECONDS = 45


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sql_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_sql_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _bounded_error_text(value: Any, limit: int = 2000) -> str:
    text = str(value or "").encode("utf-8", errors="replace").decode("utf-8")
    return text[:limit]


class ModelSyncStorageMixin:
    """模型同步 operation storage API。"""

    @staticmethod
    def _model_sync_operation_from_row(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["provider_id"] = int(item["provider_id"])
        item["generation"] = int(item["generation"])
        item["pages"] = int(item.get("pages") or 0)
        item["discovered_count"] = int(item.get("discovered_count") or 0)
        item["cancel_requested"] = bool(item.get("cancel_requested"))
        item.pop("owner_token", None)
        item.pop("provider_config_hash", None)
        return item

    def get_model_sync_operation(self, operation_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM ai_model_sync_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return None if row is None else self._model_sync_operation_from_row(row)

    def create_model_sync_operation(
        self,
        provider_id: int,
        provider_name: str,
        provider_config_hash: str,
        owner_token: str,
    ) -> dict[str, Any]:
        now = _utc_now()
        now_sql = _sql_timestamp(now)
        lease_until = _sql_timestamp(now + timedelta(seconds=_LEASE_SECONDS))
        operation_id = uuid.uuid4().hex

        with self.transaction() as conn:
            provider = conn.execute(
                "SELECT * FROM ai_providers WHERE id = ?",
                (provider_id,),
            ).fetchone()
            if provider is None:
                raise ModelSyncConflictError("Provider 不存在")
            if provider_model_sync_config_hash(dict(provider)) != provider_config_hash:
                raise ModelSyncConflictError("Provider 配置已变化，请刷新后重试")

            active = conn.execute(
                """
                SELECT operation_id
                FROM ai_model_sync_operations
                WHERE provider_id = ?
                  AND status IN ('queued', 'running')
                  AND lease_until > ?
                ORDER BY generation DESC
                LIMIT 1
                """,
                (provider_id, now_sql),
            ).fetchone()
            if active is not None:
                existing_id = str(active["operation_id"])
                raise ModelSyncConflictError(
                    "Provider 已有模型同步任务正在运行",
                    existing_operation_id=existing_id,
                )

            conn.execute(
                """
                UPDATE ai_model_sync_operations
                SET status = 'failed', error_code = 'process_interrupted',
                    error_message = '模型同步租约已失效', finished_at = ?,
                    lease_until = NULL
                WHERE provider_id = ?
                  AND status IN ('queued', 'running')
                """,
                (now_sql, provider_id),
            )
            generation = int(provider["models_sync_generation"] or 0) + 1
            conn.execute(
                """
                UPDATE ai_providers
                SET models_sync_generation = ?, models_sync_owner = ?,
                    models_sync_lease_until = ?, models_sync_attempted_at = ?
                WHERE id = ?
                """,
                (generation, owner_token, lease_until, now_sql, provider_id),
            )
            conn.execute(
                """
                INSERT INTO ai_model_sync_operations (
                    operation_id, provider_id, provider_name_snapshot,
                    provider_config_hash, owner_token, status, generation,
                    lease_until, heartbeat_at, created_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    provider_id,
                    provider_name,
                    provider_config_hash,
                    owner_token,
                    generation,
                    lease_until,
                    now_sql,
                    now_sql,
                ),
            )

        operation = self.get_model_sync_operation(operation_id)
        assert operation is not None
        return operation

    @staticmethod
    def _owned_model_sync_rows(
        conn: Any,
        operation_id: str,
        owner_token: str,
        generation: int,
        *,
        status: str,
        allow_cancel_requested: bool = False,
    ) -> tuple[Any, Any] | None:
        operation = conn.execute(
            """
            SELECT * FROM ai_model_sync_operations
            WHERE operation_id = ? AND owner_token = ?
              AND generation = ? AND status = ?
            """,
            (operation_id, owner_token, generation, status),
        ).fetchone()
        if operation is None:
            return None
        if not allow_cancel_requested and bool(operation["cancel_requested"]):
            return None
        provider = conn.execute(
            "SELECT * FROM ai_providers WHERE id = ?",
            (operation["provider_id"],),
        ).fetchone()
        if provider is None:
            return None
        if (
            int(provider["models_sync_generation"] or 0) != int(generation)
            or provider["models_sync_owner"] != owner_token
            or provider_model_sync_config_hash(dict(provider))
            != operation["provider_config_hash"]
        ):
            return None
        return operation, provider

    def claim_model_sync_operation(
        self,
        operation_id: str,
        owner_token: str,
        generation: int,
    ) -> bool:
        now = _utc_now()
        now_sql = _sql_timestamp(now)
        lease_until = _sql_timestamp(now + timedelta(seconds=_LEASE_SECONDS))
        with self.transaction() as conn:
            owned = self._owned_model_sync_rows(
                conn,
                operation_id,
                owner_token,
                generation,
                status="queued",
                allow_cancel_requested=True,
            )
            if owned is None:
                return False
            operation, _provider = owned
            conn.execute(
                """
                UPDATE ai_model_sync_operations
                SET status = 'running', started_at = COALESCE(started_at, ?),
                    heartbeat_at = ?, lease_until = ?
                WHERE operation_id = ?
                """,
                (now_sql, now_sql, lease_until, operation_id),
            )
            conn.execute(
                """
                UPDATE ai_providers SET models_sync_lease_until = ?
                WHERE id = ?
                """,
                (lease_until, operation["provider_id"]),
            )
            return True

    def heartbeat_model_sync_operation(
        self,
        operation_id: str,
        owner_token: str,
        generation: int,
    ) -> bool:
        now = _utc_now()
        now_sql = _sql_timestamp(now)
        lease_until = _sql_timestamp(now + timedelta(seconds=_LEASE_SECONDS))
        with self.transaction() as conn:
            owned = self._owned_model_sync_rows(
                conn,
                operation_id,
                owner_token,
                generation,
                status="running",
            )
            if owned is None:
                return False
            operation, _provider = owned
            conn.execute(
                """
                UPDATE ai_model_sync_operations
                SET heartbeat_at = ?, lease_until = ?
                WHERE operation_id = ?
                """,
                (now_sql, lease_until, operation_id),
            )
            conn.execute(
                """
                UPDATE ai_providers SET models_sync_lease_until = ?
                WHERE id = ?
                """,
                (lease_until, operation["provider_id"]),
            )
            return True

    def update_model_sync_progress(
        self,
        operation_id: str,
        owner_token: str,
        generation: int,
        *,
        pages: int,
        discovered_count: int,
    ) -> bool:
        if pages < 0 or discovered_count < 0:
            raise ValueError("模型同步进度不能为负数")
        now = _utc_now()
        now_sql = _sql_timestamp(now)
        lease_until = _sql_timestamp(now + timedelta(seconds=_LEASE_SECONDS))
        with self.transaction() as conn:
            owned = self._owned_model_sync_rows(
                conn,
                operation_id,
                owner_token,
                generation,
                status="running",
            )
            if owned is None:
                return False
            operation, _provider = owned
            conn.execute(
                """
                UPDATE ai_model_sync_operations
                SET pages = MAX(pages, ?),
                    discovered_count = MAX(discovered_count, ?),
                    heartbeat_at = ?, lease_until = ?
                WHERE operation_id = ?
                """,
                (pages, discovered_count, now_sql, lease_until, operation_id),
            )
            conn.execute(
                """
                UPDATE ai_providers SET models_sync_lease_until = ?
                WHERE id = ?
                """,
                (lease_until, operation["provider_id"]),
            )
            return True

    def finish_model_sync_success(
        self,
        operation_id: str,
        owner_token: str,
        generation: int,
        models: Sequence[Mapping[str, Any]],
        result_digest: str,
        *,
        empty_authoritative: bool = False,
        partial_reason: str | None = None,
    ) -> bool:
        if canonical_model_digest(models) != result_digest:
            raise ModelSyncConflictError("模型目录结果摘要不匹配")
        now_sql = _sql_timestamp(_utc_now())
        with self.transaction() as conn:
            owned = self._owned_model_sync_rows(
                conn,
                operation_id,
                owner_token,
                generation,
                status="running",
            )
            if owned is None:
                return False
            operation, _provider = owned
            if not models and not empty_authoritative:
                conn.execute(
                    """
                    UPDATE ai_model_sync_operations
                    SET status = 'needs_empty_confirmation', result_digest = ?,
                        partial_reason = ?, discovered_count = 0,
                        heartbeat_at = ?, lease_until = NULL
                    WHERE operation_id = ?
                    """,
                    (result_digest, partial_reason, now_sql, operation_id),
                )
                conn.execute(
                    """
                    UPDATE ai_providers
                    SET models_sync_owner = NULL, models_sync_lease_until = NULL
                    WHERE id = ?
                    """,
                    (operation["provider_id"],),
                )
                return True

            self.upsert_discovered_models(
                int(operation["provider_id"]),
                models,
                generation,
            )
            conn.execute(
                """
                UPDATE ai_providers
                SET models_synced_at = ?, models_sync_error = NULL,
                    models_sync_owner = NULL, models_sync_lease_until = NULL
                WHERE id = ?
                """,
                (now_sql, operation["provider_id"]),
            )
            conn.execute(
                """
                UPDATE ai_model_sync_operations
                SET status = 'succeeded', result_digest = ?, partial_reason = ?,
                    discovered_count = ?, heartbeat_at = ?, finished_at = ?,
                    lease_until = NULL
                WHERE operation_id = ?
                """,
                (
                    result_digest,
                    partial_reason,
                    len(models),
                    now_sql,
                    now_sql,
                    operation_id,
                ),
            )
            return True

    def finish_model_sync_failure(
        self,
        operation_id: str,
        owner_token: str,
        generation: int,
        *,
        error_code: str,
        error_message: str,
        cancelled: bool = False,
    ) -> bool:
        status = "cancelled" if cancelled or error_code == "cancelled" else "failed"
        safe_code = _bounded_error_text(error_code, 100) or "unknown_error"
        safe_message = _bounded_error_text(error_message)
        now_sql = _sql_timestamp(_utc_now())
        with self.transaction() as conn:
            owned = self._owned_model_sync_rows(
                conn,
                operation_id,
                owner_token,
                generation,
                status="running",
                allow_cancel_requested=status == "cancelled",
            )
            if owned is None:
                return False
            operation, _provider = owned
            conn.execute(
                """
                UPDATE ai_model_sync_operations
                SET status = ?, error_code = ?, error_message = ?,
                    heartbeat_at = ?, finished_at = ?, lease_until = NULL
                WHERE operation_id = ?
                """,
                (
                    status,
                    safe_code,
                    safe_message,
                    now_sql,
                    now_sql,
                    operation_id,
                ),
            )
            conn.execute(
                """
                UPDATE ai_providers
                SET models_sync_attempted_at = ?, models_sync_error = ?,
                    models_sync_owner = NULL, models_sync_lease_until = NULL
                WHERE id = ?
                """,
                (now_sql, safe_message, operation["provider_id"]),
            )
            return True

    def confirm_model_sync_empty(
        self,
        operation_id: str,
        generation: int,
        result_digest: str,
    ) -> dict[str, int]:
        now_sql = _sql_timestamp(_utc_now())
        with self.transaction() as conn:
            operation = conn.execute(
                "SELECT * FROM ai_model_sync_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if (
                operation is None
                or operation["status"] != "needs_empty_confirmation"
                or int(operation["generation"]) != int(generation)
                or operation["result_digest"] != result_digest
                or result_digest != canonical_model_digest([])
            ):
                raise ModelSyncConflictError(
                    "空模型目录确认信息已失效，请重新同步"
                )
            provider = conn.execute(
                "SELECT * FROM ai_providers WHERE id = ?",
                (operation["provider_id"],),
            ).fetchone()
            if (
                provider is None
                or int(provider["models_sync_generation"] or 0) != int(generation)
                or provider_model_sync_config_hash(dict(provider))
                != operation["provider_config_hash"]
            ):
                raise ModelSyncConflictError(
                    "Provider 配置或同步 generation 已变化，请重新同步"
                )

            stats = self.upsert_discovered_models(
                int(operation["provider_id"]),
                [],
                generation,
            )
            conn.execute(
                """
                UPDATE ai_providers
                SET models_synced_at = ?, models_sync_error = NULL,
                    models_sync_owner = NULL, models_sync_lease_until = NULL
                WHERE id = ?
                """,
                (now_sql, operation["provider_id"]),
            )
            conn.execute(
                """
                UPDATE ai_model_sync_operations
                SET status = 'succeeded', finished_at = ?, heartbeat_at = ?,
                    lease_until = NULL
                WHERE operation_id = ?
                """,
                (now_sql, now_sql, operation_id),
            )
            return stats

    def request_model_sync_cancel(self, operation_id: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE ai_model_sync_operations
                SET cancel_requested = 1
                WHERE operation_id = ?
                  AND status IN ('queued', 'running')
                  AND cancel_requested = 0
                """,
                (operation_id,),
            )
            return cursor.rowcount == 1

    def reconcile_model_sync_operations(
        self,
        now: datetime | None = None,
    ) -> int:
        current = now or _utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        queue_cutoff = current - timedelta(minutes=5)
        heartbeat_cutoff = current - timedelta(seconds=_LEASE_SECONDS)
        now_sql = _sql_timestamp(current)
        reconciled = 0

        with self.transaction() as conn:
            operations = conn.execute(
                """
                SELECT * FROM ai_model_sync_operations
                WHERE status IN ('queued', 'running')
                ORDER BY created_at, operation_id
                """
            ).fetchall()
            for operation in operations:
                provider = conn.execute(
                    "SELECT * FROM ai_providers WHERE id = ?",
                    (operation["provider_id"],),
                ).fetchone()
                error_code: str | None = None
                error_message: str | None = None

                if operation["status"] == "queued":
                    created_at = _parse_sql_timestamp(operation["created_at"])
                    if created_at is None or created_at <= queue_cutoff:
                        error_code = "queue_timeout"
                        error_message = "模型同步排队超过 5 分钟"
                else:
                    owner_matches = bool(
                        provider is not None
                        and provider["models_sync_owner"] == operation["owner_token"]
                        and int(provider["models_sync_generation"] or 0)
                        == int(operation["generation"])
                        and provider_model_sync_config_hash(dict(provider))
                        == operation["provider_config_hash"]
                    )
                    lease_until = _parse_sql_timestamp(operation["lease_until"])
                    heartbeat_at = _parse_sql_timestamp(operation["heartbeat_at"])
                    lease_expired = lease_until is None or lease_until <= current
                    heartbeat_expired = (
                        heartbeat_at is None or heartbeat_at <= heartbeat_cutoff
                    )
                    if not owner_matches or (lease_expired and heartbeat_expired):
                        error_code = "process_interrupted"
                        error_message = "模型同步进程中断或租约失效"

                if error_code is None or error_message is None:
                    continue
                cursor = conn.execute(
                    """
                    UPDATE ai_model_sync_operations
                    SET status = 'failed', error_code = ?, error_message = ?,
                        finished_at = ?, lease_until = NULL
                    WHERE operation_id = ? AND status IN ('queued', 'running')
                    """,
                    (
                        error_code,
                        error_message,
                        now_sql,
                        operation["operation_id"],
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                reconciled += 1
                if (
                    provider is not None
                    and provider["models_sync_owner"] == operation["owner_token"]
                    and int(provider["models_sync_generation"] or 0)
                    == int(operation["generation"])
                ):
                    conn.execute(
                        """
                        UPDATE ai_providers
                        SET models_sync_attempted_at = ?, models_sync_error = ?,
                            models_sync_owner = NULL,
                            models_sync_lease_until = NULL
                        WHERE id = ?
                        """,
                        (now_sql, error_message, operation["provider_id"]),
                    )
        return reconciled

    def cleanup_model_sync_operations(self, keep_days: int = 3) -> int:
        """保留：仅测试/兼容用途。"""
        days = max(0, int(keep_days))
        cutoff = _sql_timestamp(_utc_now() - timedelta(days=days))
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                DELETE FROM ai_model_sync_operations
                WHERE status IN ('succeeded', 'failed', 'cancelled')
                  AND finished_at IS NOT NULL
                  AND finished_at < ?
                """,
                (cutoff,),
            )
            return int(cursor.rowcount or 0)

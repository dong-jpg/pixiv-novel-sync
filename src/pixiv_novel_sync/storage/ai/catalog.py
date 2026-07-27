"""Provider 模型目录存储 mixin。

负责 ``ai_provider_models`` 的 CRUD、同步 upsert 和派生字段/统计。核心规则
（见 2026-07-23-ai-model-catalog-pools-design.md 第 5.1 节）：

- ``source`` 是派生值：仅 discovered 为 ``discovered``，仅 manual 为 ``manual``，
  两者都为真时为 ``both``。
- 有效显示名/能力/上下文优先使用 ``manual_*``，为空再用 ``discovered_*``。
- 同步只更新 ``discovered_*``、``discovered_available``、``last_seen_at``，
  永不覆盖人工字段或用户 ``enabled``。
- 有效可路由条件：``enabled=1 AND (manual=1 OR discovered_available=1)``，
  且所属 Provider 必须 enabled。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from ...ai.model_catalog import (
    ModelCatalogConflictError,
    ModelCatalogValidationError,
    normalize_capabilities,
    normalize_model_key,
    normalize_model_record,
    validate_text_field,
)

_DISPLAY_NAME_MAX_CODEPOINTS = 200
_DISPLAY_NAME_MAX_BYTES = 800
_CONTEXT_WINDOW_MIN = 256
_CONTEXT_WINDOW_MAX = 10_000_000
_METADATA_MAX_BYTES = 8192


class CatalogMixin:
    """Provider 模型目录存储操作。"""

    # --- 派生与序列化辅助 ---

    @staticmethod
    def _catalog_capabilities(raw: Any) -> list[str]:
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return list(value) if isinstance(value, list) else []

    def _row_to_provider_model(self, row: sqlite3.Row, *, provider_enabled: bool) -> dict[str, Any]:
        item = dict(row)
        discovered = bool(item.get("discovered"))
        manual = bool(item.get("manual"))
        discovered_available = bool(item.get("discovered_available"))
        enabled = bool(item.get("enabled"))

        if manual and discovered:
            source = "both"
        elif manual:
            source = "manual"
        else:
            source = "discovered"

        manual_caps = self._catalog_capabilities(item.get("manual_capabilities_json"))
        discovered_caps = self._catalog_capabilities(item.get("discovered_capabilities_json"))
        effective_caps = manual_caps if manual_caps else discovered_caps

        effective_display = item.get("manual_display_name") or item.get("discovered_display_name")
        effective_context = item.get("manual_context_window")
        if effective_context is None:
            effective_context = item.get("discovered_context_window")

        routable = bool(enabled and (manual or discovered_available) and provider_enabled)

        return {
            "id": int(item["id"]),
            "provider_id": int(item["provider_id"]),
            "model_key": item["model_key"],
            "discovered": discovered,
            "manual": manual,
            "discovered_available": discovered_available,
            "enabled": enabled,
            "source": source,
            "display_name": effective_display,
            "discovered_display_name": item.get("discovered_display_name"),
            "manual_display_name": item.get("manual_display_name"),
            "capabilities": effective_caps,
            "discovered_capabilities": discovered_caps,
            "manual_capabilities": manual_caps,
            "context_window": effective_context,
            "discovered_context_window": item.get("discovered_context_window"),
            "manual_context_window": item.get("manual_context_window"),
            "last_seen_at": item.get("last_seen_at"),
            "routable": routable,
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        }

    def _provider_enabled(self, provider_id: int) -> bool:
        row = self.conn.execute(
            "SELECT enabled FROM ai_providers WHERE id = ?", (provider_id,)
        ).fetchone()
        return bool(row[0]) if row is not None else False

    # --- 读取 ---

    def list_ai_provider_models(
        self,
        provider_id: int,
        *,
        search: str | None = None,
        routable_only: bool = False,
        enabled_only: bool = False,
    ) -> dict[str, Any]:
        with self.read_transaction():
            provider_enabled = self._provider_enabled(provider_id)
            conditions = ["provider_id = ?"]
            params: list[Any] = [provider_id]
            if search:
                conditions.append("model_key LIKE ?")
                params.append(f"%{search}%")
            if enabled_only:
                conditions.append("enabled = 1")
            where = " AND ".join(conditions)
            rows = self.conn.execute(
                f"SELECT * FROM ai_provider_models WHERE {where} ORDER BY id",
                params,
            ).fetchall()
            items = [
                self._row_to_provider_model(row, provider_enabled=provider_enabled)
                for row in rows
            ]
            if routable_only:
                items = [item for item in items if item["routable"]]

            counts = self.conn.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(discovered_available = 1), 0) AS discovered_available,
                       COALESCE(SUM(
                           enabled = 1 AND (manual = 1 OR discovered_available = 1)
                       ), 0) AS routable
                FROM ai_provider_models
                WHERE provider_id = ?
                """,
                (provider_id,),
            ).fetchone()
        return {
            "items": items,
            "total": int(counts["total"]),
            "discovered_available": int(counts["discovered_available"]),
            "routable": int(counts["routable"]) if provider_enabled else 0,
        }

    def get_ai_provider_model(self, model_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM ai_provider_models WHERE id = ?", (model_id,)
        ).fetchone()
        if row is None:
            return None
        provider_enabled = self._provider_enabled(int(row["provider_id"]))
        return self._row_to_provider_model(row, provider_enabled=provider_enabled)

    # --- 写入 ---

    @staticmethod
    def _dump_capabilities(value: Any) -> str | None:
        if value is None:
            return None
        caps = normalize_capabilities(value)
        return json.dumps(list(caps), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _validate_context_window(value: Any) -> int | None:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise ModelCatalogValidationError("context_window 必须是整数")
        if not (_CONTEXT_WINDOW_MIN <= value <= _CONTEXT_WINDOW_MAX):
            raise ModelCatalogValidationError(
                f"context_window 必须在 {_CONTEXT_WINDOW_MIN} 到 "
                f"{_CONTEXT_WINDOW_MAX} 之间"
            )
        return value

    def create_ai_provider_model(self, data: Mapping[str, Any]) -> int:
        model_key = normalize_model_key(data.get("model_key"))
        manual_display = data.get("manual_display_name")
        if manual_display is not None:
            manual_display = validate_text_field(
                manual_display,
                "manual_display_name",
                _DISPLAY_NAME_MAX_CODEPOINTS,
                _DISPLAY_NAME_MAX_BYTES,
            )
        manual_caps = self._dump_capabilities(data.get("manual_capabilities"))
        manual_context = self._validate_context_window(data.get("manual_context_window"))
        # 人工补录默认 manual=1；enabled 缺省为 1
        enabled = 1 if data.get("enabled", 1) else 0
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO ai_provider_models (
                    provider_id, model_key, manual, discovered, discovered_available,
                    enabled, manual_display_name, manual_capabilities_json,
                    manual_context_window
                ) VALUES (?, ?, 1, 0, 0, ?, ?, ?, ?)
                """,
                (
                    int(data["provider_id"]),
                    model_key,
                    enabled,
                    manual_display,
                    manual_caps,
                    manual_context,
                ),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_ai_provider_model(self, model_id: int, patch: Mapping[str, Any]) -> None:
        """只更新用户可写字段：``enabled`` 和 ``manual_*``。

        ``discovered_*`` 是同步事实，不可由此接口伪造。
        """
        fields: list[str] = []
        params: list[Any] = []
        if "enabled" in patch:
            fields.append("enabled = ?")
            params.append(1 if patch["enabled"] else 0)
        if "manual_display_name" in patch:
            value = patch["manual_display_name"]
            if value is not None:
                value = validate_text_field(
                    value,
                    "manual_display_name",
                    _DISPLAY_NAME_MAX_CODEPOINTS,
                    _DISPLAY_NAME_MAX_BYTES,
                )
            fields.append("manual_display_name = ?")
            params.append(value)
            # 补录人工显示名意味着该行成为 manual 来源
            fields.append("manual = 1")
        if "manual_capabilities" in patch:
            fields.append("manual_capabilities_json = ?")
            params.append(self._dump_capabilities(patch["manual_capabilities"]))
            fields.append("manual = 1")
        if "manual_context_window" in patch:
            fields.append("manual_context_window = ?")
            params.append(self._validate_context_window(patch["manual_context_window"]))
            fields.append("manual = 1")
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(model_id)
        with self._lock:
            self.conn.execute(
                f"UPDATE ai_provider_models SET {', '.join(fields)} WHERE id = ?",
                params,
            )
            self._commit_if_needed()

    def remove_ai_provider_model_manual(self, model_id: int) -> None:
        """清除人工保留标记。

        - 若纯人工行被池成员引用：返回中文冲突错误。
        - 若纯人工行无引用：删除整行。
        - 若同时是 discovered 行：保留发现记录，仅清除 manual 字段。
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT discovered FROM ai_provider_models WHERE id = ?",
                (model_id,),
            ).fetchone()
            if row is None:
                return
            if not bool(row[0]):
                referenced = conn.execute(
                    "SELECT 1 FROM ai_model_pool_members WHERE provider_model_id = ? LIMIT 1",
                    (model_id,),
                ).fetchone()
                if referenced is not None:
                    raise ModelCatalogConflictError(
                        "该模型仍被模型池引用，无法删除；请先从模型池移除"
                    )
                conn.execute(
                    "DELETE FROM ai_provider_models WHERE id = ?", (model_id,)
                )
            else:
                conn.execute(
                    """
                    UPDATE ai_provider_models
                    SET manual = 0,
                        manual_display_name = NULL,
                        manual_capabilities_json = NULL,
                        manual_context_window = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (model_id,),
                )

    @classmethod
    def _normalize_discovered_record(cls, model: Mapping[str, Any]) -> dict[str, Any]:
        """把一条发现记录归一化为目录条目。

        接受两种输入形态：已带 ``model_key`` 的归一化记录，或带上游 ``id``
        的原始记录。两者都经过同一套字段校验与 NFC 规范化。
        """
        if "model_key" not in model:
            return normalize_model_record(model)

        display_name = model.get("display_name")
        if display_name is not None:
            display_name = validate_text_field(
                display_name,
                "display_name",
                _DISPLAY_NAME_MAX_CODEPOINTS,
                _DISPLAY_NAME_MAX_BYTES,
            )

        metadata_json = model.get("metadata_json", "{}")
        if not isinstance(metadata_json, str):
            raise ModelCatalogValidationError("metadata_json 必须是字符串")
        try:
            metadata = json.loads(metadata_json)
        except (TypeError, ValueError) as exc:
            raise ModelCatalogValidationError("metadata_json 必须是有效 JSON") from exc
        if not isinstance(metadata, dict):
            raise ModelCatalogValidationError("metadata_json 必须是 JSON 对象")
        if len(metadata_json.encode("utf-8")) > _METADATA_MAX_BYTES:
            raise ModelCatalogValidationError(
                f"metadata_json 超过 {_METADATA_MAX_BYTES} 字节上限"
            )

        return {
            "model_key": normalize_model_key(model.get("model_key")),
            "display_name": display_name,
            "capabilities": list(normalize_capabilities(model.get("capabilities"))),
            "context_window": cls._validate_context_window(model.get("context_window")),
            "metadata_json": metadata_json,
        }

    def upsert_discovered_models(
        self,
        provider_id: int,
        models: Sequence[Mapping[str, Any]],
        generation: int,
    ) -> dict[str, int]:
        """在一个 ``BEGIN IMMEDIATE`` 内写入完整同步结果。

        - 新模型插入（discovered=1、discovered_available=1）；
        - 已存在模型只更新 discovered_* 字段和 discovered_available=1；
        - 完整结果未返回的已发现行 discovered_available=0；
        - manual、所有 manual_* 和 enabled 均不受影响。

        ``models`` 是已归一化记录（``model_key`` 键，见
        ``normalize_model_record`` 的输出），来自 Provider 发现结果。
        """
        normalized_by_key: dict[str, dict[str, Any]] = {}
        for model in models:
            record = self._normalize_discovered_record(model)
            normalized_by_key[record["model_key"]] = record
        normalized = list(normalized_by_key.values())
        inserted = 0
        updated = 0
        with self.transaction() as conn:
            seen_keys: list[str] = []
            for record in normalized:
                model_key = record["model_key"]
                seen_keys.append(model_key)
                display_name = record["display_name"]
                caps_json = json.dumps(
                    record["capabilities"], ensure_ascii=False, separators=(",", ":")
                )
                context_window = record["context_window"]
                metadata_json = record["metadata_json"]
                existing = conn.execute(
                    "SELECT id FROM ai_provider_models WHERE provider_id = ? AND model_key = ?",
                    (provider_id, model_key),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO ai_provider_models (
                            provider_id, model_key, discovered, manual,
                            discovered_available, discovered_display_name,
                            discovered_capabilities_json, discovered_context_window,
                            discovered_metadata_json, last_seen_at
                        ) VALUES (?, ?, 1, 0, 1, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (
                            provider_id,
                            model_key,
                            display_name,
                            caps_json,
                            context_window,
                            metadata_json,
                        ),
                    )
                    inserted += 1
                else:
                    conn.execute(
                        """
                        UPDATE ai_provider_models
                        SET discovered = 1,
                            discovered_available = 1,
                            discovered_display_name = ?,
                            discovered_capabilities_json = ?,
                            discovered_context_window = ?,
                            discovered_metadata_json = ?,
                            last_seen_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (
                            display_name,
                            caps_json,
                            context_window,
                            metadata_json,
                            int(existing[0]),
                        ),
                    )
                    updated += 1

            # 完整结果中未返回的已发现行：标记不可用
            if seen_keys:
                placeholders = ",".join("?" for _ in seen_keys)
                conn.execute(
                    f"""
                    UPDATE ai_provider_models
                    SET discovered_available = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE provider_id = ? AND discovered = 1
                      AND model_key NOT IN ({placeholders})
                    """,
                    [provider_id, *seen_keys],
                )
            else:
                conn.execute(
                    """
                    UPDATE ai_provider_models
                    SET discovered_available = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE provider_id = ? AND discovered = 1
                    """,
                    (provider_id,),
                )
        return {"inserted": inserted, "updated": updated}

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
from collections.abc import Iterable, Mapping, Sequence
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
# 大 IN 列表的分批大小：SQLite 3.32 之前默认最多 999 个绑定参数。
_IN_CHUNK = 900


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
            counts = self._provider_model_counts(provider_id, provider_enabled)
        return {"items": items, **counts}

    def _provider_model_counts(
        self,
        provider_id: int,
        provider_enabled: bool,
    ) -> dict[str, int]:
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
            "total": int(counts["total"]),
            "discovered_available": int(counts["discovered_available"]),
            "routable": int(counts["routable"]) if provider_enabled else 0,
        }

    def count_ai_provider_models(self, provider_id: int) -> dict[str, int]:
        """目录的 total / discovered_available / routable 三个计数，不物化条目。

        与 list_ai_provider_models 返回的计数同一条 SQL；只要计数的调用方（健康横幅）
        用它，避免为数一个数把几千行连同两次 JSON 解析都读出来。
        """
        with self.read_transaction():
            return self._provider_model_counts(
                provider_id,
                self._provider_enabled(provider_id),
            )

    def get_ai_provider_model_by_key(
        self,
        provider_id: int,
        model_key: str,
    ) -> dict[str, Any] | None:
        """按 (provider_id, model_key) 点查一条目录条目，不物化整个目录。

        走 UNIQUE(provider_id, model_key) 的自动索引。model_key 列是默认的 BINARY
        排序，按字节精确匹配，与旧实现在内存里用 == 比较的结果一致（不折叠大小写）。
        """
        with self.read_transaction():
            row = self.conn.execute(
                """
                SELECT * FROM ai_provider_models
                WHERE provider_id = ? AND model_key = ?
                """,
                (int(provider_id), model_key),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_provider_model(
                row,
                provider_enabled=self._provider_enabled(int(provider_id)),
            )

    def get_ai_provider_models_by_ids(
        self,
        model_ids: Iterable[int],
    ) -> dict[int, dict[str, Any]]:
        """按 id 批量读目录条目（派生规则与 get_ai_provider_model 相同）。

        IN 列表按 900 个一批，不依赖 SQLite 3.32 起才放宽的绑定参数上限。
        """
        unique_ids = list(dict.fromkeys(int(model_id) for model_id in model_ids))
        result: dict[int, dict[str, Any]] = {}
        provider_enabled: dict[int, bool] = {}
        with self.read_transaction():
            for start in range(0, len(unique_ids), _IN_CHUNK):
                chunk = unique_ids[start:start + _IN_CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                rows = self.conn.execute(
                    f"SELECT * FROM ai_provider_models WHERE id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    owner = int(row["provider_id"])
                    if owner not in provider_enabled:
                        provider_enabled[owner] = self._provider_enabled(owner)
                    result[int(row["id"])] = self._row_to_provider_model(
                        row,
                        provider_enabled=provider_enabled[owner],
                    )
        return result

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

        normalized = {
            "model_key": normalize_model_key(model.get("model_key")),
            "display_name": display_name,
            "capabilities": list(normalize_capabilities(model.get("capabilities"))),
            "context_window": cls._validate_context_window(model.get("context_window")),
            "metadata_json": metadata_json,
        }
        rebuild_input = dict(metadata)
        rebuild_input["id"] = normalized["model_key"]
        rebuild_input["name"] = normalized["display_name"]
        if "capabilities" in metadata:
            rebuild_input["capabilities"] = normalized["capabilities"]
        if "context_window" in metadata:
            rebuild_input["context_window"] = normalized["context_window"]
        rebuilt = normalize_model_record(rebuild_input)
        if rebuilt != normalized:
            raise ModelCatalogValidationError(
                "metadata_json 必须是 normalize_model_record 的 canonical 输出"
            )
        return rebuilt

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

        没有变化的行不重写（P23）：原样重同步 5000 个模型时原来逐行 UPDATE，整段持
        写锁。先一次读出该 Provider 现有的发现字段，在内存里比对，只把新增、内容变化、
        重新出现的行交给 ``INSERT … ON CONFLICT DO UPDATE``，只把这次消失的行按 id 置
        不可用。``updated`` 只数真正改写的行；``last_seen_at`` / ``updated_at`` 因此
        记的是该条目发现状态最后一次变化的时间（整份目录最后同步时间看
        ``ai_providers.models_synced_at``）。不再用 ``NOT IN (?, ?, …)`` 拼几千个
        占位符，不依赖 SQLite 3.32 起才放宽的 999 个绑定参数上限。
        """
        # 归一化校验与序列化只依赖输入，放在 BEGIN IMMEDIATE 之前，不占写锁。
        discovered_fields: dict[str, tuple[Any, ...]] = {}
        for model in models:
            record = self._normalize_discovered_record(model)
            discovered_fields[record["model_key"]] = (
                record["display_name"],
                json.dumps(
                    record["capabilities"], ensure_ascii=False, separators=(",", ":")
                ),
                record["context_window"],
                record["metadata_json"],
            )
        with self.transaction() as conn:
            existing = {
                row["model_key"]: row
                for row in conn.execute(
                    """
                    SELECT id, model_key, discovered, discovered_available,
                           discovered_display_name, discovered_capabilities_json,
                           discovered_context_window, discovered_metadata_json
                    FROM ai_provider_models
                    WHERE provider_id = ?
                    """,
                    (provider_id,),
                ).fetchall()
            }
            writes: list[tuple[Any, ...]] = []
            inserted = 0
            for model_key, fields in discovered_fields.items():
                row = existing.get(model_key)
                if row is None:
                    inserted += 1
                elif (
                    bool(row["discovered"])
                    and bool(row["discovered_available"])
                    and (
                        row["discovered_display_name"],
                        row["discovered_capabilities_json"],
                        row["discovered_context_window"],
                        row["discovered_metadata_json"],
                    ) == fields
                ):
                    continue
                writes.append((provider_id, model_key, *fields))
            if writes:
                # 唯一一条写入语句：新行插入，冲突行（内容变化或重新出现）改写发现
                # 字段，人工字段与用户 enabled 不在 SET 里，永不覆盖。
                conn.executemany(
                    """
                    INSERT INTO ai_provider_models (
                        provider_id, model_key, discovered, manual,
                        discovered_available, discovered_display_name,
                        discovered_capabilities_json, discovered_context_window,
                        discovered_metadata_json, last_seen_at
                    ) VALUES (?, ?, 1, 0, 1, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(provider_id, model_key) DO UPDATE SET
                        discovered = 1,
                        discovered_available = 1,
                        discovered_display_name = excluded.discovered_display_name,
                        discovered_capabilities_json = excluded.discovered_capabilities_json,
                        discovered_context_window = excluded.discovered_context_window,
                        discovered_metadata_json = excluded.discovered_metadata_json,
                        last_seen_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    writes,
                )

            # 完整结果中未返回、此前仍可用的已发现行：标记不可用
            vanished = [
                (int(row["id"]),)
                for model_key, row in existing.items()
                if model_key not in discovered_fields
                and bool(row["discovered"])
                and bool(row["discovered_available"])
            ]
            if vanished:
                conn.executemany(
                    """
                    UPDATE ai_provider_models
                    SET discovered_available = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    vanished,
                )
        return {"inserted": inserted, "updated": len(writes) - inserted}

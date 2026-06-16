"""AI providers/agents/jobs storage mixin."""
import json
import sqlite3
from typing import Any


class AiCoreMixin:
    """AI 核心对象（providers、agents、jobs）存储操作 mixin."""

    def _row_to_ai_provider(self, row: sqlite3.Row, include_secret: bool = False) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        item["has_api_key"] = bool(item.get("api_key_encrypted"))
        if item.get("available_models_json"):
            try:
                item["available_models"] = json.loads(item["available_models_json"])
            except (TypeError, ValueError):
                item["available_models"] = []
        else:
            item["available_models"] = []
        if not include_secret:
            item.pop("api_key_encrypted", None)
        item.pop("available_models_json", None)
        return item

    def list_ai_providers(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM ai_providers ORDER BY id DESC").fetchall()
        return [self._row_to_ai_provider(row) for row in rows]

    def get_ai_provider(self, provider_id: int, include_secret: bool = False) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ai_providers WHERE id = ?", (provider_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_ai_provider(row, include_secret=include_secret)

    def create_ai_provider(self, data: dict[str, Any]) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO ai_providers (
                    name, provider_type, base_url, api_key_encrypted, default_model,
                    available_models_json, timeout_seconds, max_retries, proxy, context_window, stream_enabled, enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("name"),
                    data.get("provider_type"),
                    data.get("base_url"),
                    data.get("api_key_encrypted"),
                    data.get("default_model"),
                    json.dumps(data.get("available_models") or [], ensure_ascii=False),
                    int(data.get("timeout_seconds") or 120),
                    int(data.get("max_retries") or 2),
                    data.get("proxy"),
                    int(data.get("context_window") or 128000),
                    1 if data.get("stream_enabled", True) else 0,
                    1 if data.get("enabled", True) else 0,
                ),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_ai_provider(self, provider_id: int, data: dict[str, Any]) -> None:
        allowed = {
            "name", "provider_type", "base_url", "api_key_encrypted", "default_model",
            "available_models", "timeout_seconds", "max_retries", "proxy", "context_window", "stream_enabled", "enabled",
        }
        fields: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key not in data:
                continue
            column = "available_models_json" if key == "available_models" else key
            value = json.dumps(data[key] or [], ensure_ascii=False) if key == "available_models" else data[key]
            if key in ("enabled", "stream_enabled"):
                value = 1 if value else 0
            fields.append(f"{column} = ?")
            params.append(value)
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(provider_id)
        with self._lock:
            self.conn.execute(f"UPDATE ai_providers SET {', '.join(fields)} WHERE id = ?", params)
            self._commit_if_needed()

    def delete_ai_provider(self, provider_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_providers WHERE id = ?", (provider_id,))
            self._commit_if_needed()

    def _row_to_ai_agent(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        return item

    def list_ai_agents(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT a.*, p.name AS provider_name, p.provider_type AS provider_type
            FROM ai_agents a
            LEFT JOIN ai_providers p ON p.id = a.provider_id
            ORDER BY a.id DESC
            """
        ).fetchall()
        return [self._row_to_ai_agent(row) for row in rows]

    def get_ai_agent(self, agent_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ai_agents WHERE id = ?", (agent_id,)).fetchone()
        return self._row_to_ai_agent(row) if row else None

    def create_ai_agent(self, data: dict[str, Any]) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO ai_agents (
                    name, task_type, provider_id, model, system_prompt, temperature,
                    top_p, max_tokens, context_window, enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("name"), data.get("task_type"), int(data.get("provider_id")),
                    data.get("model"), data.get("system_prompt"), float(data.get("temperature") or 0.8),
                    float(data.get("top_p") or 0.9), int(data.get("max_tokens") or 4000),
                    int(data.get("context_window") or 16000), 1 if data.get("enabled", True) else 0,
                ),
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_ai_agent(self, agent_id: int, data: dict[str, Any]) -> None:
        allowed = {"name", "task_type", "provider_id", "model", "system_prompt", "temperature", "top_p", "max_tokens", "context_window", "enabled"}
        fields: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key not in data:
                continue
            value = data[key]
            if key in {"provider_id", "max_tokens", "context_window"}:
                value = int(value)
            elif key in {"temperature", "top_p"}:
                value = float(value)
            elif key == "enabled":
                value = 1 if value else 0
            fields.append(f"{key} = ?")
            params.append(value)
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(agent_id)
        with self._lock:
            self.conn.execute(f"UPDATE ai_agents SET {', '.join(fields)} WHERE id = ?", params)
            self._commit_if_needed()

    def delete_ai_agent(self, agent_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_agents WHERE id = ?", (agent_id,))
            self._commit_if_needed()

    def create_ai_job(self, job_id: str, task_type: str, agent_id: int | None, input_data: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO ai_jobs (job_id, task_type, agent_id, status, input_json, started_at)
                VALUES (?, ?, ?, 'running', ?, CURRENT_TIMESTAMP)
                """,
                (job_id, task_type, agent_id, json.dumps(input_data, ensure_ascii=False)),
            )
            self._commit_if_needed()

    def update_ai_job(self, job_id: str, status: str, output_text: str | None = None,
                      output_json: dict[str, Any] | None = None, error_message: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                """
                UPDATE ai_jobs
                SET status = ?, output_text = COALESCE(?, output_text),
                    output_json = COALESCE(?, output_json), error_message = ?,
                    finished_at = CASE WHEN ? IN ('succeeded', 'failed', 'cancelled') THEN CURRENT_TIMESTAMP ELSE finished_at END
                WHERE job_id = ?
                """,
                (
                    status,
                    output_text,
                    json.dumps(output_json, ensure_ascii=False) if output_json is not None else None,
                    error_message,
                    status,
                    job_id,
                ),
            )
            self._commit_if_needed()

    def get_ai_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ai_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        for key in ("input_json", "output_json"):
            if item.get(key):
                try:
                    item[key[:-5]] = json.loads(item[key])
                except (TypeError, ValueError):
                    item[key[:-5]] = None
        return item

    def list_ai_jobs(self, task_type: str | None = None, status: str | None = None,
                     page: int = 1, page_size: int = 20) -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(page_size, 1)
        conditions: list[str] = []
        params: list[Any] = []
        if task_type:
            conditions.append("task_type = ?")
            params.append(task_type)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        total = int(self.conn.execute(f"SELECT COUNT(*) FROM ai_jobs {where}", params).fetchone()[0])
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            f"SELECT * FROM ai_jobs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key in ("input_json", "output_json"):
                if item.get(key):
                    try:
                        item[key[:-5]] = json.loads(item[key])
                    except (TypeError, ValueError):
                        item[key[:-5]] = None
            items.append(item)
        return {"items": items, "page": page, "page_size": page_size, "total": total, "total_pages": total_pages}

    def delete_ai_job(self, job_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM ai_jobs WHERE job_id = ?", (job_id,))
            self._commit_if_needed()

    def cleanup_ai_jobs(self, keep_days: int = 30, keep_failed_days: int | None = None) -> int:
        """清理 ai_jobs：默认保留最近 30 天的已完成任务，失败任务可单独配置保留天数。

        返回删除的行数。
        """
        if keep_failed_days is None:
            keep_failed_days = keep_days
        with self._lock:
            cur = self.conn.execute(
                """
                DELETE FROM ai_jobs
                WHERE (status IN ('succeeded', 'done', 'completed', 'success')
                       AND created_at < datetime('now', ? || ' days'))
                   OR (status IN ('failed', 'error', 'cancelled')
                       AND created_at < datetime('now', ? || ' days'))
                """,
                (f"-{int(keep_days)}", f"-{int(keep_failed_days)}"),
            )
            deleted = cur.rowcount or 0
            self._commit_if_needed()
        return int(deleted)

    def fail_stale_ai_jobs(self, older_than_minutes: int = 30) -> int:
        """把卡在 'running' 且早于阈值的 AI job 标记为 failed。

        客户端断连/进程重启会让 SSE 任务永远停留在 'running'，UI 一直转圈且
        cleanup_ai_jobs 也不会回收。建议在服务启动时调用一次做对账。返回修复行数。
        """
        with self._lock:
            cur = self.conn.execute(
                """
                UPDATE ai_jobs
                SET status = 'failed',
                    error_message = COALESCE(NULLIF(error_message, ''), '任务中断（服务重启时检测到未完成）'),
                    finished_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
                  AND created_at < datetime('now', ? || ' minutes')
                """,
                (f"-{int(older_than_minutes)}",),
            )
            fixed = cur.rowcount or 0
            self._commit_if_needed()
        return int(fixed)

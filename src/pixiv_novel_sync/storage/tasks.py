"""Task logs CRUD operations."""
from __future__ import annotations

import json
from typing import Any


class TasksMixin:
    """任务日志管理 mixin。

    提供任务执行日志的 CRUD 操作。
    """

    def create_task_log(self, task_type: str, task_name: str, job_id: str | None = None, is_auto_sync: bool = False) -> int:
        """创建任务日志记录"""
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO task_logs (task_type, task_name, job_id, status, started_at, is_auto_sync)
                VALUES (?, ?, ?, 'running', datetime('now'), ?)
                """,
                (task_type, task_name, job_id, 1 if is_auto_sync else 0)
            )
            self._commit_if_needed()
            return int(cursor.lastrowid)

    def update_task_log(self, log_id: int, status: str, stats: dict[str, Any] | None = None,
                       error_message: str | None = None, logs: list[dict[str, Any]] | None = None) -> None:
        """更新任务日志"""
        with self._lock:
            self.conn.execute(
                """
                UPDATE task_logs
                SET status = ?,
                    finished_at = datetime('now'),
                    duration_seconds = (julianday(datetime('now')) - julianday(started_at)) * 86400,
                    stats_json = ?,
                    error_message = ?,
                    logs_json = ?
                WHERE id = ?
                """,
                (status, json.dumps(stats) if stats else None, error_message, json.dumps(logs) if logs else None, log_id)
            )
            self._commit_if_needed()

    def get_task_logs(self, page: int = 1, page_size: int = 20,
                     task_type: str | None = None, is_auto_sync: bool | None = None,
                     days: int = 3) -> dict[str, Any]:
        """获取任务日志列表"""
        offset = (page - 1) * page_size

        conditions = ["started_at >= datetime('now', ? || ' days')"]
        params: list[Any] = [f"-{days}"]

        if task_type:
            conditions.append("task_type = ?")
            params.append(task_type)

        if is_auto_sync is not None:
            conditions.append("is_auto_sync = ?")
            params.append(1 if is_auto_sync else 0)

        where_clause = " AND ".join(conditions)

        # 获取总数
        total = self.conn.execute(
            f"SELECT COUNT(*) FROM task_logs WHERE {where_clause}", params
        ).fetchone()[0]

        # 获取数据
        rows = self.conn.execute(
            f"""
            SELECT * FROM task_logs
            WHERE {where_clause}
            ORDER BY started_at DESC
            LIMIT ? OFFSET ?
            """,
            params + [page_size, offset]
        ).fetchall()

        total_pages = (total + page_size - 1) // page_size

        result = []
        for row in rows:
            item = dict(row)
            if item.get("stats_json"):
                try:
                    item["stats"] = json.loads(item["stats_json"])
                except (TypeError, ValueError):
                    item["stats"] = None
            if item.get("logs_json"):
                try:
                    item["logs"] = json.loads(item["logs_json"])
                except (TypeError, ValueError):
                    item["logs"] = None
            item["is_auto_sync"] = bool(item["is_auto_sync"])
            result.append(item)

        return {
            "items": result,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }

    def cleanup_old_task_logs(self, days: int = 3) -> int:
        """清理旧的任务日志"""
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM task_logs WHERE started_at < datetime('now', ? || ' days')",
                (f"-{days}",)
            )
            self._commit_if_needed()
            return cursor.rowcount

    def get_task_log_by_id(self, log_id: int) -> dict[str, Any] | None:
        """获取单条任务日志详情"""
        row = self.conn.execute(
            "SELECT * FROM task_logs WHERE id = ?", (log_id,)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        if item.get("stats_json"):
            try:
                item["stats"] = json.loads(item["stats_json"])
            except (TypeError, ValueError):
                item["stats"] = None
        if item.get("logs_json"):
            try:
                item["logs"] = json.loads(item["logs_json"])
            except (TypeError, ValueError):
                item["logs"] = None
        item["is_auto_sync"] = bool(item.get("is_auto_sync"))
        return item

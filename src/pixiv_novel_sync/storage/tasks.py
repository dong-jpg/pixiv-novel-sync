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

    # AI 创作任务的 task_type → 中文名映射（与前端 dashboard_ai.html 的 JOB_TYPE_LABELS 对齐）
    _AI_TASK_LABELS = {
        "chapter_continue": "自动生成章节",
        "chapter_pipeline": "自动写作 Pipeline",
        "longform_plan": "全书规划",
        "longform_plan_details": "详细梗概",
        "continue": "续写",
        "rewrite": "改写",
        "audit": "内容审计",
        "plan": "写前构思",
        "distill_style": "风格蒸馏",
        "distill_novel": "小说蒸馏",
        "summarize": "摘要提取",
        "extract_summary": "摘要提取",
        "chat": "创作向导对话",
        "update_state": "状态记忆更新",
        "state_update": "状态记忆更新",
        "resolve_foreshadow": "伏笔回收",
        "foreshadow_resolve": "伏笔回收",
        "polish": "润色",
    }

    def get_ai_task_logs(
        self,
        page: int = 1,
        page_size: int = 20,
        task_type: str | None = None,
        days: int = 3,
    ) -> dict[str, Any]:
        """把 ai_jobs 表映射成与 task_logs 相同的结构，供统一任务日志页消费。

        AI 创作任务是独立的流式系统（ai_jobs 表），这里只做只读投影，不迁移数据。
        started_at 缺失时回退到 created_at 以保证时间过滤/排序一致。
        """
        with self._lock:
            offset = (page - 1) * page_size
            conditions = ["COALESCE(started_at, created_at) >= datetime('now', ? || ' days')"]
            params: list[Any] = [f"-{days}"]
            if task_type:
                conditions.append("task_type = ?")
                params.append(task_type)
            where_clause = " AND ".join(conditions)

            total = int(
                self.conn.execute(
                    f"SELECT COUNT(*) FROM ai_jobs WHERE {where_clause}", params
                ).fetchone()[0]
            )
            rows = self.conn.execute(
                f"""
                SELECT job_id, task_type, status, started_at, finished_at,
                       error_message, created_at,
                       (julianday(finished_at) - julianday(started_at)) * 86400 AS duration_seconds
                FROM ai_jobs
                WHERE {where_clause}
                ORDER BY COALESCE(started_at, created_at) DESC
                LIMIT ? OFFSET ?
                """,
                params + [page_size, offset],
            ).fetchall()
            total_pages = (total + page_size - 1) // page_size

            items: list[dict[str, Any]] = []
            for row in rows:
                r = dict(row)
                items.append({
                    "id": None,
                    "job_id": r.get("job_id"),
                    "task_type": r.get("task_type"),
                    "task_name": self._AI_TASK_LABELS.get(r.get("task_type"), r.get("task_type")),
                    "status": r.get("status"),
                    "started_at": r.get("started_at") or r.get("created_at"),
                    "finished_at": r.get("finished_at"),
                    "duration_seconds": r.get("duration_seconds"),
                    "error_message": r.get("error_message"),
                    "is_auto_sync": False,
                    "category": "ai",
                })
            return {
                "items": items,
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
            }

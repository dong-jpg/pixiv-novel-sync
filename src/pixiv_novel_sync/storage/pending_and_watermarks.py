"""Pending deletions and sync watermarks management."""
from __future__ import annotations

import json
from typing import Any


class PendingAndWatermarksMixin:
    """待删除项和同步水位线管理 mixin。

    提供待确认删除记录和同步水位线的 CRUD 操作。
    """

    def get_watermark(self, sync_type: str, key: str = "_") -> dict | None:
        """获取水位线，返回 value_json 解析后的 dict，不存在返回 None"""
        row = self.conn.execute(
            "SELECT value_json FROM sync_watermarks WHERE sync_type = ? AND key = ?",
            (sync_type, key),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    def update_watermark(self, sync_type: str, value: dict, key: str = "_") -> None:
        """写入/更新水位线"""
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO sync_watermarks (sync_type, key, value_json, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(sync_type, key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (sync_type, key, json.dumps(value, ensure_ascii=False)),
            )
            self._commit_if_needed()

    def clear_watermark(self, sync_type: str, key: str = "_") -> None:
        """删除指定水位线"""
        with self._lock:
            self.conn.execute(
                "DELETE FROM sync_watermarks WHERE sync_type = ? AND key = ?",
                (sync_type, key),
            )
            self._commit_if_needed()

    def add_pending_deletion(self, item_type: str, item_id: int, reason: str,
                             title: str, author_name: str, cover_url: str,
                             source_type: str | None = None) -> None:
        """插入待确认删除记录（已有 pending 记录则跳过，已确认/恢复的记录会被清除后重新插入）"""
        with self._lock:
            # 清除已确认或已恢复的旧记录，允许重新检测
            self.conn.execute(
                "DELETE FROM pending_deletions WHERE item_type = ? AND item_id = ? AND status IN ('confirmed', 'restored')",
                (item_type, item_id),
            )
            self.conn.execute(
                """
                INSERT OR IGNORE INTO pending_deletions
                    (item_type, item_id, reason, title, author_name, cover_url, source_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (item_type, item_id, reason, title, author_name, cover_url, source_type),
            )
            self._commit_if_needed()

    def list_pending_deletions(self, page: int = 1, page_size: int = 20,
                               item_type: str | None = None) -> dict[str, Any]:
        """分页查询 pending 状态的待确认删除记录"""
        page = max(page, 1)
        page_size = max(page_size, 1)
        where_clauses = ["status = 'pending'"]
        params: list[Any] = []
        if item_type in ("novel", "series"):
            where_clauses.append("item_type = ?")
            params.append(item_type)
        where_sql = f"WHERE {' AND '.join(where_clauses)}"
        total = int(self.conn.execute(f"SELECT COUNT(*) FROM pending_deletions {where_sql}", params).fetchone()[0])
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            f"SELECT * FROM pending_deletions {where_sql} ORDER BY detected_at DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "page": page, "page_size": page_size,
            "total": total, "total_pages": total_pages,
        }

    def confirm_pending_deletion(self, deletion_id: int) -> dict[str, Any] | None:
        """确认删除，返回记录详情，更新状态为 confirmed"""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM pending_deletions WHERE id = ? AND status = 'pending'", (deletion_id,)
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE pending_deletions SET status = 'confirmed', confirmed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (deletion_id,),
            )
            self._commit_if_needed()
            return dict(row)

    def restore_pending_deletion(self, deletion_id: int) -> dict[str, Any] | None:
        """恢复，返回记录详情，更新状态为 restored"""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM pending_deletions WHERE id = ? AND status = 'pending'", (deletion_id,)
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE pending_deletions SET status = 'restored', restored_at = CURRENT_TIMESTAMP WHERE id = ?",
                (deletion_id,),
            )
            self._commit_if_needed()
            return dict(row)

    def get_pending_deletion_count(self) -> int:
        """获取 pending 状态的记录总数"""
        return int(self.conn.execute("SELECT COUNT(*) FROM pending_deletions WHERE status = 'pending'").fetchone()[0])

    def cleanup_stale_pending(self, remote_ids: set[int], item_type: str) -> int:
        """清除已重新出现在远程列表中的 pending 记录（用户重新收藏/追更了）"""
        if not remote_ids:
            return 0
        with self._lock:
            # Phase 5.4: 分批避免超过SQLite参数限制(999)
            BATCH_SIZE = 900
            remote_list = list(remote_ids)
            total_count = 0
            for i in range(0, len(remote_list), BATCH_SIZE):
                batch = remote_list[i:i + BATCH_SIZE]
                placeholders = ",".join("?" * len(batch))
                result = self.conn.execute(
                    f"UPDATE pending_deletions SET status = 'restored', restored_at = CURRENT_TIMESTAMP "
                    f"WHERE item_type = ? AND status = 'pending' AND item_id IN ({placeholders})",
                    (item_type, *batch),
                )
                total_count += result.rowcount
            if total_count:
                self._commit_if_needed()
            return total_count

    def cleanup_old_pending_deletions(self, grace_period_days: int = 30, cleanup_confirmed_days: int = 7) -> dict[str, int]:
        """Phase 3.2: 清理过期的pending_deletions记录

        Args:
            grace_period_days: pending状态保留天数,超过此时间自动确认删除
            cleanup_confirmed_days: 已确认/已恢复记录保留天数,超过后清理

        Returns:
            {"auto_confirmed": 自动确认数, "cleaned_up": 清理数}
        """
        with self._lock:
            # 自动确认超过grace period的pending记录
            auto_confirmed = self.conn.execute(
                """
                UPDATE pending_deletions
                SET status = 'confirmed', confirmed_at = CURRENT_TIMESTAMP
                WHERE status = 'pending'
                AND datetime(detected_at) < datetime('now', '-' || ? || ' days')
                """,
                (grace_period_days,)
            ).rowcount

            # 清理过期的已确认/已恢复记录
            cleaned_up = self.conn.execute(
                """
                DELETE FROM pending_deletions
                WHERE status IN ('confirmed', 'restored')
                AND (
                    (status = 'confirmed' AND datetime(confirmed_at) < datetime('now', '-' || ? || ' days'))
                    OR (status = 'restored' AND datetime(restored_at) < datetime('now', '-' || ? || ' days'))
                )
                """,
                (cleanup_confirmed_days, cleanup_confirmed_days)
            ).rowcount

            self.conn.commit()
            return {"auto_confirmed": auto_confirmed, "cleaned_up": cleaned_up}

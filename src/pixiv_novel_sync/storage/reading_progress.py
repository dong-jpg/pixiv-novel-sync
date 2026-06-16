"""Reading progress tracking."""
from __future__ import annotations

from typing import Any


class ReadingProgressMixin:
    """阅读进度管理 mixin。

    提供小说阅读进度的 CRUD 操作。
    """

    def upsert_reading_progress(self, novel_id: int, progress: int, status: str) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO reading_progress (novel_id, progress, status, last_read_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(novel_id) DO UPDATE SET
                    progress = excluded.progress,
                    status = excluded.status,
                    last_read_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (novel_id, progress, status),
            )
            self._commit_if_needed()

    def get_reading_progress(self, novel_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM reading_progress WHERE novel_id = ?", (novel_id,)
        ).fetchone()
        return dict(row) if row else None

    def delete_reading_progress(self, novel_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM reading_progress WHERE novel_id = ?", (novel_id,))
            self._commit_if_needed()

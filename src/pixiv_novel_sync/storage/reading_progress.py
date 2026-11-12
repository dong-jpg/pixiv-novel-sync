"""Reading progress tracking."""
from __future__ import annotations

import hashlib
import json
from typing import Any
import uuid


class ReadingProgressError(ValueError):
    def __init__(self, code: str, status: int = 400, record: dict | None = None):
        self.code, self.status, self.record = code, status, record
        super().__init__(code)


def validate_progress_id(novel_id: object) -> None:
    if type(novel_id) is not int or not 0 < novel_id < 2**63:
        raise ReadingProgressError('invalid_novel_id')


def validate_progress_change(progress, status, base_version, mutation_id, *, reset=False):
    if type(base_version) is not int or not 0 <= base_version < 2**63 - 1:
        raise ReadingProgressError('invalid_progress_version')
    try:
        if not isinstance(mutation_id, str) or str(uuid.UUID(mutation_id)) != mutation_id:
            raise ValueError
    except (ValueError, AttributeError):
        raise ReadingProgressError('invalid_progress_mutation') from None
    if type(reset) is not bool:
        raise ReadingProgressError('invalid_progress_reset')
    if type(progress) is not int or not 0 <= progress <= 100:
        raise ReadingProgressError('invalid_progress')
    if not isinstance(status, str) or status not in {'unread', 'reading', 'completed'}:
        raise ReadingProgressError('invalid_progress_status')
    if reset and (progress != 0 or status != 'unread'):
        raise ReadingProgressError('invalid_progress_reset')


def _utc(value):
    # 兼容旧 SQLite CURRENT_TIMESTAMP；新客户端接收明确的 UTC 时间。
    if value and isinstance(value, str) and len(value) == 19 and value[10] == ' ':
        return value.replace(' ', 'T', 1) + 'Z'
    return value


def _record(novel_id, row):
    if row is None:
        return {'novel_id': novel_id, 'progress': 0, 'status': 'unread', 'version': 0,
                'is_reset': False, 'last_read_at': None, 'created_at': None, 'updated_at': None}
    return {'novel_id': novel_id, 'progress': row['progress'], 'status': row['status'],
            'version': row['version'], 'is_reset': bool(row['is_reset']),
            **{name: _utc(row[name]) for name in ('last_read_at', 'created_at', 'updated_at')}}


class ReadingProgressMixin:
    """阅读进度管理 mixin。

    提供小说阅读进度的 CRUD 操作。
    """

    def upsert_reading_progress(self, novel_id: int, progress: int, status: str) -> None:
        """兼容内部写入；同样提高版本，使旧客户端的 CAS 失效。"""
        self.conn.execute(
            """
            INSERT INTO reading_progress (novel_id, progress, status, last_read_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(novel_id) DO UPDATE SET
                progress = excluded.progress,
                status = excluded.status,
                version = reading_progress.version + 1,
                is_reset = 0,
                last_read_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            """,
            (novel_id, progress, status),
        )
        self._commit_if_needed()

    def progress_snapshot(self, novel_id: int) -> dict:
        validate_progress_id(novel_id)
        with self.read_transaction() as conn:
            if conn.execute('SELECT 1 FROM novels WHERE novel_id=?', (novel_id,)).fetchone() is None:
                raise ReadingProgressError('novel_not_found', 404)
            row = conn.execute('SELECT * FROM reading_progress WHERE novel_id=?', (novel_id,)).fetchone()
            return _record(novel_id, row)

    def compare_and_set_reading_progress(
        self, novel_id: int, progress: int, status: str, base_version: int,
        mutation_id: str, *, reset: bool = False,
    ) -> dict:
        """版本比较、记录及有限幂等回执在同一个 SQLite 写事务内完成。"""
        validate_progress_id(novel_id)
        validate_progress_change(progress, status, base_version, mutation_id, reset=reset)
        encoded = json.dumps({'novel_id': novel_id, 'progress': progress, 'status': status,
                              'base_version': base_version, 'reset': reset}, sort_keys=True,
                             separators=(',', ':')).encode('utf-8')
        request_hash = hashlib.sha256(encoded).hexdigest()
        with self.transaction() as conn:
            if conn.execute('SELECT 1 FROM novels WHERE novel_id=?', (novel_id,)).fetchone() is None:
                raise ReadingProgressError('novel_not_found', 404)
            row = conn.execute('SELECT * FROM reading_progress WHERE novel_id=?', (novel_id,)).fetchone()
            current = _record(novel_id, row)
            receipt = conn.execute('''SELECT request_hash,applied_version FROM reading_progress_mutations
                WHERE novel_id=? AND mutation_id=?''', (novel_id, mutation_id)).fetchone()
            if receipt:
                if receipt['request_hash'] != request_hash:
                    raise ReadingProgressError('progress_idempotency_conflict', 409, current)
                # 响应丢失后另一个设备可能已经推进；原回执不能伪装成最新写入。
                return {'record': current, 'applied_version': receipt['applied_version'], 'replayed': True}
            if current['version'] != base_version:
                raise ReadingProgressError('progress_conflict', 409, current)
            version = base_version + 1
            conn.execute('''INSERT INTO reading_progress
                (novel_id,progress,status,version,is_reset,last_read_at,created_at,updated_at)
                VALUES(?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                       strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(novel_id) DO UPDATE SET progress=excluded.progress,status=excluded.status,
                    version=excluded.version,is_reset=excluded.is_reset,last_read_at=excluded.last_read_at,
                    updated_at=excluded.updated_at''', (novel_id, progress, status, version, int(reset)))
            conn.execute('''INSERT INTO reading_progress_mutations
                (novel_id,mutation_id,request_hash,applied_version) VALUES(?,?,?,?)''',
                         (novel_id, mutation_id, request_hash, version))
            # 离线头请求始终保留原版本；回执即使被淘汰也不能通过旧版本再次写入。
            conn.execute('''DELETE FROM reading_progress_mutations WHERE novel_id=? AND mutation_id NOT IN
                (SELECT mutation_id FROM reading_progress_mutations WHERE novel_id=?
                 ORDER BY applied_version DESC LIMIT 32)''', (novel_id, novel_id))
            current = _record(novel_id, conn.execute('SELECT * FROM reading_progress WHERE novel_id=?', (novel_id,)).fetchone())
            return {'record': current, 'applied_version': version, 'replayed': False}

    def get_reading_progress(self, novel_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM reading_progress WHERE novel_id = ?", (novel_id,)
        ).fetchone()
        return dict(row) if row else None

    def delete_reading_progress(self, novel_id: int) -> None:
        """仅用于内部物理清理；阅读页重置使用带版本的 tombstone。"""
        self.conn.execute("DELETE FROM reading_progress WHERE novel_id = ?", (novel_id,))
        self._commit_if_needed()

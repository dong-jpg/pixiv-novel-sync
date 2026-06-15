"""Storage utility classes and functions."""
from __future__ import annotations

import sqlite3


class _LazyNovelMembership:
    """惰性成员判断:`novel_id in obj` 走主键索引的 EXISTS 单点查询,

    避免把整张 novels 表(可能上万行)灌进内存 set。调用方仍用 `x in obj` 语义,
    零改动。结果按 novel_id 短期缓存,同一次推荐运行内重复判断不重复打库。
    """

    def __init__(self, conn: sqlite3.Connection, sql: str) -> None:
        self._conn = conn
        self._sql = sql
        self._cache: dict[int, bool] = {}

    def __contains__(self, novel_id: object) -> bool:
        try:
            key = int(novel_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        row = self._conn.execute(self._sql, (key,)).fetchone()
        result = row is not None
        self._cache[key] = result
        return result

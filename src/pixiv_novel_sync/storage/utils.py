"""Storage utility classes and functions."""
from __future__ import annotations

import sqlite3


def escape_fts_query(search: str) -> str:
    """把用户搜索词转成安全的 FTS5 MATCH 表达式。

    FTS5 的 MATCH 语法把 `"`、`*`、`(`、`)`、`AND`/`OR`/`NEAR` 等当作操作符，
    用户随手输入一个引号或以 `*` 开头就会让 SQLite 抛
    `sqlite3.OperationalError: fts5: syntax error`，冒泡成 HTTP 500。

    这里把输入按空白拆成词，每个词包成带引号的短语（内部 `"` 转义为 `""`），
    这样所有字符都被当字面量匹配，多个词之间是隐式 AND。空白/空输入返回
    空串，调用方据此跳过 MATCH 过滤。
    """
    if not search:
        return ""
    tokens = search.split()
    if not tokens:
        return ""
    return " ".join('"' + token.replace('"', '""') + '"' for token in tokens)


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

"""Storage utility classes and functions."""
from __future__ import annotations

import sqlite3

# Pixiv 原站链接。novel/series 的地址由 ID 唯一确定，所以这是「拼接」而非「外部输入」，
# 不存在校验外部 URL 的问题。前缀集中在这里，让写库、迁移回填、读 API、油猴脚本、
# 前端推荐卡片都用同一份格式——此前 show.php / novel/series 的字面量散落在 4 个文件里各拼各的。
PIXIV_NOVEL_URL_PREFIX = "https://www.pixiv.net/novel/show.php?id="
PIXIV_SERIES_URL_PREFIX = "https://www.pixiv.net/novel/series/"


def novel_source_url(novel_id: int | None) -> str | None:
    """由 novel_id 拼出 Pixiv 原站单篇小说地址；无 ID 时返回 None。"""
    try:
        value = int(novel_id)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return f"{PIXIV_NOVEL_URL_PREFIX}{value}"


def series_source_url(series_id: int | None) -> str | None:
    """由 series_id 拼出 Pixiv 原站系列地址；无 ID 时返回 None。"""
    try:
        value = int(series_id)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return f"{PIXIV_SERIES_URL_PREFIX}{value}"


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

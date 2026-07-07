"""FTS5 搜索词转义回归测试。

用户搜索含 FTS5 语法字符（引号 / * / AND / 未闭合括号等）时，未转义会让
SQLite 抛 OperationalError，进而使 /api/dashboard/novels 等路由 500。
escape_fts_query 把输入按空白拆词、每词包成字面短语，杜绝语法注入与崩溃。
"""
from __future__ import annotations

import sqlite3

import pytest

from pixiv_novel_sync.storage.utils import escape_fts_query


def test_escape_empty_and_blank_returns_empty():
    assert escape_fts_query("") == ""
    assert escape_fts_query("   ") == ""
    assert escape_fts_query(None) == ""  # type: ignore[arg-type]


def test_escape_wraps_each_token_as_phrase():
    assert escape_fts_query("hello world") == '"hello" "world"'
    assert escape_fts_query("单词") == '"单词"'


def test_escape_neutralizes_fts_operators():
    # 这些原本是 FTS 语法，转义后必须成为字面短语
    assert escape_fts_query("AND") == '"AND"'
    assert escape_fts_query("foo AND bar") == '"foo" "AND" "bar"'
    assert escape_fts_query("*") == '"*"'


def test_escape_doubles_inner_quotes():
    # FTS 短语内的双引号需转义为 ""
    assert escape_fts_query('"') == '""""'
    assert escape_fts_query('a"b') == '"a""b"'


@pytest.mark.parametrize("bad", ['"', "*", "AND", "(unclosed", "a OR", "NEAR/", '中文"注入'])
def test_escaped_query_never_raises_operational_error(bad):
    """核心回归：任何畸形输入经转义后送入真实 FTS5 表都不再抛异常。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE novel_fts USING fts5(title)")
    conn.execute("INSERT INTO novel_fts (title) VALUES ('中文 hello world')")

    esc = escape_fts_query(bad)
    # 不抛 OperationalError 即为通过；结果行数不作断言
    conn.execute("SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?", (esc,)).fetchall()
    conn.close()


def test_raw_query_would_crash_but_escaped_survives():
    """对照组：证明转义确有必要——裸引号会让 SQLite 抛错。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE novel_fts USING fts5(title)")
    conn.execute("INSERT INTO novel_fts (title) VALUES ('hello')")

    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?", ('"',)).fetchall()

    # 转义后同样输入不再崩溃
    conn.execute(
        "SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?",
        (escape_fts_query('"'),),
    ).fetchall()
    conn.close()

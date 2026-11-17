"""Pixiv 小说正文标记解析（EPUB 导出用）。

阅读页的 JS 版在 ``templates/pixiv_markup.html``，两边按同一套规则产出同一棵树，
``tests/fixtures/pixiv_markup_cases.json`` 同时喂给两边的测试，规则改动要两边一起改。

规则按 Pixiv 帮助中心「小説作品の本文内に使える特殊タグ」，加上
``userscripts/pixiv-rescue.user.js`` 在生产正文里见过的 ``[b:]`` / ``[i:]`` /
``[[emphasismark:]]``：

- ``[newpage]`` 分页；``[chapter:标题]``、``[pixivimage:ID]``、``[uploadedimage:ID]``
  是块级标记，所在行在标记处断开；
- 行内 ``[[rb:正文 > 注音]]``、``[[jumpuri:文字 > URL]]``、``[jump:N]``、``[b:…]``、
  ``[i:…]``、``[[emphasismark:文字 > 点]]``，可嵌套（最多 8 层）；
- 配不上的方括号一律当普通文字；jumpuri 只认 http(s)，其余 URL 只留文字。

产出的是纯数据（list / dict / str），不含任何 HTML；转义由渲染端负责。

    pages = [[block, ...], ...]
    block = {"type": "paragraph", "indent": str, "content": [inline, ...]}
          | {"type": "blank"}
          | {"type": "chapter", "content": [inline, ...]}
          | {"type": "image", "kind": "pixivimage" | "uploadedimage", "id": str}
    inline = str
           | {"type": "ruby", "base": str, "rt": str}
           | {"type": "link", "href": str | None, "content": [inline, ...]}
           | {"type": "jump", "page": int}
           | {"type": "bold" | "italic", "content": [inline, ...]}
           | {"type": "emphasis", "style": "dot" | "sesame", "content": [inline, ...]}

所有扫描都是线性的：方括号一次配对，正则只在固定位置匹配，没有会回溯的
``\\s+$`` 一类写法——正文是作者可控的输入。
"""
from __future__ import annotations

import re
from typing import Any

# 连续空行最多保留几行：上万个空行不能渲染成上万个空段落
MAX_BLANK_LINES = 3
# 行内标记最多嵌套几层，再深的按普通文字处理
MAX_INLINE_DEPTH = 8

# 与 JS String.prototype.trim() 的空白集合逐字一致（行已按 \n 切开，\r 已归一），
# 两边对同一行的缩进 / 空行判断才不会分叉
WHITESPACE = (
    "\t\x0b\x0c \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)

_BRACKET_RE = re.compile(r"[\[\]]")
_PAIR_SPLIT_RE = re.compile(r"[\[\]>]")
_BLOCK_RE = re.compile(r"\[(chapter|pixivimage|uploadedimage):")
_JUMP_RE = re.compile(r"\[jump:([0-9]{1,9})\]")
_DOUBLE_RE = re.compile(r"\[\[(rb|jumpuri|emphasismark):")
_SINGLE_RE = re.compile(r"\[(b|i):")
# 字符类两边逐字相同（JS 版同一串）；re.ASCII 防止 IGNORECASE 把「ſ」折成 s
_SAFE_URL_RE = re.compile(
    "https?://[^\\x00-\\x20\\x7f-\\x9f\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029"
    "\\u202f\\u205f\\u3000\\ufeff\"'<>`\\\\]+",
    re.IGNORECASE | re.ASCII,
)
_SESAME_MARKS = ("\ufe45", "\u3001")  # 芝麻点：﹅ 与 、


def parse(text: str | None) -> list[list[dict[str, Any]]]:
    """把 ``text_raw`` 解析成按页分组的块列表；空正文返回一页空内容。"""
    return [_parse_page(page) for page in _split_pages(text or "")]


def plain_text(nodes: list[Any]) -> str:
    """行内节点的纯文字（目录标题用）：注音只取正文，跳页标记丢掉。"""
    parts: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            parts.append(node)
        elif node["type"] == "ruby":
            parts.append(node["base"])
        elif node["type"] != "jump":
            parts.append(plain_text(node["content"]))
    return "".join(parts)


def is_safe_url(url: str | None) -> bool:
    return bool(url) and _SAFE_URL_RE.fullmatch(url) is not None


def pixiv_artwork_id(image_id: str) -> str | None:
    """``[pixivimage:123-2]`` 的作品 ID；不是 ASCII 数字就返回 None。"""
    head = str(image_id or "").split("-", 1)[0]
    return head if head.isascii() and head.isdigit() else None


def _split_pages(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    pages = [page.strip("\n") for page in normalized.split("[newpage]")]
    # 只丢末尾的空页：中间的空页要留着，否则 [jump:N] 的页码会错位
    while len(pages) > 1 and not pages[-1].strip(WHITESPACE + "\n"):
        pages.pop()
    return pages


def _parse_page(page: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    blank_run = 0
    for line in page.split("\n"):
        if not line.strip(WHITESPACE):
            blank_run += 1
            if blank_run <= MAX_BLANK_LINES:
                blocks.append({"type": "blank"})
            continue
        blank_run = 0
        _parse_line(line, blocks)
    # 页首页尾的空行没有意义（页与页之间本来就有分隔），连续空行已封顶，这里最多各弹 3 次
    while blocks and blocks[0]["type"] == "blank":
        blocks.pop(0)
    while blocks and blocks[-1]["type"] == "blank":
        blocks.pop()
    return blocks


def _parse_line(line: str, blocks: list[dict[str, Any]]) -> None:
    pairs = _pair_brackets(line)
    last = 0
    for match in _BLOCK_RE.finditer(line):
        start = match.start()
        close = pairs.get(start)
        if start < last or close is None:
            continue
        kind = match.group(1)
        # 插图 ID 里不许再有方括号；只需找「[」：没有「[」时第一个「]」必然就是 close
        if kind != "chapter" and line.find("[", match.end(), close) != -1:
            continue
        _push_paragraph(blocks, line[last:start], first=last == 0)
        body = line[match.end():close].strip(WHITESPACE)
        if kind == "chapter":
            blocks.append({"type": "chapter", "content": parse_inline(body)})
        else:
            blocks.append({"type": "image", "kind": kind, "id": body})
        last = close + 1
    _push_paragraph(blocks, line[last:], first=last == 0)


def _push_paragraph(blocks: list[dict[str, Any]], segment: str, first: bool) -> None:
    """行首缩进（多为全角空格）原样记在 indent 里，只有一行的第一段才有。"""
    body = segment.strip(WHITESPACE)
    if not body:
        return
    indent = segment[: len(segment) - len(segment.lstrip(WHITESPACE))] if first else ""
    blocks.append({"type": "paragraph", "indent": indent, "content": parse_inline(body)})


def _pair_brackets(text: str) -> dict[int, int]:
    """一次扫描配好所有方括号：``{开括号下标: 闭括号下标}``，配不上的不在表里。"""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for match in _BRACKET_RE.finditer(text):
        index = match.start()
        if text[index] == "[":
            stack.append(index)
        elif stack:
            pairs[stack.pop()] = index
    return pairs


def parse_inline(text: str, depth: int = 0) -> list[Any]:
    if not text:
        return []
    if depth >= MAX_INLINE_DEPTH:
        return [text]
    pairs = _pair_brackets(text)
    nodes: list[Any] = []
    buffer: list[str] = []
    i = 0
    while i < len(text):
        open_at = text.find("[", i)
        if open_at < 0:
            buffer.append(text[i:])
            break
        buffer.append(text[i:open_at])
        token = _match_token(text, open_at, pairs, depth)
        if token is None:
            buffer.append("[")
            i = open_at + 1
            continue
        _flush(nodes, buffer)
        nodes.append(token[0])
        i = token[1]
    _flush(nodes, buffer)
    return nodes


def _flush(nodes: list[Any], buffer: list[str]) -> None:
    joined = "".join(buffer)
    buffer.clear()
    if joined:
        nodes.append(joined)


def _match_token(text: str, start: int, pairs: dict[int, int], depth: int) -> tuple[Any, int] | None:
    jump = _JUMP_RE.match(text, start)
    if jump:
        return {"type": "jump", "page": int(jump.group(1))}, jump.end()
    double = _DOUBLE_RE.match(text, start)
    if double:
        close = pairs.get(start)
        # [[kind:BODY]]：外层 [ 配最后一个 ]，内层 [ 配倒数第二个 ]
        if close is None or pairs.get(start + 1) != close - 1:
            return None
        return _double_node(double.group(1), text[double.end():close - 1], depth), close + 1
    single = _SINGLE_RE.match(text, start)
    if single:
        close = pairs.get(start)
        if close is None:
            return None
        node_type = "bold" if single.group(1) == "b" else "italic"
        return {"type": node_type, "content": parse_inline(text[single.end():close], depth + 1)}, close + 1
    return None


def _double_node(kind: str, body: str, depth: int) -> dict[str, Any]:
    if kind == "rb":
        base, rt = _split_pair(body, use_last=False)
        return {"type": "ruby", "base": base, "rt": rt}
    if kind == "jumpuri":
        label, url = _split_pair(body, use_last=True)
        return {
            "type": "link",
            "href": url if is_safe_url(url) else None,
            "content": parse_inline(label, depth + 1),
        }
    label, mark = _split_pair(body, use_last=False)
    style = "sesame" if any(ch in mark for ch in _SESAME_MARKS) else "dot"
    return {"type": "emphasis", "style": style, "content": parse_inline(label, depth + 1)}


def _split_pair(body: str, use_last: bool) -> tuple[str, str]:
    """按不在嵌套方括号里的 ``>`` 拆成两半；左半去尾部空格，右半两头去空白。

    body 取自一对配好的方括号之间，内部方括号必然配平，层级不会出负数。
    """
    at = -1
    level = 0
    for match in _PAIR_SPLIT_RE.finditer(body):
        ch = body[match.start()]
        if ch == "[":
            level += 1
        elif ch == "]":
            level -= 1
        elif level == 0:
            at = match.start()
            if not use_last:
                break
    if at < 0:
        return body, ""
    return body[:at].rstrip(" \t"), body[at + 1:].strip(WHITESPACE)

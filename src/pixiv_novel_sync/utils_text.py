from __future__ import annotations

import html
import re

TAG_RE = re.compile(r"<[^>]+>")
MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def clean_caption(value: str | None) -> str:
    if not value:
        return ""
    text = html.unescape(value)
    text = TAG_RE.sub("", text)
    return MULTI_NEWLINE_RE.sub("\n\n", text).strip()


def normalize_text(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    return MULTI_NEWLINE_RE.sub("\n\n", text).strip() + "\n"


def to_markdown(title: str, author_name: str, caption: str, body: str) -> str:
    parts = [f"# {title}", "", f"作者：{author_name}"]
    if caption:
        parts.extend(["", "## 简介", "", caption])
    parts.extend(["", "## 正文", "", body.strip(), ""])
    return "\n".join(parts)

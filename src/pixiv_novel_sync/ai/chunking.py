from __future__ import annotations


def estimate_token_count(text: str) -> int:
    """简单估算 token 数。中文约 1.5 字/token，英文约 4 字符/token。"""
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)


def needs_summarization(text: str, context_window: int) -> bool:
    """判断文本是否需要摘要处理。"""
    return estimate_token_count(text) > context_window * 0.6


def get_tail_context(text: str, context_chars: int) -> str:
    text = text or ""
    context_chars = max(int(context_chars or 0), 0)
    if not context_chars or len(text) <= context_chars:
        return text
    return text[-context_chars:]


def split_text_by_chars(text: str, max_chars: int) -> list[str]:
    text = text or ""
    max_chars = max(int(max_chars or 0), 1)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in text.splitlines(keepends=True):
        if current and current_len + len(paragraph) > max_chars:
            chunks.append("".join(current).strip())
            current = []
            current_len = 0
        if len(paragraph) > max_chars:
            for index in range(0, len(paragraph), max_chars):
                part = paragraph[index:index + max_chars]
                if part:
                    chunks.append(part.strip())
            continue
        current.append(paragraph)
        current_len += len(paragraph)
    if current:
        chunks.append("".join(current).strip())
    return [chunk for chunk in chunks if chunk]

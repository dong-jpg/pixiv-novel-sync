from __future__ import annotations

import re
from pathlib import Path


INVALID_CHARS = re.compile(r"[\\/:*?\"<>|]+")
WHITESPACE = re.compile(r"\s+")


def safe_name(value: str, fallback: str = "untitled") -> str:
    cleaned = INVALID_CHARS.sub("_", value).strip()
    cleaned = WHITESPACE.sub(" ", cleaned)
    return cleaned[:120] or fallback


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

from __future__ import annotations

from pathlib import Path


def test_sync_engine_does_not_swallow_interrupted_error() -> None:
    """每个宽泛的 except Exception 前面都要先把取消异常抛出去。"""
    source = Path("src/pixiv_novel_sync/sync_engine.py").read_text(encoding="utf-8")
    lines = source.splitlines()
    missing: list[int] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("except Exception"):
            continue
        previous = ""
        for earlier in reversed(lines[:index]):
            if earlier.strip():
                previous = earlier.strip()
                break
        if previous != "raise":
            missing.append(index + 1)
    assert missing == []

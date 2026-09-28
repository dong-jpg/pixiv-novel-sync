from __future__ import annotations

from pathlib import Path


def test_sync_engine_does_not_swallow_interrupted_error() -> None:
    """宽泛的 except Exception 之前，同一 try 必须先显式重抛 InterruptedError。

    取消传播协议要求任务把 InterruptedError 一路抛给 JobRunner 转成 CANCELLED。
    检查口径是「同一 try 语句的 handler 组里，except Exception 之前存在
    `except InterruptedError:` 且其体是 `raise`」——中间允许夹其他具体异常
    的 handler（如 RemoteListTruncated），它们不影响取消的传播。
    """
    source = Path("src/pixiv_novel_sync/sync_engine.py").read_text(encoding="utf-8")
    lines = source.splitlines()
    missing: list[int] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("except Exception"):
            continue
        indent = len(line) - len(line.lstrip())
        # 向前扫同一缩进层的 except 子句，直到 try:（或更浅缩进）为止
        rethrown = False
        for earlier_index in range(index - 1, -1, -1):
            earlier = lines[earlier_index]
            if not earlier.strip():
                continue
            earlier_indent = len(earlier) - len(earlier.lstrip())
            if earlier_indent < indent:
                break
            if earlier_indent == indent and earlier.strip() == "try:":
                break
            if earlier_indent == indent and earlier.strip().startswith(
                "except InterruptedError"
            ):
                # 下一条有效行（跳过空行和注释）必须是 raise
                for body_index in range(earlier_index + 1, index):
                    body = lines[body_index].strip()
                    if not body or body.startswith("#"):
                        continue
                    rethrown = body == "raise"
                    break
                break
        if not rethrown:
            missing.append(index + 1)
    assert missing == []

from __future__ import annotations

from pathlib import Path

import pytest

from pixiv_novel_sync.storage_files import FileStorage


@pytest.mark.parametrize("existing", [False, True], ids=["new-archive", "unchanged-archive"])
def test_local_cancellation_before_commit_prevents_database_publication(tmp_path, monkeypatch, existing):
    from types import SimpleNamespace

    from pixiv_novel_sync.settings import load_settings
    from pixiv_novel_sync.storage_db import Database, prepare_schema
    from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService

    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    settings = load_settings(tmp_path / "missing.yaml", env)
    metadata = {"id": 42, "title": "local fixture", "user": {"id": 7, "name": "author"}, "tags": []}
    api = SimpleNamespace(novel_detail=lambda novel_id: {"novel": metadata},
                          webview_novel=lambda novel_id: {"id": novel_id, "text": "fixture body"})
    db = prepare_schema(Database(settings.storage.db_path))
    storage = FileStorage(settings)
    cancelled = False
    service = BookmarkNovelSyncService(api, db, storage, settings, stop_requested=lambda: cancelled)
    arguments = dict(novel_id=42, novel=metadata, restrict="public", download_assets=False,
                     write_markdown=True, write_raw_text=True, source_type="bookmark_public")
    try:
        if existing:
            service._sync_novel_inner(**arguments)
        original = storage.resolve_archive_dir if existing else storage.write_text

        def cancel_after_io(*args, **kwargs):
            nonlocal cancelled
            result = original(*args, **kwargs)
            cancelled = True
            return result

        monkeypatch.setattr(storage, "resolve_archive_dir" if existing else "write_text", cancel_after_io)

        def forbidden_touch(*args):
            pytest.fail("cancelled result must not touch the existing novel")

        monkeypatch.setattr(db, "touch_novel", forbidden_touch)
        with pytest.raises(InterruptedError, match="Task stopped by user"):
            service._sync_novel_inner(**arguments)
        assert db.conn.execute("SELECT COUNT(*) FROM novels").fetchone()[0] == int(existing)
    finally:
        db.close()


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


@pytest.mark.parametrize("cancel_at", ["before_request", "during_stream", "after_stream"])
def test_asset_download_cancellation_keeps_previous_file_and_removes_partial(tmp_path, cancel_at):
    from types import SimpleNamespace

    target = tmp_path / "cover.jpg"
    target.write_bytes(b"previous cover")
    state = {"cancelled": cancel_at == "before_request", "requests": 0, "closed": False}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            state["closed"] = True

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b"first part"
            if cancel_at == "during_stream":
                state["cancelled"] = True
            yield b"second part"
            if cancel_at == "after_stream":
                state["cancelled"] = True

    def get(*args, **kwargs):
        state["requests"] += 1
        return Response()

    storage = FileStorage(SimpleNamespace())
    storage._session = SimpleNamespace(get=get)
    with pytest.raises(InterruptedError, match="Task stopped by user"):
        storage.download_asset(
            "https://i.pximg.net/cover.jpg", target,
            timeout=30, verify_ssl=True, proxy=None,
            stop_requested=lambda: state["cancelled"],
        )

    assert target.read_bytes() == b"previous cover"
    assert not target.with_suffix(".jpg.tmp").exists()
    assert state["requests"] == (0 if cancel_at == "before_request" else 1)
    assert state["closed"] == (cancel_at != "before_request")

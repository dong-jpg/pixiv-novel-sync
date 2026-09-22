from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_runtime_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("ENV_PATH", raising=False)
    monkeypatch.setenv("PIXIV_DB_PATH", str(tmp_path / "state" / "test.db"))
    monkeypatch.setenv("PIXIV_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("PIXIV_PRIVATE_DIR", str(tmp_path / "private"))
    # ensure_schema 的进程级记忆按 db_path 缓存；测试间 tmp_path 复用同一路径名
    # 时会误判 schema 已就绪而跳过建表，导致 "no such table"。每个测试前后清空。
    from pixiv_novel_sync.storage_db import Database

    Database._reset_schema_ready_cache()
    yield
    Database._reset_schema_ready_cache()

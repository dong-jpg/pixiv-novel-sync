from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_runtime_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIXIV_DB_PATH", str(tmp_path / "state" / "test.db"))
    monkeypatch.setenv("PIXIV_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("PIXIV_PRIVATE_DIR", str(tmp_path / "private"))

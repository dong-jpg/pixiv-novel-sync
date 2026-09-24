from __future__ import annotations

from pathlib import Path

from pixiv_novel_sync.storage_db import Database


def test_ensure_schema_initializes_each_file_once(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    original = Database.init_schema

    def counting(self: Database) -> None:
        calls.append(str(self.path.resolve()))
        original(self)

    monkeypatch.setattr(Database, "init_schema", counting)
    Database._reset_schema_ready_cache()
    first = tmp_path / "a.db"
    second = tmp_path / "b.db"

    Database(first).ensure_schema().close()
    Database(first).ensure_schema().close()
    Database(second).ensure_schema().close()

    assert calls == [str(first.resolve()), str(second.resolve())]


def test_memory_database_is_not_cached(monkeypatch) -> None:
    calls: list[str] = []
    original = Database.init_schema

    def counting(self: Database) -> None:
        calls.append(str(self.path))
        original(self)

    monkeypatch.setattr(Database, "init_schema", counting)
    Database._reset_schema_ready_cache()
    memory = Database.__new__(Database)
    memory.path = Path(":memory:")
    assert memory._schema_cache_key() is None
    assert calls == []

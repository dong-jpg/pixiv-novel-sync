from __future__ import annotations

from types import SimpleNamespace

import pytest

from pixiv_novel_sync import auth, cli, storage_db
from pixiv_novel_sync.jobs import quick_sync, services, tasks


@pytest.mark.parametrize("entry", ["bookmark", "following", "backup", "user_status", "novel_status", "db_stats"])
def test_schema_failure_closes_task_database(monkeypatch, tmp_path, entry):
    opened = []

    class FailingDatabase:
        def __init__(self, path):
            self.closed = False
            opened.append(self)

        def ensure_schema(self):
            raise RuntimeError("migration failed")

        def close(self):
            self.closed = True

    settings = SimpleNamespace(
        pixiv=SimpleNamespace(user_id=1),
        storage=SimpleNamespace(db_path=tmp_path / "task.db", public_dir=tmp_path / "public", private_dir=tmp_path / "private"),
        sync=SimpleNamespace(series_sync_limit=0, auto_sync_following_novels_users_limit=0),
        log_level="INFO",
    )
    monkeypatch.setattr(storage_db, "Database", FailingDatabase)
    monkeypatch.setattr(quick_sync, "Database", FailingDatabase)
    monkeypatch.setattr(services, "Database", FailingDatabase)
    monkeypatch.setattr(auth.PixivAuthManager, "login", lambda self, **kwargs: (object(), SimpleNamespace(user_id=1)))
    monkeypatch.setattr(services, "_login", lambda settings, **kwargs: object())
    monkeypatch.setattr(services, "_ensure_storage_dirs", lambda settings: object())
    monkeypatch.setattr(cli, "load_settings", lambda **kwargs: settings)
    monkeypatch.setattr("sys.argv", ["pixiv-novel-sync", "db-stats"])

    with pytest.raises(RuntimeError, match="migration failed"):
        if entry == "bookmark":
            quick_sync.run_bookmark_sync(settings)
        elif entry == "following":
            tasks.execute_task("following_users", settings)
        elif entry == "backup":
            services.run_user_backup_task(settings, 1)
        elif entry == "user_status":
            services.run_user_status_task(settings)
        elif entry == "novel_status":
            services.run_novel_status_task(settings)
        else:
            cli.main()

    assert len(opened) == 1
    assert opened[0].closed

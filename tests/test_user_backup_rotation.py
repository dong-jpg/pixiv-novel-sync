from __future__ import annotations

from types import SimpleNamespace

from pixiv_novel_sync.jobs.quick_sync import run_scheduled_user_backup
from pixiv_novel_sync.storage_db import Database


def test_failed_user_does_not_stall_backup_rotation(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "backup.db"
    db = Database(db_path)
    db.init_schema()
    for user_id in (1, 2, 3):
        db.conn.execute(
            "INSERT INTO users (user_id, name, raw_json) VALUES (?, '作者', '{}')",
            (user_id,),
        )
    db.close()

    def backup(settings, user_id, **kwargs):
        if user_id == 1:
            raise RuntimeError("上游失败")
        return {"novels": 3, "skipped": 0, "assets_downloaded": 0}

    monkeypatch.setattr(
        "pixiv_novel_sync.jobs.quick_sync.job_services.run_user_backup_task",
        backup,
    )
    settings = SimpleNamespace(
        storage=SimpleNamespace(db_path=db_path),
        sync=SimpleNamespace(auto_sync_following_novels_users_limit=2),
    )

    result = run_scheduled_user_backup(settings)

    assert result["failed_users"] == 1
    assert result["novels"] == 3
    db = Database(db_path)
    db.init_schema()
    watermark = db.get_watermark("user_backup_rotation")
    assert int(watermark["offset"]) == 2
    db.close()

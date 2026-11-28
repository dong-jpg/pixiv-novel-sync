from __future__ import annotations

from types import SimpleNamespace

from pixiv_novel_sync.jobs.quick_sync import run_scheduled_user_backup
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.sync.following_snapshot import save_following_snapshot


def test_failed_user_does_not_stall_backup_rotation(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "backup.db"
    db = Database(db_path)
    db.init_schema()
    for user_id in (1, 2, 3):
        db.conn.execute(
            "INSERT INTO users (user_id, name, raw_json) VALUES (?, '作者', '{}')",
            (user_id,),
        )
    save_following_snapshot(db, 99, [SimpleNamespace(id=author) for author in (1, 2)])
    db.close()

    def backup(settings, user_id, **kwargs):
        if user_id == 1:
            raise RuntimeError("上游失败")
        return {"novels": 3, "skipped": 0, "assets_downloaded": 0}

    monkeypatch.setattr("pixiv_novel_sync.jobs.services._login", lambda settings, **kwargs: object())
    monkeypatch.setattr(
        "pixiv_novel_sync.jobs.quick_sync.job_services.run_user_backup_task",
        backup,
    )
    settings = SimpleNamespace(
        storage=SimpleNamespace(db_path=db_path),
        pixiv=SimpleNamespace(user_id=99), sync=SimpleNamespace(user_backup_users_per_run=5),
    )

    result = run_scheduled_user_backup(settings)

    assert result["failed_users"] == 1
    assert result["novels"] == 3
    db = Database(db_path)
    db.init_schema()
    watermark = db.get_watermark("user_backup_rotation_v2", "99")
    assert watermark["last_author"] == 2
    assert result["users_total"] == 2
    db.close()

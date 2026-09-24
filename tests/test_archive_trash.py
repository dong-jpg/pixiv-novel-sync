from __future__ import annotations

import os
import time
from pathlib import Path

from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.storage_files import FileStorage
from pixiv_novel_sync.webapp import _sweep_stale_trash, create_app


def test_sweep_removes_trash_older_than_one_day(tmp_path: Path) -> None:
    root = tmp_path / ".trash"
    old = root / "old"
    fresh = root / "fresh"
    old.mkdir(parents=True)
    fresh.mkdir()
    (old / "note.txt").write_text("x", encoding="utf-8")
    expired = time.time() - 90_000
    os.utime(old, (expired, expired))

    assert _sweep_stale_trash(root) == 1
    assert not old.exists()
    assert fresh.exists()


def test_confirm_failure_restores_files_and_pending_status(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    public = tmp_path / "public"
    private = tmp_path / "private"
    db_path = tmp_path / "state" / "trash.db"
    monkeypatch.setenv("PIXIV_DB_PATH", str(db_path))
    monkeypatch.setenv("PIXIV_PUBLIC_DIR", str(public))
    monkeypatch.setenv("PIXIV_PRIVATE_DIR", str(private))
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)

    db = Database(db_path)
    db.init_schema()
    db.conn.execute("INSERT INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')")
    archive_dir = FileStorage.relative_novel_dir(1, "作者", 10, "标题")
    db.conn.execute(
        """
        INSERT INTO novels (
            novel_id, title, user_id, visible, restrict_value, x_restrict,
            text_length, total_bookmarks, total_views, tags_json, raw_json,
            meta_hash, archive_dir
        ) VALUES (10, '标题', 1, 1, 'public', 0, 10, 0, 0, '[]', '{}', 'h', ?)
        """,
        (archive_dir,),
    )
    db.add_pending_deletion("novel", 10, "已取消收藏", "标题", "作者", "")
    deletion_id = db.conn.execute("SELECT id FROM pending_deletions").fetchone()[0]
    db.close()

    cover = public / archive_dir / "assets" / "cover" / "a.jpg"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"img")

    def boom(self, novel_id: int) -> None:
        raise RuntimeError("删库失败")

    monkeypatch.setattr(Database, "delete_novel", boom)
    response = app.test_client().post(f"/api/dashboard/pending-deletions/{deletion_id}/confirm")

    assert response.status_code == 500
    assert cover.read_bytes() == b"img"
    db = Database(db_path)
    db.init_schema()
    status = db.conn.execute(
        "SELECT status FROM pending_deletions WHERE id = ?",
        (deletion_id,),
    ).fetchone()[0]
    assert status == "pending"
    db.close()

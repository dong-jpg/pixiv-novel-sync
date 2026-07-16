from __future__ import annotations

from pathlib import Path

from pixiv_novel_sync.storage_db import Database


def test_ai_project_cover_path_migration_and_crud(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.init_schema()
    project_id = db.create_ai_writing_project({"name": "封面测试"})

    db.update_ai_writing_project(project_id, {"cover_path": "ai_projects/1/cover.png"})
    project = db.get_ai_writing_project(project_id)

    assert project is not None
    assert project["cover_path"] == "ai_projects/1/cover.png"
    db.init_schema()
    assert db.get_ai_writing_project(project_id)["cover_path"] == "ai_projects/1/cover.png"
    db.close()

from __future__ import annotations

from types import SimpleNamespace

from pixiv_novel_sync.models import NovelRecord
from pixiv_novel_sync.settings import StorageSettings
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.storage_files import FileStorage
from pixiv_novel_sync.webapp import _ArchiveTrash


def _settings(tmp_path):
    return SimpleNamespace(
        storage=StorageSettings(
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
            db_path=tmp_path / "state.db",
        )
    )


def _record(archive_dir: str) -> NovelRecord:
    return NovelRecord(
        novel_id=10,
        user_id=1,
        series_id=None,
        title="标题",
        caption=None,
        visible=True,
        restrict="public",
        x_restrict=0,
        text_length=10,
        total_bookmarks=0,
        total_views=0,
        cover_url="https://i.pximg.net/a.jpg",
        tags_json="[]",
        create_date=None,
        raw_json="{}",
        meta_hash="h",
        archive_dir=archive_dir,
    )


def test_renamed_author_keeps_old_archive_for_cover_and_delete(tmp_path) -> None:
    settings = _settings(tmp_path)
    storage = FileStorage(settings)
    db = Database(settings.storage.db_path)
    db.init_schema()
    db.conn.execute("INSERT INTO users (user_id, name, raw_json) VALUES (1, '旧名', '{}')")
    old_rel = storage.relative_novel_dir(1, "旧名", 10, "标题")
    db.upsert_novel(_record(old_rel))

    cover = storage.get_novel_cover_path(db.get_novel_detail(10))
    assert cover is not None
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"img")
    old_dir = cover.parent.parent.parent

    db.conn.execute("UPDATE users SET name = '新名' WHERE user_id = 1")
    found = storage.get_novel_cover_path(db.get_novel_detail(10))
    assert found == cover
    assert found.read_bytes() == b"img"

    refs = db.list_novel_archive_refs(novel_ids=[10])
    trash = _ArchiveTrash(settings, refs)
    trash.stage()
    assert not old_dir.exists()
    db.close()

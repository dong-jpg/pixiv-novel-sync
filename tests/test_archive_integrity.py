from pathlib import Path

from pixiv_novel_sync.models import NovelRecord, NovelTextRecord, UserRecord
from pixiv_novel_sync.settings import PixivSettings, Settings, StorageSettings, SyncSettings
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.storage_files import FileStorage
from pixiv_novel_sync.webapp import _remove_archive_files


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        pixiv=PixivSettings(refresh_token="", access_token=None, proxy=None, timeout=30, verify_ssl=True, user_id=None),
        sync=SyncSettings(
            enabled=True,
            initial_manual_only=False,
            download_assets=True,
            write_markdown=True,
            write_raw_text=True,
            bookmark_restricts=["public"],
            max_items_per_run=None,
            max_pages_per_run=None,
            delay_seconds_between_items=0,
            delay_seconds_between_pages=0,
        ),
        storage=StorageSettings(
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
            db_path=tmp_path / "state" / "sync.db",
        ),
    )


def insert_novel(db: Database, novel_id: int = 100, cover_url: str | None = "https://i.pximg.net/c/cover.jpg") -> None:
    db.upsert_user(UserRecord(user_id=1, name="作者A", account="a", raw_json="{}"))
    db.upsert_novel(NovelRecord(
        novel_id=novel_id,
        user_id=1,
        series_id=None,
        title="测试小说",
        caption="简介",
        visible=True,
        restrict="public",
        x_restrict=0,
        text_length=6000,
        total_bookmarks=10,
        total_views=100,
        cover_url=cover_url,
        tags_json="[]",
        create_date=None,
        raw_json="{}",
        meta_hash="meta",
    ))


def test_existing_novel_ids_require_text_and_requested_assets(tmp_path: Path):
    settings = make_settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    insert_novel(db)

    assert db.get_existing_novel_ids([100]) == set()

    db.upsert_novel_text(NovelTextRecord(novel_id=100, text_raw="正文", text_markdown=None, text_hash="text"))
    assert db.get_existing_novel_ids([100]) == {100}
    assert db.get_existing_novel_ids([100], require_assets=True) == set()

    db.record_asset(100, "cover", "https://i.pximg.net/c/cover.jpg", str(tmp_path / "cover.jpg"), "hash")
    assert db.get_existing_novel_ids([100], require_assets=True) == {100}
    db.close()


def test_remove_archive_files_deletes_only_storage_paths(tmp_path: Path):
    settings = make_settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    insert_novel(db)
    db.upsert_novel_text(NovelTextRecord(novel_id=100, text_raw="正文", text_markdown=None, text_hash="text"))

    storage = FileStorage(settings)
    novel_dir = storage.novel_dir("public", 1, "作者A", 100, "测试小说")
    asset_path = storage.asset_path(novel_dir, "cover", "cover.jpg")
    storage.write_text(novel_dir / "text.txt", "正文")
    storage.write_bytes(asset_path, b"image")
    db.record_asset(100, "cover", "https://i.pximg.net/c/cover.jpg", str(asset_path), "hash")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    refs = db.list_novel_archive_refs(novel_ids=[100])
    refs[0]["asset_paths"].append(str(outside))
    stats = _remove_archive_files(settings, refs)

    assert stats["dirs_removed"] == 1
    assert stats["skipped"] == 1
    assert not novel_dir.exists()
    assert outside.exists()
    db.close()

import base64
import io
import os
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from pixiv_novel_sync.epub_exporter import create_epub_from_novel
from pixiv_novel_sync.illustrations import collect_mapping, mapped_assets, safe_image_path, save_mapping
from pixiv_novel_sync.models import AssetRecord
from pixiv_novel_sync.pixiv_fetch import fetch_novel
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.sync.series_inventory import remember_order
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService
from test_pixiv_fetch import Api
from test_rescue_api import _seed_rescue_data, _rotate_token
from test_settings_sections import _dashboard_app
from test_user_backup_incremental import _env, _OfflineStorage


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aK1sAAAAASUVORK5CYII=")
IMAGE_URL = "https://i.pximg.net/illustration.png"


@pytest.fixture
def reader(tmp_path):
    application, client, config = _dashboard_app(tmp_path, rescue_archive_read_enabled=False)
    db = Database(Path(os.environ["PIXIV_DB_PATH"]))
    _seed_rescue_data(db.path)
    image = Path(os.environ["PIXIV_PUBLIC_DIR"]) / "illustration.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(PNG)
    db.record_assets([AssetRecord(11, "inline_image", IMAGE_URL, str(image), "image-hash")])
    save_mapping(db, 11, (("uploadedimage:3", IMAGE_URL),))
    token = _rotate_token(client)
    yield SimpleNamespace(app=application, client=client, db=db, image=image,
                          headers={"Authorization": "Bearer " + token})
    db.close()


def enable_archive(reader):
    response = reader.client.put("/api/dashboard/settings/system", json={"rescue_archive_read_enabled": True})
    assert response.status_code == 200


def test_normal_archive_requires_opt_in_request_and_bearer(reader):
    path = "/api/rescue/v1/novels/11"
    assert reader.client.get(path + "?archive=1", headers=reader.headers).status_code == 404
    enable_archive(reader)
    assert reader.client.get(path + "?archive=1").status_code == 401
    assert reader.client.get(path, headers=reader.headers).status_code == 404
    response = reader.client.get(path + "?archive=1", headers=reader.headers)
    assert response.status_code == 200
    assert response.get_json()["data"]["text_raw"] == "正常正文"
    assert reader.client.get("/api/rescue/v1/lookup?novel_ids=11", headers=reader.headers).status_code == 200
    assert reader.db.evaluate_rescue_novel(11)["rescue_state"] is None


def test_explicit_exclusion_is_respected_for_archive_text_and_images(reader):
    enable_archive(reader)
    reader.db.conn.execute("INSERT INTO rescue_overrides(item_type,item_id,action) VALUES('novel',11,'exclude')")
    asset_id = mapped_assets(reader.db, 11)[0]["asset_id"]
    for path in ("/api/rescue/v1/novels/11", f"/api/rescue/v1/novels/11/assets/{asset_id}"):
        assert reader.client.get(path + "?archive=1", headers=reader.headers).status_code == 404


def test_image_access_enforces_novel_ownership_and_archive_gate(reader):
    asset_id = mapped_assets(reader.db, 11)[0]["asset_id"]
    path = f"/api/rescue/v1/novels/11/assets/{asset_id}"
    assert reader.client.get(path + "?archive=1", headers=reader.headers).status_code == 404
    enable_archive(reader)
    assert reader.client.get(path + "?archive=1").status_code == 401
    response = reader.client.get(path + "?archive=1", headers=reader.headers)
    assert response.status_code == 200 and response.data == PNG
    assert response.mimetype == "image/png" and "no-store" in response.headers["Cache-Control"]
    response.close()
    assert reader.client.get(f"/api/rescue/v1/novels/10/assets/{asset_id}", headers=reader.headers).status_code == 404
    assert reader.client.get(f"/api/dashboard/novels/10/assets/{asset_id}").status_code == 404
    response = reader.client.get(f"/api/dashboard/novels/11/assets/{asset_id}")
    assert response.data == PNG
    response.close()
    reader.image.unlink()
    assert reader.client.get(path + "?archive=1", headers=reader.headers).status_code == 404


def test_migrated_non_subscribed_series_remains_unsubscribed_and_readable(reader):
    reader.db.conn.execute("UPDATE novels SET series_id=88,raw_json=? WHERE novel_id=11",
                           ('{"series":{"id":88,"title":"已归档系列"}}',))
    reader.db.init_schema()
    reader.db.init_schema()
    series = reader.db.get_series_detail(88)
    assert series["is_subscribed"] == 0 and series["title"] == "已归档系列"
    assert reader.db.get_rescue_series(88) is None
    assert reader.db.get_rescue_series(88, archived=True)["expected_count"] is None
    reader.db.conn.execute("UPDATE series SET title='' WHERE series_id=88")
    reader.db.repair_blank_series_titles()
    assert reader.db.get_series_detail(88)["expected_count"] is None
    enable_archive(reader)
    response = reader.client.get("/api/rescue/v1/series/88/chapters?archive=1", headers=reader.headers)
    assert response.status_code == 200
    assert [item["novel_id"] for item in response.get_json()["data"]["items"]] == [11]


def test_rescue_directory_and_neighbors_follow_remote_order(reader):
    reader.db.conn.execute("UPDATE novels SET series_id=20,create_date='2020-01-01' WHERE novel_id=11")
    reader.db.conn.execute("UPDATE novels SET create_date='2021-01-01' WHERE novel_id=21")
    assert remember_order(reader.db, 20, [21, 11], 2)
    directory = reader.db.list_rescue_series_chapters(20)
    assert [chapter["novel_id"] for chapter in directory["items"]] == [21, 11]
    assert [chapter["novel_id"] for chapter in reader.db.get_series_detail(20)["novels"]] == [21, 11]
    nav = reader.db.get_rescue_novel_extras(21, 20)["series_nav"]
    assert nav["prev"] is None and nav["next"]["novel_id"] == 11
    assert nav["chapter_number"] == 1
    assert reader.db.get_rescue_series_cards([20])[20]["latest_novel_id"] == 11


def test_following_dashboard_handles_cold_start_membership_and_unfollow(reader):
    from pixiv_novel_sync.sync.following_snapshot import save_following_snapshot

    response = reader.client.get("/api/dashboard/users?scope=following")
    assert response.status_code == 409
    assert reader.client.get("/api/dashboard/follows").status_code == 409
    save_following_snapshot(reader.db, 99, [SimpleNamespace(id=1)])
    save_following_snapshot(reader.db, 99, [], "private")
    response = reader.client.get("/api/dashboard/users?scope=following")
    assert response.status_code == 200
    assert [item["user_id"] for item in response.get_json()["items"]] == [1]
    legacy = reader.client.get("/api/dashboard/follows").get_json()
    assert legacy["page_size"] == 10 and [item["user_id"] for item in legacy["items"]] == [1]
    save_following_snapshot(reader.db, 99, [])
    assert reader.client.get("/api/dashboard/users?scope=following").get_json()["total"] == 0
    assert reader.client.get("/api/dashboard/follows").get_json()["items"] == []
    assert reader.client.get("/api/dashboard/users").get_json()["total"] == 1


def test_only_explicit_matching_illustration_metadata_is_saved():
    api = Api(text="[uploadedimage:3]\n[pixivimage:4-2]\n[uploadedimage:99]")
    api.webview = {"text": api.text, "images": {"3": {"urls": {"original": IMAGE_URL}}},
                   "illusts": {"4-2": {"image_urls": {"large": "https://i.pximg.net/art.jpg"}}},
                   "avatar": "https://i.pximg.net/avatar.jpg"}
    fetched = fetch_novel(api, 42)
    assert dict(fetched.illustrations) == {"uploadedimage:3": IMAGE_URL, "pixivimage:4-2": "https://i.pximg.net/art.jpg"}
    assert collect_mapping(api.text, {"images": [IMAGE_URL], "avatar": IMAGE_URL}) == ()


def test_unknown_illustration_url_preserves_text_without_guessing_image():
    api = Api(text="[uploadedimage:3]")
    api.webview = {"text": api.text, "images": {"3": "https://unrecognized.example/illustration.png"}}
    fetched = fetch_novel(api, 42)
    assert fetched.text == "[uploadedimage:3]\n" and fetched.illustrations == ()


def test_missing_inline_image_repairs_without_refetching_text(tmp_path, monkeypatch):
    settings, api = _env(tmp_path, monkeypatch)
    api.webview_novel = lambda identity: {"text": "[uploadedimage:3]", "images": {"3": IMAGE_URL}}
    db = Database(settings.storage.db_path)
    service = BookmarkNovelSyncService(api, db, _OfflineStorage(settings), settings)
    try:
        assert service._sync_novel(SimpleNamespace(id=42), "public", True, True, True, "bookmark_public")["novels"] == 1
        asset = mapped_assets(db, 42)[0]
        image = safe_image_path(settings, asset)
        image.unlink()
        assert not service._archive_complete_on_disk(42, True)
        before = list(api.detail_calls)
        result = service._sync_novel(SimpleNamespace(id=42), None, True, True, True, "user_backup")
        assert result["assets_repaired"] == 1 and not result["failed"]
        assert api.detail_calls == before
        assert service._archive_complete_on_disk(42, True)
        api.webview_novel = lambda identity: {"text": "[uploadedimage:3]"}
        service._sync_novel(SimpleNamespace(id=42), None, True, True, True, "user_backup")
        assert mapped_assets(db, 42)[0]["remote_url"] == IMAGE_URL
        api.webview_novel = lambda identity: {"text": "新正文没有插图"}
        service._sync_novel(SimpleNamespace(id=42), None, True, True, True, "user_backup")
        assert mapped_assets(db, 42) == []
    finally:
        db.close()


def test_failed_asset_download_does_not_claim_complete_archive(tmp_path, monkeypatch):
    settings, api = _env(tmp_path, monkeypatch)
    db = Database(settings.storage.db_path)
    storage = _OfflineStorage(settings)
    storage.download_asset = lambda *args, **kwargs: None
    try:
        service = BookmarkNovelSyncService(api, db, storage, settings)
        result = service._sync_novel(SimpleNamespace(id=42), None, True, True, True, "user_backup")
        assert result["failed_assets"] == 1 and result["failed"] == 1
        assert not service._archive_complete_on_disk(42, True)
    finally:
        db.close()


def test_safe_image_path_rejects_escape_and_recovers_relative_archive_location(tmp_path):
    root = tmp_path / "public"
    target = root / "authors" / "one" / "assets" / "inline_image" / "illustration.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(PNG)
    settings = SimpleNamespace(storage=SimpleNamespace(public_dir=root, private_dir=tmp_path / "private"))
    asset = {"asset_id": 1, "local_path": "old-root/public/missing.png", "archive_dir": "authors/one",
             "remote_url": IMAGE_URL, "restrict_value": "public"}
    assert safe_image_path(settings, asset) == target
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    assert safe_image_path(settings, {"asset_id": 1, "local_path": str(outside)}) is None


def test_epub_embeds_exact_image_bytes_and_respects_image_budget(tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(PNG)
    novel = {"novel_id": 42, "title": "插图测试", "author_name": "作者"}
    for budget, expected in ((len(PNG), 1), (len(PNG) - 1, 0)):
        content = create_epub_from_novel(novel, "[uploadedimage:3]", inline_images={"uploadedimage:3": image}, image_budget=budget)
        with ZipFile(io.BytesIO(content)) as book:
            images = [name for name in book.namelist() if "/images/illustration_" in name]
            assert len(images) == expected
            if images:
                assert book.read(images[0]) == PNG
                assert b"images/illustration_1.png" in book.read("EPUB/chap_001.xhtml")

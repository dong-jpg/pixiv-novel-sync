from __future__ import annotations

import io
from pathlib import Path

import pytest

from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.webapp import create_app


PNG_1X1 = b"\x89PNG\r\n\x1a\n" + b"png-data"
JPEG_1X1 = b"\xff\xd8\xff" + b"jpeg-data"
WEBP_1X1 = b"RIFF\x04\x00\x00\x00WEBP" + b"webp-data"


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


def make_cover_client(tmp_path: Path, monkeypatch):
    public_dir = tmp_path / "public"
    private_dir = tmp_path / "private"
    db_path = tmp_path / "ai.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "storage:\n"
        f"  public_dir: {public_dir.as_posix()}\n"
        f"  private_dir: {private_dir.as_posix()}\n"
        f"  db_path: {db_path.as_posix()}\n"
        "sync:\n"
        "  auto_sync_enabled: false\n",
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "cover-test-secret")
    monkeypatch.setenv("PIXIV_DB_PATH", str(db_path))
    monkeypatch.setenv("PIXIV_PUBLIC_DIR", str(public_dir))
    monkeypatch.setenv("PIXIV_PRIVATE_DIR", str(private_dir))
    app = create_app(config_path=str(config_path), env_path=str(env_path))
    app.config["TESTING"] = True
    db = Database(db_path)
    db.init_schema()
    project_id = db.create_ai_writing_project({"name": "封面测试"})
    db.close()
    client = app.test_client()
    csrf = client.get("/api/csrf-token").get_json()["csrf_token"]
    return client, project_id, public_dir, db_path, csrf


@pytest.mark.parametrize(
    ("filename", "content_type", "payload"),
    [
        ("cover.jpg", "image/jpeg", JPEG_1X1),
        ("cover.png", "image/png", PNG_1X1),
        ("cover.webp", "image/webp", WEBP_1X1),
    ],
)
def test_ai_project_cover_upload_read_delete(
    tmp_path: Path,
    monkeypatch,
    filename: str,
    content_type: str,
    payload: bytes,
) -> None:
    client, project_id, _public_dir, _db_path, csrf = make_cover_client(tmp_path, monkeypatch)
    uploaded = client.post(
        f"/api/dashboard/ai/projects/{project_id}/cover",
        data={"cover": (io.BytesIO(payload), filename, content_type)},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": csrf},
    )

    assert uploaded.status_code == 200
    cover_url = uploaded.get_json()["data"]["cover_url"]
    fetched = client.get(cover_url)
    assert fetched.status_code == 200
    assert fetched.data == payload
    assert fetched.content_type == content_type
    fetched.close()

    assert client.get(f"/api/dashboard/ai/projects/{project_id}").get_json()["data"]["cover_url"] == cover_url
    listed = client.get("/api/dashboard/ai/projects").get_json()["data"]
    assert listed[0]["cover_url"] == cover_url
    reader = client.get(f"/api/dashboard/ai/projects/{project_id}/reader").get_json()["data"]
    assert reader["project"]["cover_url"] == cover_url

    deleted = client.delete(cover_url, headers={"X-CSRF-Token": csrf})
    assert deleted.status_code == 200, deleted.get_json()
    assert client.get(cover_url).status_code == 404
    assert client.get(f"/api/dashboard/ai/projects/{project_id}").get_json()["data"]["cover_url"] is None


@pytest.mark.parametrize(
    ("filename", "content_type", "payload"),
    [
        ("fake.png", "image/png", b"not-an-image"),
        ("cover.exe", "application/octet-stream", PNG_1X1),
        ("cover.png", "image/jpeg", PNG_1X1),
        ("fake.webp", "image/webp", b"RIFF\x04\x00\x00\x00NOPE"),
    ],
)
def test_ai_project_cover_rejects_invalid_files(
    tmp_path: Path,
    monkeypatch,
    filename: str,
    content_type: str,
    payload: bytes,
) -> None:
    client, project_id, _public_dir, _db_path, csrf = make_cover_client(tmp_path, monkeypatch)
    response = client.post(
        f"/api/dashboard/ai/projects/{project_id}/cover",
        data={"cover": (io.BytesIO(payload), filename, content_type)},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400


def test_ai_project_cover_rejects_oversized_file(tmp_path: Path, monkeypatch) -> None:
    client, project_id, _public_dir, _db_path, csrf = make_cover_client(tmp_path, monkeypatch)
    response = client.post(
        f"/api/dashboard/ai/projects/{project_id}/cover",
        data={
            "cover": (
                io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * (10 * 1024 * 1024 + 1)),
                "cover.png",
                "image/png",
            )
        },
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400


def test_ai_project_cover_replacement_removes_old_extension(tmp_path: Path, monkeypatch) -> None:
    client, project_id, public_dir, _db_path, csrf = make_cover_client(tmp_path, monkeypatch)
    for filename, content_type, payload in (
        ("cover.png", "image/png", PNG_1X1),
        ("cover.jpg", "image/jpeg", JPEG_1X1),
    ):
        response = client.post(
            f"/api/dashboard/ai/projects/{project_id}/cover",
            data={"cover": (io.BytesIO(payload), filename, content_type)},
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200

    project_dir = public_dir / "ai_projects" / str(project_id)
    assert not (project_dir / "cover.png").exists()
    assert (project_dir / "cover.jpg").read_bytes() == JPEG_1X1


def test_ai_project_cover_removes_new_file_when_database_update_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, project_id, public_dir, db_path, csrf = make_cover_client(tmp_path, monkeypatch)

    def fail_update(_service, _project_id, _cover_path):
        raise RuntimeError("database update failed")

    monkeypatch.setattr(AIWritingService, "update_writing_project_cover", fail_update)
    response = client.post(
        f"/api/dashboard/ai/projects/{project_id}/cover",
        data={"cover": (io.BytesIO(PNG_1X1), "cover.png", "image/png")},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    assert not (public_dir / "ai_projects" / str(project_id) / "cover.png").exists()
    db = Database(db_path)
    db.init_schema()
    project = db.get_ai_writing_project(project_id)
    db.close()
    assert project["cover_path"] is None


def test_ai_project_cover_restores_old_file_when_replacement_update_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, project_id, public_dir, _db_path, csrf = make_cover_client(tmp_path, monkeypatch)
    initial = client.post(
        f"/api/dashboard/ai/projects/{project_id}/cover",
        data={"cover": (io.BytesIO(PNG_1X1), "cover.png", "image/png")},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": csrf},
    )
    assert initial.status_code == 200

    def fail_update(_service, _project_id, _cover_path):
        raise RuntimeError("database update failed")

    monkeypatch.setattr(AIWritingService, "update_writing_project_cover", fail_update)
    replacement = b"\x89PNG\r\n\x1a\nreplacement"
    response = client.post(
        f"/api/dashboard/ai/projects/{project_id}/cover",
        data={"cover": (io.BytesIO(replacement), "cover.png", "image/png")},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    target = public_dir / "ai_projects" / str(project_id) / "cover.png"
    assert target.read_bytes() == PNG_1X1


def test_ai_project_cover_rejects_stored_path_escape(tmp_path: Path, monkeypatch) -> None:
    client, project_id, public_dir, db_path, _csrf = make_cover_client(tmp_path, monkeypatch)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_1X1)
    db = Database(db_path)
    db.init_schema()
    db.update_ai_writing_project(project_id, {"cover_path": "../outside.png"})
    db.close()

    response = client.get(f"/api/dashboard/ai/projects/{project_id}/cover")

    assert response.status_code == 400
    assert outside.read_bytes() == PNG_1X1
    assert not (public_dir / "outside.png").exists()


def test_ai_project_cover_rejects_path_from_another_project(tmp_path: Path, monkeypatch) -> None:
    client, project_id, public_dir, db_path, _csrf = make_cover_client(tmp_path, monkeypatch)
    other_cover = public_dir / "ai_projects" / "999" / "cover.png"
    other_cover.parent.mkdir(parents=True)
    other_cover.write_bytes(PNG_1X1)
    db = Database(db_path)
    db.init_schema()
    db.update_ai_writing_project(project_id, {"cover_path": "ai_projects/999/cover.png"})
    db.close()

    response = client.get(f"/api/dashboard/ai/projects/{project_id}/cover")

    assert response.status_code == 400
    assert other_cover.read_bytes() == PNG_1X1


def test_deleting_cover_does_not_remove_another_project_file(tmp_path: Path, monkeypatch) -> None:
    client, project_id, public_dir, db_path, csrf = make_cover_client(tmp_path, monkeypatch)
    other_cover = public_dir / "ai_projects" / "999" / "cover.png"
    other_cover.parent.mkdir(parents=True)
    other_cover.write_bytes(PNG_1X1)
    db = Database(db_path)
    db.init_schema()
    db.update_ai_writing_project(project_id, {"cover_path": "ai_projects/999/cover.png"})
    db.close()

    response = client.delete(
        f"/api/dashboard/ai/projects/{project_id}/cover",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    assert other_cover.read_bytes() == PNG_1X1


def test_project_update_api_cannot_set_cover_path(tmp_path: Path, monkeypatch) -> None:
    client, project_id, _public_dir, db_path, csrf = make_cover_client(tmp_path, monkeypatch)

    updated = client.put(
        f"/api/dashboard/ai/projects/{project_id}",
        json={"cover_path": "../outside.png"},
        headers={"X-CSRF-Token": csrf},
    )

    assert updated.status_code == 200
    db = Database(db_path)
    db.init_schema()
    project = db.get_ai_writing_project(project_id)
    db.close()
    assert project["cover_path"] is None


def test_deleting_ai_project_removes_its_cover(tmp_path: Path, monkeypatch) -> None:
    client, project_id, public_dir, _db_path, csrf = make_cover_client(tmp_path, monkeypatch)
    uploaded = client.post(
        f"/api/dashboard/ai/projects/{project_id}/cover",
        data={"cover": (io.BytesIO(PNG_1X1), "cover.png", "image/png")},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": csrf},
    )
    assert uploaded.status_code == 200
    project_dir = public_dir / "ai_projects" / str(project_id)
    assert project_dir.exists()

    deleted = client.delete(
        f"/api/dashboard/ai/projects/{project_id}",
        headers={"X-CSRF-Token": csrf},
    )

    assert deleted.status_code == 200
    assert not project_dir.exists()

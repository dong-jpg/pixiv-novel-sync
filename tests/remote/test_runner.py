from __future__ import annotations

import json
import io
import os
import zipfile

import pytest

from pixiv_novel_sync.remote.contracts import RemoteError
from pixiv_novel_sync.remote.contracts import digest
from pixiv_novel_sync.remote.runner import SpawnRunner, run_child


def test_diagnostic_is_only_fixed_challenge_response(tmp_path):
    manifest = {"protocol": 1, "renderer": "epub-v1", "challenge": "a" * 64}
    source, output = tmp_path / "manifest.json", tmp_path / "result.bin"
    source.write_text(json.dumps(manifest), encoding="utf-8")
    run_child("diagnostic.v1", source, None, None, output)
    assert json.loads(output.read_bytes()) == manifest


@pytest.mark.parametrize("kind", ["shell", "checkin", "epub.single.v2"])
def test_runner_rejects_arbitrary_execution(kind, tmp_path):
    source = tmp_path / "manifest.json"
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(RemoteError, match="unsupported_kind"):
        run_child(kind, source, None, None, tmp_path / "result.bin")


def test_runner_rejects_path_or_command_fields(tmp_path):
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps({"protocol": 1, "renderer": "epub-v1", "challenge": "a" * 64,
                                  "command": "do not execute"}), encoding="utf-8")
    with pytest.raises(RemoteError):
        run_child("diagnostic.v1", source, None, None, tmp_path / "result.bin")
    assert not (tmp_path / "result.bin").exists()


def test_renderer_preserves_local_epub_content_semantics_without_business_calls(tmp_path, monkeypatch):
    from pixiv_novel_sync.epub_exporter import create_epub_from_novel
    from pixiv_novel_sync.storage_db import Database
    import pixiv_novel_sync.webapp as webapp

    def forbidden(*args, **kwargs):
        raise AssertionError("renderer must not open business services")

    monkeypatch.setattr(Database, "__init__", forbidden)
    monkeypatch.setattr(webapp, "create_app", forbidden)
    text = "First[newpage]Second[[rb:中文 > zhongwen]]".encode("utf-8")
    metadata = {"novel_id": 42, "title": "Fixture", "author_name": "Author"}
    manifest = {"protocol": 1, "renderer": "epub-v1", **metadata,
                "text_sha256": digest(text), "text_size": len(text), "cover_sha256": None, "cover_size": 0}
    source, content, output = tmp_path / "manifest.json", tmp_path / "text.bin", tmp_path / "result.bin"
    source.write_text(json.dumps(manifest), encoding="utf-8")
    content.write_bytes(text)
    run_child("epub.single.v1", source, content, None, output)
    local = create_epub_from_novel(metadata, text.decode("utf-8"))
    with zipfile.ZipFile(output) as remote, zipfile.ZipFile(io.BytesIO(local)) as expected:
        names = [name for name in expected.namelist() if name.endswith(".xhtml")]
        assert {name: remote.read(name) for name in names} == {name: expected.read(name) for name in names}


def test_real_spawn_completes_fixed_diagnostic(tmp_path):
    manifest = {"protocol": 1, "renderer": "epub-v1", "challenge": "a" * 64}
    source, output = tmp_path / "manifest.json", tmp_path / "result.bin"
    source.write_text(json.dumps(manifest), encoding="utf-8")
    child = SpawnRunner().start("diagnostic.v1", source, None, None, output)
    try:
        child._process.join(timeout=10)
        assert child.poll() == 0
        assert json.loads(output.read_bytes()) == manifest
    finally:
        child.stop()


def test_spawn_entry_removes_credentials_before_rendering(tmp_path, monkeypatch):
    import pixiv_novel_sync.remote.runner as runner

    captured = {}
    isolated_env = {"PATH": "fixed-path", "PIXIV_REMOTE_WORKER_KEY": "worker-secret",
                    "INSTABOX_API_KEY": "provider-secret", "PIXIV_FLASK_SECRET": "flask-secret"}
    monkeypatch.setattr(os, "environ", isolated_env)
    monkeypatch.setattr(os, "umask", lambda _: 0)
    monkeypatch.setattr(runner, "run_child", lambda *args: captured.update(os.environ))
    runner._child_entry("diagnostic.v1", tmp_path / "manifest.json", None, None, tmp_path / "result.bin")
    assert captured == {"PATH": "fixed-path"}


def test_runner_rejects_duplicate_manifest_keys(tmp_path):
    source = tmp_path / "manifest.json"
    source.write_text('{"protocol":1,"protocol":1,"renderer":"epub-v1","challenge":"' + "a" * 64 + '"}')
    with pytest.raises(RemoteError, match="invalid_manifest"):
        run_child("diagnostic.v1", source, None, None, tmp_path / "result.bin")

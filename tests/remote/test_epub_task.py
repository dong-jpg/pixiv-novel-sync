from __future__ import annotations

import io
import json
import zipfile

import pytest

from pixiv_novel_sync.epub_exporter import create_epub_from_novel
from pixiv_novel_sync.remote.blobs import BlobStore
from pixiv_novel_sync.remote.contracts import RemoteError, digest
from pixiv_novel_sync.remote.epub_task import (
    decode_manifest,
    freeze_diagnostic,
    freeze_epub,
    render_epub,
    validate_manifest,
)
from pixiv_novel_sync.remote.results import validate_epub


def _frozen(tmp_path, **updates):
    novel = {"novel_id": 42, "title": "合成小说", "author_name": "Author",
             "text_raw": "First[newpage][chapter:第二页]Second [[rb:文字 > もじ]][pixivimage:123]",
             "raw_json": "private-extra", "secret": "private-value"}
    novel.update(updates)
    blobs = BlobStore(tmp_path / "blobs")
    sha = freeze_epub(novel, None, blobs)
    with blobs.open_blob(sha) as source:
        manifest = json.load(source)
    with blobs.open_blob(manifest["text_sha256"]) as source:
        text = source.read()
    return novel, blobs, sha, manifest, text


def _semantic_entries(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as book:
        return {name: book.read(name) for name in book.namelist()
                if name.endswith((".xhtml", ".css", ".ncx")) or name == "mimetype"}


def test_minimal_snapshot_renders_same_content_without_business_fields(tmp_path):
    novel, _, _, manifest, text = _frozen(tmp_path)
    assert "raw_json" not in manifest and "secret" not in manifest
    result = render_epub(manifest, text, None, tmp_path / "work")
    validate_epub(io.BytesIO(result))
    local = create_epub_from_novel(novel, novel["text_raw"])
    assert _semantic_entries(result) == _semantic_entries(local)
    assert _semantic_entries(result)["mimetype"] == b"application/epub+zip"


def test_freeze_uses_utf8_byte_limit_and_does_not_write_partial_snapshot(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    novel = {"novel_id": 1, "title": "Large", "text_raw": "中" * (6 * 1024 * 1024)}
    with pytest.raises(RemoteError, match="text_too_large"):
        freeze_epub(novel, None, blobs)
    assert list(blobs.root.iterdir()) == []


def test_freeze_rejects_large_manifest_before_writing_payloads(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    with pytest.raises(RemoteError, match="manifest_too_large"):
        freeze_epub({"novel_id": 1, "title": "中" * 24000, "text_raw": "body"}, None, blobs)
    assert list(blobs.root.iterdir()) == []


@pytest.mark.parametrize("field,value", [("url", "https://example.invalid"), ("command", "anything"),
                                         ("protocol", True), ("text_size", True), ("novel_id", False)])
def test_manifest_is_closed_and_exactly_typed(tmp_path, field, value):
    _, _, _, manifest, _ = _frozen(tmp_path)
    manifest[field] = value
    with pytest.raises(RemoteError):
        validate_manifest(manifest)


def test_input_bytes_must_match_frozen_manifest(tmp_path):
    _, _, _, manifest, text = _frozen(tmp_path)
    with pytest.raises(RemoteError, match="input_mismatch"):
        render_epub(manifest, text + b"changed", None, tmp_path / "work")
    with pytest.raises(RemoteError, match="unexpected_cover"):
        render_epub(manifest, text, b"unexpected", tmp_path / "work")


@pytest.mark.parametrize("cover,extension", [
    (b"\x89PNG\r\n\x1a\nfixture", "png"), (b"\xff\xd8\xfffixture", "jpg"),
    (b"GIF89afixture", "gif"), (b"RIFF0000WEBPfixture", "webp"),
])
def test_cover_is_checked_and_rendered_without_url_access(tmp_path, cover, extension):
    blobs = BlobStore(tmp_path / "blobs")
    sha = freeze_epub({"novel_id": 2, "title": "Cover", "text_raw": "body"}, cover, blobs)
    with blobs.open_blob(sha) as source:
        manifest = json.load(source)
    result = render_epub(manifest, b"body", cover, tmp_path / "work")
    validate_epub(io.BytesIO(result))
    with zipfile.ZipFile(io.BytesIO(result)) as book:
        assert book.read("EPUB/images/cover." + extension) == cover
    with pytest.raises(RemoteError, match="input_mismatch"):
        render_epub(manifest, b"body", b"bad", tmp_path / "work")


def test_diagnostic_freezes_only_random_challenge_and_protocol(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    first, second = freeze_diagnostic(blobs), freeze_diagnostic(blobs)
    assert first != second
    with blobs.open_blob(first) as source:
        manifest = decode_manifest(source.read(), "diagnostic.v1")
    assert set(manifest) == {"challenge", "protocol", "renderer"}
    assert len(bytes.fromhex(manifest["challenge"])) == 32
    validate_manifest(manifest, "diagnostic.v1")


def test_duplicate_json_fields_and_nonfinite_values_are_rejected():
    with pytest.raises(RemoteError):
        decode_manifest(b'{"protocol":1,"protocol":1,"renderer":"epub-v1","challenge":"' + b"a" * 64 + b'"}', "diagnostic.v1")
    with pytest.raises(RemoteError):
        decode_manifest(b'{"protocol":NaN}', "diagnostic.v1")

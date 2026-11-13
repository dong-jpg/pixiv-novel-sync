"""The closed diagnostic and single-novel EPUB input formats."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import tempfile

from pixiv_novel_sync.epub_exporter import create_epub_from_novel

from .blobs import BlobStore, ensure_safe_directory, validate_sha256
from .contracts import (
    COVER_LIMIT, MANIFEST_LIMIT, PROTOCOL, RENDERER, RESULT_LIMIT, TEXT_LIMIT,
    RemoteError, canonical_bytes, digest,
)

_EPUB_FIELDS = frozenset({
    "protocol", "renderer", "novel_id", "title", "author_name",
    "text_sha256", "text_size", "cover_sha256", "cover_size",
})
_DIAGNOSTIC_FIELDS = frozenset({"protocol", "renderer", "challenge"})


def _strict_pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise RemoteError("invalid_manifest", 400)
        result[name] = value
    return result


def _invalid_constant(value):
    raise RemoteError("invalid_manifest", 400)


def decode_manifest(payload: bytes, kind: str = "epub.single.v1") -> dict:
    if not isinstance(payload, bytes) or len(payload) > MANIFEST_LIMIT:
        raise RemoteError("manifest_too_large", 413)
    try:
        manifest = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_pairs,
                              parse_constant=_invalid_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise RemoteError("invalid_manifest", 400) from None
    validate_manifest(manifest, kind)
    return manifest


def validate_manifest(manifest: dict, kind: str = "epub.single.v1") -> None:
    if kind not in {"diagnostic.v1", "epub.single.v1"}:
        raise RemoteError("unsupported_kind", 400)
    fields = _DIAGNOSTIC_FIELDS if kind == "diagnostic.v1" else _EPUB_FIELDS
    if type(manifest) is not dict or set(manifest) != fields:
        raise RemoteError("invalid_manifest", 400)
    if type(manifest["protocol"]) is not int or manifest["protocol"] != PROTOCOL or manifest["renderer"] != RENDERER:
        raise RemoteError("protocol_mismatch", 400)
    if kind == "diagnostic.v1":
        validate_sha256(manifest["challenge"])
    else:
        if type(manifest["novel_id"]) is not int or not 0 < manifest["novel_id"] <= 2**63 - 1:
            raise RemoteError("invalid_novel_id", 400)
        if not isinstance(manifest["title"], str) or not isinstance(manifest["author_name"], str):
            raise RemoteError("invalid_manifest", 400)
        validate_sha256(manifest["text_sha256"])
        if type(manifest["text_size"]) is not int or not 0 < manifest["text_size"] <= TEXT_LIMIT:
            raise RemoteError("invalid_manifest", 400)
        if type(manifest["cover_size"]) is not int or not 0 <= manifest["cover_size"] <= COVER_LIMIT:
            raise RemoteError("invalid_manifest", 400)
        if manifest["cover_sha256"] is None:
            if manifest["cover_size"] != 0:
                raise RemoteError("invalid_manifest", 400)
        else:
            validate_sha256(manifest["cover_sha256"])
    try:
        if len(canonical_bytes(manifest)) > MANIFEST_LIMIT:
            raise RemoteError("manifest_too_large", 413)
    except (UnicodeError, ValueError, TypeError):
        raise RemoteError("invalid_manifest", 400) from None


def freeze_diagnostic(blobs: BlobStore) -> str:
    manifest = {"challenge": secrets.token_hex(32), "protocol": PROTOCOL, "renderer": RENDERER}
    return blobs.put_bytes(canonical_bytes(manifest), MANIFEST_LIMIT)


def freeze_epub(novel: dict, cover: bytes | None, blobs: BlobStore) -> str:
    if type(novel.get("novel_id")) is not int or not 0 < novel["novel_id"] <= 2**63 - 1:
        raise RemoteError("invalid_novel_id", 400)
    raw = novel.get("text_raw")
    if not isinstance(raw, str) or not raw:
        raise RemoteError("text_missing", 400)
    try:
        text = raw.encode("utf-8")
    except UnicodeError:
        raise RemoteError("invalid_text", 400) from None
    if len(text) > TEXT_LIMIT:
        raise RemoteError("text_too_large", 413)
    if cover is not None and not isinstance(cover, bytes):
        raise RemoteError("invalid_cover", 400)
    if cover is not None and len(cover) > COVER_LIMIT:
        raise RemoteError("cover_too_large", 413)
    title, author = novel.get("title") or "", novel.get("author_name") or ""
    if not isinstance(title, str) or not isinstance(author, str):
        raise RemoteError("invalid_novel_metadata", 400)
    manifest = {
        "protocol": PROTOCOL, "renderer": RENDERER,
        "novel_id": novel["novel_id"], "title": title, "author_name": author,
        "text_sha256": digest(text), "text_size": len(text),
        "cover_sha256": digest(cover) if cover is not None else None,
        "cover_size": len(cover) if cover is not None else 0,
    }
    validate_manifest(manifest)
    encoded = canonical_bytes(manifest)
    blobs.put_bytes(text, TEXT_LIMIT)
    if cover is not None:
        blobs.put_bytes(cover, COVER_LIMIT)
    return blobs.put_bytes(encoded, MANIFEST_LIMIT)


def render_epub(manifest: dict, text: bytes, cover: bytes | None, work_dir: Path) -> bytes:
    validate_manifest(manifest)
    if not isinstance(text, bytes) or len(text) != manifest["text_size"] or digest(text) != manifest["text_sha256"]:
        raise RemoteError("input_mismatch", 400)
    if manifest["cover_sha256"] is not None:
        if (not isinstance(cover, bytes) or len(cover) != manifest["cover_size"]
                or digest(cover) != manifest["cover_sha256"]):
            raise RemoteError("input_mismatch", 400)
    elif cover is not None:
        raise RemoteError("unexpected_cover", 400)
    try:
        decoded_text = text.decode("utf-8")
    except UnicodeError:
        raise RemoteError("input_mismatch", 400) from None
    work_dir = ensure_safe_directory(work_dir)
    cover_path: Path | None = None
    try:
        if cover is not None:
            fd, name = tempfile.mkstemp(prefix="cover-", suffix=".bin", dir=work_dir)
            cover_path = Path(name)
            with os.fdopen(fd, "wb") as target:
                target.write(cover)
        metadata = {key: manifest[key] for key in ("novel_id", "title", "author_name")}
        result = create_epub_from_novel(metadata, decoded_text, cover_path)
        if len(result) > RESULT_LIMIT:
            raise RemoteError("result_too_large", 413)
        return result
    except OSError:
        raise RemoteError("storage_unavailable", 503) from None
    finally:
        if cover_path is not None:
            # Only our generated file within a checked directory is removed.
            ensure_safe_directory(work_dir)
            cover_path.unlink(missing_ok=True)

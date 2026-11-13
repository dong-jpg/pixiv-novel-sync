"""Bounded, durable files addressed only by a validated SHA-256 digest.

This layer owns file integrity. Callers must reserve control-store quota before
writing inputs or results; an unreferenced file is safe to recover with GC.
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import BinaryIO

from .contracts import RemoteError, digest

CHUNK_SIZE = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def validate_sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RemoteError("invalid_digest", 400)
    return value


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _check_directory(path: Path, *, missing_ok: bool = False) -> None:
    # Check parents before mkdir: resolving first would hide a symlink/junction.
    for candidate in reversed((path, *path.parents)):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if missing_ok:
                continue
            raise RemoteError("unsafe_blob_path", 503) from None
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise RemoteError("unsafe_blob_path", 503)
    if not missing_ok and path.resolve(strict=True) != path:
        raise RemoteError("unsafe_blob_path", 503)


def ensure_safe_directory(path: Path) -> Path:
    """Create an explicitly supplied private directory without following links."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise RemoteError("unsafe_blob_path", 503)
    try:
        _check_directory(path, missing_ok=True)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_directory(path)
    except OSError:
        raise RemoteError("storage_unavailable", 503) from None
    return path


class BlobStore:
    def __init__(self, root: Path):
        self.root = ensure_safe_directory(root)

    def _path(self, sha256: str) -> Path:
        validate_sha256(sha256)
        try:
            _check_directory(self.root)
        except OSError:
            raise RemoteError("storage_unavailable", 503) from None
        return self.root / sha256

    def _open(self, path: Path) -> BinaryIO:
        _check_directory(self.root)
        info = path.lstat()
        if _is_link(info) or not stat.S_ISREG(info.st_mode):
            raise RemoteError("unsafe_blob_path", 503)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(info, opened):
                raise RemoteError("unsafe_blob_path", 503)
            _check_directory(self.root)
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise

    def open_blob(self, sha256: str) -> BinaryIO:
        path = self._path(sha256)
        try:
            return self._open(path)
        except FileNotFoundError:
            raise RemoteError("blob_missing", 404) from None
        except OSError:
            raise RemoteError("storage_unavailable", 503) from None

    def verify(self, sha256: str, size_bytes: int) -> None:
        """Verify a previously published object without loading it into memory."""
        actual = hashlib.sha256()
        count = 0
        try:
            with self.open_blob(sha256) as source:
                while chunk := source.read(CHUNK_SIZE):
                    count += len(chunk)
                    if count > size_bytes:
                        raise RemoteError("blob_corrupt", 503)
                    actual.update(chunk)
        except OSError:
            raise RemoteError("storage_unavailable", 503) from None
        if count != size_bytes or actual.hexdigest() != sha256:
            raise RemoteError("blob_corrupt", 503)

    def put_bytes(self, data: bytes, limit: int) -> str:
        if not isinstance(data, bytes):
            raise RemoteError("invalid_blob", 400)
        sha256 = digest(data)
        self.receive(io.BytesIO(data), len(data), sha256, limit)
        return sha256

    def receive(
        self, stream: BinaryIO, expected_size: int, expected_sha256: str, limit: int,
    ) -> tuple[str, int]:
        validate_sha256(expected_sha256)
        if type(expected_size) is not int or expected_size < 0 or type(limit) is not int or limit < 0:
            raise RemoteError("invalid_size", 400)
        if expected_size > limit:
            raise RemoteError("blob_too_large", 413)
        path = self._path(expected_sha256)
        temporary: Path | None = None
        try:
            fd, name = tempfile.mkstemp(prefix=".upload-", dir=self.root)
            temporary = Path(name)
            actual = hashlib.sha256()
            count = 0
            with os.fdopen(fd, "wb") as target:
                while True:
                    # A final one-byte read detects a false declared length.
                    chunk = stream.read(min(CHUNK_SIZE, expected_size - count + 1))
                    if not isinstance(chunk, bytes):
                        raise RemoteError("invalid_blob", 400)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > expected_size:
                        raise RemoteError("size_mismatch", 400)
                    if count > limit:
                        raise RemoteError("blob_too_large", 413)
                    actual.update(chunk)
                    target.write(chunk)
                if count != expected_size:
                    raise RemoteError("size_mismatch", 400)
                if actual.hexdigest() != expected_sha256:
                    raise RemoteError("digest_mismatch", 409)
                target.flush()
                os.fsync(target.fileno())

            _check_directory(self.root)
            try:
                # Linking a fully fsynced same-directory temporary is atomic and
                # does not overwrite an object another uploader just installed.
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                self.verify(expected_sha256, expected_size)
            temporary.unlink()
            temporary = None
            self._sync_directory()
            return expected_sha256, count
        except OSError:
            raise RemoteError("storage_unavailable", 503) from None
        finally:
            if temporary is not None:
                try:
                    _check_directory(self.root)
                    temporary.unlink(missing_ok=True)
                except (OSError, RemoteError):
                    # Never follow a replaced root merely to clean up a temp.
                    pass

    def _sync_directory(self) -> None:
        if os.name == "nt":
            return
        _check_directory(self.root)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.root, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

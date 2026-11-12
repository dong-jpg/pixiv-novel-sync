from __future__ import annotations

import io
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from pixiv_novel_sync.remote.blobs import BlobStore
from pixiv_novel_sync.remote.contracts import RemoteError, digest


def test_content_addressed_round_trip_and_idempotent_put(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    payload = "合成正文".encode("utf-8")
    sha = blobs.put_bytes(payload, 1024)
    assert sha == digest(payload)
    assert blobs.put_bytes(payload, 1024) == sha
    with blobs.open_blob(sha) as source:
        assert source.read() == payload
    assert [path.name for path in blobs.root.iterdir()] == [sha]


@pytest.mark.parametrize("payload,declared", [(b"short", 6), (b"over", 3)])
def test_declared_length_is_verified_and_no_partial_object_survives(tmp_path, payload, declared):
    blobs = BlobStore(tmp_path / "blobs")
    with pytest.raises(RemoteError, match="size_mismatch"):
        blobs.receive(io.BytesIO(payload), declared, digest(payload), 1024)
    assert list(blobs.root.iterdir()) == []


def test_upload_hash_is_verified(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    with pytest.raises(RemoteError, match="digest_mismatch"):
        blobs.receive(io.BytesIO(b"payload"), 7, "a" * 64, 1024)
    assert list(blobs.root.iterdir()) == []


def test_limits_are_checked_before_reading(tmp_path):
    class MustNotRead:
        def read(self, size):
            raise AssertionError("body read despite invalid declared length")

    blobs = BlobStore(tmp_path / "blobs")
    with pytest.raises(RemoteError, match="blob_too_large"):
        blobs.receive(MustNotRead(), 33, "a" * 64, 32)
    with pytest.raises(RemoteError, match="invalid_size"):
        blobs.receive(MustNotRead(), True, "a" * 64, 32)


def test_reads_are_bounded_and_disk_failure_leaves_no_published_reference(tmp_path, monkeypatch):
    payload = b"x" * (150 * 1024)

    class BoundedStream(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 64 * 1024
            return super().read(size)

    blobs = BlobStore(tmp_path / "blobs")
    sha, size = blobs.receive(BoundedStream(payload), len(payload), digest(payload), len(payload))
    assert size == len(payload)
    assert sha == digest(payload)
    other = b"different"
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fixture disk full")))
    with pytest.raises(RemoteError, match="storage_unavailable"):
        blobs.put_bytes(other, 100)
    assert not (blobs.root / digest(other)).exists()
    assert not list(blobs.root.glob(".upload-*"))


def test_half_upload_is_cleaned_without_touching_existing_object(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    existing = blobs.put_bytes(b"keep", 10)

    class Interrupted:
        read_count = 0

        def read(self, size):
            self.read_count += 1
            if self.read_count == 1:
                return b"part"
            raise OSError("fixture disconnect")

    with pytest.raises(RemoteError, match="storage_unavailable"):
        blobs.receive(Interrupted(), 8, digest(b"partrest"), 16)
    assert [path.name for path in blobs.root.iterdir()] == [existing]


def test_suspicious_existing_object_is_never_overwritten(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    payload = b"valid"
    target = blobs.root / digest(payload)
    target.write_bytes(b"wrong")
    with pytest.raises(RemoteError, match="blob_corrupt"):
        blobs.put_bytes(payload, 100)
    assert target.read_bytes() == b"wrong"


def test_concurrent_identical_installers_publish_one_complete_object(tmp_path):
    first, second = BlobStore(tmp_path / "blobs"), BlobStore(tmp_path / "blobs")
    barrier = threading.Barrier(2)
    payload = b"synthetic" * 10000

    def write(blobs):
        barrier.wait(timeout=5)
        return blobs.put_bytes(payload, len(payload))

    with ThreadPoolExecutor(max_workers=2) as pool:
        hashes = list(pool.map(write, [first, second]))
    assert hashes == [digest(payload)] * 2
    first.verify(hashes[0], len(payload))
    assert [path.name for path in first.root.iterdir()] == [hashes[0]]


@pytest.mark.parametrize("sha", ["../outside", "/tmp/file", "A" * 64, "a" * 63, "a" * 64 + "/x"])
def test_no_user_path_can_be_used_as_an_object_name(tmp_path, sha):
    with pytest.raises(RemoteError, match="invalid_digest"):
        BlobStore(tmp_path / "blobs").open_blob(sha)


def _symlink(link: Path, target: Path, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable for this Windows user")


def test_symlink_root_and_symlink_object_are_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    _symlink(link, outside, directory=True)
    with pytest.raises(RemoteError, match="unsafe_blob_path"):
        BlobStore(link)
    blobs = BlobStore(tmp_path / "blobs")
    target = outside / "important"
    target.write_bytes(b"leave alone")
    _symlink(blobs.root / digest(b"payload"), target)
    with pytest.raises(RemoteError, match="unsafe_blob_path"):
        blobs.put_bytes(b"payload", 100)
    assert target.read_bytes() == b"leave alone"

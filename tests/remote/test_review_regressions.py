"""Interleavings found during an independent protocol/storage review."""
from __future__ import annotations

import sqlite3
import uuid

from pixiv_novel_sync.remote.blobs import BlobStore
from pixiv_novel_sync.remote.contracts import canonical_bytes, digest, request_digest
from pixiv_novel_sync.remote.maintenance import collect_garbage
from pixiv_novel_sync.remote.store import RemoteStore


def test_delayed_gc_cannot_unlink_a_new_reference_to_the_same_hash(tmp_path, monkeypatch):
    store = RemoteStore(tmp_path / "control", "m_fixture")
    store.migrate()
    blobs = BlobStore(store.state_dir / "blobs")
    data = canonical_bytes({"challenge": "a" * 64, "protocol": 1, "renderer": "epub-v1"})
    sha = digest(data)
    later = 5000.0

    def install(now, enqueue=False):
        key = str(uuid.uuid4())
        reservation = store.reserve_blob("input", key, len(data), now)
        blobs.put_bytes(data, 4096)
        store.register_input(sha, {"manifest": (sha, len(data))}, reservation, now)
        if enqueue:
            return store.enqueue(key, request_digest("diagnostic.v1", None), "diagnostic.v1", sha, now)

    install(1)
    import pixiv_novel_sync.remote.maintenance as maintenance
    unlink = maintenance._unlink
    interleaved = False
    new_run = None

    def delayed_unlink(target_blobs, name):
        nonlocal interleaved, new_run
        if name == sha and not interleaved:
            interleaved = True
            # Is another collector/writer allowed between the stale candidate
            # list and unlink? Detect an actual SQLite write exclusion without
            # timing-dependent sleeps or threads. If not excluded, exercise that
            # legal interleaving before the delayed first collector continues.
            other = sqlite3.connect(store.db_path, timeout=0, isolation_level=None)
            try:
                try:
                    other.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as error:
                    assert error.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                else:
                    other.rollback()
                    collect_garbage(store, blobs, later)
                    new_run = install(later, enqueue=True)
            finally:
                other.close()
        return unlink(target_blobs, name)

    monkeypatch.setattr(maintenance, "_unlink", delayed_unlink)
    collect_garbage(store, blobs, later)
    if new_run is None:
        new_run = install(later, enqueue=True)
    collect_garbage(store, blobs, later + 1)
    assert store.get_run(new_run["run_id"])["state"] == "queued"
    with blobs.open_blob(sha) as source:
        assert source.read() == data

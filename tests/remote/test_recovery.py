import hashlib
import io
import json
import os
import uuid

import pytest

from pixiv_novel_sync.remote.blobs import BlobStore
from pixiv_novel_sync.remote.contracts import RemoteError, digest
from pixiv_novel_sync.remote.maintenance import collect_garbage, recover
from pixiv_novel_sync.remote.store import RemoteStore
from pixiv_novel_sync.remote.epub_task import freeze_diagnostic


def setup_store(tmp_path):
    store = RemoteStore(tmp_path / "remote", "m_fixture")
    store.migrate()
    blobs = BlobStore(store.state_dir / "blobs")
    return store, blobs


def test_recover_revokes_old_epoch_but_preserves_pending_cloud_operation(tmp_path):
    store, blobs = setup_store(tmp_path)
    session = store.enroll(str(uuid.uuid4()), hashlib.sha256(b"s" * 32).hexdigest(), 1, "epub-v1", 1000)
    reservation = store.reserve_blob("input", "click", 65536, 1000)
    sha = freeze_diagnostic(blobs)
    with blobs.open_blob(sha) as source:
        size = len(source.read())
    store.register_input(sha, {"manifest": (sha, size)}, reservation, 1000)
    run = store.enqueue("click", "a" * 64, "diagnostic.v1", sha, 1000)
    with store.transaction() as conn:
        conn.execute("""UPDATE machine_control SET mode='ready',provider_state='running',runtime_ready=1,
                        runtime_budget_until=-1,last_provider_at=1000,gate_ready_until=1045""")
    grant = store.claim(session["session_id"], "claim", 1001)
    action = json.dumps({"action": "suspend", "operation_id": "op_pending"})
    with store.transaction() as conn:
        conn.execute("UPDATE machine_control SET action_json=?,owned_run_started_at=900,leader_until=1100", (action,))
    with pytest.raises(RemoteError, match="controller_active"):
        recover(store, 1002)
    result = recover(store, 1101)
    assert result["epoch"] != grant.epoch
    assert store.get_run(run["run_id"])["state"] == "queued"
    with pytest.raises(RemoteError):
        store.check_grant(grant, 1102)
    with pytest.raises(RemoteError):
        store.authenticate("s" * 32, 1102)
    with store.read() as conn:
        row = conn.execute("SELECT * FROM machine_control").fetchone()
        assert row["action_json"] == action
        assert row["owned_run_started_at"] is None and row["gate_ready_until"] is None


def test_crash_orphans_are_accounted_and_gc_is_repeatable(tmp_path):
    store, blobs = setup_store(tmp_path)
    data = b"orphaned result before DB commit"
    sha = blobs.put_bytes(data, 100)
    reserve = store.reserve_blob("result", "next-upload", 1, 1000)
    store.release_reservation(reserve, 1000)
    with store.read() as conn:
        assert conn.execute("SELECT size_bytes FROM blob_objects WHERE sha256=?", (sha,)).fetchone()[0] == len(data)
    assert collect_garbage(store, blobs, 1100)["blobs_removed"] == 0
    assert collect_garbage(store, blobs, 4601)["blobs_removed"] == 1
    assert collect_garbage(store, blobs, 4700)["blobs_removed"] == 0


def test_gc_preserves_active_snapshots_and_current_boot_tombstones(tmp_path):
    store, blobs = setup_store(tmp_path)
    manifest = b'{"protocol":1}'
    reservation = store.reserve_blob("input", "input", 100, 1000)
    sha = blobs.put_bytes(manifest, 100)
    store.register_input(sha, {"manifest": (sha, len(manifest))}, reservation, 1000)
    run = store.enqueue("click", "a" * 64, "diagnostic.v1", sha, 1000)
    store.enroll(str(uuid.uuid4()), "a" * 64, 1, "epub-v1", 1000)
    store.enroll(str(uuid.uuid4()), "b" * 64, 1, "epub-v1", 1001)
    # A still-live run with an old shared input remains a strong GC reference.
    with store.transaction() as conn:
        conn.execute("UPDATE runs SET deadline=1000000")
    collect_garbage(store, blobs, 100000)
    assert (blobs.root / sha).is_file()
    with store.read() as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2
    store.cancel(run["run_id"], 100001)
    assert collect_garbage(store, blobs, 100001 + 86401)["blobs_removed"] == 1


def test_deleting_blob_cannot_gain_a_new_input_reference(tmp_path):
    store, blobs = setup_store(tmp_path)
    data = b"frozen input"
    sha = blobs.put_bytes(data, 100)
    reservation = store.reserve_blob("input", "input", 100, 1000)
    with store.transaction() as conn:
        conn.execute("UPDATE blob_objects SET state='deleting' WHERE sha256=?", (sha,))
    with pytest.raises(RemoteError, match="blob_deleting"):
        store.register_input(sha, {"manifest": (sha, len(data))}, reservation, 1001)
    with pytest.raises(RemoteError, match="input_unavailable"):
        store.enqueue("click", "a" * 64, "diagnostic.v1", sha, 1001)


def test_gc_removes_only_known_stale_temporary_files(tmp_path):
    store, blobs = setup_store(tmp_path)
    old = blobs.root / ".upload-fixture"
    old.write_bytes(b"partial")
    os.utime(old, (100, 100))
    outside = tmp_path / "user-document.txt"
    outside.write_text("keep", encoding="utf-8")
    assert collect_garbage(store, blobs, 4000)["temporary_removed"] == 1
    assert outside.read_text(encoding="utf-8") == "keep"


def test_gc_rejects_a_different_blob_root(tmp_path):
    store, _ = setup_store(tmp_path)
    with pytest.raises(RemoteError, match="gc_directory_mismatch"):
        collect_garbage(store, BlobStore(tmp_path / "different"), 1000)


def test_gc_does_not_mark_orphans_or_remove_temporaries_during_active_upload(tmp_path):
    store, blobs = setup_store(tmp_path)
    payload = b"orphan waiting for active upload registration"
    sha = blobs.put_bytes(payload, 100)
    first = store.reserve_blob("result", "first", 1, 1000)
    store.release_reservation(first, 1000)
    temporary = blobs.root / ".upload-fixture-active"
    temporary.write_bytes(b"partial")
    os.utime(temporary, (100, 100))
    active = store.reserve_blob("result", "in-flight", 100, 4600)
    result = collect_garbage(store, blobs, 4601)
    assert result["blobs_removed"] == result["temporary_removed"] == 0
    assert (blobs.root / sha).is_file() and temporary.is_file()
    with store.read() as conn:
        assert conn.execute("SELECT state FROM blob_objects WHERE sha256=?", (sha,)).fetchone()[0] == "ready"
    store.release_reservation(active, 4602)
    result = collect_garbage(store, blobs, 4603)
    assert result["blobs_removed"] == result["temporary_removed"] == 1


def test_gc_resumes_deleting_marker_after_interruption_without_touching_live_input(tmp_path, monkeypatch):
    from pixiv_novel_sync.remote import maintenance

    store, blobs = setup_store(tmp_path)
    orphan = blobs.put_bytes(b"orphan", 100)
    reservation = store.reserve_blob("input", "live-input", 65536, 1000)
    live = freeze_diagnostic(blobs)
    with blobs.open_blob(live) as source:
        size = len(source.read())
    store.register_input(live, {"manifest": (live, size)}, reservation, 1000)
    store.enqueue("live-run", "a" * 64, "diagnostic.v1", live, 1000)
    with store.transaction() as conn:
        conn.execute("UPDATE runs SET deadline=100000")
    real_unlink = maintenance._unlink

    def crash_before_unlink(blobs, name):
        raise RuntimeError("fixture crash after mark")

    monkeypatch.setattr(maintenance, "_unlink", crash_before_unlink)
    with pytest.raises(RuntimeError, match="fixture crash"):
        collect_garbage(store, blobs, 4601)
    with store.read() as conn:
        assert conn.execute("SELECT state FROM blob_objects WHERE sha256=?", (orphan,)).fetchone()[0] == "deleting"
    assert (blobs.root / orphan).is_file() and (blobs.root / live).is_file()
    monkeypatch.setattr(maintenance, "_unlink", real_unlink)
    assert collect_garbage(store, blobs, 4602)["blobs_removed"] == 1
    assert (blobs.root / live).is_file()
    assert collect_garbage(store, blobs, 4603)["blobs_removed"] == 0

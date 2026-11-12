import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import uuid

import pytest

from pixiv_novel_sync.remote.contracts import RemoteError
from pixiv_novel_sync.remote.store import RemoteStore
from pixiv_novel_sync.remote.blobs import BlobStore
from pixiv_novel_sync.remote.epub_task import freeze_diagnostic


def ready_store(root, now=1000):
    store = RemoteStore(root, "m_fixture")
    store.migrate()
    session = store.enroll(str(uuid.uuid4()), hashlib.sha256(b"s" * 32).hexdigest(), 1, "epub-v1", now)
    with store.transaction() as conn:
        conn.execute("""UPDATE machine_control SET mode='ready', provider_state='running',runtime_ready=1,
                     last_provider_at=?,gate_ready_until=?,runtime_budget_until=-1""", (now, now + 45))
    return store, session


def enqueue(store, key="click-1", now=1000):
    blobs = BlobStore(store.state_dir / "blobs")
    reservation = store.reserve_blob("input", key, 65536, now)
    sha = freeze_diagnostic(blobs)
    with blobs.open_blob(sha) as source:
        size = len(source.read())
    store.register_input(sha, {"manifest": (sha, size)}, reservation, now)
    return store.enqueue(key, "a" * 64, "diagnostic.v1", sha, now)


def test_submission_survives_reopen_and_idempotency_conflict(tmp_path):
    store, _ = ready_store(tmp_path / "queue")
    first = enqueue(store)
    epoch = store.epoch
    store.close()
    reopened = RemoteStore(tmp_path / "queue", "m_fixture")
    reopened.migrate()
    assert reopened.epoch == epoch
    assert reopened.find_submission("click-1", "a" * 64)["run_id"] == first["run_id"]
    with pytest.raises(RemoteError, match="idempotency_conflict"):
        reopened.find_submission("click-1", "c" * 64)


def test_two_connections_cannot_claim_two_slots(tmp_path):
    store, session = ready_store(tmp_path / "queue")
    enqueue(store)
    enqueue(store, "second")
    barrier = Barrier(2)

    def claim(key):
        other = RemoteStore(store.state_dir, "m_fixture")
        other.migrate()
        barrier.wait(timeout=5)
        return other.claim(session["session_id"], key, 1001)

    with ThreadPoolExecutor(max_workers=2) as pool:
        grants = list(pool.map(claim, ("claim-1", "claim-2")))
    assert len([grant for grant in grants if grant]) == 1


def test_claim_replay_and_heartbeat_replay_do_not_extend_lease(tmp_path):
    store, session = ready_store(tmp_path / "queue")
    enqueue(store)
    grant = store.claim(session["session_id"], "claim-1", 1001)
    assert store.claim(session["session_id"], "claim-1", 1002).attempt_id == grant.attempt_id
    first = store.renew(grant, 1, 1010)
    assert store.renew(grant, 1, 1100) == first
    with pytest.raises(RemoteError, match="heartbeat_out_of_order"):
        store.renew(grant, 0, 1110)
    with pytest.raises(RemoteError, match="lease_expired"):
        store.renew(grant, 2, first["lease_until"])


def test_cancel_revokes_active_grant_and_terminal_claim_cannot_take_next(tmp_path):
    store, session = ready_store(tmp_path / "queue")
    run = enqueue(store)
    grant = store.claim(session["session_id"], "claim-1", 1001)
    store.cancel(run["run_id"], 1002)
    with pytest.raises(RemoteError):
        store.check_grant(grant, 1003)
    with pytest.raises(RemoteError, match="claim_terminal") as error:
        store.claim(session["session_id"], "claim-1", 1003)
    assert error.value.run_id == run["run_id"]


def test_retry_limit_and_deadline_prevent_infinite_demand(tmp_path):
    store, session = ready_store(tmp_path / "queue")
    run = enqueue(store)
    for attempt in range(1, 4):
        now = 1000 + (attempt - 1) * 250
        store.session_heartbeat(session["session_id"], now)
        with store.transaction() as conn:
            conn.execute("UPDATE machine_control SET last_provider_at=?,gate_ready_until=?", (now, now + 45))
        grant = store.claim(session["session_id"], f"claim-{attempt}", now)
        assert grant is not None
        store.sweep(now + 180)
    assert store.get_run(run["run_id"])["state"] == "failed"
    queued = enqueue(store, "deadline", 2000)
    store.sweep(5600)
    assert store.get_run(queued["run_id"])["state"] == "expired"


def test_unknown_budget_and_stale_provider_close_gate(tmp_path):
    store, session = ready_store(tmp_path / "queue")
    enqueue(store)
    with store.transaction() as conn:
        conn.execute("UPDATE machine_control SET runtime_budget_until=NULL")
    assert store.claim(session["session_id"], "a", 1001) is None
    with store.transaction() as conn:
        conn.execute("UPDATE machine_control SET runtime_budget_until=-1")
    assert store.claim(session["session_id"], "b", 1046) is None


def test_migration_rejects_unknown_schema_and_machine_rebinding(tmp_path):
    store, _ = ready_store(tmp_path / "queue")
    with pytest.raises(RemoteError, match="machine_mismatch"):
        RemoteStore(store.state_dir, "m_other").migrate()
    with sqlite3.connect(store.state_dir / "control.db") as conn:
        conn.execute("PRAGMA user_version=99")
    with pytest.raises(RemoteError, match="unsupported_control_schema"):
        store.migrate()


def test_quota_reservations_are_atomic_and_concurrent_owner_rejected(tmp_path):
    store, _ = ready_store(tmp_path / "queue")
    store.reserve_blob("input", "click", 21 * 1024 * 1024, 1000)
    with pytest.raises(RemoteError, match="upload_in_progress"):
        store.reserve_blob("input", "click", 1, 1001)
    with store.transaction() as conn:
        conn.execute("INSERT INTO blob_objects VALUES(?,?,?,'input','ready')", ("c" * 64, 1024**3 - 1, 1000))
    with pytest.raises(RemoteError, match="storage_quota"):
        store.reserve_blob("result", "new-owner", 10, 1001)


def test_full_result_quota_preserves_queued_run_and_attempt_budget(tmp_path):
    store, session = ready_store(tmp_path / "queue")
    run = enqueue(store)
    with store.transaction() as conn:
        conn.execute("INSERT INTO blob_objects VALUES(?,?,?,'result','ready')", ("c" * 64, 1024**3, 1000))
    assert store.claim(session["session_id"], "first", 1001) is None
    assert store.get_run(run["run_id"])["state"] == "queued"
    assert store.get_run(run["run_id"])["attempt_count"] == 0
    with store.transaction() as conn:
        conn.execute("DELETE FROM blob_objects WHERE sha256=?", ("c" * 64,))
    assert store.claim(session["session_id"], "first", 1002).run_id == run["run_id"]


def test_unwritable_receiver_never_allocates_attempt_and_replay_remains_valid(tmp_path, monkeypatch):
    store, session = ready_store(tmp_path / "queue")
    run = enqueue(store)
    probe = store._probe_receiver

    def denied():
        raise OSError("write denied")

    monkeypatch.setattr(store, "_probe_receiver", denied)
    assert store.claim(session["session_id"], "first", 1001) is None
    assert store.get_run(run["run_id"])["attempt_count"] == 0
    monkeypatch.setattr(store, "_probe_receiver", probe)
    grant = store.claim(session["session_id"], "first", 1002)
    monkeypatch.setattr(store, "_probe_receiver", denied)
    assert store.claim(session["session_id"], "first", 1003).attempt_id == grant.attempt_id

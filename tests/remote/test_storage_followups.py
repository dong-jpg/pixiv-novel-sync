"""Authority and persistence regressions from the collaboration follow-up."""
from __future__ import annotations

import uuid

import pytest

from pixiv_novel_sync.remote.contracts import RemoteError, digest
from tests.remote.test_store import enqueue, ready_store


@pytest.mark.parametrize("invalid_now", [float("nan"), float("inf"), -float("inf"), True])
def test_invalid_clock_cannot_authorize_or_renew_an_existing_lease(tmp_path, invalid_now):
    store, session = ready_store(tmp_path / "queue")
    enqueue(store)
    grant = store.claim(session["session_id"], "first", 1000)
    assert grant is not None

    operations = (
        lambda: store.authenticate("s" * 32, invalid_now),
        lambda: store.check_grant(grant, invalid_now),
        lambda: store.session_heartbeat(session["session_id"], invalid_now),
        lambda: store.renew(grant, 1, invalid_now),
    )
    for operation in operations:
        with pytest.raises(RemoteError, match="invalid_time"):
            operation()

    # A rejected clock must not turn a bounded session or attempt into an
    # infinite/NULL deadline, nor consume its next heartbeat sequence.
    with store.read() as conn:
        attempt = conn.execute("SELECT lease_until,heartbeat_seq FROM attempts").fetchone()
        saved_session = conn.execute("SELECT expires_at,last_seen_at FROM sessions").fetchone()
    assert tuple(attempt) == (1180, 0)
    assert tuple(saved_session) == (1900, 1000)


@pytest.mark.parametrize("invalid_now", [float("nan"), float("inf"), -float("inf"), True])
def test_invalid_clock_cannot_revoke_authority_or_rotate_an_active_controller(tmp_path, invalid_now):
    store, session = ready_store(tmp_path / "queue")
    run = enqueue(store)
    grant = store.claim(session["session_id"], "first", 1000)
    original_epoch = store.epoch
    with store.transaction() as conn:
        conn.execute("UPDATE machine_control SET leader_id='current-controller',leader_until=1100")

    operations = (
        lambda: store.sweep(invalid_now),
        lambda: store.cancel(run["run_id"], invalid_now),
        lambda: store.rotate_epoch(invalid_now),
        lambda: store.enroll(str(uuid.uuid4()), digest(b"replacement"), 1, "epub-v1", invalid_now),
    )
    for operation in operations:
        with pytest.raises(RemoteError, match="invalid_time"):
            operation()

    assert store.epoch == original_epoch
    assert store.get_run(run["run_id"])["state"] == "active"
    store.check_grant(grant, 1001)
    with store.read() as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


@pytest.mark.parametrize("invalid_now", [float("nan"), float("inf"), -float("inf"), True])
def test_invalid_clock_cannot_create_or_release_a_quota_reservation(tmp_path, invalid_now):
    store, _ = ready_store(tmp_path / "queue")
    reservation = store.reserve_blob("result", "existing-upload", 4096, 1000)
    with pytest.raises(RemoteError, match="invalid_time"):
        store.reserve_blob("result", "other-upload", 4096, invalid_now)
    with pytest.raises(RemoteError, match="invalid_time"):
        store.release_reservation(reservation, invalid_now)
    with store.read() as conn:
        reservations = conn.execute("SELECT reservation_id,expires_at FROM blob_reservations").fetchall()
    assert [tuple(row) for row in reservations] == [(reservation, 1180)]

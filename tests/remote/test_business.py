import io
import json
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.pixiv_fetch import fetch_novel, serialize_novel
from pixiv_novel_sync.remote.account_scope import acquire_account
from pixiv_novel_sync.remote.blobs import BlobStore
from pixiv_novel_sync.remote.business import BusinessTasks
from pixiv_novel_sync.remote.business_runner import fetch_remote_novel
from pixiv_novel_sync.remote.contracts import RemoteError, digest
from pixiv_novel_sync.remote.results import ResultService
from tests.test_pixiv_fetch import Api
from .test_store import ready_store, enqueue


@pytest.fixture
def business(tmp_path):
    store, session = ready_store(tmp_path / "control")
    blobs = BlobStore(store.state_dir / "blobs")
    clock = SimpleNamespace(now=1001.0)
    scope = "a" * 64
    owner = acquire_account(store, scope, 2, clock.now)
    service = BusinessTasks(store, blobs, secret_key="S" * 32, clock=lambda: clock.now)
    with store.transaction() as conn:
        conn.execute(
            'UPDATE sessions SET task_kinds_json=\'["diagnostic.v1","epub.single.v1","pixiv.novel.v1"]\''
        )
    run = service.submit_novel(42, "A" * 32, scope, owner, "fetch-42")
    grant = store.claim(session["session_id"], "claim-business", clock.now)
    assert grant is not None
    return SimpleNamespace(
        store=store,
        blobs=blobs,
        clock=clock,
        scope=scope,
        owner=owner,
        service=service,
        session=session,
        run=run,
        grant=grant,
    )


def test_business_payload_excludes_credentials_and_reuses_durable_submission(business):
    f = business
    with f.blobs.open_blob(f.grant.input_sha256) as source:
        manifest = source.read()
    assert b"A" * 32 not in manifest and b"access_token" not in manifest
    with f.store.read() as conn:
        encrypted = bytes(conn.execute("SELECT encrypted FROM business_secrets").fetchone()[0])
    assert b"A" * 32 not in encrypted
    assert f.service._upstream_credentials(f.grant) == {"access_token": "A" * 32}
    assert f.service.submit_novel(42, "B" * 32, f.scope, f.owner, "fetch-42")["run_id"] == f.run["run_id"]
    with pytest.raises(RemoteError, match="idempotency_conflict"):
        f.service.submit_novel(43, "A" * 32, f.scope, f.owner, "fetch-42")


def test_permits_share_spacing_and_account_exclusivity(business):
    f = business
    with pytest.raises(RemoteError, match="account_busy"):
        acquire_account(f.store, f.scope, 1, f.clock.now)
    assert f.service.permit(f.grant, 1) == {"allowed": True, "retry_after": 0}
    assert f.service.permit(f.grant, 1)["allowed"]
    assert f.service.permit(f.grant, 2) == {"allowed": False, "retry_after": 2}
    f.clock.now += 2
    assert f.service.permit(f.grant, 2)["allowed"]
    with pytest.raises(RemoteError):
        f.service.permit(f.grant, 3)


def test_cancel_revokes_credentials_and_result_publication(business):
    f = business
    f.store.cancel(f.run["run_id"], f.clock.now)
    for operation in (lambda: f.service._upstream_credentials(f.grant), lambda: f.service.permit(f.grant, 1)):
        with pytest.raises(RemoteError):
            operation()
    output = serialize_novel(fetch_novel(Api(), 42))
    with pytest.raises(RemoteError):
        ResultService(f.store, f.blobs).stage(
            f.grant, io.BytesIO(output), len(output), digest(output), f.clock.now
        )


def test_business_result_validates_identity_and_publishes_idempotently(business):
    f = business
    dto = fetch_novel(Api(), 42)
    output = serialize_novel(dto)
    results = ResultService(f.store, f.blobs)
    results.stage(f.grant, io.BytesIO(output), len(output), digest(output), f.clock.now)
    first = results.publish(f.grant, digest(output), len(output), f.clock.now)
    assert results.publish(f.grant, digest(output), len(output), f.clock.now) == first
    assert f.service.result(f.run["run_id"]) == dto
    f.store.sweep(f.clock.now)
    with f.store.read() as conn:
        assert not conn.execute("SELECT * FROM business_secrets").fetchall()


def test_worker_fetches_only_selected_novel_and_obtains_two_permits(business):
    f = business
    calls = []
    api = Api()
    api.set_auth = lambda token: calls.append(("auth", len(token)))
    api.requests = SimpleNamespace(close=lambda: calls.append(("closed", True)))
    result = fetch_remote_novel(
        f.service.manifest(f.grant),
        {"access_token": "A" * 32},
        lambda ordinal: calls.append(("permit", ordinal)),
        api_factory=lambda **kw: api,
    )
    assert api.calls == [("detail", 42), ("text", 42)]
    assert calls == [("auth", 32), ("permit", 1), ("permit", 2), ("closed", True)]
    assert b"access_token" not in result


def test_legacy_worker_cannot_claim_business_task(tmp_path):
    store, session = ready_store(tmp_path / "control")
    blobs = BlobStore(store.state_dir / "blobs")
    owner = acquire_account(store, "a" * 64, 1, 1001)
    tasks = BusinessTasks(store, blobs, secret_key="S" * 32, clock=lambda: 1001)
    tasks.submit_novel(42, "A" * 32, "a" * 64, owner, "fetch")
    assert store.claim(session["session_id"], "legacy", 1001) is None
    diagnostic = enqueue(store, "diagnostic", 1001)
    assert store.claim(session["session_id"], "diagnostic", 1001).run_id == diagnostic["run_id"]


def test_business_schema_reopen_keeps_existing_attempts_and_references(tmp_path):
    store, session = ready_store(tmp_path / "control")
    old = enqueue(store)
    grant = store.claim(session["session_id"], "first", 1001)
    store.migrate()
    assert store.get_run(old["run_id"])["state"] == "active"
    assert store.claim(session["session_id"], "first", 1001) == grant
    with store.read() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

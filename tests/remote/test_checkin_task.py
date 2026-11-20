from dataclasses import replace
from datetime import timedelta
import json

import pytest

from pixiv_novel_sync.remote.checkin_task import execute, permit_submission, SubmitGateway
from pixiv_novel_sync.remote.checkin_status import GatewayObservation, CheckinStatusRequest
from pixiv_novel_sync.remote.contracts import RemoteError, canonical_bytes, digest
from .test_store import ready_store
from .test_checkin_status import (
    NOW,
    KEY,
    SESSION,
    Gateway,
    request_body,
    MemoryStore,
    TransportFactory,
    SyntheticAdapter,
)
from pixiv_novel_sync.remote.blobs import BlobStore


def enqueue_checkin(store, session, kind="checkin.submit.v1", key="one", request=None):
    from pixiv_novel_sync.remote.contracts import KINDS

    manifest = {"protocol": 1, "kind": kind, "request": request or request_body()}
    body = canonical_bytes(manifest)
    blobs = BlobStore(store.state_dir / "blobs")
    sha = blobs.put_bytes(body, 65536)
    reservation = store.reserve_blob("input", key, len(body), NOW.timestamp())
    store.register_input(sha, {"manifest": (sha, len(body))}, reservation, NOW.timestamp())
    run = store.enqueue(key, digest(body), kind, sha, NOW.timestamp())
    with store.transaction() as conn:
        conn.execute("UPDATE sessions SET task_kinds_json=?", (json.dumps(sorted(KINDS)),))
    grant = store.claim(session["session_id"], key, NOW.timestamp())
    return manifest, run, grant


def test_submit_once_across_tasks_and_result_unknown_retains_ledger(tmp_path):
    store, session = ready_store(tmp_path / "queue", NOW.timestamp())
    manifest, run, grant = enqueue_checkin(store, session)
    assert permit_submission(store, grant, manifest, NOW.timestamp())["allowed"]
    assert permit_submission(store, grant, manifest, NOW.timestamp())["allowed"]
    store.cancel(run["run_id"], NOW.timestamp())
    manifest2, _, grant2 = enqueue_checkin(store, session, key="two")
    with pytest.raises(RemoteError, match="checkin_already_submitted"):
        permit_submission(store, grant2, manifest2, NOW.timestamp())
    with store.read() as conn:
        assert conn.execute("SELECT state FROM checkin_effects").fetchone()[0] == "submitted"
    with pytest.raises(RemoteError, match="checkin_business_day_changed"):
        permit_submission(store, grant2, manifest2, (NOW + timedelta(days=1)).timestamp())


@pytest.mark.parametrize("result", ["confirmed", "unknown"])
def test_side_effect_only_after_due_and_identity_and_confirmation(result):
    class Submit(Gateway):
        submissions = 0

        def submit_once(self, request, permit):
            permit(1)
            self.submissions += 1
            if result == "confirmed":
                self.observation = GatewayObservation("success", request.business_day, True, "live_status")

    gateway = Submit()
    permits = []
    manifest = {"protocol": 1, "kind": "checkin.submit.v1", "request": request_body()}
    value = json.loads(
        execute(manifest, None, lambda n: permits.append(n), gateway=gateway, clock=lambda: NOW)
    )
    assert value["status"] == ("already_done" if result == "confirmed" else "uncertain")
    assert gateway.submissions == 1 and permits == [1]


def test_fixed_runner_submits_only_after_permit_and_never_logs_in():
    calls = []

    class Adapter(SyntheticAdapter):
        def submit(self, day, **kwargs):
            calls.append("submit")
            self.transport.request("POST", "/api/user/checkin")
            return type("Result", (), {"state": "success", "day": day})()

    class Runner:
        def __init__(self, store, transport_factory, solver=None, clock=None, adapter_factory=None):
            assert solver is None
            self.store, self.transport_factory, self.adapter_factory = (
                store,
                transport_factory,
                adapter_factory,
            )

        def run(self, ids, execute=False):
            assert execute
            site = self.store.get_site(ids[0])
            with self.transport_factory.open(site) as transport:
                adapter = self.adapter_factory(site, transport)
                adapter.identity()
                adapter.status("2026-11-12")
                adapter.submit("2026-11-12")

    transports = TransportFactory()
    gateway = SubmitGateway(
        store=MemoryStore(),
        transport_factory=transports,
        identity_key=KEY,
        session_bindings={"fixture-site": SESSION},
        clock=lambda: NOW,
        runner_factory=Runner,
        adapter_factory=Adapter,
    )
    gateway.submit_once(CheckinStatusRequest.from_dict(request_body()), lambda _: calls.append("permit"))
    assert calls == ["permit", "submit"]
    assert transports.transport.methods == ["GET", "GET", "POST"]
    assert transports.transport.logins == 0

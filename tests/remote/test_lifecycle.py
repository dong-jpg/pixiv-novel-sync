from __future__ import annotations

import json
import uuid
from dataclasses import replace

import pytest

from pixiv_novel_sync.remote.blobs import BlobStore
from pixiv_novel_sync.remote.contracts import RemoteError, digest, request_digest
from pixiv_novel_sync.remote.control_store import ControlStore
from pixiv_novel_sync.remote.epub_task import freeze_diagnostic
from pixiv_novel_sync.remote.instabox import MachineView, OperationView, ProviderError
from pixiv_novel_sync.remote.lifecycle import Controller
from pixiv_novel_sync.remote.store import RemoteStore


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class FakeProvider:
    def __init__(self, clock):
        self.clock = clock
        self.view = MachineView("m_fixture", "suspended", None, False, 480, None, None)
        self.operations = {}
        self.receipts = {}
        self.calls = []
        self.mutations = []
        self.wake_count = 0
        self.fail_next = None
        self.lose_response = False

    def machine(self):
        self.calls.append(("machine", self.clock.now))
        self._fail()
        return self.view

    def _fail(self):
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            raise error

    def mutate(self, action, key):
        self.calls.append((action, self.clock.now))
        self.mutations.append((action, key, self.clock.now))
        self._fail()
        if key in self.receipts:
            return self.receipts[key]
        operation = OperationView("op_" + uuid.uuid4().hex, "m_fixture", action, "queued")
        self.operations[operation.id] = operation
        self.receipts[key] = operation
        self.view = replace(self.view, state={"start": "starting", "resume": "resuming", "suspend": "suspending"}[action],
                            runtime_ready=False, operation_id=operation.id)
        if self.lose_response:
            self.lose_response = False
            raise ProviderError("provider_unreachable")
        return operation

    def operation(self, operation_id):
        self.calls.append(("operation", self.clock.now))
        self._fail()
        if operation_id not in self.operations:
            raise ProviderError("not_found", 404)
        return self.operations[operation_id]

    def wake(self):
        self.calls.append(("wake", self.clock.now))
        self.wake_count += 1
        self._fail()
        return self.view

    def finish(self, ready=True):
        operation = self.operations[self.view.operation_id]
        self.operations[operation.id] = replace(operation, state="succeeded")
        if operation.action == "suspend":
            self.view = replace(self.view, state="suspended", runtime_ready=False,
                                run_started_at=None, operation_id=None)
        else:
            self.view = replace(self.view, state="running", runtime_ready=ready,
                                run_started_at=self.clock.now, operation_id=None)


class Fixture:
    def __init__(self, root):
        self.clock = Clock()
        self.store = RemoteStore(root / "state", "m_fixture")
        self.store.migrate()
        self.control = ControlStore(self.store)
        self.provider = FakeProvider(self.clock)
        self.controller = Controller(self.store, self.control, self.provider, self.clock, lambda: 0)
        self.blobs = BlobStore(root / "state" / "blobs")
        self.session_id = self.store.enroll(str(uuid.uuid4()), digest(b"session-test"), 1, "epub-v1", self.clock.now)["session_id"]

    def enqueue(self):
        sha = freeze_diagnostic(self.blobs)
        with self.blobs.open_blob(sha) as source:
            size = len(source.read())
        key = str(uuid.uuid4())
        reservation = self.store.reserve_blob("input", key, size, self.clock.now)
        self.store.register_input(sha, {"manifest": (sha, size)}, reservation, self.clock.now)
        return self.store.enqueue(key, request_digest("diagnostic.v1", None), "diagnostic.v1", sha, self.clock.now)

    def step(self, seconds=15, heartbeat=True):
        self.clock.now += seconds
        if heartbeat:
            self.store.session_heartbeat(self.session_id, self.clock.now)
        before = len(self.provider.calls)
        next_tick = self.controller.tick()
        assert len(self.provider.calls) - before <= 1
        assert next_tick > self.clock.now

    def until(self, predicate, limit=30):
        for _ in range(limit):
            if predicate():
                return
            self.step()
        raise AssertionError(self.control.snapshot(self.clock.now))

    def ready(self):
        run = self.enqueue()
        self.until(lambda: bool(self.provider.mutations))
        self.provider.finish()
        self.until(lambda: self.control.snapshot(self.clock.now)["mode"] == "ready")
        return run


@pytest.fixture
def f(tmp_path):
    fixture = Fixture(tmp_path)
    yield fixture
    fixture.store.close()


def test_no_demand_does_not_start_or_wake(f):
    for _ in range(4):
        f.step()
    assert f.provider.mutations == []
    assert f.provider.wake_count == 0


def test_hold_does_not_start_suspended_machine(f):
    f.control.set_hold(3600, f.clock.now)
    for _ in range(4):
        f.step()
    assert f.provider.mutations == []
    assert f.provider.wake_count == 0


def test_accepted_start_is_not_a_claim_gate(f):
    f.enqueue()
    f.until(lambda: bool(f.provider.mutations))
    assert f.control.snapshot(f.clock.now)["mode"] == "starting"
    assert f.store.claim(f.session_id, "before-ready", f.clock.now) is None
    for _ in range(4):
        f.step()
    assert len(f.provider.mutations) == 1


def test_runtime_and_worker_must_both_be_ready(f):
    f.enqueue()
    f.until(lambda: bool(f.provider.mutations))
    f.provider.finish(ready=False)
    for _ in range(4):
        f.step()
    assert f.store.claim(f.session_id, "runtime-not-ready", f.clock.now) is None
    f.provider.view = replace(f.provider.view, runtime_ready=True)
    f.step(seconds=61, heartbeat=False)
    assert f.store.claim(f.session_id, "worker-stale", f.clock.now) is None
    f.until(lambda: f.control.snapshot(f.clock.now)["mode"] == "ready")
    assert f.store.claim(f.session_id, "fully-ready", f.clock.now) is not None


@pytest.mark.parametrize("minutes,started", [(480, None), (0, 1000), (10, 1000)])
def test_missing_or_small_runtime_budget_does_not_allow_claim(f, minutes, started):
    f.provider.view = replace(f.provider.view, state="running", runtime_ready=True,
                              max_run_minutes=minutes, run_started_at=started)
    f.enqueue()
    for _ in range(3):
        f.step()
    assert f.store.claim(f.session_id, "budget", f.clock.now) is None


def test_existing_manual_run_is_not_owned_or_suspended(f):
    f.provider.view = replace(f.provider.view, state="running", runtime_ready=True, run_started_at=900)
    run = f.enqueue()
    f.until(lambda: f.control.snapshot(f.clock.now)["mode"] == "ready")
    assert f.control.snapshot(f.clock.now)["owned_run_started_at"] is None
    f.store.cancel(run["run_id"], f.clock.now)
    for _ in range(30):
        f.step()
    assert all(action != "suspend" for action, _, _ in f.provider.mutations)


def test_owned_activation_eventually_suspends_after_idle_grace(f):
    run = f.ready()
    owned = f.control.snapshot(f.clock.now)["owned_run_started_at"]
    assert owned == f.provider.view.run_started_at
    f.store.cancel(run["run_id"], f.clock.now)
    f.until(lambda: any(action == "suspend" for action, _, _ in f.provider.mutations), limit=35)
    assert f.control.snapshot(f.clock.now)["mode"] == "suspending"
    assert f.store.claim(f.session_id, "suspending", f.clock.now) is None
    f.provider.finish()
    f.until(lambda: f.control.snapshot(f.clock.now)["action"] is None)
    assert f.control.snapshot(f.clock.now)["provider_state"] == "suspended"


def test_changed_running_identity_relinquishes_pause_permission(f):
    run = f.ready()
    f.provider.view = replace(f.provider.view, run_started_at=f.clock.now + 1)
    f.store.cancel(run["run_id"], f.clock.now)
    for _ in range(30):
        f.step()
    assert f.control.snapshot(f.clock.now)["owned_run_started_at"] is None
    assert all(action != "suspend" for action, _, _ in f.provider.mutations)


@pytest.mark.parametrize("new_demand", ["hold", "task", "upload"])
def test_new_activity_while_draining_prevents_unsent_suspend(f, new_demand):
    run = f.ready()
    f.store.cancel(run["run_id"], f.clock.now)
    f.until(lambda: f.control.snapshot(f.clock.now)["mode"] == "draining", limit=35)
    if new_demand == "hold":
        f.control.set_hold(3600, f.clock.now)
    elif new_demand == "task":
        f.enqueue()
    else:
        f.store.reserve_blob("input", "upload-pending", 100, f.clock.now)
    for _ in range(3):
        f.step()
    assert all(action != "suspend" for action, _, _ in f.provider.mutations)


def test_new_task_after_suspend_sent_waits_for_operation(f):
    run = f.ready()
    f.store.cancel(run["run_id"], f.clock.now)
    f.until(lambda: any(action == "suspend" for action, _, _ in f.provider.mutations), limit=35)
    f.enqueue()
    f.control.set_hold(3600, f.clock.now)
    before = len(f.provider.mutations)
    for _ in range(3):
        f.step()
    assert len(f.provider.mutations) == before
    assert f.store.claim(f.session_id, "suspend-in-flight", f.clock.now) is None
    f.provider.finish()
    f.until(lambda: len(f.provider.mutations) > before)
    assert f.provider.mutations[-1][0] == "resume"


def test_response_loss_follows_known_operation_across_restart(f):
    f.enqueue()
    f.provider.lose_response = True
    f.until(lambda: bool(f.provider.mutations))
    f.provider.finish()
    # A restarted process waits for the former leader to expire, then follows
    # the same persisted intent. It never emits an opposite lifecycle request.
    f.controller = Controller(f.store, ControlStore(f.store), f.provider, f.clock, lambda: 0)
    f.until(lambda: f.control.snapshot(f.clock.now)["mode"] == "ready", limit=35)
    assert f.provider.mutations[0][1] == f.provider.mutations[-1][1]
    assert len({key for _, key, _ in f.provider.mutations}) == 1


def test_timeout_before_acceptance_reuses_original_idempotency_key(f):
    f.enqueue()
    f.step()
    f.provider.fail_next = ProviderError("provider_unreachable")
    f.until(lambda: bool(f.provider.mutations))
    f.until(lambda: len(f.provider.mutations) >= 2)
    assert f.provider.mutations[0][1] == f.provider.mutations[1][1]
    assert f.provider.mutations[1][2] - f.provider.mutations[0][2] >= 30


def test_cooldown_and_retry_after_are_respected(f):
    f.enqueue()
    f.provider.view = replace(f.provider.view, cooldown_until=f.clock.now + 900)
    for _ in range(10):
        f.step()
    assert f.provider.mutations == []
    assert f.control.snapshot(f.clock.now)["mode"] == "cooldown"
    assert f.control.snapshot(f.clock.now)["next_poll_at"] >= 1900


def test_authentication_failure_stops_mutations(f):
    f.enqueue()
    f.provider.fail_next = ProviderError("invalid_key", 401)
    for _ in range(10):
        f.step()
    assert f.provider.mutations == []
    assert f.control.snapshot(f.clock.now)["last_error"] == "provider_authentication"


def test_external_operation_blocks_claim_even_when_runtime_ready(f):
    f.enqueue()
    f.provider.view = replace(f.provider.view, state="running", runtime_ready=True,
                              run_started_at=900, operation_id="op_external")
    for _ in range(3):
        f.step()
    assert f.store.claim(f.session_id, "external-operation", f.clock.now) is None
    assert f.provider.mutations == []


def test_stale_leader_cannot_apply_late_mutation_receipt(f):
    f.enqueue()
    f.step()
    first_control = f.control
    action = first_control.reserve_action("resume", str(uuid.uuid4()), f.clock.now)
    first_control.prepare_send(action["action_id"], f.clock.now)
    f.clock.now += 61
    other = ControlStore(f.store)
    assert other.claim_leader("other-controller", f.clock.now)
    old_receipt = OperationView("op_late", "m_fixture", "resume", "queued")
    assert not first_control.record_action(action["action_id"], old_receipt, f.clock.now)
    assert other.snapshot(f.clock.now)["action"]["operation_id"] is None


def test_wake_interval_and_write_rate_are_persisted(f):
    f.provider.view = replace(f.provider.view, state="running", runtime_ready=True, run_started_at=900)
    f.control.set_hold(3600, f.clock.now)
    for _ in range(45):
        f.step()
    times = [at for action, at in f.provider.calls if action == "wake"]
    assert len(times) >= 2
    assert all(second - first >= 300 for first, second in zip(times, times[1:]))
    snapshot = f.control.snapshot(f.clock.now)
    assert snapshot["last_wake_at"] == times[-1]


def test_old_unknown_action_does_not_replay_after_idempotency_retention(f):
    f.enqueue()
    f.step()
    action = f.control.reserve_action("resume", str(uuid.uuid4()), f.clock.now)
    with f.store.transaction() as conn:
        action["created_at"] -= 7 * 86400 + 1
        action["phase"] = "unknown"
        action["last_sent_at"] = action["created_at"]
        conn.execute("UPDATE machine_control SET action_json=?", (json.dumps(action),))
    f.step()
    assert f.provider.mutations == []
    assert f.control.snapshot(f.clock.now)["last_error"] == "action_expired"


def test_uncertain_suspend_never_retries_against_a_new_manual_run(f):
    run = f.ready()
    f.store.cancel(run["run_id"], f.clock.now)
    f.until(lambda: f.control.snapshot(f.clock.now)["mode"] == "draining", limit=35)
    action = f.control.reserve_action("suspend", f.control.snapshot(f.clock.now)["activation_id"], f.clock.now)
    f.control.prepare_send(action["action_id"], f.clock.now)
    f.control.fail(ProviderError("provider_unreachable"), f.clock.now, action_id=action["action_id"])
    # Before the missing response can be reconciled, a different run appears.
    f.provider.view = replace(f.provider.view, run_started_at=f.clock.now + 1)
    for _ in range(5):
        f.step()
    assert all(name != "suspend" for name, _, _ in f.provider.mutations)
    assert f.control.snapshot(f.clock.now)["last_error"] == "suspend_uncertain"


def test_unknown_start_following_external_operation_does_not_gain_ownership(f):
    f.enqueue()
    f.provider.lose_response = True
    f.until(lambda: bool(f.provider.mutations))
    f.until(lambda: f.control.snapshot(f.clock.now)["action"]["operation_id"] is not None)
    f.provider.finish()
    f.until(lambda: f.control.snapshot(f.clock.now)["mode"] == "ready")
    assert f.control.snapshot(f.clock.now)["owned_run_started_at"] is None


def test_operation_conflict_is_polled_without_an_opposite_action(f):
    f.enqueue()
    f.step()
    external = OperationView("op_external", "m_fixture", "suspend", "queued")
    f.provider.operations[external.id] = external
    f.provider.fail_next = ProviderError("operation_conflict", 409, operation_id=external.id)
    f.until(lambda: bool(f.provider.mutations))
    for _ in range(4):
        f.step()
    assert len(f.provider.mutations) == 1
    assert f.control.snapshot(f.clock.now)["action"]["operation_id"] == external.id
    assert f.store.claim(f.session_id, "conflicting-operation", f.clock.now) is None
    f.provider.operations[external.id] = replace(external, state="succeeded")
    f.until(lambda: f.control.snapshot(f.clock.now)["action"] is None)
    assert f.control.snapshot(f.clock.now)["owned_run_started_at"] is None


def test_429_delays_reads_and_writes_without_extending_grants(f):
    f.enqueue()
    f.provider.fail_next = ProviderError("rate_limited", 429, retry_after=120)
    f.step()
    calls = len(f.provider.calls)
    for _ in range(7):
        f.step()
    assert len(f.provider.calls) == calls
    assert f.store.claim(f.session_id, "rate-limited", f.clock.now) is None
    f.step()
    assert len(f.provider.calls) == calls + 1


def test_non_cooldown_409_does_not_loop_activation_requests(f):
    f.enqueue()
    f.step()
    f.provider.fail_next = ProviderError("max_run_disabled", 409)
    f.until(lambda: bool(f.provider.mutations))
    for _ in range(10):
        f.step()
    assert len(f.provider.mutations) == 1
    assert f.control.snapshot(f.clock.now)["last_error"] == "provider_rejected"


def test_unknown_activation_does_not_send_again_without_business_demand(f):
    run = f.enqueue()
    f.step()
    f.provider.fail_next = ProviderError("provider_unreachable")
    f.until(lambda: bool(f.provider.mutations))
    f.store.cancel(run["run_id"], f.clock.now)
    for _ in range(6):
        f.step()
    assert len(f.provider.mutations) == 1
    assert f.control.snapshot(f.clock.now)["last_error"] == "activation_unconfirmed"


def test_disabled_controller_has_no_provider_effect(f):
    with f.store.transaction() as conn:
        conn.execute("UPDATE machine_control SET mode='disabled'")
    f.enqueue()
    f.step()
    assert f.provider.calls == []


def test_explicit_restart_after_recovery_reconciles_pending_suspend_first(f):
    run = f.ready()
    f.store.cancel(run["run_id"], f.clock.now)
    f.until(lambda: any(action == "suspend" for action, _, _ in f.provider.mutations), limit=35)
    f.clock.now += 61
    f.store.rotate_epoch(f.clock.now)
    assert f.control.snapshot(f.clock.now)["mode"] == "disabled"
    f.session_id = f.store.enroll(str(uuid.uuid4()), digest(b"replacement-session"), 1, "epub-v1", f.clock.now)["session_id"]
    f.enqueue()
    restored_action = f.control.snapshot(f.clock.now)["action"]["action_id"]
    f.controller = Controller(f.store, ControlStore(f.store), f.provider, f.clock, lambda: 0)
    f.step()
    snapshot = f.control.snapshot(f.clock.now)
    assert snapshot["action"]["action_id"] == restored_action
    assert snapshot["owned_run_started_at"] is None
    assert snapshot["mode"] == "suspending"
    assert f.store.claim(f.session_id, "recovered-suspend", f.clock.now) is None
    f.provider.finish()
    f.until(lambda: f.control.snapshot(f.clock.now)["action"] is None)


def test_existing_controller_does_not_reenable_a_later_disabled_state(f):
    f.step()
    before = len(f.provider.calls)
    with f.store.transaction() as conn:
        conn.execute("UPDATE machine_control SET mode='disabled',last_error='epoch_rotated'")
    f.step()
    assert f.control.snapshot(f.clock.now)["mode"] == "disabled"
    assert len(f.provider.calls) == before


def test_provider_failure_in_flight_cannot_undo_a_disable(f):
    f.enqueue()

    def disabled_during_request():
        with f.store.transaction() as conn:
            conn.execute("UPDATE machine_control SET mode='disabled'")
        raise ProviderError("provider_unreachable")

    f.provider.machine = disabled_during_request
    f.step()
    assert f.control.snapshot(f.clock.now)["mode"] == "disabled"
    assert f.control.snapshot(f.clock.now)["gate_ready_until"] is None
    assert f.provider.mutations == []


@pytest.mark.parametrize("seconds", [-1, 14401, True, "3600"])
def test_hold_limits_are_strict(f, seconds):
    with pytest.raises(RemoteError):
        f.control.set_hold(seconds, f.clock.now)

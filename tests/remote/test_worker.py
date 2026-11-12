from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
import threading
import uuid

import pytest
import requests

from pixiv_novel_sync.remote.contracts import Grant, RemoteError, canonical_bytes, digest
from pixiv_novel_sync.remote.runner import run_child
from pixiv_novel_sync.remote.worker import LeaseGuard, Worker
from pixiv_novel_sync.remote.worker_http import WorkerHTTP


def test_delayed_renew_response_cannot_grant_extra_time():
    guard = LeaseGuard()
    guard.accept(sent_monotonic=100.0, received_monotonic=219.0)
    assert guard.can_work(219.0)
    assert not guard.can_work(220.0)


def test_response_after_safe_budget_is_not_work_permission():
    guard = LeaseGuard()
    guard.accept(sent_monotonic=100.0, received_monotonic=225.0)
    assert not guard.can_work(225.0)


def test_guard_rejects_clock_order_and_new_attempt_starts_without_permission():
    guard = LeaseGuard()
    with pytest.raises(ValueError, match="invalid_monotonic_order"):
        guard.accept(sent_monotonic=100.0, received_monotonic=99.0)
    assert not guard.can_work(0.0)


def test_retrying_same_heartbeat_timestamp_does_not_extend_stop_budget():
    guard = LeaseGuard()
    guard.accept(sent_monotonic=100.0, received_monotonic=101.0)
    guard.accept(sent_monotonic=100.0, received_monotonic=150.0)
    assert guard.safe_until == 220.0


class Clock:
    def __init__(self):
        self.wall, self.mono = 1000.0, 100.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class ImmediateHandle:
    def poll(self):
        return 0

    def stop(self):
        pass


class InlineRunner:
    def __init__(self):
        self.count = 0
        self.started = threading.Event()

    def start(self, *args):
        self.count += 1
        run_child(*args)
        self.started.set()
        return ImmediateHandle()


class HangingHandle:
    def __init__(self):
        self.stopped = threading.Event()

    def poll(self):
        return -15 if self.stopped.is_set() else None

    def stop(self):
        self.stopped.set()


class HangingRunner(InlineRunner):
    def __init__(self):
        super().__init__()
        self.handle = HangingHandle()

    def start(self, *args):
        self.count += 1
        self.started.set()
        return self.handle


class FakeClient:
    def __init__(self, clock):
        self.clock = clock
        self.boots, self.claims, self.renewals, self.failures = [], [], [], []
        self.heartbeats = 0
        self.manifest = canonical_bytes({"challenge": "a" * 64, "protocol": 1, "renderer": "epub-v1"})
        self.grant = Grant(*(str(uuid.uuid4()) for _ in range(4)), 1, digest(self.manifest),
                           "diagnostic.v1", clock.wall + 180, clock.wall + 600, clock.wall)
        self.claim_cache = {}
        self.renew_cache = {}
        self.lost_claim = self.lost_publish = self.lost_renew = False
        self.published = None
        self.claim_delay = 0
        self.bad_seq = False

    def enroll(self, boot_id, secret):
        self.boots.append((boot_id, secret))
        return {"session_id": self.grant.session_id, "epoch": self.grant.epoch,
                "expires_at": self.clock.wall + 900, "server_now": self.clock.wall}

    def reset_session(self):
        pass

    def close(self):
        pass

    def claim(self, key):
        self.claims.append(key)
        if key not in self.claim_cache:
            if self.claim_cache:
                return None
            self.claim_cache[key] = self.grant
        if self.claim_delay:
            self.clock.advance(self.claim_delay)
        if self.lost_claim:
            self.lost_claim = False
            raise RemoteError("worker_transient", 503)
        return replace(self.claim_cache[key], server_now=self.clock.wall)

    def session_heartbeat(self):
        self.heartbeats += 1
        return {"server_now": self.clock.wall, "expires_at": self.clock.wall + 900}

    def renew(self, grant, seq):
        self.renewals.append(seq)
        if seq not in self.renew_cache:
            self.renew_cache[seq] = {"seq": seq, "lease_until": min(self.clock.wall + 180, grant.hard_deadline)}
        if self.lost_renew:
            self.lost_renew = False
            raise RemoteError("worker_transient", 503)
        return {**self.renew_cache[seq], "seq": seq + 1 if self.bad_seq else seq, "server_now": self.clock.wall}

    def fetch_input(self, grant, part):
        assert part == "manifest"
        return self.manifest

    def stage(self, grant, path, sha256, size):
        assert digest(path.read_bytes()) == sha256
        assert path.stat().st_size == size
        return {"sha256": sha256, "size_bytes": size, "server_now": self.clock.wall}

    def publish(self, grant, sha256, size):
        self.published = {"state": "succeeded", "run_id": grant.run_id, "attempt_id": grant.attempt_id,
                          "sha256": sha256, "size_bytes": size, "server_now": self.clock.wall}
        if self.lost_publish:
            self.lost_publish = False
            raise RemoteError("worker_transient", 503)
        return self.published

    def receipt(self, run_id):
        return self.published or {"run_id": run_id, "state": "active", "server_now": self.clock.wall}

    def fail(self, grant, code):
        self.failures.append(code)
        return {"run_id": grant.run_id, "state": "failed", "server_now": self.clock.wall}


def make_worker(tmp_path, *, hanging=False, threaded=False):
    clock = Clock()
    client = FakeClient(clock)
    runner = HangingRunner() if hanging else InlineRunner()
    worker = Worker(client, tmp_path / "scratch", lambda: clock.wall, lambda: clock.mono,
                    runner, threaded=threaded)
    return worker, client, clock, runner


def test_worker_requires_published_receipt_then_cleans_only_own_directory(tmp_path):
    worker, client, _, runner = make_worker(tmp_path)
    worker.tick()  # handshake
    worker.tick()  # fixed pipeline
    worker.tick()  # observe durable acknowledgement
    assert runner.count == 1
    assert worker.completed == __import__("collections").deque([client.grant.run_id], maxlen=50)
    assert worker.state == "idle"
    assert list(worker.work_dir.iterdir()) == [worker.work_dir / ".pixiv-remote-worker"]
    worker.stop()


def test_publish_response_loss_uses_receipt_without_rerender(tmp_path):
    worker, client, _, runner = make_worker(tmp_path)
    client.lost_publish = True
    for _ in range(3):
        worker.tick()
    assert runner.count == 1
    assert worker.completed == __import__("collections").deque([client.grant.run_id], maxlen=50)
    assert client.failures == []
    worker.stop()


def test_lost_claim_response_retries_same_key_and_original_send_time(tmp_path):
    worker, client, clock, runner = make_worker(tmp_path, hanging=True, threaded=True)
    client.lost_claim = True
    worker.tick()
    worker.tick()
    clock.advance(60)
    worker.tick()
    assert runner.started.wait(2)
    assert client.claims[0] == client.claims[1]
    assert worker._attempt.guard.safe_until == 220
    worker.stop()


def test_delayed_claim_after_budget_never_starts_renderer(tmp_path):
    worker, client, _, runner = make_worker(tmp_path)
    client.claim_delay = 125
    worker.tick()
    worker.tick()
    assert runner.count == 0
    assert worker.active_grant is None
    assert worker.state == "registering"
    worker.stop()


def test_heartbeat_remains_independent_while_renderer_is_stuck(tmp_path):
    worker, client, clock, runner = make_worker(tmp_path, hanging=True, threaded=True)
    try:
        worker.tick()
        worker.tick()
        assert runner.started.wait(2)
        clock.advance(30)
        worker.tick()
        assert client.renewals == [1]
        assert client.heartbeats == 1
        clock.advance(120)
        worker.tick()
        assert runner.handle.stopped.is_set()
        assert worker.state in {"stopping", "registering"}
    finally:
        worker.stop()


def test_renew_retry_retains_original_timestamp_and_seq(tmp_path):
    worker, client, clock, runner = make_worker(tmp_path, hanging=True, threaded=True)
    try:
        worker.tick()
        worker.tick()
        assert runner.started.wait(2)
        client.lost_renew = True
        clock.advance(30)
        worker.tick()
        clock.advance(30)
        worker.tick()
        assert client.renewals == [1, 1]
        assert worker._attempt.guard.safe_until == 250
    finally:
        worker.stop()


def test_wrong_renew_sequence_stops_the_current_child(tmp_path):
    worker, client, clock, runner = make_worker(tmp_path, hanging=True, threaded=True)
    try:
        worker.tick()
        worker.tick()
        assert runner.started.wait(2)
        client.bad_seq = True
        clock.advance(30)
        worker.tick()
        assert runner.handle.stopped.is_set()
        assert not worker._attempt.guard.can_work(clock.mono + 120)
    finally:
        worker.stop()


def test_heartbeats_cannot_extend_ten_minute_hard_deadline(tmp_path):
    worker, client, clock, runner = make_worker(tmp_path, hanging=True, threaded=True)
    try:
        worker.tick()
        worker.tick()
        assert runner.started.wait(2)
        for _ in range(19):
            clock.advance(30)
            worker.tick()
        assert not runner.handle.stopped.is_set()
        clock.advance(30)
        worker.tick()
        assert runner.handle.stopped.is_set()
        assert len(client.renewals) == 19
    finally:
        worker.stop()


def test_wall_clock_pause_during_active_attempt_requires_new_handshake(tmp_path):
    worker, _, clock, runner = make_worker(tmp_path, hanging=True, threaded=True)
    try:
        worker.tick()
        worker.tick()
        assert runner.started.wait(2)
        clock.wall += 130  # VM suspension on a platform where monotonic paused
        worker.tick()
        assert runner.handle.stopped.is_set()
    finally:
        worker.stop()


def test_input_digest_mismatch_is_rejected_before_render(tmp_path):
    worker, client, _, runner = make_worker(tmp_path)
    client.manifest += b" "
    for _ in range(3):
        worker.tick()
    assert runner.count == 0
    assert client.failures == ["input_mismatch"]
    assert not worker.completed
    worker.stop()


@pytest.mark.parametrize("status,code", [(429, "storage_unavailable"), (503, "storage_unavailable"), (413, "storage_quota")])
def test_server_storage_outage_is_reported_as_bounded_retryable_failure(tmp_path, status, code):
    worker, client, _, _ = make_worker(tmp_path)

    def unavailable(*args):
        raise RemoteError(code, status)

    client.stage = unavailable
    for _ in range(3):
        worker.tick()
    assert client.failures == ["worker_transient"]
    assert not worker.completed
    assert list(worker.work_dir.iterdir()) == [worker.work_dir / ".pixiv-remote-worker"]
    worker.stop()


def test_new_worker_boot_never_continues_saved_authority(tmp_path):
    worker, client, clock, runner = make_worker(tmp_path, hanging=True, threaded=True)
    worker.tick()
    worker.tick()
    assert runner.started.wait(2)
    worker.stop()
    replacement_runner = InlineRunner()
    replacement = Worker(client, worker.work_dir, lambda: clock.wall, lambda: clock.mono,
                         replacement_runner, threaded=False)
    replacement.tick()
    assert client.boots[0][0] != client.boots[1][0]
    assert client.boots[0][1] != client.boots[1][1]
    assert replacement_runner.count == 0
    assert replacement.active_grant is None
    replacement.stop()


def test_worker_refuses_nonempty_unowned_directory(tmp_path):
    root = tmp_path / "user-data"
    root.mkdir()
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(RemoteError, match="work_directory_not_empty"):
        Worker(FakeClient(Clock()), root)
    assert (root / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize("operation", ["fetch_input", "stage"])
def test_file_transfer_does_not_block_lease_supervisor(tmp_path, operation):
    worker, client, clock, runner = make_worker(tmp_path, threaded=True)
    entered, release = threading.Event(), threading.Event()
    original = getattr(client, operation)

    def blocking(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    setattr(client, operation, blocking)
    try:
        worker.tick()
        worker.tick()
        assert entered.wait(2)
        clock.advance(30)
        worker.tick()
        assert client.renewals == [1]
        release.set()
        assert worker._attempt.done.wait(2)
        worker.tick()
        assert worker.completed
    finally:
        release.set()
        worker.stop()


def test_transfer_resume_checks_wall_discontinuity_before_supervisor_tick(tmp_path):
    worker, client, clock, runner = make_worker(tmp_path, threaded=True)
    entered, release = threading.Event(), threading.Event()
    original = client.fetch_input

    def blocking(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    client.fetch_input = blocking
    try:
        worker.tick()
        worker.tick()
        assert entered.wait(2)
        clock.wall += 130
        release.set()  # pipeline resumes before the supervisor tick
        assert worker._attempt.done.wait(2)
        assert runner.count == 0
        assert client.published is None
        assert worker._attempt.error == "lease_lost"
    finally:
        release.set()
        worker.stop()


def test_renewal_reply_cannot_revive_expired_local_safe_budget(tmp_path):
    worker, client, clock, runner = make_worker(tmp_path, hanging=True, threaded=True)
    original = client.renew
    try:
        worker.tick()
        worker.tick()
        assert runner.started.wait(2)
        clock.advance(30)
        client.lost_renew = True
        worker.tick()
        clock.advance(85)  # prior confirmed safe-until is mono=220, now=215

        def late(*args):
            clock.advance(10)
            return original(*args)

        client.renew = late
        worker.tick()
        assert worker._attempt.guard.safe_until == 220
        assert worker._attempt.cancel.is_set()
        assert runner.handle.stopped.is_set()
    finally:
        worker.stop()


def test_repeated_failed_renders_reclaim_inputs_in_live_worker(tmp_path):
    worker, client, clock, runner = make_worker(tmp_path)
    for index in range(4):
        client.manifest = canonical_bytes({"challenge": f"{index:064x}", "protocol": 1, "renderer": "epub-v1"}) + b" "
        client.grant = replace(client.grant, run_id=str(uuid.uuid4()), attempt_id=str(uuid.uuid4()),
                               input_sha256="a" * 64, server_now=clock.wall,
                               hard_deadline=clock.wall + 600, lease_until=clock.wall + 180)
        client.claim_cache.clear()
        worker.tick()
        worker.tick()
        worker.tick()
        assert list(worker.work_dir.iterdir()) == [worker.work_dir / ".pixiv-remote-worker"]
        clock.advance(5)
    assert len(client.failures) == 4
    assert runner.count == 0
    worker.stop()


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled", "expired", "404", "410"])
def test_reconcile_terminal_result_of_another_attempt_retires_old_cache(tmp_path, state):
    worker, client, _, _ = make_worker(tmp_path)
    identity = {"run_id": client.grant.run_id, "attempt_id": client.grant.attempt_id,
                "sha256": "c" * 64, "size_bytes": 1}
    directory = worker.work_dir / identity["attempt_id"]
    directory.mkdir()
    (directory / "result.bin").write_bytes(b"x")
    (directory / "receipt.json").write_bytes(canonical_bytes(identity))
    worker._pending_receipts.append(identity)

    def receipt(run_id):
        if state in {"404", "410"}:
            raise RemoteError("not_found", int(state))
        return {"run_id": run_id, "state": state, "attempt_id": str(uuid.uuid4())}

    client.receipt = receipt
    worker._reconcile_pending()
    assert not directory.exists()
    assert not worker._pending_receipts
    assert not worker.completed
    worker.stop()


def test_uncertain_receipts_are_retained_then_safely_age_out(tmp_path):
    worker, client, clock, _ = make_worker(tmp_path)
    identity = {"run_id": client.grant.run_id, "attempt_id": client.grant.attempt_id,
                "sha256": "c" * 64, "size_bytes": 1}
    directory = worker.work_dir / identity["attempt_id"]
    directory.mkdir()
    (directory / "receipt.json").write_bytes(canonical_bytes(identity))
    worker._pending_receipts.append(identity)
    os.utime(directory, (clock.wall, clock.wall))
    worker._reconcile_pending()
    assert directory.exists() and worker._pending_receipts
    clock.advance(3601)
    worker._cleanup_stale()
    assert not directory.exists() and not worker._pending_receipts
    worker.stop()


class Response:
    def __init__(self, payload, status=200, *, headers=None):
        self.status_code = status
        self.content = payload if isinstance(payload, bytes) else canonical_bytes(payload)
        self.headers = {"Content-Length": str(len(self.content)), **(headers or {})}
        self.closed = False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def enrolled_http(*responses):
    session_id, epoch = str(uuid.uuid4()), str(uuid.uuid4())
    transport = Transport(Response({"session_id": session_id, "epoch": epoch,
                                    "expires_at": 1900, "server_now": 1000}), *responses)
    client = WorkerHTTP("https://example.invalid/", "b" * 64, transport)
    client.enroll(str(uuid.uuid4()), "s" * 64)
    return client, transport


def test_http_has_fixed_origin_tls_no_redirects_and_separate_session_secret():
    client, transport = enrolled_http(Response({"server_now": 1001, "expires_at": 1901}))
    client.session_heartbeat()
    registration, heartbeat = transport.calls
    assert registration[1] == "https://example.invalid/api/remote-worker/v1/sessions"
    assert registration[2]["headers"]["Authorization"] == "Bearer " + "b" * 64
    assert heartbeat[2]["headers"]["Authorization"] == "Bearer " + "s" * 64
    for _, _, kwargs in transport.calls:
        assert kwargs["timeout"].total == 15
        assert kwargs["timeout"].connect_timeout == 5
        assert kwargs["verify"] is True
        assert kwargs["allow_redirects"] is False
        assert not ({"Cookie", "Origin"} & kwargs["headers"].keys())
    assert "s" * 64 not in repr(client)
    assert "b" * 64 not in repr(client)


@pytest.mark.parametrize("origin", ["http://example.invalid", "https://u:p@example.invalid",
                                    "https://example.invalid/path", "https://example.invalid/?token=x"])
def test_http_origin_cannot_supply_a_path_or_credentials(origin):
    with pytest.raises(RemoteError, match="invalid_server_origin"):
        WorkerHTTP(origin, "b" * 64)


@pytest.mark.parametrize("response,code", [
    (Response(b"", 302, headers={"Location": "https://other.invalid"}), "redirect_refused"),
    (Response({"expires_at": 1900}), "protocol_mismatch"),
    (Response(b"x" * (16 * 1024 + 1)), "response_too_large"),
    (Response({"error": ["secret"]}, 500), "remote_request_failed"),
])
def test_http_rejects_redirects_missing_clock_or_unbounded_response(response, code):
    client, _ = enrolled_http(response)
    with pytest.raises(RemoteError, match=code):
        client.session_heartbeat()
    assert response.closed


def test_http_transport_failure_is_redacted_and_does_not_retry_mutations():
    client, transport = enrolled_http(requests.Timeout("secret response or headers"))
    with pytest.raises(RemoteError, match="worker_transient") as error:
        client.claim("click-once")
    assert "secret" not in str(error.value)
    assert len(transport.calls) == 2


def test_http_claim_preserves_terminal_ids_only_for_receipt_query():
    run_id, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
    client, _ = enrolled_http(Response({"error": "claim_terminal", "run_id": run_id,
                                       "attempt_id": attempt_id}, 409))
    with pytest.raises(RemoteError) as error:
        client.claim("same-claim")
    assert error.value.code == "claim_terminal"
    assert (error.value.run_id, error.value.attempt_id) == (run_id, attempt_id)


def test_http_claim_checks_session_and_closed_grant_fields():
    client, transport = enrolled_http()
    grant = FakeClient(Clock()).grant
    payload = asdict(replace(grant, session_id=client.session_id, epoch=client.epoch))
    transport.responses.append(Response(payload))
    assert client.claim("claim-one").run_id == grant.run_id
    transport.responses.append(Response({**payload, "command": "not supported"}))
    with pytest.raises(RemoteError, match="protocol_mismatch"):
        client.claim("claim-two")


@pytest.mark.parametrize("payload", [b'{"server_now":1,"server_now":2}', b'{"server_now":NaN}',
                                    b'{"server_now":Infinity}', b'{"server_now":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}'])
def test_http_json_parser_rejects_ambiguous_or_deep_json(payload):
    client, _ = enrolled_http(Response(payload))
    with pytest.raises(RemoteError, match="protocol_mismatch"):
        client.session_heartbeat()


def test_http_slow_trickle_has_overall_deadline_and_uses_read1():
    clock = Clock()
    client, transport = enrolled_http()
    client._monotonic = lambda: clock.mono

    class SlowRaw:
        reads = 0
        def read1(self, amount, decode_content=False):
            assert decode_content is False
            self.reads += 1
            clock.advance(6)
            return b" "

    response = Response(b"", headers={"Content-Length": "10000"})
    response.raw = SlowRaw()
    transport.responses.append(response)
    with pytest.raises(RemoteError, match="worker_transient"):
        client.session_heartbeat()
    assert response.raw.reads == 3
    assert response.closed


def test_http_raw_body_timeout_is_redacted_as_transport_failure():
    from urllib3.exceptions import ReadTimeoutError

    class BrokenRaw:
        def read1(self, *args, **kwargs):
            raise ReadTimeoutError(None, "secret-url", "secret-error")

    response = Response(b"")
    response.raw = BrokenRaw()
    client, _ = enrolled_http(response)
    with pytest.raises(RemoteError, match="worker_transient") as error:
        client.session_heartbeat()
    assert "secret" not in str(error.value)
    assert response.closed


def test_watchdog_stops_child_even_while_supervisor_is_in_network_call(tmp_path):
    worker, client, clock, runner = make_worker(tmp_path, hanging=True, threaded=True)
    entered, release = threading.Event(), threading.Event()
    original = client.renew
    renewal_thread = None

    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    try:
        worker.tick()
        worker.tick()
        assert runner.started.wait(2)
        client.renew = blocked
        clock.advance(30)
        renewal_thread = threading.Thread(target=worker.tick)
        renewal_thread.start()
        assert entered.wait(2)
        clock.advance(91)
        assert runner.handle.stopped.wait(2)
        assert worker._attempt.cancel.is_set()
    finally:
        release.set()
        if renewal_thread is not None:
            renewal_thread.join(timeout=2)
        worker.stop()

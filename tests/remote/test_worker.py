from __future__ import annotations

from dataclasses import asdict, replace
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


@pytest.mark.parametrize("last_error", [None, "upload_in_progress", "stale_grant"])
def test_upload_retry_keeps_the_same_result_and_fences_revoked_grants(tmp_path, last_error):
    worker, client, _, runner = make_worker(tmp_path)
    original_stage = client.stage
    uploads = []

    def stage(grant, path, sha256, size):
        uploads.append((grant, sha256, size))
        original_stage(grant, path, sha256, size)
        if len(uploads) == 1:
            # The request reached the server but its response was lost. Its
            # reservation can still be busy when the same upload is retried.
            raise RemoteError("worker_transient", 503)
        if len(uploads) == 2:
            raise RemoteError("stale_grant" if last_error == "stale_grant" else "upload_in_progress", 409)
        if last_error is not None:
            raise RemoteError(last_error, 409)
        return original_stage(grant, path, sha256, size)

    client.stage = stage
    try:
        for _ in range(3):
            worker.tick()
        assert len(uploads) == (2 if last_error == "stale_grant" else 3)
        assert all(upload == uploads[0] for upload in uploads)
        assert runner.count == 1
        if last_error is None:
            assert list(worker.completed) == [client.grant.run_id]
            assert client.failures == []
        else:
            assert client.published is None
            assert not worker.completed
            if last_error == "upload_in_progress":
                assert client.failures == ["worker_transient"]
    finally:
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


@pytest.mark.parametrize("receipt", [None, b'{"run_id":', b"{}", b"[]"])
def test_cold_boot_discards_unrecoverable_owned_cache(tmp_path, receipt):
    worker, client, clock, _ = make_worker(tmp_path)
    root = worker.work_dir
    worker.stop()
    abandoned = root / client.grant.attempt_id
    abandoned.mkdir()
    (abandoned / "text.bin").write_bytes(b"partial task input")
    if receipt is not None:
        (abandoned / "receipt.json").write_bytes(receipt)
    replacement = Worker(client, root, lambda: clock.wall, lambda: clock.mono,
                         InlineRunner(), threaded=False)
    try:
        assert not abandoned.exists()
        assert not replacement._pending_receipts
        assert replacement.active_grant is None
        replacement.tick()
        assert replacement.state == "idle"
    finally:
        replacement.stop()


def test_cold_boot_preserves_complete_unconfirmed_receipt(tmp_path):
    worker, client, clock, _ = make_worker(tmp_path)
    root = worker.work_dir
    worker.stop()
    pending = root / client.grant.attempt_id
    pending.mkdir()
    identity = {"run_id": client.grant.run_id, "attempt_id": client.grant.attempt_id,
                "sha256": digest(b"result"), "size_bytes": 6}
    (pending / "result.bin").write_bytes(b"result")
    (pending / "receipt.json").write_bytes(canonical_bytes(identity))
    replacement = Worker(client, root, lambda: clock.wall, lambda: clock.mono,
                         InlineRunner(), threaded=False)
    try:
        assert list(replacement._pending_receipts) == [identity]
        assert replacement.active_grant is None
        assert pending.exists()
        replacement.tick()
        client.published = {**identity, "state": "succeeded"}
        replacement.tick()
        assert list(replacement.completed) == [client.grant.run_id]
        assert not pending.exists()
    finally:
        replacement.stop()


def test_interrupted_receipt_write_cannot_leave_partial_recovery_record(tmp_path, monkeypatch):
    import pixiv_novel_sync.remote.worker as worker_module

    worker, client, _, _ = make_worker(tmp_path)
    real_fsync = worker_module.os.fsync
    calls = 0

    def interrupted(fd):
        nonlocal calls
        calls += 1
        if calls == 2:  # renderer output succeeds; receipt persistence fails
            raise OSError("simulated disk interruption")
        return real_fsync(fd)

    monkeypatch.setattr(worker_module.os, "fsync", interrupted)
    try:
        worker.tick()
        worker.tick()
        assert client.published is None
        assert not (worker._attempt.directory / "receipt.json").exists()
        assert worker._attempt.done.is_set()
    finally:
        worker.stop()


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


def test_worker_waits_for_local_space_before_claiming_and_recovers(tmp_path, monkeypatch):
    import pixiv_novel_sync.remote.worker as worker_module

    worker, client, clock, _ = make_worker(tmp_path)
    required = (worker_module.RESULT_LIMIT + worker_module.TEXT_LIMIT
                + worker_module.COVER_LIMIT + worker_module.MANIFEST_LIMIT)
    monkeypatch.setattr(worker_module, "WORK_LIMIT", required + 1024)
    identity = {"run_id": str(uuid.uuid4()), "attempt_id": str(uuid.uuid4()),
                "sha256": digest(b"x" * 2048), "size_bytes": 2048}
    directory = worker.work_dir / identity["attempt_id"]
    directory.mkdir()
    (directory / "result.bin").write_bytes(b"x" * 2048)
    (directory / "receipt.json").write_bytes(canonical_bytes(identity))
    worker._pending_receipts.append(identity)
    try:
        worker.tick()  # register
        worker.tick()  # original result is still unconfirmed
        clock.advance(5)
        worker.tick()
        assert not client.claims
        assert worker.active_grant is None
        assert worker.last_error == "work_quota_exceeded"
        assert directory.exists()

        client.published = {"run_id": identity["run_id"], "state": "failed"}
        clock.advance(30)
        worker.tick()  # heartbeat and receipt reconciliation continue while full
        assert not directory.exists()
        assert client.heartbeats == 1
        clock.advance(5)
        worker.tick()
        worker.tick()
        assert list(worker.completed) == [client.grant.run_id]
        assert worker.last_error is None
    finally:
        worker.stop()


def test_local_capacity_check_does_not_abandon_a_claim_with_a_lost_response(tmp_path, monkeypatch):
    worker, client, clock, _ = make_worker(tmp_path)
    client.lost_claim = True
    try:
        worker.tick()
        worker.tick()
        claim_key = client.claims[0]

        def full(extra):
            raise RemoteError("work_quota_exceeded", 413)

        monkeypatch.setattr(worker, "_check_capacity", full)
        client.lost_claim = True
        clock.advance(5)
        worker.tick()
        assert client.claims == [claim_key, claim_key]
        assert worker._claim_key == claim_key
    finally:
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


def test_http_never_replays_response_cookies_into_worker_auth(monkeypatch):
    from email.message import Message
    from types import SimpleNamespace

    real_session = requests.Session
    calls = []

    class LocalAdapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            calls.append(request)
            payload = {"server_now": 1000, "expires_at": 1900}
            if len(calls) == 1:
                payload.update(session_id=str(uuid.uuid4()), epoch=str(uuid.uuid4()))
            response = requests.Response()
            response.status_code = 200
            response._content = canonical_bytes(payload)
            response._content_consumed = True
            response.request, response.url = request, request.url
            headers = Message()
            headers["Set-Cookie"] = "proxy_cookie=value; Path=/; Secure"
            response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
            return response

        def close(self):
            pass

    def factory():
        session = real_session()
        session.mount("https://", LocalAdapter())
        return session

    monkeypatch.setattr(requests, "Session", factory)
    client = WorkerHTTP("https://example.invalid", "b" * 64)
    try:
        client.enroll(str(uuid.uuid4()), "s" * 64)
        client.session_heartbeat()
        assert all("Cookie" not in call.headers for call in calls)
        assert not client._get_transport().cookies
    finally:
        client.close()


@pytest.mark.parametrize("outcome", ["succeeded", "invalid_input", "cancelled"])
def test_completed_transfer_releases_its_connection_pool_only(tmp_path, monkeypatch, outcome):
    real_session = requests.Session
    sessions = []

    def session_factory():
        session = real_session()
        original_close = session.close
        session.close_count = 0

        def close():
            session.close_count += 1
            original_close()

        session.close = close
        sessions.append(session)
        return session

    monkeypatch.setattr(requests, "Session", session_factory)
    clock = Clock()

    class SessionClient(FakeClient):
        def __init__(self):
            super().__init__(clock)
            self.http = WorkerHTTP("https://example.invalid", "b" * 64)

        def enroll(self, *args):
            self.http._get_transport()
            return super().enroll(*args)

        def fetch_input(self, *args):
            self.http._get_transport()
            return super().fetch_input(*args)

        def close_thread(self):
            self.http.close_thread()

        def close(self):
            self.http.close()

    client = SessionClient()
    runner = HangingRunner() if outcome == "cancelled" else InlineRunner()
    if outcome == "invalid_input":
        client.manifest += b" "
    worker = Worker(client, tmp_path / "scratch", lambda: clock.wall, lambda: clock.mono,
                    runner, threaded=True)
    try:
        worker.tick()
        worker.tick()
        attempt = worker._attempt
        if outcome == "cancelled":
            assert runner.started.wait(2)
            clock.advance(121)
            worker.tick()
        assert attempt.done.wait(2)
        worker.tick()
        assert len(sessions) == 2
        assert client.http._sessions == [sessions[0]]
        assert sessions[0].close_count == 0
        assert sessions[1].close_count == 1
        assert bool(worker.completed) is (outcome == "succeeded")
    finally:
        worker.stop()
    assert [session.close_count for session in sessions] == [1, 1]


def test_closed_http_client_cannot_allocate_another_thread_pool():
    client = WorkerHTTP("https://example.invalid", "b" * 64)
    first = client._get_transport()
    client.close()
    results = []

    def late_transfer():
        try:
            client._get_transport()
        except RemoteError as exc:
            results.append(exc.code)

    thread = threading.Thread(target=late_transfer)
    thread.start()
    thread.join(timeout=2)
    try:
        assert not thread.is_alive()
        assert results == ["invalid_session"]
        assert client._sessions == []
        with pytest.raises(RemoteError, match="invalid_session"):
            client._get_transport()  # no reuse of this thread's closed session
    finally:
        client.close()
        first.close()


def test_transfer_closes_pool_reopened_by_inflight_request_after_shutdown(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class DelayedSession:
        def __init__(self):
            self.cookies = requests.cookies.RequestsCookieJar()
            self.close_count = 0
            self.pool_open = False
            self.reopened_after_close = False

        def request(self, *args, **kwargs):
            entered.set()
            assert release.wait(2)
            # requests.Session.close() clears existing pools but does not
            # prevent an already-started request from opening a new one.
            self.reopened_after_close = self.close_count > 0
            self.pool_open = True
            return Response({"server_now": 1000, "expires_at": 1900})

        def close(self):
            self.close_count += 1
            self.pool_open = False

    session = DelayedSession()
    monkeypatch.setattr(requests, "Session", lambda: session)
    client = WorkerHTTP("https://example.invalid", "b" * 64)
    client._session_secret = "s" * 64
    errors = []

    def transfer():
        try:
            client.session_heartbeat()
        except Exception as exc:
            errors.append(exc)
        finally:
            client.close_thread()

    thread = threading.Thread(target=transfer)
    thread.start()
    try:
        assert entered.wait(2)
        client.close()
        assert session.close_count == 1
        assert client._sessions == []
        release.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert errors == []
        assert session.reopened_after_close
        assert not session.pool_open
        assert session.close_count == 2
    finally:
        release.set()
        thread.join(timeout=2)
        client.close()


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


def test_http_preserves_busy_upload_code_for_bounded_retries():
    client, _ = enrolled_http(Response({"error": "upload_in_progress"}, 409))
    try:
        with pytest.raises(RemoteError, match="upload_in_progress") as error:
            client._request("PUT", "/attempts/fixture/result", data=b"fixture")
        assert error.value.status == 409
    finally:
        client.close()


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


@pytest.mark.parametrize("attached_connection", [True, False])
@pytest.mark.parametrize("supports_read1", [True, False])
def test_http_body_read_timeout_shrinks_to_remaining_budget(attached_connection, supports_read1):
    import http.client
    import io

    from urllib3.connection import HTTPConnection
    from urllib3.response import HTTPResponse

    clock = Clock()
    client, transport = enrolled_http()
    client._monotonic = lambda: clock.mono

    class SlowSocket:
        timeout = 10.0

        def __init__(self):
            self.timeouts = []

        def settimeout(self, value):
            self.timeout = value
            self.timeouts.append(value)

        def makefile(self, mode):
            return io.BufferedReader(SlowWire(self))

        def close(self):
            pass

    class SlowWire(io.RawIOBase):
        def __init__(self, sock):
            self._sock = sock
            self.headers_sent = False

        def readable(self):
            return True

        def readinto(self, buffer):
            if not self.headers_sent:
                value = b"HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n"
                self.headers_sent = True
            else:
                clock.advance(min(6.0, self._sock.timeout))
                if self._sock.timeout < 6:
                    raise TimeoutError("fixture body read timed out")
                value = b" "
            buffer[:len(value)] = value
            return len(value)

    sock = SlowSocket()
    original = http.client.HTTPResponse(sock)
    original.begin()
    connection = HTTPConnection("fixture.invalid")
    # HTTP/1.0 and Connection: close detach the socket from HTTPConnection;
    # http.client's still-live response owns it until the body has been read.
    connection.sock = sock if attached_connection else None
    raw = HTTPResponse(body=original, original_response=original, connection=connection,
                       headers=original.headers.items(), status=200, preload_content=False,
                       decode_content=False)
    if not supports_read1:
        raw.read1 = None
    response = requests.Response()
    response.status_code = 200
    response.headers.update(original.headers)
    response.raw = raw
    transport.responses.append(response)

    with pytest.raises(RemoteError, match="worker_transient"):
        client.session_heartbeat()

    assert clock.mono == pytest.approx(115)
    assert sock.timeouts == pytest.approx([10, 9, 3])
    assert raw.closed


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

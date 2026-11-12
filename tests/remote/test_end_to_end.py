"""Real Web/store/worker/controller protocol with no network or account secrets."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import io
import json
import sqlite3
import uuid
import zipfile
from urllib.parse import urlsplit

import pytest
import requests

from pixiv_novel_sync.remote.control_store import ControlStore
from pixiv_novel_sync.remote.instabox import MachineView, OperationView, ProviderError
from pixiv_novel_sync.remote.lifecycle import Controller
from pixiv_novel_sync.remote.runner import run_child
from pixiv_novel_sync.remote.worker import Worker
from pixiv_novel_sync.remote.worker_http import WorkerHTTP


class FakeProvider:
    def __init__(self, clock):
        self.clock = clock
        self.view = MachineView("m_fixture", "suspended", None, False, 480, None, None)
        self.operations, self.keys, self.pending_at = {}, {}, {}
        self.calls, self.mutations = [], []
        self.auto_finish = True
        self.drop_start = False

    @property
    def state(self):
        return self.view.state

    def machine(self):
        self.calls.append("machine")
        return self.view

    def operation(self, operation_id):
        self.calls.append("operation")
        return self.operations[operation_id]

    def mutate(self, action, idempotency_key):
        self.calls.append(action)
        self.mutations.append((action, idempotency_key))
        if idempotency_key in self.keys:
            return self.keys[idempotency_key]
        operation = OperationView("op_" + uuid.uuid4().hex, "m_fixture", action, "queued")
        self.operations[operation.id] = operation
        self.keys[idempotency_key] = operation
        self.pending_at[operation.id] = self.clock.now
        self.view = replace(self.view, state={"start": "starting", "resume": "resuming", "suspend": "suspending"}[action],
                            runtime_ready=False, operation_id=operation.id)
        if self.drop_start and action in {"start", "resume"}:
            self.drop_start = False
            raise ProviderError("provider_unreachable")
        return operation

    def wake(self):
        self.calls.append("wake")
        return self.view

    def advance(self):
        operation_id = self.view.operation_id
        if (not self.auto_finish or operation_id is None
                or self.clock.now - self.pending_at[operation_id] < 15):
            return
        operation = self.operations[operation_id]
        self.operations[operation_id] = replace(operation, state="succeeded")
        if operation.action == "suspend":
            self.view = replace(self.view, state="suspended", operation_id=None, runtime_ready=False, run_started_at=None)
        else:
            self.view = replace(self.view, state="running", operation_id=None, runtime_ready=True,
                                run_started_at=self.clock.now, cooldown_until=None)


class LocalResponse:
    def __init__(self, response):
        self.response = response
        self.status_code = response.status_code
        self.headers = response.headers

    def iter_content(self, chunk_size):
        data = self.response.get_data()
        for offset in range(0, len(data), chunk_size):
            yield data[offset:offset + chunk_size]

    def close(self):
        self.response.close()


class LocalTransport:
    def __init__(self, scenario):
        self.scenario = scenario
        self.lost = Counter()
        self.before = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        assert urlsplit(url).netloc == "example.invalid"
        assert kwargs["verify"] is True and kwargs["allow_redirects"] is False
        path = urlsplit(url).path
        operation = path.rsplit("/", 1)[-1]
        if operation in self.before:
            self.before.pop(operation)()
        body = kwargs.get("data")
        if hasattr(body, "read"):
            body = body.read()
        client = self.scenario.web.app.test_client(use_cookies=False)
        response = client.open(path, method=method, headers=kwargs["headers"], data=body)
        self.calls.append((operation, response.status_code))
        if self.lost[operation] and 200 <= response.status_code < 300 and response.status_code != 204:
            self.lost[operation] -= 1
            response.close()
            raise requests.Timeout("simulated response loss")
        return LocalResponse(response)


class CompleteHandle:
    def poll(self):
        return 0

    def stop(self):
        pass


class TrackingRunner:
    def __init__(self):
        self.counts = Counter()

    def start(self, kind, manifest_path, text_path, cover_path, output_path):
        self.counts[output_path.parent.name] += 1
        run_child(kind, manifest_path, text_path, cover_path, output_path)
        return CompleteHandle()


class Scenario:
    def __init__(self, web, root, monkeypatch):
        self.web, self.root, self.monkeypatch = web, root, monkeypatch
        self.started_at = web.clock.now
        self.provider = FakeProvider(web.clock)
        self.transport = LocalTransport(self)
        self.runner = TrackingRunner()
        self.worker = None
        self.worker_clients = []
        self.owned_observations = []
        self.extra_apps = []
        self.web.login()
        self.controller = Controller(web.store, web.control, self.provider, lambda: web.clock.now, lambda: 0)
        self.library_before = self._library_snapshot()

    def _library_snapshot(self):
        with sqlite3.connect(self.web.library_db) as db:
            return {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                    for table in ("users", "novels", "novel_texts", "task_logs")}

    def enqueue(self, kind, novel_id=None, key=None):
        payload = {"kind": kind}
        if novel_id is not None:
            payload["novel_id"] = novel_id
        response = self.web.client.post("/api/dashboard/remote/runs", json=payload,
                                        headers={"X-CSRF-Token": self.web.csrf,
                                                 "Idempotency-Key": key or str(uuid.uuid4())})
        assert response.status_code == 202, response.get_json()
        return response.get_json()["run_id"]

    def _new_worker(self):
        client = WorkerHTTP("https://example.invalid", self.web.bootstrap, self.transport)
        self.worker_clients.append(client)
        self.worker = Worker(client, self.root / "scratch", lambda: self.web.clock.now,
                             lambda: self.web.clock.now - self.started_at + 100,
                             self.runner, threaded=False)

    def step(self, seconds=15):
        self.web.clock.now += seconds
        self.provider.advance()
        before = len(self.provider.calls)
        self.controller.tick()
        assert len(self.provider.calls) - before <= 1
        state = self.web.control.snapshot(self.web.clock.now)
        if state["owned_run_started_at"] is not None:
            self.owned_observations.append((state["owned_run_started_at"], self.provider.view.run_started_at))
        if self.provider.state == "running" and self.provider.view.runtime_ready:
            if self.worker is None or self.worker.state == "stopped":
                self._new_worker()
            self.worker.tick()
        elif self.worker is not None and self.provider.state in {"suspended", "stopped"}:
            self.worker.stop()

    def run_until(self, predicate, max_steps=100):
        for _ in range(max_steps):
            if predicate():
                return
            self.step()
        raise AssertionError({"machine": self.web.control.snapshot(self.web.clock.now),
                              "worker": (self.worker.state, self.worker.last_error) if self.worker else None,
                              "runs": self.web.store.list_runs(), "http": self.transport.calls[-20:]})

    def restart_vps(self):
        old_store = self.web.store
        app = self.web.new_app()
        self.extra_apps.append(app)
        self.web.app, self.web.client = app, app.test_client()
        remote = app.extensions["remote_execution"]
        self.web.store, self.web.control, self.web.blobs = remote["store"], remote["control"], remote["blobs"]
        self.web.login()
        self.controller = Controller(self.web.store, self.web.control, self.provider,
                                     lambda: self.web.clock.now, lambda: 0)
        old_store.close()

    def restart_worker(self):
        if self.worker is not None:
            self.worker.stop()
        self._new_worker()

    def drop_next_response(self, operation):
        if operation in {"start", "resume"}:
            self.provider.drop_start = True
        else:
            self.transport.lost[operation] += 1

    def crash_at(self, point):
        if point == "after_publish":
            self.drop_next_response("publish")
            return
        if point not in {"before_result_file", "after_result_file"}:
            raise AssertionError("unknown test crash point")
        blobs = self.web.blobs
        receive = blobs.receive

        def fail_once(*args, **kwargs):
            self.monkeypatch.setattr(blobs, "receive", receive)
            if point == "after_result_file":
                receive(*args, **kwargs)
            raise OSError("simulated disk interruption")

        # Input snapshots use the same durable writer. Inject only when the
        # worker is about to send its result, after enqueue has frozen input.
        self.transport.before["result"] = lambda: self.monkeypatch.setattr(blobs, "receive", fail_once)

    def run(self, run_id):
        return self.web.store.get_run(run_id)

    def download(self, run_id):
        return self.web.client.get(f"/api/dashboard/remote/runs/{run_id}/download")

    def library_unchanged(self):
        return self._library_snapshot() == self.library_before

    def controller_owned_activation(self):
        return any(owned == observed and owned is not None for owned, observed in self.owned_observations)

    def worker_idle(self):
        return self.worker is not None and self.worker.state in {"idle", "stopped"} and self.worker.active_grant is None

    def render_count(self, run_id):
        with self.web.store.read() as db:
            ids = [row[0] for row in db.execute("SELECT attempt_id FROM attempts WHERE run_id=?", (run_id,))]
        return sum(self.runner.counts[attempt_id] for attempt_id in ids)

    def published_count(self, run_id):
        with self.web.store.read() as db:
            return db.execute("SELECT count(*) FROM artifacts a JOIN attempts t USING(attempt_id) "
                              "WHERE t.run_id=? AND a.state='published'", (run_id,)).fetchone()[0]

    def close(self):
        if self.worker is not None:
            self.worker.stop()
        for app in self.extra_apps:
            app.extensions["close_work_analysis_owner"]()


@pytest.fixture
def scenario(remote_web_fixture, tmp_path, monkeypatch):
    value = Scenario(remote_web_fixture, tmp_path, monkeypatch)
    yield value
    value.close()


def test_synthetic_epub_round_trip_and_owned_idle_suspend(scenario):
    s = scenario
    run = s.enqueue("epub.single.v1", 42)
    s.run_until(lambda: s.run(run)["state"] == "succeeded")
    result = s.download(run)
    assert result.status_code == 200
    assert result.mimetype == "application/epub+zip"
    with zipfile.ZipFile(io.BytesIO(result.data)) as epub:
        assert epub.read("mimetype") == b"application/epub+zip"
        assert b"Second page" in b"".join(epub.read(name) for name in epub.namelist() if name.endswith(".xhtml"))
    result.close()
    assert s.library_unchanged()
    assert s.published_count(run) == 1
    s.run_until(s.worker_idle)
    s.step(seconds=301)
    s.run_until(lambda: s.provider.state == "suspended")
    assert s.controller_owned_activation()


def test_published_response_loss_is_confirmed_without_rerender(scenario):
    s = scenario
    s.drop_next_response("publish")
    run = s.enqueue("diagnostic.v1")
    s.run_until(lambda: s.run(run)["state"] == "succeeded")
    s.run_until(s.worker_idle)
    assert s.render_count(run) == s.published_count(run) == 1
    assert ("receipt", 200) in s.transport.calls


def test_claim_response_loss_replays_the_same_attempt(scenario):
    s = scenario
    s.drop_next_response("claim")
    run = s.enqueue("diagnostic.v1")
    s.run_until(lambda: s.run(run)["state"] == "succeeded")
    with s.web.store.read() as db:
        assert db.execute("SELECT count(*) FROM attempts WHERE run_id=?", (run,)).fetchone()[0] == 1
    assert s.render_count(run) == 1


def test_start_202_without_completion_never_opens_execution(scenario):
    s = scenario
    s.provider.auto_finish = False
    run = s.enqueue("diagnostic.v1")
    s.run_until(lambda: bool(s.provider.mutations))
    for _ in range(8):
        s.step()
    assert s.run(run)["state"] == "queued"
    assert s.web.control.snapshot(s.web.clock.now)["mode"] == "starting"
    assert s.render_count(run) == 0
    s.provider.auto_finish = True
    s.run_until(lambda: s.run(run)["state"] == "succeeded")


def test_start_response_loss_and_vps_restart_preserve_operation(scenario):
    s = scenario
    run = s.enqueue("diagnostic.v1")
    s.drop_next_response("start")
    s.run_until(lambda: bool(s.provider.mutations))
    s.restart_vps()
    s.run_until(lambda: s.run(run)["state"] == "succeeded")
    startup_keys = {key for action, key in s.provider.mutations if action in {"start", "resume"}}
    assert len(startup_keys) == 1
    assert s.published_count(run) == 1


def test_cooldown_keeps_reading_and_original_export_available(scenario):
    s = scenario
    s.provider.view = replace(s.provider.view, cooldown_until=s.web.clock.now + 600)
    run = s.enqueue("diagnostic.v1")
    for _ in range(5):
        s.step()
    assert s.provider.mutations == []
    assert s.run(run)["state"] == "queued"
    assert s.web.client.get("/api/dashboard/novels/42").status_code == 200
    assert s.web.client.get("/api/dashboard/novels?q=Synthetic").status_code == 200
    old_export = s.web.client.post("/api/dashboard/novels/export-epub", json={"novel_ids": [42]},
                                   headers={"X-CSRF-Token": s.web.csrf})
    assert old_export.status_code == 200
    assert old_export.mimetype == "application/epub+zip"
    old_export.close()
    s.run_until(lambda: s.run(run)["state"] == "succeeded")


def test_same_click_after_library_update_keeps_snapshot_through_real_worker(scenario):
    s = scenario
    run = s.enqueue("epub.single.v1", 42, key="one-click")
    with sqlite3.connect(s.web.library_db) as db:
        db.execute("UPDATE novel_texts SET text_raw='New version must not replace frozen input' WHERE novel_id=42")
    assert s.enqueue("epub.single.v1", 42, key="one-click") == run
    s.run_until(lambda: s.run(run)["state"] == "succeeded")
    response = s.download(run)
    with zipfile.ZipFile(io.BytesIO(response.data)) as epub:
        text = b"".join(epub.read(name) for name in epub.namelist() if name.endswith(".xhtml"))
    response.close()
    assert b"First page" in text and b"New version" not in text


def test_upload_cancelled_before_acceptance_cannot_publish(scenario):
    s = scenario
    run = s.enqueue("diagnostic.v1")

    def cancel():
        response = s.web.client.post(f"/api/dashboard/remote/runs/{run}/cancel",
                                     headers={"X-CSRF-Token": s.web.csrf})
        assert response.status_code == 200

    s.transport.before["result"] = cancel
    s.run_until(lambda: s.run(run)["state"] == "cancelled")
    for _ in range(4):
        s.step()
    assert s.published_count(run) == 0
    assert s.download(run).status_code != 200


@pytest.mark.parametrize("point", ["before_result_file", "after_result_file", "after_publish"])
def test_file_and_database_interruption_has_no_broken_success_reference(scenario, point):
    s = scenario
    s.crash_at(point)
    run = s.enqueue("epub.single.v1", 42)
    s.run_until(lambda: s.run(run)["state"] == "succeeded")
    assert s.published_count(run) == 1
    response = s.download(run)
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as epub:
        assert epub.testzip() is None
    response.close()


def test_worker_cold_restart_retires_old_session_without_resetting_vps_epoch(scenario):
    s = scenario
    run = s.enqueue("diagnostic.v1")
    s.run_until(lambda: s.run(run)["state"] == "succeeded")
    s.run_until(s.worker_idle)
    old_client = s.worker_clients[-1]
    with s.web.store.read() as db:
        epoch = db.execute("SELECT epoch FROM meta WHERE id=1").fetchone()[0]
    s.restart_worker()
    s.step()
    with pytest.raises(Exception) as old_error:
        old_client.session_heartbeat()
    assert getattr(old_error.value, "status", None) == 401
    s.restart_vps()
    with s.web.store.read() as db:
        assert db.execute("SELECT epoch FROM meta WHERE id=1").fetchone()[0] == epoch
    again = s.enqueue("diagnostic.v1")
    s.run_until(lambda: s.run(again)["state"] == "succeeded")
    assert s.published_count(run) == s.published_count(again) == 1

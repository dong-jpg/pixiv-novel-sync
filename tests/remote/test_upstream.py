from types import SimpleNamespace
import json
import time
import uuid

import pytest

from pixiv_novel_sync.remote.account_scope import acquire_account
from pixiv_novel_sync.remote.business import BusinessTasks
from pixiv_novel_sync.remote.contracts import KINDS, RemoteError
from pixiv_novel_sync.remote.upstream import poll_upstream
from pixiv_novel_sync.remote.worker_http import WorkerHTTP
from pixiv_novel_sync.remote.runner import run_child
from tests.test_pixiv_fetch import Api
from .test_business import business  # noqa: F401
from .test_end_to_end import LocalTransport


@pytest.mark.parametrize("proxy", [None, "https://worker:" + "S" * 32 + "@proxy.example:8443"])
def test_pixiv_upstream_accepts_optional_cloud_proxy(monkeypatch, proxy):
    from pixiv_novel_sync.remote.upstream import _pixiv_call

    if proxy is not None:
        monkeypatch.setenv("PIXIV_REMOTE_EGRESS_PROXY", proxy)
    configurations = []
    closed = []

    class LocalAPI:
        def __init__(self, **kwargs):
            configurations.append(kwargs)
            self.requests = SimpleNamespace(trust_env=True, close=lambda: closed.append(True))

        def set_auth(self, token):
            assert token == "synthetic-access-token"

        def novel_detail(self, novel_id):
            assert self.requests.trust_env is False
            return {"novel": {"id": novel_id}}

    monkeypatch.setattr("pixivpy3.AppPixivAPI", LocalAPI)
    result = _pixiv_call({"kind": "pixiv.novel.v1", "novel_id": 42},
                         {"access_token": "synthetic-access-token"}, 1)
    assert result == {"novel": {"id": 42}}
    assert configurations == [{"timeout": 30, "proxies": {"https": proxy}}]
    assert closed == [True]


def ready(tasks, grant, ordinal, **kwargs):
    for _ in range(100):
        response = poll_upstream(tasks, grant, ordinal, **kwargs)
        if response["state"] == "ready":
            return response["data"]
        time.sleep(0.01)
    raise AssertionError("relay did not complete")


def test_receipt_replay_never_repeats_upstream_and_scope_is_checked(business):
    f = business
    calls = []

    def execute(tasks, grant, manifest, ordinal):
        calls.append((manifest["novel_id"], ordinal))
        return {"novel": {"id": 42}}

    assert ready(f.service, f.grant, 1, execute=execute) == {"novel": {"id": 42}}
    assert ready(f.service, f.grant, 1, execute=execute) == {"novel": {"id": 42}}
    assert calls == [(42, 1)]
    f.store.cancel(f.run["run_id"], f.clock.now)
    with pytest.raises(RemoteError):
        ready(f.service, f.grant, 1, execute=execute)


def test_unknown_dispatch_never_replays_after_reopen(business):
    f = business
    with f.store.transaction() as conn:
        conn.execute(
            "INSERT INTO upstream_receipts(attempt_id,ordinal,state) VALUES(?,1,'sent')",
            (f.grant.attempt_id,),
        )
    with pytest.raises(RemoteError, match="upstream_result_unknown"):
        poll_upstream(f.service, f.grant, 1, execute=lambda *args: pytest.fail("must not resend"))


def test_real_worker_http_fixed_fetch_and_key_never_leaves_web(remote_web_fixture, tmp_path, monkeypatch):
    f = remote_web_fixture
    monkeypatch.setenv("PIXIV_REMOTE_TASK_SECRET", "K" * 32)
    transport = LocalTransport(SimpleNamespace(web=f))
    client = WorkerHTTP("https://example.invalid", f.bootstrap, transport)
    client.enroll(str(uuid.uuid4()), "W" * 32)
    scope = "a" * 64
    owner = acquire_account(f.store, scope, 0, f.clock.now)
    tasks = BusinessTasks(f.store, f.blobs, clock=lambda: f.clock.now)
    run = tasks.submit_novel(42, "ACCESS_TOKEN_SENTINEL" * 2, scope, owner, "novel")
    with f.store.transaction() as conn:
        conn.execute(
            "UPDATE machine_control SET mode='ready',provider_state='running',runtime_ready=1,"
            "last_provider_at=?,gate_ready_until=?,runtime_budget_until=-1",
            (f.clock.now, f.clock.now + 45),
        )
    api = Api()
    calls = []

    def fake(manifest, credentials, ordinal):
        assert credentials["access_token"] == "ACCESS_TOKEN_SENTINEL" * 2
        calls.append(ordinal)
        return api.novel_detail(42) if ordinal == 1 else api.webview_novel(42)

    monkeypatch.setattr("pixiv_novel_sync.remote.upstream._pixiv_call", fake)
    grant = client.claim("work")
    assert grant.kind == "pixiv.novel.v1"
    raw = client.fetch_input(grant, "manifest")
    assert b"ACCESS_TOKEN_SENTINEL" not in raw
    root = tmp_path / "worker"
    root.mkdir()
    manifest_path = root / "manifest.json"
    manifest_path.write_bytes(raw)

    def perform(ordinal):
        for _ in range(100):
            response = client.upstream(grant, ordinal)
            if response["state"] == "ready":
                return response["data"]
            time.sleep(0.01)
        raise AssertionError("relay timeout")

    output = root / "result.bin"
    run_child(grant.kind, manifest_path, None, None, output, permit=perform)
    from pixiv_novel_sync.remote.contracts import digest

    body = output.read_bytes()
    client.stage(grant, output, digest(body), len(body))
    client.publish(grant, digest(body), len(body))
    assert tasks.result(run["run_id"]).text == "第一段\n\n第二段\n"
    assert calls == [1, 2]
    denied = f.app.test_client(use_cookies=False).get(
        "/api/remote-worker/v1/attempts/" + grant.attempt_id + "/credentials",
        headers={"Authorization": "Bearer " + "W" * 32},
    )
    assert denied.status_code in (401, 404)
    client.close()

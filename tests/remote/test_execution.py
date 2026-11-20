"""The task adapter uses the same library writer and never falls back silently."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import time
import uuid

import pytest

from pixiv_novel_sync.remote.execution import execution_scope
from pixiv_novel_sync.remote.business import BusinessTasks
from pixiv_novel_sync.remote.worker_http import WorkerHTTP
from pixiv_novel_sync.remote.runner import run_child
from pixiv_novel_sync.remote.contracts import digest, RemoteError
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService
from tests.test_sync_engine_incremental import _settings, _Storage
from tests.test_pixiv_fetch import Api
from .test_end_to_end import LocalTransport


def test_remote_fetch_uses_original_archive_writer_and_is_incremental(
    remote_web_fixture, tmp_path, monkeypatch
):
    f = remote_web_fixture
    f.app.config["REMOTE_CLOCK"] = time.time
    monkeypatch.setenv("PIXIV_REMOTE_TASK_SECRET", "K" * 32)
    settings = _settings(tmp_path / "business")
    settings.sync.pixiv_execution = "remote"
    settings.pixiv.user_id = 7
    client = WorkerHTTP("https://example.invalid", f.bootstrap, LocalTransport(SimpleNamespace(web=f)))
    client.enroll(str(uuid.uuid4()), "W" * 32)
    with f.store.transaction() as conn:
        conn.execute(
            "UPDATE machine_control SET mode='ready',provider_state='running',runtime_ready=1,"
            "last_provider_at=?,gate_ready_until=?,runtime_budget_until=-1",
            (time.time(), time.time() + 45),
        )
    api = Api()
    calls = []

    def fake(manifest, credentials, ordinal):
        calls.append(ordinal)
        return api.novel_detail(42) if ordinal == 1 else api.webview_novel(42)

    monkeypatch.setattr("pixiv_novel_sync.remote.upstream._pixiv_call", fake)

    # Test clock remains deterministic; permit spacing is tested separately.
    def worker():
        for i in range(2):
            until = time.monotonic() + 8
            grant = None
            while grant is None and time.monotonic() < until:
                grant = client.claim(f"fetch-{i}")
                if grant is None:
                    time.sleep(0.01)
            assert grant is not None
            with f.store.transaction() as conn:
                conn.execute("UPDATE account_leases SET request_interval=0")
            root = tmp_path / f"worker-{i}"
            root.mkdir()
            source = root / "manifest.json"
            source.write_bytes(client.fetch_input(grant, "manifest"))

            def perform(ordinal):
                for _ in range(150):
                    value = client.upstream(grant, ordinal)
                    if value["state"] == "ready":
                        return value["data"]
                    time.sleep(0.01)
                raise AssertionError("relay timeout")

            result = root / "result.bin"
            run_child(grant.kind, source, None, None, result, permit=perform)
            raw = result.read_bytes()
            client.stage(grant, result, digest(raw), len(raw))
            client.publish(grant, digest(raw), len(raw))

    with Database(settings.storage.db_path) as db:
        db.init_schema()
        storage = _Storage()
        # The API object carries no callable network methods. All content comes
        # back through worker HTTP and the validated fetch DTO.
        local_api = SimpleNamespace(access_token="A" * 32)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            def stopped():
                if future.done():
                    future.result()
                return False
            with execution_scope(settings, "bookmark", stop_requested=stopped):
                service = BookmarkNovelSyncService(local_api, db, storage, settings)
                first = service._sync_novel(
                    SimpleNamespace(id=42), "private", False, True, True, "bookmark_private", "7"
                )
                second = service._sync_novel(
                    SimpleNamespace(id=42), "private", False, True, True, "bookmark_private", "7"
                )
            future.result(timeout=10)
        assert first["novels"] == 1 and second["skipped"] == 1
        row = db.get_novel_detail(42)
        assert row["restrict_value"] == "private" and row["text_raw"] == "第一段\n\n第二段\n"
        assert db.conn.execute("SELECT count(*) FROM novels").fetchone()[0] == 1
        assert calls == [1, 2, 1, 2]
    client.close()


def test_explicit_remote_with_disabled_control_fails_without_network(tmp_path):
    settings = _settings(tmp_path)
    settings.sync.pixiv_execution = "remote"
    settings.pixiv.user_id = 7
    with pytest.raises(RemoteError, match="remote_disabled"):
        with execution_scope(settings, "bookmark"):
            pytest.fail("must not fall back locally")


def test_local_control_keeps_retries_and_spaces_each_physical_call(remote_web_fixture, tmp_path, monkeypatch):
    from pixiv_novel_sync.remote.execution import bind_api
    from pixiv_novel_sync.sync.utils import apply_pixiv_retry_patch

    settings = _settings(tmp_path / "local")
    settings.sync.pixiv_execution = "local"
    settings.pixiv.user_id = 7
    calls, permits = [], []

    class API:
        def novel_detail(self, novel_id):
            calls.append("detail")
            if len(calls) == 1:
                raise RuntimeError("connection interrupted")
            return {"novel": {"id": novel_id}}

        def webview_novel(self, novel_id):
            calls.append("body")
            return {"id": novel_id, "text": "Synthetic text"}

    monkeypatch.setattr("pixiv_novel_sync.sync.utils.cancellable_sleep", lambda *args: None)
    original = API()
    apply_pixiv_retry_patch(original)
    with execution_scope(settings, "novel_status") as scope:
        monkeypatch.setattr(scope, "local_permit", lambda: permits.append(True))
        api = bind_api(original)
        assert api.novel_detail(42)["novel"]["id"] == 42
        assert api.webview_novel(42)["text"] == "Synthetic text"
    assert calls == ["detail", "detail", "body"]
    assert len(permits) == 3


@pytest.mark.parametrize("method,params", [
    ("novel_detail", {"novel_id": None}),
    ("watchlist", {"page": None}),
    ("search_novel", {"word": None}),
])
def test_missing_remote_identity_is_rejected_before_upstream(method, params):
    from pixiv_novel_sync.remote.pixiv_calls import normalize_call

    with pytest.raises(RemoteError, match="invalid_pixiv_arguments"):
        normalize_call(method, params=params)

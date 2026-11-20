from concurrent.futures import ThreadPoolExecutor
import io
import json
import time
import uuid

import pytest

from pixiv_novel_sync.ai.models import AIProviderConfig, AIStreamChunk
from pixiv_novel_sync.ai.providers import AIProviderError
from pixiv_novel_sync.remote.ai_task import RemoteAIProvider, render_ai, validate_result
from pixiv_novel_sync.remote.blobs import BlobStore
from pixiv_novel_sync.remote.business import BusinessTasks
from pixiv_novel_sync.remote.contracts import KINDS, RemoteError, digest
from pixiv_novel_sync.remote.results import ResultService
from pixiv_novel_sync.remote.store import RemoteStore


class Provider:
    def __init__(self):
        self.config = AIProviderConfig(
            1, "Synthetic", "openai_compatible", "https://fixture.invalid/v1", "test-key", None
        )
        self.closed = False

    def close(self):
        self.closed = True


@pytest.mark.parametrize("outcome", ["success", "unknown", "cancel_before_send"])
def test_remote_ai_reserves_once_and_never_replays_unknown_result(tmp_path, monkeypatch, outcome):
    root = tmp_path / "remote"
    monkeypatch.setenv("PIXIV_REMOTE_ENABLED", "1")
    monkeypatch.setenv("PIXIV_REMOTE_STATE_DIR", str(root))
    monkeypatch.setenv("PIXIV_REMOTE_MACHINE_ID", "fixture")
    monkeypatch.setenv("PIXIV_REMOTE_WORKER_KEY_SHA256", "a" * 64)
    monkeypatch.setenv("PIXIV_REMOTE_TASK_SECRET", "K" * 32)
    store = RemoteStore(root, "fixture")
    store.migrate()
    blobs = BlobStore(root / "blobs")
    session = store.enroll(str(uuid.uuid4()), digest(b"session"), 1, "epub-v1", time.time())
    with store.transaction() as conn:
        conn.execute("UPDATE sessions SET task_kinds_json=?", (json.dumps(sorted(KINDS)),))
        conn.execute(
            "UPDATE machine_control SET mode='ready',provider_state='running',runtime_ready=1,"
            "last_provider_at=?,gate_ready_until=?,runtime_budget_until=-1",
            (time.time(), time.time() + 45),
        )
    local = Provider()
    local.config.proxy = "https://configured-proxy.example:8443"
    provider = RemoteAIProvider(local)
    guards, usage = [], []
    cancelled = [False]

    def worker():
        limit = time.monotonic() + 10
        grant = None
        while grant is None and time.monotonic() < limit:
            grant = store.claim(session["session_id"], "worker", time.time())
            if grant is None:
                time.sleep(0.01)
        assert grant is not None
        service = BusinessTasks(store, blobs)
        manifest = service.manifest(grant)
        with blobs.open_blob(manifest["text_sha256"]) as source:
            assert b"synthetic test" not in source.read()
        assert not hasattr(service, "credentials")
        from pixiv_novel_sync.remote.ai_task import require_authorization

        assert require_authorization(grant, manifest)["provider"]["proxy"] == local.config.proxy
        if outcome == "cancel_before_send":
            cancelled[0] = True
            with pytest.raises(RemoteError):
                service.permit(grant, 1)
            return
        assert service.permit(grant, 1)["allowed"]
        assert service.permit(grant, 1)["allowed"]
        output = json.dumps(
            {
                "text": "synthetic result" if outcome == "success" else "",
                "usage": None,
                "finish_reason": "stop" if outcome == "success" else "error",
                "outcome": "succeeded" if outcome == "success" else "unknown",
            }
        ).encode()
        results = ResultService(store, blobs)
        results.stage(grant, io.BytesIO(output), len(output), digest(output), time.time())
        results.publish(grant, digest(output), len(output), time.time())

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        stream = provider.stream_generate(
            [{"role": "user", "content": "synthetic test"}],
            "fixture",
            0.2,
            0.9,
            128,
            request_guard=lambda: guards.append(True) or 1,
            is_cancelled=lambda: cancelled[0],
            on_usage=lambda *args: usage.append(args),
        )
        if outcome == "success":
            chunks = list(stream)
            assert "".join(chunk.text for chunk in chunks) == "synthetic result"
            assert chunks[-1].type == "done"
        else:
            with pytest.raises(AIProviderError) as error:
                list(stream)
            assert error.value.category == "cancelled"
        future.result(timeout=12)
    assert len(guards) == (0 if outcome == "cancel_before_send" else 1)
    assert usage == []  # unknown is never reported as zero
    with store.read() as conn:
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM business_secrets").fetchone()[0] == 0
    provider.close()
    assert local.closed


def test_ai_worker_rejects_second_physical_request_and_preserves_unknown_usage():
    from dataclasses import asdict

    permits = []

    class RetryProvider(Provider):
        def stream_generate(self, *args, request_guard=None, **kwargs):
            request_guard()
            request_guard()
            yield AIStreamChunk("done", data={"finish_reason": "stop"})

    provider = RetryProvider()
    credentials = {"provider": {**asdict(provider.config), "max_retries": 0}}
    manifest = {"provider_id": 1, "model": "fixture", "temperature": 0.2, "top_p": 0.9, "max_tokens": 128}
    result = validate_result(
        render_ai(
            manifest,
            [{"role": "user", "content": "synthetic"}],
            credentials,
            lambda ordinal: permits.append(ordinal),
            provider_factory=lambda config: provider,
        )
    )
    assert permits == [1]
    assert result["outcome"] == "unknown" and result["usage"]["input_tokens"] is None
    assert provider.closed

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
import time

import pytest

from pixiv_novel_sync.ai import model_sync as model_sync_module
from pixiv_novel_sync.ai.model_sync import (
    ModelSyncCoordinator,
    ModelSyncConflictError,
    provider_model_sync_config_hash,
)
from pixiv_novel_sync.ai.model_catalog import (
    canonical_model_digest,
    normalize_model_record,
)
from pixiv_novel_sync.ai.models import ModelListResult
from pixiv_novel_sync.ai.providers import AIProviderError
from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "model-sync.db")
    database.init_schema()
    try:
        yield database
    finally:
        database.close()


def seed_provider(db: Database, *, name: str = "sync-provider") -> int:
    return db.create_ai_provider(
        {
            "name": name,
            "provider_type": "openai_compatible",
            "base_url": "https://api.example.test/v1",
            "enabled": True,
        }
    )


class FakeDiscoveryProvider:
    def __init__(self) -> None:
        self.config = SimpleNamespace(api_key="sk-secret")
        self.model_result = ModelListResult(
            models=[normalize_model_record({"id": "new-model"})],
            complete=True,
            empty_authoritative=False,
            pages=1,
            result_digest=canonical_model_digest(
                [normalize_model_record({"id": "new-model"})]
            ),
            partial_reason=None,
        )
        self.list_error: Exception | None = None
        self.wait_for_cancel = False
        self.started = Event()
        self.deadline: float | None = None
        self.deadline_remaining: float | None = None

    def list_models(self, *, on_page=None, is_cancelled=None, deadline=None):
        self.deadline = deadline
        if deadline is not None:
            self.deadline_remaining = deadline - time.monotonic()
        self.started.set()
        if is_cancelled is not None and is_cancelled():
            raise AIProviderError("sk-secret cancelled")
        if self.wait_for_cancel:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if is_cancelled is not None and is_cancelled():
                    raise AIProviderError("sk-secret cancelled")
                time.sleep(0.01)
            raise AssertionError("cancel callback was not observed")
        if self.list_error is not None:
            raise self.list_error
        if on_page is not None:
            on_page(self.model_result.pages, len(self.model_result.models))
        return self.model_result


@pytest.fixture
def fake_provider() -> FakeDiscoveryProvider:
    return FakeDiscoveryProvider()


@pytest.fixture
def coordinator(
    db: Database,
    fake_provider: FakeDiscoveryProvider,
):
    value = ModelSyncCoordinator(
        db.path,
        provider_resolver=lambda _db, _provider_id: fake_provider,
    )
    try:
        yield value
    finally:
        value.close()


def wait_for_operation(
    coordinator: ModelSyncCoordinator,
    operation_id: str,
    *,
    timeout: float = 3,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        operation = coordinator.get(operation_id)
        if operation["status"] in {
            "needs_empty_confirmation",
            "succeeded",
            "failed",
            "cancelled",
        }:
            return operation
        time.sleep(0.01)
    raise AssertionError("model sync operation did not reach a waiting/terminal state")


def test_create_sync_operation_leases_provider_and_rejects_duplicate(
    db: Database,
) -> None:
    provider_id = seed_provider(db)
    provider = db.get_ai_provider(provider_id, include_secret=True)
    config_hash = provider_model_sync_config_hash(provider)

    first = db.create_model_sync_operation(
        provider_id,
        provider["name"],
        config_hash,
        "owner-a",
    )

    assert first["provider_id"] == provider_id
    assert first["status"] == "queued"
    assert first["generation"] == 1
    assert first["cancel_requested"] is False
    assert "owner_token" not in first
    leased = db.get_ai_provider(provider_id, include_secret=True)
    assert leased["models_sync_generation"] == 1
    assert leased["models_sync_owner"] == "owner-a"
    assert leased["models_sync_lease_until"] is not None

    with pytest.raises(ModelSyncConflictError) as captured:
        db.create_model_sync_operation(
            provider_id,
            provider["name"],
            config_hash,
            "owner-b",
        )

    assert captured.value.existing_operation_id == first["operation_id"]


def test_concurrent_sync_creation_has_one_lease_winner(db: Database) -> None:
    provider_id = seed_provider(db)
    provider = db.get_ai_provider(provider_id, include_secret=True)
    config_hash = provider_model_sync_config_hash(provider)
    barrier = Barrier(2)

    def create(owner: str):
        worker_db = Database(db.path)
        try:
            barrier.wait(timeout=2)
            return worker_db.create_model_sync_operation(
                provider_id,
                provider["name"],
                config_hash,
                owner,
            )
        except ModelSyncConflictError as exc:
            return exc
        finally:
            worker_db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, ("owner-a", "owner-b")))

    operations = [result for result in results if isinstance(result, dict)]
    conflicts = [
        result for result in results if isinstance(result, ModelSyncConflictError)
    ]
    assert len(operations) == 1
    assert len(conflicts) == 1
    assert conflicts[0].existing_operation_id == operations[0]["operation_id"]


def test_claim_progress_and_success_commit_catalog_with_owner_cas(
    db: Database,
) -> None:
    provider_id = seed_provider(db)
    provider = db.get_ai_provider(provider_id, include_secret=True)
    operation = db.create_model_sync_operation(
        provider_id,
        provider["name"],
        provider_model_sync_config_hash(provider),
        "owner-a",
    )
    operation_id = operation["operation_id"]
    generation = operation["generation"]
    models = [normalize_model_record({"id": "model-a", "name": "Model A"})]
    digest = canonical_model_digest(models)

    assert db.claim_model_sync_operation(operation_id, "wrong", generation) is False
    assert db.claim_model_sync_operation(operation_id, "owner-a", generation) is True
    assert db.heartbeat_model_sync_operation(
        operation_id,
        "owner-a",
        generation,
    ) is True
    assert db.update_model_sync_progress(
        operation_id,
        "owner-a",
        generation,
        pages=1,
        discovered_count=1,
    ) is True
    assert db.finish_model_sync_success(
        operation_id,
        "owner-a",
        generation,
        models,
        digest,
    ) is True

    final = db.get_model_sync_operation(operation_id)
    assert final["status"] == "succeeded"
    assert final["pages"] == 1
    assert final["discovered_count"] == 1
    assert final["result_digest"] == digest
    synced = db.get_ai_provider(provider_id, include_secret=True)
    assert synced["models_synced_at"] is not None
    assert synced["models_sync_error"] is None
    assert synced["models_sync_owner"] is None
    assert synced["models_sync_lease_until"] is None
    catalog = db.list_ai_provider_models(provider_id)["items"]
    assert [item["model_key"] for item in catalog] == ["model-a"]
    assert catalog[0]["discovered_available"] is True
    assert db.finish_model_sync_success(
        operation_id,
        "owner-a",
        generation,
        models,
        digest,
    ) is False


def test_failure_preserves_previous_catalog_and_success_time(db: Database) -> None:
    provider_id = seed_provider(db)
    old_model = normalize_model_record({"id": "old-model"})
    db.upsert_discovered_models(
        provider_id,
        [old_model],
        generation=0,
    )
    db.conn.execute(
        "UPDATE ai_providers SET models_synced_at = '2026-01-02 03:04:05' WHERE id = ?",
        (provider_id,),
    )
    db.conn.commit()
    provider = db.get_ai_provider(provider_id, include_secret=True)
    operation = db.create_model_sync_operation(
        provider_id,
        provider["name"],
        provider_model_sync_config_hash(provider),
        "owner-a",
    )
    operation_id = operation["operation_id"]
    generation = operation["generation"]
    assert db.claim_model_sync_operation(operation_id, "owner-a", generation)

    assert db.finish_model_sync_failure(
        operation_id,
        "wrong-owner",
        generation,
        error_code="provider_error",
        error_message="[REDACTED] upstream failed",
    ) is False
    assert db.finish_model_sync_failure(
        operation_id,
        "owner-a",
        generation,
        error_code="provider_error",
        error_message="[REDACTED] upstream failed",
    ) is True

    final = db.get_model_sync_operation(operation_id)
    assert final["status"] == "failed"
    assert final["error_code"] == "provider_error"
    assert final["error_message"] == "[REDACTED] upstream failed"
    old_row = db.list_ai_provider_models(provider_id)["items"][0]
    assert old_row["model_key"] == "old-model"
    assert old_row["discovered_available"] is True
    failed_provider = db.get_ai_provider(provider_id, include_secret=True)
    assert failed_provider["models_synced_at"] == "2026-01-02 03:04:05"
    assert failed_provider["models_sync_attempted_at"] is not None
    assert failed_provider["models_sync_error"] == "[REDACTED] upstream failed"
    assert failed_provider["models_sync_owner"] is None
    assert failed_provider["models_sync_lease_until"] is None


def test_non_authoritative_empty_requires_exact_confirmation(db: Database) -> None:
    provider_id = seed_provider(db)
    db.upsert_discovered_models(
        provider_id,
        [normalize_model_record({"id": "old-model"})],
        generation=0,
    )
    provider = db.get_ai_provider(provider_id, include_secret=True)
    operation = db.create_model_sync_operation(
        provider_id,
        provider["name"],
        provider_model_sync_config_hash(provider),
        "owner-a",
    )
    operation_id = operation["operation_id"]
    generation = operation["generation"]
    empty_digest = canonical_model_digest([])
    assert db.claim_model_sync_operation(operation_id, "owner-a", generation)

    assert db.finish_model_sync_success(
        operation_id,
        "owner-a",
        generation,
        [],
        empty_digest,
    ) is True

    waiting = db.get_model_sync_operation(operation_id)
    assert waiting["status"] == "needs_empty_confirmation"
    assert waiting["result_digest"] == empty_digest
    assert waiting["finished_at"] is None
    assert db.list_ai_provider_models(provider_id)["items"][0][
        "discovered_available"
    ] is True
    released = db.get_ai_provider(provider_id, include_secret=True)
    assert released["models_sync_owner"] is None
    assert released["models_sync_lease_until"] is None

    with pytest.raises(ModelSyncConflictError):
        db.confirm_model_sync_empty(
            operation_id,
            generation + 1,
            empty_digest,
        )
    with pytest.raises(ModelSyncConflictError):
        db.confirm_model_sync_empty(
            operation_id,
            generation,
            "f" * 64,
        )

    stats = db.confirm_model_sync_empty(
        operation_id,
        generation,
        empty_digest,
    )
    assert stats == {"inserted": 0, "updated": 0}
    assert db.get_model_sync_operation(operation_id)["status"] == "succeeded"
    assert db.list_ai_provider_models(provider_id)["items"][0][
        "discovered_available"
    ] is False


def test_authoritative_empty_reconciles_without_confirmation(db: Database) -> None:
    provider_id = seed_provider(db)
    db.upsert_discovered_models(
        provider_id,
        [normalize_model_record({"id": "old-model"})],
        generation=0,
    )
    provider = db.get_ai_provider(provider_id, include_secret=True)
    operation = db.create_model_sync_operation(
        provider_id,
        provider["name"],
        provider_model_sync_config_hash(provider),
        "owner-a",
    )
    assert db.claim_model_sync_operation(
        operation["operation_id"],
        "owner-a",
        operation["generation"],
    )

    assert db.finish_model_sync_success(
        operation["operation_id"],
        "owner-a",
        operation["generation"],
        [],
        canonical_model_digest([]),
        empty_authoritative=True,
    )

    assert db.get_model_sync_operation(operation["operation_id"])["status"] == "succeeded"
    assert db.list_ai_provider_models(provider_id)["items"][0][
        "discovered_available"
    ] is False


def test_new_generation_invalidates_pending_empty_confirmation(db: Database) -> None:
    provider_id = seed_provider(db)
    provider = db.get_ai_provider(provider_id, include_secret=True)
    config_hash = provider_model_sync_config_hash(provider)
    first = db.create_model_sync_operation(
        provider_id,
        provider["name"],
        config_hash,
        "owner-a",
    )
    assert db.claim_model_sync_operation(
        first["operation_id"],
        "owner-a",
        first["generation"],
    )
    empty_digest = canonical_model_digest([])
    assert db.finish_model_sync_success(
        first["operation_id"],
        "owner-a",
        first["generation"],
        [],
        empty_digest,
    )
    second = db.create_model_sync_operation(
        provider_id,
        provider["name"],
        config_hash,
        "owner-b",
    )

    with pytest.raises(ModelSyncConflictError):
        db.confirm_model_sync_empty(
            first["operation_id"],
            first["generation"],
            empty_digest,
        )

    assert db.get_model_sync_operation(second["operation_id"])["status"] == "queued"


def test_provider_config_change_rejects_late_success(db: Database) -> None:
    provider_id = seed_provider(db)
    provider = db.get_ai_provider(provider_id, include_secret=True)
    operation = db.create_model_sync_operation(
        provider_id,
        provider["name"],
        provider_model_sync_config_hash(provider),
        "owner-a",
    )
    assert db.claim_model_sync_operation(
        operation["operation_id"],
        "owner-a",
        operation["generation"],
    )
    db.update_ai_provider(
        provider_id,
        {"base_url": "https://changed.example.test/v1"},
    )
    models = [normalize_model_record({"id": "late-model"})]

    assert db.finish_model_sync_success(
        operation["operation_id"],
        "owner-a",
        operation["generation"],
        models,
        canonical_model_digest(models),
    ) is False
    assert db.list_ai_provider_models(provider_id)["items"] == []


def test_cancel_flag_stops_lease_updates_and_owner_can_finish_cancelled(
    db: Database,
) -> None:
    provider_id = seed_provider(db)
    provider = db.get_ai_provider(provider_id, include_secret=True)
    operation = db.create_model_sync_operation(
        provider_id,
        provider["name"],
        provider_model_sync_config_hash(provider),
        "owner-a",
    )
    operation_id = operation["operation_id"]
    generation = operation["generation"]

    assert db.request_model_sync_cancel(operation_id) is True
    assert db.request_model_sync_cancel(operation_id) is False
    assert db.claim_model_sync_operation(operation_id, "owner-a", generation) is True
    assert db.heartbeat_model_sync_operation(
        operation_id,
        "owner-a",
        generation,
    ) is False
    assert db.finish_model_sync_failure(
        operation_id,
        "owner-a",
        generation,
        error_code="cancelled",
        error_message="模型同步已取消",
        cancelled=True,
    ) is True

    final = db.get_model_sync_operation(operation_id)
    assert final["status"] == "cancelled"
    assert final["cancel_requested"] is True


def test_late_worker_cannot_overwrite_new_generation(db: Database) -> None:
    provider_id = seed_provider(db)
    provider = db.get_ai_provider(provider_id, include_secret=True)
    config_hash = provider_model_sync_config_hash(provider)
    first = db.create_model_sync_operation(
        provider_id,
        provider["name"],
        config_hash,
        "owner-a",
    )
    db.conn.execute(
        "UPDATE ai_model_sync_operations SET lease_until = '2000-01-01 00:00:00' WHERE operation_id = ?",
        (first["operation_id"],),
    )
    db.conn.execute(
        "UPDATE ai_providers SET models_sync_lease_until = '2000-01-01 00:00:00' WHERE id = ?",
        (provider_id,),
    )
    db.conn.commit()

    second = db.create_model_sync_operation(
        provider_id,
        provider["name"],
        config_hash,
        "owner-b",
    )
    late_models = [normalize_model_record({"id": "late-model"})]

    assert db.finish_model_sync_success(
        first["operation_id"],
        "owner-a",
        first["generation"],
        late_models,
        canonical_model_digest(late_models),
    ) is False
    assert db.get_model_sync_operation(second["operation_id"])["status"] == "queued"
    assert db.list_ai_provider_models(provider_id)["items"] == []


def test_reconcile_times_out_only_stale_active_operations_and_cleanup(
    db: Database,
) -> None:
    now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)

    queued_provider = seed_provider(db, name="queued-provider")
    queued_row = db.get_ai_provider(queued_provider, include_secret=True)
    queued = db.create_model_sync_operation(
        queued_provider,
        queued_row["name"],
        provider_model_sync_config_hash(queued_row),
        "queued-owner",
    )
    db.conn.execute(
        """
        UPDATE ai_model_sync_operations
        SET created_at = ?, lease_until = ?
        WHERE operation_id = ?
        """,
        (
            (now - timedelta(minutes=6)).strftime("%Y-%m-%d %H:%M:%S"),
            (now + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
            queued["operation_id"],
        ),
    )

    running_provider = seed_provider(db, name="running-provider")
    running_row = db.get_ai_provider(running_provider, include_secret=True)
    running = db.create_model_sync_operation(
        running_provider,
        running_row["name"],
        provider_model_sync_config_hash(running_row),
        "running-owner",
    )
    assert db.claim_model_sync_operation(
        running["operation_id"],
        "running-owner",
        running["generation"],
    )
    db.conn.execute(
        """
        UPDATE ai_model_sync_operations
        SET heartbeat_at = ?, lease_until = ?
        WHERE operation_id = ?
        """,
        (
            (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),
            (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
            running["operation_id"],
        ),
    )
    db.conn.execute(
        "UPDATE ai_providers SET models_sync_lease_until = ? WHERE id = ?",
        (
            (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
            running_provider,
        ),
    )

    terminal_provider = seed_provider(db, name="terminal-provider")
    terminal_row = db.get_ai_provider(terminal_provider, include_secret=True)
    terminal = db.create_model_sync_operation(
        terminal_provider,
        terminal_row["name"],
        provider_model_sync_config_hash(terminal_row),
        "terminal-owner",
    )
    assert db.claim_model_sync_operation(
        terminal["operation_id"],
        "terminal-owner",
        terminal["generation"],
    )
    assert db.finish_model_sync_failure(
        terminal["operation_id"],
        "terminal-owner",
        terminal["generation"],
        error_code="provider_error",
        error_message="terminal",
    )
    db.conn.commit()

    assert db.reconcile_model_sync_operations(now=now) == 2
    assert db.get_model_sync_operation(queued["operation_id"])["error_code"] == "queue_timeout"
    assert db.get_model_sync_operation(running["operation_id"])["error_code"] == "process_interrupted"
    assert db.get_model_sync_operation(terminal["operation_id"])["error_code"] == "provider_error"

    old_finished = (now - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
    db.conn.execute(
        "UPDATE ai_model_sync_operations SET finished_at = ? WHERE operation_id IN (?, ?)",
        (old_finished, queued["operation_id"], running["operation_id"]),
    )
    db.conn.commit()
    assert db.cleanup_model_sync_operations(keep_days=3) == 2
    assert db.get_model_sync_operation(queued["operation_id"]) is None
    assert db.get_model_sync_operation(running["operation_id"]) is None
    assert db.get_model_sync_operation(terminal["operation_id"]) is not None


def test_coordinator_failure_redacts_secret_and_preserves_catalog(
    coordinator: ModelSyncCoordinator,
    db: Database,
    fake_provider: FakeDiscoveryProvider,
) -> None:
    provider_id = seed_provider(db)
    db.upsert_discovered_models(
        provider_id,
        [normalize_model_record({"id": "old-model"})],
        generation=0,
    )
    db.conn.execute(
        "UPDATE ai_providers SET models_synced_at = '2026-01-02 03:04:05' WHERE id = ?",
        (provider_id,),
    )
    db.conn.commit()
    fake_provider.list_error = AIProviderError("sk-secret upstream failed")

    assert coordinator._executor._max_workers == 2
    assert coordinator._executor._thread_name_prefix == "ai-model-sync"

    operation = coordinator.start(provider_id)
    assert operation["status"] == "queued"
    final = wait_for_operation(coordinator, operation["operation_id"])

    assert final["status"] == "failed"
    assert "sk-secret" not in final["error_message"]
    assert "[REDACTED]" in final["error_message"]
    assert db.list_ai_provider_models(provider_id)["items"][0][
        "discovered_available"
    ] is True
    provider = db.get_ai_provider(provider_id, include_secret=True)
    assert provider["models_synced_at"] == "2026-01-02 03:04:05"


def test_coordinator_empty_result_requires_confirmation_and_exact_event(
    coordinator: ModelSyncCoordinator,
    db: Database,
    fake_provider: FakeDiscoveryProvider,
) -> None:
    provider_id = seed_provider(db)
    db.upsert_discovered_models(
        provider_id,
        [normalize_model_record({"id": "old-model"})],
        generation=0,
    )
    empty_digest = canonical_model_digest([])
    fake_provider.model_result = ModelListResult(
        models=[],
        complete=True,
        empty_authoritative=False,
        pages=1,
        result_digest=empty_digest,
        partial_reason=None,
    )

    operation = coordinator.start(provider_id)
    waiting = wait_for_operation(coordinator, operation["operation_id"])
    events = list(coordinator.events(operation["operation_id"], poll_interval=0.01))

    assert waiting["status"] == "needs_empty_confirmation"
    assert [event["event"] for event in events] == [
        "started",
        "page",
        "empty_confirmation_required",
    ]
    assert events[-1]["data"] == {
        "operation_id": operation["operation_id"],
        "generation": waiting["generation"],
        "result_digest": empty_digest,
    }
    with pytest.raises(ModelSyncConflictError):
        coordinator.confirm_empty(
            operation["operation_id"],
            waiting["generation"] + 1,
            empty_digest,
        )
    coordinator.confirm_empty(
        operation["operation_id"],
        waiting["generation"],
        empty_digest,
    )
    assert coordinator.get(operation["operation_id"])["status"] == "succeeded"
    assert db.list_ai_provider_models(provider_id)["items"][0][
        "discovered_available"
    ] is False


def test_coordinator_cancel_stops_before_another_request_and_closes_cancelled(
    coordinator: ModelSyncCoordinator,
    db: Database,
    fake_provider: FakeDiscoveryProvider,
) -> None:
    provider_id = seed_provider(db)
    fake_provider.wait_for_cancel = True
    operation = coordinator.start(provider_id)
    assert fake_provider.started.wait(timeout=2)

    assert coordinator.cancel(operation["operation_id"]) is True
    final = wait_for_operation(coordinator, operation["operation_id"])

    assert final["status"] == "cancelled"
    events = list(coordinator.events(operation["operation_id"], poll_interval=0.01))
    assert events[-1] == {
        "event": "cancelled",
        "data": {"operation_id": operation["operation_id"]},
    }


def test_coordinator_enforces_sync_deadline(
    coordinator: ModelSyncCoordinator,
    db: Database,
    fake_provider: FakeDiscoveryProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_id = seed_provider(db)
    monkeypatch.setattr(model_sync_module, "_MODEL_SYNC_DEADLINE_SECONDS", 0)

    operation = coordinator.start(provider_id)
    final = wait_for_operation(coordinator, operation["operation_id"])

    assert final["status"] == "failed"
    assert final["error_code"] == "deadline_exceeded"
    assert "10 分钟" in final["error_message"]


def test_coordinator_bounds_provider_requests_with_absolute_deadline(
    coordinator: ModelSyncCoordinator,
    db: Database,
    fake_provider: FakeDiscoveryProvider,
) -> None:
    provider_id = seed_provider(db)
    operation = coordinator.start(provider_id)
    final = wait_for_operation(coordinator, operation["operation_id"])

    assert final["status"] == "succeeded"
    assert fake_provider.deadline is not None
    assert fake_provider.deadline_remaining is not None
    assert 0 < fake_provider.deadline_remaining <= 10 * 60


def test_service_core_reuses_and_closes_one_model_sync_coordinator(
    db: Database,
    fake_provider: FakeDiscoveryProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_id = seed_provider(db)
    service = AIWritingService(db.path)
    monkeypatch.setattr(service, "_get_provider", lambda _config: fake_provider)

    try:
        operation = service.start_model_sync(provider_id)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            final = service.get_model_sync_operation(operation["operation_id"])
            if final["status"] == "succeeded":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("service model sync did not finish")

        coordinator_instance = service._model_sync_coordinator
        assert coordinator_instance is not None
        events = list(
            service.iter_model_sync_events(
                operation["operation_id"],
                poll_interval=0.01,
            )
        )
        assert [event["event"] for event in events] == [
            "started",
            "page",
            "completed",
        ]
        assert service._model_sync() is coordinator_instance
    finally:
        service.close()

    assert coordinator_instance._closed is True

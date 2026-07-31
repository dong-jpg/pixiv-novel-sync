from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from flask import Flask

from pixiv_novel_sync.ai.model_catalog import (
    canonical_model_digest,
    normalize_model_record,
)
from pixiv_novel_sync.ai.models import AIStreamChunk, ModelListResult
from pixiv_novel_sync.ai.providers import AIProviderError
from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.ai_web import register_ai_routes
from pixiv_novel_sync.settings import (
    PixivSettings,
    Settings,
    StorageSettings,
    SyncSettings,
)
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.webapp import create_app


class BlockingDiscoveryProvider:
    def __init__(self) -> None:
        self.config = SimpleNamespace(api_key="sk-model-api-secret")
        self.started = Event()
        self.release = Event()
        models = [normalize_model_record({"id": "discovered-model"})]
        self.result = ModelListResult(
            models=models,
            complete=True,
            empty_authoritative=False,
            pages=1,
            result_digest=canonical_model_digest(models),
            partial_reason=None,
        )
        self.generate_calls: list[str] = []

    def estimate_message_tokens(
        self,
        messages: list[dict[str, str]],
    ) -> None:
        del messages
        return None

    def stream_generate(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
        *,
        request_guard=None,
        is_cancelled=None,
    ) -> Iterator[AIStreamChunk]:
        del messages, temperature, top_p, max_tokens
        if is_cancelled is not None and is_cancelled():
            raise AIProviderError("请求已取消")
        self.generate_calls.append(model)
        if request_guard is not None:
            request_guard()
        yield AIStreamChunk(type="delta", text="恢复生成正文")
        yield AIStreamChunk(type="done", data={"finish_reason": "stop"})

    def list_models(self, *, on_page=None, is_cancelled=None, deadline=None):
        self.started.set()
        while not self.release.wait(0.01):
            if is_cancelled is not None and is_cancelled():
                raise AIProviderError("模型同步已取消")
        if on_page is not None:
            on_page(self.result.pages, len(self.result.models))
        return self.result


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        pixiv=PixivSettings(
            refresh_token="",
            access_token=None,
            proxy=None,
            timeout=30,
            verify_ssl=True,
            user_id=None,
        ),
        sync=SyncSettings(
            enabled=True,
            initial_manual_only=False,
            download_assets=False,
            write_markdown=True,
            write_raw_text=True,
            bookmark_restricts=["public"],
            max_items_per_run=None,
            max_pages_per_run=None,
            delay_seconds_between_items=0,
            delay_seconds_between_pages=0,
        ),
        storage=StorageSettings(
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
            db_path=tmp_path / "model-api.db",
        ),
    )


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = make_settings(tmp_path)
    fake_provider = BlockingDiscoveryProvider()
    monkeypatch.setattr(
        AIWritingService,
        "_get_provider",
        lambda _self, _config: fake_provider,
    )
    app = Flask(__name__)
    app.secret_key = "model-api-test-secret"
    app.config["TESTING"] = True
    register_ai_routes(app, settings)
    client = app.test_client()
    database = Database(settings.storage.db_path)
    database.init_schema()
    try:
        yield SimpleNamespace(
            app=app,
            client=client,
            csrf="test-csrf-token",
            db=database,
            fake_provider=fake_provider,
        )
    finally:
        fake_provider.release.set()
        manager = app.extensions.get("pixiv_novel_sync.ai_service")
        if manager is not None:
            manager.close()
        database.close()


@pytest.fixture
def seeded_provider(api) -> int:
    return api.db.create_ai_provider(
        {
            "name": "model-api-provider",
            "provider_type": "openai_compatible",
            "base_url": "https://api.example.test/v1",
            "enabled": True,
        }
    )


@pytest.fixture
def seeded_models(api, seeded_provider: int) -> list[int]:
    return [
        api.db.create_ai_provider_model(
            {
                "provider_id": seeded_provider,
                "model_key": model_key,
                "enabled": True,
            }
        )
        for model_key in ("manual-a", "manual-b")
    ]


@pytest.fixture
def seeded_pool(api) -> int:
    return api.db.create_ai_model_pool(
        {
            "name": "主模型池",
            "description": "",
            "pool_kind": "primary",
            "enabled": False,
        }
    )


def test_sync_start_is_202_and_duplicate_is_409(
    api,
    seeded_provider: int,
) -> None:
    path = f"/api/dashboard/ai/providers/{seeded_provider}/models/sync"

    first = api.client.post(path, headers={"X-CSRF-Token": api.csrf})

    assert first.status_code == 202, first.get_json()
    operation_id = first.get_json()["data"]["operation_id"]
    duplicate = api.client.post(path, headers={"X-CSRF-Token": api.csrf})
    assert duplicate.status_code == 409
    assert duplicate.get_json()["data"]["operation_id"] == operation_id


def test_provider_model_api_exposes_three_counts_and_never_secret(
    api,
    seeded_provider: int,
    seeded_models: list[int],
) -> None:
    response = api.client.get(
        f"/api/dashboard/ai/providers/{seeded_provider}/models"
    )

    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert {"total", "discovered_available", "routable", "items"} <= payload.keys()
    assert payload["total"] == len(seeded_models)
    assert "api_key" not in json.dumps(payload).lower()
    assert "sk-model-api-secret" not in json.dumps(payload)


def test_provider_list_never_exposes_model_sync_owner(
    api,
    seeded_provider: int,
) -> None:
    api.db.conn.execute(
        "UPDATE ai_providers SET models_sync_owner = ? WHERE id = ?",
        ("owner-secret-token", seeded_provider),
    )
    api.db.conn.commit()

    response = api.client.get("/api/dashboard/ai/providers")

    assert response.status_code == 200
    serialized = json.dumps(response.get_json(), ensure_ascii=False)
    assert "models_sync_owner" not in serialized
    assert "owner-secret-token" not in serialized


def test_pool_member_stale_version_returns_409(
    api,
    seeded_pool: int,
    seeded_models: list[int],
) -> None:
    response = api.client.put(
        f"/api/dashboard/ai/model-pools/{seeded_pool}/members",
        json={
            "expected_version": 0,
            "members": [{"provider_model_id": seeded_models[0]}],
        },
        headers={"X-CSRF-Token": api.csrf},
    )

    assert response.status_code == 409
    assert "版本" in response.get_json()["error"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/dashboard/ai/providers/1/models"),
        ("post", "/api/dashboard/ai/model-pools"),
        ("put", "/api/dashboard/ai/model-pools/1/members"),
    ],
)
def test_new_write_routes_reject_non_object_json(api, method: str, path: str) -> None:
    response = getattr(api.client, method)(
        path,
        json=["not", "an", "object"],
        headers={"X-CSRF-Token": api.csrf},
    )

    assert response.status_code == 400
    assert "JSON 对象" in response.get_json()["error"]


def test_manual_model_crud_and_update_field_whitelist(
    api,
    seeded_provider: int,
) -> None:
    created = api.client.post(
        f"/api/dashboard/ai/providers/{seeded_provider}/models",
        json={
            "model_key": "manual-model",
            "manual_display_name": "人工模型",
            "manual_capabilities": ["streaming", "json"],
            "manual_context_window": 32000,
            "enabled": True,
        },
        headers={"X-CSRF-Token": api.csrf},
    )

    assert created.status_code == 200
    model_id = created.get_json()["data"]["id"]
    updated = api.client.put(
        f"/api/dashboard/ai/provider-models/{model_id}",
        json={
            "enabled": False,
            "manual_display_name": "人工覆盖",
            "manual_capabilities": ["vision"],
            "manual_context_window": 64000,
        },
        headers={"X-CSRF-Token": api.csrf},
    )
    assert updated.status_code == 200

    rejected = api.client.put(
        f"/api/dashboard/ai/provider-models/{model_id}",
        json={"discovered_available": True},
        headers={"X-CSRF-Token": api.csrf},
    )
    assert rejected.status_code == 400
    assert "discovered_available" in rejected.get_json()["error"]

    listed = api.client.get(
        f"/api/dashboard/ai/providers/{seeded_provider}/models"
    ).get_json()["data"]
    item = next(value for value in listed["items"] if value["id"] == model_id)
    assert item["enabled"] is False
    assert item["display_name"] == "人工覆盖"
    assert item["capabilities"] == ["vision"]
    assert item["context_window"] == 64000

    deleted = api.client.delete(
        f"/api/dashboard/ai/provider-models/{model_id}",
        headers={"X-CSRF-Token": api.csrf},
    )
    assert deleted.status_code == 200
    assert api.db.get_ai_provider_model(model_id) is None


def test_model_list_filters_search_and_routable_state(
    api,
    seeded_provider: int,
    seeded_models: list[int],
) -> None:
    api.db.update_ai_provider_model(seeded_models[1], {"enabled": False})

    response = api.client.get(
        f"/api/dashboard/ai/providers/{seeded_provider}/models"
        "?search=manual&routable_only=true"
    )

    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert [item["model_key"] for item in payload["items"]] == ["manual-a"]
    assert payload["total"] == 2
    assert payload["routable"] == 1


def test_sync_operation_query_cancel_and_sse_whitelist(
    api,
    seeded_provider: int,
) -> None:
    started = api.client.post(
        f"/api/dashboard/ai/providers/{seeded_provider}/models/sync",
        headers={"X-CSRF-Token": api.csrf},
    )
    assert started.status_code == 202, started.get_json()
    operation_id = started.get_json()["data"]["operation_id"]

    queried = api.client.get(
        f"/api/dashboard/ai/model-sync-operations/{operation_id}"
    )
    assert queried.status_code == 200
    serialized = json.dumps(queried.get_json(), ensure_ascii=False).lower()
    assert "owner_token" not in serialized
    assert "provider_config_hash" not in serialized
    assert "sk-model-api-secret" not in serialized

    cancelled = api.client.delete(
        f"/api/dashboard/ai/model-sync-operations/{operation_id}",
        headers={"X-CSRF-Token": api.csrf},
    )
    assert cancelled.status_code == 200
    final = wait_for_sync_status(api.client, operation_id, {"cancelled"})
    assert final["status"] == "cancelled"

    events_response = api.client.get(
        f"/api/dashboard/ai/model-sync-operations/{operation_id}/events"
    )
    assert events_response.status_code == 200
    assert events_response.mimetype == "text/event-stream"
    body = events_response.get_data(as_text=True)
    event_names = {
        line.removeprefix("event: ")
        for line in body.splitlines()
        if line.startswith("event: ")
    }
    assert event_names <= {
        "started",
        "page",
        "empty_confirmation_required",
        "completed",
        "failed",
        "cancelled",
    }
    assert event_names == {"started", "cancelled"}


def test_empty_confirmation_requires_exact_generation_and_digest(
    api,
    seeded_provider: int,
) -> None:
    empty_digest = canonical_model_digest([])
    api.fake_provider.result = ModelListResult(
        models=[],
        complete=True,
        empty_authoritative=False,
        pages=1,
        result_digest=empty_digest,
        partial_reason=None,
    )
    api.fake_provider.release.set()
    started = api.client.post(
        f"/api/dashboard/ai/providers/{seeded_provider}/models/sync",
        headers={"X-CSRF-Token": api.csrf},
    )
    assert started.status_code == 202, started.get_json()
    operation_id = started.get_json()["data"]["operation_id"]
    waiting = wait_for_sync_status(
        api.client,
        operation_id,
        {"needs_empty_confirmation"},
    )

    events = api.client.get(
        f"/api/dashboard/ai/model-sync-operations/{operation_id}/events"
    ).get_data(as_text=True)
    event_payloads = [
        json.loads(line.removeprefix("data: "))
        for line in events.splitlines()
        if line.startswith("data: ")
    ]
    empty_event = event_payloads[-1]
    assert set(empty_event) == {"operation_id", "generation", "result_digest"}

    stale = api.client.post(
        f"/api/dashboard/ai/model-sync-operations/{operation_id}/confirm-empty",
        json={
            "generation": waiting["generation"] + 1,
            "result_digest": empty_digest,
        },
        headers={"X-CSRF-Token": api.csrf},
    )
    assert stale.status_code == 409

    confirmed = api.client.post(
        f"/api/dashboard/ai/model-sync-operations/{operation_id}/confirm-empty",
        json={
            "generation": waiting["generation"],
            "result_digest": empty_digest,
        },
        headers={"X-CSRF-Token": api.csrf},
    )
    assert confirmed.status_code == 200
    assert confirmed.get_json()["data"] == {"inserted": 0, "updated": 0}


def wait_for_sync_status(client, operation_id: str, statuses: set[str]) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/dashboard/ai/model-sync-operations/{operation_id}"
        )
        if response.status_code == 200:
            operation = response.get_json()["data"]
            if operation["status"] in statuses:
                return operation
        time.sleep(0.01)
    raise AssertionError("模型同步 operation 未进入预期状态")


def test_pool_crud_member_replacement_and_attempt_summary(
    api,
    seeded_models: list[int],
) -> None:
    created = api.client.post(
        "/api/dashboard/ai/model-pools",
        json={
            "name": "API 模型池",
            "description": "初始说明",
            "pool_kind": "custom",
            "enabled": False,
        },
        headers={"X-CSRF-Token": api.csrf},
    )
    assert created.status_code == 200
    pool_id = created.get_json()["data"]["id"]

    fetched = api.client.get(f"/api/dashboard/ai/model-pools/{pool_id}")
    assert fetched.status_code == 200
    assert fetched.get_json()["data"]["version"] == 1

    updated = api.client.put(
        f"/api/dashboard/ai/model-pools/{pool_id}",
        json={"expected_version": 1, "name": "已更新模型池"},
        headers={"X-CSRF-Token": api.csrf},
    )
    assert updated.status_code == 200
    assert updated.get_json()["data"]["version"] == 2

    replaced = api.client.put(
        f"/api/dashboard/ai/model-pools/{pool_id}/members",
        json={
            "expected_version": 2,
            "members": [
                {"provider_model_id": seeded_models[1], "enabled": True},
                {"provider_model_id": seeded_models[0], "enabled": False},
            ],
        },
        headers={"X-CSRF-Token": api.csrf},
    )
    assert replaced.status_code == 200
    assert replaced.get_json()["data"]["version"] == 3

    detail = api.client.get(f"/api/dashboard/ai/model-pools/{pool_id}").get_json()[
        "data"
    ]
    assert [member["provider_model_id"] for member in detail["members"]] == [
        seeded_models[1],
        seeded_models[0],
    ]
    assert [member["position"] for member in detail["members"]] == [1, 2]

    listed = api.client.get("/api/dashboard/ai/model-pools").get_json()["data"]
    assert any(pool["id"] == pool_id for pool in listed)
    attempts = api.client.get(
        f"/api/dashboard/ai/model-pools/{pool_id}/attempts?limit=10"
    )
    assert attempts.status_code == 200
    assert attempts.get_json()["data"] == []

    deleted = api.client.delete(
        f"/api/dashboard/ai/model-pools/{pool_id}",
        headers={"X-CSRF-Token": api.csrf},
    )
    assert deleted.status_code == 200
    assert api.client.get(f"/api/dashboard/ai/model-pools/{pool_id}").status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/dashboard/ai/providers/999/models", None),
        ("post", "/api/dashboard/ai/providers/999/models/sync", None),
        ("put", "/api/dashboard/ai/provider-models/999", {"enabled": True}),
        ("delete", "/api/dashboard/ai/provider-models/999", None),
        ("get", "/api/dashboard/ai/model-pools/999", None),
        ("put", "/api/dashboard/ai/model-pools/999", {"expected_version": 1}),
        ("delete", "/api/dashboard/ai/model-pools/999", None),
        ("get", "/api/dashboard/ai/model-pools/999/attempts", None),
        ("get", "/api/dashboard/ai/model-sync-operations/missing", None),
        ("get", "/api/dashboard/ai/model-sync-operations/missing/events", None),
        ("delete", "/api/dashboard/ai/model-sync-operations/missing", None),
        (
            "post",
            "/api/dashboard/ai/model-sync-operations/missing/confirm-empty",
            {"generation": 1, "result_digest": "0" * 64},
        ),
    ],
)
def test_new_api_missing_resources_return_404(
    api,
    method: str,
    path: str,
    payload: dict | None,
) -> None:
    kwargs = {"headers": {"X-CSRF-Token": api.csrf}}
    if payload is not None:
        kwargs["json"] = payload
    response = getattr(api.client, method)(path, **kwargs)

    assert response.status_code == 404
    assert "不存在" in response.get_json()["error"]


def test_confirm_empty_rejects_extra_client_fields(api) -> None:
    response = api.client.post(
        "/api/dashboard/ai/model-sync-operations/missing/confirm-empty",
        json={
            "generation": 1,
            "result_digest": "0" * 64,
            "provider_config_hash": "1" * 64,
        },
        headers={"X-CSRF-Token": api.csrf},
    )

    assert response.status_code == 400
    assert "provider_config_hash" in response.get_json()["error"]


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"name": "坏\x00名称", "pool_kind": "custom"}, "name"),
        (
            {
                "name": "坏说明池",
                "description": "第一行\n第二行",
                "pool_kind": "custom",
            },
            "description",
        ),
        (
            {
                "name": "坏后备池",
                "pool_kind": "custom",
                "fallback_pool_id": "1",
            },
            "fallback_pool_id",
        ),
    ],
)
def test_pool_api_rejects_control_text_and_non_integer_fallback(
    api,
    payload: dict,
    field: str,
) -> None:
    response = api.client.post(
        "/api/dashboard/ai/model-pools",
        json=payload,
        headers={"X-CSRF-Token": api.csrf},
    )

    assert response.status_code == 400
    assert field in response.get_json()["error"]


def test_referenced_manual_model_and_pool_deletes_return_409(
    api,
    seeded_provider: int,
    seeded_models: list[int],
    seeded_pool: int,
) -> None:
    version = api.db.replace_ai_model_pool_members(
        seeded_pool,
        [{"provider_model_id": seeded_models[0]}],
        expected_version=1,
    )
    api.db.update_ai_model_pool(
        seeded_pool,
        {"enabled": True},
        expected_version=version,
    )
    api.db.create_ai_agent(
        {
            "name": "池绑定 Agent",
            "task_type": "continue",
            "binding_type": "pool",
            "model_pool_id": seeded_pool,
            "system_prompt": "test",
            "enabled": True,
        }
    )

    model_response = api.client.delete(
        f"/api/dashboard/ai/provider-models/{seeded_models[0]}",
        headers={"X-CSRF-Token": api.csrf},
    )
    pool_response = api.client.delete(
        f"/api/dashboard/ai/model-pools/{seeded_pool}",
        headers={"X-CSRF-Token": api.csrf},
    )

    assert model_response.status_code == 409
    assert "引用" in model_response.get_json()["error"]
    assert pool_response.status_code == 409
    assert "引用" in pool_response.get_json()["error"]


def test_model_api_mutations_use_dashboard_csrf(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "model-api-token")
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "model-api-csrf-secret")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PIXIV_REFRESH_TOKEN=test\nDASHBOARD_TOKEN=model-api-token\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "storage:\n"
        f"  public_dir: {(tmp_path / 'public').as_posix()}\n"
        f"  private_dir: {(tmp_path / 'private').as_posix()}\n"
        f"  db_path: {(tmp_path / 'csrf.db').as_posix()}\n"
        "sync:\n"
        "  auto_sync_enabled: false\n",
        encoding="utf-8",
    )
    app = create_app(
        config_path=str(config_path),
        env_path=str(env_path),
        start_scheduler=False,
    )
    client = app.test_client()
    assert client.post(
        "/api/auth/login",
        data={"token": "model-api-token"},
    ).status_code == 302

    blocked = client.post(
        "/api/dashboard/ai/model-pools",
        json={"name": "CSRF 池", "pool_kind": "custom"},
    )
    csrf = client.get("/api/csrf-token").get_json()["csrf_token"]
    allowed = client.post(
        "/api/dashboard/ai/model-pools",
        json={"name": "CSRF 池", "pool_kind": "custom"},
        headers={"X-CSRF-Token": csrf},
    )
    continue_payload = {
        "parent_job_id": "missing-parent",
        "idempotency_key": "continue-00000003",
        "candidate_snapshot_hash": "f" * 64,
        "resume_candidate_index": 0,
    }
    blocked_continue = client.post(
        "/api/dashboard/ai/jobs/missing-parent/continue",
        json=continue_payload,
    )
    allowed_continue = client.post(
        "/api/dashboard/ai/jobs/missing-parent/continue",
        json=continue_payload,
        headers={"X-CSRF-Token": csrf},
    )

    assert blocked.status_code == 403
    assert allowed.status_code == 200
    assert blocked_continue.status_code == 403
    assert allowed_continue.status_code == 404
    manager = app.extensions.get("pixiv_novel_sync.ai_service")
    if manager is not None:
        manager.close()


def test_registration_reconciles_sync_once_and_exposes_reusable_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[AIWritingService] = []

    def fake_reconcile(self) -> int:
        calls.append(self)
        return 0

    monkeypatch.setattr(
        AIWritingService,
        "reconcile_model_sync_operations",
        fake_reconcile,
    )
    app = Flask(__name__)
    app.secret_key = "model-api-registration-secret"
    register_ai_routes(app, make_settings(tmp_path))

    manager = app.extensions["pixiv_novel_sync.ai_service"]
    first = manager._current()
    second = manager._current()

    assert calls == [first]
    assert second is first
    manager.close()


def test_sync_reconciliation_still_runs_when_job_reconciliation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_calls: list[AIWritingService] = []

    def fail_job_reconcile(self, older_than_minutes=30):
        raise RuntimeError("job reconcile failed")

    def fake_sync_reconcile(self) -> int:
        sync_calls.append(self)
        return 0

    monkeypatch.setattr(Database, "fail_stale_ai_jobs", fail_job_reconcile)
    monkeypatch.setattr(
        AIWritingService,
        "reconcile_model_sync_operations",
        fake_sync_reconcile,
    )
    app = Flask(__name__)
    app.secret_key = "model-api-reconcile-secret"
    register_ai_routes(app, make_settings(tmp_path))

    manager = app.extensions["pixiv_novel_sync.ai_service"]
    assert sync_calls == [manager._current()]
    manager.close()


def seed_api_partial_continue_job(api) -> SimpleNamespace:
    provider_ids: list[int] = []
    model_ids: list[int] = []
    for index in range(2):
        provider_id = api.db.create_ai_provider(
            {
                "name": f"continue-provider-{index + 1}",
                "provider_type": "openai_compatible",
                "base_url": f"https://continue-{index + 1}.example.test/v1",
                "context_window": 16_000,
                "enabled": True,
            }
        )
        provider_ids.append(provider_id)
        model_ids.append(
            api.db.create_ai_provider_model(
                {
                    "provider_id": provider_id,
                    "model_key": f"continue-model-{index + 1}",
                    "manual_context_window": 16_000,
                    "enabled": True,
                }
            )
        )
    pool_id = api.db.create_ai_model_pool(
        {"name": "继续任务模型池", "pool_kind": "custom"}
    )
    version = api.db.replace_ai_model_pool_members(
        pool_id,
        [
            {"provider_model_id": model_id, "enabled": True}
            for model_id in model_ids
        ],
        expected_version=1,
    )
    api.db.update_ai_model_pool(
        pool_id,
        {"enabled": True},
        expected_version=version,
    )
    agent_id = api.db.create_ai_agent(
        {
            "name": "继续任务 Agent",
            "task_type": "continue",
            "binding_type": "pool",
            "provider_id": None,
            "model": None,
            "model_pool_id": pool_id,
            "system_prompt": "继续写作",
            "max_tokens": 1_000,
            "context_window": 16_000,
            "enabled": True,
        }
    )
    service = api.app.extensions["pixiv_novel_sync.ai_service"]._current()
    agent = service._load_agent_config(api.db, agent_id)
    snapshot = service.model_router.resolve_candidates(agent)
    parent_job_id = "api-partial-parent"
    owner_token = "api-partial-owner"
    api.db.create_ai_job(
        parent_job_id,
        "continue",
        agent_id,
        {
            "agent_id": agent_id,
            "source_type": "manual",
            "text": "已有正文",
            "smart_context": False,
            "context_chars": 2_000,
        },
        owner_token=owner_token,
        route_deadline_at="2099-01-01 00:00:00",
    )
    assert api.db.set_ai_job_candidate_snapshot(
        parent_job_id,
        owner_token,
        {
            "agent_config_hash": snapshot.agent_config_hash,
            "binding_version": snapshot.binding_version,
            "candidates": [asdict(candidate) for candidate in snapshot.candidates],
        },
        snapshot.snapshot_hash,
    )
    candidate = snapshot.candidates[0]
    attempt_index = api.db.allocate_ai_model_attempt(
        parent_job_id,
        owner_token,
        {
            "pool_id": candidate.pool_id,
            "provider_id": candidate.provider_id,
            "provider_model_id": candidate.provider_model_id,
            "pool_version_snapshot": candidate.pool_version,
            "pool_position_snapshot": candidate.pool_position,
            "model_key": candidate.model_key,
            "pool_name_snapshot": candidate.pool_name,
            "provider_name_snapshot": candidate.provider_name,
            "agent_config_hash": snapshot.agent_config_hash,
            "provider_config_hash": candidate.provider_config_hash,
            "candidate_list_hash": snapshot.snapshot_hash,
            "stage": "main",
        },
    )
    assert api.db.mark_ai_job_output_started(
        parent_job_id,
        attempt_index,
        owner_token,
        candidate.candidate_index,
    )
    assert api.db.finish_ai_model_attempt(
        parent_job_id,
        attempt_index,
        owner_token,
        "partial",
        output_started=True,
        error_category="network",
        error_message="连接中断",
        finish_reason="error",
    )
    assert api.db.finish_ai_job_cas(
        parent_job_id,
        owner_token,
        "partial",
        output_text="半截",
        error_message="连接中断",
    )
    return SimpleNamespace(
        parent_job_id=parent_job_id,
        snapshot=snapshot,
        remaining_provider_id=provider_ids[1],
        payload={
            "parent_job_id": parent_job_id,
            "idempotency_key": "continue-00000002",
            "candidate_snapshot_hash": snapshot.snapshot_hash,
            "resume_candidate_index": 1,
        },
    )


def test_continue_endpoint_replays_child_idempotently(api) -> None:
    seeded = seed_api_partial_continue_job(api)
    path = f"/api/dashboard/ai/jobs/{seeded.parent_job_id}/continue"

    first = api.client.post(
        path,
        json=seeded.payload,
        headers={"X-CSRF-Token": api.csrf},
    )
    first_body = first.get_data(as_text=True)
    calls_after_first = list(api.fake_provider.generate_calls)
    second = api.client.post(
        path,
        json=seeded.payload,
        headers={"X-CSRF-Token": api.csrf},
    )

    assert first.status_code == 200
    assert first.mimetype == "text/event-stream"
    assert "event: done" in first_body
    assert calls_after_first == ["continue-model-2"]
    assert api.fake_provider.generate_calls == calls_after_first
    assert "event: done" in second.get_data(as_text=True)
    children = api.db.conn.execute(
        "SELECT job_id FROM ai_jobs WHERE parent_job_id = ?",
        (seeded.parent_job_id,),
    ).fetchall()
    assert len(children) == 1


def test_continue_rejects_snapshot_hash_before_opening_sse(api) -> None:
    seeded = seed_api_partial_continue_job(api)
    payload = {**seeded.payload, "candidate_snapshot_hash": "0" * 64}

    response = api.client.post(
        f"/api/dashboard/ai/jobs/{seeded.parent_job_id}/continue",
        json=payload,
        headers={"X-CSRF-Token": api.csrf},
    )

    assert response.status_code == 409
    assert "候选快照" in response.get_json()["error"]


def test_continue_rejects_remaining_provider_config_change_with_409(api) -> None:
    seeded = seed_api_partial_continue_job(api)
    api.db.update_ai_provider(
        seeded.remaining_provider_id,
        {"timeout_seconds": 31},
    )

    response = api.client.post(
        f"/api/dashboard/ai/jobs/{seeded.parent_job_id}/continue",
        json=seeded.payload,
        headers={"X-CSRF-Token": api.csrf},
    )

    assert response.status_code == 409
    assert "Provider 配置" in response.get_json()["error"]


def test_continue_validates_exact_body_and_next_candidate_index(api) -> None:
    seeded = seed_api_partial_continue_job(api)
    path = f"/api/dashboard/ai/jobs/{seeded.parent_job_id}/continue"
    invalid_payloads = [
        ({**seeded.payload, "parent_job_id": "different-parent"}, 400),
        ({**seeded.payload, "idempotency_key": "short"}, 400),
        ({**seeded.payload, "candidate_snapshot_hash": "A" * 64}, 400),
        ({**seeded.payload, "resume_candidate_index": 0}, 409),
        ({**seeded.payload, "extra": True}, 400),
    ]

    for payload, expected_status in invalid_payloads:
        response = api.client.post(
            path,
            json=payload,
            headers={"X-CSRF-Token": api.csrf},
        )
        assert response.status_code == expected_status, response.get_data(as_text=True)

    api.db.conn.execute(
        "UPDATE ai_jobs SET status = 'running' WHERE job_id = ?",
        (seeded.parent_job_id,),
    )
    api.db.conn.commit()
    non_terminal = api.client.post(
        path,
        json=seeded.payload,
        headers={"X-CSRF-Token": api.csrf},
    )
    assert non_terminal.status_code == 409
    assert "终态" in non_terminal.get_json()["error"]

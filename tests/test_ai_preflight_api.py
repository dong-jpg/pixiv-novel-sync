from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.ai_web import register_ai_routes
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.webapp import create_app


@pytest.fixture
def preflight_api(tmp_path: Path):
    path = tmp_path / "preflight.db"
    app = Flask(__name__)
    app.secret_key = "preflight-fixture"
    app.config["TESTING"] = True
    register_ai_routes(app, SimpleNamespace(storage=SimpleNamespace(db_path=path)))
    db = Database(path)
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "private-test-provider",
        "provider_type": "openai_compatible",
        "base_url": "https://private-provider.example.test/v1",
        "api_key_encrypted": "not-a-decryptable-key",
        "context_window": 1_000_000,
        "enabled": True,
    })
    long_id = db.create_ai_provider_model({
        "provider_id": provider_id,
        "model_key": "long-model",
        "manual_context_window": 1_000_000,
        "enabled": True,
    })
    short_id = db.create_ai_provider_model({
        "provider_id": provider_id,
        "model_key": "short-model",
        "manual_context_window": 128_000,
        "enabled": True,
    })
    pool_id = db.create_ai_model_pool({"name": "reading-route", "pool_kind": "custom"})
    pool_version = db.replace_ai_model_pool_members(
        pool_id,
        [{"provider_model_id": long_id, "enabled": True},
         {"provider_model_id": short_id, "enabled": True}],
        expected_version=1,
    )
    db.update_ai_model_pool(
        pool_id,
        {"enabled": True},
        expected_version=pool_version,
    )
    agent_id = db.create_ai_agent({
        "name": "preflight-agent",
        "task_type": "keyword_clean",
        "binding_type": "pool",
        "model_pool_id": pool_id,
        "provider_id": None,
        "model": None,
        "system_prompt": "SAVED_SYSTEM_NOT_AUTOMATICALLY_PREPENDED",
        "context_window": 1_000_000,
        "max_tokens": 1000,
        "temperature": 0.2,
        "top_p": 0.9,
    })
    try:
        yield app, app.test_client(), db, agent_id, provider_id
    finally:
        app.extensions["pixiv_novel_sync.ai_service"].close()
        db.close()


@pytest.fixture
def preflight_sql_guard(preflight_api, monkeypatch):
    guard = SimpleNamespace(databases={}, reads={}, denied_operations=[])
    original_conn = Database.conn.fget
    read_actions = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_SAVEPOINT,
        sqlite3.SQLITE_RECURSIVE,
    }

    def guarded_conn(database):
        conn = original_conn(database)
        if conn not in guard.databases:
            guard.databases[conn] = database
            guard.reads[conn] = set()

            def authorize(action, first, second, _database, _trigger):
                if action == sqlite3.SQLITE_READ:
                    guard.reads[conn].add(first)
                if action not in read_actions:
                    guard.denied_operations.append((action, first, second))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            conn.set_authorizer(authorize)
        return conn

    # Setup/connection PRAGMAs finish first; the descriptor also covers the
    # Router's already-bound factory and every fresh service Database instance.
    monkeypatch.setattr(Database, "conn", property(guarded_conn))
    return guard


@pytest.mark.parametrize("factory_name", ["service", "router"])
def test_preflight_sql_guard_rejects_writes_on_actual_connections(
    preflight_api, preflight_sql_guard, factory_name,
):
    app, _, db, agent_id, _ = preflight_api
    service = app.extensions["pixiv_novel_sync.ai_service"]._current()
    factory = service._db if factory_name == "service" else service.model_router._db_factory
    actual_db = factory()
    try:
        assert actual_db is not db
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            actual_db.conn.execute("UPDATE ai_agents SET name = name WHERE id = ?", (agent_id,))
        assert preflight_sql_guard.databases[actual_db.conn] is actual_db
        assert preflight_sql_guard.denied_operations == [
            (sqlite3.SQLITE_UPDATE, "ai_agents", "name"),
        ]
    finally:
        actual_db.close()


def test_preflight_filters_short_fallback_without_external_work(
    preflight_api, preflight_sql_guard, monkeypatch,
):
    app, client, db, agent_id, _ = preflight_api
    service = app.extensions["pixiv_novel_sync.ai_service"]._current()

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight attempted external/secret work")

    monkeypatch.setattr(AIWritingService, "_get_provider", forbidden)
    monkeypatch.setattr(service.model_router, "_get_provider", forbidden)
    monkeypatch.setattr(type(service.secret_manager), "decrypt", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    body = "x" * 800_000 + "PRIVATE_INPUT_TAIL_SENTINEL"
    response = client.post(
        f"/api/dashboard/ai/agents/{agent_id}/preflight",
        json={"messages": [{"role": "user", "content": body}], "max_tokens": 1000},
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["can_run"] is True
    assert [row["model_key"] for row in data["eligible"]] == ["long-model"]
    assert data["excluded"][0]["reason"] == "context_overflow"
    public = json.dumps(data)
    for secret in ("PRIVATE_INPUT_TAIL_SENTINEL", "not-a-decryptable-key",
                   "private-provider.example.test", "request_fingerprint"):
        assert secret not in public
    assert preflight_sql_guard.databases
    assert all(actual_db is not db for actual_db in preflight_sql_guard.databases.values())
    observed_tables = set().union(*preflight_sql_guard.reads.values())
    assert {"ai_agents", "ai_provider_models"} <= observed_tables
    assert preflight_sql_guard.denied_operations == []
    assert db.conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 0


def test_valid_preflight_with_empty_scope_is_not_a_generation_error(preflight_api):
    _, client, _, agent_id, _ = preflight_api
    response = client.post(
        f"/api/dashboard/ai/agents/{agent_id}/preflight",
        json={"messages": [{"role": "user", "content": "test"}],
              "allowed_provider_ids": []},
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert response.get_json()["data"]["can_run"] is False
    assert response.get_json()["data"]["eligible"] == []


@pytest.mark.parametrize("payload", [
    [], {}, {"messages": []},
    {"messages": [{"role": "tool", "content": "test"}]},
    {"messages": [{"role": "user", "content": 2}]},
    {"messages": [{"role": "user", "content": "test"}], "tools": []},
    {"messages": [{"role": "user", "content": "test"}], "allowed_provider_ids": None},
    {"messages": [{"role": "user", "content": "test"}]} | {"max_tokens": True},
])
def test_preflight_rejects_invalid_contract(preflight_api, payload):
    _, client, _, agent_id, _ = preflight_api
    response = client.post(f"/api/dashboard/ai/agents/{agent_id}/preflight", json=payload)
    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_preflight_http_limits_and_missing_agent(preflight_api):
    _, client, _, agent_id, _ = preflight_api
    path = f"/api/dashboard/ai/agents/{agent_id}/preflight"
    too_many = client.post(path, json={"messages": [{"role": "user", "content": "x"}] * 257})
    assert too_many.status_code == 400
    too_large = client.post(path, data=b"{}", content_type="application/json",
                            environ_overrides={"CONTENT_LENGTH": str(16 * 1024 * 1024 + 1)})
    assert too_large.status_code == 413
    missing = client.post("/api/dashboard/ai/agents/99999999/preflight",
                          json={"messages": [{"role": "user", "content": "test"}]})
    assert missing.status_code == 404


def test_bounded_reader_checks_unknown_length_stream_before_json_parse():
    from pixiv_novel_sync.web.request_limits import JSONBodyError, read_bounded_json_object

    class TrackedStream(io.BytesIO):
        sizes = []

        def read(self, size=-1):
            self.sizes.append(size)
            return super().read(size)

    stream = TrackedStream(b"x" * 100)
    request = SimpleNamespace(is_json=True, content_length=None, stream=stream)
    with pytest.raises(JSONBodyError) as raised:
        read_bounded_json_object(request, limit_bytes=8)
    assert raised.value.status_code == 413
    assert stream.sizes == [9]


def test_real_dashboard_auth_and_csrf_apply_to_preflight(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "preflight-test-token")
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "preflight-test-secret")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\nDASHBOARD_TOKEN=preflight-test-token\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    app.config["RAW_HTTP"] = True
    client = app.test_client()
    path = "/api/dashboard/ai/agents/99999999/preflight"
    payload = {"messages": [{"role": "user", "content": "test"}]}
    try:
        assert client.post(path, json=payload).status_code == 401
        assert client.post("/api/auth/login", data={"token": "preflight-test-token"}).status_code == 302
        assert client.post(path, json=payload).status_code == 403
        token = client.get("/api/csrf-token").get_json()["csrf_token"]
        allowed = client.post(path, json=payload, headers={"X-CSRF-Token": token})
        assert allowed.status_code == 404
    finally:
        app.extensions["pixiv_novel_sync.ai_service"].close()


def test_service_preflight_uses_exact_supplied_messages(preflight_api):
    from pixiv_novel_sync.ai.invocation import normalize_text_invocation

    app, _, db, agent_id, _ = preflight_api
    service = app.extensions["pixiv_novel_sync.ai_service"]._current()
    payload = {"messages": [
        {"role": "system", "content": "EXPLICIT_SYSTEM_ONCE"},
        {"role": "user", "content": "EXPLICIT_USER_ONCE"},
    ]}
    original = json.loads(json.dumps(payload))
    agent = service._load_agent_config(db, agent_id)
    request = normalize_text_invocation(payload, agent=agent)
    expected = service.model_router.plan_invocation(agent, request).to_public_dict()
    result = service.preflight_agent(agent_id, payload)
    assert result == expected
    assert "SAVED_SYSTEM_NOT_AUTOMATICALLY_PREPENDED" not in json.dumps(result)
    assert payload == original


def _enabled_rows(db, provider_id):
    return dict(db.conn.execute(
        "SELECT id, enabled FROM ai_provider_models WHERE provider_id = ? ORDER BY id",
        (provider_id,),
    ).fetchall())


def test_enabled_route_applies_one_atomic_selection_and_rejects_stale_revision(preflight_api):
    _, client, db, _, provider_id = preflight_api
    before = _enabled_rows(db, provider_id)
    first, second = list(before)
    revision = db.get_ai_provider_catalog_revision(provider_id)
    path = f"/api/dashboard/ai/providers/{provider_id}/models/enabled"
    response = client.put(path, json={
        "catalog_revision": revision,
        "changes": [{"id": first, "enabled": False}, {"id": second, "enabled": True}],
    })
    assert response.status_code == 200
    assert _enabled_rows(db, provider_id) == {first: 0, second: 1}
    stale = client.put(path, json={
        "catalog_revision": revision,
        "changes": [{"id": second, "enabled": False}],
    })
    assert stale.status_code == 409
    assert _enabled_rows(db, provider_id) == {first: 0, second: 1}


def test_enabled_route_rejects_foreign_model_without_partial_write(preflight_api):
    _, client, db, _, provider_id = preflight_api
    before = _enabled_rows(db, provider_id)
    other = db.create_ai_provider({"name": "other", "provider_type": "openai_compatible"})
    foreign = db.create_ai_provider_model({"provider_id": other, "model_key": "foreign", "enabled": True})
    response = client.put(f"/api/dashboard/ai/providers/{provider_id}/models/enabled", json={
        "catalog_revision": db.get_ai_provider_catalog_revision(provider_id),
        "changes": [{"id": next(iter(before)), "enabled": False}, {"id": foreign, "enabled": False}],
    })
    assert response.status_code == 400
    assert _enabled_rows(db, provider_id) == before
    assert _enabled_rows(db, other)[foreign] == 1


def test_job_api_exposes_unknown_usage_and_unsupported_continue(preflight_api):
    app, client, db, agent_id, provider_id = preflight_api
    service = app.extensions["pixiv_novel_sync.ai_service"]._current()
    agent = service._load_agent_config(db, agent_id)
    agent_config_hash = service.model_router._agent_config_hash(agent)
    candidates = service.model_router.resolve_candidates(agent).candidates
    selected = candidates[0]
    job_id, owner = "preflight-diagnostics-job", "preflight-diagnostics-owner"
    db.create_ai_job(job_id, "keyword_clean", agent_id, {}, owner_token=owner)
    attempt = db.allocate_ai_model_attempt(job_id, owner, {
        "provider_id": provider_id, "model_key": "long-model", "stage": "main",
        "agent_config_hash": agent_config_hash,
        "provider_config_hash": selected.provider_config_hash,
        "candidate_list_hash": candidates[0].provider_config_hash,
    })
    assert db.claim_ai_job_network_request(job_id, owner, attempt_index=attempt) == 1
    response = client.get(f"/api/dashboard/ai/jobs/{job_id}")
    assert response.status_code == 200
    job = response.get_json()["data"]
    assert job["usage_summary"]["unreported_requests"] == 1
    assert job["usage_summary"]["complete"] is False
    assert job["usage_summary"]["known_total_tokens"] is None
    assert job["can_continue"] is False
    assert job["continue_unavailable_reason"]
    raw = db.conn.execute("SELECT validation_status FROM ai_jobs WHERE job_id = ?", (job_id,)).fetchone()[0]
    assert raw is None
    assert job["validation_status"] == "unknown"


def test_main_continue_rejects_valid_history_without_starting_a_job(preflight_api, monkeypatch):
    from dataclasses import asdict

    app, client, db, agent_id, _ = preflight_api
    service = app.extensions["pixiv_novel_sync.ai_service"]._current()
    agent = service._load_agent_config(db, agent_id)
    snapshot = service.model_router.resolve_candidates(agent)
    snapshot_payload = {
        "agent_config_hash": snapshot.agent_config_hash,
        "binding_version": snapshot.binding_version,
        "candidates": [asdict(candidate) for candidate in snapshot.candidates],
    }
    job_id, owner = "legacy-unhandled-job", "legacy-unhandled-owner"
    db.create_ai_job(job_id, "keyword_clean", agent_id, {}, owner_token=owner)
    assert db.set_ai_job_candidate_snapshot(job_id, owner, snapshot_payload, snapshot.snapshot_hash)
    assert db.finish_ai_job_cas(job_id, owner, "failed")
    before = db.conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0]

    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported continue dispatched a model")

    monkeypatch.setattr(AIWritingService, "_get_provider", forbidden)
    response = client.post(f"/api/dashboard/ai/jobs/{job_id}/continue", json={
        "parent_job_id": job_id,
        "idempotency_key": "legacy-unsupported-continue-0001",
        "candidate_snapshot_hash": snapshot.snapshot_hash,
        "resume_candidate_index": 0,
    })
    assert response.status_code == 409
    assert "暂不支持手动候选继续" in response.get_json()["error"]
    assert db.conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == before


def test_agent_candidates_legacy_missing_agent_remains_400(preflight_api):
    _, client, _, _, _ = preflight_api

    response = client.get("/api/dashboard/ai/agents/999999/candidates")

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_job_list_projects_diagnostics_without_extra_queries(preflight_api, monkeypatch):
    from pixiv_novel_sync.ai.job_diagnostics import project_main_job_diagnostics

    app, client, db, agent_id, _ = preflight_api
    service = app.extensions["pixiv_novel_sync.ai_service"]._current()
    agent = service._load_agent_config(db, agent_id)
    candidates = service.model_router.resolve_candidates(agent).candidates
    job_id, owner = "preflight-list-diagnostics", "preflight-list-owner"
    db.create_ai_job(job_id, "keyword_clean", agent_id, {}, owner_token=owner)
    db.allocate_ai_model_attempt(job_id, owner, {
        "provider_id": candidates[0].provider_id,
        "model_key": candidates[0].model_key,
        "stage": "main",
        "agent_config_hash": service.model_router._agent_config_hash(agent),
        "provider_config_hash": candidates[0].provider_config_hash,
        "candidate_list_hash": candidates[0].provider_config_hash,
    })
    detail_queries = []

    def forbidden_detail_query(database, *args, **kwargs):
        detail_queries.append(database)
        raise AssertionError("job list projection must not issue detail queries")

    monkeypatch.setattr(Database, "get_ai_job", forbidden_detail_query)
    with pytest.raises(AssertionError, match="must not issue detail queries"):
        service.get_job(job_id)
    assert len(detail_queries) == 1
    assert detail_queries[0] is not db
    detail_queries.clear()

    response = client.get("/api/dashboard/ai/jobs")

    assert response.status_code == 200
    assert detail_queries == []
    item = response.get_json()["data"]["items"][0]
    assert item["can_continue"] is False
    assert item["snapshot_version"] is None
    assert "usage_summary" not in item
    assert project_main_job_diagnostics({})["continue_unavailable_reason"]


def test_job_diagnostics_projects_frozen_v2_plan_without_current_provider_access(preflight_api, monkeypatch):
    from pixiv_novel_sync.ai.invocation import normalize_text_invocation
    from pixiv_novel_sync.ai.invocation_snapshot import freeze_plan, planned_snapshot_payload
    from pixiv_novel_sync.ai.job_diagnostics import project_main_job_diagnostics

    app, _, db, agent_id, _ = preflight_api
    service = app.extensions["pixiv_novel_sync.ai_service"]._current()
    agent = service._load_agent_config(db, agent_id)
    request = normalize_text_invocation(
        {"messages": [{"role": "user", "content": "PRIVATE_PLAN_INPUT"}]}, agent=agent,
    )
    plan = service.model_router.plan_invocation(agent, request)
    snapshot = freeze_plan(service.model_router._agent_config_hash(agent), agent.binding_version, plan)

    def forbidden(*args, **kwargs):
        raise AssertionError("saved diagnostics must not reload configuration")

    monkeypatch.setattr(AIWritingService, "_get_provider", forbidden)
    job = {"status": "failed", "validation_status": "failed",
           "candidate_snapshot": planned_snapshot_payload(snapshot),
           "candidate_snapshot_hash": snapshot.snapshot_hash}
    result = project_main_job_diagnostics(job)
    assert result["snapshot_version"] == 2
    assert result["invocation_plan"] == plan.to_public_dict()
    assert result["status"] == "failed"
    assert "PRIVATE_PLAN_INPUT" not in json.dumps(result)
    assert "request_fingerprint" not in json.dumps(result["invocation_plan"])
    corrupt = dict(job, candidate_snapshot_hash="0" * 64)
    invalid = project_main_job_diagnostics(corrupt)
    assert invalid["invocation_plan"] is None
    assert invalid["plan_unavailable_reason"]

from copy import deepcopy
from types import SimpleNamespace

import pytest

import pixiv_novel_sync.webapp as webapp_module
from pixiv_novel_sync.webapp import create_app
from work_analysis_api_helpers import (
    BASE, confirmed_payload, facade_fixture, request_payload, seed_database,
)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "fixture-login-token")
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "fixture-flask-secret-not-a-credential")
    env = tmp_path / ".env"
    env.write_text("PIXIV_REFRESH_TOKEN=fixture\nDASHBOARD_TOKEN=fixture-login-token\n",
                   encoding="utf-8")
    fixture = facade_fixture(tmp_path / "fixture.db")
    app = create_app(env_path=str(env), start_scheduler=False)
    app.config.update(TESTING=True, RAW_HTTP=True)
    app.extensions["pixiv_novel_sync.work_analysis_service"] = fixture.service
    client = app.test_client()
    fixture.app, fixture.client = app, client
    try:
        yield fixture
    finally:
        app.extensions["pixiv_novel_sync.ai_service"].close()
        fixture.ai.close()
        fixture.db.close()


def login(api):
    assert api.client.post("/api/auth/login", data={"token": "fixture-login-token"}).status_code == 302
    token = api.client.get("/api/csrf-token").get_json()["csrf_token"]
    return {"X-CSRF-Token": token}


@pytest.fixture
def registered_api(tmp_path, monkeypatch):
    db_a, agent_id, _ = seed_database(tmp_path / "owner-a.db")
    db_b, _, _ = seed_database(tmp_path / "ready-b.db")
    config = tmp_path / "config.yaml"
    config.write_text("sync:\n  auto_sync_enabled: false\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("PIXIV_REFRESH_TOKEN=synthetic-never-used\n", encoding="utf-8")
    monkeypatch.setenv("DASHBOARD_TOKEN", "fixture-login-token")
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "fixture-flask-secret-not-a-credential")
    monkeypatch.setenv("PIXIV_DB_PATH", str(db_a.path))
    settings = webapp_module.SettingsManager(str(config))
    monkeypatch.setattr(webapp_module, "SettingsManager", lambda _path: settings)
    app = create_app(str(config), str(env), start_scheduler=False)
    app.config.update(TESTING=True, RAW_HTTP=True)
    coordinator = app.extensions["work_analysis_coordinator"]
    manager = app.config["job_manager"]
    dispatches = []

    def forbidden_dispatch(job_id):
        dispatches.append(job_id)
        raise AssertionError("registered API test must not dispatch a worker")

    def switch_database(path):
        monkeypatch.setenv("PIXIV_DB_PATH", str(path))
        settings.invalidate()

    monkeypatch.setattr(coordinator, "run_shared", forbidden_dispatch)
    # Keep real admission/registration, but hold the sole slot to prevent work.
    assert manager.acquire_run_slot()
    try:
        yield SimpleNamespace(
            app=app, client=app.test_client(), db_a=db_a, db_b=db_b,
            coordinator=coordinator, manager=manager, dispatches=dispatches,
            payload=request_payload(agent_id), switch_database=switch_database,
        )
    finally:
        switch_database(db_a.path)
        active, waiting = manager.shared_admission_snapshot()
        for job in active + waiting:
            coordinator.cancel_shared_job(job.job_id)
        manager.release_run_slot()
        app.extensions["close_work_analysis_owner"]()
        app.extensions["pixiv_novel_sync.ai_service"].close()
        db_a.close()
        db_b.close()


def test_registered_work_api_rejects_database_switch_before_creating_a_stranded_parent(registered_api):
    api = registered_api
    unauthorized = api.client.get(BASE)
    assert unauthorized.status_code == 401
    assert "no-store" in unauthorized.headers["Cache-Control"]
    headers = login(api)
    assert api.client.post(BASE + "/preview", json=api.payload).status_code == 403
    preview = api.client.post(BASE + "/preview", json=api.payload, headers=headers)
    assert preview.status_code == 200
    request = {**deepcopy(api.payload), "idempotency_key": "1" * 32,
               "preview_fingerprint": preview.get_json()["data"]["preview_fingerprint"]}
    request["confirmations"] = {name: True for name in request["confirmations"]}
    created = api.client.post(BASE, json=request, headers=headers)
    assert created.status_code == 200
    run = created.get_json()["data"]
    for path in (BASE, BASE + "/"):
        listed = api.client.get(path)
        assert listed.status_code == 200
        assert listed.get_json()["data"]["total"] == 1
        assert "Location" not in listed.headers
        assert "no-store" in listed.headers["Cache-Control"]
    rules = [rule.rule for rule in api.app.url_map.iter_rules()
             if rule.endpoint.startswith("work_analysis_")]
    assert rules and all(rule.startswith(BASE + "/") or rule == BASE for rule in rules)

    owner_key = webapp_module._scheduler_registry_key(api.db_a.path)
    owner = webapp_module._auto_sync_scheduler_registry[owner_key]
    a_before = list(api.db_a.conn.iterdump())
    jobs_before = list(api.manager._jobs)
    assert api.db_b.conn.execute("SELECT count(*) FROM work_analysis_runs").fetchone()[0] == 0
    api.switch_database(api.db_b.path)
    assert api.coordinator.settings_provider().storage.db_path == api.db_b.path
    # Dashboard sessions are database-backed; authenticate against B as well.
    headers = login(api)
    request["idempotency_key"] = "2" * 32
    url = BASE + "/" + run["run_id"]
    responses = [
        api.client.post(BASE + "/preview", json=api.payload, headers=headers),
        api.client.post(BASE, json=request, headers=headers),
        api.client.get(BASE),
        api.client.get(url),
        api.client.get(url + "/units"),
        api.client.post(url + "/pause", json={"expected_version": run["version"]}, headers=headers),
        api.client.delete(url, json={"expected_version": run["version"]}, headers=headers),
    ]
    assert api.db_b.conn.execute("SELECT count(*) FROM work_analysis_runs").fetchone()[0] == 0
    for response in responses:
        assert response.status_code == 503
        assert response.get_json() == {"ok": False, "error": "storage_unavailable",
                                       "code": "storage_unavailable"}
        assert "no-store" in response.headers["Cache-Control"]
    assert list(api.db_a.conn.iterdump()) == a_before
    assert list(api.manager._jobs) == jobs_before
    assert api.dispatches == []
    assert api.app.extensions["work_analysis_coordinator"] is api.coordinator
    assert webapp_module._auto_sync_scheduler_registry[owner_key] is owner
    assert owner.scheduler.work_coordinator is api.coordinator
    assert webapp_module._scheduler_registry_key(api.db_b.path) not in webapp_module._auto_sync_scheduler_registry


@pytest.mark.parametrize("path", [BASE, BASE + "/", BASE + "/" + "1" * 32,
                                  BASE + "/" + "1" * 32 + "/report",
                                  BASE + "/" + "1" * 32 + "/evidence/" + "2" * 64])
def test_work_api_uses_existing_authentication_and_no_store_on_failure(api, path):
    response = api.client.get(path)
    assert response.status_code == 401
    assert "no-store" in response.headers["Cache-Control"]
    assert "Location" not in response.headers


def test_work_preview_requires_csrf_even_without_external_send(api):
    login(api)
    response = api.client.post(BASE + "/preview", json=api.payload)
    assert response.status_code == 403
    assert "no-store" in response.headers["Cache-Control"]
    assert api.coordinator.wakes == []


@pytest.mark.parametrize("suffix", ["", "/"])
def test_base_routes_never_redirect_and_write_returns_the_committed_parent(api, suffix):
    headers = login(api)
    preview = api.client.post(BASE + "/preview", json=api.payload, headers=headers)
    assert preview.status_code == 200
    assert "no-store" in preview.headers["Cache-Control"]
    payload = confirmed_payload(api.service, api.payload)
    created = api.client.post(BASE + suffix, json=payload, headers=headers)
    assert created.status_code == 200
    assert "Location" not in created.headers
    assert created.get_json()["data"]["status"] == "queued"
    listed = api.client.get(BASE + suffix)
    assert listed.status_code == 200
    assert listed.get_json()["data"]["total"] == 1
    assert "Location" not in listed.headers


@pytest.mark.parametrize("raw,status", [
    ("[]", 400), ("null", 400), ('{"agent_id":1,"agent_id":2}', 400),
    ('{"budget":{"token_limit":NaN}}', 400), ("{" + " " * (256 * 1024) + "}", 413),
], ids=["array", "null", "duplicate-key", "nonfinite", "too-large"])
def test_work_body_parser_is_bounded_and_rejects_duplicate_or_nonfinite_json(api, raw, status):
    headers = login(api)
    response = api.client.post(BASE + "/preview", data=raw,
                               content_type="application/json", headers=headers)
    assert response.status_code == status
    assert "no-store" in response.headers["Cache-Control"]
    assert response.get_json()["ok"] is False


@pytest.mark.parametrize("query", ["offset=-1", "limit=0", "limit=101", "offset=1.0",
                                   "limit=true", "offset=0&offset=1", "include_private=true"])
def test_work_list_rejects_invalid_or_ambiguous_query_fields(api, query):
    login(api)
    response = api.client.get(BASE + "?" + query)
    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_controls_require_explicit_version_and_budget_consent(api):
    headers = login(api)
    run = api.service.create(confirmed_payload(api.service, api.payload))
    url = BASE + "/" + run["run_id"]
    for body in ({}, {"expected_version": True}, {"expected_version": run["version"], "force": True}):
        assert api.client.post(url + "/pause", json=body, headers=headers).status_code == 400
    assert api.client.post(url + "/budget", json={
        "expected_version": run["version"], "budget": run["budget"],
        "budget_confirmed": False,
    }, headers=headers).status_code == 400
    paused = api.client.post(url + "/pause", json={"expected_version": run["version"]}, headers=headers)
    assert paused.status_code == 200
    assert paused.get_json()["data"]["status"] == "paused"
    conflict = api.client.post(url + "/resume", json={"expected_version": run["version"]}, headers=headers)
    assert conflict.status_code == 409


def test_incomplete_report_and_unrelated_evidence_are_not_exposed(api):
    login(api)
    run = api.service.create(confirmed_payload(api.service, api.payload))
    url = BASE + "/" + run["run_id"]
    assert api.client.get(url + "/report").status_code == 409
    assert api.client.get(url + "/evidence/" + "f" * 64).status_code in {404, 409}
    detail = api.client.get(url).get_json()["data"]
    assert "report" not in detail and "configuration" not in detail and "owner_token" not in detail
    units = api.client.get(url + "/units").get_json()["data"]
    assert all("artifact" not in item for item in units["items"])


def test_work_errors_never_echo_exception_messages_or_offer_sse(api, monkeypatch):
    login(api)

    def explode(offset, limit):
        raise RuntimeError("PRIVATE_BODY PRIVATE_KEY PRIVATE_REPORT")

    monkeypatch.setattr(api.service, "list_runs", explode)
    response = api.client.get(BASE)
    assert response.status_code == 500
    assert response.get_json() == {"ok": False, "error": "work_analysis_unavailable",
                                   "code": "work_analysis_unavailable"}
    assert "PRIVATE" not in response.get_data(as_text=True)
    assert api.client.get(BASE + "/events").status_code == 400
    assert api.client.get(BASE + "/" + "1" * 32 + "/events").status_code == 404


def test_delete_is_idempotent_and_never_resumes_a_tombstone(api):
    headers = login(api)
    request = confirmed_payload(api.service, api.payload)
    run = api.service.create(request)
    url = BASE + "/" + run["run_id"]
    body = {"expected_version": run["version"]}
    first = api.client.delete(url, json=body, headers=headers)
    assert first.status_code == 200
    again = api.client.delete(url, json=body, headers=headers)
    assert again.status_code == 200
    assert again.get_json()["data"]["deleted"] is True
    assert api.client.post(BASE, json=deepcopy(request), headers=headers).status_code == 410
    assert api.client.get(url + "/report").status_code == 410
    assert api.client.get(url + "/evidence/" + "f" * 64).status_code == 410


@pytest.mark.parametrize("method,path,status", [
    ("GET", BASE + "/" + "a" * 32 + "/unknown", 404),
    ("PUT", BASE, 405),
    ("GET", BASE + "/" + "a" * 32 + "/pause", 405),
])
def test_framework_failures_under_prefix_are_not_cacheable(api, method, path, status):
    headers = login(api)
    response = api.client.open(path, method=method, headers=headers)
    assert response.status_code == status
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["Expires"] == "0"
    assert api.coordinator.wakes == []


@pytest.mark.parametrize("bad_id", ["A" * 32, "1" * 31, "g" * 32])
def test_work_routes_reject_noncanonical_run_ids(api, bad_id):
    login(api)
    response = api.client.get(BASE + "/" + bad_id)
    assert response.status_code == 400
    assert response.get_json() == {"ok": False, "error": "invalid_id", "code": "invalid_id"}
    assert api.coordinator.wakes == []


def test_all_work_gets_are_read_only_and_private_routes_have_no_rescue_alias(api):
    login(api)
    run = api.service.create(confirmed_payload(api.service, api.payload))
    api.coordinator.wakes.clear()
    url = BASE + "/" + run["run_id"]
    for path in (BASE, BASE + "/", url, url + "/units", url + "/report",
                 url + "/evidence/" + "f" * 64):
        response = api.client.get(path)
        assert response.status_code in {200, 404, 409}
        assert "no-store" in response.headers["Cache-Control"]
    assert api.coordinator.wakes == []
    assert api.coordinator.controls == []
    assert api.db.conn.execute("SELECT count(*) FROM ai_jobs").fetchone()[0] == 0
    work_rules = [rule.rule for rule in api.app.url_map.iter_rules()
                  if rule.endpoint.startswith("work_analysis_")]
    assert work_rules and all(rule.startswith(BASE) for rule in work_rules)

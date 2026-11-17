from types import SimpleNamespace

import pytest

from pixiv_novel_sync.webapp import create_app
from pixiv_novel_sync.work_analysis.service import WorkAnalysisService
from test_preference_learning_profiles import env as published_report_fixture


env = published_report_fixture


@pytest.fixture(params=[False, True], ids=["loopback", "authenticated"])
def claim_api(env, tmp_path, monkeypatch, request):
    token = "synthetic-login-token" if request.param else ""
    monkeypatch.setenv("DASHBOARD_TOKEN", token)
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "synthetic-flask-secret-not-a-credential")
    monkeypatch.setenv("PIXIV_DB_PATH", str(env.db.path))
    env_file = tmp_path / ".env"
    env_file.write_text("PIXIV_REFRESH_TOKEN=synthetic\nDASHBOARD_TOKEN=" + token + "\n", encoding="utf-8")
    app = create_app(env_path=str(env_file), start_scheduler=False)
    app.config.update(TESTING=True, RAW_HTTP=True)
    app.extensions["pixiv_novel_sync.work_analysis_service"] = WorkAnalysisService(env.db_factory, None, None)
    client = app.test_client()
    report = env.db.get_work_analysis_report(env.run_id)
    claim_id = next(iter(report["claim_feedback"].values()))["report_claim_id"]
    url = "/api/dashboard/work-analyses/" + env.run_id + "/claims/" + claim_id
    try:
        yield SimpleNamespace(env=env, client=client, token=token, url=url, report=report, app=app)
    finally:
        app.extensions["close_work_analysis_owner"]()
        app.extensions["pixiv_novel_sync.ai_service"].close()


def login(api):
    if api.token:
        assert api.client.post("/api/auth/login", data={"token": api.token}).status_code == 302
    token = api.client.get("/api/csrf-token").get_json()["csrf_token"]
    return {"X-CSRF-Token": token}


def test_claim_feedback_uses_auth_csrf_strict_schema_and_committed_report(claim_api):
    api = claim_api
    body = {"expected_version": api.report["version"], "unreliable": True}
    response = api.client.post(api.url, json=body)
    assert response.status_code == (401 if api.token else 403)
    assert "no-store" in response.headers["Cache-Control"]
    headers = login(api)
    assert api.client.post(api.url, json=body).status_code == 403
    response = api.client.post(api.url, json=body, headers=headers)
    assert response.status_code == 200 and "no-store" in response.headers["Cache-Control"]
    marked = response.get_json()["data"]
    assert marked["artifact"] == api.report["artifact"]
    assert marked["claim_feedback_revision"] == 1
    assert all(value["unreliable"] for value in marked["claim_feedback"].values())
    conflict = api.client.post(api.url, json=body, headers=headers)
    assert conflict.status_code == 409 and conflict.get_json()["code"] == "version_conflict"
    repeated = api.client.post(api.url, json={**body, "expected_version": marked["version"]}, headers=headers)
    assert repeated.get_json()["data"]["version"] == marked["version"]
    restored = api.client.post(api.url, json={"unreliable": False, "expected_version": marked["version"]}, headers=headers)
    assert restored.status_code == 200 and restored.get_json()["data"]["claim_feedback_revision"] == 2
    assert api.env.summary_calls == []


@pytest.mark.parametrize("bad", [
    {}, {"unreliable": True}, {"expected_version": True, "unreliable": True},
    {"expected_version": 1, "unreliable": "true"}, {"expected_version": 1, "unreliable": 1},
    {"expected_version": 1, "unreliable": []}, {"expected_version": 1, "unreliable": True, "force": True},
])
def test_claim_feedback_rejects_inexact_fields(claim_api, bad):
    api = claim_api
    response = api.client.post(api.url, json=bad, headers=login(api))
    assert response.status_code == 400
    assert api.env.db.get_work_analysis_report(api.env.run_id)["claim_feedback_revision"] == 0


@pytest.mark.parametrize("raw", [
    '{"expected_version":1,"unreliable":true,"unreliable":false}',
    '{"expected_version":NaN,"unreliable":true}',
    '[]',
])
def test_claim_feedback_rejects_ambiguous_json(claim_api, raw):
    api = claim_api
    response = api.client.post(api.url, data=raw, content_type="application/json", headers=login(api))
    assert response.status_code == 400
    assert api.env.db.get_work_analysis_report(api.env.run_id)["claim_feedback_revision"] == 0


def test_claim_feedback_rejects_foreign_ids_queries_and_deleted_reports(claim_api):
    api = claim_api
    headers = login(api)
    body = {"expected_version": api.report["version"], "unreliable": True}
    for suffix in ("?force=true", "?offset=0&offset=1"):
        assert api.client.post(api.url + suffix, json=body, headers=headers).status_code == 400
    assert api.client.post(api.url.rsplit("/", 1)[0] + "/" + "f" * 64,
                           json=body, headers=headers).status_code == 404
    assert api.client.post(api.url.rsplit("/", 1)[0] + "/" + "F" * 64,
                           json=body, headers=headers).status_code == 400
    api.env.db.request_work_analysis_stop(api.env.run_id, "delete", api.report["version"])
    deleted = api.client.post(api.url, json=body, headers=headers)
    assert deleted.status_code == 410 and deleted.get_json()["code"] == "work_deleted"
    assert "no-store" in deleted.headers["Cache-Control"]

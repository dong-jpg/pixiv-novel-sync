import sqlite3


def test_worker_bearer_cannot_access_dashboard(remote_web_fixture):
    f = remote_web_fixture
    response = f.client.get("/api/dashboard/remote/runs", headers={"Authorization": f"Bearer {f.bootstrap}"})
    assert response.status_code == 401


def test_mutations_require_real_login_csrf_and_closed_schema(remote_web_fixture):
    f = remote_web_fixture
    headers = f.login()
    path = "/api/dashboard/remote/runs"
    assert f.client.post(path, json={"kind": "diagnostic.v1"}, headers={"Idempotency-Key": "first"}).status_code == 403
    headers["Idempotency-Key"] = "first"
    assert f.client.post(path, json={"kind": "shell", "command": "echo bad"}, headers=headers).status_code == 400
    assert f.client.post(path, json={"kind": "diagnostic.v1", "url": "https://example.test"}, headers=headers).status_code == 400
    assert f.client.post(path, json={"kind": "diagnostic.v1"}, headers=headers).status_code == 202


def test_submission_replay_keeps_first_frozen_text(remote_web_fixture):
    f = remote_web_fixture
    headers = f.login() | {"Idempotency-Key": "same-click"}
    path = "/api/dashboard/remote/runs"
    first = f.client.post(path, json={"kind": "epub.single.v1", "novel_id": 42}, headers=headers)
    assert first.status_code == 202
    run_id = first.get_json()["run_id"]
    before = f.store.get_run(run_id)["input_sha256"]
    with sqlite3.connect(f.library_db) as db:
        db.execute("UPDATE novel_texts SET text_raw='Changed after first click' WHERE novel_id=42")
    second = f.client.post(path, json={"kind": "epub.single.v1", "novel_id": 42}, headers=headers)
    assert second.get_json()["run_id"] == run_id
    assert f.store.get_run(run_id)["input_sha256"] == before
    assert f.client.post(path, json={"kind": "diagnostic.v1"}, headers=headers).status_code == 409


def test_public_metadata_and_hold_do_not_expose_paths_or_secrets(remote_web_fixture):
    f = remote_web_fixture
    headers = f.login()
    response = f.client.post("/api/dashboard/remote/machine/hold", json={"seconds": 3600}, headers=headers)
    assert response.status_code == 200
    machine = f.client.get("/api/dashboard/remote/machine")
    assert machine.status_code == 200
    assert machine.get_json()["hold_until"] == f.clock.now + 3600
    rendered = machine.get_data(as_text=True) + f.client.get("/api/dashboard/remote/runs").get_data(as_text=True)
    for sensitive in (f.bootstrap, str(f.library_db), "token_hash", "control.db", "session_secret"):
        assert sensitive not in rendered
    assert f.client.post("/api/dashboard/remote/machine/hold", json={"seconds": True}, headers=headers).status_code == 400
    assert f.provider_calls == []


def test_submission_rejects_missing_length_and_oversized_body(remote_web_fixture):
    f = remote_web_fixture
    headers = f.login() | {"Idempotency-Key": "oversize", "Content-Type": "application/json"}
    result = f.client.post("/api/dashboard/remote/runs", data=b" " * (16 * 1024 + 1), headers=headers)
    assert result.status_code == 413

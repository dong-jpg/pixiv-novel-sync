import uuid


PREFIX = "/api/remote-worker/v1"


def enroll(f):
    client = f.app.test_client(use_cookies=False)
    secret = "S" * 32
    response = client.post(PREFIX + "/sessions", headers={"Authorization": "Bearer " + f.bootstrap},
                           json={"boot_id": str(uuid.uuid4()), "session_secret": secret, "protocol": 1, "renderer": "epub-v1"})
    assert response.status_code == 200
    assert "server_now" in response.get_json()
    return client, secret, response.get_json()


def test_worker_bootstrap_and_session_are_isolated_from_cookies(remote_web_fixture):
    f = remote_web_fixture
    f.login()
    assert f.client.post(PREFIX + "/claim", json={"claim_key": "x"}).status_code == 401
    client, secret, session = enroll(f)
    headers = {"Authorization": "Bearer " + secret}
    assert client.post(PREFIX + "/claim", json={"claim_key": "first"}, headers=headers).status_code == 204
    assert client.get("/api/dashboard/remote/runs", headers=headers).status_code == 401
    assert client.post(PREFIX + "/sessions", json={}, headers=headers).status_code == 401
    assert client.post(PREFIX + "/session-heartbeat", json={"session_id": session["session_id"]}, headers=headers).status_code == 200
    assert client.post(PREFIX + "/claim?token=" + secret, json={"claim_key": "x"}, headers=headers).status_code == 403
    assert client.post(PREFIX + "/claim", json={"claim_key": "x"}, headers=headers | {"Origin": "https://host.test"}).status_code == 403
    assert client.post(PREFIX + "/claim", json={"claim_key": "x"}, headers=headers | {"Cookie": "session=anything"}).status_code == 401
    assert client.get(PREFIX + "/not-a-real-route").status_code == 401


def test_worker_response_is_private_and_unknown_fields_rejected(remote_web_fixture):
    client, secret, session = enroll(remote_web_fixture)
    response = client.post(PREFIX + "/claim", json={"claim_key": "x", "command": "bad"}, headers={"Authorization": "Bearer " + secret})
    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_fields"}
    assert "no-store" in response.headers["Cache-Control"]
    assert "Access-Control-Allow-Origin" not in response.headers


def test_worker_cannot_forge_session_or_attempt(remote_web_fixture):
    client, secret, session = enroll(remote_web_fixture)
    response = client.post(PREFIX + "/session-heartbeat", json={"session_id": str(uuid.uuid4())}, headers={"Authorization": "Bearer " + secret})
    assert response.status_code == 403
    response = client.get(PREFIX + "/attempts/../../file/input/text", headers={"Authorization": "Bearer " + secret})
    assert response.status_code in {401, 404}

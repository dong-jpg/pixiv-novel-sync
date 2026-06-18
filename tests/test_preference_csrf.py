from __future__ import annotations

from pixiv_novel_sync.webapp import create_app


def test_preference_analyze_accepts_authenticated_csrf_request(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret-token")
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\nDASHBOARD_TOKEN=secret-token\n", encoding="utf-8")
    app = create_app(env_path=str(env_path))
    client = app.test_client()

    assert client.post("/api/auth/login", data={"token": "secret-token"}).status_code == 302
    csrf_token = client.get("/api/csrf-token").get_json()["csrf_token"]

    response = client.post(
        "/api/dashboard/preferences/profiles/analyze",
        json={"name": "本地偏好画像", "is_default": True, "scope": {"min_text_length": 1000}},
        headers={"X-CSRF-Token": csrf_token},
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["data"]["job_id"]

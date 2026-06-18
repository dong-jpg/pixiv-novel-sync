from __future__ import annotations

from pixiv_novel_sync.webapp import create_app


def test_dashboard_novels_api_supports_bookmark_category(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path))
    client = app.test_client()

    response = client.get(
        "/api/dashboard/novels?category=bookmark",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["category"] == "bookmark"
    assert "items" in payload


def test_dashboard_novels_api_supports_default_category(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path))
    client = app.test_client()

    response = client.get(
        "/api/dashboard/novels",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["category"] == "all"
    assert "items" in payload

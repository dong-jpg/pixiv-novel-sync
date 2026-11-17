"""Writing owner checks inherit the shared revocable dashboard login."""
from types import SimpleNamespace

import pytest
from flask import jsonify, request, session

from pixiv_novel_sync.ai.adult_auth import (
    require_adult_owner, sign_adult_access, verify_adult_access,
)
from pixiv_novel_sync.webapp import create_app


def test_persistent_login_admits_adult_owner_and_relogin_revokes_access(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "synthetic-writing-password")
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "synthetic-writing-cookie-secret")
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    app = create_app(config_path=str(tmp_path / "missing.yaml"), env_path=str(env), start_scheduler=False)
    app.config.update(TESTING=True, RAW_HTTP=True)
    settings = SimpleNamespace(dashboard_token="synthetic-writing-password")

    @app.get("/api/dashboard/writing-auth-probe")
    def probe():
        owner = require_adult_owner(settings)
        supplied = request.args.get("access_token")
        if supplied:
            try:
                verify_adult_access(supplied, owner, "synthetic-writing-job")
            except PermissionError:
                return jsonify({"error": "expired access"}), 403
        return jsonify({"access_token": sign_adult_access(owner, "synthetic-writing-job"),
                        "authenticated_at": owner.authenticated_at})

    client = app.test_client()
    assert client.get("/api/dashboard/writing-auth-probe").status_code == 401
    assert client.post("/api/auth/login", data={"token": settings.dashboard_token}).status_code == 302
    first = client.get("/api/dashboard/writing-auth-probe")
    assert first.status_code == 200
    first = first.get_json()
    with client.session_transaction() as cookie:
        assert "auth_session_id" in cookie
        assert cookie["auth_session_started_at_ns"] == first["authenticated_at"]
        assert "authenticated" not in cookie and "authenticated_at" not in cookie
    assert client.get("/api/dashboard/writing-auth-probe", query_string={"access_token": first["access_token"]}).status_code == 200

    assert client.post("/api/auth/login", data={"token": settings.dashboard_token}).status_code == 302
    assert client.get("/api/dashboard/writing-auth-probe", query_string={"access_token": first["access_token"]}).status_code == 403
    second = client.get("/api/dashboard/writing-auth-probe").get_json()
    assert second["authenticated_at"] > first["authenticated_at"]
    csrf = client.get("/api/csrf-token").get_json()["csrf_token"]
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/dashboard/writing-auth-probe").status_code == 401


def test_adult_owner_rejects_persistent_cookie_before_shared_gate_verifies_it():
    from flask import Flask

    app = Flask(__name__)
    app.secret_key = "synthetic-secret"
    with app.test_request_context("/"):
        session["auth_session_id"] = "a" * 43
        session["auth_session_started_at_ns"] = 1
        session["authenticated"] = True
        session["authenticated_at"] = 1
        with pytest.raises(PermissionError):
            require_adult_owner(SimpleNamespace(dashboard_token="synthetic-password"))

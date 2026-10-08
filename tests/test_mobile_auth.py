"""Real, offline Flask/SQLite regressions for revocable dashboard logins."""
from __future__ import annotations

import hashlib
import hmac
import socket
import sqlite3
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from pixiv_novel_sync import webapp as webapp_module
from pixiv_novel_sync.storage_db import Database


LOGIN = "/api/auth/login"
LOGOUT = "/api/auth/logout"
PROTECTED = "/api/dashboard/sync/status"
PASSWORD = "test-only-dashboard-password"
DAY = 24 * 60 * 60


@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    """Never load a real config, start workers, or contact an external service."""
    monkeypatch.setenv("DASHBOARD_TOKEN", PASSWORD)
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "test-only-flask-secret-not-for-deployment")
    monkeypatch.setenv("PIXIV_REFRESH_TOKEN", "test-only-refresh-token")
    monkeypatch.setenv("PIXIV_ACCESS_TOKEN", "")
    env_path = tmp_path / "auth-test.env"
    env_path.write_text("", encoding="utf-8")
    clock = SimpleNamespace(now=1_791_400_000.0)
    monkeypatch.setattr(webapp_module.time, "time", lambda: clock.now)

    def no_network(*args, **kwargs):
        raise AssertionError("Authentication tests must not use the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)

    def new_app():
        app = webapp_module.create_app(
            config_path=str(tmp_path / "missing.yaml"),
            env_path=str(env_path),
            start_scheduler=False,
        )
        app.config.update(TESTING=True, RAW_HTTP=True)
        return app

    return SimpleNamespace(
        app=new_app(),
        new_app=new_app,
        db_path=tmp_path / "state" / "test.db",
        clock=clock,
    )


def _login(client, *, remember=False, password=PASSWORD, next_path=None):
    data = {"token": password}
    if remember:
        data["remember_device"] = "1"
    if next_path is not None:
        data["next"] = next_path
    return client.post(LOGIN, data=data)


def _cookie(client):
    return client.get_cookie(client.application.config["SESSION_COOKIE_NAME"]).value


def _cookie_payload(client):
    serializer = client.application.session_interface.get_signing_serializer(client.application)
    return serializer.loads(_cookie(client))


def _replay(app, cookie):
    client = app.test_client()
    client.set_cookie(app.config["SESSION_COOKIE_NAME"], cookie)
    return client


def _rows(env):
    with sqlite3.connect(env.db_path) as db:
        db.row_factory = sqlite3.Row
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_auth_sessions'"
        ).fetchone(), "Logins must have a server-side revocation record"
        return [dict(row) for row in db.execute("SELECT * FROM web_auth_sessions")]


@pytest.mark.parametrize("remember,days", [(False, 7), (True, 30)])
def test_login_creates_hashed_server_record_with_absolute_lifetime(auth_env, remember, days):
    client = auth_env.app.test_client()
    response = _login(client, remember=remember)
    assert response.status_code == 302

    rows = _rows(auth_env)
    assert len(rows) == 1
    payload = _cookie_payload(client)
    session_id = payload["auth_session_id"]
    assert len(session_id) >= 43
    assert rows[0]["token_hash"] == hashlib.sha256(session_id.encode("ascii")).hexdigest()
    assert rows[0]["created_at"] == auth_env.clock.now
    assert rows[0]["expires_at"] == auth_env.clock.now + days * DAY
    assert rows[0]["persistent"] == int(remember)
    assert PASSWORD not in str(rows) + str(payload)
    assert session_id not in str(rows)


def test_remember_option_sets_a_persistent_cookie(auth_env):
    client = auth_env.app.test_client()
    response = _login(client, remember=True)
    cookie = SimpleCookie(response.headers["Set-Cookie"])["session"]
    assert cookie["expires"], "Remembering this device must survive a browser restart"
    assert cookie["httponly"]
    assert cookie["samesite"] == "Lax"
    assert cookie["path"] == "/"


def test_logout_revokes_old_cookie_without_logging_out_another_device(auth_env):
    first, other = auth_env.app.test_client(), auth_env.app.test_client()
    assert _login(first, remember=True).status_code == 302
    assert _login(other, remember=True).status_code == 302
    stolen_cookie = _cookie(first)
    csrf = first.get("/api/csrf-token").get_json()["csrf_token"]

    response = first.post(LOGOUT, headers={"X-CSRF-Token": csrf})

    assert response.status_code == 200
    assert _replay(auth_env.app, stolen_cookie).get(PROTECTED).status_code == 401
    assert first.get(PROTECTED).status_code == 401
    assert other.get(PROTECTED).status_code == 200
    assert len(_rows(auth_env)) == 1


def test_relogin_rotates_and_revokes_this_browser_only(auth_env):
    client, other = auth_env.app.test_client(), auth_env.app.test_client()
    assert _login(client).status_code == 302
    assert _login(other).status_code == 302
    old_cookie = _cookie(client)

    assert _login(client, remember=True).status_code == 302

    assert _cookie(client) != old_cookie
    assert _replay(auth_env.app, old_cookie).get(PROTECTED).status_code == 401
    assert client.get(PROTECTED).status_code == 200
    assert other.get(PROTECTED).status_code == 200
    assert len(_rows(auth_env)) == 2


def test_effective_password_change_invalidates_old_session(auth_env, monkeypatch):
    client = auth_env.app.test_client()
    assert _login(client, remember=True).status_code == 302
    monkeypatch.setenv("DASHBOARD_TOKEN", "new-test-only-password")
    auth_env.clock.now += 6  # The existing SettingsManager caches settings for five seconds.

    assert client.get(PROTECTED).status_code == 401
    assert _login(client, password="new-test-only-password").status_code == 302
    assert client.get(PROTECTED).status_code == 200


def test_legacy_signed_cookie_is_not_authentication(auth_env):
    client = auth_env.app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["authenticated_at"] = auth_env.clock.now

    assert client.get(PROTECTED).status_code == 401


def test_verification_storage_outage_returns_503_and_preserves_cookie(auth_env, monkeypatch):
    client = auth_env.app.test_client()
    assert _login(client, remember=True).status_code == 302
    original_cookie = _cookie(client)

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("private database path must not leak")

    with monkeypatch.context() as patch:
        patch.setattr(webapp_module, "_open_database", unavailable)
        response = client.get(PROTECTED)
        assert response.status_code == 503
        assert "Set-Cookie" not in response.headers
        assert _cookie(client) == original_cookie
        assert "private database path" not in response.get_data(as_text=True)

    assert client.get(PROTECTED).status_code == 200


def test_login_page_is_standalone_mobile_form(auth_env):
    response = auth_env.app.test_client().get(LOGIN)
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'name="viewport"' in body
    assert 'name="remember_device"' in body
    assert 'value="1"' in body
    assert 'autocomplete="current-password"' in body
    assert '<label for="token">' in body
    assert "48px" in body and "16px" in body
    assert "<script" not in body
    assert "localStorage" not in body
    assert "cdn" not in body.lower()
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_login_restores_safe_next_path(auth_env):
    client = auth_env.app.test_client()
    next_path = "/dashboard/novels/42?from=bookmarks&page=3"
    response = _login(client, next_path=next_path)
    assert response.status_code == 302
    assert response.headers["Location"] == next_path


@pytest.mark.parametrize("remember,days", [(False, 7), (True, 30)])
def test_expiry_boundary_is_absolute_and_requests_do_not_refresh_it(auth_env, remember, days):
    client = auth_env.app.test_client()
    assert _login(client, remember=remember).status_code == 302
    original_cookie = _cookie(client)
    original_rows = _rows(auth_env)
    issued = auth_env.clock.now

    for elapsed in (DAY, (days - 1) * DAY, days * DAY - 1):
        auth_env.clock.now = issued + elapsed
        response = client.get(PROTECTED)
        assert response.status_code == 200
        assert "Set-Cookie" not in response.headers
        assert _cookie(client) == original_cookie
        assert _rows(auth_env) == original_rows

    auth_env.clock.now = issued + days * DAY
    assert _replay(auth_env.app, original_cookie).get(PROTECTED).status_code == 401
    auth_env.clock.now += 1
    assert _replay(auth_env.app, original_cookie).get(PROTECTED).status_code == 401


def test_unchecked_remember_is_a_browser_session_cookie(auth_env):
    response = _login(auth_env.app.test_client())
    cookie = SimpleCookie(response.headers["Set-Cookie"])["session"]
    assert not cookie["expires"]
    assert not cookie["max-age"]


@pytest.mark.parametrize("submitted", ["0", "true", "on", ""])
def test_only_explicit_remember_value_one_enables_persistence(auth_env, submitted):
    client = auth_env.app.test_client()
    response = client.post(LOGIN, data={"token": PASSWORD, "remember_device": submitted})
    assert response.status_code == 302
    assert not SimpleCookie(response.headers["Set-Cookie"])["session"]["expires"]
    row, = _rows(auth_env)
    assert row["persistent"] == 0
    assert row["expires_at"] - row["created_at"] == 7 * DAY


def test_persistent_cookie_expiry_stays_fixed_when_csrf_is_reissued(auth_env):
    client = auth_env.app.test_client()
    first_response = _login(client, remember=True)
    expected_expiry = auth_env.clock.now + 30 * DAY
    first_cookie = SimpleCookie(first_response.headers["Set-Cookie"])["session"]
    assert parsedate_to_datetime(first_cookie["expires"]).timestamp() == expected_expiry
    original_rows = _rows(auth_env)

    # A session mutation may cause Flask to save its cookie again; it must not
    # reset the browser's Expires even on the CSRF endpoint (an auth exemption).
    with client.session_transaction() as sess:
        sess.pop("csrf_token")
    auth_env.clock.now += 20 * DAY
    response = client.get("/api/csrf-token")
    assert response.status_code == 200
    refreshed = SimpleCookie(response.headers["Set-Cookie"])["session"]
    assert parsedate_to_datetime(refreshed["expires"]).timestamp() == expected_expiry
    assert _rows(auth_env) == original_rows


def test_remembered_login_survives_app_and_browser_restart(auth_env):
    client = auth_env.app.test_client()
    assert _login(client, remember=True).status_code == 302
    remembered_cookie = _cookie(client)
    original_rows = _rows(auth_env)
    auth_env.clock.now += 10 * DAY
    Database._reset_schema_ready_cache()  # Simulate a fresh process, not just a new Flask app.

    restarted_app = auth_env.new_app()

    assert restarted_app.secret_key == auth_env.app.secret_key
    assert _replay(restarted_app, remembered_cookie).get(PROTECTED).status_code == 200
    assert _rows(auth_env) == original_rows
    assert restarted_app.test_client().get(PROTECTED).status_code == 401


def test_credential_version_is_purpose_separated_hmac_not_a_password_hash(auth_env):
    client = auth_env.app.test_client()
    assert _login(client).status_code == 302
    row, = _rows(auth_env)
    expected = hmac.new(
        auth_env.app.secret_key.encode("utf-8"),
        b"pixiv-novel-sync:web-auth-credential:v1\0" + PASSWORD.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert row["credential_version"] == expected
    assert row["credential_version"] != hashlib.sha256(PASSWORD.encode("utf-8")).hexdigest()
    assert "credential_version" not in _cookie_payload(client)
    assert "authenticated" not in _cookie_payload(client)
    assert "authenticated_at" not in _cookie_payload(client)


@pytest.mark.parametrize("session_id", [None, "", 17, [], "x" * 43, "../../token", "中文" * 22])
def test_missing_malformed_and_unknown_identifiers_never_authenticate(auth_env, session_id):
    client = auth_env.app.test_client()
    with client.session_transaction() as sess:
        # Legacy fields must not turn an absent/unrecognised identifier into auth.
        sess["authenticated"] = True
        sess["authenticated_at"] = auth_env.clock.now
        sess["auth_session_id"] = session_id
    assert client.get(PROTECTED).status_code == 401


def test_unsigned_or_tampered_cookie_never_authenticates(auth_env):
    client = auth_env.app.test_client()
    assert _login(client, remember=True).status_code == 302
    cookie = _cookie(client)
    payload, timestamp, signature = cookie.rsplit(".", 2)
    tampered = f"{payload}.{timestamp}.{'a' if signature[0] != 'a' else 'b'}{signature[1:]}"
    assert _replay(auth_env.app, tampered).get(PROTECTED).status_code == 401
    assert _replay(auth_env.app, '{"authenticated":true}').get(PROTECTED).status_code == 401


@pytest.mark.parametrize("target", [PROTECTED, "/dashboard", LOGOUT])
@pytest.mark.parametrize("error", [sqlite3.OperationalError, OSError, RuntimeError])
def test_storage_read_failures_fail_closed_without_changing_cookie(auth_env, monkeypatch, target, error):
    client = auth_env.app.test_client()
    assert _login(client, remember=True).status_code == 302
    original_cookie = _cookie(client)
    csrf = client.get("/api/csrf-token").get_json()["csrf_token"]

    def unavailable(*args, **kwargs):
        raise error("sensitive internal storage details")

    with monkeypatch.context() as patch:
        patch.setattr(Database, "get_web_auth_session", unavailable, raising=False)
        response = (client.post(target, headers={"X-CSRF-Token": csrf})
                    if target == LOGOUT else client.get(target))
        assert response.status_code == 503
        assert "Set-Cookie" not in response.headers
        assert "sensitive internal" not in response.get_data(as_text=True)
        assert _cookie(client) == original_cookie

    assert client.get(PROTECTED).status_code == 200


def test_login_storage_failure_keeps_old_login_and_does_not_count_as_bad_password(auth_env, monkeypatch):
    client = auth_env.app.test_client()
    assert _login(client, remember=True).status_code == 302
    original_cookie, original_rows = _cookie(client), _rows(auth_env)
    tracker = auth_env.app.config["login_failure_tracker"]
    tracker.record_failure("127.0.0.1", auth_env.clock.now)

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("private write failure")

    with monkeypatch.context() as patch:
        patch.setattr(webapp_module, "_open_database", unavailable)
        response = _login(client)
        assert response.status_code == 503
        assert response.mimetype == "text/html"
        assert 'role="alert"' in response.get_data(as_text=True)
        assert "Set-Cookie" not in response.headers
        assert _cookie(client) == original_cookie
        assert len(tracker) == 1

    assert _rows(auth_env) == original_rows
    assert client.get(PROTECTED).status_code == 200
    assert _login(client).status_code == 302
    assert len(tracker) == 0


def test_failed_session_rotation_rolls_back_revoke_and_insert(auth_env):
    client = auth_env.app.test_client()
    assert _login(client, remember=True).status_code == 302
    old_cookie, old_rows = _cookie(client), _rows(auth_env)
    with sqlite3.connect(auth_env.db_path) as db:
        db.execute("""CREATE TRIGGER fail_session_insert BEFORE INSERT ON web_auth_sessions
                      BEGIN SELECT RAISE(ABORT, 'test write failure'); END""")

    response = _login(client)

    assert response.status_code == 503
    assert "Set-Cookie" not in response.headers
    assert _cookie(client) == old_cookie
    assert _rows(auth_env) == old_rows
    assert client.get(PROTECTED).status_code == 200
    with sqlite3.connect(auth_env.db_path) as db:
        db.execute("DROP TRIGGER fail_session_insert")
    assert _login(client).status_code == 302
    assert _replay(auth_env.app, old_cookie).get(PROTECTED).status_code == 401


def test_logout_write_failure_preserves_cookie_until_revocation_succeeds(auth_env):
    client = auth_env.app.test_client()
    assert _login(client, remember=True).status_code == 302
    old_cookie, old_rows = _cookie(client), _rows(auth_env)
    csrf = client.get("/api/csrf-token").get_json()["csrf_token"]
    with sqlite3.connect(auth_env.db_path) as db:
        db.execute("""CREATE TRIGGER fail_session_delete BEFORE DELETE ON web_auth_sessions
                      BEGIN SELECT RAISE(ABORT, 'test revoke failure'); END""")

    response = client.post(LOGOUT, headers={"X-CSRF-Token": csrf})

    assert response.status_code == 503
    assert "Set-Cookie" not in response.headers
    assert _cookie(client) == old_cookie
    assert _rows(auth_env) == old_rows
    with sqlite3.connect(auth_env.db_path) as db:
        db.execute("DROP TRIGGER fail_session_delete")
    assert client.get(PROTECTED).status_code == 200
    assert client.post(LOGOUT, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert _replay(auth_env.app, old_cookie).get(PROTECTED).status_code == 401


def test_new_login_cleans_expired_records_but_keeps_other_live_devices(auth_env):
    expired, live, fresh = (auth_env.app.test_client() for _ in range(3))
    assert _login(expired).status_code == 302
    assert _login(live, remember=True).status_code == 302
    old_cookie = _cookie(expired)
    auth_env.clock.now += 7 * DAY

    assert _login(fresh).status_code == 302

    assert len(_rows(auth_env)) == 2
    assert live.get(PROTECTED).status_code == 200
    assert _replay(auth_env.app, old_cookie).get(PROTECTED).status_code == 401


def test_auth_schema_is_additive_and_idempotent(auth_env):
    client = auth_env.app.test_client()
    assert _login(client).status_code == 302
    original_rows = _rows(auth_env)
    with sqlite3.connect(auth_env.db_path) as db:
        db.execute("INSERT INTO users(user_id, name, raw_json) VALUES (123, 'untouched', '{}')")
        original_user = db.execute("SELECT * FROM users WHERE user_id=123").fetchone()
    db = Database(auth_env.db_path)
    try:
        db.init_schema()
        db.init_schema()
        assert tuple(db.conn.execute("SELECT * FROM users WHERE user_id=123").fetchone()) == original_user
        indexes = {row[1] for row in db.conn.execute("PRAGMA index_list(web_auth_sessions)")}
        assert "idx_web_auth_sessions_expiry" in indexes
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(
                "INSERT INTO web_auth_sessions VALUES ('bad', 'version', 1, 2, 2)"
            )
    finally:
        db.close()
    assert _rows(auth_env) == original_rows


def test_relogin_rotates_csrf_and_logout_requires_current_csrf(auth_env):
    client = auth_env.app.test_client()
    assert _login(client, remember=True).status_code == 302
    old_csrf = client.get("/api/csrf-token").get_json()["csrf_token"]
    assert _login(client).status_code == 302
    csrf = client.get("/api/csrf-token").get_json()["csrf_token"]
    assert csrf != old_csrf
    assert client.post(LOGOUT).status_code == 403
    assert client.post(LOGOUT, headers={"X-CSRF-Token": old_csrf}).status_code == 403
    assert client.get(PROTECTED).status_code == 200
    assert client.post(LOGOUT, headers={"X-CSRF-Token": csrf}).status_code == 200


def test_health_and_rescue_auth_boundaries_are_preserved(auth_env, monkeypatch):
    client = auth_env.app.test_client()
    assert _login(client).status_code == 302
    rescue = client.get("/api/rescue/v1/novels/1")
    assert rescue.status_code == 401
    assert "Bearer" in rescue.headers["WWW-Authenticate"]

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("unavailable")

    monkeypatch.setattr(webapp_module, "_open_database", unavailable)
    assert client.get("/api/health").status_code == 200
    assert client.get(LOGIN).status_code == 200


@pytest.mark.parametrize("secure", ["0", "1"])
def test_cookie_secure_configuration_is_honoured(auth_env, monkeypatch, secure):
    monkeypatch.setenv("PIXIV_COOKIE_SECURE", secure)
    app = auth_env.new_app()
    response = _login(app.test_client(), remember=True)
    cookie = SimpleCookie(response.headers["Set-Cookie"])["session"]
    assert bool(cookie["secure"]) is (secure == "1")
    assert cookie["httponly"] and cookie["samesite"] == "Lax"


class _FormParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = {}
        self.forms = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name"):
            self.inputs[attrs["name"]] = attrs
        if tag == "form":
            self.forms.append(attrs)


def test_remember_is_unchecked_by_default_and_password_is_never_reflected(auth_env):
    client = auth_env.app.test_client()
    html = client.get(LOGIN).get_data(as_text=True)
    form = _FormParser(html)
    assert "remember_device" in form.inputs
    assert "checked" not in form.inputs["remember_device"]
    assert form.forms[0]["method"].lower() == "post"
    assert form.forms[0]["action"] == LOGIN

    bad_password = '<secret-"-script>'
    response = _login(client, password=bad_password, remember=True, next_path="/dashboard/novels?page=3")
    assert response.status_code == 401
    assert response.mimetype == "text/html"
    body = response.get_data(as_text=True)
    assert 'role="alert"' in body
    assert bad_password not in body and "secret-" not in body
    parsed = _FormParser(body)
    assert not parsed.inputs["token"].get("value")
    assert "checked" in parsed.inputs["remember_device"]
    assert parsed.inputs["next"]["value"] == "/dashboard/novels?page=3"
    assert "no-store" in response.headers["Cache-Control"]


def test_rate_limit_is_inline_does_not_issue_auth_and_recovers_after_window(auth_env):
    client = auth_env.app.test_client()
    for _ in range(5):
        assert _login(client, password="wrong").status_code == 401
    blocked = _login(client, remember=True)
    assert blocked.status_code == 429
    assert blocked.mimetype == "text/html"
    assert 'role="alert"' in blocked.get_data(as_text=True)
    assert int(blocked.headers["Retry-After"]) > 0
    assert client.get(PROTECTED).status_code == 401
    assert not _rows(auth_env)
    auth_env.clock.now += 300
    assert _login(client).status_code == 302


def test_chinese_password_logs_in_with_hmac_binding(auth_env, monkeypatch):
    password = "访问口令🔑"
    monkeypatch.setenv("DASHBOARD_TOKEN", password)
    auth_env.clock.now += 6
    client = auth_env.app.test_client()
    assert _login(client, password="错误口令").status_code == 401
    assert _login(client, password=password, remember=True).status_code == 302
    assert client.get(PROTECTED).status_code == 200
    assert password not in str(_rows(auth_env)) + str(_cookie_payload(client))


def test_protected_page_redirect_preserves_path_and_query(auth_env):
    response = auth_env.app.test_client().get("/dashboard/novels/42?from=bookmarks&page=3")
    assert response.status_code == 302
    parsed = urlsplit(response.headers["Location"])
    assert parsed.path == LOGIN
    assert parse_qs(parsed.query)["next"] == ["/dashboard/novels/42?from=bookmarks&page=3"]


@pytest.mark.parametrize("next_path", [
    "https://evil.example/", "http://localhost/dashboard", "//evil.example/x",
    "\\evil.example", "/\\evil.example", "/safe\\bad", "javascript:alert(1)",
    "/%2fevil.example", "/%255cevil.example", "/good%0abad", "/good\r\nbad",
    "/good\x00bad", "/good\x7fbad", "/good\u0085bad",
    LOGIN, LOGIN + "/", LOGIN + "?next=/dashboard", LOGIN + "#fragment",
    "/other/../api/auth/login", "/%61pi/auth/login", "relative/path",
])
def test_unsafe_next_paths_fall_back_to_home(auth_env, next_path):
    client = auth_env.app.test_client()
    response = _login(client, next_path=next_path)
    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_next_is_preserved_in_form_but_revalidated_on_post(auth_env):
    client = auth_env.app.test_client()
    target = '/dashboard/novels?title="quoted"&page=3'
    html = client.get(LOGIN, query_string={"next": target}).get_data(as_text=True)
    assert _FormParser(html).inputs["next"]["value"] == target
    assert 'value="/dashboard/novels?title="quoted"' not in html
    response = client.post(LOGIN, data={"token": PASSWORD, "next": "//evil.example"})
    assert response.headers["Location"] == "/"

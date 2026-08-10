from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from flask import Flask, session

from pixiv_novel_sync.settings import Settings, StorageSettings


def _settings(tmp_path, dashboard_token: str | None) -> Settings:
    return Settings(
        pixiv=None,  # type: ignore[arg-type]
        sync=None,  # type: ignore[arg-type]
        storage=StorageSettings(
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
            db_path=tmp_path / "adult-auth.db",
        ),
        dashboard_token=dashboard_token,
    )


def test_require_adult_owner_rejects_authenticated_loopback_without_token(tmp_path):
    from pixiv_novel_sync.ai.adult_auth import require_adult_owner

    app = Flask(__name__)
    app.secret_key = "test-app-secret"
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        session["authenticated"] = True
        session["authenticated_at"] = int(time.time())

        with pytest.raises(PermissionError):
            require_adult_owner(_settings(tmp_path, None))
        with pytest.raises(PermissionError):
            require_adult_owner(object())  # type: ignore[arg-type]


def test_require_adult_owner_derives_secret_free_hmac_scope(tmp_path):
    from pixiv_novel_sync.ai.adult_auth import require_adult_owner

    app = Flask(__name__)
    app.secret_key = "test-app-secret"
    authenticated_at = int(time.time())
    with app.test_request_context("/"):
        session["authenticated"] = True
        session["authenticated_at"] = authenticated_at

        owner = require_adult_owner(_settings(tmp_path, "dashboard-secret"))

    expected = hmac.new(
        b"test-app-secret",
        b"adult-owner:dashboard-secret",
        hashlib.sha256,
    ).hexdigest()
    assert owner.scope == expected
    assert owner.authenticated_at == authenticated_at
    assert "dashboard-secret" not in repr(owner)


def test_signed_adult_access_is_expiry_owner_and_job_bound(tmp_path, monkeypatch):
    from pixiv_novel_sync.ai import adult_auth
    from pixiv_novel_sync.ai.adult_auth import AdultOwner

    app = Flask(__name__)
    app.secret_key = "test-app-secret"
    base_ns = 1_700_000_000 * 1_000_000_000
    owner = AdultOwner(scope="a" * 64, authenticated_at=base_ns)
    other_owner = AdultOwner(scope="b" * 64, authenticated_at=base_ns)
    monkeypatch.setattr(adult_auth.time, "time_ns", lambda: base_ns)
    with app.app_context():
        token = adult_auth.sign_adult_access(owner, "job-1")
        adult_auth.verify_adult_access(token, owner, "job-1")

        with pytest.raises(PermissionError):
            adult_auth.verify_adult_access(token, other_owner, "job-1")
        with pytest.raises(PermissionError):
            adult_auth.verify_adult_access(token, owner, "job-2")

        monkeypatch.setattr(
            adult_auth.time,
            "time_ns",
            lambda: base_ns + 1_000_000_000,
        )
        reauthenticated_owner = AdultOwner(
            scope=owner.scope,
            authenticated_at=base_ns + 1_000_000_000,
        )
        with pytest.raises(PermissionError):
            adult_auth.verify_adult_access(token, reauthenticated_owner, "job-1")

        monkeypatch.setattr(
            adult_auth.time,
            "time_ns",
            lambda: base_ns + 600 * 1_000_000_000,
        )
        with pytest.raises(PermissionError):
            adult_auth.verify_adult_access(token, owner, "job-1")


def test_signed_access_reauthentication_in_same_second_invalidates_prior_token(
    monkeypatch,
):
    from pixiv_novel_sync.ai import adult_auth
    from pixiv_novel_sync.ai.adult_auth import AdultOwner

    app = Flask(__name__)
    app.secret_key = "test-app-secret"
    second = 1_700_000_000
    issued_ns = second * 1_000_000_000 + 100
    monkeypatch.setattr(adult_auth.time, "time", lambda: second)
    monkeypatch.setattr(adult_auth.time, "time_ns", lambda: issued_ns)
    owner = AdultOwner(scope="a" * 64, authenticated_at=issued_ns - 1)

    with app.app_context():
        token = adult_auth.sign_adult_access(owner, "job-1")
        adult_auth.verify_adult_access(token, owner, "job-1")

        reauthenticated_owner = AdultOwner(
            scope=owner.scope,
            authenticated_at=issued_ns + 1,
        )
        with pytest.raises(PermissionError):
            adult_auth.verify_adult_access(token, reauthenticated_owner, "job-1")

from types import SimpleNamespace

import pytest
from pixivpy3 import PixivError

from pixiv_novel_sync.auth import PixivAuthManager
from pixiv_novel_sync.sync.utils import apply_pixiv_retry_patch


class AuthApi:
    def __init__(self, identity=99, token="renewed"):
        self.identity = identity
        self.access_token = token
        self.refresh_token = "refresh"
        self.calls = []
        self.response = {"error": {"message": "Error occurred at the OAuth process: invalid_grant"}}

    def auth(self, **kwargs):
        return {"user": {"id": self.identity}}

    def set_auth(self, access_token, refresh_token):
        self.access_token = access_token
        self.refresh_token = refresh_token

    def novel_detail(self, novel_id):
        self.calls.append(novel_id)
        if self.access_token == "old":
            if isinstance(self.response, Exception):
                raise self.response
            return self.response
        return {"novel": {"id": novel_id}}


def login(monkeypatch, candidate=None):
    original = AuthApi(token="old")
    candidate = candidate or AuthApi()
    clients = iter([original, candidate])
    manager = PixivAuthManager(SimpleNamespace(refresh_token="refresh", access_token=None, user_id=99))
    monkeypatch.setattr(manager, "create_api", lambda: next(clients))
    api, result = manager.login()
    apply_pixiv_retry_patch(api)
    return api, candidate


def test_oauth_renewal_retries_only_current_read_and_only_once(monkeypatch):
    api, candidate = login(monkeypatch)
    assert api.novel_detail(13) == {"novel": {"id": 13}}
    assert api.calls == [13, 13]
    api.access_token = "old"
    assert "error" in api.novel_detail(14)
    assert api.calls == [13, 13, 14]


def test_oauth_exception_body_is_renewed(monkeypatch):
    api, candidate = login(monkeypatch)
    api.response = PixivError("invalid response", body='{"error":{"message":"invalid_grant"}}')
    assert api.novel_detail(13)["novel"]["id"] == 13
    assert api.calls == [13, 13]


def test_wrong_account_is_rejected_without_replacing_credentials(monkeypatch):
    api, candidate = login(monkeypatch, AuthApi(identity=123))
    assert "error" in api.novel_detail(13)
    assert api.access_token == "old"
    assert api.calls == [13]


def test_cancellation_during_renewal_prevents_retry(monkeypatch):
    api, candidate = login(monkeypatch)
    cancelled = []

    def renew(**kwargs):
        cancelled.append(True)
        return {"user": {"id": 99}}

    candidate.auth = renew
    apply_pixiv_retry_patch(api, stop_requested=lambda: bool(cancelled))
    with pytest.raises(InterruptedError):
        api.novel_detail(13)
    assert api.calls == [13]


def test_ambiguous_restriction_does_not_trigger_renewal(monkeypatch):
    api, candidate = login(monkeypatch)
    api.response = {"error": {"user_message": "您的访问权限已经被限制了"}}
    assert "error" in api.novel_detail(13)
    assert api.access_token == "old"
    assert api.calls == [13]

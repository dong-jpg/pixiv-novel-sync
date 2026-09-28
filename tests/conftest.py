from __future__ import annotations

import os
import secrets
from pathlib import Path

import pytest
from flask.testing import FlaskClient

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_original_client_open = FlaskClient.open


def _open_with_local_csrf(self, *args, **kwargs):
    """未配置口令时，测试客户端替浏览器补上 CSRF。

    生产代码已经要求本机变更请求带头。显式断言「没有头就 403」的用例把
    ``app.config['RAW_HTTP']`` 设为真，这里就不代填。配置了口令的用例自己带头。
    """
    if os.environ.get("DASHBOARD_TOKEN") or os.environ.get("PIXIV_DASHBOARD_TOKEN"):
        return _original_client_open(self, *args, **kwargs)
    if self.application.config.get("RAW_HTTP"):
        return _original_client_open(self, *args, **kwargs)
    method = str(kwargs.get("method") or "GET").upper()
    if method not in _MUTATING:
        return _original_client_open(self, *args, **kwargs)
    headers = dict(kwargs.get("headers") or {})
    if not any(str(key).lower() == "x-csrf-token" for key in headers):
        with self.session_transaction() as sess:
            token = sess.get("csrf_token")
            if not token:
                token = secrets.token_urlsafe(32)
                sess["csrf_token"] = token
        headers["X-CSRF-Token"] = token
        kwargs["headers"] = headers
    return _original_client_open(self, *args, **kwargs)


FlaskClient.open = _open_with_local_csrf


@pytest.fixture(autouse=True)
def isolate_runtime_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("ENV_PATH", raising=False)
    # load_dotenv 会把测试 .env 里的键写进进程环境；用例内部再 delenv 会把
    # 污染值记成「原值」，teardown 时原样放回（实测 DASHBOARD_TRUST_PROXY=true
    # 泄漏后，后续无 token 用例全部 403）。这里在每个用例开头兜底清掉。
    monkeypatch.delenv("DASHBOARD_TRUST_PROXY", raising=False)
    monkeypatch.delenv("DASHBOARD_TRUSTED_PROXY_HOPS", raising=False)
    monkeypatch.delenv("PIXIV_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("PIXIV_DB_PATH", str(tmp_path / "state" / "test.db"))
    monkeypatch.setenv("PIXIV_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("PIXIV_PRIVATE_DIR", str(tmp_path / "private"))
    # ensure_schema 的进程级记忆按 db_path 缓存；测试间 tmp_path 复用同一路径名
    # 时会误判 schema 已就绪而跳过建表，导致 "no such table"。每个测试前后清空。
    from pixiv_novel_sync.storage_db import Database

    Database._reset_schema_ready_cache()
    yield
    Database._reset_schema_ready_cache()

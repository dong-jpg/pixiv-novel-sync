from __future__ import annotations

import os
import subprocess
import sys
import types
from contextlib import contextmanager

from pixiv_novel_sync import playwright_login
from pixiv_novel_sync.playwright_login import PlaywrightLoginHelper


def test_login_web_restores_display_so_the_next_login_starts_xvfb_again(monkeypatch):
    """回归（审查 2026-10-28 S3）：login_web 启动 Xvfb 后设了 DISPLAY，结束时只关掉 Xvfb
    不还原 DISPLAY；同一进程第二次登录以为已有显示器，不再拉起 Xvfb，headed 浏览器连不上。"""
    started: list[list[str]] = []

    class _FakeXvfb:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self):
            return 0

    def fake_popen(args, **kwargs):
        started.append(list(args))
        return _FakeXvfb()

    @contextmanager
    def fake_sync_playwright():
        yield object()

    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = fake_sync_playwright
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(playwright_login.time, "sleep", lambda seconds: None)
    monkeypatch.delenv("DISPLAY", raising=False)
    helper = PlaywrightLoginHelper()
    monkeypatch.setattr(helper, "_do_login_web", lambda p, username, password: {"success": False, "error": "fake"})

    helper.login_web("user", "pass")
    assert "DISPLAY" not in os.environ
    helper.login_web("user", "pass")

    assert len(started) == 2
    assert all(args[0] == "Xvfb" for args in started)
    assert "DISPLAY" not in os.environ


def test_pixiv_callback_url_requires_hostname_and_code():
    helper = PlaywrightLoginHelper()

    assert helper._is_pixiv_callback_url(
        "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?code=abc&state=xyz"
    )
    assert not helper._is_pixiv_callback_url("mailto:user@example.test?code=abc")
    assert not helper._is_pixiv_callback_url("https://app-api.pixiv.net.evil.test/callback?code=abc")
    assert not helper._is_pixiv_callback_url("https://app-api.pixiv.net/callback?state=xyz")

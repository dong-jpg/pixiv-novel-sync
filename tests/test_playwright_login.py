from __future__ import annotations

from pixiv_novel_sync.playwright_login import PlaywrightLoginHelper


def test_pixiv_callback_url_requires_hostname_and_code():
    helper = PlaywrightLoginHelper()

    assert helper._is_pixiv_callback_url(
        "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?code=abc&state=xyz"
    )
    assert not helper._is_pixiv_callback_url("mailto:user@example.test?code=abc")
    assert not helper._is_pixiv_callback_url("https://app-api.pixiv.net.evil.test/callback?code=abc")
    assert not helper._is_pixiv_callback_url("https://app-api.pixiv.net/callback?state=xyz")

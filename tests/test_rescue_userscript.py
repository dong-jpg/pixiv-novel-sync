from __future__ import annotations

from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).parents[1] / "userscripts" / "pixiv-rescue.user.js"


def _script() -> str:
    assert SCRIPT_PATH.exists(), "救援油猴脚本尚未创建"
    return SCRIPT_PATH.read_text(encoding="utf-8")


def test_userscript_metadata_and_security_contract() -> None:
    script = _script()

    assert "@match        https://www.pixiv.net/novel/show.php*" in script
    assert "@match        https://www.pixiv.net/novel/series/*" in script
    assert "@connect     pixiv.dongboapp.com" in script
    assert "GM_xmlhttpRequest" in script
    assert "GM_getValue" in script
    assert "GM_setValue" in script
    assert "GM_registerMenuCommand" in script
    assert "Authorization" in script
    assert "textContent" in script
    assert "innerHTML" not in script
    assert "?token=" not in script
    assert "location.origin" not in script
    assert "new URL(response" not in script


def test_userscript_uses_fixed_api_paths_and_safe_rendering() -> None:
    script = _script()

    assert "const API_ORIGIN = 'https://pixiv.dongboapp.com';" in script
    assert "'/api/rescue/v1/novels/'" in script
    assert "'/api/rescue/v1/series/'" in script
    assert "'/chapters'" in script
    assert "encodeURIComponent" in script
    assert "data-pixiv-rescue" in script
    assert "source_notice" in script
    assert "append(" in script


def test_userscript_does_not_replace_healthy_pixiv_pages() -> None:
    script = _script()

    assert "isNovelPageHealthy" in script
    assert "isSeriesPageHealthy" in script
    assert "if (isNovelPageHealthy()) return;" in script
    assert "if (isSeriesPageHealthy()) return;" in script


@pytest.fixture(scope="module")
def rescue_browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - depends on local browser install
            pytest.skip(f"Playwright 浏览器不可用：{exc}")
        yield browser
        browser.close()


def _install_script(
    page,
    html: str,
    responses: dict[str, dict],
    path: str = "/novel/show.php?id=101",
) -> None:
    script = _script()
    page.add_init_script(
        """
        window.__rescueRequests = [];
        window.GM_getValue = (key, fallback) => key === 'pixivRescueToken' ? 'rsq_test' : fallback;
        window.GM_setValue = () => {};
        window.GM_registerMenuCommand = () => {};
        window.GM_xmlhttpRequest = (options) => {
          window.__rescueRequests.push({url: options.url, method: options.method});
          const payload = window.__rescueResponses[options.url];
          setTimeout(() => {
            if (!payload) {
              options.onload({status: 404, responseText: JSON.stringify({ok: false, error: 'missing fixture'})});
              return;
            }
            options.onload({status: 200, responseText: JSON.stringify({ok: true, data: payload})});
          }, 0);
        };
        """
    )
    page.route(
        "http://pixiv.test/**",
        lambda route: route.fulfill(
            status=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=html,
        ),
    )
    page.goto("http://pixiv.test" + path)
    page.evaluate("responses => { window.__rescueResponses = responses; }", responses)
    page.add_script_tag(content=script)


def test_userscript_fixture_keeps_healthy_novel_untouched(rescue_browser) -> None:
    page = rescue_browser.new_page()
    html = "<main><article class='novel-text'>这是原始正文内容，不应触发救援接口。</article></main>"
    _install_script(page, html, {})

    page.wait_for_timeout(50)
    assert page.evaluate("window.__rescueRequests") == []
    assert page.locator("[data-pixiv-rescue]").count() == 0
    assert page.locator(".novel-text").inner_text() == "这是原始正文内容，不应触发救援接口。"
    page.close()


def test_userscript_fixture_renders_deleted_novel_as_text(rescue_browser) -> None:
    page = rescue_browser.new_page()
    html = "<main><div class='error'>この作品は削除されています</div></main>"
    responses = {
        "https://pixiv.dongboapp.com/api/rescue/v1/novels/101": {
            "title": "备份小说",
            "text_raw": "第一行\n<script>alert('xss')</script>",
            "source_notice": "内容来自私人备份，并非 Pixiv 官方恢复",
            "rescue_state": "success",
        }
    }
    _install_script(page, html, responses)

    page.wait_for_timeout(100)
    assert page.locator("[data-pixiv-rescue]").count() == 1
    assert "拯救数据" in page.locator("[data-pixiv-rescue]").inner_text()
    assert "第一行" in page.locator("[data-pixiv-rescue]").inner_text()
    assert "<script>alert('xss')</script>" in page.locator("[data-pixiv-rescue]").inner_text()
    assert page.locator("[data-pixiv-rescue] script").count() == 0
    assert page.locator(".error").count() == 1
    page.close()


def test_userscript_fixture_loads_only_clicked_series_chapter(rescue_browser) -> None:
    page = rescue_browser.new_page()
    html = "<main><div class='error'>このシリーズは削除されています</div></main>"
    responses = {
        "https://pixiv.dongboapp.com/api/rescue/v1/series/202": {
            "series_id": 202,
            "title": "备份系列",
            "source_notice": "内容来自私人备份，并非 Pixiv 官方恢复",
            "rescue_state": "partial",
        },
        "https://pixiv.dongboapp.com/api/rescue/v1/series/202/chapters": {
            "items": [
                {"novel_id": 301, "title": "第一章"},
                {"novel_id": 302, "title": "第二章"},
            ]
        },
        "https://pixiv.dongboapp.com/api/rescue/v1/novels/302": {
            "title": "第二章",
            "text_raw": "第二章正文",
            "source_notice": "内容来自私人备份，并非 Pixiv 官方恢复",
            "rescue_state": "partial",
        },
    }
    _install_script(page, html, responses, path="/novel/series/202")

    page.wait_for_timeout(100)
    page.get_by_role("button", name="加载目录").click()
    page.get_by_role("button", name="第二章").click()
    page.wait_for_timeout(100)
    urls = page.evaluate("window.__rescueRequests.map(item => item.url)")
    assert urls.count("https://pixiv.dongboapp.com/api/rescue/v1/novels/302") == 1
    assert "https://pixiv.dongboapp.com/api/rescue/v1/novels/301" not in urls
    assert "第二章正文" in page.locator("[data-pixiv-rescue]").inner_text()
    page.close()

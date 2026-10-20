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
    assert "@match        https://www.pixiv.net/users/*/bookmarks/novels*" in script
    assert "@match        https://www.pixiv.net/following/watchlist/novels*" in script
    assert "@match        https://www.pixiv.net/novel/bookmark_new.php*" in script
    # 手机端语言前缀路径（/en/ 等）也要注入（0.3.3）
    assert "@match        https://www.pixiv.net/*/novel/show.php*" in script
    assert "@match        https://www.pixiv.net/*/users/*/bookmarks/novels*" in script
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

    assert "const DEFAULT_API_ORIGIN = 'https://pixiv.dongboapp.com';" in script
    assert "pixivRescueOrigin" in script
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


def test_userscript_matches_list_pages_without_disturbing_healthy_cards() -> None:
    script = _script()

    assert "isBookmarksListPage" in script
    assert "isWatchlistPage" in script
    assert "cardIsBroken" in script
    assert "/api/rescue/v1/lookup" in script
    assert "personal_hold" in script
    # 健康卡片不进候选：文本超过阈值的卡片直接跳过
    assert "text.length > BROKEN_CARD_TEXT_LIMIT) return false;" in script
    # 列表页只在失效卡片上打标记，不渲染救援面板、不动原页面
    assert "pixiv-rescue-card-badge" in script
    assert "handleListPage" in script
    # 系列被删/隐藏时卡片没有链接：靠 data-gtm-value 里的作品 ID 兜底识别，
    # 类型未知时 novel/series 两头都查，命中哪个算哪个
    assert "data-gtm-value" in script
    assert "'unknown'" in script
    # bookmark_new.php 顶部的「拯救系列」tab：复用救援系列列表接口分页渲染
    assert "isNovelBookmarksPage" in script
    assert "'/api/rescue/v1/series?page='" in script
    assert "injectRescueSeriesTab" in script
    assert "拯救系列" in script
    # Pixiv SPA 兼容：replaceState 也要触发路由重判
    assert "history.replaceState" in script


def test_userscript_locks_a14_fixes() -> None:
    """锁定 2026-10-20 批次修复：失败重试、40 ID 分批、路由代次、tab 追加。"""
    script = _script()

    # A14-03：卡片标记是 pending 两态，失败撤销后下一轮扫描可重试
    assert "'card-pending'" in script
    assert "removeAttribute(ROOT_ATTRIBUTE)" in script
    # A14-04：lookup 按 novel+series 合计 40 切批
    assert "LOOKUP_BATCH_LIMIT = 40" in script
    assert "buildLookupBatches" in script
    # A14-05：SPA 切页后旧请求应答按路由代次丢弃
    assert "routeGeneration" in script
    assert "bumpRouteGeneration" in script
    assert "novelIdFromPage() !== novelId" in script
    # P2-5：系列 tab「加载更多」是追加而非清空（loadPage 内不得再 clearNode(grid)）
    assert "loadingNote.remove()" in script
    load_page_body = script.split("function loadPage(page)", 1)[1].split("function ", 1)[0]
    assert "clearNode(grid)" not in load_page_body
    # interval 防重复注册
    assert "__pixivRescueListInterval" in script
    # 0.3.2：请求失败不再静默——Token 缺失/失效与网络故障显示页面级提示条
    assert "showRescueNotice" in script
    assert "救援脚本未设置 Token" in script
    assert "data-pixiv-rescue-notice" in script
    # 0.3.3：手机端兼容——GM API 能力检测 + 语言前缀路径剥离
    assert "checkRuntimeSupport" in script
    assert "pathWithoutLangPrefix" in script
    # 0.3.5：手机端真实 DOM 修复——文案「该作品无法显示」+ 官方类名锚点
    assert "'该作品无法显示'" in script
    assert "invisible-work-container" in script
    assert "div.works-item" in script
    # 失效文案距链接卡片 3 层深，cardIsBroken 上查四层
    assert "for (let i = 0; i < 4 && node && node.parentElement; i += 1)" in script
    # 0.3.7：去重键是共享容器（同一失效卡的双嵌套 <a> 只注册一次，单徽标）
    assert "link.closest('div.works-item, li') || link" in script
    assert "handledCards" in script
    # 0.4.0/0.4.1：列表页注入样式（旧版徽标 CSS 从未进列表页）+ 占位文案替换
    # 模式（「该作品无法显示」横贯整卡 316/412px，角标无处安放必重叠）
    assert "addStyles()" in script
    assert "placeholder.replaceWith(badge)" in script


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


def test_userscript_fixture_leaves_unknown_page_untouched(rescue_browser) -> None:
    page = rescue_browser.new_page()
    html = "<main><div class='loading'>正在加载 Pixiv 内容…</div></main>"
    _install_script(page, html, {})

    page.wait_for_timeout(1800)
    assert page.evaluate("window.__rescueRequests") == []
    assert page.locator("[data-pixiv-rescue]").count() == 0
    assert page.locator(".loading").count() == 1
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


def test_userscript_fixture_loads_additional_series_pages(rescue_browser) -> None:
    page = rescue_browser.new_page()
    html = "<main><div class='error'>このシリーズは削除されています</div></main>"
    responses = {
        "https://pixiv.dongboapp.com/api/rescue/v1/series/204": {
            "series_id": 204,
            "title": "长系列",
            "source_notice": "内容来自私人备份，并非 Pixiv 官方恢复",
            "rescue_state": "partial",
        },
        "https://pixiv.dongboapp.com/api/rescue/v1/series/204/chapters": {
            "items": [{"novel_id": 401, "title": "第一百章"}],
            "page": 1,
            "total_pages": 2,
        },
        "https://pixiv.dongboapp.com/api/rescue/v1/series/204/chapters?page=2&page_size=100": {
            "items": [{"novel_id": 402, "title": "第一百零一章"}],
            "page": 2,
            "total_pages": 2,
        },
    }
    _install_script(page, html, responses, path="/novel/series/204")

    page.get_by_role("button", name="加载目录").click()
    page.get_by_role("button", name="加载更多章节").click()
    page.get_by_role("button", name="第一百零一章").wait_for(state="visible")
    urls = page.evaluate("window.__rescueRequests.map(item => item.url)")
    assert urls.count(
        "https://pixiv.dongboapp.com/api/rescue/v1/series/204/chapters?page=2&page_size=100"
    ) == 1
    assert not any("/api/rescue/v1/novels/" in url for url in urls)
    page.close()


def test_userscript_fixture_preserves_pixiv_error_when_api_fails(rescue_browser) -> None:
    page = rescue_browser.new_page()
    html = "<main><div class='error'>この作品は削除されています</div></main>"
    _install_script(page, html, {}, path="/novel/show.php?id=999")

    page.locator(".pixiv-rescue-error").wait_for(state="visible")
    assert page.locator(".error").count() == 1
    assert "この作品は削除されています" in page.locator(".error").inner_text()
    page.close()

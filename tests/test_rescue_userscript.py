from __future__ import annotations

import json
import re
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
    """0.9.0：作品是否还在问 Pixiv 自己的 ajax，不再猜页面 DOM。

    2026-10-23 真机页实测：手机站健康正文页同样没有 <main>，旧版
    「无 main ⇒ 空壳」的判断让每个健康正文页都去请求备份、在页尾塞一条
    「未找到可用的救援数据」。Pixiv ajax 对已删作品稳定返回 404 + error:true。
    """
    script = _script()

    assert "function pixivWorkState(type, id)" in script
    assert "'/ajax/novel/series/'" in script
    assert "'/ajax/novel/'" in script
    assert "if (isStale() || state === 'available') return;" in script
    # 「确认失效」只认 404/410，403 等歧义状态不当成被删
    assert "response.status === 404 || response.status === 410" in script
    # 旧的 DOM 猜测已整体删除
    for removed in ("looksEmpty", "isNovelPageHealthy", "isSeriesPageHealthy",
                    "pageSaysUnavailable", "waitForUnavailableState"):
        assert removed not in script, removed


def test_userscript_matches_list_pages_without_disturbing_healthy_cards() -> None:
    script = _script()

    assert "isBookmarksListPage" in script
    assert "isWatchlistPage" in script
    assert "cardIsBroken" in script
    assert "/api/rescue/v1/lookup" in script
    assert "personal_hold" in script
    # 健康卡片不进候选：文本超过阈值的卡片直接跳过
    assert "text.length > BROKEN_CARD_TEXT_LIMIT) return false;" in script
    # 列表页只在失效卡片上重建为正常卡片外观（封面+标题+作者+小「拯救」标识），
    # 不渲染救援面板、不动健康卡片
    assert "pixiv-rescue-rescued-card" in script
    assert "pixiv-rescue-mark" in script
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
    # 0.4.0/0.4.1：列表页注入样式（旧版徽标 CSS 从未进列表页）
    assert "addStyles()" in script
    # 0.5.0：失效卡重建为正常卡片（封面+标题+作者）+ 小「拯救」标识，
    # 不再用整块红色警告；封面经 blob 取回，Token 只走 header
    assert "renderRescuedCard" in script
    assert "apiGetBlobUrl" in script
    assert "pixiv-rescue-mark" in script
    assert "/cover" in script
    # 0.6.0/0.6.1：本地缓存 + 首屏观察器；点击走 Pixiv 正文页（同正常卡片），
    # 由 handleNovelPage 在该页渲染备份，不再用遮挡收藏页的站内弹窗
    assert "readLookupCache" in script
    assert "readCoverCache" in script
    assert "MutationObserver" in script
    assert "openRescueReader" not in script
    # 0.6.2：「尚无此页」留在失效文案表里（列表页卡片识别仍用文案表；
    # 正文页 0.9.0 起改问 Pixiv ajax，不再靠文案或 <main> 判断）
    assert "'尚无此页'" in script
    # 列表页首屏快速轮询窗口（SPA 分批渲染时避免错过卡片）
    assert "fastScanUntil" in script
    # 0.6.3：点击意图优先——从救援卡点进来时直接渲染，不依赖页面文案；
    # 另加 1s URL 变化兜底（Pixiv 内部路由可能绕过 history hook）
    assert "consumeRescueIntent" in script
    assert "pixivRescueIntent" in script
    assert "window.setInterval(onRoute, 1000)" in script
    # 0.6.4：URL 标记跨整页导航可靠传递意图；document-start 保证尽早就位
    assert "pixiv_rescue=1" in script
    assert "params.get('pixiv_rescue') === '1'" in script
    assert "@run-at       document-start" in script
    # 0.6.5：不再把 start 挂到 DOMContentLoaded 上——晚注入（注入时该事件
    # 已过）会让监听器永不触发、脚本零反应。必须立即启动。
    assert "addEventListener('DOMContentLoaded', start" not in script
    # 0.7.0：顶层 try/catch 包裹（可见错误条），start 仍立即调用
    assert "    start();\n  } catch (error) {\n    showFatalError(error);" in script
    # 0.7.0：可见诊断（沙箱环境控制台读不到标记）+ GM API 缺失时重试
    assert "showBootBadge" in script
    assert "showFatalError" in script
    assert "missingRuntimeApis" in script
    # 0.7.1：异步回调迟到时的空指针守卫（生产报错
    # "Cannot read properties of null (reading 'insertBefore')"）
    assert "!panel.contains(viewer)" in script
    assert "mountPoint.parentElement)" in script
    # document-start 时 body/head 可能为空，需兜底
    assert "document.body || document.documentElement" in script
    assert "'.pixiv-rescue-rescued-cover{position:relative;flex:0 0 64px;width:64px;height:90px" in script


def test_userscript_reader_is_a_fixed_layer_not_page_flow() -> None:
    """0.9.0：正文/系列备份渲染进脚本自有的固定全屏阅读层（生产根因）。

    2026-10-23 真机页实测：手机站已删作品页是 SPA 404 空壳，一个
    position:absolute、不透明背景的通栏容器从 y=48 起铺满 2000+px。0.7.x 把
    面板当普通块元素追加到 body，被它整块盖住——20 万字全在 DOM 里，用户什么
    都看不到；而所有 E2E 只断言了「面板存在」。0.8.0（未发布）改成事后命中测试
    + 升级覆盖，依赖时机且会把健康正文页滚到页尾。可见性必须由 CSS 保证。
    """
    script = _script()

    root_css = script.split("'.pixiv-rescue-root{", 1)[1].split("'", 1)[0]
    assert "position:fixed" in root_css
    assert "top:0;right:0;bottom:0;left:0" in root_css
    assert "z-index:2147483600" in root_css
    # 正文可达 45 万像素高：阅读层自带滚动，且不把滚动链到背后的 Pixiv 页面
    assert "overflow-y:auto" in root_css
    assert "overscroll-behavior:contain" in root_css
    # 能退回 Pixiv 原页面，且收起后还能再打开（不是一次性弹窗）
    assert "'回到 Pixiv 页面'" in script
    assert "'查看私人备份'" in script
    assert "pixiv-rescue-root--minimized" in script
    # 挂载与保活：body 未就绪时等它出现；SPA 摘掉节点就挂回去；切页销毁
    assert "function mountReaderRoot(node)" in script
    assert "keepReaderAttached();" in script
    assert "destroyReader();" in script
    # 请求往返期间有可见占位；超时等错误给「重试」
    assert "正在读取备份" in script
    assert "renderError(error, show)" in script
    # 提示条也是固定定位（插进页面流同样会被 404 空壳盖住）
    notice_css = script.split("'.pixiv-rescue-notice-bar{", 1)[1].split("'", 1)[0]
    assert "position:fixed" in notice_css
    # 0.7.2 临时诊断与 0.8.0 事后检测机制都已移除
    for removed in ("showStep", "__pixivRescueDebug", "elementsFromPoint",
                    "ensureRootVisible", "pixiv-rescue-root--overlay", "getRoot("):
        assert removed not in script, removed
    # 正文接口放宽超时（20 万字 JSON 约 216 KB）
    assert "FULL_TEXT_TIMEOUT_MS = 30000" in script


def test_userscript_bounds_cover_cache_and_list_scan() -> None:
    """0.9.1：封面缓存有上限；列表页 3 秒扫描离开列表页后不再干活。

    封面 dataURL 单张几十 KB，旧版只在读取时判过期、从不删除；脚本没有
    GM_listValues，旧键永远留在存储里，直到管理器配额写满后 GM_setValue
    静默失败。列表页 interval 注册后永不停止，SPA 跳出列表页后仍每 3 秒
    全页扫描一次。
    """
    script = _script()

    # 一个 GM 键记录最近使用的封面键，上限约 150，淘汰/过期用 '' 覆盖清掉
    assert "const COVER_CACHE_INDEX_KEY = 'pixivRescueCoverLru';" in script
    assert "const COVER_CACHE_MAX_ENTRIES = 150;" in script
    assert "if (kept.length >= COVER_CACHE_MAX_ENTRIES || expired)" in script
    assert "GM_setValue(entry.key, '');" in script
    # 读到过期/损坏的封面也立即清掉（旧版只返回 null，值留在存储里）：
    # at 传 0 即视为过期，随这一轮淘汰一起清成 ''
    read_body = script.split("function readCoverCache(", 1)[1].split("\n  }\n", 1)[0]
    assert "updateCoverIndex(dataKey, 0);" in read_body
    assert "updateCoverIndex(dataKey, cached.at);" in read_body
    # 先记账再写值：存储里不出现索引不知道的封面键
    write_body = script.split("function writeCoverCache(", 1)[1].split("\n  }\n", 1)[0]
    assert "if (!updateCoverIndex(dataKey, now)) return;" in write_body
    assert write_body.index("updateCoverIndex(") < write_body.index("GM_setValue(")
    # 不扩权限（@grant 集合由 test_rescue_userscript_install.py 锁定）：
    # 也不调用未授权的 GM_listValues / GM_deleteValue
    assert "GM_listValues(" not in script
    assert "GM_deleteValue(" not in script

    # 手机端以右下角「救援 <SCRIPT_VERSION>」角标认版本，必须与 @version 一致
    header_version = re.search(r"^// @version\s+(\S+)$", script, re.MULTILINE)
    const_version = re.search(r"const SCRIPT_VERSION = '([^']+)';", script)
    assert header_version and const_version
    assert header_version.group(1) == const_version.group(1)
    assert tuple(map(int, const_version.group(1).split("."))) >= (0, 9, 1)

    # interval 只注册一次，但每次 tick 先确认还在列表页
    tick = script.split("__pixivRescueListInterval = window.setInterval(", 1)[1].split("}, 3000);", 1)[0]
    assert tick.startswith("function () {")
    assert "if (!isBookmarksListPage() && !isWatchlistPage()) return;" in tick
    assert "markBrokenCards();" in tick
    assert "setInterval(markBrokenCards, 3000)" not in script


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


PIXIV_AJAX_BODIES = {
    # 2026-10-23 生产实测的 Pixiv ajax 应答形状
    "available": (200, '{"error":false,"message":"","body":{"id":"101","title":"健康作品"}}'),
    "unavailable": (404, '{"error":true,"message":"","body":[]}'),
    # 状态不明：限流/网关错误等，应答甚至不是 JSON
    "unknown": (500, "<html>upstream error</html>"),
}


def _install_script(
    page,
    html: str,
    responses: dict[str, dict],
    path: str = "/novel/show.php?id=101",
    ajax_state: str = "available",
    at_document_start: bool = False,
) -> list[str]:
    """装载脚本并返回 Pixiv ajax 请求记录（正文/系列页判定作品存活用）。

    默认在页面加载完后注入（模拟晚注入的管理器）；at_document_start=True 时
    走 init script，在 body 解析出来之前就运行（模拟 Tampermonkey document-start）。
    """
    script = _script()
    ajax_hits: list[str] = []
    if at_document_start:
        page.add_init_script(
            "window.__rescueResponses = " + json.dumps(responses, ensure_ascii=False) + ";"
        )
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
    if at_document_start:
        page.add_init_script(script)

    def fulfill(route) -> None:
        if "/ajax/" in route.request.url:
            ajax_hits.append(route.request.url)
            status, body = PIXIV_AJAX_BODIES[ajax_state]
            route.fulfill(status=status, headers={"Content-Type": "application/json"}, body=body)
            return
        route.fulfill(status=200, headers={"Content-Type": "text/html; charset=utf-8"}, body=html)

    page.route("http://pixiv.test/**", fulfill)
    page.goto("http://pixiv.test" + path)
    if not at_document_start:
        page.evaluate("responses => { window.__rescueResponses = responses; }", responses)
        page.add_script_tag(content=script)
    return ajax_hits


def _reader_state(page) -> dict:
    """阅读层是否真的「用户看得见」：在面板位置做命中测试，而不是数 DOM 节点。"""
    return page.evaluate(
        """() => {
          const root = document.querySelector('[data-pixiv-rescue="content"]');
          if (!root) return { present: false };
          const target = root.querySelector('.pixiv-rescue-panel, .pixiv-rescue-error') || root;
          const rect = target.getBoundingClientRect();
          const x = Math.max(1, Math.min(innerWidth - 2, Math.max(rect.left, 0) + 20));
          const y = Math.max(1, Math.min(innerHeight - 2, Math.max(rect.top, 0) + 20));
          const top = (document.elementsFromPoint(x, y) || [])[0];
          return {
            present: true,
            mode: root.getAttribute('data-pixiv-rescue-mode'),
            position: getComputedStyle(root).position,
            hit: Boolean(top) && (top === root || root.contains(top)),
          };
        }"""
    )


def test_userscript_fixture_keeps_healthy_novel_untouched(rescue_browser) -> None:
    page = rescue_browser.new_page()
    html = "<main><article class='novel-text'>这是原始正文内容，不应触发救援接口。</article></main>"
    ajax_hits = _install_script(page, html, {}, ajax_state="available")

    page.wait_for_timeout(300)
    assert ajax_hits == ["http://pixiv.test/ajax/novel/101"]
    assert page.evaluate("window.__rescueRequests") == []
    assert page.locator("[data-pixiv-rescue]").count() == 0
    assert page.locator(".novel-text").inner_text() == "这是原始正文内容，不应触发救援接口。"
    page.close()


def test_userscript_fixture_leaves_healthy_touch_page_without_main_untouched(rescue_browser) -> None:
    """回归 2026-10-23：手机站健康正文页没有 <main>（Vue 挂在 #spa-contents）。

    0.7.2 把「无 main」当成已删空壳：每个健康正文页都请求备份、在页尾塞一条
    「未找到可用的救援数据」；未发布的 0.8.0 还会把页面滚到页尾。
    """
    page = rescue_browser.new_page()
    html = (
        "<div id='spa-contents'><div class='novel-content'>"
        + "健康正文。" * 400
        + "</div></div>"
    )
    ajax_hits = _install_script(page, html, {}, ajax_state="available", at_document_start=True)

    page.wait_for_timeout(1500)
    assert ajax_hits == ["http://pixiv.test/ajax/novel/101"]
    assert page.evaluate("window.__rescueRequests") == []
    assert page.locator("[data-pixiv-rescue]").count() == 0
    assert page.locator("[data-pixiv-rescue-notice]").count() == 0
    assert page.evaluate("window.scrollY") == 0
    page.close()


def test_userscript_fixture_stays_silent_when_pixiv_state_unknown_and_no_backup(rescue_browser) -> None:
    """Pixiv ajax 状态不明（限流/网关错误）时：查得到备份才出现，查不到不打扰。"""
    page = rescue_browser.new_page()
    html = "<main><article class='novel-text'>也许好好的正文</article></main>"
    _install_script(page, html, {}, ajax_state="unknown")

    page.wait_for_function("() => window.__rescueRequests.length === 1")
    page.wait_for_timeout(200)
    assert page.locator("[data-pixiv-rescue]").count() == 0
    assert page.locator("[data-pixiv-rescue-notice]").count() == 0
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
    # 无救援标记：靠 Pixiv ajax 404 判定已失效，再查备份
    _install_script(page, html, responses, ajax_state="unavailable")

    page.locator(".pixiv-rescue-body").wait_for(state="visible")
    assert page.locator("[data-pixiv-rescue]").count() == 1
    assert "拯救数据" in page.locator("[data-pixiv-rescue]").inner_text()
    assert "第一行" in page.locator("[data-pixiv-rescue]").inner_text()
    assert "<script>alert('xss')</script>" in page.locator("[data-pixiv-rescue]").inner_text()
    assert page.locator("[data-pixiv-rescue] script").count() == 0
    assert page.locator(".error").count() == 1
    state = _reader_state(page)
    assert state["hit"] is True and state["mode"] == "reader" and state["position"] == "fixed"
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
    _install_script(page, html, responses, path="/novel/series/202", ajax_state="unavailable")

    page.get_by_role("button", name="加载目录").click()
    page.get_by_role("button", name="第二章").click()
    page.get_by_text("第二章正文").wait_for(state="visible")
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
    _install_script(page, html, responses, path="/novel/series/204", ajax_state="unavailable")

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
    """Pixiv 确认已删、备份里也没有：不开阅读层，只给一条固定定位的提示。"""
    page = rescue_browser.new_page()
    html = "<main><div class='error'>この作品は削除されています</div></main>"
    _install_script(page, html, {}, path="/novel/show.php?id=999", ajax_state="unavailable")

    notice = page.locator("[data-pixiv-rescue-notice]")
    notice.wait_for(state="visible")
    assert "私人备份里也没有收录" in notice.inner_text()
    assert page.locator("[data-pixiv-rescue]").count() == 0
    assert page.locator(".error").count() == 1
    assert "この作品は削除されています" in page.locator(".error").inner_text()
    page.close()


def test_userscript_fixture_series_load_more_failure_keeps_directory(rescue_browser) -> None:
    """「加载更多章节」失败只在面板里提示，不再把整个系列面板换成错误条。"""
    page = rescue_browser.new_page()
    responses = {
        "https://pixiv.dongboapp.com/api/rescue/v1/series/205": {
            "series_id": 205, "title": "长系列", "rescue_state": "partial",
            "source_notice": "内容来自私人备份，并非 Pixiv 官方恢复",
        },
        "https://pixiv.dongboapp.com/api/rescue/v1/series/205/chapters": {
            "items": [{"novel_id": 501, "title": "第一章"}], "page": 1, "total_pages": 2,
        },
    }
    _install_script(page, "<div id='spa-contents'></div>", responses,
                    path="/novel/series/205?pixiv_rescue=1")

    page.get_by_role("button", name="加载目录").click()
    page.get_by_role("button", name="加载更多章节").click()
    page.locator(".pixiv-rescue-directory-error").wait_for(state="visible")
    assert page.get_by_role("button", name="第一章").is_visible()
    assert page.get_by_text("长系列").is_visible()
    page.close()


# 2026-10-23 真机页实测的手机站 404 空壳（已删作品页）：没有 <main>；Vue 挂在
# #spa-contents，里面一个 position:absolute、不透明背景（rgb(245,245,245)）的
# 通栏容器从 y=48 起铺 2000+px，「尚无此页」「回到主页」和推荐流都画在它里面。
# 0.7.x 追加到 body 的静态面板被它整块盖住：DOM 里文字齐全，用户什么都看不到。
PIXIV_404_SHELL = (
    "<div id='container' style='position:relative'>"
    "<div id='spa-contents' style='height:48px'>pixiv 头部"
    "<div><div style='position:absolute;top:48px;left:0;width:100%;height:2132px;"
    "background:rgb(245,245,245)'>"
    "<div style='padding:116px 16px 64px'>尚无此页<button>回到主页</button></div>"
    "</div></div></div></div>"
)


def test_userscript_fixture_reader_beats_pixiv_404_shell(rescue_browser) -> None:
    """回归 2026-10-23 生产根因：404 空壳的绝对定位不透明背景盖住了 0.7.x 的面板。

    断言是命中测试（用户真能看见），不是 locator 计数（DOM 存在）——旧 E2E 只断言
    `panel: true`，面板被盖住了却一路绿灯。走 document-start 注入：body 还没解析
    时脚本已经在跑，阅读层要等 body 出现再挂载。
    """
    page = rescue_browser.new_page()
    responses = {
        "https://pixiv.dongboapp.com/api/rescue/v1/novels/101": {
            "title": "备份小说",
            "text_raw": "被 404 空壳挡住的正文",
            "source_notice": "内容来自私人备份，并非 Pixiv 官方恢复",
            "rescue_state": "success",
        }
    }
    ajax_hits = _install_script(page, PIXIV_404_SHELL, responses,
                                path="/novel/show.php?id=101&pixiv_rescue=1", at_document_start=True)

    page.get_by_text("被 404 空壳挡住的正文").wait_for(state="visible")
    state = _reader_state(page)
    assert state["hit"] is True, "阅读层被 Pixiv 404 背景层遮挡，用户看不见"
    assert state["mode"] == "reader" and state["position"] == "fixed"
    # 带救援标记：用户意图明确，不再多问一次 Pixiv
    assert ajax_hits == []
    page.close()


def test_userscript_fixture_reader_minimizes_and_reopens(rescue_browser) -> None:
    """层叠极高的遮挡层也盖不住；「回到 Pixiv 页面」收起为胶囊，可再打开。"""
    page = rescue_browser.new_page()
    responses = {
        "https://pixiv.dongboapp.com/api/rescue/v1/novels/101": {
            "title": "备份小说",
            "text_raw": "压在最上层的正文",
            "source_notice": "内容来自私人备份，并非 Pixiv 官方恢复",
            "rescue_state": "success",
        }
    }
    harder_shell = (
        "<div style='position:fixed;top:0;left:0;right:0;bottom:0;z-index:2147483000;"
        "background:rgb(245,245,245)'>更高层叠的遮挡层</div>"
    )
    _install_script(page, harder_shell, responses, path="/novel/show.php?id=101&pixiv_rescue=1")

    page.get_by_text("压在最上层的正文").wait_for(state="visible")
    assert _reader_state(page)["hit"] is True
    page.get_by_role("button", name="回到 Pixiv 页面").click()
    page.get_by_role("button", name="查看私人备份").wait_for(state="visible")
    assert page.get_by_text("压在最上层的正文").is_hidden()
    assert _reader_state(page)["mode"] == "minimized"
    page.get_by_role("button", name="查看私人备份").click()
    page.get_by_text("压在最上层的正文").wait_for(state="visible")
    assert _reader_state(page)["mode"] == "reader"
    page.close()


def test_userscript_fixture_rescue_intent_survives_failure_with_retry(rescue_browser) -> None:
    """带标记进入但备份请求失败：阅读层里给错误与「重试」，不是死路。"""
    page = rescue_browser.new_page()
    _install_script(page, PIXIV_404_SHELL, {}, path="/novel/show.php?id=101&pixiv_rescue=1")

    page.locator(".pixiv-rescue-error").wait_for(state="visible")
    assert _reader_state(page)["hit"] is True
    page.evaluate(
        """() => { window.__rescueResponses['https://pixiv.dongboapp.com/api/rescue/v1/novels/101'] =
             { title: '重试后的备份', text_raw: '重试成功的正文', rescue_state: 'success' }; }"""
    )
    page.get_by_role("button", name="重试").click()
    page.get_by_text("重试成功的正文").wait_for(state="visible")
    page.close()


# ── 列表页（收藏 / 追更）────────────────────────────────────────────

LIST_TOKEN = "rsq_test"
LIST_PATH = "/users/1/bookmarks/novels"
LOOKUP_URL = "https://pixiv.dongboapp.com/api/rescue/v1/lookup?novel_ids="
# 1×1 透明 PNG：封面接口的 blob 应答
TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _broken_card_html(novel_id: int) -> str:
    """2026-10-20 手机端实测的失效卡片：div.works-item 内只剩一句失效文案。"""
    return (
        f"<li><div class='works-item'><a href='/novel/show.php?id={novel_id}'>"
        "<div><div><div>该作品无法显示</div></div></div></a></div></li>"
    )


def _cover_key_suffix(token: str) -> str:
    """复刻脚本 cacheKeySuffix 的滚动哈希（h * 31 + charCode，32 位截断）。"""
    h = 0
    for char in token:
        h = (h * 31 + ord(char)) & 0xFFFFFFFF
    return str(h)


def _install_list_script(page, html: str, responses: dict[str, dict], store: dict[str, str]) -> None:
    """列表页装载：document-start 注入，GM 存储是可读写的真实 Map，封面走 blob。"""
    page.add_init_script(
        "window.__rescueResponses = " + json.dumps(responses, ensure_ascii=False) + ";"
        "window.__rescueStore = new Map(Object.entries(" + json.dumps(store) + "));"
        "window.__rescuePng = " + json.dumps(TINY_PNG_BASE64) + ";"
    )
    page.add_init_script(
        """
        window.__rescueRequests = [];
        window.GM_getValue = (key, fallback) => window.__rescueStore.has(key) ? window.__rescueStore.get(key) : fallback;
        window.GM_setValue = (key, value) => { window.__rescueStore.set(key, value); };
        window.GM_registerMenuCommand = () => {};
        window.GM_xmlhttpRequest = (options) => {
          window.__rescueRequests.push({url: options.url, method: options.method});
          setTimeout(() => {
            if (options.responseType === 'blob') {
              const bytes = Uint8Array.from(atob(window.__rescuePng), c => c.charCodeAt(0));
              options.onload({status: 200, response: new Blob([bytes], {type: 'image/png'})});
              return;
            }
            const payload = window.__rescueResponses[options.url];
            if (!payload) {
              options.onload({status: 404, responseText: JSON.stringify({ok: false, error: 'missing fixture'})});
              return;
            }
            options.onload({status: 200, responseText: JSON.stringify({ok: true, data: payload})});
          }, 0);
        };
        """
    )
    page.add_init_script(_script())
    page.route("http://pixiv.test/**", lambda route: route.fulfill(
        status=200, headers={"Content-Type": "text/html; charset=utf-8"}, body=html))
    page.goto("http://pixiv.test" + LIST_PATH)


def test_userscript_fixture_cover_cache_is_bounded_and_clears_evicted(rescue_browser) -> None:
    """回归：封面缓存写满后 GM_setValue 静默失败（旧版从不删除任何封面键）。

    预置一份满额的最近使用列表（其中一条已过期），再让一张失效卡的封面过期
    重取：新封面排到最前，列表仍是上限条数，被挤出与过期的键都被清成 ''。
    """
    page = rescue_browser.new_page()
    now_ms = int(page.evaluate("Date.now()"))
    prefix = "pixivRescueCover:" + _cover_key_suffix(LIST_TOKEN) + ":novel:"
    real_key = prefix + "101"
    fresh_at = now_ms - 10 * 60 * 1000
    expired_at = now_ms - 25 * 60 * 60 * 1000
    seed_keys = [prefix + str(900000 + i) for i in range(151)]
    expired_key = seed_keys[75]
    index = [{"key": key, "at": expired_at if key == expired_key else fresh_at} for key in seed_keys]
    store = {
        "pixivRescueToken": LIST_TOKEN,
        "pixivRescueCoverLru": json.dumps(index),
        # 旧版留下、未进索引的过期封面：读到即清掉并重取
        real_key: json.dumps({"at": expired_at, "dataUrl": "data:image/png;base64,stale"}),
    }
    for entry in index:
        store[entry["key"]] = json.dumps({"at": entry["at"], "dataUrl": "data:image/png;base64,seed"})
    responses = {
        LOOKUP_URL + "101": {
            "novels": [{"novel_id": 101, "title": "备份小说", "author_name": "作者", "has_cover": True,
                        "personal_hold": True}],
            "series": [],
        }
    }
    _install_list_script(page, "<ul id='list'>" + _broken_card_html(101) + "</ul>", responses, store)

    page.wait_for_function(
        """([key, staleAt]) => {
          const raw = window.__rescueStore.get(key);
          if (!raw) return false;
          const value = JSON.parse(raw);
          return value.at > staleAt && value.dataUrl.startsWith('data:image/png;base64,');
        }""",
        arg=[real_key, expired_at],
    )
    result = page.evaluate(
        """() => ({
          index: JSON.parse(window.__rescueStore.get('pixivRescueCoverLru')),
          store: Object.fromEntries(window.__rescueStore),
          urls: window.__rescueRequests.map(item => item.url),
        })"""
    )
    keys = [entry["key"] for entry in result["index"]]
    assert keys[0] == real_key
    assert len(keys) == 150 and len(set(keys)) == 150
    # 过期封面没有被当成命中：确实重新拉了封面
    assert "https://pixiv.dongboapp.com/api/rescue/v1/novels/101/cover" in result["urls"]
    # 索引里的键都有值；不在索引里的封面键全部清成 ''（没有无主的 dataURL）
    cover_keys = [key for key in result["store"] if key.startswith("pixivRescueCover:")]
    for key in cover_keys:
        assert bool(result["store"][key]) == (key in keys), key
    assert expired_key not in keys and result["store"][expired_key] == ""
    # 挤出的是最久未用的尾部，最近使用的头部保留
    assert seed_keys[-1] not in keys and result["store"][seed_keys[-1]] == ""
    assert seed_keys[0] in keys
    page.close()


def test_userscript_fixture_list_interval_idles_off_list_page(rescue_browser) -> None:
    """回归：列表页 3 秒 interval 永不停止，SPA 跳出列表页后仍在全页扫描。"""
    page = rescue_browser.new_page()
    responses = {
        LOOKUP_URL + "777001": {
            "novels": [{"novel_id": 777001, "title": "列表页备份", "author_name": "作者",
                        "has_cover": False, "personal_hold": True}],
            "series": [],
        }
    }
    _install_list_script(page, "<ul id='list'></ul>", responses, {"pixivRescueToken": LIST_TOKEN})
    insert_card = "html => document.getElementById('list').insertAdjacentHTML('beforeend', html)"
    lookups = "() => window.__rescueRequests.filter(item => item.url.includes('/lookup?')).length"

    # 列表页上：新插入的失效卡会被扫到并重建
    page.evaluate(insert_card, _broken_card_html(777001))
    page.locator(".pixiv-rescue-rescued-card").wait_for(state="visible")
    before = page.evaluate(lookups)

    # SPA 跳出列表页：之后出现的失效卡不应再被扫描（至少等过一个 3 秒 tick）
    page.evaluate("() => history.pushState({}, '', '/discovery')")
    page.evaluate(insert_card, _broken_card_html(777002))
    page.wait_for_timeout(3600)
    assert page.evaluate(lookups) == before

    # 回到列表页：扫描恢复，跳出期间插入的卡片被补扫
    page.evaluate("path => history.pushState({}, '', path)", LIST_PATH)
    page.wait_for_function(
        "() => window.__rescueRequests.some(item => item.url.endsWith('/lookup?novel_ids=777002'))"
    )
    page.close()

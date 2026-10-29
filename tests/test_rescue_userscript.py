from __future__ import annotations

import base64
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
    # 作品还在：不请求备份、不动页面（只读地记一下 Aa 阅读设置后就返回）
    available_branch = script.split("if (state === 'available') {", 1)[1].split("return;", 1)[0]
    assert "loadBackup" not in available_branch and "renderBackup" not in available_branch
    assert "learnReaderTheme(isStale)" in available_branch
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
    # 0.12.0：「拯救系列」不再是错放在 bookmark_new.php 页头上方的链接，而是接管手机站
    # 新作页的第三个 tab（好P友新作），复用救援系列列表接口按页渲染
    for removed in ("isNovelBookmarksPage", "injectRescueSeriesTab", "renderRescueSeriesTabBody",
                    "handleNovelBookmarksPage", "pixiv-rescue-tab-"):
        assert removed not in script, removed
    assert "const RESCUE_FEED_PATH = '/novel/mypixiv_new.php';" in script
    assert "const RESCUE_FEED_LABEL = '拯救系列';" in script
    assert "'/api/rescue/v1/series?page='" in script
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
    # 0.12.0：拯救系列 tab 翻页时旧列表留在原处（与 Pixiv 一样），只把翻页条换成转圈；
    # 迟到的应答按代次丢弃，不会画进别的页
    loading_body = script.split("function showRescueFeedLoading(", 1)[1].split("\n  }\n", 1)[0]
    assert "clearNode(" not in loading_body
    assert "generation !== rescueFeedGeneration || rescueFeed !== state" in script
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
    # "Cannot read properties of null (reading 'insertBefore')"）；0.11.0 起目录
    # 「加载更多」的回调在目录已离开面板时放弃本轮
    assert "!panel.contains(list)" in script
    # 0.12.0：追更列表填空的回调在切页/列表被换掉后放弃本轮
    assert "pageParamFromUrl() !== page || !list.isConnected" in script
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
    # 管理器只在 @version 变大时自动更新：不能低于已发布的 0.12.2（合并时误回退会被拦下）
    assert tuple(map(int, const_version.group(1).split("."))) >= (0, 12, 2)
    # 救援指南的版本说明要写到当前版本（手机上看到角标的版本号，指南里查得到改了什么）
    guide = (SCRIPT_PATH.parents[1] / "docs" / "RESCUE_USER_GUIDE.md").read_text(encoding="utf-8")
    assert re.search(r"^" + re.escape(const_version.group(1)) + r" ", guide, re.MULTILINE), const_version.group(1)

    # interval 只注册一次，但每次 tick 先确认还在列表页；一轮扫描 = 失效卡重建 + 追更列表填空
    tick = script.split("__pixivRescueListInterval = window.setInterval(", 1)[1].split("}, 3000);", 1)[0]
    assert tick.startswith("function () {")
    assert "if (!isBookmarksListPage() && !isWatchlistPage()) return;" in tick
    assert "scanListPage();" in tick
    scan = script.split("function scanListPage() {", 1)[1].split("\n  }\n", 1)[0]
    assert "markBrokenCards();" in scan and "fillWatchListBlanks();" in scan
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
    store: dict[str, str] | None = None,
    before_inject=None,
) -> list[str]:
    """装载脚本并返回 Pixiv ajax 请求记录（正文/系列页判定作品存活用）。

    默认在页面加载完后注入（模拟晚注入的管理器）；at_document_start=True 时
    走 init script，在 body 解析出来之前就运行（模拟 Tampermonkey document-start）。
    before_inject(page)：晚注入前一刻调用（例如先滚动页面、挂上 DOM 变动记录）。
    """
    script = _script()
    ajax_hits: list[str] = []
    if store is not None:
        page.add_init_script(
            "window.__rescueStore = new Map(Object.entries(" + json.dumps(store, ensure_ascii=False) + "));"
        )
    if at_document_start:
        page.add_init_script(
            "window.__rescueResponses = " + json.dumps(responses, ensure_ascii=False) + ";"
        )
    page.add_init_script(
        """
        window.__rescueRequests = [];
        // store 夹具：可读写的 GM 存储（Aa 阅读设置的学习与套用）；默认只读出 Token
        window.GM_getValue = (key, fallback) => (window.__rescueStore && window.__rescueStore.has(key))
          ? window.__rescueStore.get(key) : (key === 'pixivRescueToken' ? 'rsq_test' : fallback);
        window.GM_setValue = (key, value) => { if (window.__rescueStore) window.__rescueStore.set(key, value); };
        window.GM_registerMenuCommand = () => {};
        window.GM_xmlhttpRequest = (options) => {
          window.__rescueRequests.push({url: options.url, method: options.method});
          const payload = window.__rescueResponses[options.url];
          setTimeout(() => {
            if (!payload) {
              options.onload({status: 404, responseText: JSON.stringify({ok: false, error: 'missing fixture'})});
              return;
            }
            // {__raw: {status, body}}：原样回状态码与正文（网关的 HTML 错误页、被截断的 JSON）
            if (payload.__raw) {
              options.onload({status: payload.__raw.status, responseText: payload.__raw.body});
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
        if before_inject is not None:
            before_inject(page)
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


def test_userscript_fixture_series_page_lists_chapters_without_fetching_text(rescue_browser) -> None:
    """系列页直接列出章节卡片（不再先点「加载目录」），且不预取任何一章正文。"""
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
                {"novel_id": 301, "title": "第一章", "chapter_number": 1},
                {"novel_id": 302, "title": "第二章", "chapter_number": 2},
            ],
            "total": 2,
        },
    }
    _install_script(page, html, responses, path="/novel/series/202", ajax_state="unavailable")

    cards = page.locator("a.pixiv-rescue-chapter-card")
    cards.first.wait_for(state="visible")
    assert cards.count() == 2
    assert cards.nth(1).get_attribute("href") == "/novel/show.php?id=302&pixiv_rescue=1"
    assert page.locator(".pixiv-rescue-chapter-title").all_inner_texts() == ["#1 第一章", "#2 第二章"]
    urls = page.evaluate("window.__rescueRequests.map(item => item.url)")
    assert not any("/api/rescue/v1/novels/" in url for url in urls)
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

    page.get_by_role("button", name="加载更多章节").click()
    page.locator(".pixiv-rescue-chapter-title", has_text="第一百零一章").wait_for(state="visible")
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

    page.get_by_role("button", name="加载更多章节").click()
    page.locator(".pixiv-rescue-directory-error").wait_for(state="visible")
    assert page.locator(".pixiv-rescue-chapter-title", has_text="第一章").is_visible()
    assert page.locator(".pixiv-rescue-series-head .pixiv-rescue-title").inner_text() == "长系列"
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


def _install_list_script(
    page,
    html: str,
    responses: dict[str, dict],
    store: dict[str, str],
    path: str = LIST_PATH,
    pixiv_json: dict[str, dict] | None = None,
    cover_status: dict[str, list[int]] | None = None,
    hold: list[str] | None = None,
) -> list[str]:
    """列表页装载：document-start 注入，GM 存储是可读写的真实 Map，封面走 blob。

    pixiv_json：Pixiv 自己的同源 ajax（URL 片段 → JSON 应答），例如追更列表的
    /touch/ajax/watch_list/novel；返回这些请求的记录。作者头像（i.pximg.net）回一张小图。
    cover_status：封面 URL → 依次回的状态码（用完后回 200），例如先 429 再恢复。
    hold：URL 里含这些片段的救援请求先压着不回，等页面里调 window.__rescueRelease(片段)
    才应答（同步交给脚本）；应答过的 URL 记在 window.__rescueDelivered。
    GM_getValue 按键计数读取次数（window.__rescueReads）。
    """
    pixiv_hits: list[str] = []
    page.add_init_script(
        "window.__rescueResponses = " + json.dumps(responses, ensure_ascii=False) + ";"
        "window.__rescueStore = new Map(Object.entries(" + json.dumps(store) + "));"
        "window.__rescuePng = " + json.dumps(TINY_PNG_BASE64) + ";"
        "window.__rescueCoverStatus = " + json.dumps(cover_status or {}) + ";"
        "window.__rescueHold = " + json.dumps(hold or []) + ";"
    )
    page.add_init_script(
        """
        window.__rescueRequests = [];
        window.__rescueReads = {};
        window.__rescueHeld = [];
        window.__rescueDelivered = [];
        window.__rescueRelease = (fragment) => {
          window.__rescueHeld.filter((item) => item.url.includes(fragment)).forEach((item) => item.respond());
          window.__rescueHeld = window.__rescueHeld.filter((item) => !item.url.includes(fragment));
        };
        window.GM_getValue = (key, fallback) => {
          window.__rescueReads[key] = (window.__rescueReads[key] || 0) + 1;
          return window.__rescueStore.has(key) ? window.__rescueStore.get(key) : fallback;
        };
        window.GM_setValue = (key, value) => { window.__rescueStore.set(key, value); };
        window.GM_registerMenuCommand = () => {};
        window.GM_xmlhttpRequest = (options) => {
          window.__rescueRequests.push({url: options.url, method: options.method});
          const later = (respond) => {
            if (window.__rescueHold.some((fragment) => options.url.includes(fragment))) {
              window.__rescueHeld.push({ url: options.url, respond: () => {
                respond();
                window.__rescueDelivered.push(options.url);
              } });
              return;
            }
            setTimeout(respond, 0);
          };
          later(() => {
            if (options.responseType === 'blob') {
              const queue = window.__rescueCoverStatus[options.url];
              const status = queue && queue.length ? queue.shift() : 200;
              if (status !== 200) {
                options.onload({status: status, response: null});
                return;
              }
              const bytes = Uint8Array.from(atob(window.__rescuePng), c => c.charCodeAt(0));
              options.onload({status: 200, response: new Blob([bytes], {type: 'image/png'})});
              return;
            }
            const payload = window.__rescueResponses[options.url];
            if (!payload) {
              options.onload({status: 404, responseText: JSON.stringify({ok: false, error: 'missing fixture'})});
              return;
            }
            if (payload.__raw) {
              options.onload({status: payload.__raw.status, responseText: payload.__raw.body});
              return;
            }
            options.onload({status: 200, responseText: JSON.stringify({ok: true, data: payload})});
          });
        };
        """
    )
    page.add_init_script(_script())

    def fulfill(route) -> None:
        url = route.request.url
        for fragment, body in (pixiv_json or {}).items():
            if fragment in url:
                pixiv_hits.append(url)
                route.fulfill(status=200, headers={"Content-Type": "application/json"},
                              body=json.dumps(body, ensure_ascii=False))
                return
        route.fulfill(status=200, headers={"Content-Type": "text/html; charset=utf-8"}, body=html)

    page.route("http://pixiv.test/**", fulfill)
    page.route("https://i.pximg.net/**", lambda route: route.fulfill(
        status=200, headers={"Content-Type": "image/png"}, body=base64.b64decode(TINY_PNG_BASE64)))
    page.goto("http://pixiv.test" + path)
    return pixiv_hits


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


def _css_rule(script: str, selector: str) -> str:
    """取 addStyles 里以 selector 开头的那条 CSS（整行，含字体常量拼接）。"""
    return script.split("'" + selector + "{", 1)[1].split("\n", 1)[0]


def test_userscript_reader_typography_matches_pixiv_mobile() -> None:
    """0.10.0：阅读层正文逐项照搬 2026-10-23 实测的 Pixiv 手机站排版。

    实测值（#novel-text-container，默认 Aa 设置 background-0/size-1/spacing-1）：
    16px、行高 1.8（28.8px）、#1f1f1f、白底、上下内边距 40px、pre-wrap；
    p.novel-paragraph 底距 1.5em、左右 16px；h2.novel-chapter 20px/700/#969696、
    上下 1em、左右 26px；翻页条行高 45px、#474747、上下 1px rgba(0,0,0,.08)。
    """
    script = _script()
    font = script.split("const PIXIV_GOTHIC_FONT = '", 1)[1].split("';", 1)[0]
    assert font.startswith('"Avenir Next",Avenir,"Source Sans","Noto Sans",Roboto,Verdana,"Pingfang SC"')
    assert font.endswith('"Malgun Gothic",sans-serif')

    text = _css_rule(script, ".pixiv-rescue-novel-text")
    for value in ("padding:40px 0", "background:#fff", "color:#1f1f1f", "font-family:' + PIXIV_GOTHIC_FONT + '",
                  "font-size:16px", "line-height:1.8", "white-space:pre-wrap", "overflow-wrap:break-word"):
        assert value in text, value
    para = _css_rule(script, ".pixiv-rescue-novel-text .pixiv-rescue-para")
    assert "margin:0 0 1.5em" in para and "padding:0 16px" in para
    chapter = _css_rule(script, ".pixiv-rescue-novel-text .pixiv-rescue-chapter")
    for value in ("margin:1em 0", "padding:0 26px", "font-size:20px", "font-weight:700", "color:#969696"):
        assert value in chapter, value
    pager = _css_rule(script, ".pixiv-rescue-pager")
    for value in ("line-height:45px", "color:#474747", "border-top:1px solid rgba(0,0,0,.08)"):
        assert value in pager, value
    # Aa 的五种背景（同样实测自手机站 CSS）
    for selector, value in (
        (".pixiv-rescue-novel-text.background-1", "background:#1f1f1f;color:#b7b7b7"),
        (".pixiv-rescue-novel-text.background-2", "background:#edf7ff;color:#4d3e2a"),
        (".pixiv-rescue-novel-text.background-3", "background:#fbf0d9;color:#4d3e2a"),
        (".pixiv-rescue-novel-text.background-4", "background:#f5f5f5;color:#333"),
    ):
        assert value in _css_rule(script, selector), selector
    # 旧版的衬线字体 + 行高 2 的自定义排版已删除
    assert '"Noto Serif SC"' not in script
    # 简介按纯文本处理，正文链接只放行 http/https
    assert "new DOMParser().parseFromString(" in script
    assert "/^https?:\\/\\//i.test(pair[1])" in script


MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Mobile Safari/537.36"
)
MARKUP_TEXT = (
    "[chapter:第一章]\n第一段第一行\n第一段第二行\n\n"
    "[[rb:漢字 > かんじ]]与[b:[i:粗斜体]]\n\n"
    "[[jumpuri:安全链接 > https://example.com/a]] 和 [[jumpuri:坏链接 > javascript:alert(1)]]\n\n"
    "[[emphasismark:重点 > •]]<script>alert('xss')</script>\n"
    "[uploadedimage:123]\n[pixivimage:456]\n"
    "[newpage]\n第二页正文 [jump:1]"
)
MARKUP_NOVEL = {
    "novel_id": 101,
    "title": "排版测试",
    "author_name": "测试作者",
    "caption": "第一行简介<br />第二行简介<script>bad()</script>",
    "tags": [{"name": "R-18", "translated_name": None}, {"name": "原创", "translated_name": "original"}],
    "create_date": "2026-10-20T15:00:00",
    "text_raw": MARKUP_TEXT,
    "source_notice": "内容来自私人备份，并非 Pixiv 官方恢复",
    "rescue_state": "success",
}


def _computed(page, selector: str, props: list[str]) -> dict:
    return page.evaluate(
        "([sel, props]) => { const s = getComputedStyle(document.querySelector(sel));"
        " return Object.fromEntries(props.map((p) => [p, s[p]])); }",
        [selector, props],
    )


def test_userscript_fixture_renders_pixiv_markup_like_pixiv(rescue_browser) -> None:
    """标记逐项按 Pixiv 手机站的 DOM 规则渲染，计算样式等于实测值，且不引入 XSS。"""
    context = rescue_browser.new_context(user_agent=MOBILE_UA, viewport={"width": 412, "height": 915})
    page = context.new_page()
    responses = {"https://pixiv.dongboapp.com/api/rescue/v1/novels/101": MARKUP_NOVEL}
    _install_script(page, PIXIV_404_SHELL, responses, path="/novel/show.php?id=101&pixiv_rescue=1")

    page.locator(".pixiv-rescue-chapter").wait_for(state="visible")
    assert "pixiv-rescue-root--pc" not in (page.locator("[data-pixiv-rescue='content']").get_attribute("class") or "")
    body = page.locator(".pixiv-rescue-novel-text")
    assert body.get_attribute("lang") == "ja"  # 正文含假名
    assert page.locator(".pixiv-rescue-chapter").inner_text() == "第一章"
    assert body.locator("ruby rt").inner_text() == "かんじ"
    assert body.locator("b > i").inner_text() == "粗斜体"
    safe = body.locator("a.pixiv-rescue-jump", has_text="安全链接")
    assert safe.get_attribute("href") == "https://example.com/a"
    assert safe.get_attribute("target") == "_blank" and "noopener" in safe.get_attribute("rel")
    assert body.locator("a.pixiv-rescue-jump", has_text="坏链接").get_attribute("href") is None
    assert "dot" in _computed(page, ".pixiv-rescue-emphasis", ["textEmphasisStyle"])["textEmphasisStyle"]
    assert body.locator("script").count() == 0
    assert "<script>alert('xss')</script>" in body.inner_text()
    assert body.locator(".pixiv-rescue-image-missing").count() == 2
    assert body.locator(".pixiv-rescue-image-missing a").get_attribute("href") == "https://www.pixiv.net/artworks/456"
    # 空行分段，段内换行保留
    assert body.locator(".pixiv-rescue-para").count() == 4
    assert body.locator(".pixiv-rescue-para").first.inner_text() == "第一段第一行\n第一段第二行"
    assert _computed(page, ".pixiv-rescue-novel-text",
                     ["fontSize", "lineHeight", "color", "backgroundColor", "paddingTop", "whiteSpace"]) == {
        "fontSize": "16px", "lineHeight": "28.8px", "color": "rgb(31, 31, 31)",
        "backgroundColor": "rgb(255, 255, 255)", "paddingTop": "40px", "whiteSpace": "pre-wrap"}
    assert _computed(page, ".pixiv-rescue-para", ["marginBottom", "paddingLeft", "paddingRight"]) == {
        "marginBottom": "24px", "paddingLeft": "16px", "paddingRight": "16px"}
    assert _computed(page, ".pixiv-rescue-chapter", ["fontSize", "fontWeight", "color", "marginTop", "paddingLeft"]) == {
        "fontSize": "20px", "fontWeight": "700", "color": "rgb(150, 150, 150)", "marginTop": "20px", "paddingLeft": "26px"}
    # 作品信息区：标题、作者、标签（R-18 红字）、简介纯文本、日期
    assert page.locator(".pixiv-rescue-title").inner_text() == "排版测试"
    assert page.locator(".pixiv-rescue-author-name").inner_text() == "测试作者"
    assert page.locator(".pixiv-rescue-tag-r18").inner_text() == "R-18"
    assert "#原创" in page.locator(".pixiv-rescue-tags").inner_text()
    assert page.locator(".pixiv-rescue-caption").inner_text() == "第一行简介\n第二行简介"
    assert page.locator(".pixiv-rescue-date").inner_text().startswith("2026年10月20日下午3点00分")
    # 分页：一次一页，「下一页」翻页，[jump:1] 跳回第一页
    labels = page.locator(".pixiv-rescue-pager-label")
    assert labels.all_inner_texts() == ["1 / 2 页", "1 / 2 页"]
    page.locator(".pixiv-rescue-pager").first.get_by_role("button", name="下一页").click()
    assert labels.all_inner_texts() == ["2 / 2 页", "2 / 2 页"]
    assert "第二页正文" in body.inner_text() and "第一段第一行" not in body.inner_text()
    body.get_by_text("1ページへ").click()
    assert labels.all_inner_texts() == ["1 / 2 页", "1 / 2 页"]
    context.close()


@pytest.mark.parametrize("create_date, expected", [
    ("2026-10-20T11:30:00", "2026年10月20日中午11点30分"),
    ("2026-10-20T12:30:00", "2026年10月20日下午12点30分"),
    ("2026-10-20T05:59:00", "2026年10月20日凌晨5点59分"),
    ("2026-10-20T18:00:00", "2026年10月20日晚上6点00分"),
])
def test_userscript_fixture_date_uses_pixiv_meridiem_boundaries(rescue_browser, create_date, expected) -> None:
    """时段分界照手机站 JS 包里 moment.js zh-cn 的 meridiem（11:30 起中午、12:30 起下午）。"""
    page = rescue_browser.new_page()
    novel = dict(MARKUP_NOVEL, create_date=create_date)
    _install_script(page, PIXIV_404_SHELL, {"https://pixiv.dongboapp.com/api/rescue/v1/novels/101": novel},
                    path="/novel/show.php?id=101&pixiv_rescue=1")
    page.locator(".pixiv-rescue-date").wait_for(state="visible")
    assert page.locator(".pixiv-rescue-date").inner_text().startswith(expected)
    page.close()


def test_userscript_fixture_learns_reader_theme_read_only(rescue_browser) -> None:
    """健康正文页上只读地记下用户的 Aa 设置：不请求备份、不改页面。"""
    context = rescue_browser.new_context(user_agent=MOBILE_UA, viewport={"width": 412, "height": 915})
    page = context.new_page()
    html = (
        "<section id='novel-text-container' class='horizontal background-3 size-2 spacing-0'"
        " style='font-family: serif;'><p class='novel-paragraph horizontal'>健康正文</p></section>"
    )
    _install_script(page, html, {}, ajax_state="available", store={})

    page.wait_for_function("() => window.__rescueStore.has('pixivRescueReaderTheme')", timeout=5000)
    saved = json.loads(page.evaluate("() => window.__rescueStore.get('pixivRescueReaderTheme')"))
    assert saved == {"background": 3, "size": 2, "spacing": 0, "font": "serif"}
    assert page.evaluate("window.__rescueRequests") == []
    assert page.locator("[data-pixiv-rescue]").count() == 0
    assert page.locator("#novel-text-container").get_attribute("class") == "horizontal background-3 size-2 spacing-0"
    context.close()


def test_userscript_fixture_applies_learned_theme_to_reader(rescue_browser) -> None:
    """记下的 Aa 设置套用到阅读层（这里是深色背景 + 大字号）；桌面 UA 走 PC 版式。"""
    page = rescue_browser.new_page()
    store = {"pixivRescueReaderTheme": json.dumps({"background": 1, "size": 2, "spacing": 1, "font": ""})}
    responses = {"https://pixiv.dongboapp.com/api/rescue/v1/novels/101": MARKUP_NOVEL}
    _install_script(page, PIXIV_404_SHELL, responses, path="/novel/show.php?id=101&pixiv_rescue=1", store=store)

    page.locator(".pixiv-rescue-chapter").wait_for(state="visible")
    assert page.locator(".pixiv-rescue-novel-text").get_attribute("data-pixiv-rescue-theme") == "1-2-1"
    assert _computed(page, ".pixiv-rescue-novel-text", ["backgroundColor", "color", "fontSize"]) == {
        "backgroundColor": "rgb(31, 31, 31)", "color": "rgb(183, 183, 183)", "fontSize": "19.2px"}
    assert "pixiv-rescue-root--pc" in page.locator("[data-pixiv-rescue='content']").get_attribute("class")
    page.close()


SERIES_ID = 301
SERIES_RESPONSES = {
    f"https://pixiv.dongboapp.com/api/rescue/v1/series/{SERIES_ID}": {
        "series_id": SERIES_ID, "title": "测试系列", "author_name": "系列作者",
        "rescue_state": "partial", "expected_count": 4, "complete_count": 3,
        "source_notice": "内容来自私人备份，并非 Pixiv 官方恢复",
    },
    f"https://pixiv.dongboapp.com/api/rescue/v1/series/{SERIES_ID}/chapters": {
        "items": [
            # 字数取自 2026-10-26 实测的 Pixiv 卡片：69,437字 → 2小时53分钟；5,824字 → 14分钟
            {"novel_id": 3011, "title": "开端", "chapter_number": 1, "text_length": 69437, "total_bookmarks": 6098,
             "tags": ["R-18", "小説", "调教"], "is_original": True, "ai_type": 1, "has_cover": False},
            {"novel_id": 3012, "title": "发展", "chapter_number": 2, "text_length": 5824, "total_bookmarks": 311,
             "tags": ["R-18"], "is_original": True, "ai_type": 1, "has_cover": False},
            {"novel_id": 3013, "title": "短章", "chapter_number": 3, "text_length": 126, "total_bookmarks": 127,
             "tags": [], "is_original": True, "ai_type": 1, "has_cover": False},
        ],
        "total": 3, "page": 1, "total_pages": 1,
        "total_text_length": 69437 + 5824 + 126, "last_create_date": "2026-01-27T10:00:00",
    },
}


def test_userscript_fixture_series_page_matches_pixiv_layout(rescue_browser) -> None:
    """系列页照 Pixiv：R-18/原创标记、「共N话（备份M话） · 更新 · 字数 · 时长」、黑色「从最初开始阅读」、章节卡片。"""
    context = rescue_browser.new_context(user_agent=MOBILE_UA, viewport={"width": 412, "height": 915})
    page = context.new_page()
    _install_script(page, PIXIV_404_SHELL, SERIES_RESPONSES, path=f"/novel/series/{SERIES_ID}?pixiv_rescue=1")

    page.locator("a.pixiv-rescue-chapter-card").first.wait_for(state="visible")
    assert page.locator(".pixiv-rescue-author-name").inner_text() == "系列作者"
    assert page.locator(".pixiv-rescue-badge").all_inner_texts() == ["R-18", "原创"]
    assert page.locator(".pixiv-rescue-series-head .pixiv-rescue-title").inner_text() == "测试系列"
    assert page.locator(".pixiv-rescue-series-info-item").all_inner_texts() == [
        "共4话（备份3话）", "2026年1月27日更新", "75,387字", "3小时8分钟"]
    start = page.locator("a.pixiv-rescue-read-link")
    assert start.all_inner_texts() == ["从最初开始阅读"]
    assert start.get_attribute("href") == "/novel/show.php?id=3011&pixiv_rescue=1"
    # 卡片：标签行（R-18 → 原创 → #标签，不带译名）、「字数 · 时长 · 收藏」；不足 1 分钟不写时长
    assert page.locator(".pixiv-rescue-chapter-title").all_inner_texts() == ["#1 开端", "#2 发展", "#3 短章"]
    assert page.locator(".pixiv-rescue-chapter-tags").first.inner_text().split() == ["R-18", "原创", "#小説", "#调教"]
    stats = page.locator(".pixiv-rescue-chapter-stats")
    assert stats.nth(0).inner_text().split() == ["69,437字", "2小时53分钟", "6,098"]
    assert stats.nth(1).inner_text().split() == ["5,824字", "14分钟", "311"]
    assert stats.nth(2).inner_text().split() == ["126字", "127"]
    # 没有本地封面：素色占位印上章节标题（Pixiv 对这类作品显示默认模板封面）
    assert page.locator(".pixiv-rescue-chapter-cover--plain").count() == 3
    assert page.locator(".pixiv-rescue-chapter-cover-title").first.inner_text() == "开端"
    # 实测样式：系列名 16px/700/24px；黑色胶囊 rgba(0,0,0,.88)、圆角 100px；卡片标题 14px/700/22px
    assert _computed(page, ".pixiv-rescue-series-head .pixiv-rescue-title", ["fontSize", "fontWeight", "lineHeight"]) == {
        "fontSize": "16px", "fontWeight": "700", "lineHeight": "24px"}
    assert _computed(page, "a.pixiv-rescue-read-link", ["backgroundColor", "borderTopLeftRadius", "color"]) == {
        "backgroundColor": "rgba(0, 0, 0, 0.88)", "borderTopLeftRadius": "100px", "color": "rgb(255, 255, 255)"}
    assert _computed(page, ".pixiv-rescue-chapter-title", ["fontSize", "fontWeight", "lineHeight"]) == {
        "fontSize": "14px", "fontWeight": "700", "lineHeight": "22px"}
    assert _reader_state(page)["hit"] is True
    # 卡片右侧留 40px 给心形占位（实测 .tag-container 右内边距），心形不可点
    assert _computed(page, ".pixiv-rescue-chapter-detail", ["paddingRight"]) == {"paddingRight": "40px"}
    assert page.locator(".pixiv-rescue-chapter-heart").first.get_attribute("aria-hidden") == "true"
    # 「浏览更多」打开「作品情报」面板，不跳页
    page.locator(".pixiv-rescue-chapter-more").first.click()
    sheet = page.get_by_role("dialog", name="作品情报")
    sheet.wait_for(state="visible")
    assert sheet.locator(".pixiv-rescue-sheet-tags").inner_text().split() == ["R-18", "原创", "#小説", "#调教"]
    assert sheet.get_by_role("link", name="阅读作品").get_attribute("href") == "/novel/show.php?id=3011&pixiv_rescue=1"
    assert page.url.endswith(f"/novel/series/{SERIES_ID}?pixiv_rescue=1")
    sheet.get_by_role("button", name="关闭").click()
    assert page.get_by_role("dialog", name="作品情报").count() == 0
    context.close()


def test_userscript_fixture_series_page_offers_continue_reading(rescue_browser) -> None:
    """读过的系列多一颗「继续阅读 #N」（进度存在脚本自己的存储里）。"""
    page = rescue_browser.new_page()
    store = {"pixivRescueSeriesProgress": json.dumps(
        {str(SERIES_ID): {"novel_id": 3012, "chapter_number": 2, "at": 1}})}
    _install_script(page, PIXIV_404_SHELL, SERIES_RESPONSES, path=f"/novel/series/{SERIES_ID}?pixiv_rescue=1",
                    store=store)

    links = page.locator("a.pixiv-rescue-read-link")
    links.first.wait_for(state="visible")
    assert links.all_inner_texts() == ["继续阅读 #2", "从最初开始阅读"]
    assert links.first.get_attribute("href") == "/novel/show.php?id=3012&pixiv_rescue=1"
    page.close()


def test_userscript_fixture_chapter_has_series_link_stats_and_nav(rescue_browser) -> None:
    """系列章节：标题上方系列名 #N、收藏/浏览快照、文末「下一话 / 上一话」系列卡，并记下阅读进度。"""
    context = rescue_browser.new_context(user_agent=MOBILE_UA, viewport={"width": 412, "height": 915})
    page = context.new_page()
    chapter = dict(MARKUP_NOVEL, novel_id=3012, title="发展", total_bookmarks=433, total_views=14525,
                   is_original=True, series_nav={
                       "series_id": SERIES_ID, "series_title": "测试系列", "chapter_number": 2, "chapter_total": 3,
                       "prev": {"novel_id": 3011, "title": "开端", "chapter_number": 1},
                       "next": {"novel_id": 3013, "title": "短章", "chapter_number": 3}})
    _install_script(page, PIXIV_404_SHELL, {"https://pixiv.dongboapp.com/api/rescue/v1/novels/3012": chapter},
                    path="/novel/show.php?id=3012&pixiv_rescue=1", store={})

    page.locator(".pixiv-rescue-series-card").wait_for(state="visible")
    series = page.locator("a.pixiv-rescue-series-link")
    assert series.inner_text() == "测试系列 #2"
    assert series.get_attribute("href") == f"/novel/series/{SERIES_ID}?pixiv_rescue=1"
    assert page.locator(".pixiv-rescue-stats").inner_text().split() == ["433", "14,525"]
    assert page.locator(".pixiv-rescue-tags").inner_text().split()[:2] == ["R-18", "原创"]
    nav = page.locator("a.pixiv-rescue-nav-button")
    assert nav.all_inner_texts() == ["下一话 #3", "上一话 #1"]
    assert [nav.nth(i).get_attribute("href") for i in range(2)] == [
        "/novel/show.php?id=3013&pixiv_rescue=1", "/novel/show.php?id=3011&pixiv_rescue=1"]
    # 实测样式：rgba(0,0,0,.04) 底、#474747、14px/700、圆角 100px
    assert _computed(page, "a.pixiv-rescue-nav-button", ["backgroundColor", "color", "fontSize", "fontWeight",
                                                         "borderTopLeftRadius"]) == {
        "backgroundColor": "rgba(0, 0, 0, 0.04)", "color": "rgb(71, 71, 71)", "fontSize": "14px",
        "fontWeight": "700", "borderTopLeftRadius": "100px"}
    progress = json.loads(page.evaluate("() => window.__rescueStore.get('pixivRescueSeriesProgress')"))
    assert progress[str(SERIES_ID)]["novel_id"] == 3012 and progress[str(SERIES_ID)]["chapter_number"] == 2
    # 单篇（无 series_nav）不画系列卡
    assert "series_nav" not in MARKUP_NOVEL
    context.close()


def test_userscript_reading_time_uses_pixiv_ratio() -> None:
    """阅读时长 = 字数 × 0.15 秒（实测 Pixiv series_content 的 readingTime / textLength）。"""
    script = _script()
    body = script.split("function formatReadingTime(", 1)[1].split("\n  }\n", 1)[0]
    assert "* 0.15 / 60" in body and "if (minutes < 1) return '';" in body


# ── 新作页「拯救系列」tab 与追更列表填空（0.12.0）────────────────────

FEED_PATHS = {
    "bookmark": "/novel/bookmark_new.php",
    "watchlist": "/following/watchlist/novels",
    "mypixiv": "/novel/mypixiv_new.php",
}
FEED_LABELS = {"bookmark": "已关注用户的新作", "watchlist": "追更列表新作", "mypixiv": "好P友新作"}
SERIES_LIST_URL = "https://pixiv.dongboapp.com/api/rescue/v1/series"
WATCH_AJAX = "/touch/ajax/watch_list/novel"
# 夹具没有 Pixiv 的全站样式：补上与布局有关的几条实测值（body 无外边距、工具类间距、
# 健康卡高 152、空白框 pv-16 + 90 高封面、1px 分隔线），间距断言才有意义。
# 必须带 <!DOCTYPE html>：Pixiv 页面是标准模式，缺了它夹具进怪异模式，行高不算块的 strut
# （「浏览更多」那一行会从实测的 18.4 变成 16）
PIXIV_BASE_CSS = (
    "<!DOCTYPE html><style>body{margin:0}.mv-16{margin-top:16px;margin-bottom:16px}.ph-16{padding-left:16px;padding-right:16px}"
    ".c-mb-16{margin-bottom:16px}.pv-16{padding-top:16px;padding-bottom:16px}.ml-16{margin-left:16px}"
    ".novel-series-thumbnail-detail{display:flex;height:152px}.novel-series-not-viewable-detail{display:flex}"
    ".novel-series-not-viewable-detail .cover{width:64px;height:90px}"
    "hr.divider{margin:0;border:0;border-top:1px solid rgba(0,0,0,.08)}</style>"
)


def _feed_heading(active: str) -> str:
    """2026-10-26 实测的手机站新作页 tab 栏：激活 tab 带 router-link 类，文字包在 <h2> 里。"""
    links = []
    for key, label in FEED_LABELS.items():
        if key == active:
            links.append(f"<a href='{FEED_PATHS[key]}' aria-current='page' "
                         "class='router-link-exact-active router-link-active active f-title-xs'>"
                         f"<h2 class='f-title-xs'>{label}</h2></a>")
        else:
            links.append(f"<a href='{FEED_PATHS[key]}' class='f-title-xs'> {label} </a>")
    return ("<div class='heading bg-c-white'><h1 class='f-title-xs c-gray-90'></h1><div class='tabs'>"
            "<div class='v-nav-tabs wide center show'><!---->" + "".join(links) + "</div></div></div>")


def _tab_texts(page) -> list[str]:
    return page.evaluate("() => Array.from(document.querySelectorAll('.v-nav-tabs a')).map((a) => a.textContent.trim())")


@pytest.mark.parametrize("active", ["bookmark", "watchlist", "mypixiv"])
def test_userscript_fixture_feed_tab_is_renamed_on_all_feed_pages(rescue_browser, active) -> None:
    """三个新作页都把第三个 tab「好P友新作」改名「拯救系列」；Vue 切路由重建 tab 栏后照样改。

    只改文本节点、不换元素：激活态的 <a> 与 <h2> 还是 Pixiv 自己的（蓝色指示线由它画）。
    改名在新节点插入的同一批变动里完成，下一个微任务里就已经是新名字（不会先露出原文案）。
    """
    context = rescue_browser.new_context(user_agent=MOBILE_UA, viewport={"width": 412, "height": 915})
    page = context.new_page()
    html = PIXIV_BASE_CSS + "<div id='app'>" + _feed_heading(active) + "</div>"
    _install_list_script(page, html, {}, {"pixivRescueToken": LIST_TOKEN}, path=FEED_PATHS[active])

    page.wait_for_function("() => document.querySelector('.v-nav-tabs a[href$=\"mypixiv_new.php\"]')"
                           ".textContent.trim() === '拯救系列'")
    assert _tab_texts(page) == ["已关注用户的新作", "追更列表新作", "拯救系列"]
    if active == "mypixiv":
        tab = page.locator(".v-nav-tabs a[href$='mypixiv_new.php']")
        assert tab.locator("h2").inner_text() == "拯救系列"
        assert "router-link-exact-active" in tab.get_attribute("class")

    # Vue 切路由时整个 heading 连同 tab 栏重新创建
    renamed_in_microtask = page.evaluate(
        """(html) => new Promise((resolve) => {
          const old = document.querySelector('.heading');
          old.insertAdjacentHTML('afterend', html);
          old.remove();
          Promise.resolve().then(() => {
            const link = document.querySelector('.v-nav-tabs a[href$="mypixiv_new.php"]');
            resolve(link.textContent.trim());
          });
        })""",
        _feed_heading(active),
    )
    assert renamed_in_microtask == "拯救系列"
    assert _tab_texts(page) == ["已关注用户的新作", "追更列表新作", "拯救系列"]
    # 其它两个 tab 与 Pixiv 自己的链接不动
    assert page.locator(".v-nav-tabs a").evaluate_all("(links) => links.map((a) => a.getAttribute('href'))") == [
        FEED_PATHS["bookmark"], FEED_PATHS["watchlist"], FEED_PATHS["mypixiv"]]
    # 旧版错放在页头上方的「拯救系列」链接已删除
    assert page.locator("[data-pixiv-rescue='tab'], .pixiv-rescue-tab-link").count() == 0
    context.close()


def _feed_series(series_id: int, index: int) -> dict:
    return {
        "series_id": series_id, "title": f"拯救系列{index}", "author_name": f"作者{index}",
        "user_id": 7000 + index, "rescue_state": "partial" if index == 1 else "success",
        "expected_count": 12 if index == 1 else None, "chapter_count": 11 if index == 1 else 5,
        "last_create_date": "2026-09-19T12:00:00+09:00", "latest_novel_id": 9000 + index,
        "x_restrict": 1 if index % 3 == 1 else 0, "has_cover": False,
        "author_avatar_url": "https://i.pximg.net/user-profile/img/a_50.jpg" if index == 1 else None,
        "personal_hold": True,
    }


FEED_PAGE_1 = SERIES_LIST_URL + "?page=1&page_size=10"
FEED_PAGE_2 = SERIES_LIST_URL + "?page=2&page_size=10"
FEED_RESPONSES = {
    FEED_PAGE_1: {"items": [_feed_series(5000 + i, i) for i in range(1, 11)], "page": 1, "total": 12, "total_pages": 2},
    FEED_PAGE_2: {"items": [_feed_series(5000 + i, i) for i in range(11, 13)], "page": 2, "total": 12, "total_pages": 2},
}
# mypixiv_new.php 的原生内容（2026-10-26 实测）：.nav-tab 子 tab、注释、原生内容块（空状态 + 翻页条）、
# 广告框、推荐
MYPIXIV_HTML = (
    PIXIV_BASE_CSS + "<div id='app'>" + _feed_heading("mypixiv")
    + "<div><div class='nav-tab'><div class='tab-list'><div class='tab-item small'> 插画・漫画 </div>"
    "<div class='tab-item active small'> 小说 </div></div></div><!---->"
    "<div id='native'><div class='pager-view-nav'></div><span id='pager-scroll-position'></span>"
    "<div><div class='works-grid-list grid'></div></div>"
    "<div class='notifications c-text-text3'><p>抱歉，目前没有作品</p></div>"
    "<div><nav class='pager add-margin-above'><div class='pager'>"
    "<a href='/novel/mypixiv_new.php' class='previous'>‹</a><a href='/novel/mypixiv_new.php' class='next'>›</a>"
    "<div><ul class='pagenum'><li><a href='/novel/mypixiv_new.php' aria-current='page'>1</a></li></ul></div>"
    "</div></nav></div></div>"
    "<div id='ad'>广告</div><span id='recommend'>推荐</span></div></div>"
)


def _feed_requests(page, url: str) -> int:
    return page.evaluate("(url) => window.__rescueRequests.filter((item) => item.url === url).length", url)


def _row_title(page, title: str):
    """按整串匹配卡片标题（封面占位上印着同样的标题，不能用 get_by_text）。"""
    return page.locator(".pixiv-rescue-series-row-title-text", has_text=re.compile("^" + re.escape(title) + "$"))


def test_userscript_fixture_rescue_feed_replaces_mypixiv_tab(rescue_browser) -> None:
    """mypixiv_new.php：原生内容隐藏（不删），紧跟其后是拯救系列列表与 Pixiv 同款翻页条；pushState 切页。"""
    context = rescue_browser.new_context(user_agent=MOBILE_UA, viewport={"width": 412, "height": 915},
                                         timezone_id="Asia/Shanghai")
    page = context.new_page()
    _install_list_script(page, MYPIXIV_HTML, FEED_RESPONSES, {"pixivRescueToken": LIST_TOKEN},
                         path=FEED_PATHS["mypixiv"])

    rows = page.locator("[data-pixiv-rescue='feed'] .pixiv-rescue-series-row")
    _row_title(page, "拯救系列1").wait_for(state="visible")
    assert page.locator("#native").get_attribute("data-pixiv-rescue-hidden") == ""
    assert page.locator("#native").is_hidden() and page.locator(".nav-tab").is_visible()
    assert page.evaluate("() => document.querySelector('#native').nextElementSibling.getAttribute('data-pixiv-rescue')") == "feed"
    assert page.locator("#ad").is_visible() and page.locator("#recommend").is_visible()
    assert rows.count() == 10
    # 卡片：链接全部带救援标记；部分备份写「12话（备份11话）」；R-18 角标；头像用 <img>
    first = rows.nth(0)
    assert first.locator(".pixiv-rescue-series-row-title-text").inner_text() == "拯救系列1"
    assert first.locator("a.pixiv-rescue-series-row-cover").get_attribute("href") == "/novel/series/5001?pixiv_rescue=1"
    assert first.locator("a.pixiv-rescue-series-row-title").get_attribute("href") == "/novel/series/5001?pixiv_rescue=1"
    assert first.get_by_role("link", name="阅读最新话").get_attribute("href") == "/novel/show.php?id=9001&pixiv_rescue=1"
    assert first.locator("a.pixiv-rescue-series-row-user").get_attribute("href") == "/users/7001/novels"
    assert first.locator(".pixiv-rescue-series-row-meta span").all_inner_texts() == ["12话（备份11话）", "2026年9月19日更新"]
    assert first.locator(".pixiv-rescue-series-row-badge").inner_text() == "R-18"
    assert first.locator("img.pixiv-rescue-series-row-avatar").get_attribute("src").startswith("https://i.pximg.net/")
    assert first.locator(".pixiv-rescue-mark").inner_text() == "拯救"
    second = rows.nth(1)
    assert second.locator(".pixiv-rescue-series-row-badge").count() == 0
    assert second.locator(".pixiv-rescue-series-row-meta span").first.inner_text() == "5话"
    # 拯救系列 tab 里没有可转发的 Pixiv 菜单，不画「…」
    assert page.locator(".pixiv-rescue-series-row-menu").count() == 0
    # 翻页条：5 个页码窗口（这里只有 2 页）、当前页、上一页/下一页
    pager = page.locator("[data-pixiv-rescue='feed'] .pixiv-rescue-feed-pager")
    current = pager.locator(".pixiv-rescue-feed-pagenum a[aria-current='page']")
    assert pager.locator(".pixiv-rescue-feed-pagenum a").all_inner_texts() == ["1", "2"]
    assert current.inner_text() == "1"
    assert pager.get_by_role("link", name="上一页").get_attribute("href") == "/novel/mypixiv_new.php"
    assert pager.get_by_role("link", name="下一页").get_attribute("href") == "/novel/mypixiv_new.php?p=2"
    # 尺寸照 Pixiv：列表顶距容器 32（16 + 16）、带角标的单行卡高 152、卡距 185、「阅读最新话」高 32、
    # 翻页条上距 36、高 35。「浏览更多」那一行沿用字体的行高 1.15，随夹具字体差 1px 以内
    geometry = page.evaluate("""() => {
      const box = (el) => el.getBoundingClientRect();
      const feed = document.querySelector('[data-pixiv-rescue="feed"]');
      const items = Array.from(feed.querySelectorAll('.pixiv-rescue-series-row')).map(box);
      const hrs = Array.from(feed.querySelectorAll('hr.pixiv-rescue-divider')).map(box);
      return { feedTop: box(feed).top, first: items[0], second: items[1], lastHr: hrs[hrs.length - 1],
        pager: box(feed.querySelector('.pixiv-rescue-feed-pager')),
        read: box(feed.querySelector('.pixiv-rescue-series-row-read')) };
    }""")
    assert round(geometry["first"]["top"] - geometry["feedTop"]) == 32
    assert abs(geometry["first"]["height"] - 152) <= 1
    assert abs(geometry["second"]["top"] - geometry["first"]["top"] - 185) <= 1
    assert round(geometry["read"]["height"]) == 32
    assert round(geometry["pager"]["top"] - geometry["lastHr"]["bottom"]) == 36
    assert round(geometry["pager"]["height"]) == 35
    assert _computed(page, ".pixiv-rescue-series-row-title-text", ["fontSize", "fontWeight", "lineHeight", "color"]) == {
        "fontSize": "14px", "fontWeight": "700", "lineHeight": "22px", "color": "rgb(31, 31, 31)"}
    assert _computed(page, ".pixiv-rescue-series-row-read", ["backgroundColor", "color", "borderTopLeftRadius"]) == {
        "backgroundColor": "rgba(0, 0, 0, 0.88)", "color": "rgb(255, 255, 255)", "borderTopLeftRadius": "100px"}
    assert _computed(page, ".pixiv-rescue-feed-pager-bar", ["backgroundColor"]) == {"backgroundColor": "rgb(74, 144, 226)"}

    # pushState 切页：URL 带 ?p=2，state 形状同 vue-router 3；钩子/观察器/轮询都不会重复请求
    pager.get_by_role("link", name="2", exact=True).click()
    _row_title(page, "拯救系列11").wait_for(state="visible")
    assert page.url.endswith("/novel/mypixiv_new.php?p=2")
    assert re.fullmatch(r"\d+\.\d{3}", page.evaluate("history.state.key"))
    assert rows.count() == 2
    assert page.evaluate("() => document.querySelector('[data-pixiv-rescue=\"feed\"]').getBoundingClientRect().top") >= 0
    page.wait_for_timeout(1300)
    assert _feed_requests(page, FEED_PAGE_2) == 1
    assert current.inner_text() == "2"
    # 浏览器后退回到第 1 页
    page.evaluate("() => history.back()")
    _row_title(page, "拯救系列1").wait_for(state="visible")
    assert "p=" not in page.url
    assert _feed_requests(page, FEED_PAGE_1) == 2
    # 「浏览更多」打开页面级「作品情报」面板：「查看目录」指向系列页，遮罩盖过 Pixiv 页头
    rows.nth(0).get_by_text("浏览更多").click()
    sheet = page.get_by_role("dialog", name="作品情报")
    sheet.wait_for(state="visible")
    assert sheet.get_by_role("link", name="查看目录").get_attribute("href") == "/novel/series/5001?pixiv_rescue=1"
    assert page.locator(".pixiv-rescue-sheet-backdrop--page").count() == 1
    sheet.get_by_role("button", name="关闭").click()
    assert page.get_by_role("dialog", name="作品情报").count() == 0

    # SPA 跳到别的 tab：撤掉列表、放回原生内容
    page.evaluate("() => history.pushState({}, '', '/novel/bookmark_new.php')")
    page.wait_for_function("() => !document.querySelector('[data-pixiv-rescue=\"feed\"]')")
    assert page.locator("[data-pixiv-rescue-hidden]").count() == 0
    assert page.locator("#native").is_visible()
    context.close()


def test_userscript_fixture_rescue_feed_error_retry_and_empty(rescue_browser) -> None:
    """拯救系列 tab 读取失败：列表位置给提示与「重试」；备份里没有系列时照 Pixiv 写空状态，翻页条照画。"""
    context = rescue_browser.new_context(user_agent=MOBILE_UA, viewport={"width": 412, "height": 915})
    page = context.new_page()
    _install_list_script(page, MYPIXIV_HTML, {}, {"pixivRescueToken": LIST_TOKEN}, path=FEED_PATHS["mypixiv"])

    retry = page.locator("[data-pixiv-rescue='feed']").get_by_role("button", name="重试")
    retry.wait_for(state="visible")
    assert "拯救系列没有读到" in page.locator(".pixiv-rescue-feed-empty").inner_text()
    assert page.locator("#native").is_hidden()
    page.evaluate("(url) => { window.__rescueResponses[url] = { items: [], page: 1, total: 0, total_pages: 1 }; }",
                  FEED_PAGE_1)
    retry.click()
    page.get_by_text("抱歉，目前没有拯救系列").wait_for(state="visible")
    assert page.locator("[data-pixiv-rescue='feed'] .pixiv-rescue-feed-pagenum a").all_inner_texts() == ["1"]
    assert _computed(page, ".pixiv-rescue-feed-empty", ["fontSize", "fontWeight", "color", "paddingTop"]) == {
        "fontSize": "12px", "fontWeight": "700", "color": "rgb(133, 133, 133)", "paddingTop": "10px"}
    context.close()


def _watch_healthy(series_id: int) -> str:
    """健康追更项（2026-10-26 实测结构节选）：每个系列 ID 一个 div，里面 div.c-mb-16 > 卡片 + 分隔线。"""
    return (
        f"<div><div class='c-mb-16'><div class='novel-series-thumbnail-detail c-mb-16'>"
        f"<a href='/novel/series/{series_id}' class='cover-link'><div class='cover'></div></a>"
        f"<div class='details'><a href='/novel/series/{series_id}'><div class='line-clamp'>健康系列{series_id}</div></a>"
        "<a href='/users/1/novels' class='user-container'><div class='user-name'>作者</div></a>"
        "<div class='meta-container'><span>3话</span><span class='ml-8'>2026年9月1日更新</span></div>"
        "</div></div><hr class='divider'></div></div>"
    )


def _watch_blank(series_id: int) -> str:
    """被删系列的空白框：DOM 里没有系列 ID（data-probe 只给测试定位用，脚本不读）；
    三点图标在 Pixiv 上会打开它自己的底部菜单，这里只记下点击。"""
    return (
        "<div><div class='c-mb-16'><div class='c-mb-16'>"
        f"<div class='novel-series-not-viewable-detail pv-16' data-probe='{series_id}'>"
        "<div class='cover'></div><div class='details ml-16'><div> 该作品无法显示 </div>"
        f"<div class='menu-icon' onclick='window.__menuClicks.push({series_id})'>⋯</div></div><!----></div>"
        "</div><hr class='divider'></div></div>"
    )


def _watchlist_html(items: list[str]) -> str:
    return (
        PIXIV_BASE_CSS + "<script>window.__menuClicks = [];</script><div id='app'>" + _feed_heading("watchlist")
        + "<div><div class='nav-tab'></div><div class='v-space-16'></div><div><span id='pager-scroll-position'></span>"
        + "<div class='mv-16 ph-16' id='watch-list'>" + "".join(items) + "</div></div></div></div>"
    )


WATCH_LOOKUP_302_304 = "https://pixiv.dongboapp.com/api/rescue/v1/lookup?series_ids=302%2C304"
# Pixiv 自己的 watch_list 应答（2026-10-26 实测形状）：被删的 302/304 不在 novelSeries 里
WATCH_BODY = {
    "error": False, "message": "",
    "body": {"total": 5, "maxPage": 1, "watchedSeriesIds": ["301", "302", "303", "304", "305"],
             "novelSeries": [{"id": "301"}, {"id": "303"}, {"id": "305"}]},
}


def test_userscript_fixture_watchlist_fills_deleted_series_in_place(rescue_browser) -> None:
    """追更列表：被删系列的空白框按位置原地换成备份卡片，高度与健康项一致；备份里没有的照旧空着。

    DOM 里没有系列 ID：靠 Pixiv watch_list 接口的有序 watchedSeriesIds 按位置对应
    （未加载的项只渲染空 div，也占一个位置）。
    """
    context = rescue_browser.new_context(user_agent=MOBILE_UA, viewport={"width": 412, "height": 915},
                                         timezone_id="Asia/Shanghai")
    page = context.new_page()
    items = [_watch_healthy(301), _watch_blank(302), "<div></div>", _watch_blank(304), _watch_healthy(305)]
    responses = {WATCH_LOOKUP_302_304: {"novels": [], "series": [dict(_feed_series(302, 1), title="被删的追更系列")]}}
    hits = _install_list_script(page, _watchlist_html(items), responses, {"pixivRescueToken": LIST_TOKEN},
                                path=FEED_PATHS["watchlist"], pixiv_json={WATCH_AJAX: WATCH_BODY})

    card = page.locator("[data-pixiv-rescue='watch-card']")
    card.wait_for(state="visible")
    assert card.count() == 1 and card.get_attribute("data-series-id") == "302"
    # 卡片插在空白框父节点 div.c-mb-16 的最前面；空白框根节点（含底部菜单挂载位）留着，
    # 只隐藏封面与文字、内边距归零
    placement = page.evaluate("""() => {
      const card = document.querySelector('[data-pixiv-rescue="watch-card"]');
      const root = card.nextElementSibling;
      return { first: card.parentElement.firstElementChild === card,
        nested: card.parentElement.classList.contains('c-mb-16') && card.parentElement.parentElement.classList.contains('c-mb-16'),
        probe: root.getAttribute('data-probe'), filled: root.getAttribute('data-pixiv-rescue-filled'),
        rootHeight: root.getBoundingClientRect().height, comment: root.lastChild.nodeType === 8 };
    }""")
    assert placement == {"first": True, "nested": True, "probe": "302", "filled": "302", "rootHeight": 0, "comment": True}
    assert page.locator("[data-probe='302'] .cover").is_hidden()
    assert page.locator("[data-probe='302'] .details").is_hidden()
    assert card.locator(".pixiv-rescue-series-row-title-text").inner_text() == "被删的追更系列"
    assert card.locator(".pixiv-rescue-series-row-meta span").all_inner_texts() == ["12话（备份11话）", "2026年9月19日更新"]
    assert card.get_by_role("link", name="阅读最新话").get_attribute("href") == "/novel/show.php?id=9001&pixiv_rescue=1"
    assert card.locator("a.pixiv-rescue-series-row-title").get_attribute("href") == "/novel/series/302?pixiv_rescue=1"
    # 备份里没有的被删系列（304）与未加载项（303）原样不动
    assert page.locator("[data-probe='304']").get_attribute("data-pixiv-rescue-filled") is None
    assert page.locator("[data-probe='304']").get_by_text("该作品无法显示").is_visible()
    # 填空项高度与健康项一致（Pixiv 实测 169），卡距 185
    boxes = page.evaluate("""() => Array.from(document.getElementById('watch-list').children)
      .map((child) => { const r = child.getBoundingClientRect(); return { top: r.top, height: r.height }; })""")
    assert abs(boxes[1]["height"] - boxes[0]["height"]) <= 1
    assert abs(boxes[1]["top"] - boxes[0]["top"] - 185) <= 1

    # 再扫几轮：自己画的卡片不会被当成失效卡再「救」一次，也不重复请求
    page.wait_for_timeout(3600)
    assert card.count() == 1 and page.locator(".pixiv-rescue-rescued-card").count() == 0
    assert card.locator(".pixiv-rescue-series-row-title-text").inner_text() == "被删的追更系列"
    urls = page.evaluate("window.__rescueRequests.map(item => item.url)")
    assert urls.count(WATCH_LOOKUP_302_304) == 1
    assert not any(url.startswith(LOOKUP_URL) and url != LOOKUP_URL + "1" for url in urls)
    # Pixiv 的 watch_list 只问了一次：填完、判定完的空白框不再触发请求
    assert len(hits) == 1 and "p=1&limit=10&new=1" in hits[0]

    # 「…」转给被隐藏的 Pixiv 三点图标（它会打开 Pixiv 自己的「从追更列表中移除」菜单）
    card.get_by_role("button", name="作品选项").click()
    assert page.evaluate("window.__menuClicks") == [302]
    # 移出追更后 Vue 把整块换成注释节点：卡片跟着撤掉
    page.evaluate("() => document.querySelector('[data-probe=\"302\"]').replaceWith(document.createComment(''))")
    page.wait_for_function("() => !document.querySelector('[data-pixiv-rescue=\"watch-card\"]')")
    context.close()


@pytest.mark.parametrize("watched", [["302", "301"], ["301", "302", "303"]], ids=["order", "count"])
def test_userscript_fixture_watchlist_skips_fill_when_positions_disagree(rescue_browser, watched) -> None:
    """位置对不上（健康卡 ID 与同下标的 ID 不一致，或项数与 ID 数不同）就不填，也不去查备份。"""
    page = rescue_browser.new_page()
    body = {"error": False, "message": "", "body": {"watchedSeriesIds": watched, "novelSeries": [{"id": "301"}]}}
    hits = _install_list_script(page, _watchlist_html([_watch_healthy(301), _watch_blank(302)]), {},
                                {"pixivRescueToken": LIST_TOKEN}, path=FEED_PATHS["watchlist"],
                                pixiv_json={WATCH_AJAX: body})

    page.wait_for_timeout(1500)
    assert hits, "应当问过 Pixiv 的 watch_list 接口"
    assert page.locator("[data-pixiv-rescue='watch-card']").count() == 0
    assert page.locator("[data-probe='302']").get_attribute("data-pixiv-rescue-filled") is None
    urls = page.evaluate("window.__rescueRequests.map(item => item.url)")
    assert not any("series_ids=" in url for url in urls)
    # 只有一张健康卡 + 一个空白框时，健康卡的链接上查四层总字数不到 64、还带着「该作品无法显示」：
    # 旧的失效卡扫描会把它当成失效卡重画。Pixiv 的 .novel-series-thumbnail-detail 只给能显示的系列
    assert page.locator(".novel-series-thumbnail-detail").count() == 1
    assert page.locator(".pixiv-rescue-rescued-card, [data-pixiv-rescue='card'], "
                        "[data-pixiv-rescue='card-pending']").count() == 0
    page.close()


# ── 0.12.1：2026-10-28 审查的 P3 与性能项 ─────────────────────────────

NGINX_502_PAGE = ("<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n<body>\r\n"
                  "<center><h1>502 Bad Gateway</h1></center>\r\n<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n")
CLOUDFLARE_522_PAGE = ("<!DOCTYPE html><html lang='en-US'><head><title>pixiv.dongboapp.com | 522: Connection timed out"
                       "</title></head><body><div id='cf-wrapper'>Connection timed out</div></body></html>")


@pytest.mark.parametrize("status, body, code, text", [
    (502, NGINX_502_PAGE, "server_error", "救援服务暂时出错，请稍后再试。"),
    (522, CLOUDFLARE_522_PAGE, "server_error", "救援服务暂时出错，请稍后再试。"),
    # 对照：2xx 但 JSON 被截断（部分手机管理器截断大应答）仍是 invalid_response
    (200, '{"ok": true, "data": {"title": "截', "invalid_response", "备份数据没有完整读到，请重试。"),
], ids=["nginx-502", "cloudflare-522", "truncated-200"])
def test_userscript_fixture_gateway_error_pages_are_classified_by_status(rescue_browser, status, body, code, text) -> None:
    """回归 U1：部署重启时网关回 HTML 错误页。0.12.0 先 JSON.parse 再看状态码，
    502/52x 都被判成 invalid_response，阅读层显示「备份数据没有完整读到」。"""
    page = rescue_browser.new_page()
    responses = {"https://pixiv.dongboapp.com/api/rescue/v1/novels/101": {"__raw": {"status": status, "body": body}}}
    _install_script(page, PIXIV_404_SHELL, responses, path="/novel/show.php?id=101&pixiv_rescue=1")

    page.locator(".pixiv-rescue-error").wait_for(state="visible")
    assert page.locator(".pixiv-rescue-error").inner_text() == text
    assert page.locator(".pixiv-rescue-meta").inner_text() == "错误代码：" + code
    page.close()


def test_userscript_fixture_list_page_shows_gateway_error(rescue_browser) -> None:
    """回归 U1：列表页 lookup 遇 502 HTML。0.12.0 判成 invalid_response，不在提示白名单里，页面上什么都没有。"""
    page = rescue_browser.new_page()
    responses = {LOOKUP_URL + "101": {"__raw": {"status": 502, "body": NGINX_502_PAGE}}}
    _install_list_script(page, "<ul id='list'>" + _broken_card_html(101) + "</ul>", responses,
                         {"pixivRescueToken": LIST_TOKEN})

    notice = page.locator("[data-pixiv-rescue-notice]")
    notice.wait_for(state="visible")
    assert "救援服务暂时出错" in notice.inner_text()
    # 失败的卡片撤销 pending，下一轮照常重试
    assert page.locator(".pixiv-rescue-rescued-card").count() == 0
    page.close()


RUNTIME_BAR_TEXT = "救援脚本需要脚本管理器支持"
COUNT_RUNTIME_BARS = ("(text) => Array.from(document.querySelectorAll('body > div, html > div'))"
                      ".filter((node) => node.textContent.startsWith(text)).length")


def _install_with_late_gm(page, path: str, gm_after_ms: int | None) -> None:
    """GM API 晚注入：脚本先跑（拿不到 GM_*），gm_after_ms 毫秒后才补上；None = 一直不给。"""
    html = PIXIV_404_SHELL
    page.route("http://pixiv.test/**", lambda route: route.fulfill(
        status=200, headers={"Content-Type": "text/html; charset=utf-8"}, body=html))
    page.goto("http://pixiv.test" + path)
    if gm_after_ms is not None:
        page.evaluate(
            """(delay) => setTimeout(() => {
              window.GM_getValue = (key, fallback) => key === 'pixivRescueToken' ? 'rsq_test' : fallback;
              window.GM_setValue = () => {};
              window.GM_registerMenuCommand = () => {};
              window.GM_xmlhttpRequest = (options) => setTimeout(() => options.onload({status: 200,
                responseText: JSON.stringify({ok: true, data: {title: '备份', text_raw: '晚注入后读到的正文'}})}), 0);
            }, delay)""",
            gm_after_ms,
        )
    page.add_script_tag(content=_script())


def test_userscript_fixture_late_gm_leaves_no_runtime_bar(rescue_browser) -> None:
    """回归 U2：GM API 晚注入（第 3 次重试时才就绪）。0.12.0 每次重试失败都 prepend 一条红条，
    功能恢复后还剩 3 条叠在页头，盖住阅读层顶栏的「回到 Pixiv 页面」。"""
    page = rescue_browser.new_page()
    _install_with_late_gm(page, "/novel/show.php?id=101&pixiv_rescue=1", gm_after_ms=1200)

    page.get_by_text("晚注入后读到的正文").wait_for(state="visible")
    assert page.evaluate(COUNT_RUNTIME_BARS, RUNTIME_BAR_TEXT) == 0
    # 顶栏按钮没被盖住：Playwright 的 click 会先做命中检查，被红条盖住就一直等到超时
    page.get_by_role("button", name="回到 Pixiv 页面").click()
    page.get_by_role("button", name="查看私人备份").wait_for(state="visible")
    page.close()


def test_userscript_fixture_missing_gm_shows_one_closable_bar(rescue_browser) -> None:
    """回归 U2：管理器真不支持 GM API。5 秒重试用尽才提示一条，可以关掉（0.12.0 叠 11 条、关不掉）。"""
    page = rescue_browser.new_page()
    _install_with_late_gm(page, "/novel/show.php?id=101", gm_after_ms=None)

    page.wait_for_timeout(1200)
    assert page.evaluate(COUNT_RUNTIME_BARS, RUNTIME_BAR_TEXT) == 0
    page.get_by_text(RUNTIME_BAR_TEXT).wait_for(state="visible", timeout=10000)
    page.wait_for_timeout(600)
    assert page.evaluate(COUNT_RUNTIME_BARS, RUNTIME_BAR_TEXT) == 1
    assert "GM_getValue、GM_xmlhttpRequest" in page.get_by_text(RUNTIME_BAR_TEXT).inner_text()
    page.get_by_role("button", name="知道了").click()
    assert page.evaluate(COUNT_RUNTIME_BARS, RUNTIME_BAR_TEXT) == 0
    page.close()


def test_userscript_fixture_info_sheet_closes_on_spa_route_change(rescue_browser) -> None:
    """回归 U3：挂在 body 上的「作品情报」面板（遮罩层叠 2147483500）在 SPA 切页时不清理，盖住新页面。"""
    context = rescue_browser.new_context(user_agent=MOBILE_UA, viewport={"width": 412, "height": 915})
    page = context.new_page()
    _install_list_script(page, MYPIXIV_HTML, FEED_RESPONSES, {"pixivRescueToken": LIST_TOKEN},
                         path=FEED_PATHS["mypixiv"])
    sheets = "() => document.querySelectorAll('[data-pixiv-rescue=\"sheet\"]').length"

    def open_sheet() -> None:
        _row_title(page, "拯救系列1").wait_for(state="visible")
        page.locator("[data-pixiv-rescue='feed'] .pixiv-rescue-series-row").nth(0).get_by_text("浏览更多").click()
        page.get_by_role("dialog", name="作品情报").wait_for(state="visible")

    open_sheet()
    # 地址没变：1 秒路由轮询不能把刚打开的面板关掉
    page.wait_for_timeout(1300)
    assert page.evaluate(sheets) == 1
    # Pixiv 站内跳页（pushState）
    page.evaluate("() => history.pushState({}, '', '/novel/bookmark_new.php')")
    assert page.evaluate(sheets) == 0
    # 手机返回 / 前进手势（popstate）
    page.evaluate("() => history.back()")
    open_sheet()
    page.evaluate("() => history.forward()")
    page.wait_for_function("() => !document.querySelector('[data-pixiv-rescue=\"sheet\"]')")
    context.close()


def test_userscript_fixture_rescue_feed_pager_follows_served_page(rescue_browser) -> None:
    """回归 U4：书签里存的 ?p=5 已超出页数，服务端钳到末页并回 page: 2。0.12.0 按请求页画翻页条：
    没有一个页码是当前页，「上一页」指向的就是正在看的第 2 页。"""
    context = rescue_browser.new_context(user_agent=MOBILE_UA, viewport={"width": 412, "height": 915})
    page = context.new_page()
    responses = dict(FEED_RESPONSES)
    responses[SERIES_LIST_URL + "?page=5&page_size=10"] = FEED_RESPONSES[FEED_PAGE_2]
    _install_list_script(page, MYPIXIV_HTML, responses, {"pixivRescueToken": LIST_TOKEN},
                         path=FEED_PATHS["mypixiv"] + "?p=5")

    _row_title(page, "拯救系列11").wait_for(state="visible")
    pager = page.locator("[data-pixiv-rescue='feed'] .pixiv-rescue-feed-pager")
    assert pager.locator(".pixiv-rescue-feed-pagenum a").all_inner_texts() == ["1", "2"]
    assert pager.locator(".pixiv-rescue-feed-pagenum a[aria-current='page']").all_inner_texts() == ["2"]
    assert pager.get_by_role("link", name="上一页").get_attribute("href") == "/novel/mypixiv_new.php"
    assert pager.get_by_role("link", name="下一页").get_attribute("href") == "/novel/mypixiv_new.php?p=2"
    context.close()


def test_userscript_fixture_rate_limited_cover_retries_on_next_view(rescue_browser) -> None:
    """回归 U5 + P17：封面 429 先空着、卡片下次进入视口时重试（0.12.0 永久换成占位图）；
    404 仍是没有封面、不重试。object URL 在 <img> 与回写缓存都用完后回收（0.12.0 只建不回收）。"""
    page = rescue_browser.new_page()
    page.add_init_script(
        """window.__blobUrls = { created: 0, revoked: 0 };
        const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
        URL.createObjectURL = (obj) => { window.__blobUrls.created += 1; return create(obj); };
        URL.revokeObjectURL = (url) => { window.__blobUrls.revoked += 1; return revoke(url); };"""
    )
    cover = "https://pixiv.dongboapp.com/api/rescue/v1/novels/{}/cover"
    responses = {LOOKUP_URL + "101%2C102": {"novels": [
        {"novel_id": novel_id, "title": f"备份{novel_id}", "author_name": "作者", "has_cover": True, "personal_hold": True}
        for novel_id in (101, 102)], "series": []}}
    html = "<ul id='list'>" + _broken_card_html(101) + _broken_card_html(102) + "</ul><div style='height:4000px'></div>"
    _install_list_script(page, html, responses, {"pixivRescueToken": LIST_TOKEN},
                         cover_status={cover.format(101): [429], cover.format(102): [404]})
    state = """(index) => { const box = document.querySelectorAll('.pixiv-rescue-rescued-cover')[index];
      if (!box) return 'missing';
      const img = box.querySelector('img');
      return !img ? 'removed' : (img.complete && img.naturalWidth > 0 ? 'loaded' : 'blank'); }"""
    requests = "(url) => window.__rescueRequests.filter((item) => item.url === url).length"

    def wait_state(index: int, expected: str) -> None:
        page.wait_for_function("([index, expected]) => (" + state + ")(index) === expected", arg=[index, expected])

    page.wait_for_function(requests + " === 1", arg=cover.format(101))
    wait_state(1, "removed")
    assert page.evaluate(state, 0) == "blank"
    # 滑走：等观察器真的报告「离开视口」再滑回来（同一轮渲染里所有观察器一起收到通知），不靠固定等待
    page.evaluate("""() => new Promise((resolve) => {
      window.scrollTo(0, 3000);
      const img = document.querySelectorAll('.pixiv-rescue-rescued-cover')[0].querySelector('img');
      const io = new IntersectionObserver((entries) => {
        if (entries.some((entry) => !entry.isIntersecting)) { io.disconnect(); setTimeout(resolve, 0); }
      });
      io.observe(img);
    })""")
    page.evaluate("() => window.scrollTo(0, 0)")
    wait_state(0, "loaded")
    assert page.evaluate(requests, cover.format(101)) == 2
    assert page.evaluate(requests, cover.format(102)) == 1 and page.evaluate(state, 1) == "removed"
    page.wait_for_function("() => window.__blobUrls.revoked === 1")
    assert page.evaluate("() => window.__blobUrls") == {"created": 1, "revoked": 1}
    # 回收不影响回写缓存（FileReader 读的是 Blob 本身，不是 URL）
    cover_key = "pixivRescueCover:" + _cover_key_suffix(LIST_TOKEN) + ":novel:101"
    page.wait_for_function("(key) => Boolean(window.__rescueStore.get(key))", arg=cover_key)
    assert json.loads(page.evaluate("(key) => window.__rescueStore.get(key)", cover_key))["dataUrl"].startswith(
        "data:image/png;base64,")
    page.close()


def test_userscript_fixture_list_scan_reads_lookup_cache_once(rescue_browser) -> None:
    """回归 P17：一轮扫描只读、只解析一次整份 lookup 缓存（0.12.0 每张卡都重新读一遍）。"""
    page = rescue_browser.new_page()
    now_ms = int(page.evaluate("Date.now()"))
    ids = [660001 + i for i in range(5)]
    entries = {f"n:{novel_id}": {"at": now_ms, "item": {
        "novel_id": novel_id, "title": f"缓存{novel_id}", "author_name": "作者", "has_cover": False,
        "personal_hold": True}} for novel_id in ids}
    store = {"pixivRescueToken": LIST_TOKEN, "pixivRescueLookupCache": json.dumps(
        {"suffix": _cover_key_suffix(LIST_TOKEN), "entries": entries}, ensure_ascii=False)}
    _install_list_script(page, "<ul id='list'>" + "".join(_broken_card_html(i) for i in ids) + "</ul>", {}, store)

    page.wait_for_function("() => document.querySelectorAll('.pixiv-rescue-rescued-card').length === 5")
    page.wait_for_timeout(1200)
    assert page.evaluate("() => window.__rescueReads.pixivRescueLookupCache") == 1
    urls = page.evaluate("window.__rescueRequests.map(item => item.url)")
    assert not any(str(novel_id) in url for url in urls for novel_id in ids)
    page.close()


def test_userscript_card_scan_stops_reading_ancestor_text_past_limit() -> None:
    """P16：祖先文本必然包含子孙文本，某层超过阈值后外层只查类名，不再取 textContent。

    0.12.0 每层都取 textContent，外两层常是整个列表乃至整页（审查实测 60 张卡单轮 59 ms）。
    判定结果与旧版逐个等价：`.probe0121` 的对照在 60 张卡的列表、测试夹具与 2000+ 个随机节点上零差异。
    """
    body = _script().split("function cardIsBroken(card) {", 1)[1].split("\n  }\n", 1)[0]
    loop = body.split("for (const target of targets) {", 1)[1]
    assert loop.index("if (!tooLong) {") < loop.index("target.textContent")
    assert "if (text.length > BROKEN_CARD_TEXT_LIMIT) tooLong = true;" in loop
    # 类名信号在每一层都查，不受文本长度影响
    assert loop.index("invisible-work-container") > loop.index("if (!tooLong) {")
    assert "elementMatchesUnavailable(target)" not in body


@pytest.mark.parametrize("path", ["/novel/show.php?id=101", "/zh-tw/novel/show.php?id=101"], ids=["ja", "zh-tw"])
def test_userscript_fixture_healthy_novel_page_records_no_dom_changes(rescue_browser, path) -> None:
    """健康正文页：先滚到页中再晚注入，记下注入后的全部 DOM 变动。

    唯一的变动是 0.7.0 起所有 Pixiv 页都有的「救援 <版本>」启动角标（data-pixiv-rescue-boot，
    6 秒后自删）；其余零节点、零属性、零文本改动，不请求备份，scrollY 不变。
    0.12.2（U6）：/zh-tw/ 前缀页也认得出是正文页（照样只问一次 Pixiv ajax），同样零改动。
    """
    context = rescue_browser.new_context(user_agent=MOBILE_UA, viewport={"width": 412, "height": 915})
    page = context.new_page()
    html = ("<!DOCTYPE html><div id='spa-contents'><section id='novel-text-container' "
            "class='horizontal background-0 size-1 spacing-1'>"
            + "<p class='novel-paragraph'>健康正文段落。</p>" * 300 + "</section></div>")

    def watch(target) -> None:
        target.evaluate("() => window.scrollTo(0, 600)")
        target.evaluate(
            """() => { window.__mutations = [];
              new MutationObserver((records) => records.forEach((r) => {
                const nodes = Array.from(r.addedNodes).concat(Array.from(r.removedNodes));
                const own = nodes.length && nodes.every((n) => n.nodeType === 1 &&
                  (n.hasAttribute('data-pixiv-rescue-boot') || n.tagName === 'SCRIPT'));
                if (r.type !== 'childList' || !own) window.__mutations.push(r.type + ':' + r.target.nodeName);
              })).observe(document, { subtree: true, childList: true, attributes: true, characterData: true }); }"""
        )

    ajax_hits = _install_script(page, html, {}, path=path, ajax_state="available", before_inject=watch)
    page.wait_for_timeout(2000)
    assert ajax_hits == ["http://pixiv.test/ajax/novel/101"]
    assert page.evaluate("window.__rescueRequests") == []
    assert page.evaluate("window.__mutations") == []
    assert page.evaluate("window.scrollY") == 600
    assert page.locator("[data-pixiv-rescue-boot]").count() == 1
    context.close()


# ── 0.12.2：2026-10-28 审查的 U6 / U7 与第 2、4 节（指南「0.12.2 修同一份审查…」一段）────

RESCUE_NOVEL_101 = {
    "novel_id": 101, "title": "前缀页的备份", "text_raw": "前缀页读到的正文",
    "source_notice": "内容来自私人备份，并非 Pixiv 官方恢复", "rescue_state": "success",
}


@pytest.mark.parametrize("prefix, kind", [
    ("/zh-tw", "novel"), ("/zh-tw", "series"), ("/zh-tw", "bookmarks"),
    # 对照：两个字母的前缀 0.12.1 就认
    ("/en", "novel"),
], ids=["zh-tw-novel", "zh-tw-series", "zh-tw-bookmarks", "en-novel"])
def test_userscript_fixture_lang_prefixed_pages_are_rescued(rescue_browser, prefix, kind) -> None:
    """回归 U6：语言前缀只认两个字母。/zh-tw/（繁体中文界面）剥不掉，正文、系列、收藏列表
    都被判成非目标页：0.12.1 注入后什么都不做，已删作品的备份不出来。"""
    page = rescue_browser.new_page()
    if kind == "novel":
        _install_script(page, "<!DOCTYPE html>" + PIXIV_404_SHELL,
                        {"https://pixiv.dongboapp.com/api/rescue/v1/novels/101": RESCUE_NOVEL_101},
                        path=prefix + "/novel/show.php?id=101", ajax_state="unavailable")
        page.get_by_text("前缀页读到的正文").wait_for(state="visible", timeout=5000)
        assert _reader_state(page)["hit"] is True
    elif kind == "series":
        _install_script(page, "<!DOCTYPE html>" + PIXIV_404_SHELL, SERIES_RESPONSES,
                        path=prefix + f"/novel/series/{SERIES_ID}", ajax_state="unavailable")
        page.locator("a.pixiv-rescue-chapter-card").first.wait_for(state="visible", timeout=5000)
        assert page.locator("a.pixiv-rescue-chapter-card").count() == 3
    else:
        responses = {LOOKUP_URL + "101": {"novels": [{"novel_id": 101, "title": "前缀页的收藏备份",
                                                      "author_name": "作者", "has_cover": False,
                                                      "personal_hold": True}], "series": []}}
        _install_list_script(page, "<!DOCTYPE html><ul id='list'>" + _broken_card_html(101) + "</ul>", responses,
                             {"pixivRescueToken": LIST_TOKEN}, path=prefix + LIST_PATH)
        page.locator(".pixiv-rescue-rescued-title", has_text="前缀页的收藏备份").wait_for(state="visible", timeout=5000)
    page.close()


def _script_functions(script: str, *names: str) -> str:
    """按函数名摘出脚本里的顶层函数源码（到同缩进的「  }」为止）；脚本里没有的名字跳过。"""
    parts = []
    for name in names:
        marker = "\n  function " + name + "("
        if marker in script:
            parts.append("function " + name + "(" + script.split(marker, 1)[1].split("\n  }\n", 1)[0] + "\n}\n")
    return "".join(parts)


def test_userscript_fixture_ten_thousand_blank_lines_render_in_linear_time(rescue_browser) -> None:
    """回归 U7：0.12.1 用 replace(/^\\n+|\\n+$/g, '') 剥每页首尾换行，页中间连续 k 个换行时 O(k²)：
    1 万个空行约 1.7 秒、2 万个约 5.4 秒（node 22 / V8 实测，与 Chromium 同一引擎）。

    计时只量分页函数本身：阅读层整体渲染有约 150–200 ms 与正文无关的固定开销（两版相同），
    拿整页渲染卡阈值量的是那部分。再整页渲染一遍核对剥换行的语义不变：首尾空行去掉、
    页中的空行不出空段落、中间的空页保留（[jump:N] 页码不错位）。
    """
    page = rescue_browser.new_page()
    url = "https://pixiv.dongboapp.com/api/rescue/v1/novels/101"
    blank = "\n" * 10001  # 两段之间 1 万个空行
    text = (blank + "第一段正文" + blank + "最后一段正文" + blank
            + "[newpage]" + blank + "[newpage]第三页" + blank)
    source = _script_functions(_script(), "trimPageNewlines", "splitPixivPages")
    split = page.evaluate(
        """([source, text]) => {
          const splitPixivPages = new Function(source + 'return splitPixivPages;')();
          const started = performance.now();
          const pages = splitPixivPages(text);
          return { ms: performance.now() - started, pages: pages };
        }""",
        [source, text],
    )
    assert split["ms"] < 50, f"1 万个空行分页用了 {split['ms']:.0f} ms"
    assert split["pages"] == ["第一段正文" + blank + "最后一段正文", "", "第三页"]

    novel = dict(RESCUE_NOVEL_101, title="很多空行", text_raw=text)
    _install_script(page, "<!DOCTYPE html>" + PIXIV_404_SHELL, {url: novel},
                    path="/novel/show.php?id=101&pixiv_rescue=1")
    page.locator(".pixiv-rescue-para", has_text="最后一段正文").wait_for(state="visible", timeout=20000)
    paras = page.locator(".pixiv-rescue-novel-text .pixiv-rescue-para").all_inner_texts()
    assert [para.strip() for para in paras] == ["第一段正文", "最后一段正文"]
    labels = page.locator(".pixiv-rescue-pager-label")
    assert labels.all_inner_texts() == ["1 / 3 页", "1 / 3 页"]
    page.locator(".pixiv-rescue-pager").first.get_by_role("button", name="下一页").click()
    assert labels.all_inner_texts() == ["2 / 3 页", "2 / 3 页"]
    assert page.locator(".pixiv-rescue-novel-text .pixiv-rescue-para").all_inner_texts() == ["备份正文为空"]
    page.close()


def _held_lookup_novel(novel_id: int, title: str) -> dict:
    return {"novels": [{"novel_id": novel_id, "title": title, "author_name": "作者", "has_cover": False,
                        "personal_hold": True}], "series": []}


# 应答回来之前卡片变成了什么样（模拟 Vue 翻页复用同一个节点、或整块换掉）
REUSE_MUTATIONS = {
    # 同一个节点改画另一部失效作品：只改链接
    "relinked": "() => { document.querySelector('#list a').setAttribute('href', '/novel/show.php?id=102'); }",
    # 同一个节点改画一部健康作品：链接与文字都换掉
    "healed": """() => { const link = document.querySelector('#list a');
        link.setAttribute('href', '/novel/show.php?id=103');
        link.querySelector('div div div').textContent = '健康作品的标题，简介与标签都在：' + '一段足够长的简介文字'.repeat(6); }""",
    # 整块从页面上摘掉（保留引用，之后再放回去）
    "detached": "() => { window.__detached = document.querySelector('#list li'); window.__detached.remove(); }",
}


@pytest.mark.parametrize("mutation", list(REUSE_MUTATIONS), ids=list(REUSE_MUTATIONS))
def test_userscript_fixture_lookup_reply_skips_card_that_changed(rescue_browser, mutation) -> None:
    """审查第 2 节：lookup 回调写卡片前核对卡片仍在页面上、仍是 card-pending、链接里的作品 ID 没变。

    0.12.1 不核对：应答迟到时把上一部作品（101）的备份画到已经改画别的作品的节点上，并标成终态
    不再查询（relinked 时 102 永远查不到；healed 时健康卡被换成 101 的备份卡；detached 时写进
    已摘掉的节点，放回页面后显示的是没核对过的卡）。
    """
    page = rescue_browser.new_page()
    responses = {LOOKUP_URL + "101": _held_lookup_novel(101, "旧作品备份"),
                 LOOKUP_URL + "102": _held_lookup_novel(102, "新作品备份")}
    _install_list_script(page, "<!DOCTYPE html><ul id='list'>" + _broken_card_html(101) + "</ul>", responses,
                         {"pixivRescueToken": LIST_TOKEN}, hold=["novel_ids=101"])
    lookups = "(url) => window.__rescueRequests.filter((item) => item.url === url).length"
    page.wait_for_function(lookups + " === 1", arg=LOOKUP_URL + "101")
    assert page.locator("#list [data-pixiv-rescue='card-pending']").count() == 1
    page.evaluate(REUSE_MUTATIONS[mutation])
    page.evaluate("() => new Promise((resolve) => { window.__rescueRelease('novel_ids=101'); setTimeout(resolve, 0); })")
    assert page.evaluate("() => window.__rescueDelivered") == [LOOKUP_URL + "101"]
    assert page.locator(".pixiv-rescue-rescued-title", has_text="旧作品备份").count() == 0

    if mutation == "relinked":
        # 下一轮扫描按现在的链接重新登记，查到的是 102
        page.locator(".pixiv-rescue-rescued-title", has_text="新作品备份").wait_for(state="visible", timeout=5000)
        assert page.evaluate(lookups, LOOKUP_URL + "102") == 1
    elif mutation == "healed":
        page.wait_for_timeout(1200)
        assert page.locator(".pixiv-rescue-rescued-card").count() == 0
        assert page.locator("#list [data-pixiv-rescue]").count() == 0
        assert "健康作品的标题" in page.locator("#list a").inner_text()
    else:
        # 摘下来的节点原样不动、也不挂着 pending；放回页面后照常从缓存画出来，不再多查一次
        detached = page.evaluate("""() => ({ text: window.__detached.textContent,
            state: window.__detached.querySelector('.works-item').getAttribute('data-pixiv-rescue') })""")
        assert detached == {"text": "该作品无法显示", "state": None}
        page.evaluate("() => document.getElementById('list').append(window.__detached)")
        page.locator(".pixiv-rescue-rescued-title", has_text="旧作品备份").wait_for(state="visible", timeout=5000)
        assert page.evaluate(lookups, LOOKUP_URL + "101") == 1
    page.close()


def test_userscript_fixture_series_directory_first_load_failure_offers_retry(rescue_browser) -> None:
    """审查第 2 节：系列目录第一次就没读到时给「重试」（0.12.1 只有一行提示，只能整页刷新）。

    重试只重取目录，不重取系列信息；再失败就换上新的提示和一颗新的「重试」，不叠加；
    读到后整页照首次成功那样画（话数、更新日、字数、「从最初开始阅读」、章节卡片）。
    """
    context = rescue_browser.new_context(user_agent=MOBILE_UA, viewport={"width": 412, "height": 915},
                                         timezone_id="Asia/Shanghai")
    page = context.new_page()
    series_url = f"https://pixiv.dongboapp.com/api/rescue/v1/series/{SERIES_ID}"
    chapters_url = series_url + "/chapters"
    responses = {series_url: SERIES_RESPONSES[series_url],
                 chapters_url: {"__raw": {"status": 502, "body": NGINX_502_PAGE}}}
    _install_script(page, "<!DOCTYPE html>" + PIXIV_404_SHELL, responses,
                    path=f"/novel/series/{SERIES_ID}?pixiv_rescue=1")
    calls = "(url) => window.__rescueRequests.filter((item) => item.url === url).length"

    note = page.locator(".pixiv-rescue-directory-error")
    note.wait_for(state="visible")
    assert note.get_by_text("目录没有读到：救援服务暂时出错，请稍后再试。").is_visible()
    # 系列头照常画，但没有目录就没有「从最初开始阅读」
    assert page.locator(".pixiv-rescue-series-head .pixiv-rescue-title").inner_text() == "测试系列"
    assert page.locator("a.pixiv-rescue-read-link").count() == 0
    retry = note.get_by_role("button", name="重试")
    assert retry.count() == 1
    assert _computed(page, ".pixiv-rescue-directory-error .pixiv-rescue-button", ["backgroundColor"]) == {
        "backgroundColor": "rgb(255, 255, 255)"}

    # 再失败一次：提示换成这次的原因，仍然只有一颗「重试」
    page.evaluate("(url) => { window.__rescueResponses[url] = { __raw: { status: 429, body: '' } }; }", chapters_url)
    retry.click()
    page.wait_for_function(calls + " === 2", arg=chapters_url)
    note.get_by_text("目录没有读到：请求太频繁，请稍后再试。").wait_for(state="visible")
    assert page.locator(".pixiv-rescue-directory-error").count() == 1
    assert note.get_by_role("button", name="重试").count() == 1

    # 这次读到了：整页重画，和首次就成功一样
    page.evaluate("([url, data]) => { window.__rescueResponses[url] = data; }",
                  [chapters_url, SERIES_RESPONSES[chapters_url]])
    note.get_by_role("button", name="重试").click()
    page.locator("a.pixiv-rescue-chapter-card").first.wait_for(state="visible")
    assert page.locator("a.pixiv-rescue-chapter-card").count() == 3
    assert page.locator(".pixiv-rescue-directory-error").count() == 0
    assert page.locator(".pixiv-rescue-series-info-item").all_inner_texts() == [
        "共4话（备份3话）", "2026年1月27日更新", "75,387字", "3小时8分钟"]
    assert page.locator("a.pixiv-rescue-read-link").all_inner_texts() == ["从最初开始阅读"]
    assert page.locator("[data-pixiv-rescue='content']").count() == 1
    assert page.locator(".pixiv-rescue-panel").count() == 1
    assert page.evaluate(calls, chapters_url) == 3
    assert page.evaluate(calls, series_url) == 1
    context.close()


# 一轮列表扫描 = collectBrokenCards 里那一次全页 querySelectorAll(链接选择器)：包一层记下时间
LIST_SCAN_COUNTER = """
(() => {
  const original = Document.prototype.querySelectorAll;
  window.__listScans = [];
  Document.prototype.querySelectorAll = function (selector) {
    if (selector === 'a[href*="/novel/show.php?id="], a[href*="/novel/series/"]') window.__listScans.push(Date.now());
    return original.apply(this, arguments);
  };
})();
"""


def test_userscript_fixture_list_fast_scan_ends_15s_after_entering_while_dom_keeps_changing(rescue_browser) -> None:
    """审查第 2 节：删掉观察器里延长 fastScanUntil 的那一行，500ms 快速轮询只在进入列表页后 15 秒内。

    核实（假时钟跑 60 秒数全页扫描）：页面静止时那一行确实不起作用（快速轮询 15 秒后已结束，
    推后的截止时间没人读）；但页面一直有 DOM 变动（广告、懒加载）时它让 500ms 全页扫描永不停止，
    0.12.1 在 30–60 秒里扫 85 次，删掉后 25 次（3 秒定时 + 每批变动后一次）。插入的卡片由每批
    变动后的那一轮扫描接住，不靠快速轮询。
    """
    page = rescue_browser.new_page()
    page.clock.install(time=1_800_000_000_000)
    page.add_init_script(LIST_SCAN_COUNTER)
    _install_list_script(page, "<!DOCTYPE html><ul id='list'></ul>", {}, {"pixivRescueToken": LIST_TOKEN})
    started = page.evaluate("() => Date.now()")
    # 每 2 秒往列表里插一个节点（childList 变动）
    page.evaluate("() => setInterval(() => document.getElementById('list').append(document.createElement('li')), 2000)")
    for _ in range(45):
        page.clock.run_for(1000)
    scans = page.evaluate("(started) => window.__listScans.map((at) => at - started)", started)
    first = sum(1 for at in scans if at < 15000)
    later = sum(1 for at in scans if 20000 <= at < 45000)
    assert first > 25, f"前 15 秒只扫了 {first} 次：快速轮询没跑起来，这个计数说明不了什么"
    assert later < 40, f"20–45 秒还扫了 {later} 次：快速轮询没在进入列表页 15 秒后停下"
    page.close()


# 管理器只给了 GM_getValue / GM_xmlhttpRequest（手机端 X浏览器、Via 等内置管理器的情形）
PARTIAL_GM = """
(() => {
  window.__menus = [];
  window.GM_getValue = (key, fallback) => key === 'pixivRescueToken' ? 'rsq_test' : fallback;
  window.GM_xmlhttpRequest = (options) => setTimeout(() => options.onload(
    options.url === 'https://pixiv.dongboapp.com/api/rescue/v1/novels/101'
      ? { status: 200, responseText: JSON.stringify({ ok: true, data: { title: '备份', text_raw: '缺 API 时读到的正文' } }) }
      : { status: 404, responseText: '{"ok":false}' }), 0);
  if (window.__withMenu) window.GM_registerMenuCommand = (name, callback) => { window.__menus.push([name, callback]); };
})();
"""


@pytest.mark.parametrize("variant", ["absent", "local-undefined", "menu-without-setvalue"])
def test_userscript_fixture_missing_optional_gm_apis_degrade_locally(rescue_browser, variant) -> None:
    """审查第 2 节：GM_registerMenuCommand / GM_setValue 缺失时就地降级，不再写 window.GM_xxx = 空函数。

    absent：两个 API 都不存在。0.12.1 把空函数写到页面的 window 上（别的脚本会以为真有这个 API）。
    local-undefined：管理器把没实现的 API 作为值为 undefined 的局部变量传进脚本。0.12.1 写 window
    兜不住，registerTokenMenus 照样抛错，整个脚本停在那里，备份不出来。
    menu-without-setvalue：有菜单、没 GM_setValue。0.12.1 存了个空、照样弹「已保存」；现在如实说存不了。
    """
    page = rescue_browser.new_page()
    page.add_init_script("window.__withMenu = " + ("true" if variant == "menu-without-setvalue" else "false") + ";")
    page.add_init_script(PARTIAL_GM)
    page.route("http://pixiv.test/**", lambda route: route.fulfill(
        status=200, headers={"Content-Type": "text/html; charset=utf-8"}, body="<!DOCTYPE html>" + PIXIV_404_SHELL))
    page.goto("http://pixiv.test/novel/show.php?id=101&pixiv_rescue=1")
    script = _script()
    if variant == "local-undefined":
        script = "(function (GM_registerMenuCommand, GM_setValue) {\n" + script + "\n})();"
    page.add_script_tag(content=script)

    page.get_by_text("缺 API 时读到的正文").wait_for(state="visible", timeout=5000)
    assert page.locator("[data-pixiv-rescue-fatal]").count() == 0
    if variant == "menu-without-setvalue":
        alerts = page.evaluate("""() => {
          const said = [];
          window.alert = (text) => said.push(String(text));
          window.prompt = () => 'rsq_new_token';
          const menu = window.__menus.find((entry) => entry[0] === '设置或更新救援 Token');
          menu[1]();
          return said;
        }""")
        assert alerts == ["当前脚本管理器不支持保存（缺 GM_setValue）。请在管理器里直接编辑本脚本的存储。"]
        assert page.evaluate("() => typeof window.GM_setValue") == "undefined"
    else:
        assert page.evaluate("() => [typeof window.GM_registerMenuCommand, typeof window.GM_setValue]") == [
            "undefined", "undefined"]
    page.close()


def test_userscript_dead_branches_from_the_audit_are_gone() -> None:
    """审查第 4 节的三个死分支已删，删前逐个核实没有写入方 / 调用方：

    - `data-pixiv-rescue="badge"`：脚本全部 33 个历史版本里没有一个写过这个属性值（a486f1b 引入
      这两处检查时徽标只有 class，0.5.0 连徽标都删了），检查永远不成立。
    - rescueErrorText 的「章节级」文案：调用点全都传 pageLevel=true。
    - 「这一页没有拯救系列」：/api/rescue/v1/series 在同一个读事务里计数、取页，页码钳到末页，
      total > 0 时所在页必有条目（已部署的 0c7578e 也是如此）。
    文案可见的那几条（阅读层错误、目录错误、空状态）由各自的页面用例照常核对。
    """
    script = _script()
    assert '="badge"' not in script
    for removed in ("rescueErrorText(error, true)", "rescueErrorText(error, pageLevel)", "pageLevel ?",
                    "'备份里没有这一章，目录还在。'", "'这一章没有读到，目录还在。'", "'这一页没有拯救系列'"):
        assert removed not in script, removed
    assert "function rescueErrorText(error) {" in script

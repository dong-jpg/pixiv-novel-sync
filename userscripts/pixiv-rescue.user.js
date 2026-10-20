// ==UserScript==
// @name         Pixiv 小说私人备份救援阅读
// @namespace    https://pixiv.dongboapp.com/
// @version      0.4.1
// @description  在 Pixiv 小说/系列明细页及收藏/追更列表页识别失效内容，从私人备份读取并标记救援内容；收藏页提供「拯救系列」tab。
// @author       dong-jpg
// @license      MIT
// @homepageURL  https://github.com/dong-jpg/pixiv-novel-sync/tree/main
// @supportURL   https://github.com/dong-jpg/pixiv-novel-sync/issues
// @downloadURL  https://raw.githubusercontent.com/dong-jpg/pixiv-novel-sync/main/userscripts/pixiv-rescue.user.js
// @updateURL    https://raw.githubusercontent.com/dong-jpg/pixiv-novel-sync/main/userscripts/pixiv-rescue.user.js
// @match        https://www.pixiv.net/novel/show.php*
// @match        https://www.pixiv.net/novel/series/*
// @match        https://www.pixiv.net/users/*/bookmarks/novels*
// @match        https://www.pixiv.net/following/watchlist/novels*
// @match        https://www.pixiv.net/novel/bookmark_new.php*
// @match        https://www.pixiv.net/*/novel/show.php*
// @match        https://www.pixiv.net/*/novel/series/*
// @match        https://www.pixiv.net/*/users/*/bookmarks/novels*
// @match        https://www.pixiv.net/*/following/watchlist/novels*
// @match        https://www.pixiv.net/*/novel/bookmark_new.php*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect     pixiv.dongboapp.com
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  const DEFAULT_API_ORIGIN = 'https://pixiv.dongboapp.com';
  function apiOrigin() {
    const custom = String(GM_getValue('pixivRescueOrigin', '') || '').trim();
    return (custom || DEFAULT_API_ORIGIN).replace(/\/$/, '');
  }
  const TOKEN_KEY = 'pixivRescueToken';
  const ROOT_ATTRIBUTE = 'data-pixiv-rescue';
  const UNAVAILABLE_MARKERS = [
    'この作品は削除されています',
    'この作品は閲覧できません',
    '作品が見つかりません',
    'このシリーズは削除されています',
    'シリーズが見つかりません',
    '该作品已被删除',
    '该作品无法浏览',
    '作品不存在',
    'This work has been deleted',
    'Page not found',
    // 列表页（收藏/追更）白块的实际文案变体
    '削除されたか、存在しなくなっています',
    'この作品は非公開',
    '非公開の作品',
    '作品の削除',
    '該当作品は削除',
    '此作品已删除',
    '该作品不可见',
    // 2026-10-20 生产实测（手机端 lang=zh 收藏页）：失效卡片的实际文案是
    // 「该作品无法显示」。旧列表缺少这六个字，卡片识别全部落空——这正是
    // 「页面上什么都没有」的根因。DOM 探针见 docs/AUDIT_REPORT_2026-10-20.md。
    '该作品无法显示'
  ];
  // 列表页白块的判定阈值：Pixiv 失效卡片只有一句提示文案，健康卡片至少有
  // 标题+作者+标签。放到 64 字仍能排除几乎所有健康卡片。
  const BROKEN_CARD_TEXT_LIMIT = 64;

  // 请求失败提示条：旧版失败完全静默（Token 缺失/失效/网络错误都只是 reject
  // 后被吞），用户无从知道「为什么没有救援标记」。同一种错误只提示一次，
  // 不随每轮扫描刷屏。
  let lastNoticeKey = null;
  // Token 探测只做一次：没识别到失效卡片时用一次 lookup 判断 Token 配置，
  // 避免「卡片识别失败」与「脚本没运行」在用户眼里无法区分。
  let tokenProbed = false;
  function showRescueNotice(kind) {
    const messages = {
      missing_token: '救援脚本未设置 Token：点 Tampermonkey 菜单「设置或更新救援 Token」后刷新页面',
      unauthorized: '救援 Token 无效：请到网站设置页轮换 Token 后重新填写',
      network_error: '无法连接救援服务，救援标记暂不可用',
      network_timeout: '救援服务响应超时，救援标记暂不可用',
      server_error: '救援服务暂时出错，救援标记暂不可用'
    };
    if (lastNoticeKey === kind) return;
    if (document.querySelector('[data-pixiv-rescue-notice]')) return;
    lastNoticeKey = kind;
    addStyles();
    const bar = element('div', 'pixiv-rescue-notice-bar', messages[kind] || '救援标记暂不可用');
    bar.setAttribute('data-pixiv-rescue-notice', '');
    const close = element('button', 'pixiv-rescue-button', '知道了');
    close.type = 'button';
    close.addEventListener('click', function () { bar.remove(); });
    bar.append(close);
    const host = document.querySelector('main') || document.body;
    host.prepend(bar);
  }

  // ── 手机端兼容 ─────────────────────────────────────────────
  // 部分手机脚本管理器（X浏览器/Via 等）对 GM API 支持不齐：缺 GM_xmlhttpRequest
  // 时 apiGet 一调用就抛 TypeError，整个脚本静默死亡——页面什么都没有，用户
  // 无从排查。这里在启动最前面做能力检测并给出可见提示。
  function checkRuntimeSupport() {
    const missing = [];
    if (typeof GM_getValue !== 'function') missing.push('GM_getValue');
    if (typeof GM_xmlhttpRequest !== 'function') missing.push('GM_xmlhttpRequest');
    if (missing.length) {
      const bar = document.createElement('div');
      bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;padding:10px 14px;background:#fef2f2;color:#991b1b;font:13px/1.6 -apple-system,sans-serif;border-bottom:1px solid #fca5a5';
      bar.textContent = '救援脚本需要脚本管理器支持 ' + missing.join('、') +
        '。当前浏览器/管理器未提供，救援功能不可用。请换用支持完整 GM API 的管理器（如 Tampermonkey）。';
      (document.body || document.documentElement).prepend(bar);
      return false;
    }
    // GM_registerMenuCommand 缺失不致命（只是没有菜单入口，Token 还能通过
    // 管理器自带的方式设置），但不做兜底会在 registerTokenMenus 里抛错、
    // 让后面所有初始化（包括页面处理）全部不执行——这同样是「页面零反应」
    // 的一种来源。降级为无菜单模式继续运行。
    if (typeof GM_registerMenuCommand !== 'function') {
      window.GM_registerMenuCommand = function () {};
    }
    if (typeof GM_setValue !== 'function') {
      window.GM_setValue = function () {};
    }
    return true;
  }

  function registerTokenMenus() {
    GM_registerMenuCommand('设置或更新救援 Token', function () {
      const value = window.prompt('请输入救援 API Token。Token 只会保存在油猴脚本存储中。', '');
      if (value === null) return;
      const token = String(value).trim();
      if (!token) {
        window.alert('Token 不能为空。');
        return;
      }
      GM_setValue(TOKEN_KEY, token);
      window.alert('救援 Token 已保存。');
    });
    GM_registerMenuCommand('清除救援 Token', function () {
      GM_setValue(TOKEN_KEY, '');
      window.alert('救援 Token 已清除。');
    });
  }

  function apiGet(path) {
    const token = String(GM_getValue(TOKEN_KEY, '') || '').trim();
    if (!token) return Promise.reject(new Error('missing_token'));
    return new Promise(function (resolve, reject) {
      GM_xmlhttpRequest({
        method: 'GET',
        url: apiOrigin() + path,
        headers: {
          Authorization: 'Bearer ' + token,
          Accept: 'application/json'
        },
        timeout: 15000,
        onload: function (response) {
          let envelope;
          try {
            envelope = JSON.parse(response.responseText || '{}');
          } catch (_error) {
            reject(new Error('invalid_response'));
            return;
          }
          if (response.status < 200 || response.status >= 300 || envelope.ok === false) {
            const status = Number(response.status || 0);
            if (status === 401) reject(new Error('unauthorized'));
            else if (status === 404) reject(new Error('not_found'));
            else if (status === 429) reject(new Error('rate_limited'));
            else if (status >= 500) reject(new Error('server_error'));
            else reject(new Error('request_failed'));
            return;
          }
          resolve(envelope.data || {});
        },
        onerror: function () {
          reject(new Error('network_error'));
        },
        ontimeout: function () {
          reject(new Error('network_timeout'));
        }
      });
    });
  }

  function pageText() {
    return String(document.body && document.body.textContent || '');
  }

  function pageSaysUnavailable() {
    const nodes = document.querySelectorAll(
      '[class*="error"], [class*="Error"], .error-message, main h1, main h2'
    );
    const text = Array.from(nodes).map(function (node) {
      return String(node.textContent || '');
    }).join('\n');
    return UNAVAILABLE_MARKERS.some(function (marker) {
      return text.includes(marker);
    });
  }

  function hasSubstantialText(node) {
    return Boolean(node && String(node.textContent || '').trim().length >= 12);
  }

  function isNovelPageHealthy() {
    if (pageSaysUnavailable()) return false;
    const selectors = [
      '.novel-text',
      '[data-gtm-value="novel-text"]',
      '[class*="NovelText"]',
      'article'
    ];
    return selectors.some(function (selector) {
      return Array.from(document.querySelectorAll(selector)).some(hasSubstantialText);
    });
  }

  function isSeriesPageHealthy() {
    if (pageSaysUnavailable()) return false;
    const chapterLinks = document.querySelectorAll('a[href*="/novel/show.php?id="]');
    if (chapterLinks.length > 0) return true;
    return Array.from(document.querySelectorAll('[class*="NovelSeries"], [class*="Series"]'))
      .some(hasSubstantialText);
  }

  function waitForUnavailableState(healthyCheck) {
    const deadline = Date.now() + 1200;
    return new Promise(function (resolve) {
      function inspect() {
        if (healthyCheck()) {
          resolve(false);
          return;
        }
        if (pageSaysUnavailable()) {
          resolve(true);
          return;
        }
        if (Date.now() >= deadline) {
          resolve(false);
          return;
        }
        window.setTimeout(inspect, 100);
      }
      inspect();
    });
  }

  function addStyles() {
    if (document.querySelector('style[data-pixiv-rescue-styles]')) return;
    const style = document.createElement('style');
    style.setAttribute('data-pixiv-rescue-styles', '');
    style.textContent = [
      '.pixiv-rescue-root{box-sizing:border-box;max-width:760px;margin:24px auto;padding:0 16px;color:#1f2937;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}',
      '.pixiv-rescue-panel{border:2px solid #dc2626;border-radius:8px;background:#fff;padding:20px;box-shadow:0 8px 24px rgba(17,24,39,.12)}',
      '.pixiv-rescue-kicker{display:inline-flex;align-items:center;padding:4px 8px;border-radius:4px;background:#dc2626;color:#fff;font-size:12px;font-weight:700}',
      '.pixiv-rescue-title{margin:12px 0 4px;font-size:24px;line-height:1.4;font-weight:700;color:#111827}',
      '.pixiv-rescue-meta{margin:0 0 16px;color:#6b7280;font-size:13px;line-height:1.6}',
      '.pixiv-rescue-notice{margin:12px 0;padding:10px 12px;border-left:3px solid #dc2626;background:#fef2f2;color:#991b1b;font-size:13px;line-height:1.6}',
      '.pixiv-rescue-body{white-space:pre-wrap;overflow-wrap:anywhere;font-family:"Noto Serif SC","Yu Mincho",serif;font-size:16px;line-height:2;color:#374151}',
      '.pixiv-rescue-button{display:inline-flex;align-items:center;justify-content:center;min-height:36px;margin:4px 8px 4px 0;padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;background:#fff;color:#374151;font-size:14px;cursor:pointer}',
      '.pixiv-rescue-button:hover{border-color:#2563eb;color:#1d4ed8;background:#eff6ff}',
      '.pixiv-rescue-button:disabled{cursor:wait;opacity:.55}',
      '.pixiv-rescue-directory{margin:16px 0 0;padding:0;list-style:none;border-top:1px solid #e5e7eb}',
      '.pixiv-rescue-directory li{padding:8px 0;border-bottom:1px solid #f3f4f6}',
      '.pixiv-rescue-viewer{margin-top:18px;padding-top:18px;border-top:1px solid #d1d5db}',
      '.pixiv-rescue-error{max-width:760px;margin:16px auto;padding:10px 14px;border:1px solid #fca5a5;border-radius:6px;background:#fef2f2;color:#991b1b;font-size:13px}',
      '@media(max-width:640px){.pixiv-rescue-root{margin:12px auto;padding:0 10px}.pixiv-rescue-panel{padding:16px}.pixiv-rescue-title{font-size:20px}}',
      // 徽标是绝对定位角标：失效卡是 grid 布局，旧版把徽标当普通子元素 prepend，
      // 会被吸进第一个网格单元与标题文字重叠。脱离布局流浮在卡片右上角。
      // 注意：这个数组经 join('') 拼接，注释必须保持在字符串元素之间的合法
      // JS 位置——否则注释会吞掉 join 的逗号让前后两条 CSS 粘连成废规则
      // （0.3.8 的 position:absolute 就这么失效过一次）。
      '.pixiv-rescue-card-badge{position:relative;z-index:10;margin:6px 8px;padding:5px 8px;border-radius:4px;background:#dc2626;color:#fff;font-size:12px;font-weight:700;line-height:1.4;overflow-wrap:anywhere}',
      '.pixiv-rescue-card-badge::after{content:"";display:block}',
      // 宿主容器需要 position 才能让角标锚定：works-item/li 默认静态。
      // 注意：数组里的字符串元素是 CSS，行内注释照上一条说明保持在逗号之间。
      'div.works-item[data-pixiv-rescue="card"]{position:relative}',
      'li[data-pixiv-rescue="card"]{position:relative}',
      'div.works-item[data-pixiv-rescue="card-pending"]{position:relative}',
      'li[data-pixiv-rescue="card-pending"]{position:relative}',
      '.pixiv-rescue-tab-link{display:inline-flex;align-items:center;min-height:32px;margin:0 6px;padding:6px 12px;border-bottom:2px solid transparent;color:#333;font-size:14px;text-decoration:none;cursor:pointer}',
      '.pixiv-rescue-tab-link.pixiv-rescue-tab-active{border-bottom-color:#dc2626;color:#dc2626;font-weight:700}',
      '.pixiv-rescue-tab{max-width:1200px;margin:16px auto;padding:0 16px}',
      '.pixiv-rescue-tab-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}',
      '.pixiv-rescue-tab-card{display:flex;flex-direction:column;gap:6px;padding:12px;border:1px solid #e5e7eb;border-radius:8px;background:#fff}',
      '.pixiv-rescue-tab-title{font-size:15px;font-weight:700;color:#111827;line-height:1.4;overflow-wrap:anywhere}',
      '.pixiv-rescue-tab-meta{font-size:12px;color:#6b7280;line-height:1.6}',
      '.pixiv-rescue-tab-pager{margin:12px 0;text-align:center}',
      '.pixiv-rescue-notice-bar{display:flex;align-items:center;gap:12px;justify-content:center;max-width:760px;margin:12px auto;padding:10px 14px;border:1px solid #fca5a5;border-radius:6px;background:#fef2f2;color:#991b1b;font-size:13px;line-height:1.6}'
    ].join('');
    document.head.append(style);
  }

  function element(tagName, className, text) {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clearNode(node) {
    while (node.firstChild) node.firstChild.remove();
  }

  function getRoot() {
    let root = document.querySelector('[' + ROOT_ATTRIBUTE + '="content"]');
    if (root) return root;
    root = element('section', 'pixiv-rescue-root');
    root.setAttribute(ROOT_ATTRIBUTE, 'content');
    const target = document.querySelector('main') || document.body;
    target.append(root);
    return root;
  }

  function renderText(parent, text) {
    const body = element('div', 'pixiv-rescue-body');
    body.textContent = String(text || '');
    parent.append(body);
    return body;
  }

  function renderNotice(panel, data) {
    panel.append(element(
      'div',
      'pixiv-rescue-notice',
      data.source_notice || '内容来自私人备份，并非 Pixiv 官方恢复'
    ));
  }

  function renderNovel(data) {
    addStyles();
    const root = getRoot();
    clearNode(root);
    const panel = element('div', 'pixiv-rescue-panel');
    panel.append(element('span', 'pixiv-rescue-kicker', '拯救数据'));
    panel.append(element('h1', 'pixiv-rescue-title', data.title || '未命名备份小说'));
    const author = data.author_name ? '作者：' + data.author_name : '';
    panel.append(element('p', 'pixiv-rescue-meta', author));
    renderNotice(panel, data);
    renderText(panel, data.text_raw || '备份正文为空');
    root.append(panel);
  }

  function renderError(error) {
    addStyles();
    const root = getRoot();
    clearNode(root);
    root.append(element('div', 'pixiv-rescue-error', rescueErrorText(error, true)));
  }

  function rescueErrorText(error, pageLevel) {
    const messages = {
      missing_token: '未设置救援 Token，请通过油猴菜单完成设置。',
      unauthorized: '救援 Token 无效，请在网站设置页轮换后重新填写。',
      not_found: pageLevel ? '未找到可用的救援数据，Pixiv 原页面已保留。' : '备份里没有这一章，目录还在。',
      rate_limited: '请求太频繁，请稍后再试。',
      server_error: '救援服务暂时出错，请稍后再试。',
      network_timeout: '救援服务响应超时，Pixiv 原页面已保留。',
      network_error: '无法连接救援服务，Pixiv 原页面已保留。'
    };
    return messages[error && error.message] || (pageLevel
      ? '未找到可用的救援数据，Pixiv 原页面已保留。'
      : '这一章没有读到，目录还在。');
  }

  function renderChapterError(viewer, error) {
    let note = viewer.querySelector('.pixiv-rescue-chapter-error');
    if (!note) {
      note = element('div', 'pixiv-rescue-error pixiv-rescue-chapter-error', '');
      viewer.prepend(note);
    }
    note.textContent = rescueErrorText(error, false);
  }

  // ── 收藏页 / 追更页列表匹配 ──────────────────────────────────

  // Pixiv 非日语界面会给路径加语言前缀（/en/、/zh/ 等）。统一在这里剥掉，
  // 各页判定只写无前缀形态，避免每处正则都要枚举语言。
  function pathWithoutLangPrefix() {
    const path = window.location.pathname;
    // 命中语言前缀时保留开头的「/」：剥的是 /en/ 段而不是整个前缀串
    const m = path.match(/^\/(en|zh|ja|ko|fr|de|it|es|pt|ru|th|vi|id|ms|pl|hu|nl|sv|da|fi|no|tr|ar|he|fa|hi|bn|ta|tl|uk|el|ro|bg|sr|hr|sk|sl|lt|lv|et|ka|az|kk|uz|mn|my|km|lo|si|ne)(\/|$)/);
    if (!m) return path;
    const rest = path.slice(m[1].length + 1); // 去掉「/语言」
    return rest.startsWith('/') ? rest : '/' + rest;
  }

  function isBookmarksListPage() {
    return /^\/users\/\d+\/bookmarks\/novels/.test(pathWithoutLangPrefix());
  }

  function isWatchlistPage() {
    return pathWithoutLangPrefix() === '/following/watchlist/novels';
  }

  function climbToCard(node) {
    // 列表卡片容器：手机端生产实测是 div.works-item（不是 li）。先锚定已知
    // 容器类，退回 li，最后才按「文本仍只有失效提示」向上爬。
    const card = node.closest('div.works-item, li');
    if (card && card !== document.body) return card;
    let current = node;
    for (let i = 0; i < 5 && current.parentElement; i += 1) {
      current = current.parentElement;
      if (String(current.textContent || '').trim().length > BROKEN_CARD_TEXT_LIMIT * 3) break;
    }
    return current;
  }

  function elementMatchesUnavailable(node) {
    if (!node) return false;
    const text = String(node.textContent || '').trim();
    if (!text || text.length > BROKEN_CARD_TEXT_LIMIT) return false;
    return UNAVAILABLE_MARKERS.some(function (marker) {
      return text.includes(marker);
    });
  }

  function cardIsBroken(card) {
    // 先看链接元素本身；Pixiv 的卡片结构是嵌套的，提示文案可能在链接的
    // 祖先容器里（白块卡片整体替换掉了标题/封面区），所以再向上查四层。
    // 层数依据 2026-10-20 手机端生产 DOM 实测：文案在 a.outer-router-link 内
    // 第 3 层 div（f-title-xs → novel-info → works-item-novel-content → a），
    // 旧版只查两层导致「该作品无法显示」永远识别不到。
    const targets = [card];
    let node = card;
    for (let i = 0; i < 4 && node && node.parentElement; i += 1) {
      node = node.parentElement;
      targets.push(node);
    }
    for (const target of targets) {
      if (elementMatchesUnavailable(target)) return true;
      // Pixiv 官方的「不可见作品」容器类名：比文案匹配更稳定的结构信号
      if (typeof target.className === 'string' && target.className.includes('invisible-work-container')) return true;
    }
    return false;
  }

  function collectBrokenCards() {
    const novelIds = new Set();
    const seriesIds = new Set();
    const broken = [];

    function register(card, id, type) {
      // 标记为 pending 而非终态：查询失败时会撤销（见 markBrokenCards 的 catch），
      // 下一轮扫描允许重试。A14-03 的根因就是这里先写了永久标记。
      card.setAttribute(ROOT_ATTRIBUTE, 'card-pending');
      broken.push({ type: type, id: id, card: card });
      if (type === 'novel') novelIds.add(id);
      else if (type === 'series') seriesIds.add(id);
      else {
        // 无链接的白块拿不到类型：两头都查，命中哪个算哪个
        novelIds.add(id);
        seriesIds.add(id);
      }
    }

    // 卡片已被处理过（终态 card / 处理中 card-pending）都跳过，等待中的卡片由
    // 上一轮的回调负责收尾——pending 挂太久（请求超时等）会由 catch 撤销。
    function alreadyHandled(card) {
      const state = card.getAttribute(ROOT_ATTRIBUTE);
      return state === 'card' || state === 'card-pending';
    }

    // 1) 有链接的失效卡片。去重键是共享容器（div.works-item / li）而不是链接
    // 自身：手机端生产实测同一失效卡有两个嵌套 <a>（outer-router-link 外层 +
    // 内层，href 相同），旧版在链接上打标记，第二个链接不认第一个，同一张卡
    // 注册两次、两个「已备份」徽标叠在同一张卡上（用户看到的挤压重影）。
    const handledCards = new Set();
    document.querySelectorAll('a[href*="/novel/show.php?id="], a[href*="/novel/series/"]').forEach(function (link) {
      const card = link.closest('div.works-item, li') || link;
      if (handledCards.has(card)) return;
      if (alreadyHandled(card)) { handledCards.add(card); return; }
      if (card.querySelector('[' + ROOT_ATTRIBUTE + '="badge"]')) { handledCards.add(card); return; }
      if (!cardIsBroken(link)) return;
      const href = link.getAttribute('href') || '';
      const seriesMatch = href.match(/\/novel\/series\/(\d+)/);
      const novelMatch = href.match(/[?&]id=(\d+)/);
      if (seriesMatch) { handledCards.add(card); register(card, seriesMatch[1], 'series'); }
      else if (novelMatch) { handledCards.add(card); register(card, novelMatch[1], 'novel'); }
    });

    // 2) 无链接的白块：系列被删/隐藏时 Pixiv 不再渲染链接，但卡片上通常还留着
    //    data-gtm-value（作品 ID）之类的数据属性（列表项本身还没从收藏数据里消失）。
    document.querySelectorAll('[data-gtm-value]').forEach(function (node) {
      const value = String(node.getAttribute('data-gtm-value') || '').trim();
      if (!/^\d{3,12}$/.test(value)) return;
      const card = climbToCard(node);
      if (!card || card === document.body) return;
      if (alreadyHandled(card)) return;
      if (card.querySelector('a[href*="/novel/show.php?id="], a[href*="/novel/series/"]')) return;
      if (card.querySelector('[' + ROOT_ATTRIBUTE + '="badge"]')) return;
      // 卡片本身（或其附近文本）必须就是失效提示，避免把健康卡片的 gtm 值也抓进来
      if (!cardIsBroken(card) && !elementMatchesUnavailable(card)) return;
      register(card, value, 'unknown');
    });

    return { broken: broken, novelIds: novelIds, seriesIds: seriesIds };
  }

  // /api/rescue/v1/lookup 的单次上限：novel_ids + series_ids 合计 40（A14-04）
  const LOOKUP_BATCH_LIMIT = 40;

  function buildLookupPath(novelIds, seriesIds) {
    const parts = [];
    if (novelIds.size) parts.push('novel_ids=' + encodeURIComponent(Array.from(novelIds).join(',')));
    if (seriesIds.size) parts.push('series_ids=' + encodeURIComponent(Array.from(seriesIds).join(',')));
    return '/api/rescue/v1/lookup?' + parts.join('&');
  }

  // 按 novel+series 合计 ≤40 切批；返回的每批是 {entries, novelIds, seriesIds}。
  // unknown 类型一张卡占 2 个槽位（novel+series 各一），必须在加入前判断是否
  // 放得下——先加后查会让单批到 41，服务端 400 后陷入「撤销-重试」循环。
  // 单批失败只影响该批的卡片，下一轮扫描重试（见 markBrokenCards 的 catch）。
  function buildLookupBatches(broken) {
    const batches = [];
    let current = { entries: [], novelIds: new Set(), seriesIds: new Set() };
    function closeBatch() {
      if (current.entries.length) batches.push(current);
      current = { entries: [], novelIds: new Set(), seriesIds: new Set() };
    }
    broken.forEach(function (entry) {
      const novelNew = (entry.type === 'novel' || entry.type === 'unknown') && !current.novelIds.has(entry.id) ? 1 : 0;
      const seriesNew = (entry.type === 'series' || entry.type === 'unknown') && !current.seriesIds.has(entry.id) ? 1 : 0;
      if (current.novelIds.size + current.seriesIds.size + novelNew + seriesNew > LOOKUP_BATCH_LIMIT) {
        closeBatch();
      }
      current.entries.push(entry);
      if (novelNew) current.novelIds.add(entry.id);
      if (seriesNew) current.seriesIds.add(entry.id);
    });
    closeBatch();
    return batches;
  }

  function markBrokenCards() {
    const found = collectBrokenCards();
    if (!found.broken.length) {
      // 没识别到失效卡片 ≠ 一切正常：Token 缺失时旧版在这里静默 return，
      // 用户永远看不到配置提示。用一次轻量 lookup 探测 Token 状态（只发一次）。
      if (!tokenProbed) {
        tokenProbed = true;
        apiGet('/api/rescue/v1/lookup?novel_ids=1').catch(function (error) {
          const kind = error && error.message;
          if (kind === 'missing_token' || kind === 'unauthorized' ||
              kind === 'network_error' || kind === 'network_timeout' || kind === 'server_error') {
            showRescueNotice(kind);
          }
        });
      }
      return;
    }
    buildLookupBatches(found.broken).forEach(function (batch) {
      apiGet(buildLookupPath(batch.novelIds, batch.seriesIds)).then(function (data) {
        const novels = {};
        (data.novels || []).forEach(function (item) { novels[item.novel_id] = item; });
        const series = {};
        (data.series || []).forEach(function (item) { series[item.series_id] = item; });
        batch.entries.forEach(function (entry) {
          const novelMatch = novels[entry.id];
          const seriesMatch = series[entry.id];
          const match = entry.type === 'series' ? seriesMatch
            : (entry.type === 'novel' ? novelMatch : (seriesMatch || novelMatch));
          // 这批已成功应答：卡片转终态，不再重复查询（无备份的也转，避免每轮空查）
          entry.card.setAttribute(ROOT_ATTRIBUTE, 'card');
          if (!match) return;
          // 有备份时直接替换失效占位文案：「该作品无法显示」横贯整卡
          // （316/412px），角标无处安放必与它重叠——替换成信息量更大的
          // 徽标（含原作标题 + 救援入口提示），无备份时保持 Pixiv 原样。
          const badge = element('div', 'pixiv-rescue-card-badge');
          badge.textContent = '已备份：' + (match.title || '未命名') +
            (match.personal_hold ? '' : '（本人已取消收藏/追更）');
          const placeholder = Array.from(entry.card.querySelectorAll('div, span, p'))
            .find(function (el) {
              return el.childElementCount === 0 && elementMatchesUnavailable(el);
            });
          if (placeholder && placeholder.parentElement) {
            placeholder.replaceWith(badge);
          } else {
            // 找不到占位文案（结构变化）：退回角标浮层，容器补 relative
            const cardStyle = window.getComputedStyle(entry.card);
            if (cardStyle.position === 'static') {
              entry.card.style.position = 'relative';
            }
            entry.card.prepend(badge);
          }
        });
      }).catch(function (error) {
        // 失败撤销 pending 标记：下一轮扫描（3 秒后）会对这些卡片重试。
        // A14-03 的根因是旧版在查询前就写永久标记且失败不撤销。
        batch.entries.forEach(function (entry) {
          if (entry.card.getAttribute(ROOT_ATTRIBUTE) === 'card-pending') {
            entry.card.removeAttribute(ROOT_ATTRIBUTE);
          }
        });
        // 不再静默：Token 缺失/失效、网络故障给出可见提示（同种错误只提示一次）
        const kind = error && error.message;
        if (kind === 'missing_token' || kind === 'unauthorized' ||
            kind === 'network_error' || kind === 'network_timeout' || kind === 'server_error') {
          showRescueNotice(kind);
        }
      });
    });
  }

  function handleListPage() {
    if (!isBookmarksListPage() && !isWatchlistPage()) return;
    // 徽标/提示条的样式只在明细页与错误路径注入过，列表页此前从不调
    // addStyles——徽标 CSS 根本没进页面，角标定位全部失效（0.3.8 实测
    // pos:static 的根因）。
    addStyles();
    markBrokenCards();
    // SPA 切页会重复进入本函数：interval 只注册一次，避免叠加扫描（泄漏修复）
    if (!window.__pixivRescueListInterval) {
      window.__pixivRescueListInterval = window.setInterval(markBrokenCards, 3000);
    }
  }

  // ── 收藏页（bookmark_new.php）「拯救系列」tab ─────────────────

  function isNovelBookmarksPage() {
    return pathWithoutLangPrefix() === '/novel/bookmark_new.php';
  }

  function renderSeriesCard(item) {
    const card = element('div', 'pixiv-rescue-tab-card');
    const title = element('div', 'pixiv-rescue-tab-title', item.title || '未命名系列');
    const meta = element('div', 'pixiv-rescue-tab-meta');
    const coverage = item.complete_count !== undefined
      ? '已备份 ' + (item.complete_count ?? '?') + ' / ' + (item.expected_count ?? '?') + ' 章'
      : '';
    const stateText = item.rescue_state === 'success' ? '完整救援' : '部分救援';
    const holdText = item.personal_hold ? '仍在追更' : '已取消追更';
    meta.textContent = [coverage, stateText, holdText, item.author_name || '']
      .filter(Boolean).join(' · ');
    const open = element('button', 'pixiv-rescue-button', '查看章节');
    open.type = 'button';
    open.addEventListener('click', function () {
      // 系列本体在 Pixiv 已打不开；把 ID 塞进系列页 URL，让 handleSeriesPage
      // 的「不可用 → 救援」管线接管渲染。
      window.location.href = 'https://www.pixiv.net/novel/series/' + encodeURIComponent(String(item.series_id));
    });
    card.append(title, meta, open);
    return card;
  }

  function renderRescueSeriesTabBody(container) {
    clearNode(container);
    const grid = element('div', 'pixiv-rescue-tab-grid');
    container.append(grid);
    let currentPage = 1;
    let hasMore = false;

    function loadPage(page) {
      const loadingNote = element('div', 'pixiv-rescue-tab-meta', '正在加载拯救系列…');
      grid.append(loadingNote);
      apiGet('/api/rescue/v1/series?page=' + encodeURIComponent(String(page)) + '&page_size=24')
        .then(function (data) {
          loadingNote.remove();
          // 追加而不是清空：旧版 clearNode(grid) 会把上一页卡片全部丢弃，
          // 「加载更多」实际变成「跳到下一页」，已看的系列在 tab 会话内无法回看。
          const items = Array.isArray(data.items) ? data.items : [];
          items.forEach(function (item) {
            grid.append(renderSeriesCard(item));
          });
          if (!items.length && !grid.children.length) {
            grid.append(element('div', 'pixiv-rescue-tab-meta', '没有可展示的拯救系列。'));
          }
          currentPage = Math.max(currentPage, Math.max(1, Number(data.page || page)));
          const totalPages = Math.max(currentPage, Number(data.total_pages || currentPage));
          hasMore = currentPage < totalPages;
          const pager = container.querySelector('[data-rescue-tab-pager]');
          if (pager) pager.remove();
          if (hasMore) {
            const pager = element('div', 'pixiv-rescue-tab-pager');
            pager.setAttribute('data-rescue-tab-pager', '');
            const more = element('button', 'pixiv-rescue-button', '加载更多');
            more.type = 'button';
            more.addEventListener('click', function () {
              more.disabled = true;
              loadPage(currentPage + 1);
            });
            pager.append(more);
            container.append(pager);
          }
        })
        .catch(function (error) {
          loadingNote.remove();
          if (!grid.children.length) {
            grid.append(element('div', 'pixiv-rescue-error', rescueErrorText(error, true)));
          } else {
            // 已有卡片时只在 pager 位置提示，不清空已加载内容
            grid.append(element('div', 'pixiv-rescue-error', rescueErrorText(error, true)));
          }
        });
    }

    loadPage(1);
  }

  function injectRescueSeriesTab() {
    // Pixiv 的 tab 区是若干 <a>（收藏/关注的新作分栏）。找到它，在其后追加一个
    // 「拯救系列」入口；找不到就退化为页面顶部插入按钮。
    const tabs = document.querySelectorAll('nav a, [role="tablist"] a, section a[href*="bookmark_new.php"]');
    let tabHost = null;
    tabs.forEach(function (tab) {
      if (tab.textContent && tab.textContent.includes('小説') && !tabHost) tabHost = tab.parentElement;
    });
    if (!tabHost) {
      // 兜底：找页面上第一个 h1/h2 附近
      const heading = document.querySelector('h1, h2');
      tabHost = heading ? heading.parentElement : document.querySelector('main') || document.body;
    }
    if (!tabHost || document.querySelector('[' + ROOT_ATTRIBUTE + '="tab"]')) return;

    const tabLink = element('a', 'pixiv-rescue-tab-link', '拯救系列');
    tabLink.setAttribute(ROOT_ATTRIBUTE, 'tab');
    tabLink.setAttribute('href', '#pixiv-rescue-series');
    tabHost.append(tabLink);

    const container = element('section', 'pixiv-rescue-tab');
    container.setAttribute(ROOT_ATTRIBUTE, 'tab-body');
    container.style.display = 'none';

    let loaded = false;
    tabLink.addEventListener('click', function (event) {
      event.preventDefault();
      const active = container.style.display === 'none';
      container.style.display = active ? '' : 'none';
      tabLink.classList.toggle('pixiv-rescue-tab-active', active);
      if (active && !loaded) {
        loaded = true;
        renderRescueSeriesTabBody(container);
      }
    });

    const mountPoint = tabHost.closest('section, div') || tabHost.parentElement;
    if (mountPoint && mountPoint !== document.body) {
      mountPoint.parentElement.insertBefore(container, mountPoint.nextSibling);
    } else {
      (document.querySelector('main') || document.body).append(container);
    }
  }

  function handleNovelBookmarksPage() {
    if (!isNovelBookmarksPage()) return;
    injectRescueSeriesTab();
  }

  function renderSeriesChapter(viewer, data) {
    clearNode(viewer);
    viewer.append(element('h2', 'pixiv-rescue-title', data.title || '未命名章节'));
    renderNotice(viewer, data);
    renderText(viewer, data.text_raw || '备份正文为空');
  }

  function renderChapterDirectory(panel, viewer, data, seriesId) {
    let list = panel.querySelector('.pixiv-rescue-directory');
    if (!list) {
      list = element('ol', 'pixiv-rescue-directory');
      panel.insertBefore(list, viewer);
    }
    const existingMore = list.querySelector('[data-rescue-load-more]');
    if (existingMore) existingMore.remove();
    const chapters = Array.isArray(data.items) ? data.items : [];
    chapters.forEach(function (chapter, index) {
      const item = element('li');
      const button = element('button', 'pixiv-rescue-button', chapter.title || '第 ' + (index + 1) + ' 章');
      button.type = 'button';
      button.addEventListener('click', function () {
        button.disabled = true;
        apiGet('/api/rescue/v1/novels/' + encodeURIComponent(String(chapter.novel_id)))
          .then(function (novel) {
            renderSeriesChapter(viewer, novel);
          })
          .catch(function (error) {
            renderChapterError(viewer, error);
          })
          .finally(function () {
            button.disabled = false;
          });
      });
      item.append(button);
      list.append(item);
    });
    if (!chapters.length && !list.children.length) {
      list.append(element('li', '', '备份中没有可读取的章节。'));
    }

    const currentPage = Math.max(1, Number(data.page || 1));
    const totalPages = Math.max(currentPage, Number(data.total_pages || currentPage));
    if (currentPage < totalPages) {
      const moreItem = element('li');
      moreItem.setAttribute('data-rescue-load-more', '');
      const moreButton = element('button', 'pixiv-rescue-button', '加载更多章节');
      moreButton.type = 'button';
      moreButton.addEventListener('click', function () {
        moreButton.disabled = true;
        const nextPage = currentPage + 1;
        const path = '/api/rescue/v1/series/' + encodeURIComponent(String(seriesId))
          + '/chapters?page=' + encodeURIComponent(String(nextPage)) + '&page_size=100';
        apiGet(path)
          .then(function (nextDirectory) {
            renderChapterDirectory(panel, viewer, nextDirectory, seriesId);
          })
          .catch(renderError)
          .finally(function () {
            moreButton.disabled = false;
          });
      });
      moreItem.append(moreButton);
      list.append(moreItem);
    }
  }

  function renderSeries(data, seriesId) {
    addStyles();
    const root = getRoot();
    clearNode(root);
    const panel = element('div', 'pixiv-rescue-panel');
    panel.append(element('span', 'pixiv-rescue-kicker', '拯救数据'));
    panel.append(element('h1', 'pixiv-rescue-title', data.title || '未命名备份系列'));
    const coverage = data.complete_count !== undefined
      ? '已备份 ' + data.complete_count + ' / ' + (data.expected_count || '?') + ' 章'
      : '';
    panel.append(element('p', 'pixiv-rescue-meta', coverage));
    renderNotice(panel, data);
    const loadButton = element('button', 'pixiv-rescue-button', '加载目录');
    loadButton.type = 'button';
    const viewer = element('div', 'pixiv-rescue-viewer');
    loadButton.addEventListener('click', function () {
      loadButton.disabled = true;
      apiGet('/api/rescue/v1/series/' + encodeURIComponent(String(seriesId)) + '/chapters')
        .then(function (directory) {
          renderChapterDirectory(panel, viewer, directory, seriesId);
          loadButton.remove();
        })
        .catch(renderError)
        .finally(function () {
          loadButton.disabled = false;
        });
    });
    panel.append(loadButton, viewer);
    root.append(panel);
  }

  function novelIdFromPage() {
    const params = new URLSearchParams(window.location.search);
    const value = params.get('id') || params.get('novel_id') || '';
    return /^\d+$/.test(value) ? value : null;
  }

  function seriesIdFromPage() {
    const match = pathWithoutLangPrefix().match(/\/novel\/series\/(\d+)/);
    return match ? match[1] : null;
  }

  // SPA 切页后旧请求的应答不能渲染进新页面（A14-05）：每次 routeRescue 递增代次，
  // 异步回调里核对代次与目标 ID，不一致直接丢弃。
  function routeGeneration() {
    return Number(window.__pixivRescueRouteGen || 0);
  }

  function bumpRouteGeneration() {
    window.__pixivRescueRouteGen = routeGeneration() + 1;
    return window.__pixivRescueRouteGen;
  }

  function handleNovelPage() {
    if (isNovelPageHealthy()) return;
    const novelId = novelIdFromPage();
    if (!novelId) return;
    const generation = bumpRouteGeneration();
    waitForUnavailableState(isNovelPageHealthy).then(function (unavailable) {
      if (!unavailable) return;
      if (routeGeneration() !== generation || novelIdFromPage() !== novelId) return;
      apiGet('/api/rescue/v1/novels/' + encodeURIComponent(novelId))
        .then(function (data) {
          if (routeGeneration() !== generation || novelIdFromPage() !== novelId) return;
          renderNovel(data);
        })
        .catch(function (error) {
          if (routeGeneration() !== generation || novelIdFromPage() !== novelId) return;
          renderError(error);
        });
    });
  }

  function handleSeriesPage() {
    if (isSeriesPageHealthy()) return;
    const seriesId = seriesIdFromPage();
    if (!seriesId) return;
    const generation = bumpRouteGeneration();
    waitForUnavailableState(isSeriesPageHealthy).then(function (unavailable) {
      if (!unavailable) return;
      if (routeGeneration() !== generation || seriesIdFromPage() !== seriesId) return;
      apiGet('/api/rescue/v1/series/' + encodeURIComponent(seriesId))
        .then(function (data) {
          if (routeGeneration() !== generation || seriesIdFromPage() !== seriesId) return;
          renderSeries(data, seriesId);
        })
        .catch(function (error) {
          if (routeGeneration() !== generation || seriesIdFromPage() !== seriesId) return;
          renderError(error);
        });
    });
  }

  function start() {
    if (!checkRuntimeSupport()) return;
    // 隐形加载标记：控制台可查 window.__pixivRescueLoaded 区分
    // 「脚本没注入」与「注入了但没识别到目标」。零可见输出的排查入口。
    window.__pixivRescueLoaded = true;
    registerTokenMenus();
    routeRescue();
    let lastHref = window.location.href;
    function onRoute() {
      if (window.location.href === lastHref) return;
      lastHref = window.location.href;
      const old = document.querySelector('[' + ROOT_ATTRIBUTE + '="content"]');
      if (old) old.remove();
      routeRescue();
    }
    const pushState = history.pushState;
    history.pushState = function () {
      pushState.apply(history, arguments);
      onRoute();
    };
    const replaceState = history.replaceState;
    history.replaceState = function () {
      replaceState.apply(history, arguments);
      onRoute();
    };
    window.addEventListener('popstate', onRoute);
  }

  function routeRescue() {
    if (pathWithoutLangPrefix() === '/novel/show.php') {
      handleNovelPage();
      return;
    }
    if (pathWithoutLangPrefix().startsWith('/novel/series/')) {
      handleSeriesPage();
      return;
    }
    if (isBookmarksListPage() || isWatchlistPage()) {
      handleListPage();
      return;
    }
    if (isNovelBookmarksPage()) handleNovelBookmarksPage();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();

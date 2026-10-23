// ==UserScript==
// @name         Pixiv 小说私人备份救援阅读
// @namespace    https://pixiv.dongboapp.com/
// @version      0.9.1
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
// 全域兜底：部分脚本管理器对带查询参数的 match pattern 处理不一致，导致
// 小说正文页（show.php?id=...）漏注入（实测 window.__pixivRescueLoaded
// 为 undefined，收藏页却正常）。脚本内部 routeRescue 严格限定目标路径，
// 其余页面立即返回，无副作用。
// @match        https://www.pixiv.net/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect     pixiv.dongboapp.com
// @run-at       document-start
// ==/UserScript==

(function () {
  'use strict';

  const DEFAULT_API_ORIGIN = 'https://pixiv.dongboapp.com';
  function apiOrigin() {
    const custom = String(GM_getValue('pixivRescueOrigin', '') || '').trim();
    return (custom || DEFAULT_API_ORIGIN).replace(/\/$/, '');
  }
  const SCRIPT_VERSION = '0.9.1';
  const TOKEN_KEY = 'pixivRescueToken';
  const ROOT_ATTRIBUTE = 'data-pixiv-rescue';
  // 本地缓存：收藏页每次进入都要重新 lookup + 拉封面，用户反馈「每次都要
  // 等一会」。lookup 结果缓存 10 分钟（目录变更由同步任务驱动，10 分钟足够
  // 新鲜）；封面 blob 缓存 24 小时（封面内容几乎不变）。缓存键带 token 前缀
  // 哈希，轮换 Token 后旧缓存自然失效。
  // 封面是一键一张 dataURL（单张几十 KB）。脚本没有 GM_listValues，无法枚举
  // 存储里有哪些键：0.9.0 及以前只在读取时判过期、从不删除，旧键一直留到
  // 管理器配额写满、GM_setValue 静默失败。现在用一个 GM 键记录最近使用的
  // 封面键（上限 COVER_CACHE_MAX_ENTRIES），被挤出或过期的键用 '' 覆盖清掉
  // ——同样没有 GM_deleteValue，键名本身留下，但只剩几十字节。
  const LOOKUP_CACHE_KEY = 'pixivRescueLookupCache';
  const COVER_CACHE_PREFIX = 'pixivRescueCover:';
  const COVER_CACHE_INDEX_KEY = 'pixivRescueCoverLru';
  const COVER_CACHE_MAX_ENTRIES = 150;
  const LOOKUP_CACHE_TTL_MS = 10 * 60 * 1000;
  const COVER_CACHE_TTL_MS = 24 * 60 * 60 * 1000;

  function cacheKeySuffix() {
    const token = String(GM_getValue(TOKEN_KEY, '') || '');
    // 简单滚动哈希即可，避免把 Token 明文写进存储键
    let h = 0;
    for (let i = 0; i < token.length; i += 1) {
      h = (h * 31 + token.charCodeAt(i)) | 0;
    }
    return String(h >>> 0);
  }

  function readLookupCache() {
    try {
      const raw = GM_getValue(LOOKUP_CACHE_KEY, '');
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      if (!parsed || parsed.suffix !== cacheKeySuffix()) return {};
      const now = Date.now();
      const entries = parsed.entries || {};
      const fresh = {};
      Object.keys(entries).forEach(function (key) {
        if (entries[key] && now - entries[key].at < LOOKUP_CACHE_TTL_MS) fresh[key] = entries[key];
      });
      return fresh;
    } catch (_error) {
      return {};
    }
  }

  function writeLookupCache(entries) {
    try {
      GM_setValue(LOOKUP_CACHE_KEY, JSON.stringify({
        suffix: cacheKeySuffix(),
        entries: entries
      }));
    } catch (_error) { /* 存储失败不影响功能 */ }
  }

  function cacheLookupResults(novels, series) {
    const entries = readLookupCache();
    const now = Date.now();
    (novels || []).forEach(function (item) { entries['n:' + item.novel_id] = { at: now, item: item }; });
    (series || []).forEach(function (item) { entries['s:' + item.series_id] = { at: now, item: item }; });
    // 限制条目数，避免长期使用撑大存储
    const keys = Object.keys(entries);
    if (keys.length > 400) {
      keys.sort(function (a, b) { return entries[a].at - entries[b].at; });
      keys.slice(0, keys.length - 400).forEach(function (key) { delete entries[key]; });
    }
    writeLookupCache(entries);
  }

  function readCachedLookup(id, type) {
    const entries = readLookupCache();
    const entry = entries[(type === 'series' ? 's:' : 'n:') + id];
    if (!entry) return null;
    if (Date.now() - entry.at >= LOOKUP_CACHE_TTL_MS) return null;
    return entry.item;
  }

  function coverCacheKey(id, type) {
    return COVER_CACHE_PREFIX + cacheKeySuffix() + ':' + type + ':' + id;
  }

  // 最近使用列表：[{key, at}]，最近用过的在前，at 是该封面写入缓存的时间。
  // 只认 COVER_CACHE_PREFIX 开头的键：淘汰时要把键清成 ''，列表万一被写坏
  // 也不能波及 Token 等其他存储。
  function readCoverIndex() {
    try {
      const parsed = JSON.parse(GM_getValue(COVER_CACHE_INDEX_KEY, '') || '[]');
      if (!Array.isArray(parsed)) return [];
      return parsed.filter(function (entry) {
        return entry && typeof entry.key === 'string' && entry.key.startsWith(COVER_CACHE_PREFIX) &&
          typeof entry.at === 'number';
      });
    } catch (_error) {
      return [];
    }
  }

  // 把 key 移到列表最前，顺带把过期与超出上限的旧键清成 ''。at 传 0 即视为
  // 已过期，随这一轮一起清掉（用于丢弃读到的过期/损坏封面）。返回列表是否
  // 写成功；存储出错只影响缓存，不影响显示。
  function updateCoverIndex(key, at) {
    try {
      const now = Date.now();
      const entries = [{ key: key, at: at }];
      readCoverIndex().forEach(function (entry) {
        if (entry.key !== key) entries.push(entry);
      });
      const kept = [];
      const seen = new Set();
      entries.forEach(function (entry) {
        if (seen.has(entry.key)) return;
        seen.add(entry.key);
        const expired = now - entry.at >= COVER_CACHE_TTL_MS;
        if (kept.length >= COVER_CACHE_MAX_ENTRIES || expired) {
          GM_setValue(entry.key, '');
        } else {
          kept.push(entry);
        }
      });
      GM_setValue(COVER_CACHE_INDEX_KEY, JSON.stringify(kept));
      return true;
    } catch (_error) {
      return false;
    }
  }

  function readCoverCache(id, type) {
    const dataKey = coverCacheKey(id, type);
    let cached = null;
    try {
      const raw = GM_getValue(dataKey, '');
      if (!raw) return null;
      cached = JSON.parse(raw);
    } catch (_error) { /* 损坏的值按过期处理 */ }
    if (cached && typeof cached.dataUrl === 'string' && cached.dataUrl &&
        typeof cached.at === 'number' && Date.now() - cached.at < COVER_CACHE_TTL_MS) {
      // 读到即算「最近使用」：常看的封面不会被挤出；0.9.0 及以前写下、
      // 还没进列表的键也借这次读取补登记
      updateCoverIndex(dataKey, cached.at);
      return cached.dataUrl;
    }
    // 过期或损坏：旧版只返回 null，值一直占着存储
    updateCoverIndex(dataKey, 0);
    return null;
  }

  function writeCoverCache(id, type, dataUrl) {
    try {
      const dataKey = coverCacheKey(id, type);
      const now = Date.now();
      // 先记账再写值：记账失败就不写，存储里不留列表管不到的封面
      if (!updateCoverIndex(dataKey, now)) return;
      GM_setValue(dataKey, JSON.stringify({
        at: now,
        dataUrl: dataUrl
      }));
    } catch (_error) { /* 大图可能超存储配额，静默失败 */ }
  }
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
    // 2026-10-21 生产实测：已删作品的正文页显示「尚无此页」+「回到主页」
    // （SPA 渲染的 404 占位）。旧列表遗漏，导致跳转到正文页后脚本不渲染。
    // 只收「尚无此页」——「回到主页」太泛，任何 404 页都有，会误伤。
    '尚无此页',
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
  // 列表页快速轮询窗口的截止时间戳（见 handleListPage）
  let fastScanUntil = 0;
  function showRescueNotice(kind) {
    const messages = {
      missing_token: '救援脚本未设置 Token：点 Tampermonkey 菜单「设置或更新救援 Token」后刷新页面',
      unauthorized: '救援 Token 无效：请到网站设置页轮换 Token 后重新填写',
      network_error: '无法连接救援服务，救援标记暂不可用',
      network_timeout: '救援服务响应超时，救援标记暂不可用',
      server_error: '救援服务暂时出错，救援标记暂不可用',
      not_found: '这部作品在 Pixiv 上已无法查看，私人备份里也没有收录'
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
    // 提示条是 position:fixed（见 CSS）：插进页面流会被手机站 404 空壳的
    // 绝对定位背景盖住，与 0.7.x 正文面板「渲染了却看不见」同一个坑。
    const host = document.body || document.documentElement;
    if (host) host.append(bar);
  }

  // ── 手机端兼容 ─────────────────────────────────────────────
  // 部分手机脚本管理器（X浏览器/Via 等）对 GM API 支持不齐：缺 GM_xmlhttpRequest
  // 时 apiGet 一调用就抛 TypeError，整个脚本静默死亡——页面什么都没有，用户
  // 无从排查。这里在启动最前面做能力检测并给出可见提示。
  // GM API 是否齐备。手机端管理器可能在文档早期异步注入 GM 对象，因此
  // 缺失时由 start() 重试，而不是立即放弃（旧版直接 return，脚本静默退出，
  // 页面只剩 Pixiv 原生内容——与用户现象一致）。
  function missingRuntimeApis() {
    const missing = [];
    if (typeof GM_getValue !== 'function') missing.push('GM_getValue');
    if (typeof GM_xmlhttpRequest !== 'function') missing.push('GM_xmlhttpRequest');
    return missing;
  }

  function showRuntimeUnsupported(missing) {
    try {
      const bar = document.createElement('div');
      bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;padding:10px 14px;background:#fef2f2;color:#991b1b;font:13px/1.6 -apple-system,sans-serif;border-bottom:1px solid #fca5a5';
      bar.textContent = '救援脚本需要脚本管理器支持 ' + missing.join('、') +
        '。当前浏览器/管理器未提供，救援功能不可用。请换用支持完整 GM API 的管理器（如 Tampermonkey）。';
      (document.body || document.documentElement).prepend(bar);
    } catch (_error) { /* ignore */ }
  }

  function checkRuntimeSupport() {
    const missing = missingRuntimeApis();
    if (missing.length) {
      showRuntimeUnsupported(missing);
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

  // timeoutMs：整本正文可达 20 万字（JSON 约 216 KB），手机网络 15 秒不够稳，
  // 正文接口由调用方放宽；列表 lookup 等小请求保持 15 秒。
  const FULL_TEXT_TIMEOUT_MS = 30000;
  function apiGet(path, timeoutMs) {
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
        timeout: timeoutMs || 15000,
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

  // 二进制 GET（封面图）：Token 只走 Authorization 头，不放进 URL——
  // 否则会进 nginx access log 和浏览器历史。返回 object URL 供 <img> 使用。
  function apiGetBlobUrl(path) {
    const token = String(GM_getValue(TOKEN_KEY, '') || '').trim();
    if (!token) return Promise.reject(new Error('missing_token'));
    return new Promise(function (resolve, reject) {
      GM_xmlhttpRequest({
        method: 'GET',
        url: apiOrigin() + path,
        headers: { Authorization: 'Bearer ' + token },
        responseType: 'blob',
        timeout: 15000,
        onload: function (response) {
          if (response.status < 200 || response.status >= 300) {
            reject(new Error(response.status === 404 ? 'not_found' : 'request_failed'));
            return;
          }
          try {
            resolve(URL.createObjectURL(response.response));
          } catch (_error) {
            reject(new Error('request_failed'));
          }
        },
        onerror: function () { reject(new Error('network_error')); },
        ontimeout: function () { reject(new Error('network_timeout')); }
      });
    });
  }

  // 作品在 Pixiv 上还在不在：问 Pixiv 自己的 ajax 接口，不再猜页面 DOM。
  // 2026-10-23 生产实测：已删小说 /ajax/novel/<id> → 404 {error:true, body:[]}；
  // 已删系列 /ajax/novel/series/<id> → 404 {error:true}；健康作品 → 200
  // {error:false, body:{...}}。旧版按「有没有 <main>」「有没有失效文案」判断，
  // 而手机站健康正文页同样没有 <main>——结果每个健康正文页都去请求备份，
  // 再在页尾塞一条「未找到可用的救援数据」。
  // 只有 404/410 才算「确认已失效」；403（仅好友可见、R-18 未开）等歧义状态
  // 记为 unknown：备份有就给，没有就不提示，避免把「看不了」误报成「被删了」。
  function pixivWorkState(type, id) {
    const path = type === 'series'
      ? '/ajax/novel/series/' + encodeURIComponent(id)
      : '/ajax/novel/' + encodeURIComponent(id);
    return fetch(path, { credentials: 'include', headers: { Accept: 'application/json' } })
      .then(function (response) {
        return response.json().then(function (payload) {
          if (response.ok && payload && payload.error === false) return 'available';
          if (response.status === 404 || response.status === 410) return 'unavailable';
          return 'unknown';
        }, function () {
          return 'unknown';
        });
      })
      .catch(function () {
        return 'unknown';
      });
  }

  function addStyles() {
    if (document.querySelector('style[data-pixiv-rescue-styles]')) return;
    const style = document.createElement('style');
    style.setAttribute('data-pixiv-rescue-styles', '');
    style.textContent = [
      // 正文/系列阅读层：脚本自有的固定全屏层，自带滚动，不进 Pixiv 页面流。
      // 生产根因（2026-10-23 真机页实测）：手机站已删作品页是 SPA 404 空壳，
      // 一个 position:absolute、不透明背景（rgb(245,245,245)）的通栏容器从
      // y=48 起铺满 2000+px；0.7.x 把面板当普通块元素追加到 body，按 CSS 绘制
      // 顺序被它整块盖住——20 万字全在 DOM 里，用户什么都看不到。
      '.pixiv-rescue-root{position:fixed;top:0;right:0;bottom:0;left:0;z-index:2147483600;box-sizing:border-box;margin:0;padding:0;overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain;background:#f9fafb;color:#1f2937;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;text-align:left}',
      // 收起态：只剩底部一颗「查看私人备份」胶囊，Pixiv 原页面完全可用
      '.pixiv-rescue-root.pixiv-rescue-root--minimized{top:auto;right:auto;bottom:16px;bottom:calc(16px + env(safe-area-inset-bottom, 0px));left:50%;transform:translateX(-50%);overflow:visible;background:transparent}',
      '.pixiv-rescue-root--minimized .pixiv-rescue-reader-bar,.pixiv-rescue-root--minimized .pixiv-rescue-reader-body{display:none}',
      '.pixiv-rescue-reopen{display:none;min-height:40px;padding:8px 18px;border:0;border-radius:999px;background:#dc2626;color:#fff;font-size:14px;font-weight:700;box-shadow:0 6px 18px rgba(17,24,39,.25);cursor:pointer;white-space:nowrap}',
      '.pixiv-rescue-root--minimized .pixiv-rescue-reopen{display:inline-flex;align-items:center}',
      '.pixiv-rescue-reader-bar{position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 12px;border-bottom:1px solid #e5e7eb;background:#fff}',
      '.pixiv-rescue-reader-label{font-size:13px;font-weight:700;color:#dc2626}',
      '.pixiv-rescue-reader-body{box-sizing:border-box;max-width:760px;margin:0 auto;padding:16px 16px 48px}',
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
      // 章节切换后 scrollIntoView 到正文开头：留出粘性顶栏的高度，标题不被盖住
      '.pixiv-rescue-viewer{scroll-margin-top:64px}',
      '@media(max-width:640px){.pixiv-rescue-reader-body{padding:12px 10px 48px}.pixiv-rescue-panel{padding:16px}.pixiv-rescue-title{font-size:20px}}',
      // 徽标是绝对定位角标：失效卡是 grid 布局，旧版把徽标当普通子元素 prepend，
      // 会被吸进第一个网格单元与标题文字重叠。脱离布局流浮在卡片右上角。
      // 注意：这个数组经 join('') 拼接，注释必须保持在字符串元素之间的合法
      // JS 位置——否则注释会吞掉 join 的逗号让前后两条 CSS 粘连成废规则
      // （0.3.8 的 position:absolute 就这么失效过一次）。
            // 重建后的「正常卡片」外观：沿用 Pixiv 卡片的信息层级（封面左、文字右），
      // 只在封面右上角放一个小「拯救」标识；不再整块红色（用户反馈像警告）。
      '.pixiv-rescue-rescued-card{padding:0}',
      '.pixiv-rescue-rescued-link{display:flex;gap:12px;padding:10px 0;color:inherit;text-decoration:none;align-items:flex-start}',
      '.pixiv-rescue-rescued-cover{position:relative;flex:0 0 64px;width:64px;height:90px;border-radius:2px;overflow:hidden;background:#f3f4f6}',
      '.pixiv-rescue-rescued-img{width:100%;height:100%;object-fit:cover;display:block}',
      '.pixiv-rescue-mark{position:absolute;top:0;left:0;z-index:2;padding:0 4px;border-radius:0 0 3px 0;background:rgba(37,99,235,.92);color:#fff;font-size:10px;font-weight:600;line-height:1.5}',
      '.pixiv-rescue-rescued-body{min-width:0;flex:1 1 auto}',
      '.pixiv-rescue-rescued-title{font-size:15px;font-weight:700;line-height:1.5;color:#1f2937;overflow-wrap:anywhere}',
      '.pixiv-rescue-rescued-author{margin-top:6px;font-size:12px;color:#6b7280}',
      '.pixiv-rescue-rescued-hint{margin-top:4px;font-size:11px;color:#9ca3af}',
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
      // 提示条同样固定定位：插进页面流会被 404 空壳的绝对定位背景盖住
      '.pixiv-rescue-notice-bar{position:fixed;left:12px;right:12px;bottom:12px;z-index:2147483640;display:flex;align-items:center;gap:12px;justify-content:center;max-width:760px;margin:0 auto;padding:10px 14px;border:1px solid #fca5a5;border-radius:6px;background:#fef2f2;color:#991b1b;font-size:13px;line-height:1.6;box-shadow:0 6px 18px rgba(17,24,39,.15)}'
    ].join('');
    (document.head || document.documentElement).append(style);
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

  // ── 阅读层（0.9.0）──────────────────────────────────────────
  // 正文/系列备份统一渲染进脚本自有的固定全屏层（CSS 见 .pixiv-rescue-root）：
  // 不进 Pixiv 页面流，就不会被它的绝对定位/不透明容器盖住，也不受它的布局、
  // 滚动与渲染时机影响。0.8.0（未发布）的做法是先插页面流、再在 0~14 秒内做
  // 命中测试检测遮挡、被盖住才升级覆盖——依赖时机，且在健康正文页上会把页面
  // 一路滚到页尾。可见性应该由 CSS 保证，而不是事后检测。
  let readerRoot = null;
  let readerMountTimer = 0;

  function attachReaderRoot(node) {
    if (node.isConnected) return true;
    if (!document.body) return false;
    addStyles();
    document.body.append(node);
    return true;
  }

  // document-start 时 body 还没解析出来：短轮询等它出现再挂载。手机站
  // DOMContentLoaded 实测约 4 秒（大量阻塞脚本），不值得为挂载干等那么久。
  function mountReaderRoot(node) {
    if (attachReaderRoot(node)) return;
    window.clearInterval(readerMountTimer);
    readerMountTimer = window.setInterval(function () {
      if (readerRoot !== node || attachReaderRoot(node)) window.clearInterval(readerMountTimer);
    }, 50);
  }

  // 打开（或复用）阅读层，返回清空后的内容容器
  function openReader() {
    if (!readerRoot) {
      const root = element('section', 'pixiv-rescue-root');
      root.setAttribute(ROOT_ATTRIBUTE, 'content');
      root.setAttribute('aria-label', '私人备份阅读');
      const bar = element('div', 'pixiv-rescue-reader-bar');
      bar.append(element('span', 'pixiv-rescue-reader-label', '私人备份阅读'));
      const close = element('button', 'pixiv-rescue-button', '回到 Pixiv 页面');
      close.type = 'button';
      close.addEventListener('click', minimizeReader);
      bar.append(close);
      const reopen = element('button', 'pixiv-rescue-reopen', '查看私人备份');
      reopen.type = 'button';
      reopen.addEventListener('click', restoreReader);
      root.append(bar, element('div', 'pixiv-rescue-reader-body'), reopen);
      readerRoot = root;
    }
    restoreReader();
    mountReaderRoot(readerRoot);
    const body = readerRoot.querySelector('.pixiv-rescue-reader-body');
    clearNode(body);
    readerRoot.scrollTop = 0;
    return body;
  }

  // 「回到 Pixiv 页面」只收起为底部胶囊而不销毁：随时能再点开，不用重新请求
  function minimizeReader() {
    if (!readerRoot) return;
    readerRoot.classList.add('pixiv-rescue-root--minimized');
    readerRoot.setAttribute('data-pixiv-rescue-mode', 'minimized');
  }

  function restoreReader() {
    if (!readerRoot) return;
    readerRoot.classList.remove('pixiv-rescue-root--minimized');
    readerRoot.setAttribute('data-pixiv-rescue-mode', 'reader');
  }

  // 切页时调用：先清引用再摘节点，保活逻辑就不会把它挂回去
  function destroyReader() {
    window.clearInterval(readerMountTimer);
    const root = readerRoot;
    readerRoot = null;
    if (root) root.remove();
  }

  // 保活：Pixiv SPA 若整块重渲染 body 把阅读层摘掉，挂回去（随 1 秒路由轮询调用）
  function keepReaderAttached() {
    if (readerRoot && !readerRoot.isConnected) attachReaderRoot(readerRoot);
  }

  // 请求还没回来时的占位：用户立刻看到「脚本已经在工作」，而不是盯着空壳页面
  function renderPending() {
    const body = openReader();
    const panel = element('div', 'pixiv-rescue-panel');
    panel.append(element('span', 'pixiv-rescue-kicker', '拯救数据'));
    panel.append(element('p', 'pixiv-rescue-meta', '正在读取备份…'));
    body.append(panel);
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
    const body = openReader();
    const panel = element('div', 'pixiv-rescue-panel');
    panel.append(element('span', 'pixiv-rescue-kicker', '拯救数据'));
    panel.append(element('h1', 'pixiv-rescue-title', data.title || '未命名备份小说'));
    const author = data.author_name ? '作者：' + data.author_name : '';
    panel.append(element('p', 'pixiv-rescue-meta', author));
    renderNotice(panel, data);
    renderText(panel, data.text_raw || '备份正文为空');
    body.append(panel);
  }

  // 阅读层内的整页错误。retry 存在时给「重试」——超时/网络抖动不该是死路，
  // 旧版只能整页刷新。
  function renderError(error, retry) {
    const body = openReader();
    body.append(element('div', 'pixiv-rescue-error', rescueErrorText(error, true)));
    // 错误代码直接画出来：手机沙箱里看不到控制台，这是唯一的排查线索
    const code = error && error.message ? String(error.message) : '';
    if (code) body.append(element('p', 'pixiv-rescue-meta', '错误代码：' + code));
    if (typeof retry === 'function') {
      const again = element('button', 'pixiv-rescue-button', '重试');
      again.type = 'button';
      again.addEventListener('click', retry);
      body.append(again);
    }
  }

  // 目录/加载更多失败：只在面板里提示，不清掉已加载的目录。
  // 旧版这里走 renderError，一次「加载更多」失败就把整个系列面板换成错误条。
  function renderDirectoryError(panel, viewer, error) {
    let note = panel.querySelector('.pixiv-rescue-directory-error');
    if (!note) {
      note = element('div', 'pixiv-rescue-error pixiv-rescue-directory-error', '');
      if (viewer && panel.contains(viewer)) panel.insertBefore(note, viewer);
      else panel.append(note);
    }
    note.textContent = '目录没有读到：' + rescueErrorText(error, true);
  }

  function rescueErrorText(error, pageLevel) {
    const messages = {
      missing_token: '未设置救援 Token，请通过油猴菜单完成设置。',
      unauthorized: '救援 Token 无效，请在网站设置页轮换后重新填写。',
      not_found: pageLevel ? '未找到可用的救援数据，Pixiv 原页面已保留。' : '备份里没有这一章，目录还在。',
      rate_limited: '请求太频繁，请稍后再试。',
      server_error: '救援服务暂时出错，请稍后再试。',
      network_timeout: '救援服务响应超时，Pixiv 原页面已保留。',
      network_error: '无法连接救援服务，Pixiv 原页面已保留。',
      // 整本正文 JSON 约 216 KB：部分手机管理器可能截断大应答，解析失败落到这里
      invalid_response: '备份数据没有完整读到，请重试。'
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

  // 把失效卡片重建为「正常卡片」外观：封面 + 标题 + 作者，右上角一个小巧的
  // 「拯救」标识。用户反馈红色整块太像警告，且备份本身有封面却没用上。
  // 无封面（或封面加载失败）时退化为纯文字卡片，不显示破图。
  function renderRescuedCard(card, match, isSeries) {
    const title = match.title || '未命名';
    const author = match.author_name || '';
    const isNovel = !isSeries && match.novel_id;
    // 清空 Pixiv 的失效占位内容，保留外层容器（布局/间距由它维持）
    clearNode(card);
    card.classList.add('pixiv-rescue-rescued-card');

    const link = element('a', 'pixiv-rescue-rescued-link');
    // 跳转到 Pixiv 正文页并带 &pixiv_rescue=1 标记。目标页据此直接渲染备份，
    // 完全不依赖 Pixiv 的失效文案——生产已连续踩坑不同文案（「该作品无法
    // 显示」「尚无此页」），这类识别天然脆弱。URL 参数跨整页导航可靠，
    // sessionStorage 作为第二重信号（隐私模式下可能失效）。
    link.href = isNovel
      ? '/novel/show.php?id=' + encodeURIComponent(String(match.novel_id)) + '&pixiv_rescue=1'
      : '/novel/series/' + encodeURIComponent(String(match.series_id || match.novel_id)) + '?pixiv_rescue=1';
    link.addEventListener('click', function () {
      try {
        sessionStorage.setItem('pixivRescueIntent', JSON.stringify({
          type: isNovel ? 'novel' : 'series',
          id: isNovel ? match.novel_id : (match.series_id || match.novel_id),
          at: Date.now()
        }));
      } catch (_error) { /* 隐私模式等场景下写失败，退回 URL 标记 */ }
    });

    const coverBox = element('div', 'pixiv-rescue-rescued-cover');
    const mark = element('span', 'pixiv-rescue-mark', '拯救');
    coverBox.append(mark);
    if (match.has_cover) {
      const coverId = isNovel ? match.novel_id : (match.series_id || match.novel_id);
      const coverType = isNovel ? 'novel' : 'series';
      const coverPath = isNovel
        ? '/api/rescue/v1/novels/' + encodeURIComponent(String(coverId)) + '/cover'
        : '/api/rescue/v1/series/' + encodeURIComponent(String(coverId)) + '/cover';
      const img = element('img', 'pixiv-rescue-rescued-img');
      img.alt = '';
      img.loading = 'lazy';
      coverBox.append(img);
      // 封面优先读本地缓存（dataURL，24h），未命中再拉 blob 并回写。
      // 每次进收藏页重复拉封面正是用户感知到的「等一会」之一。
      const cachedCover = readCoverCache(coverId, coverType);
      if (cachedCover) {
        img.src = cachedCover;
      } else {
        apiGetBlobUrl(coverPath)
          .then(function (url) {
            img.src = url;
            // blob URL 不能跨页面持久化：取回真实字节转 dataURL 存缓存
            fetch(url).then(function (resp) { return resp.blob(); })
              .then(function (blob) {
                const fr = new FileReader();
                fr.onload = function () {
                  if (typeof fr.result === 'string') writeCoverCache(coverId, coverType, fr.result);
                };
                fr.readAsDataURL(blob);
              })
              .catch(function () { /* 缓存写失败不影响显示 */ });
          })
          .catch(function () { img.remove(); });
      }
    }

    const body = element('div', 'pixiv-rescue-rescued-body');
    body.append(element('div', 'pixiv-rescue-rescued-title', title));
    if (author) body.append(element('div', 'pixiv-rescue-rescued-author', author));
    if (!match.personal_hold) {
      body.append(element('div', 'pixiv-rescue-rescued-hint', '本人已取消收藏/追更'));
    }

    link.append(coverBox, body);
    card.append(link);
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
    // 缓存命中：直接渲染，不发任何请求（每次进收藏页的等待主要来自这里）
    const pendingEntries = [];
    found.broken.forEach(function (entry) {
      const cached = readCachedLookup(entry.id, entry.type === 'series' ? 'series' : 'novel');
      if (cached) {
        renderRescuedCard(entry.card, cached, entry.type === 'series');
        entry.card.setAttribute(ROOT_ATTRIBUTE, 'card');
      } else {
        pendingEntries.push(entry);
      }
    });
    if (!pendingEntries.length) return;
    const pending = { broken: pendingEntries };
    buildLookupBatches(pending.broken).forEach(function (batch) {
      apiGet(buildLookupPath(batch.novelIds, batch.seriesIds)).then(function (data) {
        const novels = {};
        (data.novels || []).forEach(function (item) { novels[item.novel_id] = item; });
        const series = {};
        (data.series || []).forEach(function (item) { series[item.series_id] = item; });
        cacheLookupResults(data.novels, data.series);
        batch.entries.forEach(function (entry) {
          const novelMatch = novels[entry.id];
          const seriesMatch = series[entry.id];
          const match = entry.type === 'series' ? seriesMatch
            : (entry.type === 'novel' ? novelMatch : (seriesMatch || novelMatch));
          // 这批已成功应答：卡片转终态，不再重复查询（无备份的也转，避免每轮空查）
          entry.card.setAttribute(ROOT_ATTRIBUTE, 'card');
          if (!match) return;
          renderRescuedCard(entry.card, match, entry.type === 'series');
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
    // 首屏加速：Pixiv 列表是 SPA 动态渲染，进页面到卡片出现之间有等待。
    // MutationObserver 在卡片节点插入时立刻扫一轮，不必等下一个 3 秒 tick；
    // 观察器去抖，避免渲染风暴时重复扫描。
    if (!window.__pixivRescueObserver) {
      let scheduled = false;
      const observer = new MutationObserver(function () {
        if (scheduled) return;
        scheduled = true;
        window.setTimeout(function () {
          scheduled = false;
          if (!isBookmarksListPage() && !isWatchlistPage()) return;
          markBrokenCards();
          // 快速通道：首次看到失效卡但还没渲染完时，短时间内多扫几次。
          // 用户反馈「有时等很久没反应、刷新一下几秒就出来」——SPA 分批
          // 渲染时，单靠 150ms 去抖可能刚好错过卡片插入的那一批。
          fastScanUntil = Date.now() + 15000;
        }, 150);
      });
      observer.observe(document.body || document.documentElement, { childList: true, subtree: true });
      window.__pixivRescueObserver = observer;
    }
    // 快速轮询窗口：进入列表页后 15 秒内每 500ms 扫一次（SPA 分批渲染），
    // 之后交给 3 秒 interval。定时器用链式 setTimeout，避免两个 interval 叠加。
    fastScanUntil = Date.now() + 15000;
    if (!window.__pixivRescueFastScan) {
      const fastTick = function () {
        if (!isBookmarksListPage() && !isWatchlistPage()) {
          window.__pixivRescueFastScan = null;
          return;
        }
        markBrokenCards();
        if (Date.now() < fastScanUntil) {
          window.__pixivRescueFastScan = window.setTimeout(fastTick, 500);
        } else {
          window.__pixivRescueFastScan = null;
        }
      };
      window.__pixivRescueFastScan = window.setTimeout(fastTick, 500);
    }
    // SPA 切页会重复进入本函数：interval 只注册一次，避免叠加扫描（泄漏修复）。
    // 它从不停止，跳出列表页后每个 tick 先看路径再决定扫不扫——0.9.0 及以前
    // 在正文页、首页上也每 3 秒全页扫一遍。不 clearInterval：回到列表页时
    // 不必重新注册，下一个 tick 就恢复扫描。
    if (!window.__pixivRescueListInterval) {
      window.__pixivRescueListInterval = window.setInterval(function () {
        if (!isBookmarksListPage() && !isWatchlistPage()) return;
        markBrokenCards();
      }, 3000);
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
      // 带 ?pixiv_rescue=1：用户明确要看备份。部分救援的系列在 Pixiv 上仍然
      // 存在（只缺几章），不带标记的话 handleSeriesPage 会判「作品还在」而不开阅读层。
      window.location.href = 'https://www.pixiv.net/novel/series/'
        + encodeURIComponent(String(item.series_id)) + '?pixiv_rescue=1';
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
    if (mountPoint && mountPoint !== document.body && mountPoint.parentElement) {
      mountPoint.parentElement.insertBefore(container, mountPoint.nextSibling);
    } else {
      const host = document.querySelector('main') || document.body || document.documentElement;
      if (host) host.append(container);
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
    // 目录可能很长：章节正文出现在目录下方，不滚过去用户会以为点了没反应
    viewer.scrollIntoView(true);
  }

  function renderChapterDirectory(panel, viewer, data, seriesId) {
    // 异步回调可能在页面已切走、panel 被清空后才到达：此时 viewer 已脱离
    // panel，insertBefore 会抛 "Cannot read properties of null"。直接放弃本轮。
    if (!panel || !viewer || !panel.contains(viewer)) return;
    const staleError = panel.querySelector('.pixiv-rescue-directory-error');
    if (staleError) staleError.remove();
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
        apiGet('/api/rescue/v1/novels/' + encodeURIComponent(String(chapter.novel_id)), FULL_TEXT_TIMEOUT_MS)
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
          .catch(function (error) {
            renderDirectoryError(panel, viewer, error);
          })
          .finally(function () {
            moreButton.disabled = false;
          });
      });
      moreItem.append(moreButton);
      list.append(moreItem);
    }
  }

  function renderSeries(data, seriesId) {
    const body = openReader();
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
        .catch(function (error) {
          renderDirectoryError(panel, viewer, error);
        })
        .finally(function () {
          loadButton.disabled = false;
        });
    });
    panel.append(loadButton, viewer);
    body.append(panel);
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

  // 读取「点击卡片的救援意图」：存在且与当前页面 ID 匹配、且足够新（30 秒）
  // 时返回 true。这是「用户明确从救援卡点进来」的直接信号，不需要识别文案。
  function consumeRescueIntent(expectedType, expectedId) {
    // URL 标记参数是跨整页导航最可靠的信号：sessionStorage 在部分浏览器
    // 的跨页跳转/隐私模式下会丢，URL 参数不会。
    try {
      const params = new URLSearchParams(window.location.search);
      if (params.get('pixiv_rescue') === '1') return true;
    } catch (_error) { /* ignore */ }
    try {
      const raw = sessionStorage.getItem('pixivRescueIntent');
      if (!raw) return false;
      const intent = JSON.parse(raw);
      if (!intent || intent.type !== expectedType) return false;
      if (String(intent.id) !== String(expectedId)) return false;
      if (Date.now() - Number(intent.at || 0) > 30000) return false;
      return true;
    } catch (_error) {
      return false;
    }
  }

  // 正文页/系列页共用的救援流程。是否打开阅读层只看两个确定性信号：
  //   1. 救援意图（URL 标记 / sessionStorage）：用户明确要看备份，立刻打开；
  //   2. 无意图时问 Pixiv 自己的 ajax：作品还在 → 什么都不做（不请求备份、
  //      不动 DOM）；确认失效或状态不明 → 静默查备份，查到才打开。
  // 旧版的「页面文案」「有没有 <main>」「等 1.2 秒」全部删除——那些都是猜，
  // 而且猜错的代价是在每个健康正文页底部塞一条「未找到可用的救援数据」。
  function rescueDetailPage(type, id, isStale, loadBackup, renderBackup) {
    const show = function () {
      renderPending();
      loadBackup()
        .then(function (data) {
          if (!isStale()) renderBackup(data);
        })
        .catch(function (error) {
          if (!isStale()) renderError(error, show);
        });
    };
    if (consumeRescueIntent(type, id)) {
      show();
      return;
    }
    pixivWorkState(type, id).then(function (state) {
      if (isStale() || state === 'available') return;
      loadBackup()
        .then(function (data) {
          if (!isStale()) renderBackup(data);
        })
        .catch(function (error) {
          // 状态不明（403/限流/网络）时查不到就不提示：作品多半好好的，不打扰
          if (isStale() || state !== 'unavailable') return;
          showRescueNotice(error && error.message);
        });
    });
  }

  function handleNovelPage() {
    const novelId = novelIdFromPage();
    if (!novelId) return;
    const generation = bumpRouteGeneration();
    rescueDetailPage('novel', novelId, function () {
      return routeGeneration() !== generation || novelIdFromPage() !== novelId;
    }, function () {
      return apiGet('/api/rescue/v1/novels/' + encodeURIComponent(novelId), FULL_TEXT_TIMEOUT_MS);
    }, renderNovel);
  }

  function handleSeriesPage() {
    const seriesId = seriesIdFromPage();
    if (!seriesId) return;
    const generation = bumpRouteGeneration();
    rescueDetailPage('series', seriesId, function () {
      return routeGeneration() !== generation || seriesIdFromPage() !== seriesId;
    }, function () {
      return apiGet('/api/rescue/v1/series/' + encodeURIComponent(seriesId));
    }, function (data) {
      renderSeries(data, seriesId);
    });
  }

  function isTargetPage() {
    const path = pathWithoutLangPrefix();
    return path === '/novel/show.php'
      || path.startsWith('/novel/series/')
      || isBookmarksListPage()
      || isWatchlistPage()
      || isNovelBookmarksPage();
  }

  // 可见启动徽标：手机端脚本管理器常把脚本跑在沙箱里，页面控制台读不到
  // window.__pixivRescueLoaded（沙箱 window !== page window），控制台诊断在
  // 这类环境完全失效。用一个极小的角标证明「脚本跑到了这一步」。
  function showBootBadge() {
    try {
      if (document.querySelector('[data-pixiv-rescue-boot]')) return;
      const badge = document.createElement('div');
      badge.setAttribute('data-pixiv-rescue-boot', '');
      badge.textContent = '救援 ' + SCRIPT_VERSION;
      badge.style.cssText = 'position:fixed;right:6px;bottom:6px;z-index:2147483647;padding:2px 6px;border-radius:4px;background:rgba(37,99,235,.85);color:#fff;font:10px/1.5 -apple-system,sans-serif;pointer-events:none;opacity:.75';
      (document.body || document.documentElement).appendChild(badge);
      window.setTimeout(function () { try { badge.remove(); } catch (_e) {} }, 6000);
    } catch (_error) { /* 诊断徽标失败不能影响主流程 */ }
  }

  // 可见错误条：沙箱里抛的异常在控制台看不到，直接画到页面上
  function showFatalError(error) {
    try {
      const bar = document.createElement('div');
      bar.setAttribute('data-pixiv-rescue-fatal', '');
      bar.textContent = '救援脚本出错：' + String(error && error.message || error).slice(0, 200);
      bar.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:2147483647;padding:10px 14px;background:#fef2f2;color:#991b1b;font:12px/1.6 -apple-system,sans-serif;border-bottom:1px solid #fca5a5';
      (document.body || document.documentElement).prepend(bar);
    } catch (_ignored) { /* 连错误条都画不出来就只能作罢 */ }
  }

  // 0.7.2 的「救援流程：<步骤>」分步状态条（手机沙箱排查用的临时诊断）已移除：
  // 阅读层的「正在读取备份…」占位本身就是脚本在工作的可见证据。

  function isDetailPage() {
    const path = pathWithoutLangPrefix();
    return path === '/novel/show.php' || path.startsWith('/novel/series/');
  }

  function start(attempt) {
    showBootBadge();
    if (!checkRuntimeSupport()) {
      // 手机脚本管理器可能稍后才注入 GM 对象：在 5 秒内重试，超时才放弃
      const tries = Number(attempt || 0);
      if (tries < 10 && missingRuntimeApis().length) {
        window.setTimeout(function () { start(tries + 1); }, 500);
      }
      return;
    }
    // 全域 @match 兜底会把脚本注入每个 pixiv 页面：非目标页面不注册任何
    // 钩子/定时器，只保留一个 SPA 路由监听（Pixiv 站内跳转后可能进入目标页）。
    // 沙箱环境下页面控制台读不到这个标记，仅作调试辅助。
    window.__pixivRescueLoaded = true;
    registerTokenMenus();
    // 正文/系列页的判定只依赖 URL 与网络（意图标记、Pixiv ajax、备份接口），
    // 不读 DOM：立即开跑，阅读层自己等 body 出现再挂载。手机站 DOMContentLoaded
    // 实测约 4 秒，旧版等它才发请求，白白多等这 4 秒。列表页要扫 DOM，仍等 body。
    const routeWhenReady = function () {
      if (!isTargetPage()) return;
      if (document.body || isDetailPage()) {
        routeRescue();
      } else {
        document.addEventListener('DOMContentLoaded', routeRescue, { once: true });
      }
    };
    routeWhenReady();
    let lastHref = window.location.href;
    function onRoute() {
      keepReaderAttached();
      if (window.location.href === lastHref) return;
      lastHref = window.location.href;
      destroyReader();
      // 固定定位的提示条不随页面流消失，切页时一并清掉，并允许新页面重新提示
      const notice = document.querySelector('[data-pixiv-rescue-notice]');
      if (notice) notice.remove();
      lastNoticeKey = null;
      try {
        routeWhenReady();
      } catch (error) {
        showFatalError(error);
      }
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
    // 兜底：Pixiv 的内部路由器可能用我们 hook 不到的方式切换 URL（不同版本
    // 用不同的 history 封装）。定时比对 href，变化就重跑路由——这是廉价且
    // 不依赖对方实现的最后保险。
    window.setInterval(onRoute, 1000);
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

  // 立即启动，不等 DOMContentLoaded。
  // @run-at document-start 时 readyState 为 'loading'，旧代码把 start 挂到
  // DOMContentLoaded 上——若脚本注入晚于该事件（部分管理器会延迟注入），
  // 监听器永不再触发，start() 一次都不执行。注意这不是「点击后仍显示尚无此页」
  // 的根因（0.6.5 曾这么认为）：真正的根因是面板被 404 空壳盖住，见阅读层注释。
  // DOM 缺失由各路径自己兜底（阅读层等 body 出现再挂载，列表页等 DOMContentLoaded）。
  try {
    start();
  } catch (error) {
    showFatalError(error);
  }
})();

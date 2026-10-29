// ==UserScript==
// @name         Pixiv 小说私人备份救援阅读
// @namespace    https://pixiv.dongboapp.com/
// @version      0.12.1
// @description  在 Pixiv 小说/系列明细页及收藏/追更列表页识别失效内容，从私人备份读取并标记救援内容；「好P友新作」tab 改为「拯救系列」，追更列表空白框原位替换为备份卡片。
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
  const SCRIPT_VERSION = '0.12.1';
  // Pixiv 手机站正文容器的 gothic 字体栈（2026-10-23 实测 #novel-text-container
  // 的内联 font-family，Aa 设置默认值）。阅读层正文用同一套，排版才能逐字对齐。
  const PIXIV_GOTHIC_FONT = '"Avenir Next",Avenir,"Source Sans","Noto Sans",Roboto,Verdana,"Pingfang SC","Hiragino Sans GB","Lantinghei SC","Source Han Sans CN","Noto Sans CJK SC","Microsoft Yahei",DengXian,YuGothic,"Hiragino Kaku Gothic Pro",Meiryo,"Source Han Sans","Source Han Sans JP","Noto Sans CJK JP","Pingfang TC","Pingfang HK","Hiragino Sans CNS","Lantinghei TC","Source Han Sans TW","Source Han Sans HK","Noto Sans CJK TC","Microsoft JhengHei","Apple SD Gothic Neo","Source Han Sans K","Source Han Sans KR","Noto Sans CJK KR","Malgun Gothic",sans-serif';
  // 用户在 Pixiv 正文页「Aa」里选的背景/字号/行距：在健康正文页上只读地记下来，
  // 阅读层照着用（见 learnReaderTheme）
  const READER_THEME_KEY = 'pixivRescueReaderTheme';
  const SERIES_PROGRESS_KEY = 'pixivRescueSeriesProgress';
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

  // entries 是调用方取好的 readLookupCache()：一轮扫描只读、只解析一次整份缓存。
  // 0.12.0 及以前每张卡都重新读一遍（400 条约 114 KB）
  function readCachedLookup(entries, id, type) {
    const entry = entries[(type === 'series' ? 's:' : 'n:') + id];
    if (!entry) return null;
    if (Date.now() - entry.at >= LOOKUP_CACHE_TTL_MS) return null;
    // 0.11.0 及以前缓存的系列条目没有卡片字段（章节数、最新一话等），追更列表填空
    // 画不出完整卡片：当作未命中，重新问一次服务端
    if (type === 'series' && (!entry.item || entry.item.chapter_count === undefined)) return null;
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

  // 只在 start() 重试用尽后调用一次。0.12.0 及以前每次重试失败都 prepend 一条：GM 晚注入时
  // 页头叠着好几条关不掉的红条，功能恢复后仍盖住页面（含阅读层顶栏）。
  function showRuntimeUnsupported(missing) {
    try {
      if (document.querySelector('[data-pixiv-rescue-runtime]')) return;
      const bar = document.createElement('div');
      bar.setAttribute('data-pixiv-rescue-runtime', '');
      bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;padding:10px 14px;background:#fef2f2;color:#991b1b;font:13px/1.6 -apple-system,sans-serif;border-bottom:1px solid #fca5a5';
      bar.textContent = '救援脚本需要脚本管理器支持 ' + missing.join('、') +
        '。当前浏览器/管理器未提供，救援功能不可用。请换用支持完整 GM API 的管理器（如 Tampermonkey）。';
      const close = document.createElement('button');
      close.type = 'button';
      close.textContent = '知道了';
      close.style.cssText = 'margin-left:8px;padding:0 10px;border:1px solid #fca5a5;border-radius:4px;background:#fff;color:#991b1b;font:inherit;cursor:pointer';
      close.addEventListener('click', function () { bar.remove(); });
      bar.append(close);
      (document.body || document.documentElement).prepend(bar);
    } catch (_error) { /* ignore */ }
  }

  // GM API 就绪：撤掉页面上残留的提示条。本实例只在放弃后才提示、放弃后不再重试，
  // 残留只可能来自同一页面上另一份已经放弃的脚本实例
  function clearRuntimeUnsupported() {
    try {
      document.querySelectorAll('[data-pixiv-rescue-runtime]').forEach(function (node) { node.remove(); });
    } catch (_error) { /* ignore */ }
  }

  function checkRuntimeSupport() {
    if (missingRuntimeApis().length) return false;
    clearRuntimeUnsupported();
    // GM_registerMenuCommand、GM_setValue 缺失不致命，由 registerTokenMenus 就地降级（见那里）
    return true;
  }

  // GM_registerMenuCommand 缺失不致命（只是没有菜单入口，Token 还能通过管理器自带的方式设置），
  // 但直接调用会抛错、让后面所有初始化（包括页面处理）全部不执行——这同样是「页面零反应」的一种
  // 来源。降级为无菜单模式继续运行。GM_setValue 的其余调用点都在 try 里，存不下只影响缓存。
  // 兜底用局部变量：0.12.1 及以前写 window.GM_xxx = 空函数，一来污染页面（别的脚本会以为真有
  // 这个 API），二来管理器若把没实现的 API 作为值为 undefined 的局部变量传进脚本，这里调用的是
  // 那个局部变量，写 window 根本兜不住，照样抛错
  function registerTokenMenus() {
    const register = typeof GM_registerMenuCommand === 'function' ? GM_registerMenuCommand : null;
    const save = typeof GM_setValue === 'function' ? GM_setValue : null;
    if (!register) return;
    // 存不了就如实说，不再弹「已保存」（0.12.1 及以前兜底成空函数后照样提示已保存）
    const store = function (value, done) {
      if (!save) {
        window.alert('当前脚本管理器不支持保存（缺 GM_setValue）。请在管理器里直接编辑本脚本的存储。');
        return;
      }
      save(TOKEN_KEY, value);
      window.alert(done);
    };
    register('设置或更新救援 Token', function () {
      const value = window.prompt('请输入救援 API Token。Token 只会保存在油猴脚本存储中。', '');
      if (value === null) return;
      const token = String(value).trim();
      if (!token) {
        window.alert('Token 不能为空。');
        return;
      }
      store(token, '救援 Token 已保存。');
    });
    register('清除救援 Token', function () {
      store('', '救援 Token 已清除。');
    });
  }

  // timeoutMs：整本正文可达 20 万字（JSON 约 216 KB），手机网络 15 秒不够稳，
  // 正文接口由调用方放宽；列表 lookup 等小请求保持 15 秒。
  const FULL_TEXT_TIMEOUT_MS = 30000;
  // 先按状态码分类，2xx 才解析 JSON。0.12.0 及以前先 JSON.parse：部署重启时 Nginx 502、
  // Cloudflare 52x 回的是 HTML 错误页，解析失败被判成 invalid_response——阅读层显示
  // 「备份数据没有完整读到」，列表页的提示白名单里没有这一项，干脆什么都不显示。
  function apiErrorKind(status) {
    if (status === 401) return 'unauthorized';
    if (status === 404) return 'not_found';
    if (status === 429) return 'rate_limited';
    if (status >= 500) return 'server_error';
    return 'request_failed';
  }

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
          if (response.status < 200 || response.status >= 300) {
            reject(new Error(apiErrorKind(Number(response.status || 0))));
            return;
          }
          let envelope;
          try {
            envelope = JSON.parse(response.responseText || '{}');
          } catch (_error) {
            // 2xx 却不是完整 JSON：部分手机管理器会截断大应答（整本正文约 216 KB）
            reject(new Error('invalid_response'));
            return;
          }
          if (envelope.ok === false) {
            reject(new Error('request_failed'));
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
            // 与 apiGet 同一套分类：429 要认出来，封面被限流时等卡片下次进入视口再取
            reject(new Error(apiErrorKind(Number(response.status || 0))));
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
    // 新作页在 document-start 就开始观察：那时可能连 <html> 都还没解析出来，
    // 没有挂载点就等下一次调用（观察器每批变动都会再进来）
    const host = document.head || document.documentElement;
    if (!host) return;
    const style = document.createElement('style');
    style.setAttribute('data-pixiv-rescue-styles', '');
    style.textContent = [
      // 正文/系列阅读层：脚本自有的固定全屏层，自带滚动，不进 Pixiv 页面流。
      // 生产根因（2026-10-23 真机页实测）：手机站已删作品页是 SPA 404 空壳，
      // 一个 position:absolute、不透明背景（rgb(245,245,245)）的通栏容器从
      // y=48 起铺满 2000+px；0.7.x 把面板当普通块元素追加到 body，按 CSS 绘制
      // 顺序被它整块盖住——20 万字全在 DOM 里，用户什么都看不到。
      '.pixiv-rescue-root{position:fixed;top:0;right:0;bottom:0;left:0;z-index:2147483600;box-sizing:border-box;margin:0;padding:0;overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain;background:#fff;color:#1f1f1f;font-family:Helvetica,"Hiragino Kaku Gothic Pro","Segoe UI","Yu Gothic",Meiryo,sans-serif;text-align:left;-webkit-text-size-adjust:100%}',
      // 收起态：只剩底部一颗「查看私人备份」胶囊，Pixiv 原页面完全可用
      '.pixiv-rescue-root.pixiv-rescue-root--minimized{top:auto;right:auto;bottom:16px;bottom:calc(16px + env(safe-area-inset-bottom, 0px));left:50%;transform:translateX(-50%);overflow:visible;background:transparent}',
      '.pixiv-rescue-root--minimized .pixiv-rescue-reader-bar,.pixiv-rescue-root--minimized .pixiv-rescue-reader-body{display:none}',
      '.pixiv-rescue-reopen{display:none;min-height:40px;padding:8px 18px;border:0;border-radius:999px;background:#0096fa;color:#fff;font-size:14px;font-weight:700;box-shadow:0 6px 18px rgba(0,0,0,.25);cursor:pointer;white-space:nowrap}',
      '.pixiv-rescue-root--minimized .pixiv-rescue-reopen{display:inline-flex;align-items:center}',
      // 顶栏仿手机站 48px 白底导航条：左边「拯救数据」标识，右边退回 Pixiv
      '.pixiv-rescue-reader-bar{position:sticky;top:0;z-index:2;box-sizing:border-box;display:flex;align-items:center;justify-content:space-between;gap:8px;height:48px;padding:0 12px 0 16px;border-bottom:1px solid rgba(0,0,0,.08);background:#fff}',
      '.pixiv-rescue-reader-label{display:flex;align-items:center;gap:8px;min-width:0;font-size:13px;color:#858585;white-space:nowrap;overflow:hidden}',
      '.pixiv-rescue-kicker{display:inline-flex;align-items:center;padding:2px 8px;border-radius:4px;background:#0096fa;color:#fff;font-size:12px;font-weight:700;line-height:18px}',
      '.pixiv-rescue-reader-body{box-sizing:border-box;margin:0;padding:0 0 48px}',
      '.pixiv-rescue-panel{background:#fff}',
      // 作品信息区：尺寸与层级照 2026-10-23 手机站实测（标题 14px/700/22px，
      // 简介 12px 灰，标签 12px，日期 10px #858585，封面 128×180 上圆角 8px）
      '.pixiv-rescue-author{box-sizing:border-box;display:flex;align-items:center;gap:8px;height:56px;padding:0 16px;font-size:14px;font-weight:700;line-height:22px;color:#1f1f1f}',
      '.pixiv-rescue-author-avatar{flex:0 0 40px;width:40px;height:40px;border-radius:50%;background:#e8e8e8}',
      '.pixiv-rescue-author-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
      '.pixiv-rescue-cover-band{position:relative;padding:16px 0 0;text-align:center;background:linear-gradient(135deg,#f0f0f0 50%,#e6e6e6 50%)}',
      '.pixiv-rescue-cover-img{display:block;width:128px;height:180px;margin:0 auto;object-fit:cover;border-radius:8px 8px 0 0;background:#e0e0e0}',
      '.pixiv-rescue-text-length{position:absolute;top:8px;right:8px;padding:0 8px;border-radius:16px;background:rgba(0,0,0,.2);color:#fff;font-size:10px;font-weight:700;line-height:16px}',
      '.pixiv-rescue-title{margin:16px 16px 0;font-size:14px;font-weight:700;line-height:22px;color:#1f1f1f;overflow-wrap:anywhere}',
      '.pixiv-rescue-caption{margin:4px 16px 0;font-size:12px;line-height:18px;color:#5c5c5c;white-space:pre-wrap;overflow-wrap:anywhere}',
      '.pixiv-rescue-caption--collapsed{max-height:36px;overflow:hidden}',
      '.pixiv-rescue-caption-more{display:block;margin:2px 16px 0 auto;padding:0;border:0;background:none;color:#858585;font-size:12px;line-height:18px;cursor:pointer}',
      '.pixiv-rescue-tags{margin:12px 16px 0;font-size:12px;line-height:16px;color:#858585;overflow-wrap:anywhere}',
      '.pixiv-rescue-tag{margin-right:8px;color:#205f82}',
      '.pixiv-rescue-tag-r18{margin-right:8px;color:#ff4060;font-weight:700}',
      '.pixiv-rescue-tag-translation{margin-left:4px;color:#858585;font-size:10px}',
      '.pixiv-rescue-date{margin:12px 16px 0;font-size:10px;line-height:14px;color:#858585}',
      '.pixiv-rescue-notice{margin:4px 16px 0;font-size:10px;line-height:14px;color:#adadad}',
      // 按钮仿手机站「已关注」那种浅灰圆角胶囊
      '.pixiv-rescue-button{display:inline-flex;align-items:center;justify-content:center;min-height:32px;margin:4px 8px 4px 0;padding:0 16px;border:0;border-radius:16px;background:#f2f2f2;color:#1f1f1f;font-size:14px;font-weight:700;line-height:20px;cursor:pointer}',
      '.pixiv-rescue-button:disabled{cursor:wait;opacity:.55}',
      '.pixiv-rescue-reader-bar .pixiv-rescue-button{flex:0 0 auto;margin:0;font-size:13px}',
      // 翻页条照手机站 nav.novel-pager-container：行高 45px、上下 1px 浅分隔线、#474747
      '.pixiv-rescue-pager{box-sizing:border-box;display:flex;align-items:center;justify-content:space-between;margin:16px 0 0;padding:0 8px;border-top:1px solid rgba(0,0,0,.08);border-bottom:1px solid rgba(0,0,0,.08);color:#474747;font-size:16px;line-height:45px;scroll-margin-top:48px}',
      '.pixiv-rescue-pager--bottom{margin:0}',
      '.pixiv-rescue-pager-label{flex:1 1 auto;text-align:center}',
      '.pixiv-rescue-pager-button{flex:0 0 64px;margin:0;padding:0;border:0;background:none;color:#474747;font-size:14px;line-height:45px;cursor:pointer}',
      '.pixiv-rescue-pager-button:disabled,.pixiv-rescue-pager--single .pixiv-rescue-pager-button{visibility:hidden}',
      // 正文容器逐项照搬 2026-10-23 实测的 Pixiv 手机站 #novel-text-container
      // （默认 Aa 设置 background-0/size-1/spacing-1）：16px、行高 1.8、#1f1f1f、
      // 上下内边距 40px、pre-wrap；段落 p.novel-paragraph 底距 1.5em、左右 16px；
      // 章节标题 h2.novel-chapter 20px/700/#969696、上下 1em、左右 26px。
      // 每条都显式写死 font/margin：Pixiv 的全站样式会改 p/h2/b/i 的默认值。
      '.pixiv-rescue-novel-text{box-sizing:border-box;width:100%;margin:0;padding:40px 0;background:#fff;color:#1f1f1f;font-family:' + PIXIV_GOTHIC_FONT + ';font-size:16px;font-weight:400;line-height:1.8;letter-spacing:normal;text-align:left;white-space:pre-wrap;overflow-wrap:break-word;word-break:normal}',
      '.pixiv-rescue-text-start{height:1px}',
      '.pixiv-rescue-novel-text .pixiv-rescue-para{margin:0 0 1.5em;padding:0 16px;font:inherit;font-size:inherit;line-height:inherit;color:inherit;white-space:pre-wrap}',
      '.pixiv-rescue-novel-text .pixiv-rescue-chapter{margin:1em 0;padding:0 26px;font-family:inherit;font-size:20px;font-weight:700;line-height:1.8;color:#969696;white-space:pre-wrap}',
      '.pixiv-rescue-novel-text b{font-weight:700}',
      '.pixiv-rescue-novel-text i{font-style:italic}',
      '.pixiv-rescue-novel-text ruby{white-space:normal}',
      '.pixiv-rescue-novel-text rt{font-size:50%;line-height:normal}',
      '.pixiv-rescue-novel-text .pixiv-rescue-jump{color:inherit;text-decoration:underline;cursor:pointer}',
      '.pixiv-rescue-image-missing{margin:0 16px 1.5em;padding:24px 16px;border-radius:4px;background:rgba(0,0,0,.04);color:#858585;font-size:12px;line-height:18px;text-align:center;white-space:normal}',
      '.pixiv-rescue-image-missing a{color:#205f82}',
      // Aa 阅读设置的五种背景、三档字号、两档行距（同样实测自手机站 CSS）
      '.pixiv-rescue-novel-text.background-1{background:#1f1f1f;color:#b7b7b7}',
      '.pixiv-rescue-novel-text.background-2{background:#edf7ff;color:#4d3e2a}',
      '.pixiv-rescue-novel-text.background-3{background:#fbf0d9;color:#4d3e2a}',
      '.pixiv-rescue-novel-text.background-4{background:#f5f5f5;color:#333}',
      '.pixiv-rescue-novel-text.size-0{font-size:12.8px}',
      '.pixiv-rescue-novel-text.size-2{font-size:19.2px}',
      '.pixiv-rescue-novel-text.spacing-0{line-height:normal}',
      // 系列页：目录与章节阅读区沿用同一套排版
      // 正文页信息区里的系列名（标题上方一行灰字）与收藏/浏览快照（实测 .work-stats）
      '.pixiv-rescue-series-link{display:block;margin:16px 16px 0;font-size:12px;line-height:20px;color:#858585;text-decoration:none}',
      '.pixiv-rescue-series-link + .pixiv-rescue-title{margin-top:0}',
      '.pixiv-rescue-stats{margin:10px 16px 0;font-size:12px;line-height:16px;color:#858585}',
      '.pixiv-rescue-stat{display:inline-flex;align-items:center;gap:4px;margin-right:16px;vertical-align:middle}',
      '.pixiv-rescue-stat-icon{flex:0 0 auto;fill:currentColor}',
      '.pixiv-rescue-stats + .pixiv-rescue-date{margin-top:12px}',
      '.pixiv-rescue-tag-flag{margin-right:8px;color:#205f82;font-weight:700}',
      // 章节末尾系列卡（实测 .novel-series-navigation：rgba(0,0,0,.04) 底、8px 圆角、
      // 内边距 4/16/24px；按钮 14px/700/22px、#474747、圆角 100px、高 40px）
      '.pixiv-rescue-series-card{margin:16px 16px 24px;padding:4px 16px 24px;border-radius:8px;background:rgba(0,0,0,.04)}',
      '.pixiv-rescue-series-card-label{margin-top:4px;font-size:12px;line-height:20px;color:#858585;text-align:center}',
      '.pixiv-rescue-series-card-title{display:block;font-size:14px;font-weight:700;line-height:22px;color:#1f1f1f;text-align:center;text-decoration:none;overflow-wrap:anywhere}',
      '.pixiv-rescue-series-card-nav{margin-top:20px}',
      '.pixiv-rescue-nav-button{display:block;box-sizing:border-box;margin:0;padding:9px 24px;border-radius:100px;background:rgba(0,0,0,.04);color:#474747;font-size:14px;font-weight:700;line-height:22px;text-align:center;text-decoration:none}',
      '.pixiv-rescue-nav-button + .pixiv-rescue-nav-button{margin-top:8px}',
      // 系列页头部（实测 .series-info-container / .text-information / a.read-link）
      '.pixiv-rescue-series-head{padding:16px 16px 0}',
      '.pixiv-rescue-series-badges{display:flex;gap:2px;margin-bottom:12px}',
      '.pixiv-rescue-badge{display:inline-block;padding:3px 6px;border-radius:3px;color:#fff;font-size:10px;font-weight:700;line-height:10px}',
      '.pixiv-rescue-badge-r18{background:#ff4060}',
      '.pixiv-rescue-badge-original{background:#858585}',
      '.pixiv-rescue-series-head .pixiv-rescue-title{margin:0;font-size:16px;line-height:24px}',
      '.pixiv-rescue-series-info{display:flex;flex-wrap:wrap;margin-top:8px;font-size:12px;line-height:16px;color:#858585}',
      '.pixiv-rescue-series-info-item{margin-right:8px}',
      '.pixiv-rescue-series-head .pixiv-rescue-notice{margin:4px 0 0}',
      '.pixiv-rescue-read-actions{padding:20px 0}',
      '.pixiv-rescue-read-link{display:block;box-sizing:border-box;padding:9px 24px;border-radius:100px;background:rgba(0,0,0,.88);color:#fff;font-size:14px;font-weight:700;line-height:22px;text-align:center;text-decoration:none}',
      '.pixiv-rescue-read-link--secondary{margin-top:8px;background:rgba(0,0,0,.04);color:#474747}',
      '.pixiv-rescue-series-tab{margin:0 16px;text-align:center}',
      '.pixiv-rescue-series-tab-label{display:inline-block;padding:13px 16px 11px;border-bottom:2px solid #0096fa;font-size:14px;font-weight:700;line-height:22px;color:#1f1f1f}',
      // 章节卡片（实测 .works-item-novel.is-dense：卡片宽 364px、64×90 封面、文字列左距 16px；
      // 第二张起上外边距 22px + 上内边距 14px，相邻卡片间距 138px；分隔线占 1px，内边距让出 1px）
      '.pixiv-rescue-directory{margin:16px 24px 0;padding:0;list-style:none}',
      '.pixiv-rescue-chapter-item{margin:22px 0 0;padding:13px 0 0;border-top:1px solid rgba(0,0,0,.08)}',
      '.pixiv-rescue-chapter-item:first-child{margin:0;padding:0;border-top:0}',
      // 卡片最矮 102px（实测封面列 64×102：90px 封面 + 12px 留白）；内容不够高时
      // 余量落在「浏览更多」上方，它贴底（实测短卡片 浏览更多 距封面顶 87px）
      '.pixiv-rescue-chapter-card{position:relative;display:flex;align-items:stretch;min-height:102px;color:inherit;text-decoration:none}',
      '.pixiv-rescue-chapter-cover{flex:0 0 64px;align-self:flex-start;width:64px;height:90px;border-radius:4px;overflow:hidden;background:#f0f0f0}',
      '.pixiv-rescue-chapter-thumb{display:block;width:100%;height:100%;object-fit:cover}',
      '.pixiv-rescue-chapter-cover--plain{box-sizing:border-box;display:flex;align-items:center;justify-content:center;padding:8px 6px;border:3px double #c9bb98;background:#f3ecd9}',
      '.pixiv-rescue-chapter-cover-title{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:4;overflow:hidden;color:#5c5240;font-family:"Noto Serif CJK SC","Source Han Serif SC",serif;font-size:9px;line-height:13px;text-align:center;overflow-wrap:anywhere}',
      '.pixiv-rescue-chapter-info{display:flex;flex-direction:column;flex:1 1 auto;min-width:0;padding-left:16px}',
      '.pixiv-rescue-chapter-detail{display:flex;flex-direction:column;justify-content:space-between;flex:1 1 auto;align-items:flex-start;padding-right:40px}',
      '.pixiv-rescue-chapter-more{display:block;margin-top:8px;font-size:12px;line-height:16px;color:#858585;cursor:pointer}',
      '.pixiv-rescue-chapter-heart{position:absolute;right:0;bottom:6px;width:32px;height:32px;color:#1f1f1f;pointer-events:none}',
      '.pixiv-rescue-chapter-heart svg{display:block}',
      // 「作品情报」底部面板（实测 .bottom-popup：半透明遮罩、白底、标题 16px/700、全宽「阅读作品」）
      '.pixiv-rescue-sheet-backdrop{position:fixed;top:0;right:0;bottom:0;left:0;z-index:5;background:rgba(0,0,0,.4)}',
      '.pixiv-rescue-sheet{position:absolute;right:0;bottom:0;left:0;padding:0 16px 32px;padding-bottom:calc(32px + env(safe-area-inset-bottom, 0px));background:#fff}',
      '.pixiv-rescue-sheet-head{position:relative;padding:12px 40px;text-align:center}',
      '.pixiv-rescue-sheet-title{font-size:16px;font-weight:700;line-height:24px;color:#1f1f1f}',
      '.pixiv-rescue-sheet-close{position:absolute;top:8px;right:0;width:32px;height:32px;padding:0;border:0;background:none;color:#858585;font-size:28px;line-height:32px;cursor:pointer}',
      '.pixiv-rescue-sheet-tags{margin:16px 0 0;font-size:12px;line-height:16px;color:#3d7699;overflow-wrap:anywhere}',
      '.pixiv-rescue-sheet-tags .pixiv-rescue-tag,.pixiv-rescue-sheet-tags .pixiv-rescue-tag-flag{color:#3d7699}',
      '.pixiv-rescue-sheet .pixiv-rescue-read-link{margin-top:16px}',
      '.pixiv-rescue-chapter-title{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;max-height:44px;overflow:hidden;font-size:14px;font-weight:700;line-height:22px;color:#1f1f1f;overflow-wrap:anywhere}',
      '.pixiv-rescue-chapter-tags{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;align-self:stretch;max-height:60px;overflow:hidden;font-size:12px;line-height:20px;color:#3d7699;overflow-wrap:anywhere}',
      '.pixiv-rescue-chapter-tags .pixiv-rescue-tag,.pixiv-rescue-chapter-tags .pixiv-rescue-tag-flag{color:#3d7699}',
      '.pixiv-rescue-chapter-stats{display:flex;flex-wrap:wrap;align-items:center;font-size:12px;line-height:20px;color:#858585}',
      '.pixiv-rescue-chapter-stat{display:inline-flex;align-items:center;gap:4px;margin-right:8px}',
      '.pixiv-rescue-chapter-empty{padding:24px 0;color:#858585;font-size:14px;text-align:center}',
      '.pixiv-rescue-directory [data-rescue-load-more]{margin-top:22px;padding:0;text-align:center}',
      '.pixiv-rescue-pending{padding:48px 16px;color:#858585;font-size:14px;line-height:22px;text-align:center}',
      '.pixiv-rescue-meta{margin:4px 16px 0;color:#858585;font-size:12px;line-height:18px}',
      '.pixiv-rescue-error{margin:16px;padding:10px 14px;border:1px solid #fca5a5;border-radius:6px;background:#fef2f2;color:#991b1b;font-size:13px;line-height:1.6}',
      // 目录错误里的「重试」：照提示条里「知道了」的白底胶囊
      '.pixiv-rescue-directory-error .pixiv-rescue-button{margin:8px 0 0;background:#fff}',
      '.pixiv-rescue-reader-body > .pixiv-rescue-button{margin:0 16px}',
      // PC 站（非手机 UA）：照 PC 阅读器——灰底上居中 704px 白卡，正文列 620px、
      // 行高 32px、两端对齐、段距 32px（实测 PC 版 p 上下外边距 31/32px 折叠）
      '.pixiv-rescue-root--pc{background:#f5f5f5}',
      '.pixiv-rescue-root--pc .pixiv-rescue-reader-body{max-width:704px;margin:0 auto;background:#fff}',
      '.pixiv-rescue-root--pc .pixiv-rescue-novel-text{padding:40px 42px;line-height:2;text-align:justify}',
      '.pixiv-rescue-root--pc .pixiv-rescue-novel-text .pixiv-rescue-para{margin:0 0 2em;padding:0}',
      '.pixiv-rescue-root--pc .pixiv-rescue-novel-text .pixiv-rescue-chapter{padding:0}',
      '.pixiv-rescue-root--pc .pixiv-rescue-novel-text.spacing-0{line-height:normal}',
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
      // 0.12.0 系列卡片（拯救系列 tab 与追更列表填空共用）：逐项照搬 2026-10-26 实测的手机站
      // .novel-series-thumbnail-detail——封面 64×90 圆角 8，文字列左距 16；R-18 角标行高 20、
      // 标题 14px/700/22px 两行截断、作者行 20、「10话 2026年9月19日更新」12px/20px、
      // 「浏览更多」12px/16px（所在行沿用继承的行高 1.15，高 18.4）、「阅读最新话」上距 12。
      // 单行标题带角标时卡片高 152，与 Pixiv 健康卡片相同。
      '.pixiv-rescue-series-row{position:relative;box-sizing:border-box;display:flex;margin:0;padding:0;color:#1f1f1f;font-size:16px;font-weight:400;line-height:1.15;text-align:left}',
      '.pixiv-rescue-series-row a{text-decoration:none}',
      '.pixiv-rescue-series-row-cover{position:relative;box-sizing:border-box;display:flex;flex:0 0 64px;width:64px;height:90px;overflow:hidden;border-radius:8px}',
      '.pixiv-rescue-series-row-img{display:block;width:64px;height:90px;object-fit:cover;background:rgba(0,0,0,.02)}',
      '.pixiv-rescue-series-row-details{flex:1 1 auto;min-width:0;margin-left:16px}',
      '.pixiv-rescue-series-row-badges{margin:-4px -2px 4px;line-height:0}',
      '.pixiv-rescue-series-row-badge{display:inline-block;margin:4px 2px 0;padding:3px 6px;border-radius:3px;background:#ff4060;color:#fff;font-size:10px;font-weight:700;line-height:10px}',
      '.pixiv-rescue-series-row-title{display:block;color:#1f1f1f}',
      '.pixiv-rescue-series-row-title-text{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;max-height:44px;overflow:hidden;color:#1f1f1f;font-size:14px;font-weight:700;line-height:22px;overflow-wrap:anywhere}',
      '.pixiv-rescue-series-row-user{display:flex;align-items:center;width:fit-content;max-width:100%;height:20px}',
      '.pixiv-rescue-series-row-avatar{display:block;flex:0 0 16px;width:16px;height:16px;border-radius:50%;background:#e8e8e8;object-fit:cover}',
      '.pixiv-rescue-series-row-name{min-width:0;margin-left:4px;overflow:hidden;color:#474747;font-size:12px;line-height:20px;text-overflow:ellipsis;white-space:nowrap}',
      '.pixiv-rescue-series-row-meta{color:#858585;font-size:12px;line-height:20px}',
      '.pixiv-rescue-series-row-meta span + span{margin-left:8px}',
      '.pixiv-rescue-series-row-more-line{margin-top:8px;font-size:16px;line-height:1.15}',
      '.pixiv-rescue-series-row-more{color:#858585;font-size:12px;line-height:16px;cursor:pointer}',
      '.pixiv-rescue-series-row-actions{display:flex;align-items:center;margin-top:12px}',
      '.pixiv-rescue-series-row-read{display:inline-block;padding:5px 16px;border-radius:100px;background:rgba(0,0,0,.88);color:#fff;font-size:14px;font-weight:700;line-height:22px;white-space:nowrap}',
      '.pixiv-rescue-series-row-menu{display:block;flex:0 0 24px;width:24px;height:24px;margin:0 0 0 16px;padding:0;border:0;background:none;cursor:pointer}',
      '.pixiv-rescue-series-row-menu svg{display:block}',
      // 拯救系列 tab 的列表照 Pixiv 追更列表：外层上下 16 + 左右 16，每项 卡片 + 16 + 分隔线 + 16，
      // 卡距 185；容器自带上内边距 16（相当于 Pixiv 子 tab 下的 16px 间隔块），列表顶落在 y=160
      '.pixiv-rescue-feed{box-sizing:border-box;margin:0;padding:16px 0 0;color:#1f1f1f;font-size:16px;line-height:1.15;text-align:left}',
      '.pixiv-rescue-feed-list{margin:16px 0;padding:0 16px}',
      '.pixiv-rescue-feed-item{margin:0 0 16px}',
      '.pixiv-rescue-feed-item > .pixiv-rescue-series-row{margin-bottom:16px}',
      '.pixiv-rescue-divider{box-sizing:content-box;display:block;height:0;margin:0;padding:0;overflow:visible;border:0;border-top:1px solid rgba(0,0,0,.08)}',
      // 空状态照 Pixiv 的 .notifications：12px/700 #858585，容器内边距 10，段落上下 12
      '.pixiv-rescue-feed-empty{padding:10px;color:#858585;font-size:12px;font-weight:700;line-height:1.15;text-align:center}',
      '.pixiv-rescue-feed-empty p{margin:12px 0;color:inherit;font:inherit}',
      '.pixiv-rescue-feed-retry{display:inline-block;margin:0 0 12px;padding:5px 16px;border:0;border-radius:100px;background:rgba(0,0,0,.88);color:#fff;font-size:14px;font-weight:700;line-height:22px;cursor:pointer}',
      // 翻页条照 Pixiv 的 nav.pager.add-margin-above（上距 36）：蓝条高 35；左右箭头宽 46、
      // 内侧 1px 白边、高 34、上距 1；页码圆 24×24、左右 5，当前页白底黑字。
      // 类名另起：.pixiv-rescue-pager 是正文阅读层的翻页条
      '.pixiv-rescue-feed-pager{display:block;margin:36px 0 0;padding:0;font-size:16px;line-height:1.15}',
      '.pixiv-rescue-feed-pager-bar{height:35px;background:#4a90e2;text-align:center}',
      '.pixiv-rescue-feed-pager-arrow{display:block;box-sizing:content-box;width:46px;height:34px;margin:1px 0 0;color:#fff;font-size:18px;font-weight:700;line-height:32px;text-align:center;text-decoration:none}',
      '.pixiv-rescue-feed-pager-prev{float:left;border-right:1px solid #fff}',
      '.pixiv-rescue-feed-pager-next{float:right;border-left:1px solid #fff}',
      '.pixiv-rescue-feed-pager-icon{display:inline-flex;align-items:center;justify-content:center;width:25px;height:34px;vertical-align:top}',
      '.pixiv-rescue-feed-pager-icon svg{display:block;width:10px;height:18px}',
      '.pixiv-rescue-feed-pagenum{margin:0;padding:0;list-style:none;line-height:35px}',
      '.pixiv-rescue-feed-pagenum li{display:inline-block;width:34px;margin:0;padding:0}',
      '.pixiv-rescue-feed-pagenum a{display:inline-block;width:24px;height:24px;margin:0 5px;border-radius:24px;color:#fff;font-size:14px;font-weight:700;line-height:24px;text-align:center;text-decoration:none}',
      '.pixiv-rescue-feed-pagenum a[aria-current="page"]{background:#fff;color:#000}',
      // 加载中：蓝条（高 36）里居中一个 30×30、12 根 4×8 白条的转圈（实测 .spinnercontainer）
      '.pixiv-rescue-feed-spinner{height:36px;background:#4a90e2;line-height:0;text-align:center}',
      '.pixiv-rescue-feed-spinner-box{position:relative;display:inline-block;width:30px;height:30px;margin-top:2px;vertical-align:top}',
      '.pixiv-rescue-feed-spinner-box span{position:absolute;top:0;left:13px;width:4px;height:8px;border-radius:50px;background:#fff;transform-origin:2px 15px;animation:pixiv-rescue-feed-spin 1s linear infinite}',
      '@keyframes pixiv-rescue-feed-spin{from{opacity:1}to{opacity:.25}}',
      // 接管后的原生内容与填空后的空白框：只用脚本自己的属性挂样式——Vue 打补丁会整串
      // 重写 class，属性它不认识就不碰
      '[data-pixiv-rescue-hidden]{display:none !important}',
      '[data-pixiv-rescue-filled]{padding:0 !important}',
      '[data-pixiv-rescue-filled] > .cover,[data-pixiv-rescue-filled] > .details{display:none !important}',
      // 列表页的「作品情报」面板挂在 body 上：遮罩要盖过 Pixiv 页头
      '.pixiv-rescue-sheet-backdrop--page{z-index:2147483500}',
      // 提示条同样固定定位：插进页面流会被 404 空壳的绝对定位背景盖住
      '.pixiv-rescue-notice-bar{position:fixed;left:12px;right:12px;bottom:12px;z-index:2147483640;display:flex;align-items:center;gap:12px;justify-content:center;max-width:760px;margin:0 auto;padding:10px 14px;border:1px solid #fca5a5;border-radius:6px;background:#fef2f2;color:#991b1b;font-size:13px;line-height:1.6;box-shadow:0 6px 18px rgba(17,24,39,.15)}',
      '.pixiv-rescue-notice-bar .pixiv-rescue-button{flex:0 0 auto;margin:0;background:#fff}'
    ].join('');
    host.append(style);
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
      if (!isMobileUserAgent()) root.classList.add('pixiv-rescue-root--pc');
      const bar = element('div', 'pixiv-rescue-reader-bar');
      const label = element('span', 'pixiv-rescue-reader-label');
      label.append(element('span', 'pixiv-rescue-kicker', '拯救数据'), document.createTextNode('私人备份阅读'));
      bar.append(label);
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

  // 手机 UA 拿到的是 Pixiv 手机站（touch），其余是 PC 站：两边正文排版不同
  function isMobileUserAgent() {
    return /Android|iPhone|iPad|iPod|Mobile|HarmonyOS/i.test(navigator.userAgent || '');
  }

  // 请求还没回来时的占位：用户立刻看到「脚本已经在工作」，而不是盯着空壳页面
  function renderPending() {
    const body = openReader();
    const panel = element('div', 'pixiv-rescue-panel');
    panel.append(element('p', 'pixiv-rescue-pending', '正在读取备份…'));
    body.append(panel);
  }

  function renderNotice(panel, data) {
    panel.append(element(
      'div',
      'pixiv-rescue-notice',
      data.source_notice || '内容来自私人备份，并非 Pixiv 官方恢复'
    ));
  }

  // 正文页按 Pixiv 手机站的版式重建：作者、封面、标题、简介、标签、日期，
  // 再是「1 / N 页」与正文。只画备份里真有的数据，没有的（头像、收藏数、
  // 浏览数、工具栏）不伪造。
  function renderNovel(data) {
    const body = openReader();
    const panel = element('div', 'pixiv-rescue-panel');
    renderWorkHeader(panel, data, { cover: true });
    renderPixivText(panel, data.text_raw);
    // 系列章节：文末照 Pixiv 放系列卡（「下一话 #N」「上一话 #N」），并记下阅读进度
    if (data.series_nav) {
      renderSeriesNavCard(panel, data.series_nav);
      saveSeriesProgress(data.series_nav, data.novel_id);
    }
    body.append(panel);
  }

  function rescueNovelUrl(novelId) {
    return '/novel/show.php?id=' + encodeURIComponent(String(novelId)) + '&pixiv_rescue=1';
  }

  function rescueSeriesUrl(seriesId) {
    return '/novel/series/' + encodeURIComponent(String(seriesId)) + '?pixiv_rescue=1';
  }

  function formatCount(value) {
    return Number(value || 0).toLocaleString('en-US');
  }

  // 阅读时长照 Pixiv：readingTime = 字数 × 0.15 秒（2026-10-26 实测手机站
  // series_content 接口，6 章比值全是 0.15），取整到分钟显示「2小时53分钟」「14分钟」；
  // 不足 1 分钟不显示（Pixiv 的短篇卡片同样只显示字数）
  function formatReadingTime(textLength) {
    const minutes = Math.floor(Number(textLength || 0) * 0.15 / 60);
    if (minutes < 1) return '';
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    return hours ? hours + '小时' + (rest ? rest + '分钟' : '') : rest + '分钟';
  }

  // 收藏（心形）与浏览（眼睛）图标：SVG 节点逐个创建，不拼 HTML
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const STAT_ICON_PATHS = {
    like: 'M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z',
    view: 'M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z'
  };

  function statIcon(kind) {
    const size = kind === 'view' ? '14' : '12';
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', size);
    svg.setAttribute('height', size);
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('class', 'pixiv-rescue-stat-icon');
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('fill', 'currentColor');
    path.setAttribute('d', STAT_ICON_PATHS[kind]);
    svg.append(path);
    return svg;
  }

  function statItem(kind, value) {
    const stat = element('span', 'pixiv-rescue-stat');
    stat.append(statIcon(kind), element('span', 'pixiv-rescue-stat-count', formatCount(value)));
    return stat;
  }

  // 正文页的收藏/浏览数：同步那一刻的快照（作品删了，Pixiv 上已经看不到了）
  function renderStatsRow(container, data) {
    if (data.total_bookmarks == null && data.total_views == null) return;
    const row = element('div', 'pixiv-rescue-stats');
    if (data.total_bookmarks != null) row.append(statItem('like', data.total_bookmarks));
    if (data.total_views != null) row.append(statItem('view', data.total_views));
    container.append(row);
  }

  // 章节末尾的系列卡照 Pixiv 手机站 .novel-series-navigation：灰底圆角卡里
  // 「系列」、系列名，再是「下一话 #N」「上一话 #N」两颗胶囊（下一话在上）
  function renderSeriesNavCard(container, nav) {
    const card = element('div', 'pixiv-rescue-series-card');
    card.append(element('div', 'pixiv-rescue-series-card-label', '系列'));
    const title = element('a', 'pixiv-rescue-series-card-title', nav.series_title || '未命名系列');
    title.href = rescueSeriesUrl(nav.series_id);
    card.append(title);
    const buttons = element('div', 'pixiv-rescue-series-card-nav');
    [['下一话', nav.next], ['上一话', nav.prev]].forEach(function (pair) {
      if (!pair[1]) return;
      const link = element('a', 'pixiv-rescue-nav-button', pair[0] + ' #' + pair[1].chapter_number);
      link.href = rescueNovelUrl(pair[1].novel_id);
      buttons.append(link);
    });
    if (buttons.children.length) card.append(buttons);
    container.append(card);
  }

  // 系列阅读进度（系列页的「继续阅读 #N」）：只存在脚本自己的存储里，最多记 200 个系列
  function readSeriesProgressMap() {
    try {
      const map = JSON.parse(GM_getValue(SERIES_PROGRESS_KEY, '') || '{}');
      return map && typeof map === 'object' && !Array.isArray(map) ? map : {};
    } catch (_error) {
      return {};
    }
  }

  function readSeriesProgress(seriesId) {
    const entry = readSeriesProgressMap()[String(seriesId)];
    return entry && /^\d+$/.test(String(entry.novel_id)) ? entry : null;
  }

  function saveSeriesProgress(nav, novelId) {
    try {
      const map = readSeriesProgressMap();
      map[String(nav.series_id)] = {
        novel_id: Number(novelId),
        chapter_number: Number(nav.chapter_number) || 0,
        at: Date.now()
      };
      const keys = Object.keys(map);
      if (keys.length > 200) {
        keys.sort(function (a, b) { return (map[a].at || 0) - (map[b].at || 0); });
        keys.slice(0, keys.length - 200).forEach(function (key) { delete map[key]; });
      }
      GM_setValue(SERIES_PROGRESS_KEY, JSON.stringify(map));
    } catch (_error) { /* 存不下就不记进度，阅读不受影响 */ }
  }
  // 字数照 Pixiv 的算法按码点计（代理对算一个字），封面右上角显示「52,813字」
  function pixivTextLength(text) {
    const value = String(text || '');
    const surrogates = value.match(/[\uD800-\uDBFF][\uDC00-\uDFFF]/g);
    return value.length - (surrogates ? surrogates.length : 0);
  }

  // 日期照手机站的中文写法：「2026年10月20日下午3点00分」（按浏览器本地时区）。
  // 时段分界取自手机站 JS 包里 moment.js zh-cn 的 meridiem（2026-10-23 实测）：
  // 时×100+分 <600 凌晨、<900 早上、<1130 上午、<1230 中午、<1800 下午，其余晚上
  function formatPixivDate(value) {
    const date = new Date(String(value || ''));
    if (Number.isNaN(date.getTime())) return '';
    const hour = date.getHours();
    const hm = hour * 100 + date.getMinutes();
    const period = hm < 600 ? '凌晨' : hm < 900 ? '早上' : hm < 1130 ? '上午'
      : hm < 1230 ? '中午' : hm < 1800 ? '下午' : '晚上';
    const minute = String(date.getMinutes()).padStart(2, '0');
    return date.getFullYear() + '年' + (date.getMonth() + 1) + '月' + date.getDate() + '日'
      + period + (hour % 12 || 12) + '点' + minute + '分';
  }

  // 只到日的写法（系列「2026年9月19日更新」）：正文页的 formatPixivDate 带时段，不能混用
  function formatPixivDay(value) {
    const date = new Date(String(value || ''));
    if (Number.isNaN(date.getTime())) return '';
    return date.getFullYear() + '年' + (date.getMonth() + 1) + '月' + date.getDate() + '日';
  }

  // 简介是 Pixiv 下发的 HTML：在不渲染的离线文档里取纯文本（不执行脚本、
  // 不加载图片），<br> 换成换行。绝不把它当 HTML 插进页面。
  function captionText(html) {
    const source = String(html || '').replace(/<br\s*\/?>/gi, '\n');
    try {
      const doc = new DOMParser().parseFromString('<body>' + source + '</body>', 'text/html');
      doc.querySelectorAll('script,style').forEach(function (node) { node.remove(); });
      return String(doc.body.textContent || '').trim();
    } catch (_error) {
      return source.replace(/<[^>]*>/g, '').trim();
    }
  }

  function workTags(data) {
    return (Array.isArray(data.tags) ? data.tags : []).map(function (tag) {
      if (typeof tag === 'string') return { name: tag, translated: '' };
      if (tag && typeof tag === 'object') {
        return { name: String(tag.name || ''), translated: String(tag.translated_name || '') };
      }
      return { name: '', translated: '' };
    }).filter(function (tag) { return tag.name; });
  }

  // 封面：本地缓存（dataURL，24h）优先，未命中再经 blob 取回并回写缓存。
  // 列表页救援卡与阅读层封面共用这一份逻辑。
  function loadRescueCover(img, coverId, coverType, onFail) {
    const cached = readCoverCache(coverId, coverType);
    if (cached) {
      img.src = cached;
      return;
    }
    const coverPath = coverType === 'series'
      ? '/api/rescue/v1/series/' + encodeURIComponent(String(coverId)) + '/cover'
      : '/api/rescue/v1/novels/' + encodeURIComponent(String(coverId)) + '/cover';
    apiGetBlobUrl(coverPath)
      .then(function (url) {
        // object URL 有两个读者：<img> 显示、下面回写缓存的 fetch，两边都用完再回收。
        // 0.12.0 及以前只建不回收，每张没缓存的封面都在页面里留一份
        let readers = 2;
        const release = function () {
          readers -= 1;
          if (readers === 0) URL.revokeObjectURL(url);
        };
        const settled = function () {
          img.removeEventListener('load', settled);
          img.removeEventListener('error', settled);
          release();
        };
        img.addEventListener('load', settled);
        img.addEventListener('error', settled);
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
          .catch(function () { /* 缓存写失败不影响显示 */ })
          .finally(release);
      })
      .catch(function (error) {
        // 被限流（429）不等于没有封面：先空着，卡片下次进入视口时再取
        const retry = function () { loadRescueCover(img, coverId, coverType, onFail); };
        if (error && error.message === 'rate_limited' && retryCoverOnNextView(img, retry)) return;
        if (onFail) onFail();
      });
  }

  // 封面 429 后的重试：等它离开视口、再次进入时取一次。不在原地马上重试——同一个限流窗口里
  // 只会接着 429。0.12.0 及以前 429 也走 onFail，一路滑到底超过限额的封面永久换成占位图。
  // 没有 IntersectionObserver 时返回 false，调用方照旧走 onFail。
  let coverRetryObserver = null;
  function retryCoverOnNextView(img, retry) {
    if (typeof IntersectionObserver !== 'function') return false;
    if (!coverRetryObserver) {
      coverRetryObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          const target = entry.target;
          // 开始观察时的第一次回调也会进来：已在视口里的要先离开一次，才算「下次进入」
          if (!entry.isIntersecting) {
            target.__pixivRescueRetryArmed = true;
            return;
          }
          if (!target.__pixivRescueRetryArmed) return;
          coverRetryObserver.unobserve(target);
          const again = target.__pixivRescueRetry;
          target.__pixivRescueRetry = null;
          target.__pixivRescueRetryArmed = false;
          if (typeof again === 'function') again();
        });
      });
    }
    img.__pixivRescueRetry = retry;
    img.__pixivRescueRetryArmed = false;
    coverRetryObserver.observe(img);
    return true;
  }

  // 切页时调用：旧页面的封面节点已经没用了，不让观察器一直拿着它们（连带整棵旧阅读层）
  function resetCoverRetries() {
    if (coverRetryObserver) coverRetryObserver.disconnect();
  }

  function renderWorkHeader(container, data, options) {
    const lengthText = pixivTextLength(data.text_raw).toLocaleString('en-US') + '字';
    if (data.author_name) {
      const author = element('div', 'pixiv-rescue-author');
      author.append(element('span', 'pixiv-rescue-author-avatar'),
        element('span', 'pixiv-rescue-author-name', data.author_name));
      container.append(author);
    }
    const date = element('div', 'pixiv-rescue-date', formatPixivDate(data.create_date));
    if (options && options.cover && data.novel_id) {
      const band = element('div', 'pixiv-rescue-cover-band');
      const img = element('img', 'pixiv-rescue-cover-img');
      img.alt = '';
      band.append(img, element('span', 'pixiv-rescue-text-length', lengthText));
      container.append(band);
      // 没有本地封面就整条去掉，字数改挂到日期行（不留空白灰条）
      loadRescueCover(img, data.novel_id, 'novel', function () {
        band.remove();
        date.textContent = [date.textContent, lengthText].filter(Boolean).join(' · ');
      });
    } else {
      date.textContent = [date.textContent, lengthText].filter(Boolean).join(' · ');
    }
    // 系列章节：标题上方一行灰色系列名（Pixiv 手机站同位置），带「#N」
    if (data.series_nav) {
      const series = element('a', 'pixiv-rescue-series-link',
        (data.series_nav.series_title || '未命名系列') + ' #' + data.series_nav.chapter_number);
      series.href = rescueSeriesUrl(data.series_nav.series_id);
      container.append(series);
    }
    container.append(element('h1', 'pixiv-rescue-title', data.title || '未命名备份小说'));
    const caption = captionText(data.caption);
    if (caption) {
      const box = element('div', 'pixiv-rescue-caption', caption);
      container.append(box);
      // 手机站默认只露两行简介；长简介给「展开」
      if (caption.length > 72 || caption.split('\n').length > 2) {
        box.classList.add('pixiv-rescue-caption--collapsed');
        const more = element('button', 'pixiv-rescue-caption-more', '展开');
        more.type = 'button';
        more.addEventListener('click', function () {
          const collapsed = box.classList.toggle('pixiv-rescue-caption--collapsed');
          more.textContent = collapsed ? '展开' : '收起';
        });
        container.append(more);
      }
    }
    const line = renderTagLine('pixiv-rescue-tags', data, true);
    if (line.children.length) container.append(line);
    renderStatsRow(container, data);
    container.append(date);
    renderNotice(container, data);
  }

  // 标签行照 Pixiv：R-18（红、粗）→ AI生成/原创（粗）→ 其余「#标签」。
  // 正文页的标签带译名（「#中文 Chinese」），系列页卡片不带（与 Pixiv 一致）。
  function renderTagLine(className, data, withTranslation) {
    const line = element('div', className);
    const tags = workTags(data);
    const push = function (node) { line.append(node, document.createTextNode(' ')); };
    tags.filter(function (tag) { return /^R-18G?$/.test(tag.name); }).forEach(function (tag) {
      push(element('span', 'pixiv-rescue-tag-r18', tag.name));
    });
    if (Number(data.ai_type) === 2) push(element('span', 'pixiv-rescue-tag-flag', 'AI生成'));
    if (data.is_original) push(element('span', 'pixiv-rescue-tag-flag', '原创'));
    tags.forEach(function (tag) {
      if (/^R-18G?$/.test(tag.name)) return;
      const node = element('span', 'pixiv-rescue-tag', '#' + tag.name);
      if (withTranslation && tag.translated) {
        node.append(element('span', 'pixiv-rescue-tag-translation', tag.translated));
      }
      push(node);
    });
    return line;
  }
  // 正文按 Pixiv 手机站的规则还原（2026-10-23 逐项对照真实页面 DOM）：
  //   空行分段（每段一个 p，段距 1.5em）；段内单个换行保留（pre-wrap）；
  //   [newpage] 分页，一次只显示一页，上下各一条「k / N 页」；
  //   [chapter:标题] → 章节标题，吞掉紧邻的一个换行；
  //   [[rb:正文 > 注音]] → <ruby>；[b:…]/[i:…] → 粗体/斜体（可嵌套）；
  //   [[jumpuri:文字 > URL]] → 链接（只放行 http/https）；[jump:N] →「Nページへ」；
  //   [[emphasismark:文字 > •]] → 着重号；[uploadedimage:ID]/[pixivimage:ID] →
  //   插图占位（备份未收录插图与正文的对应关系）。
  // 全部用 DOM 节点 + textContent 构建，正文里的 <script> 只会是字面文字。
  function renderPixivText(container, rawText) {
    const pages = splitPixivPages(rawText);
    const section = element('div', 'pixiv-rescue-novel-text pixiv-rescue-body');
    section.setAttribute('lang', /[぀-ヿ]/.test(String(rawText || '').slice(0, 4000)) ? 'ja' : 'zh-CN');
    applyReaderTheme(section, readReaderTheme());
    let current = 0;
    const move = function (delta) { show(current + delta, true); };
    const top = renderPixivPager(move);
    const bottom = renderPixivPager(move);
    bottom.node.classList.add('pixiv-rescue-pager--bottom');
    // 翻页后回到本页开头（顶部翻页条贴着粘性顶栏），和 Pixiv 一样一次只看一页
    function show(index, scroll) {
      current = Math.max(0, Math.min(pages.length - 1, index));
      clearNode(section);
      renderPixivPage(section, pages[current], function (page) { show(page, true); });
      top.update(current, pages.length);
      bottom.update(current, pages.length);
      if (scroll) top.node.scrollIntoView(true);
    }
    container.append(top.node, section, bottom.node);
    show(0, false);
  }

  function renderPixivPager(onMove) {
    const nav = element('nav', 'pixiv-rescue-pager');
    const prev = element('button', 'pixiv-rescue-pager-button', '上一页');
    const label = element('span', 'pixiv-rescue-pager-label', '1 / 1 页');
    const next = element('button', 'pixiv-rescue-pager-button', '下一页');
    prev.type = 'button';
    next.type = 'button';
    prev.addEventListener('click', function () { onMove(-1); });
    next.addEventListener('click', function () { onMove(1); });
    nav.append(prev, label, next);
    return {
      node: nav,
      update: function (index, total) {
        label.textContent = (index + 1) + ' / ' + total + ' 页';
        nav.classList.toggle('pixiv-rescue-pager--single', total <= 1);
        prev.disabled = index <= 0;
        next.disabled = index >= total - 1;
      }
    };
  }

  // 剥掉一页首尾的换行，中间的原样保留。0.12.1 及以前用 replace(/^\n+|\n+$/g, '')：页中间
  // 连续 k 个换行时，\n+$ 在其中每个位置都吞到这段换行末尾再失败，O(k²)，1 万个空行约 1.7 秒。
  // 从两头各扫一遍，线性
  function trimPageNewlines(page) {
    let start = 0;
    let end = page.length;
    while (start < end && page.charCodeAt(start) === 10) start += 1;
    while (end > start && page.charCodeAt(end - 1) === 10) end -= 1;
    return page.slice(start, end);
  }

  function splitPixivPages(rawText) {
    const pages = String(rawText || '').replace(/\r\n?/g, '\n').split('[newpage]').map(trimPageNewlines);
    // 只丢末尾的空页：中间的空页要保留，否则 [jump:N] 的页码会错位
    while (pages.length > 1 && !pages[pages.length - 1].trim()) pages.pop();
    return pages;
  }

  // 块级标记（章节标题、插图）把一页切成若干文本块，各文本块再按空行分段
  const PIXIV_BLOCK_RE = /\n?\[chapter:([^\]\n]*)\]\n?|\n?\[(uploadedimage|pixivimage):([^\]\n]*)\]\n?/g;

  function renderPixivPage(section, pageText, goToPage) {
    // Pixiv 正文容器开头有一个 1px 的阅读进度埋点元素：逐段对照实测，少了它
    // 全文会整体上移 1px，这里照样占位
    const start = element('div', 'pixiv-rescue-text-start');
    start.setAttribute('aria-hidden', 'true');
    section.append(start);
    const text = String(pageText || '');
    if (!text.trim()) {
      section.append(element('p', 'pixiv-rescue-para', '备份正文为空'));
      return;
    }
    let last = 0;
    let match;
    PIXIV_BLOCK_RE.lastIndex = 0;
    while ((match = PIXIV_BLOCK_RE.exec(text)) !== null) {
      appendPixivParagraphs(section, text.slice(last, match.index), goToPage);
      if (match[1] !== undefined) {
        section.append(element('h2', 'pixiv-rescue-chapter', match[1]));
      } else {
        section.append(renderMissingImage(match[2], match[3]));
      }
      last = PIXIV_BLOCK_RE.lastIndex;
    }
    appendPixivParagraphs(section, text.slice(last), goToPage);
  }

  function appendPixivParagraphs(section, chunk, goToPage) {
    chunk.split('\n\n').forEach(function (paragraph) {
      if (!paragraph) return;
      const p = element('p', 'pixiv-rescue-para');
      renderPixivInline(p, paragraph, goToPage, 0);
      section.append(p);
    });
  }
  // 从 start 处的「[」起按括号深度找配对的「]」：标记可以嵌套
  // （生产实测 [b:[i:[[jumpuri:… > …]]]]），配不上就当普通文字。
  function findPixivClose(text, start) {
    let depth = 0;
    for (let i = start; i < text.length; i += 1) {
      const ch = text.charAt(i);
      if (ch === '[') depth += 1;
      else if (ch === ']') {
        depth -= 1;
        if (depth === 0) return i;
      }
    }
    return -1;
  }

  function matchPixivToken(text, start) {
    const head = text.slice(start, start + 16);
    let m = /^\[jump:(\d+)\]/.exec(head);
    if (m) return { kind: 'jump', page: Number(m[1]), end: start + m[0].length };
    m = /^\[\[(rb|jumpuri|emphasismark):/.exec(head);
    if (m) {
      const close = findPixivClose(text, start);
      if (close < 0 || text.charAt(close - 1) !== ']') return null;
      return { kind: m[1], body: text.slice(start + m[0].length, close - 1), end: close + 1 };
    }
    m = /^\[(b|i):/.exec(head);
    if (m) {
      const close = findPixivClose(text, start);
      if (close < 0) return null;
      return { kind: m[1], body: text.slice(start + m[0].length, close), end: close + 1 };
    }
    return null;
  }

  function renderPixivInline(parent, text, goToPage, depth) {
    let plain = '';
    let i = 0;
    const flush = function () {
      if (plain) parent.append(document.createTextNode(plain));
      plain = '';
    };
    while (i < text.length) {
      const open = text.indexOf('[', i);
      if (open < 0) {
        plain += text.slice(i);
        break;
      }
      plain += text.slice(i, open);
      const token = depth < 8 ? matchPixivToken(text, open) : null;
      if (!token) {
        plain += '[';
        i = open + 1;
        continue;
      }
      flush();
      renderPixivToken(parent, token, goToPage, depth);
      i = token.end;
    }
    flush();
  }

  function splitPixivPair(body, useLast) {
    const at = useLast ? body.lastIndexOf('>') : body.indexOf('>');
    if (at < 0) return [body, ''];
    return [body.slice(0, at).replace(/[ \t]+$/, ''), body.slice(at + 1).trim()];
  }

  function renderPixivToken(parent, token, goToPage, depth) {
    if (token.kind === 'b' || token.kind === 'i') {
      const node = element(token.kind);
      renderPixivInline(node, token.body, goToPage, depth + 1);
      parent.append(node);
      return;
    }
    if (token.kind === 'jump') {
      // 页内跳转：Pixiv 显示「Nページへ」；不用 href，免得改 URL 触发路由轮询销毁阅读层
      const link = element('a', 'pixiv-rescue-jump', token.page + 'ページへ');
      link.setAttribute('role', 'link');
      link.tabIndex = 0;
      const go = function (event) {
        event.preventDefault();
        goToPage(token.page - 1);
      };
      link.addEventListener('click', go);
      link.addEventListener('keydown', function (event) { if (event.key === 'Enter') go(event); });
      parent.append(link);
      return;
    }
    const pair = splitPixivPair(token.body, token.kind === 'jumpuri');
    if (token.kind === 'rb') {
      const ruby = element('ruby');
      ruby.append(document.createTextNode(pair[0]), element('rp', '', '('), element('rt', '', pair[1]), element('rp', '', ')'));
      parent.append(ruby);
      return;
    }
    if (token.kind === 'jumpuri') {
      const link = element('a', 'pixiv-rescue-jump');
      if (/^https?:\/\//i.test(pair[1])) {
        link.href = pair[1];
        link.target = '_blank';
        link.rel = 'noopener noreferrer nofollow';
      }
      renderPixivInline(link, pair[0], goToPage, depth + 1);
      parent.append(link);
      return;
    }
    // emphasismark：着重号，Pixiv 用 text-emphasis（filled dot，下方）
    const mark = element('span', 'pixiv-rescue-emphasis');
    const style = /[﹅、]/.test(pair[1]) ? 'filled sesame' : 'filled dot';
    mark.style.setProperty('text-emphasis-style', style);
    mark.style.setProperty('-webkit-text-emphasis-style', style);
    mark.style.setProperty('text-emphasis-position', 'under right');
    mark.style.setProperty('-webkit-text-emphasis-position', 'under right');
    renderPixivInline(mark, pair[0], goToPage, depth + 1);
    parent.append(mark);
  }

  // 插图：备份没有存「插图 ID → 图片文件」的对应关系，如实给占位，不猜图
  function renderMissingImage(kind, id) {
    const box = element('div', 'pixiv-rescue-image-missing', '此处原有插图（私人备份未收录插图）');
    if (kind === 'pixivimage' && /^\d+$/.test(String(id).split('-')[0])) {
      box.append(document.createTextNode(' '));
      const link = element('a', '', '到 Pixiv 查看作品');
      link.href = 'https://www.pixiv.net/artworks/' + String(id).split('-')[0];
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      box.append(link);
    }
    return box;
  }
  // ── 阅读设置（Aa）────────────────────────────────────────────
  // 用户在 Pixiv 正文页「Aa」里选的背景/字号/行距/字体存在 Pixiv 自己那边，
  // 已删作品页上读不到。做法：在健康正文页上只读地看一眼 #novel-text-container
  // 的 class（background-N size-N spacing-N）和字体，记进脚本存储；阅读层照用。
  // 没记过就用手机站默认值（白底、标准字号、行距 1.8、gothic）。
  function readReaderTheme() {
    const fallback = { background: 0, size: 1, spacing: 1, font: '' };
    try {
      const saved = JSON.parse(GM_getValue(READER_THEME_KEY, '') || 'null');
      if (!saved || typeof saved !== 'object') return fallback;
      const pick = function (value, max, def) {
        const n = Number(value);
        return Number.isInteger(n) && n >= 0 && n <= max ? n : def;
      };
      const font = typeof saved.font === 'string' && saved.font.length <= 600 && !/[;{}<>]/.test(saved.font)
        ? saved.font : '';
      return {
        background: pick(saved.background, 4, 0),
        size: pick(saved.size, 2, 1),
        spacing: pick(saved.spacing, 1, 1),
        font: font
      };
    } catch (_error) {
      return fallback;
    }
  }

  function applyReaderTheme(section, theme) {
    if (theme.background) section.classList.add('background-' + theme.background);
    if (theme.size !== 1) section.classList.add('size-' + theme.size);
    if (theme.spacing !== 1) section.classList.add('spacing-' + theme.spacing);
    if (theme.font) section.style.fontFamily = theme.font;
    section.setAttribute('data-pixiv-rescue-theme',
      [theme.background, theme.size, theme.spacing].join('-'));
  }

  // 只读：不改 Pixiv 页面任何东西，只把它当前的 Aa 设置记下来（最多等 15 秒渲染）
  function learnReaderTheme(isStale) {
    const deadline = Date.now() + 15000;
    const timer = window.setInterval(function () {
      if (isStale() || Date.now() > deadline) {
        window.clearInterval(timer);
        return;
      }
      const box = document.getElementById('novel-text-container');
      if (!box) return;
      window.clearInterval(timer);
      const cls = String(box.className || '');
      const grab = function (name) {
        const m = cls.match(new RegExp('(?:^|\\s)' + name + '-(\\d)(?:\\s|$)'));
        return m ? Number(m[1]) : null;
      };
      const background = grab('background');
      if (background === null) return;
      try {
        GM_setValue(READER_THEME_KEY, JSON.stringify({
          background: background,
          size: grab('size') === null ? 1 : grab('size'),
          spacing: grab('spacing') === null ? 1 : grab('spacing'),
          font: String(box.style.fontFamily || '').slice(0, 600)
        }));
      } catch (_error) { /* 存储失败就继续用默认值 */ }
    }, 500);
  }

  // 阅读层内的整页错误。retry 存在时给「重试」——超时/网络抖动不该是死路，
  // 旧版只能整页刷新。
  function renderError(error, retry) {
    const body = openReader();
    body.append(element('div', 'pixiv-rescue-error', rescueErrorText(error)));
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
  // retry：第一次就没读到目录时给，提示下面一颗「重试」，点了把按钮交给 retry（禁用、重画都由它管）。
  // 「加载更多」失败不给：那颗按钮还在，再点一次就是重试
  function renderDirectoryError(panel, error, retry) {
    let note = panel.querySelector('.pixiv-rescue-directory-error');
    if (!note) {
      note = element('div', 'pixiv-rescue-error pixiv-rescue-directory-error', '');
      panel.append(note);
    }
    clearNode(note);
    note.append(element('div', '', '目录没有读到：' + rescueErrorText(error)));
    if (typeof retry === 'function') {
      const again = element('button', 'pixiv-rescue-button', '重试');
      again.type = 'button';
      again.addEventListener('click', function () { retry(again); });
      note.append(again);
    }
  }

  // 系列目录第一次就没读到：只重取目录，读到后整页重画（系列头里的话数、更新日、「从最初开始阅读」
  // 都要用目录）。0.12.1 及以前这里只有一行错误提示，只能整页刷新。
  // 阅读层已经不是画这一页的那个了（切页、重画）就放弃
  function retrySeriesChapters(panel, data, seriesId, button) {
    button.disabled = true;
    const stale = function () { return !readerRoot || !readerRoot.contains(panel); };
    apiGet('/api/rescue/v1/series/' + encodeURIComponent(String(seriesId)) + '/chapters')
      .then(function (chapters) {
        if (stale()) return;
        renderSeries({ series: data.series, chapters: chapters, chaptersError: null }, seriesId);
      })
      .catch(function (error) {
        if (stale()) return;
        renderDirectoryError(panel, error, function (again) { retrySeriesChapters(panel, data, seriesId, again); });
      });
  }

  // 阅读层整页错误与目录错误共用。0.12.1 及以前还有一套「章节级」文案（pageLevel=false），
  // 调用方全都传 true，那一套从没用上过，已删
  function rescueErrorText(error) {
    const messages = {
      missing_token: '未设置救援 Token，请通过油猴菜单完成设置。',
      unauthorized: '救援 Token 无效，请在网站设置页轮换后重新填写。',
      not_found: '未找到可用的救援数据，Pixiv 原页面已保留。',
      rate_limited: '请求太频繁，请稍后再试。',
      server_error: '救援服务暂时出错，请稍后再试。',
      network_timeout: '救援服务响应超时，Pixiv 原页面已保留。',
      network_error: '无法连接救援服务，Pixiv 原页面已保留。',
      // 整本正文 JSON 约 216 KB：部分手机管理器可能截断大应答，解析失败落到这里
      invalid_response: '备份数据没有完整读到，请重试。'
    };
    return messages[error && error.message] || '未找到可用的救援数据，Pixiv 原页面已保留。';
  }

  // ── 收藏页 / 追更页列表匹配 ──────────────────────────────────

  // Pixiv 非日语界面会给路径加语言前缀（/en/、/zh/、/zh-tw/ 等，全小写）。统一在这里剥掉，
  // 各页判定只写无前缀形态，避免每处正则都要枚举语言。
  // 认「两个字母」或「两个字母-两个字母」：0.12.1 及以前只认两个字母的固定列表，
  // /zh-tw/ 页（繁体中文界面）剥不掉前缀，正文、系列、列表页全都判成非目标页，注入后静默。
  // Pixiv 自己的一级路径没有两个字母的（novel、users、artworks…），不会误剥。
  function pathWithoutLangPrefix() {
    const path = window.location.pathname;
    // 命中语言前缀时保留开头的「/」：剥的是 /zh-tw/ 段而不是整个前缀串
    const m = path.match(/^\/([a-z]{2}(?:-[a-z]{2})?)(\/|$)/);
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

  // text 是已 trim 的 textContent
  function textMatchesUnavailable(text) {
    if (!text || text.length > BROKEN_CARD_TEXT_LIMIT) return false;
    return UNAVAILABLE_MARKERS.some(function (marker) {
      return text.includes(marker);
    });
  }

  function elementMatchesUnavailable(node) {
    if (!node) return false;
    return textMatchesUnavailable(String(node.textContent || '').trim());
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
    // 由内向外查：祖先的文本必然包含子孙的文本，某一层超过阈值后外层只会更长，文案判定
    // 必然不成立，只剩类名要查。0.12.0 及以前每层都取 textContent，外层容器（可能已是
    // 整个列表）的文本每个链接都要拼一遍
    let tooLong = false;
    for (const target of targets) {
      if (!tooLong) {
        const text = String(target.textContent || '').trim();
        if (text.length > BROKEN_CARD_TEXT_LIMIT) tooLong = true;
        else if (textMatchesUnavailable(text)) return true;
      }
      // Pixiv 官方的「不可见作品」容器类名：比文案匹配更稳定的结构信号
      if (typeof target.className === 'string' && target.className.includes('invisible-work-container')) return true;
    }
    return false;
  }

  // 链接指向的作品：{type, id}；不是正文/系列链接返回 null。登记失效卡与回调复核共用这一个判定
  function linkWork(link) {
    const href = link.getAttribute('href') || '';
    const seriesMatch = href.match(/\/novel\/series\/(\d+)/);
    if (seriesMatch) return { type: 'series', id: seriesMatch[1] };
    const novelMatch = href.match(/[?&]id=(\d+)/);
    return novelMatch ? { type: 'novel', id: novelMatch[1] } : null;
  }

  // lookup 应答回来时，卡片还是不是登记时那部作品：仍在页面上、仍是 card-pending、登记时取 ID 的
  // 那个节点（链接 / data-gtm-value）还在卡片里且 ID 没变。Pixiv 列表是 Vue 渲染的，翻页时可能
  // 复用同一个节点改画另一部作品；0.12.1 及以前不核对，迟到的应答会把上一部的备份画到这张卡上
  function entryStillMatches(entry) {
    const card = entry.card;
    if (!card.isConnected || card.getAttribute(ROOT_ATTRIBUTE) !== 'card-pending') return false;
    const source = entry.source;
    if (!source || !source.isConnected || !card.contains(source)) return false;
    if (entry.type === 'unknown') return String(source.getAttribute('data-gtm-value') || '').trim() === entry.id;
    const work = linkWork(source);
    return Boolean(work) && work.type === entry.type && work.id === entry.id;
  }

  function collectBrokenCards() {
    const novelIds = new Set();
    const seriesIds = new Set();
    const broken = [];

    // source：从哪个节点读到的 ID（失效卡的链接，或无链接白块上带 data-gtm-value 的节点），回调复核用
    function register(card, id, type, source) {
      // 标记为 pending 而非终态：查询失败时会撤销（见 markBrokenCards 的 catch），
      // 下一轮扫描允许重试。A14-03 的根因就是这里先写了永久标记。
      card.setAttribute(ROOT_ATTRIBUTE, 'card-pending');
      broken.push({ type: type, id: id, card: card, source: source });
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
      // 脚本自己画的卡片/面板（见 OWN_UI_SELECTOR）不是 Pixiv 的失效卡；
      // .novel-series-thumbnail-detail 是 Pixiv 只给能显示的系列画的追更卡。追更不多时，
      // 健康卡的链接上查四层会连带旁边空白框的「该作品无法显示」且总字数不到 64，
      // 不排除就会把健康卡当成失效卡整块重画（位置对齐也跟着坏）
      if (link.closest(OWN_UI_SELECTOR) || link.closest('.novel-series-thumbnail-detail')) return;
      const card = link.closest('div.works-item, li') || link;
      if (handledCards.has(card)) return;
      if (alreadyHandled(card)) { handledCards.add(card); return; }
      if (!cardIsBroken(link)) return;
      const work = linkWork(link);
      if (!work) return;
      handledCards.add(card);
      register(card, work.id, work.type, link);
    });

    // 2) 无链接的白块：系列被删/隐藏时 Pixiv 不再渲染链接，但卡片上通常还留着
    //    data-gtm-value（作品 ID）之类的数据属性（列表项本身还没从收藏数据里消失）。
    document.querySelectorAll('[data-gtm-value]').forEach(function (node) {
      const value = String(node.getAttribute('data-gtm-value') || '').trim();
      if (!/^\d{3,12}$/.test(value)) return;
      if (node.closest(OWN_UI_SELECTOR)) return;
      const card = climbToCard(node);
      if (!card || card === document.body) return;
      if (alreadyHandled(card)) return;
      if (card.querySelector('a[href*="/novel/show.php?id="], a[href*="/novel/series/"]')) return;
      // 卡片本身（或其附近文本）必须就是失效提示，避免把健康卡片的 gtm 值也抓进来
      if (!cardIsBroken(card) && !elementMatchesUnavailable(card)) return;
      register(card, value, 'unknown', node);
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
      const img = element('img', 'pixiv-rescue-rescued-img');
      img.alt = '';
      img.loading = 'lazy';
      coverBox.append(img);
      // 每次进收藏页重复拉封面正是用户感知到的「等一会」之一：先读缓存
      loadRescueCover(img, coverId, isNovel ? 'novel' : 'series', function () { img.remove(); });
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
    const cachedEntries = readLookupCache();
    const pendingEntries = [];
    found.broken.forEach(function (entry) {
      const cached = readCachedLookup(cachedEntries, entry.id, entry.type === 'series' ? 'series' : 'novel');
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
          // 卡片已经不是登记时那部作品：不写，撤掉 pending 让下一轮扫描按它现在的样子重新判断
          // （查到的结果已进缓存，同一部作品再出现时直接命中）
          if (!entryStillMatches(entry)) {
            if (entry.card.getAttribute(ROOT_ATTRIBUTE) === 'card-pending') entry.card.removeAttribute(ROOT_ATTRIBUTE);
            return;
          }
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
    scanListPage();
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
          scanListPage();
          // 这里不延长下面的快速轮询：每批插入都会走到这一轮扫描，漏不掉。0.12.1 及以前在这里把
          // fastScanUntil 再推后 15 秒：页面一直有变动（广告、懒加载）时 500ms 全页扫描永不停止；
          // 安静满 15 秒、快速轮询结束后，这一行又什么都不做
        }, 150);
      });
      observer.observe(document.body || document.documentElement, { childList: true, subtree: true });
      window.__pixivRescueObserver = observer;
    }
    // 快速轮询窗口：进入列表页（含 SPA 切页、翻页）后 15 秒内每 500ms 扫一次（SPA 分批渲染），
    // 之后交给 3 秒 interval。定时器用链式 setTimeout，避免两个 interval 叠加。
    fastScanUntil = Date.now() + 15000;
    if (!window.__pixivRescueFastScan) {
      const fastTick = function () {
        if (!isBookmarksListPage() && !isWatchlistPage()) {
          window.__pixivRescueFastScan = null;
          return;
        }
        scanListPage();
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
        scanListPage();
      }, 3000);
    }
  }

  // ── 新作页「拯救系列」tab（0.12.0）──────────────────────────
  // Pixiv 手机站「新作」页顶部三个 tab：已关注用户的新作（bookmark_new.php）/ 追更列表新作
  // （/following/watchlist/novels）/ 好P友新作（mypixiv_new.php）。第三个改名「拯救系列」，
  // 它的页面内容换成备份里的系列，版式与翻页照另外两个 tab。只认手机站的 .v-nav-tabs，
  // PC 站没有这套 DOM，什么都不动。
  const RESCUE_FEED_PATH = '/novel/mypixiv_new.php';
  const RESCUE_FEED_LABEL = '拯救系列';
  const RESCUE_FEED_PAGE_SIZE = 10;
  // 脚本自己画的节点：扫描失效卡片时整块跳过。追更填空卡里同样有系列/正文链接，
  // 旁边还有被隐藏的「该作品无法显示」，不跳过就会被当成失效卡再「救」一次
  const OWN_UI_SELECTOR = ['feed', 'watch-card', 'sheet', 'content'].map(function (value) {
    return '[' + ROOT_ATTRIBUTE + '="' + value + '"]';
  }).join(',');

  function isFeedPage() {
    const path = pathWithoutLangPrefix();
    return path === '/novel/bookmark_new.php' || path === RESCUE_FEED_PATH || isWatchlistPage();
  }

  function isRescueFeedPage() {
    return pathWithoutLangPrefix() === RESCUE_FEED_PATH;
  }

  // tab 链接可能带语言前缀（/en/novel/mypixiv_new.php），按路径结尾认
  function rescueFeedTabLinks() {
    return Array.from(document.querySelectorAll('.v-nav-tabs a[href*="mypixiv_new.php"]')).filter(function (link) {
      return /\/novel\/mypixiv_new\.php$/.test(String(link.pathname || ''));
    });
  }

  // 改名只改文本节点（激活态在 <h2> 里，非激活态直接在 <a> 里），不替换元素：
  // Vue 2 打补丁比较的是 vnode，文本没变就不会写回 DOM，改过的字留得住。
  // 切路由时 heading 与 tab 栏整个重建，由观察器在新节点插入时再改一次。
  function relabelRescueTab() {
    rescueFeedTabLinks().forEach(function (link) {
      const host = link.querySelector('h2') || link;
      for (let node = host.firstChild; node; node = node.nextSibling) {
        if (node.nodeType !== 3 || !node.nodeValue.trim()) continue;
        const text = node.nodeValue.trim();
        if (text !== RESCUE_FEED_LABEL) node.nodeValue = node.nodeValue.replace(text, RESCUE_FEED_LABEL);
        return;
      }
    });
  }

  // 拯救系列 tab 的状态：{native, container, page}。native 是 Pixiv 原生内容块（隐藏不删），
  // container 是紧跟其后的我们的列表。离开 mypixiv 页时整体复原。
  let rescueFeed = null;
  let rescueFeedGeneration = 0;
  let rescueFeedScrollPending = false;

  function rescueTabIsActive(link) {
    return link.classList.contains('active') || link.classList.contains('router-link-active')
      || link.getAttribute('aria-current') === 'page';
  }

  // .nav-tab（插画・漫画 / 小说 子 tab，保留不动）后面第一个元素就是原生内容块：
  // 里面有 .pager-view-nav / .works-grid-list / .notifications / nav.pager。
  // 认不出这个结构就什么都不做——SPA 可能还没渲染完，下一批变动再来。
  function findRescueFeedNative() {
    const tabs = document.querySelectorAll('.nav-tab');
    for (let i = 0; i < tabs.length; i += 1) {
      let node = tabs[i].nextElementSibling;
      while (node && node.getAttribute(ROOT_ATTRIBUTE) === 'feed') node = node.nextElementSibling;
      if (node && node.tagName === 'DIV'
          && node.querySelector('.works-grid-list, .notifications, nav.pager, .pager-view-nav')) {
        return node;
      }
    }
    return null;
  }

  function resetRescueFeed() {
    rescueFeedGeneration += 1;
    rescueFeedScrollPending = false;
    if (rescueFeed) {
      rescueFeed.native.removeAttribute('data-pixiv-rescue-hidden');
      rescueFeed.container.remove();
      rescueFeed = null;
    }
    // 兜底：状态丢了也不留下被隐藏的原生块或孤立的列表
    document.querySelectorAll('[data-pixiv-rescue-hidden]').forEach(function (node) {
      node.removeAttribute('data-pixiv-rescue-hidden');
    });
    document.querySelectorAll('[' + ROOT_ATTRIBUTE + '="feed"]').forEach(function (node) { node.remove(); });
  }

  // ?p=N（拯救系列 tab 与追更列表共用）；缺省、非法都按第 1 页
  function pageParamFromUrl() {
    const value = Number(new URLSearchParams(window.location.search).get('p') || 1);
    return Number.isInteger(value) && value >= 1 ? value : 1;
  }

  // 幂等：路由钩子、观察器、1 秒轮询都会调用，只在页码变了时才发请求
  function ensureRescueFeed() {
    if (!isRescueFeedPage()) return;
    // pushState 之后的一瞬间旧页面的 DOM 可能还在：激活 tab 必须已经是 mypixiv
    if (!rescueFeedTabLinks().some(rescueTabIsActive)) return;
    const native = findRescueFeedNative();
    if (!native) return;
    addStyles();
    if (!rescueFeed || !rescueFeed.container.isConnected) {
      if (rescueFeed) rescueFeed.native.removeAttribute('data-pixiv-rescue-hidden');
      const container = element('div', 'pixiv-rescue-feed');
      container.setAttribute(ROOT_ATTRIBUTE, 'feed');
      rescueFeed = { native: native, container: container, page: 0 };
    } else if (rescueFeed.native !== native) {
      // Vue 重渲染换了原生块：已加载的列表留着，只改隐藏对象
      rescueFeed.native.removeAttribute('data-pixiv-rescue-hidden');
      rescueFeed.native = native;
    }
    if (!native.hasAttribute('data-pixiv-rescue-hidden')) native.setAttribute('data-pixiv-rescue-hidden', '');
    if (native.nextElementSibling !== rescueFeed.container) native.after(rescueFeed.container);
    const page = pageParamFromUrl();
    if (rescueFeed.page !== page) loadRescueFeed(page);
  }

  function loadRescueFeed(page) {
    const state = rescueFeed;
    state.page = page;
    rescueFeedGeneration += 1;
    const generation = rescueFeedGeneration;
    showRescueFeedLoading(state.container);
    apiGet('/api/rescue/v1/series?page=' + encodeURIComponent(String(page))
      + '&page_size=' + RESCUE_FEED_PAGE_SIZE)
      .then(function (data) {
        if (generation !== rescueFeedGeneration || rescueFeed !== state) return;
        renderRescueFeed(state.container, data, page);
        if (rescueFeedScrollPending) {
          rescueFeedScrollPending = false;
          scrollToRescueFeed(state.container);
        }
      })
      .catch(function (error) {
        if (generation !== rescueFeedGeneration || rescueFeed !== state) return;
        rescueFeedScrollPending = false;
        renderRescueFeedError(state.container, error, function () { loadRescueFeed(page); });
      });
  }

  // 翻页后回到列表开头（Pixiv 的 scrollToContentTop：滚到 #pager-scroll-position）
  function scrollToRescueFeed(container) {
    const top = container.getBoundingClientRect().top + window.scrollY;
    if (window.scrollY > top) window.scrollTo(0, top);
  }

  function renderRescueFeed(container, data, page) {
    clearNode(container);
    const items = Array.isArray(data.items) ? data.items : [];
    if (items.length) {
      const list = element('div', 'pixiv-rescue-feed-list');
      items.forEach(function (item) {
        const entry = element('div', 'pixiv-rescue-feed-item');
        entry.append(renderSeriesRow(item, null), element('hr', 'pixiv-rescue-divider'));
        list.append(entry);
      });
      container.append(list);
    } else {
      // 服务端在同一个读事务里计数、取页，页码钳到末页：total > 0 时所在页必有条目，
      // 所以空列表只有「一个都没有」这一种。0.12.1 及以前另有「这一页没有拯救系列」，到不了
      const empty = element('div', 'pixiv-rescue-feed-empty');
      empty.append(element('p', '', '抱歉，目前没有拯救系列'));
      container.append(empty);
    }
    // 翻页条按服务端实际给的页画：过期的 ?p=N 超出页数时服务端钳到末页，0.12.0 及以前
    // 按请求页画，没有一个页码是当前页、「上一页」也指错。旧后端没有 page 字段时退回请求页
    const served = Number(data.page);
    const shown = Number.isInteger(served) && served >= 1 ? served : page;
    container.append(renderRescueFeedPager(shown, Math.max(1, Number(data.total_pages || 1))));
  }

  function renderRescueFeedError(container, error, retry) {
    clearNode(container);
    const box = element('div', 'pixiv-rescue-feed-empty');
    box.append(element('p', '', rescueFeedErrorText(error)));
    const again = element('button', 'pixiv-rescue-feed-retry', '重试');
    again.type = 'button';
    again.addEventListener('click', retry);
    box.append(again);
    container.append(box);
  }

  function rescueFeedErrorText(error) {
    const messages = {
      missing_token: '未设置救援 Token，请通过油猴菜单完成设置',
      unauthorized: '救援 Token 无效，请在网站设置页轮换后重新填写',
      rate_limited: '请求太频繁，请稍后再试',
      server_error: '救援服务暂时出错，请稍后再试',
      network_timeout: '救援服务响应超时，请重试',
      network_error: '无法连接救援服务，请重试'
    };
    return messages[error && error.message] || '拯救系列没有读到，请重试';
  }

  // 加载中：旧列表留在原处（与 Pixiv 一样），只把翻页条换成蓝底转圈
  function showRescueFeedLoading(container) {
    container.querySelectorAll('.pixiv-rescue-feed-empty').forEach(function (node) { node.remove(); });
    const spinner = element('nav', 'pixiv-rescue-feed-pager');
    const bar = element('div', 'pixiv-rescue-feed-spinner');
    const box = element('div', 'pixiv-rescue-feed-spinner-box');
    box.setAttribute('role', 'progressbar');
    box.setAttribute('aria-label', '正在加载');
    for (let i = 0; i < 12; i += 1) {
      const tick = element('span');
      tick.style.transform = 'rotate(' + (i * 30) + 'deg)';
      tick.style.animationDelay = (i / 12 - 1).toFixed(3) + 's';
      box.append(tick);
    }
    bar.append(box);
    spinner.append(bar);
    const old = container.querySelector('.pixiv-rescue-feed-pager');
    if (old) old.replaceWith(spinner);
    else container.append(spinner);
  }

  // 翻页条照 Pixiv：左右箭头 + 最多 5 个页码；上一页/下一页分别指向 max(1,p-1)/min(末页,p+1)，
  // 只有一页时也照样画箭头和「1」。点击用 pushState 切页（state 形状同 vue-router 3），
  // 路由钩子随之触发 ensureRescueFeed 去取新的一页
  const FEED_ARROW_PATHS = {
    prev: 'M14.914 25.521L13.5 26.936.065 13.5 13.5.065l1.414 1.414L2.894 13.5z',
    next: 'M.086 25.521L1.5 26.936 14.936 13.5 1.5.065.086 1.479 12.106 13.5z'
  };

  function rescueFeedPageHref(page) {
    const params = new URLSearchParams(window.location.search);
    if (page > 1) params.set('p', String(page));
    else params.delete('p');
    const query = params.toString();
    return window.location.pathname + (query ? '?' + query : '');
  }

  function renderRescueFeedPager(page, lastPage) {
    const nav = element('nav', 'pixiv-rescue-feed-pager');
    nav.setAttribute('aria-label', '翻页');
    const bar = element('div', 'pixiv-rescue-feed-pager-bar');
    const current = Math.min(page, lastPage);
    const link = function (target, className, text) {
      const anchor = element('a', className, text);
      anchor.href = rescueFeedPageHref(target);
      if (target === page) anchor.setAttribute('aria-current', 'page');
      anchor.addEventListener('click', function (event) {
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        if (target === page) return;
        rescueFeedScrollPending = true;
        history.pushState({ key: performance.now().toFixed(3) }, '', anchor.href);
      });
      return anchor;
    };
    const arrow = function (target, kind, label) {
      const anchor = link(target, 'pixiv-rescue-feed-pager-arrow pixiv-rescue-feed-pager-' + kind);
      anchor.setAttribute('aria-label', label);
      const icon = element('span', 'pixiv-rescue-feed-pager-icon');
      const svg = document.createElementNS(SVG_NS, 'svg');
      svg.setAttribute('viewBox', '0 0 15 27');
      svg.setAttribute('aria-hidden', 'true');
      const path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('fill', '#FFF');
      path.setAttribute('d', FEED_ARROW_PATHS[kind]);
      svg.append(path);
      icon.append(svg);
      anchor.append(icon);
      return anchor;
    };
    bar.append(arrow(Math.max(1, Math.min(page - 1, lastPage)), 'prev', '上一页'),
      arrow(Math.min(lastPage, page + 1), 'next', '下一页'));
    const first = Math.max(1, Math.min(current - 2, lastPage - 4));
    const last = Math.min(lastPage, first + 4);
    const list = element('ul', 'pixiv-rescue-feed-pagenum');
    for (let value = first; value <= last; value += 1) {
      const item = element('li');
      item.append(link(value, '', String(value)));
      list.append(item);
    }
    const numbers = element('div');
    numbers.append(list);
    bar.append(numbers);
    nav.append(bar);
    return nav;
  }

  // 系列卡片照 Pixiv 手机站 .novel-series-thumbnail-detail（2026-10-26 实测）：封面、R-18 角标、
  // 两行标题、作者（16px 圆头像）、「N话 · 某日更新」、「浏览更多」、「阅读最新话」。
  // 被删的系列没有铃铛；追更列表里换成「…」，转给 Pixiv 自己的菜单（「从追更列表中移除」）。
  function renderSeriesRow(item, menuTarget) {
    const row = element('div', 'pixiv-rescue-series-row');
    const seriesHref = rescueSeriesUrl(item.series_id);
    const cover = element('a', 'pixiv-rescue-series-row-cover');
    cover.href = seriesHref;
    cover.setAttribute('aria-label', item.title || '未命名系列');
    // 没有本地封面：素色占位印上系列名（与系列页章节卡同一套样式）
    const plain = function () {
      clearNode(cover);
      cover.classList.add('pixiv-rescue-chapter-cover--plain');
      const label = element('span', 'pixiv-rescue-chapter-cover-title', item.title || '');
      label.setAttribute('aria-hidden', 'true');
      cover.append(label, element('span', 'pixiv-rescue-mark', '拯救'));
    };
    if (item.has_cover) {
      const img = element('img', 'pixiv-rescue-series-row-img');
      img.alt = '';
      cover.append(img, element('span', 'pixiv-rescue-mark', '拯救'));
      loadRescueCover(img, item.series_id, 'series', plain);
    } else {
      plain();
    }
    const details = element('div', 'pixiv-rescue-series-row-details');
    const restrict = Number(item.x_restrict || 0);
    if (restrict > 0) {
      const badges = element('div', 'pixiv-rescue-series-row-badges');
      badges.append(element('span', 'pixiv-rescue-series-row-badge', restrict >= 2 ? 'R-18G' : 'R-18'));
      details.append(badges);
    }
    const title = element('a', 'pixiv-rescue-series-row-title');
    title.href = seriesHref;
    title.append(element('div', 'pixiv-rescue-series-row-title-text', item.title || '未命名系列'));
    details.append(title, renderSeriesRowUser(item));
    const meta = element('div', 'pixiv-rescue-series-row-meta');
    const saved = Number(item.chapter_count || 0);
    const expected = Number(item.expected_count || 0);
    meta.append(element('span', '', expected > saved ? expected + '话（备份' + saved + '话）' : saved + '话'));
    const day = formatPixivDay(item.last_create_date);
    if (day) meta.append(element('span', '', day + '更新'));
    details.append(meta);
    const moreLine = element('div', 'pixiv-rescue-series-row-more-line');
    const more = element('span', 'pixiv-rescue-series-row-more', '浏览更多');
    more.setAttribute('role', 'button');
    more.tabIndex = 0;
    const openSheet = function (event) {
      event.preventDefault();
      openInfoSheet(document.body, { pageLevel: true, linkText: '查看目录', href: seriesHref });
    };
    more.addEventListener('click', openSheet);
    more.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') openSheet(event);
    });
    moreLine.append(more);
    details.append(moreLine);
    const actions = element('div', 'pixiv-rescue-series-row-actions');
    if (/^\d+$/.test(String(item.latest_novel_id || ''))) {
      const read = element('a', 'pixiv-rescue-series-row-read', '阅读最新话');
      read.href = rescueNovelUrl(item.latest_novel_id);
      actions.append(read);
    }
    if (menuTarget) actions.append(renderSeriesRowMenu(menuTarget));
    if (actions.children.length) details.append(actions);
    row.append(cover, details);
    return row;
  }

  function renderSeriesRowUser(item) {
    const userId = String(item.user_id || '');
    const user = element(/^[1-9]\d*$/.test(userId) ? 'a' : 'div', 'pixiv-rescue-series-row-user');
    if (user.tagName === 'A') user.href = '/users/' + userId + '/novels';
    const avatarUrl = String(item.author_avatar_url || '');
    if (/^https:\/\/i\.pximg\.net\//.test(avatarUrl)) {
      const avatar = element('img', 'pixiv-rescue-series-row-avatar');
      avatar.alt = '';
      avatar.addEventListener('error', function () {
        avatar.replaceWith(element('span', 'pixiv-rescue-series-row-avatar'));
      }, { once: true });
      avatar.src = avatarUrl;
      user.append(avatar);
    } else {
      user.append(element('span', 'pixiv-rescue-series-row-avatar'));
    }
    user.append(element('span', 'pixiv-rescue-series-row-name', item.author_name || ''));
    return user;
  }

  // 「…」：点击转给被隐藏的 Pixiv 三点图标，打开它自己的底部菜单（「从追更列表中移除」）。
  // 脚本只转发用户自己的这次点击，从不替用户点菜单项
  function renderSeriesRowMenu(menuTarget) {
    const button = element('button', 'pixiv-rescue-series-row-menu');
    button.type = 'button';
    button.setAttribute('aria-label', '作品选项');
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '24');
    svg.setAttribute('height', '24');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('fill', '#858585');
    path.setAttribute('fill-rule', 'evenodd');
    path.setAttribute('d', 'M5 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4zm7 0a2 2 0 1 0 0-4 2 2 0 0 0 0 4zm9-2a2 2 0 1 1-4 0 2 2 0 0 1 4 0z');
    svg.append(path);
    button.append(svg);
    button.addEventListener('click', function (event) {
      event.preventDefault();
      event.stopPropagation();
      const target = menuTarget();
      if (target) target.click();
    });
    return button;
  }

  // ── 追更列表填空（0.12.0）────────────────────────────────────
  // 被删系列在 /following/watchlist/novels 显示成空白框「该作品无法显示」，DOM 里没有系列 ID
  // （Vue prop 有，沙箱里读不到）。只能按位置对应：Pixiv 自己的 watch_list 接口给出这一页
  // 有序的 watchedSeriesIds，被删的不在 novelSeries 里。列表外层 div.mv-16.ph-16 的每个
  // 子元素对应一个 ID（未加载的项只渲染空 div，也占位）；子元素数与 ID 数一致、且每张健康卡
  // 的系列 ID 与同下标的 ID 一致时才填，否则本轮跳过——SPA 可能还没渲染完。
  // 只有「novelSeries 里缺这个 ID」才算被删；仅限年龄、单篇、未发布等原因的空白框不填。
  const WATCH_PAGE_TTL_MS = 30 * 1000;
  const WATCH_MISS_TTL_MS = 10 * 60 * 1000;
  const WATCH_RETRY_MS = 15 * 1000;
  const watchPageCache = new Map();
  const watchFreshItems = new Map();
  const watchMissing = new Map();
  const watchInFlight = new Set();
  let watchRetryAt = 0;

  // 同源请求 Pixiv 自己的接口（与 pixivWorkState 一样带 Cookie）；同一页 30 秒内只取一次，
  // 失败也记下来，30 秒后再试
  function fetchWatchListPage(page) {
    const cached = watchPageCache.get(page);
    if (cached && Date.now() - cached.at < WATCH_PAGE_TTL_MS) return cached.promise;
    const promise = fetch('/touch/ajax/watch_list/novel?p=' + encodeURIComponent(String(page)) + '&limit=10&new=1', {
      credentials: 'include',
      headers: { Accept: 'application/json' }
    })
      .then(function (response) { return response.json(); })
      .then(function (payload) {
        return payload && payload.error === false && payload.body ? payload.body : null;
      })
      .catch(function () { return null; });
    watchPageCache.set(page, { at: Date.now(), promise: promise });
    return promise;
  }

  // 列表外层：所有系列卡/空白框共同所在的那个 div.mv-16.ph-16，必须唯一
  function findWatchListContainer() {
    const lists = new Set();
    document.querySelectorAll('.novel-series-thumbnail-detail, .novel-series-not-viewable-detail').forEach(function (node) {
      const list = node.closest('div.mv-16.ph-16');
      if (list) lists.add(list);
    });
    return lists.size === 1 ? lists.values().next().value : null;
  }

  // 返回每个位置的 {id, state, holder, root}：state 为 healthy / deleted / other / empty；
  // 对不上（数量不同、健康卡 ID 错位、结构不是小说列表）返回 null
  function watchListSlots(list, body) {
    const ids = (Array.isArray(body.watchedSeriesIds) ? body.watchedSeriesIds : []).map(String);
    const present = new Set((Array.isArray(body.novelSeries) ? body.novelSeries : []).map(function (series) {
      return String(series && series.id);
    }));
    const children = Array.from(list.children);
    if (!ids.length || children.length !== ids.length) return null;
    const slots = [];
    for (let i = 0; i < children.length; i += 1) {
      const id = ids[i];
      const holder = children[i].firstElementChild;
      if (!holder) {
        slots.push({ id: id, state: 'empty', holder: null, root: null });
        continue;
      }
      if (!holder.classList.contains('c-mb-16')) return null;
      const healthy = holder.querySelector(':scope > .novel-series-thumbnail-detail');
      if (healthy) {
        const link = healthy.querySelector('a[href*="/novel/series/"]');
        const match = link && (link.getAttribute('href') || '').match(/\/novel\/series\/(\d+)/);
        if (!match || match[1] !== id) return null;
        slots.push({ id: id, state: 'healthy', holder: holder, root: healthy });
        continue;
      }
      const inner = holder.querySelector(':scope > .c-mb-16');
      const root = inner ? inner.querySelector(':scope > .novel-series-not-viewable-detail') : null;
      slots.push({
        id: id,
        state: root ? (present.has(id) ? 'other' : 'deleted') : 'empty',
        holder: inner,
        root: root
      });
    }
    return slots;
  }

  // 已判定不填的空白框（年龄限制等非删除原因、备份里没有、服务端还没有卡片字段）：
  // 节点不变就不再为它请求，停在这一页时不会每 30 秒问一次 Pixiv
  const watchSettledRoots = new WeakSet();

  function fillWatchListBlanks() {
    if (!isWatchlistPage()) return;
    const list = findWatchListContainer();
    if (!list) return;
    const open = Array.from(list.querySelectorAll('.novel-series-not-viewable-detail')).some(function (root) {
      return !root.hasAttribute('data-pixiv-rescue-filled') && !watchSettledRoots.has(root);
    });
    if (!open) return;
    const page = pageParamFromUrl();
    fetchWatchListPage(page).then(function (body) {
      if (!body || !isWatchlistPage() || pageParamFromUrl() !== page || !list.isConnected) return;
      const slots = watchListSlots(list, body);
      if (!slots) return;
      const wanted = [];
      slots.forEach(function (slot) {
        const card = slot.holder ? slot.holder.querySelector(':scope > [' + ROOT_ATTRIBUTE + '="watch-card"]') : null;
        if (slot.state !== 'deleted') {
          if (card) removeWatchCard(card);
          if (slot.root) watchSettledRoots.add(slot.root);
          return;
        }
        if (card && card.getAttribute('data-series-id') === slot.id) return;
        if (card) removeWatchCard(card);
        wanted.push(slot);
      });
      const unresolved = [];
      const cachedEntries = wanted.length ? readLookupCache() : {};
      wanted.forEach(function (slot) {
        const item = watchSeriesItem(slot.id, cachedEntries);
        const missing = watchMissing.has(slot.id) && Date.now() - watchMissing.get(slot.id) < WATCH_MISS_TTL_MS;
        if (item && item.chapter_count !== undefined) insertWatchCard(slot, item);
        else if (item || missing) watchSettledRoots.add(slot.root);
        else unresolved.push(slot.id);
      });
      if (unresolved.length) lookupWatchSeries(unresolved);
    });
  }

  function removeWatchCard(card) {
    const root = card.parentElement
      ? card.parentElement.querySelector(':scope > .novel-series-not-viewable-detail') : null;
    if (root) root.removeAttribute('data-pixiv-rescue-filled');
    card.remove();
  }

  // 刚查回来的结果优先用（服务端还没升级、缺卡片字段时也不会每轮重查），其次读 10 分钟缓存
  function watchSeriesItem(id, cachedEntries) {
    const fresh = watchFreshItems.get(id);
    if (fresh && Date.now() - fresh.at < LOOKUP_CACHE_TTL_MS) return fresh.item;
    return readCachedLookup(cachedEntries, id, 'series');
  }

  function lookupWatchSeries(ids) {
    if (Date.now() < watchRetryAt) return;
    const now = Date.now();
    const pending = ids.filter(function (id) {
      return !watchInFlight.has(id) && !(watchMissing.has(id) && now - watchMissing.get(id) < WATCH_MISS_TTL_MS);
    }).slice(0, LOOKUP_BATCH_LIMIT);
    if (!pending.length) return;
    pending.forEach(function (id) { watchInFlight.add(id); });
    apiGet(buildLookupPath(new Set(), new Set(pending))).then(function (data) {
      pending.forEach(function (id) { watchInFlight.delete(id); });
      const found = new Set();
      (data.series || []).forEach(function (item) {
        found.add(String(item.series_id));
        watchFreshItems.set(String(item.series_id), { at: Date.now(), item: item });
      });
      cacheLookupResults([], data.series);
      pending.forEach(function (id) { if (!found.has(id)) watchMissing.set(id, Date.now()); });
      fillWatchListBlanks();
    }, function (error) {
      pending.forEach(function (id) { watchInFlight.delete(id); });
      watchRetryAt = Date.now() + WATCH_RETRY_MS;
      const kind = error && error.message;
      if (kind === 'missing_token' || kind === 'unauthorized' ||
          kind === 'network_error' || kind === 'network_timeout' || kind === 'server_error') {
        showRescueNotice(kind);
      }
    });
  }

  // 卡片插到空白框父节点 div.c-mb-16 的最前面；空白框根节点（连同 Pixiv 底部菜单的挂载位）
  // 保留，只隐藏它的封面与文字、内边距归零。外层已有 16px 间距，卡片自身不加外边距，
  // 这样一项高 169，与健康项一致
  function insertWatchCard(slot, item) {
    addStyles();
    const root = slot.root;
    const card = renderSeriesRow(item, function () {
      return root.isConnected ? root.querySelector(':scope > .details .menu-icon') : null;
    });
    card.setAttribute(ROOT_ATTRIBUTE, 'watch-card');
    card.setAttribute('data-series-id', slot.id);
    slot.holder.insertBefore(card, slot.holder.firstChild);
    root.setAttribute('data-pixiv-rescue-filled', slot.id);
  }

  // 每轮复核：空白框根节点没了（移出追更后 Vue 把整块换成注释节点）就撤掉卡片
  function pruneWatchCards() {
    const onWatchlist = isWatchlistPage();
    document.querySelectorAll('[' + ROOT_ATTRIBUTE + '="watch-card"]').forEach(function (card) {
      const root = card.parentElement
        ? card.parentElement.querySelector(':scope > .novel-series-not-viewable-detail') : null;
      if (!onWatchlist || !root || root.getAttribute('data-pixiv-rescue-filled') !== card.getAttribute('data-series-id')) {
        removeWatchCard(card);
      }
    });
  }

  // 列表页每轮扫描：收藏/追更页的失效卡重建 + 追更列表填空
  function scanListPage() {
    pruneWatchCards();
    markBrokenCards();
    fillWatchListBlanks();
  }

  // 新作页观察器：Vue 每次切路由都重建 tab 栏，在新节点插入的同一批变动里改名
  // （回调是微任务，赶在绘制之前，原文案不会闪一下）；拯救系列页的原生内容出现或重渲染时
  // 也由它接管。观察 document 而不是 documentElement：document-start 时 <html> 可能还没有
  function installFeedObserver() {
    if (window.__pixivRescueFeedObserver) return;
    const observer = new MutationObserver(function () {
      if (!isFeedPage()) return;
      relabelRescueTab();
      ensureRescueFeed();
    });
    observer.observe(document, { childList: true, subtree: true });
    window.__pixivRescueFeedObserver = observer;
  }

  function removeFeedObserver() {
    if (!window.__pixivRescueFeedObserver) return;
    window.__pixivRescueFeedObserver.disconnect();
    window.__pixivRescueFeedObserver = null;
  }

  // 幂等：路由钩子、观察器之外，1 秒轮询也会调用（Vue 重写文本节点不产生 childList 变动）
  function handleFeedPage() {
    if (!isFeedPage()) return;
    installFeedObserver();
    relabelRescueTab();
    ensureRescueFeed();
  }

  // 切页时复原：离开 mypixiv 页撤掉拯救系列列表、放回原生内容；离开追更列表撤掉填空卡；
  // 离开新作页停掉观察器
  function leavePages() {
    if (!isRescueFeedPage()) resetRescueFeed();
    if (!isWatchlistPage()) pruneWatchCards();
    if (!isFeedPage()) removeFeedObserver();
  }

  // 系列页章节卡片照 Pixiv 手机站 .works-item-novel（2026-10-26 实测）：
  // 64×90 封面、「#N 标题」14px/700 两行截断、标签行（R-18/原创/#标签，最多三行）、
  // 「字数 · 时长 · ♥收藏数」。整张卡是链接：点进该章自己的正文页，由单篇阅读层渲染。
  function renderChapterCard(chapter) {
    const item = element('li', 'pixiv-rescue-chapter-item');
    const link = element('a', 'pixiv-rescue-chapter-card');
    link.href = rescueNovelUrl(chapter.novel_id);
    const cover = element('div', 'pixiv-rescue-chapter-cover');
    // 没有自定义封面的作品（Pixiv 上显示默认模板封面，备份里既没有图也没有地址；
    // 生产实测已删作品约 9%）：画一张素色占位、印上标题，和 Pixiv 的默认封面一样能认出是哪章
    const placeholder = function () {
      clearNode(cover);
      cover.classList.add('pixiv-rescue-chapter-cover--plain');
      const label = element('span', 'pixiv-rescue-chapter-cover-title', chapter.title || '');
      // 纯装饰：标题在卡片右侧已经有一份，读屏不该念两遍
      label.setAttribute('aria-hidden', 'true');
      cover.append(label);
    };
    if (chapter.has_cover) {
      const img = element('img', 'pixiv-rescue-chapter-thumb');
      img.alt = '';
      cover.append(img);
      lazyLoadCover(img, chapter.novel_id, placeholder);
    } else {
      placeholder();
    }
    const info = element('div', 'pixiv-rescue-chapter-info');
    info.append(element('div', 'pixiv-rescue-chapter-title',
      (chapter.chapter_number ? '#' + chapter.chapter_number + ' ' : '') + (chapter.title || '未命名章节')));
    // 标签与统计行右侧留 40px 给收藏心形（实测 .tag-container 右内边距 40px），换行位置才与 Pixiv 一致
    const detail = element('div', 'pixiv-rescue-chapter-detail');
    const tags = renderTagLine('pixiv-rescue-chapter-tags', chapter, false);
    if (tags.children.length) detail.append(tags);
    const stats = element('div', 'pixiv-rescue-chapter-stats');
    if (chapter.text_length != null) {
      stats.append(element('span', 'pixiv-rescue-chapter-stat', formatCount(chapter.text_length) + '字'));
      const minutes = formatReadingTime(chapter.text_length);
      if (minutes) stats.append(element('span', 'pixiv-rescue-chapter-stat', minutes));
    }
    if (chapter.total_bookmarks != null) {
      const like = element('span', 'pixiv-rescue-chapter-stat');
      like.append(statIcon('like'), element('span', 'pixiv-rescue-stat-count', formatCount(chapter.total_bookmarks)));
      stats.append(like);
    }
    if (stats.children.length) detail.append(stats);
    // 「浏览更多」照 Pixiv：点开底部「作品情报」面板（完整标签 + 阅读作品），不跳页
    const more = element('span', 'pixiv-rescue-chapter-more', '浏览更多');
    more.setAttribute('role', 'button');
    more.tabIndex = 0;
    const openSheet = function (event) {
      event.preventDefault();
      event.stopPropagation();
      openWorkInfoSheet(chapter);
    };
    more.addEventListener('click', openSheet);
    more.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') openSheet(event);
    });
    detail.append(more);
    info.append(detail);
    // 收藏心形只是版式占位：作品在 Pixiv 上已删，收藏不了；不响应点击，不进无障碍树
    link.append(cover, info, outlineHeart());
    item.append(link);
    return item;
  }

  function outlineHeart() {
    const box = element('span', 'pixiv-rescue-chapter-heart');
    box.setAttribute('aria-hidden', 'true');
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '32');
    svg.setAttribute('height', '32');
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('fill', 'currentColor');
    path.setAttribute('d', 'M16.5 3c-1.74 0-3.41.81-4.5 2.09C10.91 3.81 9.24 3 7.5 3 4.42 3 2 5.42 2 8.5c0 3.78 3.4 6.86 8.55 11.54L12 21.35l1.45-1.32C18.6 15.36 22 12.28 22 8.5 22 5.42 19.58 3 16.5 3zm-4.4 15.55l-.1.1-.1-.1C7.14 14.24 4 11.39 4 8.5 4 6.5 5.5 5 7.5 5c1.54 0 3.04.99 3.57 2.36h1.87C13.46 5.99 14.96 5 16.5 5c2 0 3.5 1.5 3.5 3.5 0 2.89-3.14 5.74-7.9 10.05z');
    svg.append(path);
    box.append(svg);
    return box;
  }

  // 「作品情报」底部面板（实测手机站 .bottom-popup）：标题、关闭、（标签）、整宽黑色按钮。
  // 阅读层里挂进 readerRoot；列表页（拯救系列 tab、追更列表）挂到 body，遮罩换成页面级层叠
  function openInfoSheet(host, options) {
    if (!host) return;
    const backdrop = element('div', 'pixiv-rescue-sheet-backdrop');
    if (options.pageLevel) backdrop.classList.add('pixiv-rescue-sheet-backdrop--page');
    backdrop.setAttribute(ROOT_ATTRIBUTE, 'sheet');
    const sheet = element('div', 'pixiv-rescue-sheet');
    sheet.setAttribute('role', 'dialog');
    sheet.setAttribute('aria-modal', 'true');
    sheet.setAttribute('aria-label', '作品情报');
    const head = element('div', 'pixiv-rescue-sheet-head');
    head.append(element('span', 'pixiv-rescue-sheet-title', '作品情报'));
    const close = element('button', 'pixiv-rescue-sheet-close', '×');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭');
    head.append(close);
    sheet.append(head);
    if (options.tagLine && options.tagLine.children.length) sheet.append(options.tagLine);
    const action = element('a', 'pixiv-rescue-read-link', options.linkText);
    action.href = options.href;
    sheet.append(action);
    backdrop.append(sheet);
    const dismiss = function () { backdrop.remove(); };
    close.addEventListener('click', dismiss);
    backdrop.addEventListener('click', function (event) { if (event.target === backdrop) dismiss(); });
    host.append(backdrop);
    close.focus();
  }

  function openWorkInfoSheet(chapter) {
    openInfoSheet(readerRoot, {
      tagLine: renderTagLine('pixiv-rescue-sheet-tags', chapter, false),
      linkText: '阅读作品',
      href: rescueNovelUrl(chapter.novel_id)
    });
  }

  // 切页时调用：挂在 body 上的面板（遮罩层叠 2147483500）不随 Pixiv 的 SPA 切页消失，
  // 0.12.0 及以前会一直盖在新页面上。阅读层里的面板随阅读层一起销毁，这里不碰
  function removePageSheets() {
    document.querySelectorAll('[' + ROOT_ATTRIBUTE + '="sheet"]').forEach(function (node) {
      if (!readerRoot || !readerRoot.contains(node)) node.remove();
    });
  }

  // 封面滚到附近才取：76 话的系列不在打开时一口气拉 76 张图
  let coverObserver = null;
  function lazyLoadCover(img, novelId, onFail) {
    const load = function () {
      loadRescueCover(img, novelId, 'novel', onFail);
    };
    if (typeof IntersectionObserver !== 'function') {
      load();
      return;
    }
    if (!coverObserver) {
      coverObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          coverObserver.unobserve(entry.target);
          if (typeof entry.target.__pixivRescueLoad === 'function') entry.target.__pixivRescueLoad();
        });
      }, { rootMargin: '400px 0px' });
    }
    img.__pixivRescueLoad = load;
    coverObserver.observe(img);
  }

  function renderChapterDirectory(panel, data, seriesId) {
    const staleError = panel.querySelector('.pixiv-rescue-directory-error');
    if (staleError) staleError.remove();
    let list = panel.querySelector('.pixiv-rescue-directory');
    if (!list) {
      list = element('ol', 'pixiv-rescue-directory');
      panel.append(list);
    }
    const existingMore = list.querySelector('[data-rescue-load-more]');
    if (existingMore) existingMore.remove();
    const chapters = Array.isArray(data.items) ? data.items : [];
    chapters.forEach(function (chapter) {
      list.append(renderChapterCard(chapter));
    });
    if (!chapters.length && !list.children.length) {
      list.append(element('li', 'pixiv-rescue-chapter-empty', '备份中没有可读取的章节。'));
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
            // 异步回调可能在切页、目录被清空后才到达：目录已不在面板里就放弃本轮
            if (!panel.isConnected || !panel.contains(list)) return;
            renderChapterDirectory(panel, nextDirectory, seriesId);
          })
          .catch(function (error) {
            if (panel.isConnected) renderDirectoryError(panel, error);
          })
          .finally(function () {
            moreButton.disabled = false;
          });
      });
      moreItem.append(moreButton);
      list.append(moreItem);
    }
  }

  // 系列页照搬 Pixiv 手机站（2026-10-26 实测）：作者、封面、R-18/原创标记、
  // 系列名（16px/700）、「共N话 · 某日更新 · 总字数 · 总时长」、「从最初开始阅读」
  // 黑色胶囊，再是「作品」下的章节卡片。追更/分享等对已删系列无效的按钮不画。
  function renderSeries(data, seriesId) {
    const series = data.series;
    const chapters = data.chapters;
    const items = chapters && Array.isArray(chapters.items) ? chapters.items : [];
    const body = openReader();
    const panel = element('div', 'pixiv-rescue-panel');
    if (series.author_name) {
      const author = element('div', 'pixiv-rescue-author');
      author.append(element('span', 'pixiv-rescue-author-avatar'),
        element('span', 'pixiv-rescue-author-name', series.author_name));
      panel.append(author);
    }
    const band = element('div', 'pixiv-rescue-cover-band');
    const img = element('img', 'pixiv-rescue-cover-img');
    img.alt = '';
    band.append(img);
    panel.append(band);
    loadRescueCover(img, seriesId, 'series', function () { band.remove(); });

    const head = element('div', 'pixiv-rescue-series-head');
    const badges = element('div', 'pixiv-rescue-series-badges');
    const r18 = items.some(function (item) {
      return Number(item.x_restrict) > 0 || workTags(item).some(function (tag) { return /^R-18G?$/.test(tag.name); });
    });
    if (r18) badges.append(element('span', 'pixiv-rescue-badge pixiv-rescue-badge-r18', 'R-18'));
    if (items.length && items.every(function (item) { return item.is_original; })) {
      badges.append(element('span', 'pixiv-rescue-badge pixiv-rescue-badge-original', '原创'));
    }
    if (badges.children.length) head.append(badges);
    head.append(element('h1', 'pixiv-rescue-title', series.title || '未命名备份系列'));
    head.append(renderSeriesInfo(series, chapters));
    renderNotice(head, series);
    if (items.length) head.append(renderSeriesReadActions(seriesId, items[0]));
    panel.append(head);
    const tab = element('div', 'pixiv-rescue-series-tab');
    tab.append(element('span', 'pixiv-rescue-series-tab-label', '作品'));
    panel.append(tab);
    if (chapters) renderChapterDirectory(panel, chapters, seriesId);
    else renderDirectoryError(panel, data.chaptersError, function (button) { retrySeriesChapters(panel, data, seriesId, button); });
    body.append(panel);
  }

  // 「共25话 · 2026年1月27日更新 · 600,558字 · 25小时1分钟」；只备份了部分章节时
  // 如实写「共12话（备份11话）」，不假装完整
  function renderSeriesInfo(series, chapters) {
    const line = element('div', 'pixiv-rescue-series-info');
    const saved = chapters ? Number(chapters.total || 0) : Number(series.complete_count || 0);
    const expected = Number(series.expected_count || 0);
    const count = expected > saved ? '共' + expected + '话（备份' + saved + '话）' : '共' + saved + '话';
    const parts = [count];
    if (chapters) {
      const updated = formatPixivDay(chapters.last_create_date);
      if (updated) parts.push(updated + '更新');
      if (chapters.total_text_length) {
        parts.push(formatCount(chapters.total_text_length) + '字');
        const minutes = formatReadingTime(chapters.total_text_length);
        if (minutes) parts.push(minutes);
      }
    }
    parts.forEach(function (text) { line.append(element('span', 'pixiv-rescue-series-info-item', text)); });
    return line;
  }

  // 「从最初开始阅读」；读过的系列另给「继续阅读 #N」（进度存在脚本自己的存储里）
  function renderSeriesReadActions(seriesId, firstChapter) {
    const box = element('div', 'pixiv-rescue-read-actions');
    const progress = readSeriesProgress(seriesId);
    if (progress && String(progress.novel_id) !== String(firstChapter.novel_id)) {
      const resume = element('a', 'pixiv-rescue-read-link',
        '继续阅读' + (progress.chapter_number ? ' #' + progress.chapter_number : ''));
      resume.href = rescueNovelUrl(progress.novel_id);
      box.append(resume);
      const restart = element('a', 'pixiv-rescue-read-link pixiv-rescue-read-link--secondary', '从最初开始阅读');
      restart.href = rescueNovelUrl(firstChapter.novel_id);
      box.append(restart);
    } else {
      const start = element('a', 'pixiv-rescue-read-link', '从最初开始阅读');
      start.href = rescueNovelUrl(firstChapter.novel_id);
      box.append(start);
    }
    return box;
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
      if (isStale()) return;
      if (state === 'available') {
        // 健康作品：不请求备份、不动页面，只顺带记下用户的 Aa 阅读设置
        if (type === 'novel') learnReaderTheme(isStale);
        return;
      }
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
      // 系列信息与第一页目录一起取，页面一次画完（不再要求先点「加载目录」）。
      // 目录失败不拖垮整页：系列头照常显示，目录位置给错误提示。
      const base = '/api/rescue/v1/series/' + encodeURIComponent(seriesId);
      return Promise.all([
        apiGet(base),
        apiGet(base + '/chapters').catch(function (error) { return { __error: error }; })
      ]).then(function (results) {
        const chapters = results[1] && results[1].__error ? null : results[1];
        return { series: results[0], chapters: chapters, chaptersError: chapters ? null : results[1].__error };
      });
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
      || isFeedPage();
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
      // 手机脚本管理器可能稍后才注入 GM 对象：在 5 秒内重试，超时才放弃并提示（只提示这一次）
      const tries = Number(attempt || 0);
      if (tries < 10) {
        window.setTimeout(function () { start(tries + 1); }, 500);
      } else {
        showRuntimeUnsupported(missingRuntimeApis());
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
      // 新作页的观察器不等 body：Vue 首次渲染 tab 栏时就要改名，晚了原文案会先露出来
      handleFeedPage();
      if (document.body || isDetailPage()) {
        routeRescue();
      } else {
        document.addEventListener('DOMContentLoaded', routeRescue, { once: true });
      }
    };
    routeWhenReady();
    let lastHref = window.location.href;
    let feedFailed = false;
    function onRoute() {
      keepReaderAttached();
      // 新作页每秒复核一次：tab 改名、拯救系列 tab 的翻页（自己 pushState 后同步进到这里）。
      // 这里也在 Pixiv 自己的 pushState 调用栈里，异常不能往外抛；每秒都跑，错误条只出一次
      try {
        handleFeedPage();
      } catch (error) {
        if (!feedFailed) showFatalError(error);
        feedFailed = true;
      }
      if (window.location.href === lastHref) return;
      lastHref = window.location.href;
      removePageSheets();
      destroyReader();
      resetCoverRetries();
      // 固定定位的提示条不随页面流消失，切页时一并清掉，并允许新页面重新提示
      const notice = document.querySelector('[data-pixiv-rescue-notice]');
      if (notice) notice.remove();
      lastNoticeKey = null;
      try {
        leavePages();
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
    // 追更列表既是新作页（tab 改名）又是列表页（失效卡与填空），两样都要
    handleFeedPage();
    if (isBookmarksListPage() || isWatchlistPage()) handleListPage();
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

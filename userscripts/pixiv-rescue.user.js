// ==UserScript==
// @name         Pixiv 小说私人备份救援阅读
// @namespace    https://pixiv.dongboapp.com/
// @version      0.1.0
// @description  仅在 Pixiv 小说或系列明确失效时，从私人备份读取并标记救援内容。
// @match        https://www.pixiv.net/novel/show.php*
// @match        https://www.pixiv.net/novel/series/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect     pixiv.dongboapp.com
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  const API_ORIGIN = 'https://pixiv.dongboapp.com';
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
    'Page not found'
  ];

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
        url: API_ORIGIN + path,
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
            reject(new Error(response.status === 401 ? 'unauthorized' : 'request_failed'));
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
    const text = pageText();
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
      '@media(max-width:640px){.pixiv-rescue-root{margin:12px auto;padding:0 10px}.pixiv-rescue-panel{padding:16px}.pixiv-rescue-title{font-size:20px}}'
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
    const messages = {
      missing_token: '未设置救援 Token，请通过油猴菜单完成设置。',
      unauthorized: '救援 Token 无效，请在网站设置页轮换后重新填写。',
      network_timeout: '救援服务响应超时，Pixiv 原页面已保留。',
      network_error: '无法连接救援服务，Pixiv 原页面已保留。'
    };
    root.append(element('div', 'pixiv-rescue-error', messages[error.message] || '未找到可用的救援数据，Pixiv 原页面已保留。'));
  }

  function renderSeriesChapter(viewer, data) {
    clearNode(viewer);
    viewer.append(element('h2', 'pixiv-rescue-title', data.title || '未命名章节'));
    renderNotice(viewer, data);
    renderText(viewer, data.text_raw || '备份正文为空');
  }

  function renderChapterDirectory(panel, viewer, data) {
    const existing = panel.querySelector('.pixiv-rescue-directory');
    if (existing) existing.remove();
    const list = element('ol', 'pixiv-rescue-directory');
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
          .catch(renderError)
          .finally(function () {
            button.disabled = false;
          });
      });
      item.append(button);
      list.append(item);
    });
    if (!chapters.length) list.append(element('li', '', '备份中没有可读取的章节。'));
    panel.insertBefore(list, viewer);
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
          renderChapterDirectory(panel, viewer, directory);
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
    const match = window.location.pathname.match(/\/novel\/series\/(\d+)/);
    return match ? match[1] : null;
  }

  function handleNovelPage() {
    if (isNovelPageHealthy()) return;
    const novelId = novelIdFromPage();
    if (!novelId) return;
    apiGet('/api/rescue/v1/novels/' + encodeURIComponent(novelId))
      .then(renderNovel)
      .catch(renderError);
  }

  function handleSeriesPage() {
    if (isSeriesPageHealthy()) return;
    const seriesId = seriesIdFromPage();
    if (!seriesId) return;
    apiGet('/api/rescue/v1/series/' + encodeURIComponent(seriesId))
      .then(function (data) {
        renderSeries(data, seriesId);
      })
      .catch(renderError);
  }

  function start() {
    registerTokenMenus();
    if (window.location.pathname === '/novel/show.php') {
      handleNovelPage();
      return;
    }
    if (window.location.pathname.startsWith('/novel/series/')) handleSeriesPage();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();

/* Local-only VM harness. Watch callbacks are explicitly simulated, not Vue scheduling. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const templates = path.resolve(__dirname, '../../src/pixiv_novel_sync/templates');
const source = name => fs.readFileSync(path.join(templates, name), 'utf8');
function resolveIncludes(name, depth = 0) {
  assert.ok(depth < 12 && /^[a-z0-9_]+\.html$/.test(name), 'Only bounded local template includes are allowed');
  return source(name).replace(/{%\s*include\s+['"]([^'"]+)['"]\s*%}/g,
    (_, included) => resolveIncludes(included, depth + 1));
}
const base = source('base.html');
const FAKE_TOKEN = 'FAKE-TASK5-TOKEN-NOT-A-CREDENTIAL';
const response = (data, status = 200) => ({
  ok: status >= 200 && status < 300, status, json: async () => data,
});
const envelope = data => response({ok: true, data});
const pendingPage = (page, id = page) => response({
  items: [{id, item_id: id, item_type: 'novel', title: `Fixture ${id}`}],
  page, total_pages: 8, total: 141,
});
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
const plain = value => JSON.parse(JSON.stringify(value));
const flush = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };

function mount(name, search = '') {
  const html = resolveIncludes(name);
  const calls = [], historyWrites = [], navigation = [], domCalls = [], clipboardWrites = [];
  const mounted = [], unmounted = [], timers = new Map(), storageWrites = [];
  const watchers = [], components = new Map(), listeners = new Map();
  const stored = new Map();
  let state, clock = 0, timerId = 0;
  let handleRequest = (url, options) => {
    throw new Error(`Unexpected fake request: ${options.method || 'GET'} ${url}`);
  };
  const pathname = name === 'dashboard_pending_deletions.html' ? '/dashboard/pending-deletions'
    : '/dashboard/' + name.replace(/^dashboard_/, '').replace(/\.html$/, '').replaceAll('_', '/');
  const location = {pathname, search, hash: '', assign(next) {
    const url = new URL(next, 'https://management.test.invalid');
    assert.equal(url.origin, 'https://management.test.invalid', 'Only local fixture navigation');
    navigation.push(url.pathname + url.search + url.hash);
    location.pathname = url.pathname; location.search = url.search; location.hash = url.hash;
  }};
  const history = {replaceState(_state, _title, next) {
    const url = new URL(next, 'https://management.test.invalid');
    historyWrites.push(next);
    location.pathname = url.pathname;
    location.search = url.search;
  }};
  const dispatch = (via, url, options = {}) => {
    assert.match(url, /^\/api\//, 'VM transport accepts only fixture API paths');
    const request = {via, url, method: (options.method || 'GET').toUpperCase(), options};
    calls.push(request);
    return handleRequest(url, options, request);
  };
  const document = {activeElement: null, getElementById: id => elements.get(id) || null};
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, {
    id,
    focus(options) { document.activeElement = this; domCalls.push({id, method: 'focus', options}); },
    scrollIntoView(options) { domCalls.push({id, method: 'scrollIntoView', options}); },
    select() { domCalls.push({id, method: 'select'}); },
    setSelectionRange(start, end) { domCalls.push({id, method: 'setSelectionRange', start, end}); },
  }]));
  const navigator = {clipboard: {writeText: async value => { clipboardWrites.push(value); }}};
  const window = {
    location, history, navigator, confirm: () => false,
    csrfFetch: (url, options) => dispatch('csrfFetch', url, options),
    toast() { throw new Error('Management feedback must not depend on a toast'); },
    addEventListener(type, fn) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(fn); },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
  };
  const localStorage = {
    getItem: key => stored.get(key) || null,
    setItem(key, value) { storageWrites.push([key, value]); stored.set(key, value); },
    removeItem: key => stored.delete(key),
  };
  const context = vm.createContext({
    window, document, navigator, location, history, localStorage, URL, URLSearchParams,
    TextDecoder, TextEncoder, Uint8Array, AbortController,
    console: {log() {}, warn() {}, error() {}},
    confirm: text => window.confirm(text),
    fetch: (url, options) => dispatch('fetch', url, options),
    csrfFetch: (url, options) => window.csrfFetch(url, options),
    errorText: (...args) => window.errorText(...args),
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, {fn, at: clock + delay}); return id; },
    clearTimeout: id => timers.delete(id),
    setInterval() { throw new Error('These read/copy tests must not start detection polling'); },
    clearInterval() {},
    Vue: {
      ref: value => ({value}), reactive: value => value,
      computed: getter => ({get value() { return getter(); }}),
      nextTick: callback => Promise.resolve().then(() => callback && callback()),
      onMounted: callback => mounted.push(callback),
      onUnmounted: callback => unmounted.push(callback),
      watch(getter, callback, options = {}) {
        const read = () => typeof getter === 'function' ? getter() : Array.isArray(getter)
          ? getter.map(item => typeof item === 'function' ? item() : item.value) : getter.value;
        const entry = {read, callback, previous: JSON.stringify(read())};
        watchers.push(entry);
        if (options.immediate) callback(read(), undefined);
        return () => { watchers.splice(watchers.indexOf(entry), 1); };
      },
    },
    initVueApp: options => {
      if (window.registerPageComponents) window.registerPageComponents({component: (name, definition) => components.set(name, definition)});
      state = options.setup();
    },
  });
  window.Vue = context.Vue;
  // Reuse the real error/query/API helpers; only the transport is replaced.
  for (const helper of ['errorText', 'readListQuery', 'writeListQuery']) {
    const match = base.match(new RegExp(`window\\.${helper} = function[\\s\\S]*?\\n    };`));
    assert.ok(match, `Shared helper ${helper} exists`);
    vm.runInContext(match[0], context, {filename: `base.html:${helper}`});
  }
  const aiApi = base.match(/window\.aiApi = \{[\s\S]*?\n    };/);
  assert.ok(aiApi);
  vm.runInContext(aiApi[0], context, {filename: 'base.html:aiApi'});
  const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)];
  assert.equal(scripts.filter(script => !/\bsrc=/.test(script[1])).length, 1,
    'Execute the actual page script without rewriting its logic');
  for (const script of scripts) {
    assert.ok(script[1].includes('nonce="{[ csp_nonce() ]}"'), 'Shipped scripts require the CSP nonce');
    const src = script[1].match(/\bsrc="([^"]+)"/)?.[1];
    if (src) {
      assert.match(src, /^\/static\/js\/ai_[a-z_]+\.js$/, 'Only local unversioned AI scripts');
      const file = path.join(templates, '..', src.slice(1));
      vm.runInContext(fs.readFileSync(file, 'utf8'), context, {filename: src});
    } else vm.runInContext(script[2], context, {filename: name});
  }
  return {
    state, html, calls, historyWrites, navigation, location, document, domCalls, elements,
    navigator, window, clipboardWrites, storageWrites, stored, components, listeners,
    replyWith(fn) { handleRequest = fn; },
    async start() { for (const fn of mounted) await fn(); await flush(); },
    unmount() { for (const fn of unmounted) fn(); },
    triggerWatches() {
      for (const entry of watchers) {
        const value = entry.read(), next = JSON.stringify(value);
        if (next !== entry.previous) {
          const previous = entry.previous === undefined ? undefined : JSON.parse(entry.previous);
          entry.previous = next;
          entry.callback(value, previous);
        }
      }
    },
    advance(ms) {
      clock += ms;
      for (const [id, timer] of timers) {
        if (timer.at <= clock) { timers.delete(id); timer.fn(); }
      }
    },
  };
}


module.exports = {source, FAKE_TOKEN, response, envelope, pendingPage, deferred, plain, flush, mount};

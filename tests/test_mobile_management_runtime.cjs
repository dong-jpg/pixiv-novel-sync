/* Pure unit tests: execute the shipped template scripts in a Node VM.
 * Transport, clipboard, Vue reactivity and DOM methods are in-memory fakes.
 * There is no browser, server, provider, credential or real mutation here.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const templates = path.resolve(__dirname, '../src/pixiv_novel_sync/templates');
const source = name => fs.readFileSync(path.join(templates, name), 'utf8');
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
  const html = source(name);
  const calls = [], historyWrites = [], domCalls = [], clipboardWrites = [];
  const mounted = [], unmounted = [], timers = new Map(), storageWrites = [];
  const stored = new Map();
  let state, clock = 0, timerId = 0;
  let handleRequest = (url, options) => {
    throw new Error(`Unexpected fake request: ${options.method || 'GET'} ${url}`);
  };
  const location = {pathname: '/dashboard/pending-deletions', search};
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
  };
  const localStorage = {
    getItem: key => stored.get(key) || null,
    setItem(key, value) { storageWrites.push([key, value]); stored.set(key, value); },
    removeItem: key => stored.delete(key),
  };
  const context = vm.createContext({
    window, document, navigator, location, history, localStorage, URL, URLSearchParams,
    TextDecoder, Uint8Array, AbortController,
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
    },
    initVueApp: options => { state = options.setup(); },
  });
  // Reuse the real error/query/API helpers; only the transport is replaced.
  for (const helper of ['errorText', 'readListQuery', 'writeListQuery']) {
    const match = base.match(new RegExp(`window\\.${helper} = function[\\s\\S]*?\\n    };`));
    assert.ok(match, `Shared helper ${helper} exists`);
    vm.runInContext(match[0], context, {filename: `base.html:${helper}`});
  }
  const aiApi = base.match(/window\.aiApi = \{[\s\S]*?\n    };/);
  assert.ok(aiApi);
  vm.runInContext(aiApi[0], context, {filename: 'base.html:aiApi'});
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  assert.equal(scripts.length, 1, 'Execute the actual page script without rewriting its logic');
  vm.runInContext(scripts[0][1], context, {filename: name});
  return {
    state, html, calls, historyWrites, location, document, domCalls,
    navigator, window, clipboardWrites, storageWrites,
    replyWith(fn) { handleRequest = fn; },
    async start() { for (const fn of mounted) await fn(); await flush(); },
    unmount() { for (const fn of unmounted) fn(); },
    advance(ms) {
      clock += ms;
      for (const [id, timer] of timers) {
        if (timer.at <= clock) { timers.delete(id); timer.fn(); }
      }
    },
  };
}

test('pending: HTTP 500 JSON is an error, preserves rows and selected page/filter', async () => {
  const m = mount('dashboard_pending_deletions.html');
  m.state.items.value = [{id: 91, title: 'Last successful list'}];
  m.state.totalItems.value = 141;
  m.state.totalPages.value = 8;
  m.replyWith(() => response({detail: 'Fixture service unavailable'}, 500));
  await m.state.loadPending(3, 'series');
  assert.match(m.state.loadError.value, /Fixture service unavailable/);
  assert.deepEqual(plain(m.state.items.value), [{id: 91, title: 'Last successful list'}]);
  assert.equal(m.state.totalItems.value, 141);
  assert.equal(m.state.totalPages.value, 8);
  assert.equal(m.state.currentPage.value, 3);
  assert.equal(m.state.currentType.value, 'series');
  assert.equal(m.location.search, '?page=3&type=series');
  assert.equal(m.state.loading.value, false);
});

test('pending: HTTP 200 business error cannot become the empty state', async () => {
  const m = mount('dashboard_pending_deletions.html');
  m.replyWith(() => response({ok: false, error: 'Fixture business error', items: []}));
  await m.state.loadPending(2, 'novel');
  assert.match(m.state.loadError.value, /Fixture business error/);
  assert.equal(m.state.currentPage.value, 2);
  assert.equal(m.state.currentType.value, 'novel');
});

test('pending: non-JSON HTTP failure retains its HTTP status', async () => {
  const m = mount('dashboard_pending_deletions.html');
  m.replyWith(() => ({ok: false, status: 503, json: async () => { throw new Error('HTML gateway'); }}));
  await m.state.loadPending(4, 'series');
  assert.match(m.state.loadError.value, /HTTP 503/);
});

test('pending: malformed success is an error, but a valid empty list is not', async () => {
  const m = mount('dashboard_pending_deletions.html');
  m.replyWith(() => response({message: 'not a list'}));
  await m.state.loadPending(1, '');
  assert.ok(m.state.loadError.value, 'Missing items must not mean no pending records');
  m.replyWith(() => response({items: [], page: 1, total_pages: 1, total: 0}));
  await m.state.loadPending(1, '');
  assert.equal(m.state.loadError.value, '');
  assert.equal(m.state.items.value.length, 0);
  assert.equal(m.state.totalItems.value, 0);
});

test('pending: selection and query update immediately, not after the response', async () => {
  const m = mount('dashboard_pending_deletions.html');
  const slow = deferred();
  m.replyWith(() => slow.promise);
  const loading = m.state.loadPending(6, 'series');
  try {
    assert.equal(m.state.currentPage.value, 6);
    assert.equal(m.state.currentType.value, 'series');
    assert.equal(m.location.search, '?page=6&type=series');
    assert.equal(m.state.loading.value, true);
  } finally { slow.resolve(pendingPage(6)); await loading; }
});

test('pending: late page success cannot overwrite the newer filter response or URL', async () => {
  const m = mount('dashboard_pending_deletions.html');
  const old = deferred(), latest = deferred();
  m.replyWith(url => url.includes('page=2') ? old.promise : latest.promise);
  const first = m.state.loadPending(2, 'novel');
  const second = m.state.loadPending(4, 'series');
  latest.resolve(pendingPage(4, 404)); await second;
  const writes = m.historyWrites.length;
  old.resolve(pendingPage(2, 202)); await first;
  assert.equal(m.state.items.value[0].id, 404);
  assert.equal(m.state.currentPage.value, 4);
  assert.equal(m.state.currentType.value, 'series');
  assert.equal(m.location.search, '?page=4&type=series');
  assert.equal(m.historyWrites.length, writes, 'Stale completion must not rewrite history');
});

test('pending: stale failure cannot clear the new loading state or show an old error', async () => {
  const m = mount('dashboard_pending_deletions.html');
  const old = deferred(), latest = deferred();
  m.replyWith(url => url.includes('page=2') ? old.promise : latest.promise);
  const first = m.state.loadPending(2, 'novel');
  const second = m.state.loadPending(3, 'series');
  old.reject(new Error('Old offline request')); await first;
  try {
    assert.equal(m.state.loading.value, true);
    assert.equal(m.state.loadError.value, '');
    assert.equal(m.location.search, '?page=3&type=series');
  } finally { latest.resolve(pendingPage(3)); await second; }
});

test('pending: new failure survives a late old success', async () => {
  const m = mount('dashboard_pending_deletions.html');
  const old = deferred();
  m.replyWith(url => url.includes('page=2') ? old.promise : response({detail: 'Latest failed'}, 500));
  const first = m.state.loadPending(2, 'novel');
  await m.state.loadPending(3, 'series');
  old.resolve(pendingPage(2)); await first;
  assert.match(m.state.loadError.value, /Latest failed/);
  assert.equal(m.state.currentPage.value, 3);
  assert.equal(m.state.currentType.value, 'series');
});

test('pending: inline retry repeats only the selected GET, never detect/delete/restore', async () => {
  const m = mount('dashboard_pending_deletions.html');
  m.replyWith(() => { throw new Error('Fixture offline'); });
  await m.state.loadPending(5, 'series');
  assert.equal(typeof m.state.retryPending, 'function', 'Expose a read-only retry action');
  m.replyWith(() => pendingPage(5));
  await m.state.retryPending();
  assert.equal(m.calls.length, 2);
  for (const call of m.calls) {
    assert.equal(call.url, '/api/dashboard/pending-deletions?page=5&item_type=series');
    assert.equal(call.method, 'GET');
    assert.equal(call.via, 'fetch');
  }
  assert.equal(m.state.loadError.value, '');
  assert.equal(m.state.currentPage.value, 5);
  assert.equal(m.state.currentType.value, 'series');
});

test('pending: initial URL context survives a failed mount request', async () => {
  const m = mount('dashboard_pending_deletions.html', '?page=7&type=novel');
  m.replyWith(() => response({detail: 'Fixture failure'}, 500));
  await m.start();
  assert.equal(m.location.search, '?page=7&type=novel');
  assert.equal(m.state.currentPage.value, 7);
  assert.equal(m.state.currentType.value, 'novel');
  assert.ok(m.state.loadError.value);
  assert.equal(m.calls.length, 1);
});

test('pending: unmount invalidates a late response', async () => {
  const m = mount('dashboard_pending_deletions.html');
  const late = deferred();
  m.replyWith(() => late.promise);
  const loading = m.state.loadPending(3, 'series');
  m.unmount();
  const writes = m.historyWrites.length;
  late.resolve(pendingPage(3)); await loading;
  assert.equal(m.state.items.value.length, 0);
  assert.equal(m.historyWrites.length, writes);
});

for (const action of ['editProvider', 'copyFromProvider']) {
  test(`models: ${action} locates and focuses its populated form without fetching`, async () => {
    const m = mount('dashboard_settings_models.html');
    m.state.providerForm.api_key = 'FAKE-UNSAVED-KEY';
    m.state[action]({id: 17, name: 'Synthetic Provider', provider_type: 'openai_compatible'});
    await flush();
    assert.equal(m.state.providerForm.name, 'Synthetic Provider');
    assert.equal(m.state.providerForm.id, action === 'editProvider' ? 17 : 0);
    assert.equal(m.state.providerForm.api_key, '');
    assert.equal(m.document.activeElement?.id, 'provider-name');
    assert.ok(m.domCalls.some(call => call.id === 'provider-editor' && call.method === 'scrollIntoView'));
    assert.equal(m.calls.length, 0);
  });
}

const poolDetail = (id = 23, name = 'Synthetic pool') => ({
  id, name, description: '', pool_kind: 'custom', fallback_pool_id: null,
  enabled: false, version: 2, members: [], referenced_by_agents: [], referenced_by_pools: [],
});

function poolEditorDisabled(m) {
  // Evaluate the shipped fieldset binding against unwrapped setup state. This
  // checks the actual gate, but is not a browser/rendered-disabled-state test.
  const binding = m.html.match(/<fieldset\b[^>]*:disabled="([^"]+)"/);
  assert.ok(binding, 'Pool controls have a fieldset-level edit gate');
  const bindings = Object.fromEntries(Object.entries(m.state).map(([key, value]) => [
    key, value && typeof value === 'object' && 'value' in value ? value.value : value,
  ]));
  return Boolean(vm.runInNewContext(binding[1], bindings));
}

function templateBindings(m, locals = {}) {
  return {...Object.fromEntries(Object.entries(m.state).map(([key, value]) => [
    key, value && typeof value === 'object' && 'value' in value ? value.value : value,
  ])), ...locals};
}

function templateControlStates(m, matches, locals = {}) {
  // Inspect actual ancestor bindings, not just the reset handler. This small
  // source-tree check is intentionally not a CSS/layout or browser simulation.
  const content = m.html.match(/{% block content %}([\s\S]*?){% endblock %}/)[1]
    .replace(/{#[\s\S]*?#}|<!--[\s\S]*?-->/g, '');
  const stack = [], controls = [];
  const voidTags = new Set(['input', 'img', 'br', 'hr', 'meta', 'link']);
  for (const match of content.matchAll(/<\/?([a-z][\w:-]*)\b(?:[^"'<>]|"[^"]*"|'[^']*')*>/gi)) {
    const raw = match[0], tag = match[1].toLowerCase();
    if (raw.startsWith('</')) {
      assert.equal(stack.at(-1)?.tag, tag, 'Template ancestor stack must stay balanced');
      stack.pop();
      continue;
    }
    const attrs = Object.fromEntries([...raw.matchAll(/\s([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)]
      .map(([, key, double, single, bare]) => [key, double ?? single ?? bare ?? '']));
    const node = {tag, attrs};
    if (['button', 'input', 'select', 'textarea'].includes(tag) && matches(attrs, tag)) {
      controls.push({attrs, chain: [...stack, node]});
    }
    if (!voidTags.has(tag) && !raw.endsWith('/>')) stack.push(node);
  }
  const bindings = templateBindings(m, locals);
  const evaluate = expression => Boolean(vm.runInNewContext(expression, bindings));
  return controls.map(control => ({
    expression: control.attrs['@click'],
    id: control.attrs.id,
    model: control.attrs['v-model'] || control.attrs['v-model.number'],
    visible: control.chain.every(({attrs}) => {
      assert.ok(!('v-else' in attrs) && !('v-else-if' in attrs), 'Sibling branches need explicit evaluator support');
      return !('hidden' in attrs)
        && (!('v-if' in attrs) || evaluate(attrs['v-if']))
        && (!('v-show' in attrs) || evaluate(attrs['v-show']));
    }),
    disabled: control.chain.some(({tag, attrs}) => ['button', 'input', 'select', 'textarea', 'fieldset'].includes(tag)
      && ('disabled' in attrs || (':disabled' in attrs && evaluate(attrs[':disabled'])))),
  }));
}

function clickAvailableTemplateAction(m, expression, locals = {}) {
  const states = templateControlStates(m, attrs => attrs['@click'] === expression, locals);
  const control = states.find(item => item.visible && !item.disabled);
  assert.ok(control, 'Action must be reachable before dispatch: ' + JSON.stringify(states));
  const result = vm.runInNewContext(control.expression, templateBindings(m, locals));
  return typeof result === 'function' ? result() : result;
}

function clickReachablePoolReset(m) {
  const states = templateControlStates(m, attrs => /^resetPoolForm(?:\(|$)/.test(attrs['@click'] || ''));
  const cancel = states.find(control => control.expression === 'resetPoolForm(true)' && control.visible && !control.disabled);
  assert.ok(cancel, 'A visible, enabled cancel/new control must remain reachable: ' + JSON.stringify(states));
  // Invoke the handler expression from the available control, never bypass its
  // visibility/fieldset gate by directly calling state.resetPoolForm.
  vm.runInNewContext(cancel.expression, templateBindings(m));
}

async function loadPoolAThenFailB(m) {
  m.replyWith((url, options) => {
    if (options.method) return response({detail: 'Unexpected fixture-only write rejected'}, 400);
    if (url.includes('/23/attempts?')) return envelope([]);
    if (url.endsWith('/23')) return envelope(poolDetail(23, 'Fixture pool A'));
    assert.ok(url.endsWith('/24'), `Unexpected fixture GET: ${url}`);
    return response({detail: 'Fixture pool B detail unavailable'}, 500);
  });
  await m.state.editModelPool({id: 23});
  await m.state.editModelPool({id: 24});
}

test('models R1: failed B selection never makes the retained A form writable', async () => {
  const m = mount('dashboard_settings_models.html');
  await loadPoolAThenFailB(m);
  assert.equal(m.state.poolForm.id, 23, 'Retain the old snapshot without pretending it is B');
  assert.equal(m.state.poolLoading.value, false, 'Failure is not indefinite loading');
  assert.equal(m.document.activeElement?.id, 'pool-editor-title');
  assert.equal(poolEditorDisabled(m), true, 'Failed B selection must not re-enable A');
  assert.match(m.state.poolLoadError?.value || '', /Fixture pool B detail unavailable/);
  assert.equal(m.state.selectedPoolId.value, 24);
  assert.ok(m.calls.every(call => call.method === 'GET'));
});

test('models R1: the save entry point rejects the stale A snapshot after B fails', async () => {
  const m = mount('dashboard_settings_models.html');
  await loadPoolAThenFailB(m);
  const before = m.calls.length;
  await m.state.saveModelPool();
  assert.equal(m.calls.length, before, 'A disabled/stale editor must not send even a fake mutation');
  assert.equal(m.state.poolForm.id, 23);
  assert.equal(m.state.poolSaving.value, false);
});

test('models R1: retry reads selected B only and unlocks B after a successful detail GET', async () => {
  const m = mount('dashboard_settings_models.html');
  await loadPoolAThenFailB(m);
  assert.equal(typeof m.state.retryModelPool, 'function', 'Expose a selected-pool GET retry');
  const before = m.calls.length, detail = deferred();
  m.replyWith(url => {
    if (url.includes('/24/attempts?')) return envelope([]);
    assert.ok(url.endsWith('/24'), 'Retry must not load the retained A snapshot');
    return detail.promise;
  });
  const retrying = m.state.retryModelPool();
  try {
    assert.equal(poolEditorDisabled(m), true);
    assert.equal(m.state.poolLoading.value, true);
  } finally { detail.resolve(envelope(poolDetail(24, 'Fixture pool B'))); await retrying; }
  assert.equal(m.state.poolForm.id, 24);
  assert.equal(m.state.selectedPoolId.value, 24);
  assert.equal(m.state.poolLoadError.value, '');
  assert.equal(poolEditorDisabled(m), false);
  assert.deepEqual(m.calls.slice(before).map(call => [call.method, call.url]), [
    ['GET', '/api/dashboard/ai/model-pools/24'],
    ['GET', '/api/dashboard/ai/model-pools/24/attempts?limit=20'],
  ]);
});

test('models R1: explicit new-form reset cancels a retry without restoring stale A or B', async () => {
  const m = mount('dashboard_settings_models.html');
  await loadPoolAThenFailB(m);
  assert.equal(typeof m.state.retryModelPool, 'function');
  const detail = deferred();
  m.replyWith(url => url.includes('/attempts?') ? envelope([]) : detail.promise);
  const retrying = m.state.retryModelPool();
  try {
    await flush();
    assert.equal(m.state.poolLoading.value, true);
    assert.equal(m.state.poolLoadError.value, '');
    assert.equal(poolEditorDisabled(m), true, 'Cancel must not re-enable the retained snapshot');
    clickReachablePoolReset(m);
    await flush();
  } finally { detail.resolve(envelope(poolDetail(24))); await retrying; }
  assert.equal(m.state.poolForm.id, 0);
  assert.equal(m.state.poolForm.name, '');
  assert.equal(m.state.selectedPoolId.value, 0);
  assert.equal(m.state.poolLoadError.value, '');
  assert.equal(poolEditorDisabled(m), false);
  assert.equal(m.document.activeElement?.id, 'pool-name');
  assert.ok(m.calls.every(call => call.method === 'GET'));
  assert.equal(m.calls.filter(call => call.url.includes('/24/attempts?')).length, 0);
});

test('models R3: a normal pending pool selection has a reachable cancel/new control', async () => {
  const m = mount('dashboard_settings_models.html');
  m.replyWith(url => url.includes('/attempts?') ? envelope([]) : envelope(poolDetail(23)));
  await m.state.editModelPool({id: 23});
  const detail = deferred();
  m.replyWith(url => url.includes('/attempts?') ? envelope([]) : detail.promise);
  const selecting = m.state.editModelPool({id: 24});
  try {
    await flush();
    assert.equal(m.state.poolLoading.value, true);
    assert.equal(m.state.poolLoadError.value, '');
    assert.equal(m.state.poolForm.id, 23);
    assert.equal(poolEditorDisabled(m), true);
    clickReachablePoolReset(m);
    await flush();
  } finally { detail.resolve(envelope(poolDetail(24))); await selecting; }
  assert.equal(m.state.poolForm.id, 0);
  assert.equal(m.state.poolForm.name, '');
  assert.equal(m.state.selectedPoolId.value, 0);
  assert.equal(poolEditorDisabled(m), false);
  assert.equal(m.document.activeElement?.id, 'pool-name');
  assert.ok(m.calls.every(call => call.method === 'GET'));
  assert.equal(m.calls.filter(call => call.url.includes('/24/attempts?')).length, 0);
});

test('models: pool edit locates immediately, then focuses the loaded name before slow attempts', async () => {
  const m = mount('dashboard_settings_models.html');
  const detail = deferred(), attempts = deferred();
  m.replyWith(url => url.includes('/attempts?') ? attempts.promise : detail.promise);
  const edit = m.state.editModelPool({id: 23});
  await flush();
  try {
    assert.equal(m.document.activeElement?.id, 'pool-editor-title');
    assert.ok(m.domCalls.some(call => call.id === 'pool-editor' && call.method === 'scrollIntoView'));
    assert.equal(m.state.poolLoading?.value, true);
    detail.resolve(envelope(poolDetail()));
    await flush();
    assert.equal(m.state.poolForm.id, 23);
    assert.equal(m.document.activeElement?.id, 'pool-name');
    assert.equal(m.state.poolLoading.value, false);
    assert.equal(m.state.poolAttemptsLoading.value, true);
  } finally {
    detail.resolve(envelope(poolDetail())); attempts.resolve(envelope([])); await edit;
  }
  assert.ok(m.calls.every(call => call.method === 'GET'));
});

test('models: a late pool detail cannot replace a more recently selected editor', async () => {
  const m = mount('dashboard_settings_models.html');
  const old = deferred();
  m.replyWith(url => {
    if (url.includes('/attempts?')) return envelope([]);
    return url.endsWith('/23') ? old.promise : envelope(poolDetail(24, 'New selection'));
  });
  const first = m.state.editModelPool({id: 23});
  await m.state.editModelPool({id: 24});
  old.resolve(envelope(poolDetail(23, 'Old selection'))); await first;
  assert.equal(m.state.poolForm.id, 24);
  assert.equal(m.state.poolForm.name, 'New selection');
  assert.equal(m.calls.filter(call => call.url.includes('/23/attempts')).length, 0);
});

test('models: late attempts from the previous pool cannot replace the new pool history', async () => {
  const m = mount('dashboard_settings_models.html');
  const oldAttempts = deferred();
  m.replyWith(url => {
    if (url.includes('/23/attempts?')) return oldAttempts.promise;
    if (url.includes('/24/attempts?')) return envelope([{job_id: 'new-fixture-job'}]);
    return envelope(poolDetail(url.endsWith('/23') ? 23 : 24));
  });
  const first = m.state.editModelPool({id: 23});
  await flush();
  await m.state.editModelPool({id: 24});
  oldAttempts.resolve(envelope([{job_id: 'old-fixture-job'}])); await first;
  assert.equal(m.state.poolForm.id, 24);
  assert.equal(m.state.poolAttempts.value[0].job_id, 'new-fixture-job');
});

test('models: a delayed pool load does not steal focus after the user chooses another form', async () => {
  const m = mount('dashboard_settings_models.html');
  const detail = deferred();
  m.replyWith(url => url.includes('/attempts?') ? envelope([]) : detail.promise);
  const pool = m.state.editModelPool({id: 23});
  await flush();
  m.state.editProvider({id: 17, name: 'Chosen Provider'});
  await flush();
  detail.resolve(envelope(poolDetail())); await pool; await flush();
  assert.equal(m.document.activeElement?.id, 'provider-name');
});

test('models: save failure is attached to the provider form and survives four seconds', async () => {
  const m = mount('dashboard_settings_models.html');
  m.state.providerForm.name = 'Fixture only';
  m.replyWith(() => response({detail: 'Fixture validation error'}, 400));
  await m.state.saveProvider();
  assert.match(m.state.aiMessages?.['provider-form']?.text || '', /Fixture validation error/);
  assert.equal(m.state.aiMessages['provider-form'].type, 'error');
  m.advance(5000);
  assert.match(m.state.aiMessages['provider-form'].text, /Fixture validation error/);
  assert.equal(m.calls[0].via, 'csrfFetch');
  assert.equal(m.calls[0].method, 'POST');
});

test('models: an earlier success timer cannot erase a later save error', async () => {
  const m = mount('dashboard_settings_models.html');
  m.replyWith((_url, options) => envelope(options.method ? {id: 17, warnings: []} : []));
  await m.state.saveProvider();
  m.replyWith(() => response({detail: 'Later fixture error'}, 400));
  await m.state.saveProvider();
  m.advance(5000);
  assert.match(m.state.aiMessages?.['provider-form']?.text || '', /Later fixture error/);
});

test('models: catalog read errors remain near the affected Provider, not only the header', async () => {
  const m = mount('dashboard_settings_models.html');
  m.replyWith(() => response({detail: 'Fixture catalog unavailable'}, 500));
  await m.state.loadProviderModels(17);
  assert.match(m.state.aiMessages?.['provider-17']?.text || '', /Fixture catalog unavailable/);
  m.advance(5000);
  assert.equal(m.state.aiMessages['provider-17'].type, 'error');
});

test('models: successful pool save preserves version/member protocol and reports at the form', async () => {
  const m = mount('dashboard_settings_models.html');
  Object.assign(m.state.poolForm, poolDetail(), {original_enabled: false, members: [
    {provider_model_id: 32, enabled: true}, {provider_model_id: 31, enabled: false},
  ]});
  m.replyWith((url, options) => {
    if (options.method === 'PUT') return envelope({version: url.endsWith('/members') ? 4 : 3});
    if (url.endsWith('/model-pools')) return envelope([poolDetail()]);
    if (url.includes('/attempts?')) return envelope([]);
    return envelope({...poolDetail(), version: 4});
  });
  await m.state.saveModelPool();
  assert.equal(m.state.poolError.value, '');
  const writes = m.calls.filter(call => call.method === 'PUT');
  assert.equal(writes.length, 2);
  assert.ok(writes.every(call => call.via === 'csrfFetch'));
  assert.equal(JSON.parse(writes[0].options.body).expected_version, 2);
  assert.deepEqual(JSON.parse(writes[1].options.body), {expected_version: 3, members: [
    {provider_model_id: 32, enabled: true}, {provider_model_id: 31, enabled: false},
  ]});
  assert.match(m.state.aiMessages?.['pool-form']?.text || '', /模型池已保存/);
});

test('models: pool conflict remains an explicit form error, not a false success', async () => {
  const m = mount('dashboard_settings_models.html');
  Object.assign(m.state.poolForm, poolDetail(), {original_enabled: false});
  m.replyWith((url, options) => {
    if (options.method) return response({detail: 'Fixture conflict'}, 409);
    if (url.endsWith('/model-pools')) return envelope([poolDetail()]);
    if (url.includes('/attempts?')) return envelope([]);
    return envelope(poolDetail());
  });
  await m.state.saveModelPool();
  m.advance(5000);
  assert.match(m.state.poolError.value, /版本冲突/);
  assert.equal(m.calls.filter(call => call.method !== 'GET').length, 1);
  assert.notEqual(m.state.aiMessages?.['pool-form']?.type, 'success');
});

async function saveFixturePool(m) {
  Object.assign(m.state.poolForm, poolDetail(), {original_enabled: false});
  m.replyWith((url, options) => {
    if (options.method === 'PUT') return envelope({version: url.endsWith('/members') ? 4 : 3});
    if (url.endsWith('/model-pools')) return envelope([poolDetail()]);
    if (url.includes('/attempts?')) return envelope([]);
    return envelope({...poolDetail(), version: 4});
  });
  await m.state.saveModelPool();
  assert.equal(m.state.poolError.value, '');
  assert.equal(m.state.aiMessages['pool-form'].type, 'success');
}

for (const status of [400, 409]) {
  test(`models R2: a prior successful save cannot survive the next HTTP ${status} save failure`, async () => {
    const m = mount('dashboard_settings_models.html');
    await saveFixturePool(m);
    m.advance(5000);
    m.state.poolForm.name = 'Changed but not saved';
    const before = m.calls.length;
    m.replyWith((url, options) => {
      if (options.method) return response({detail: 'Fixture later save rejected'}, status);
      if (url.endsWith('/model-pools')) return envelope([poolDetail()]);
      if (url.includes('/attempts?')) return envelope([]);
      return envelope({...poolDetail(), version: 5});
    });
    await m.state.saveModelPool();
    assert.match(m.state.poolError.value, status === 409 ? /版本冲突/ : /Fixture later save rejected/);
    assert.notEqual(m.state.aiMessages['pool-form']?.type, 'success', 'Current failure must invalidate earlier success');
    m.advance(5000);
    assert.ok(m.state.poolError.value, 'The current error remains persistent');
    assert.equal(m.calls.slice(before).filter(call => call.method !== 'GET').length, 1);
    assert.ok(m.calls.slice(before).every(call => call.via === 'csrfFetch'));
  });
}

test('models R2: starting a new save clears success before waiting for its response', async () => {
  const m = mount('dashboard_settings_models.html');
  await saveFixturePool(m);
  const pending = deferred();
  m.replyWith(() => pending.promise);
  m.state.poolForm.name = 'Fixture unsaved edit';
  const saving = m.state.saveModelPool();
  try {
    assert.equal(m.state.poolSaving.value, true);
    assert.equal(m.state.aiMessages['pool-form'], undefined, 'Prior success is not current save progress');
  } finally { pending.resolve(response({detail: 'Fixture later failure'}, 400)); await saving; }
  assert.match(m.state.poolError.value, /Fixture later failure/);
});

async function loadedPoolWriteFixture() {
  const m = mount('dashboard_settings_models.html');
  const provider = {id: 17, name: 'Independent fixture Provider', provider_type: 'openai_compatible'};
  const poolA = {...poolDetail(23, 'Fixture writing pool A'), members: [
    {provider_model_id: 31, provider_id: 17, model_key: 'fixture-first', enabled: true},
    {provider_model_id: 32, provider_id: 17, model_key: 'fixture-second', enabled: true},
  ]};
  const poolB = {...poolDetail(24, 'Fixture pool B'), members: []};
  m.state.providers.value = [provider];
  m.state.modelPools.value = [poolA, poolB];
  m.replyWith(url => url.includes('/attempts?') ? envelope([]) : envelope(poolA));
  await m.state.editModelPool({id: 23});
  return {m, provider, poolA, poolB};
}

async function heldPoolSave(heldPhase) {
  const fixture = await loadedPoolWriteFixture();
  const {m, poolA, poolB} = fixture;
  const creating = heldPhase.startsWith('create');
  const savedName = creating ? 'Fixture created pool' : poolA.name;
  if (creating) {
    m.state.resetPoolForm();
    Object.assign(m.state.poolForm, {name: savedName, members: plain(poolA.members)});
  }
  m.state.poolForm.enabled = true; // Exercise the existing final enable PUT too.
  await flush();
  const writeStart = m.calls.length, gate = deferred(), reached = deferred();
  let heldResponse, version = 2, membersWritten = false;
  m.replyWith((url, options) => {
    const method = options.method || 'GET';
    let phase, result;
    if (method === 'POST' && url.endsWith('/model-pools')) {
      phase = 'create'; result = envelope({id: 23});
    } else if (method === 'PUT' && url.endsWith('/members')) {
      membersWritten = true;
      phase = 'members'; result = envelope({version: ++version});
    } else if (method === 'PUT') {
      const body = JSON.parse(options.body);
      phase = Object.hasOwn(body, 'name') ? 'metadata' : 'enable';
      result = envelope({version: ++version});
    } else if (method === 'DELETE') {
      phase = 'unexpected-delete'; result = envelope({});
    } else if (url.endsWith('/model-pools')) {
      phase = 'list'; result = envelope([{...poolA, name: savedName, version, enabled: true}, poolB]);
    } else if (url.includes('/23/attempts?')) {
      phase = 'history'; result = envelope([]);
    } else if (url.endsWith('/23')) {
      phase = creating && !membersWritten ? 'create-detail' : 'detail';
      result = envelope({...poolA, name: savedName, version, enabled: true});
    } else if (url.endsWith('/24')) {
      phase = 'unexpected-selection'; result = envelope(poolB);
    } else if (url.includes('/24/attempts?')) {
      phase = 'unexpected-history'; result = envelope([]);
    } else {
      throw new Error('Unexpected fixture path: ' + url);
    }
    if (phase === heldPhase) {
      heldResponse = result;
      reached.resolve();
      return gate.promise;
    }
    return result;
  });
  const saving = m.state.saveModelPool();
  return {...fixture, saving, creating, savedName, writeStart,
    entered: Promise.race([reached.promise, saving.then(() => {
      throw new Error('Save settled before held phase: ' + heldPhase);
    })]),
    release(result) { gate.resolve(result === undefined ? heldResponse : result); },
  };
}

const poolMutationControl = attrs =>
  /^(resetPoolForm|editModelPool|retryModelPool|deleteModelPool|saveModelPool|addPoolMember|removePoolMember|movePoolMember)(?:\(|$)/
    .test(attrs['@click'] || '')
  || Object.entries(attrs).some(([key, value]) => key.startsWith('v-model') && /^(poolForm|member)\./.test(value));

function assertPoolWriteControlsLocked(fixture) {
  const {m, poolA, poolB, provider} = fixture;
  const states = templateControlStates(m, poolMutationControl, {
    pool: poolB, provider, index: 0, member: poolA.members[0],
    model: {id: 33, provider_id: 17, model_key: 'fixture-third'},
  });
  assert.ok(states.some(control => control.id === 'pool-name' && control.visible));
  assert.ok(states.some(control => control.expression === 'editModelPool(pool)' && control.visible));
  assert.ok(states.some(control => control.expression === 'deleteModelPool(pool)' && control.visible));
  assert.deepEqual(states.filter(control => control.visible && !control.disabled)
    .map(control => control.id || control.expression || control.model), [],
    'No pool field, member action, selection, new/reset, retry, save or delete may escape write ownership');
}

for (const phase of ['metadata', 'members', 'enable', 'list', 'detail', 'history', 'create', 'create-detail']) {
  test('models R4: write owns actual controls through ' + phase + ' without blocking Provider edit', async () => {
    const h = await heldPoolSave(phase);
    try {
      await h.entered;
      assert.equal(h.m.state.poolSaving.value, true);
      assertPoolWriteControlsLocked(h);
      clickAvailableTemplateAction(h.m, 'editProvider(provider)', {provider: h.provider});
      await flush();
      assert.equal(h.m.state.providerForm.id, 17);
      assert.equal(h.m.document.activeElement?.id, 'provider-name');
    } finally { h.release(); await h.saving; }
    await flush();
    assert.equal(h.m.state.poolSaving.value, false);
    assert.equal(poolEditorDisabled(h.m), false);
    assert.equal(h.m.state.poolForm.id, 23);
    assert.equal(h.m.state.poolForm.name, h.savedName);
    assert.equal(h.m.document.activeElement?.id, 'provider-name', 'Internal read-back must not steal focus');
    assert.equal(h.m.state.aiMessages['pool-form'].type, 'success');
    assert.deepEqual(h.m.calls.slice(h.writeStart).filter(call => call.method !== 'GET').map(call => call.method),
      h.creating ? ['POST', 'PUT', 'PUT'] : ['PUT', 'PUT', 'PUT']);
    clickAvailableTemplateAction(h.m, 'resetPoolForm');
    h.m.state.poolForm.name = 'Synthetic UNSAVED new draft';
    await flush();
    assert.equal(h.m.state.poolForm.id, 0);
    assert.equal(h.m.state.aiMessages['pool-form'], undefined, 'A new draft never inherits the completed write result');
  });
}

const guardedPoolEntries = [
  ['cancel/new', 'detail', m => m.state.resetPoolForm(true)],
  ['ordinary new', 'metadata', m => m.state.resetPoolForm()],
  ['select another pool', 'members', m => m.state.editModelPool({id: 24})],
  ['selection with locate=false', 'detail', m => m.state.editModelPool({id: 24}, false)],
  ['retry', 'metadata', m => m.state.retryModelPool()],
  ['add member', 'metadata', m => m.state.addPoolMember({id: 33, provider_id: 17, model_key: 'fixture-third'})],
  ['remove member', 'members', m => m.state.removePoolMember(0)],
  ['move member', 'enable', m => m.state.movePoolMember(0, 1)],
  ['duplicate save', 'metadata', m => m.state.saveModelPool()],
  ['delete current pool', 'detail', m => m.state.deleteModelPool({id: 23, name: 'Fixture writing pool A'})],
];
for (const [entry, phase, invoke] of guardedPoolEntries) {
  test('models R4: handler entry ' + entry + ' cannot change an owned snapshot', async () => {
    const h = await heldPoolSave(phase);
    let attempted, confirmations = 0;
    h.m.window.confirm = () => { confirmations++; return true; };
    try {
      await h.entered;
      const before = plain(h.m.state.poolForm), selected = h.m.state.selectedPoolId.value;
      const callCount = h.m.calls.length;
      attempted = invoke(h.m);
      await flush();
      assert.deepEqual(plain(h.m.state.poolForm), before, 'The write-owned form snapshot must not change');
      assert.equal(h.m.state.selectedPoolId.value, selected);
      assert.equal(h.m.calls.length, callCount, 'Blocked entry must issue no request');
      assert.equal(confirmations, 0, 'Blocked delete must not even open a confirmation');
    } finally { h.release(); await Promise.all([h.saving, attempted]); }
    assert.equal(h.m.state.poolSaving.value, false);
  });
}

test('models R4: internal write helpers and ownership state are not exposed as page actions', () => {
  const m = mount('dashboard_settings_models.html');
  for (const name of ['replacePoolMembers', 'readPoolEditor', 'resetPoolEditor', 'poolWriteOperation']) {
    assert.equal(m.state[name], undefined, name + ' is private to the owning operation');
  }
});

for (const status of [400, 409, 500]) {
  test('models R4: HTTP ' + status + ' releases ownership only after its failure handling settles', async () => {
    const h = await heldPoolSave('metadata');
    try {
      await h.entered;
      assertPoolWriteControlsLocked(h);
    } finally { h.release(response({detail: 'Fixture write rejected'}, status)); await h.saving; }
    assert.equal(h.m.state.poolSaving.value, false);
    assert.ok(h.m.state.poolError.value);
    assert.notEqual(h.m.state.aiMessages['pool-form']?.type, 'success');
    clickAvailableTemplateAction(h.m, 'resetPoolForm');
    assert.equal(h.m.state.poolForm.id, 0);
  });
}

test('models R4: failed automatic read-back releases recovery without leaving an active old save', async () => {
  const h = await heldPoolSave('detail');
  try {
    await h.entered;
    assertPoolWriteControlsLocked(h);
  } finally { h.release(response({detail: 'Fixture read-back unavailable'}, 500)); await h.saving; }
  assert.equal(h.m.state.poolSaving.value, false);
  assert.match(h.m.state.poolLoadError.value, /Fixture read-back unavailable/);
  clickReachablePoolReset(h.m);
  h.m.state.poolForm.name = 'UNSAVED fixture after settled write';
  await flush();
  assert.equal(h.m.state.poolForm.id, 0);
  assert.equal(h.m.state.aiMessages['pool-form'], undefined);
});

async function heldPoolDelete(heldPhase) {
  const fixture = await loadedPoolWriteFixture();
  const {m, poolA, poolB} = fixture;
  const reached = deferred(), gate = deferred();
  let heldResponse;
  m.window.confirm = () => true;
  m.replyWith((url, options) => {
    let phase, result;
    if (options.method === 'DELETE') {
      phase = 'delete'; result = envelope({});
    } else if (url.endsWith('/model-pools')) {
      phase = 'delete-list'; result = envelope([poolB]);
    } else if (url.includes('/attempts?')) {
      result = envelope([]);
    } else if (url.endsWith('/24')) {
      result = envelope(poolB);
    } else {
      throw new Error('Unexpected delete fixture path: ' + url);
    }
    if (phase === heldPhase) { heldResponse = result; reached.resolve(); return gate.promise; }
    return result;
  });
  const deleting = m.state.deleteModelPool(poolA);
  return {...fixture, deleting,
    entered: Promise.race([reached.promise, deleting.then(() => {
      throw new Error('Delete settled before held phase: ' + heldPhase);
    })]),
    release(result) { gate.resolve(result === undefined ? heldResponse : result); },
  };
}

for (const phase of ['delete', 'delete-list']) {
  test('models R4: ' + phase + ' also owns pool editing and preserves Provider focus', async () => {
    const h = await heldPoolDelete(phase);
    try {
      await h.entered;
      assertPoolWriteControlsLocked(h);
      clickAvailableTemplateAction(h.m, 'editProvider(provider)', {provider: h.provider});
      await flush();
      const before = plain(h.m.state.poolForm), calls = h.m.calls.length;
      h.m.state.resetPoolForm(true);
      await h.m.state.editModelPool({id: 24});
      await h.m.state.saveModelPool();
      assert.deepEqual(plain(h.m.state.poolForm), before);
      assert.equal(h.m.calls.length, calls);
    } finally { h.release(); await h.deleting; }
    assert.equal(h.m.state.poolForm.id, 0, 'Owning delete can perform its private editor reset');
    assert.equal(poolEditorDisabled(h.m), false);
    assert.equal(h.m.document.activeElement?.id, 'provider-name');
    assert.equal(h.m.state.aiMessages['pool-list'].type, 'success');
    await clickAvailableTemplateAction(h.m, 'editModelPool(pool)', {pool: h.poolB});
    assert.equal(h.m.state.poolForm.id, 24, 'User selection becomes available after write settlement');
  });
}

test('models R4: a failed delete releases the pool editor and preserves its error', async () => {
  const h = await heldPoolDelete('delete');
  try {
    await h.entered;
    assertPoolWriteControlsLocked(h);
  } finally { h.release(response({detail: 'Fixture delete rejected'}, 500)); await h.deleting; }
  assert.equal(h.m.state.poolForm.id, 23);
  assert.equal(poolEditorDisabled(h.m), false);
  assert.equal(h.m.state.aiMessages['pool-list'].type, 'error');
  await clickAvailableTemplateAction(h.m, 'editModelPool(pool)', {pool: h.poolB});
  assert.equal(h.m.state.poolForm.id, 24);
});

test('models R4: a pending pure selection cannot race a delete, but remains cancellable', async () => {
  const h = await loadedPoolWriteFixture();
  const detail = deferred();
  h.m.window.confirm = () => true;
  h.m.replyWith((url, options) => {
    if (options.method) return envelope({});
    if (url.endsWith('/model-pools')) return envelope([h.poolB]);
    if (url.includes('/attempts?')) return envelope([]);
    return detail.promise;
  });
  const selecting = h.m.state.editModelPool({id: 24});
  try {
    await flush();
    const deleting = templateControlStates(h.m, attrs => attrs['@click'] === 'deleteModelPool(pool)', {pool: h.poolA});
    assert.ok(deleting.every(control => !control.visible || control.disabled));
    const calls = h.m.calls.length;
    await h.m.state.deleteModelPool(h.poolA);
    assert.equal(h.m.calls.length, calls);
    clickReachablePoolReset(h.m);
  } finally { detail.resolve(envelope(h.poolB)); await selecting; }
  assert.equal(h.m.state.poolForm.id, 0);
  assert.ok(h.m.calls.every(call => call.method === 'GET'));
});

test('agents: long fixture names survive selection and preview remains a read-only GET', async () => {
  const m = mount('dashboard_settings_agents.html');
  const longName = 'Very-long-synthetic-agent-'.repeat(30);
  m.state.agents.value = [{id: 77, name: longName, task_type: 'general'}];
  m.state.candidateAgentId.value = 77;
  m.replyWith(url => {
    assert.equal(url, '/api/dashboard/ai/agents/77/candidates');
    return envelope({agent_id: 77, agent_name: longName, pool_name: '', candidates: [], limits: {
      max_candidate_attempts: 16, max_network_requests: 32, max_resolved_candidates: 64, max_pool_nodes: 8,
    }});
  });
  await m.state.loadCandidateChain(77);
  assert.equal(m.state.filteredAgents.value[0].name, longName);
  assert.equal(m.state.candidateChain.value.agent_name, longName);
  assert.equal(m.state.candidateChainError.value, '');
  assert.equal(m.calls.length, 1);
  assert.equal(m.calls[0].method, 'GET');
});

function batchFixture() {
  const m = mount('dashboard_settings_agents.html');
  m.state.agents.value = [1, 2].map(id => ({id, name: `Fixture agent ${id}`, binding_summary: 'Fixture old binding'}));
  m.state.providers.value = [{id: 7, name: 'Fixture provider'}];
  m.state.selectedAgentIds.value = [1, 2];
  const binding = {binding_type: 'fixed', provider_id: 7, model: 'fixture-model', model_pool_id: 0};
  m.state.openBatchRebind(binding);
  return {m, binding};
}

function replyToBatch(m, write) {
  m.replyWith((url, options, request) => {
    if (url === '/api/dashboard/ai/agents/bindings' && request.method === 'PUT') return write(request);
    assert.equal(request.method, 'GET');
    if (url === '/api/dashboard/ai/agents') return envelope(plain(m.state.agents.value));
    assert.equal(url, '/api/dashboard/ai/health');
    return envelope({agents: []});
  });
}

// Run the actual shared modal controller with the actual caller's bindings.
// Only lifecycle scheduling and DOM boundaries are doubled. Inert/focus *calls*
// below are integration contracts, not rendered-browser accessibility evidence.
async function batchDialog(m) {
  const markup = m.html.match(/<app-modal\b[^>]*title="确认批量改绑"[\s\S]*?<\/app-modal>/)?.[0];
  assert.ok(markup);
  const scope = vm.createContext({});
  for (const [key, value] of Object.entries(m.state)) {
    const isRef = value && typeof value === 'object' && 'value' in value;
    Object.defineProperty(scope, key, {get: () => isRef ? value.value : value,
      set: next => { assert.ok(isRef, 'Only Vue refs are assigned by these page events'); value.value = next; }});
  }
  const evaluate = expression => vm.runInContext(expression, scope);
  const action = expression => { const result = evaluate(expression); return typeof result === 'function' ? result() : result; };
  const applyTag = markup.match(/<button\b[^>]*@click="batchRebind"[^>]*>/)[0];
  const closeExpression = markup.match(/@close="([^"]+)"/)[1];
  const cancelExpression = markup.match(/<button\b[^>]*@click="([^"]+)"[^>]*>取消<\/button>/)[1];
  const listeners = new Map(), mounted = [], unmounted = [], watchers = [], components = new Map();
  const document = {activeElement: null,
    addEventListener(type, fn) { listeners.set(type, fn); },
    removeEventListener(type) { listeners.delete(type); },
  };
  const element = parent => {
    const attrs = new Map(), styles = new Map();
    const node = {parent, inert: false, isConnected: true,
      style: {getPropertyValue: key => styles.get(key) || '', getPropertyPriority: () => '',
        setProperty: (key, value) => styles.set(key, value), removeProperty: key => styles.delete(key)},
      setAttribute: (key, value) => attrs.set(key, value), removeAttribute: key => attrs.delete(key),
      getAttribute: key => attrs.get(key), getClientRects: () => [{}], matches: () => false,
      querySelectorAll: () => [],
      closest() { return node.inert || attrs.get('aria-hidden') === 'true' ? node : parent?.closest(); },
      contains(other) { return other === node || Boolean(other?.parent && node.contains(other.parent)); },
      focus() { document.activeElement = node; },
    };
    return node;
  };
  document.body = element();
  document.documentElement = element();
  document.documentElement.clientWidth = 390;
  const appRoot = element(document.body), opener = element(appRoot), dialog = element(document.body);
  document.getElementById = id => id === 'app' ? appRoot : null;
  document.activeElement = opener;
  const Vue = {
    ref: value => ({value}), computed: getter => ({get value() { return getter(); }}),
    nextTick: fn => Promise.resolve().then(fn),
    onMounted: fn => mounted.push(fn), onBeforeUnmount: fn => unmounted.push(fn),
    watch(getter, fn) { watchers.push({getter, fn, value: getter()}); },
  };
  const context = vm.createContext({Vue, document, window: {...m.window, innerWidth: 390,
    getComputedStyle: () => ({visibility: 'visible', paddingRight: '0px'})}});
  vm.runInContext(source('vue_components.html').match(/<script>([\s\S]*?)<\/script>/)[1], context);
  context.registerGlobalComponents({component: (name, definition) => components.set(name, definition)});
  const isOpen = () => Boolean(evaluate(markup.match(/:is-open="([^"]+)"/)[1]));
  const props = {title: '确认批量改绑', isOpen: isOpen(), closeOnBackdrop: true};
  const state = components.get('app-modal').setup(props, {emit(event) {
    assert.equal(event, 'close'); action(closeExpression);
  }});
  state.dialog.value = dialog;
  for (const fn of mounted) fn();
  await flush();
  const sync = async () => {
    props.isOpen = isOpen();
    for (const watcher of watchers) {
      const value = watcher.getter();
      if (value !== watcher.value) { watcher.value = value; watcher.fn(value); }
    }
    await flush();
  };
  return {appRoot, dialog, document, opener, sync,
    apply: () => action(applyTag.match(/@click="([^"]+)"/)[1]),
    applyDisabled: () => Boolean(evaluate(applyTag.match(/:disabled="([^"]+)"/)?.[1] || 'false')),
    message(role) {
      const panel = markup.match(new RegExp(`<p\\b[^>]*role="${role}"[^>]*>([\\s\\S]*?)<\\/p>`));
      if (!props.isOpen || !panel || !evaluate(panel[0].match(/v-if="([^"]+)"/)[1])) return '';
      return panel[1].replace(/\{\{([\s\S]*?)\}\}/g, (_, expression) => String(evaluate(expression)));
    },
    async close(via = 'button') {
      if (via === 'cancel') action(cancelExpression);
      else if (via === 'backdrop') state.onBackdrop();
      else if (via === 'escape') listeners.get('keydown')?.({key: 'Escape', preventDefault() {}, stopPropagation() {}});
      else state.requestClose();
      await sync();
    },
    unmount() { for (const fn of unmounted) fn(); },
  };
}

for (const failure of ['http', 'network']) {
  test(`WB-1: ${failure} rejection persists inside the active shared Agent dialog and retries the same payload`, async () => {
    const {m, binding} = batchFixture(), preview = m.state.batchPreview.value;
    m.replyWith(() => response({ok: false, detail: 'Older header notice'}, 503));
    await m.state.loadModelPools(); // Start the existing four-second header timer.
    replyToBatch(m, async () => {
      if (failure === 'network') throw new Error('Fixture batch offline');
      return response({ok: false, detail: 'Fixture batch rejected'}, 400);
    });
    const dialog = await batchDialog(m);
    await dialog.apply();
    assert.equal(m.state.batchPreview.value, preview);
    assert.deepEqual(plain(m.state.selectedAgentIds.value), [1, 2]);
    assert.deepEqual(JSON.parse(m.calls.find(call => call.method === 'PUT').options.body), {agent_ids: [1, 2], binding});
    assert.match(dialog.message('alert'), /Fixture batch/, 'The failure must be bound inside the active modal, not the inert header');
    assert.equal(dialog.appRoot.inert, true);
    assert.equal(dialog.dialog.getAttribute('aria-modal'), 'true');
    m.advance(4001);
    assert.equal(m.state.aiMessage.value, '');
    assert.match(dialog.message('alert'), /Fixture batch/);
    const pending = deferred();
    replyToBatch(m, () => pending.promise);
    const retrying = dialog.apply();
    const during = {error: dialog.message('alert'), status: dialog.message('status'), disabled: dialog.applyDisabled()};
    pending.resolve(envelope({updated: 2}));
    await retrying;
    await dialog.sync();
    assert.equal(during.error, '', 'Only explicit retry clears the operation error');
    assert.match(during.status, /改绑/);
    assert.equal(during.disabled, true);
    const writes = m.calls.filter(call => call.method === 'PUT');
    assert.equal(writes.length, 2);
    assert.equal(writes[1].options.body, writes[0].options.body);
    assert.ok(writes.every(call => call.via === 'csrfFetch'));
    assert.equal(m.state.batchPreview.value, null);
    assert.equal(m.state.batchRebindBusy.value, false);
    assert.deepEqual(plain(m.state.selectedAgentIds.value), []);
    assert.equal(dialog.appRoot.inert, false);
    assert.equal(dialog.document.activeElement, dialog.opener);
  });
}

for (const via of ['cancel', 'button', 'escape', 'backdrop']) {
  test(`WB-1: shared dialog ${via} clears only confirmation/error, keeps selection, and reopens cleanly`, async () => {
    const {m, binding} = batchFixture();
    replyToBatch(m, () => response({ok: false, detail: 'Fixture batch rejected'}, 400));
    const dialog = await batchDialog(m);
    await dialog.apply();
    await dialog.close(via);
    assert.equal(m.state.batchPreview.value, null);
    assert.equal(m.state.batchRebindError?.value, '');
    assert.deepEqual(plain(m.state.selectedAgentIds.value), [1, 2]);
    assert.equal(dialog.appRoot.inert, false);
    m.state.openBatchRebind(binding);
    await dialog.sync();
    assert.ok(m.state.batchPreview.value);
    assert.equal(dialog.message('alert'), '');
    assert.equal(dialog.appRoot.inert, true);
    assert.equal(m.calls.length, 1, 'Close/reopen must not write or reload');
  });
}

test('WB-1: repeated apply and close/reopen cannot overlap an in-flight rebind', async () => {
  const {m, binding} = batchFixture(), pending = deferred();
  replyToBatch(m, () => pending.promise);
  const dialog = await batchDialog(m);
  const first = dialog.apply(), duplicate = dialog.apply();
  const disabled = dialog.applyDisabled();
  await dialog.close('cancel');
  m.state.openBatchRebind(binding);
  const whilePending = m.state.batchPreview.value;
  pending.resolve(response({ok: false, detail: 'Fixture late rejection'}, 400));
  await Promise.all([first, duplicate]);
  assert.equal(m.calls.length, 1, 'The handler must guard duplicate calls, not only disable the button');
  assert.equal(disabled, true);
  assert.equal(whilePending, null, 'Closing is not cancellation: keep write ownership until settlement');
  assert.equal(m.state.batchRebindBusy.value, false);
  assert.equal(m.state.batchRebindError.value, '');
  m.state.openBatchRebind(binding);
  await dialog.sync();
  assert.ok(m.state.batchPreview.value);
  assert.equal(dialog.message('alert'), '');
});

for (const outcome of ['success', 'http', 'network']) {
  test(`WB-1: a closed rebind's late ${outcome} cannot clear a newer selection or restore dialog feedback`, async () => {
    const {m, binding} = batchFixture(), pending = deferred();
    replyToBatch(m, () => pending.promise);
    const dialog = await batchDialog(m), saving = dialog.apply();
    await dialog.close('escape');
    m.state.selectedAgentIds.value = [2];
    if (outcome === 'network') pending.reject(new Error('Fixture abandoned write'));
    else pending.resolve(outcome === 'success' ? envelope({updated: 2}) : response({ok: false, detail: 'Fixture abandoned write'}, 400));
    await saving;
    assert.deepEqual(plain(m.state.selectedAgentIds.value), [2]);
    assert.equal(m.state.batchPreview.value, null);
    assert.equal(m.state.batchRebindError?.value, '');
    assert.equal(m.state.batchRebindBusy?.value, false);
    m.state.openBatchRebind({...binding, model: 'fixture-next-model'});
    await dialog.sync();
    assert.equal(m.state.batchPreview.value.binding.model, 'fixture-next-model');
    assert.equal(dialog.message('alert'), '');
    assert.equal(m.calls.filter(call => call.method === 'PUT').length, 1);
  });
}

for (const outcome of ['success', 'failure']) {
  test(`WB-1: unmount invalidates a pending rebind's late ${outcome} and prevents new batch actions`, async () => {
    const {m, binding} = batchFixture(), pending = deferred();
    replyToBatch(m, () => pending.promise);
    const saving = m.state.batchRebind();
    m.unmount();
    if (outcome === 'failure') pending.reject(new Error('Fixture disposed write'));
    else pending.resolve(envelope({updated: 2}));
    await saving;
    assert.equal(m.state.batchPreview.value, null);
    assert.equal(m.state.batchRebindError?.value, '');
    assert.equal(m.state.batchRebindBusy?.value, false);
    assert.equal(m.state.aiMessage.value, '');
    m.state.openBatchRebind(binding);
    await m.state.batchRebind();
    assert.equal(m.state.batchPreview.value, null);
    assert.equal(m.calls.length, 1, 'A disposed operation must not start reconciliation or another write');
  });
}

test('WB-1: a confirmation snapshots its selected IDs and binding rather than a later mutable form', async () => {
  const {m, binding} = batchFixture(), expected = {agent_ids: [1, 2], binding: {...binding}};
  binding.model = 'unconfirmed-model';
  m.state.selectedAgentIds.value = [2];
  replyToBatch(m, () => response({ok: false, detail: 'Fixture rejected snapshot'}, 400));
  await m.state.batchRebind();
  assert.deepEqual(JSON.parse(m.calls[0].options.body), expected);
  assert.equal(m.state.batchPreview.value.binding.model, expected.binding.model);
  assert.deepEqual(plain(m.state.selectedAgentIds.value), [2], 'Failure must not overwrite current selection');
});

test('WB-1: confirmed PUT with failed read-back is not a retryable write failure and retains its in-flight lock', async () => {
  const {m, binding} = batchFixture(), readback = deferred();
  m.replyWith((url, options, request) => request.method === 'PUT' ? envelope({updated: 2}) : readback.promise);
  const saving = m.state.batchRebind();
  await flush();
  const closed = m.state.batchPreview.value, busy = m.state.batchRebindBusy?.value;
  m.state.openBatchRebind(binding);
  const attemptedReopen = m.state.batchPreview.value;
  readback.reject(new Error('Fixture read-back offline'));
  await saving;
  assert.equal(closed, null);
  assert.equal(busy, true);
  assert.equal(attemptedReopen, null);
  assert.equal(m.state.batchRebindError?.value, '');
  assert.equal(m.state.batchRebindBusy.value, false);
  assert.match(m.state.aiMessage.value, /已改绑.*刷新.*Fixture read-back offline/);
  assert.equal(m.state.aiMessageType.value, 'warning');
  await m.state.batchRebind();
  assert.equal(m.calls.filter(call => call.method === 'PUT').length, 1);
});

function tokenModal(m) { return m.html.match(/<app-modal\b[^>]*title="救援 API Token"[\s\S]*?<\/app-modal>/)?.[0] || ''; }

test('token: clipboard rejection is visible inside the modal and selects a manual fallback', async () => {
  const m = mount('dashboard_settings_system.html');
  m.state.rescueTokenPlaintext.value = FAKE_TOKEN;
  m.navigator.clipboard.writeText = async () => { throw new Error('Fixture clipboard denied'); };
  await m.state.copyRescueToken();
  assert.match(m.state.rescueTokenCopyMessage?.value || '', /复制失败/);
  assert.equal(m.state.rescueTokenCopyType.value, 'error');
  assert.match(tokenModal(m), /\{\{ rescueTokenCopyMessage \}\}/);
  assert.equal(m.document.activeElement?.id, 'rescue-token-plaintext');
  assert.ok(m.domCalls.some(call => call.method === 'select' && call.id === 'rescue-token-plaintext'));
  assert.equal(m.state.rescueTokenPlaintext.value, FAKE_TOKEN);
  assert.equal(m.calls.length, 0);
  assert.equal(m.storageWrites.length, 0);
});

test('token: successful copying is reported inside the modal without clearing the token', async () => {
  const m = mount('dashboard_settings_system.html');
  m.state.rescueTokenPlaintext.value = FAKE_TOKEN;
  await m.state.copyRescueToken();
  assert.match(m.state.rescueTokenCopyMessage?.value || '', /已复制/);
  assert.equal(m.state.rescueTokenCopyType.value, 'success');
  assert.match(tokenModal(m), /\{\{ rescueTokenCopyMessage \}\}/);
  assert.deepEqual(m.clipboardWrites, [FAKE_TOKEN]);
  assert.equal(m.state.rescueTokenPlaintext.value, FAKE_TOKEN);
  assert.equal(m.calls.length, 0);
  assert.equal(m.storageWrites.length, 0);
});

test('token: missing Clipboard API still offers selection and manual-copy instructions', async () => {
  const m = mount('dashboard_settings_system.html');
  m.state.rescueTokenPlaintext.value = FAKE_TOKEN;
  m.navigator.clipboard = undefined;
  await m.state.copyRescueToken();
  assert.match(m.state.rescueTokenCopyMessage?.value || '', /手动复制/);
  assert.equal(typeof m.state.selectRescueToken, 'function');
  m.state.selectRescueToken();
  assert.equal(m.document.activeElement?.id, 'rescue-token-plaintext');
  assert.match(tokenModal(m), /@click="selectRescueToken"/);
  assert.match(tokenModal(m), /长按|Ctrl/);
  assert.equal(m.calls.length, 0);
});

test('token: explicit close clears plaintext and ignores late clipboard completion', async () => {
  const m = mount('dashboard_settings_system.html');
  const clipboard = deferred();
  m.state.rescueTokenPlaintext.value = FAKE_TOKEN;
  m.navigator.clipboard.writeText = () => clipboard.promise;
  const copy = m.state.copyRescueToken();
  try { assert.equal(m.state.copyingRescueToken?.value, true); }
  finally { m.state.closeRescueToken(); clipboard.resolve(); await copy; }
  assert.equal(m.state.rescueTokenPlaintext.value, '');
  assert.equal(m.state.rescueTokenCopyMessage.value, '');
  assert.equal(m.state.copyingRescueToken.value, false);
  assert.equal(m.calls.length, 0);
  assert.equal(m.storageWrites.length, 0);
});

test('token: repeated taps share one in-flight clipboard attempt', async () => {
  const m = mount('dashboard_settings_system.html');
  const clipboard = deferred();
  let copies = 0;
  m.state.rescueTokenPlaintext.value = FAKE_TOKEN;
  m.navigator.clipboard.writeText = () => { copies++; return clipboard.promise; };
  const first = m.state.copyRescueToken(), second = m.state.copyRescueToken();
  clipboard.resolve(); await Promise.all([first, second]);
  assert.equal(copies, 1);
  assert.equal(m.calls.length, 0);
});

test('token: fake issuance occurs once; copying/retrying never reissues or stores plaintext', async () => {
  const m = mount('dashboard_settings_system.html');
  m.window.confirm = () => true;
  m.replyWith((url, options) => {
    assert.equal(url, '/api/dashboard/rescue-token/rotate');
    assert.equal(options.method, 'POST');
    return envelope({token: FAKE_TOKEN, token_prefix: 'FAKE', rotated_at: '2000-01-01'});
  });
  await m.state.rotateRescueToken();
  await m.state.copyRescueToken(); await m.state.copyRescueToken();
  assert.equal(m.calls.length, 1);
  assert.equal(m.calls[0].via, 'csrfFetch');
  assert.deepEqual(m.clipboardWrites, [FAKE_TOKEN, FAKE_TOKEN]);
  m.state.closeRescueToken(); await m.state.copyRescueToken();
  assert.equal(m.calls.length, 1);
  assert.equal(m.state.rescueTokenPlaintext.value, '');
  assert.equal(m.storageWrites.length, 0);
});

test('token: unmount clears plaintext and a late rejected copy cannot restore feedback', async () => {
  const m = mount('dashboard_settings_system.html');
  const clipboard = deferred();
  m.state.rescueTokenPlaintext.value = FAKE_TOKEN;
  m.navigator.clipboard.writeText = () => clipboard.promise;
  const copying = m.state.copyRescueToken();
  m.unmount(); clipboard.reject(new Error('Fixture denied after unmount')); await copying;
  assert.equal(m.state.rescueTokenPlaintext.value, '');
  assert.equal(m.state.rescueTokenCopyMessage?.value, '');
  assert.equal(m.calls.length, 0);
});

test('token: unmount also ignores a late fake issuance response', async () => {
  const m = mount('dashboard_settings_system.html');
  const issuance = deferred();
  m.window.confirm = () => true;
  m.replyWith(() => issuance.promise);
  const rotating = m.state.rotateRescueToken();
  m.unmount();
  issuance.resolve(envelope({token: FAKE_TOKEN, token_prefix: 'FAKE', rotated_at: '2000-01-01'}));
  await rotating;
  assert.equal(m.state.rescueTokenPlaintext.value, '');
  assert.equal(m.calls.length, 1);
  assert.equal(m.storageWrites.length, 0);
});

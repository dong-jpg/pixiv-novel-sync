// Execute scripts and bindings from the real templates. Browser rendering/CUA is separate.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const root = path.resolve(__dirname, '../src/pixiv_novel_sync/templates');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const response = (data, ok = true) => ({ok, status: ok ? 200 : 500, json: async () => data});
const deferred = () => {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return {promise, resolve};
};
const value = ref => ref && ref.__v_isRef ? ref.value : ref;
const storage = map => ({
  getItem: key => map.get(key) ?? null,
  setItem: (key, data) => map.set(key, String(data)),
  removeItem: key => map.delete(key),
});

function environment(url = '/dashboard/novels', options = {}) {
  const parsed = new URL(url, 'https://library.invalid');
  const local = options.local || new Map(), session = options.session || new Map();
  const events = new Map(), ticks = [], mounted = [], unmounted = [], watchers = [];
  const calls = [], notices = [], scrolls = [], timers = new Map(), errors = [];
  let timerId = 0, state;
  const addEventListener = (name, fn) => {
    if (!events.has(name)) events.set(name, new Set());
    events.get(name).add(fn);
  };
  const removeEventListener = (name, fn) => events.get(name)?.delete(fn);
  const location = {pathname: parsed.pathname, search: parsed.search, hash: parsed.hash,
    origin: parsed.origin, href: ''};
  const history = {scrollRestoration: 'auto', replaceState(_state, _unused, href) {
    const next = new URL(href, location.origin);
    Object.assign(location, {pathname: next.pathname, search: next.search, hash: next.hash});
  }};
  const document = {documentElement: {scrollHeight: 3000}, title: '',
    referrer: 'https://library.invalid/dashboard/settings', addEventListener, removeEventListener};
  const window = {location, history, document, scrollY: 0, innerHeight: 600,
    addEventListener, removeEventListener,
    scrollTo({top}) { this.scrollY = top; scrolls.push(top); },
    sessionStorage: storage(session), localStorage: storage(local),
    toast: (...args) => notices.push(args), errorText: data => data.error || 'request failed',
    confirm: () => true,
    csrfFetch: async (url, opts) => { calls.push({url, options: opts}); return response({ok: true}); },
  };
  const fakeFetch = async url => {
    const target = new URL(url, location.origin);
    const page = Number(target.searchParams.get('page') || 1);
    if (target.pathname.endsWith('/progress')) return response({progress: 0});
    if (target.pathname === '/api/dashboard/novels/1') {
      return response({novel_id: 1, user_id: 9, series_id: 2, title: 'fixture book', text_raw: 'fixture only'});
    }
    if (target.pathname === '/api/dashboard/series/2') {
      return response({series_id: 2, user_id: 9, title: 'fixture series', total_novels: 3,
        novels: [1, 2, 3].map(novel_id => ({novel_id, title: `chapter ${novel_id}`}))});
    }
    if (target.pathname === '/api/dashboard/users/9') return response({user_id: 9, name: 'fixture author'});
    const data = {items: [{novel_id: 1, series_id: 2, user_id: 9, item_id: 1, item_type: 'novel',
      content_kind: 'standalone', title: 'fixture title', sources: [{kind: 'bookmark', label: 'fixture source'}]}],
    total: 90, page, total_pages: 9};
    return response(target.pathname === '/api/dashboard/rescues' ? {ok: true, data} : data);
  };
  const context = vm.createContext({
    window, document, location, history, localStorage: window.localStorage, sessionStorage: window.sessionStorage,
    URL, URLSearchParams, AbortController, console,
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; },
    clearTimeout: id => timers.delete(id),
    confirm: message => window.confirm(message),
    csrfFetch: (...args) => window.csrfFetch(...args),
    fetch: async (url, opts) => {
      calls.push({url, options: opts});
      return options.fetch ? options.fetch(url, opts, fakeFetch) : fakeFetch(url, opts);
    },
    initVueApp: app => { state = app.setup(); },
    Vue: {
      ref: initial => ({value: initial, __v_isRef: true}), reactive: initial => initial,
      computed: fn => ({get value() { return fn(); }, __v_isRef: true}),
      watch: (source, fn) => watchers.push({source, fn, previous: JSON.stringify(typeof source === 'function' ? source() : value(source))}),
      onMounted: fn => mounted.push(fn), onUnmounted: fn => unmounted.push(fn),
      nextTick: fn => { if (fn) ticks.push(fn); return Promise.resolve(); },
    },
  });
  // Keep the existing base query semantics real, not a copied test implementation.
  const base = read('base.html');
  vm.runInContext(base.slice(base.indexOf('window.readListQuery ='), base.indexOf('window.streamSSE =')), context);
  function execute(html, filename) {
    for (const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
      vm.runInContext(match[1], context, {filename});
    }
  }
  const helper = path.join(root, 'navigation_helpers.html');
  if (fs.existsSync(helper)) execute(read('navigation_helpers.html'), helper);
  const capture = result => { if (result?.catch) result.catch(error => errors.push(error)); };
  const api = {
    window, document, context, location, history, calls, notices, local, session, scrolls, events,
    get state() { return state; },
    currentPath: () => location.pathname + location.search,
    emit(name, event = {}) { for (const fn of [...(events.get(name) || [])]) fn(event); },
    count(name) { return events.get(name)?.size || 0; },
    mount(name) { this.html = read(name); execute(this.html, path.join(root, name)); return this; },
    async flush() {
      for (let turn = 0; turn < 30; turn++) {
        await Promise.resolve();
        for (const fn of ticks.splice(0)) capture(fn());
      }
      if (errors.length) throw errors.shift();
    },
    async start() { for (const fn of mounted) capture(fn()); await this.flush(); },
    async changed() {
      for (const watcher of watchers) {
        const current = typeof watcher.source === 'function' ? watcher.source() : value(watcher.source);
        if (JSON.stringify(current) !== watcher.previous) {
          watcher.previous = JSON.stringify(current);
          capture(watcher.fn(current));
        }
      }
      await this.flush();
    },
    async runTimers() {
      const callbacks = [...timers.values()];
      timers.clear();
      for (const fn of callbacks) capture(fn());
      await this.flush();
    },
    dispose() { for (const fn of unmounted) fn(); },
    evaluate(expression, locals = {}) {
      context.__bindings = new Proxy({...state, ...locals}, {
        get: (obj, key) => value(obj[key]),
        set: (obj, key, next) => {
          if (obj[key]?.__v_isRef) obj[key].value = next;
          else obj[key] = next;
          return true;
        },
      });
      return vm.runInContext(`(function(scope) { with (scope) { return (${expression}); } })(__bindings)`, context);
    },
    href(needle, locals = {}) {
      const binding = [...this.html.matchAll(/:href="([^"]+)"/g)].find(match => match[1].includes(needle));
      assert.ok(binding, `missing actual href binding: ${needle}`);
      return this.evaluate(binding[1], locals);
    },
  };
  return api;
}

function scrollState(env) {
  assert.equal(typeof env.window.createDashboardScrollState, 'function', 'missing explicit scroll lifecycle');
  return env.window.createDashboardScrollState();
}
function savedList(url, scrollY) {
  const env = environment(url), state = scrollState(env);
  state.restore();
  env.window.scrollY = scrollY;
  state.save();
  state.dispose();
  return env.session;
}

test('reader rejects external/protocol-relative return paths without referrer fallback', () => {
  for (const from of ['//evil.invalid/x', '/\\evil.invalid/x', '/outside', 'https://evil.invalid', '/dashboard/../logout']) {
    const m = environment('/dashboard/novels/1?from=' + encodeURIComponent(from)).mount('dashboard_novel_detail.html');
    assert.equal(value(m.state.backHref), '/dashboard/novels', from);
  }
  const direct = environment('/dashboard/novels/1').mount('dashboard_novel_detail.html');
  assert.equal(value(direct.state.backHref), '/dashboard/novels', 'no reliance on document.referrer');
});

test('shared return validator rejects raw, encoded and nested unsafe paths and unsafe fallbacks', () => {
  const m = environment();
  assert.equal(typeof m.window.safeDashboardReturn, 'function');
  const safe = m.window.safeDashboardReturn;
  for (const bad of [null, {}, '', ' dashboard/novels', '/dashboardish', '//evil.invalid/dashboard',
    'https://library.invalid/dashboard', 'javascript:alert(1)', '/dashboard\\evil', '/dashboard\n/novels',
    '/dashboard?x=\u0000', '/dashboard/../logout', '/dashboard/%2e%2e/logout', '/dashboard/%252e%252e/logout',
    '/%2f/evil.invalid', '/dashboard/%5cfoo', '/dashboard?x=%0d%0a', '/dashboard?x=%250a', '/dashboard?x=%7f',
    '/dashboard/%', '/dashboard//novels']) {
    assert.equal(safe(bad, '/dashboard/follows?page=2'), '/dashboard/follows?page=2', String(bad));
  }
  assert.equal(safe('//evil', '//other'), '/dashboard/novels');
  for (const good of ['/dashboard', '/dashboard/novels?category=rescue&page=4&search=%E4%B9%A6',
    '/dashboard/users/9?category=series&page=3', '/dashboard/series/2#chapters']) {
    assert.equal(safe(good, '/dashboard/follows'), good);
  }
});

test('return builder preserves target query/hash and full nested dashboard source', () => {
  const m = environment('/dashboard/follows?status=normal&page=4&q=fixture');
  assert.equal(typeof m.window.withDashboardReturn, 'function');
  const from = '/dashboard/users/9?category=series&page=3&from=' + encodeURIComponent(m.currentPath());
  const href = m.window.withDashboardReturn('/dashboard/series/2?view=chapters#list', from);
  const result = new URL(href, m.location.origin);
  assert.equal(result.searchParams.get('from'), from);
  assert.equal(result.searchParams.get('view'), 'chapters');
  assert.equal(result.hash, '#list');
  assert.equal(new URL(m.window.withDashboardReturn('//evil', '//evil'), m.location.origin).origin, m.location.origin);
  assert.equal(new URL(m.window.withDashboardReturn('/dashboard/novels/1'), m.location.origin).searchParams.get('from'), m.currentPath());
});

test('valid literal percent searches survive nested return validation without becoming malformed escapes', () => {
  const m = environment('/dashboard/novels?search=100%25&page=3');
  const source = m.currentPath();
  assert.equal(m.window.safeDashboardReturn(source), source);
  const author = m.window.withDashboardReturn('/dashboard/users/9?category=series&page=2', source);
  const series = m.window.withDashboardReturn('/dashboard/series/2', author);
  assert.equal(new URL(series, m.location.origin).searchParams.get('from'), author);
  assert.equal(m.window.safeDashboardReturn('/dashboard/novels?search=50%25%20%26%20%E4%B9%A6'), '/dashboard/novels?search=50%25%20%26%20%E4%B9%A6');
  assert.equal(m.window.safeDashboardReturn('/dashboard/novels?search=%25E4'), '/dashboard/novels?search=%25E4');
  assert.equal(m.window.safeDashboardReturn('/dashboard/novels?search=%25E4%255c'), '/dashboard/novels');
  assert.equal(m.window.safeDashboardReturn('/dashboard/novels?search=%25c2%2580'), '/dashboard/novels');
});

const returnRoles = ['value', 'fallback', 'target', 'from'];
function assertRejectedReturn(m, role, input) {
  const fallback = '/dashboard/follows?status=normal&page=3';
  if (role === 'value') assert.equal(m.window.safeDashboardReturn(input, fallback), fallback, input);
  if (role === 'fallback') assert.equal(m.window.safeDashboardReturn('/outside', input), '/dashboard/novels', input);
  if (role === 'target') {
    const url = new URL(m.window.withDashboardReturn(input, fallback), m.location.origin);
    assert.equal(url.pathname, '/dashboard/novels', input);
    assert.equal(url.searchParams.get('from'), fallback);
    assert.equal(url.searchParams.size, 1, 'a rejected target must not retain its query: ' + input);
    assert.equal(url.hash, '', 'a rejected target must not retain its hash: ' + input);
  }
  if (role === 'from') {
    const url = new URL(m.window.withDashboardReturn('/dashboard/series/2', input), m.location.origin);
    assert.equal(url.pathname, '/dashboard/series/2');
    assert.equal(url.searchParams.get('from'), '/dashboard/novels', input);
  }
}
function assertAcceptedReturn(m, input) {
  assert.equal(m.window.safeDashboardReturn(input, '/dashboard/follows'), input);
  assert.equal(m.window.safeDashboardReturn('/outside', input), input);
  const original = new URL(input, m.location.origin);
  const target = new URL(m.window.withDashboardReturn(input, '/dashboard/follows'), m.location.origin);
  assert.equal(target.pathname, original.pathname);
  assert.equal(target.hash, original.hash);
  for (const [key, expected] of original.searchParams) assert.equal(target.searchParams.get(key), expected, key);
  const from = new URL(m.window.withDashboardReturn('/dashboard/novels/1', input), m.location.origin);
  assert.equal(from.searchParams.get('from'), input);
}

for (const delimiter of ['%3f', '%23']) {
  for (const role of returnRoles) {
    test(`I1: encoded ${delimiter} cannot hide pathname traversal in ${role}`, () => {
      const m = environment();
      const exploit = `/dashboard/${delimiter}/%2e%2e/%2e%2e/outside`;
      assert.equal(new URL(exploit, m.location.origin).pathname, '/outside', 'real URL parser reproduces the escape');
      for (const input of [exploit, exploit + '?page=3#section',
        `/dashboard/${delimiter.toUpperCase()}/%2E%2E/%2e%2e/outside`,
        `/dashboard/${delimiter.replace('%', '%25')}/%252e%252e/%252e%252e/outside`]) {
        assertRejectedReturn(m, role, input);
      }
    });
  }
}

test('I1: original query/hash boundaries preserve legitimate percent, Unicode and literal delimiters in every role', () => {
  const m = environment();
  for (const input of [
    '/dashboard/novels?category=bookmark&page=2&page_size=10&search=%E9%9B%A8%E5%90%8E',
    '/dashboard/novels?search=%3F%23%25%20%E4%B9%A6%2F..%2F..&page=3#%E7%AB%A0%E8%8A%82',
    '/dashboard/series/章节%3F%23%25?view=chapters#part%3F%23',
    '/dashboard/novels?search=100%25%20%26%20%E4%B9%A6#part',
  ]) assertAcceptedReturn(m, input);
});

for (const role of returnRoles) {
  test(`I2: malformed nested UTF-8 cannot shield any valid C1 control in ${role}`, () => {
    const m = environment();
    // Start with the exact reviewed payload, then cover the entire UTF-8 C1 range.
    assertRejectedReturn(m, role, '/dashboard/novels?search=%25E4%25C2%2580');
    for (let codepoint = 0x80; codepoint <= 0x9f; codepoint++) {
      const control = encodeURIComponent(encodeURIComponent(String.fromCharCode(codepoint)));
      for (const input of [
        '/dashboard/novels?search=%25E4' + control,
        '/dashboard/%25FF' + control.toLowerCase() + '%25E4',
        '/dashboard/novels#%25E4' + control,
        '/dashboard/novels?search=' + encodeURIComponent('%25E4' + control),
      ]) assertRejectedReturn(m, role, input);
    }
  });
}

test('I2: malformed nested literals beside valid non-control Unicode remain valid in every role', () => {
  const m = environment();
  for (const input of [
    '/dashboard/novels?search=%25E4',
    '/dashboard/novels?search=%25E4%25C2%25A0',
    '/dashboard/novels?search=%25FF%25E4%25B9%25A6#%25E4',
    '/dashboard/novels?search=100%25%20%25E4%25F0%259F%2598%2580',
  ]) assertAcceptedReturn(m, input);
});

for (const [name, url, needle, item] of [
  ['dashboard_novels.html', '/dashboard/novels?category=bookmark&search=fixture&page=4', 'item.novel_id', {novel_id: 1}],
  ['dashboard_novels.html', '/dashboard/novels?category=following&page=3', 'item.series_id', {series_id: 2}],
  ['dashboard_novels.html', '/dashboard/novels?category=rescue&page=3', 'item.content_kind', {item_type: 'novel', item_id: 1, content_kind: 'standalone'}],
  ['dashboard_follows.html', '/dashboard/follows?status=normal&page=4&q=fixture', 'item.user_id', {user_id: 9}],
  ['dashboard_user_detail.html', '/dashboard/users/9?category=series&page=3', 'item.series_id', {series_id: 2}],
  ['dashboard_user_detail.html', '/dashboard/users/9?category=single&page=3', 'item.novel_id', {novel_id: 1}],
  ['dashboard_series_detail.html', '/dashboard/series/2?from=%2Fdashboard%2Fnovels%3Fcategory%3Dfollowing%26page%3D4', 'novel.novel_id', {novel_id: 1}],
]) {
  test(`${name}: ${needle} actual link carries the current list context`, async () => {
    const m = environment(url).mount(name);
    await m.start();
    const result = new URL(m.href(needle, {item, novel: item}), m.location.origin);
    assert.equal(result.searchParams.get('from'), m.currentPath());
    assert.equal(typeof m.state.rememberScroll, 'function');
  });
}

test('rescue query restores every filter/page size and keeps them in returned URL', async () => {
  const m = environment('/dashboard/novels?category=rescue&search=fixture&sort=updated_desc&page=4&page_size=40&state=partial&content_kind=series_chapter&source_kind=user_backup').mount('dashboard_novels.html');
  await m.start();
  assert.equal(m.state.pageSize.value, 40);
  assert.equal(m.state.rescueFilters.state, 'partial');
  assert.equal(m.state.rescueFilters.content_kind, 'series_chapter');
  assert.equal(m.state.rescueFilters.source_kind, 'user_backup');
  const api = new URL(m.calls[0].url, m.location.origin);
  for (const [key, expected] of Object.entries({page: '4', page_size: '40', state: 'partial', content_kind: 'series_chapter', source_kind: 'user_backup'})) {
    assert.equal(api.searchParams.get(key), expected);
    assert.equal(new URLSearchParams(m.location.search).get(key), expected);
  }
});

test('author -> series -> reader -> author chain preserves each immediate parent and chapter context', async () => {
  const source = '/dashboard/follows?page=4&status=normal&q=fixture';
  const user = environment('/dashboard/users/9?category=series&page=3&from=' + encodeURIComponent(source)).mount('dashboard_user_detail.html');
  await user.start();
  assert.equal(value(user.state.backHref), source);
  assert.equal(new URLSearchParams(user.location.search).get('from'), source, 'query writing must not discard parent');
  const series = environment(user.href('item.series_id', {item: {series_id: 2}})).mount('dashboard_series_detail.html');
  await series.start();
  assert.equal(value(series.state.backHref), user.currentPath());
  const novel = environment(series.href('novel.novel_id', {novel: {novel_id: 1}})).mount('dashboard_novel_detail.html');
  await novel.start();
  assert.equal(value(novel.state.backHref), series.currentPath());
  assert.equal(typeof novel.state.chapterHref, 'function');
  const next = new URL(novel.state.chapterHref(2), novel.location.origin);
  assert.equal(next.pathname, '/dashboard/novels/2');
  assert.equal(next.searchParams.get('from'), series.currentPath(), 'chapter changes must not create a reader return loop');
  series.state.continueNovelId.value = 1;
  series.state.continueReading();
  assert.equal(new URL(series.location.href, series.location.origin).searchParams.get('from'), series.currentPath());
});

test('scroll state is isolated by complete path/query, stores coordinates only and restores once after render', () => {
  const url = '/dashboard/novels?category=rescue&page=4&search=fixture';
  const session = savedList(url, 1370);
  assert.equal(session.size, 1);
  assert.deepEqual(JSON.parse([...session.values()][0]), {scrollY: 1370});
  for (const other of ['/dashboard/novels?category=rescue&page=3&search=fixture', '/dashboard/follows?page=4', '/dashboard/novels?category=bookmark&page=4&search=fixture']) {
    const m = environment(other, {session}), state = scrollState(m);
    state.restore();
    assert.deepEqual(m.scrolls, []);
    state.dispose();
  }
  const m = environment(url, {session}), state = scrollState(m);
  m.document.documentElement.scrollHeight = 600;
  assert.deepEqual(m.scrolls, [], 'do not restore into the loading skeleton');
  m.document.documentElement.scrollHeight = 3000;
  state.restore();
  assert.equal(m.window.scrollY, 1370);
  for (const event of ['scroll', 'wheel', 'touchstart', 'pointerdown', 'keydown']) assert.equal(m.count(event), 0, event);
  m.window.scrollY = 500;
  state.restore();
  assert.equal(m.window.scrollY, 500, 'late callbacks cannot restore twice');
  state.dispose();
  assert.equal([...m.events.values()].reduce((sum, set) => sum + set.size, 0), 0);
  assert.equal(m.history.scrollRestoration, 'auto');
});

for (const event of ['scroll', 'wheel', 'touchstart', 'pointerdown', 'keydown']) {
  test(`loading-time ${event} cancels list restoration, including a round trip back to zero`, () => {
    const url = '/dashboard/follows?page=4';
    const m = environment(url, {session: savedList(url, 1100)}), state = scrollState(m);
    m.window.scrollY = 80;
    m.emit(event, {key: 'PageDown'});
    m.window.scrollY = 0;
    m.emit(event, {key: 'Home'});
    state.restore();
    assert.deepEqual(m.scrolls, []);
    for (const name of ['scroll', 'wheel', 'touchstart', 'pointerdown', 'keydown']) assert.equal(m.count(name), 0);
    state.dispose();
  });
}

test('scroll lifecycle handles browser history, BFCache, hash navigation, disposal and unavailable storage', () => {
  const url = '/dashboard/users/9?category=series&page=2';
  const first = environment(url), state = scrollState(first);
  state.restore();
  first.window.scrollY = 700;
  first.emit('pagehide');
  const back = environment(url, {session: first.session}), backState = scrollState(back);
  backState.restore();
  assert.equal(back.window.scrollY, 700, 'ordinary browser back needs no from parameter');
  first.window.scrollY = 0;
  first.emit('pageshow', {persisted: true});
  assert.equal(first.window.scrollY, 700, 'BFCache restores the saved list without fetching');
  const hash = environment(url + '#target', {session: first.session});
  scrollState(hash).restore();
  assert.deepEqual(hash.scrolls, [], 'explicit fragment navigation wins');
  const disposed = environment(url, {session: first.session}), disposedState = scrollState(disposed);
  disposedState.dispose();
  disposedState.restore();
  assert.deepEqual(disposed.scrolls, []);
  const denied = environment(url);
  Object.defineProperty(denied.window, 'sessionStorage', {get() { throw Error('denied'); }});
  assert.doesNotThrow(() => { const deniedState = scrollState(denied); deniedState.restore(); deniedState.save(); deniedState.dispose(); });
  const corrupt = environment(url, {session: new Map([...first.session].map(([key]) => [key, '{bad json']))});
  assert.doesNotThrow(() => scrollState(corrupt).restore());
});

const restoreInteractions = ['scroll', 'wheel', 'touchstart', 'pointerdown', 'keydown'];
for (const readiness of ['while hidden', 'after resume', 'after a second resume']) {
  test(`I3: an eligible pending restore survives suspension when data becomes ready ${readiness}`, () => {
    const url = '/dashboard/follows?page=3&status=normal&q=fixture';
    const m = environment(url, {session: savedList(url, 900)}), state = scrollState(m);
    m.document.documentElement.scrollHeight = 600;
    m.emit('pagehide', {persisted: true});
    assert.equal(m.history.scrollRestoration, 'auto');
    for (const name of restoreInteractions) assert.equal(m.count(name), 0, 'suspension must detach listeners');
    if (readiness === 'while hidden') {
      m.document.documentElement.scrollHeight = 3000;
      state.restore();
      state.restore();
    }
    assert.deepEqual(m.scrolls, [], 'never restore a hidden page or loading skeleton');
    assert.equal(JSON.parse([...m.session.values()][0]).scrollY, 900, 'do not save the skeleton zero');
    m.emit('pageshow', {persisted: true});
    if (readiness === 'after a second resume') {
      m.emit('pagehide', {persisted: true});
      m.emit('pageshow', {persisted: true});
    }
    const resumedListeners = restoreInteractions.map(name => m.count(name));
    if (readiness !== 'while hidden') {
      assert.deepEqual(m.scrolls, []);
      m.document.documentElement.scrollHeight = 3000;
      state.restore();
    }
    assert.equal(m.window.scrollY, 900, 'consume the eligible initial restoration only when active and ready');
    assert.deepEqual(m.scrolls, [900]);
    if (readiness !== 'while hidden') assert.deepEqual(resumedListeners, [1, 1, 1, 1, 1]);
    for (const name of restoreInteractions) assert.equal(m.count(name), 0);
    m.window.scrollY = 120;
    state.restore();
    assert.deepEqual(m.scrolls, [900], 'late readiness callbacks cannot restore twice');
    assert.equal(m.window.scrollY, 120);
    state.dispose();
    assert.equal([...m.events.values()].reduce((sum, set) => sum + set.size, 0), 0);
  });
}

test('I3: manual cancellation before suspension remains cancelled whether data resolves hidden or resumed', () => {
  const url = '/dashboard/follows?page=3';
  for (const name of restoreInteractions) {
    for (const readyWhileHidden of [true, false]) {
      const m = environment(url, {session: savedList(url, 900)}), state = scrollState(m);
      m.window.scrollY = 80;
      m.emit(name, {key: 'PageDown'});
      m.window.scrollY = 0;
      m.emit(name, {key: 'Home'});
      m.emit('pagehide', {persisted: true});
      if (readyWhileHidden) state.restore();
      m.emit('pageshow', {persisted: true});
      state.restore();
      assert.equal(m.window.scrollY, 0, `${name}, hidden readiness=${readyWhileHidden}`);
      assert.deepEqual(m.scrolls, [], 'lifecycle events must not undo a real user cancellation');
      state.dispose();
    }
  }
});

test('I3: resumed pending listeners still cancel on every manual interaction, including a round trip to zero', () => {
  const url = '/dashboard/follows?page=3';
  for (const name of restoreInteractions) {
    const m = environment(url, {session: savedList(url, 900)}), state = scrollState(m);
    m.emit('pagehide', {persisted: true});
    m.emit('pageshow', {persisted: true});
    assert.equal(m.count(name), 1, 'the resumed wait must listen for ' + name);
    m.window.scrollY = 80;
    m.emit(name, {key: 'PageDown'});
    m.window.scrollY = 0;
    m.emit(name, {key: 'Home'});
    state.restore();
    assert.deepEqual(m.scrolls, []);
    for (const event of restoreInteractions) assert.equal(m.count(event), 0);
    state.dispose();
  }
});

test('I3: changed query, explicit hash, explicit cancellation or disposal prevents a suspended restore', () => {
  const url = '/dashboard/follows?page=3';
  for (const reason of ['query', 'hash', 'cancel', 'dispose']) {
    const m = environment(url, {session: savedList(url, 900)}), state = scrollState(m);
    m.emit('pagehide', {persisted: true});
    if (reason === 'query') m.history.replaceState(null, '', '/dashboard/follows?page=4');
    if (reason === 'hash') m.location.hash = '#target';
    if (reason === 'cancel') state.cancel();
    if (reason === 'dispose') state.dispose();
    m.emit('pageshow', {persisted: true});
    state.restore();
    assert.deepEqual(m.scrolls, [], reason);
    state.dispose();
    assert.equal([...m.events.values()].reduce((sum, set) => sum + set.size, 0), 0, reason);
  }
});

for (const [name, url] of [
  ['dashboard_novels.html', '/dashboard/novels?category=bookmark&page=3'],
  ['dashboard_follows.html', '/dashboard/follows?page=3&status=normal&q=fixture'],
  ['dashboard_user_detail.html', '/dashboard/users/9?category=single&page=3&from=%2Fdashboard%2Ffollows'],
  ['dashboard_series_detail.html', '/dashboard/series/2?from=%2Fdashboard%2Fnovels%3Fcategory%3Dfollowing'],
]) {
  for (const manualCancel of [false, true]) {
    test(`I3: ${name} deferred data after persisted return ${manualCancel ? 'honors manual cancellation' : 'restores exactly once'}`, async () => {
      const first = environment(url).mount(name);
      await first.start();
      first.window.scrollY = 900;
      first.emit('pagehide', {persisted: true});
      const loading = deferred();
      const m = environment(first.currentPath(), {session: first.session,
        fetch: async (request, options, fixture) => { await loading.promise; return fixture(request, options); },
      }).mount(name);
      m.document.documentElement.scrollHeight = 600;
      await m.start();
      const requests = m.calls.length;
      m.emit('pagehide', {persisted: true});
      m.emit('pageshow', {persisted: true});
      assert.deepEqual(m.scrolls, []);
      if (manualCancel) m.emit('touchstart');
      m.document.documentElement.scrollHeight = 3000;
      loading.resolve();
      await m.flush();
      assert.equal(m.window.scrollY, manualCancel ? 0 : 900);
      assert.deepEqual(m.scrolls, manualCancel ? [] : [900]);
      assert.equal(m.calls.length, requests, 'persisted return must not launch a duplicate fetch');
      m.dispose();
      assert.equal(m.count('pagehide'), 0);
      assert.equal(m.count('pageshow'), 0);
    });
  }
}

for (const [name, url] of [
  ['dashboard_novels.html', '/dashboard/novels?category=bookmark&page=3'],
  ['dashboard_follows.html', '/dashboard/follows?page=3&status=normal&q=fixture'],
  ['dashboard_user_detail.html', '/dashboard/users/9?category=single&page=3&from=%2Fdashboard%2Ffollows'],
  ['dashboard_series_detail.html', '/dashboard/series/2?from=%2Fdashboard%2Fnovels%3Fcategory%3Dfollowing'],
]) {
  test(`${name}: actual lifecycle restores explicit return and never overrides a loading-time gesture`, async () => {
    const first = environment(url).mount(name);
    await first.start();
    first.window.scrollY = 940;
    first.emit('pagehide');
    const back = environment(first.currentPath(), {session: first.session}).mount(name);
    await back.start();
    assert.equal(back.window.scrollY, 940);
    back.dispose();
    assert.equal(back.count('pagehide'), 0);
    const manual = environment(first.currentPath(), {session: first.session}).mount(name);
    const starting = manual.start();
    manual.emit('touchstart');
    await starting;
    assert.equal(manual.window.scrollY, 0);
  });
}

for (const [name, url, oldEndpoint] of [
  ['dashboard_novels.html', '/dashboard/novels', '/api/dashboard/novels?'],
  ['dashboard_follows.html', '/dashboard/follows', '/api/dashboard/users?'],
  ['dashboard_user_detail.html', '/dashboard/users/9', '/api/dashboard/users/9/novels?'],
]) {
  test(`${name}: an old list response cannot rewrite newer pagination/return context`, async () => {
    const old = deferred();
    const m = environment(url, {fetch: async request => {
      if (!request.includes(oldEndpoint)) return response({user_id: 9, name: 'fixture author'});
      const page = Number(new URL(request, 'https://library.invalid').searchParams.get('page') || 1);
      return page === 1 ? old.promise : response({items: [{novel_id: 2}], page, total: 30, total_pages: 3});
    }}).mount(name);
    await m.start();
    m.state.handlePageChange(2);
    await m.flush();
    old.resolve(response({items: [{novel_id: 1}], page: 1, total: 30, total_pages: 3}));
    await m.flush();
    assert.equal(m.state.page.value, 2);
    assert.equal(new URLSearchParams(m.location.search).get('page'), '2');
    assert.equal(m.state.items.value[0].novel_id, 2);
  });
}

test('source expansion uses per-item type/id keys, never navigates or mutates source content', () => {
  const m = environment('/dashboard/novels?category=rescue').mount('dashboard_novels.html');
  assert.equal(typeof m.state.toggleSources, 'function');
  const novel = {item_type: 'novel', item_id: 7, sources: [{label: '<unsafe> very long fixture source'}]};
  const series = {item_type: 'series', item_id: 7, sources: [{label: 'another source'}]};
  const original = JSON.stringify([novel, series]);
  const click = m.html.match(/@click\.stop="(toggleSources\(item\))"/);
  assert.ok(click, 'exercise the actual source button binding');
  m.evaluate(click[1], {item: novel});
  assert.equal(m.state.sourcesExpanded(novel), true);
  assert.equal(m.state.sourcesExpanded(series), false);
  m.evaluate(click[1], {item: series});
  assert.equal(m.state.sourcesExpanded(novel), true);
  assert.equal(m.state.sourcesExpanded(series), true);
  m.evaluate(click[1], {item: novel});
  assert.equal(m.state.sourcesExpanded(novel), false);
  assert.equal(m.state.sourcesExpanded(series), true);
  assert.equal(JSON.stringify([novel, series]), original);
  assert.equal(m.location.href, '');
  assert.equal(m.calls.length, 0);
});

test('a changed debounced search invalidates the old response before its replacement request starts', async () => {
  const old = deferred();
  const m = environment('/dashboard/novels', {fetch: async request => {
    const query = new URL(request, 'https://library.invalid').searchParams;
    return query.get('search') === 'new fixture' ? response({items: [{novel_id: 2}], page: 1, total_pages: 1}) : old.promise;
  }}).mount('dashboard_novels.html');
  await m.start();
  m.state.filters.search = 'new fixture';
  await m.changed();
  old.resolve(response({items: [{novel_id: 1}], page: 4, total_pages: 5}));
  await m.flush();
  assert.equal(m.state.items.value.length, 0, 'do not show the superseded list while search is debouncing');
  assert.equal(m.state.loading.value, true);
  await m.runTimers();
  assert.equal(m.state.items.value[0].novel_id, 2);
  assert.equal(new URLSearchParams(m.location.search).get('search'), 'new fixture');
  assert.equal(m.state.page.value, 1);
});

test('series archive deletion preserves failures and returns to its safe parent only after success', async () => {
  const from = '/dashboard/users/9?category=series&page=3';
  const m = environment('/dashboard/series/2?from=' + encodeURIComponent(from)).mount('dashboard_series_detail.html');
  await m.start();
  m.window.csrfFetch = async () => response({ok: false, error: 'fixture rejection'});
  await m.state.deleteSeries();
  assert.equal(m.location.href, '');
  assert.equal(m.notices.at(-1)[1], 'error');
  m.window.csrfFetch = async () => { throw Error('fixture offline'); };
  await m.state.deleteSeries();
  assert.equal(m.location.href, '');
  m.window.confirm = () => false;
  let requests = 0;
  m.window.csrfFetch = async () => { requests++; return response({ok: true}); };
  await m.state.deleteSeries();
  assert.equal(requests, 0);
  m.window.confirm = () => true;
  m.window.csrfFetch = async () => response({ok: true});
  await m.state.deleteSeries();
  assert.equal(m.location.href, from);
});

test('saving from the actual reader toolbar samples live scroll, never moves, and rejects duplicate saves', async () => {
  const m = environment('/dashboard/novels/1').mount('dashboard_novel_detail.html');
  await m.start();
  assert.match(m.html, /aria-label="阅读工具"/);
  const pending = deferred();
  m.window.csrfFetch = async (url, options) => { m.calls.push({url, options}); return pending.promise; };
  m.window.scrollY = 1440;
  const priorScrolls = m.scrolls.length;
  const saving = m.state.saveServerProgress();
  const count = m.calls.length;
  await m.state.saveServerProgress();
  assert.equal(m.calls.length, count);
  assert.equal(JSON.parse(m.calls.at(-1).options.body).progress, 60);
  m.window.scrollY = 1560;
  m.emit('scroll');
  pending.resolve(response({ok: true}));
  await saving;
  assert.equal(m.window.scrollY, 1560);
  assert.equal(m.scrolls.length, priorScrolls);
  assert.equal(m.state.progressBusy.value, false);
});

test('failed save/reset/archive deletion preserves reading position, local progress and safe return', async () => {
  const local = new Map([['pixivNovelSync:reading:1', JSON.stringify({scrollY: 720, progress: 0.3})]]);
  const m = environment('/dashboard/novels/1?from=%2Fdashboard%2Fnovels%3Fpage%3D3', {local}).mount('dashboard_novel_detail.html');
  await m.start();
  const before = m.local.get('pixivNovelSync:reading:1');
  m.window.csrfFetch = async () => response({ok: false, error: 'fixture rejection'});
  await m.state.resetServerProgress();
  assert.equal(m.window.scrollY, 720);
  assert.equal(m.local.get('pixivNovelSync:reading:1'), before);
  await m.state.saveServerProgress();
  assert.equal(m.notices.at(-1)[1], 'error');
  assert.equal(m.window.scrollY, 720);
  m.window.csrfFetch = async () => { throw Error('fixture offline'); };
  await m.state.deleteThisNovel();
  assert.equal(m.location.href, '');
  assert.equal(m.window.scrollY, 720);
  assert.ok(m.local.has('pixivNovelSync:reading:1'));
  assert.equal(m.notices.at(-1)[1], 'error');
  m.window.confirm = () => false;
  let requests = 0;
  m.window.csrfFetch = async () => { requests++; return response({ok: true}); };
  await m.state.deleteThisNovel();
  assert.equal(requests, 0);
  m.window.confirm = () => true;
  await m.state.deleteThisNovel();
  assert.equal(m.location.href, '/dashboard/novels?page=3');
});

test('reader font controls retain all four settings without navigation', async () => {
  const m = environment('/dashboard/novels/1').mount('dashboard_novel_detail.html');
  await m.start();
  m.window.scrollY = 600;
  const seen = new Set();
  for (let i = 0; i < 4; i++) { m.state.cycleFontSize(); seen.add(value(m.state.fontSize)); }
  assert.deepEqual([...seen].sort(), ['large', 'normal', 'small', 'xlarge']);
  assert.equal(m.window.scrollY, 600);
  assert.equal(m.location.href, '');
  assert.equal(m.local.get('pixivNovelSync:fontSize'), value(m.state.fontSize));
});

test('reader toolbar dialog bindings open and close the correct shared modal', async () => {
  const m = environment('/dashboard/novels/1').mount('dashboard_novel_detail.html');
  await m.start();
  for (const name of ['showReaderActions', 'showChapters']) {
    const button = [...m.html.matchAll(/<button\b[^>]*>/g)].find(match => match[0].includes(`@click="${name} = true"`));
    const modal = [...m.html.matchAll(/<app-modal\b[^>]*>/g)].find(match => match[0].includes(`:is-open="${name}"`));
    assert.ok(button && modal);
    assert.equal(m.evaluate(name), false);
    m.evaluate(button[0].match(/@click="([^"]+)"/)[1]);
    assert.equal(m.evaluate(modal[0].match(/:is-open="([^"]+)"/)[1]), true);
    m.evaluate(modal[0].match(/@close="([^"]+)"/)[1]);
    assert.equal(m.evaluate(name), false);
  }
});

test('a standalone novel keeps in-place save/font tools and disables only chapter navigation', async () => {
  const m = environment('/dashboard/novels/1', {fetch: async url => response(url.endsWith('/progress')
    ? {progress: 0} : {novel_id: 1, user_id: 9, series_id: null, title: 'standalone fixture', text_raw: 'fixture only'})}).mount('dashboard_novel_detail.html');
  await m.start();
  assert.equal(m.state.seriesInfo.value, null);
  const chapter = [...m.html.matchAll(/<button\b[^>]*>/g)].find(match => match[0].includes('@click="showChapters = true"'));
  assert.ok(chapter);
  assert.equal(m.evaluate(chapter[0].match(/:disabled="([^"]+)"/)[1]), true);
  m.window.scrollY = 960;
  await m.state.saveServerProgress();
  assert.equal(JSON.parse(m.calls.at(-1).options.body).progress, 40);
  assert.equal(m.window.scrollY, 960);
  assert.equal(m.state.progressBusy.value, false);
  assert.equal(m.location.href, '');
});

function installDownloadFixture(m) {
  const downloads = {created: [], revoked: [], clicked: [], failAt: ''};
  m.context.URL = class extends URL {
    static createObjectURL(blob) {
      if (downloads.failAt === 'create') throw Error('fixture create failure');
      downloads.created.push(blob);
      return 'blob:fixture/epub-' + downloads.created.length;
    }
    static revokeObjectURL(url) { downloads.revoked.push(url); }
  };
  m.document.createElement = tag => {
    assert.equal(tag, 'a');
    return {href: '', download: '', click() {
      if (downloads.failAt === 'click') throw Error('fixture click failure');
      downloads.clicked.push({href: this.href, download: this.download});
    }};
  };
  return downloads;
}
async function readerWithOpenActions() {
  const local = new Map([['pixivNovelSync:reading:1', JSON.stringify({scrollY: 1688, progress: 1688 / 2400})]]);
  const m = environment('/dashboard/novels/1?from=%2Fdashboard%2Fnovels%3Fpage%3D2%26page_size%3D10%26search%3D%25E9%259B%25A8%25E5%2590%258E', {local}).mount('dashboard_novel_detail.html');
  await m.start();
  m.state.showReaderActions.value = true;
  m.downloads = installDownloadFixture(m);
  return m;
}
function assertReaderPositionKept(m, previousScrolls) {
  assert.equal(m.window.scrollY, 1688);
  assert.deepEqual(m.scrolls, previousScrolls);
  const progress = JSON.parse(m.local.get('pixivNovelSync:reading:1'));
  assert.equal(progress.scrollY, 1688);
  assert.equal(progress.progress, 1688 / 2400);
  assert.equal(m.state.novel.value.text_raw, 'fixture only');
}
function assertDialogFeedback(m, type, expected, id = 'reader-action-feedback') {
  assert.equal(value(m.state.readerActionMessageType), type, 'dialog feedback must have a result type');
  assert.equal(value(m.state.readerActionMessage), expected, 'dialog feedback must retain the action result');
  const region = m.html.match(new RegExp(`<div\\b([^>]*id="${id}"[^>]*)>([\\s\\S]*?)<\\/div>`));
  assert.ok(region, 'missing actual dialog feedback region: ' + id);
  assert.equal(m.evaluate(region[1].match(/:role="([^"]+)"/)[1]), type === 'error' ? 'alert' : 'status');
  assert.equal(m.evaluate(region[1].match(/:aria-live="([^"]+)"/)[1]), type === 'error' ? 'assertive' : 'polite');
  assert.equal(m.evaluate(region[2].match(/{{\s*([\s\S]*?)\s*}}/)[1]), expected, 'actual visible text binding');
}

for (const method of ['saveServerProgress', 'resetServerProgress', 'removeLocalBookmark', 'deleteThisNovel', 'exportEpub']) {
  for (const failure of ['HTTP 503', 'business rejection', 'network rejection']) {
    test(`I4: ${method} ${failure} stays readable inside More without closing or losing progress`, async () => {
      const m = await readerWithOpenActions();
      const scrolls = m.scrolls.slice(), writes = [];
      let confirmations = 0, formatted = 0;
      m.window.confirm = () => { confirmations++; return true; };
      const message = `fixture ${method} ${failure} <unsafe> 正文不丢失`;
      m.window.errorText = data => { formatted++; return 'formatted: ' + data.error; };
      m.window.csrfFetch = async (url, options) => {
        writes.push({url, options});
        if (failure === 'network rejection') throw Error(message);
        return {...response({ok: false, error: message}, failure !== 'HTTP 503'),
          status: failure === 'HTTP 503' ? 503 : 200,
          headers: {get: () => 'application/json'}, blob: async () => ({type: 'application/json'}),
        };
      };
      await assert.doesNotReject(m.state[method](), 'action failures must be handled, including EPUB network errors');
      assert.equal(writes.length, 1);
      assert.equal(confirmations, ['resetServerProgress', 'removeLocalBookmark', 'deleteThisNovel'].includes(method) ? 1 : 0);
      assert.equal(formatted, failure === 'network rejection' ? 0 : 1);
      const expected = failure === 'network rejection' ? message : 'formatted: ' + message;
      assertDialogFeedback(m, 'error', expected);
      assert.equal(m.state.showReaderActions.value, true);
      assert.equal(m.location.href, '');
      assert.equal(m.notices.length, 0, 'do not rely on the obscured body-level toast');
      assert.equal(m.downloads.clicked.length, 0, 'a JSON error is not a downloadable EPUB');
      assertReaderPositionKept(m, scrolls);
      await m.runTimers();
      assertDialogFeedback(m, 'error', expected);
    });
  }
}

for (const method of ['saveServerProgress', 'resetServerProgress', 'removeLocalBookmark', 'exportEpub']) {
  test(`I4: ${method} success is persistent dialog-local status`, async () => {
    const m = await readerWithOpenActions();
    const scrolls = m.scrolls.slice();
    m.window.csrfFetch = async () => ({...response({ok: true}),
      headers: {get: () => 'application/epub+zip'}, blob: async () => ({type: 'application/epub+zip'}),
    });
    await m.state[method]();
    const message = value(m.state.readerActionMessage);
    assert.equal(typeof message, 'string');
    assert.ok(message.length > 0);
    assertDialogFeedback(m, 'success', message);
    assert.equal(m.state.showReaderActions.value, true);
    assert.equal(m.notices.length, 0);
    if (method === 'resetServerProgress') {
      assert.equal(m.window.scrollY, 0, 'only a confirmed successful reset intentionally moves to the top');
      assert.equal(m.local.has('pixivNovelSync:reading:1'), false);
    } else assertReaderPositionKept(m, scrolls);
    if (method === 'exportEpub') {
      assert.match(message, /EPUB/);
      assert.equal(m.downloads.clicked.length, 1);
      assert.equal(m.downloads.clicked[0].download, 'fixture book.epub');
      assert.deepEqual(m.downloads.revoked, ['blob:fixture/epub-1']);
      assert.equal(value(m.state.exportingEpub), false);
    }
    await m.runTimers();
    assertDialogFeedback(m, 'success', message);
  });
}

test('I4: successful archive deletion records status and preserves the validated return navigation', async () => {
  const m = await readerWithOpenActions();
  await m.state.deleteThisNovel();
  const message = value(m.state.readerActionMessage);
  assert.equal(typeof message, 'string');
  assert.match(message, /删除/);
  assertDialogFeedback(m, 'success', message);
  assert.equal(m.location.href, '/dashboard/novels?page=2&page_size=10&search=%E9%9B%A8%E5%90%8E');
});

test('I4: EPUB blob/object-URL/click failures are visible and release any created object URL', async () => {
  for (const phase of ['blob', 'create', 'click']) {
    const m = await readerWithOpenActions();
    const scrolls = m.scrolls.slice();
    m.downloads.failAt = phase;
    m.window.csrfFetch = async () => ({...response({ok: true}), headers: {get: () => 'application/epub+zip'},
      blob: async () => { if (phase === 'blob') throw Error('fixture blob failure'); return {type: 'application/epub+zip'}; },
    });
    await assert.doesNotReject(m.state.exportEpub());
    assertDialogFeedback(m, 'error', `fixture ${phase} failure`);
    assert.deepEqual(m.downloads.revoked, phase === 'click' ? ['blob:fixture/epub-1'] : []);
    assert.equal(value(m.state.exportingEpub), false);
    assert.equal(m.state.showReaderActions.value, true);
    assertReaderPositionKept(m, scrolls);
  }
});

test('I4: pending EPUB export rejects duplicate clicks without requesting native confirmation', async () => {
  const m = await readerWithOpenActions(), post = deferred();
  let requests = 0;
  m.window.confirm = () => { throw Error('EPUB must not require native confirmation'); };
  m.window.csrfFetch = async () => { requests++; return post.promise; };
  const exporting = m.state.exportEpub();
  assert.equal(value(m.state.exportingEpub), true);
  await m.state.exportEpub();
  assert.equal(requests, 1);
  post.resolve(response({error: 'fixture busy export failure'}, false));
  await exporting;
  assertDialogFeedback(m, 'error', 'fixture busy export failure');
  assert.equal(value(m.state.exportingEpub), false);
});

test('I4: closing More during an export keeps normal-page toast feedback without reopening the dialog', async () => {
  const m = await readerWithOpenActions(), post = deferred();
  m.window.csrfFetch = async () => post.promise;
  const exporting = m.state.exportEpub();
  m.state.showReaderActions.value = false;
  post.resolve(response({error: 'fixture late export failure'}, false));
  await exporting;
  assert.equal(m.state.showReaderActions.value, false);
  assert.equal(value(m.state.readerActionMessage), 'fixture late export failure');
  assert.deepEqual(m.notices.at(-1), ['fixture late export failure', 'error']);
  assert.equal(m.window.scrollY, 1688);
});

test('I4: a save completing while Chapters is active reports in that dialog without moving the reader', async () => {
  const m = await readerWithOpenActions(), post = deferred();
  const scrolls = m.scrolls.slice();
  m.state.showReaderActions.value = false;
  m.window.csrfFetch = async () => post.promise;
  const saving = m.state.saveServerProgress();
  m.state.showChapters.value = true;
  post.resolve(response({error: 'fixture chapter-visible save failure'}, false));
  await saving;
  assertDialogFeedback(m, 'error', 'fixture chapter-visible save failure', 'reader-chapter-action-feedback');
  assert.equal(m.state.showChapters.value, true);
  assert.equal(m.notices.length, 0);
  assertReaderPositionKept(m, scrolls);
});

test('I4: cancelling a native confirmation preserves the prior result and never mutates data', async () => {
  for (const method of ['resetServerProgress', 'removeLocalBookmark', 'deleteThisNovel']) {
    const m = await readerWithOpenActions();
    m.window.csrfFetch = async () => response({error: 'fixture previous rejection'}, false);
    await m.state[method]();
    const previous = value(m.state.readerActionMessage), scrolls = m.scrolls.slice();
    let writes = 0;
    m.window.csrfFetch = async () => { writes++; return response({ok: true}); };
    m.window.confirm = () => false;
    await m.state[method]();
    assert.equal(writes, 0);
    assert.equal(value(m.state.readerActionMessage), previous);
    assert.equal(m.state.showReaderActions.value, true);
    assertReaderPositionKept(m, scrolls);
  }
});

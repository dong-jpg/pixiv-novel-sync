const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const templateRoot = path.resolve(__dirname, '../src/pixiv_novel_sync/templates');
const response = (data, ok = true) => ({ok, json: async () => data});
const deferred = () => { let resolve; const promise = new Promise(r => {resolve = r;}); return {promise, resolve}; };
function mount(name, saved) {
  let state;
  const mounted = [], ticks = [], calls = [], notices = [], local = new Map(), events = {};
  if (saved) local.set('pixivNovelSync:reading:1', JSON.stringify(saved));
  const window = {
    location: {pathname: '/dashboard/novels/1', search: '', hash: '', origin: 'http://localhost', href: ''},
    scrollY: 0, innerHeight: 500,
    scrollTo({top}) { this.scrollY = top; }, addEventListener(name, fn) {events[name] = fn;}, removeEventListener() {},
    readListQuery: defaults => defaults, writeListQuery() {},
    toast: (...args) => notices.push(args), errorText: data => data.error || 'failed',
    csrfFetch: async (url, options) => { calls.push({url, options}); return response({ok: true}); },
    confirm: () => true,
  };
  const document = {documentElement: {scrollHeight: 2000}, referrer: ''};
  const localStorage = {getItem: key => local.get(key) || null,
    setItem: (key, value) => local.set(key, value), removeItem: key => local.delete(key)};
  const context = {window, document, localStorage, location: window.location, URL, URLSearchParams, AbortController,
    setTimeout: () => 0, clearTimeout() {}, console,
    confirm: message => window.confirm(message),
    csrfFetch: (...args) => window.csrfFetch(...args),
    fetch: async url => { calls.push({url}); return response(url.endsWith('/progress') ? {progress: 60} : {novel_id: 1, title: 'book', user_id: 1, text_raw: 'text'}); },
    initVueApp: app => {state = app.setup();},
    Vue: {ref: value => ({value}), reactive: value => value,
      computed: fn => ({get value() {return fn();}}), watch() {},
      onMounted: fn => mounted.push(fn), onUnmounted() {}, nextTick: fn => ticks.push(fn)},
  };
  const html = fs.readFileSync(path.join(templateRoot, name), 'utf8');
  const navigation = fs.readFileSync(path.join(templateRoot, 'navigation_helpers.html'), 'utf8');
  vm.runInNewContext(navigation.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  return {state, window, calls, notices, local, context, html, events, async start() {
    mounted.forEach(fn => fn());
    for (let i = 0; i < 12; i++) await Promise.resolve();
    for (const fn of ticks) await fn();
  }};
}
(async () => {
  {
    const m = mount('dashboard_novel_detail.html');
    await m.start();
    assert.equal(m.window.scrollY, 900, 'server percentage must restore scroll on another device');
    assert.equal(m.state.progressPercent.value, 60);
    assert.equal(JSON.parse(m.local.get('pixivNovelSync:reading:1')).progress, 0.6);
    m.window.scrollY = 1200; // Scroll listener may still be throttled when clicked.
    await m.state.saveServerProgress();
    assert.equal(m.calls.at(-1).options.method, 'POST');
    assert.equal(JSON.parse(m.calls.at(-1).options.body).progress, 80);
    await m.state.resetServerProgress();
    assert.equal(m.calls.at(-1).options.method, 'DELETE');
    assert.match(m.calls.at(-1).url, /\/novels\/1\/progress$/);
    assert.equal(m.window.scrollY, 0);
    assert.equal(m.state.progressPercent.value, 0);
    assert.equal(m.local.has('pixivNovelSync:reading:1'), false);
    assert.match(m.html, /@click="resetServerProgress"/);
  }
  {
    const m = mount('dashboard_novel_detail.html');
    m.context.fetch = async url => {
      if (url.endsWith('/progress')) {
        m.window.scrollY = 150; m.events.scroll();
        m.window.scrollY = 0; m.events.scroll();
        return response({progress: 60});
      }
      return response({novel_id: 1, title: 'book', text_raw: 'text'});
    };
    await m.start();
    assert.equal(m.window.scrollY, 0, 'manual scroll back to origin must still cancel restoration');
  }
  {
    const m = mount('dashboard_novel_detail.html');
    const get = deferred(), post = deferred(), getStarted = deferred(), postStarted = deferred();
    m.context.fetch = async url => {
      if (url.endsWith('/progress')) {getStarted.resolve(); return get.promise;}
      return response({novel_id: 1, title: 'book', text_raw: 'text'});
    };
    m.window.csrfFetch = async (_url, options) => {
      assert.equal(JSON.parse(options.body).progress, 0);
      postStarted.resolve(); return post.promise;
    };
    const starting = m.start();
    await getStarted.promise;
    const saving = m.state.saveServerProgress();
    await postStarted.promise;
    get.resolve(response({progress: 60}));
    await starting;
    post.resolve(response({success: true}));
    await saving;
    assert.equal(m.window.scrollY, 0, 'old GET cannot override an in-flight explicit save');
    assert.equal(m.state.progressPercent.value, 0);
    assert.equal(JSON.parse(m.local.get('pixivNovelSync:reading:1')).progress, 0);
  }
  {
    const m = mount('dashboard_novel_detail.html');
    m.context.fetch = async url => {
      if (url.endsWith('/progress')) {
        m.window.scrollY = 150; // User scrolled while the account request was pending.
        return response({progress: 60});
      }
      return response({novel_id: 1, title: 'book', text_raw: 'text'});
    };
    await m.start();
    assert.equal(m.window.scrollY, 150);
    assert.equal(m.state.progressPercent.value, 10);
  }
  {
    const m = mount('dashboard_novel_detail.html', {scrollY: 300, progress: 0.2});
    await m.start();
    assert.equal(m.window.scrollY, 300, 'existing local position still wins');
    assert.equal(m.calls.some(c => c.url.endsWith('/progress')), false);
    m.window.csrfFetch = async () => response({error: 'reset rejected'}, false);
    await m.state.resetServerProgress();
    assert.equal(m.window.scrollY, 300);
    assert.equal(m.state.progressPercent.value, 20);
    assert.equal(m.local.has('pixivNovelSync:reading:1'), true);
  }
  for (const [name, method, suffix] of [
    ['dashboard_novel_detail.html', 'removeLocalBookmark', '/bookmarks/1'],
  ]) {
    const m = mount(name);
    assert.equal(typeof m.state[method], 'function');
    assert.ok(m.html.includes(`@click="${method}"`));
    m.window.confirm = () => false;
    await m.state[method]();
    assert.equal(m.calls.length, 0, 'cancel confirmation must not mutate');
    m.window.confirm = () => true;
    m.window.csrfFetch = async () => { throw Error('offline'); };
    await m.state[method]();
    assert.equal(m.window.location.href, '', 'failed deletion must not navigate');
    m.window.csrfFetch = async (url, options) => {m.calls.push({url, options}); return response({ok:true});};
    await m.state[method]();
    assert.ok(m.calls.at(-1).url.endsWith(suffix));
    assert.equal(m.calls.at(-1).options.method, 'DELETE');
  }
  const user = mount('dashboard_user_detail.html');
  assert.equal(user.state.deleteLocalUser, undefined, 'unused bulk user deletion must not be exposed during background backup');
  assert.ok(!user.html.includes('@click="deleteLocalUser"'));
  console.log('reader/user action runtime regressions passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

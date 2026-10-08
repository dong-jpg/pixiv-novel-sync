// Pure Node VM tests of the actual page/partial scripts, not a browser substitute.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const templateRoot = path.resolve(__dirname, '../src/pixiv_novel_sync/templates');
const pages = ['dashboard.html', 'dashboard_preferences.html'];
const item = (id, status = 'new') => ({
  id, run_id: 1, profile_id: 1, item_type: 'novel', novel_id: id, series_id: null,
  title: `长标题推荐 ${id}`, author_id: 7, author_name: '测试作者', tags: ['冒险', '很长的标签'.repeat(8)],
  score: 8, text_length: 15000, series_total_text_length: 0, reason: '推荐理由'.repeat(80),
  caption: '简介', status, source_url: `https://www.pixiv.net/novel/show.php?id=${id}`,
  created_at: '2026-10-08 00:00:00', updated_at: '2026-10-08 00:00:00',
});
const response = (data, status = 200) => {
  // HTTP JSON is a snapshot, not an alias to the fake server's mutable rows.
  const snapshot = JSON.stringify(data);
  return {ok: status >= 200 && status < 300, status, json: async () => JSON.parse(snapshot)};
};
const envelope = (items, page = 1, total = items.length) => ({
  ok: true, data: {items, page, page_size: 10, total, total_pages: Math.max(1, Math.ceil(total / 10))},
});
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
const tick = async () => { for (let i = 0; i < 24; i++) await Promise.resolve(); };

function mount(name) {
  const mounted = [], unmounted = [], components = new Map(), listeners = new WeakMap();
  const timers = new Map();
  let clock = 0, timerId = 0;
  const m = {gets: [], writes: [], notices: [], confirms: [], focuses: [], scrolls: [], components};
  m.get = async url => response(url.includes('/preferences/profiles') ? {ok: true, data: []} : envelope([]));
  m.post = async () => response({ok: true, data: null});
  m.confirm = () => true;
  const Vue = {
    ref(initial) {
      let value = initial;
      const hooks = new Set();
      const result = {get value() { return value; }, set value(next) {
        const previous = value; value = next;
        if (next !== previous) for (const hook of hooks) hook(next, previous);
      }};
      listeners.set(result, hooks);
      return result;
    },
    computed: fn => ({get value() { return fn(); }}),
    reactive: value => value,
    watch(source, callback) {
      assert(listeners.has(source), 'These unit tests watch refs; no DOM/reactivity simulation');
      listeners.get(source).add(callback);
      return () => listeners.get(source).delete(callback);
    },
    onMounted: fn => mounted.push(fn), onUnmounted: fn => unmounted.push(fn),
  };
  const window = {
    scrollY: 3062,
    TASK_LABELS: {bookmark: '收藏同步'},
    toast: (...args) => m.notices.push(args),
    confirm: text => { m.confirms.push(text); return m.confirm(text); },
    csrfFetch: async (url, options) => { m.writes.push({url, options}); return m.post(url, options); },
  };
  // Only observe calls to the native viewport/focus boundary; no layout/browser simulation.
  m.heading = {
    focus(options) { m.focuses.push({...options}); m.focused = this; },
    scrollIntoView(options) { m.scrolls.push({...options}); window.scrollY = 200; },
  };
  const document = {getElementById: id => id === 'recommendation-results-heading' ? m.heading : null};
  const context = vm.createContext({
    window, document, Vue, URL, URLSearchParams, console,
    fetch: async (url, options) => { m.gets.push({url, options}); return m.get(url, options); },
    csrfFetch: (...args) => window.csrfFetch(...args),
    errorText: (...args) => window.errorText(...args),
    confirm: text => window.confirm(text),
    setTimeout(fn, delay = 0) { const id = ++timerId; timers.set(id, {fn, at: clock + delay}); return id; },
    clearTimeout: id => timers.delete(id), setInterval: () => 1, clearInterval() {},
  });
  const base = fs.readFileSync(path.join(templateRoot, 'base.html'), 'utf8');
  for (const helper of ['errorText', 'formatDbTime']) {
    const source = base.match(new RegExp(`window\\.${helper} = function[\\s\\S]*?\\n    };`));
    assert(source, `Use the real shared ${helper} helper`);
    vm.runInContext(source[0], context);
  }
  window.initVueApp = context.initVueApp = options => {
    window.registerPageComponents?.({component: (key, value) => components.set(key, value)});
    m.state = options.setup();
  };
  const run = file => {
    const html = fs.readFileSync(path.join(templateRoot, file), 'utf8');
    for (const match of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)) {
      vm.runInContext(match[1], context, {filename: file});
    }
  };
  if (fs.existsSync(path.join(templateRoot, 'recommendation_components.html'))) run('recommendation_components.html');
  run(name);
  m.window = window;
  m.load = page => m.state.loadRecommendationItems(page);
  // Before F2, the real pagination event was wired directly to the loader.
  m.changePage = page => (m.state.changeRecommendationPage || m.state.loadRecommendationItems)(page);
  // The legacy pages had different public names; run their real handlers for RED evidence.
  m.feedback = (value, type) => (m.state.sendRecommendationFeedback || m.state.feedback)(value, type);
  m.mute = value => (m.state.muteRecommendationAuthor || m.state.muteAuthor)(value);
  m.retryProfiles = () => (m.state.retryProfiles || m.state.loadProfiles)();
  m.advanceTime = async milliseconds => {
    const until = clock + milliseconds;
    while (true) {
      const next = Array.from(timers).filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      const [id, timer] = next;
      timers.delete(id);
      clock = timer.at;
      timer.fn();
      await tick();
    }
    clock = until;
    await tick();
  };
  m.start = async () => { mounted.forEach(fn => fn()); await tick(); };
  m.stop = () => unmounted.forEach(fn => fn());
  return m;
}

// Models the existing API, including its default omission of dismissed/muted and
// its authoritative page clamp. This is an HTTP boundary double, never a real DB.
function serveItems(m, rows) {
  m.get = async url => {
    const parsed = new URL(url, 'https://unit.invalid');
    const status = parsed.searchParams.get('status');
    const filtered = rows.filter(row => status ? row.status === status : !['dismissed', 'muted'].includes(row.status));
    if (!parsed.searchParams.has('page')) return response({ok: true, data: filtered.slice(0, 100)});
    const pages = Math.max(1, Math.ceil(filtered.length / 10));
    const page = Math.min(pages, Math.max(1, Number(parsed.searchParams.get('page'))));
    return response(envelope(filtered.slice((page - 1) * 10, page * 10), page, filtered.length));
  };
}

for (const name of pages) {
  test(`${name}: hidden feedback does not leave a false empty first page`, async () => {
    const m = mount(name);
    serveItems(m, [...Array.from({length: 10}, (_, i) => item(i + 1, 'interested')), item(11), item(12)]);
    await m.load(1);
    const visible = m.state.visibleRecommendations?.value || m.state.recommendationItems.value;
    assert.deepEqual(Array.from(visible, row => row.id), [11, 12]);
    assert.equal(m.state.recommendationTotalPages.value, 1);
  });

  test(`${name}: requests page_size=10 and status=new, never the old array API`, async () => {
    const m = mount(name);
    await m.load(2);
    const query = new URL(m.gets[0].url, 'https://unit.invalid').searchParams;
    assert.equal(query.get('page'), '2');
    assert.equal(query.get('page_size'), '10');
    assert.equal(query.get('status'), 'new');
    assert.equal(query.has('limit'), false);
  });

  test(`${name}: toggling either direction resets to page one with matching totals`, async () => {
    const m = mount(name);
    serveItems(m, [...Array.from({length: 11}, (_, i) => item(i + 1)), ...Array.from({length: 20}, (_, i) => item(i + 12, 'saved'))]);
    await m.load(2);
    assert(m.state.hideFeedback, 'Both pages need the same hide-feedback control');
    m.state.hideFeedback.value = false;
    await tick();
    assert.equal(m.state.recommendationPage.value, 1);
    assert.equal(m.state.recommendationTotalPages.value, 4);
    assert.equal(m.state.recommendationTotal.value, 31);
    assert.equal(new URL(m.gets.at(-1).url, 'https://unit.invalid').searchParams.has('status'), false);
    await m.load(3);
    m.state.hideFeedback.value = true;
    await tick();
    assert.equal(m.state.recommendationPage.value, 1);
    assert.equal(m.state.recommendationTotalPages.value, 2);
    assert.equal(m.state.recommendationTotal.value, 11);
  });

  test(`${name}: a late page response cannot replace the newest page`, async () => {
    const m = mount(name), old = deferred(), current = deferred();
    let calls = 0;
    m.get = () => calls++ ? current.promise : old.promise;
    const earlier = m.load(2), later = m.load(3);
    current.resolve(response(envelope([item(30)], 3, 30)));
    await later;
    old.resolve(response(envelope([item(20)], 2, 30)));
    await earlier;
    assert.equal(m.state.recommendationPage?.value, 3);
    assert.equal(m.state.recommendationItems.value[0].id, 30);
  });

  test(`${name}: stale errors and finally do not alter a newer request`, async () => {
    const m = mount(name), old = deferred(), current = deferred();
    let calls = 0;
    m.get = () => calls++ ? current.promise : old.promise;
    const earlier = m.load(1), later = m.load(2);
    old.reject(new Error('old offline error'));
    await Promise.allSettled([earlier]);
    const stillLoading = m.state.loadingRecommendations?.value;
    const oldError = m.state.recommendationError?.value;
    current.resolve(response(envelope([item(20)], 2, 20)));
    await later;
    assert.equal(stillLoading, true);
    assert.equal(oldError, '');
    assert.equal(m.state.recommendationItems.value[0].id, 20);
    assert.equal(m.state.recommendationError.value, '');
  });

  test(`${name}: filter changes invalidate already-started page requests`, async () => {
    const m = mount(name), old = deferred();
    let calls = 0;
    m.get = () => calls++ ? response(envelope([item(5, 'interested')], 1, 1)) : old.promise;
    assert(m.state.hideFeedback, 'Both pages need the same hide-feedback control');
    const earlier = m.load(3);
    m.state.hideFeedback.value = false;
    await tick();
    old.resolve(response(envelope([item(30)], 3, 30)));
    await earlier;
    assert.equal(m.state.recommendationPage.value, 1);
    assert.equal(m.state.recommendationItems.value[0].id, 5);
    assert.equal(m.state.hideFeedback.value, false);
  });

  test(`${name}: refreshing a populated page has an explicit loading state`, async () => {
    const m = mount(name), pending = deferred();
    m.state.recommendationItems.value = [item(1)];
    m.get = () => pending.promise;
    const loading = m.load(1);
    const visibleLoading = m.state.loadingRecommendations?.value;
    pending.resolve(response(envelope([item(2)])));
    await loading;
    assert.equal(visibleLoading, true);
    assert.equal(m.state.loadingRecommendations.value, false);
  });

  test(`${name}: HTTP errors use errorText and retry the requested page`, async () => {
    const m = mount(name);
    m.get = async () => response({ok: false, detail: '临时不可用'}, 503);
    await Promise.allSettled([m.load(3)]);
    assert.match(m.state.recommendationError?.value || '', /临时不可用/);
    m.get = async () => response(envelope([item(30)], 3, 30));
    await m.state.retryRecommendationItems();
    assert.equal(new URL(m.gets.at(-1).url, 'https://unit.invalid').searchParams.get('page'), '3');
    assert.equal(m.state.recommendationError.value, '');
    assert.equal(m.state.recommendationItems.value[0].id, 30);
  });

  for (const failure of ['network', 'application', 'malformed']) {
    test(`${name}: ${failure} failure is not a genuine empty result`, async () => {
      const m = mount(name);
      m.get = async () => {
        if (failure === 'network') throw new Error('offline');
        if (failure === 'application') return response({ok: false, detail: '稍后再试'});
        return response({ok: true, data: []});
      };
      await Promise.allSettled([m.load(1)]);
      assert(m.state.recommendationError?.value, 'Show an error, not the empty-state message');
      assert.equal(m.state.loadingRecommendations.value, false);
    });
  }

  test(`${name}: a real empty filtered envelope remains page one without errors`, async () => {
    const m = mount(name);
    await m.load(1);
    assert.equal(m.state.recommendationItems.value.length, 0);
    assert.equal(m.state.recommendationPage?.value, 1);
    assert.equal(m.state.recommendationTotalPages?.value, 1);
    assert.equal(m.state.recommendationTotal?.value, 0);
    assert.equal(m.state.recommendationError.value, '');
    assert.equal(m.state.loadingRecommendations.value, false);
  });

  test(`${name}: feedback has a per-item busy guard and only posts through csrfFetch`, async () => {
    const m = mount(name), pending = deferred(), value = item(1);
    m.post = () => pending.promise;
    const first = m.feedback(value, 'interested'), duplicate = m.feedback(value, 'dismissed');
    const count = m.writes.length, busy = m.state.feedbackBusy?.value[value.id];
    pending.resolve(response({ok: true, data: null}));
    await Promise.allSettled([first, duplicate]);
    assert.equal(count, 1, 'Do not record duplicate/contradictory feedback while busy');
    assert.equal(busy, true);
    assert.equal(Boolean(m.state.feedbackBusy.value[value.id]), false);
    assert.equal(m.writes[0].options.method, 'POST');
    assert.equal(JSON.parse(m.writes[0].options.body).feedback_type, 'interested');
    assert.equal(m.gets.some(call => call.url.endsWith('/feedback')), false);
  });

  for (const failure of ['network', 'http', 'application']) {
    test(`${name}: ${failure} feedback failure stays visible, preserves the item, and can retry`, async () => {
      const m = mount(name), value = item(1);
      m.state.recommendationItems.value = [value];
      m.post = async () => {
        if (failure === 'network') throw new Error('offline');
        return response({ok: false, detail: '反馈被拒绝'}, failure === 'http' ? 403 : 200);
      };
      await Promise.allSettled([m.feedback(value, 'interested')]);
      assert(m.state.feedbackErrors?.value[value.id], 'Failure must persist next to the card');
      assert.equal(Boolean(m.state.feedbackBusy.value[value.id]), false);
      assert.equal(value.status, 'new', 'No optimistic success on a failed POST');
      assert.equal(m.gets.length, 0, 'Failed feedback must not pretend to refresh successfully');
      m.post = async () => response({ok: true, data: null});
      await m.feedback(value, 'interested');
      assert.equal(m.writes.length, 2);
      assert.equal(Boolean(m.state.feedbackErrors.value[value.id]), false);
    });
  }

  test(`${name}: feedback removing the final page adopts the server's valid page`, async () => {
    const m = mount(name), rows = Array.from({length: 21}, (_, i) => item(i + 1));
    serveItems(m, rows);
    await m.load(3);
    m.post = async (_url, options) => {
      rows.at(-1).status = JSON.parse(options.body).feedback_type;
      return response({ok: true, data: null});
    };
    await m.feedback(rows.at(-1), 'interested');
    assert.equal(m.state.recommendationPage?.value, 2);
    assert.equal(m.state.recommendationTotalPages.value, 2);
    assert.equal(m.state.recommendationItems.value.length, 10);
    assert.equal(m.state.recommendationTotal.value, 20);
  });

  test(`${name}: late feedback refresh respects the user's newest requested page`, async () => {
    const m = mount(name), post = deferred(), oldRead = deferred();
    serveItems(m, Array.from({length: 30}, (_, i) => item(i + 1)));
    await m.load(3);
    m.post = () => post.promise;
    const saving = m.feedback(item(30), 'interested');
    let calls = 0;
    m.get = () => calls++ ? response(envelope([item(12)], 2, 20)) : oldRead.promise;
    const navigating = m.load(2);
    post.resolve(response({ok: true, data: null}));
    await saving;
    oldRead.resolve(response(envelope([item(11)], 2, 30)));
    await navigating;
    assert.equal(new URL(m.gets.at(-1).url, 'https://unit.invalid').searchParams.get('page'), '2');
    assert.equal(m.state.recommendationPage?.value, 2);
    assert.equal(m.state.recommendationItems.value[0].id, 12);
  });

  test(`${name}: a successful POST followed by a failed GET is not a failed feedback POST`, async () => {
    const m = mount(name), value = item(1);
    m.get = async () => response({ok: false, detail: '刷新失败'}, 503);
    await m.feedback(value, 'interested');
    assert.equal(m.writes.length, 1);
    assert.equal(Boolean(m.state.feedbackErrors?.value[value.id]), false);
    assert.match(m.state.recommendationError?.value || '', /刷新失败/);
    assert.match(m.state.feedbackNotice?.value || '', /已/);
  });

  test(`${name}: unmount prevents late responses from updating page state`, async () => {
    const m = mount(name), pending = deferred();
    m.get = () => pending.promise;
    const loading = m.load(1);
    m.stop();
    pending.resolve(response(envelope([item(1)])));
    await loading;
    assert.equal(m.state.recommendationItems.value.length, 0);
  });
}

test('F3: initial profile errors persist beyond the old 3.5-second message timeout', async () => {
  const m = mount('dashboard_preferences.html');
  m.get = async url => url.includes('/preferences/profiles')
    ? response({ok: false, detail: '画像读取失败'}, 503)
    : response(envelope([item(8)]));
  await m.start();
  const visibleError = () => m.state.profileError?.value ?? m.state.message.value;
  assert.match(visibleError(), /画像读取失败/);
  await m.advanceTime(5000);
  assert.match(visibleError(), /画像读取失败/, 'A closed disclosure must not silently lose the initial error');
  assert.equal(m.state.pageLoading.value, false);
  assert.equal(m.state.recommendationItems.value[0].id, 8);
  assert.equal(m.writes.length, 0);
});

test('F3: profile retry is read-only, guarded while busy, and retains errors until success', async () => {
  const m = mount('dashboard_preferences.html'), pending = deferred();
  m.get = async url => url.includes('/preferences/profiles')
    ? response({ok: false, detail: '画像初次读取失败'}, 503)
    : response(envelope([item(8)]));
  await m.start();
  const visibleError = () => m.state.profileError?.value ?? m.state.message.value;
  const profileReads = () => m.gets.filter(call => call.url === '/api/dashboard/preferences/profiles').length;
  m.get = () => pending.promise;
  const retrying = m.retryProfiles(), duplicate = m.retryProfiles();
  const busy = m.state.pageLoading.value, duringRetry = visibleError(), reads = profileReads();
  pending.resolve(response({ok: false, detail: '画像仍不可用'}));
  await Promise.allSettled([retrying, duplicate]);
  assert.equal(reads, 2, 'Only one additional GET may run while retry is busy');
  assert.equal(busy, true);
  assert.match(duringRetry, /画像初次读取失败/);
  assert.match(visibleError(), /画像仍不可用/);
  await m.advanceTime(10000);
  assert.match(visibleError(), /画像仍不可用/);
  m.get = async () => response({ok: true, data: [{id: 9, name: '已有画像', is_default: true, stats: {}, profile: {}}]});
  await m.retryProfiles();
  assert.equal(visibleError(), '');
  assert.equal(m.state.pageLoading.value, false);
  assert.equal(m.state.selected.value.id, 9);
  assert.equal(profileReads(), 3);
  assert.equal(m.gets.filter(call => call.url.includes('/recommendations/items')).length, 1);
  assert.equal(m.state.recommendationItems.value[0].id, 8);
  assert.equal(m.writes.length, 0, 'Retry must not trigger analysis, generation or feedback');
  assert.equal(m.scrolls.length, 0);
  assert.equal(m.focuses.length, 0);
});

test('F3: an initial profile network failure stays visible until a genuine empty success', async () => {
  const m = mount('dashboard_preferences.html');
  m.get = async url => {
    if (url.includes('/preferences/profiles')) throw new Error('profile offline');
    return response(envelope([item(8)]));
  };
  await m.start();
  await m.advanceTime(5000);
  assert.match(m.state.profileError?.value ?? m.state.message.value, /profile offline/);
  m.get = async () => response({ok: true, data: []});
  await m.retryProfiles();
  assert.equal(m.state.profileError.value, '');
  assert.equal(m.state.profiles.value.length, 0);
  assert.equal(m.state.selected.value, null);
  assert.equal(m.state.pageLoading.value, false);
  assert.equal(m.writes.length, 0);
});

test('preferences load recommendations independently of a slow or failed profile request', async () => {
  const m = mount('dashboard_preferences.html'), profiles = deferred();
  m.get = url => url.includes('/preferences/profiles') ? profiles.promise : response(envelope([item(8)]));
  await m.start();
  const startedRecommendations = m.gets.some(call => call.url.includes('/recommendations/items'));
  profiles.reject(new Error('profile offline'));
  await tick();
  assert.equal(startedRecommendations, true);
  assert.equal(m.state.recommendationItems.value[0].id, 8);
});

test('homepage retains scheduler toggling, task labels/progress and manual versus scheduled stop', async () => {
  const m = mount('dashboard.html');
  m.get = async () => response({running: true});
  await m.state.toggleAutoSync();
  assert.equal(m.writes[0].url, '/api/dashboard/auto-sync/toggle');
  assert.equal(JSON.parse(m.writes[0].options.body).enabled, true);
  assert.equal(m.state.autoSyncStatus.value.running, true);
  assert.equal(m.state.togglingAutoSync.value, false);
  m.state.latestJob.value = {
    job_id: 'job & one', status: 'running', is_auto_sync: false, task_list: ['bookmark'],
    progress: {current: 2, total: 4, phase: '同步中'},
  };
  assert.equal(m.state.currentTaskName.value, '收藏同步');
  assert.equal(m.state.progressPercent.value, 50);
  assert.match(m.state.currentTaskLine.value, /收藏同步.*同步中.*2\/4/);
  await m.state.stopAutoTask();
  assert.equal(m.writes[1].url, '/api/dashboard/sync/cancel?job_id=job%20%26%20one');
  m.state.latestJob.value.is_auto_sync = true;
  await m.state.stopAutoTask();
  assert.equal(m.writes[2].url, '/api/dashboard/auto-sync/stop-task');
  assert(m.writes.every(call => call.options.method === 'POST'));
});

test('preferences preserve incremental analysis parameters, profile selection and progress', async () => {
  const m = mount('dashboard_preferences.html');
  const profile = {id: 9, name: '保留的画像名称', is_default: true, stats: {novel_count: 12}, profile: {}};
  m.post = async () => response({ok: true, data: {job_id: 'analysis-test'}});
  m.get = async url => response(url.includes('/preferences/profiles')
    ? {ok: true, data: [profile]}
    : {job: {status: 'succeeded', stats: {processed_this_run: 2, analyzed_total: 12, remaining: 0}}});
  await m.state.analyze();
  assert.equal(m.writes[0].url, '/api/dashboard/preferences/profiles/analyze');
  const payload = JSON.parse(m.writes[0].options.body);
  assert.deepEqual(payload, {is_default: true, scope: {min_text_length: 1000, max_batches: 10}});
  assert.equal(m.state.selected.value.name, '保留的画像名称');
  assert.equal(m.state.analysisResult.value.analyzed_total, 12);
  assert.equal(m.state.loading.value, false);
});

test('preferences preserve search-plan limits, run completion, saved count and result refresh', async () => {
  const m = mount('dashboard_preferences.html');
  m.state.selected.value = {id: 9};
  const plan = {queries: [{query: '测试关键词'}], filters: {}};
  m.post = async url => response({ok: true, data: url.endsWith('/search-plan') ? plan : {job_id: 'recommendation-test'}});
  m.get = async url => response(url.includes('/sync/status')
    ? {job: {status: 'succeeded', stats: {stats: {saved: 2}}}}
    : envelope([item(1), item(2)]));
  await m.state.runRecommendations();
  assert.equal(m.writes[0].url, '/api/dashboard/recommendations/search-plan');
  assert.deepEqual(JSON.parse(m.writes[0].options.body), {profile_id: 9, filters: {max_queries: 12, per_query_limit: 20}});
  assert.equal(m.writes[1].url, '/api/dashboard/recommendations/run');
  assert.deepEqual(JSON.parse(m.writes[1].options.body), {profile_id: 9, search_plan: plan});
  assert.match(m.state.message.value, /保存 2 条/);
  assert.equal(m.state.recommendationItems.value.length, 2);
  assert.equal(m.state.recommendationLoading.value, false);
});

test('preferences keep author mute confirmation and do not send after cancellation', async () => {
  const m = mount('dashboard_preferences.html');
  m.confirm = () => false;
  await m.mute(item(1));
  assert.equal(m.confirms.length, 1);
  assert.equal(m.writes.length, 0);
});

test('preferences mute failure does not post feedback or claim success', async () => {
  const m = mount('dashboard_preferences.html'), value = item(1);
  m.post = async () => response({ok: false, detail: '不能屏蔽'}, 503);
  await m.mute(value);
  assert.equal(m.writes.length, 1);
  assert.match(m.state.feedbackErrors?.value[value.id] || '', /不能屏蔽/);
  assert.equal(m.gets.length, 0);
});

test('preferences author mute and its feedback share a busy lock; partial failure is explicit', async () => {
  const m = mount('dashboard_preferences.html'), pending = deferred(), value = item(1);
  m.post = url => url.endsWith('/mutes') ? pending.promise : response({ok: false, detail: '状态更新失败'}, 503);
  const muting = m.mute(value), duplicate = m.feedback(value, 'interested');
  const count = m.writes.length;
  pending.resolve(response({ok: true, data: {id: 1}}));
  await Promise.allSettled([muting, duplicate]);
  assert.equal(count, 1);
  assert.equal(m.writes.length, 2);
  assert.equal(JSON.parse(m.writes[0].options.body).mute_type, 'author');
  assert.equal(JSON.parse(m.writes[1].options.body).feedback_type, 'muted');
  assert.match(m.state.feedbackErrors?.value[value.id] || '', /作者已屏蔽.*状态更新失败/);
  assert.equal(m.notices.some(notice => notice[0] === '已屏蔽作者'), false);
});

function authorMuteRows() {
  return Array.from({length: 22}, (_, i) => ({...item(i + 1), author_id: [2, 21, 22].includes(i + 1) ? 7 : 99}));
}

function applyAuthorMute(rows, authorId) {
  for (const row of rows) {
    if (row.author_id === authorId && row.status === 'new') row.status = 'muted';
  }
}

for (const failure of ['http', 'network']) {
  test(`F1: confirmed mute followed by ${failure} feedback failure reconciles all author rows and the valid page`, async () => {
    const m = mount('dashboard_preferences.html'), rows = authorMuteRows();
    serveItems(m, rows);
    await m.load(3);
    const value = m.state.recommendationItems.value[0];
    assert.equal(value.id, 21);
    m.post = async url => {
      if (url.endsWith('/mutes')) {
        applyAuthorMute(rows, value.author_id);
        return response({ok: true, data: {id: 1}});
      }
      if (failure === 'network') throw new Error('feedback offline');
      return response({ok: false, detail: '反馈记录失败'}, 503);
    };
    await m.mute(value);
    assert.deepEqual(Array.from(m.state.recommendationItems.value, row => row.id), [12, 13, 14, 15, 16, 17, 18, 19, 20]);
    assert.equal(m.state.recommendationPage.value, 2);
    assert.equal(m.state.recommendationTotalPages.value, 2);
    assert.equal(m.state.recommendationTotal.value, 19);
    assert.equal(m.gets.length, 2, 'A confirmed server mutation needs a new authoritative snapshot');
    assert.equal(m.writes.length, 2, 'Do not repeat either POST');
    assert.equal(m.state.feedbackNoticeType.value, 'error');
    assert.match(m.state.feedbackNotice.value, /作者已屏蔽.*状态更新失败/);
    assert.equal(m.state.recommendationError.value, '');
  });
}

test('F1: partial-mute reconciliation keeps its busy lock and a failed GET is read-only retryable', async () => {
  const m = mount('dashboard_preferences.html'), rows = authorMuteRows(), pending = deferred();
  serveItems(m, rows);
  const read = m.get;
  await m.load(3);
  const value = m.state.recommendationItems.value[0];
  m.get = () => pending.promise;
  m.post = async url => {
    if (url.endsWith('/mutes')) {
      applyAuthorMute(rows, value.author_id);
      return response({ok: true, data: {id: 1}});
    }
    return response({ok: false, detail: '反馈记录失败'}, 503);
  };
  const muting = m.mute(value);
  await tick();
  const busy = m.state.feedbackBusy.value[value.id], loading = m.state.loadingRecommendations.value;
  pending.resolve(response({ok: false, detail: '列表重载失败'}, 503));
  await muting;
  assert.equal(busy, true, 'Keep actions locked until reconciliation finishes');
  assert.equal(loading, true);
  assert.match(m.state.recommendationError.value, /列表重载失败/);
  assert.match(m.state.feedbackNotice.value, /作者已屏蔽.*状态更新失败/);
  assert.equal(Boolean(m.state.feedbackBusy.value[value.id]), false);
  m.get = read;
  await m.state.retryRecommendationItems();
  assert.equal(m.writes.length, 2);
  assert.equal(m.gets.length, 3);
  assert.equal(m.state.recommendationPage.value, 2);
  assert.equal(m.state.recommendationTotal.value, 19);
  assert.equal(m.state.recommendationError.value, '');
  assert.match(m.state.feedbackNotice.value, /作者已屏蔽.*状态更新失败/);
});

test('F1: partial-mute reconciliation uses the latest requested page/filter and rejects the superseded read', async () => {
  const m = mount('dashboard_preferences.html'), rows = authorMuteRows(), feedback = deferred(), oldRead = deferred();
  rows.push(...Array.from({length: 10}, (_, i) => ({...item(i + 23, 'saved'), author_id: 99})));
  serveItems(m, rows);
  await m.load(3);
  const value = m.state.recommendationItems.value[0];
  m.post = async url => {
    if (url.endsWith('/mutes')) {
      applyAuthorMute(rows, value.author_id);
      return response({ok: true, data: {id: 1}});
    }
    return feedback.promise;
  };
  const muting = m.mute(value);
  await tick();
  m.state.hideFeedback.value = false;
  await tick();
  const read = m.get;
  let calls = 0;
  m.get = url => calls++ ? read(url) : oldRead.promise;
  const navigating = m.load(2);
  feedback.resolve(response({ok: false, detail: '反馈记录失败'}, 503));
  await muting;
  oldRead.resolve(response(envelope([item(999)], 2, 40)));
  await navigating;
  const query = new URL(m.gets.at(-1).url, 'https://unit.invalid').searchParams;
  assert.equal(query.get('page'), '2');
  assert.equal(query.has('status'), false);
  assert.equal(m.state.recommendationPage.value, 2);
  assert.equal(m.state.recommendationTotal.value, 29);
  assert.deepEqual(Array.from(m.state.recommendationItems.value, row => row.id), [12, 13, 14, 15, 16, 17, 18, 19, 20, 23]);
  assert.equal(m.writes.length, 2);
});

for (const name of pages) {
  for (const outcome of ['success', 'failure']) {
    test(`F2: ${name} explicit paging scrolls/focuses immediately, never after ${outcome}`, async () => {
      const m = mount(name), pending = deferred();
      serveItems(m, Array.from({length: 30}, (_, i) => item(i + 1)));
      await m.load(1);
      m.get = () => pending.promise;
      const changing = m.changePage(2);
      const immediateScrolls = m.scrolls.length, immediateFocus = m.focused;
      m.window.scrollY = 1777; // The user moves again while the request is pending.
      pending.resolve(outcome === 'success'
        ? response(envelope(Array.from({length: 10}, (_, i) => item(i + 11)), 2, 30))
        : response({ok: false, detail: '分页失败'}, 503));
      await changing;
      assert.equal(immediateScrolls, 1, 'Move to the stable results heading before the response arrives');
      assert.equal(immediateFocus, m.heading, 'The old pagination button will unmount; retain a meaningful keyboard focus');
      assert.deepEqual(m.focuses, [{preventScroll: true}]);
      assert.deepEqual(m.scrolls, [{behavior: 'instant', block: 'start'}]);
      assert.equal(m.window.scrollY, 1777, 'Completion must not steal a later user scroll');
      assert.equal(m.state.recommendationPage.value, 2);
    });
  }

  test(`F2: ${name} invalid or unchanged explicit pages do not fetch, focus or scroll`, async () => {
    const m = mount(name);
    serveItems(m, Array.from({length: 30}, (_, i) => item(i + 1)));
    await m.load(1);
    const reads = m.gets.length;
    for (const page of [1, 0, -1, 1.5, 4, NaN, Infinity, '2', null, undefined]) await m.changePage(page);
    assert.equal(m.gets.length, reads);
    assert.equal(m.state.recommendationPage.value, 1);
    assert.equal(m.scrolls.length, 0);
    assert.equal(m.focuses.length, 0);
  });

  test(`F2: ${name} refresh, retry, filter changes and feedback never move the viewport/focus`, async () => {
    const m = mount(name);
    serveItems(m, Array.from({length: 30}, (_, i) => item(i + 1)));
    const read = m.get;
    await m.load(2);
    m.get = async () => response({ok: false, detail: '请重试'}, 503);
    await m.load(2);
    m.get = read;
    await m.state.retryRecommendationItems();
    m.state.hideFeedback.value = false;
    await tick();
    await m.feedback(m.state.recommendationItems.value[0], 'interested');
    assert.equal(m.scrolls.length, 0);
    assert.equal(m.focuses.length, 0);
    assert.equal(m.window.scrollY, 3062);
  });

  test(`F2: ${name} rapid explicit page choices retain the latest selection without completion scrolling`, async () => {
    const m = mount(name), old = deferred(), current = deferred();
    serveItems(m, Array.from({length: 30}, (_, i) => item(i + 1)));
    await m.load(1);
    let calls = 0;
    m.get = () => calls++ ? current.promise : old.promise;
    const earlier = m.changePage(2), later = m.changePage(3);
    const immediateScrolls = m.scrolls.length;
    m.window.scrollY = 888;
    current.resolve(response(envelope(Array.from({length: 10}, (_, i) => item(i + 21)), 3, 30)));
    await later;
    old.resolve(response(envelope(Array.from({length: 10}, (_, i) => item(i + 11)), 2, 30)));
    await earlier;
    assert.equal(immediateScrolls, 2);
    assert.equal(m.state.recommendationPage.value, 3);
    assert.equal(m.state.recommendationItems.value[0].id, 21);
    assert.equal(m.scrolls.length, 2);
    assert.equal(m.focuses.length, 2);
    assert.equal(m.window.scrollY, 888);
  });

  test(`F2: ${name} a missing heading does not block paging and unmount prevents new actions`, async () => {
    const m = mount(name);
    serveItems(m, Array.from({length: 30}, (_, i) => item(i + 1)));
    await m.load(1);
    const heading = m.heading;
    m.heading = null;
    await m.changePage(2);
    assert.equal(m.state.recommendationPage.value, 2);
    const reads = m.gets.length;
    m.heading = heading;
    m.stop();
    await m.changePage(3);
    assert.equal(m.gets.length, reads);
    assert.equal(m.scrolls.length, 0);
    assert.equal(m.focuses.length, 0);
  });
}

test('the shared card only displays/expands/emits; no requests and no feedback while busy', () => {
  const m = mount('dashboard.html'), emitted = [];
  const card = m.components.get('recommendation-card');
  assert(card, 'registerPageComponents must register recommendation-card before mount');
  const props = {item: item(1), busy: false, error: '', allowMute: true};
  const state = card.setup(props, {emit: (...args) => emitted.push(args)});
  assert.match(state.itemUrl.value, /^https:\/\/www\.pixiv\.net\/novel\/show\.php\?id=1$/);
  assert.equal(state.reasonExpanded.value, false);
  state.toggleReason();
  assert.equal(state.reasonExpanded.value, true);
  state.sendFeedback('interested');
  state.muteAuthor();
  assert.deepEqual(emitted, [['feedback', 'interested'], ['mute-author']]);
  props.busy = true;
  state.sendFeedback('dismissed');
  state.muteAuthor();
  assert.equal(emitted.length, 2);
  assert.equal(m.gets.length + m.writes.length, 0);
});

test('the shared card retains Pixiv novel/series URLs, length/time and safe URL fallbacks', () => {
  const m = mount('dashboard.html'), card = m.components.get('recommendation-card');
  assert(card, 'Shared card registration is required');
  const props = {item: {...item(1), item_type: 'series', series_id: 42, source_url: ''}, busy: false};
  const state = card.setup(props, {emit() {}});
  assert.equal(state.itemUrl.value, 'https://www.pixiv.net/novel/series/42');
  props.item = {...item(9), source_url: ''};
  assert.equal(state.itemUrl.value, 'https://www.pixiv.net/novel/show.php?id=9');
  assert.equal(state.lengthText.value, '1.5 万字');
  assert.equal(state.timeText.value, m.window.formatDbTime(props.item.created_at, {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }));
  props.item = {...item(9), novel_id: null, source_url: 'javascript:alert(1)'};
  assert.equal(state.itemUrl.value, '');
  props.item.source_url = 'https://www.pixiv.net/novel/show.php?id=123';
  assert.equal(state.itemUrl.value, props.item.source_url);
});

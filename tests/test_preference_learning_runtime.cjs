const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const BASE = '/api/dashboard/preference-learning';
const RUN = 'a'.repeat(32), BATCH = 'b'.repeat(32), REPORT = 'c'.repeat(32), HASH = 'd'.repeat(64);
const clone = value => JSON.parse(JSON.stringify(value));
const budget = {request_limit: 4, token_limit: 100000, active_seconds_limit: 300};
const totalBudget = {request_limit: 40, token_limit: 1000000, active_seconds_limit: 3000};
const agent = {kind: 'agent', agent_id: 7, label: 'Fixture Agent', availability: 'ready'};
const later = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
function profile(overrides = {}) {
  return {run_id: RUN, version: 1, status: 'queued', active: false, stale: false, agent_id: 7,
    budget: clone(budget), source_count: 2, created_at: '2026-01-01T00:00:00Z', ...overrides};
}
function batch(overrides = {}) {
  return {batch_id: BATCH, version: 1, status: 'queued', origin: 'manual', scope: 'all', work_limit: 10,
    work_count: 0, scanned_count: 0, skipped_count: 0, budget: clone(totalBudget),
    allocated: {request_limit: 0, token_limit: 0, active_seconds_limit: 0}, ...overrides};
}
function preview(kind, body) {
  const common = {preview_fingerprint: HASH, can_create: true, warnings: [],
    targets: [{provider_id: 3, provider_name: '<img src=x>', model_key: 'fixture/model '}]};
  return kind === 'profile' ? {...common, budget: body.budget, source_count: 2, missing_reports: 1,
    minimum_budget: {request_limit: 1, token_limit: 1000, active_seconds_limit: 30}}
    : {...common, configuration: body, candidate_count: 3, samples: [{source_kind: 'novel', source_id: 101, title: 'Synthetic'}]};
}
function fixture() {
  const calls = [], watchers = [], unmounted = [], timers = new Map(), events = new Map();
  let handle, nonce = 0, timerId = 0;
  const server = {profile: profile(), batch: batch(), feedback: {source_kind: 'novel', source_id: 101, reaction: 'clear', note: '', version: 0},
    policy: {version: 4, enabled: false, configuration: null, work_count: 0,
      allocated: {request_limit: 0, token_limit: 0, active_seconds_limit: 0}}};
  const Vue = {
    reactive: value => value, computed: get => ({get value() { return get(); }}), onUnmounted: fn => unmounted.push(fn),
    watch(get, callback) {
      const entry = {get, callback, previous: JSON.stringify(get())}; watchers.push(entry);
      return () => { const index = watchers.indexOf(entry); if (index >= 0) watchers.splice(index, 1); };
    },
  };
  const root = {document: {hidden: false}, crypto: {getRandomValues: bytes => bytes.fill(++nonce)},
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; }, clearTimeout: id => timers.delete(id),
    addEventListener(name, fn) { if (!events.has(name)) events.set(name, new Set()); events.get(name).add(fn); },
    removeEventListener(name, fn) { events.get(name)?.delete(fn); },
    csrfFetch: () => { throw new Error('Unconfigured HTTP call'); }};
  const context = vm.createContext({window: root, AbortController, Uint8Array, URLSearchParams});
  const template = path.resolve(__dirname, '../src/pixiv_novel_sync/templates/preference_feedback_scripts.html');
  vm.runInContext(fs.readFileSync(template, 'utf8').match(/<script[^>]*>([\s\S]*?)<\/script>/)[1], context, {filename: template});
  const script = path.resolve(__dirname, '../src/pixiv_novel_sync/static/js/preference_learning.js');
  vm.runInContext(fs.readFileSync(script, 'utf8'), context, {filename: script});
  const state = () => ({profiles: {feedback_counts: {liked: 2, disliked: 1, neutral: 0}, eligible_works: 2,
    missing_reports: 1, active_run_id: server.profile.active ? RUN : null, runs: [server.profile]},
    library: {policy: server.policy, batches: [server.batch]}});
  const answer = async (url, options = {}) => {
    const method = options.method || 'GET', body = options.body ? JSON.parse(options.body) : null;
    if (url === '/api/dashboard/ai/agents') return [{id: 7, name: 'Fixture Agent', enabled: true}];
    if (url === BASE + '/state') return state();
    if (url === BASE + '/profile-preview') return preview('profile', body);
    if (url === BASE + '/batch-preview') return preview('library', body);
    if (url === BASE + '/profiles' && method === 'POST') return server.profile;
    if (url === BASE + '/profiles/' + RUN && method === 'GET') return server.profile;
    if (url.startsWith(BASE + '/profiles/' + RUN)) {
      server.profile = {...server.profile, version: server.profile.version + 1,
        ...(method === 'DELETE' ? {status: 'deleted', active: false, result: undefined} :
          url.endsWith('/activate') ? {active: true} : {status: 'cancelled'})};
      return server.profile;
    }
    if (url === BASE + '/batches' && method === 'POST') return server.batch;
    if (url.startsWith(BASE + '/batches/' + BATCH) && method === 'GET') {
      const offset = Number(new URL(url, 'https://fixture.invalid').searchParams.get('offset') || 0);
      return {batch: server.batch, items: [], total: 40, offset, limit: 20};
    }
    if (url.startsWith(BASE + '/batches/' + BATCH) && method === 'POST') {
      server.batch = {...server.batch, version: server.batch.version + 1,
        status: url.endsWith('/pause') ? 'paused' : url.endsWith('/resume') ? 'queued' : 'cancelled'};
      return server.batch;
    }
    if (url === BASE + '/automation') {
      server.policy = {...server.policy, version: server.policy.version + 1, enabled: body.enabled,
        configuration: body.enabled ? body : server.policy.configuration};
      return server.policy;
    }
    if (url.startsWith(BASE + '/feedback/')) {
      if (method === 'PUT') server.feedback = {...server.feedback, reaction: body.reaction, note: body.note, version: server.feedback.version + 1};
      return server.feedback;
    }
    throw new Error('Unexpected synthetic route: ' + url);
  };
  handle = answer;
  const send = async (url, options = {}) => {
    calls.push({url, method: options.method || 'GET', body: options.body, signal: options.signal});
    return clone(await handle(url, options));
  };
  const controller = root.PreferenceLearning.createController(Vue, send);
  const flush = () => {
    for (let pass = 0; pass < 5; pass++) {
      for (const entry of [...watchers]) {
        const next = JSON.stringify(entry.get());
        if (next !== entry.previous) { entry.previous = next; entry.callback(); }
      }
    }
  };
  return {controller, root, Vue, server, calls, timers, events, state, answer, send, flush,
    handle(fn) { handle = fn; }, writes: () => calls.filter(row => row.method !== 'GET'),
    feedback: () => root.PreferenceFeedback.createController(Vue, 'novel', 101, send)};
}
async function ready(f, kind = 'profile') {
  const c = f.controller;
  c.pl.agents = [clone(agent)]; c.chooseAgent(kind, agent);
  if (kind === 'profile') c.pl.form.profile.budget = clone(budget);
  else Object.assign(c.pl.form.library, {work_budget: clone(budget), batch_budget: clone(totalBudget), work_limit: 10});
  f.flush(); await c.preview(kind);
  for (const key of Object.keys(c.pl.confirmations[kind])) c.pl.confirmations[kind][key] = true;
  assert.equal(c.canCreate(kind), true);
}

test('mount reads only metadata and agents; spend budgets and feedback stay untouched', async () => {
  const f = fixture(); await f.controller.mount();
  assert.deepEqual(f.calls.map(row => row.url).sort(), ['/api/dashboard/ai/agents', BASE + '/state'].sort());
  assert.deepEqual(clone(f.controller.pl.form.profile.budget), {request_limit: '', token_limit: '', active_seconds_limit: ''});
  assert.ok(Object.values(f.controller.pl.form.library.batch_budget).every(value => value === ''));
  assert.equal(f.writes().length, 0);
  assert.equal(f.controller.feedback.f.record, null);
});

test('an explicit recommendation source link loads that profile while keeping mutations separate', async () => {
  const f = fixture(); f.root.location = {search: '?view=learning&profile_run_id=' + RUN};
  await f.controller.mount();
  assert.equal(f.controller.pl.selectedProfileId, RUN);
  assert.ok(f.calls.some(row => row.url === BASE + '/profiles/' + RUN));
  assert.equal(f.writes().length, 0);
});

test('invalid finite budgets cannot reach preview or create method entry', async () => {
  for (const value of ['', true, '1.5', '-1', 'Infinity', 513]) {
    const f = fixture(); f.controller.pl.agents = [clone(agent)]; f.controller.chooseAgent('profile', agent);
    f.controller.pl.form.profile.budget = {...budget, request_limit: value};
    assert.equal(await f.controller.preview('profile'), false);
    assert.equal(await f.controller.create('profile'), false);
    assert.equal(f.writes().length, 0);
  }
  const f = fixture(); await ready(f, 'library');
  f.controller.pl.form.library.batch_budget.request_limit = 3; f.flush();
  assert.equal(await f.controller.preview('library'), false);
  assert.match(f.controller.pl.writeError, /至少一个作品/);
});

test('draft changes revoke preview and all spend confirmations at buttons and methods', async () => {
  for (const kind of ['profile', 'library']) {
    const f = fixture(); await ready(f, kind);
    f.controller.pl.form[kind].output_tokens = '4001'; f.flush();
    assert.equal(f.controller.pl.previews[kind], null);
    assert.ok(Object.values(f.controller.pl.confirmations[kind]).every(value => value === false));
    assert.equal(f.controller.canCreate(kind), false);
    assert.equal(await f.controller.create(kind), false);
  }
});

test('removed current Agent stays unavailable and never selects another Agent', async () => {
  const f = fixture(); await ready(f);
  f.handle((url, options) => url === '/api/dashboard/ai/agents' ? [{id: 8, name: 'Other', enabled: true}] : f.answer(url, options));
  await f.controller.reloadAgents();
  assert.equal(f.controller.currentAgent('profile').agent_id, 7);
  assert.equal(f.controller.currentAgent('profile').availability, 'unavailable');
  assert.equal(f.controller.canCreate('profile'), false);
});

test('late preview failure cannot overwrite a newer preview owner', async () => {
  const f = fixture(); await ready(f);
  const first = later(), second = later(); let count = 0;
  f.handle((url, options) => url === BASE + '/profile-preview' ? (++count === 1 ? first.promise : second.promise) : f.answer(url, options));
  const old = f.controller.preview('profile'), latest = f.controller.preview('profile');
  first.reject(new Error('PRIVATE_OLD_RESPONSE')); await old;
  assert.equal(f.controller.pl.busy['preview:profile'], true);
  assert.equal(f.controller.pl.errors['preview:profile'], '');
  second.resolve(preview('profile', {budget})); await latest;
  assert.equal(f.controller.pl.previews.profile.preview_fingerprint, HASH);
});

test('double creation is locked and unknown creation retries the exact idempotent body', async () => {
  const f = fixture(); await ready(f); const held = later(); let count = 0;
  f.handle((url, options) => url === BASE + '/profiles' ? (++count === 1 ? held.promise : f.server.profile) : f.answer(url, options));
  const sending = f.controller.create('profile');
  assert.equal(await f.controller.create('profile'), false);
  held.reject(new Error('transport lost')); await sending;
  assert.equal(f.controller.pl.pending.phase, 'unknown');
  f.controller.pl.form.profile.output_tokens = '5000'; f.flush();
  assert.equal(await f.controller.retryUnknown(), true);
  const writes = f.calls.filter(row => row.url === BASE + '/profiles');
  assert.equal(writes.length, 2); assert.equal(writes[0].body, writes[1].body);
  assert.match(JSON.parse(writes[0].body).idempotency_key, /^[0-9a-f]{32}$/);
  assert.equal(JSON.parse(writes[0].body).output_tokens, 4000);
});

test('known creation success with failed refresh never becomes an unknown write', async () => {
  const f = fixture(); await ready(f);
  f.handle((url, options) => url === BASE + '/state' ? Promise.reject(new Error('offline')) : f.answer(url, options));
  assert.equal(await f.controller.create('profile'), true);
  assert.equal(f.controller.pl.pending, null);
  assert.match(f.controller.pl.notice, /操作已确认，刷新失败/);
  assert.equal(await f.controller.retryUnknown(), false);
});

test('late sensitive profile detail cannot revive a deleted profile observed by metadata polling', async () => {
  const f = fixture(); f.server.profile = profile({status: 'succeeded', result: {summary: 'PRIVATE'}});
  await f.controller.selectProfile(RUN);
  const held = later(); f.handle((url, options) => url === BASE + '/profiles/' + RUN ? held.promise : f.answer(url, options));
  const stale = f.controller.loadProfile();
  f.server.profile = profile({status: 'deleted', version: 3}); await f.controller.refreshState();
  held.resolve(profile({status: 'succeeded', version: 2, result: {summary: 'PRIVATE'}})); await stale;
  assert.equal(f.controller.pl.profile.status, 'deleted');
  assert.equal(f.controller.pl.profile.result, undefined);
  assert.equal(f.controller.profileAllowed('activate'), false);
});

test('a selected profile omitted by metadata is cleared and its deleted identity is rechecked', async () => {
  const f = fixture(); f.server.profile = profile({status: 'succeeded', result: {summary: 'PRIVATE'}});
  await f.controller.selectProfile(RUN);
  const held = later();
  f.handle((url, options) => url === BASE + '/state' ? {...f.state(), profiles: {...f.state().profiles, runs: []}}
    : url === BASE + '/profiles/' + RUN ? held.promise : f.answer(url, options));
  const refreshing = f.controller.refreshState();
  for (let index = 0; index < 12; index++) await Promise.resolve();
  assert.equal(f.controller.pl.profile, null);
  held.resolve(profile({status: 'deleted', version: 2})); await refreshing;
  assert.equal(f.controller.pl.profile.status, 'deleted'); assert.equal(f.controller.pl.profile.result, undefined);
});

test('unavailable execution still allows local preview and stopping but blocks new paid work', async () => {
  const f = fixture(); await ready(f);
  f.handle((url, options) => url === BASE + '/state' ? {...f.state(), admission_ready: false} : f.answer(url, options));
  await f.controller.refreshState();
  assert.equal(f.controller.canCreate('profile'), false); assert.equal(await f.controller.create('profile'), false);
  await f.controller.selectBatch(BATCH); assert.equal(f.controller.batchAllowed('pause'), true);
  f.controller.pl.batch.status = 'paused'; assert.equal(f.controller.batchAllowed('resume'), false);
});

test('late activation receipt preserves a newer deleted state and cannot restore its result', async () => {
  const f = fixture(); f.server.profile = profile({status: 'succeeded', result: {summary: 'PRIVATE'}});
  await f.controller.selectProfile(RUN); f.controller.pl.activateConfirmed = true;
  const held = later(); f.handle((url, options) => url.endsWith('/activate') ? held.promise : f.answer(url, options));
  const activating = f.controller.controlProfile('activate');
  f.server.profile = profile({status: 'deleted', version: 3}); await f.controller.refreshState();
  held.resolve(profile({status: 'succeeded', active: true, version: 2})); await activating;
  assert.equal(f.controller.pl.profile.status, 'deleted');
  assert.equal(f.controller.pl.profile.result, undefined);
});

test('unknown CAS write requires reconciliation and rejects stale observations', async () => {
  const f = fixture(); await f.controller.selectProfile(RUN);
  f.handle((url, options) => url.endsWith('/cancel') ? Promise.reject(new Error('lost receipt')) : f.answer(url, options));
  await f.controller.controlProfile('cancel'); assert.equal(await f.controller.retryUnknown(), false);
  f.server.profile = profile({version: 3, status: 'cancelled'}); await f.controller.refreshState();
  f.handle((url, options) => url === BASE + '/profiles/' + RUN ? profile() : f.answer(url, options));
  await f.controller.reconcile();
  assert.equal(f.controller.pl.pending.canRetry, false); assert.match(f.controller.pl.notice, /较旧版本/);
  f.handle(f.answer); await f.controller.reconcile();
  assert.equal(f.controller.pl.pending, null); assert.match(f.controller.pl.notice, /原提交结果仍未知/);
});

test('automation uses one cumulative configuration and disabling sends no new spend authority', async () => {
  const f = fixture(); f.server.policy.work_count = 3;
  f.server.policy.allocated = {request_limit: 12, token_limit: 300000, active_seconds_limit: 900};
  await f.controller.refreshState(); f.controller.pl.form.library.mode = 'policy'; await ready(f, 'library');
  await f.controller.create('library');
  let operation = f.calls.find(row => row.url === BASE + '/automation');
  const body = JSON.parse(operation.body);
  assert.equal(body.enabled, true); assert.equal(body.expected_version, 4);
  assert.equal(body.purpose, undefined);
  assert.equal(JSON.parse(f.calls.find(row => row.url === BASE + '/batch-preview').body).purpose, 'policy');
  assert.equal(body.idempotency_key, undefined); assert.deepEqual(body.batch_budget, totalBudget);
  assert.equal(f.controller.pl.library.policy.allocated.request_limit, 12);
  assert.equal(f.controller.pl.library.policy.work_count, 3);
  await f.controller.disableAutomation(); operation = f.calls.filter(row => row.url === BASE + '/automation').at(-1);
  assert.deepEqual(JSON.parse(operation.body), {enabled: false, expected_version: 5});
  assert.equal(f.controller.pl.library.policy.allocated.request_limit, 12);
});

test('batch paging and controls use the selected batch and latest CAS version', async () => {
  const f = fixture(); await f.controller.selectBatch(BATCH); await f.controller.loadBatch(20);
  assert.equal(f.controller.pl.offset, 20);
  await f.controller.controlBatch('pause');
  const pause = f.calls.find(row => row.url.endsWith('/pause'));
  assert.deepEqual(JSON.parse(pause.body), {expected_version: 1});
  assert.equal(f.controller.pl.batch.status, 'paused');
  await f.controller.controlBatch('resume');
  assert.deepEqual(JSON.parse(f.calls.find(row => row.url.endsWith('/resume')).body), {expected_version: 2});
  f.controller.pl.batch.status = 'failed';
  assert.equal(await f.controller.controlBatch('resume'), false);
});

test('profile activation, deletion and stale profile use require explicit valid state', async () => {
  const f = fixture(); f.server.profile = profile({status: 'succeeded'}); await f.controller.selectProfile(RUN);
  assert.equal(await f.controller.controlProfile('activate'), false);
  assert.equal(await f.controller.controlProfile('delete'), false);
  f.controller.pl.activateConfirmed = true; f.controller.pl.profile.stale = true;
  assert.equal(await f.controller.controlProfile('activate'), false);
  f.controller.pl.deleteConfirmed = true; assert.equal(await f.controller.controlProfile('delete'), true);
  assert.equal(f.controller.pl.profile.status, 'deleted');
});

test('reading feedback makes no request until explicitly opened and uses observed version', async () => {
  const f = fixture(), feedback = f.feedback();
  assert.equal(f.calls.length, 0); assert.equal(await feedback.save(), false);
  await feedback.toggle(); assert.equal(f.calls.length, 1); assert.equal(f.calls[0].method, 'GET');
  feedback.f.reaction = 'liked'; feedback.f.note = '<img src=x> synthetic note';
  await feedback.save();
  const operation = f.calls.at(-1);
  assert.deepEqual(JSON.parse(operation.body), {reaction: 'liked', note: '<img src=x> synthetic note', expected_version: 0});
  assert.equal(feedback.f.record.version, 1);
});

test('feedback validation rejects unknown reactions and oversized notes, withdrawal clears note', async () => {
  const f = fixture(), feedback = f.feedback(); await feedback.load();
  feedback.f.reaction = 'not_a_reaction'; assert.equal(await feedback.save(), false);
  feedback.f.reaction = 'disliked'; feedback.f.note = 'x'.repeat(501); assert.equal(await feedback.save(), false);
  feedback.f.note = 'private reason'; await feedback.save(); await feedback.save(true);
  assert.deepEqual(JSON.parse(f.calls.at(-1).body), {reaction: 'clear', note: '', expected_version: 1});
});

test('feedback unknown write is locked and exact body can retry only after same-version read', async () => {
  const f = fixture(), feedback = f.feedback(); await feedback.load(); feedback.f.reaction = 'liked';
  const held = later(); let count = 0;
  f.handle((url, options) => options.method === 'PUT' && ++count === 1 ? held.promise : f.answer(url, options));
  const saving = feedback.save(); assert.equal(await feedback.save(), false);
  held.reject(new Error('lost response')); await saving; assert.equal(await feedback.retry(), false);
  await feedback.reconcile(); assert.equal(feedback.f.pending.canRetry, true);
  await feedback.retry(); const writes = f.writes(); assert.equal(writes.length, 2);
  assert.equal(writes[0].body, writes[1].body); assert.equal(feedback.f.record.reaction, 'liked');
});

test('feedback read ownership prevents old target notes appearing under a new target', async () => {
  const f = fixture(), feedback = f.feedback(), held = later();
  f.handle(() => held.promise); const loading = feedback.load();
  feedback.f.source_id = 102; f.flush(); held.resolve({...f.server.feedback, note: 'PRIVATE'}); await loading;
  assert.equal(feedback.f.record, null); assert.equal(feedback.f.note, ''); assert.equal(feedback.f.loading, false);
});

test('disposed controllers ignore late writes and stop timers and reads', async () => {
  const f = fixture(); await ready(f); const held = later();
  f.handle((url, options) => url === BASE + '/profiles' ? held.promise : f.answer(url, options));
  const creating = f.controller.create('profile'); f.controller.dispose();
  held.resolve(profile({status: 'succeeded', result: {summary: 'PRIVATE'}})); await creating;
  assert.equal(f.controller.pl.profile, null); assert.equal(f.timers.size, 0);
});

test('HTTP helper uses CSRF-aware no-store transport and never displays raw remote errors', async () => {
  const f = fixture(); let captured;
  f.root.csrfFetch = async (url, options) => {
    captured = {url, options}; return {ok: false, status: 500, json: async () => ({ok: false, error: 'PRIVATE', code: '<script>'})};
  };
  await assert.rejects(() => f.root.PreferenceLearning.request(BASE + '/state'), error => error.message === 'request_failed' && error.code === null);
  assert.equal(captured.options.cache, 'no-store'); assert.equal(captured.options.credentials, 'same-origin');
});

test('profile evidence links refer to the underlying report and bounded evidence identity', () => {
  const f = fixture(), source = {report_run_id: REPORT}, evidence = {novel_id: 101, evidence_id: HASH};
  assert.match(f.controller.profileSourceHref(source), new RegExp('analysis_id=' + REPORT + '$'));
  assert.equal(f.controller.profileEvidenceHref(source, evidence), '/dashboard/novels/101?analysis_id=' + REPORT + '&evidence_id=' + HASH + '#work-analysis-evidence');
});

function recommendationPage(answer) {
  let state;
  const calls = [], response = (data, ok = true) => ({ok, status: ok ? 200 : 503, json: async () => data});
  const window = {useRecommendations: () => ({loadRecommendationItems: async () => {}}),
    errorText: () => 'synthetic request failure', initVueApp: options => { state = options.setup(); },
    csrfFetch: async (url, options) => { calls.push({url, options}); return answer(url, options, response); }};
  const context = vm.createContext({window, Vue: {ref: value => ({value}), computed: get => ({get value() { return get(); }}), onMounted() {}},
    setTimeout: () => 0, fetch: async () => response({job: {status: 'succeeded', stats: {stats: {saved: 0}}}})});
  const file = path.resolve(__dirname, '../src/pixiv_novel_sync/templates/dashboard_preferences.html');
  const script = fs.readFileSync(file, 'utf8').match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
  vm.runInContext(script, context, {filename: file});
  state.selected.value = {id: 9}; state.searchPlan.value = {queries: [{query: 'STALE'}]};
  return {state, calls};
}

test('recommendation run replaces an existing cached plan with current learning signals', async () => {
  const latest = {queries: [{query: 'current preference'}], filters: {}};
  const page = recommendationPage((url, options, response) => response({ok: true,
    data: url.endsWith('/search-plan') ? latest : {job_id: 'fixture-job'}}));
  await page.state.runRecommendations();
  assert.equal(page.calls.length, 2); assert.match(page.calls[0].url, /search-plan$/);
  assert.deepEqual(JSON.parse(page.calls[1].options.body).search_plan, latest);
});

test('failed current search plan cannot submit a recommendation run using stale inputs', async () => {
  const page = recommendationPage((url, options, response) => response({ok: false}, false));
  await page.state.runRecommendations();
  assert.equal(page.calls.length, 1); assert.match(page.calls[0].url, /search-plan$/);
  assert.equal(page.state.searchPlan.value, null); assert.equal(page.state.recommendationLoading.value, false);
});

test('double recommendation action is locked while a fresh plan is being built', async () => {
  const held = later();
  const page = recommendationPage((url, options, response) => url.endsWith('/search-plan') ? held.promise : response({ok: true, data: {job_id: 'fixture-job'}}));
  const running = page.state.runRecommendations(); await page.state.runRecommendations();
  assert.equal(page.calls.length, 1);
  held.resolve({ok: true, json: async () => ({ok: true, data: {queries: []}})}); await running;
  assert.equal(page.calls.length, 2);
});

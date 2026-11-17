const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const settings = require('../src/pixiv_novel_sync/static/js/ai_settings.js');
const BASE = '/api/dashboard/work-analyses';
const RUN = 'a'.repeat(32), HASH = 'b'.repeat(64), UNIT = 'c'.repeat(64);
const clone = value => JSON.parse(JSON.stringify(value));
const tick = async () => { for (let i = 0; i < 24; i++) await Promise.resolve(); };
const later = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
const budget = {request_limit: 16, token_limit: 100000, active_seconds_limit: 600};
const agent = {kind: 'agent', agent_id: 7, label: 'Fixture Agent', availability: 'ready'};
function run(overrides = {}) {
  return {run_id: RUN, version: 1, status: 'queued', reason: null, title: 'Synthetic book',
    deleted: false, has_report: false, can_resume: false, budget: clone(budget),
    request_count: 2, planned_tokens: 5000, active_seconds_debited: 4,
    unit_counts: {total: 1, succeeded: 0}, coverage: null, usage_summary: null, ...overrides};
}
function preview(body) {
  return {selection: clone(body.selection), agent_id: body.agent_id,
    budget: clone(body.budget), output_tokens: body.output_tokens,
    manifest: {schema_version: 1, source_kind: 'novel', source_id: 101,
      title: 'Synthetic book', publication_state: 'unknown', publication_source: 'unknown',
      order_source: 'publication_date_suggestion', expected_count: null,
      expected_count_observed_at: null, remote_inventory_verified: false,
      requires_partial_confirmation: true, manifest_fingerprint: HASH,
      chapters: [{novel_id: 101, position: 0, title: 'Synthetic chapter', text_hash: HASH,
        chars: 10, utf8_bytes: 10, fetched_at: null, has_text: true}]},
    plan: {schema_version: 1, strategy: 'whole', manifest_fingerprint: HASH,
      chunker_version: 1, prompt_version: 1, report_schema_version: 1,
      minimum_requests: 1, warnings: [], original_units: [{unit_id: UNIT, kind: 'whole',
        is_final: true, level: 0, primary_ranges: [{novel_id: 101, text_hash: HASH, start: 0, end: 10}],
        context_ranges: [], child_unit_ids: [], claim_refs: [], input_fingerprint: HASH}]},
    preview_fingerprint: HASH, targets: [{provider_id: 3, provider_name: 'Synthetic', model_key: 'org/model '}],
    warnings: [], can_create: true, confirmation_requirements: {order_confirmed: false,
      current_archive_only: true, external_send_confirmed: true, budget_confirmed: true}};
}
function fixture() {
  const calls = [], watchers = [], timers = new Map(), listeners = new Map();
  let timerId = 0, nonce = 0, handle;
  const Vue = {
    reactive: value => value, computed: get => ({get value() { return get(); }}),
    nextTick: async () => {}, onUnmounted: () => {},
    watch(read, callback, options = {}) {
      const entry = {read, callback, previous: JSON.stringify(read())};
      watchers.push(entry);
      if (options.immediate) callback(read());
      return () => { const index = watchers.indexOf(entry); if (index >= 0) watchers.splice(index, 1); };
    },
  };
  const root = {
    location: {search: '?source_kind=novel&source_id=101'},
    document: {hidden: false, getElementById: () => null},
    crypto: {getRandomValues: bytes => bytes.fill(++nonce)},
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout: id => timers.delete(id),
    addEventListener(name, fn) { listeners.set(name, fn); },
    removeEventListener(name) { listeners.delete(name); },
  };
  const context = vm.createContext({window: root, URLSearchParams, AbortController, Uint8Array});
  const filename = path.resolve(__dirname, '../src/pixiv_novel_sync/static/js/work_analysis.js');
  vm.runInContext(fs.readFileSync(filename, 'utf8'), context, {filename});
  const answer = async (url, options = {}) => {
    if (url === '/api/dashboard/ai/agents') return [{id: 7, name: 'Fixture Agent', enabled: true}];
    if (url === BASE + '/preview') return preview(JSON.parse(options.body));
    if (url === BASE && options.method === 'POST') return run();
    if (url === BASE + '/' + RUN) return run();
    if (url === BASE + '?offset=0&limit=20') return {items: [run()], total: 1, offset: 0, limit: 20};
    throw new Error('Unexpected synthetic route: ' + url);
  };
  handle = answer;
  const controller = root.WorkAnalysis.createController(Vue, async (url, options = {}) => {
    calls.push({url, method: options.method || 'GET', body: options.body, signal: options.signal});
    return clone(await handle(url, options));
  });
  controller.wa.agents = [clone(agent)];
  controller.chooseAgent(agent);
  controller.wa.form.budget = clone(budget);
  const trigger = () => {
    for (const entry of watchers.slice()) {
      const value = JSON.stringify(entry.read());
      if (value !== entry.previous) { entry.previous = value; entry.callback(entry.read()); }
    }
  };
  trigger();
  return {controller, api: root.WorkAnalysis, root, calls, timers, trigger, answer,
    handle: next => { handle = next; }, writes: () => calls.filter(row => row.method !== 'GET')};
}
async function ready(f) {
  assert.equal(await f.controller.previewWork(), true);
  for (const name of Object.keys(f.controller.wa.confirmations)) f.controller.wa.confirmations[name] = true;
  assert.equal(f.controller.canStart.value, true);
}

test('agent-kind identity never collapses to an undefined model identity', () => {
  assert.deepEqual(JSON.parse(settings.optionKey(agent)), ['agent', 7]);
  assert.notEqual(settings.optionKey(agent), settings.optionKey({...agent, agent_id: 8}));
  assert.notEqual(settings.optionKey(agent), settings.optionKey({kind: 'model', provider_id: 7, model_key: 'org/model '}));
  assert.equal(JSON.parse(settings.optionKey({kind: 'model', provider_id: 7, model_key: 'org/model '}))[2], 'org/model ');
});
test('consent does not invalidate a preview, but direct stale-method calls cannot create', async () => {
  const f = fixture();
  await f.controller.previewWork();
  assert.equal(await f.controller.createWork(), false);
  await ready(f);
  f.controller.wa.form.budget.token_limit += 1;
  assert.equal(await f.controller.createWork(), false);
  assert.equal(f.calls.filter(row => row.url === BASE && row.method === 'POST').length, 0);
  f.trigger();
  assert.equal(f.controller.wa.preview, null);
  assert.equal(f.controller.wa.confirmations.external_send_confirmed, false);
});
test('same-form preview replacement always requires fresh consent at method entry', async () => {
  for (const changedOnServer of [false, true]) {
    const f = fixture(), held = later();
    await ready(f);
    const replacement = preview(JSON.parse(f.calls.at(-1).body));
    if (changedOnServer) {
      replacement.preview_fingerprint = 'd'.repeat(64);
      replacement.manifest.chapters[0].text_hash = 'e'.repeat(64);
      replacement.targets = [{provider_id: 4, provider_name: 'Changed synthetic service', model_key: 'new/model '}];
    }
    f.handle((url, options) => url === BASE + '/preview' ? held.promise : f.answer(url, options));
    const reading = f.controller.previewWork();
    assert.ok(Object.values(f.controller.wa.confirmations).every(value => value === false));
    assert.equal(await f.controller.createWork(), false);
    // A response cannot inherit even an approval written while its request was pending.
    for (const name of Object.keys(f.controller.wa.confirmations)) f.controller.wa.confirmations[name] = true;
    held.resolve(replacement);
    assert.equal(await reading, true);
    assert.equal(f.controller.wa.preview.preview_fingerprint, replacement.preview_fingerprint);
    assert.ok(Object.values(f.controller.wa.confirmations).every(value => value === false));
    assert.equal(f.controller.canStart.value, false);
    assert.equal(await f.controller.createWork(), false);
    assert.equal(f.calls.filter(row => row.url === BASE && row.method === 'POST').length, 0);
    for (const name of Object.keys(f.controller.wa.confirmations)) f.controller.wa.confirmations[name] = true;
    assert.equal(f.controller.canStart.value, true);
  }
});
test('missing or disabled current Agent stays visible but cannot silently become a new selection', async () => {
  const f = fixture();
  await ready(f);
  f.handle(async url => url === '/api/dashboard/ai/agents' ? [{id: 8, name: 'Other', enabled: true}] : f.answer(url));
  await f.controller.reloadAgents();
  assert.equal(f.controller.currentAgent.value.agent_id, 7);
  assert.equal(f.controller.currentAgent.value.availability, 'unavailable');
  assert.equal(f.controller.chooseAgent({...agent, availability: 'disabled'}), false);
  assert.equal(await f.controller.createWork(), false);
});
test('invalid budget or missing amendment/deletion consent blocks direct method entry', async () => {
  for (const value of ['', '0', '-1', '1.5', 'Infinity', 'NaN', true, 513]) {
    const f = fixture();
    f.controller.wa.form.budget.request_limit = value;
    assert.equal(await f.controller.previewWork(), false);
    assert.equal(f.writes().length, 0);
  }
  const f = fixture();
  f.controller.wa.selectedId = RUN; f.controller.wa.run = run({status: 'paused', can_resume: true});
  f.controller.wa.amend = clone(budget);
  assert.equal(await f.controller.control('budget'), false);
  assert.equal(await f.controller.control('delete'), false);
  f.controller.wa.amendConfirmed = true; f.controller.wa.amend.active_seconds_limit = '0';
  assert.equal(await f.controller.control('budget'), false);
  assert.equal(f.writes().length, 0);
});
test('late preview rejection cannot clear a newer loading owner or replace its error', async () => {
  const f = fixture(), first = later(), second = later();
  let count = 0;
  f.handle((url, options) => url === BASE + '/preview' ? (++count === 1 ? first.promise : second.promise) : f.answer(url, options));
  const old = f.controller.previewWork(), current = f.controller.previewWork();
  assert.equal(f.calls[0].signal.aborted, true);
  first.reject(new Error('OLD_UNTRUSTED_FAILURE'));
  await old;
  assert.equal(f.controller.wa.busy.preview, true);
  assert.equal(f.controller.wa.readErrors.preview, '');
  second.resolve(preview(JSON.parse(f.calls[1].body)));
  await current;
  assert.equal(f.controller.wa.busy.preview, false);
  assert.equal(f.controller.wa.preview.preview_fingerprint, HASH);
});
test('double create and unknown outcome preserve one immutable body and idempotency key', async () => {
  const f = fixture(), held = later();
  await ready(f);
  let sends = 0;
  f.handle((url, options) => {
    if (url === BASE && options.method === 'POST') return ++sends === 1 ? held.promise : run();
    return f.answer(url, options);
  });
  const first = f.controller.createWork();
  assert.equal(await f.controller.createWork(), false);
  assert.equal(f.controller.chooseAgent({...agent, agent_id: 8}), false);
  held.reject(new TypeError('NETWORK_OUTCOME_UNKNOWN'));
  await first;
  assert.equal(f.controller.wa.pending.phase, 'unknown');
  const event = {prevented: false, preventDefault() { this.prevented = true; }};
  f.controller.beforeUnload(event);
  assert.equal(event.prevented, true); assert.equal(event.returnValue, '');
  assert.equal(await f.controller.retryUnknown(), true);
  const writes = f.calls.filter(row => row.url === BASE && row.method === 'POST');
  assert.equal(writes.length, 2);
  assert.equal(writes[0].body, writes[1].body);
  assert.match(JSON.parse(writes[0].body).idempotency_key, /^[0-9a-f]{32}$/);
  assert.equal(f.controller.wa.pending, null);
  assert.equal(f.controller.wa.run.run_id, RUN);
});
test('known write success survives failed GET readback without permitting POST retry', async () => {
  const f = fixture();
  await ready(f);
  f.handle((url, options) => {
    if (url === BASE && options.method === 'POST') return run();
    throw Object.assign(new Error('FAILED_GET'), {status: 503});
  });
  assert.equal(await f.controller.createWork(), true);
  assert.equal(f.controller.wa.pending, null);
  assert.equal(f.controller.wa.run.status, 'queued');
  assert.equal(f.controller.wa.writeError, '');
  assert.match(f.controller.wa.notice, /刷新失败/);
  assert.equal(await f.controller.retryUnknown(), false);
  assert.equal(f.calls.filter(row => row.url === BASE && row.method === 'POST').length, 1);
});
for (const deleted of [false, true]) {
  test(`delayed successful mutation preserves a newer ${deleted ? 'tombstone' : 'state'} after failed readback`, async () => {
    const f = fixture(), held = later();
    const initial = run({version: 4, status: 'running', title: 'SYNTHETIC_OLD_TITLE'});
    const newer = run({version: 7, status: deleted ? 'cancelled' : 'paused', reason: 'user_pause',
      deleted, title: deleted ? null : 'SYNTHETIC_NEW_TITLE',
      budget: {...budget, request_limit: 32}, manifest: deleted ? null : {title: 'NEW_MANIFEST'}});
    f.controller.wa.selectedId = RUN; f.controller.wa.run = initial;
    let reads = 0;
    f.handle((url, options) => {
      if (url.endsWith('/pause')) return held.promise;
      if (url === BASE + '/' + RUN && ++reads === 1) return newer;
      throw Object.assign(new Error('FAILED_GET'), {status: 503});
    });
    const writing = f.controller.control('pause');
    assert.equal(f.controller.wa.pending.phase, 'sending');
    assert.equal(await f.controller.refreshRun(), true);
    assert.deepEqual(clone(f.controller.wa.run), newer);
    held.resolve({...initial, version: 5, status: 'pausing', reason: 'user_pause'});
    assert.equal(await writing, true);
    assert.equal(f.controller.wa.pending, null);
    assert.equal(f.controller.wa.selectedId, RUN);
    assert.deepEqual(clone(f.controller.wa.run), newer);
    assert.equal(f.controller.wa.writeError, '');
    assert.match(f.controller.wa.notice, /刷新失败/);
    assert.equal(await f.controller.retryUnknown(), false);
    assert.equal(f.writes().length, 1);
    assert.equal(reads, 2);
    if (deleted) {
      assert.equal(f.controller.controlAllowed('resume'), false);
      assert.equal(f.controller.wa.report, null);
      assert.deepEqual(clone(f.controller.wa.units), []);
      assert.deepEqual(clone(f.controller.wa.evidence), {});
      assert.equal(f.timers.size, 0);
    } else assert.deepEqual(clone(f.controller.wa.amend), newer.budget);
  });
}
test('successful create selects its new run despite another selected run having a higher version', async () => {
  const f = fixture();
  await ready(f);
  f.controller.wa.selectedId = 'd'.repeat(32);
  f.controller.wa.run = run({run_id: f.controller.wa.selectedId, version: 99, deleted: true});
  f.handle((url, options) => {
    if (url === BASE && options.method === 'POST') return run();
    throw Object.assign(new Error('FAILED_GET'), {status: 503});
  });
  assert.equal(await f.controller.createWork(), true);
  assert.equal(f.controller.wa.pending, null);
  assert.equal(f.controller.wa.selectedId, RUN);
  assert.deepEqual(clone(f.controller.wa.run), run());
  assert.equal(f.controller.wa.preview, null);
  assert.equal(f.writes().filter(row => row.url === BASE).length, 1);
});
test('delayed successful mutation cannot retire or overwrite a replacement pending owner', async () => {
  const f = fixture(), held = later();
  f.controller.wa.selectedId = RUN; f.controller.wa.run = run({version: 4, status: 'running'});
  f.handle(() => held.promise);
  const writing = f.controller.control('pause');
  const replacement = {...f.controller.wa.pending, phase: 'unknown', sending: false};
  f.controller.wa.pending = replacement;
  const current = f.controller.wa.run;
  held.resolve(run({version: 5, status: 'pausing', reason: 'user_pause'}));
  assert.equal(await writing, false);
  assert.equal(f.controller.wa.pending, replacement);
  assert.equal(f.controller.wa.run, current);
  assert.equal(f.calls.length, 1);
});
test('unknown control requires readback before an explicit same-version CAS retry', async () => {
  const f = fixture();
  f.controller.wa.selectedId = RUN; f.controller.wa.run = run({status: 'running', version: 4});
  let sends = 0;
  f.handle((url, options) => {
    if (url === BASE + '/' + RUN + '/pause') {
      sends += 1;
      if (sends === 1) throw new TypeError('UNKNOWN_CONTROL');
      return run({status: 'pausing', version: 5, reason: 'user_pause'});
    }
    if (url === BASE + '/' + RUN) return run({status: sends > 1 ? 'pausing' : 'running', version: sends > 1 ? 5 : 4});
    return f.answer(url, options);
  });
  assert.equal(await f.controller.control('pause'), false);
  assert.equal(await f.controller.retryUnknown(), false);
  await f.controller.reconcileWrite();
  assert.equal(f.controller.wa.pending.canRetry, true);
  assert.equal(await f.controller.retryUnknown(), true);
  const writes = f.calls.filter(row => row.url.endsWith('/pause'));
  assert.equal(writes.length, 2); assert.equal(writes[0].body, writes[1].body);
  assert.equal(f.controller.wa.run.status, 'pausing');
});
test('unknown pause cannot turn a scheduler yield into confirmed user intent', async () => {
  for (const status of ['pausing', 'paused']) {
    const f = fixture();
    let current = run({status: 'running', version: 4}), sends = 0;
    f.controller.wa.selectedId = RUN; f.controller.wa.run = current;
    f.handle((url, options) => {
      if (url === BASE + '/' + RUN + '/pause') {
        if (++sends === 1) throw new TypeError('PAUSE_NOT_ACKNOWLEDGED');
        assert.equal(JSON.parse(options.body).expected_version, 5);
        current = run({status, version: 6, reason: 'user_pause', can_resume: status === 'paused'});
        return current;
      }
      if (url === BASE + '/' + RUN) return current;
      return f.answer(url, options);
    });
    assert.equal(await f.controller.control('pause'), false);
    current = run({status, version: 5, reason: 'sync_yield', can_resume: status === 'paused'});
    await f.controller.reconcileWrite();
    assert.equal(f.controller.wa.pending, null);
    assert.doesNotMatch(f.controller.wa.notice, /写入已确认/);
    assert.match(f.controller.wa.writeError, /未确认原写入/);
    assert.equal(f.controller.controlAllowed('pause'), true);
    assert.equal(sends, 1);
    assert.equal(await f.controller.control('pause'), true);
    assert.equal(f.controller.wa.run.reason, 'user_pause');
    const writes = f.calls.filter(row => row.url.endsWith('/pause'));
    assert.deepEqual(writes.map(row => JSON.parse(row.body).expected_version), [4, 5]);
    assert.equal(f.calls.filter(row => row.url.endsWith('/resume')).length, 0);
  }
});
test('a newer matching state is observed but never attributed to an unacknowledged write', async () => {
  for (const [action, status, reason] of [
    ['pause', 'paused', 'user_pause'], ['cancel', 'cancelled', 'user_cancel'],
    ['resume', 'running', null],
  ]) {
    const f = fixture();
    f.controller.wa.selectedId = RUN;
    f.controller.wa.run = run({status: action === 'resume' ? 'paused' : 'running', version: 4, can_resume: action === 'resume'});
    f.handle((url, options) => {
      if (url === BASE + '/' + RUN + '/' + action) throw new TypeError('UNKNOWN_CONTROL');
      if (url === BASE + '/' + RUN) return run({status, reason, version: 7});
      return f.answer(url, options);
    });
    await f.controller.control(action);
    await f.controller.reconcileWrite();
    assert.equal(f.controller.wa.pending, null);
    assert.equal(f.controller.wa.run.version, 7);
    assert.match(f.controller.wa.notice, /原写入结果仍未知/);
    assert.doesNotMatch(f.controller.wa.notice, /写入已确认/);
    assert.equal(await f.controller.retryUnknown(), false);
    assert.equal(f.calls.filter(row => row.method !== 'GET').length, 1);
  }
});
test('older reconciliation cannot replace a newer poll or authorize another CAS write', async () => {
  const f = fixture(), held = later();
  f.controller.wa.selectedId = RUN; f.controller.wa.run = run({status: 'running', version: 4});
  f.handle((url, options) => {
    if (url.endsWith('/pause')) throw new TypeError('UNKNOWN_CONTROL');
    if (url === BASE + '/' + RUN) return held.promise;
    return f.answer(url, options);
  });
  await f.controller.control('pause');
  const reading = f.controller.reconcileWrite();
  f.controller.wa.run = run({status: 'paused', reason: 'user_pause', version: 6});
  held.resolve(run({status: 'paused', reason: 'sync_yield', version: 5}));
  await reading;
  assert.equal(f.controller.wa.run.version, 6);
  assert.equal(f.controller.wa.run.reason, 'user_pause');
  assert.equal(f.controller.wa.pending.phase, 'unknown');
  assert.equal(f.controller.wa.pending.canRetry, false);
  assert.equal(await f.controller.retryUnknown(), false);
  assert.match(f.controller.wa.notice, /较旧版本/);
});
test('reconciliation cannot apply to another selection or a replacement write owner', async () => {
  for (const changed of ['selection', 'operation']) {
    const f = fixture(), held = later();
    f.controller.wa.selectedId = RUN; f.controller.wa.run = run({status: 'running', version: 4});
    f.handle((url, options) => {
      if (url.endsWith('/pause')) throw new TypeError('UNKNOWN_CONTROL');
      if (url === BASE + '/' + RUN) return held.promise;
      return f.answer(url, options);
    });
    await f.controller.control('pause');
    const original = f.controller.wa.pending;
    const reading = f.controller.reconcileWrite();
    if (changed === 'selection') {
      f.controller.wa.selectedId = 'f'.repeat(32);
      f.controller.wa.run = run({run_id: f.controller.wa.selectedId, version: 1});
    } else f.controller.wa.pending = {...original, phase: 'unknown', canRetry: false};
    const currentRun = f.controller.wa.run, currentPending = f.controller.wa.pending;
    held.resolve(run({status: 'paused', reason: 'user_pause', version: 5}));
    await reading;
    assert.equal(f.controller.wa.run, currentRun);
    assert.equal(f.controller.wa.pending, currentPending);
    assert.equal(f.controller.wa.pending.canRetry, false);
    assert.doesNotMatch(f.controller.wa.notice, /写入已确认/);
  }
});
test('a new reconciliation revokes a prior retry authorization while its read is pending', async () => {
  const f = fixture(), held = later();
  f.controller.wa.selectedId = RUN; f.controller.wa.run = run({status: 'running', version: 4});
  let reads = 0;
  f.handle((url, options) => {
    if (url.endsWith('/pause')) throw new TypeError('UNKNOWN_CONTROL');
    if (url === BASE + '/' + RUN) return ++reads === 1 ? run({status: 'running', version: 4}) : held.promise;
    return f.answer(url, options);
  });
  await f.controller.control('pause');
  await f.controller.reconcileWrite();
  assert.equal(f.controller.wa.pending.canRetry, true);
  const reading = f.controller.reconcileWrite();
  assert.equal(f.controller.wa.pending.canRetry, false);
  assert.equal(await f.controller.retryUnknown(), false);
  held.resolve(run({status: 'paused', reason: 'sync_yield', version: 5}));
  await reading;
  assert.equal(f.calls.filter(row => row.url.endsWith('/pause')).length, 1);
});
test('a newer poll revokes old-version retry and an older poll cannot roll back reconciliation', async () => {
  const f = fixture(), held = later();
  f.controller.wa.selectedId = RUN; f.controller.wa.run = run({status: 'running', version: 4});
  let reads = 0;
  f.handle((url, options) => {
    if (url.endsWith('/pause')) throw new TypeError('UNKNOWN_CONTROL');
    if (url === BASE + '/' + RUN) {
      reads += 1;
      return reads === 1 ? run({status: 'running', version: 4}) : reads === 3 ? held.promise
        : run({status: 'paused', reason: 'sync_yield', version: 5});
    }
    return f.answer(url, options);
  });
  await f.controller.control('pause');
  await f.controller.reconcileWrite();
  assert.equal(f.controller.wa.pending.canRetry, true);
  await f.controller.refreshRun();
  assert.equal(f.controller.wa.pending.canRetry, false);
  const polling = f.controller.refreshRun();
  assert.equal(await f.controller.retryUnknown(), false);
  assert.equal(f.calls.filter(row => row.url.endsWith('/pause')).length, 1);
  await f.controller.reconcileWrite();
  held.resolve(run({status: 'running', version: 4}));
  await polling;
  assert.equal(f.controller.wa.run.version, 5);
  assert.equal(f.controller.wa.run.reason, 'sync_yield');
  assert.equal(f.controller.wa.pending, null);
  assert.equal(f.controller.controlAllowed('pause'), true);
  assert.equal(f.calls.filter(row => row.url.endsWith('/pause')).length, 1);
});
test('negative, overlong, and UTF-16-confused evidence never becomes a precise location', () => {
  const f = fixture();
  const evidence = {novel_id: 101, text_hash: HASH, start: 1001, end: 1003, quote: '\u{1f600}Z'};
  const value = {evidence, jump: {...evidence, context_start: 1000, context_text: 'A\u{1f600}ZB'}};
  assert.deepEqual(clone(f.api.partsFromEvidence(value)), {before: 'A', quote: '\u{1f600}Z', after: 'B'});
  assert.throws(() => f.api.partsFromEvidence({...value, jump: {...value.jump, end: 1004}}));
  assert.throws(() => f.api.partsFromEvidence({...value, jump: {...value.jump, context_start: -1}}));
  const long = {evidence: {...evidence, start: 129, end: 131},
    jump: {...value.jump, start: 129, end: 131, context_start: 0, context_text: 'A'.repeat(129) + '\u{1f600}Z'}};
  assert.throws(() => f.api.partsFromEvidence(long));
});
const sensitiveReads = [
  {scope: 'report', load: controller => controller.loadReport(),
    value: {artifact: {summary: 'SYNTHETIC_PRIVATE_REPORT', evidence: {[HASH]: {novel_id: 101}}}}},
  {scope: 'units', load: controller => controller.loadUnits(),
    value: {items: [{unit_id: UNIT, summary: 'SYNTHETIC_PRIVATE_UNIT'}], total: 1, offset: 0}},
  {scope: 'evidence:' + HASH, load: controller => controller.inspectEvidence(HASH),
    value: {evidence: {novel_id: 101, text_hash: HASH, start: 0, end: 5, quote: 'QUOTE'},
      jump: {novel_id: 101, text_hash: HASH, start: 0, end: 5, context_start: 0, context_text: 'QUOTE'}}},
];
function seedSensitive(controller) {
  controller.wa.report = clone(sensitiveReads[0].value);
  controller.wa.evidence = {[HASH]: {parts: {quote: 'OLD_QUOTE'}}};
  controller.wa.units = [{unit_id: UNIT}];
}
function assertSensitiveCleared(controller) {
  assert.equal(controller.wa.report, null);
  assert.deepEqual(clone(controller.wa.evidence), {});
  assert.deepEqual(clone(controller.wa.units), []);
}
for (const resource of sensitiveReads) {
  for (const outcome of ['success', 'error']) {
    test(`tombstone revokes deferred ${resource.scope} ${outcome} and finally`, async () => {
      const f = fixture(), held = later();
      f.controller.wa.selectedId = RUN;
      f.controller.wa.run = run({has_report: true});
      seedSensitive(f.controller);
      f.handle(url => url === BASE + '/' + RUN ? run({deleted: true, version: 2}) : held.promise);
      const stale = resource.load(f.controller), staleCall = f.calls.at(-1);
      assert.equal(await f.controller.refreshRun(), true);
      const tombstoneCall = f.calls.at(-1);
      assertSensitiveCleared(f.controller);
      if (outcome === 'success') held.resolve(resource.value);
      else held.reject(Object.assign(new Error('STALE_SENSITIVE_FAILURE'), {status: 503}));
      assert.equal(await stale, false);
      assertSensitiveCleared(f.controller);
      assert.equal(f.controller.wa.readErrors[resource.scope], '');
      assert.equal(f.controller.wa.busy[resource.scope], false);
      assert.equal(staleCall.signal.aborted, true);
      assert.equal(tombstoneCall.signal.aborted, false);
      assert.equal(f.controller.wa.busy.run, false);
      assert.equal(f.timers.size, 0);
    });
    test(`revoked ${resource.scope} ${outcome} cannot disturb a newer selected owner`, async () => {
      const f = fixture(), old = later(), current = later(), nextId = 'd'.repeat(32);
      f.controller.wa.selectedId = RUN;
      f.controller.wa.run = run({has_report: true});
      seedSensitive(f.controller);
      f.handle(url => {
        if (url === BASE + '/' + RUN) return run({deleted: true, version: 2});
        if (url === BASE + '/' + nextId) return run({run_id: nextId, has_report: true});
        return url.startsWith(BASE + '/' + RUN + '/') ? old.promise : current.promise;
      });
      const stale = resource.load(f.controller), staleCall = f.calls.at(-1);
      await f.controller.refreshRun();
      assert.equal(staleCall.signal.aborted, true);
      await f.controller.selectRun(nextId);
      seedSensitive(f.controller);
      const latest = resource.load(f.controller);
      const currentState = clone({report: f.controller.wa.report, evidence: f.controller.wa.evidence, units: f.controller.wa.units});
      if (outcome === 'success') old.resolve(resource.value);
      else old.reject(Object.assign(new Error('OLD_FAILURE'), {status: 503}));
      assert.equal(await stale, false);
      assert.equal(f.controller.wa.selectedId, nextId);
      assert.deepEqual(clone({report: f.controller.wa.report, evidence: f.controller.wa.evidence, units: f.controller.wa.units}), currentState);
      assert.equal(f.controller.wa.busy[resource.scope], true);
      assert.equal(f.controller.wa.readErrors[resource.scope], '');
      current.resolve(resource.value);
      assert.equal(await latest, true);
      assert.equal(f.controller.wa.busy[resource.scope], false);
    });
  }
}
test('tombstone revokes reconciliation without discarding an unknown pending write', async () => {
  const f = fixture(), held = later();
  f.controller.wa.selectedId = RUN; f.controller.wa.run = run({status: 'running'});
  let reads = 0;
  f.handle((url, options) => {
    if (url.endsWith('/pause')) throw new TypeError('UNKNOWN_WRITE');
    if (url === BASE + '/' + RUN) return ++reads === 1 ? held.promise : run({deleted: true, version: 2});
    return f.answer(url, options);
  });
  await f.controller.control('pause');
  const pending = f.controller.wa.pending, body = pending.body;
  const stale = f.controller.reconcileWrite(), staleCall = f.calls.at(-1);
  await f.controller.refreshRun();
  held.resolve(run({version: 3, status: 'paused', reason: 'user_pause'}));
  assert.equal(await stale, false);
  assert.equal(f.controller.wa.run.deleted, true);
  assert.equal(f.controller.wa.run.version, 2);
  assert.equal(f.controller.wa.pending, pending);
  assert.equal(pending.body, body);
  assert.equal(pending.phase, 'unknown');
  assert.equal(pending.canRetry, false);
  assert.equal(staleCall.signal.aborted, true);
  assert.equal(await f.controller.retryUnknown(), false);
  assert.equal(f.writes().length, 1);
});
test('a reconciled deletion also revokes every sensitive reader without retrying the write', async () => {
  for (const resource of sensitiveReads) {
    for (const outcome of ['success', 'error']) {
      const f = fixture(), held = later();
      f.controller.wa.selectedId = RUN; f.controller.wa.run = run({has_report: true});
      f.controller.wa.deleteConfirmed = true;
      seedSensitive(f.controller);
      f.handle((url, options) => {
        if (options.method === 'DELETE') throw new TypeError('UNKNOWN_DELETE');
        if (url === BASE + '/' + RUN) return run({deleted: true, version: 2});
        return held.promise;
      });
      await f.controller.control('delete');
      const stale = resource.load(f.controller), staleCall = f.calls.at(-1);
      assert.equal(await f.controller.reconcileWrite(), true);
      assert.equal(f.controller.wa.pending, null);
      assert.equal(f.controller.wa.run.deleted, true);
      assertSensitiveCleared(f.controller);
      if (outcome === 'success') held.resolve(resource.value);
      else held.reject(Object.assign(new Error('STALE_DELETED_READ'), {status: 503}));
      assert.equal(await stale, false);
      assertSensitiveCleared(f.controller);
      assert.equal(staleCall.signal.aborted, true);
      assert.equal(f.controller.wa.readErrors[resource.scope], '');
      assert.equal(f.controller.wa.busy[resource.scope], false);
      assert.equal(await f.controller.retryUnknown(), false);
      assert.equal(f.writes().length, 1);
    }
  }
});
test('stale detail responses cannot revive a tombstone or overwrite a newer detail owner', async () => {
  for (const [outcome, changeSelection] of [['success', false], ['error', false], ['success', true], ['error', true]]) {
    const f = fixture(), old = later(), current = later(), nextId = 'd'.repeat(32);
    f.controller.wa.selectedId = RUN; f.controller.wa.run = run();
    let reads = 0;
    f.handle(url => url === BASE + '/' + RUN ? (++reads === 1 ? old.promise : run({deleted: true, version: 2})) : current.promise);
    const stale = f.controller.refreshRun();
    await f.controller.refreshRun();
    const selected = changeSelection ? f.controller.selectRun(nextId) : null;
    if (outcome === 'success') old.resolve(run({version: 3, has_report: true}));
    else old.reject(Object.assign(new Error('OLD_DETAIL_FAILURE'), {status: 503}));
    assert.equal(await stale, false);
    assert.equal(f.controller.wa.selectedId, changeSelection ? nextId : RUN);
    if (changeSelection) assert.equal(f.controller.wa.run, null);
    else {
      assert.equal(f.controller.wa.run.deleted, true);
      assert.equal(f.controller.wa.run.version, 2);
    }
    assert.equal(f.controller.wa.busy.run, changeSelection);
    assert.equal(f.controller.wa.readErrors.run, '');
    assertSensitiveCleared(f.controller);
    if (changeSelection) {
      current.resolve(run({run_id: nextId}));
      assert.equal(await selected, true);
      assert.equal(f.controller.wa.run.run_id, nextId);
    }
  }
});
test('direct sensitive read methods cannot restart reads for a tombstone', async () => {
  const f = fixture();
  f.controller.wa.selectedId = RUN;
  f.controller.wa.run = run({deleted: true, has_report: true, version: 2});
  for (const resource of sensitiveReads) {
    seedSensitive(f.controller);
    assert.equal(await resource.load(f.controller), false);
  }
  assert.equal(f.calls.length, 0);
});
test('hidden/disposed controllers do not poll or accept late reads', async () => {
  const f = fixture(), held = later();
  f.controller.wa.selectedId = RUN;
  await f.controller.refreshRun();
  assert.equal(f.timers.size, 1);
  f.root.document.hidden = true; f.controller.visibility();
  assert.equal(f.timers.size, 0);
  f.handle((url, options) => url === BASE + '/preview' ? held.promise : f.answer(url, options));
  const reading = f.controller.previewWork();
  f.controller.suspend();
  held.resolve(preview(JSON.parse(f.calls.at(-1).body)));
  await reading;
  assert.equal(f.controller.wa.preview, null);
  f.controller.dispose();
  const count = f.calls.length;
  f.controller.restore(); await tick();
  assert.equal(await f.controller.createWork(), false);
  assert.equal(f.calls.length, count);
  assert.equal(f.timers.size, 0);
});

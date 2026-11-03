/* In-memory contracts against shipped helpers and real page controllers. */
const assert = require('node:assert/strict');
const test = require('node:test');
const S = require('../src/pixiv_novel_sync/static/js/ai_settings.js');
const Picker = require('../src/pixiv_novel_sync/static/js/ai_picker.js');
const Onboarding = require('../src/pixiv_novel_sync/static/js/ai_provider_onboarding.js');
const {mount, response, envelope, plain, flush} = require('./helpers/management_vm.cjs');

const option = (overrides = {}) => ({
  kind: 'model', provider_id: 7, provider_model_id: 31,
  provider_name: 'Fixture relay', model_key: 'org/model/v1',
  label: 'Same display name', availability: 'ready', ...overrides,
});

test('opaque choice identity keeps provider IDs and slash-containing keys distinct', () => {
  assert.notEqual(S.optionKey(option()), S.optionKey(option({provider_id: 8})));
  assert.equal(JSON.parse(S.optionKey(option()))[2], 'org/model/v1');
});
test('unavailable current choice survives refresh and filtering without replacement', () => {
  const current = option({availability: 'unavailable', reason: 'not in catalogue'});
  const other = option({provider_id: 8, provider_model_id: 40});
  const rows = S.filterOptions(S.retainCurrent([other], current), 'not-matching');
  assert.equal(rows.length, 1);
  assert.equal(rows[0].current, true);
  assert.equal(rows[0].availability, 'unavailable');
  assert.equal(current.current, undefined);
  assert.equal(S.retainCurrent([], option({availability: 'unverified'}))[0].availability, 'unverified');
});
test('touched patches distinguish 0, false, missing, and null', () => {
  const old = {temperature: 0.8, max_retries: 2, stream_enabled: true, name: 'A'};
  const draft = {...old, temperature: 0, max_retries: 0, stream_enabled: false, extra: null};
  assert.deepEqual(S.touchedPatch(old, draft, ['temperature', 'max_retries', 'stream_enabled', 'extra']),
    {temperature: 0, max_retries: 0, stream_enabled: false, extra: null});
  assert.deepEqual(S.touchedPatch({}, {temperature: 0.8}, []), {});
});
test('workspace parsing never turns an invalid requested provider into provider zero', () => {
  assert.deepEqual(S.readWorkspace('?view=wrong&provider=missing'),
    {view: 'providers', providerId: null, invalidProvider: 'missing'});
  assert.deepEqual(S.readWorkspace('?view=models&provider=17'),
    {view: 'models', providerId: 17, invalidProvider: null});
  assert.equal(S.workspaceSearch('?provider=17&keep=yes', {view: 'groups'}),
    '?provider=17&keep=yes&view=groups');
});
test('onboarding selects persisted discovered rows and never changes manual-only rows', () => {
  const catalogue = {catalog_revision: 'a'.repeat(64), items: [
    {id: 1, model_key: 'wanted', discovered: true, discovered_available: true},
    {id: 2, model_key: 'other', discovered: true, discovered_available: true},
    {id: 3, model_key: 'manual', discovered: false, manual: true, enabled: true},
    {id: 4, model_key: 'gone', discovered: true, discovered_available: false},
  ]};
  assert.deepEqual(S.initialModelSelection(catalogue, ['wanted', 'gone']), {
    revision: 'a'.repeat(64), changes: [{id: 1, enabled: true}, {id: 2, enabled: false}], missing: ['gone'],
  });
});
test('reorder is immutable and output override clearing is explicit', () => {
  const rows = [{id: 1}, {id: 2}, {id: 3}];
  assert.deepEqual(S.moveItem(rows, 0, 2).map(x => x.id), [2, 3, 1]);
  assert.deepEqual(rows.map(x => x.id), [1, 2, 3]);
  assert.equal(S.parseNullableOutput(null), null);
  assert.equal(S.parseNullableOutput('1000000'), 1000000);
  for (const bad of ['', true, 0, -1, 1000001, '1.5']) assert.throws(() => S.parseNullableOutput(bad));
});
test('atomic selection rejects more than 5000 rows rather than chunking', () => {
  const items = Array.from({length: 5001}, (_, id) => ({id, model_key: String(id), discovered: true, discovered_available: true}));
  assert.throws(() => S.initialModelSelection({catalog_revision: 'a'.repeat(64), items}, []), RangeError);
});
test('draft guard blocks failed saves and explicitly discards without modifying baseline early', async () => {
  let draft = {name: 'old'}, action = 'save', reject = false;
  const guard = S.createDraftGuard({read: () => draft, save: async () => {
    if (reject) throw new Error('fixture save failed');
    return false;
  }, discard: async () => { draft = {name: 'old'}; }, confirm: async () => action});
  draft.name = 'new';
  assert.equal(await guard.leave(), false);
  assert.equal(guard.isDirty(), true);
  reject = true;
  await assert.rejects(guard.leave(), /fixture save failed/);
  assert.equal(guard.isDirty(), true);
  action = 'cancel';
  assert.equal(await guard.leave(), false);
  action = 'discard';
  assert.equal(await guard.leave(), true);
  assert.equal(guard.isDirty(), false);
});
test('picker opening/filtering/reloading emits nothing; selection emits exact identity', () => {
  const Vue = {ref: value => ({value}), computed: get => ({get value() { return get(); }})};
  const props = {id: 'fixture-model', label: 'Model', disabled: false,
    options: [option({label: '<img onerror=fixture>'})], current: option({model_key: 'old', availability: 'unavailable'})};
  const events = [];
  const component = Picker.createModelPicker(Vue);
  const picker = component.setup(props, {emit: (...args) => events.push(args)});
  picker.show();
  picker.query.value = 'same';
  props.options = [option({provider_id: 8})];
  assert.equal(events.length, 0);
  assert.equal(picker.rows.value[0].model_key, 'old');
  assert.equal(picker.choose(picker.rows.value[0]), false);
  assert.equal(picker.choose(props.options[0]), true);
  assert.equal(events[0][0], 'select');
  assert.equal(events[0][1].provider_id, 8);
  assert.equal(events[0][1].model_key, 'org/model/v1');
  picker.close();
  props.disabled = true;
  picker.show();
  assert.equal(picker.open.value, false);
  assert.equal(picker.choose(props.options[0]), false);
  assert.equal(events.length, 1);
  assert.doesNotMatch(component.template, /v-html|autofocus/);
  assert.match(component.template, /app-modal/);
});

const later = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
const tick = async () => { for (let i = 0; i < 24; i++) await Promise.resolve(); };
const catalogue = (items = [
  {id: 41, model_key: 'chosen', discovered: true, discovered_available: true},
  {id: 42, model_key: 'not-chosen', discovered: true, discovered_available: true},
  {id: 43, model_key: 'manual', source: 'manual', discovered: false, manual: true, enabled: true},
], revision = 'b') => ({catalog_revision: revision.repeat(64), items, total: items.length});
function onboardingFixture(overrides = {}) {
  const calls = [], states = [];
  let sync = {status: 'succeeded'}, catalog = catalogue(), applyError = null;
  const flow = Onboarding.createOnboarding({
    request: async (url, init = {}) => {
      calls.push({url, method: init.method || 'GET', body: init.body && JSON.parse(init.body)});
      if (overrides.request) return overrides.request(url, init);
      if (url === '/api/dashboard/ai/providers') return {id: 17};
      if (url === '/api/dashboard/ai/providers/17') return {warnings: []};
      if (url === '/api/dashboard/ai/providers/17/models/sync') return {operation_id: 'fixture-operation'};
      if (url === '/api/dashboard/ai/providers/17/models/enabled') { if (applyError) throw applyError; return {}; }
      throw new Error('Unexpected request ' + url);
    },
    waitForSync: async (id, operationId) => {
      assert.equal(id, 17); assert.equal(operationId, 'fixture-operation');
      return overrides.waitForSync ? overrides.waitForSync(id, operationId) : sync;
    },
    readCatalog: async id => {
      calls.push({url: `catalog:${id}`, method: 'GET'});
      return overrides.readCatalog ? overrides.readCatalog(id) : catalog;
    },
    onChange: state => states.push(state),
  });
  return {flow, calls, states, setSync: value => { sync = value; }, setCatalog: value => { catalog = value; },
    setApplyError: value => { applyError = value; }};
}
const fakeForm = {id: 0, name: 'Fixture', api_key: 'FAKE-KEY-NOT-A-CREDENTIAL'};
test('explicit retry captures latest visible choices once before async sync', async () => {
  const held = later(); let retrying = false;
  const f = onboardingFixture({waitForSync: () => retrying ? held.promise : {status: 'failed'}});
  await f.flow.save(fakeForm, ['chosen']);
  retrying = true;
  const keys = ['not-chosen'];
  const retry = f.flow.retry(keys);
  keys[0] = 'chosen';
  const duplicate = f.flow.retry(['chosen']);
  assert.equal(retry, duplicate);
  held.resolve({status: 'succeeded'}); await retry;
  assert.deepEqual(f.flow.getState().selectedKeys, ['not-chosen']);
  assert.deepEqual(f.calls.at(-1).body.changes, [{id: 41, enabled: false}, {id: 42, enabled: true}]);
  assert.equal(f.calls.filter(call => call.url === '/api/dashboard/ai/providers').length, 1);
});
test('explicit revision confirmation uses its latest choices and reviewed catalogue revision', async () => {
  const f = onboardingFixture();
  f.setApplyError(Object.assign(new Error('conflict'), {status: 409}));
  await f.flow.save(fakeForm, ['chosen']);
  f.setApplyError(null); f.setCatalog(catalogue(undefined, 'c'));
  await f.flow.retry(['not-chosen']);
  assert.equal(f.flow.getState().phase, 'confirm_revision');
  assert.equal(f.calls.filter(call => call.method === 'PUT').length, 1);
  const keys = ['chosen'];
  const confirming = f.flow.confirmRevision(keys); keys[0] = 'not-chosen';
  await confirming;
  assert.deepEqual(f.calls.at(-1).body, {catalog_revision: 'c'.repeat(64),
    changes: [{id: 41, enabled: true}, {id: 42, enabled: false}]});
  assert.deepEqual(f.flow.getState().selectedKeys, ['chosen']);
});
test('a vanished nonempty selection cannot silently disable every discovered row', async () => {
  const f = onboardingFixture();
  await f.flow.save(fakeForm, ['gone']);
  assert.equal(f.flow.getState().phase, 'selection_unavailable');
  assert.deepEqual(f.flow.getState().missing, ['gone']);
  assert.equal(f.calls.filter(call => call.method === 'PUT').length, 0);
  await f.flow.retry(['not-chosen']);
  assert.equal(f.flow.getState().phase, 'confirm_revision');
  await f.flow.confirmRevision(['gone-again']);
  assert.equal(f.flow.getState().phase, 'selection_unavailable');
  assert.equal(f.calls.filter(call => call.method === 'PUT').length, 0);
});
test('explicitly clearing every visible choice at confirmation disables discovered rows only', async () => {
  const f = onboardingFixture();
  f.setApplyError(Object.assign(new Error('conflict'), {status: 409}));
  await f.flow.save(fakeForm, ['chosen']);
  f.setApplyError(null); await f.flow.retry(['chosen']);
  await f.flow.confirmRevision([]);
  assert.deepEqual(f.calls.at(-1).body.changes, [{id: 41, enabled: false}, {id: 42, enabled: false}]);
  assert.deepEqual(f.flow.getState().selectedKeys, []);
});
test('sync failure retains created ID and selections; retry never recreates Provider', async () => {
  const f = onboardingFixture();
  f.setSync({status: 'failed', error_message: 'fixture failure'});
  await f.flow.save(fakeForm, ['chosen']);
  assert.equal(f.flow.getState().providerId, 17);
  assert.equal(f.flow.getState().phase, 'sync_failed');
  assert.equal(f.flow.getState().operationId, null);
  assert.equal(f.calls.filter(x => x.url.endsWith('/models/enabled')).length, 0);
  f.setSync({status: 'succeeded'});
  await f.flow.retry();
  assert.equal(f.flow.getState().phase, 'applied');
  assert.equal(f.calls.filter(x => x.url === '/api/dashboard/ai/providers').length, 1);
  assert.deepEqual(f.calls.at(-1).body, {catalog_revision: 'b'.repeat(64), changes: [{id: 41, enabled: true}, {id: 42, enabled: false}]});
  assert.equal(JSON.stringify(f.states).includes('FAKE-KEY'), false);
  const copy = f.flow.getState(); copy.selectedKeys.push('not-stored');
  assert.deepEqual(f.flow.getState().selectedKeys, ['chosen']);
});
test('no selected keys saves only; existing update uses known ID even without ID response', async () => {
  for (const id of [0, 17]) {
    const f = onboardingFixture();
    await f.flow.save({...fakeForm, id}, []);
    assert.equal(f.flow.getState().phase, 'saved_only');
    assert.equal(f.flow.getState().providerId, 17);
    assert.equal(f.calls.length, 1);
    assert.equal(f.calls[0].method, id ? 'PUT' : 'POST');
  }
});
test('empty confirmation is not success and resumes the same operation only after explicit action', async () => {
  const f = onboardingFixture();
  f.setSync({status: 'needs_empty_confirmation'});
  await f.flow.save(fakeForm, ['chosen']);
  assert.equal(f.flow.getState().phase, 'awaiting_empty_confirmation');
  assert.equal(f.calls.length, 2);
  f.setSync({status: 'succeeded'}); f.setCatalog(catalogue([]));
  await f.flow.retry();
  assert.equal(f.flow.getState().phase, 'selection_unavailable');
  assert.deepEqual(f.flow.getState().missing, ['chosen']);
  assert.equal(f.calls.filter(x => x.method === 'PUT').length, 0);
  assert.equal(f.calls.filter(x => x.url.endsWith('/sync')).length, 1);
});
test('missing selections reported separately while manual-only entries remain untouched', async () => {
  const f = onboardingFixture();
  await f.flow.save(fakeForm, ['chosen', 'gone', 'manual']);
  assert.equal(f.flow.getState().phase, 'applied');
  assert.deepEqual(f.flow.getState().missing, ['gone', 'manual']);
  assert.deepEqual(f.calls.at(-1).body.changes, [{id: 41, enabled: true}, {id: 42, enabled: false}]);
});
test('oversize atomic selection has no chunked writes', async () => {
  const f = onboardingFixture();
  f.setCatalog(catalogue(Array.from({length: 5001}, (_, id) => ({id, model_key: String(id), discovered: true, discovered_available: true}))));
  await f.flow.save(fakeForm, ['0']);
  assert.equal(f.flow.getState().phase, 'apply_failed');
  assert.equal(f.calls.filter(x => x.method === 'PUT').length, 0);
});
for (const status of [409, 503]) test(`apply failure ${status} refreshes but requires explicit revision reconfirmation`, async () => {
  const f = onboardingFixture();
  f.setApplyError(Object.assign(new Error('fixture conflict'), {status}));
  await f.flow.save(fakeForm, ['chosen']);
  assert.equal(f.flow.getState().phase, status === 409 ? 'conflict' : 'apply_failed');
  f.setApplyError(null); f.setCatalog(catalogue(undefined, 'c'));
  await f.flow.retry();
  assert.equal(f.flow.getState().phase, 'confirm_revision');
  assert.equal(f.calls.filter(x => x.method === 'PUT').length, 1);
  assert.equal(f.calls.filter(x => x.url === '/api/dashboard/ai/providers').length, 1);
  assert.equal(f.calls.filter(x => x.url.endsWith('/sync')).length, 1);
  await f.flow.confirmRevision();
  assert.equal(f.flow.getState().phase, 'applied');
  assert.equal(f.calls.at(-1).body.catalog_revision, 'c'.repeat(64));
  await f.flow.retry(); await f.flow.save(fakeForm, ['other']);
  assert.equal(f.calls.filter(x => x.method === 'PUT').length, 2, 'completed flow cannot repeat writes');
});
test('duplicate saves share a promise and disposal prevents every late chain boundary', async () => {
  for (const stage of ['create', 'sync', 'catalog', 'apply']) {
    const held = later();
    const f = onboardingFixture({
      request: async url => {
        if (url === '/api/dashboard/ai/providers') return stage === 'create' ? held.promise : {id: 17};
        if (url.endsWith('/models/sync')) return {operation_id: 'fixture-operation'};
        if (url.endsWith('/models/enabled')) return held.promise;
        throw new Error('Unexpected ' + url);
      },
      waitForSync: async () => stage === 'sync' ? held.promise : {status: 'succeeded'},
      readCatalog: async () => stage === 'catalog' ? held.promise : catalogue(),
    });
    const first = f.flow.save(fakeForm, ['chosen']);
    assert.equal(f.flow.save(fakeForm, ['other']), first);
    await tick();
    const count = f.calls.length, stateCount = f.states.length;
    f.flow.dispose();
    held.resolve(stage === 'create' ? {id: 17} : stage === 'sync' ? {status: 'succeeded'} : catalogue());
    await first; await f.flow.retry(); await f.flow.confirmRevision(); await f.flow.save(fakeForm, []);
    assert.equal(f.calls.length, count);
    assert.equal(f.states.length, stateCount);
  }
});
test('unconfirmed initial creation never retries a non-idempotent POST', async () => {
  const f = onboardingFixture({request: async () => { throw new Error('transport lost'); }});
  await f.flow.save(fakeForm, ['chosen']);
  assert.equal(f.flow.getState().phase, 'create_unconfirmed');
  assert.match(f.flow.getState().error, /refresh|刷新/i);
  await f.flow.retry(); await f.flow.save(fakeForm, ['chosen']);
  assert.equal(f.calls.length, 1);
});

const pageProvider = (overrides = {}) => ({id: 17, name: 'Fixture provider', provider_type: 'openai_compatible',
  base_url: 'https://fixture.invalid/v1', default_model: '', api_key: '', max_retries: 0,
  timeout_seconds: 120, context_window: 128000, stream_enabled: false, enabled: true, proxy: null, ...overrides});
const pagePool = (overrides = {}) => ({id: 23, name: 'Fixture group', description: '', pool_kind: 'custom',
  fallback_pool_id: null, enabled: false, version: 2, members: [], referenced_by_agents: [], referenced_by_pools: [], ...overrides});
const sse = blocks => ({ok: true, status: 200, body: {getReader() {
  let sent = false;
  return {read: async () => sent ? {done: true} : (sent = true, {done: false, value: new TextEncoder().encode(blocks)}), cancel: async () => {}};
}}});
test('actual page loads shared factories in order and API errors retain metadata', async () => {
  const m = mount('dashboard_settings_models.html', '?view=groups&provider=17&keep=1');
  assert.ok(m.components.has('ai-model-picker'));
  assert.equal(typeof m.window.AISettings.optionKey, 'function');
  assert.equal(typeof m.window.AIProviderOnboarding.createOnboarding, 'function');
  assert.deepEqual(plain(m.state.workspace), {view: 'groups', providerId: 17, invalidProvider: null});
  m.replyWith(() => response({ok: false, error: 'fixture conflict', data: {current_version: 3}}, 409));
  await assert.rejects(m.window.aiApi.request('/api/fixture'), error => error.status === 409 && error.data.current_version === 3);
});
test('actual Provider save retains created ID, clears Key and applies full persisted selection once', async () => {
  const m = mount('dashboard_settings_models.html', '?view=providers');
  Object.assign(m.state.providerForm, pageProvider({id: 0, api_key: 'FAKE-UI-KEY'}));
  m.state.probedModels.value = [{model_key: 'chosen', checked: true}, {model_key: 'other', checked: false}];
  m.state.modelSearch[17] = 'chosen';
  m.replyWith((url, init) => {
    if (url === '/api/dashboard/ai/providers' && init.method === 'POST') return envelope({id: 17});
    if (url.endsWith('/17/models/sync')) return envelope({operation_id: 'fixture-operation'});
    if (url.endsWith('/fixture-operation/events')) return sse('event: completed\r\ndata: {"status":"succeeded"}\r\n\r\n');
    if (url === '/api/dashboard/ai/providers/17/models') return envelope(catalogue());
    if (url.endsWith('/17/models/enabled')) return envelope({});
    if (url === '/api/dashboard/ai/providers') return envelope([pageProvider()]);
    if (url === '/api/dashboard/ai/providers/17/models?search=chosen') return envelope(catalogue([catalogue().items[0]]));
    throw new Error('Unexpected request ' + url);
  });
  assert.equal(await m.state.saveProvider(), true);
  assert.equal(m.state.providerForm.id, 17);
  assert.equal(m.state.providerForm.api_key, '');
  assert.equal(m.state.probedModels.value[0].checked, true);
  assert.equal(m.state.onboardingState.value.phase, 'applied');
  assert.equal(m.calls.filter(x => x.method === 'POST' && x.url === '/api/dashboard/ai/providers').length, 1);
  const enabled = m.calls.filter(x => x.url.endsWith('/models/enabled'));
  assert.equal(enabled.length, 1);
  assert.deepEqual(JSON.parse(enabled[0].options.body).changes, [{id: 41, enabled: true}, {id: 42, enabled: false}]);
  assert.ok(m.storageWrites.every(([key, value]) => key.startsWith('pixiv-ai-model-sync:') && value === 'fixture-operation'));
});
test('actual Provider retry and revision confirmation submit visible checkboxes, not original choices', async () => {
  const m = mount('dashboard_settings_models.html');
  Object.assign(m.state.providerForm, pageProvider({id: 0}));
  m.state.probedModels.value = [{model_key: 'chosen', checked: true}, {model_key: 'not-chosen', checked: false}];
  let operation = 0, applies = 0;
  m.replyWith((url, init) => {
    if (url === '/api/dashboard/ai/providers' && init.method === 'POST') return envelope({id: 17});
    if (url.endsWith('/models/sync')) return envelope({operation_id: 'fixture-' + (++operation)});
    if (url.endsWith('/fixture-1/events')) return sse('event: failed\ndata: {}\n\n');
    if (url.endsWith('/fixture-2/events')) return sse('event: completed\ndata: {}\n\n');
    if (url.endsWith('/models/enabled')) return ++applies === 1 ? response({error: 'conflict'}, 409) : envelope({});
    if (url.endsWith('/17/models')) return envelope(catalogue());
    if (url === '/api/dashboard/ai/providers') return envelope([pageProvider()]);
    throw new Error('Unexpected ' + url);
  });
  await m.state.saveProvider();
  m.state.probedModels.value[0].checked = false; m.state.probedModels.value[1].checked = true;
  await m.state.retryOnboarding();
  const writes = () => m.calls.filter(call => call.url.endsWith('/models/enabled')).map(call => JSON.parse(call.options.body));
  assert.deepEqual(writes()[0].changes, [{id: 41, enabled: false}, {id: 42, enabled: true}]);
  await m.state.retryOnboarding();
  assert.equal(m.state.onboardingState.value.phase, 'confirm_revision');
  assert.equal(writes().length, 1);
  m.state.probedModels.value[0].checked = true; m.state.probedModels.value[1].checked = false;
  await m.state.confirmOnboardingRevision();
  assert.deepEqual(writes()[1].changes, [{id: 41, enabled: true}, {id: 42, enabled: false}]);
  assert.equal(m.calls.filter(call => call.url === '/api/dashboard/ai/providers' && call.method === 'POST').length, 1);
});
test('actual Provider touched patch preserves zero/false/proxy and omits server metadata or blank Key', async () => {
  const m = mount('dashboard_settings_models.html');
  await m.state.editProvider(pageProvider({unknown_metadata: 'never-submit'}));
  assert.equal(m.state.providerForm.max_retries, 0);
  assert.equal(m.state.providerForm.stream_enabled, false);
  assert.equal(m.state.providerForm.proxy, null);
  m.state.providerForm.name = 'renamed'; m.state.touchProvider('name');
  m.replyWith((url, init) => {
    if (url === '/api/dashboard/ai/providers/17' && init.method === 'PUT') return envelope({warnings: []});
    if (url === '/api/dashboard/ai/providers') return envelope([]);
    throw new Error('Unexpected ' + url);
  });
  assert.equal(await m.state.saveProvider(), true);
  const put = m.calls.find(x => x.method === 'PUT');
  assert.deepEqual(JSON.parse(put.options.body), {name: 'renamed'});
  assert.equal(m.state.providerForm.id, 17);
});
test('workspace guard save failure blocks navigation; explicit discard navigates without a write', async () => {
  const m = mount('dashboard_settings_models.html', '?view=providers&keep=1');
  m.state.providerForm.name = 'unsaved'; m.state.touchProvider('name');
  m.replyWith(() => response({error: 'fixture rejected'}, 400));
  let moving = m.state.navigateWorkspace({view: 'models'});
  await flush(); assert.equal(m.state.draftConfirmation.value, 'provider');
  m.state.resolveDraft('save'); await moving;
  assert.equal(m.navigation.length, 0);
  assert.equal(m.state.providerForm.name, 'unsaved');
  const writes = m.calls.length;
  moving = m.state.navigateWorkspace({view: 'groups'});
  await flush(); m.state.resolveDraft('discard'); await moving;
  assert.deepEqual(m.navigation, ['/dashboard/settings/models?view=groups&keep=1']);
  assert.equal(m.calls.length, writes);
});
test('unload protection is attached only while dirty and removed on unmount', () => {
  const m = mount('dashboard_settings_models.html');
  assert.equal(m.listeners.get('beforeunload')?.size || 0, 0);
  m.state.providerForm.name = 'unsaved'; m.triggerWatches();
  assert.equal(m.listeners.get('beforeunload')?.size, 1);
  m.unmount();
  assert.equal(m.listeners.get('beforeunload')?.size, 0);
});
test('probe selection starts unchecked and old success/error cannot replace a new connection identity', async () => {
  for (const failed of [false, true]) {
    const m = mount('dashboard_settings_models.html');
    await m.state.editProvider(pageProvider());
    const old = later(); m.replyWith(() => old.promise);
    const probing = m.state.probeModels();
    await m.state.editProvider(pageProvider({id: 18, name: 'New editor'}));
    if (failed) old.reject(new Error('404 fixture stale'));
    else old.resolve(envelope({items: [{model_key: 'stale', suggested: true}], latency_ms: 1}));
    await probing;
    assert.equal(m.state.probedModels.value.length, 0);
    assert.equal(m.state.probeRetryWithV1.value, '');
    m.replyWith(() => envelope({items: [{model_key: 'chosen', suggested: true}], latency_ms: 1}));
    await m.state.probeModels();
    assert.equal(m.state.probedModels.value[0].checked, false);
    m.state.providerForm.proxy = 'http://fixture-proxy.invalid'; m.triggerWatches();
    assert.equal(m.state.probedModels.value.length, 0);
  }
});
test('probe preserves explicit clearing of a saved proxy instead of null inheritance', async () => {
  const m = mount('dashboard_settings_models.html');
  await m.state.editProvider(pageProvider({proxy: 'http://saved-proxy.invalid'}));
  m.state.providerForm.proxy = null;
  m.state.touchProvider('proxy');
  m.replyWith(() => envelope({items: [], latency_ms: 1}));
  await m.state.probeModels();
  const probe = m.calls.find(call => call.url.endsWith('/probe-models'));
  assert.equal(JSON.parse(probe.options.body).proxy, '');
});
test('catalog presentation ignores stale search replies and authoritative reads reject incomplete snapshots', async () => {
  const m = mount('dashboard_settings_models.html', '?view=models&provider=17');
  const old = later();
  m.replyWith(url => url.includes('search=old') ? old.promise : envelope(catalogue([], 'c')));
  m.state.modelSearch[17] = 'old'; const first = m.state.loadProviderModels(17);
  m.state.modelSearch[17] = 'new'; await m.state.loadProviderModels(17);
  old.resolve(envelope(catalogue())); await first;
  assert.equal(m.state.providerModels[17].catalog_revision, 'c'.repeat(64));
  m.replyWith(() => envelope({...catalogue(), total: 999}));
  await assert.rejects(m.state.readCatalog(17), /complete|完整/i);
});
test('SSE whitelist handles residual CRLF and EOF reads actual operation instead of claiming success', async () => {
  const m = mount('dashboard_settings_models.html');
  m.replyWith(url => {
    if (url.endsWith('/models/sync')) return envelope({operation_id: 'fixture-operation'});
    if (url.endsWith('/events')) return sse('event: ignored\r\ndata: {"status":"succeeded"}\r\n\r\nevent: page\r\ndata: {"pages":1}');
    if (url.endsWith('/fixture-operation')) return envelope({operation_id: 'fixture-operation', status: 'failed', error_message: 'fixture fail'});
    throw new Error('Unexpected ' + url);
  });
  const operation = await m.state.syncProviderModels(17);
  assert.equal(operation.status, 'failed');
  assert.equal(m.calls.filter(x => x.url.includes('/providers/17/models') && x.method === 'GET').length, 0);
});
test('manual output create/update/reset each has explicit nullable semantics', async () => {
  const m = mount('dashboard_settings_models.html', '?view=models');
  m.replyWith((url, init) => init.method ? envelope({id: 71}) : envelope(catalogue()));
  Object.assign(m.state.manualModelForm(17), {model_key: 'manual', manual_max_output_tokens: '1234', output_touched: true});
  await m.state.createManualModel(17);
  assert.equal(JSON.parse(m.calls[0].options.body).manual_max_output_tokens, 1234);
  const model = {id: 71, manual_max_output_tokens: 1234, discovered_max_output_tokens: 42};
  m.state.modelOutputForm(model).value = ''; m.state.modelOutputForm(model).touched = true;
  const count = m.calls.length;
  await m.state.saveModelOutput(17, model);
  assert.equal(m.calls.length, count, 'blank input is validation error, not reset');
  m.state.modelOutputForm(model).value = '999'; await m.state.saveModelOutput(17, model);
  assert.deepEqual(JSON.parse(m.calls.findLast(x => x.method === 'PUT').options.body), {manual_max_output_tokens: 999});
  await m.state.resetModelOutput(17, model);
  assert.deepEqual(JSON.parse(m.calls.findLast(x => x.method === 'PUT').options.body), {manual_max_output_tokens: null});
});
test('pool conflict retains local draft separately and reapply is explicitly reviewed', async () => {
  const m = mount('dashboard_settings_models.html', '?view=groups');
  Object.assign(m.state.poolForm, pagePool({name: 'My draft'}), {original_enabled: false});
  m.replyWith((url, init) => init.method ? response({error: 'version conflict'}, 409)
    : url.endsWith('/model-pools') ? envelope([pagePool({name: 'Server', version: 8})])
      : envelope(pagePool({name: 'Server', version: 8})));
  assert.equal(await m.state.saveModelPool(), false);
  assert.equal(m.state.poolForm.name, 'My draft');
  assert.equal(m.state.poolForm.version, 2);
  assert.equal(m.state.poolConflict.value.name, 'Server');
  assert.equal(m.state.poolConflict.value.version, 8);
  const count = m.calls.length;
  m.state.reviewPoolChanges();
  assert.equal(m.state.poolForm.version, 8);
  assert.equal(m.state.poolForm.name, 'My draft');
  assert.equal(m.calls.length, count, 'review itself cannot automatically write');
});
test('confirmed provider and pool writes with failed read-back remain saved, not retryable creates', async () => {
  const m = mount('dashboard_settings_models.html');
  Object.assign(m.state.providerForm, pageProvider({id: 0}));
  m.replyWith((url, init) => init.method ? envelope({id: 17}) : response({error: 'refresh failed'}, 503));
  assert.equal(await m.state.saveProvider(), true);
  assert.equal(m.state.providerForm.id, 17);
  assert.equal(m.state.aiMessages['provider-form'].type, 'warning');
  const g = mount('dashboard_settings_models.html', '?view=groups');
  Object.assign(g.state.poolForm, pagePool(), {original_enabled: false});
  g.replyWith((url, init) => init.method ? envelope({version: url.endsWith('/members') ? 4 : 3}) : response({error: 'refresh failed'}, 503));
  assert.equal(await g.state.saveModelPool(), true);
  assert.equal(g.state.poolForm.version, 4);
  assert.equal(g.state.aiMessages['pool-form'].type, 'warning');
});
test('pool create ID survives a failed following read; late disposed writes cannot chain', async () => {
  const m = mount('dashboard_settings_models.html', '?view=groups');
  m.state.poolForm.name = 'Created';
  m.replyWith((url, init) => init.method === 'POST' ? envelope({id: 23}) : response({error: 'read failed'}, 503));
  assert.equal(await m.state.saveModelPool(), false);
  assert.equal(m.state.poolForm.id, 23);
  await m.state.saveModelPool();
  assert.equal(m.calls.filter(x => x.method === 'POST').length, 1);
  const n = mount('dashboard_settings_models.html', '?view=groups');
  Object.assign(n.state.poolForm, pagePool(), {original_enabled: false});
  const held = later(); n.replyWith(() => held.promise);
  const writing = n.state.saveModelPool(); await flush(); n.unmount();
  held.resolve(envelope({version: 3})); await writing;
  assert.equal(n.calls.length, 1);
});
test('finishing an onboarding retry cannot mark later unsaved Provider edits as saved', async () => {
  const m = mount('dashboard_settings_models.html');
  Object.assign(m.state.providerForm, pageProvider({id: 0, name: 'Saved name'}));
  m.state.probedModels.value = [{model_key: 'chosen', checked: true}];
  let operation = 0;
  m.replyWith((url, init) => {
    if (url === '/api/dashboard/ai/providers' && init.method === 'POST') return envelope({id: 17});
    if (url.endsWith('/models/sync')) return envelope({operation_id: 'fixture-' + (++operation)});
    if (url.endsWith('/fixture-1/events')) return sse('event: failed\ndata: {"error_message":"fixture fail"}\n\n');
    if (url.endsWith('/fixture-2/events')) return sse('event: completed\ndata: {}\n\n');
    if (url.endsWith('/models/enabled')) return envelope({});
    if (url.endsWith('/17/models')) return envelope(catalogue());
    if (url === '/api/dashboard/ai/providers') return envelope([pageProvider({name: 'Saved name'})]);
    throw new Error('Unexpected ' + url);
  });
  assert.equal(await m.state.saveProvider(), false);
  m.state.providerForm.name = 'Still unsaved'; m.state.touchProvider('name');
  assert.equal(await m.state.retryOnboarding(), false, 'Selection success is not confirmation of another Provider edit');
  assert.equal(m.state.providerForm.name, 'Still unsaved');
  const moving = m.state.navigateWorkspace({view: 'groups'});
  await flush(); assert.equal(m.state.draftConfirmation.value, 'provider');
  m.state.resolveDraft('cancel'); await moving;
  assert.equal(m.navigation.length, 0);
});
test('provider zero/false edits are sent explicitly and local checks never contact the server', async () => {
  const m = mount('dashboard_settings_models.html');
  await m.state.editProvider(pageProvider({max_retries: 2, stream_enabled: true}));
  m.state.providerForm.max_retries = 0; m.state.touchProvider('max_retries');
  m.state.providerForm.stream_enabled = false; m.state.touchProvider('stream_enabled');
  assert.equal(m.state.checkProviderForm(), true);
  assert.equal(m.calls.length, 0);
  m.replyWith((url, init) => init.method === 'PUT' ? envelope({warnings: []}) : envelope([]));
  await m.state.saveProvider();
  assert.deepEqual(JSON.parse(m.calls[0].options.body), {max_retries: 0, stream_enabled: false});
});
test('workspace filters retain missing provider state and include real fallback membership without mutation', async () => {
  const m = mount('dashboard_settings_models.html', '?view=groups&provider=17');
  m.replyWith(url => {
    if (url === '/api/dashboard/ai/providers') return envelope([pageProvider(), pageProvider({id: 18})]);
    if (/\/providers\/(17|18)\/models$/.test(url)) return envelope(catalogue([]));
    throw new Error('Unexpected ' + url);
  });
  await m.state.loadProviders();
  m.state.modelPools.value = [pagePool({id: 1, fallback_pool_id: 2}), pagePool({id: 2, members: [{provider_id: 17}]}), pagePool({id: 3, members: [{provider_id: 18}]})];
  assert.deepEqual(plain(m.state.visibleModelPools.value.map(row => row.id)), [1, 2]);
  assert.equal(m.state.modelPools.value.length, 3);
  assert.ok(m.calls.every(call => call.method === 'GET'));
  const missing = mount('dashboard_settings_models.html', '?view=models&provider=999');
  missing.replyWith(url => url.endsWith('/providers') ? envelope([pageProvider()]) : envelope(catalogue([])));
  await missing.state.loadProviders();
  assert.equal(missing.state.providerNotFound.value, true);
  assert.equal(missing.state.visibleProviders.value.length, 0);
  assert.equal(missing.state.providerForm.id, 0);
});
test('same-name provider picker selections use server model IDs; disabled/missing members keep real order', async () => {
  const m = mount('dashboard_settings_models.html', '?view=groups');
  m.replyWith(url => {
    if (url.endsWith('/providers')) return envelope([pageProvider(), pageProvider({id: 18})]);
    const id = url.includes('/18/') ? 18 : 17;
    return envelope(catalogue([{id: id === 17 ? 31 : 41, provider_id: id, model_key: 'org/model/v1', display_name: 'Same display name', routable: true}]));
  });
  await m.state.loadProviders();
  const options = m.state.poolPickerOptions.value;
  assert.equal(options.length, 2);
  const before = m.calls.length;
  m.state.choosePoolModel(options[1]);
  m.state.choosePoolModel(options[0]);
  assert.deepEqual(plain(m.state.poolForm.members.map(row => row.provider_model_id)), [41, 31]);
  assert.equal(m.calls.length, before);
  m.state.poolForm.members[0].enabled = false;
  m.state.poolForm.members.push({provider_model_id: 99, provider_id: 17, model_key: 'gone', enabled: true});
  m.state.movePoolMember(2, -1);
  assert.deepEqual(plain(m.state.poolForm.members.map(row => row.provider_model_id)), [41, 99, 31]);
  assert.equal(m.state.poolForm.members[0].enabled, false);
  assert.match(m.state.memberAvailability(m.state.poolForm.members[1]), /不可用/);
  assert.equal(m.calls.length, before);
});
test('provider editor changes invalidate late catalogue replies as well as probes', async () => {
  const m = mount('dashboard_settings_models.html');
  await m.state.editProvider(pageProvider());
  const old = later(); m.replyWith(() => old.promise);
  const reading = m.state.loadProviderModels(17);
  await m.state.editProvider(pageProvider({id: 18}));
  old.resolve(envelope(catalogue())); await reading;
  assert.equal(m.state.providerModels[17], undefined);
});
test('unmount during the real page Provider create prevents chained sync and late Key/form writes', async () => {
  const m = mount('dashboard_settings_models.html');
  Object.assign(m.state.providerForm, pageProvider({id: 0, api_key: 'FAKE-UNMOUNT-KEY'}));
  m.state.probedModels.value = [{model_key: 'chosen', checked: true}];
  const old = later(); m.replyWith(() => old.promise);
  const saving = m.state.saveProvider(); await flush(); m.unmount();
  old.resolve(envelope({id: 17})); await saving;
  assert.equal(m.calls.length, 1);
  assert.equal(m.state.providerForm.id, 0);
  assert.equal(m.state.providerForm.api_key, '');
});
for (const status of [400, 503]) test(`failed existing Provider update ${status} retains the typed replacement Key for an explicit retry`, async () => {
  const m = mount('dashboard_settings_models.html');
  await m.state.editProvider(pageProvider());
  m.state.providerForm.api_key = 'FAKE-REPLACEMENT-KEY'; m.state.touchProvider('api_key');
  m.replyWith(() => response({error: 'fixture rejected'}, status));
  assert.equal(await m.state.saveProvider(), false);
  assert.equal(m.state.providerForm.api_key, 'FAKE-REPLACEMENT-KEY');
  m.replyWith((url, init) => init.method === 'PUT' ? envelope({warnings: []}) : envelope([]));
  assert.equal(await m.state.saveProvider(), true);
  assert.equal(JSON.parse(m.calls.filter(x => x.method === 'PUT').at(-1).options.body).api_key, 'FAKE-REPLACEMENT-KEY');
  assert.equal(m.state.providerForm.api_key, '');
});
test('provider-list errors stay visible in models/groups and cannot become a false empty state', async () => {
  for (const view of ['models', 'groups']) {
    const m = mount('dashboard_settings_models.html', '?view=' + view);
    m.replyWith(() => response({error: 'fixture providers unavailable'}, 503));
    await m.state.loadProviders();
    assert.equal(m.state.providersLoading.value, false);
    assert.equal(m.state.providersLoaded.value, false);
    assert.match(m.state.providerLoadError.value, /fixture providers unavailable/);
    const shared = m.html.split('id="ai-api"')[0];
    assert.match(shared, /v-if="providerLoadError"/);
    assert.match(shared, /@click="loadProviders"/);
    assert.match(m.html, /!visibleProviders\.length[^"\n]+providersLoaded/);
  }
});
test('full picker catalogue errors have a read-only retry rather than silently claiming no models', async () => {
  const m = mount('dashboard_settings_models.html', '?view=groups');
  m.state.providers.value = [pageProvider()];
  m.replyWith(() => response({error: 'fixture full catalogue failed'}, 503));
  await m.state.loadPoolProviderModels();
  assert.match(m.state.poolCatalogErrors[17], /fixture full catalogue failed/);
  assert.ok(m.calls.every(call => call.method === 'GET'));
});
test('model output write owns its row and never overwrites edits made after its snapshot', async () => {
  const m = mount('dashboard_settings_models.html', '?view=models');
  const model = {id: 71, manual_max_output_tokens: 123};
  const form = m.state.modelOutputForm(model);
  form.value = '111'; form.touched = true;
  const held = later(); m.replyWith((url, init) => init.method ? held.promise : envelope(catalogue()));
  const saving = m.state.saveModelOutput(17, model);
  await flush();
  assert.equal(m.state.modelOutputWriting[71], true);
  form.value = '222';
  await m.state.resetModelOutput(17, model);
  assert.equal(m.calls.filter(x => x.method === 'PUT').length, 1);
  held.resolve(envelope({})); await saving;
  assert.equal(m.state.modelOutputForm(model).value, '222');
  assert.equal(m.state.modelOutputForm(model).touched, true);
  assert.equal(m.state.modelOutputWriting[71], false);
});
test('empty-catalogue cancellation refusal cannot become a synthetic cancelled/successful onboarding state', async () => {
  const m = mount('dashboard_settings_models.html');
  Object.assign(m.state.providerForm, pageProvider({id: 0}));
  m.state.probedModels.value = [{model_key: 'chosen', checked: true}];
  m.replyWith((url, init) => {
    if (url === '/api/dashboard/ai/providers') return envelope({id: 17});
    if (url.endsWith('/models/sync')) return envelope({operation_id: 'fixture-empty'});
    if (url.endsWith('/events')) return sse('event: empty_confirmation_required\ndata: {"operation_id":"fixture-empty","generation":1,"result_digest":"fixture"}\n\n');
    if (url.endsWith('/fixture-empty') && init.method === 'DELETE') return envelope({cancel_requested: false});
    if (url.endsWith('/fixture-empty')) return envelope({operation_id: 'fixture-empty', status: 'needs_empty_confirmation'});
    throw new Error('Unexpected ' + url);
  });
  await m.state.saveProvider();
  assert.equal(m.state.onboardingState.value.phase, 'awaiting_empty_confirmation');
  await m.state.cancelModelSync(17);
  assert.equal(m.state.modelSyncOperations[17].status, 'needs_empty_confirmation');
  assert.equal(m.state.onboardingState.value.phase, 'awaiting_empty_confirmation');
  assert.equal(m.calls.filter(call => call.url.endsWith('/models/enabled')).length, 0);
  assert.match(m.state.aiMessages['provider-17'].text, /未接受|不能|未取消/);
});
test('empty confirmation owns its operation and blocks a new sync while its response is pending', async () => {
  const m = mount('dashboard_settings_models.html', '?view=models');
  m.state.modelSyncOperations[17] = {operation_id: 'fixture-empty', status: 'needs_empty_confirmation'};
  m.state.emptyConfirmations[17] = {operation_id: 'fixture-empty', generation: 1, result_digest: 'a'.repeat(64)};
  const held = later();
  m.replyWith((url, init) => url.endsWith('/confirm-empty') ? held.promise : url.endsWith('/17/models') ? envelope(catalogue([]))
    : envelope({operation_id: 'unexpected-sync'}));
  const confirming = m.state.confirmEmptyCatalog(17);
  try {
    await flush();
    await m.state.syncProviderModels(17);
    assert.equal(m.calls.filter(call => call.url.endsWith('/models/sync')).length, 0);
  } finally { held.resolve(envelope({})); await confirming; }
});
test('duplicate operation recovery shares one actual SSE consumer', async () => {
  const m = mount('dashboard_settings_models.html');
  const held = later();
  m.replyWith(url => url.endsWith('/events') ? held.promise : envelope({operation_id: 'fixture-op', status: 'running'}));
  const first = m.state.loadModelSyncOperation(17, 'fixture-op');
  const second = m.state.loadModelSyncOperation(17, 'fixture-op');
  try {
    await flush();
    assert.equal(m.calls.filter(call => call.url.endsWith('/events')).length, 1);
  } finally {
    held.resolve(sse('event: completed\ndata: {"operation_id":"fixture-op"}\n\n'));
    await Promise.all([first, second]);
  }
});
test('an event from another operation cannot make EOF a sync success', async () => {
  const m = mount('dashboard_settings_models.html');
  m.replyWith(url => {
    if (url.endsWith('/models/sync')) return envelope({operation_id: 'fixture-current'});
    if (url.endsWith('/events')) return sse('event: completed\ndata: {"operation_id":"fixture-old"}\n\n');
    if (url.endsWith('/fixture-current')) return envelope({operation_id: 'fixture-current', status: 'failed'});
    if (url.endsWith('/17/models')) return envelope(catalogue());
    throw new Error('Unexpected ' + url);
  });
  const operation = await m.state.syncProviderModels(17);
  assert.equal(operation.status, 'failed');
  assert.equal(m.calls.filter(call => call.url.endsWith('/fixture-current')).length, 1);
  assert.equal(m.calls.filter(call => call.url.endsWith('/17/models')).length, 0);
});
test('manual create owns its form and retains a newer draft after confirmed creation', async () => {
  const m = mount('dashboard_settings_models.html', '?view=models');
  const form = m.state.manualModelForm(17); form.model_key = 'first';
  const held = later(); m.replyWith((url, init) => init.method ? held.promise : envelope(catalogue()));
  const saving = m.state.createManualModel(17);
  await flush();
  assert.equal(m.state.manualModelSaving[17], true);
  form.model_key = 'next';
  await m.state.createManualModel(17);
  assert.equal(m.calls.filter(call => call.method === 'POST').length, 1);
  held.resolve(envelope({id: 71})); await saving;
  assert.equal(m.state.manualModelForm(17).model_key, 'next');
  assert.equal(m.state.manualModelSaving[17], false);
});
test('conflict-state read retry keeps the local pool draft rather than demanding discard', async () => {
  const m = mount('dashboard_settings_models.html', '?view=groups');
  Object.assign(m.state.poolForm, pagePool({name: 'Keep local'}), {original_enabled: false});
  let readFailed = true;
  m.replyWith((url, init) => {
    if (init.method) return response({error: 'fixture conflict'}, 409);
    if (url.endsWith('/model-pools')) return envelope([]);
    return readFailed ? response({error: 'fixture conflict read failed'}, 503) : envelope(pagePool({name: 'Server', version: 9}));
  });
  await m.state.saveModelPool();
  readFailed = false;
  const retry = m.state.retryModelPool();
  await flush();
  if (m.state.draftConfirmation.value) m.state.resolveDraft('cancel');
  await retry;
  assert.equal(m.state.poolConflict.value?.version, 9);
  assert.equal(m.state.poolForm.name, 'Keep local');
  assert.equal(m.calls.filter(call => call.method !== 'GET').length, 1);
});
test('confirmed Provider save warnings remain visible without treating them as failed writes', async () => {
  const m = mount('dashboard_settings_models.html');
  await m.state.editProvider(pageProvider());
  m.state.providerForm.enabled = false; m.state.touchProvider('enabled');
  m.replyWith((url, init) => init.method ? envelope({warnings: [{message: 'Fixture bound tasks need this Provider'}]}) : envelope([]));
  assert.equal(await m.state.saveProvider(), true);
  assert.equal(m.state.aiMessages['provider-form'].type, 'warning');
  assert.match(m.state.aiMessages['provider-form'].text, /Fixture bound tasks/);
});
test('a lost sync transport retains its known operation for reconnection instead of starting another sync', async () => {
  let lost = true;
  const f = onboardingFixture({waitForSync: async () => {
    if (lost) throw new Error('fixture stream disconnected');
    return {status: 'succeeded'};
  }});
  await f.flow.save(fakeForm, ['chosen']);
  assert.equal(f.flow.getState().phase, 'sync_failed');
  assert.equal(f.flow.getState().operationId, 'fixture-operation');
  lost = false; await f.flow.retry();
  assert.equal(f.flow.getState().phase, 'applied');
  assert.equal(f.calls.filter(call => call.url.endsWith('/models/sync')).length, 1);
});

test('rejected sync starts release their false queued state and allow an explicit retry', async () => {
  const m = mount('dashboard_settings_models.html', '?view=models');
  m.replyWith((url, init) => {
    assert.equal(url, '/api/dashboard/ai/providers/17/models/sync');
    assert.equal(init.method, 'POST');
    return response({error: 'fixture start rejected'}, 400);
  });
  const result = await m.state.syncProviderModels(17);
  assert.equal(result.status, 'start_rejected');
  assert.equal(m.state.modelSyncOperations[17].operation_id, null);
  assert.equal(m.state.modelSyncBusy(17), false);
  assert.match(m.state.aiMessages['provider-17'].text, /拒绝/);
  await m.state.cancelModelSync(17);
  assert.equal(m.calls.length, 1, 'There is no operation to cancel');
  await m.state.syncProviderModels(17);
  assert.equal(m.calls.length, 2);
});

test('ambiguous sync starts are not queued or replayed without explicit confirmation', async () => {
  for (const status of [0, 408, 503]) {
    const m = mount('dashboard_settings_models.html', '?view=models');
    m.replyWith((url, init) => {
      assert.equal(url, '/api/dashboard/ai/providers/17/models/sync');
      assert.equal(init.method, 'POST');
      if (!status) throw new Error('fixture disconnected');
      return response({error: 'fixture unconfirmed'}, status);
    });
    const result = await m.state.syncProviderModels(17);
    assert.equal(result.status, 'start_unconfirmed');
    assert.equal(m.state.modelSyncBusy(17), false);
    assert.match(m.state.aiMessages['provider-17'].text, /未确认/);
    assert.match(m.state.aiMessages['provider-17'].text, /operation_id/);
    let confirmations = 0;
    m.window.confirm = text => { confirmations++; assert.match(text, /可能已启动/); return false; };
    await m.state.syncProviderModels(17);
    assert.equal(confirmations, 1);
    assert.equal(m.calls.length, 1, 'Cancelling recovery confirmation cannot repeat POST');
    m.window.confirm = () => true;
    await m.state.syncProviderModels(17);
    assert.equal(m.calls.length, 2, 'Only explicit confirmation permits a new start request');
    assert.equal(m.storageWrites.length, 0, 'No invented operation recovery key');
  }
});

test('sync start conflict reconnects its supplied existing operation without another POST', async () => {
  const m = mount('dashboard_settings_models.html', '?view=models');
  m.replyWith((url, init) => {
    if (url.endsWith('/models/sync')) return response({error: 'fixture active operation', data: {operation_id: 'fixture-existing'}}, 409);
    assert.equal(url, '/api/dashboard/ai/model-sync-operations/fixture-existing');
    assert.equal(init.method, undefined);
    return envelope({operation_id: 'fixture-existing', status: 'needs_empty_confirmation'});
  });
  const result = await m.state.syncProviderModels(17);
  assert.equal(result.status, 'needs_empty_confirmation');
  assert.equal(m.state.modelSyncOperations[17].operation_id, 'fixture-existing');
  assert.equal(m.state.emptyConfirmations[17].operation_id, 'fixture-existing');
  assert.equal(m.calls.filter(call => call.method === 'POST').length, 1);
});

test('recovered successful sync refreshes the displayed catalogue without replaying any write', async () => {
  for (const streamed of [false, true]) {
    const m = mount('dashboard_settings_models.html', '?view=models');
    m.state.providers.value = [pageProvider()];
    m.state.providerModels[17] = catalogue([{id: 1, model_key: 'old'}]);
    m.state.modelSearch[17] = 'chosen';
    m.stored.set('pixiv-ai-model-sync:17', 'fixture-recovered');
    m.replyWith((url, init) => {
      assert.equal(init.method, undefined);
      if (url === '/api/dashboard/ai/model-sync-operations/fixture-recovered') return envelope({operation_id: 'fixture-recovered', status: streamed ? 'running' : 'succeeded'});
      if (url === '/api/dashboard/ai/model-sync-operations/fixture-recovered/events') return sse('event: completed\ndata: {"operation_id":"fixture-recovered"}\n\n');
      assert.equal(url, '/api/dashboard/ai/providers/17/models?search=chosen');
      return envelope(catalogue([{id: 41, model_key: 'chosen'}]));
    });
    await m.state.recoverModelSyncOperations();
    assert.equal(m.state.providerModels[17].items[0].model_key, 'chosen');
    assert.equal(m.state.modelSyncOperations[17].status, 'succeeded');
    assert.equal(m.stored.has('pixiv-ai-model-sync:17'), false);
    assert.equal(m.calls.filter(call => call.url.includes('/17/models?')).length, 1);
    assert.ok(m.calls.every(call => call.method === 'GET'));
    assert.equal(m.state.onboardingState.value.phase, 'idle');
  }
});

test('superseded same-operation recovery cannot warn after a newer catalogue refresh succeeds', async () => {
  const m = mount('dashboard_settings_models.html'), old = later(), latest = later();
  let reads = 0;
  m.replyWith(url => {
    if (url === '/api/dashboard/ai/model-sync-operations/fixture-recovered') return envelope({operation_id: 'fixture-recovered', status: 'succeeded'});
    assert.equal(url, '/api/dashboard/ai/providers/17/models');
    return ++reads === 1 ? old.promise : latest.promise;
  });
  const first = m.state.loadModelSyncOperation(17, 'fixture-recovered'); await flush();
  const second = m.state.loadModelSyncOperation(17, 'fixture-recovered'); await flush();
  latest.resolve(envelope(catalogue([{id: 41, model_key: 'latest'}]))); await second;
  old.resolve(envelope(catalogue([{id: 1, model_key: 'old'}]))); await first;
  assert.equal(m.state.providerModels[17].items[0].model_key, 'latest');
  assert.equal(m.state.aiMessages['provider-17'], undefined);
  assert.ok(m.calls.every(call => call.method === 'GET'));
});

test('recovered success stays successful when its catalogue read-back fails', async () => {
  const m = mount('dashboard_settings_models.html', '?view=models');
  m.state.providerModels[17] = catalogue([{id: 1, model_key: 'retained'}]);
  m.replyWith(url => {
    if (url === '/api/dashboard/ai/model-sync-operations/fixture-recovered') return envelope({operation_id: 'fixture-recovered', status: 'succeeded'});
    assert.equal(url, '/api/dashboard/ai/providers/17/models');
    return response({error: 'fixture catalogue read failed'}, 503);
  });
  const result = await m.state.loadModelSyncOperation(17, 'fixture-recovered');
  assert.equal(result.status, 'succeeded');
  assert.equal(m.state.modelSyncOperations[17].status, 'succeeded');
  assert.equal(m.state.providerModels[17].items[0].model_key, 'retained');
  assert.equal(m.state.aiMessages['provider-17']?.type, 'warning');
  assert.match(m.state.aiMessages['provider-17'].text, /同步已完成.*目录刷新失败/);
  assert.ok(m.calls.every(call => call.method === 'GET'));
});

test('recovered catalogue replies cannot overwrite a newly owned operation', async () => {
  const m = mount('dashboard_settings_models.html', '?view=models');
  m.state.providerModels[17] = catalogue([{id: 1, model_key: 'retained'}]);
  const held = later();
  m.replyWith(url => {
    if (url === '/api/dashboard/ai/model-sync-operations/fixture-recovered') return envelope({operation_id: 'fixture-recovered', status: 'succeeded'});
    assert.equal(url, '/api/dashboard/ai/providers/17/models');
    return held.promise;
  });
  const recovering = m.state.loadModelSyncOperation(17, 'fixture-recovered');
  await flush();
  assert.equal(m.calls.length, 2, 'Recovery must start a presentation refresh');
  m.state.modelSyncOperations[17] = {operation_id: 'fixture-new', status: 'running'};
  held.resolve(envelope(catalogue([{id: 2, model_key: 'stale'}])));
  await recovering;
  assert.equal(m.state.providerModels[17].items[0].model_key, 'retained');
  assert.equal(m.state.modelSyncOperations[17].operation_id, 'fixture-new');
  assert.equal(m.state.aiMessages['provider-17'], undefined);
});

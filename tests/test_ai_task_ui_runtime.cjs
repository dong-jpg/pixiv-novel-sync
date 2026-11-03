const assert = require('node:assert/strict');
const test = require('node:test');
const P = require('../src/pixiv_novel_sync/static/js/ai_preflight.js');
const D = require('../src/pixiv_novel_sync/static/js/ai_job_diagnostics.js');
const {mount, envelope, response, deferred, plain, source, flush} = require('./helpers/management_vm.cjs');

const row = (name, patch = {}) => ({source_index: 0, candidate_index: 0, provider_id: 17,
  provider_name: name, provider_model_id: 41, model_key: 'org/model/v1', display_name: name,
  pool_id: null, pool_name: null, pool_position: null, fallback_depth: 0,
  context_window: 1000000, agent_context_window: 1000000, provider_context_window: 1000000,
  model_context_window: 1000000, max_output_tokens: null, limiting_factors: ['agent', 'provider', 'model'],
  input_tokens: 100, input_budget: 999644, output_reserve: 100, message_overhead: 0,
  safety_margin: 256, estimator_source: 'fixture', exact: false, reason: null, reason_message: '', ...patch});
const plan = name => ({version: 2, planner_version: 'v2', estimator_version: 'fixture', can_run: true,
  allowed_provider_ids: [17], output_reserve: 100, eligible: [row(name)], excluded: [],
  limits: {max_candidate_attempts: 16, max_network_requests: 32, max_resolved_candidates: 64, max_pool_nodes: 8}});

test('complete canonical messages preserve CJK/emoji/tail, zero and explicit provider restrictions', () => {
  const messages = [{role: 'system', content: 'Visible system'}, {role: 'user', content: '\u5c3e\u90e8\ud83d\ude00TAIL'}];
  assert.deepEqual(JSON.parse(P.buildPreflightBody(messages, {temperature: 0, top_p: 0, allowed_provider_ids: []})),
    {messages, temperature: 0, top_p: 0, allowed_provider_ids: []});
  for (const messages of [[{role: 'tool', content: 'x'}], [{role: 'user', content: {text: 'x'}}],
    [{role: 'user', content: 'x', secret: 'x'}], [], Array.from({length: 257}, () => ({role: 'user', content: 'x'}))]) {
    assert.throws(() => P.buildPreflightBody(messages, {}));
  }
  for (const parameters of [{tools: []}, {max_tokens: 0}, {temperature: Infinity}, {allowed_provider_ids: [0]}, null, []]) {
    assert.throws(() => P.buildPreflightBody(messages, parameters));
  }
});
test('16 MiB boundary counts UTF-8 encoded complete JSON rather than characters', () => {
  const empty = P.buildPreflightBody([{role: 'user', content: ''}], {});
  const room = 16 * 1024 * 1024 - Buffer.byteLength(empty);
  const text = '\u4e2d'.repeat(Math.floor(room / 3)) + 'x'.repeat(room % 3);
  const body = P.buildPreflightBody([{role: 'user', content: text}], {});
  assert.equal(Buffer.byteLength(body), 16 * 1024 * 1024);
  assert.throws(() => P.buildPreflightBody([{role: 'user', content: text + 'x'}], {}));
});
test('old success cannot replace newer selection or clear its busy state', async () => {
  const a = deferred(), b = deferred(), calls = [];
  const c = P.createPreflight({request: (url, options) => { calls.push({url, options}); return calls.length === 1 ? a.promise : b.promise; }});
  const first = c.run(1, [{role: 'user', content: 'A'}]);
  const second = c.run(2, [{role: 'user', content: 'B'}]);
  a.resolve(plan('old')); await first;
  assert.equal(c.getState().busy, true);
  b.resolve(plan('new')); await second;
  assert.equal(c.getState().result.eligible[0].provider_name, 'new');
  assert.equal(calls[0].options.signal.aborted, true);
  assert.equal(calls[1].url, '/api/dashboard/ai/agents/2/preflight');
  assert.equal(calls[1].options.method, 'POST');
  assert.equal(Object.hasOwn(c.getState(), 'messages'), false);
});
test('old failed request after new success is ignored; reset/dispose invalidate late requests', async () => {
  const a = deferred(); let n = 0;
  const c = P.createPreflight({request: () => ++n === 1 ? a.promise : Promise.resolve(plan('new'))});
  const first = c.run(1, [{role: 'user', content: 'A'}]);
  await c.run(2, [{role: 'user', content: 'B'}]);
  a.reject(new Error('old failure')); await first;
  assert.equal(c.getState().error, '');
  assert.equal(c.getState().result.eligible[0].provider_name, 'new');
  const b = deferred(), reset = P.createPreflight({request: () => b.promise});
  const pending = reset.run(1, [{role: 'user', content: 'x'}]); reset.reset();
  b.resolve(plan('late')); await pending; assert.equal(reset.getState().result, null);
  reset.dispose(); assert.equal(await reset.run(1, [{role: 'user', content: 'x'}]), undefined);
});
test('non-runnable plan remains a result and HTTP failures have fixed safe text', async () => {
  const c = P.createPreflight({request: async () => ({...plan('excluded'), can_run: false, eligible: []})});
  await c.run(1, [{role: 'user', content: 'x'}]);
  assert.equal(c.getState().result.can_run, false); assert.equal(c.getState().error, '');
  for (const status of [400, 413, 500]) {
    const c = P.createPreflight({request: async () => { throw Object.assign(new Error('PRIVATE BODY'), {status}); }});
    await c.run(1, [{role: 'user', content: 'PRIVATE INPUT'}]);
    assert.ok(c.getState().error); assert.doesNotMatch(c.getState().error, /PRIVATE/);
    assert.equal(c.getState().busy, false);
  }
});
test('server diagnostics preserve zero, unknown, partial reporting and no-network state', () => {
  const data = D.projectJobDiagnostics({validation_status: 'failed', can_continue: false,
    continue_unavailable_reason: 'No replayable input', usage_summary: {known_input_tokens: 0,
      known_output_tokens: 5, known_total_tokens: null, reported_requests: 1, unreported_requests: 1, complete: false}});
  assert.equal(data.usage_summary.known_input_tokens, 0); assert.equal(data.usage_summary.known_total_tokens, null);
  assert.equal(data.usage_status, 'incomplete'); assert.equal(data.validation_status, 'failed');
  assert.equal(data.can_continue, false); assert.equal(D.projectJobDiagnostics({}).usage_status, 'unknown');
  assert.equal(D.projectJobDiagnostics({}).validation_status, 'unknown');
  assert.equal(D.projectJobDiagnostics({usage_summary: {reported_requests: 0, unreported_requests: 0}}).usage_status, 'not_sent');
  assert.equal(D.projectJobDiagnostics({usage_summary: {complete: true, reported_requests: 1}}).usage_status, 'reported');
  assert.equal(D.projectJobDiagnostics({candidate_snapshot: {private: true}}).invocation_plan, null);
});

const agent = {id: 4, name: 'Before', task_type: 'keyword_clean', binding_type: 'fixed', provider_id: 17,
  model: 'old/not-in-catalogue', system_prompt: 'Visible system', temperature: 0, top_p: 0, enabled: false};
function editingFixture() {
  const m = mount('dashboard_settings_agents.html'), writes = [];
  m.replyWith((url, options) => {
    if (url === '/api/dashboard/ai/agents/4' && options.method === 'PUT') { writes.push(JSON.parse(options.body)); return envelope({}); }
    if (url === '/api/dashboard/ai/agents') return envelope([agent]);
    if (url === '/api/dashboard/ai/agents/4/candidates') return envelope({candidates: [], limits: {max_candidate_attempts: 16}});
    if (url === '/api/dashboard/ai/health') return envelope({agents: []});
    throw new Error('Unexpected request ' + url);
  });
  return {m, writes};
}
test('actual Agent name edit does not rewrite hidden values or legacy binding', async () => {
  const {m, writes} = editingFixture(); await m.state.editAgent(agent);
  assert.equal(m.state.agentForm.temperature, 0); assert.equal(m.state.agentForm.top_p, 0);
  assert.equal(m.state.agentForm.enabled, false);
  m.state.agentForm.name = 'After'; m.state.markAgentTouched('name'); m.state.markAgentTouched('private_config');
  await m.state.saveAgent(); assert.deepEqual(writes, [{name: 'After'}]);
});
for (const action of ['edit', 'reset', 'picker']) {
  test(`Agent save owns its editor through deferred list read and blocks ${action}`, async () => {
    const {m, writes} = editingFixture(); await m.state.editAgent(agent);
    m.state.agentForm.name = 'Saved'; m.state.markAgentTouched('name');
    const held = deferred();
    m.replyWith((url, options) => {
      if (options.method === 'PUT') { writes.push(JSON.parse(options.body)); return envelope({}); }
      if (url === '/api/dashboard/ai/agents') return held.promise;
      return envelope({candidates: [], limits: {}});
    });
    const saving = m.state.saveAgent(); await flush();
    try {
      assert.equal(writes.length, 1);
      assert.equal(m.state.agentSaving.value, true);
      const draft = plain(m.state.agentForm);
      if (action === 'edit') m.state.editAgent({...agent, id: 5, name: 'Newer editor'});
      else if (action === 'reset') m.state.resetAgentForm();
      else m.state.chooseAgentBinding({kind: 'model', provider_id: 18, model_key: 'other', availability: 'ready'});
      assert.deepEqual(plain(m.state.agentForm), draft);
    } finally { held.resolve(envelope([{...agent, name: 'Saved'}])); await saving; }
    assert.equal(m.state.agentSaving.value, false);
    assert.equal(m.state.agentForm.id, 0);
    m.state.editAgent({...agent, id: 5, name: 'Newer editor'});
    assert.equal(m.state.agentForm.name, 'Newer editor');
  });
}
test('confirmed Agent create remains saved when its following list read fails', async () => {
  const m = mount('dashboard_settings_agents.html'); let writes = 0;
  Object.assign(m.state.agentForm, {name: 'Saved', provider_id: 17, model: 'model '});
  m.replyWith((url, options) => {
    if (options.method === 'POST') { writes++; return envelope({id: 4}); }
    return response({error: 'read-back unavailable'}, 503);
  });
  assert.equal(await m.state.saveAgent(), true);
  assert.equal(m.state.agentSaving.value, false);
  assert.equal(m.state.agentForm.name, '');
  assert.equal(m.state.aiMessageType.value, 'warning');
  await m.state.saveAgent();
  assert.equal(writes, 1);
});
for (const key of ['model', 'model ', ' vendor/model/v1 ', ' ']) {
  for (const mode of ['picker', 'legacy']) for (const existing of [false, true]) {
    test(`Agent ${existing ? 'update' : 'create'} preserves opaque ${mode} key ${JSON.stringify(key)}`, async () => {
      const m = mount('dashboard_settings_agents.html'), writes = [];
      m.replyWith((url, init) => {
        if (init.method === 'POST' || init.method === 'PUT') { writes.push(JSON.parse(init.body)); return envelope({id: 4}); }
        if (url.endsWith('/candidates')) return envelope({candidates: [], limits: {}});
        if (url.endsWith('/health')) return envelope({agents: []});
        return envelope([]);
      });
      if (existing) await m.state.editAgent(agent);
      else Object.assign(m.state.agentForm, {name: 'Synthetic agent', provider_id: 17});
      m.state.providers.value = [{id: 17, name: 'Fixture', enabled: true}];
      m.state.providerModels[17] = {items: ['model', 'model ', ' vendor/model/v1 ', ' '].map((model_key, i) =>
        ({id: 41 + i, model_key, enabled: true, routable: true}))};
      if (mode === 'picker') m.state.chooseAgentBinding({kind: 'model', provider_id: 17,
        provider_model_id: m.state.providerModels[17].items.find(item => item.model_key === key).id,
        model_key: key, availability: 'ready'});
      else { m.state.agentForm.model = key; m.state.markAgentTouched('model'); }
      assert.equal(await m.state.saveAgent(), true);
      assert.equal(writes[0].model, key);
    });
  }
}
test('explicit picker selection sends a complete tuple; refresh does not select or replace current', async () => {
  const {m, writes} = editingFixture(); await m.state.editAgent(agent);
  m.state.providers.value = [{id: 17, name: 'Fixture A', enabled: true}];
  m.state.providerModels[17] = {items: [{id: 41, model_key: 'new-model', enabled: true, routable: true}]};
  assert.equal(m.state.agentPickerCurrent.value.model_key, agent.model);
  assert.equal(m.state.agentPickerCurrent.value.availability, 'unverified');
  m.state.chooseAgentBinding({kind: 'pool', pool_id: 7, availability: 'ready'});
  await m.state.saveAgent(); assert.deepEqual(writes, [{binding_type: 'pool', model_pool_id: 7}]);
});
test('actual Agent preflight submits only the displayed saved messages and explicit overrides', async () => {
  const m = mount('dashboard_settings_agents.html'), mixed = plan('one-million');
  mixed.excluded = [row('small', {source_index: 1, candidate_index: null, context_window: 128000, reason: 'context_overflow'})];
  m.state.agents.value = [agent]; m.state.candidateAgentId.value = 4;
  m.state.preflightText.value = '\u4e2d\ud83d\ude00TAIL';
  m.state.preflightOverrideDraft.temperature = 0; m.state.setPreflightOverride('temperature', true);
  m.replyWith((url, options) => {
    if (url === '/api/dashboard/ai/agents/4/candidates') return envelope({candidates: [], limits: {max_candidate_attempts: 16}});
    assert.equal(url, '/api/dashboard/ai/agents/4/preflight'); assert.equal(options.method, 'POST');
    assert.deepEqual(JSON.parse(options.body), {messages: [{role: 'system', content: 'Visible system'}, {role: 'user', content: '\u4e2d\ud83d\ude00TAIL'}], temperature: 0});
    return envelope(mixed);
  });
  m.triggerWatches(); await flush();
  const before = m.calls.length;
  await m.state.runPreflight(); assert.equal(m.calls.length - before, 1);
  assert.equal(m.calls.filter(call => call.method === 'POST').length, 1);
  assert.equal(m.state.preflight.result.eligible[0].context_window, 1000000);
  assert.equal(m.state.preflight.result.excluded[0].candidate_index, null);
  m.state.preflightText.value = 'changed'; m.triggerWatches(); assert.equal(m.state.preflight.result, null);
  m.state.setPreflightOverride('temperature', false); assert.deepEqual(plain(m.state.preflightOverrides()), {});
  m.unmount(); assert.equal(m.state.preflightText.value, '');
});
test('actual logs project v1/v2/unavailable plans and never enable continuation', async () => {
  const m = mount('dashboard_logs.html');
  for (const fields of [{snapshot_version: 1, prompt_budget: {input_budget: 700}},
    {snapshot_version: 2, invocation_plan: plan('safe')}, {snapshot_version: null, plan_unavailable_reason: 'Invalid stored plan',
      prompt_budget: {input_budget: 987654321, effective_context_window: 987654322, output_reserve: 987654323}}]) {
    m.replyWith(url => { assert.equal(url, '/api/dashboard/ai/jobs/fixture'); return envelope({job_id: 'fixture', can_continue: false,
      validation_status: 'failed', usage_summary: {known_input_tokens: 0, known_total_tokens: null, reported_requests: 1, unreported_requests: 1, complete: false},
      candidate_snapshot: {SECRET: 'NO'}, input: {fingerprint: 'PRIVATE'}, ...fields}); });
    await m.state.showDetail({job_id: 'fixture', category: 'ai'});
    assert.equal(m.state.jobDiagnostics.value.snapshot_version, fields.snapshot_version);
    assert.equal(m.state.jobDiagnostics.value.usage_summary.known_input_tokens, 0);
    assert.equal(m.state.jobDiagnostics.value.usage_summary.known_total_tokens, null);
    assert.equal(m.state.jobDiagnostics.value.invocation_plan?.eligible[0].provider_name, fields.invocation_plan ? 'safe' : undefined);
    assert.equal(m.state.continueCandidate.value, null);
    const calls = m.calls.length; await m.state.continueWithNextModel(); assert.equal(m.calls.length, calls);
    assert.equal(m.state.showLegacyAiContent.value, false);
  }
  assert.doesNotMatch(source('dashboard_logs.html'), /\{\{ selectedLog\.candidate_snapshot_hash/);
});
test('legacy budget panel requires a positively decoded version1, never an unavailable saved plan', () => {
  const html = source('dashboard_logs.html');
  assert.ok(html.includes('id="job-legacy-budget" v-if="jobDiagnostics.snapshot_version === 1 && selectedLog.prompt_budget"'),
    'Only a positively decoded v1 may show the legacy budget panel');
});
test('late detail/refresh responses cannot replace a new job or reopen a closed modal', async () => {
  const m = mount('dashboard_logs.html'), first = deferred(), second = deferred();
  m.replyWith(url => url.endsWith('/one') ? first.promise : second.promise);
  const a = m.state.showDetail({category: 'ai', job_id: 'one'});
  const b = m.state.showDetail({category: 'ai', job_id: 'two'});
  first.resolve(envelope({job_id: 'one'})); await a; assert.equal(m.state.detailLoading.value, true);
  second.resolve(envelope({job_id: 'two'})); await b; assert.equal(m.state.selectedLog.value.job_id, 'two');
  const late = deferred(); m.replyWith(() => late.promise);
  const c = m.state.refreshAiJobDetail('two'); m.state.closeDetail(); late.resolve(envelope({job_id: 'two'})); await c;
  assert.equal(m.state.modalOpen.value, false); assert.equal(m.state.selectedLog.value, null);
});
test('keyword compatibility shows only bounded keyword input, never audit fingerprints', async () => {
  const m = mount('dashboard_logs.html');
  m.replyWith(() => envelope({task_type: 'keyword_clean', input: {raw_keywords: ['fixture'], tags: ['tag'], fingerprint: 'PRIVATE'},
    output: {keywords: ['fixture'], dropped_sample: [], extra: 'PRIVATE'}, snapshot_version: 2}));
  await m.state.showDetail({category: 'ai', job_id: 'keyword'});
  assert.equal(m.state.showLegacyAiContent.value, true);
  assert.deepEqual(plain(m.state.detailInput.value), {raw_keywords: ['fixture'], tags: ['tag']});
  assert.deepEqual(plain(m.state.detailOutput.value), {keywords: ['fixture'], dropped_sample: []});
});
test('disabled current catalogue choice remains disabled, and explicit fixed binding is complete', async () => {
  const {m, writes} = editingFixture(); await m.state.editAgent(agent);
  m.state.providers.value = [{id: 17, name: 'Fixture', enabled: true}];
  m.state.providerModels[17] = {items: [{id: 41, model_key: agent.model, enabled: false, routable: false}]};
  assert.equal(m.state.agentPickerCurrent.value.availability, 'disabled');
  m.state.chooseAgentBinding({kind: 'model', provider_id: 18, model_key: 'chosen', availability: 'ready'});
  await m.state.saveAgent(); assert.deepEqual(writes, [{binding_type: 'fixed', provider_id: 18, model: 'chosen'}]);
});
test('closing a pending initial job detail invalidates its result and busy state', async () => {
  const m = mount('dashboard_logs.html'), pending = deferred(); m.replyWith(() => pending.promise);
  const request = m.state.showDetail({category: 'ai', job_id: 'late'}); m.state.closeDetail();
  pending.resolve(envelope({job_id: 'late'})); await request;
  assert.equal(m.state.selectedLog.value, null); assert.equal(m.state.detailLoading.value, false);
});
test('late cancellation acknowledgement cannot overwrite a newly selected job', async () => {
  const m = mount('dashboard_logs.html'), pending = deferred();
  m.replyWith((url, options) => options.method === 'POST' ? pending.promise : envelope({job_id: 'new'}));
  m.state.modalOpen.value = true; m.state.selectedLog.value = {job_id: 'old', category: 'ai', status: 'running'};
  const cancelling = m.state.cancelAiJob(); await m.state.showDetail({category: 'ai', job_id: 'new'});
  pending.resolve(envelope({cancel_requested: true})); await cancelling;
  assert.equal(m.state.selectedLog.value.job_id, 'new'); assert.equal(m.state.cancelNotice.value, '');
});

const chain = id => ({agent_id: id, agent_name: 'Fixture task ' + id,
  candidates: [{order: 1, provider_name: 'Fixture provider ' + id, model_key: 'model-' + id}],
  limits: {max_candidate_attempts: 16, max_network_requests: 32, max_resolved_candidates: 64, max_pool_nodes: 8}});
const selectCandidate = (m, id) => { m.state.candidateAgentId.value = id; m.triggerWatches(); };
test('shared candidate selection owns one GET and immediately clears old chain/error at B and zero', async () => {
  const m = mount('dashboard_settings_agents.html'), b = deferred();
  m.replyWith(url => {
    if (url === '/api/dashboard/ai/agents/4/candidates') return envelope(chain(4));
    assert.equal(url, '/api/dashboard/ai/agents/5/candidates'); return b.promise;
  });
  selectCandidate(m, 4); await flush();
  assert.equal(m.state.candidateChain.value.agent_id, 4);
  m.state.candidateChainError.value = 'Previous error';
  selectCandidate(m, 5);
  assert.equal(m.state.candidateChain.value, null); assert.equal(m.state.candidateChainError.value, '');
  assert.equal(m.state.candidateChainLoading.value, true);
  b.resolve(envelope(chain(5))); await flush(); assert.equal(m.state.candidateChain.value.agent_id, 5);
  selectCandidate(m, 0);
  assert.equal(m.state.candidateChain.value, null); assert.equal(m.state.candidateChainLoading.value, false);
  assert.deepEqual(m.calls.map(call => call.url), ['/api/dashboard/ai/agents/4/candidates', '/api/dashboard/ai/agents/5/candidates']);
});
test('old candidate success/finally cannot replace a newer pending selection', async () => {
  const m = mount('dashboard_settings_agents.html'), a = deferred(), b = deferred();
  m.replyWith(url => url.endsWith('/4/candidates') ? a.promise : b.promise);
  selectCandidate(m, 4); selectCandidate(m, 5);
  a.resolve(envelope(chain(4))); await flush();
  assert.equal(m.state.candidateChain.value, null); assert.equal(m.state.candidateChainLoading.value, true);
  b.resolve(envelope(chain(5))); await flush(); assert.equal(m.state.candidateChain.value.agent_id, 5);
});
test('old candidate error cannot replace a newer successful selection', async () => {
  const m = mount('dashboard_settings_agents.html'), a = deferred();
  m.replyWith(url => url.endsWith('/4/candidates') ? a.promise : envelope(chain(5)));
  selectCandidate(m, 4); selectCandidate(m, 5); await flush();
  a.resolve(response({ok: false, error: 'Obsolete error'}, 503)); await flush();
  assert.equal(m.state.candidateChain.value.agent_id, 5); assert.equal(m.state.candidateChainError.value, '');
  assert.equal(m.state.candidateChainLoading.value, false);
});
for (const close of ['zero', 'unmount']) for (const outcome of ['success', 'error']) {
  test(`candidate ${close} invalidates a pending ${outcome} and releases loading`, async () => {
    const m = mount('dashboard_settings_agents.html'), pending = deferred(); m.replyWith(() => pending.promise);
    selectCandidate(m, 4); assert.equal(m.state.candidateChainLoading.value, true);
    if (close === 'zero') selectCandidate(m, 0); else m.unmount();
    assert.equal(m.state.candidateChainLoading.value, false);
    if (outcome === 'success') pending.resolve(envelope(chain(4))); else pending.reject(new Error('Obsolete error'));
    await flush(); assert.equal(m.state.candidateChain.value, null); assert.equal(m.state.candidateChainError.value, '');
  });
}
test('changed-ID edit, same-ID edit, explicit refresh and post-save keep one intended candidate GET each', async () => {
  const {m, writes} = editingFixture();
  await m.state.editAgent(agent); m.triggerWatches(); await flush();
  assert.equal(m.calls.filter(call => call.url.endsWith('/candidates')).length, 1);
  await m.state.editAgent(agent); m.triggerWatches(); await flush();
  assert.equal(m.calls.filter(call => call.url.endsWith('/candidates')).length, 2);
  await m.state.loadCandidateChain(4);
  assert.equal(m.calls.filter(call => call.url.endsWith('/candidates')).length, 3);
  m.state.agentForm.name = 'Saved'; m.state.markAgentTouched('name'); await m.state.saveAgent(); m.triggerWatches(); await flush();
  assert.equal(m.calls.filter(call => call.url.endsWith('/candidates')).length, 4);
  assert.deepEqual(writes, [{name: 'Saved'}]);
});

(function (root) {
  'use strict';
  const BASE = '/api/dashboard/preference-learning';
  const budgetKeys = ['request_limit', 'token_limit', 'active_seconds_limit'];
  const workLimits = [[1, 512], [1, 100000000], [30, 14400]];
  const batchLimits = [[1, 1000000], [1, 1000000000000], [30, 31536000]];
  const approvals = {profile: ['external_send_confirmed', 'budget_confirmed'],
    library: ['external_send_confirmed', 'budget_confirmed', 'current_archive_only', 'order_confirmed']};
  const copy = value => JSON.parse(JSON.stringify(value));
  const blankBudget = () => Object.fromEntries(budgetKeys.map(key => [key, '']));
  const opaqueId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,80}$/.test(value);
  const activeProfiles = ['queued', 'running'];
  const activeBatches = ['queued', 'scanning', 'waiting'];
  function integer(value, low, high) {
    const text = String(value);
    if (!/^[0-9]+$/.test(text)) throw new Error('请输入范围内的整数。');
    const number = Number(text);
    if (!Number.isSafeInteger(number) || number < low || number > high) throw new Error('数值超出允许范围。');
    return number;
  }
  function budget(value, limits = workLimits) {
    return Object.fromEntries(budgetKeys.map((key, index) => [key, integer(value[key], ...limits[index])]));
  }
  function safeError(error) {
    if (error && [401, 403].includes(error.status)) return '登录或 CSRF 校验失败，请重新登录。';
    if (error && error.status === 409) return '数据、配置或版本已变化，请刷新后重新预览确认。';
    if (error && error.status === 410) return '记录已删除，无法继续使用。';
    if (error && error.status === 413) return '输入或结果超过上限，请缩小范围。';
    if (error && error.code) return '请求未完成：' + error.code;
    return '连接失败或结果未知，请先核对已有记录。';
  }
  async function request(url, options = {}) {
    const response = await root.csrfFetch(url, {credentials: 'same-origin', cache: 'no-store', ...options});
    let value;
    try { value = await response.json(); }
    catch (_) { throw Object.assign(new Error('invalid_response'), {status: null}); }
    if (!response.ok || value.ok !== true || !Object.prototype.hasOwnProperty.call(value, 'data')) {
      const code = typeof value.code === 'string' && /^[a-z][a-z0-9_]{0,79}$/.test(value.code) ? value.code : null;
      throw Object.assign(new Error('request_failed'), {status: response.status, code});
    }
    return value.data;
  }
  function statusText(value) {
    return {queued: '排队中', running: '生成中', succeeded: '已完成', failed: '失败', cancelled: '已取消',
      invalidated: '依据已失效', deleted: '已删除', scanning: '发现作品中', waiting: '等待作品完成',
      completed: '批次发现已结束', paused: '已暂停', created: '已建立分析', reused: '复用已有结果',
      skipped: '已跳过', error: '无法分析'}[value] || value || '未知';
  }
  function budgetText(value) {
    if (!value) return '尚未设置预算';
    return value.request_limit + ' 次请求 / ' + value.token_limit + ' 计划 token / ' + value.active_seconds_limit + ' 活跃秒';
  }
  function warningText(value) {
    return {explicit_reading_feedback_only: '仅把明确阅读评价作为个人喜好依据。',
      estimated_not_billed_tokens: '计划 token 是估算额度，实际账单以服务商为准。',
      whole_work_reaction_is_not_causation: '对整部作品的喜恶不证明其中每个情节都是喜恶原因。',
      feedback_without_current_report: '部分已评价作品缺少有效报告，暂不纳入画像。',
      no_verified_feedback_sources: '没有同时具备明确评价和有效报告的作品，暂不能生成画像。',
      budget_below_known_minimum: '预算低于当前预检最低预留，请调整后重新预览。',
      summary_capacity_exceeded: '当前模型容量不足以容纳完整画像依据，请调整配置。',
      legacy_repreview_required: '所选服务配置需要完成本地升级，当前预览不能开始；更新后重新预览。',
      current_archive_only: '只分析现有归档，不自动补齐缺失正文。',
      allocated_limits_are_not_refunded: '各作品分配的预算计入累计授权，不因跳过后续请求或中断而自动退回。',
      interrupted_calls_require_explicit_resume: '中断的调用不会自动重发，恢复仍受累计预算约束。',
      new_since_policy_first_enabled: '自动新增范围从首次开启时算起，之后保留这个起点。',
      new_since_library_tracking_started: '手动新增范围从本功能开始跟踪时算起。'}[value] || value;
  }
  function createController(Vue, send = request) {
    const pl = Vue.reactive({
      form: {profile: {agent_id: '', output_tokens: '4000', budget: blankBudget()},
        library: {mode: 'manual', scope: 'all', agent_id: '', output_tokens: '4000', work_budget: blankBudget(),
          batch_budget: blankBudget(), work_limit: '', interval_minutes: '60'}},
      confirmations: Object.fromEntries(Object.entries(approvals).map(([kind, keys]) =>
        [kind, Object.fromEntries(keys.map(key => [key, false]))])),
      agents: [], selectedAgents: {profile: null, library: null},
      previews: {profile: null, library: null}, previewFor: {profile: '', library: ''},
      profiles: {feedback_counts: {}, eligible_works: 0, missing_reports: 0, runs: [], active_run_id: null},
      library: {policy: null, batches: []}, selectedProfileId: '', profile: null,
      selectedBatchId: '', batch: null, items: [], total: 0, offset: 0,
      busy: {}, errors: {}, pending: null, notice: '', writeError: '', admissionReady: true,
      activateConfirmed: false, deleteConfirmed: false,
    });
    let live = true, timer = null;
    const owners = new Map(), knownProfiles = new Map(), knownBatches = new Map();
    const stops = [];
    const identity = kind => JSON.stringify(pl.form[kind]);
    function cancelRead(scope) {
      const owner = owners.get(scope);
      if (owner) owner.abort();
      owners.delete(scope); pl.busy[scope] = false;
    }
    function invalidate(kind) {
      cancelRead('preview:' + kind);
      pl.previews[kind] = null; pl.previewFor[kind] = '';
      for (const key of approvals[kind]) pl.confirmations[kind][key] = false;
    }
    for (const kind of ['profile', 'library']) stops.push(Vue.watch(() => identity(kind), () => invalidate(kind), {flush: 'sync'}));
    function currentAgent(kind) {
      const found = pl.agents.find(row => String(row.agent_id) === String(pl.form[kind].agent_id));
      return found || (pl.selectedAgents[kind] ? {...pl.selectedAgents[kind], availability: 'unavailable', reason: '当前 Agent 已移除，请重新选择'} : null);
    }
    function chooseAgent(kind, row) {
      if (!live || pl.pending || !approvals[kind] || !row || row.kind !== 'agent' || row.availability !== 'ready') return false;
      pl.selectedAgents[kind] = copy(row); pl.form[kind].agent_id = String(row.agent_id);
      return true;
    }
    async function read(scope, url, apply) {
      if (!live) return false;
      cancelRead(scope);
      const owner = new AbortController(); owners.set(scope, owner); pl.busy[scope] = true; pl.errors[scope] = '';
      try {
        const value = await send(url, {signal: owner.signal});
        if (!live || owners.get(scope) !== owner) return false;
        apply(value); return true;
      } catch (error) {
        if (live && owners.get(scope) === owner && error.name !== 'AbortError') pl.errors[scope] = safeError(error);
        return false;
      } finally {
        if (live && owners.get(scope) === owner) { owners.delete(scope); pl.busy[scope] = false; }
      }
    }
    function rememberProfile(row, except) {
      const id = String(row.run_id), old = knownProfiles.get(id);
      const newest = old && (old.status === 'deleted' || old.version > row.version) ? old : row;
      knownProfiles.set(id, newest);
      if (pl.selectedProfileId === id) {
        const detail = pl.profile && pl.profile.run_id === id ? pl.profile : null;
        pl.profile = {...detail, ...newest};
        if (newest.status !== 'succeeded') {
          delete pl.profile.result; delete pl.profile.sources;
          if (except !== 'profile') cancelRead('profile');
        }
        if (newest.status === 'deleted' || newest.stale) { pl.activateConfirmed = false; pl.deleteConfirmed = false; }
      }
      return newest;
    }
    function rememberBatch(row) {
      const id = String(row.batch_id), old = knownBatches.get(id);
      const newest = old && old.version > row.version ? old : row;
      knownBatches.set(id, newest);
      if (pl.selectedBatchId === id) pl.batch = newest;
      return newest;
    }
    function rememberPolicy(row) {
      if (!row) return;
      if (!pl.library.policy || row.version >= pl.library.policy.version) pl.library.policy = row;
    }
    function applyState(value) {
      pl.admissionReady = value.admission_ready !== false;
      const profiles = value.profiles || {}, library = value.library || {};
      pl.profiles = {...profiles, runs: (profiles.runs || []).map(row => rememberProfile(row))};
      const policy = pl.library.policy;
      pl.library = {...library, policy, batches: (library.batches || []).map(rememberBatch)};
      rememberPolicy(library.policy);
    }
    function stopPolling() { if (timer !== null) root.clearTimeout(timer); timer = null; }
    function poll() {
      stopPolling();
      const active = pl.profiles.runs.some(row => activeProfiles.includes(row.status)) ||
        pl.library.batches.some(row => activeBatches.includes(row.status)) || pl.library.policy?.enabled;
      if (!live || root.document.hidden || !active) return;
      timer = root.setTimeout(async () => {
        timer = null; await refreshState();
        if (pl.selectedBatchId && pl.batch && activeBatches.includes(pl.batch.status)) await loadBatch(pl.offset);
        poll();
      }, 4000);
    }
    async function refreshState() {
      const loaded = await read('state', BASE + '/state', applyState);
      if (loaded && pl.selectedProfileId && pl.profile?.status !== 'deleted' &&
          !pl.profiles.runs.some(row => String(row.run_id) === pl.selectedProfileId)) {
        // Metadata lists omit deleted (and older) rows. Clear the visible copy
        // before resolving the selected identity; a missing row is not success.
        cancelRead('profile'); pl.profile = null;
        await loadProfile();
      }
      poll(); return loaded;
    }
    async function reloadAgents() {
      return read('agents', '/api/dashboard/ai/agents', rows => {
        pl.agents = rows.map(row => ({kind: 'agent', agent_id: row.id, label: row.name,
          provider_name: 'Agent #' + row.id, availability: row.enabled ? 'ready' : 'disabled',
          reason: row.enabled ? '服务目标以预览为准' : 'Agent 已禁用'}));
        invalidate('profile'); invalidate('library');
      });
    }
    function payload(kind) {
      const form = pl.form[kind], agent = currentAgent(kind);
      if (!agent || agent.availability !== 'ready') throw new Error('请选择当前可用的 Agent。');
      const result = {agent_id: integer(form.agent_id, 1, Number.MAX_SAFE_INTEGER),
        output_tokens: integer(form.output_tokens, 1, 1000000), confirmations: copy(pl.confirmations[kind])};
      if (kind === 'profile') return {...result, budget: budget(form.budget)};
      if (!['all', 'new'].includes(form.scope) || !['manual', 'policy'].includes(form.mode)) throw new Error('请选择有效的归档范围和运行方式。');
      const workBudget = budget(form.work_budget), batchBudget = budget(form.batch_budget, batchLimits);
      if (budgetKeys.some(key => batchBudget[key] < workBudget[key])) throw new Error('累计预算的每项上限都须覆盖至少一个作品预算。');
      return {...result, scope: form.scope, work_budget: workBudget, batch_budget: batchBudget,
        work_limit: integer(form.work_limit, 1, 100000), interval_minutes: integer(form.interval_minutes, 1, 10080)};
    }
    async function preview(kind) {
      if (!live || pl.pending || !approvals[kind]) return false;
      invalidate(kind);
      let body;
      try { body = payload(kind); } catch (error) { pl.writeError = error.message; return false; }
      if (kind === 'library') body.purpose = pl.form.library.mode;
      const submittedFor = identity(kind), scope = 'preview:' + kind;
      pl.writeError = ''; cancelRead(scope);
      const owner = new AbortController(); owners.set(scope, owner); pl.busy[scope] = true; pl.errors[scope] = '';
      try {
        const value = await send(BASE + (kind === 'profile' ? '/profile-preview' : '/batch-preview'),
          {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), signal: owner.signal});
        if (!live || owners.get(scope) !== owner || identity(kind) !== submittedFor) return false;
        pl.previews[kind] = value; pl.previewFor[kind] = submittedFor;
        for (const key of approvals[kind]) pl.confirmations[kind][key] = false;
        return true;
      } catch (error) {
        if (live && owners.get(scope) === owner && error.name !== 'AbortError') pl.errors[scope] = safeError(error);
        return false;
      } finally {
        if (live && owners.get(scope) === owner) { owners.delete(scope); pl.busy[scope] = false; }
      }
    }
    function canCreate(kind) {
      const value = pl.previews[kind];
      if (kind === 'library' && pl.form.library.mode === 'policy' && !pl.library.policy) return false;
      return live && pl.admissionReady && !pl.pending && !pl.busy['preview:' + kind] && !!value && value.can_create === true &&
        pl.previewFor[kind] === identity(kind) && currentAgent(kind)?.availability === 'ready' &&
        approvals[kind].every(key => pl.confirmations[kind][key] === true);
    }
    function freshKey() {
      return Array.from(root.crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join('');
    }
    async function selectProfile(id) {
      if (!live || pl.pending || !opaqueId(id)) return false;
      cancelRead('profile'); pl.selectedProfileId = id; pl.profile = knownProfiles.get(id) || null;
      pl.activateConfirmed = false; pl.deleteConfirmed = false;
      return loadProfile();
    }
    async function loadProfile() {
      const id = pl.selectedProfileId;
      if (!opaqueId(id) || pl.profile?.status === 'deleted') return false;
      return read('profile', BASE + '/profiles/' + encodeURIComponent(id), value => {
        if (pl.selectedProfileId !== id || String(value.run_id) !== id) return;
        rememberProfile(value, 'profile');
      });
    }
    async function selectBatch(id) {
      if (!live || pl.pending || !opaqueId(id)) return false;
      cancelRead('batch'); pl.selectedBatchId = id; pl.batch = knownBatches.get(id) || null;
      pl.items = []; pl.total = 0; pl.offset = 0;
      return loadBatch(0);
    }
    async function loadBatch(offset = pl.offset) {
      const id = pl.selectedBatchId;
      if (!opaqueId(id)) return false;
      return read('batch', BASE + '/batches/' + encodeURIComponent(id) + '?offset=' + offset + '&limit=20', value => {
        if (pl.selectedBatchId !== id || String(value.batch.batch_id) !== id) return;
        const newest = rememberBatch(value.batch);
        if (newest.version > value.batch.version) return;
        pl.items = value.items; pl.total = value.total; pl.offset = value.offset;
      });
    }
    function profileAllowed(action) {
      const row = pl.profile;
      if (!live || pl.pending || !row || row.status === 'deleted') return false;
      if (action === 'activate') return row.status === 'succeeded' && !row.stale && !row.active && pl.activateConfirmed;
      if (action === 'delete') return pl.deleteConfirmed;
      return action === 'cancel' && activeProfiles.includes(row.status);
    }
    function batchAllowed(action) {
      if (!live || pl.pending || !pl.batch) return false;
      if (action === 'pause') return activeBatches.includes(pl.batch.status);
      if (action === 'resume') return pl.admissionReady && ['paused', 'cancelled'].includes(pl.batch.status);
      return action === 'cancel' && [...activeBatches, 'paused'].includes(pl.batch.status);
    }
    function accept(operation, value) {
      if (operation.entity === 'profile') {
        pl.selectedProfileId = String(value.run_id); rememberProfile(value);
        pl.activateConfirmed = false; pl.deleteConfirmed = false;
      } else if (operation.entity === 'batch') {
        pl.selectedBatchId = String(value.batch_id); rememberBatch(value);
      } else rememberPolicy(value);
      if (operation.draft) invalidate(operation.draft);
      pl.pending = null; pl.writeError = ''; pl.notice = '操作已确认。';
    }
    async function refreshAfterWrite() {
      const good = await refreshState();
      if (live && !good) pl.notice = '操作已确认，刷新失败；请重试读取，不要重复提交。';
      if (pl.selectedBatchId) await loadBatch(0);
    }
    async function sendPending() {
      const operation = pl.pending;
      if (!live || !operation || operation.sending) return false;
      cancelRead('reconcile'); operation.sending = true; operation.phase = 'sending'; pl.writeError = '';
      try {
        const value = await send(operation.url, {method: operation.method,
          headers: {'Content-Type': 'application/json'}, body: operation.body});
        if (!live || pl.pending !== operation) return false;
        accept(operation, value); await refreshAfterWrite(); return true;
      } catch (error) {
        if (!live || pl.pending !== operation) return false;
        if (error.status == null || error.status >= 500) {
          operation.phase = 'unknown'; pl.writeError = '提交结果未知，已保留原请求。请核对或重试同一创建。';
        } else {
          pl.pending = null; pl.writeError = safeError(error);
          if (operation.draft) invalidate(operation.draft);
          await refreshState();
        }
        return false;
      } finally { if (live && pl.pending === operation) operation.sending = false; }
    }
    function startOperation(operation, body) {
      if (!live || pl.pending) return Promise.resolve(false);
      pl.pending = {...operation, body: JSON.stringify(body), phase: 'ready', sending: false, canRetry: !!operation.create};
      return sendPending();
    }
    async function create(kind) {
      if (!canCreate(kind)) { if (live) pl.writeError = '预览已失效或确认未完成，请重新预览并确认。'; return false; }
      let body;
      try { body = payload(kind); } catch (error) { pl.writeError = error.message; return false; }
      body.preview_fingerprint = pl.previews[kind].preview_fingerprint;
      if (kind === 'library' && pl.form.library.mode === 'policy') {
        if (!pl.library.policy) return false;
        body.enabled = true; body.expected_version = pl.library.policy.version;
        return startOperation({entity: 'policy', action: 'enable', method: 'PUT', url: BASE + '/automation',
          draft: kind, expectedVersion: body.expected_version}, body);
      }
      body.idempotency_key = freshKey();
      return startOperation({entity: kind === 'profile' ? 'profile' : 'batch', action: 'create', create: true,
        draft: kind, method: 'POST', url: BASE + (kind === 'profile' ? '/profiles' : '/batches')}, body);
    }
    function controlProfile(action) {
      if (!profileAllowed(action)) return Promise.resolve(false);
      const id = pl.profile.run_id, version = pl.profile.version;
      return startOperation({entity: 'profile', action, id, expectedVersion: version,
        method: action === 'delete' ? 'DELETE' : 'POST',
        url: BASE + '/profiles/' + encodeURIComponent(id) + (action === 'delete' ? '' : '/' + action)}, {expected_version: version});
    }
    function controlBatch(action) {
      if (!batchAllowed(action)) return Promise.resolve(false);
      const id = pl.batch.batch_id, version = pl.batch.version;
      return startOperation({entity: 'batch', action, id, expectedVersion: version, method: 'POST',
        url: BASE + '/batches/' + encodeURIComponent(id) + '/' + action}, {expected_version: version});
    }
    function disableAutomation() {
      if (!live || pl.pending || !pl.library.policy?.enabled) return Promise.resolve(false);
      const version = pl.library.policy.version;
      return startOperation({entity: 'policy', action: 'disable', expectedVersion: version, method: 'PUT', url: BASE + '/automation'},
        {enabled: false, expected_version: version});
    }
    function editAutomation() {
      if (!live || pl.pending || !pl.library.policy?.configuration) return false;
      const config = pl.library.policy.configuration;
      for (const key of ['scope', 'agent_id', 'output_tokens', 'work_budget', 'batch_budget', 'work_limit', 'interval_minutes']) {
        if (Object.prototype.hasOwnProperty.call(config, key)) pl.form.library[key] = copy(config[key]);
      }
      pl.form.library.mode = 'policy'; invalidate('library'); return true;
    }
    async function reconcile() {
      const operation = pl.pending;
      if (!live || !operation || operation.create || operation.phase !== 'unknown' || operation.sending) return false;
      operation.canRetry = false;
      const url = operation.entity === 'policy' ? BASE + '/state' : BASE + '/' +
        (operation.entity === 'profile' ? 'profiles/' : 'batches/') + encodeURIComponent(operation.id);
      return read('reconcile', url, value => {
        if (pl.pending !== operation || operation.sending || operation.phase !== 'unknown') return;
        const row = operation.entity === 'policy' ? value.library.policy : operation.entity === 'batch' ? value.batch : value;
        const current = operation.entity === 'policy' ? pl.library.policy : operation.entity === 'batch'
          ? knownBatches.get(operation.id) : knownProfiles.get(operation.id);
        if (row.version < Math.max(operation.expectedVersion, current?.version || 0)) {
          pl.notice = '读取返回较旧版本，请再次核对。'; return;
        }
        if (operation.entity === 'policy') rememberPolicy(row);
        else if (operation.entity === 'batch') rememberBatch(row);
        else rememberProfile(row);
        if (row.version === operation.expectedVersion) {
          operation.canRetry = true; pl.notice = '版本尚未变化，可以重试同一请求。'; return;
        }
        pl.pending = null; pl.activateConfirmed = false; pl.deleteConfirmed = false;
        if (operation.draft) invalidate(operation.draft);
        pl.writeError = ''; pl.notice = '当前状态已核对；原提交结果仍未知，未重复提交。'; poll();
      });
    }
    function retryUnknown() {
      const operation = pl.pending;
      if (!operation || operation.phase !== 'unknown' || !operation.canRetry || pl.busy.reconcile) return Promise.resolve(false);
      if (!operation.create) {
        const row = operation.entity === 'policy' ? pl.library.policy : operation.entity === 'batch'
          ? knownBatches.get(operation.id) : knownProfiles.get(operation.id);
        if (!row || row.version !== operation.expectedVersion) return Promise.resolve(false);
      }
      return sendPending();
    }
    function beforeUnload(event) { if (pl.pending) { event.preventDefault(); event.returnValue = ''; } }
    function suspend() { stopPolling(); for (const scope of Array.from(owners.keys())) cancelRead(scope); }
    function restore() { if (live) { refreshState(); reloadAgents(); } }
    function visibility() { if (root.document.hidden) stopPolling(); else if (live) refreshState(); }
    function dispose() {
      if (!live) return;
      live = false; suspend(); for (const stop of stops) if (typeof stop === 'function') stop();
    }
    const feedback = root.PreferenceFeedback ? root.PreferenceFeedback.createController(Vue, 'novel', '', undefined, refreshState) : null;
    async function mount() {
      await Promise.all([reloadAgents(), refreshState()]);
      const selected = new URLSearchParams(root.location?.search || '').get('profile_run_id');
      if (selected && opaqueId(selected)) await selectProfile(selected);
    }
    return {pl, feedback, currentAgent, chooseAgent, reloadAgents, refreshState, preview, canCreate, create,
      selectProfile, loadProfile, profileAllowed, controlProfile, selectBatch, loadBatch, batchAllowed, controlBatch,
      disableAutomation, editAutomation, reconcile, retryUnknown, statusText, budgetText, warningText,
      profileSourceHref: row => row?.report_run_id ? '/dashboard/preferences?view=work-analysis&analysis_id=' + encodeURIComponent(row.report_run_id) : '#',
      profileEvidenceHref: (source, evidence) => '/dashboard/novels/' + encodeURIComponent(evidence.novel_id) +
        '?analysis_id=' + encodeURIComponent(source.report_run_id) + '&evidence_id=' + encodeURIComponent(evidence.evidence_id) + '#work-analysis-evidence',
      workHref: row => row?.run_id ? '/dashboard/preferences?view=work-analysis&analysis_id=' + encodeURIComponent(row.run_id) : '#',
      beforeUnload, suspend, restore, visibility, dispose,
      mount};
  }
  root.PreferenceLearning = {createController, request, statusText, budgetText};
})(window);

(function (root) {
  'use strict';
  const BASE = '/api/dashboard/work-analyses';
  const approvals = ['order_confirmed', 'current_archive_only', 'external_send_confirmed', 'budget_confirmed'];
  const copy = value => JSON.parse(JSON.stringify(value));
  const hash32 = value => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value);
  const hash64 = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
  const budgetKeys = ['request_limit', 'token_limit', 'active_seconds_limit'];
  const limits = {request_limit: [1, 512], token_limit: [1, 100000000], active_seconds_limit: [30, 14400]};
  function integer(value, low, high) {
    const raw = String(value);
    if (!/^[1-9][0-9]*$/.test(raw)) throw new Error('请输入有限正整数');
    const result = Number(raw);
    if (!Number.isSafeInteger(result) || result < low || result > high) throw new Error('数值超出允许范围');
    return result;
  }
  function budget(value) {
    return Object.fromEntries(budgetKeys.map(key => [key, integer(value[key], ...limits[key])]));
  }
  function newKey() {
    return Array.from(root.crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join('');
  }
  function safeError(error) {
    if (error && (error.status === 401 || error.status === 403)) return '登录或 CSRF 校验失败，请重新登录后重试。';
    if (error && error.status === 410) return '分析已删除，或当前正文版本不再支持此证据定位。';
    if (error && error.status === 409) return '状态、正文、配置或预览已变化，请刷新并重新确认。';
    if (error && error.status === 413) return '输入或产物超过上限，未进行裁剪。';
    if (error && error.code) return '请求未完成：' + error.code;
    return '连接失败或结果未知；请先核对已有任务，不要另建重复分析。';
  }
  async function request(url, options) {
    const response = await root.csrfFetch(url, {credentials: 'same-origin', cache: 'no-store', ...options});
    let result;
    try { result = await response.json(); }
    catch (_error) { throw Object.assign(new Error('invalid_response'), {status: null}); }
    if (!response.ok || result.ok !== true || !Object.prototype.hasOwnProperty.call(result, 'data')) {
      const code = typeof result.code === 'string' && /^[a-z][a-z0-9_]{0,79}$/.test(result.code) ? result.code : null;
      throw Object.assign(new Error('request_failed'), {status: response.status, code});
    }
    return result.data;
  }
  function sourceFromSearch(search) {
    const params = new URLSearchParams(search);
    return {source_kind: params.get('source_kind') === 'series' ? 'series' : 'novel',
      source_id: params.get('source_id') || '', analysis_id: params.get('analysis_id') || ''};
  }
  function partsFromEvidence(value) {
    const evidence = value && value.evidence, jump = value && value.jump;
    if (!evidence || !jump || evidence.novel_id !== jump.novel_id || evidence.text_hash !== jump.text_hash ||
        evidence.start !== jump.start || evidence.end !== jump.end || !hash64(jump.text_hash) ||
        ![jump.start, jump.end, jump.context_start].every(Number.isSafeInteger) ||
        typeof jump.context_text !== 'string') throw new Error('invalid_evidence');
    const chars = Array.from(jump.context_text), start = jump.start - jump.context_start, end = jump.end - jump.context_start;
    if (chars.length > 496 || jump.context_start < 0 || start < 0 || start > 128 ||
        start >= end || end > chars.length || chars.length - end > 128 || end - start > 240 ||
        chars.slice(start, end).join('') !== evidence.quote) throw new Error('invalid_evidence');
    return {before: chars.slice(0, start).join(''), quote: chars.slice(start, end).join(''),
      after: chars.slice(end).join('')};
  }
  function usageText(value) {
    if (!value) return '用量未知 / 未报告';
    const known = value.known_total_tokens == null ? '未知' : String(value.known_total_tokens);
    return '已报告 token：' + known + '；未报告请求：' + String(value.unreported_requests ?? '未知') +
      (value.complete ? '；报告完整' : '；用量不完整，不按 0 计费');
  }
  function coverageText(value) {
    if (!value) return '尚无已验证输入覆盖';
    return '已验证输入 ' + value.validated_chars + ' / ' + value.available_chars + ' code point (' +
      Math.round(value.ratio * 10000) / 100 + '%)；不表示理解正确或原作已经完结';
  }
  function createController(Vue, send = request, search = root.location.search) {
    const source = sourceFromSearch(search);
    const wa = Vue.reactive({
      form: {source_kind: source.source_kind, source_id: source.source_id, publication_state: 'unknown',
        agent_id: '', output_tokens: '4000', budget: {request_limit: '', token_limit: '', active_seconds_limit: ''}},
      order: [], chapters: [], confirmations: Object.fromEntries(approvals.map(key => [key, false])),
      agents: [], selectedAgent: null, preview: null, previewFor: '', startKey: '',
      busy: {}, readErrors: {}, writeError: '', notice: '', pending: null,
      runs: [], total: 0, offset: 0, selectedId: '', run: null,
      units: [], unitTotal: 0, unitOffset: 0, report: null, evidence: {},
      amend: {request_limit: '', token_limit: '', active_seconds_limit: ''},
      amendConfirmed: false, deleteConfirmed: false,
    });
    let live = true, generation = 0, timer = null;
    const owners = new Map();
    const draftIdentity = () => JSON.stringify({form: wa.form, order: wa.order});
    function cancel(scope) {
      const owner = owners.get(scope);
      if (owner) owner.abort.abort();
      owners.delete(scope);
      wa.busy[scope] = false;
    }
    function invalidate() {
      if (!live) return;
      cancel('preview');
      wa.preview = null; wa.previewFor = ''; wa.startKey = '';
      for (const key of approvals) wa.confirmations[key] = false;
    }
    const stopSource = Vue.watch(() => [wa.form.source_kind, wa.form.source_id], () => {
      wa.order = []; wa.chapters = [];
    }, {flush: 'sync'});
    const stopDraft = Vue.watch(draftIdentity, invalidate, {flush: 'sync'});
    const stopBudget = Vue.watch(() => JSON.stringify(wa.amend), () => { wa.amendConfirmed = false; }, {flush: 'sync'});
    function currentAgent() {
      const selected = wa.agents.find(row => String(row.agent_id) === String(wa.form.agent_id));
      if (selected) return selected;
      return wa.selectedAgent ? {...wa.selectedAgent, availability: 'unavailable', reason: '当前 Agent 已移除，请明确重选'} : null;
    }
    function chooseAgent(row) {
      if (!live || wa.pending || !row || row.kind !== 'agent' || row.availability !== 'ready') return false;
      wa.selectedAgent = copy(row); wa.form.agent_id = String(row.agent_id);
      return true;
    }
    async function read(scope, url, apply) {
      if (!live) return false;
      cancel(scope);
      const owner = {generation: ++generation, abort: new AbortController()};
      owners.set(scope, owner); wa.busy[scope] = true; wa.readErrors[scope] = '';
      try {
        const value = await send(url, {signal: owner.abort.signal});
        if (!live || owners.get(scope) !== owner) return false;
        apply(value);
        return true;
      } catch (error) {
        if (live && owners.get(scope) === owner && error.name !== 'AbortError') wa.readErrors[scope] = safeError(error);
        return false;
      } finally {
        if (live && owners.get(scope) === owner) { owners.delete(scope); wa.busy[scope] = false; }
      }
    }
    function payload() {
      const agent = currentAgent();
      if (!agent || agent.availability !== 'ready') throw new Error('请选择当前可用的 Agent');
      const selection = {source_kind: wa.form.source_kind,
        source_id: integer(wa.form.source_id, 1, Number.MAX_SAFE_INTEGER),
        publication_state: wa.form.publication_state};
      if (!['novel', 'series'].includes(selection.source_kind)) throw new Error('无效作品类型');
      if (!['unknown', 'ongoing', 'completed'].includes(selection.publication_state)) throw new Error('无效出版状态');
      if (wa.order.length) selection.ordered_novel_ids = wa.order.slice();
      return {selection, agent_id: integer(wa.form.agent_id, 1, Number.MAX_SAFE_INTEGER),
        output_tokens: integer(wa.form.output_tokens, 1, 1000000), budget: budget(wa.form.budget),
        confirmations: copy(wa.confirmations)};
    }
    async function reloadAgents() {
      return read('agents', '/api/dashboard/ai/agents', rows => {
        wa.agents = rows.map(row => ({kind: 'agent', agent_id: row.id, label: row.name,
          provider_name: 'Agent #' + row.id, availability: row.enabled ? 'ready' : 'disabled',
          reason: row.enabled ? '实际外发目标以本次预览为准' : 'Agent 已禁用'}));
        invalidate();
      });
    }
    async function previewWork() {
      if (!live || wa.pending) return false;
      invalidate();
      let submitted;
      try { submitted = payload(); }
      catch (error) { wa.writeError = error.message; return false; }
      const identity = draftIdentity();
      wa.writeError = '';
      const owner = {generation: ++generation, abort: new AbortController()};
      owners.set('preview', owner); wa.busy.preview = true; wa.readErrors.preview = '';
      try {
        const result = await send(BASE + '/preview', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(submitted), signal: owner.abort.signal});
        if (!live || owners.get('preview') !== owner || draftIdentity() !== identity) return false;
        for (const key of approvals) wa.confirmations[key] = false;
        wa.preview = result; wa.previewFor = identity; wa.chapters = result.manifest.chapters.slice();
        wa.startKey = '';
        return true;
      } catch (error) {
        if (live && owners.get('preview') === owner && error.name !== 'AbortError') wa.readErrors.preview = safeError(error);
        return false;
      } finally {
        if (live && owners.get('preview') === owner) { owners.delete('preview'); wa.busy.preview = false; }
      }
    }
    function moveChapter(index, direction) {
      if (wa.pending || !['series'].includes(wa.form.source_kind)) return false;
      const next = index + direction;
      if (next < 0 || next >= wa.chapters.length) return false;
      const chapters = wa.chapters.slice();
      chapters.splice(next, 0, chapters.splice(index, 1)[0]);
      wa.chapters = chapters; wa.order = chapters.map(row => row.novel_id);
      return true;
    }
    function canStart() {
      if (!live || wa.pending || wa.busy.preview || !wa.preview || !wa.preview.can_create || wa.previewFor !== draftIdentity()) return false;
      const agent = currentAgent();
      return !!agent && agent.availability === 'ready' && approvals.every(key => !wa.preview.confirmation_requirements[key] || wa.confirmations[key] === true);
    }
    function stopPolling() { if (timer !== null) root.clearTimeout(timer); timer = null; }
    function poll() {
      stopPolling();
      if (!live || root.document.hidden || !wa.run || wa.run.deleted || ['succeeded', 'failed', 'invalidated', 'cancelled'].includes(wa.run.status)) return;
      timer = root.setTimeout(async () => { timer = null; await refreshRun(); poll(); }, 2500);
    }
    function cancelRunReads(except) {
      for (const scope of Array.from(owners.keys())) if (scope !== except && !['agents', 'list', 'preview'].includes(scope)) cancel(scope);
      stopPolling();
    }
    async function loadRuns(offset = wa.offset) {
      return read('list', BASE + '?offset=' + offset + '&limit=20', data => {
        wa.runs = data.items; wa.total = data.total; wa.offset = data.offset;
      });
    }
    async function refreshRun() {
      if (!hash32(wa.selectedId)) return false;
      const id = wa.selectedId;
      const result = await read('run', BASE + '/' + id, data => {
        if (wa.selectedId === id && data.run_id === id && (!wa.run || data.version >= wa.run.version)) {
          wa.run = data;
          if (wa.pending && wa.pending.kind !== 'create' && wa.pending.runId === id &&
              data.version !== wa.pending.expectedVersion) wa.pending.canRetry = false;
          if (data.deleted) {
            // Revoke stale artifact/detail readers without aborting this accepted read.
            cancelRunReads('run');
            wa.report = null; wa.evidence = {}; wa.units = [];
          }
        }
      });
      poll();
      return result;
    }
    async function selectRun(id) {
      if (!live || wa.pending || !hash32(id)) return false;
      cancelRunReads(); wa.selectedId = id; wa.run = null; wa.report = null;
      wa.units = []; wa.unitTotal = 0; wa.unitOffset = 0; wa.evidence = {};
      wa.amendConfirmed = false; wa.deleteConfirmed = false;
      const loaded = await refreshRun();
      if (loaded && wa.selectedId === id && wa.run && !wa.run.deleted) wa.amend = copy(wa.run.budget);
      return loaded;
    }
    async function loadUnits(offset = wa.unitOffset) {
      const id = wa.selectedId;
      if (!hash32(id) || (wa.run && wa.run.deleted)) return false;
      return read('units', BASE + '/' + id + '/units?offset=' + offset + '&limit=20', data => {
        if (wa.selectedId === id) { wa.units = data.items; wa.unitTotal = data.total; wa.unitOffset = data.offset; }
      });
    }
    async function loadReport() {
      const id = wa.selectedId;
      if (!wa.run || wa.run.deleted || !wa.run.has_report || !hash32(id)) return false;
      wa.report = null; wa.evidence = {};
      return read('report', BASE + '/' + id + '/report', data => {
        if (wa.selectedId === id) { wa.report = data; wa.evidence = {}; }
      });
    }
    async function inspectEvidence(id) {
      const run = wa.selectedId;
      if (!hash32(run) || (wa.run && wa.run.deleted) || !hash64(id) || !wa.report || !wa.report.artifact.evidence[id]) return false;
      delete wa.evidence[id];
      return read('evidence:' + id, BASE + '/' + run + '/evidence/' + id, data => {
        if (wa.selectedId === run) wa.evidence[id] = {parts: partsFromEvidence(data), jump: data.jump};
      });
    }
    function controlAllowed(action) {
      if (!live || wa.pending || !wa.run || wa.run.deleted) return false;
      if (action === 'resume') return wa.run.can_resume === true;
      if (action === 'pause') return ['queued', 'running'].includes(wa.run.status) ||
        (wa.run.reason === 'sync_yield' && ['pausing', 'paused'].includes(wa.run.status));
      if (action === 'cancel') return ['queued', 'running', 'pausing', 'paused'].includes(wa.run.status);
      if (action === 'delete') return wa.deleteConfirmed;
      return action === 'budget' && wa.amendConfirmed;
    }
    async function refreshAfterWrite() {
      const results = await Promise.all([refreshRun(), loadRuns()]);
      if (live && results.some(value => !value)) wa.notice = '写入已确认，但刷新失败；请只重试读取，不要重复提交。';
    }
    function acceptWrite(operation, data) {
      wa.pending = null; wa.writeError = ''; wa.notice = '写入已确认。';
      // The receipt acknowledges the command, not necessarily the newest run state.
      const current = wa.run;
      const accepted = current && current.run_id === data.run_id &&
        (current.deleted || current.version > data.version) ? current : data;
      cancelRunReads(); wa.selectedId = data.run_id; wa.run = accepted;
      wa.report = null; wa.evidence = {}; wa.units = [];
      if (operation.kind === 'create') invalidate();
      if (accepted.budget && !accepted.deleted) wa.amend = copy(accepted.budget);
      wa.amendConfirmed = false; wa.deleteConfirmed = false;
      poll();
    }
    async function sendPending() {
      const operation = wa.pending;
      if (!live || !operation || operation.sending) return false;
      cancel('reconcile');
      operation.sending = true; operation.phase = 'sending'; wa.writeError = '';
      try {
        const result = await send(operation.url, {method: operation.method, headers: {'Content-Type': 'application/json'},
          body: operation.body});
        if (!live || wa.pending !== operation) return false;
        acceptWrite(operation, result);
        await refreshAfterWrite();
        return true;
      } catch (error) {
        if (!live || wa.pending !== operation) return false;
        if (error.status == null || error.status >= 500) {
          operation.phase = 'unknown'; wa.writeError = '写入结果未知；保留原请求，请先核对或重试同一创建。';
        } else {
          wa.pending = null; wa.writeError = safeError(error);
          if (operation.kind === 'create') invalidate();
          else await refreshRun();
        }
        return false;
      } finally {
        if (live && wa.pending === operation) operation.sending = false;
      }
    }
    async function createWork() {
      if (!canStart()) { if (live) wa.writeError = '预览已失效或确认不完整，请重新预览并确认。'; return false; }
      let submitted;
      try { submitted = payload(); } catch (error) { wa.writeError = error.message; return false; }
      if (wa.previewFor !== draftIdentity()) return false;
      wa.startKey = wa.startKey || newKey();
      submitted.preview_fingerprint = wa.preview.preview_fingerprint; submitted.idempotency_key = wa.startKey;
      wa.pending = {kind: 'create', method: 'POST', url: BASE, body: JSON.stringify(submitted),
        phase: 'ready', sending: false, canRetry: true};
      return sendPending();
    }
    async function control(action) {
      if (!controlAllowed(action)) return false;
      const submitted = {expected_version: wa.run.version};
      if (action === 'budget') {
        try { submitted.budget = budget(wa.amend); }
        catch (error) { wa.writeError = error.message; return false; }
        submitted.budget_confirmed = true;
      }
      wa.pending = {kind: action, method: action === 'delete' ? 'DELETE' : 'POST',
        url: BASE + '/' + wa.run.run_id + (action === 'delete' ? '' : '/' + action),
        runId: wa.run.run_id, expectedVersion: wa.run.version, requestedBudget: submitted.budget || null,
        body: JSON.stringify(submitted), phase: 'ready', sending: false, canRetry: false};
      return sendPending();
    }
    async function reconcileWrite() {
      const operation = wa.pending;
      if (!live || !operation || operation.phase !== 'unknown' || operation.sending ||
          operation.kind === 'create' || wa.selectedId !== operation.runId) return false;
      operation.canRetry = false;
      return read('reconcile', BASE + '/' + operation.runId, data => {
        if (wa.pending !== operation || operation.phase !== 'unknown' || operation.sending ||
            wa.selectedId !== operation.runId || data.run_id !== operation.runId) return;
        const newest = wa.run && wa.run.run_id === operation.runId ? wa.run.version : operation.expectedVersion;
        if (data.version < Math.max(newest, operation.expectedVersion)) {
          wa.notice = '读取返回较旧版本；原写入结果仍未知，请重新核对。';
          return;
        }
        wa.run = data;
        const states = {pause: ['paused', 'pausing'], cancel: ['cancelled', 'cancelling'], resume: ['queued', 'running', 'succeeded']};
        const satisfied = operation.kind === 'delete' ? data.deleted === true : operation.kind === 'budget'
          ? !!data.budget && budgetKeys.every(key => data.budget[key] === operation.requestedBudget[key])
          : (states[operation.kind] || []).includes(data.status) &&
            (operation.kind !== 'pause' || data.reason === 'user_pause') &&
            (operation.kind !== 'cancel' || data.reason === 'user_cancel');
        if (data.version === operation.expectedVersion) {
          operation.canRetry = true; wa.notice = '读取确认版本未变化，可明确重试同一 CAS 请求。';
          return;
        }
        // A GET observes current state, never receipt/ownership of the lost command.
        cancelRunReads(); wa.pending = null;
        wa.report = null; wa.evidence = {}; wa.units = [];
        if (data.budget && !data.deleted) wa.amend = copy(data.budget);
        wa.amendConfirmed = false; wa.deleteConfirmed = false;
        wa.notice = satisfied ? '当前状态已核对；原写入结果仍未知，未重复提交。' : '';
        wa.writeError = satisfied ? '' : '状态已经变化；未确认原写入，也未重复提交，请按当前状态重新选择。';
        poll();
      });
    }
    async function retryUnknown() {
      const operation = wa.pending;
      if (!operation || operation.phase !== 'unknown' || !operation.canRetry) return false;
      if (operation.kind !== 'create' && (wa.busy.reconcile || wa.selectedId !== operation.runId ||
          !wa.run || wa.run.run_id !== operation.runId || wa.run.version !== operation.expectedVersion)) return false;
      return sendPending();
    }
    function evidenceHref(id) {
      const evidence = wa.report && wa.report.artifact.evidence[id];
      return evidence ? '/dashboard/novels/' + encodeURIComponent(evidence.novel_id) + '?analysis_id=' +
        encodeURIComponent(wa.selectedId) + '&evidence_id=' + encodeURIComponent(id) + '#work-analysis-evidence' : '#';
    }
    function dispose() {
      if (!live) return;
      live = false; stopPolling(); stopSource(); stopDraft(); stopBudget();
      for (const owner of owners.values()) owner.abort.abort();
      owners.clear();
    }
    function visibility() { if (root.document.hidden) stopPolling(); else if (live) refreshRun(); }
    function beforeUnload(event) { if (wa.pending) { event.preventDefault(); event.returnValue = ''; } }
    function suspend() { stopPolling(); for (const scope of Array.from(owners.keys())) cancel(scope); }
    function restore() { if (live) { reloadAgents(); loadRuns(); refreshRun(); } }
    async function mount() { await Promise.all([reloadAgents(), loadRuns()]); if (hash32(source.analysis_id)) await selectRun(source.analysis_id); }
    return {wa, currentAgent: Vue.computed(currentAgent), canStart: Vue.computed(canStart), chooseAgent,
      previewWork, createWork, moveChapter, reloadAgents, loadRuns, selectRun, refreshRun, loadUnits,
      loadReport, inspectEvidence, controlAllowed, control, retryUnknown, reconcileWrite, evidenceHref,
      usageText, coverageText, invalidate, mount, dispose, visibility, beforeUnload, suspend, restore};
  }
  function useReaderEvidence(Vue, novel, novelId) {
    const params = new URLSearchParams(root.location.search), run = params.get('analysis_id'), id = params.get('evidence_id');
    const state = Vue.reactive({active: !!(run || id), loading: false, error: '', parts: null, jump: null});
    let live = true, generation = 0, abort = null;
    const reload = async () => {
      if (!state.active || !novel.value || !live) return;
      const mine = ++generation;
      if (abort) abort.abort();
      abort = new AbortController(); state.parts = null; state.jump = null; state.error = '';
      if (!hash32(run) || !hash64(id)) { state.error = '证据链接无效。'; return; }
      state.loading = true;
      try {
        const value = await request(BASE + '/' + run + '/evidence/' + id, {signal: abort.signal});
        if (!live || mine !== generation) return;
        if (value.jump.novel_id !== novelId || novel.value.text_hash !== value.jump.text_hash) {
          state.error = '当前阅读正文与证据版本不同，不能把旧偏移应用到新正文。'; return;
        }
        state.parts = partsFromEvidence(value); state.jump = value.jump;
        await Vue.nextTick();
        if (live && mine === generation) {
          const panel = root.document.getElementById('work-analysis-evidence');
          if (panel) { panel.focus(); panel.scrollIntoView({block: 'start'}); }
        }
      } catch (error) {
        if (live && mine === generation && error.name !== 'AbortError') state.error = safeError(error);
      } finally { if (live && mine === generation) state.loading = false; }
    };
    const stop = Vue.watch(() => [novel.value && novel.value.novel_id, novel.value && novel.value.text_hash], reload, {immediate: true});
    const hide = event => {
      if (event.persisted) { ++generation; if (abort) abort.abort(); state.parts = null; state.loading = false; }
      else dispose();
    };
    const show = event => { if (event.persisted && live) reload(); };
    const dispose = () => {
      live = false; ++generation; stop(); if (abort) abort.abort();
      root.removeEventListener('pagehide', hide); root.removeEventListener('pageshow', show);
    };
    Vue.onUnmounted(dispose);
    root.addEventListener('pagehide', hide); root.addEventListener('pageshow', show);
    return state;
  }
  root.WorkAnalysis = {createController, useReaderEvidence, partsFromEvidence, request};
})(window);

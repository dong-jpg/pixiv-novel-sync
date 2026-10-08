const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../src/pixiv_novel_sync/templates');
const names = ['chapters', 'notes', 'project', 'output_panel', 'source_search'];
for (const file of [...names.map(n => `dashboard_ai_${n}.html`), 'dashboard_wizard.html']) {
  for (const match of fs.readFileSync(path.join(root, file), 'utf8').matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)) {
    new vm.Script(match[1].replace(/\{\[[\s\S]*?\]\}/g, '1'), {filename: file});
  }
}
const chapterTemplate = fs.readFileSync(path.join(root, 'dashboard_ai_chapters.html'), 'utf8');
const modalTemplate = fs.readFileSync(path.join(root, 'dashboard_ai_pipeline_modal.html'), 'utf8');
const script = chapterTemplate.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\{\[[\s\S]*?\]\}/g, '1');
function mount(source = script) {
  let state;
  const requests = [];
  const alerts = [];
  const alert = (...args) => alerts.push(args);
  const chapter = {id: 1, project_id: 1, chapter_number: 1, chapter_revision: 3, content: '正文', metadata: {}};
  const window = {
    alert,
    history: {replaceState() {}}, location: {search: ''},
    initVueApp(app) { state = app.setup(); },
    aiApi: {async request(url, options) {
      requests.push({url, options});
      if (url.endsWith('/dashboard')) return {chapter};
      if (url.endsWith('/foreshadows')) return [];
      return structuredClone(chapter);
    }},
    async streamSSE() { throw new Error('unexpected stream'); },
  };
  vm.runInNewContext(source, {window, alert, location: window.location, initVueApp: window.initVueApp, console, URLSearchParams, AbortController,
    setTimeout: () => 0, clearTimeout() {}, confirm: () => true,
    Vue: {nextTick: async () => {}, ref: value => ({value}), reactive: value => value,
      computed: fn => ({get value() {return fn();}}), onMounted() {},
      onBeforeUnmount() {}, watch() {}, nextTick: async () => {}},
  });
  if (source === script) {
  state.currentProject.value = {id: 1, settings: {}};
  state.currentChapter.value = structuredClone(chapter);
  state.chapterEditForm.content = chapter.content;
  state.pipelineSelectedSteps.value = ['detect'];
  state.showPipelineModal.value = true;
  }
  return {state, window, requests, alerts};
}
(async () => {
  for (const name of ['dashboard_ai_notes.html', 'dashboard_ai_project.html', 'dashboard_wizard.html']) {
    const source = fs.readFileSync(path.join(root, name), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\{\[[\s\S]*?\]\}/g, '1');
    assert.ok(mount(source).state, name + ' setup exports');
  }
  {
    const {state, window, requests} = mount();
    let runs = 0;
    window.streamSSE = async (_url, options, {onEvent}) => {
      assert.ok(options.signal);
      runs++;
      onEvent('step_start', {step: 'detect'});
      onEvent(runs === 1 ? 'step_failed' : 'step_done', {step: 'detect'});
      onEvent('done', {});
    };
    await state.startChapterPipeline();
    assert.equal(runs, 2);
    assert.equal(state.pipelineSteps.value[0].status, 'done');
    assert.equal(state.showPipelineModal.value, true);
    const save = requests.find(r => r.url.endsWith('/chapters/1') && r.options?.method === 'PUT');
    assert.equal(JSON.parse(save.options.body).expected_revision, 3);
  }
  {
    const {state, window} = mount();
    let runs = 0;
    window.aiApi.request = async () => { throw new Error('版本冲突'); };
    window.streamSSE = async () => { runs++; };
    await state.startChapterPipeline();
    assert.equal(runs, 0);
    assert.equal(state.chapterEditForm.content, '正文');
  }
  {
    const {state, window} = mount();
    state.agents.value = [{id: 7, enabled: true, task_type: 'extract_summary'}];
    window.streamSSE = async (_url, options, {onEvent}) => {
      assert.equal(JSON.parse(options.body).agent_id, 7);
      onEvent('error', {message: '注入失败'});
    };
    await state.runSingleStep('extract-summary');
    assert.equal(state.message.value, '注入失败');
    assert.equal(state.pipelineRunning.value, false);
  }
  {
    const {state, window} = mount();
    let runs = 0;
    window.streamSSE = async (_url, options, {onEvent}) => {
      runs++;
      state.cancelPipeline();
      assert.equal(options.signal.aborted, true);
      onEvent('step_failed', {step: 'detect'});
      throw new Error('aborted');
    };
    await state.startChapterPipeline();
    assert.equal(runs, 1);
    assert.equal(state.pipelineRunning.value, false);
    assert.equal(state.showPipelineModal.value, true);
  }
  {
    const {state} = mount();
    state.streamAutosaved.value = true;
    state.streamOutput.value = '重复内容';
    state.appendOutput();
    assert.equal(state.chapterEditForm.content, '正文');
    state.pipelineRunning.value = true;
    assert.equal(await state.saveCurrentChapter(), false);
  }
  const reviewFailures = [];
  async function reviewCase(name, run) {
    try { await run(); } catch (error) { reviewFailures.push(name + ': ' + error.message); }
  }
  await reviewCase('保存期间的新编辑不被覆盖', async () => {
    const {state, window} = mount();
    let releasePut;
    const put = new Promise(resolve => { releasePut = resolve; });
    const original = window.aiApi.request;
    window.aiApi.request = async (url, options) => {
      if (options?.method === 'PUT' && url.endsWith('/chapters/1')) {
        await put;
        return {id: 1, chapter_revision: 4, content: '正文'};
      }
      return original(url, options);
    };
    const saving = state.saveCurrentChapter();
    await Promise.resolve();
    state.chapterEditForm.content = '保存期间继续输入';
    releasePut();
    await saving;
    assert.equal(state.chapterEditForm.content, '保存期间继续输入');
    assert.equal(state.currentChapter.value.chapter_revision, 4);
  });
  await reviewCase('单步重试保留其他步骤', async () => {
    const {state, window} = mount();
    state.pipelineSteps.value = [{name: 'detect', status: 'done', score: 88}, {name: 'index', status: 'failed'}];
    window.streamSSE = async (_url, options, {onEvent}) => {
      onEvent('metadata', {steps: [{name: 'index', label: '索引'}]});
      onEvent('step_done', {step: 'index'});
      onEvent('done', {});
    };
    await state.retryPipelineStep('index');
    assert.equal(state.pipelineSteps.value.find(s => s.name === 'detect')?.score, 88);
    assert.equal(state.pipelineSteps.value.find(s => s.name === 'index')?.status, 'done');
  });
  await reviewCase('批量EOF不伪报完成', async () => {
    const {state, window} = mount();
    state.batchSelectedChapterIds.value = [1];
    window.streamSSE = async (_url, _options, {onEvent}) => {
      onEvent('batch_start', {total: 1});
      onEvent('chapter_start', {chapter_number: 1, title: '第一章'});
    };
    await state.startBatchChapterPipeline();
    assert.equal(state.messageType.value, 'error');
    assert.match(state.message.value, /中断/);
    assert.notEqual(state.batchPipelineProgress.currentLabel, '已完成');
    assert.equal(state.batchPipelineProgress.succeeded, 0);
    assert.equal(state.batchPipelineRunning.value, false);
  });
  await reviewCase('产出面板刷新期间输入阻止后续生成', async () => {
    const {state, window} = mount();
    let releaseDashboard;
    let dashboardStarted;
    const started = new Promise(resolve => { dashboardStarted = resolve; });
    const dashboard = new Promise(resolve => { releaseDashboard = resolve; });
    const original = window.aiApi.request;
    window.aiApi.request = async (url, options) => {
      if (url.endsWith('/dashboard')) { dashboardStarted(); await dashboard; }
      return original(url, options);
    };
    const saving = state.saveCurrentChapter();
    await started;
    state.chapterEditForm.content = '刷新面板期间继续输入';
    releaseDashboard();
    assert.equal(await saving, false);
    assert.equal(state.chapterEditForm.content, '刷新面板期间继续输入');
    assert.match(state.message.value, /未保存/);
  });
  for (const ending of ['eof', 'error']) {
    await reviewCase('批量异常保留已完成计数 ' + ending, async () => {
      const {state, window} = mount();
      state.batchSelectedChapterIds.value = [1, 2];
      window.streamSSE = async (_url, _options, {onEvent}) => {
        onEvent('batch_start', {total: 2});
        onEvent('chapter_done', {chapter_id: 1, status: 'succeeded'});
        onEvent('chapter_start', {chapter_number: 2, title: '第二章'});
        if (ending === 'error') onEvent('error', {message: '执行失败'});
      };
      await state.startBatchChapterPipeline();
      assert.equal(state.batchPipelineProgress.succeeded, 1);
      assert.equal(state.batchPipelineProgress.total, 2);
      assert.equal(state.messageType.value, 'error');
      assert.notEqual(state.batchPipelineProgress.currentLabel, '已完成');
    });
  }
  await reviewCase('Pipeline准备阶段输入不被完成刷新覆盖', async () => {
    const {state, window} = mount();
    let releaseProfile;
    let profileStarted;
    const started = new Promise(resolve => { profileStarted = resolve; });
    const profile = new Promise(resolve => { releaseProfile = resolve; });
    const original = window.aiApi.request;
    window.aiApi.request = async (url, options) => {
      if (options?.method === 'PUT' && url.includes('/projects/')) { profileStarted(); await profile; }
      return original(url, options);
    };
    let streams = 0;
    window.streamSSE = async (_url, _options, {onEvent}) => {
      streams++;
      onEvent('step_done', {step: 'detect'});
      onEvent('done', {});
    };
    const starting = state.startChapterPipeline();
    await started;
    state.chapterEditForm.content = '保存档案期间继续输入';
    releaseProfile();
    await starting;
    assert.equal(state.chapterEditForm.content, '保存档案期间继续输入');
    assert.equal(streams, 0);
    assert.match(state.message.value, /未保存/);
    assert.equal(state.pipelineRunning.value, false);
  });
  // These execute real setup methods/event handlers with transport boundaries
  // stubbed. Binding checks below are NOT browser/viewport rendering acceptance.
  let coverageCases = 0;
  async function coverageCase(name, run) {
    coverageCases++;
    await reviewCase(name, run);
  }
  for (const types of [
    ['wizard'], ['adult_polish', 'adult_safety_review', 'adult_fact_guard'],
    ['wizard', 'adult_polish', 'adult_safety_review', 'adult_fact_guard'],
  ]) {
    for (const entry of ['single', 'batch']) {
      await coverageCase(`Pipeline默认排除 ${types.join('/')} (${entry})`, async () => {
        const {state, window} = mount();
        state.agents.value = types.map((task_type, i) => ({id: i + 70, task_type, enabled: true}));
        const steps = state.pipelineAllSteps.filter(s => !['detect', 'index'].includes(s.id)).map(s => s.id);
        state.pipelineSelectedSteps.value = [...steps];
        state.batchSelectedChapterIds.value = [1];
        const original = window.aiApi.request;
        window.aiApi.request = async (url, options) => url.endsWith('/chapters') ? [] : original(url, options);
        const sent = [];
        window.streamSSE = async (_url, options, {onEvent}) => {
          sent.push(JSON.parse(options.body));
          onEvent('done', {total: 1, succeeded: 1});
        };
        if (entry === 'single') await state.startChapterPipeline();
        else await state.startBatchChapterPipeline();
        assert.equal(sent.length, 1, 'must exercise the request path');
        assert.deepEqual(sent[0].agent_ids, Object.fromEntries(steps.map(s => [s, 0])));
        assert.equal(state.chapterContinueAgent.value, 0);
        for (const step of steps) {
          assert.equal(state.pipelineAgentIds[step], 0, step + ' default');
          assert.equal(state.pipelineAgentsFor(step).length, 0, step + ' options');
        }
      });
    }
  }
  await coverageCase('合法Agent仍按专用类型优先、general回退、disabled不自动选', async () => {
    const {state, window} = mount();
    state.agents.value = [
      {id: 70, task_type: 'wizard', enabled: true},
      {id: 71, task_type: 'adult_polish', enabled: true},
      {id: 72, task_type: 'continue', enabled: false},
      {id: 80, task_type: 'general', enabled: true},
      {id: 81, task_type: 'continue', enabled: true},
      {id: 82, task_type: 'extract_summary', enabled: true},
    ];
    state.pipelineSelectedSteps.value = ['continue', 'summary', 'audit'];
    state.batchSelectedChapterIds.value = [1];
    const original = window.aiApi.request;
    window.aiApi.request = async (url, options) => url.endsWith('/chapters') ? [] : original(url, options);
    const sent = [];
    window.streamSSE = async (_url, options, {onEvent}) => {
      sent.push(JSON.parse(options.body));
      onEvent('done', {total: 1, succeeded: 1});
    };
    await state.startBatchChapterPipeline();
    assert.equal(sent.length, 1);
    assert.deepEqual(sent[0].agent_ids, {continue: 81, summary: 82, audit: 80});
    assert.equal(state.chapterContinueAgent.value, 81);
    assert.equal(state.pipelineAgentIds.continue, 81);
    assert.equal(state.pipelineAgentIds.summary, 82);
    assert.equal(state.pipelineAgentIds.audit, 80);
  });
  for (const entry of ['continue', 'pipeline-flat', 'pipeline-nested']) {
    await coverageCase(`路由progress的action/reason呈现 (${entry})`, async () => {
      const {state, window} = mount();
      state.chapterContinueAgent.value = 7;
      const observed = [];
      const progressEvents = [
        {action: 'switch_candidate', reason: 'rate_limited', message: '正在尝试备用模型'},
        {action: 'retry', reason: 'transport_error'},
        {reason: '等待限流解除'},
        {message: '继续生成'},
        {},
      ];
      window.streamSSE = async (_url, _options, {onEvent}) => {
        for (const progress of progressEvents) {
          onEvent('progress', entry === 'pipeline-nested' ? {step: 'detect', progress} : progress);
          observed.push({text: state.streamProgress.value, running: state.streaming.value || state.pipelineRunning.value});
        }
        onEvent('delta', {text: '新正文'});
        onEvent('done', {});
      };
      if (entry === 'continue') await state.startChapterContinue();
      else await state.startChapterPipeline();
      assert.deepEqual(observed, [
        'switch_candidate · rate_limited · 正在尝试备用模型',
        'retry · transport_error', '等待限流解除', '继续生成', '',
      ].map(text => ({text, running: true})));
      assert.equal(state.messageType.value, 'success');
      // Guard the visible bindings too: correct state with no template consumer
      // would otherwise pass. This does not measure actual browser rendering.
      assert.match(chapterTemplate, /<span\b[^>]*v-if="streamProgress"[^>]*>\s*\{\{ streamProgress \}\}\s*<\/span>/);
      assert.match(modalTemplate, /<p\b[^>]*v-if="streamProgress"[^>]*role="status"[^>]*>\s*\{\{ streamProgress \}\}\s*<\/p>/);
    });
  }
  for (const [name, report, expected] of [
    ['有问题', {score: 62.6, issues: [{severity: 'warning', message: '套话重复'}, {severity: 'info', message: '句式单一'}]},
      'AI 痕迹评分：63/100（越高越像人写）\n问题：[warning] 套话重复；[info] 句式单一'],
    ['无问题', {score: 100, issues: []}, 'AI 痕迹评分：100/100（越高越像人写）'],
    ['省略issues', {score: 81}, 'AI 痕迹评分：81/100（越高越像人写）'],
    ['请求失败', new Error('检测服务暂不可用'), '检测服务暂不可用'],
  ]) {
    await coverageCase(`AI检测就地呈现且不用alert (${name})`, async () => {
      const {state, window, alerts} = mount();
      const requests = [];
      state.streamOutput.value = '待检测正文';
      window.aiApi.request = async (url, options) => {
        requests.push({url, options});
        if (report instanceof Error) throw report;
        return report;
      };
      await state.detectAITells();
      assert.equal(requests.length, 1);
      assert.equal(requests[0].url, '/api/dashboard/ai/detect-ai-tells');
      assert.equal(requests[0].options.method, 'POST');
      assert.deepEqual(JSON.parse(requests[0].options.body), {text: '待检测正文'});
      assert.equal(state.message.value, expected);
      assert.equal(state.toastMessage.value, expected);
      assert.equal(state.messageType.value, report instanceof Error ? 'error' : 'success');
      assert.equal(state.toastType.value, state.messageType.value);
      assert.equal(state.streamOutput.value, '待检测正文');
      assert.deepEqual(alerts, []);
      assert.match(chapterTemplate, /<div\b[^>]*v-if="message"[^>]*>\s*\{\{ message \}\}\s*<\/div>/);
      assert.match(chapterTemplate, /<span>\{\{ toastMessage \}\}<\/span>/);
      assert.match(chapterTemplate, /<output-panel\b[^>]*@detect="detectAITells"/);
    });
  }
  await coverageCase('AI检测空输出不发送请求或弹窗', async () => {
    const {state, requests, alerts} = mount();
    await state.detectAITells();
    assert.deepEqual(requests, []);
    assert.deepEqual(alerts, []);
    assert.equal(state.message.value, '');
    assert.equal(state.toastMessage.value, '');
  });
  assert.deepEqual(reviewFailures, []);
  console.log(`6 template syntax checks; 12 existing + ${coverageCases} coverage-completion writing UI regressions passed (Node VM, not browser acceptance)`);
})().catch(error => { console.error(error); process.exitCode = 1; });

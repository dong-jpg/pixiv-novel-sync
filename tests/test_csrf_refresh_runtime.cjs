const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../src/pixiv_novel_sync/templates/base.html'), 'utf8');
const source = html.slice(html.indexOf('    let _csrfToken = null;'), html.indexOf('    // 从任意响应里取出'));
function mount(fetch) { const window = {}; vm.runInNewContext(source, {window, fetch}); return window; }
const reply = (csrf_token, ok = true, status = 200) => ({ok, status, json: async () => ({csrf_token})});
function deferred() { let resolve; const promise = new Promise(done => {resolve = done;}); return {resolve, promise}; }

test('another-tab login can refresh token while replaying the original operation', async () => {
  let token = 'old', tokenGets = 0; const requests = [];
  const window = mount(async (url, options) => {
    if (url === '/api/csrf-token') {tokenGets++; return reply(token);}
    requests.push(options);
    return {ok: options.headers['X-CSRF-Token'] === token, status: options.headers['X-CSRF-Token'] === token ? 200 : 403};
  });
  const operation = {method: 'POST', body: '{"mutation_id":"stable"}', keepalive: true, headers: {'Content-Type': 'application/json'}};
  assert.equal((await window.csrfFetch('/progress', operation)).status, 200);
  token = 'new';
  assert.equal((await window.csrfFetch('/progress', operation)).status, 403);
  assert.equal(requests.length, 2, '403 must not automatically repeat arbitrary writes');
  await window.ensureCsrfToken({refresh: true});
  assert.equal((await window.csrfFetch('/progress', operation)).status, 200);
  assert.equal(tokenGets, 2);
  assert.ok(requests.every(request => request.body === operation.body && request.keepalive));
});

test('late pre-login token response cannot replace a refreshed token', async () => {
  const old = deferred(); let count = 0;
  const window = mount(() => ++count === 1 ? old.promise : Promise.resolve(reply('new')));
  const original = window.ensureCsrfToken();
  const concurrent = window.ensureCsrfToken();
  assert.equal(count, 1);
  await window.ensureCsrfToken({refresh: true});
  old.resolve(reply('old'));
  assert.equal(await original, 'new');
  assert.equal(await concurrent, 'new');
  assert.equal(await window.ensureCsrfToken(), 'new');
  assert.equal(count, 2);
});

test('failed token lookup never sends an unauthenticated mutation and is retryable', async () => {
  let count = 0, writes = 0;
  const window = mount(async url => {
    if (url !== '/api/csrf-token') {writes++; return {ok: true};}
    return ++count === 1 ? reply(null, false, 401) : reply('valid');
  });
  await assert.rejects(window.csrfFetch('/progress', {method: 'DELETE'}));
  assert.equal(writes, 0);
  await window.csrfFetch('/progress', {method: 'DELETE'});
  assert.equal(writes, 1);
});

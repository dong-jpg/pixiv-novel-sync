from __future__ import annotations

from pathlib import Path
import json
import re
import shutil
import subprocess

import pytest

from pixiv_novel_sync.webapp import create_app


def test_remote_page_requires_dashboard_login(remote_web_fixture):
    f = remote_web_fixture
    response = f.client.get("/dashboard/remote")
    assert response.status_code in {302, 303}
    assert "/api/auth/login" in response.headers["Location"]


def test_page_is_explicit_and_preserves_existing_downloads(remote_web_fixture):
    f = remote_web_fixture
    f.login()
    response = f.client.get("/dashboard/remote")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "远程执行" in page and "单本 EPUB" in page and "提交诊断" in page
    assert "远程实例" in page and "任务记录" in page
    assert "/api/dashboard/novels/export-epub" in {rule.rule for rule in f.app.url_map.iter_rules()}
    assert f.provider_calls == []


def test_disabled_page_has_no_submission_controls_or_state_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("PIXIV_REMOTE_ENABLED", raising=False)
    state = tmp_path / "unused-control"
    monkeypatch.setenv("PIXIV_REMOTE_STATE_DIR", str(state))
    monkeypatch.setenv("DASHBOARD_TOKEN", "fixture-dashboard")
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "fixture-only-secret")
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    app = create_app(config_path=str(tmp_path / "missing.yaml"), env_path=str(env), start_scheduler=False)
    app.config.update(TESTING=True, RAW_HTTP=True)
    client = app.test_client()
    login = client.post("/api/auth/login", data={"token": "fixture-dashboard"})
    assert login.status_code == 302
    page = client.get("/dashboard/remote").get_data(as_text=True)
    assert "远程执行未启用" in page
    assert 'id="remote-diagnostic-submit"' not in page
    assert 'id="remote-epub-form"' not in page
    assert not state.exists()


def test_page_never_embeds_runtime_credentials_or_filesystem_paths(remote_web_fixture):
    f = remote_web_fixture
    f.login()
    page = f.client.get("/dashboard/remote").get_data(as_text=True)
    assert str(f.store.state_dir) not in page
    assert "INSTABOX_API_KEY" not in page
    assert "PIXIV_REMOTE_WORKER_KEY" not in page
    assert "session_secret" not in page
    # A single shared navigation entry feeds desktop and mobile More.
    assert "path: '/dashboard/remote', label: '远程执行'" in page


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for browser-state contract tests")
def test_uncertain_submission_survives_refresh_and_reuses_request_identity():
    template = Path(__file__).resolve().parents[2] / "src/pixiv_novel_sync/templates/dashboard_remote.html"
    source = re.search(r'<script id="remote-page-script"[^>]*>(.*?)</script>', template.read_text(encoding="utf-8"), re.S).group(1)
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = JSON.parse(require('node:fs').readFileSync(0, 'utf8')).source;
const saved = new Map();
const calls = [];
let sequence = 0;
const resultID = '9cf64477-81a7-4c53-b445-a97316e1a5f9';
async function request(url, options) {
  if (options.method === 'POST' && url.endsWith('/runs')) {
    calls.push(options);
    if (calls.length === 1) throw new TypeError('fixture lost response');
    return {ok: true, status: 202, json: async () => ({run_id: resultID, state: 'queued'})};
  }
  return {ok: true, status: 200, json: async () => url.endsWith('/runs') ? {runs: []} : {mode: 'idle'}};
}
function boot() {
  let page;
  const ctx = {
    Vue: {ref: value => ({value}), computed: fn => ({get value() {return fn();}}), onMounted() {}, onBeforeUnmount() {}},
    window: {
      csrfFetch: request,
      sessionStorage: {getItem: key => saved.get(key) || null, setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key)},
      initVueApp: app => {page = app.setup();},
    },
    crypto: {randomUUID: () => '0d0b46fe-e0c9-41df-8329-' + String(++sequence).padStart(12, '0')},
    document: {hidden: false, addEventListener() {}, removeEventListener() {}},
    setInterval() {}, clearInterval() {},
  };
  vm.runInNewContext(source, ctx);
  return page;
}
(async () => {
  let page = boot();
  await page.submitDiagnostic();
  assert.equal(calls.length, 1);
  assert.ok(page.pendingRequest.value);
  const key = calls[0].headers['Idempotency-Key'];
  page.novelId.value = '42';
  await page.submitEpub();
  assert.equal(calls.length, 1, 'a new task cannot overwrite an unconfirmed request');
  page = boot();
  assert.equal(page.pendingRequest.value.key, key);
  await page.retryPending();
  assert.equal(calls.length, 2);
  assert.equal(calls[1].headers['Idempotency-Key'], key);
  assert.equal(calls[1].body, calls[0].body);
  assert.equal(saved.size, 0);
  assert.equal(page.pendingRequest.value, null);
  page.novelId.value = '42';
  await page.submitEpub();
  assert.equal(calls.length, 3);
  assert.notEqual(calls[2].headers['Idempotency-Key'], key);
  assert.deepEqual(JSON.parse(calls[2].body), {kind: 'epub.single.v1', novel_id: 42});
  page.machine.value = {mode: 'ready', provider_state: 'running', gate_ready_until: 0};
  assert.equal(page.machineMode.value, '等待就绪确认');
  assert.equal(page.providerLabel.value, '运行中');
  assert.equal(page.runLabel('active'), '执行中');
})().catch(err => {console.error(err); process.exitCode = 1;});
"""
    completed = subprocess.run([shutil.which("node"), "-e", harness], input=json.dumps({"source": source}),
                               text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr

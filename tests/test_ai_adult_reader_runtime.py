from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_regenerate_runtime_preserves_old_candidate_on_403_and_rotates_after_metadata():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for reader runtime checks")
    html = Path("src/pixiv_novel_sync/templates/dashboard_ai_reader.html").read_text(encoding="utf-8")
    functions = []
    for start, end in (
        ("function resetAdultCandidate()", "function captureAdultSelection()"),
        ("function consumeAdultEvent(", "async function streamAdultEvents("),
        ("async function startAdultGeneration(", "async function cancelAdultGeneration("),
    ):
        functions.append(html[html.index(start):html.index(end)])
    script = r"""
const assert = require('node:assert/strict');
const ref = value => ({value});
const candidate = ref('old candidate'), validation = ref({applicable:true}), warnings = ref([]), blockingIssues = ref([]);
const warningAcknowledged = ref(true), warningAckHash = ref('ack'), accessToken = ref('old-token'), jobId = ref('old-job');
const adultStatus = ref('succeeded'), adultError = ref(''), adultProgress = ref(''), canGenerateAdult = ref(true);
const buildAdultPayload = async parent_job_id => ({parent_job_id});
let streamAdultEvents = async (url, options) => {
  assert.equal(options.headers['X-Adult-Access-Token'], 'old-token');
  throw new Error('HTTP 403');
};
const recoverAdultEvents = async () => { throw new Error('must not query old job on rejected regeneration'); };
""" + "\n".join(functions) + r"""
(async () => {
 await startAdultGeneration('/regenerate', 'old-job');
 assert.equal(candidate.value, 'old candidate');
 assert.equal(accessToken.value, 'old-token');
 assert.equal(jobId.value, 'old-job');
 assert.equal(adultStatus.value, 'succeeded');
 assert.equal(adultError.value, 'HTTP 403');
 streamAdultEvents = async (url, options) => {
   assert.equal(candidate.value, 'old candidate');
   assert.equal(options.headers['X-Adult-Access-Token'], 'old-token');
   consumeAdultEvent('metadata', {job_id:'new-job', access_token:'new-token'});
   assert.equal(candidate.value, '');
 };
 await startAdultGeneration('/regenerate', 'old-job');
 assert.equal(accessToken.value, 'new-token');
 assert.equal(jobId.value, 'new-job');
 assert.equal(adultStatus.value, 'running');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["dashboard_ai_reader.html", "dashboard_settings_adult.html"])
def test_adult_template_scripts_have_valid_javascript_syntax(name):
    import re
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for script checks")
    html = Path("src/pixiv_novel_sync/templates", name).read_text(encoding="utf-8")
    for script in re.findall(r"<script[^>]*>([\s\S]*?)</script>", html):
        script = re.sub(r"\{\[[\s\S]*?\]\}", "null", script)
        result = subprocess.run([node, "--check", "-"], input=script, text=True, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr

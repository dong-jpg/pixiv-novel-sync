"""阅读同步的真实 JavaScript 状态机及阅读页接入契约。"""
from pathlib import Path
import hashlib
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_reading_progress_concurrency_and_offline_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for reading synchronization runtime checks")
    result = subprocess.run([node, "--test", "tests/test_reading_progress_sync.cjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_reading_progress_asset_is_loaded_with_its_exact_revision_before_page_setup():
    template = (ROOT / "src/pixiv_novel_sync/templates/dashboard_novel_detail.html").read_text(encoding="utf-8")
    asset = (ROOT / "src/pixiv_novel_sync/static/js/reading_progress_sync.js").read_bytes()
    version = re.search(r'/static/js/reading_progress_sync\.js\?v=([0-9a-f]+)', template)
    assert version is not None
    assert version.group(1) == hashlib.sha256(asset).hexdigest()[:12]
    assert template.index("reading_progress_sync.js") < template.index("window.ReadingProgressSync.create")
    assert "sendBeacon" not in template

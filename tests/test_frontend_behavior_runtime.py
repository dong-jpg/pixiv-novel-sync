from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_writing_frontend_executable_regressions():
    node = shutil.which('node')
    if node is None:
        pytest.skip('安装 Node.js 后运行写作页面的可执行行为回归')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [str(node), str(root / 'tests/test_writing_frontend_remediation.cjs')],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

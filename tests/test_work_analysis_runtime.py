from pathlib import Path
import shutil
import subprocess

import pytest


def test_work_analysis_runtime():
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the work-analysis VM regression")
    result = subprocess.run(
        [node, "--test", str(root / "tests/test_work_analysis_runtime.cjs")],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

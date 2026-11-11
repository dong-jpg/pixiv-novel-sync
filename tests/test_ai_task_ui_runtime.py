from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_ai_task_ui_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for AI task UI runtime tests")
    result = subprocess.run(
        [node, "--test", str(ROOT / "tests" / "test_ai_task_ui_runtime.cjs")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

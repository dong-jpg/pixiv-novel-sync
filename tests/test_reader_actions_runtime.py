from pathlib import Path
import shutil
import subprocess

import pytest


def test_reader_and_user_actions_runtime():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for frontend runtime regression checks')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / 'tests/test_reader_actions_runtime.cjs')],
                            cwd=root, capture_output=True, text=True,
                            encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_unused_bulk_user_delete_route_removed():
    import ast
    source = Path('src/pixiv_novel_sync/webapp.py').read_text(encoding='utf-8')
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == 'delete' and node.args:
                assert not (isinstance(node.args[0], ast.Constant)
                            and node.args[0].value == '/api/dashboard/users/<int:user_id>')

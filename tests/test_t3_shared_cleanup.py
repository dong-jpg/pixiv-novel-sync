from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from pixiv_novel_sync import cli
from pixiv_novel_sync.jobs import quick_sync


@pytest.mark.parametrize("truncated", [True, 1, 2])
def test_sync_bookmarks_truncated_exits_nonzero(monkeypatch, truncated):
    settings = SimpleNamespace(log_level="INFO")
    monkeypatch.setattr(sys, "argv", ["pixiv-novel-sync", "sync-bookmarks"])
    monkeypatch.setattr(cli, "load_settings", lambda **kwargs: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)

    def sync(actual_settings):
        assert actual_settings is settings
        return {"novels": 3, "truncated": truncated}

    monkeypatch.setattr(quick_sync, "run_bookmark_sync", sync)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1


@pytest.mark.parametrize("stats", [{"novels": 3}, {"truncated": False}, {"truncated": 0}])
def test_sync_bookmarks_complete_keeps_success_exit(monkeypatch, stats):
    monkeypatch.setattr(sys, "argv", ["pixiv-novel-sync", "sync-bookmarks"])
    monkeypatch.setattr(cli, "load_settings", lambda **kwargs: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(quick_sync, "run_bookmark_sync", lambda settings: stats)
    assert cli.main() is None


@pytest.mark.parametrize("args", [[], ["unknown-command"]])
def test_cli_parser_rejects_missing_or_unknown_command_before_loading_settings(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["pixiv-novel-sync", *args])

    def unexpected_load(**kwargs):
        pytest.fail("argparse must reject the command before loading settings")

    monkeypatch.setattr(cli, "load_settings", unexpected_load)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_importing_web_package_does_not_eagerly_load_submodules():
    import os
    from pathlib import Path
    import subprocess

    root = Path(__file__).resolve().parents[1]
    script = """
import sys
import pixiv_novel_sync.web
assert "pixiv_novel_sync.web.managers" not in sys.modules
assert "pixiv_novel_sync.web.utils" not in sys.modules
assert "pixiv_novel_sync.webapp" not in sys.modules
from pixiv_novel_sync.web import managers, utils
assert callable(managers.SettingsManager)
assert callable(utils._remove_archive_files)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_recommendation_callback_has_no_unemitted_rate_limit_branch():
    import ast
    import inspect
    from pixiv_novel_sync.jobs.tasks import _run_recommendation_run_task

    tree = ast.parse(inspect.getsource(_run_recommendation_run_task))
    assert not any(
        isinstance(node, ast.Constant) and node.value == "rate_limit"
        for node in ast.walk(tree)
    )


def test_sync_novel_has_no_duplicate_exception_handlers():
    import ast
    import inspect
    import textwrap
    from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService

    tree = ast.parse(textwrap.dedent(inspect.getsource(BookmarkNovelSyncService._sync_novel)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            handlers = [ast.dump(handler.type) for handler in node.handlers if handler.type]
            assert len(handlers) == len(set(handlers))


def test_sync_novel_preserves_cancellation_without_recording_a_failure(monkeypatch):
    from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService

    service = object.__new__(BookmarkNovelSyncService)
    cancelled = InterruptedError("Task stopped by user")

    def cancel(*args, **kwargs):
        raise cancelled

    monkeypatch.setattr(service, "_sync_novel_inner", cancel)
    with pytest.raises(InterruptedError) as exc:
        service._sync_novel(SimpleNamespace(id=123), "public", False, False, False, "bookmark")
    assert exc.value is cancelled


@pytest.mark.parametrize(
    "name",
    ["get_rescue_catalog_item", "list_rescue_catalog_sources", "get_recommendation_run"],
)
def test_test_only_storage_inspectors_are_not_production_methods(name):
    from pixiv_novel_sync.storage_db import Database

    assert not hasattr(Database, name)

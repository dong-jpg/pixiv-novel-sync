from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from pixiv_novel_sync.jobs.tasks import execute_task
from pixiv_novel_sync.settings import load_settings
from pixiv_novel_sync.web.managers import SettingsManager
from pixiv_novel_sync.webapp import create_app


@pytest.fixture
def retired_environment(tmp_path, monkeypatch):
    state_dir = tmp_path / "retired-control"
    monkeypatch.setenv("PIXIV_REMOTE_ENABLED", "1")
    monkeypatch.setenv("PIXIV_REMOTE_AI_EXECUTION", "remote")
    monkeypatch.setenv("PIXIV_REMOTE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("PIXIV_REMOTE_EGRESS_PROXY", "https://offline.invalid:8443")
    monkeypatch.setenv("INSTABOX_API_KEY", "retired-test-key")
    return state_dir


def test_legacy_execution_setting_is_ignored_and_removed_on_save(tmp_path, retired_environment):
    config = tmp_path / "config.yaml"
    config.write_text("sync:\n  pixiv_execution: remote\n  max_items_per_run: 17\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    settings = load_settings(config, env)
    assert not hasattr(settings.sync, "pixiv_execution")
    assert settings.sync.max_items_per_run == 17
    manager = SettingsManager(config)
    manager.save_sync_settings({"max_items_per_run": 19, "pixiv_execution": "remote"})
    saved = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert "pixiv_execution" not in saved["sync"]
    assert saved["sync"]["max_items_per_run"] == 19
    assert not retired_environment.exists()


def test_legacy_flags_do_not_dispatch_sync_to_cloud(monkeypatch, retired_environment):
    calls = []
    settings = SimpleNamespace(sync=SimpleNamespace(pixiv_execution="remote"))

    def local_sync(current, **kwargs):
        calls.append(current)
        assert kwargs["stop_requested"]() is False
        return {"novels": 1}

    monkeypatch.setattr("pixiv_novel_sync.jobs.quick_sync.run_bookmark_sync", local_sync)
    assert execute_task("bookmark", settings) == {"novels": 1}
    assert calls == [settings]
    assert not retired_environment.exists()


def test_retired_routes_and_navigation_are_absent(tmp_path, retired_environment):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    app = create_app(str(tmp_path / "missing.yaml"), str(env), start_scheduler=False)
    try:
        assert not any("remote" in rule.rule for rule in app.url_map.iter_rules())
        client = app.test_client()
        for path in ("/dashboard/remote", "/api/dashboard/remote/runs", "/api/remote-worker/v1/sessions"):
            assert client.get(path).status_code == 404
        page = client.get("/dashboard/settings/sync")
        assert page.status_code == 200
        assert b"/dashboard/remote" not in page.data
        assert b"pixiv_execution" not in page.data
        assert "pixiv_execution" not in client.get("/api/dashboard/settings").get_json()
        assert client.get("/api/health").get_json()["status"] == "ok"
        assert not retired_environment.exists()
    finally:
        app.extensions.get("close_work_analysis_owner", lambda: None)()

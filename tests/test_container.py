from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from pixiv_novel_sync import container
from pixiv_novel_sync.web.managers import SettingsManager
from pixiv_novel_sync.webapp import _load_or_create_flask_secret


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("sync:\n  max_items_per_run: 11\n", encoding="utf-8")
    (config_dir / ".env").write_text("DASHBOARD_TOKEN=container-test-password\n", encoding="utf-8")
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    return tmp_path


@pytest.mark.parametrize("missing", ["config.yaml", ".env"])
def test_missing_persistent_configuration_prevents_start(runtime, missing):
    (runtime / "config" / missing).unlink()
    with pytest.raises(RuntimeError, match="缺少"):
        container.prepare_runtime(runtime)


def test_missing_password_prevents_unusable_nas_service(runtime):
    (runtime / "config" / ".env").write_text("DASHBOARD_TOKEN=\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="DASHBOARD_TOKEN"):
        container.prepare_runtime(runtime)


def test_invalid_yaml_does_not_print_private_configuration(runtime, monkeypatch, capsys):
    (runtime / "config" / "config.yaml").write_text("pixiv: [cookie-private-fixture", encoding="utf-8")
    monkeypatch.chdir(runtime)
    with pytest.raises(SystemExit) as error:
        container.main()
    assert error.value.code == 1
    message = capsys.readouterr().err
    assert "格式无效" in message
    assert "cookie-private-fixture" not in message


def test_configuration_and_session_key_survive_runtime_restart(runtime, monkeypatch):
    config, env = container.prepare_runtime(runtime)
    first_secret = _load_or_create_flask_secret(str(env))
    SettingsManager(config).save_sync_settings({"max_items_per_run": 23})
    assert yaml.safe_load(config.read_text(encoding="utf-8"))["sync"]["max_items_per_run"] == 23
    assert f"PIXIV_FLASK_SECRET={first_secret}" in env.read_text(encoding="utf-8")
    monkeypatch.delenv("PIXIV_FLASK_SECRET")
    container.prepare_runtime(runtime)
    assert _load_or_create_flask_secret(str(env)) == first_secret
    assert "container-test-password" in env.read_text(encoding="utf-8")


def test_mount_permissions_are_checked_before_starting_server(runtime, monkeypatch, capsys):
    monkeypatch.chdir(runtime)

    def unwritable(**kwargs):
        raise PermissionError("read-only test mount")

    monkeypatch.setattr(container.tempfile, "TemporaryFile", unwritable)
    with pytest.raises(SystemExit) as error:
        container.main()
    assert error.value.code == 1
    message = capsys.readouterr().err
    assert "PIXIV_UID/PIXIV_GID" in message
    assert "container-test-password" not in message


def test_entrypoint_executes_one_cli_with_persistent_paths(runtime, monkeypatch):
    monkeypatch.chdir(runtime)
    monkeypatch.setattr(container.sys, "argv", ["container", "web-token-ui", "--host", "0.0.0.0", "--port", "5010"])
    executed = []
    monkeypatch.setattr(container.os, "execvp", lambda executable, args: executed.append((executable, args)))
    container.main()
    assert executed == [("pixiv-novel-sync", [
        "pixiv-novel-sync", "--config", str(runtime / "config" / "config.yaml"),
        "--env-file", str(runtime / "config" / ".env"),
        "web-token-ui", "--host", "0.0.0.0", "--port", "5010",
    ])]
    assert os.environ["ENV_PATH"] == str(runtime / "config" / ".env")


def test_compose_preserves_mutable_directories_without_injecting_stale_credentials():
    root = Path(__file__).resolve().parents[1]
    service = yaml.safe_load((root / "compose.yaml").read_text(encoding="utf-8"))["services"]["pixiv-novel-sync"]
    assert service["volumes"] == ["./config:/app/config", "./data:/app/data"]
    # env_file 会冻结 Token 为容器创建时的环境值，覆盖网页保存的新配置。
    assert "env_file" not in service
    assert "PIXIV_REFRESH_TOKEN" not in service["environment"]
    assert service["init"] is True
    assert "1000" in service["user"]

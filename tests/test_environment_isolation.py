from __future__ import annotations

import os
from pathlib import Path

from pixiv_novel_sync.settings import load_settings


def test_01_loading_env_file_sets_authentication_environment(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DASHBOARD_TOKEN=primary-token\n"
        "PIXIV_DASHBOARD_TOKEN=compatibility-token\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=tmp_path / "missing.yaml", env_path=env_path)

    assert settings.dashboard_token == "primary-token"
    assert os.environ["ENV_PATH"] == str(env_path)


def test_02_authentication_environment_does_not_leak_between_tests() -> None:
    assert "DASHBOARD_TOKEN" not in os.environ
    assert "PIXIV_DASHBOARD_TOKEN" not in os.environ
    assert "ENV_PATH" not in os.environ


def test_03_loading_env_file_can_populate_remote_settings(tmp_path: Path) -> None:
    env_path = tmp_path / "remote.env"
    env_path.write_text(
        "PIXIV_REMOTE_ENABLED=1\n"
        f"PIXIV_REMOTE_STATE_DIR={tmp_path.as_posix()}/remote\n"
        "PIXIV_REMOTE_MACHINE_ID=m_test_isolation\n"
        "INSTABOX_API_KEY=test-only-provider-key\n",
        encoding="utf-8",
    )
    load_settings(config_path=tmp_path / "missing.yaml", env_path=env_path)
    assert os.environ["PIXIV_REMOTE_ENABLED"] == "1"
    fixture_key_loaded = os.environ.get("INSTABOX_API_KEY") == "test-only-provider-key"
    assert fixture_key_loaded


def test_04_remote_environment_does_not_leak_into_ordinary_web_tests() -> None:
    # 旧 .env 的未知键仍可被 dotenv 加载，测试之间必须隔离。
    assert not any(name.startswith("PIXIV_REMOTE_") for name in os.environ)
    assert "INSTABOX_API_KEY" not in os.environ

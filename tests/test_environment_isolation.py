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

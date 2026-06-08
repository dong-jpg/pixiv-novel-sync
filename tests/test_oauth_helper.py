from __future__ import annotations

import os

from pixiv_novel_sync.oauth_helper import OAuthManager
from pixiv_novel_sync.settings import load_settings


def test_save_to_env_uses_configured_path_and_updates_process_env(tmp_path, monkeypatch):
    monkeypatch.delenv("ENV_PATH", raising=False)
    monkeypatch.delenv("PIXIV_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_USER_ID", raising=False)
    env_path = tmp_path / "custom.env"
    env_path.write_text("OTHER=value\nPIXIV_REFRESH_TOKEN=old\n", encoding="utf-8")

    OAuthManager(env_path=env_path).save_to_env("new-token", user_id=456)

    content = env_path.read_text(encoding="utf-8")
    assert "OTHER=value" in content
    assert "PIXIV_REFRESH_TOKEN=new-token" in content
    assert "PIXIV_USER_ID=456" in content
    assert env_path.with_suffix(".env.tmp").exists() is False
    assert os.environ["ENV_PATH"] == str(env_path)
    assert os.environ["PIXIV_REFRESH_TOKEN"] == "new-token"
    assert os.environ["PIXIV_USER_ID"] == "456"


def test_load_settings_records_env_path(tmp_path, monkeypatch):
    monkeypatch.delenv("ENV_PATH", raising=False)
    monkeypatch.delenv("PIXIV_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_USER_ID", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=token-from-file\n", encoding="utf-8")

    settings = load_settings(config_path=tmp_path / "missing.yaml", env_path=env_path)

    assert os.environ["ENV_PATH"] == str(env_path)
    assert settings.pixiv.refresh_token == "token-from-file"

"""空环境变量的语义：布尔与超时回落默认，代理只在「未设置」时回落 YAML。"""

from __future__ import annotations

from pathlib import Path

from pixiv_novel_sync.settings import _parse_bool, load_settings


def test_parse_bool_blank_keeps_default() -> None:
    assert _parse_bool("", default=True) is True
    assert _parse_bool("   ", default=True) is True
    assert _parse_bool(None, default=False) is False
    assert _parse_bool("false", default=True) is False
    assert _parse_bool("yes", default=False) is True


def _write_config(tmp_path: Path, *, proxy: str, timeout: int, verify_ssl: bool) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "pixiv:\n"
        f"  proxy: {proxy}\n"
        f"  timeout: {timeout}\n"
        f"  verify_ssl: {'true' if verify_ssl else 'false'}\n",
        encoding="utf-8",
    )
    return config_path


def test_blank_timeout_and_verify_ssl_fall_back_to_yaml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PIXIV_TIMEOUT", raising=False)
    monkeypatch.delenv("PIXIV_VERIFY_SSL", raising=False)
    monkeypatch.delenv("PIXIV_PROXY", raising=False)
    config_path = _write_config(tmp_path, proxy="http://yaml-proxy:8080", timeout=45, verify_ssl=True)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_TIMEOUT=\nPIXIV_VERIFY_SSL=\n", encoding="utf-8")

    settings = load_settings(config_path=config_path, env_path=env_path)

    assert settings.pixiv.timeout == 45
    assert settings.pixiv.verify_ssl is True


def test_unset_proxy_uses_yaml_and_blank_proxy_clears_it(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PIXIV_PROXY", raising=False)
    monkeypatch.delenv("PIXIV_TIMEOUT", raising=False)
    config_path = _write_config(tmp_path, proxy="http://yaml-proxy:8080", timeout=45, verify_ssl=True)

    unset = load_settings(config_path=config_path, env_path=tmp_path / "missing.env")
    assert unset.pixiv.proxy == "http://yaml-proxy:8080"

    monkeypatch.delenv("PIXIV_PROXY", raising=False)
    blank = tmp_path / "blank.env"
    blank.write_text("PIXIV_PROXY=\n", encoding="utf-8")
    cleared = load_settings(config_path=config_path, env_path=blank)
    assert cleared.pixiv.proxy is None

    monkeypatch.delenv("PIXIV_PROXY", raising=False)
    explicit = tmp_path / "explicit.env"
    explicit.write_text("PIXIV_PROXY=http://env-proxy:9\n", encoding="utf-8")
    overridden = load_settings(config_path=config_path, env_path=explicit)
    assert overridden.pixiv.proxy == "http://env-proxy:9"

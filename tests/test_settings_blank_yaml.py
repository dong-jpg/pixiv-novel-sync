"""手改 YAML 的留空与拼错（审查 S12）。

`settings.py` 以前用 `str(sync_raw.get(...))`：留空的 `auto_sync_timezone:` / `*_cron:`
在 YAML 里是 None，`str(None)` 得到字符串 "None"，时区回落 UTC 且每轮告警，cron 回落
interval，设置页再保存时还会被校验拒绝。`PIXIV_VERIFY_SSL` 拼错（如 flase）以前解析成
False，静默关掉证书校验；应当 fail-closed 回落默认（开启）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pixiv_novel_sync.settings import _parse_bool, load_settings


def _load(tmp_path: Path, yaml_text: str):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")
    # 指向不存在的 .env：不读开发机工作目录里的真实 .env
    return load_settings(config_path=config_path, env_path=tmp_path / "missing.env")


def test_blank_timezone_falls_back_to_utc_not_the_string_none(tmp_path: Path) -> None:
    settings = _load(tmp_path, "sync:\n  auto_sync_timezone:\n")

    assert settings.sync.auto_sync_timezone == "UTC"


def test_blank_cron_means_no_cron_not_the_string_none(tmp_path: Path) -> None:
    settings = _load(
        tmp_path,
        "sync:\n"
        "  auto_sync_bookmarks_cron:\n"
        "  auto_sync_preference_analyze_cron:\n"
        "  auto_sync_user_backup_cron: '  0 3 * * *  '\n",
    )

    # 留空 = 不用 cron、回落 interval，与设置页保存时写回的空串同义
    assert settings.sync.auto_sync_bookmarks_cron == ""
    assert settings.sync.auto_sync_preference_analyze_cron == ""
    assert settings.sync.auto_sync_user_backup_cron == "0 3 * * *"


def test_missing_cron_keys_keep_their_defaults(tmp_path: Path) -> None:
    settings = _load(tmp_path, "sync:\n  enabled: false\n")

    assert settings.sync.auto_sync_timezone == "UTC"
    assert settings.sync.auto_sync_bookmarks_cron == ""
    assert settings.sync.auto_sync_preference_analyze_cron == "15 7,19 * * *"
    assert settings.sync.auto_sync_recommendation_run_cron == "50 8 * * *"


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("flase", True),  # 拼错：fail-closed，保持校验
        ("ture", True),
        ("disabled", True),
        ("false", False),
        ("0", False),
        ("off", False),
        ("true", True),
    ],
)
def test_verify_ssl_env_typo_fails_closed(tmp_path: Path, monkeypatch, env_value: str, expected: bool) -> None:
    monkeypatch.setenv("PIXIV_VERIFY_SSL", env_value)

    settings = _load(tmp_path, "pixiv:\n  timeout: 30\n")

    assert settings.pixiv.verify_ssl is expected


def test_verify_ssl_yaml_string_false_is_respected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PIXIV_VERIFY_SSL", raising=False)

    settings = _load(tmp_path, "pixiv:\n  verify_ssl: 'false'\n")

    # 以前 bool("false") 为 True：用户写了引号的 false 反而开着
    assert settings.pixiv.verify_ssl is False


def test_unrecognized_bool_logs_a_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="pixiv_novel_sync.settings"):
        assert _parse_bool("flase", default=True, name="PIXIV_VERIFY_SSL") is True

    assert any("PIXIV_VERIFY_SSL" in record.getMessage() for record in caplog.records)

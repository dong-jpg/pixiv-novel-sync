"""请求间隔的缺省值：读取与设置页保存必须一致（审查 §6「需要决定的文档问题」）。

以前 `delay_seconds_between_items/pages` 缺键时 `load_settings` 回落 0.0（不限速），
`save_sync_settings` 却回落 1.0 并写回 YAML：手改配置删掉这两个键后，没点过保存的
进程零间隔连发请求，点一次保存又悄悄变成 1 秒。
"""

from __future__ import annotations

import pytest
import yaml

from pixiv_novel_sync.settings import load_settings
from pixiv_novel_sync.webapp import SettingsManager

KEYS = ("delay_seconds_between_items", "delay_seconds_between_pages")


def _write(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _load(path, tmp_path):
    return load_settings(config_path=path, env_path=tmp_path / "missing.env")


@pytest.mark.parametrize(
    "yaml_text",
    [
        "sync:\n  enabled: false\n",  # 缺键
        "sync:\n  delay_seconds_between_items:\n  delay_seconds_between_pages:\n",  # 留空
    ],
    ids=["missing", "blank"],
)
def test_load_and_save_agree_on_the_default_delay(tmp_path, yaml_text) -> None:
    path = _write(tmp_path, yaml_text)

    loaded = _load(path, tmp_path)
    saved = SettingsManager(str(path)).save_sync_settings({}, section="sync")
    reloaded = _load(path, tmp_path)

    for key in KEYS:
        assert getattr(loaded.sync, key) == 1.0
        assert saved[key] == 1.0
        assert getattr(reloaded.sync, key) == 1.0


def test_cleared_form_field_saves_the_default_and_explicit_zero_is_kept(tmp_path) -> None:
    path = _write(tmp_path, "sync:\n  delay_seconds_between_items: 3\n  delay_seconds_between_pages: 3\n")

    SettingsManager(str(path)).save_sync_settings(
        {"delay_seconds_between_items": "", "delay_seconds_between_pages": 0}, section="sync"
    )
    stored = yaml.safe_load(path.read_text(encoding="utf-8"))["sync"]

    assert stored["delay_seconds_between_items"] == 1.0  # 清空 = 回到默认
    assert stored["delay_seconds_between_pages"] == 0.0  # 显式 0 = 不限速
    assert _load(path, tmp_path).sync.delay_seconds_between_pages == 0.0

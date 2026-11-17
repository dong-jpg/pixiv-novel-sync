from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import yaml
import pytest

from pixiv_novel_sync.web import managers


def test_concurrent_toggle_and_section_save_preserve_both_updates(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("sync:\n  auto_sync_enabled: false\n  max_items_per_run: 10\n", encoding="utf-8")
    first = managers.SettingsManager(str(path))
    second = managers.SettingsManager(str(path))
    original = managers._load_yaml_file
    first_read = threading.Event()
    release = threading.Event()
    second_done = threading.Event()
    first_thread = None

    def held_read(config_path):
        result = original(config_path)
        if threading.get_ident() == first_thread:
            first_read.set()
            assert release.wait(5)
        return result

    def toggle():
        nonlocal first_thread
        first_thread = threading.get_ident()
        return first.set_auto_sync_enabled(True)

    def save():
        try:
            return second.save_sync_settings({"max_items_per_run": 40}, section="sync")
        finally:
            second_done.set()

    monkeypatch.setattr(managers, "_load_yaml_file", held_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        toggled = pool.submit(toggle)
        try:
            assert first_read.wait(5)
            saved = pool.submit(save)
            # 锁若只保护 os.replace，第二个写入会在这里先完成，再被第一个旧快照覆盖。
            second_done.wait(0.5)
        finally:
            release.set()
        assert toggled.result(timeout=5) is True
        saved.result(timeout=5)

    values = yaml.safe_load(path.read_text(encoding="utf-8"))["sync"]
    assert values["auto_sync_enabled"] is True
    assert values["max_items_per_run"] == 40


def test_inflight_load_cannot_resurrect_cache_after_save(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("sync:\n  max_items_per_run: 10\n", encoding="utf-8")
    manager = managers.SettingsManager(str(path))
    original = managers.load_settings
    read_old = threading.Event()
    release = threading.Event()
    saved_done = threading.Event()
    first_thread = None

    def held_load(*args, **kwargs):
        result = original(*args, **kwargs)
        if threading.get_ident() == first_thread and not release.is_set():
            read_old.set()
            assert release.wait(5)
        return result

    def load():
        nonlocal first_thread
        first_thread = threading.get_ident()
        return manager.load()

    def save():
        try:
            return manager.save_sync_settings({"max_items_per_run": 40}, section="sync")
        finally:
            saved_done.set()

    monkeypatch.setattr(managers, "load_settings", held_load)
    with ThreadPoolExecutor(max_workers=2) as pool:
        loading = pool.submit(load)
        try:
            assert read_old.wait(5)
            saving = pool.submit(save)
            saved_done.wait(0.5)
        finally:
            release.set()
        loading.result(timeout=5)
        saving.result(timeout=5)
    assert manager.load().sync.max_items_per_run == 40


def test_settings_cache_is_scoped_to_requested_environment(tmp_path, monkeypatch):
    first, second = object(), object()
    calls = []

    def load(_config, env):
        calls.append(env)
        return first if env == "first.env" else second

    monkeypatch.setattr(managers, "load_settings", load)
    manager = managers.SettingsManager(str(tmp_path / "config.yaml"))
    assert manager.load("first.env") is first
    assert manager.load("second.env") is second
    assert manager.load("second.env") is second
    assert calls == ["first.env", "second.env"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "NaN", "Infinity", "-Infinity"])
def test_nonfinite_delay_does_not_modify_config(tmp_path, value):
    path = tmp_path / "config.yaml"
    original = "sync:\n  delay_seconds_between_items: 3\n"
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="有限数"):
        managers.SettingsManager(str(path)).save_sync_settings(
            {"delay_seconds_between_items": value}, section="sync"
        )
    assert path.read_text(encoding="utf-8") == original

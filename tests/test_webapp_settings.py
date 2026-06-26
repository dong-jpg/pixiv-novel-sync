from __future__ import annotations

import yaml

from pixiv_novel_sync.settings import PixivSettings, Settings, StorageSettings, SyncSettings, load_settings
from pixiv_novel_sync.sync_check import build_sync_check_fingerprint, sync_check_task_types
from pixiv_novel_sync.webapp import SettingsManager, SyncJobManager, SyncJobState, _settings_to_dict


def make_settings(tmp_path) -> Settings:
    return Settings(
        pixiv=PixivSettings(refresh_token="", access_token=None, proxy=None, timeout=30, verify_ssl=True, user_id=123),
        sync=SyncSettings(
            enabled=True,
            initial_manual_only=False,
            download_assets=True,
            write_markdown=True,
            write_raw_text=True,
            bookmark_restricts=["public"],
            max_items_per_run=None,
            max_pages_per_run=None,
            delay_seconds_between_items=0,
            delay_seconds_between_pages=0,
            sync_bookmarks=True,
            sync_following_novels=True,
        ),
        storage=StorageSettings(
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
            db_path=tmp_path / "state" / "sync.db",
        ),
    )


def test_save_sync_settings_allows_zero_following_novels_users_limit(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("sync:\n  auto_sync_following_novels_users_limit: 5\n", encoding="utf-8")

    saved = SettingsManager(str(config_path)).save_sync_settings(
        {"auto_sync_following_novels_users_limit": 0}
    )

    assert saved["auto_sync_following_novels_users_limit"] == 0
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["sync"]["auto_sync_following_novels_users_limit"] == 0


def test_save_sync_settings_clamps_negative_following_novels_users_limit(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("sync:\n  auto_sync_following_novels_users_limit: 5\n", encoding="utf-8")

    saved = SettingsManager(str(config_path)).save_sync_settings(
        {"auto_sync_following_novels_users_limit": -3}
    )

    assert saved["auto_sync_following_novels_users_limit"] == 0


def test_load_settings_reads_pending_deletion_cleanup_days(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "sync:\n"
        "  pending_deletion_grace_period_days: 45\n"
        "  pending_deletion_cleanup_confirmed_days: 9\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=config_path, env_path=tmp_path / ".env")

    assert settings.sync.pending_deletion_grace_period_days == 45
    assert settings.sync.pending_deletion_cleanup_confirmed_days == 9


def test_dashboard_settings_payload_includes_preference_and_pending_cleanup(tmp_path):
    payload = _settings_to_dict(make_settings(tmp_path))

    assert payload["auto_sync_preference_analyze_enabled"] is False
    assert payload["auto_sync_preference_analyze_cron"] == "*/30 * * * *"
    assert payload["preference_analyze_batch_size"] == 200
    assert payload["pending_deletion_grace_period_days"] == 30
    assert payload["pending_deletion_cleanup_confirmed_days"] == 7


def test_save_sync_settings_persists_pending_cleanup_days(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("sync: {}\n", encoding="utf-8")

    saved = SettingsManager(str(config_path)).save_sync_settings(
        {
            "pending_deletion_grace_period_days": 21,
            "pending_deletion_cleanup_confirmed_days": 3,
        }
    )

    assert saved["pending_deletion_grace_period_days"] == 21
    assert saved["pending_deletion_cleanup_confirmed_days"] == 3
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["sync"]["pending_deletion_grace_period_days"] == 21
    assert config["sync"]["pending_deletion_cleanup_confirmed_days"] == 3


def test_latest_matching_sync_check_scope_requires_same_fingerprint_and_task(tmp_path):
    settings = make_settings(tmp_path)
    fingerprint = build_sync_check_fingerprint(settings, 123)
    manager = SyncJobManager(config_path=None, env_path=None)
    manager._jobs["check_old"] = SyncJobState(
        job_id="check_old",
        status="succeeded",
        finished_at=1,
        progress={
            "sync_check_scope": "check_old",
            "sync_check_fingerprint": "stale",
            "sync_check_task_types": ["bookmark"],
        },
    )
    manager._jobs["check_new"] = SyncJobState(
        job_id="check_new",
        status="succeeded",
        finished_at=2,
        progress={
            "sync_check_scope": "check_new",
            "sync_check_fingerprint": fingerprint,
            "sync_check_task_types": sync_check_task_types(settings),
        },
    )

    assert manager.latest_matching_sync_check_scope(settings, 123, "bookmark") == ("check_new", "check_new")
    assert manager.latest_matching_sync_check_scope(settings, 123, "subscribed_series") is None

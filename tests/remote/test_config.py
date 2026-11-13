import pytest

from pixiv_novel_sync.remote.config import RemoteConfig
from pixiv_novel_sync.remote.contracts import RemoteError


def test_disabled_needs_no_secrets_or_disk(tmp_path):
    assert not RemoteConfig.from_env({}, "web").enabled
    assert not RemoteConfig.from_env({}, "controller").enabled
    assert list(tmp_path.iterdir()) == []


def test_roles_do_not_retain_unneeded_secrets(tmp_path):
    env = {"PIXIV_REMOTE_ENABLED": "1", "PIXIV_REMOTE_STATE_DIR": str(tmp_path / "control"),
           "PIXIV_REMOTE_MACHINE_ID": "m_fixture", "PIXIV_REMOTE_WORKER_KEY_SHA256": "a" * 64,
           "INSTABOX_API_KEY": "provider-secret", "PIXIV_REMOTE_WORKER_KEY": "w" * 32}
    web = RemoteConfig.from_env(env, "web")
    controller = RemoteConfig.from_env(env, "controller")
    assert web.api_key is None and web.worker_key is None
    assert controller.worker_key is None and controller.worker_key_sha256 is None
    assert "provider-secret" not in repr(controller)
    assert not (tmp_path / "control").exists()


@pytest.mark.parametrize("origin", ["http://host.test", "https://u:p@host.test", "https://host.test/api",
                                   "https://host.test/?token=x", "https://host.test/#x", "https:///", "//host.test"])
def test_worker_rejects_non_origin_urls(tmp_path, origin):
    env = {"PIXIV_REMOTE_SERVER_ORIGIN": origin, "PIXIV_REMOTE_WORK_DIR": str(tmp_path / "work"),
           "PIXIV_REMOTE_WORKER_KEY": "w" * 32}
    with pytest.raises(RemoteError):
        RemoteConfig.from_env(env, "worker")


def test_worker_requires_explicit_configuration_and_hides_key(tmp_path):
    with pytest.raises(RemoteError):
        RemoteConfig.from_env({}, "worker")
    env = {"PIXIV_REMOTE_SERVER_ORIGIN": "https://host.test/", "PIXIV_REMOTE_WORK_DIR": str(tmp_path / "work"),
           "PIXIV_REMOTE_WORKER_KEY": "w" * 32, "INSTABOX_API_KEY": "unneeded"}
    cfg = RemoteConfig.from_env(env, "worker")
    assert cfg.enabled and cfg.server_origin == "https://host.test"
    assert cfg.api_key is None and cfg.state_dir is None
    assert "w" * 32 not in repr(cfg)


def test_invalid_enable_and_library_overlap_are_rejected(tmp_path):
    with pytest.raises(RemoteError):
        RemoteConfig.from_env({"PIXIV_REMOTE_ENABLED": "true"}, "web")
    env = {"PIXIV_REMOTE_ENABLED": "1", "PIXIV_REMOTE_STATE_DIR": str(tmp_path),
           "PIXIV_REMOTE_MACHINE_ID": "m_fixture", "PIXIV_REMOTE_WORKER_KEY_SHA256": "a" * 64,
           "PIXIV_DB_PATH": str(tmp_path / "control.db")}
    with pytest.raises(RemoteError, match="state_overlaps_library"):
        RemoteConfig.from_env(env, "web")
    env["PIXIV_REMOTE_STATE_DIR"] = "relative-directory"
    with pytest.raises(RemoteError, match="absolute_directory_required"):
        RemoteConfig.from_env(env, "web")

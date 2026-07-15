from __future__ import annotations

import errno
import importlib
import os
import stat
from pathlib import Path
from types import ModuleType
from typing import Callable

import pytest

from pixiv_novel_sync import oauth_helper, sync_engine, webapp


SecureWriter = Callable[[Path, bytes, int], None]


def _load_env_module() -> ModuleType:
    try:
        module = importlib.import_module("pixiv_novel_sync.utils_env")
    except ModuleNotFoundError as exc:
        if exc.name == "pixiv_novel_sync.utils_env":
            pytest.fail("缺少 pixiv_novel_sync.utils_env 安全写入模块", pytrace=False)
        raise
    if not hasattr(module, "secure_atomic_write"):
        pytest.fail("缺少 secure_atomic_write() 安全写入器", pytrace=False)
    return module


def _capture_writer(calls: list[tuple[Path, bytes, int]]) -> SecureWriter:
    def capture(path: Path, payload: bytes, mode: int = 0o600) -> None:
        target = Path(path)
        calls.append((target, payload, mode))
        target.write_bytes(payload)

    return capture


def _private_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_secure_atomic_write_uses_safe_flags_and_atomic_sequence(tmp_path, monkeypatch):
    module = _load_env_module()
    target = tmp_path / ".env"
    target.write_bytes(b"old\n")
    payload = b"PIXIV_REFRESH_TOKEN=new\n"
    opened: list[tuple[Path, int, int, int]] = []
    events: list[str] = []

    real_open = os.open
    real_fsync = os.fsync
    real_replace = os.replace
    real_chmod = os.chmod

    monkeypatch.setattr(module.secrets, "token_hex", lambda _size: "random-token")

    def open_spy(path, flags, mode=0o777):
        fd = real_open(path, flags, mode)
        opened.append((Path(path), flags, mode, fd))
        return fd

    def fsync_spy(fd):
        assert fd == opened[0][3]
        events.append("fsync")
        return real_fsync(fd)

    def replace_spy(source, destination):
        assert Path(destination) == target
        assert target.read_bytes() == b"old\n"
        with pytest.raises(OSError):
            os.fstat(opened[0][3])
        events.append("replace")
        return real_replace(source, destination)

    def chmod_spy(path, mode):
        assert Path(path) == target
        assert target.read_bytes() == payload
        events.append("chmod")
        return real_chmod(path, mode)

    monkeypatch.setattr(module.os, "open", open_spy)
    monkeypatch.setattr(module.os, "fsync", fsync_spy)
    monkeypatch.setattr(module.os, "replace", replace_spy)
    monkeypatch.setattr(module.os, "chmod", chmod_spy)

    module.secure_atomic_write(target, payload)

    temporary, flags, create_mode, _fd = opened[0]
    assert temporary == target.with_name(".env.random-token.tmp")
    assert flags & os.O_CREAT
    assert flags & os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        assert flags & os.O_NOFOLLOW
    assert create_mode == 0o600
    assert events == ["fsync", "replace", "chmod"]
    assert target.read_bytes() == payload
    assert not temporary.exists()
    if os.name != "nt":
        assert _private_mode(target) == 0o600


def test_secure_atomic_write_retries_short_writes(tmp_path, monkeypatch):
    module = _load_env_module()
    target = tmp_path / ".env"
    payload = b"abcdefghijklmnopqrstuvwxyz"
    real_write = os.write
    write_sizes: list[int] = []

    def short_write(fd, data):
        chunk = bytes(data[:3])
        write_sizes.append(len(chunk))
        return real_write(fd, chunk)

    monkeypatch.setattr(module.os, "write", short_write)

    module.secure_atomic_write(target, payload)

    assert target.read_bytes() == payload
    assert len(write_sizes) > 1
    assert sum(write_sizes) == len(payload)


def test_secure_atomic_write_rejects_zero_length_write(tmp_path, monkeypatch):
    module = _load_env_module()
    target = tmp_path / ".env"
    target.write_bytes(b"old\n")
    calls = 0

    def zero_write(_fd, _data):
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr(module.os, "write", zero_write)

    with pytest.raises(OSError, match="os.write"):
        module.secure_atomic_write(target, b"new\n")

    assert calls == 1
    assert target.read_bytes() == b"old\n"


def test_secure_atomic_write_does_not_follow_prebuilt_symlink(tmp_path, monkeypatch):
    module = _load_env_module()
    target = tmp_path / ".env"
    target.write_bytes(b"old\n")
    victim = tmp_path / "victim"
    victim.write_bytes(b"do-not-touch")
    candidate = target.with_name(".env.blocked.tmp")
    try:
        candidate.symlink_to(victim)
    except NotImplementedError as exc:
        pytest.skip(f"当前平台无法创建测试符号链接：{exc}")
    except OSError as exc:
        unsupported_errnos = {
            value
            for name in ("ENOSYS", "ENOTSUP", "EOPNOTSUPP")
            if (value := getattr(errno, name, None)) is not None
        }
        permission_errnos = {errno.EACCES, errno.EPERM}
        if (
            isinstance(exc, PermissionError)
            or exc.errno in permission_errnos | unsupported_errnos
            or getattr(exc, "winerror", None) == 1314
        ):
            pytest.skip(f"当前平台无法创建测试符号链接：{exc}")
        raise

    monkeypatch.setattr(module.secrets, "token_hex", lambda _size: "blocked")

    with pytest.raises(FileExistsError):
        module.secure_atomic_write(target, b"new\n")

    assert candidate.is_symlink()
    assert victim.read_bytes() == b"do-not-touch"
    assert target.read_bytes() == b"old\n"


def test_secure_atomic_write_cleans_only_its_created_temporary_file(tmp_path, monkeypatch):
    module = _load_env_module()
    target = tmp_path / ".env"
    target.write_bytes(b"old\n")
    preexisting = tmp_path / "preexisting.tmp"
    preexisting.write_bytes(b"keep")
    opened: list[tuple[Path, int]] = []
    real_open = os.open

    def open_spy(path, flags, mode=0o777):
        fd = real_open(path, flags, mode)
        opened.append((Path(path), fd))
        return fd

    def failing_fsync(_fd):
        raise OSError("fsync failed")

    monkeypatch.setattr(module.os, "open", open_spy)
    monkeypatch.setattr(module.os, "fsync", failing_fsync)

    with pytest.raises(OSError, match="fsync failed"):
        module.secure_atomic_write(target, b"new\n")

    temporary, fd = opened[0]
    with pytest.raises(OSError):
        os.fstat(fd)
    assert not temporary.exists()
    assert preexisting.read_bytes() == b"keep"
    assert target.read_bytes() == b"old\n"


def test_secure_atomic_write_does_not_close_released_fd_twice(tmp_path, monkeypatch):
    module = _load_env_module()
    target = tmp_path / ".env"
    target.write_bytes(b"old\n")
    temporary = target.with_name(".env.close-error.tmp")
    real_close = os.close
    close_calls: list[int] = []

    def close_after_release(fd):
        close_calls.append(fd)
        if len(close_calls) == 1:
            real_close(fd)
            raise OSError("close failed after release")
        raise OSError("descriptor closed twice")

    monkeypatch.setattr(module.secrets, "token_hex", lambda _size: "close-error")
    monkeypatch.setattr(module.os, "close", close_after_release)

    with pytest.raises(OSError, match="close failed after release"):
        module.secure_atomic_write(target, b"new\n")

    assert len(close_calls) == 1
    assert not temporary.exists()
    assert target.read_bytes() == b"old\n"


def test_oauth_save_delegates_final_bytes_to_shared_writer(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("OTHER=value\nPIXIV_REFRESH_TOKEN=old\n", encoding="utf-8")
    calls: list[tuple[Path, bytes, int]] = []
    monkeypatch.delenv("ENV_PATH", raising=False)
    monkeypatch.delenv("PIXIV_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_USER_ID", raising=False)
    monkeypatch.setattr(oauth_helper, "secure_atomic_write", _capture_writer(calls), raising=False)

    oauth_helper.OAuthManager(env_path).save_to_env("new-token", user_id=456)

    assert calls == [
        (
            env_path,
            b"OTHER=value\nPIXIV_REFRESH_TOKEN=new-token\nPIXIV_USER_ID=456\n",
            0o600,
        )
    ]


def test_flask_secret_delegates_final_bytes_to_shared_writer(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    calls: list[tuple[Path, bytes, int]] = []
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    monkeypatch.setattr(webapp, "secure_atomic_write", _capture_writer(calls), raising=False)
    monkeypatch.setattr(webapp.os, "urandom", lambda size: b"\xab" * size)

    secret = webapp._load_or_create_flask_secret(str(env_path))

    assert secret == "ab" * 32
    assert calls == [
        (
            env_path,
            f"PIXIV_REFRESH_TOKEN=test\nPIXIV_FLASK_SECRET={secret}\n".encode(),
            0o600,
        )
    ]


def test_web_cookie_delegates_final_bytes_to_shared_writer(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("OTHER=value\nPIXIV_WEB_COOKIE=old\n", encoding="utf-8")
    calls: list[tuple[Path, bytes, int]] = []
    monkeypatch.setenv("ENV_PATH", str(env_path))
    monkeypatch.setattr(sync_engine, "secure_atomic_write", _capture_writer(calls), raising=False)

    sync_engine.BookmarkNovelSyncService._save_web_cookie_to_env(object(), "new-cookie")

    assert calls == [
        (env_path, b"OTHER=value\nPIXIV_WEB_COOKIE=new-cookie\n", 0o600)
    ]


@pytest.mark.skipif(os.name == "nt", reason="Windows 无法可靠验证 POSIX 文件 mode")
def test_oauth_save_keeps_existing_env_private(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=old\n", encoding="utf-8")
    env_path.chmod(0o600)
    monkeypatch.delenv("ENV_PATH", raising=False)
    monkeypatch.delenv("PIXIV_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_USER_ID", raising=False)

    oauth_helper.OAuthManager(env_path).save_to_env("new-token")

    assert _private_mode(env_path) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Windows 无法可靠验证 POSIX 文件 mode")
def test_flask_secret_keeps_existing_env_private(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    env_path.chmod(0o600)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)

    webapp._load_or_create_flask_secret(str(env_path))

    assert _private_mode(env_path) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Windows 无法可靠验证 POSIX 文件 mode")
def test_web_cookie_keeps_existing_env_private(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_WEB_COOKIE=old\n", encoding="utf-8")
    env_path.chmod(0o600)
    monkeypatch.setenv("ENV_PATH", str(env_path))

    sync_engine.BookmarkNovelSyncService._save_web_cookie_to_env(object(), "new-cookie")

    assert _private_mode(env_path) == 0o600

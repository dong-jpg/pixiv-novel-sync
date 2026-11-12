"""Role-specific configuration without dotenv, business settings, or I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit
import re

from .contracts import RemoteError


def _directory(value: str | None) -> Path:
    if not value or not Path(value).is_absolute():
        raise RemoteError("absolute_directory_required", 400)
    path = Path(value)
    if path.parent == path or path.is_symlink() or path.resolve() != path.absolute():
        raise RemoteError("dedicated_directory_required", 400)
    return path


def validate_origin(value: str | None) -> str:
    try:
        parsed = urlsplit(value or "")
        port = parsed.port
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or "?" in (value or "") or "#" in (value or "")
                or any(c.isspace() or ord(c) < 32 for c in (value or ""))
                or "\\" in (value or "") or (port is not None and not 1 <= port <= 65535)):
            raise ValueError
    except (ValueError, TypeError):
        raise RemoteError("invalid_server_origin", 400) from None
    return f"https://{parsed.netloc}"


@dataclass(frozen=True)
class RemoteConfig:
    enabled: bool
    role: str
    state_dir: Path | None = None
    machine_id: str | None = None
    worker_key_sha256: str | None = None
    server_origin: str | None = None
    work_dir: Path | None = None
    api_key: str | None = field(default=None, repr=False)
    worker_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str], role: str) -> "RemoteConfig":
        if role not in {"web", "controller", "worker"}:
            raise RemoteError("invalid_remote_role", 400)
        enabled = env.get("PIXIV_REMOTE_ENABLED", "0")
        if enabled not in {"0", "1"}:
            raise RemoteError("invalid_remote_enabled", 400)
        if role == "worker":
            work_dir = _directory(env.get("PIXIV_REMOTE_WORK_DIR"))
            origin = validate_origin(env.get("PIXIV_REMOTE_SERVER_ORIGIN"))
            key = env.get("PIXIV_REMOTE_WORKER_KEY", "")
            if re.fullmatch(r"[!-~]{32,256}", key) is None:
                raise RemoteError("invalid_worker_key", 400)
            for library_key in ("PIXIV_PUBLIC_DIR", "PIXIV_PRIVATE_DIR"):
                if env.get(library_key) and work_dir.resolve() == Path(env[library_key]).resolve():
                    raise RemoteError("work_overlaps_library", 400)
            return cls(True, role, server_origin=origin, work_dir=work_dir, worker_key=key)
        if enabled == "0":
            return cls(False, role)
        state_dir = _directory(env.get("PIXIV_REMOTE_STATE_DIR"))
        if env.get("PIXIV_DB_PATH") and (state_dir / "control.db").resolve() == Path(env["PIXIV_DB_PATH"]).resolve():
            raise RemoteError("state_overlaps_library", 400)
        machine_id = env.get("PIXIV_REMOTE_MACHINE_ID", "")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", machine_id) is None:
            raise RemoteError("invalid_machine_id", 400)
        if role == "web":
            key_hash = env.get("PIXIV_REMOTE_WORKER_KEY_SHA256", "").lower()
            if re.fullmatch(r"[0-9a-f]{64}", key_hash) is None:
                raise RemoteError("invalid_worker_key_hash", 400)
            return cls(True, role, state_dir=state_dir, machine_id=machine_id, worker_key_sha256=key_hash)
        api_key = env.get("INSTABOX_API_KEY", "")
        if re.fullmatch(r"[!-~]{1,512}", api_key) is None:
            raise RemoteError("invalid_provider_key", 400)
        return cls(True, role, state_dir=state_dir, machine_id=machine_id, api_key=api_key)

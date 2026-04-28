from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os

import yaml
from dotenv import load_dotenv


@dataclass(slots=True)
class PixivSettings:
    refresh_token: str
    access_token: str | None
    proxy: str | None
    timeout: int
    verify_ssl: bool
    user_id: int | None


@dataclass(slots=True)
class SyncSettings:
    enabled: bool
    initial_manual_only: bool
    download_assets: bool
    write_markdown: bool
    write_raw_text: bool
    bookmark_restricts: list[str]
    max_items_per_run: int | None
    max_pages_per_run: int | None
    delay_seconds_between_items: float
    delay_seconds_between_pages: float


@dataclass(slots=True)
class StorageSettings:
    public_dir: Path
    private_dir: Path
    db_path: Path


@dataclass(slots=True)
class Settings:
    pixiv: PixivSettings
    sync: SyncSettings
    storage: StorageSettings
    log_level: str = "INFO"


DEFAULT_CONFIG_PATH = Path("config/config.yaml")


def load_settings(config_path: str | Path | None = None, env_path: str | Path | None = None) -> Settings:
    if env_path is not None:
        load_dotenv(env_path)
    else:
        load_dotenv()

    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    raw_config = _load_yaml(path) if path.exists() else {}

    pixiv_raw = raw_config.get("pixiv", {})
    sync_raw = raw_config.get("sync", {})
    storage_raw = raw_config.get("storage", {})

    refresh_token = os.getenv("PIXIV_REFRESH_TOKEN", "").strip()
    access_token = os.getenv("PIXIV_ACCESS_TOKEN", "").strip() or None
    proxy = os.getenv("PIXIV_PROXY") or pixiv_raw.get("proxy")
    timeout = int(os.getenv("PIXIV_TIMEOUT", pixiv_raw.get("timeout", 30)))
    verify_ssl = _parse_bool(os.getenv("PIXIV_VERIFY_SSL"), default=pixiv_raw.get("verify_ssl", True))
    user_id = _parse_optional_int(os.getenv("PIXIV_USER_ID"))

    public_dir = Path(os.getenv("PIXIV_PUBLIC_DIR", storage_raw.get("public_dir", "./data/library/public")))
    private_dir = Path(os.getenv("PIXIV_PRIVATE_DIR", storage_raw.get("private_dir", "./data/library/private")))
    db_path = Path(os.getenv("PIXIV_DB_PATH", storage_raw.get("db_path", "./data/state/pixiv_sync.db")))
    log_level = os.getenv("PIXIV_LOG_LEVEL", "INFO").upper()

    return Settings(
        pixiv=PixivSettings(
            refresh_token=refresh_token,
            access_token=access_token,
            proxy=proxy,
            timeout=timeout,
            verify_ssl=verify_ssl,
            user_id=user_id,
        ),
        sync=SyncSettings(
            enabled=bool(sync_raw.get("enabled", False)),
            initial_manual_only=bool(sync_raw.get("initial_manual_only", True)),
            download_assets=bool(sync_raw.get("download_assets", True)),
            write_markdown=bool(sync_raw.get("write_markdown", True)),
            write_raw_text=bool(sync_raw.get("write_raw_text", True)),
            bookmark_restricts=list(sync_raw.get("bookmark_restricts", ["public", "private"])),
            max_items_per_run=_coerce_optional_int(sync_raw.get("max_items_per_run")),
            max_pages_per_run=_coerce_optional_int(sync_raw.get("max_pages_per_run")),
            delay_seconds_between_items=_coerce_float(sync_raw.get("delay_seconds_between_items"), 0.0),
            delay_seconds_between_pages=_coerce_float(sync_raw.get("delay_seconds_between_pages"), 0.0),
        ),
        storage=StorageSettings(
            public_dir=public_dir,
            private_dir=private_dir,
            db_path=db_path,
        ),
        log_level=log_level,
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a mapping: {path}")
    return data


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return bool(default)
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _parse_optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value)



def _coerce_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None



def _coerce_float(value: Any, default: float) -> float:
    if value in (None, ""):
        return float(default)
    return float(value)

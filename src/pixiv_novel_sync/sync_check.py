from __future__ import annotations

import hashlib
import json

from .settings import Settings


def build_sync_check_fingerprint(settings: Settings, user_id: int | None) -> str:
    """Return a stable key for the precheck inputs that affect skip decisions."""
    sync = settings.sync
    payload = {
        "user_id": int(user_id) if user_id is not None else None,
        "db_path": str(settings.storage.db_path),
        "download_assets": bool(sync.download_assets),
        "bookmark_restricts": list(sync.bookmark_restricts),
        "sync_bookmarks": bool(sync.sync_bookmarks),
        "sync_following_novels": bool(sync.sync_following_novels),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def sync_check_task_types(settings: Settings) -> list[str]:
    tasks: list[str] = []
    if settings.sync.sync_bookmarks:
        tasks.append("bookmark")
    if settings.sync.sync_following_novels:
        tasks.append("following_novels")
    return tasks

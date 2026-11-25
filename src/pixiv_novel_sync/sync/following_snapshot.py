from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any


def save_following_snapshot(db: Any, owner_id: int, users: list[Any]) -> None:
    db.update_watermark(f"following_public:{int(owner_id)}", {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "members": {str(int(user.id)): str(getattr(user, "name", "") or user.id) for user in users},
    })


def read_following_snapshot(db: Any, owner_id: int | None) -> list[SimpleNamespace] | None:
    if not owner_id:
        return None
    data = db.get_watermark(f"following_public:{int(owner_id)}") or {}
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(data["observed_at"])
        if not timedelta(0) <= age <= timedelta(hours=36):
            return None
        return [SimpleNamespace(id=int(author_id), name=name) for author_id, name in data["members"].items()]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None

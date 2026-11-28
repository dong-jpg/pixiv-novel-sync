from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any


def save_following_snapshot(db: Any, owner_id: int, users: list[Any], restrict: str = "public") -> None:
    db.update_watermark(f"following_{restrict}:{int(owner_id)}", {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "members": {str(int(user.id)): str(getattr(user, "name", "") or user.id) for user in users},
    })
    db.update_watermark("following_owner", {"user_id": int(owner_id)})


def read_following_snapshot(db: Any, owner_id: int | None, restrict: str = "public") -> list[SimpleNamespace] | None:
    if not owner_id:
        return None
    data = db.get_watermark(f"following_{restrict}:{int(owner_id)}") or {}
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(data["observed_at"])
        if not timedelta(0) <= age <= timedelta(hours=36):
            return None
        return [SimpleNamespace(id=int(author_id), name=name) for author_id, name in data["members"].items()]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def known_following_members(db, owner_id):
    if not owner_id:
        profile = db.get_watermark("self_profile") or db.get_watermark("following_owner") or {}
        owner_id = profile.get("user_id")
    if not owner_id:
        return None
    members = {}
    try:
        for scope in ("public", "private"):
            data = db.get_watermark(f"following_{scope}:{int(owner_id)}") or {}
            if not isinstance(data.get("members"), dict):
                return None
            for author_id in data["members"]:
                identity = int(author_id)
                if identity <= 0:
                    return None
                members.setdefault(identity, []).append(scope)
    except (AttributeError, TypeError, ValueError):
        return None
    return members

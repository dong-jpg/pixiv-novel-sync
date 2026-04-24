from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class UserRecord:
    user_id: int
    name: str
    account: str | None
    raw_json: str


@dataclass(slots=True)
class NovelRecord:
    novel_id: int
    user_id: int
    series_id: int | None
    title: str
    caption: str | None
    visible: bool
    restrict: str
    x_restrict: int
    text_length: int
    total_bookmarks: int
    total_views: int
    cover_url: str | None
    tags_json: str
    create_date: str | None
    raw_json: str
    meta_hash: str


@dataclass(slots=True)
class NovelTextRecord:
    novel_id: int
    text_raw: str
    text_markdown: str | None
    text_hash: str


@dataclass(slots=True)
class AssetRecord:
    novel_id: int
    asset_type: str
    remote_url: str
    local_path: str
    file_hash: str | None


@dataclass(slots=True)
class SourceRecord:
    novel_id: int
    source_type: str
    source_key: str


def as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}

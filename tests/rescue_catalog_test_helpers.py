"""Read-only raw catalog inspectors used by storage and API regressions."""
from __future__ import annotations

from typing import Any

from pixiv_novel_sync.storage_db import Database


def get_rescue_catalog_item(
    db: Database,
    item_type: str,
    item_id: int,
) -> dict[str, Any] | None:
    normalized_type = db._validate_rescue_item_type(item_type)
    row = db.conn.execute(
        """
        SELECT *
        FROM rescue_catalog
        WHERE item_type = ? AND item_id = ?
        """,
        (normalized_type, int(item_id)),
    ).fetchone()
    return dict(row) if row else None


def list_rescue_catalog_sources(
    db: Database,
    item_type: str,
    item_id: int,
) -> list[dict[str, Any]]:
    normalized_type = db._validate_rescue_item_type(item_type)
    rows = db.conn.execute(
        """
        SELECT source_kind, source_type, source_key,
               source_user_id, source_user_name
        FROM rescue_catalog_sources
        WHERE item_type = ? AND item_id = ?
        """,
        (normalized_type, int(item_id)),
    ).fetchall()
    result = [dict(row) for row in rows]
    result.sort(key=db._source_sort_key)
    return result

from __future__ import annotations

import json
import logging

from ..auth import PixivAuthManager
from ..settings import Settings
from ..storage_db import Database
from ..storage_files import FileStorage
from ..sync_engine import BookmarkNovelSyncService

logger = logging.getLogger(__name__)


def run_bookmark_sync(settings: Settings) -> None:
    auth = PixivAuthManager(settings.pixiv)
    api, auth_result = auth.login()
    if auth_result.user_id is None:
        raise RuntimeError("Unable to determine PIXIV_USER_ID. Set PIXIV_USER_ID in .env.")

    db = Database(settings.storage.db_path)
    db.init_schema()
    storage = FileStorage(settings)
    storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])

    try:
        service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)
        stats = service.sync(
            user_id=auth_result.user_id,
            restricts=settings.sync.bookmark_restricts,
            download_assets=settings.sync.download_assets,
            write_markdown=settings.sync.write_markdown,
            write_raw_text=settings.sync.write_raw_text,
        )
        logger.info("Bookmark sync finished: %s", json.dumps(stats, ensure_ascii=False))
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        db.close()

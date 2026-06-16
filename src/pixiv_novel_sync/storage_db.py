from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import AssetRecord, NovelRecord, NovelTextRecord, SourceRecord, UserRecord
from .storage.connection import DatabaseConnection
from .storage.schema import SchemaMixin
from .storage.utils import _LazyNovelMembership
from .storage.novels import NovelsMixin
from .storage.users import UsersMixin
from .storage.series import SeriesMixin
from .storage.bookmarks import BookmarksMixin
from .storage.tasks import TasksMixin
from .storage.pending_and_watermarks import PendingAndWatermarksMixin
from .storage.reading_progress import ReadingProgressMixin
from .storage.recommendations import RecommendationsMixin
from .storage.ai.core import AiCoreMixin
from .storage.ai.documents import AiDocumentsMixin
from .storage.ai.writing import AiWritingMixin


class Database(
    NovelsMixin,
    UsersMixin,
    SeriesMixin,
    BookmarksMixin,
    TasksMixin,
    PendingAndWatermarksMixin,
    ReadingProgressMixin,
    RecommendationsMixin,
    AiCoreMixin,
    AiDocumentsMixin,
    AiWritingMixin,
    SchemaMixin,
    DatabaseConnection,
):
    def __init__(self, path: Path) -> None:
        super().__init__(path)


    def export_stats(self) -> str:
        row = self.conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM users) AS users_count, "
            "(SELECT COUNT(*) FROM novels) AS novels_count, "
            "(SELECT COUNT(*) FROM series) AS series_count, "
            "(SELECT COUNT(*) FROM pending_deletions WHERE status = 'pending') AS pending_count"
        ).fetchone()
        return json.dumps(dict(row), ensure_ascii=False)

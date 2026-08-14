from __future__ import annotations

import json
from pathlib import Path

from .storage.connection import DatabaseConnection
from .storage.schema import SchemaMixin
from .storage.novels import NovelsMixin
from .storage.users import UsersMixin
from .storage.series import SeriesMixin
from .storage.bookmarks import BookmarksMixin
from .storage.tasks import TasksMixin
from .storage.pending_and_watermarks import PendingAndWatermarksMixin
from .storage.reading_progress import ReadingProgressMixin
from .storage.recommendations import RecommendationsMixin
from .storage.rescue import RescueMixin
from .storage.ai.core import AiCoreMixin
from .storage.ai.documents import AiDocumentsMixin
from .storage.ai.writing import AiWritingMixin
from .storage.ai.catalog import CatalogMixin
from .storage.ai.model_sync import ModelSyncStorageMixin
from .storage.ai.pools import PoolsMixin
from .storage.ai.adult import AdultStorageMixin


class Database(
    NovelsMixin,
    UsersMixin,
    SeriesMixin,
    BookmarksMixin,
    TasksMixin,
    PendingAndWatermarksMixin,
    ReadingProgressMixin,
    RecommendationsMixin,
    RescueMixin,
    AiCoreMixin,
    AiDocumentsMixin,
    AiWritingMixin,
    CatalogMixin,
    ModelSyncStorageMixin,
    PoolsMixin,
    AdultStorageMixin,
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

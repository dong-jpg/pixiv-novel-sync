from __future__ import annotations

import json
import threading
from pathlib import Path

from typing import Any

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
from .storage.ai.catalog import CatalogMixin
from .storage.ai.model_sync import ModelSyncStorageMixin
from .storage.ai.pools import PoolsMixin


def prepare_schema(db: Any) -> Any:
    """初始化 schema。

    真实 ``Database`` 走按路径缓存的 ``ensure_schema``。测试替身往往只实现
    ``init_schema``，这里回退到它，避免每个替身都再抄一份缓存。
    """
    ensure = getattr(db, "ensure_schema", None)
    if callable(ensure):
        ensure()
    else:
        db.init_schema()
    return db


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
    CatalogMixin,
    ModelSyncStorageMixin,
    PoolsMixin,
    SchemaMixin,
    DatabaseConnection,
):
    # 记录已初始化 schema 的 db_path，避免每次打开连接都重跑全部迁移。
    # 用绝对路径字符串作键：同进程内多个 Database 指向同一文件时共享这份记忆
    # （参考 ai/services/core.py:_initialized_paths）。
    _schema_ready_paths: set[str] = set()
    _schema_ready_lock = threading.Lock()

    def __init__(self, path: Path) -> None:
        super().__init__(path)

    def _schema_cache_key(self) -> str | None:
        """文件库按绝对路径去重。内存库每次都是独立连接，不能共用缓存。"""
        raw = str(self.path)
        if raw == ":memory:" or raw.startswith("file::memory:"):
            return None
        return str(self.path.resolve())

    def ensure_schema(self) -> "Database":
        """按 db_path 只初始化一次 schema，后续打开直接跳过迁移。

        init_schema() 对存量大库要跑十几个 _migrate_* / foreign_key_check，
        单次几十毫秒且持写锁。每个 Web 请求都重跑既拖慢 _open_database，
        又在另一线程持 BEGIN IMMEDIATE 时把页面请求堵在写锁上。schema 是
        进程内不变量，记住已处理过的路径即可。
        """
        key = self._schema_cache_key()
        if key is None:
            self.init_schema()
            return self
        with Database._schema_ready_lock:
            if key in Database._schema_ready_paths:
                return self
        self.init_schema()
        with Database._schema_ready_lock:
            Database._schema_ready_paths.add(key)
        return self

    @classmethod
    def _reset_schema_ready_cache(cls) -> None:
        """测试钩子：清空进程级 schema 记忆，避免跨库/跨 tmp 目录串台。"""
        with cls._schema_ready_lock:
            cls._schema_ready_paths.clear()

    def export_stats(self) -> str:
        row = self.conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM users) AS users_count, "
            "(SELECT COUNT(*) FROM novels) AS novels_count, "
            "(SELECT COUNT(*) FROM series) AS series_count, "
            "(SELECT COUNT(*) FROM pending_deletions WHERE status = 'pending') AS pending_count"
        ).fetchone()
        return json.dumps(dict(row), ensure_ascii=False)

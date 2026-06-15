"""Storage layer modularization."""
from __future__ import annotations

# 临时 stub：完整拆分后会替换为完整的 Database facade
# 当前阶段仅导出工具类供测试

from .connection import DatabaseConnection
from .utils import _LazyNovelMembership

__all__ = [
    "DatabaseConnection",
    "_LazyNovelMembership",
]

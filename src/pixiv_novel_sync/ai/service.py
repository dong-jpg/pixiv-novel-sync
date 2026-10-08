from __future__ import annotations

from .providers import create_provider  # noqa: F401 - 供 core 经 service_facade 调用，tests monkeypatch
from .services import (
    AIAdminMixin,
    AIConflictError,
    AIKeywordCleanMixin,
    AINotFoundError,
    AIServiceCore,
    AIServiceError,
    RouteJobContext,
    RouteResumeSpec,
)


class AIWritingService(
    AIKeywordCleanMixin,
    AIAdminMixin,
    AIServiceCore,
):
    """AI 服务门面。main 分支上只承载偏好关键词清洗与模型/Agent 管理；
    完整的写作 / 向导 / 蒸馏 / 成人润色能力在 ai-writing 分支。"""

    pass


__all__ = [
    "create_provider",
    "AIWritingService",
    "AIServiceError",
    "AIConflictError",
    "AINotFoundError",
    "RouteJobContext",
    "RouteResumeSpec",
]

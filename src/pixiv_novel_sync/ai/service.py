from __future__ import annotations

from .providers import create_provider  # noqa: F401 - 供 core 经 service_facade 调用，tests monkeypatch
from .services import (
    AIAdultPolishMixin,
    AIAdminMixin,
    AIChatWizardMixin,
    AIConflictError,
    AIGenerationMixin,
    AINotFoundError,
    AIProjectsMixin,
    AIServiceCore,
    AIServiceError,
    AdultRouteRequest,
    PreparedAdultJob,
    RouteJobContext,
    RouteResumeSpec,
)


class AIWritingService(
    AIAdultPolishMixin,
    AIChatWizardMixin,
    AIProjectsMixin,
    AIGenerationMixin,
    AIAdminMixin,
    AIServiceCore,
):
    pass


__all__ = [
    "AIWritingService",
    "AIServiceError",
    "AIConflictError",
    "AINotFoundError",
    "RouteJobContext",
    "AdultRouteRequest",
    "PreparedAdultJob",
    "RouteResumeSpec",
]

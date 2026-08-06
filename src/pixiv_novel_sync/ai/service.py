from __future__ import annotations

from .providers import create_provider
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

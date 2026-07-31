from __future__ import annotations

from .providers import create_provider
from .services import (
    AIAdminMixin,
    AIChatWizardMixin,
    AIGenerationMixin,
    AIProjectsMixin,
    AINotFoundError,
    AIConflictError,
    AIServiceCore,
    AIServiceError,
    RouteJobContext,
    RouteResumeSpec,
)


class AIWritingService(
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
    "RouteResumeSpec",
]

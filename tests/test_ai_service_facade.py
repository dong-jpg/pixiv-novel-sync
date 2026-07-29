from pixiv_novel_sync.ai import service as service_facade
from pixiv_novel_sync.ai.service import AIServiceError, AIWritingService


def test_ai_service_facade_exports_public_api() -> None:
    assert AIWritingService.__name__ == "AIWritingService"
    assert issubclass(AIServiceError, RuntimeError)


def test_ai_service_facade_exports_conflict_error() -> None:
    assert issubclass(service_facade.AIConflictError, AIServiceError)
    assert "AIConflictError" in service_facade.__all__

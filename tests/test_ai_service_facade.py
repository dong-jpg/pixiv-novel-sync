from pixiv_novel_sync.ai.service import AIServiceError, AIWritingService


def test_ai_service_facade_exports_public_api() -> None:
    assert AIWritingService.__name__ == "AIWritingService"
    assert issubclass(AIServiceError, RuntimeError)

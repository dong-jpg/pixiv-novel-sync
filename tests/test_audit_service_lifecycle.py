from pathlib import Path

import pytest

from pixiv_novel_sync.ai.models import AIProviderConfig
from pixiv_novel_sync.ai.service import AIServiceError, AIWritingService
from pixiv_novel_sync.ai.services import core


def test_schema_failure_closes_connection_before_propagating(monkeypatch, tmp_path):
    opened = []

    class FailingDatabase:
        def __init__(self, path):
            self.closed = False
            opened.append(self)

        def ensure_schema(self):
            raise RuntimeError("migration failed")

        def close(self):
            self.closed = True

    monkeypatch.setattr(core, "Database", FailingDatabase)
    service = AIWritingService(tmp_path / "isolated.db")
    with pytest.raises(RuntimeError, match="migration failed"):
        service._db()
    assert len(opened) == 1 and opened[0].closed
    service.close()


def test_closed_service_cannot_reopen_provider_pool(monkeypatch, tmp_path: Path):
    created = []
    monkeypatch.setattr("pixiv_novel_sync.ai.service.create_provider", lambda config: created.append(config))
    service = AIWritingService(tmp_path / "isolated.db")
    service.close()
    config = AIProviderConfig(
        id=1, name="test", provider_type="openai_compatible", api_key="test-key",
        base_url="https://example.com", default_model="test-model",
    )
    with pytest.raises(AIServiceError, match="已关闭"):
        service._get_provider(config)
    assert created == []

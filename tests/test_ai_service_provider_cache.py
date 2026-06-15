from __future__ import annotations

from pathlib import Path

from pixiv_novel_sync.ai.models import AIProviderConfig
from pixiv_novel_sync.ai.service import AIWritingService


class FakeProvider:
    def __init__(self, config: AIProviderConfig) -> None:
        self.config = config
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeDB:
    def __init__(self) -> None:
        self.closed = False
        self.updated: list[tuple[int, dict]] = []
        self.deleted: list[int] = []

    def update_ai_provider(self, provider_id: int, data: dict) -> None:
        self.updated.append((provider_id, data))

    def delete_ai_provider(self, provider_id: int) -> None:
        self.deleted.append(provider_id)

    def close(self) -> None:
        self.closed = True


def make_config(provider_id: int = 1, api_key: str = "key") -> AIProviderConfig:
    return AIProviderConfig(
        id=provider_id,
        name="provider",
        provider_type="openai_compatible",
        base_url="https://example.com/v1",
        api_key=api_key,
        default_model="model-a",
        timeout_seconds=1,
        max_retries=2,
        stream_enabled=True,
    )


def test_get_provider_reuses_cached_provider(monkeypatch, tmp_path: Path) -> None:
    created: list[FakeProvider] = []

    def fake_create_provider(config: AIProviderConfig) -> FakeProvider:
        provider = FakeProvider(config)
        created.append(provider)
        return provider

    monkeypatch.setattr("pixiv_novel_sync.ai.service.create_provider", fake_create_provider)
    service = AIWritingService(tmp_path / "test.db")

    first = service._get_provider(make_config())
    second = service._get_provider(make_config())

    assert first is second
    assert len(created) == 1
    assert not created[0].closed


def test_get_provider_closes_stale_provider_for_same_id(monkeypatch, tmp_path: Path) -> None:
    created: list[FakeProvider] = []

    def fake_create_provider(config: AIProviderConfig) -> FakeProvider:
        provider = FakeProvider(config)
        created.append(provider)
        return provider

    monkeypatch.setattr("pixiv_novel_sync.ai.service.create_provider", fake_create_provider)
    service = AIWritingService(tmp_path / "test.db")

    first = service._get_provider(make_config(api_key="old"))
    second = service._get_provider(make_config(api_key="new"))

    assert first is not second
    assert first.closed
    assert not second.closed
    assert len(created) == 2


def test_update_and_delete_provider_invalidate_cached_provider(monkeypatch, tmp_path: Path) -> None:
    fake_db = FakeDB()
    monkeypatch.setattr("pixiv_novel_sync.ai.service.create_provider", lambda config: FakeProvider(config))
    service = AIWritingService(tmp_path / "test.db")
    monkeypatch.setattr(service, "_db", lambda: fake_db)

    provider = service._get_provider(make_config(provider_id=7))
    service.update_provider(7, {"name": "renamed"})

    assert provider.closed
    assert fake_db.updated == [(7, {"name": "renamed"})]
    assert fake_db.closed

    fake_db.closed = False
    provider = service._get_provider(make_config(provider_id=7))
    service.delete_provider(7)

    assert provider.closed
    assert fake_db.deleted == [7]
    assert fake_db.closed


def test_close_closes_cached_providers_and_retriever(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("pixiv_novel_sync.ai.service.create_provider", lambda config: FakeProvider(config))
    service = AIWritingService(tmp_path / "test.db")
    first = service._get_provider(make_config(provider_id=1))
    second = service._get_provider(make_config(provider_id=2))
    retriever = FakeProvider(make_config(provider_id=3))
    service._retriever = retriever
    service._retriever_config_key = (None, None, "model", 60)

    service.close()

    assert first.closed
    assert second.closed
    assert retriever.closed
    assert service._provider_cache == {}
    assert service._provider_cache_by_id == {}
    assert service._retriever is None
    assert service._retriever_config_key is None

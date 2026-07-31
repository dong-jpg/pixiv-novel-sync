from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pixiv_novel_sync.ai.model_router import ModelRouter
from pixiv_novel_sync.ai.models import (
    AIAgentConfig,
    AIProviderConfig,
    AIStreamChunk,
)
from pixiv_novel_sync.ai.providers import AIProvider, AIProviderError
from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.storage_db import Database


def normal_done() -> AIStreamChunk:
    return AIStreamChunk(type="done", data={"finish_reason": "stop"})


class QueuedProvider(AIProvider):
    def __init__(
        self,
        config: AIProviderConfig,
        registry: "ProviderRegistry",
    ) -> None:
        super().__init__(config)
        self.registry = registry

    def stream_generate(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
        *,
        request_guard=None,
        is_cancelled=None,
    ) -> Iterator[AIStreamChunk]:
        del messages, temperature, top_p, max_tokens
        if is_cancelled is not None and is_cancelled():
            raise AIProviderError(
                "请求已取消",
                category="cancelled",
                scope="model",
                finish_reason="cancelled",
            )
        if request_guard is not None:
            request_guard()
        self.registry.calls.append((self.config.name, model))
        queue = self.registry.responses.get((self.config.name, model))
        if not queue:
            raise AIProviderError(
                "未配置测试响应",
                category="test_missing_response",
                scope="model",
            )
        events = queue.popleft()
        for event in events:
            if isinstance(event, BaseException):
                raise event
            yield event


class ProviderRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.responses: dict[
            tuple[str, str],
            deque[list[AIStreamChunk | BaseException]],
        ] = {}
        self.providers: dict[int, QueuedProvider] = {}

    def queue(
        self,
        provider_name: str,
        model: str,
        events: list[AIStreamChunk | BaseException],
    ) -> None:
        self.responses.setdefault(
            (provider_name, model),
            deque(),
        ).append(list(events))

    def get_provider(self, config: AIProviderConfig) -> AIProvider:
        provider = self.providers.get(config.id)
        if provider is None:
            provider = QueuedProvider(config, self)
            self.providers[config.id] = provider
        return provider

    def close(self) -> None:
        for provider in self.providers.values():
            provider.close()


class BatchSetup:
    def __init__(
        self,
        db: Database,
        service: AIWritingService,
        registry: ProviderRegistry,
        agent: AIAgentConfig,
    ) -> None:
        self.db = db
        self.service = service
        self.registry = registry
        self.agent = agent


def _provider_config(db: Database, provider_id: int) -> AIProviderConfig:
    row = db.get_ai_provider(provider_id, include_secret=True)
    assert row is not None
    return AIProviderConfig(
        id=int(row["id"]),
        name=str(row["name"]),
        provider_type=str(row["provider_type"]),
        base_url=row.get("base_url"),
        api_key="test-key",
        default_model=row.get("default_model"),
        timeout_seconds=int(row.get("timeout_seconds") or 120),
        max_retries=int(row.get("max_retries") or 2),
        proxy=row.get("proxy"),
        context_window=int(row.get("context_window") or 16000),
        stream_enabled=bool(row.get("stream_enabled", 1)),
        enabled=bool(row.get("enabled")),
    )


@pytest.fixture
def batch_setup(tmp_path: Path) -> Iterator[BatchSetup]:
    db = Database(tmp_path / "multi-batch.db")
    db.init_schema()
    model_ids: list[int] = []
    for name, model in (("p1", "m1"), ("p2", "m2")):
        provider_id = db.create_ai_provider(
            {
                "name": name,
                "provider_type": "openai_compatible",
                "base_url": f"https://{name}.example.test/v1",
                "api_key_encrypted": "ciphertext",
                "default_model": model,
                "context_window": 16000,
                "enabled": True,
            }
        )
        model_ids.append(
            db.create_ai_provider_model(
                {
                    "provider_id": provider_id,
                    "model_key": model,
                    "manual_context_window": 16000,
                }
            )
        )
    pool_id = db.create_ai_model_pool(
        {"name": "multi-batch", "pool_kind": "custom"}
    )
    version = db.replace_ai_model_pool_members(
        pool_id,
        [
            {"provider_model_id": model_ids[0], "enabled": True},
            {"provider_model_id": model_ids[1], "enabled": True},
        ],
        expected_version=1,
    )
    db.update_ai_model_pool(
        pool_id,
        {"enabled": True},
        expected_version=version,
    )
    agent_id = db.create_ai_agent(
        {
            "name": "多批次 Agent",
            "task_type": "distill_style",
            "binding_type": "pool",
            "provider_id": None,
            "model": None,
            "model_pool_id": pool_id,
            "system_prompt": "提炼风格",
            "max_tokens": 2000,
            "context_window": 16000,
            "enabled": True,
        }
    )
    registry = ProviderRegistry()

    def db_factory() -> Database:
        return Database(db.path)

    def load_provider_config(
        database: Database,
        provider_id: int,
    ) -> AIProviderConfig:
        return _provider_config(database, provider_id)

    router = ModelRouter(
        db_factory,
        load_provider_config,
        registry.get_provider,
    )
    service = AIWritingService(db.path)
    service.model_router.close()
    service.model_router = router
    setup = BatchSetup(
        db,
        service,
        registry,
        service._load_agent_config(db, agent_id),
    )
    try:
        yield setup
    finally:
        service.close()
        registry.close()
        db.close()


def _payload(agent_id: int) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "source_type": "manual",
        "text": "长文" * 1000,
        "full_text": True,
        "chunk_chars": 1000,
        "batch_size": 1,
    }


def _job_id(chunks: list[AIStreamChunk]) -> str:
    metadata = next(chunk for chunk in chunks if chunk.type == "metadata")
    return str((metadata.data or {})["job_id"])


def _delta_text(chunks: list[AIStreamChunk]) -> str:
    return "".join(chunk.text for chunk in chunks if chunk.type == "delta")


def test_distill_batches_pin_first_successful_main_candidate(
    batch_setup: BatchSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pixiv_novel_sync.ai.services.generation.time.sleep",
        lambda _seconds: None,
    )
    batch_setup.registry.queue(
        "p1",
        "m1",
        [AIStreamChunk(type="delta", text="第一批"), normal_done()],
    )
    batch_setup.registry.queue(
        "p1",
        "m1",
        [AIStreamChunk(type="delta", text="第二批"), normal_done()],
    )

    chunks = list(
        batch_setup.service.stream_distill_style(
            _payload(batch_setup.agent.id)
        )
    )

    assert batch_setup.registry.calls == [("p1", "m1"), ("p1", "m1")]
    assert _delta_text(chunks) == "第二批"
    batch_progress = [
        chunk
        for chunk in chunks
        if chunk.type == "progress"
        and (chunk.data or {}).get("phase") == "batch"
    ]
    assert [chunk.data["batch"] for chunk in batch_progress] == [1, 2]
    job = batch_setup.db.get_ai_job(_job_id(chunks))
    assert job["pinned_candidate_index"] == 0
    assert job["status"] == "succeeded"
    assert job["output_text"] == "第二批"


def test_later_batch_failure_is_partial_without_fallback(
    batch_setup: BatchSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pixiv_novel_sync.ai.services.generation.time.sleep",
        lambda _seconds: None,
    )
    batch_setup.registry.queue(
        "p1",
        "m1",
        [AIStreamChunk(type="delta", text="第一批"), normal_done()],
    )
    batch_setup.registry.queue(
        "p1",
        "m1",
        [
            AIStreamChunk(type="delta", text="半截"),
            AIProviderError(
                "第二批中断",
                category="transport_error",
                scope="model",
            ),
        ],
    )
    batch_setup.registry.queue(
        "p2",
        "m2",
        [AIStreamChunk(type="delta", text="不应调用"), normal_done()],
    )

    chunks = list(
        batch_setup.service.stream_distill_style(
            _payload(batch_setup.agent.id)
        )
    )

    assert batch_setup.registry.calls == [("p1", "m1"), ("p1", "m1")]
    assert "不应调用" not in _delta_text(chunks)
    assert chunks[-1].type == "error"
    job = batch_setup.db.get_ai_job(_job_id(chunks))
    assert job["status"] == "partial"
    assert job["output_text"] == "半截"

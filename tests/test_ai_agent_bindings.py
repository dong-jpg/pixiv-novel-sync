from __future__ import annotations

from pathlib import Path

import pytest

from pixiv_novel_sync.ai.models import AIAgentConfig
from pixiv_novel_sync.ai.service import (
    AIConflictError,
    AIServiceError,
    AIWritingService,
)
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "agent-bindings.db")
    database.init_schema()
    try:
        yield database
    finally:
        database.close()


def seed_provider(db: Database, *, name: str = "provider") -> int:
    return db.create_ai_provider(
        {
            "name": name,
            "provider_type": "openai_compatible",
            "enabled": True,
        }
    )


@pytest.fixture
def service(db: Database) -> AIWritingService:
    return AIWritingService(db.path)


def seed_enabled_pool(db: Database) -> tuple[int, int]:
    provider_id = seed_provider(db, name="pool-provider")
    model_id = db.create_ai_provider_model(
        {
            "provider_id": provider_id,
            "model_key": "pool-model",
            "manual_capabilities": ["json", "streaming"],
            "enabled": True,
        }
    )
    pool_id = db.create_ai_model_pool(
        {"name": "可用池", "pool_kind": "custom"}
    )
    version = db.replace_ai_model_pool_members(
        pool_id,
        [{"provider_model_id": model_id, "enabled": True}],
        expected_version=1,
    )
    db.update_ai_model_pool(
        pool_id,
        {"enabled": True},
        expected_version=version,
    )
    return pool_id, provider_id


def test_agent_config_keeps_fixed_constructor_compatible() -> None:
    agent = AIAgentConfig(
        id=7,
        name="旧 Agent",
        task_type="continue",
        provider_id=3,
        model="legacy-model",
        system_prompt="prompt",
    )

    assert agent.provider_id == 3
    assert agent.binding_type == "fixed"
    assert agent.model_pool_id is None
    assert agent.required_capabilities == ()
    assert agent.binding_version == 1


def test_fixed_agent_storage_returns_binding_compatibility_fields(db: Database) -> None:
    provider_id = seed_provider(db)
    agent_id = db.create_ai_agent(
        {
            "name": "固定 Agent",
            "task_type": "continue",
            "provider_id": provider_id,
            "model": "manual-model",
            "system_prompt": "prompt",
        }
    )

    row = db.get_ai_agent(agent_id)
    assert row["binding_type"] == "fixed"
    assert row["provider_id"] == provider_id
    assert row["model"] == "manual-model"
    assert row["model_pool_id"] is None
    assert row["model_pool_name"] is None
    assert row["required_capabilities"] == []
    assert row["binding_version"] == 1
    assert row["binding_summary"] == "固定：provider / manual-model"


def test_pool_agent_cannot_submit_provider_or_model(
    service: AIWritingService,
    db: Database,
) -> None:
    pool_id, provider_id = seed_enabled_pool(db)

    with pytest.raises(
        AIServiceError,
        match="固定模型和模型池不能同时提交",
    ):
        service.create_agent(
            {
                "name": "混合 Agent",
                "task_type": "general",
                "binding_type": "pool",
                "provider_id": provider_id,
                "model": "pool-model",
                "model_pool_id": pool_id,
                "system_prompt": "prompt",
            }
        )


def test_fixed_agent_with_required_capability_needs_catalog_model(
    service: AIWritingService,
    db: Database,
) -> None:
    provider_id = seed_provider(db)

    with pytest.raises(AIServiceError, match="能力"):
        service.create_agent(
            {
                "name": "JSON Agent",
                "task_type": "general",
                "provider_id": provider_id,
                "model": "unknown-model",
                "required_capabilities": ["json"],
                "system_prompt": "prompt",
            }
        )


def test_fixed_agent_without_capabilities_keeps_manual_model_compatibility(
    service: AIWritingService,
    db: Database,
) -> None:
    provider_id = seed_provider(db)

    agent_id = service.create_agent(
        {
            "name": "手填模型 Agent",
            "task_type": "general",
            "provider_id": provider_id,
            "model": "not-in-catalog",
            "system_prompt": "prompt",
        }
    )

    assert db.get_ai_agent(agent_id)["model"] == "not-in-catalog"


@pytest.mark.parametrize(
    ("capabilities", "message"),
    [
        (["json", "json"], "重复"),
        (["unknown"], "未知"),
        (["json"] * 33, "重复"),
    ],
)
def test_agent_rejects_invalid_required_capabilities(
    service: AIWritingService,
    db: Database,
    capabilities: list[str],
    message: str,
) -> None:
    provider_id = seed_provider(db)

    with pytest.raises(AIServiceError, match=message):
        service.create_agent(
            {
                "name": "非法能力 Agent",
                "task_type": "general",
                "provider_id": provider_id,
                "model": "manual-model",
                "required_capabilities": capabilities,
                "system_prompt": "prompt",
            }
        )


def test_fixed_agent_persists_capabilities_and_loads_binding_config(
    service: AIWritingService,
    db: Database,
) -> None:
    provider_id = seed_provider(db)
    db.create_ai_provider_model(
        {
            "provider_id": provider_id,
            "model_key": "structured-model",
            "manual_capabilities": ["streaming", "json"],
            "enabled": True,
        }
    )

    agent_id = service.create_agent(
        {
            "name": "结构化 Agent",
            "task_type": "general",
            "provider_id": provider_id,
            "model": "structured-model",
            "required_capabilities": ["streaming", "json"],
            "system_prompt": "prompt",
        }
    )

    row = db.get_ai_agent(agent_id)
    assert row["required_capabilities"] == ["json", "streaming"]
    config = service._load_agent_config(db, agent_id)
    assert config.binding_type == "fixed"
    assert config.model_pool_id is None
    assert config.required_capabilities == ("json", "streaming")
    assert config.binding_version == 1


def test_pool_agent_persists_nullable_fixed_fields_and_pool_summary(
    service: AIWritingService,
    db: Database,
) -> None:
    pool_id, _provider_id = seed_enabled_pool(db)

    agent_id = service.create_agent(
        {
            "name": "模型池 Agent",
            "task_type": "general",
            "binding_type": "pool",
            "model_pool_id": pool_id,
            "required_capabilities": ["json"],
            "system_prompt": "prompt",
        }
    )

    row = db.get_ai_agent(agent_id)
    assert row["binding_type"] == "pool"
    assert row["provider_id"] is None
    assert row["model"] is None
    assert row["model_pool_id"] == pool_id
    assert row["model_pool_name"] == "可用池"
    assert row["required_capabilities"] == ["json"]
    assert row["binding_summary"] == "模型池：可用池"
    config = service._load_agent_config(db, agent_id)
    assert config.provider_id is None
    assert config.binding_type == "pool"
    assert config.model_pool_id == pool_id


def test_pool_agent_requires_a_candidate_with_all_capabilities(
    service: AIWritingService,
    db: Database,
) -> None:
    pool_id, _provider_id = seed_enabled_pool(db)

    with pytest.raises(AIServiceError, match="能力"):
        service.create_agent(
            {
                "name": "视觉 Agent",
                "task_type": "general",
                "binding_type": "pool",
                "model_pool_id": pool_id,
                "required_capabilities": ["vision"],
                "system_prompt": "prompt",
            }
        )


def test_pool_agent_rejects_disabled_pool(
    service: AIWritingService,
    db: Database,
) -> None:
    pool_id, _provider_id = seed_enabled_pool(db)
    pool = db.get_ai_model_pool(pool_id)
    db.update_ai_model_pool(
        pool_id,
        {"enabled": False},
        expected_version=pool["version"],
    )

    with pytest.raises(AIServiceError, match="必须启用"):
        service.create_agent(
            {
                "name": "禁用池 Agent",
                "task_type": "general",
                "binding_type": "pool",
                "model_pool_id": pool_id,
                "system_prompt": "prompt",
            }
        )


def test_legacy_capabilities_json_uses_canonical_agent_validation(
    service: AIWritingService,
    db: Database,
) -> None:
    provider_id = seed_provider(db)
    db.create_ai_provider_model(
        {
            "provider_id": provider_id,
            "model_key": "json-model",
            "manual_capabilities": ["json"],
        }
    )

    agent_id = service.create_agent(
        {
            "name": "旧表单 Agent",
            "task_type": "general",
            "provider_id": provider_id,
            "model": "json-model",
            "required_capabilities_json": '["json"]',
            "system_prompt": "prompt",
        }
    )

    assert db.get_ai_agent(agent_id)["required_capabilities"] == ["json"]


def test_agent_update_switches_binding_atomically_and_increments_version(
    service: AIWritingService,
    db: Database,
) -> None:
    fixed_provider_id = seed_provider(db, name="fixed-provider")
    agent_id = service.create_agent(
        {
            "name": "可切换 Agent",
            "task_type": "general",
            "provider_id": fixed_provider_id,
            "model": "manual-model",
            "system_prompt": "prompt",
        }
    )
    pool_id, _pool_provider_id = seed_enabled_pool(db)

    service.update_agent(
        agent_id,
        {
            "binding_type": "pool",
            "model_pool_id": pool_id,
            "required_capabilities": ["json"],
        },
    )

    row = db.get_ai_agent(agent_id)
    assert row["binding_type"] == "pool"
    assert row["provider_id"] is None
    assert row["model"] is None
    assert row["model_pool_id"] == pool_id
    assert row["required_capabilities"] == ["json"]
    assert row["binding_version"] == 2


def test_pool_agent_partial_update_uses_existing_binding_type(
    service: AIWritingService,
    db: Database,
) -> None:
    pool_id, _provider_id = seed_enabled_pool(db)
    agent_id = service.create_agent(
        {
            "name": "局部更新 Agent",
            "task_type": "general",
            "binding_type": "pool",
            "model_pool_id": pool_id,
            "system_prompt": "prompt",
        }
    )

    service.update_agent(agent_id, {"model_pool_id": pool_id})

    row = db.get_ai_agent(agent_id)
    assert row["binding_type"] == "pool"
    assert row["model_pool_id"] == pool_id
    assert row["binding_version"] == 2


def test_invalid_binding_update_rolls_back_without_incrementing_version(
    service: AIWritingService,
    db: Database,
) -> None:
    provider_id = seed_provider(db, name="fixed-provider")
    agent_id = service.create_agent(
        {
            "name": "回滚 Agent",
            "task_type": "general",
            "provider_id": provider_id,
            "model": "manual-model",
            "system_prompt": "prompt",
        }
    )
    pool_id, _pool_provider_id = seed_enabled_pool(db)

    with pytest.raises(AIServiceError, match="能力"):
        service.update_agent(
            agent_id,
            {
                "binding_type": "pool",
                "model_pool_id": pool_id,
                "required_capabilities": ["vision"],
            },
        )

    row = db.get_ai_agent(agent_id)
    assert row["binding_type"] == "fixed"
    assert row["provider_id"] == provider_id
    assert row["model_pool_id"] is None
    assert row["binding_version"] == 1


def test_delete_provider_reports_fixed_agent_reference_as_conflict(
    service: AIWritingService,
    db: Database,
) -> None:
    provider_id = seed_provider(db)
    service.create_agent(
        {
            "name": "引用 Agent",
            "task_type": "general",
            "provider_id": provider_id,
            "model": "manual-model",
            "system_prompt": "prompt",
        }
    )

    with pytest.raises(AIConflictError, match="引用"):
        service.delete_provider(provider_id)

    assert db.get_ai_provider(provider_id) is not None


def test_delete_provider_reports_catalog_reference_as_conflict(
    service: AIWritingService,
    db: Database,
) -> None:
    provider_id = seed_provider(db)
    db.create_ai_provider_model(
        {"provider_id": provider_id, "model_key": "catalog-model"}
    )

    with pytest.raises(AIConflictError, match="模型目录"):
        service.delete_provider(provider_id)

    assert db.get_ai_provider(provider_id) is not None


def test_delete_provider_reports_pool_member_reference_as_conflict(
    service: AIWritingService,
    db: Database,
) -> None:
    provider_id = seed_provider(db)
    model_id = db.create_ai_provider_model(
        {"provider_id": provider_id, "model_key": "pooled-model"}
    )
    pool_id = db.create_ai_model_pool(
        {"name": "引用池", "pool_kind": "custom"}
    )
    db.replace_ai_model_pool_members(
        pool_id,
        [{"provider_model_id": model_id, "enabled": True}],
        expected_version=1,
    )

    with pytest.raises(AIConflictError, match="模型池"):
        service.delete_provider(provider_id)

    assert db.get_ai_provider(provider_id) is not None

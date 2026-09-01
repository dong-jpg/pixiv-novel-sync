"""Agent 候选模型链预览（只读解析，绝不发起真实生成请求）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from pixiv_novel_sync.ai.service import AIServiceError, AIWritingService
from pixiv_novel_sync.ai_web import register_ai_routes
from pixiv_novel_sync.settings import (
    PixivSettings,
    Settings,
    StorageSettings,
    SyncSettings,
)
from pixiv_novel_sync.storage_db import Database


# Provider 的 api_key 是加密存储的，这里塞一个能被 grep 的哨兵密文，
# 用来断言候选链响应不会把 Provider 机密带出去。
SECRET_CIPHER = "cipher-must-never-leak"


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        pixiv=PixivSettings(
            refresh_token="",
            access_token=None,
            proxy=None,
            timeout=30,
            verify_ssl=True,
            user_id=None,
        ),
        sync=SyncSettings(
            enabled=True,
            initial_manual_only=False,
            download_assets=False,
            write_markdown=True,
            write_raw_text=True,
            bookmark_restricts=["public"],
            max_items_per_run=None,
            max_pages_per_run=None,
            delay_seconds_between_items=0,
            delay_seconds_between_pages=0,
        ),
        storage=StorageSettings(
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
            db_path=tmp_path / "agent-candidates.db",
        ),
    )


@pytest.fixture
def service(tmp_path: Path):
    instance = AIWritingService(tmp_path / "agent-candidates.db")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def db(service: AIWritingService):
    database = service._db()
    try:
        yield database
    finally:
        database.close()


def seed_provider(
    db: Database,
    name: str,
    *,
    default_model: str | None = None,
    context_window: int = 32_000,
) -> int:
    return db.create_ai_provider(
        {
            "name": name,
            "provider_type": "openai_compatible",
            "base_url": f"https://{name}.example.test/v1",
            "api_key_encrypted": SECRET_CIPHER,
            "default_model": default_model,
            "context_window": context_window,
            "enabled": True,
        }
    )


def seed_fixed_agent(db: Database) -> int:
    provider_id = seed_provider(db, "fixed-provider", default_model="provider-default")
    return db.create_ai_agent(
        {
            "name": "章节续写",
            "task_type": "continue",
            "binding_type": "fixed",
            "provider_id": provider_id,
            "model": "test-model",
            "system_prompt": "写作助手",
        }
    )


def seed_pool_agent(db: Database) -> dict[str, Any]:
    """两级模型池：主池 2 个模型 + 后备池 1 个模型，用来验证展开顺序。"""

    primary_provider = seed_provider(db, "pool-primary", context_window=128_000)
    backup_provider = seed_provider(db, "pool-backup", context_window=128_000)
    model_a = db.create_ai_provider_model(
        {
            "provider_id": primary_provider,
            "model_key": "pool-model-a",
            "manual_capabilities": ["streaming"],
            "manual_context_window": 64_000,
            "enabled": True,
        }
    )
    model_b = db.create_ai_provider_model(
        {
            "provider_id": primary_provider,
            "model_key": "pool-model-b",
            "manual_capabilities": ["streaming"],
            "enabled": True,
        }
    )
    model_c = db.create_ai_provider_model(
        {
            "provider_id": backup_provider,
            "model_key": "pool-model-c",
            "manual_capabilities": ["streaming"],
            "enabled": True,
        }
    )

    fallback_id = db.create_ai_model_pool({"name": "后备池", "pool_kind": "secondary"})
    fallback_version = db.replace_ai_model_pool_members(
        fallback_id,
        [{"provider_model_id": model_c, "enabled": True}],
        expected_version=1,
    )
    db.update_ai_model_pool(
        fallback_id,
        {"enabled": True},
        expected_version=fallback_version,
    )

    primary_id = db.create_ai_model_pool(
        {
            "name": "主池",
            "pool_kind": "primary",
            "fallback_pool_id": fallback_id,
        }
    )
    primary_version = db.replace_ai_model_pool_members(
        primary_id,
        [
            {"provider_model_id": model_a, "enabled": True},
            {"provider_model_id": model_b, "enabled": True},
        ],
        expected_version=1,
    )
    db.update_ai_model_pool(
        primary_id,
        {"enabled": True},
        expected_version=primary_version,
    )

    agent_id = db.create_ai_agent(
        {
            "name": "模型池续写",
            "task_type": "continue",
            "binding_type": "pool",
            "model_pool_id": primary_id,
            "system_prompt": "写作助手",
        }
    )
    return {"agent_id": agent_id, "primary_id": primary_id, "fallback_id": fallback_id}


@pytest.fixture
def api(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = Flask(__name__)
    app.secret_key = "agent-candidates-test-secret"
    app.config["TESTING"] = True
    register_ai_routes(app, settings)
    database = Database(settings.storage.db_path)
    database.init_schema()
    try:
        yield app.test_client(), database
    finally:
        manager = app.extensions.get("pixiv_novel_sync.ai_service")
        if manager is not None:
            manager.close()
        database.close()


def test_preview_returns_candidate_chain_for_fixed_agent(
    service: AIWritingService,
    db: Database,
) -> None:
    agent_id = seed_fixed_agent(db)

    result = service.preview_agent_candidates(agent_id)

    assert result["agent_id"] == agent_id
    assert result["agent_name"] == "章节续写"
    assert result["binding_type"] == "fixed"
    assert result["pool_id"] is None
    assert len(result["candidates"]) == 1
    first = result["candidates"][0]
    assert first["order"] == 1
    assert first["model_key"] == "test-model"
    assert first["provider_name"] == "fixed-provider"
    assert first["source"] == "fixed"
    assert first["pool_id"] is None


def test_preview_exposes_router_hard_limits(
    service: AIWritingService,
    db: Database,
) -> None:
    """前端要能直接显示每个 job 的硬上限，不许自己抄一份数字。"""
    agent_id = seed_fixed_agent(db)

    limits = service.preview_agent_candidates(agent_id)["limits"]

    assert limits["max_candidate_attempts"] == 16
    assert limits["max_network_requests"] == 32
    assert limits["max_resolved_candidates"] == 64
    assert limits["max_pool_nodes"] == 8


def test_preview_expands_pool_chain_in_router_order(
    service: AIWritingService,
    db: Database,
) -> None:
    """池绑定要按主池成员顺序、再接后备池的顺序展开，来源标出具体池节点。"""
    seeded = seed_pool_agent(db)

    result = service.preview_agent_candidates(seeded["agent_id"])

    assert result["binding_type"] == "pool"
    assert result["pool_id"] == seeded["primary_id"]
    assert result["pool_name"] == "主池"
    assert [item["model_key"] for item in result["candidates"]] == [
        "pool-model-a",
        "pool-model-b",
        "pool-model-c",
    ]
    assert [item["order"] for item in result["candidates"]] == [1, 2, 3]
    assert [item["source"] for item in result["candidates"]] == ["主池", "主池", "后备池"]
    assert [item["pool_position"] for item in result["candidates"]] == [1, 2, 1]
    # 后备池比主池深一层，界面据此显示「第 N 级后备」
    assert [item["fallback_depth"] for item in result["candidates"]] == [0, 0, 1]
    assert result["candidates"][0]["capabilities"] == ["streaming"]
    # 有效上下文取 Provider 与模型的较小值（Provider 128k / 模型 64k）
    assert result["candidates"][0]["context_window"] == 64_000


def test_preview_never_calls_stream_generate(
    service: AIWritingService,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预览必须是纯解析：绝不能触发任何真实生成请求。"""
    agent_id = seed_fixed_agent(db)
    calls: list[Any] = []

    def _boom(*args: Any, **kwargs: Any):
        calls.append(args)
        raise AssertionError("预览不得发起真实生成请求")

    monkeypatch.setattr(
        "pixiv_novel_sync.ai.providers.OpenAICompatibleProvider.stream_generate",
        _boom,
        raising=True,
    )
    # 连 Provider 实例都不该被构造：构造即意味着解密 API key 并准备发请求。
    monkeypatch.setattr(
        AIWritingService,
        "_get_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("预览不得构造 Provider 实例")
        ),
    )

    result = service.preview_agent_candidates(agent_id)

    assert result["candidates"]
    assert calls == []


def test_preview_rejects_unknown_agent(service: AIWritingService) -> None:
    with pytest.raises(AIServiceError):
        service.preview_agent_candidates(999999)


def test_candidates_endpoint_returns_resolved_chain(api) -> None:
    client, database = api
    agent_id = seed_fixed_agent(database)

    response = client.get(f"/api/dashboard/ai/agents/{agent_id}/candidates")

    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["ok"] is True
    assert body["data"]["binding_type"] == "fixed"
    assert body["data"]["candidates"][0]["model_key"] == "test-model"
    assert body["data"]["limits"]["max_candidate_attempts"] == 16


def test_candidates_endpoint_never_leaks_provider_secrets(api) -> None:
    """token 响应会脱敏，候选链也必须一样克制：不带密文、base_url、配置哈希。"""
    client, database = api
    agent_id = seed_fixed_agent(database)

    response = client.get(f"/api/dashboard/ai/agents/{agent_id}/candidates")

    serialized = json.dumps(response.get_json(), ensure_ascii=False)
    assert SECRET_CIPHER not in serialized
    assert "api_key" not in serialized.lower()
    assert "base_url" not in serialized
    assert "provider_config_hash" not in serialized
    assert "example.test" not in serialized


def test_candidates_endpoint_rejects_unknown_agent(api) -> None:
    client, _database = api

    response = client.get("/api/dashboard/ai/agents/999999/candidates")

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


AGENTS_TEMPLATE = Path(
    "src/pixiv_novel_sync/templates/dashboard_settings_agents.html"
).read_text(encoding="utf-8")


def test_agents_template_actually_renders_the_candidate_chain() -> None:
    """端点早就就绪，缺的一直是渲染——占位说明不算接线。

    「这个 Agent 会依次调哪些模型」此前只能等任务跑完去 /dashboard/logs 事后看。
    """
    assert "将接入" not in AGENTS_TEMPLATE
    assert "window.aiApi.agentCandidates" in AGENTS_TEMPLATE
    for expr in (
        "candidateChain",
        "loadCandidateChain",
        "candidateChain.candidates",
        "candidate.provider_name",
        "candidate.model_key",
        "candidateSourceLabel",
        "candidateOrderMark",
    ):
        assert expr in AGENTS_TEMPLATE, expr


def test_candidate_chain_renders_router_limits_from_the_response() -> None:
    """上限必须来自响应的 limits，不许前端再抄一份数字。"""
    assert "candidateLimitText" in AGENTS_TEMPLATE
    for field in (
        "limits.max_candidate_attempts",
        "limits.max_network_requests",
        "limits.max_resolved_candidates",
        "limits.max_pool_nodes",
    ):
        assert field in AGENTS_TEMPLATE, field
    # 写死的 16 / 32 会在后端调整硬上限之后静默说谎
    assert "16 次候选尝试" not in AGENTS_TEMPLATE


def test_candidate_chain_hides_pool_block_for_fixed_binding() -> None:
    """生产实测 0 个模型池、16 个 Agent 全 fixed：不能显示空的池区块。"""
    assert 'v-if="candidateChain.pool_name"' in AGENTS_TEMPLATE
    assert "固定模型" in AGENTS_TEMPLATE


def test_candidate_chain_marks_fallback_pool_depth() -> None:
    """池绑定要看得出候选来自主池还是第几级后备池。"""
    assert "fallback_depth" in AGENTS_TEMPLATE
    assert "pool_position" in AGENTS_TEMPLATE
    assert "级后备池" in AGENTS_TEMPLATE


def test_candidate_chain_has_empty_and_error_states() -> None:
    """解析结果可能是空链（Provider/模型被停用），空白面板会被当成加载中。"""
    assert "candidateChainError" in AGENTS_TEMPLATE
    assert "candidateChainLoading" in AGENTS_TEMPLATE
    assert "候选链为空" in AGENTS_TEMPLATE


def test_candidate_chain_does_not_reach_for_provider_secrets() -> None:
    """端点已经过滤机密，前端也不许去别的端点补 base_url 或密钥。"""
    assert "base_url" not in AGENTS_TEMPLATE
    assert "api_key" not in AGENTS_TEMPLATE
    assert "provider_config_hash" not in AGENTS_TEMPLATE

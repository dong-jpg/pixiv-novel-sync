from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask

import pixiv_novel_sync.ai_web as ai_web
from pixiv_novel_sync.ai.service import AIConflictError, AIServiceError, AIWritingService
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "adult-admin.db")
    database.init_schema()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def service(db: Database):
    instance = AIWritingService(db.path)
    try:
        yield instance
    finally:
        instance.close()


def _seed_model(
    db: Database,
    *,
    capabilities: list[str],
    model_key: str = "json-model",
) -> tuple[int, int]:
    provider_id = db.create_ai_provider(
        {
            "name": f"provider-{model_key}",
            "provider_type": "openai_compatible",
            "default_model": model_key,
            "enabled": True,
        }
    )
    model_id = db.create_ai_provider_model(
        {
            "provider_id": provider_id,
            "model_key": model_key,
            "manual_capabilities": capabilities,
            "enabled": True,
        }
    )
    return provider_id, model_id


def _seed_pool(db: Database, model_id: int) -> int:
    pool_id = db.create_ai_model_pool({"name": "成人写作池", "pool_kind": "custom"})
    version = db.replace_ai_model_pool_members(
        pool_id,
        [{"provider_model_id": model_id, "enabled": True}],
        expected_version=1,
    )
    db.update_ai_model_pool(pool_id, {"enabled": True}, expected_version=version)
    return pool_id


@pytest.mark.parametrize("task_type", ["adult_safety_review", "adult_fact_guard"])
def test_normal_agent_crud_cannot_create_internal_review_agent(
    db,
    service,
    task_type,
):
    provider_id, _ = _seed_model(db, capabilities=[])

    with pytest.raises(AIServiceError, match="内部"):
        service.create_agent(
            {
                "name": "内部审查",
                "task_type": task_type,
                "provider_id": provider_id,
                "system_prompt": "试图修改策略",
            }
        )


@pytest.mark.parametrize(
    "field",
    [
        "policy_id",
        "policy_text",
        "output_schema",
        "safety_policy_hash",
        "validator_policy_hash",
        "binding_version",
    ],
)
def test_normal_agent_crud_rejects_policy_fields(db, service, field):
    provider_id, _ = _seed_model(db, capabilities=[])

    with pytest.raises(AIServiceError, match="策略|内部"):
        service.create_agent(
            {
                "name": "普通 Agent",
                "task_type": "general",
                "provider_id": provider_id,
                "system_prompt": "普通提示",
                field: "tampered",
            }
        )


def test_legacy_internal_agent_is_hidden_and_cannot_be_updated_or_deleted(db, service):
    provider_id, _ = _seed_model(db, capabilities=[])
    agent_id = db.create_ai_agent(
        {
            "name": "旧内部审查",
            "task_type": "adult_safety_review",
            "provider_id": provider_id,
            "system_prompt": "旧数据",
        }
    )

    assert all(row["id"] != agent_id for row in service.list_agents())
    with pytest.raises(AIServiceError, match="内部"):
        service.update_agent(agent_id, {"name": "试图修改"})
    with pytest.raises(AIServiceError, match="内部"):
        service.delete_agent(agent_id)
    assert db.get_ai_agent(agent_id) is not None


def test_adult_agent_template_has_no_provider_default(db, service):
    _provider_id, model_id = _seed_model(db, capabilities=[])
    pool_id = _seed_pool(db, model_id)

    result = service.ensure_adult_polish_agent(
        {
            "name": "成人描写润色",
            "binding_type": "pool",
            "model_pool_id": pool_id,
        }
    )

    assert result["task_type"] == "adult_polish"
    assert result["binding_type"] == "pool"
    assert result["model_pool_id"] == pool_id
    assert result["provider_id"] is None
    assert "Provider" not in result["system_prompt"]
    assert "替换片段" in result["system_prompt"]


def test_adult_agent_template_requires_explicit_binding(service):
    with pytest.raises(AIServiceError, match="绑定"):
        service.ensure_adult_polish_agent({"name": "成人描写润色"})


def test_review_binding_uses_expected_version_and_fixed_json_capability(db, service):
    provider_id, _ = _seed_model(db, capabilities=["json"])

    saved = service.update_adult_review_binding(
        "safety",
        {
            "binding_type": "fixed",
            "provider_id": provider_id,
            "model": "json-model",
            "enabled": True,
        },
        expected_version=1,
    )

    assert saved["enabled"] is True
    assert saved["required_capabilities"] == ["json"]
    assert saved["version"] == 2
    with pytest.raises(AIServiceError, match="409"):
        service.update_adult_review_binding(
            "safety",
            {"enabled": False},
            expected_version=1,
        )


def test_review_binding_invalid_candidate_is_left_disabled(db, service):
    provider_id, _ = _seed_model(
        db,
        capabilities=["streaming"],
        model_key="plain-model",
    )

    with pytest.raises(AIServiceError, match="json|能力|配置"):
        service.update_adult_review_binding(
            "fact_guard",
            {
                "binding_type": "fixed",
                "provider_id": provider_id,
                "model": "plain-model",
                "enabled": True,
            },
            expected_version=1,
        )

    binding = service.list_adult_review_bindings()["fact_guard"]
    assert binding["enabled"] is False
    assert binding["binding_type"] is None
    assert binding["required_capabilities"] == ["json"]


def test_review_binding_policy_state_mismatch_fails_closed(db, service):
    provider_id, _ = _seed_model(db, capabilities=["json"])
    db.conn.execute(
        "UPDATE ai_adult_policy_state SET policy_hash = ? WHERE policy_kind = 'safety'",
        ("0" * 64,),
    )
    db.conn.commit()

    with pytest.raises(AIServiceError, match="策略"):
        service.update_adult_review_binding(
            "safety",
            {
                "binding_type": "fixed",
                "provider_id": provider_id,
                "model": "json-model",
                "enabled": True,
            },
            expected_version=1,
        )

    assert service.list_adult_review_bindings()["safety"]["enabled"] is False


def test_review_binding_unexpected_validation_error_is_left_disabled(
    db,
    service,
    monkeypatch,
):
    provider_id, _ = _seed_model(db, capabilities=["json"])

    def fail_validation(_db, _data):
        raise RuntimeError("resolver crashed")

    monkeypatch.setattr(service, "_validate_agent_binding", fail_validation)

    with pytest.raises(AIServiceError, match="配置无效") as exc_info:
        service.update_adult_review_binding(
            "safety",
            {
                "binding_type": "fixed",
                "provider_id": provider_id,
                "model": "json-model",
                "enabled": True,
            },
            expected_version=1,
        )
    assert "resolver crashed" not in str(exc_info.value)

    binding = service.list_adult_review_bindings()["safety"]
    assert binding["enabled"] is False
    assert binding["binding_type"] is None
    assert binding["version"] == 2


class _RouteDatabase:
    def fail_stale_ai_jobs(self):
        return 0

    def close(self):
        pass


class _RouteService:
    def __init__(self, _db_path):
        self.calls = []

    def _db(self):
        return _RouteDatabase()

    def reconcile_model_sync_operations(self):
        return 0

    def close(self):
        pass

    def list_adult_review_bindings(self):
        self.calls.append(("list",))
        return {"safety": {"enabled": False, "version": 1}}

    def update_adult_review_binding(self, kind, payload, expected_version):
        self.calls.append(("update", kind, payload, expected_version))
        if expected_version != 1:
            raise AIConflictError("409: binding revision 已变化")
        return {"enabled": False, "version": 2}

    def ensure_adult_polish_agent(self, payload):
        self.calls.append(("seed", payload))
        return {"id": 7, "task_type": "adult_polish"}


def test_adult_admin_routes_forward_versioned_requests(monkeypatch, tmp_path):
    created = []

    def factory(path):
        instance = _RouteService(path)
        created.append(instance)
        return instance

    monkeypatch.setattr(ai_web, "AIWritingService", factory)
    app = Flask(__name__, template_folder=str(tmp_path))
    settings = type("Settings", (), {"storage": type("Storage", (), {"db_path": tmp_path / "x.db"})()})()
    ai_web.register_ai_routes(app, settings)
    client = app.test_client()

    listed = client.get("/api/dashboard/ai/adult-review-bindings/safety")
    updated = client.put(
        "/api/dashboard/ai/adult-review-bindings/safety",
        json={"enabled": False, "expected_version": 1},
    )
    seeded = client.post(
        "/api/dashboard/ai/agents/adult-polish/seed",
        json={"name": "成人描写润色", "binding_type": "pool", "model_pool_id": 8},
    )

    assert listed.status_code == 200
    assert updated.status_code == 200
    assert seeded.status_code == 200
    assert ("update", "safety", {"enabled": False}, 1) in created[0].calls


def test_settings_exposes_readonly_adult_review_bindings():
    html = Path("src/pixiv_novel_sync/templates/dashboard_settings.html").read_text(
        encoding="utf-8"
    )

    for text in (
        "adult_safety_policy",
        "adult_fact_guard_policy",
        "adultReviewBindings",
        "/api/dashboard/ai/adult-review-bindings",
        "/api/dashboard/ai/agents/adult-polish/seed",
        "expected_version",
        "json",
    ):
        assert text in html
    assert 'v-model="binding.policy_hash"' not in html
    assert 'v-model="binding.policy_text"' not in html


def test_settings_allows_disabling_binding_when_policy_state_mismatches():
    html = Path("src/pixiv_novel_sync/templates/dashboard_settings.html").read_text(
        encoding="utf-8"
    )

    disabled_guard = "if (binding.enabled === false) return true;"
    policy_guard = "if (binding.stored_matches === false) return false;"
    assert disabled_guard in html
    assert policy_guard in html
    assert html.index(disabled_guard) < html.index(policy_guard)

from __future__ import annotations

import pytest

from pixiv_novel_sync.storage_db import Database


def _provider(db: Database, name: str = "网关") -> int:
    return db.create_ai_provider({
        "name": name, "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1", "api_key_encrypted": "cipher", "enabled": 1,
    })


def _agent(db: Database, name: str, task_type: str = "continue", provider_id: int | None = None) -> int:
    if provider_id is None:
        provider_id = _provider(db)
    return db.create_ai_agent({
        "name": name, "task_type": task_type, "binding_type": "fixed",
        "provider_id": provider_id, "model": "old-model", "system_prompt": "p",
        "temperature": 0.7, "max_tokens": 3000, "enabled": 1,
    })


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "batch.db")
    database.init_schema()
    yield database
    database.close()


def test_batch_rebind_bumps_binding_version_for_every_row(db: Database) -> None:
    """binding_version 必须涨：候选快照按 agent_config_hash 缓存，不涨版本在跑的 job 会继续用旧候选。"""
    ids = [_agent(db, f"a{i}") for i in range(3)]
    target = _provider(db, "目标网关")
    before = {a["id"]: a["binding_version"] for a in db.list_ai_agents()}

    updated = db.update_ai_agent_bindings(ids, {
        "binding_type": "fixed", "provider_id": target, "model": "glm-4.7", "model_pool_id": None,
    })

    assert updated == 3
    for agent in db.list_ai_agents():
        assert agent["provider_id"] == target
        assert agent["model"] == "glm-4.7"
        assert agent["binding_version"] == before[agent["id"]] + 1


def test_batch_rebind_is_atomic(db: Database) -> None:
    """中途失败不能留下改了一半的状态——那正是「说不清现在到底在用什么」的成因。

    sqlite3.Connection.execute 是 C 扩展只读属性，没法 monkeypatch，所以用一个
    BEFORE UPDATE 触发器在第三行更新时 RAISE(ABORT)，真实制造一次中间失败，
    验证 transaction() 的回滚把前两行也一起还原。
    """
    import sqlite3

    ids = [_agent(db, f"a{i}") for i in range(3)]
    target = _provider(db, "目标网关")
    third = int(ids[2])
    db.conn.execute(
        f"CREATE TRIGGER boom BEFORE UPDATE ON ai_agents WHEN NEW.id = {third} "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.update_ai_agent_bindings(ids, {
            "binding_type": "fixed", "provider_id": target, "model": "glm-4.7", "model_pool_id": None,
        })

    for agent in db.list_ai_agents():
        assert agent["model"] == "old-model"
        assert agent["binding_version"] == 1


def test_batch_rebind_rejects_adult_agents_outright(db: Database) -> None:
    """fail-closed：混入成人 Agent 要整体拒绝，不是静默跳过。"""
    from pixiv_novel_sync.ai.service import AIServiceError, AIWritingService

    normal = _agent(db, "普通")
    adult = _agent(db, "成人润色", task_type="adult_polish")
    target = _provider(db, "目标网关")
    db_path = db.path
    db.close()
    service = AIWritingService(db_path=db_path)

    with pytest.raises(AIServiceError) as excinfo:
        service.update_agent_bindings({
            "agent_ids": [normal, adult],
            "binding": {"binding_type": "fixed", "provider_id": target, "model": "m"},
        })

    assert "成人" in str(excinfo.value)
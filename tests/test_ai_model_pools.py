from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from pixiv_novel_sync.ai.model_pools import (
    ModelPoolConflictError,
    ModelPoolValidationError,
)
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "model-pools.db")
    database.init_schema()
    try:
        yield database
    finally:
        database.close()


def seed_models(db: Database, *, count: int = 2) -> tuple[int, ...]:
    provider_id = db.create_ai_provider(
        {
            "name": "pool-provider",
            "provider_type": "openai_compatible",
            "enabled": True,
        }
    )
    return tuple(
        db.create_ai_provider_model(
            {
                "provider_id": provider_id,
                "model_key": f"model-{index}",
                "enabled": True,
            }
        )
        for index in range(count)
    )


def seed_enabled_pool(db: Database) -> int:
    pool_id = db.create_ai_model_pool({"name": "已启用池", "pool_kind": "custom"})
    (model_id,) = seed_models(db, count=1)
    version = db.replace_ai_model_pool_members(
        pool_id,
        [{"provider_model_id": model_id, "enabled": 1}],
        expected_version=1,
    )
    db.update_ai_model_pool(pool_id, {"enabled": 1}, expected_version=version)
    return pool_id


def seed_agent_bound_to_pool(db: Database, pool_id: int) -> int:
    cursor = db.conn.execute(
        """
        INSERT INTO ai_agents (
            name, task_type, binding_type, provider_id, model,
            model_pool_id, system_prompt
        ) VALUES ('池 Agent', 'continue', 'pool', NULL, NULL, ?, 'prompt')
        """,
        (pool_id,),
    )
    db.conn.commit()
    return int(cursor.lastrowid)


def test_pool_graph_rejects_direct_cycle() -> None:
    model_pools = importlib.import_module("pixiv_novel_sync.ai.model_pools")
    pools = [
        {"id": 1, "fallback_pool_id": 2, "enabled": 0},
        {"id": 2, "fallback_pool_id": 1, "enabled": 0},
    ]

    with pytest.raises(model_pools.ModelPoolValidationError, match="循环"):
        model_pools.validate_pool_graph(pools, {1: [], 2: []})


def test_expand_pool_ids_preserves_fallback_order() -> None:
    model_pools = importlib.import_module("pixiv_novel_sync.ai.model_pools")
    pools = {
        1: {"id": 1, "fallback_pool_id": 2},
        2: {"id": 2, "fallback_pool_id": 3},
        3: {"id": 3, "fallback_pool_id": None},
    }

    assert model_pools.expand_pool_ids(1, pools) == (1, 2, 3)


def test_pool_graph_rejects_fallback_depth_over_eight() -> None:
    model_pools = importlib.import_module("pixiv_novel_sync.ai.model_pools")
    pools = [
        {
            "id": pool_id,
            "fallback_pool_id": pool_id + 1 if pool_id < 9 else None,
            "enabled": 0,
        }
        for pool_id in range(1, 10)
    ]

    with pytest.raises(model_pools.ModelPoolValidationError, match="8"):
        model_pools.validate_pool_graph(pools, {pool_id: [] for pool_id in range(1, 10)})


def test_pool_graph_allows_only_disabled_empty_pool() -> None:
    model_pools = importlib.import_module("pixiv_novel_sync.ai.model_pools")
    disabled = {"id": 1, "fallback_pool_id": None, "enabled": 0}
    model_pools.validate_pool_graph([disabled], {1: []})

    enabled = {**disabled, "enabled": 1}
    with pytest.raises(model_pools.ModelPoolValidationError, match="空"):
        model_pools.validate_pool_graph([enabled], {1: []})


def test_pool_graph_rejects_more_than_64_members_in_one_pool() -> None:
    model_pools = importlib.import_module("pixiv_novel_sync.ai.model_pools")
    pool = {"id": 1, "fallback_pool_id": None, "enabled": 0}
    members = [
        {
            "provider_model_id": model_id,
            "provider_id": 1,
            "model_key": f"model-{model_id}",
            "enabled": 1,
        }
        for model_id in range(1, 66)
    ]

    with pytest.raises(model_pools.ModelPoolValidationError, match="64"):
        model_pools.validate_pool_graph([pool], {1: members})


def test_pool_graph_rejects_more_than_64_unique_candidates_across_chain() -> None:
    model_pools = importlib.import_module("pixiv_novel_sync.ai.model_pools")
    pools = [
        {"id": 1, "fallback_pool_id": 2, "enabled": 0},
        {"id": 2, "fallback_pool_id": None, "enabled": 0},
    ]
    members = {
        1: [
            {"provider_id": 1, "model_key": f"model-{index}", "enabled": 1}
            for index in range(33)
        ],
        2: [
            {"provider_id": 1, "model_key": f"model-{index}", "enabled": 1}
            for index in range(33, 65)
        ],
    }

    with pytest.raises(model_pools.ModelPoolValidationError, match="64"):
        model_pools.validate_pool_graph(pools, members)


def test_pool_graph_deduplicates_same_candidate_across_chain() -> None:
    model_pools = importlib.import_module("pixiv_novel_sync.ai.model_pools")
    pools = [
        {"id": 1, "fallback_pool_id": 2, "enabled": 0},
        {"id": 2, "fallback_pool_id": None, "enabled": 0},
    ]
    repeated = [
        {"provider_id": 1, "model_key": f"model-{index}", "enabled": 1}
        for index in range(40)
    ]

    model_pools.validate_pool_graph(pools, {1: repeated, 2: repeated})


def test_enabled_pool_cannot_use_empty_fallback() -> None:
    model_pools = importlib.import_module("pixiv_novel_sync.ai.model_pools")
    pools = [
        {"id": 1, "fallback_pool_id": 2, "enabled": 1},
        {"id": 2, "fallback_pool_id": None, "enabled": 1},
    ]
    members = {
        1: [{"provider_id": 1, "model_key": "primary", "enabled": 1}],
        2: [],
    }

    with pytest.raises(model_pools.ModelPoolValidationError, match="空"):
        model_pools.validate_pool_graph(pools, members)


def test_enabled_pool_cannot_use_disabled_fallback() -> None:
    model_pools = importlib.import_module("pixiv_novel_sync.ai.model_pools")
    pools = [
        {"id": 1, "fallback_pool_id": 2, "enabled": 1},
        {"id": 2, "fallback_pool_id": None, "enabled": 0},
    ]
    members = {
        1: [{"provider_id": 1, "model_key": "primary", "enabled": 1}],
        2: [{"provider_id": 1, "model_key": "fallback", "enabled": 1}],
    }

    with pytest.raises(model_pools.ModelPoolValidationError, match="启用"):
        model_pools.validate_pool_graph(pools, members)


def test_root_pool_for_binding_must_be_enabled_and_nonempty() -> None:
    model_pools = importlib.import_module("pixiv_novel_sync.ai.model_pools")
    pool = {"id": 1, "fallback_pool_id": None, "enabled": 0}

    with pytest.raises(model_pools.ModelPoolValidationError, match="启用"):
        model_pools.validate_pool_graph([pool], {1: []}, root_pool_id=1)


def test_pool_storage_creates_lists_and_gets_disabled_empty_pool(db: Database) -> None:
    pool_id = db.create_ai_model_pool(
        {"name": "一级模型池", "description": "主路由", "pool_kind": "primary"}
    )

    item = db.get_ai_model_pool(pool_id)
    assert item == {
        "id": pool_id,
        "name": "一级模型池",
        "description": "主路由",
        "pool_kind": "primary",
        "fallback_pool_id": None,
        "fallback_pool_name": None,
        "enabled": False,
        "version": 1,
        "members": [],
        "referenced_by_agents": [],
        "referenced_by_pools": [],
    }
    assert db.list_ai_model_pools() == [item]


def test_pool_storage_rejects_enabled_empty_pool_atomically(db: Database) -> None:
    with pytest.raises(ModelPoolValidationError, match="空"):
        db.create_ai_model_pool(
            {"name": "非法空池", "pool_kind": "custom", "enabled": True}
        )
    assert db.list_ai_model_pools() == []


def test_members_replace_is_ordered_and_stale_version_conflicts(db: Database) -> None:
    pool_id = db.create_ai_model_pool({"name": "顺序池", "pool_kind": "custom"})
    first, second = seed_models(db)

    version = db.replace_ai_model_pool_members(
        pool_id,
        [
            {"provider_model_id": second, "enabled": 1},
            {"provider_model_id": first, "enabled": 1},
        ],
        expected_version=1,
    )
    assert version == 2
    rows = db.conn.execute(
        """
        SELECT provider_model_id, position
        FROM ai_model_pool_members
        WHERE pool_id = ?
        ORDER BY position
        """,
        (pool_id,),
    ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [(second, 1), (first, 2)]

    with pytest.raises(ModelPoolConflictError, match="版本"):
        db.replace_ai_model_pool_members(pool_id, [], expected_version=1)


def test_members_replace_rejects_duplicates_and_rolls_back(db: Database) -> None:
    pool_id = db.create_ai_model_pool({"name": "重复成员池", "pool_kind": "custom"})
    (model_id,) = seed_models(db, count=1)

    with pytest.raises(ModelPoolValidationError, match="重复"):
        db.replace_ai_model_pool_members(
            pool_id,
            [
                {"provider_model_id": model_id, "enabled": 1},
                {"provider_model_id": model_id, "enabled": 1},
            ],
            expected_version=1,
        )
    item = db.get_ai_model_pool(pool_id)
    assert item["version"] == 1
    assert item["members"] == []


def test_members_replace_rejects_missing_model_and_preserves_snapshot(
    db: Database,
) -> None:
    pool_id = db.create_ai_model_pool({"name": "模型引用池", "pool_kind": "custom"})
    (model_id,) = seed_models(db, count=1)
    version = db.replace_ai_model_pool_members(
        pool_id,
        [{"provider_model_id": model_id, "enabled": 1}],
        expected_version=1,
    )

    with pytest.raises(ModelPoolValidationError, match="不存在"):
        db.replace_ai_model_pool_members(
            pool_id,
            [{"provider_model_id": 999999, "enabled": 1}],
            expected_version=version,
        )
    item = db.get_ai_model_pool(pool_id)
    assert item["version"] == version
    assert [member["provider_model_id"] for member in item["members"]] == [model_id]


def test_pool_cannot_be_enabled_with_only_unroutable_models(db: Database) -> None:
    pool_id = db.create_ai_model_pool({"name": "不可用模型池", "pool_kind": "custom"})
    (model_id,) = seed_models(db, count=1)
    db.update_ai_provider_model(model_id, {"enabled": 0})
    version = db.replace_ai_model_pool_members(
        pool_id,
        [{"provider_model_id": model_id, "enabled": 1}],
        expected_version=1,
    )

    with pytest.raises(ModelPoolValidationError, match="空|可用"):
        db.update_ai_model_pool(pool_id, {"enabled": 1}, expected_version=version)


def test_pool_update_rejects_indirect_cycle_and_rolls_back(db: Database) -> None:
    first = db.create_ai_model_pool({"name": "一级", "pool_kind": "primary"})
    second = db.create_ai_model_pool({"name": "二级", "pool_kind": "secondary"})

    updated = db.update_ai_model_pool(
        first, {"fallback_pool_id": second}, expected_version=1
    )
    assert updated == 2

    with pytest.raises(ModelPoolValidationError, match="循环"):
        db.update_ai_model_pool(
            second, {"fallback_pool_id": first}, expected_version=1
        )
    unchanged = db.get_ai_model_pool(second)
    assert unchanged["version"] == 1
    assert unchanged["fallback_pool_id"] is None


def test_agent_referenced_pool_cannot_be_disabled_emptied_or_deleted(
    db: Database,
) -> None:
    pool_id = seed_enabled_pool(db)
    seed_agent_bound_to_pool(db, pool_id)
    version = db.get_ai_model_pool(pool_id)["version"]

    with pytest.raises(ModelPoolConflictError, match="引用"):
        db.update_ai_model_pool(pool_id, {"enabled": 0}, expected_version=version)
    with pytest.raises(ModelPoolConflictError, match="引用"):
        db.replace_ai_model_pool_members(pool_id, [], expected_version=version)
    with pytest.raises(ModelPoolConflictError, match="引用"):
        db.delete_ai_model_pool(pool_id)


def test_fallback_referenced_pool_cannot_be_disabled_emptied_or_deleted(
    db: Database,
) -> None:
    fallback_id = seed_enabled_pool(db)
    model_id = db.get_ai_model_pool(fallback_id)["members"][0]["provider_model_id"]
    primary_id = db.create_ai_model_pool({"name": "主池", "pool_kind": "primary"})
    version = db.replace_ai_model_pool_members(
        primary_id,
        [{"provider_model_id": model_id, "enabled": 1}],
        expected_version=1,
    )
    db.update_ai_model_pool(
        primary_id,
        {"enabled": 1, "fallback_pool_id": fallback_id},
        expected_version=version,
    )
    fallback_version = db.get_ai_model_pool(fallback_id)["version"]

    with pytest.raises(ModelPoolConflictError, match="引用"):
        db.update_ai_model_pool(
            fallback_id, {"enabled": 0}, expected_version=fallback_version
        )
    with pytest.raises(ModelPoolConflictError, match="引用"):
        db.replace_ai_model_pool_members(
            fallback_id, [], expected_version=fallback_version
        )
    with pytest.raises(ModelPoolConflictError, match="引用"):
        db.delete_ai_model_pool(fallback_id)


def test_pool_attempts_use_immutable_snapshots_after_pool_deleted(db: Database) -> None:
    pool_id = db.create_ai_model_pool({"name": "历史池", "pool_kind": "custom"})
    db.conn.execute(
        "INSERT INTO ai_jobs (job_id, task_type, input_json) VALUES ('job-1', 'continue', '{}')"
    )
    db.conn.execute(
        """
        INSERT INTO ai_job_model_attempts (
            job_id, attempt_index, pool_id, provider_id, provider_model_id,
            pool_version_snapshot, pool_position_snapshot, model_key,
            pool_name_snapshot, provider_name_snapshot, agent_config_hash,
            provider_config_hash, candidate_list_hash, stage, status,
            output_started, started_at, finished_at, latency_ms
        ) VALUES (
            'job-1', 0, ?, 9, 99, 3, 2, 'model-x', '历史池', '历史 Provider',
            ?, ?, ?, 'main', 'failed', 0,
            '2026-07-29T00:00:00+00:00', '2026-07-29T00:00:01+00:00', 1000
        )
        """,
        (pool_id, "a" * 64, "b" * 64, "c" * 64),
    )
    db.conn.commit()
    db.delete_ai_model_pool(pool_id)

    attempts = db.list_ai_model_pool_attempts(pool_id)
    assert attempts == [
        {
            "job_id": "job-1",
            "attempt_index": 0,
            "pool_id": pool_id,
            "pool_version": 3,
            "pool_position": 2,
            "pool_name": "历史池",
            "provider_id": 9,
            "provider_model_id": 99,
            "provider_name": "历史 Provider",
            "model_key": "model-x",
            "stage": "main",
            "status": "failed",
            "error_scope": None,
            "error_message": None,
            "error_category": None,
            "finish_reason": None,
            "output_started": False,
            "started_at": "2026-07-29T00:00:00+00:00",
            "finished_at": "2026-07-29T00:00:01+00:00",
            "latency_ms": 1000,
        }
    ]

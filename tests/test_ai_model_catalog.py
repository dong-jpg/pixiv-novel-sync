from __future__ import annotations

import threading
from pathlib import Path

import pytest

from pixiv_novel_sync.ai.model_catalog import (
    ModelCatalogConflictError,
    ModelCatalogValidationError,
    canonical_model_digest,
    normalize_capabilities,
    normalize_model_key,
    normalize_model_record,
    validate_text_field,
)
from pixiv_novel_sync.ai.models import ModelListResult
from pixiv_novel_sync.storage_db import Database

# 在运行时构造 Unicode，源文件保持纯 ASCII，避免被编辑器/工具做 NFC 归一化
_COMBINING_ACUTE = chr(0x0301)  # 组合尖音符
_DECOMPOSED_A = "A" + _COMBINING_ACUTE  # NFD 分解形式 "Á"
_PRECOMPOSED_A = chr(0x00C1)  # NFC 预组合形式 "Á"
_NUL = chr(0x0000)  # NUL 控制字符
_TAB = chr(0x0009)  # 制表符（控制字符）
_DECOMPOSED_E = "e" + _COMBINING_ACUTE  # NFD 分解形式 "é"
_PRECOMPOSED_E = chr(0x00E9)  # NFC 预组合形式 "é"


def test_model_list_result_has_exact_fields():
    result = ModelListResult(
        models=[{"model_key": "m"}],
        complete=True,
        empty_authoritative=False,
        pages=1,
        result_digest="0" * 64,
        partial_reason=None,
    )
    assert result.models == [{"model_key": "m"}]
    assert result.complete is True
    assert result.empty_authoritative is False
    assert result.pages == 1
    assert result.result_digest == "0" * 64
    assert result.partial_reason is None


def test_model_key_is_opaque_but_display_fields_are_nfc_normalized():
    raw = {
        "id": "  " + _DECOMPOSED_A + "/模型  ",
        "name": _DECOMPOSED_A,
        "capabilities": ["streaming", "unknown"],
    }
    item = normalize_model_record(raw)
    # model_key 原样保留：不做 NFC、不去空白，仍是分解形式
    assert item["model_key"] == "  " + _DECOMPOSED_A + "/模型  "
    # 显示名做 NFC 规范化：分解形式 -> 预组合形式
    assert item["display_name"] == _PRECOMPOSED_A
    assert item["display_name"] != _DECOMPOSED_A
    # 未知能力标签保留用于展示
    assert item["capabilities"] == ["streaming", "unknown"]


def test_invalid_control_character_or_length_fails_without_truncation():
    with pytest.raises(ModelCatalogValidationError, match="model_key"):
        normalize_model_key("ok\n")
    with pytest.raises(ModelCatalogValidationError, match="display_name"):
        normalize_model_record({"id": "m", "name": "x" * 201})


def test_digest_deduplicates_by_original_model_key_and_is_stable():
    first = canonical_model_digest([{"model_key": "B"}, {"model_key": "A"}])
    second = canonical_model_digest([{"model_key": "A"}, {"model_key": "B"}])
    assert first == second
    assert len(first) == 64
    # NUL 控制字符：非法 model_key，整体拒绝而不截断
    with pytest.raises(ModelCatalogValidationError):
        normalize_model_record({"id": "m" + _NUL})


def test_model_key_trailing_space_is_preserved_not_rejected():
    # 尾部空格对 opaque model_key 合法，必须原样保留
    assert normalize_model_key("m ") == "m "


def test_required_capabilities_reject_unknown_and_duplicates():
    assert normalize_capabilities(["streaming", "json"], reject_unknown=True) == (
        "streaming",
        "json",
    )
    with pytest.raises(ModelCatalogValidationError, match="能力"):
        normalize_capabilities(["unknown"], reject_unknown=True)
    with pytest.raises(ModelCatalogValidationError, match="能力"):
        normalize_capabilities(["streaming", "streaming"], reject_unknown=True)


def test_display_capabilities_keep_unknown_and_order():
    # 展示用能力（reject_unknown=False）保留未知标签和首次出现顺序
    assert normalize_capabilities(
        ["vision", "streaming", "vision", "unknown"]
    ) == ("vision", "streaming", "unknown")


def test_validate_text_field_enforces_codepoint_and_byte_limits():
    assert validate_text_field(_DECOMPOSED_E, "display_name", 200, 800) == _PRECOMPOSED_E
    with pytest.raises(ModelCatalogValidationError, match="display_name"):
        validate_text_field("x" * 201, "display_name", 200, 800)
    with pytest.raises(ModelCatalogValidationError, match="display_name"):
        validate_text_field("坏" + _TAB + "值", "display_name", 200, 800)


# --- Task 3: Provider 模型目录存储 ---


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "catalog.db")
    database.init_schema()
    try:
        yield database
    finally:
        database.close()


def seed_provider(db: Database, *, available_models: list | None = None) -> int:
    return db.create_ai_provider(
        {
            "name": "provider",
            "provider_type": "openai_compatible",
            "available_models": available_models or [],
            "enabled": True,
        }
    )


def test_catalog_upsert_preserves_manual_overrides_and_marks_missing_unavailable(db):
    provider_id = seed_provider(db, available_models=["legacy"])
    model_id = db.create_ai_provider_model(
        {
            "provider_id": provider_id,
            "model_key": "manual",
            "manual_display_name": "手工名",
            "manual_capabilities": ["json"],
            "enabled": 1,
        }
    )
    db.upsert_discovered_models(
        provider_id,
        [
            normalize_model_record(
                {"id": "manual", "name": "上游名", "capabilities": ["streaming"]}
            ),
            normalize_model_record({"id": "new", "name": "新模型"}),
        ],
        generation=1,
    )
    db.upsert_discovered_models(provider_id, [{"model_key": "new"}], generation=2)
    row = db.get_ai_provider_model(model_id)
    assert row["manual_display_name"] == "手工名"
    assert row["manual_capabilities"] == ["json"]
    # "manual" 模型在第二次同步没被上游返回 -> discovered_available=False，
    # 但 manual=1 仍可路由
    assert row["discovered_available"] is False
    assert row["source"] == "both"
    # routable：manual（enabled）+ new（discovered_available）= 2
    assert db.list_ai_provider_models(provider_id)["routable"] == 2


def test_catalog_three_counts_are_independent(db):
    provider_id = seed_provider(db)
    db.upsert_discovered_models(
        provider_id,
        [{"model_key": "a"}, {"model_key": "b"}, {"model_key": "c"}],
        generation=1,
    )
    # 禁用其中一个
    rows = db.list_ai_provider_models(provider_id)["items"]
    disabled_id = rows[0]["id"]
    db.update_ai_provider_model(disabled_id, {"enabled": 0})
    result = db.list_ai_provider_models(provider_id)
    assert result["total"] == 3
    assert result["discovered_available"] == 3
    assert result["routable"] == 2

    filtered_results = (
        db.list_ai_provider_models(provider_id, search="b"),
        db.list_ai_provider_models(provider_id, enabled_only=True),
        db.list_ai_provider_models(provider_id, routable_only=True),
    )
    assert [len(item["items"]) for item in filtered_results] == [1, 2, 2]
    for item in filtered_results:
        assert item["total"] == 3
        assert item["discovered_available"] == 3
        assert item["routable"] == 2


def test_provider_disabled_makes_all_catalog_models_unroutable(db):
    provider_id = seed_provider(db)
    db.upsert_discovered_models(
        provider_id,
        [{"model_key": "a"}, {"model_key": "b"}],
        generation=1,
    )
    db.update_ai_provider(provider_id, {"enabled": False})

    result = db.list_ai_provider_models(provider_id, routable_only=True)
    assert result["items"] == []
    assert result["total"] == 2
    assert result["discovered_available"] == 2
    assert result["routable"] == 0


def test_upsert_does_not_overwrite_manual_fields_or_enabled(db):
    provider_id = seed_provider(db)
    model_id = db.create_ai_provider_model(
        {
            "provider_id": provider_id,
            "model_key": "m",
            "manual_display_name": "保留名",
            "enabled": 0,
        }
    )
    db.upsert_discovered_models(
        provider_id,
        [{"model_key": "m", "display_name": "上游覆盖"}],
        generation=1,
    )
    row = db.get_ai_provider_model(model_id)
    assert row["manual_display_name"] == "保留名"
    assert row["enabled"] is False
    assert row["discovered_display_name"] == "上游覆盖"
    # 有效显示名优先 manual
    assert row["display_name"] == "保留名"


def test_upsert_persists_every_normalized_discovered_field(db):
    provider_id = seed_provider(db)
    normalized = normalize_model_record(
        {
            "id": "normalized",
            "name": _DECOMPOSED_E,
            "capabilities": ["streaming", "unknown"],
            "context_window": 4096,
            "owned_by": _DECOMPOSED_E,
        }
    )

    db.upsert_discovered_models(provider_id, [normalized], generation=1)

    row = db.list_ai_provider_models(provider_id)["items"][0]
    assert row["discovered_display_name"] == normalized["display_name"]
    assert row["discovered_capabilities"] == normalized["capabilities"]
    assert row["discovered_context_window"] == normalized["context_window"]
    stored_metadata = db.conn.execute(
        "SELECT discovered_metadata_json FROM ai_provider_models WHERE id = ?",
        (row["id"],),
    ).fetchone()[0]
    assert stored_metadata == normalized["metadata_json"]


@pytest.mark.parametrize(
    "metadata_json",
    [
        (
            '{"api_key":"secret","capabilities":["streaming"],'
            '"context_window":4096,"created":1,"owned_by":"owner",'
            '"prompt":"private body"}'
        ),
        (
            '{ "owned_by": "owner", "created": 1, "context_window": 4096, '
            '"capabilities": ["streaming"] }'
        ),
        (
            '{"capabilities":["json"],"context_window":4096,'
            '"created":1,"owned_by":"owner"}'
        ),
        (
            '{"capabilities":["streaming"],"context_window":8192,'
            '"created":1,"owned_by":"owner"}'
        ),
    ],
    ids=(
        "non-whitelisted-secrets",
        "non-canonical-json",
        "capabilities-mismatch",
        "context-window-mismatch",
    ),
)
def test_upsert_rejects_noncanonical_normalized_metadata_before_writing(
    db, metadata_json: str
):
    provider_id = seed_provider(db)
    existing = normalize_model_record(
        {"id": "existing", "name": "before", "owned_by": "original"}
    )
    db.upsert_discovered_models(provider_id, [existing], generation=1)
    valid_new = normalize_model_record({"id": "valid-new", "owned_by": "safe"})
    invalid = normalize_model_record(
        {
            "id": "invalid",
            "capabilities": ["streaming"],
            "context_window": 4096,
            "created": 1,
            "owned_by": "owner",
        }
    )
    invalid["metadata_json"] = metadata_json

    with pytest.raises(ModelCatalogValidationError, match="metadata_json"):
        db.upsert_discovered_models(
            provider_id,
            [valid_new, invalid],
            generation=2,
        )

    result = db.list_ai_provider_models(provider_id)
    assert result["total"] == 1
    assert result["discovered_available"] == 1
    assert result["items"][0]["model_key"] == "existing"
    assert result["items"][0]["discovered_display_name"] == "before"
    stored_metadata = db.conn.execute(
        "SELECT discovered_metadata_json FROM ai_provider_models WHERE provider_id = ?",
        (provider_id,),
    ).fetchone()[0]
    assert stored_metadata == existing["metadata_json"]


def test_duplicate_discovered_keys_upsert_one_row_with_last_value(db):
    provider_id = seed_provider(db)

    mutation_counts = db.upsert_discovered_models(
        provider_id,
        [
            {"model_key": "same", "display_name": "first"},
            {"model_key": "same", "display_name": "last"},
        ],
        generation=1,
    )

    result = db.list_ai_provider_models(provider_id)
    assert mutation_counts == {"inserted": 1, "updated": 0}
    assert result["total"] == 1
    assert result["items"][0]["discovered_display_name"] == "last"


def test_catalog_list_uses_one_snapshot_for_provider_items_and_counts(db):
    provider_id = seed_provider(db)
    db.upsert_discovered_models(provider_id, [{"model_key": "a"}], generation=1)
    count_query_started = threading.Event()
    mutation_finished = threading.Event()
    worker_errors: list[BaseException] = []

    def trace(statement: str) -> None:
        if "SELECT COUNT(*) AS TOTAL" in statement.upper():
            count_query_started.set()
            mutation_finished.wait(timeout=5)

    def mutate_catalog() -> None:
        competing = Database(db.path)
        try:
            if not count_query_started.wait(timeout=5):
                raise AssertionError("catalog list did not reach its count query")
            competing.update_ai_provider(provider_id, {"enabled": False})
            competing.upsert_discovered_models(
                provider_id,
                [{"model_key": "a"}, {"model_key": "b"}],
                generation=2,
            )
        except BaseException as exc:
            worker_errors.append(exc)
        finally:
            mutation_finished.set()
            competing.close()

    db.conn.set_trace_callback(trace)
    worker = threading.Thread(target=mutate_catalog)
    worker.start()
    try:
        snapshot = db.list_ai_provider_models(provider_id)
    finally:
        db.conn.set_trace_callback(None)
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert worker_errors == []
    assert [item["model_key"] for item in snapshot["items"]] == ["a"]
    assert snapshot["total"] == 1
    assert snapshot["discovered_available"] == 1
    assert snapshot["routable"] == 1

    current = db.list_ai_provider_models(provider_id)
    assert current["total"] == 2
    assert current["discovered_available"] == 2
    assert current["routable"] == 0


def test_upsert_validation_failure_does_not_partially_write(db):
    provider_id = seed_provider(db)
    db.upsert_discovered_models(
        provider_id,
        [{"model_key": "existing", "display_name": "before"}],
        generation=1,
    )

    with pytest.raises(ModelCatalogValidationError, match="model_key"):
        db.upsert_discovered_models(
            provider_id,
            [
                {"model_key": "new", "display_name": "valid"},
                {"model_key": "invalid\n"},
            ],
            generation=2,
        )

    result = db.list_ai_provider_models(provider_id)
    assert result["total"] == 1
    assert result["discovered_available"] == 1
    assert result["items"][0]["model_key"] == "existing"
    assert result["items"][0]["discovered_display_name"] == "before"


def test_manual_model_delete_is_blocked_when_pool_member_references_it(db):
    provider_id = seed_provider(db)
    model_id = db.create_ai_provider_model(
        {"provider_id": provider_id, "model_key": "m", "manual_display_name": "x"}
    )
    # 直接使用存储层建池，保持本测试只覆盖目录引用语义。
    db.conn.execute(
        "INSERT INTO ai_model_pools(name, pool_kind, version) VALUES ('p', 'custom', 1)"
    )
    pool_id = int(db.conn.execute("SELECT id FROM ai_model_pools").fetchone()[0])
    db.conn.execute(
        "INSERT INTO ai_model_pool_members(pool_id, provider_model_id, position) VALUES (?, ?, 1)",
        (pool_id, model_id),
    )
    db.conn.commit()
    with pytest.raises(ModelCatalogConflictError, match="模型池"):
        db.remove_ai_provider_model_manual(model_id)


def test_search_never_returns_secret_fields(db):
    provider_id = seed_provider(db)
    db.upsert_discovered_models(provider_id, [{"model_key": "gpt-4"}], generation=1)
    result = db.list_ai_provider_models(provider_id, search="gpt")
    assert len(result["items"]) == 1
    import json as _json

    dumped = _json.dumps(result, ensure_ascii=False).lower()
    assert "api_key" not in dumped
    assert "available_models_json" not in dumped


def test_remove_manual_keeps_discovered_row(db):
    provider_id = seed_provider(db)
    # 先发现，再补录 manual，删除 manual 标记后 discovered 行保留
    db.upsert_discovered_models(provider_id, [{"model_key": "m"}], generation=1)
    row_id = db.list_ai_provider_models(provider_id)["items"][0]["id"]
    db.update_ai_provider_model(row_id, {"manual_display_name": "手工"})
    db.conn.execute("UPDATE ai_provider_models SET manual=1 WHERE id=?", (row_id,))
    db.conn.commit()
    db.remove_ai_provider_model_manual(row_id)
    row = db.get_ai_provider_model(row_id)
    assert row is not None
    assert row["manual"] is False
    assert row["discovered"] is True


def test_remove_manual_from_referenced_discovered_row_keeps_reference(db):
    provider_id = seed_provider(db)
    db.upsert_discovered_models(provider_id, [{"model_key": "m"}], generation=1)
    model_id = db.list_ai_provider_models(provider_id)["items"][0]["id"]
    db.update_ai_provider_model(model_id, {"manual_display_name": "manual"})
    db.conn.execute(
        "INSERT INTO ai_model_pools(name, pool_kind, version) VALUES ('p', 'custom', 1)"
    )
    pool_id = int(db.conn.execute("SELECT id FROM ai_model_pools").fetchone()[0])
    db.conn.execute(
        "INSERT INTO ai_model_pool_members(pool_id, provider_model_id, position) VALUES (?, ?, 1)",
        (pool_id, model_id),
    )
    db.conn.commit()

    db.remove_ai_provider_model_manual(model_id)

    row = db.get_ai_provider_model(model_id)
    assert row is not None
    assert row["manual"] is False
    assert row["discovered"] is True
    assert db.conn.execute(
        "SELECT provider_model_id FROM ai_model_pool_members WHERE pool_id = ?",
        (pool_id,),
    ).fetchone()[0] == model_id


def test_remove_manual_serializes_with_concurrent_discovery(db):
    provider_id = seed_provider(db)
    model_id = db.create_ai_provider_model(
        {"provider_id": provider_id, "model_key": "m"}
    )
    delete_started = threading.Event()
    discovery_finished = threading.Event()
    worker_errors: list[BaseException] = []
    trace_state = {"immediate": False}

    def trace(statement: str) -> None:
        sql = statement.lstrip().upper()
        if sql.startswith("BEGIN IMMEDIATE"):
            trace_state["immediate"] = True
        if sql.startswith("DELETE FROM AI_PROVIDER_MODELS"):
            delete_started.set()
            if not trace_state["immediate"]:
                discovery_finished.wait(timeout=5)

    def discover() -> None:
        competing = Database(db.path)
        try:
            if not delete_started.wait(timeout=5):
                raise AssertionError("manual delete did not reach its write")
            competing.upsert_discovered_models(
                provider_id,
                [{"model_key": "m", "display_name": "discovered"}],
                generation=1,
            )
        except BaseException as exc:
            worker_errors.append(exc)
        finally:
            discovery_finished.set()
            competing.close()

    db.conn.set_trace_callback(trace)
    worker = threading.Thread(target=discover)
    worker.start()
    try:
        db.remove_ai_provider_model_manual(model_id)
    finally:
        db.conn.set_trace_callback(None)
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert worker_errors == []
    items = db.list_ai_provider_models(provider_id)["items"]
    assert len(items) == 1
    row = items[0]
    assert row["model_key"] == "m"
    assert row["manual"] is False
    assert row["discovered"] is True

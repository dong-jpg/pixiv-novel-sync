from __future__ import annotations

import hashlib
import json

import pytest

from pixiv_novel_sync.ai.model_catalog import (
    ModelCatalogValidationError,
    canonical_model_digest,
    normalize_capabilities,
    normalize_model_key,
    normalize_model_record,
)


def _catalog_item(**overrides):
    item = {
        "model_key": "model-a",
        "display_name": "Model A",
        "capabilities": ["streaming"],
        "context_window": 4096,
        "metadata_json": '{"owned_by":"vendor"}',
    }
    item.update(overrides)
    return item


def test_digest_hashes_only_sorted_normalized_catalog_fields():
    models = [
        _catalog_item(model_key="model-b", api_key="secret", response_body="private"),
        _catalog_item(),
    ]
    canonical_items = [
        _catalog_item(),
        _catalog_item(model_key="model-b"),
    ]
    canonical_json = json.dumps(
        canonical_items,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    assert canonical_model_digest(models) == expected


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("display_name", "Renamed"),
        ("capabilities", ["streaming", "vision"]),
        ("context_window", 8192),
        ("metadata_json", '{"owned_by":"other"}'),
    ],
)
def test_digest_changes_with_each_normalized_catalog_field(field, changed_value):
    original = _catalog_item()
    changed = _catalog_item(**{field: changed_value})

    assert canonical_model_digest([original]) != canonical_model_digest([changed])


def test_digest_deduplicates_equal_original_model_keys():
    model = _catalog_item()

    assert canonical_model_digest([model, dict(model)]) == canonical_model_digest([model])


def test_metadata_uses_normalized_whitelist_values():
    combining_acute = chr(0x0301)
    normalized_e = chr(0x00E9)
    normalized_a = chr(0x00C1)
    item = normalize_model_record(
        {
            "id": "model-a",
            "owned_by": "e" + combining_acute,
            "created": "A" + combining_acute,
            "capabilities": ["e" + combining_acute, normalized_e, "vision"],
            "context_window": 4096,
            "api_key": "secret",
            "response_body": "private",
        }
    )
    expected_metadata = {
        "capabilities": [normalized_e, "vision"],
        "context_window": 4096,
        "created": normalized_a,
        "owned_by": normalized_e,
    }

    assert item["capabilities"] == [normalized_e, "vision"]
    assert item["metadata_json"] == json.dumps(
        expected_metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("owned_by", 123),
        ("owned_by", "bad\nvalue"),
        ("owned_by", object()),
        ("created", True),
        ("created", 1.5),
        ("created", []),
        ("created", "bad\nvalue"),
    ],
)
def test_invalid_metadata_values_raise_catalog_validation_error(field, invalid_value):
    with pytest.raises(ModelCatalogValidationError):
        normalize_model_record({"id": "model-a", field: invalid_value})


def test_null_metadata_values_are_omitted():
    item = normalize_model_record(
        {
            "id": "model-a",
            "owned_by": None,
            "created": None,
            "capabilities": None,
            "context_window": None,
        }
    )

    assert item["metadata_json"] == "{}"


def test_created_integer_is_preserved_in_metadata():
    item = normalize_model_record({"id": "model-a", "created": 1_700_000_000})

    assert json.loads(item["metadata_json"])["created"] == 1_700_000_000


def test_metadata_limit_uses_normalized_compact_utf8_json():
    json_overhead = len('{"owned_by":""}'.encode("utf-8"))
    exact_ascii_value = "x" * (8192 - json_overhead)
    exact_item = normalize_model_record({"id": "model-a", "owned_by": exact_ascii_value})
    assert len(exact_item["metadata_json"].encode("utf-8")) == 8192

    with pytest.raises(ModelCatalogValidationError):
        normalize_model_record({"id": "model-a", "owned_by": exact_ascii_value + "x"})

    combining_value = ("e" + chr(0x0301)) * ((8192 - json_overhead) // 2)
    normalized_item = normalize_model_record({"id": "model-a", "owned_by": combining_value})
    assert len(normalized_item["metadata_json"].encode("utf-8")) <= 8192


def test_model_key_accepts_the_1200_byte_boundary():
    model_key = chr(0x1F600) * 300
    assert len(model_key.encode("utf-8")) == 1200
    assert normalize_model_key(model_key) == model_key

    with pytest.raises(ModelCatalogValidationError):
        normalize_model_key(model_key + "x")


def test_display_capabilities_enforce_the_64_item_boundary():
    labels = [f"capability-{index}" for index in range(64)]
    assert normalize_capabilities(labels) == tuple(labels)

    with pytest.raises(ModelCatalogValidationError):
        normalize_capabilities([*labels, "capability-64"])

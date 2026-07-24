from __future__ import annotations

import pytest

from pixiv_novel_sync.ai.model_catalog import (
    ModelCatalogValidationError,
    canonical_model_digest,
    normalize_capabilities,
    normalize_model_key,
    normalize_model_record,
    validate_text_field,
)
from pixiv_novel_sync.ai.models import ModelListResult

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

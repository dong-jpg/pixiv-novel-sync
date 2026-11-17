from collections import UserDict
import json

import pytest

from pixiv_novel_sync.work_analysis.contracts import (
    WorkAnalysisError, WorkCallBinding, WorkLease, canonical_bytes,
    fingerprint, hex_id, strict_int, strict_object, strict_text, validate_budget,
)


@pytest.mark.parametrize("data", [{"required": 1}, {"required": 1, "optional": 2}])
def test_strict_object_accepts_mapping_and_returns_an_independent_outer_dict(data):
    source = UserDict(data)
    result = strict_object(source, {"required"}, optional={"optional"})
    assert type(result) is dict
    assert result == data
    result["required"] = 3
    assert source["required"] == 1


@pytest.mark.parametrize("value", [
    {}, {"optional": 2}, {"required": 1, "extra": 3},
    None, [], [["required", 1]], "required", 1,
])
def test_strict_object_rejects_missing_extra_and_non_mapping_values(value):
    with pytest.raises(WorkAnalysisError) as raised:
        strict_object(value, {"required"}, optional={"optional"})
    assert raised.value.code == "invalid_fields"
    assert raised.value.status == 400


@pytest.mark.parametrize("value", [-2, 0, 2])
def test_strict_int_accepts_inclusive_bounds(value):
    assert strict_int(value, -2, 2) == value


@pytest.mark.parametrize("value", [True, False, "1", 1.0, -3, 3])
def test_strict_int_rejects_non_integers_and_out_of_range_values(value):
    with pytest.raises(WorkAnalysisError) as raised:
        strict_int(value, -2, 2)
    assert raised.value.code == "invalid_integer"
    assert raised.value.status == 400


@pytest.mark.parametrize("length", [32, 64])
def test_hex_id_accepts_lowercase_at_explicit_length(length):
    value = "0123456789abcdef" * (length // 16)
    assert hex_id(value, length) == value


@pytest.mark.parametrize("length", [32, 64])
@pytest.mark.parametrize("kind", ["uppercase", "short", "long", "nonhex", "null", "number", "list"])
def test_hex_id_rejects_wrong_case_length_alphabet_and_type(length, kind):
    valid = "abcd" * (length // 4)
    value = {"uppercase": valid.upper(), "short": valid[:-1], "long": valid + "0",
             "nonhex": "g" * length, "null": None, "number": length, "list": [valid]}[kind]
    with pytest.raises(WorkAnalysisError) as raised:
        hex_id(value, length)
    assert raised.value.code == "invalid_id"
    assert raised.value.status == 400


@pytest.mark.parametrize("field,value", [
    ("request_limit", True), ("request_limit", 0), ("request_limit", 513),
    ("token_limit", 0), ("token_limit", 100000001),
    ("active_seconds_limit", 29), ("active_seconds_limit", 14401),
    ("active_seconds_limit", float("inf")),
])
def test_budget_rejects_invalid_or_implicit_values(field, value):
    budget = {"request_limit": 10, "token_limit": 10000, "active_seconds_limit": 60}
    budget[field] = value
    with pytest.raises(WorkAnalysisError):
        validate_budget(budget)


def test_budget_has_exact_fields_and_canonical_identity():
    budget = {"request_limit": 512, "token_limit": 100000000, "active_seconds_limit": 14400}
    assert validate_budget(budget) == budget
    with pytest.raises(WorkAnalysisError):
        validate_budget({**budget, "amount": 1})
    with pytest.raises(WorkAnalysisError):
        validate_budget({"request_limit": 1})
    assert fingerprint({"b": 2, "a": 1}) == fingerprint({"a": 1, "b": 2})
    assert fingerprint({"a": 1}) != fingerprint({"a": True})
    assert json.loads(canonical_bytes({"text": "\u4e2d\U0001f600"})) == {"text": "\u4e2d\U0001f600"}
    with pytest.raises(ValueError):
        canonical_bytes({"nan": float("nan")})


def test_binding_is_immutable_and_text_length_counts_codepoints():
    lease = WorkLease("a" * 32, 1, "b" * 64)
    binding = WorkCallBinding(lease, "c" * 64, 1, "d" * 64)
    with pytest.raises((AttributeError, TypeError)):
        binding.try_no = 2
    assert strict_text("\U0001f600", 1) == "\U0001f600"
    with pytest.raises(WorkAnalysisError):
        strict_text("\ud800", 1)

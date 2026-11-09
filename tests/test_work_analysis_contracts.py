import json

import pytest

from pixiv_novel_sync.work_analysis.contracts import (
    WorkAnalysisError, WorkCallBinding, WorkLease, canonical_bytes,
    fingerprint, strict_text, validate_budget,
)


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

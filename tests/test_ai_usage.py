from __future__ import annotations

import pytest

from pixiv_novel_sync.ai.usage import (
    ProviderUsage,
    merge_usage_snapshot,
    normalize_anthropic_usage,
    normalize_openai_usage,
    summarize_usage,
)


def test_chat_usage_includes_cache_and_reasoning_without_adding_them_twice():
    usage = normalize_openai_usage({
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 80},
        "completion_tokens_details": {"reasoning_tokens": 5},
    })
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (100, 20, 120)
    assert usage.cached_input_tokens == 80
    assert usage.reasoning_output_tokens == 5
    assert usage.complete is False


def test_anthropic_input_adds_only_reported_cache_components():
    usage = normalize_anthropic_usage({
        "input_tokens": 12, "cache_creation_input_tokens": 8,
        "cache_read_input_tokens": 80, "output_tokens": 20,
    })
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (100, 20, 120)
    assert usage.cached_input_tokens == 80
    assert usage.cache_creation_input_tokens == 8
    assert usage.reasoning_output_tokens is None


def test_usage_distinguishes_missing_from_explicit_zero_and_replay_is_idempotent():
    unknown = ProviderUsage().to_dict()
    zero = ProviderUsage(input_tokens=0, output_tokens=0, total_tokens=0,
                         complete=True, missing_reason=None).to_dict()
    assert unknown["input_tokens"] is None
    assert zero["input_tokens"] == 0
    first = ProviderUsage(input_tokens=10, output_tokens=3, total_tokens=13,
                          missing_reason="partial_report").to_dict()
    final = ProviderUsage(input_tokens=10, output_tokens=7, total_tokens=17,
                          complete=True, missing_reason=None).to_dict()
    assert merge_usage_snapshot(first, final) == final
    assert merge_usage_snapshot(final, final) == final
    assert merge_usage_snapshot(final, unknown) == final


@pytest.mark.parametrize("bad", [True, False, -1, "12", 1.5])
def test_invalid_counter_is_rejected_not_persisted_as_zero(bad):
    with pytest.raises(ValueError):
        normalize_openai_usage({"prompt_tokens": bad})
    with pytest.raises(ValueError):
        normalize_anthropic_usage({"output_tokens": bad})


@pytest.mark.parametrize("normalizer", [normalize_openai_usage, normalize_anthropic_usage])
@pytest.mark.parametrize("bad", [None, [], "private usage text", 1, True])
def test_usage_requires_an_object(normalizer, bad):
    with pytest.raises(ValueError, match="usage must be an object"):
        normalizer(bad)


def test_chat_usage_rejects_inconsistent_total():
    with pytest.raises(ValueError, match="inconsistent usage totals"):
        normalize_openai_usage({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 11})


@pytest.mark.parametrize("key", ["prompt_tokens_details", "completion_tokens_details"])
@pytest.mark.parametrize("bad", [True, 0, [], "private details"])
def test_chat_usage_rejects_malformed_details(key, bad):
    with pytest.raises(ValueError, match="invalid usage details"):
        normalize_openai_usage({key: bad})


@pytest.mark.parametrize("normalizer,raw", [
    (normalize_openai_usage, {"completion_tokens": 2}),
    (normalize_anthropic_usage, {"output_tokens": 2}),
])
def test_missing_input_does_not_fabricate_total_or_subdivisions(normalizer, raw):
    usage = normalizer(raw)
    assert usage.input_tokens is None
    assert usage.output_tokens == 2
    assert usage.total_tokens is None
    assert usage.cached_input_tokens is None
    assert usage.cache_creation_input_tokens is None
    assert usage.reasoning_output_tokens is None


def test_anthropic_cache_only_report_does_not_guess_missing_base_input():
    usage = normalize_anthropic_usage({"cache_read_input_tokens": 80, "cache_creation_input_tokens": 8})
    assert usage.input_tokens is None
    assert usage.total_tokens is None
    assert usage.cached_input_tokens == 80
    assert usage.cache_creation_input_tokens == 8


@pytest.mark.parametrize("normalizer,raw", [
    (normalize_openai_usage, {"prompt_tokens": 0, "completion_tokens": 0}),
    (normalize_anthropic_usage, {"input_tokens": 0, "output_tokens": 0}),
])
def test_absent_optional_cache_and_reasoning_remain_unknown(normalizer, raw):
    usage = normalizer(raw)
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (0, 0, 0)
    assert usage.cached_input_tokens is None
    assert usage.cache_creation_input_tokens is None
    assert usage.reasoning_output_tokens is None


@pytest.mark.parametrize("values", [
    {"input_tokens": 10, "cached_input_tokens": 11},
    {"input_tokens": 10, "cache_creation_input_tokens": 11},
    {"output_tokens": 2, "reasoning_output_tokens": 3},
])
def test_usage_subdivision_cannot_exceed_known_inclusive_total(values):
    with pytest.raises(ValueError, match="subdivision"):
        ProviderUsage(**values)


@pytest.mark.parametrize("details", [
    {"prompt_tokens_details": {"cached_tokens": 11}},
    {"completion_tokens_details": {"reasoning_tokens": 3}},
])
def test_chat_normalizer_rejects_subdivision_exceeding_total(details):
    with pytest.raises(ValueError, match="subdivision"):
        normalize_openai_usage({"prompt_tokens": 10, "completion_tokens": 2, **details})


@pytest.mark.parametrize("bad", ["private upstream reason", True, 1, [], {}, {"pending"}])
def test_usage_reason_validation_is_safe_for_non_strings_and_unhashable_values(bad):
    with pytest.raises(ValueError, match="invalid usage completeness") as caught:
        ProviderUsage(missing_reason=bad)
    assert "private upstream reason" not in str(caught.value)


@pytest.mark.parametrize("values", [
    {"complete": 1},
    {"complete": True, "missing_reason": None},
    {"complete": True, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
])
def test_usage_complete_requires_boolean_complete_totals_and_no_missing_reason(values):
    with pytest.raises(ValueError):
        ProviderUsage(**values)


def test_normalized_usage_rejects_contradictory_total():
    with pytest.raises(ValueError, match="inconsistent usage totals"):
        ProviderUsage(input_tokens=10, output_tokens=2, total_tokens=99)


def test_cumulative_merge_uses_maxima_not_sum_and_recomputes_total():
    old = ProviderUsage(input_tokens=10, output_tokens=3, total_tokens=13,
                        cached_input_tokens=8, missing_reason="partial_report").to_dict()
    new = ProviderUsage(input_tokens=8, output_tokens=7, total_tokens=15,
                        cached_input_tokens=6, reasoning_output_tokens=2,
                        missing_reason="partial_report").to_dict()
    merged = merge_usage_snapshot(old, new)
    assert (merged["input_tokens"], merged["output_tokens"], merged["total_tokens"]) == (10, 7, 17)
    assert merged["cached_input_tokens"] == 8
    assert merged["reasoning_output_tokens"] == 2
    assert merge_usage_snapshot(merged, old) == merged


@pytest.mark.parametrize("bad", [
    {},
    {**ProviderUsage().to_dict(), "raw_text": "private upstream content"},
    {**ProviderUsage().to_dict(), "missing_reason": []},
    [],
    None,
])
def test_merge_requires_exact_normalized_schema_and_safe_validation(bad):
    valid = ProviderUsage().to_dict()
    with pytest.raises(ValueError):
        merge_usage_snapshot(valid, bad)
    with pytest.raises(ValueError):
        merge_usage_snapshot(bad, valid)


def test_merge_rejects_contradictory_cross_snapshot_total_instead_of_decreasing_it():
    previous = ProviderUsage(total_tokens=100).to_dict()
    incoming = ProviderUsage(input_tokens=5, output_tokens=3).to_dict()
    with pytest.raises(ValueError, match="inconsistent usage totals"):
        merge_usage_snapshot(previous, incoming)


def request_record(ordinal, **values):
    return {"request_ordinal": ordinal, **ProviderUsage(**values).to_dict()}


def test_summary_no_reserved_requests_is_unknown_not_complete():
    assert summarize_usage([], network_request_count=0) == {
        "known_input_tokens": None,
        "known_output_tokens": None,
        "known_total_tokens": None,
        "reported_requests": 0,
        "unreported_requests": 0,
        "complete": False,
    }


def test_summary_legacy_and_pending_requests_stay_unknown():
    attempts = [{"usage": None}, {"usage": {"requests": [request_record(1)]}}]
    assert summarize_usage(attempts, network_request_count=2) == {
        "known_input_tokens": None,
        "known_output_tokens": None,
        "known_total_tokens": None,
        "reported_requests": 0,
        "unreported_requests": 2,
        "complete": False,
    }


def test_summary_explicit_zero_is_known_and_complete():
    attempts = [{"usage": {"requests": [request_record(
        1, input_tokens=0, output_tokens=0, total_tokens=0, complete=True, missing_reason=None,
    )]}}]
    assert summarize_usage(attempts, network_request_count=1) == {
        "known_input_tokens": 0,
        "known_output_tokens": 0,
        "known_total_tokens": 0,
        "reported_requests": 1,
        "unreported_requests": 0,
        "complete": True,
    }


def test_summary_sums_known_fields_independently_not_subdivisions_or_missing_values():
    attempts = [{"usage": {"requests": [
        request_record(1, input_tokens=10, output_tokens=2, total_tokens=12,
                       cached_input_tokens=8, reasoning_output_tokens=1,
                       complete=True, missing_reason=None),
        request_record(2, input_tokens=5, missing_reason="partial_report"),
    ]}}, {"usage": {"requests": [request_record(3, total_tokens=7, missing_reason="partial_report")]}}]
    assert summarize_usage(attempts, network_request_count=4) == {
        "known_input_tokens": 15,
        "known_output_tokens": 2,
        "known_total_tokens": 19,
        "reported_requests": 1,
        "unreported_requests": 3,
        "complete": False,
    }


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("duplicate_kind", ["identical", "corrupt"])
def test_summary_duplicate_ordinal_is_unknown_even_across_attempts(reverse, duplicate_kind):
    valid = request_record(1, input_tokens=10, output_tokens=2, total_tokens=12,
                           complete=True, missing_reason=None)
    duplicate = dict(valid)
    if duplicate_kind == "corrupt":
        duplicate["missing_reason"] = ["private content"]
    records = [valid, duplicate]
    if reverse:
        records.reverse()
    attempts = [{"usage": {"requests": [record]}} for record in records]
    summary = summarize_usage(attempts, network_request_count=1)
    assert summary["known_total_tokens"] is None
    assert summary["reported_requests"] == 0
    assert summary["unreported_requests"] == 1
    assert summary["complete"] is False


def test_summary_corrupt_out_of_range_and_invalid_ordinals_do_not_invent_requests():
    complete = {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3,
                "complete": True, "missing_reason": None}
    records = [request_record(ordinal, **complete) for ordinal in [0, 3, True, "1", [], {}]]
    records.extend([
        {**request_record(1, **complete), "raw": "private content"},
        {**request_record(2, **complete), "total_tokens": 100},
        None,
    ])
    attempts = [{"usage": {"requests": records}}, {"usage": []}, {"usage": {"requests": "private"}}, None]
    summary = summarize_usage(attempts, network_request_count=2)
    assert summary == {
        "known_input_tokens": None,
        "known_output_tokens": None,
        "known_total_tokens": None,
        "reported_requests": 0,
        "unreported_requests": 2,
        "complete": False,
    }
    assert summarize_usage(attempts, network_request_count=0)["known_total_tokens"] is None


@pytest.mark.parametrize("bad", [True, -1, "2", None])
def test_summary_rejects_invalid_authoritative_network_count(bad):
    with pytest.raises(ValueError, match="network request count"):
        summarize_usage([], network_request_count=bad)


def make_observer(protocol="openai_compatible", ordinal=1):
    from pixiv_novel_sync.ai.provider_usage import RequestUsageObserver

    seen = []
    observer = RequestUsageObserver(protocol, ordinal, lambda number, usage: seen.append((number, usage)))
    return observer, seen


def test_observer_emits_cumulative_incomplete_then_idempotent_terminal_usage():
    observer, seen = make_observer()
    observer.observe({"prompt_tokens": 10, "completion_tokens": 3}, final=True)
    observer.observe({"completion_tokens": 7}, final=True)
    assert all(not usage["complete"] for _, usage in seen)
    assert seen[-1][1]["total_tokens"] == 17
    observer.finish(terminal=True)
    expected = list(seen)
    observer.finish(terminal=False, missing_reason="interrupted")
    observer.observe({"completion_tokens": 99}, final=True)
    assert seen == expected
    assert seen[-1][1]["complete"] is True
    assert seen[-1][1]["missing_reason"] is None


def test_observer_anthropic_merges_start_cache_and_replayed_output_cumulatively():
    observer, seen = make_observer("anthropic")
    observer.observe({"input_tokens": 12, "cache_creation_input_tokens": 8,
                      "cache_read_input_tokens": 80, "output_tokens": 1})
    for output in [5, 7, 7, 3]:
        observer.observe({"output_tokens": output}, final=True)
    observer.finish(terminal=True)
    assert (seen[-1][1]["input_tokens"], seen[-1][1]["output_tokens"], seen[-1][1]["total_tokens"]) == (100, 7, 107)
    assert seen[-1][1]["complete"] is True


@pytest.mark.parametrize("final_usage", [None, {}, {"other": "private"}, {"output_tokens": None}])
def test_observer_anthropic_start_output_alone_is_not_a_final_report(final_usage):
    observer, seen = make_observer("anthropic")
    observer.observe({"input_tokens": 10, "output_tokens": 1})
    observer.observe(final_usage, final=True)
    observer.finish(terminal=True)
    assert seen[-1][1]["output_tokens"] == 1
    assert seen[-1][1]["complete"] is False
    assert seen[-1][1]["missing_reason"] == "partial_report"


@pytest.mark.parametrize("terminal,reason", [(False, "interrupted"), (True, "not_reported")])
def test_observer_missing_usage_uses_fixed_terminal_reason(terminal, reason):
    observer, seen = make_observer()
    observer.finish(terminal=terminal, missing_reason=None if terminal else "interrupted")
    assert seen == [(1, ProviderUsage(missing_reason=reason).to_dict())]


def test_observer_final_totals_without_terminal_marker_are_still_incomplete():
    observer, seen = make_observer()
    observer.observe({"prompt_tokens": 10, "completion_tokens": 2}, final=True)
    observer.finish(terminal=False, missing_reason="interrupted")
    assert seen[-1][1]["complete"] is False
    assert seen[-1][1]["missing_reason"] == "interrupted"
    assert seen[-1][1]["total_tokens"] == 12


@pytest.mark.parametrize("bad", ["private usage text", [], {"prompt_tokens": True},
                                 {"prompt_tokens_details": {"cached_tokens": "private details"}}])
def test_observer_invalid_usage_is_sticky_and_does_not_keep_raw_values(bad):
    observer, seen = make_observer()
    observer.observe(bad, final=True)
    observer.observe({"prompt_tokens": 10, "completion_tokens": 2}, final=True)
    observer.finish(terminal=True)
    assert seen[-1][1]["complete"] is False
    assert seen[-1][1]["missing_reason"] == "invalid_usage"
    assert "private" not in repr(observer.raw)
    assert "private" not in repr(seen)


def test_observer_retains_only_known_raw_numeric_fields_and_copies_nested_details():
    observer, seen = make_observer()
    details = {"cached_tokens": 8, "unknown": "private details"}
    observer.observe({"prompt_tokens": 10, "completion_tokens": 2,
                      "prompt_tokens_details": details, "raw_text": "private response"}, final=True)
    details["cached_tokens"] = 999
    observer.finish(terminal=True)
    assert observer.raw["prompt_tokens_details"] == {"cached_tokens": 8}
    assert "private" not in repr(observer.raw)
    assert seen[-1][1]["cached_input_tokens"] == 8


@pytest.mark.parametrize("ordinal", [None, 0])
def test_observer_only_none_ordinal_disables_callback(ordinal):
    observer, seen = make_observer(ordinal=ordinal)
    observer.finish(terminal=True)
    assert len(seen) == (0 if ordinal is None else 1)
    if seen:
        assert seen[0][0] == 0


def test_observer_callback_failure_propagates_once_and_prevents_final_reemission():
    from pixiv_novel_sync.ai.provider_usage import RequestUsageObserver

    error = ValueError("local persistence unavailable")
    calls = []

    def fail(ordinal, usage):
        calls.append((ordinal, usage))
        raise error

    observer = RequestUsageObserver("openai_compatible", 1, fail)
    with pytest.raises(ValueError) as caught:
        observer.observe({"prompt_tokens": 10}, final=True)
    observer.finish(terminal=False, missing_reason="interrupted")
    assert caught.value is error
    assert len(calls) == 1

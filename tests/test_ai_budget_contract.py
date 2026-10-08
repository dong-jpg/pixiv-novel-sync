from __future__ import annotations

from pixiv_novel_sync.ai.model_router import estimate_token_count
from pixiv_novel_sync.ai.services.core import (
    _fit_route_messages, _fit_tail_text_messages, _drop_oldest_history_messages,
)


def tokens(messages):
    return sum(estimate_token_count(m["content"]) for m in messages)


def test_chinese_fixed_prompt_uses_tokens_not_bytes():
    messages = [{"role": "system", "content": "规" * 2000},
                {"role": "user", "content": "文" * 22000}]
    fitted = _fit_route_messages(messages, 4000)
    assert fitted[0] == messages[0]
    assert tokens(fitted) <= 4000
    assert len(fitted[1]["content"]) >= 3900


def test_tail_fitter_preserves_affordable_chinese_and_wrappers():
    build = lambda text: [{"role": "user", "content": "前缀" + text + "后缀"}]
    fitted = _fit_tail_text_messages(build, "文" * 22000, 15000)
    assert fitted == build("文" * 22000)


def test_six_round_history_is_not_dropped_for_byte_cost():
    messages = [{"role": "system", "content": "规则"}] + [
        {"role": role, "content": "文" * 1000}
        for _ in range(6) for role in ("user", "assistant")
    ] + [{"role": "user", "content": "继续"}]
    assert _drop_oldest_history_messages(messages, 9000) == messages


def test_split_accounts_for_wrappers_and_preserves_unicode():
    from pixiv_novel_sync.ai.services.core import _split_prompt_text
    text = "中文🙂abc" * 1000
    build = lambda part: [{"role": "system", "content": "规则" * 100},
                          {"role": "user", "content": "开始" + part + "结束"}]
    parts = _split_prompt_text(text, build, 200)
    assert "".join(parts) == text
    assert all(tokens(build(part)) <= 200 for part in parts)


def test_provider_fitter_checks_whole_prompt_and_keeps_input_unchanged():
    messages = [{"role": "system", "content": "fixed"}, {"role": "user", "content": "文" * 22000}]
    estimate = lambda ms: sum(len(m["content"]) for m in ms) * 2
    fitted = _fit_route_messages(messages, 1000, estimator=estimate)
    assert estimate(fitted) == 1000
    assert messages[-1]["content"] == "文" * 22000
    assert fitted[0] == messages[0]


def test_history_budget_removes_whole_turn_not_only_user():
    messages = [{'role':'system','content':'SS'}, {'role':'user','content':'UUUU'},
                {'role':'assistant','content':'AAAA'}, {'role':'user','content':'N'}]
    cost = lambda items: sum(len(m['content']) for m in items)
    assert _drop_oldest_history_messages(messages, 7, estimator=cost) == [messages[0], messages[-1]]


def test_odd_history_cap_does_not_keep_leading_assistant():
    messages = [{'role':'system','content':'SS'}, {'role':'assistant','content':'old answer'},
                {'role':'user','content':'new question'}, {'role':'assistant','content':'new answer'},
                {'role':'user','content':'now'}]
    result = _drop_oldest_history_messages(messages, 100)
    assert result == [messages[0], *messages[2:]]


def test_system_only_budget_never_truncates_fixed_rules():
    import pytest
    from pixiv_novel_sync.ai.services.core import AIServiceError
    messages = [{'role':'system','content':'SSSSSSSS'}]
    with pytest.raises(AIServiceError):
        _drop_oldest_history_messages(messages, 7, estimator=lambda ms: sum(len(m['content']) for m in ms))
    assert messages[0]['content'] == 'SSSSSSSS'


def test_history_recomputes_message_overhead_after_dropping_turns():
    messages = [{'role':'system','content':'S'*700}] + [
        {'role':role,'content':'H'*10} for _ in range(6) for role in ('user','assistant')
    ] + [{'role':'user','content':'N'*20}]
    cost = lambda items: sum(len(m['content']) for m in items)
    budget = lambda items: min(738, 2000-1000-256-(4*len(items)+2))
    result = _drop_oldest_history_messages(messages, budget(messages), estimator=cost, budget_for_messages=budget)
    assert result[0] == messages[0]
    assert result[-1] == messages[-1]
    assert cost(result) <= budget(result)

from __future__ import annotations

from pixiv_novel_sync.ai.prompts import DEFAULT_WIZARD_PROMPT, build_chat_messages, build_wizard_prompt


def test_default_wizard_prompt_keeps_import_marker():
    assert "<<<READY_FOR_IMPORT>>>" in DEFAULT_WIZARD_PROMPT
    assert '"project"' in DEFAULT_WIZARD_PROMPT


def test_build_chat_messages_uses_default_wizard_prompt():
    messages = build_chat_messages(system_prompt=None, history=[], user_message="帮我整理设定")

    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == DEFAULT_WIZARD_PROMPT
    assert messages[-1]["content"] == "帮我整理设定"


def test_build_wizard_prompt_supports_general_genre():
    prompt = build_wizard_prompt("general", extra_prompt="额外规则")

    assert "通用长篇" in prompt
    assert "额外规则" in prompt

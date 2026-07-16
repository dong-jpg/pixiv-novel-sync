from __future__ import annotations

from pixiv_novel_sync.ai.prompts import (
    DEFAULT_WIZARD_PROMPT,
    build_chat_messages,
    build_longform_detail_messages,
    build_longform_plan_messages,
    build_wizard_prompt,
)


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


def test_longform_prompts_include_project_style_constraint():
    project = {"name": "测试项目", "description": "简介", "settings": {}}
    plan_messages = build_longform_plan_messages(
        system_prompt=None,
        project=project,
        target_words=100_000,
        style_prompt="项目风格约束",
    )
    detail_messages = build_longform_detail_messages(
        system_prompt=None,
        project=project,
        longform_plan={"project_outline": "总纲", "chapters": []},
        chapters=[{"chapter_number": 1, "title": "第一章", "outline": "开篇"}],
        style_prompt="项目风格约束",
    )

    assert "项目风格约束" in plan_messages[-1]["content"]
    assert "项目风格约束" in detail_messages[-1]["content"]

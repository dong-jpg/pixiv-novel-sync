from __future__ import annotations

from typing import Any


DEFAULT_CONTINUE_PROMPT = """你是专业中文小说续写助手。
你的任务是根据用户提供的上下文继续写正文。
规则：
1. 你要续写，不要总结，不要解释。
2. 保持人物设定、叙述视角、语气和文风。
3. 不要突然跳剧情，不要随意引入新角色或重大设定。
4. 不要输出标题、列表、分析或写作说明。
5. 只输出续写后的小说正文。"""


DEFAULT_REWRITE_PROMPT = """你是专业中文小说改写助手。
你的任务是按用户要求改写文本。
规则：
1. 保留原剧情事实和关键信息。
2. 不新增重大事件，不删除关键情节。
3. 按用户指定的改写目标调整表达。
4. 不要解释修改过程。
5. 只输出改写后的正文。"""


def build_continue_messages(
    *,
    system_prompt: str | None,
    context: str,
    instruction: str | None = None,
    output_chars: int | None = None,
    style_prompt: str | None = None,
    novel_prompt: str | None = None,
) -> list[dict[str, str]]:
    parts = []
    if style_prompt:
        parts.append(f"【风格要求】\n{style_prompt}")
    if novel_prompt:
        parts.append(f"【小说设定与连续性要求】\n{novel_prompt}")
    if instruction:
        parts.append(f"【用户指令】\n{instruction}")
    if output_chars:
        parts.append(f"【输出长度】\n约 {output_chars} 字。")
    parts.append(f"【待续写上下文】\n{context}")
    return [
        {"role": "system", "content": system_prompt or DEFAULT_CONTINUE_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def build_rewrite_messages(
    *,
    system_prompt: str | None,
    text: str,
    rewrite_type: str | None = None,
    instruction: str | None = None,
) -> list[dict[str, str]]:
    parts = []
    if rewrite_type:
        parts.append(f"【改写类型】\n{rewrite_type}")
    if instruction:
        parts.append(f"【用户指令】\n{instruction}")
    parts.append(f"【原文】\n{text}")
    return [
        {"role": "system", "content": system_prompt or DEFAULT_REWRITE_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def safe_prompt_preview(messages: list[dict[str, Any]], max_chars: int = 1000) -> str:
    text = "\n\n".join(str(message.get("content", "")) for message in messages)
    return text[:max_chars]

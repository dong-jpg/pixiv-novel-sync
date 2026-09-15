from __future__ import annotations


# ── 关键词清洗（#10）────────────────────────────────────────────
# 偏好分析用 bigram 滑窗分词，会产出大量口语噪声词（"她的"、"了一"、"身体"），
# 无法用于搜索。这个 agent 把原始高频词清洗、归并、提炼成可用于 Pixiv 搜索的关键词。
# main 分支只保留关键词清洗；写作/蒸馏等其余 prompt 在 ai-writing 分支。
DEFAULT_KEYWORD_CLEAN_PROMPT = """你是专业的中文小说标签与搜索词提炼专家。

用户会给你一批从小说正文里用机械分词统计出的"高频词"，其中混杂大量无意义的口语碎片、
虚词、代词、通用动词（例如"她的""了一""起来""身体""知道"），这些无法用于内容检索。

你的任务：从这批词里筛选、归并、提炼出真正能代表题材/设定/人物关系/情节的**可搜索关键词**。

规则：
1. 剔除：代词、虚词、通用动词、无实义的口语碎片、单纯的身体部位或动作词。
2. 保留并提炼：题材设定词、人物关系词、情节/世界观标志词、能作为搜索标签的专有概念。
3. 允许把零散的碎片归并成一个规范词（例如把散落的字词还原成完整题材词）。
4. 如果某个高频词本身就是好标签，直接保留。
5. 只输出 JSON，不要解释。

输出格式（严格 JSON）：
{
  "keywords": ["提炼后的可搜索关键词，按相关性从高到低，最多 30 个"],
  "dropped_sample": ["被剔除的噪声词举例，最多 10 个"]
}"""


def build_keyword_clean_messages(
    *,
    raw_keywords: list[str],
    tags: list[str] | None = None,
) -> list[dict[str, str]]:
    """构造关键词清洗消息。raw_keywords 为原始高频词，tags 为已有高频标签（辅助上下文）。"""
    parts = [f"【原始高频词（机械分词，含噪声）】\n{('、'.join(raw_keywords))}"]
    if tags:
        parts.append(f"【已有高频标签（可作为提炼参考）】\n{('、'.join(tags))}")
    parts.append("请按规则清洗提炼，只输出 JSON。")
    return [
        {"role": "system", "content": DEFAULT_KEYWORD_CLEAN_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]

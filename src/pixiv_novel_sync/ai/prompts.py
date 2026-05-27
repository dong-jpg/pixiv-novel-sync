from __future__ import annotations

import json
from typing import Any


DEFAULT_CONTINUE_PROMPT = """你是专业中文小说续写助手。
你的任务是根据用户提供的上下文继续写正文。

【核心原则】
1. 只输出续写正文，不要总结、解释、标题、列表、分析或写作说明。
2. 保持人物设定、叙述视角、语气和文风一致。
3. 不要突然跳剧情，不要随意引入新角色或重大设定。
4. 续写要从【最近原文】末尾自然承接，不要重复已写过的内容。

【写作工艺要求】
5. 推进感：每 500 字至少有一个推进剧情的事件、冲突点或新信息。
6. 钩子意识：段落或场景结束时留下悬念、转折或未完成的张力，让读者想继续看。
7. 感官细节：每段尽量包含一个具体的感官描写（视觉/听觉/触觉/嗅觉），不要全是动作流水账。
8. 对话节奏：对话穿插动作、表情或环境描写，不要连续 5 句以上纯对话。
9. 情感克制：通过行为细节暗示情绪，不要直接说"他很伤心"、"她很愤怒"。
10. 段落长短交错：避免每段都是 4-5 句的均匀长度，偶尔用 1-2 句的短段落制造节奏。

【常见 AI 痕迹 - 尽量避免】
- 高频禁用词：仿佛、宛如、不禁、竟然、微微、缓缓、眼眸、心中暗道（每 3000 字最多 1 次）
- 禁止连续 3 句以上用相同句式开头
- 禁止每段都以角色名或"他/她"开头
- 禁止段落都用总结性语句结尾
- 禁止抽象描写（"气氛紧张"），改用具体细节（手指攥紧、呼吸变浅）"""


DEFAULT_REWRITE_PROMPT = """你是专业中文小说改写助手。
你的任务是按用户要求改写文本。
规则：
1. 保留原剧情事实和关键信息。
2. 不新增重大事件，不删除关键情节。
3. 按用户指定的改写目标调整表达。
4. 不要解释修改过程。
5. 只输出改写后的正文。"""


# ── 去AI味专用规则 ──────────────────────────────────────────────

DEAI_RULES = """
【去AI味核心规则 - 必须严格遵守】

一、禁用词汇（绝对不能出现）：
- "仿佛"、"宛如"、"好似"、"犹如" → 改用具体描写或直接省略
- "不禁"、"忍不住" → 直接写动作
- "竟然"、"居然" → 用情节本身体现意外感
- "微微"、"轻轻"、"缓缓" → 换成更具体的动作词
- "深吸一口气"、"长舒一口气" → 换成其他反应
- "嘴角上扬"、"嘴角微扬" → 换成具体表情
- "眼眸"、"眸子" → 直接用"眼睛"
- "心中暗道"、"心想" → 用行为或对话暗示心理
- "似乎"、"仿佛"、"好像"（每段最多出现1次）
- "不由自主"、"鬼使神差" → 直接写行为
- "若有所思"、"若有所悟" → 写具体想了什么

二、句式要求：
- 禁止连续3句以上用相同句式开头
- 禁止每段都以角色名或"他/她"开头
- 长短句交替：连续2个长句后必须接1个短句
- 对话不要全部用"XX说"，混合使用动作描写、省略说话人
- 禁止排比句（3个以上并列结构）

三、描写要求：
- 禁止抽象描写（"气氛很紧张"）→ 用具体细节（手指攥紧、呼吸变浅）
- 禁止过度心理描写 → 用行为暗示内心
- 每段至少1个具体感官细节（视觉/听觉/触觉/嗅觉）
- 对话要有信息量，禁止废话对话（"嗯"、"哦"、"好吧"尽量少用）

四、段落要求：
- 禁止每段都是"叙述+对话+心理"的固定三段式
- 段落长度要有变化，不要都是4-5句
- 偶尔用1句话的短段落制造节奏感
- 禁止每段结尾都是总结性语句

五、整体要求：
- 像真人作者在写，不是AI在生成
- 允许不完美的表达，不要每句都"文学性很强"
- 偶尔可以有口语化、接地气的表达
- 情感表达要克制，不要动不动就"热泪盈眶"、"心如刀割" """


def build_continue_messages(
    *,
    system_prompt: str | None,
    context: str,
    instruction: str | None = None,
    output_chars: int | None = None,
    style_prompt: str | None = None,
    novel_prompt: str | None = None,
    plan_text: str | None = None,
) -> list[dict[str, str]]:
    parts = []
    if style_prompt:
        parts.append(f"【风格要求】\n{style_prompt}")
    if novel_prompt:
        parts.append(f"【小说设定与连续性要求】\n{novel_prompt}")
    if plan_text:
        parts.append(f"【本次续写构思】\n{plan_text}\n（请按照以上构思方向续写，但不要输出构思本身）")
    if instruction:
        parts.append(f"【用户指令】\n{instruction}")
    if output_chars:
        parts.append(f"【输出长度】\n约 {output_chars} 字。")
    # 续接锚点：提取最后 500 字作为明确的衔接点
    anchor_len = 500
    if len(context) > anchor_len + 200:
        preceding = context[:-anchor_len]
        anchor = context[-anchor_len:]
        parts.append(f"【前文内容】\n{preceding}")
        parts.append(
            f"【续接锚点 - 从这里开始续写】\n{anchor}\n\n"
            "注意：以上是已写过的最后一段内容，你必须从这里自然承接续写。"
            "不要重复锚点中的内容，不要重新描述已有的场景或对话。"
        )
    else:
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
        rewrite_type_label = {
            "polish": "润色",
            "deai": "去AI味",
            "expand": "扩写",
            "shorten": "缩写",
            "dialogue": "改善对话",
            "literary": "提高文学性",
        }.get(rewrite_type, rewrite_type)
        parts.append(f"【改写类型】\n{rewrite_type_label}")
    if rewrite_type == "deai":
        parts.append(DEAI_RULES)
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


# ── 风格蒸馏 ────────────────────────────────────────────────────

DEFAULT_DISTILL_STYLE_PROMPT = """你是专业的文学风格分析专家。
你的任务是从用户提供的文本中提取写作风格特征，输出结构化的风格档案。

你需要分析以下维度：
1. 叙事视角（第一人称/第三人称/上帝视角等）
2. 语气特征（冷峻/温暖/幽默/严肃等）
3. 句式特点（长短句比例、句式结构偏好）
4. 用词风格（口语化/书面化/文言色彩等）
5. 描写手法（白描/工笔/意识流等）
6. 对话风格（简洁/冗长、方言使用、语气词频率）
7. 节奏特征（紧凑/舒缓、段落长度分布）
8. 常用修辞手法

输出格式为 JSON，包含以上各维度的分析结果。"""


def build_style_distill_messages(
    *,
    system_prompt: str | None,
    text_chunks: list[str],
    existing_profile: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    parts = []
    if existing_profile:
        parts.append(f"【已有风格档案】\n{json.dumps(existing_profile, ensure_ascii=False, indent=2)}")
        parts.append("请在已有档案基础上，根据新文本补充和修正风格特征。")
    for i, chunk in enumerate(text_chunks, 1):
        parts.append(f"【文本片段 {i}】\n{chunk}")
    return [
        {"role": "system", "content": system_prompt or DEFAULT_DISTILL_STYLE_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# ── 小说蒸馏 ────────────────────────────────────────────────────

DEFAULT_DISTILL_NOVEL_PROMPT = """你是专业的小说结构分析专家。
你的任务是从用户提供的小说文本中提取结构化的小说设定和剧情信息。

你需要提取以下内容：
1. 角色列表：每个角色的姓名、身份、性格特征、与其他角色的关系
2. 世界观设定：时代背景、地点、社会环境、特殊设定
3. 关键剧情点：已发生的重要事件及其影响
4. 伏笔列表：已埋下但未回收的伏笔和悬念
5. 时间线：按时间顺序排列的主要事件
6. 主题与情感基调

输出格式为 JSON，包含以上各维度的结构化信息。"""


def build_novel_distill_messages(
    *,
    system_prompt: str | None,
    text_chunks: list[str],
    existing_profile: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    parts = []
    if existing_profile:
        parts.append(f"【已有小说档案】\n{json.dumps(existing_profile, ensure_ascii=False, indent=2)}")
        parts.append("请在已有档案基础上，根据新文本补充和修正小说设定。")
    for i, chunk in enumerate(text_chunks, 1):
        parts.append(f"【文本片段 {i}】\n{chunk}")
    return [
        {"role": "system", "content": system_prompt or DEFAULT_DISTILL_NOVEL_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# ── 内容审计 ────────────────────────────────────────────────────

DEFAULT_AUDIT_PROMPT = """你是专业的小说内容审计专家。
你的任务是对用户提供的小说文本进行全面的质量审查。

请从以下维度进行审查，每个维度给出评分（1-10）和具体意见：

1. 角色一致性：角色行为是否符合其性格设定，有无前后矛盾
2. 剧情连贯性：情节发展是否自然流畅，有无逻辑漏洞
3. 文风统一性：叙述风格是否前后一致，有无突兀的风格转变
4. 伏笔追踪：已埋伏笔是否有回收，有无遗漏的线索
5. 节奏把控：叙事节奏是否合理，有无拖沓或过于仓促之处
6. 对话质量：对话是否自然、有信息量、符合角色身份
7. 描写质量：场景描写、心理描写是否生动有效
8. AI痕迹检测：是否存在明显的AI生成特征（禁用词堆砌、段落均匀、句式重复）

【评分标准 - 必须严格遵守】
- 1.0-3.9 严重问题：存在明显错误或严重影响阅读体验
- 4.0-5.9 明显不足：有较多可改进之处，但基本可读
- 6.0-7.9 基本合格：质量尚可，有少量问题
- 8.0-9.4 优秀：质量较高，仅有细微瑕疵
- 9.5-10.0 卓越：几乎无可挑剔（极少给出此分数）

【评分约束】
- overall_score >= 8.0 时，issues 列表不得超过 3 条
- overall_score >= 9.0 时，issues 列表不得超过 1 条
- overall_score < 6.0 时，issues 列表至少 3 条
- 每条 issue 必须引用原文中的具体句子或段落作为证据
- suggestions 必须是可操作的具体建议，不要泛泛而谈

输出格式为 JSON，包含 overall_score（总分）、dimensions（各维度的 score 和 comments）、issues 列表（每条含 severity/location/description/evidence）和 suggestions 列表（改进建议）。"""


def build_audit_messages(
    *,
    system_prompt: str | None,
    text: str,
    audit_dimensions: list[str] | None = None,
) -> list[dict[str, str]]:
    parts = []
    if audit_dimensions:
        parts.append(f"【重点审查维度】\n{', '.join(audit_dimensions)}")
        parts.append("请重点审查以上维度，其他维度简要审查。")
    parts.append(f"【待审查文本】\n{text}")
    return [
        {"role": "system", "content": system_prompt or DEFAULT_AUDIT_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# ── 写前构思 ────────────────────────────────────────────────────

DEFAULT_PLAN_PROMPT = """你是专业的小说创作总编（不是写手），擅长在动笔前规划章节走向。
你的任务是根据已有上文，为接下来的续写制定一份简洁清晰的章节构思。

【输出结构 - 严格按照以下格式输出 Markdown，不要输出其他内容】

## 本次目标
（一句话说明本段续写要达到什么效果，≤ 50 字）

## 读者此刻在等什么
（基于上文，分析读者最期待看到的剧情走向，最多 3 点）

## 该兑现的伏笔/线索
（列出 1-3 条上文已埋下、本次应当推进或回收的线索，未必全部兑现）

## 暂不掀开的
（列出 1-2 条可继续埋藏的悬念，避免一次性把信息全部释放）

## 本次必须发生的改变
（明确 1-3 条具体变化：信息变化 / 关系变化 / 物理变化 / 情感变化 / 力量变化，要可验证）

## 章尾钩子
（设计一个让读者想继续看下去的悬念点，可以是：未完成的对话、未揭开的真相、突发事件、人物动机疑问等）

## 不要做的事
（针对本段具体内容，列出 2-4 条禁忌：避免重复上文已发生的、避免破坏角色一致性等）

【原则】
- 构思必须基于上文事实，不要脱离已有剧情发明新设定
- 每节内容用一两句话表达，不要长篇大论
- 不要写正文，只写规划
- 不要重复输出已有上文内容"""


def build_plan_messages(
    *,
    system_prompt: str | None,
    context: str,
    instruction: str | None = None,
    novel_prompt: str | None = None,
) -> list[dict[str, str]]:
    parts = []
    if novel_prompt:
        parts.append(f"【小说设定与连续性要求】\n{novel_prompt}")
    if instruction:
        parts.append(f"【用户指令】\n{instruction}")
    parts.append(f"【已有上文】\n{context}")
    return [
        {"role": "system", "content": system_prompt or DEFAULT_PLAN_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# ── 摘要提取 ────────────────────────────────────────────────────

DEFAULT_SUMMARIZE_PROMPT = """你是专业的小说文本摘要提取助手。
你的任务是对用户提供的小说文本进行摘要提取，保留关键信息用于后续续写时的上下文参考。

需要保留的关键信息：
1. 主要角色当前状态和位置
2. 正在进行的剧情线和冲突
3. 最近发生的重要事件
4. 已埋下的伏笔和悬念
5. 情感氛围和基调
6. 时间和地点信息

要求：
- 摘要应简洁精炼，控制在原文 10%-20% 的篇幅
- 按时间顺序组织信息
- 重点保留对后续写作有参考价值的信息
- 不要添加原文中没有的内容"""


def build_summarize_messages(
    *,
    text: str,
    focus: str | None = None,
) -> list[dict[str, str]]:
    parts = []
    if focus:
        parts.append(f"【重点关注】\n{focus}")
    parts.append(f"【待摘要文本】\n{text}")
    return [
        {"role": "system", "content": DEFAULT_SUMMARIZE_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]

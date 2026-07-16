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


# ── 项目级风格控制（#14）────────────────────────────────────────
# 滑块维度：0-100。低/中/高分别渲染成不同强度的中文写作指令。
# 只有明显偏离中间值（<35 或 >65）才注入指令，避免中庸值污染 prompt。
STYLE_SLIDER_SPECS: dict[str, dict[str, Any]] = {
    "explicitness": {
        "label": "情色露骨度",
        "low": "情欲场面点到为止，以暗示、留白和情绪张力为主，避免直接的器官与动作描写。",
        "high": "情欲场面写得直接露骨，细致描写身体、动作与感官细节，不回避直白的性爱描写。",
    },
    "lyricism": {
        "label": "抒情浓度",
        "low": "行文克制冷静，少用比喻和抒情，以动作和对话推进，语言干净利落。",
        "high": "行文抒情唯美，注重意象、情绪渲染和氛围营造，适度使用比喻与细腻的心理描写。",
    },
    "pacing": {
        "label": "节奏",
        "low": "放慢节奏，铺陈细节与情绪，给场景和人物心理充分展开的空间。",
        "high": "加快节奏，情节紧凑，冲突和转折密集，减少冗余铺垫。",
    },
    "darkness": {
        "label": "黑暗/压抑度",
        "low": "基调明亮温暖，即便有冲突也保留希望感和治愈色彩。",
        "high": "基调黑暗压抑，不回避痛苦、扭曲和沉重的情感，营造强烈的宿命感或绝望氛围。",
    },
    "vulgarity": {
        "label": "粗俗/口语度",
        "low": "用词讲究，保持书面语的雅致，避免粗口和低俗表达。",
        "high": "允许粗口、脏话和市井口语，让对话和叙述更生猛、接地气、有街头感。",
    },
}


def compose_style_control_prompt(style_control: dict[str, Any] | None) -> str | None:
    """把项目级风格控制（滑块 + 标签 + 自定义）渲染成一段中文写作指令。

    返回 None 表示无有效控制（全部滑块处于中庸区间且无标签/自定义）。
    这段文本会拼进 build_continue_messages 的 style_prompt，从初期即控制生成风格。
    """
    if not isinstance(style_control, dict):
        return None
    lines: list[str] = []

    sliders = style_control.get("sliders")
    if isinstance(sliders, dict):
        for key, spec in STYLE_SLIDER_SPECS.items():
            raw = sliders.get(key)
            if raw is None or raw == "":
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value < 35:
                lines.append(f"- {spec['label']}：{spec['low']}")
            elif value > 65:
                lines.append(f"- {spec['label']}：{spec['high']}")
            # 35-65 视为中庸，不注入指令

    tags = style_control.get("tags")
    if isinstance(tags, list):
        clean_tags = [str(t).strip() for t in tags if str(t).strip()]
        if clean_tags:
            lines.append(f"- 风格标签：{('、'.join(clean_tags))}（请让文本贴合这些标签的调性）")

    custom = style_control.get("custom")
    if isinstance(custom, str) and custom.strip():
        lines.append(f"- 额外要求：{custom.strip()}")

    if not lines:
        return None
    return "【本作风格设定 - 请贯穿全文严格遵守】\n" + "\n".join(lines)


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
    existing_profile: dict[str, Any] | str | None = None,
) -> list[dict[str, str]]:
    parts = []
    if existing_profile:
        if isinstance(existing_profile, dict):
            profile_text = json.dumps(existing_profile, ensure_ascii=False, indent=2)
        else:
            profile_text = str(existing_profile)
        parts.append(f"【已有风格档案】\n{profile_text}")
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
    existing_profile: dict[str, Any] | str | None = None,
) -> list[dict[str, str]]:
    parts = []
    if existing_profile:
        if isinstance(existing_profile, dict):
            profile_text = json.dumps(existing_profile, ensure_ascii=False, indent=2)
        else:
            profile_text = str(existing_profile)
        parts.append(f"【已有小说档案】\n{profile_text}")
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
    rule_detection_context: str | None = None,
) -> list[dict[str, str]]:
    parts = []
    if audit_dimensions:
        parts.append(f"【重点审查维度】\n{', '.join(audit_dimensions)}")
        parts.append("请重点审查以上维度，其他维度简要审查。")
    if rule_detection_context:
        parts.append(rule_detection_context)
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


DEFAULT_LONGFORM_PLAN_PROMPT = """你是专业中文长篇小说总编，负责在正式写正文前制定全书规划。

你的任务：根据目标总字数、项目信息和用户要求，生成可直接用于后续扩写章节梗概的结构化长篇规划。

【必须输出严格 JSON】
只输出一个 JSON 对象，不要 markdown，不要解释，不要代码块。结构如下：
{
  "project_outline": "全书总纲，说明主线冲突、阶段结构、人物变化、结局方向",
  "target_words": 500000,
  "expected_chapter_count": 120,
  "average_chapter_words": 4200,
  "structure_notes": "说明为什么这样拆分章节、每个阶段大约多少字、如何扩展剧情容量",
  "volumes": [
    {
      "volume_number": 1,
      "title": "阶段名",
      "chapter_range": "1-30",
      "target_words": 120000,
      "core_conflict": "本阶段核心冲突",
      "turning_points": ["关键转折1", "关键转折2"]
    }
  ],
  "chapters": [
    {
      "chapter_number": 1,
      "title": "章节标题",
      "outline": "概要大纲：本章目标、关键事件、人物变化、伏笔、结尾钩子",
      "detailed_outline": "",
      "target_words": 4000,
      "volume_number": 1,
      "story_function": "开局/铺垫/转折/高潮/回收/过渡",
      "key_events": ["事件1", "事件2"],
      "foreshadow_refs": ["伏笔描述或编号"]
    }
  ],
  "foreshadows": [
    {
      "description": "伏笔描述",
      "planted_chapter": 1,
      "target_resolve_chapter": 40,
      "importance": "normal"
    }
  ]
}

【规划规则】
- 不要写正文，只规划。
- target_words 是核心约束；如果用户给了目标总字数，必须围绕总字数设计剧情容量、阶段结构和章节数。
- expected_chapter_count 是你最终估算出的章节数，不是简单复述用户输入。
- 用户给出的章节数只是参考；如果与目标总字数或剧情节奏冲突，可以调整，并在 structure_notes 中说明。
- 每章 outline 只写概要大纲，控制在 80-180 字；不要在第一阶段写详细章节梗概。
- detailed_outline 初始留空字符串，后续由“详细梗概扩写”步骤填充。
- 每章 title 必须具体，不要使用“第一章”“第二章”这种空泛标题。
- 每章 target_words 总和应尽量接近 target_words，允许 5%-10% 误差。
- 超长篇必须拆成 volumes/阶段，避免章节列表像流水账。
- 如果项目已有大纲或已有章节，要在其基础上续接，不要推翻已存在内容。
"""


def build_longform_plan_messages(
    *,
    system_prompt: str | None,
    project: dict[str, Any],
    chapters: list[dict[str, Any]] | None = None,
    instruction: str | None = None,
    target_words: int | None = None,
    expected_chapters: int | None = None,
    chapter_words_reference: int | None = None,
    style_prompt: str | None = None,
) -> list[dict[str, str]]:
    parts = []
    parts.append(f"【项目名称】\n{project.get('name') or '未命名作品'}")
    if project.get("description"):
        parts.append(f"【项目简介】\n{project.get('description')}")
    if project.get("outline"):
        outline = project.get("outline")
        if not isinstance(outline, str):
            outline = json.dumps(outline, ensure_ascii=False, indent=2)
        parts.append(f"【已有全书大纲】\n{outline}")
    settings = project.get("settings") or {}
    if settings:
        parts.append(f"【项目设置/素材】\n{json.dumps(settings, ensure_ascii=False, indent=2)}")
    if chapters:
        chapter_lines = []
        for ch in chapters:
            chapter_lines.append(
                f"第{ch.get('chapter_number')}章：{ch.get('title') or '未命名'}\n"
                f"大纲：{ch.get('outline') or '（无）'}\n"
                f"正文字数：{ch.get('word_count') or 0}"
            )
        parts.append("【已有章节】\n" + "\n\n".join(chapter_lines))
    if target_words:
        parts.append(f"【目标总字数】\n全书目标约 {target_words} 字。请以此为核心规划全书容量、章节数和剧情扩展密度。")
    if expected_chapters:
        parts.append(f"【章节数参考】\n用户参考值约 {expected_chapters} 章；如与目标总字数或剧情节奏冲突，可自行调整并在 structure_notes 说明。")
    if chapter_words_reference:
        parts.append(f"【单章字数参考】\n用户希望单章约 {chapter_words_reference} 字；可根据高潮/过渡章节上下浮动。")
    if instruction:
        parts.append(f"【用户规划要求】\n{instruction}")
    if style_prompt:
        parts.append(f"【全书风格约束】\n{style_prompt}")
    return [
        {"role": "system", "content": system_prompt or DEFAULT_LONGFORM_PLAN_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


DEFAULT_LONGFORM_DETAIL_PROMPT = """你是专业中文小说章节统筹编辑，负责把每章概要大纲扩展为可供正文生成 AI 使用的详细章节梗概。

【任务】
根据全书规划、全部章节概要和待扩写章节，把待扩写章节的 outline 扩展为 detailed_outline。

【必须输出严格 JSON】
只输出一个 JSON 对象，不要 markdown，不要解释，不要代码块。结构如下：
{
  "chapters": [
    {
      "chapter_number": 1,
      "detailed_outline": "详细章节梗概：场景顺序、人物行动、冲突推进、情绪变化、信息揭示、伏笔处理、结尾钩子。不要写正文。",
      "scene_beats": [
        {
          "scene": "场景名或地点",
          "purpose": "场景功能",
          "events": ["事件1", "事件2"],
          "emotional_shift": "人物情绪变化",
          "hook": "本场景或章尾钩子"
        }
      ],
      "writing_notes": "给正文生成 AI 的注意事项"
    }
  ]
}

【规则】
- 不要写正文，只写详细梗概。
- 不要改变章节编号、标题、目标字数和主线走向。
- 必须基于概要 outline 扩展，不要脱离全书规划。
- detailed_outline 要足够指导后续正文生成，包含场景顺序、关键事件、人物状态变化、冲突推进、伏笔埋设/回收、章尾钩子。
- 详细程度按 target_words 调整：目标字数越多，梗概越细。
"""


def build_longform_detail_messages(
    *,
    system_prompt: str | None,
    project: dict[str, Any],
    longform_plan: dict[str, Any],
    chapters: list[dict[str, Any]],
    instruction: str | None = None,
    style_prompt: str | None = None,
) -> list[dict[str, str]]:
    all_chapters = longform_plan.get("chapters") or []
    chapter_lines = []
    for ch in all_chapters:
        chapter_lines.append(
            f"第{ch.get('chapter_number')}章 {ch.get('title') or '未命名'}\n"
            f"目标字数：{ch.get('target_words') or '-'}\n"
            f"概要：{ch.get('outline') or '（无）'}"
        )
    target_lines = []
    for ch in chapters:
        target_lines.append(
            f"第{ch.get('chapter_number')}章 {ch.get('title') or '未命名'}\n"
            f"目标字数：{ch.get('target_words') or '-'}\n"
            f"概要：{ch.get('outline') or '（无）'}"
        )
    parts = [
        f"【项目名称】\n{project.get('name') or '未命名作品'}",
        f"【项目简介】\n{project.get('description') or '（无）'}",
        f"【全书总纲】\n{longform_plan.get('project_outline') or project.get('outline') or '（无）'}",
    ]
    if longform_plan.get("structure_notes"):
        parts.append(f"【结构拆分说明】\n{longform_plan.get('structure_notes')}")
    if longform_plan.get("volumes"):
        parts.append(f"【卷/阶段结构】\n{json.dumps(longform_plan.get('volumes'), ensure_ascii=False, indent=2)}")
    parts.append("【全部章节概要】\n" + "\n\n".join(chapter_lines))
    parts.append("【本次需要扩写的章节】\n" + "\n\n".join(target_lines))
    if instruction:
        parts.append(f"【扩写要求】\n{instruction}")
    if style_prompt:
        parts.append(f"【全书风格约束】\n{style_prompt}")
    return [
        {"role": "system", "content": system_prompt or DEFAULT_LONGFORM_DETAIL_PROMPT},
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


# ── 创作向导（多轮对话） ─────────────────────────────────────────

WIZARD_BASE_PROMPT = """你是一名资深小说创作编辑，正在和作者展开「前期创作素材产出」对话。
目标是把作者零散的设定、人物、剧情、风格偏好整理成系统能直接导入的长篇小说素材包。

【交付目标 - 你必须主动引导作者把以下 7 块素材填齐】
1. 一句话梗概（≤50 字）
2. 核心卖点（3-5 条）
3. 人物设定（主角必须有：年龄、外貌、性格、背景、心理创伤、行为习惯、关系动机；配角列表 3-6 个）
4. 全书总纲（分册结构、各册核心事件，建议总字数与章节数）
5. 详细大纲（按事件块/剧情段，每段对应章节区间和关键转折）
6. 主角变化曲线（按章节区间列出外部表现、内部心理、触发事件）
7. 写作规范（人物语言档案、特殊台词体系、写作红线、视角与基调）

【对话风格】
- 直接、简洁、像编辑跟作者沟通，不要客套与空泛评价
- 作者输入碎片化或不专业时，你主动补全：把碎片转译为可写的剧情节奏、人物动机、场景细节
- 给具体人物姓名/品牌/地点等占位时，明确告诉作者「建议改为 XX」
- 每轮回复结尾给作者两个选项：A) 继续填充哪一块；B) 你还可以补什么

【输出格式 - 重要】
- 每次回复尽量按 markdown 节段输出已确定的内容，节段标题固定使用：
  `## 一句话梗概`、`## 核心卖点`、`## 人物设定 - 男主`、`## 人物设定 - 女主`、`## 配角表`、
  `## 分册结构`、`## 剧情节点总览`、`## 详细大纲（第N册）`、
  `## 主角变化曲线`、`## 写作规范 - 人物语言档案`、`## 写作规范 - 特殊台词体系`、`## 写作规范 - 红线`、`## 开篇示范`
- 节段内允许任意 markdown 内容（表格、列表、引文均可）
- 节段之间用空行分隔
- 同名节段如果重写，会覆盖旧版本
- 每次只输出本轮新增或修改的节段，不要每次都把全部素材重打一遍

【何时触发导入】
当作者明确表示「差不多了/可以开始写了/导入吧」，或者你判断 7 块素材已经齐备时，
在最后一行单独输出：

<<<READY_FOR_IMPORT>>>

紧跟一个 ```json 代码块，包含完整结构化数据，结构如下：

```json
{
  "project": {
    "name": "书名",
    "description": "一句话梗概",
    "outline": "全书总纲文本（可含 markdown）",
    "settings": {
      "core_selling_points": ["..."],
      "characters": {
        "male_lead": {"name": "...", "age": 0, "traits": "...", "background": "..."},
        "female_lead": {"name": "...", "age": 0, "traits": "...", "background": "..."},
        "supporting": [{"name": "...", "role": "...", "relation": "..."}]
      },
      "structure": {
        "books": [{"index": 1, "name": "...", "word_count": 0, "chapter_range": "...", "core_events": "..."}]
      },
      "progress_curve": [
        {"stage_percent": 0, "chapter_range": "...", "external": "...", "internal": "...", "trigger": "..."}
      ],
      "writing_rules": {
        "language_profile": "...",
        "special_dialogue_system": "...",
        "red_lines": ["..."],
        "narrative_pov": "...",
        "tone": "..."
      },
      "opening_sample": "（可选）开篇示范文本"
    }
  },
  "chapters": [
    {"chapter_number": 1, "title": "...", "outline": "本章构思..."}
  ],
  "foreshadows": [
    {"description": "...", "planted_chapter": 1, "target_resolve_chapter": 50, "importance": "high"}
  ]
}
```

【绝对禁止】
- 不要每轮都重复发问「还有什么补充」，要主动推进，给具体建议供作者点头/否决
- 不要写整章正文（除非作者要求「开篇示范」），你的任务是产出素材而不是代写
- 不要在 JSON 块外混杂额外的 ``` 代码块（会让前端解析混乱）
"""

WIZARD_GENRE_PROMPTS = {
    "default": """【题材模板 - 商业情感长篇】
- 重点关注长篇都市/情感拉扯/虐恋甜文/反差变化类题材
- 对作者的个人化创作偏好保持中立尊重，并转化为人物动机、关系张力、场景功能和章节节奏
- 需要额外整理关系推进曲线、主角变化曲线、情绪强度变化和写作红线""",
    "general": """【题材模板 - 通用长篇】
- 不预设题材，优先追问类型、主角、核心冲突、读者期待和篇幅目标
- 需要把作者偏好转化为明确的剧情承诺、人物弧光和章节推进规则""",
}


def build_wizard_prompt(genre: str = "default", extra_prompt: str | None = None) -> str:
    genre_prompt = WIZARD_GENRE_PROMPTS.get(genre) or WIZARD_GENRE_PROMPTS["default"]
    parts = [WIZARD_BASE_PROMPT, genre_prompt]
    if extra_prompt:
        parts.append(str(extra_prompt).strip())
    return "\n\n".join(part for part in parts if part)


DEFAULT_WIZARD_PROMPT = build_wizard_prompt()



def build_chat_messages(
    *,
    system_prompt: str | None,
    history: list[dict[str, str]],
    user_message: str,
    user_attachments: str | None = None,
    extra_system_context: str | None = None,
) -> list[dict[str, str]]:
    """构造多轮对话 messages。
    history 是 [{role, content}] 列表，按时间顺序，已经包含之前的 user/assistant 轮。
    user_message 是本轮新增的用户输入；本函数会把它追加到末尾。
    extra_system_context 可放当前会话累计的结构化产物摘要等。
    """
    sys_content = system_prompt or DEFAULT_WIZARD_PROMPT
    if extra_system_context:
        sys_content = sys_content + "\n\n【当前会话累计产物摘要】\n" + extra_system_context
    msgs: list[dict[str, str]] = [{"role": "system", "content": sys_content}]
    for h in history or []:
        role = (h.get("role") or "").strip()
        if role not in ("user", "assistant", "system"):
            continue
        content = h.get("content") or ""
        if not content:
            continue
        msgs.append({"role": role, "content": content})
    user_content = user_message or ""
    if user_attachments:
        user_content = f"{user_content}\n\n【附加资料】\n{user_attachments}"
    if user_content:
        msgs.append({"role": "user", "content": user_content})
    return msgs


# ── 章节摘要 + 关键事件提取 ─────────────────────────────────────

DEFAULT_CHAPTER_SUMMARY_PROMPT = """你是专业的小说章节信息提取助手。
你的任务是从用户给出的章节正文中，提取「章节摘要」和「关键事件清单」，用于后续章节生成时作为上下文参考。

【输出格式 - 严格按以下格式，不要附加其他内容】
=== summary ===
（200-400 字的章节摘要：本章发生了什么 / 角色状态变化 / 主要冲突 / 关系推进 / 留下的悬念）

=== key_events ===
- 事件 1（一句话，必须含主语+动作+对象，可包含场景）
- 事件 2
- 事件 3
（共 3-8 条，按时间顺序，每条不超过 60 字）

【原则】
- 只提取已发生的客观事件，不分析、不评价
- 关键事件 = 推动后续剧情的 / 改变人物关系的 / 揭示新信息的 / 埋下/回收伏笔的
- 不要包含细节描写（如某段对话的具体内容），只记录事件骨架
"""


def build_chapter_summary_messages(
    *,
    system_prompt: str | None,
    chapter_text: str,
    chapter_number: int | None = None,
    chapter_title: str | None = None,
) -> list[dict[str, str]]:
    parts = []
    if chapter_number:
        head = f"【第 {chapter_number} 章】"
        if chapter_title:
            head += f" {chapter_title}"
        parts.append(head)
    parts.append(f"【章节正文】\n{chapter_text}")
    return [
        {"role": "system", "content": system_prompt or DEFAULT_CHAPTER_SUMMARY_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# ── 伏笔自动回收识别 ────────────────────────────────────────────

DEFAULT_FORESHADOW_RESOLVE_PROMPT = """你是专业的小说伏笔追踪助手。
你的任务是分析用户给出的「最新章节正文」与「待回收伏笔列表」，判断哪些伏笔在本章已被回收。

【判定原则】
- 必须有正文中可引用的明确证据，才能认定回收
- 暗示性回收也算（不要求字面一致），但要在 evidence 中说明
- 不要为了凑数而强行认定，宁缺毋滥
- 如果伏笔涉及"长期承诺"（如"终有一天会重逢"），仅当本章实质性兑现时才回收

【输出格式 - 严格输出 JSON，不要附加任何其他文字、不要 markdown 代码块包裹】
{"resolved": [{"id": <foreshadow_id>, "evidence": "<本章中能体现该伏笔被回收的具体段落或一句话引用>"}], "still_pending": [<未回收的 foreshadow_id 列表>]}
"""


def build_foreshadow_resolve_messages(
    *,
    chapter_text: str,
    pending_foreshadows: list[dict[str, Any]],
    chapter_number: int | None = None,
) -> list[dict[str, str]]:
    parts = []
    if chapter_number:
        parts.append(f"【最新章节序号】第 {chapter_number} 章")
    fs_lines = []
    for fs in pending_foreshadows:
        fs_id = fs.get("id")
        desc = fs.get("description", "")
        planted = fs.get("planted_chapter")
        target = fs.get("target_resolve_chapter")
        importance = fs.get("importance", "normal")
        line = f"- id={fs_id} [重要性={importance}]"
        if planted:
            line += f" 埋于第{planted}章"
        if target:
            line += f"，预期第{target}章回收"
        line += f" — {desc}"
        fs_lines.append(line)
    parts.append("【待回收伏笔列表】\n" + "\n".join(fs_lines) if fs_lines else "【待回收伏笔列表】\n（无）")
    parts.append(f"【最新章节正文】\n{chapter_text}")
    return [
        {"role": "system", "content": DEFAULT_FORESHADOW_RESOLVE_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# ── 对话润色 / 心理润色 ─────────────────────────────────────────

DEFAULT_POLISH_DIALOGUE_PROMPT = """你是专业小说对话润色专家。
你的任务是只对用户给出的章节文本中的"对话部分"做润色优化，不改剧情骨架、不改非对话叙述。

【润色目标】
1. 让每段对话符合发言者的身份/性格/语言档案（如有提供）
2. 对话要有信息量：避免"嗯"、"哦"、"好的"等空话
3. 适度穿插动作/表情/环境描写，避免连续超过 5 句纯对话
4. 保留原对话的剧情功能（推进、揭示、冲突），不要把对话改没
5. 长短句交替，避免每句对话都同样长度

【输出要求】
- 输出**完整的章节文本**（含润色后的对话和未改动的叙述）
- 不要解释你改了什么
- 不要输出 diff 或对照表
"""


DEFAULT_POLISH_PSYCHOLOGY_PROMPT = """你是专业小说心理描写润色专家。
你的任务是只对用户给出的章节文本中的"心理描写部分"做润色优化，不改剧情、不改对话内容。

【润色目标】
1. 抽象心理 → 具体感官（"她很紧张" → "她攥紧裙摆，听见自己心跳的声音"）
2. 大段直白心理独白 → 用动作/微表情/呼吸/视线穿插表达
3. 删除"心想/暗道"类直白标识，让心理流自然嵌入叙述
4. 保留所有信息密度（角色想法的实质内容不能丢）
5. 注意视角统一：第一人称小说不要写"她想…"，第三人称同理

【输出要求】
- 输出**完整的章节文本**
- 不要解释你改了什么
"""


def build_polish_messages(
    *,
    polish_type: str,
    text: str,
    extra_context: str | None = None,
    instruction: str | None = None,
) -> list[dict[str, str]]:
    """polish_type: 'dialogue' | 'psychology'"""
    if polish_type == "dialogue":
        sys = DEFAULT_POLISH_DIALOGUE_PROMPT
    else:
        sys = DEFAULT_POLISH_PSYCHOLOGY_PROMPT
    parts = []
    if extra_context:
        parts.append(f"【人物语言档案与背景】\n{extra_context}")
    if instruction:
        parts.append(f"【用户指令】\n{instruction}")
    parts.append(f"【原章节文本】\n{text}")
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# ── 关键词清洗（#10）────────────────────────────────────────────
# 偏好分析用 bigram 滑窗分词，会产出大量口语噪声词（"她的"、"了一"、"身体"），
# 无法用于搜索。这个 agent 把原始高频词清洗、归并、提炼成可用于 Pixiv 搜索的关键词。
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

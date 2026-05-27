"""AI-tell 规则检测器 —— 不需要 LLM，纯规则分析。

检测文本中的 AI 生成痕迹，返回问题列表和总体评分。
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field


@dataclass(slots=True)
class AITellIssue:
    type: str
    severity: str  # "high" | "medium" | "low"
    message: str
    detail: str = ""


@dataclass(slots=True)
class AITellReport:
    score: float  # 0-100, 越高越像人写的
    issues: list[AITellIssue] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


# ── 禁用词表 ──────────────────────────────────────────────────

AI_BANNED_WORDS = [
    "仿佛", "宛如", "好似", "犹如",
    "不禁", "忍不住",
    "竟然", "居然",
    "微微", "轻轻", "缓缓", "淡淡",
    "深吸一口气", "长舒一口气",
    "嘴角上扬", "嘴角微扬", "嘴角勾起",
    "眼眸", "眸子", "眸光",
    "心中暗道", "心想", "暗自思忖",
    "不由自主", "鬼使神差",
    "若有所思", "若有所悟",
    "热泪盈眶", "心如刀割", "五味杂陈",
    "不是…而是…",
]

# 每 3000 字允许出现的最大次数
AI_WORD_THRESHOLD_PER_3K = 1


def detect_ai_tells(text: str) -> AITellReport:
    """对文本进行 AI 痕迹检测，返回报告。"""
    if not text or not text.strip():
        return AITellReport(score=100.0)

    issues: list[AITellIssue] = []
    stats: dict = {}
    penalty = 0.0  # 扣分累计

    # ── 1. 段落均匀度检测 ──────────────────────────────────────
    paragraphs = [p for p in text.split("\n") if p.strip()]
    lengths = [len(p) for p in paragraphs]
    stats["paragraph_count"] = len(paragraphs)

    if len(lengths) >= 4:
        mean_len = statistics.mean(lengths)
        stdev_len = statistics.stdev(lengths)
        cv = stdev_len / mean_len if mean_len > 0 else 0
        stats["paragraph_length_cv"] = round(cv, 3)

        if cv < 0.15:
            issues.append(AITellIssue(
                type="uniformity",
                severity="high",
                message="段落长度过于均匀（变异系数 < 0.15），像 AI 生成",
                detail=f"变异系数 = {cv:.3f}，均值 = {mean_len:.0f} 字",
            ))
            penalty += 15
        elif cv < 0.25:
            issues.append(AITellIssue(
                type="uniformity",
                severity="medium",
                message="段落长度较为均匀（变异系数 < 0.25）",
                detail=f"变异系数 = {cv:.3f}",
            ))
            penalty += 8

    # ── 2. 禁用词频率检测 ──────────────────────────────────────
    text_len = len(text)
    threshold = max(1, int(text_len / 3000)) * AI_WORD_THRESHOLD_PER_3K
    word_hits: list[tuple[str, int]] = []

    for word in AI_BANNED_WORDS:
        if "…" in word:
            continue
        count = text.count(word)
        if count > threshold:
            word_hits.append((word, count))

    if word_hits:
        top_words = sorted(word_hits, key=lambda x: x[1], reverse=True)[:5]
        detail = ", ".join(f"「{w}」×{c}" for w, c in top_words)
        severity = "high" if len(word_hits) >= 3 else "medium"
        issues.append(AITellIssue(
            type="ai_vocabulary",
            severity=severity,
            message=f"检测到 {len(word_hits)} 个 AI 高频词超标",
            detail=detail,
        ))
        penalty += min(len(word_hits) * 5, 25)
    stats["ai_word_hits"] = len(word_hits)

    # ── 3. 句式重复检测（连续相同开头）────────────────────────
    sentences = re.split(r'[。！？\n]', text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 4]
    consecutive_same_start = 0
    max_consecutive = 0

    for i in range(1, len(sentences)):
        # 取前 2 个字比较
        if sentences[i][:2] == sentences[i - 1][:2]:
            consecutive_same_start += 1
            max_consecutive = max(max_consecutive, consecutive_same_start + 1)
        else:
            consecutive_same_start = 0

    stats["max_consecutive_same_start"] = max_consecutive
    if max_consecutive >= 4:
        issues.append(AITellIssue(
            type="repetitive_structure",
            severity="high",
            message=f"连续 {max_consecutive} 句以相同方式开头",
        ))
        penalty += 12
    elif max_consecutive >= 3:
        issues.append(AITellIssue(
            type="repetitive_structure",
            severity="medium",
            message=f"连续 {max_consecutive} 句以相同方式开头",
        ))
        penalty += 6

    # ── 4. 连续短段落检测 ──────────────────────────────────────
    short_threshold = 20  # 少于 20 字算短段落
    max_short_streak = 0
    current_streak = 0
    for length in lengths:
        if length <= short_threshold:
            current_streak += 1
            max_short_streak = max(max_short_streak, current_streak)
        else:
            current_streak = 0

    if max_short_streak >= 4:
        issues.append(AITellIssue(
            type="short_paragraph_streak",
            severity="medium",
            message=f"连续 {max_short_streak} 个短段落（≤{short_threshold}字）",
        ))
        penalty += 5

    # ── 5. 过渡词密度检测 ──────────────────────────────────────
    hedge_words = ["然而", "不过", "但是", "可是", "尽管", "虽然", "事实上", "实际上", "毕竟"]
    hedge_count = sum(text.count(w) for w in hedge_words)
    hedge_density = hedge_count / (text_len / 1000) if text_len > 0 else 0
    stats["hedge_density_per_1k"] = round(hedge_density, 2)

    if hedge_density > 3:
        issues.append(AITellIssue(
            type="hedge_density",
            severity="medium",
            message=f"过渡/转折词密度过高（{hedge_density:.1f}/千字）",
            detail="真人写作中转折词使用更节制",
        ))
        penalty += 8

    # ── 6. "他/她" 开头段落比例 ────────────────────────────────
    pronoun_start_count = sum(1 for p in paragraphs if p.strip()[:1] in ("他", "她"))
    if paragraphs:
        pronoun_ratio = pronoun_start_count / len(paragraphs)
        stats["pronoun_start_ratio"] = round(pronoun_ratio, 2)
        if pronoun_ratio > 0.5:
            issues.append(AITellIssue(
                type="pronoun_start",
                severity="medium",
                message=f"{pronoun_ratio:.0%} 的段落以「他/她」开头",
            ))
            penalty += 8

    # ── 计算总分 ──────────────────────────────────────────────
    score = max(0.0, min(100.0, 100.0 - penalty))
    return AITellReport(score=score, issues=issues, stats=stats)

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from .storage_db import Database


class PreferenceAnalyzer:
    def __init__(self, db: Database) -> None:
        self.db = db

    def analyze_local(self, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        scope = scope or {}
        min_text_length = int(scope.get("min_text_length") or 1000)
        limit = int(scope.get("limit") or 0)
        rows = self.db.fetch_preference_source_rows(min_text_length=min_text_length, limit=limit)

        tag_counter: Counter[str] = Counter()
        keyword_counter: Counter[str] = Counter()
        title_keyword_counter: Counter[str] = Counter()
        caption_keyword_counter: Counter[str] = Counter()
        author_counter: Counter[str] = Counter()
        source_counter: Counter[str] = Counter()
        x_restrict_counter: Counter[str] = Counter()
        tag_cooccurrence: Counter[str] = Counter()
        length_buckets: Counter[str] = Counter()
        series_count = 0
        total_chars = 0

        for row in rows:
            text_length = int(row.get("text_length") or 0)
            total_chars += text_length
            if row.get("series_id"):
                series_count += 1
            length_buckets[self._length_bucket(text_length)] += 1
            author = str(row.get("author_name") or "").strip()
            if author:
                author_counter[author] += 1
            x_restrict_counter[str(row.get("x_restrict") or 0)] += 1
            for source in str(row.get("source_types") or "").split(","):
                source = source.strip()
                if source:
                    source_counter[source] += 1

            tags = self._parse_tags(row.get("tags_json"))
            tag_counter.update(tags)
            for i, left in enumerate(tags):
                for right in tags[i + 1:]:
                    key = " + ".join(sorted((left, right)))
                    tag_cooccurrence[key] += 1

            title_keyword_counter.update(self._tokenize(row.get("title") or ""))
            caption_keyword_counter.update(self._tokenize(row.get("caption") or ""))
            keyword_counter.update(self._tokenize(row.get("text_raw") or ""))

        stats = {
            "novel_count": len(rows),
            "series_novel_count": series_count,
            "single_novel_count": len(rows) - series_count,
            "total_chars": total_chars,
            "avg_text_length": int(total_chars / len(rows)) if rows else 0,
            "top_tags": self._top(tag_counter, 60),
            "top_tag_pairs": self._top(tag_cooccurrence, 40),
            "top_keywords": self._top(keyword_counter, 80),
            "top_title_keywords": self._top(title_keyword_counter, 40),
            "top_caption_keywords": self._top(caption_keyword_counter, 40),
            "top_authors": self._top(author_counter, 40),
            "source_distribution": dict(source_counter),
            "x_restrict_distribution": dict(x_restrict_counter),
            "length_distribution": dict(length_buckets),
        }
        profile = self._build_profile(stats)
        return {"source_scope": {"min_text_length": min_text_length, "limit": limit}, "stats": stats, "profile": profile}

    def _build_profile(self, stats: dict[str, Any]) -> dict[str, Any]:
        top_tags = [item["name"] for item in stats.get("top_tags", [])[:20]]
        top_keywords = [item["name"] for item in stats.get("top_keywords", [])[:30]]
        title_keywords = [item["name"] for item in stats.get("top_title_keywords", [])[:15]]
        caption_keywords = [item["name"] for item in stats.get("top_caption_keywords", [])[:15]]
        primary_tags = top_tags[:10]
        secondary_tags = top_tags[10:25]
        broad_queries = primary_tags[:8]
        precise_queries = []
        for tag in primary_tags[:6]:
            for keyword in (title_keywords + caption_keywords + top_keywords)[:8]:
                if keyword != tag:
                    precise_queries.append(f"{tag} {keyword}")
                    break
        experimental_queries = [f"{a} {b}" for a, b in zip(primary_tags[:6], top_keywords[:6]) if a != b]
        return {
            "version": 1,
            "summary": self._summary(stats, primary_tags, top_keywords),
            "positive_preferences": {
                "tags": primary_tags,
                "keywords": top_keywords[:25],
                "themes": title_keywords[:10],
                "relationship_dynamics": [],
                "scenes_or_situations": caption_keywords[:10],
                "tone": [],
                "pacing": [],
                "narrative_patterns": [],
            },
            "negative_preferences": {
                "excluded_tags": [],
                "excluded_keywords": [],
                "avoid_themes": [],
            },
            "search_strategy": {
                "primary_tags": primary_tags,
                "secondary_tags": secondary_tags,
                "broad_queries": broad_queries,
                "precise_queries": precise_queries[:12],
                "experimental_queries": experimental_queries[:12],
            },
            "reading_bias": {
                "preferred_min_length": max(5000, int(stats.get("avg_text_length") or 0)),
                "preferred_series": stats.get("series_novel_count", 0) >= stats.get("single_novel_count", 0),
                "preferred_authors": [item["name"] for item in stats.get("top_authors", [])[:10]],
                "common_x_restrict": [item["name"] for item in self._top(Counter(stats.get("x_restrict_distribution", {})), 3)],
                "bookmark_range": None,
                "view_range": None,
            },
            "confidence": {
                "overall": min(1.0, (stats.get("novel_count", 0) / 50) * 0.6 + (stats.get("total_chars", 0) / 500000) * 0.4),
                "based_on_novel_count": stats.get("novel_count", 0),
                "based_on_total_chars": stats.get("total_chars", 0),
            },
        }

    def _summary(self, stats: dict[str, Any], tags: list[str], keywords: list[str]) -> str:
        if not stats.get("novel_count"):
            return "暂无足够归档小说生成偏好画像。"
        tag_text = "、".join(tags[:8]) or "暂无明显标签"
        keyword_text = "、".join(keywords[:8]) or "暂无明显关键词"
        return f"基于 {stats['novel_count']} 篇归档小说、约 {stats['total_chars']} 字文本，当前高频标签为：{tag_text}；高频正文关键词为：{keyword_text}。"

    def _parse_tags(self, tags_json: Any) -> list[str]:
        try:
            data = json.loads(tags_json or "[]") if isinstance(tags_json, str) else (tags_json or [])
        except (TypeError, ValueError):
            return []
        tags: list[str] = []
        for item in data:
            if isinstance(item, str):
                value = item.strip()
            elif isinstance(item, dict):
                value = str(item.get("name") or item.get("tag") or "").strip()
            else:
                value = ""
            if value:
                tags.append(value)
        return tags

    def _tokenize(self, text: str) -> list[str]:
        text = text.lower()
        tokens: list[str] = []
        for seg in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            if len(seg) <= 8:
                tokens.append(seg)
            for i in range(len(seg) - 1):
                tokens.append(seg[i:i + 2])
        tokens.extend(re.findall(r"[a-z0-9_]{2,}", text))
        return [t for t in tokens if t not in STOPWORDS]

    def _top(self, counter: Counter[str], limit: int) -> list[dict[str, Any]]:
        return [{"name": name, "count": int(count)} for name, count in counter.most_common(limit)]

    def _length_bucket(self, length: int) -> str:
        if length < 5000:
            return "<5k"
        if length < 10000:
            return "5k-10k"
        if length < 20000:
            return "10k-20k"
        if length < 50000:
            return "20k-50k"
        return ">=50k"


STOPWORDS = {
    "这个", "一个", "一些", "没有", "自己", "什么", "时候", "可以", "因为", "所以", "但是", "只是", "已经", "还是", "不是", "他们", "她们", "我们", "你们",
    "the", "and", "for", "with", "from", "this", "that",
}

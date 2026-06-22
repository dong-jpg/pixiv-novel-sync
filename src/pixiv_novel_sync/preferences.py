from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from .storage_db import Database


class PreferenceAnalyzer:
    # 词项类型 -> (stats 键, top-N 数量)
    TERM_TYPES = {
        "tag": ("top_tags", 60),
        "tag_pair": ("top_tag_pairs", 40),
        "keyword": ("top_keywords", 80),
        "title_kw": ("top_title_keywords", 40),
        "caption_kw": ("top_caption_keywords", 40),
        "author": ("top_authors", 40),
    }
    # 高基数词项类型(用于噪声清理)
    HIGH_CARDINALITY_TYPES = ("keyword", "title_kw", "caption_kw", "tag_pair")
    TERM_COUNTS_MAX_ROWS = 300000

    def __init__(self, db: Database) -> None:
        self.db = db

    def _new_counters(self) -> dict[str, Any]:
        """新建一组空累加容器(词项 Counter + 标量)。"""
        return {
            "terms": {term_type: Counter() for term_type in self.TERM_TYPES},
            "length_buckets": Counter(),
            "source_dist": Counter(),
            "x_restrict_dist": Counter(),
            "novel_count": 0,
            "series_novel_count": 0,
            "total_chars": 0,
        }

    def _accumulate_row(self, row: dict[str, Any], acc: dict[str, Any]) -> None:
        """将单篇小说的统计并入累加容器。增量与全量共用。"""
        terms = acc["terms"]
        text_length = int(row.get("text_length") or 0)
        acc["total_chars"] += text_length
        acc["novel_count"] += 1
        if row.get("series_id"):
            acc["series_novel_count"] += 1
        acc["length_buckets"][self._length_bucket(text_length)] += 1

        author = str(row.get("author_name") or "").strip()
        if author:
            terms["author"][author] += 1
        acc["x_restrict_dist"][str(row.get("x_restrict") or 0)] += 1
        for source in str(row.get("source_types") or "").split(","):
            source = source.strip()
            if source:
                acc["source_dist"][source] += 1

        tags = self._parse_tags(row.get("tags_json"))
        terms["tag"].update(tags)
        for i, left in enumerate(tags):
            for right in tags[i + 1:]:
                key = " + ".join(sorted((left, right)))
                terms["tag_pair"][key] += 1

        terms["title_kw"].update(self._tokenize(row.get("title") or ""))
        terms["caption_kw"].update(self._tokenize(row.get("caption") or ""))
        terms["keyword"].update(self._tokenize(row.get("text_raw") or ""))

    def analyze_local(self, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        scope = scope or {}
        min_text_length = int(scope.get("min_text_length") or 1000)
        limit = int(scope.get("limit") or 0)
        rows = self.db.fetch_preference_source_rows(min_text_length=min_text_length, limit=limit)

        acc = self._new_counters()
        for row in rows:
            self._accumulate_row(row, acc)

        stats = self._stats_from_counters(acc)
        profile = self._build_profile(stats)
        return {"source_scope": {"min_text_length": min_text_length, "limit": limit}, "stats": stats, "profile": profile}

    def _stats_from_counters(self, acc: dict[str, Any]) -> dict[str, Any]:
        """从内存累加容器生成 stats(全量分析路径)。"""
        terms = acc["terms"]
        novel_count = acc["novel_count"]
        stats: dict[str, Any] = {
            "novel_count": novel_count,
            "series_novel_count": acc["series_novel_count"],
            "single_novel_count": novel_count - acc["series_novel_count"],
            "total_chars": acc["total_chars"],
            "avg_text_length": int(acc["total_chars"] / novel_count) if novel_count else 0,
            "source_distribution": dict(acc["source_dist"]),
            "x_restrict_distribution": dict(acc["x_restrict_dist"]),
            "length_distribution": dict(acc["length_buckets"]),
        }
        for term_type, (stats_key, top_n) in self.TERM_TYPES.items():
            stats[stats_key] = self._top(terms[term_type], top_n)
        return stats

    def analyze_incremental(self, batch_size: int = 200, max_batches: int = 1,
                            min_text_length: int = 1000,
                            progress: Any = None) -> dict[str, Any]:
        """增量分析: 每批取 batch_size 篇未分析小说,累加入库。返回进度。

        - batch_size: 单批小说数(控制内存/CPU)
        - max_batches: 本次最多跑几批(手动按钮用大值,定时任务用 1)
        - progress: 可选回调 progress(processed, remaining)
        """
        total_processed = 0
        for _ in range(max(1, int(max_batches))):
            rows = self.db.fetch_unanalyzed_preference_rows(min_text_length=min_text_length, batch_size=batch_size)
            if not rows:
                break

            acc = self._new_counters()
            analyzed_ids: list[int] = []
            for row in rows:
                self._accumulate_row(row, acc)
                analyzed_ids.append(int(row.get("novel_id")))

            term_deltas = {term_type: dict(counter) for term_type, counter in acc["terms"].items()}
            scalar_deltas = {
                "novel_count": acc["novel_count"],
                "series_novel_count": acc["series_novel_count"],
                "total_chars": acc["total_chars"],
                "length_buckets": dict(acc["length_buckets"]),
                "source_dist": dict(acc["source_dist"]),
                "x_restrict_dist": dict(acc["x_restrict_dist"]),
            }
            self.db.merge_preference_batch(term_deltas, scalar_deltas, analyzed_ids, min_text_length)
            total_processed += len(rows)

            if callable(progress):
                remaining = self.db.count_unanalyzed_preference_rows(min_text_length)
                progress(total_processed, remaining)

            # 词项表过大时清理低频噪声
            self.db.prune_preference_term_noise(self.HIGH_CARDINALITY_TYPES, self.TERM_COUNTS_MAX_ROWS)

            if len(rows) < batch_size:
                break  # 已无更多

        analyzed = self.db.count_analyzed_preference_rows()
        remaining = self.db.count_unanalyzed_preference_rows(min_text_length)
        return {
            "processed_this_run": total_processed,
            "analyzed_total": analyzed,
            "remaining": remaining,
            "done": remaining == 0,
        }

    def rebuild_profile_from_accumulator(self) -> dict[str, Any]:
        """从持久化累加器读取 top-N 重建 stats 与 profile。"""
        acc = self.db.get_preference_accumulator()
        novel_count = int(acc.get("novel_count") or 0)
        total_chars = int(acc.get("total_chars") or 0)
        series_count = int(acc.get("series_novel_count") or 0)
        stats: dict[str, Any] = {
            "novel_count": novel_count,
            "series_novel_count": series_count,
            "single_novel_count": novel_count - series_count,
            "total_chars": total_chars,
            "avg_text_length": int(total_chars / novel_count) if novel_count else 0,
            "source_distribution": acc.get("source_dist", {}),
            "x_restrict_distribution": acc.get("x_restrict_dist", {}),
            "length_distribution": acc.get("length_buckets", {}),
        }
        for term_type, (stats_key, top_n) in self.TERM_TYPES.items():
            stats[stats_key] = self.db.top_preference_terms(term_type, top_n)
        profile = self._build_profile(stats)
        return {
            "source_scope": {"min_text_length": int(acc.get("min_text_length") or 1000), "incremental": True},
            "stats": stats,
            "profile": profile,
        }


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

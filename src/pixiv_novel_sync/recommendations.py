from __future__ import annotations

import time
from typing import Any

from pixivpy3 import AppPixivAPI

from .auth import PixivAuthManager
from .settings import Settings
from .storage_db import Database


class RecommendationService:
    def __init__(self, db: Database, settings: Settings, api: AppPixivAPI | None = None) -> None:
        self.db = db
        self.settings = settings
        self.api = api

    def build_search_plan(self, profile: dict[str, Any], filters: dict[str, Any] | None = None) -> dict[str, Any]:
        filters = filters or {}
        profile_data = profile.get("profile") or profile
        strategy = profile_data.get("search_strategy") or {}
        queries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for query_type, key in (("tag", "primary_tags"), ("keyword", "broad_queries"), ("combined", "precise_queries"), ("experimental", "experimental_queries")):
            for query in strategy.get(key) or []:
                query = str(query).strip()
                if not query or query in seen:
                    continue
                seen.add(query)
                queries.append({
                    "query": query,
                    "type": query_type,
                    "expected_reason": "基于默认偏好画像生成",
                    "exclude_terms": strategy.get("exclude_terms") or [],
                    "limit": int(filters.get("per_query_limit") or 30),
                })
        single_min_chars = max(5000, int(filters.get("single_min_chars") or 5000))
        series_min_total_chars = max(20000, int(filters.get("series_min_total_chars") or 20000))
        return {
            "profile_id": profile.get("id"),
            "queries": queries[: int(filters.get("max_queries") or 20)],
            "filters": {
                "single_min_chars": single_min_chars,
                "series_min_total_chars": series_min_total_chars,
                "exclude_archived": bool(filters.get("exclude_archived", True)),
                "exclude_recommended_before": bool(filters.get("exclude_recommended_before", True)),
                "exclude_muted_authors": bool(filters.get("exclude_muted_authors", True)),
                "exclude_muted_tags": bool(filters.get("exclude_muted_tags", True)),
            },
        }

    def run(self, profile_id: int | None = None, search_plan: dict[str, Any] | None = None) -> dict[str, Any]:
        profile = self.db.get_preference_profile(profile_id) if profile_id else self.db.get_default_preference_profile()
        if not profile:
            raise RuntimeError("需要先生成默认偏好画像")
        plan = search_plan or self.build_search_plan(profile)
        run_id = self.db.create_recommendation_run(int(profile["id"]), plan)
        stats = {"searched": 0, "candidates": 0, "saved": 0, "filtered": 0, "errors": 0}
        try:
            api = self.api or self._login_api()
            filter_state = self.db.get_recommendation_filter_state()
            for query in plan.get("queries") or []:
                stats["searched"] += 1
                try:
                    novels = self._search_novels(api, query["query"], int(query.get("limit") or 30))
                except Exception:
                    stats["errors"] += 1
                    continue
                for novel in novels:
                    stats["candidates"] += 1
                    item = self._candidate_to_item(api, novel, query, profile, plan.get("filters") or {}, filter_state)
                    if item is None:
                        stats["filtered"] += 1
                        continue
                    item["run_id"] = run_id
                    item["profile_id"] = int(profile["id"])
                    self.db.upsert_recommendation_item(item)
                    stats["saved"] += 1
                time.sleep(float(getattr(self.settings.sync, "delay_seconds_between_pages", 1.0) or 1.0))
            self.db.update_recommendation_run(run_id, "succeeded", stats=stats)
            return {"run_id": run_id, "stats": stats, "items": self.db.list_recommendation_items(limit=100)}
        except Exception as exc:
            self.db.update_recommendation_run(run_id, "failed", stats=stats, error_message=str(exc))
            raise

    def _login_api(self) -> AppPixivAPI:
        auth = PixivAuthManager(self.settings.pixiv)
        api, _ = auth.login()
        return api

    def _search_novels(self, api: AppPixivAPI, query: str, limit: int) -> list[Any]:
        results: list[Any] = []
        next_query: dict[str, Any] | None = {"word": query, "search_target": "partial_match_for_tags", "sort": "date_desc"}
        while next_query and len(results) < limit:
            response = api.search_novel(**next_query)
            novels = list(getattr(response, "novels", []) or [])
            results.extend(novels)
            next_query = api.parse_qs(getattr(response, "next_url", None))
            if next_query and len(results) < limit:
                time.sleep(float(getattr(self.settings.sync, "delay_seconds_between_pages", 1.0) or 1.0))
        return results[:limit]

    def _candidate_to_item(
        self,
        api: AppPixivAPI,
        novel: Any,
        query: dict[str, Any],
        profile: dict[str, Any],
        filters: dict[str, Any],
        filter_state: dict[str, Any],
    ) -> dict[str, Any] | None:
        novel_id = int(getattr(novel, "id", 0) or 0)
        if not novel_id:
            return None
        if filters.get("exclude_archived", True) and novel_id in filter_state["archived_novel_ids"]:
            return None
        if filters.get("exclude_recommended_before", True) and novel_id in filter_state["dismissed_novel_ids"]:
            return None

        series_id = self._series_id(novel)
        text_length = int(getattr(novel, "text_length", 0) or 0)
        series_total_text_length = 0
        series_total_novels = 0
        item_type = "series" if series_id else "novel"
        single_min_chars = max(5000, int(filters.get("single_min_chars") or 5000))
        series_min_total_chars = max(20000, int(filters.get("series_min_total_chars") or 20000))
        if series_id:
            series_total_text_length, series_total_novels = self._series_length(api, series_id)
            if series_total_text_length < series_min_total_chars:
                return None
        elif text_length < single_min_chars:
            return None

        author = getattr(novel, "user", None)
        author_id = int(getattr(author, "id", 0) or 0) if author else 0
        author_name = str(getattr(author, "name", "") or "") if author else ""
        if filters.get("exclude_muted_authors", True) and str(author_id) in filter_state["muted_authors"]:
            return None

        tags = self._tags(novel)
        if filters.get("exclude_muted_tags", True) and set(tags) & filter_state["muted_tags"]:
            return None

        score, matched = self._score(novel, tags, profile, series_total_text_length)
        title = str(getattr(novel, "title", "") or "未命名")
        caption = str(getattr(novel, "caption", "") or "")
        matched_tags = matched.get("tags") or []
        matched_keywords = matched.get("keywords") or []
        reason_parts = []
        if matched_tags:
            reason_parts.append("命中标签：" + "、".join(matched_tags[:6]))
        if matched_keywords:
            reason_parts.append("命中关键词：" + "、".join(matched_keywords[:6]))
        if item_type == "series":
            reason_parts.append(f"系列总字数约 {series_total_text_length} 字")
        else:
            reason_parts.append(f"单篇约 {text_length} 字")
        return {
            "item_type": item_type,
            "novel_id": novel_id,
            "series_id": series_id,
            "title": title,
            "author_id": author_id,
            "author_name": author_name,
            "caption": caption,
            "tags": tags,
            "text_length": text_length,
            "series_total_text_length": series_total_text_length,
            "series_total_novels": series_total_novels,
            "total_bookmarks": int(getattr(novel, "total_bookmarks", 0) or 0),
            "total_views": int(getattr(novel, "total_view", 0) or getattr(novel, "total_views", 0) or 0),
            "score": score,
            "reason": "；".join(reason_parts),
            "matched": matched,
            "source_query": query.get("query"),
            "status": "new",
        }

    def _score(self, novel: Any, tags: list[str], profile: dict[str, Any], series_total_text_length: int) -> tuple[float, dict[str, Any]]:
        profile_data = profile.get("profile") or {}
        positive = profile_data.get("positive_preferences") or {}
        search_strategy = profile_data.get("search_strategy") or {}
        preferred_tags = set(positive.get("tags") or []) | set(search_strategy.get("primary_tags") or [])
        preferred_keywords = set(positive.get("keywords") or [])
        text = f"{getattr(novel, 'title', '')}\n{getattr(novel, 'caption', '')}"
        matched_tags = [tag for tag in tags if tag in preferred_tags]
        matched_keywords = [kw for kw in preferred_keywords if kw and kw in text]
        score = 0.0
        score += len(matched_tags) * 12
        score += len(matched_keywords) * 6
        score += min(15, int(getattr(novel, "total_bookmarks", 0) or 0) / 100)
        if series_total_text_length >= 20000:
            score += 10
        elif int(getattr(novel, "text_length", 0) or 0) >= 5000:
            score += 5
        return round(score, 2), {"tags": matched_tags, "keywords": matched_keywords}

    def _series_id(self, novel: Any) -> int | None:
        series = getattr(novel, "series", None)
        value = getattr(series, "id", None) if series else getattr(novel, "series_id", None)
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            number = 0
        return number or None

    def _series_length(self, api: AppPixivAPI, series_id: int) -> tuple[int, int]:
        total_length = 0
        total_count = 0
        next_query: dict[str, Any] | None = {"series_id": series_id}
        while next_query:
            try:
                response = api.novel_series(**next_query)
            except TypeError:
                response = api.novel_series(series_id)
            novels = self._extract_series_novels(response)
            total_count += len(novels)
            total_length += sum(self._novel_text_length(item) for item in novels)
            next_url = response.get("next_url") if isinstance(response, dict) else getattr(response, "next_url", None)
            next_query = api.parse_qs(next_url)
            if next_query:
                time.sleep(float(getattr(self.settings.sync, "delay_seconds_between_pages", 1.0) or 1.0))
        return total_length, total_count

    def _extract_series_novels(self, response: Any) -> list[Any]:
        if isinstance(response, dict):
            for key in ("novels", "series_novels"):
                value = response.get(key)
                if isinstance(value, list):
                    return value
            body = response.get("body")
            if isinstance(body, dict):
                value = body.get("novels") or body.get("series_novels")
                if isinstance(value, list):
                    return value
        return list(getattr(response, "novels", []) or [])

    def _novel_text_length(self, item: Any) -> int:
        if isinstance(item, dict):
            value = item.get("text_length") or item.get("textLength") or 0
        else:
            value = getattr(item, "text_length", 0) or getattr(item, "textLength", 0) or 0
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _tags(self, novel: Any) -> list[str]:
        tags: list[str] = []
        for item in list(getattr(novel, "tags", []) or []):
            if isinstance(item, str):
                value = item.strip()
            elif isinstance(item, dict):
                value = str(item.get("name") or item.get("tag") or "").strip()
            else:
                value = str(getattr(item, "name", "") or "").strip()
            if value:
                tags.append(value)
        return tags

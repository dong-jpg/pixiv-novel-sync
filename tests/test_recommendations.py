from pathlib import Path
from types import SimpleNamespace

from pixiv_novel_sync.preferences import PreferenceAnalyzer
from pixiv_novel_sync.recommendations import RecommendationService, _SERIES_PAGE_SAFETY_LIMIT
from pixiv_novel_sync.settings import PixivSettings, Settings, StorageSettings, SyncSettings
from pixiv_novel_sync.storage_db import Database


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        pixiv=PixivSettings(refresh_token="", access_token=None, proxy=None, timeout=30, verify_ssl=True, user_id=None),
        sync=SyncSettings(
            enabled=True,
            initial_manual_only=False,
            download_assets=False,
            write_markdown=True,
            write_raw_text=True,
            bookmark_restricts=["public"],
            max_items_per_run=None,
            max_pages_per_run=None,
            delay_seconds_between_items=0,
            delay_seconds_between_pages=0,
        ),
        storage=StorageSettings(public_dir=tmp_path / "public", private_dir=tmp_path / "private", db_path=tmp_path / "rec.db"),
    )


def test_page_delay_forwards_stop_requested(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "rec.db")
    db.init_schema()
    stop_requested = lambda: True
    service = RecommendationService(
        db,
        make_settings(tmp_path),
        stop_requested=stop_requested,
    )
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(
        service.rate_limiter,
        "wait",
        lambda **kwargs: observed.append(kwargs),
    )

    service._page_delay()

    assert observed == [{"stop_requested": stop_requested}]
    db.close()


def test_build_search_plan_from_profile(tmp_path: Path):
    db = Database(tmp_path / "rec.db")
    db.init_schema()
    service = RecommendationService(db, make_settings(tmp_path))
    profile = {
        "id": 1,
        "profile": {
            "search_strategy": {
                "primary_tags": ["甜文", "冒险"],
                "broad_queries": ["甜文"],
                "precise_queries": ["甜文 温柔"],
                "experimental_queries": ["冒险 温柔"],
            }
        },
    }

    plan = service.build_search_plan(profile, {"max_queries": 10, "per_query_limit": 5})

    assert plan["filters"]["single_min_chars"] == 5000
    assert plan["filters"]["series_min_total_chars"] == 20000
    assert [q["query"] for q in plan["queries"]] == ["甜文", "冒险", "甜文 温柔", "冒险 温柔"]
    assert all(q["limit"] == 5 for q in plan["queries"])
    db.close()


def test_search_plan_uses_profile_rebuilt_from_refined_keywords(tmp_path: Path):
    db = Database(tmp_path / "rec.db")
    db.init_schema()
    analyzer = PreferenceAnalyzer(db)
    stats = {
        "novel_count": 5,
        "total_chars": 50_000,
        "series_novel_count": 0,
        "single_novel_count": 5,
        "avg_text_length": 10_000,
        "top_tags": [{"name": "校园", "count": 5}],
        "top_keywords": [{"name": "她的", "count": 20}],
        "refined_keywords": ["秘密恋爱"],
    }
    profile = {"id": 1, "profile": analyzer.build_profile(stats)}

    plan = RecommendationService(db, make_settings(tmp_path)).build_search_plan(profile)

    queries = [item["query"] for item in plan["queries"]]
    assert any("秘密恋爱" in query for query in queries)
    assert all("她的" not in query for query in queries)
    db.close()


def test_build_search_plan_enforces_minimum_length_filters(tmp_path: Path):
    db = Database(tmp_path / "rec.db")
    db.init_schema()
    service = RecommendationService(db, make_settings(tmp_path))
    profile = {"id": 1, "profile": {"search_strategy": {"primary_tags": ["甜文"]}}}

    plan = service.build_search_plan(profile, {"single_min_chars": -1, "series_min_total_chars": -1})

    assert plan["filters"]["single_min_chars"] == 5000
    assert plan["filters"]["series_min_total_chars"] == 20000
    db.close()


def test_candidate_filters_short_single_and_scores(tmp_path: Path):
    db = Database(tmp_path / "rec.db")
    db.init_schema()
    service = RecommendationService(db, make_settings(tmp_path))
    profile = {
        "profile": {
            "positive_preferences": {"tags": ["甜文"], "keywords": ["温柔"]},
            "search_strategy": {"primary_tags": ["甜文"]},
        }
    }
    filter_state = {"archived_novel_ids": set(), "dismissed_novel_ids": set(), "muted_authors": set(), "muted_tags": set()}
    short = SimpleNamespace(id=1, text_length=4999, title="温柔", caption="", tags=["甜文"], user=SimpleNamespace(id=9, name="A"), total_bookmarks=0)
    good = SimpleNamespace(id=2, text_length=6000, title="温柔", caption="", tags=["甜文"], user=SimpleNamespace(id=9, name="A"), total_bookmarks=200)

    assert service._candidate_to_item(None, short, {"query": "甜文"}, profile, {"single_min_chars": -1}, filter_state) is None
    item = service._candidate_to_item(None, good, {"query": "甜文"}, profile, {"single_min_chars": -1}, filter_state)

    assert item is not None
    assert item["item_type"] == "novel"
    assert item["score"] > 0
    assert item["matched"]["tags"] == ["甜文"]
    db.close()


def test_recommendation_item_upsert_and_mutes(tmp_path: Path):
    db = Database(tmp_path / "rec.db")
    db.init_schema()
    profile_id = db.create_preference_profile({"name": "p", "source_scope": {}, "stats": {}, "profile": {}})
    run_id = db.create_recommendation_run(profile_id, {"queries": []})
    data = {
        "run_id": run_id,
        "profile_id": profile_id,
        "item_type": "novel",
        "novel_id": 1,
        "title": "旧标题",
        "tags": ["甜文"],
        "score": 1,
        "matched": {},
    }
    first_id = db.upsert_recommendation_item(data)
    data["title"] = "新标题"
    data["score"] = 9
    second_id = db.upsert_recommendation_item(data)

    items = db.list_recommendation_items()
    assert first_id == second_id
    assert len(items) == 1
    assert items[0]["title"] == "新标题"
    assert items[0]["score"] == 9

    mute_id = db.create_recommendation_mute("tag", "甜文")
    state = db.get_recommendation_filter_state()
    assert 1 in state["recommended_novel_ids"]
    assert 1 not in state["dismissed_novel_ids"]
    assert "甜文" in state["muted_tags"]
    db.delete_recommendation_mute(mute_id)
    assert not db.list_recommendation_mutes()
    db.close()


def test_recommendation_item_round_trips_restriction_and_risks(tmp_path: Path) -> None:
    db = Database(tmp_path / "rec.db")
    db.init_schema()
    profile_id = db.create_preference_profile(
        {"name": "p", "source_scope": {}, "stats": {}, "profile": {}}
    )
    run_id = db.create_recommendation_run(profile_id, {"queries": []})

    item_id = db.upsert_recommendation_item(
        {
            "run_id": run_id,
            "profile_id": profile_id,
            "item_type": "novel",
            "novel_id": 11,
            "title": "受限内容",
            "tags": [],
            "score": 1,
            "matched": {},
            "x_restrict": 1,
            "risk_notes": ["包含成人限制内容"],
        }
    )

    item = db.get_recommendation_item(item_id)
    assert item is not None
    assert item["x_restrict"] == 1
    assert item["risk_notes"] == ["包含成人限制内容"]
    db.close()


def test_filter_state_tracks_recommended_and_dismissed_series(tmp_path: Path) -> None:
    db = Database(tmp_path / "rec.db")
    db.init_schema()
    profile_id = db.create_preference_profile(
        {"name": "p", "source_scope": {}, "stats": {}, "profile": {}}
    )
    run_id = db.create_recommendation_run(profile_id, {"queries": []})
    for novel_id, series_id, status in ((11, 99, "new"), (12, 100, "dismissed")):
        db.upsert_recommendation_item(
            {
                "run_id": run_id,
                "profile_id": profile_id,
                "item_type": "series",
                "novel_id": novel_id,
                "series_id": series_id,
                "title": f"系列 {series_id}",
                "tags": [],
                "score": 1,
                "matched": {},
                "status": status,
            }
        )

    state = db.get_recommendation_filter_state()

    assert state["recommended_series_ids"] == {99, 100}
    assert state["dismissed_series_ids"] == {100}
    db.close()


def test_exclude_recommended_before_filters_any_previous_item(tmp_path: Path):
    db = Database(tmp_path / "rec.db")
    db.init_schema()
    profile_id = db.create_preference_profile({"name": "p", "source_scope": {}, "stats": {}, "profile": {}})
    run_id = db.create_recommendation_run(profile_id, {"queries": []})
    db.upsert_recommendation_item({
        "run_id": run_id,
        "profile_id": profile_id,
        "item_type": "novel",
        "novel_id": 42,
        "title": "已推荐",
        "tags": [],
        "score": 1,
        "matched": {},
        "status": "new",
    })
    service = RecommendationService(db, make_settings(tmp_path))
    novel = SimpleNamespace(id=42, text_length=6000, title="已推荐", caption="", tags=[], user=SimpleNamespace(id=1, name="A"))

    item = service._candidate_to_item(
        None,
        novel,
        {"query": "x"},
        {"profile": {}},
        {"exclude_recommended_before": True},
        db.get_recommendation_filter_state(),
    )

    assert item is None
    db.close()


def test_previous_series_filters_different_member_before_detail_lookup(tmp_path: Path) -> None:
    db = Database(tmp_path / "rec.db")
    db.init_schema()
    profile_id = db.create_preference_profile(
        {"name": "p", "source_scope": {}, "stats": {}, "profile": {}}
    )
    previous_run_id = db.create_recommendation_run(profile_id, {"queries": []})
    db.upsert_recommendation_item(
        {
            "run_id": previous_run_id,
            "profile_id": profile_id,
            "item_type": "series",
            "novel_id": 11,
            "series_id": 99,
            "title": "旧成员",
            "author_id": 1,
            "tags": [],
            "score": 1,
            "matched": {},
        }
    )
    candidate = SimpleNamespace(
        id=12,
        text_length=6000,
        title="不同的新成员",
        caption="",
        tags=[],
        user=SimpleNamespace(id=2, name="B"),
        total_bookmarks=10,
        series=SimpleNamespace(id=99),
    )

    class FakeApi:
        def __init__(self) -> None:
            self.series_detail_calls = 0

        def search_novel(self, **kwargs):
            return SimpleNamespace(novels=[candidate], next_url=None)

        def novel_series(self, **kwargs):
            self.series_detail_calls += 1
            return SimpleNamespace(
                novels=[SimpleNamespace(text_length=25_000)],
                next_url=None,
            )

        def parse_qs(self, url):
            return None

    api = FakeApi()
    service = RecommendationService(db, make_settings(tmp_path), api=api)
    result = service.run(
        profile_id=profile_id,
        search_plan={
            "queries": [{"query": "新成员", "limit": 1}],
            "filters": {
                "exclude_archived": True,
                "exclude_recommended_before": True,
                "exclude_muted_authors": True,
                "exclude_muted_tags": True,
                "series_min_total_chars": 20_000,
            },
        },
    )

    assert result["stats"]["saved"] == 0
    assert api.series_detail_calls == 0
    db.close()


def test_candidate_exposes_restriction_and_preference_risks(tmp_path: Path) -> None:
    db = Database(tmp_path / "rec.db")
    db.init_schema()
    service = RecommendationService(db, make_settings(tmp_path))
    profile = {
        "profile": {
            "negative_preferences": {
                "excluded_tags": ["禁忌标签"],
                "excluded_keywords": ["冲突情节"],
            }
        }
    }
    novel = SimpleNamespace(
        id=22,
        text_length=6000,
        title="包含冲突情节的标题",
        caption="",
        tags=["禁忌标签"],
        user=SimpleNamespace(id=3, name="C"),
        total_bookmarks=0,
        x_restrict=1,
    )

    item = service._candidate_to_item(
        None,
        novel,
        {"query": "x"},
        profile,
        {"single_min_chars": 5000},
        {
            "archived_novel_ids": set(),
            "recommended_novel_ids": set(),
            "dismissed_novel_ids": set(),
            "recommended_series_ids": set(),
            "dismissed_series_ids": set(),
            "muted_authors": set(),
            "muted_tags": set(),
        },
    )

    assert item is not None
    assert item["x_restrict"] == 1
    assert item["risk_notes"] == [
        "包含成人限制内容",
        "命中负向标签：禁忌标签",
        "命中负向关键词：冲突情节",
    ]
    db.close()


def test_series_length_caps_pagination(tmp_path: Path):
    db = Database(tmp_path / "rec.db")
    db.init_schema()
    service = RecommendationService(db, make_settings(tmp_path))

    class LoopingApi:
        def __init__(self) -> None:
            self.calls = 0

        def novel_series(self, **kwargs):
            self.calls += 1
            return {"novels": [{"text_length": 1000}], "next_url": "https://example.test/next"}

        def parse_qs(self, url):
            return {"series_id": 1} if url else None

    api = LoopingApi()
    total_length, total_count = service._series_length(api, 1)

    # 永远返回 next_url 的接口必须被安全上限截断，而不是无限翻页
    assert api.calls == _SERIES_PAGE_SAFETY_LIMIT
    assert total_count == _SERIES_PAGE_SAFETY_LIMIT
    assert total_length == _SERIES_PAGE_SAFETY_LIMIT * 1000
    db.close()


def test_run_accepts_progress_callback_and_reports_progress(tmp_path: Path):
    """C1: run() 必须接受 progress_callback（Web 推荐 job 走这条路径）。"""
    db = Database(tmp_path / "rec.db")
    db.init_schema()
    profile_id = db.create_preference_profile({
        "name": "default", "source_scope": {}, "stats": {},
        "profile": {"search_strategy": {"primary_tags": ["甜文"]}},
    })
    service = RecommendationService(db, make_settings(tmp_path))

    good = SimpleNamespace(
        id=2, text_length=6000, title="温柔", caption="", tags=["甜文"],
        user=SimpleNamespace(id=9, name="A"), total_bookmarks=200, series=None, series_id=None,
    )

    class FakeApi:
        def search_novel(self, **kwargs):
            return SimpleNamespace(novels=[good], next_url=None)

        def parse_qs(self, url):
            return None

    service.api = FakeApi()

    events: list[str] = []

    def progress_callback(event_type, data):
        events.append(event_type)

    result = service.run(profile_id=profile_id, progress_callback=progress_callback)

    assert "run_id" in result and "stats" in result
    assert result["stats"]["saved"] == 1
    # 至少发出过 phase 进度事件
    assert "phase" in events
    db.close()


def test_run_progress_callback_can_cancel(tmp_path: Path):
    """C1: progress_callback 抛 InterruptedError 时 run() 应中断并将 run 标记为 failed。"""
    db = Database(tmp_path / "rec.db")
    db.init_schema()
    profile_id = db.create_preference_profile({
        "name": "default", "source_scope": {}, "stats": {},
        "profile": {"search_strategy": {"primary_tags": ["甜文", "冒险"]}},
    })
    service = RecommendationService(db, make_settings(tmp_path))

    class FakeApi:
        def search_novel(self, **kwargs):
            return SimpleNamespace(novels=[], next_url=None)

        def parse_qs(self, url):
            return None

    service.api = FakeApi()

    def progress_callback(event_type, data):
        # 首个 phase 事件即请求取消
        raise InterruptedError("stop")

    import pytest
    with pytest.raises(InterruptedError):
        service.run(profile_id=profile_id, progress_callback=progress_callback)
    db.close()


def test_archived_membership_is_lazy_and_correct(tmp_path: Path):
    """5.3: archived_novel_ids 走主键索引 EXISTS 惰性判断,而非全表载入 set。"""
    db = Database(tmp_path / "rec.db")
    db.init_schema()
    # 归档一本小说 novel_id=100
    from pixiv_novel_sync.models import NovelRecord
    db.upsert_novel(NovelRecord(
        novel_id=100, user_id=1, series_id=None, title="已归档", caption=None,
        visible=True, restrict="public", x_restrict=0, text_length=6000,
        total_bookmarks=0, total_views=0, cover_url=None, tags_json="[]",
        create_date=None, raw_json="{}", meta_hash="h",
    ))

    state = db.get_recommendation_filter_state()
    archived = state["archived_novel_ids"]

    # 不是真 set,但 `in` 语义照常工作
    assert not isinstance(archived, set)
    assert 100 in archived          # 已归档命中
    assert 999 not in archived      # 未归档不命中
    assert 100 in archived          # 命中结果走缓存,重复判断不重复打库
    assert "bad" not in archived    # 非法值安全返回 False

    # _candidate_to_item 应据此过滤掉已归档候选
    service = RecommendationService(db, make_settings(tmp_path))
    novel = SimpleNamespace(id=100, text_length=6000, title="t", caption="", tags=[], user=SimpleNamespace(id=1, name="A"))
    item = service._candidate_to_item(
        None, novel, {"query": "x"}, {"profile": {}},
        {"exclude_archived": True}, state,
    )
    assert item is None
    db.close()

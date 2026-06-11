from pathlib import Path
from types import SimpleNamespace

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

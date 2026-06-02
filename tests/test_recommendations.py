from pathlib import Path
from types import SimpleNamespace

from pixiv_novel_sync.recommendations import RecommendationService
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
    assert "甜文" in state["muted_tags"]
    db.delete_recommendation_mute(mute_id)
    assert not db.list_recommendation_mutes()
    db.close()

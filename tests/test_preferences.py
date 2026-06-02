from pathlib import Path

from pixiv_novel_sync.models import NovelRecord, NovelTextRecord, SourceRecord, UserRecord
from pixiv_novel_sync.preferences import PreferenceAnalyzer
from pixiv_novel_sync.storage_db import Database


def test_preference_analyzer_builds_profile(tmp_path: Path):
    db = Database(tmp_path / "prefs.db")
    db.init_schema()
    db.upsert_user(UserRecord(user_id=1, name="作者A", account="a", raw_json="{}"))
    db.upsert_novel(NovelRecord(
        novel_id=100,
        user_id=1,
        series_id=None,
        title="甜蜜 冒险",
        caption="温柔 关系",
        visible=True,
        restrict="public",
        x_restrict=0,
        text_length=6000,
        total_bookmarks=10,
        total_views=100,
        cover_url=None,
        tags_json='["甜文", "冒险"]',
        create_date=None,
        raw_json="{}",
        meta_hash="h1",
    ))
    db.upsert_novel_text(NovelTextRecord(
        novel_id=100,
        text_raw="温柔的冒险故事" * 500,
        text_markdown=None,
        text_hash="t1",
    ))
    db.upsert_source(SourceRecord(novel_id=100, source_type="bookmark", source_key="public"))

    result = PreferenceAnalyzer(db).analyze_local({"min_text_length": 1000})

    assert result["stats"]["novel_count"] == 1
    assert result["stats"]["total_chars"] == 6000
    assert result["profile"]["search_strategy"]["primary_tags"][:2] == ["甜文", "冒险"]
    assert result["profile"]["reading_bias"]["preferred_min_length"] >= 5000
    db.close()


def test_preference_profile_crud(tmp_path: Path):
    db = Database(tmp_path / "prefs.db")
    db.init_schema()

    first_id = db.create_preference_profile({
        "name": "画像1",
        "source_scope": {"min_text_length": 1000},
        "stats": {"novel_count": 0},
        "profile": {"summary": "空"},
        "is_default": True,
    })
    second_id = db.create_preference_profile({
        "name": "画像2",
        "source_scope": {},
        "stats": {},
        "profile": {},
        "is_default": True,
    })

    assert db.get_default_preference_profile()["id"] == second_id
    assert db.get_preference_profile(first_id)["is_default"] is False
    db.set_default_preference_profile(first_id)
    assert db.get_default_preference_profile()["id"] == first_id
    db.delete_preference_profile(second_id)
    assert db.get_preference_profile(second_id) is None
    db.close()

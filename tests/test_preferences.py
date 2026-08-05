import sqlite3
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


def test_build_profile_prefers_refined_keywords(tmp_path: Path):
    db = Database(tmp_path / "prefs.db")
    db.init_schema()
    analyzer = PreferenceAnalyzer(db)
    stats = {
        "novel_count": 10,
        "total_chars": 100_000,
        "series_novel_count": 0,
        "single_novel_count": 10,
        "avg_text_length": 10_000,
        "top_tags": [{"name": "百合", "count": 8}],
        "top_keywords": [{"name": "她的", "count": 50}, {"name": "了一", "count": 40}],
        "refined_keywords": ["校园恋爱", "百合"],
    }

    profile = analyzer.build_profile(stats)

    assert profile["positive_preferences"]["keywords"] == ["校园恋爱", "百合"]
    assert "她的" not in profile["summary"]
    assert all("她的" not in query for query in profile["search_strategy"]["precise_queries"])
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


def test_project_preference_fields_round_trip(tmp_path: Path) -> None:
    db = Database(tmp_path / "prefs.db")
    db.init_schema()
    profile_id = db.create_preference_profile(
        {
            "name": "画像",
            "source_scope": {},
            "stats": {},
            "profile": {},
        }
    )

    project_id = db.create_ai_writing_project(
        {
            "name": "项目",
            "preference_profile_id": profile_id,
            "preference_injection_strength": "standard",
        }
    )
    project = db.get_ai_writing_project(project_id)
    assert project is not None
    assert project["preference_profile_id"] == profile_id
    assert project["preference_injection_strength"] == "standard"

    db.update_ai_writing_project(
        project_id,
        {
            "preference_profile_id": None,
            "preference_injection_strength": "off",
        },
    )
    project = db.get_ai_writing_project(project_id)
    assert project is not None
    assert project["preference_profile_id"] is None
    assert project["preference_injection_strength"] == "off"
    db.close()


def test_project_preference_migration_preserves_old_projects(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-project.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE ai_writing_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                outline_json TEXT,
                style_profile_id INTEGER,
                novel_profile_id INTEGER,
                settings_json TEXT,
                cover_path TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO ai_writing_projects (id, name, settings_json)
            VALUES (41, '旧项目', '{}');
            """
        )

    db = Database(db_path)
    db.init_schema()
    project = db.get_ai_writing_project(41)

    assert project is not None
    assert project["name"] == "旧项目"
    assert project["preference_profile_id"] is None
    assert project["preference_injection_strength"] == "off"
    db.close()

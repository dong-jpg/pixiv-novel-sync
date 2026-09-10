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

def test_build_profile_includes_muted_tags_as_negative_preferences(tmp_path: Path):
    """负向偏好: 用户屏蔽的标签(mute_type=tag)必须并入 negative_preferences.excluded_tags。"""
    db = Database(tmp_path / "prefs.db")
    db.init_schema()
    db.create_recommendation_mute("tag", "雷点标签")
    db.create_recommendation_mute("tag", "另一个雷点")
    db.create_recommendation_mute("author", "某作者")  # author 屏蔽不进标签

    profile = PreferenceAnalyzer(db).build_profile({
        "novel_count": 1, "total_chars": 6000,
        "top_tags": [{"name": "甜文", "count": 1}],
    })

    assert set(profile["negative_preferences"]["excluded_tags"]) == {"雷点标签", "另一个雷点"}
    assert profile["negative_preferences"]["excluded_keywords"] == []
    db.close()


def test_build_profile_negative_tags_empty_without_mutes(tmp_path: Path):
    db = Database(tmp_path / "prefs.db")
    db.init_schema()
    profile = PreferenceAnalyzer(db).build_profile({"novel_count": 0, "total_chars": 0})
    assert profile["negative_preferences"]["excluded_tags"] == []
    db.close()


def _make_feedback_app(tmp_path: Path):
    from flask import Flask
    from pixiv_novel_sync.preference_web import register_preference_routes
    from pixiv_novel_sync.settings import PixivSettings, Settings, StorageSettings, SyncSettings

    settings = Settings(
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
        storage=StorageSettings(public_dir=tmp_path / "public", private_dir=tmp_path / "private", db_path=tmp_path / "prefs.db"),
    )
    app = Flask(__name__)
    register_preference_routes(app, settings)
    return app, settings


def _seed_recommendation_item(db_path) -> int:
    db = Database(db_path)
    db.init_schema()
    profile_id = db.create_preference_profile({"name": "p", "source_scope": {}, "stats": {}, "profile": {}})
    run_id = db.create_recommendation_run(profile_id, {"queries": []})
    item_id = db.upsert_recommendation_item({
        "run_id": run_id, "profile_id": profile_id, "item_type": "novel",
        "novel_id": 1, "title": "t", "tags": [], "score": 1, "matched": {},
    })
    db.close()
    return item_id


def _seed_recommendation_runs(db_path, runs: int, items_per_run: int) -> None:
    """造 runs 轮推书、每轮 items_per_run 条，run_id 递增。"""
    db = Database(db_path)
    db.init_schema()
    profile_id = db.create_preference_profile({"name": "p", "source_scope": {}, "stats": {}, "profile": {}})
    for run_index in range(runs):
        run_id = db.create_recommendation_run(profile_id, {"queries": []})
        for item_index in range(items_per_run):
            db.upsert_recommendation_item({
                "run_id": run_id, "profile_id": profile_id, "item_type": "novel",
                "novel_id": run_index * 100 + item_index, "title": f"t{run_index}-{item_index}",
                "tags": [], "score": item_index, "matched": {},
            })
    db.close()


def test_list_recommendation_items_paged_orders_newest_run_first(tmp_path: Path):
    """首页要「最新一轮排最前」：排序键是 run_id 而不是 created_at / updated_at。

    同一本书被新一轮重推时 upsert 会刷 run_id 与 updated_at，created_at 停在首次入库；
    反馈操作也会刷 updated_at。拿那两个排序会让老结果跳回第一页。
    """
    _seed_recommendation_runs(tmp_path / "paged.db", runs=3, items_per_run=4)
    db = Database(tmp_path / "paged.db")
    db.init_schema()

    first = db.list_recommendation_items_paged(page=1, page_size=4)
    assert first["total"] == 12
    assert first["total_pages"] == 3
    assert first["page"] == 1
    # 最新一轮（run_id 最大）的 4 条整体在第一页，轮内按分数倒序
    assert [item["score"] for item in first["items"]] == [3, 2, 1, 0]
    assert all(item["title"].startswith("t2-") for item in first["items"])

    last = db.list_recommendation_items_paged(page=3, page_size=4)
    assert all(item["title"].startswith("t0-") for item in last["items"])

    # 越界页夹回最后一页，不返回空
    clamped = db.list_recommendation_items_paged(page=99, page_size=4)
    assert clamped["page"] == 3
    db.close()


def test_recommendation_items_endpoint_returns_envelope_with_page(tmp_path: Path):
    """带 page 参数返回分页信封；不带则保持平铺数组（偏好页与 run 返回值依赖旧形状）。"""
    from flask import Flask

    from pixiv_novel_sync.preference_web import register_preference_routes
    from pixiv_novel_sync.settings import PixivSettings, Settings, StorageSettings, SyncSettings

    settings = Settings(
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
        storage=StorageSettings(public_dir=tmp_path / "public", private_dir=tmp_path / "private", db_path=tmp_path / "env.db"),
    )
    app = Flask(__name__)
    register_preference_routes(app, settings)
    _seed_recommendation_runs(settings.storage.db_path, runs=2, items_per_run=3)
    client = app.test_client()

    envelope = client.get("/api/dashboard/recommendations/items?page=1&page_size=3").get_json()
    assert envelope["ok"] is True
    data = envelope["data"]
    assert set(data) == {"items", "page", "page_size", "total", "total_pages"}
    assert data["total"] == 6
    assert data["total_pages"] == 2
    assert len(data["items"]) == 3

    flat = client.get("/api/dashboard/recommendations/items?limit=100").get_json()
    assert isinstance(flat["data"], list)
    assert len(flat["data"]) == 6

    bad = client.get("/api/dashboard/recommendations/items?page=abc")
    assert bad.status_code == 400


def test_feedback_rejects_invalid_feedback_type(tmp_path: Path):
    """反馈接口必须校验枚举,非法值返回 400 且不写库。"""
    app, settings = _make_feedback_app(tmp_path)
    item_id = _seed_recommendation_item(settings.storage.db_path)
    client = app.test_client()

    resp = client.post(
        f"/api/dashboard/recommendations/items/{item_id}/feedback",
        json={"feedback_type": "totally_bogus"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False

    db = Database(settings.storage.db_path)
    db.init_schema()
    assert db.conn.execute("SELECT COUNT(*) FROM recommendation_feedback").fetchone()[0] == 0
    assert db.get_recommendation_item(item_id)["status"] == "new"
    db.close()


def test_feedback_accepts_allowed_enum_values(tmp_path: Path):
    app, settings = _make_feedback_app(tmp_path)
    item_id = _seed_recommendation_item(settings.storage.db_path)
    client = app.test_client()

    resp = client.post(
        f"/api/dashboard/recommendations/items/{item_id}/feedback",
        json={"feedback_type": "dismissed"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    db = Database(settings.storage.db_path)
    db.init_schema()
    assert db.get_recommendation_item(item_id)["status"] == "dismissed"
    assert db.conn.execute("SELECT feedback_type FROM recommendation_feedback").fetchone()[0] == "dismissed"
    db.close()

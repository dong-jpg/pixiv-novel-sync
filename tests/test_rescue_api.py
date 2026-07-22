from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pixiv_novel_sync.models import SourceRecord
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.webapp import SettingsManager, create_app


def _seed_rescue_data(db_path: Path) -> None:
    db = Database(db_path)
    db.init_schema()
    db.conn.execute(
        "INSERT INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')"
    )
    db.conn.execute(
        """
        INSERT INTO novels (
            novel_id, user_id, title, caption, visible, restrict_value,
            x_restrict, text_length, total_bookmarks, total_views, tags_json,
            raw_json, meta_hash, status
        ) VALUES (10, 1, '救援小说', '简介', 1, 'public', 0, 2, 0, 0,
                  '["标签"]', '{"secret":"hidden"}', 'h10', 'deleted')
        """
    )
    db.conn.execute(
        "INSERT INTO novel_texts (novel_id, text_raw, has_content, text_hash) "
        "VALUES (10, '正文', 1, 't10')"
    )
    db.upsert_source(SourceRecord(10, "bookmark_public", "1"))
    db.conn.execute(
        """
        INSERT INTO novels (
            novel_id, user_id, title, visible, restrict_value, x_restrict,
            text_length, total_bookmarks, total_views, tags_json, raw_json,
            meta_hash, status
        ) VALUES (11, 1, '正常小说', 1, 'public', 0, 2, 0, 0,
                  '[]', '{}', 'h11', 'normal')
        """
    )
    db.conn.execute(
        "INSERT INTO novel_texts (novel_id, text_raw, has_content, text_hash) "
        "VALUES (11, '正常正文', 1, 't11')"
    )
    db.conn.execute(
        "INSERT INTO series (series_id, title, user_id, total_novels, status) VALUES (20, '救援系列', 1, 1, 'deleted')"
    )
    db.conn.execute(
        """
        INSERT INTO novels (
            novel_id, user_id, series_id, title, visible, restrict_value,
            x_restrict, text_length, total_bookmarks, total_views, tags_json,
            raw_json, meta_hash, status
        ) VALUES (21, 1, 20, '系列章节', 1, 'public', 0, 4, 0, 0,
                  '[]', '{}', 'h21', 'normal')
        """
    )
    db.conn.execute(
        "INSERT INTO novel_texts (novel_id, text_raw, has_content, text_hash) "
        "VALUES (21, '系列正文', 1, 't21')"
    )
    db.conn.commit()
    db.rebuild_rescue_catalog()
    db.close()


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    db_path = tmp_path / "state" / "rescue.db"
    monkeypatch.setenv("PIXIV_DB_PATH", str(db_path))
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    application = create_app(env_path=str(env_path))
    application.config.update(TESTING=True, PIXIV_DB_PATH=str(db_path))
    _seed_rescue_data(db_path)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _rotate_token(client) -> str:
    response = client.post("/api/dashboard/rescue-token/rotate")
    assert response.status_code == 200
    return str(response.get_json()["data"]["token"])


def _rebuild_catalog(app) -> None:
    db = Database(Path(app.config["PIXIV_DB_PATH"]))
    try:
        db.rebuild_rescue_catalog()
    finally:
        db.close()


def _set_catalog_refreshed_at(app, refreshed_at: str) -> None:
    db = Database(Path(app.config["PIXIV_DB_PATH"]))
    try:
        db.conn.execute(
            "UPDATE rescue_catalog_meta SET refreshed_at = ? WHERE singleton_id = 1",
            (refreshed_at,),
        )
        db.conn.commit()
    finally:
        db.close()


def test_dashboard_rescue_list_filters_content_kind_and_source(client) -> None:
    response = client.get(
        "/api/dashboard/rescues?content_kind=standalone&source_kind=bookmark"
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["total"] == 1
    assert [
        (item["item_type"], item["item_id"], item["content_kind"])
        for item in data["items"]
    ] == [("novel", 10, "standalone")]
    assert any(source["kind"] == "bookmark" for source in data["items"][0]["sources"])


def test_dashboard_rescue_list_returns_503_before_first_refresh(client, monkeypatch) -> None:
    monkeypatch.setattr(Database, "get_rescue_catalog_meta", lambda self: None)
    monkeypatch.setattr(
        Database,
        "rebuild_rescue_catalog",
        lambda *_args: pytest.fail("请求线程不得执行首次完整刷新"),
    )
    response = client.get("/api/dashboard/rescues")
    assert response.status_code == 503
    assert response.get_json() == {"ok": False, "error": "救援目录尚未生成"}


def test_dashboard_rescue_list_content_kind_takes_priority_over_item_type(client) -> None:
    response = client.get(
        "/api/dashboard/rescues?content_kind=standalone&item_type=series"
    )

    data = response.get_json()["data"]
    assert [(item["item_type"], item["content_kind"]) for item in data["items"]] == [
        ("novel", "standalone")
    ]
    compatible = client.get(
        "/api/dashboard/rescues?content_kind=all&item_type=series"
    ).get_json()["data"]
    assert compatible["total"] == 1
    assert [(item["item_type"], item["item_id"]) for item in compatible["items"]] == [
        ("series", 20)
    ]


def test_dashboard_rescue_list_search_preserves_unicode_casefold(app, client) -> None:
    db = Database(Path(app.config["PIXIV_DB_PATH"]))
    try:
        db.conn.execute(
            "UPDATE rescue_catalog SET title = 'ÄRGER 救援' "
            "WHERE item_type = 'novel' AND item_id = 10"
        )
        db.conn.commit()
    finally:
        db.close()

    response = client.get("/api/dashboard/rescues?search=ärger")

    data = response.get_json()["data"]
    assert data["total"] == 1
    assert [(item["item_type"], item["item_id"]) for item in data["items"]] == [
        ("novel", 10)
    ]


@pytest.mark.parametrize("search", ["%", "_", "\\"])
def test_dashboard_rescue_list_search_treats_sql_wildcards_as_literals(
    app,
    client,
    search,
) -> None:
    db = Database(Path(app.config["PIXIV_DB_PATH"]))
    try:
        db.conn.execute(
            "UPDATE rescue_catalog SET title = '符号 % _ \\ 救援' "
            "WHERE item_type = 'novel' AND item_id = 10"
        )
        db.conn.commit()
    finally:
        db.close()

    response = client.get(
        "/api/dashboard/rescues",
        query_string={"search": search},
    )

    data = response.get_json()["data"]
    assert data["total"] == 1
    assert [(item["item_type"], item["item_id"]) for item in data["items"]] == [
        ("novel", 10)
    ]


def test_dashboard_rescue_list_source_filter_keeps_all_sources(app, client) -> None:
    db_path = app.config["PIXIV_DB_PATH"]
    db = Database(Path(db_path))
    try:
        db.upsert_source(SourceRecord(10, "following_user_scan", "1"))
        db.rebuild_rescue_catalog()
    finally:
        db.close()

    response = client.get("/api/dashboard/rescues?source_kind=following_user")

    items = response.get_json()["data"]["items"]
    assert len(items) == 1
    assert [source["kind"] for source in items[0]["sources"]] == [
        "bookmark",
        "following_user",
    ]


def test_dashboard_rescue_list_returns_chinese_labels(client) -> None:
    response = client.get("/api/dashboard/rescues")

    items = response.get_json()["data"]["items"]
    by_kind = {item["content_kind"]: item for item in items}
    assert by_kind["series"]["content_kind_label"] == "系列"
    assert by_kind["standalone"]["content_kind_label"] == "独立小说"
    assert by_kind["standalone"]["sources"] == [
        {
            "kind": "bookmark",
            "label": "我的收藏",
            "user_id": None,
            "user_name": None,
        }
    ]


def test_dashboard_rescue_list_returns_series_chapter_label(app, client) -> None:
    db = Database(Path(app.config["PIXIV_DB_PATH"]))
    try:
        db.conn.execute(
            "INSERT INTO series (series_id, title, user_id, total_novels, status) "
            "VALUES (30, '正常系列', 1, 1, 'normal')"
        )
        db.conn.execute(
            """
            INSERT INTO novels (
                novel_id, user_id, series_id, title, visible, restrict_value,
                x_restrict, text_length, total_bookmarks, total_views, tags_json,
                raw_json, meta_hash, status
            ) VALUES (31, 1, 30, '失效单章', 1, 'public', 0, 4, 0, 0,
                      '[]', '{}', 'h31', 'deleted')
            """
        )
        db.conn.execute(
            "INSERT INTO novel_texts (novel_id, text_raw, has_content, text_hash) "
            "VALUES (31, '单章正文', 1, 't31')"
        )
        db.conn.commit()
        db.rebuild_rescue_catalog()
    finally:
        db.close()

    response = client.get("/api/dashboard/rescues?content_kind=series_chapter")

    item = response.get_json()["data"]["items"][0]
    assert item["content_kind"] == "series_chapter"
    assert item["content_kind_label"] == "系列单章"


def test_dashboard_rescue_list_orders_and_labels_all_sources(app, client) -> None:
    db = Database(Path(app.config["PIXIV_DB_PATH"]))
    try:
        db.conn.executemany(
            "INSERT INTO users (user_id, name, raw_json) VALUES (?, ?, '{}')",
            [(2, "A作者"), (3, "A作者"), (4, "B作者")],
        )
        for source_type, source_key in [
            ("subscribed_series", "20"),
            ("following_user_scan", "3"),
            ("following_user_scan", "2"),
            ("following_user_scan", "4"),
            ("user_backup", "1"),
            ("unexpected_source", "x"),
        ]:
            db.upsert_source(SourceRecord(10, source_type, source_key))
        db.rebuild_rescue_catalog()
    finally:
        db.close()

    item = client.get(
        "/api/dashboard/rescues?content_kind=standalone"
    ).get_json()["data"]["items"][0]

    assert item["sources"] == [
        {"kind": "bookmark", "label": "我的收藏", "user_id": None, "user_name": None},
        {"kind": "subscribed_series", "label": "我的追更", "user_id": None, "user_name": None},
        {"kind": "following_user", "label": "关注用户：A作者", "user_id": 2, "user_name": "A作者"},
        {"kind": "following_user", "label": "关注用户：A作者", "user_id": 3, "user_name": "A作者"},
        {"kind": "following_user", "label": "关注用户：B作者", "user_id": 4, "user_name": "B作者"},
        {"kind": "user_backup", "label": "用户备份：作者", "user_id": 1, "user_name": "作者"},
        {"kind": "other", "label": "其他来源", "user_id": None, "user_name": None},
    ]


def test_dashboard_rescue_list_sorts_and_paginates_catalog(app, client) -> None:
    db = Database(Path(app.config["PIXIV_DB_PATH"]))
    try:
        db.conn.execute(
            """
            UPDATE rescue_catalog
            SET last_checked_at = CASE item_id
                    WHEN 10 THEN '2024-02-01 00:00:00'
                    ELSE '2024-01-01 00:00:00'
                END,
                updated_at = CASE item_id
                    WHEN 10 THEN '2024-01-01 00:00:00'
                    ELSE '2024-02-01 00:00:00'
                END
            """
        )
        db.conn.commit()
    finally:
        db.close()

    first = client.get(
        "/api/dashboard/rescues?sort=updated_desc&page=1&page_size=1"
    ).get_json()["data"]
    second = client.get(
        "/api/dashboard/rescues?sort=updated_desc&page=2&page_size=1"
    ).get_json()["data"]

    assert first["total"] == 2
    assert first["total_pages"] == 2
    assert first["page"] == 1
    assert second["page"] == 2
    assert len(first["items"]) == len(second["items"]) == 1
    assert first["items"][0]["item_id"] == 20
    assert second["items"][0]["item_id"] == 10
    checked = client.get(
        "/api/dashboard/rescues?sort=checked_desc&page=1&page_size=1"
    ).get_json()["data"]
    assert checked["items"][0]["item_id"] == 10


def test_dashboard_rescue_list_reports_catalog_freshness(
    app,
    client,
    monkeypatch,
) -> None:
    fresh = client.get("/api/dashboard/rescues").get_json()["data"]
    assert fresh["refreshed_at"]
    assert fresh["stale"] is False

    floor_stale_at = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    _set_catalog_refreshed_at(app, floor_stale_at)
    floor_stale = client.get("/api/dashboard/rescues").get_json()["data"]
    assert floor_stale["refreshed_at"] == floor_stale_at
    assert floor_stale["stale"] is True
    assert floor_stale["items"]

    original_load = SettingsManager.load

    def load_with_long_intervals(self, *args, **kwargs):
        settings = original_load(self, *args, **kwargs)
        settings.sync.auto_sync_novel_status_interval_hours = 18
        settings.sync.auto_sync_series_status_interval_hours = 30
        return settings

    monkeypatch.setattr(SettingsManager, "load", load_with_long_intervals)

    fresh_at = (datetime.now(timezone.utc) - timedelta(hours=59)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    _set_catalog_refreshed_at(app, fresh_at)

    long_interval_fresh = client.get("/api/dashboard/rescues").get_json()["data"]
    assert long_interval_fresh["refreshed_at"] == fresh_at
    assert long_interval_fresh["stale"] is False
    assert long_interval_fresh["items"]

    stale_at = (datetime.now(timezone.utc) - timedelta(hours=61)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    _set_catalog_refreshed_at(app, stale_at)

    stale = client.get("/api/dashboard/rescues").get_json()["data"]
    assert stale["refreshed_at"] == stale_at
    assert stale["stale"] is True
    assert stale["items"] == long_interval_fresh["items"]


@pytest.mark.parametrize(
    "query",
    ["content_kind=novel", "source_kind=other"],
)
def test_dashboard_rescue_list_rejects_invalid_catalog_filters(client, query) -> None:
    response = client.get(f"/api/dashboard/rescues?{query}")

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


@pytest.mark.parametrize("action", ["put", "delete"])
def test_dashboard_rescue_override_does_not_refresh_catalog_yet(
    client,
    monkeypatch,
    action,
) -> None:
    monkeypatch.setattr(
        Database,
        "refresh_rescue_item",
        lambda *_args: pytest.fail("增量刷新调用由任务 6 接入"),
    )

    if action == "put":
        response = client.put(
            "/api/dashboard/rescue-overrides/novel/10",
            json={"action": "exclude"},
        )
    else:
        response = client.delete("/api/dashboard/rescue-overrides/novel/10")

    assert response.status_code == 200


def test_rescue_public_api_requires_bearer_token(client) -> None:
    response = client.get("/api/rescue/v1/novels/10")

    assert response.status_code == 401
    assert "Bearer" in response.headers["WWW-Authenticate"]


def test_rescue_public_api_rejects_query_token(client) -> None:
    response = client.get("/api/rescue/v1/novels/10?token=secret")

    assert response.status_code == 401


def test_rescue_public_api_uses_rescue_auth_for_non_local_request(client) -> None:
    response = client.get(
        "/api/rescue/v1/novels/10",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )

    assert response.status_code == 401
    assert "Bearer" in response.headers["WWW-Authenticate"]


def test_dashboard_rotates_single_active_token(client) -> None:
    first = client.post("/api/dashboard/rescue-token/rotate")
    assert first.headers["Cache-Control"] == "no-store"
    old_token = str(first.get_json()["data"]["token"])
    new_token = _rotate_token(client)

    assert old_token != new_token
    assert client.get(
        "/api/rescue/v1/novels/10",
        headers={"Authorization": f"Bearer {old_token}"},
    ).status_code == 401
    assert client.get(
        "/api/rescue/v1/novels/10",
        headers={"Authorization": f"Bearer {new_token}"},
    ).status_code == 200

    status = client.get("/api/dashboard/rescue-token/status").get_json()["data"]
    assert status["configured"] is True
    assert status["token_prefix"].startswith("rsq_")
    assert "token" not in status
    assert "token_hash" not in status

    status_response = client.get("/api/dashboard/rescue-token/status")
    assert status_response.headers["Cache-Control"] == "no-store"


def test_rescue_novel_response_uses_field_whitelist_and_security_headers(client) -> None:
    token = _rotate_token(client)
    response = client.get(
        "/api/rescue/v1/novels/10",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["novel_id"] == 10
    assert data["text_raw"] == "正文"
    assert data["source_notice"] == "内容来自私人备份，并非 Pixiv 官方恢复"
    assert "raw_json" not in data
    assert "local_path" not in data
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"


def test_rescue_public_api_hides_normal_private_novel(client) -> None:
    token = _rotate_token(client)
    response = client.get(
        "/api/rescue/v1/novels/11",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


def test_rescue_parent_series_exposes_chapter_and_paginated_directory(client) -> None:
    token = _rotate_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    series = client.get("/api/rescue/v1/series/20", headers=headers)
    directory = client.get(
        "/api/rescue/v1/series/20/chapters?page=1&page_size=1",
        headers=headers,
    )
    chapter = client.get("/api/rescue/v1/novels/21", headers=headers)

    assert series.status_code == 200
    assert series.get_json()["data"]["rescue_state"] == "success"
    assert directory.status_code == 200
    assert directory.get_json()["data"]["items"][0]["novel_id"] == 21
    assert chapter.status_code == 200
    assert chapter.get_json()["data"]["eligibility_reason"] == "parent_series_unavailable"


def test_dashboard_rescue_list_and_override_crud(app, client) -> None:
    listed = client.get("/api/dashboard/rescues?item_type=novel&state=success")
    assert listed.status_code == 200
    assert listed.get_json()["data"]["items"][0]["novel_id"] == 10

    excluded = client.put(
        "/api/dashboard/rescue-overrides/novel/10",
        json={"action": "exclude", "note": "仍然可访问"},
    )
    assert excluded.status_code == 200
    _rebuild_catalog(app)
    assert client.get(
        "/api/dashboard/rescues?item_type=novel&state=success"
    ).get_json()["data"]["items"] == []

    restored = client.delete("/api/dashboard/rescue-overrides/novel/10")
    assert restored.status_code == 200
    _rebuild_catalog(app)
    assert client.get(
        "/api/dashboard/rescues?item_type=novel&state=success"
    ).get_json()["data"]["items"][0]["novel_id"] == 10


def test_dashboard_novel_detail_includes_rescue_evaluation(client) -> None:
    rescued = client.get("/api/dashboard/novels/10").get_json()
    normal = client.get("/api/dashboard/novels/11").get_json()

    assert rescued["rescue"]["rescue_state"] == "success"
    assert rescued["rescue"]["remote_status"] == "deleted"
    assert rescued["rescue"]["complete_count"] == 1
    assert rescued["rescue"]["override_action"] is None
    assert normal["rescue"]["rescue_state"] is None
    assert normal["rescue"]["remote_status"] == "normal"


def test_dashboard_series_detail_includes_rescue_coverage(client) -> None:
    detail = client.get("/api/dashboard/series/20").get_json()

    assert detail["rescue"]["rescue_state"] == "success"
    assert detail["rescue"]["expected_count"] == 1
    assert detail["rescue"]["local_count"] == 1
    assert detail["rescue"]["complete_count"] == 1
    assert detail["rescue"]["override_action"] is None


def test_dashboard_detail_reflects_manual_rescue_override(client) -> None:
    response = client.put(
        "/api/dashboard/rescue-overrides/novel/11",
        json={"action": "include", "note": "页面确认失效"},
    )

    assert response.status_code == 200
    rescue = client.get("/api/dashboard/novels/11").get_json()["rescue"]
    assert rescue["rescue_state"] == "success"
    assert rescue["override_action"] == "include"
    assert rescue["override_note"] == "页面确认失效"


def test_rescue_public_api_is_read_only(client) -> None:
    token = _rotate_token(client)
    response = client.post(
        "/api/rescue/v1/novels/10",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 405


@pytest.mark.parametrize(
    "path",
    [
        "/api/rescue/v1/novels/10",
        "/api/rescue/v1/series/20",
        "/api/rescue/v1/series/20/chapters",
    ],
)
def test_rescue_public_api_rejects_options(client, path: str) -> None:
    response = client.open(path, method="OPTIONS")

    assert response.status_code == 405
    assert "no-store" in response.headers["Cache-Control"]


def test_rescue_public_api_returns_429_when_limiter_rejects(app, client, monkeypatch) -> None:
    token = _rotate_token(client)
    limiter = app.extensions["rescue_rate_limiter"]
    monkeypatch.setattr(limiter, "allow", lambda _key: False)

    response = client.get(
        "/api/rescue/v1/novels/10",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 429


def test_public_rescue_prefix_bypasses_dashboard_session_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "admin-secret")
    db_path = tmp_path / "secured" / "rescue.db"
    monkeypatch.setenv("PIXIV_DB_PATH", str(db_path))
    env_path = tmp_path / "secured.env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    secured_app = create_app(env_path=str(env_path))
    secured_app.config.update(TESTING=True)
    secured_client = secured_app.test_client()

    response = secured_client.get("/api/rescue/v1/novels/10")

    assert response.status_code == 401
    assert "Bearer" in response.headers["WWW-Authenticate"]


def test_dashboard_rescue_override_uses_existing_csrf_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "admin-secret")
    db_path = tmp_path / "csrf" / "rescue.db"
    monkeypatch.setenv("PIXIV_DB_PATH", str(db_path))
    env_path = tmp_path / "csrf.env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    csrf_app = create_app(env_path=str(env_path))
    csrf_app.config.update(TESTING=True)
    _seed_rescue_data(db_path)
    csrf_client = csrf_app.test_client()
    with csrf_client.session_transaction() as session:
        session["authenticated"] = True

    blocked = csrf_client.put(
        "/api/dashboard/rescue-overrides/novel/10",
        json={"action": "exclude"},
    )
    csrf_token = csrf_client.get("/api/csrf-token").get_json()["csrf_token"]
    allowed = csrf_client.put(
        "/api/dashboard/rescue-overrides/novel/10",
        json={"action": "exclude"},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert blocked.status_code == 403
    assert allowed.status_code == 200


def test_public_rescue_api_redacts_database_errors(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _rotate_token(client)

    def fail_read(_self, _novel_id):
        raise RuntimeError("C:/private/archive/path")

    monkeypatch.setattr(Database, "get_rescue_novel", fail_read)
    response = client.get(
        "/api/rescue/v1/novels/10",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 500
    assert response.is_json
    assert response.get_json()["error"] == "救援内容读取失败"
    assert "private" not in response.get_data(as_text=True)

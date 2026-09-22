from __future__ import annotations

from types import SimpleNamespace

from pixiv_novel_sync.storage_db import Database


def test_task_log_page_size_is_clamped(tmp_path) -> None:
    db = Database(tmp_path / "logs.db")
    db.init_schema()
    try:
        zero = db.get_task_logs(page=0, page_size=0)
        huge = db.get_task_logs(page=-4, page_size=9999)
        ai_zero = db.get_ai_task_logs(page=0, page_size=-1)
    finally:
        db.close()

    assert zero["page"] == 1
    assert zero["page_size"] == 1
    assert huge["page"] == 1
    assert huge["page_size"] == 200
    assert ai_zero["page"] == 1
    assert ai_zero["page_size"] == 1


def test_empty_cover_does_not_replace_existing_series_cover(tmp_path) -> None:
    db = Database(tmp_path / "series.db")
    db.init_schema()
    try:
        db.upsert_subscribed_series(5, "系列", "说明", 1, "https://img/a.jpg", 2)
        db.upsert_subscribed_series(5, "系列", "说明", 1, "", 2)
        cover = db.conn.execute("SELECT cover_url FROM series WHERE series_id = 5").fetchone()[0]
    finally:
        db.close()

    assert cover == "https://img/a.jpg"


def test_quoted_empty_object_does_not_replace_user_raw_json(tmp_path) -> None:
    db = Database(tmp_path / "users.db")
    db.init_schema()
    try:
        db.upsert_user(SimpleNamespace(user_id=8, name="作者", account="a", raw_json='{"id":8}'))
        db.upsert_user(SimpleNamespace(user_id=8, name="作者", account="a", raw_json='"{}"'))
        raw = db.conn.execute("SELECT raw_json FROM users WHERE user_id = 8").fetchone()[0]
    finally:
        db.close()

    assert raw == '{"id":8}'

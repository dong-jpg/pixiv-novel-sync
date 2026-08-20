"""本人账号资料（含会员状态）落库与读取。"""
from __future__ import annotations

from pixiv_novel_sync.auth import AuthResult
from pixiv_novel_sync.storage_db import Database


def _db(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init_schema()
    return db


def test_auth_result_extracts_self_profile_with_premium():
    res = AuthResult(
        access_token="a", refresh_token="r", user_id=None,
        raw={"user": {
            "id": "34936679", "name": "乐少", "account": "1248683871",
            "is_premium": True, "x_restrict": 2, "is_mail_authorized": True,
            "profile_image_urls": {"px_170x170": "https://i.pximg.net/a_170.jpg", "px_50x50": "https://i.pximg.net/a_50.jpg"},
        }},
    )
    p = res.self_profile()
    assert p["user_id"] == 34936679
    assert p["name"] == "乐少"
    assert p["account"] == "1248683871"
    assert p["is_premium"] is True
    assert p["avatar_url"] == "https://i.pximg.net/a_170.jpg"


def test_auth_result_self_profile_none_without_user_payload():
    assert AuthResult(access_token=None, refresh_token=None, user_id=1, raw={}).self_profile() is None


def test_auth_result_self_profile_falls_back_to_smaller_avatar():
    res = AuthResult(access_token=None, refresh_token=None, user_id=7,
                     raw={"user": {"id": "7", "profile_image_urls": {"px_50x50": "https://x/50.jpg"}}})
    assert res.self_profile()["avatar_url"] == "https://x/50.jpg"


def test_user_summary_returns_saved_self_profile(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_self_profile({"user_id": 42, "name": "我", "account": "me", "avatar_url": "u", "is_premium": True})
        s = db.get_user_summary(42)
        assert s["name"] == "我"
        assert s["is_premium"] is True
        assert s["is_self"] is True
        assert s["is_fallback"] is False
    finally:
        db.close()


def test_user_summary_never_falls_back_to_recently_synced_author(tmp_path):
    """回归：侧边栏曾把「最近同步的作者」当成本人账号展示。"""
    db = _db(tmp_path)
    try:
        db.conn.execute(
            "INSERT INTO users (user_id, name, account, raw_json) VALUES (?,?,?,?)",
            (74270071, "不知火/钝刀飞雪", "user_xspm4238", "{}"),
        )
        db.conn.commit()
        s = db.get_user_summary(34936679)
        assert s["user_id"] == 34936679
        assert s["name"] != "不知火/钝刀飞雪"
        assert s["is_fallback"] is True
    finally:
        db.close()


def test_user_summary_ignores_self_profile_of_other_account(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_self_profile({"user_id": 111, "name": "旧账号", "is_premium": True})
        s = db.get_user_summary(222)
        assert s["name"] != "旧账号"
        assert s["user_id"] == 222
    finally:
        db.close()


def test_user_summary_none_without_user_id_and_profile(tmp_path):
    db = _db(tmp_path)
    try:
        assert db.get_user_summary(None) is None
    finally:
        db.close()

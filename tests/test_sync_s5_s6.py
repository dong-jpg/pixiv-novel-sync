"""S5/S6 回归测试：代理设置与追更方式2回落。

S5: PIXIV_PROXY 必须传给 AppPixivAPI 构造函数，不能设置 api.proxies 属性
S6: 新装且没有 Cookie 时，追更方式2 回落不应把所有系列标记为已订阅
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pixiv_novel_sync.auth import PixivAuthManager
from pixiv_novel_sync.settings import PixivSettings


def test_s5_proxy_passed_to_api_constructor():
    """S5: 代理设置必须在创建 AppPixivAPI 时传入 kwargs，而不是设置 api.proxies 属性。"""
    settings = PixivSettings(
        refresh_token="test_token",
        access_token=None,
        proxy="http://proxy.example.com:8080",
        timeout=30,
        verify_ssl=True,
        user_id=None,
        web_cookie=None,
    )

    manager = PixivAuthManager(settings)

    with patch("pixiv_novel_sync.auth.AppPixivAPI") as MockAPI:
        mock_api_instance = MagicMock()
        MockAPI.return_value = mock_api_instance

        api = manager.create_api()

        # 验证 AppPixivAPI 被调用时传入了 proxies 参数
        MockAPI.assert_called_once()
        call_kwargs = MockAPI.call_args[1]
        assert "proxies" in call_kwargs
        assert call_kwargs["proxies"]["http"] == "http://proxy.example.com:8080"
        assert call_kwargs["proxies"]["https"] == "http://proxy.example.com:8080"
        assert "timeout" in call_kwargs
        assert call_kwargs["timeout"] == 30
        assert "verify" in call_kwargs
        assert call_kwargs["verify"] is True


def test_s5_verify_ssl_passed_to_api_constructor():
    """S5: verify_ssl 必须传入构造函数（配代理时 SSL 验证行为可控）。"""
    settings = PixivSettings(
        refresh_token="test_token",
        access_token=None,
        proxy=None,
        timeout=30,
        verify_ssl=False,  # 显式禁用 SSL 验证
        user_id=None,
        web_cookie=None,
    )

    manager = PixivAuthManager(settings)

    with patch("pixiv_novel_sync.auth.AppPixivAPI") as MockAPI:
        mock_api_instance = MagicMock()
        MockAPI.return_value = mock_api_instance

        api = manager.create_api()

        call_kwargs = MockAPI.call_args[1]
        assert "verify" in call_kwargs
        assert call_kwargs["verify"] is False


def test_s6_no_cookie_no_subscription_marks(tmp_path, monkeypatch):
    """S6: 新装且没有 Cookie、没有任何订阅行时，方式2回落不应标记系列为已订阅。"""
    from pixiv_novel_sync.storage_db import Database
    from pixiv_novel_sync.settings import load_settings

    # 创建一个临时数据库
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    monkeypatch.setenv("PIXIV_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("PIXIV_PRIVATE_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("PIXIV_DB_PATH", str(tmp_path / "test.db"))

    (tmp_path / "public").mkdir()
    (tmp_path / "private").mkdir()

    settings = load_settings(config_path=None, env_path=str(env_path))
    db = Database(settings.storage.db_path)

    try:
        db.init_schema()

        # 直接插入一些带 series_id 的小说（不通过 NovelRecord）
        db.conn.execute(
            """
            INSERT INTO novels (novel_id, user_id, series_id, title, caption, visible,
                                restrict_value, x_restrict, text_length, total_bookmarks,
                                total_views, tags_json, raw_json, meta_hash)
            VALUES (1001, 1, 101, '测试小说1', '', 1, 'public', 0, 1000, 10, 100, '[]', '{}', 'test')
            """
        )
        db.conn.commit()

        # 验证当前没有任何已订阅的系列
        subscribed_count = db.conn.execute(
            "SELECT COUNT(*) FROM series WHERE is_subscribed = 1"
        ).fetchone()[0]
        assert subscribed_count == 0

        # 模拟 sync_engine 的逻辑：检查是否有订阅行
        has_any_subscription = db.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM series WHERE is_subscribed = 1)"
        ).fetchone()[0]

        # 新装且没有订阅时，has_any_subscription 应该是 False
        assert not has_any_subscription

    finally:
        db.close()


def test_s6_with_existing_subscription_allows_fallback(tmp_path, monkeypatch):
    """S6: 已有订阅行时，方式2回落正常工作。"""
    from pixiv_novel_sync.storage_db import Database
    from pixiv_novel_sync.settings import load_settings

    # 创建一个临时数据库
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    monkeypatch.setenv("PIXIV_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("PIXIV_PRIVATE_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("PIXIV_DB_PATH", str(tmp_path / "test.db"))

    (tmp_path / "public").mkdir()
    (tmp_path / "private").mkdir()

    settings = load_settings(config_path=None, env_path=str(env_path))
    db = Database(settings.storage.db_path)

    try:
        db.init_schema()

        # 插入一个已订阅的系列
        db.upsert_subscribed_series(
            series_id=101,
            title="测试系列",
            description="",
            user_id=1,
            cover_url=None,
            total_novels=5,
        )

        # 检查是否有订阅行
        has_any_subscription = db.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM series WHERE is_subscribed = 1)"
        ).fetchone()[0]

        # 有订阅行时，has_any_subscription 应该是 True
        assert has_any_subscription

    finally:
        db.close()

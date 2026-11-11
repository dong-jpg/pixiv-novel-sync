"""全量备份：已归档的跳过，每位作者每 7 天全部重抓一次（审查性能 P19，用户已拍板）。

以前每本都 novel_detail + webview_novel 两次请求、逐本没有间隔，是单轮最长 2.3 小时的
主因。全部重抓保留为唯一会重抓已归档正文的路径（作者改正文、S2 那种占位数据的修复途径），
上次全部重抓的时间只有该作者那一轮完整跑完才记。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from pixiv_novel_sync.jobs import quick_sync, services
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.storage_files import FileStorage

AUTHOR_ID = 7
NOVEL_IDS = [4242, 4243]
MARK_KEY = "user_backup_full_refresh"


class _OfflineStorage(FileStorage):
    def download_asset(self, url, target, timeout, verify_ssl, proxy, max_retries=3, stop_requested=None):
        return self.write_bytes(Path(target), b"img")


def _novel(novel_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=novel_id,
        user=SimpleNamespace(id=AUTHOR_ID, name="author", account="acc"),
        caption="",
        tags=[],
        image_urls=SimpleNamespace(large=f"https://i.pximg.net/c/{novel_id}.jpg", medium=None, square_medium=None),
        series=None,
        title=f"title {novel_id}",
        visible=True,
        x_restrict=0,
        text_length=4,
        total_bookmarks=1,
        total_view=1,
        create_date="2026-01-01T00:00:00+00:00",
    )


class _Api:
    def __init__(self) -> None:
        self.detail_calls: list[int] = []

    def user_novels(self, **query):
        return SimpleNamespace(novels=[SimpleNamespace(id=i, title=str(i)) for i in NOVEL_IDS], next_url=None)

    def novel_detail(self, novel_id):
        self.detail_calls.append(int(novel_id))
        return SimpleNamespace(novel=_novel(int(novel_id)))

    def webview_novel(self, novel_id):
        return {"text": "body"}

    def parse_qs(self, next_url):
        return None


def _env(tmp_path: Path, monkeypatch, item_delay: float = 0.0):
    settings = SimpleNamespace(
        pixiv=SimpleNamespace(timeout=1, verify_ssl=True, proxy=None, web_cookie=None, user_id=999),
        sync=SimpleNamespace(
            delay_seconds_between_pages=0,
            delay_seconds_between_items=item_delay,
            delay_seconds_between_skips=0,
            max_items_per_run=None,
            max_pages_per_run=None,
            user_backup_max_pages_per_run=None,
            auto_sync_following_novels_users_limit=0,
            download_assets=True,
            write_markdown=True,
            write_raw_text=True,
        ),
        storage=SimpleNamespace(
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
            db_path=tmp_path / "state" / "test.db",
        ),
    )
    settings.storage.db_path.parent.mkdir(parents=True)
    db = Database(settings.storage.db_path)
    db.init_schema()
    db.conn.execute("INSERT INTO users(user_id, name, raw_json) VALUES(?, 'author', '{}')", (AUTHOR_ID,))
    db.conn.commit()
    db.close()
    api = _Api()
    monkeypatch.setattr(services, "_login", lambda s: api)
    monkeypatch.setattr(services, "FileStorage", _OfflineStorage)
    return settings, api


def _watermark(settings):
    db = Database(settings.storage.db_path)
    try:
        return db.get_watermark(MARK_KEY)
    finally:
        db.close()


def test_archived_novels_are_skipped_without_requests_until_the_next_full_refresh(tmp_path, monkeypatch) -> None:
    settings, api = _env(tmp_path, monkeypatch)

    first = services.run_user_backup_task(settings, AUTHOR_ID)
    second = services.run_user_backup_task(settings, AUTHOR_ID)

    assert first["full_refresh"] is True and first["novels"] == 2
    assert str(AUTHOR_ID) in _watermark(settings)["full_refresh_at"]
    # 7 天内再来：已归档的两本都不发请求
    assert second["full_refresh"] is False
    assert second["skipped_archived"] == 2
    assert api.detail_calls == NOVEL_IDS


def test_full_refresh_again_after_seven_days(tmp_path, monkeypatch) -> None:
    settings, api = _env(tmp_path, monkeypatch)
    services.run_user_backup_task(settings, AUTHOR_ID)
    db = Database(settings.storage.db_path)
    try:
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        db.update_watermark(MARK_KEY, {"full_refresh_at": {str(AUTHOR_ID): stale}})
    finally:
        db.close()

    stats = services.run_user_backup_task(settings, AUTHOR_ID)

    assert stats["full_refresh"] is True
    assert stats["skipped_archived"] == 0
    assert api.detail_calls == NOVEL_IDS * 2  # 已归档的也重抓


def test_interrupted_full_refresh_is_not_recorded(tmp_path, monkeypatch) -> None:
    settings, api = _env(tmp_path, monkeypatch)
    calls = {"n": 0}

    def stop_after_first_novel() -> bool:
        calls["n"] += 1
        return bool(api.detail_calls)

    stopped = services.run_user_backup_task(settings, AUTHOR_ID, stop_requested=stop_after_first_novel)
    after = services.run_user_backup_task(settings, AUTHOR_ID)

    assert stopped["stopped"] is True
    # 没跑完不记：下一轮仍全部重抓
    assert after["full_refresh"] is True


def test_requests_wait_between_items(tmp_path, monkeypatch) -> None:
    settings, api = _env(tmp_path, monkeypatch, item_delay=0.5)
    slept: list[float] = []
    monkeypatch.setattr(services, "time", SimpleNamespace(sleep=slept.append))

    services.run_user_backup_task(settings, AUTHOR_ID)

    # 每本发完请求后间隔 0.5 s（_sleep_with_cancel 按 0.2 s 分段，便于取消）
    assert len(api.detail_calls) == 2
    assert abs(sum(slept) - 1.0) < 1e-6


def test_scheduled_backup_reports_which_mode_each_round_used(tmp_path, monkeypatch) -> None:
    settings, _api = _env(tmp_path, monkeypatch)

    first = quick_sync.run_scheduled_user_backup(settings)
    second = quick_sync.run_scheduled_user_backup(settings)

    assert first["full_refresh_users"] == 1 and first["skipped_archived"] == 0
    assert second["full_refresh_users"] == 0 and second["skipped_archived"] == 2

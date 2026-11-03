"""已确认 deleted 的小说降频复查（审查性能 P21）。

novel_status 与待删检测以前每轮都对已标 deleted 的作品重查 novel_detail：novel_status
每批约 10% 是重复确认，待删检测的失败分支还不间隔、连发。现在最近 7 天内复查过的
deleted 作品本轮不发请求；超过 7 天照常复查（作品可能被恢复）。
「已知删除不清零 missing 连击」与 confirmed_missing 的规则不受影响。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from pixiv_novel_sync.jobs import services
from pixiv_novel_sync.models import SourceRecord
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService


def _days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _seed(db: Database, rows: list[tuple[int, str, str | None]]) -> None:
    db.conn.execute("INSERT INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')")
    for novel_id, status, checked_at in rows:
        db.conn.execute(
            """
            INSERT INTO novels (
                novel_id, title, user_id, visible, restrict_value, x_restrict,
                text_length, total_bookmarks, total_views, tags_json, raw_json, meta_hash, status, last_checked_at
            ) VALUES (?, ?, 1, 1, 'public', 0, 0, 0, 0, '[]', '{}', 'h', ?, ?)
            """,
            (novel_id, f"作品{novel_id}", status, checked_at),
        )
    db.conn.commit()


def _status_settings(tmp_path: Path, batch_size: int) -> SimpleNamespace:
    return SimpleNamespace(
        pixiv=SimpleNamespace(user_id=999),
        storage=SimpleNamespace(db_path=tmp_path / "test.db"),
        sync=SimpleNamespace(delay_seconds_between_skips=0, novel_status_batch_size=batch_size),
    )


def test_novel_status_defers_recently_confirmed_deleted_and_fills_the_batch(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "test.db")
    db.init_schema()
    _seed(
        db,
        [
            (1, "normal", None),  # 从未检查，排最前
            (2, "deleted", _days_ago(2)),  # 2 天前确认删除：降频，本轮不查
            (3, "deleted", _days_ago(10)),  # 10 天前：照常复查
            (4, "normal", _days_ago(1)),
        ],
    )
    db.close()
    checked: list[int] = []

    def check(api, novel_id: int) -> str:
        checked.append(int(novel_id))
        return "deleted" if novel_id == 3 else "normal"

    monkeypatch.setattr(services, "_login", lambda settings: SimpleNamespace())
    monkeypatch.setattr(services, "_ensure_storage_dirs", lambda settings: None)
    monkeypatch.setattr(services, "_check_novel_status", check)

    stats = services.run_novel_status_task(_status_settings(tmp_path, batch_size=3))

    # 名额让给后面的作品，而不是被降频的那本占着
    assert checked == [1, 3, 4]
    assert stats["deleted_recheck_deferred"] == 1
    # 10 天前那本仍是「早就知道没了」的再次确认，不进 missing 连击
    assert stats["confirmed_missing"] == 1
    assert stats["remaining"] == 0


class _DetectApi:
    def __init__(self, remote_ids: list[int]) -> None:
        self.remote_ids = list(remote_ids)
        self.detail_calls: list[int] = []

    def user_bookmarks_novel(self, **kwargs):
        return SimpleNamespace(novels=[SimpleNamespace(id=i) for i in self.remote_ids], next_url=None)

    def novel_detail(self, novel_id):
        self.detail_calls.append(int(novel_id))
        return SimpleNamespace(novel=SimpleNamespace(id=novel_id))

    def parse_qs(self, next_url):
        return None


def test_pending_detection_skips_recently_confirmed_deleted_candidates(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.init_schema()
    try:
        _seed(db, [(1, "deleted", _days_ago(2)), (2, "normal", _days_ago(1)), (9, "normal", _days_ago(1))])
        for novel_id in (1, 2, 9):
            db.upsert_source(SourceRecord(novel_id=novel_id, source_type="bookmark_public", source_key="999"))
        db.conn.commit()
        api = _DetectApi(remote_ids=[9])  # 1 和 2 都不在远端收藏里
        settings = SimpleNamespace(
            pixiv=SimpleNamespace(timeout=1, verify_ssl=True, proxy=None, web_cookie=None, user_id=999),
            sync=SimpleNamespace(
                delay_seconds_between_pages=0, delay_seconds_between_items=0, delay_seconds_between_skips=0
            ),
        )
        service = BookmarkNovelSyncService(api, db, SimpleNamespace(), settings)

        stats = service.detect_unbookmarked_novels(999, ["public"])
    finally:
        db.close()

    # 已确认删除的那本不再逐本 novel_detail；正常的那本照常判定为取消收藏
    assert api.detail_calls == [2]
    assert stats["skipped_known_deleted"] == 1
    assert stats["new_pending"] == 1

from __future__ import annotations

from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService
from tests.test_sync_engine_incremental import _settings


def _subscribed(db: Database, count: int) -> None:
    for series_id in range(1, count + 1):
        db.conn.execute(
            """
            INSERT INTO series (series_id, title, user_id, total_novels, is_subscribed)
            VALUES (?, '系列', 1, 1, 1)
            """,
            (series_id,),
        )


def test_only_novel_series_errors_trip_the_circuit(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.storage.db_path)
    db.init_schema()
    _subscribed(db, 5)

    class _Api:
        def novel_series(self, series_id, **kwargs):
            return {
                "novel_series_detail": {
                    "title": "系列",
                    "caption": "",
                    "user": {"id": 1, "name": "作者", "account": "a"},
                    "content_count": 1,
                },
                "novels": [{"id": series_id * 10}],
                "next_url": None,
            }

    service = BookmarkNovelSyncService(_Api(), db, object(), settings)
    monkeypatch.setattr(service, "_sync_novel", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("章节失败")))

    stats = service.sync_subscribed_series()

    assert stats.get("aborted_reason") is None
    assert stats.get("failed", 0) >= 5
    db.close()

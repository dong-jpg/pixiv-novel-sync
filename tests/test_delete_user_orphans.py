from __future__ import annotations

from pixiv_novel_sync.storage_db import Database


def _novel(db: Database, novel_id: int, user_id: int, series_id: int | None) -> None:
    db.conn.execute(
        """
        INSERT INTO novels (
            novel_id, title, user_id, series_id, visible, restrict_value, x_restrict,
            text_length, total_bookmarks, total_views, tags_json, raw_json, meta_hash
        ) VALUES (?, '一篇', ?, ?, 1, 'public', 0, 10, 0, 0, '[]', '{}', 'h')
        """,
        (novel_id, user_id, series_id),
    )


def test_delete_user_removes_series_and_orphan_rows(tmp_path) -> None:
    db = Database(tmp_path / "user.db")
    db.init_schema()
    db.conn.execute("INSERT INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')")
    db.upsert_subscribed_series(3, "系列", "", 1, "https://img/a.jpg", 1)
    _novel(db, 10, 1, 3)
    db.conn.execute("INSERT INTO reading_progress (novel_id, progress) VALUES (10, 4)")
    db.conn.execute("INSERT INTO preference_analyzed_novels (novel_id) VALUES (10)")

    db.delete_user(1)

    assert db.list_following_series()["total"] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM reading_progress").fetchone()[0] == 0
    # D4：已分析标记保留（理由见下一个测试）
    assert db.conn.execute("SELECT COUNT(*) FROM preference_analyzed_novels").fetchone()[0] == 1
    db.close()


def test_delete_novel_clears_progress_keeps_analysis_mark(tmp_path) -> None:
    db = Database(tmp_path / "novel.db")
    db.init_schema()
    db.conn.execute("INSERT INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')")
    _novel(db, 10, 1, None)
    db.conn.execute("INSERT INTO reading_progress (novel_id, progress) VALUES (10, 4)")
    db.conn.execute("INSERT INTO preference_analyzed_novels (novel_id) VALUES (10)")

    db.delete_novel(10)

    assert db.conn.execute("SELECT COUNT(*) FROM reading_progress").fetchone()[0] == 0
    # D4：词频累加器不能按本扣减。删掉标记的话，作品被同步带回后会再分析一次，
    # 它的词频与 novel_count 就算了两遍。
    assert db.conn.execute("SELECT COUNT(*) FROM preference_analyzed_novels").fetchone()[0] == 1
    db.close()


def test_novel_synced_back_after_delete_is_not_counted_twice(tmp_path) -> None:
    from pixiv_novel_sync.preferences import PreferenceAnalyzer

    db = Database(tmp_path / "twice.db")
    db.init_schema()
    db.conn.execute("INSERT INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')")

    def seed() -> None:
        db.conn.execute(
            """
            INSERT INTO novels (
                novel_id, title, user_id, visible, restrict_value, x_restrict,
                text_length, total_bookmarks, total_views, tags_json, raw_json, meta_hash
            ) VALUES (10, '一篇', 1, 1, 'public', 0, 2000, 0, 0, '["百合"]', '{}', 'h')
            """
        )
        db.conn.execute(
            "INSERT INTO novel_texts (novel_id, text_raw, has_content, text_hash) VALUES (10, ?, 1, 't')",
            ("正文" * 1000,),
        )

    seed()
    analyzer = PreferenceAnalyzer(db)
    analyzer.analyze_incremental(batch_size=10, max_batches=5)
    db.delete_novel(10)
    seed()  # 同步又把它带回来
    analyzer.analyze_incremental(batch_size=10, max_batches=5)

    assert db.get_preference_accumulator()["novel_count"] == 1
    tag = db.conn.execute(
        "SELECT count FROM preference_term_counts WHERE term_type = 'tag' AND term = '百合'"
    ).fetchone()
    assert tag is not None and tag[0] == 1
    db.close()

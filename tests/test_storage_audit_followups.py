"""2026-10-28 审查存储与推荐批次的回归测试：D5、D6、P10–P13。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pixiv_novel_sync.preferences import PreferenceAnalyzer
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path: Path):
    instance = Database(tmp_path / "audit.db")
    instance.init_schema()
    yield instance
    instance.close()


def _novel(db: Database, novel_id: int, *, text: str = "正文" * 600, status: str = "normal",
           series_id: int | None = None, tags: list[str] | None = None, bookmarks: int = 0) -> None:
    db.conn.execute(
        """
        INSERT INTO novels (novel_id, user_id, series_id, title, visible, restrict_value, x_restrict,
            text_length, total_bookmarks, total_views, tags_json, raw_json, meta_hash, status)
        VALUES (?, 1, ?, ?, 1, 'public', 0, ?, ?, 0, ?, '{}', 'h', ?)
        """,
        (novel_id, series_id, f"小说{novel_id}", len(text), bookmarks,
         json.dumps(tags or [], ensure_ascii=False), status),
    )
    db.conn.execute(
        "INSERT INTO novel_texts (novel_id, text_raw, has_content, text_hash) VALUES (?, ?, ?, 't')",
        (novel_id, text, 1 if text.strip() else 0),
    )


def _plan(db: Database, sql: str, params: tuple = ()) -> str:
    return " ".join(str(row[3]) for row in db.conn.execute("EXPLAIN QUERY PLAN " + sql, params))


# ---------------------------------------------------------------- D5


def test_prune_leaves_headroom_so_new_terms_survive_the_next_batch(db: Database) -> None:
    """D5：词项表过了阈值之后，下一批新词不能立刻被清掉。"""
    db.conn.executemany(
        "INSERT INTO preference_term_counts (term_type, term, count) VALUES ('keyword', ?, ?)",
        [(f"老词{i}", 2 + i % 5) for i in range(120)],
    )
    db.conn.executemany(
        "INSERT INTO preference_term_counts (term_type, term, count) VALUES ('keyword', ?, 1)",
        [(f"一次{i}",) for i in range(40)],
    )
    assert db.prune_preference_term_noise(("keyword",), max_rows=100) > 0
    remaining = db.conn.execute("SELECT COUNT(*) FROM preference_term_counts").fetchone()[0]
    assert remaining <= 70  # 清到阈值的 70% 以下，而不是刚好卡在阈值边上

    db.conn.executemany(
        "INSERT INTO preference_term_counts (term_type, term, count) VALUES ('keyword', ?, 1)",
        [(f"新词{i}",) for i in range(20)],
    )
    assert db.prune_preference_term_noise(("keyword",), max_rows=100) == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM preference_term_counts WHERE term LIKE '新词%'"
    ).fetchone()[0] == 20


def test_analyze_prunes_once_per_run_not_per_batch(db: Database, monkeypatch) -> None:
    for novel_id in range(1, 7):
        _novel(db, novel_id)
    calls: list[int] = []
    original = db.prune_preference_term_noise
    monkeypatch.setattr(db, "prune_preference_term_noise", lambda *a, **k: calls.append(1) or original(*a, **k))
    PreferenceAnalyzer(db).analyze_incremental(batch_size=2, max_batches=10)
    assert len(calls) == 1


# ---------------------------------------------------------------- D6


def test_transaction_inside_read_transaction_fails_with_clear_message(db: Database) -> None:
    with pytest.raises(RuntimeError, match="read_transaction"):
        with db.read_transaction():
            with db.transaction():
                pass
    assert not db.conn.in_transaction
    with db.transaction():  # 之后照常可用
        db.conn.execute("INSERT INTO users (user_id, name, raw_json) VALUES (7, '作者', '{}')")
    assert db.conn.execute("SELECT name FROM users WHERE user_id = 7").fetchone()[0] == "作者"


def test_read_transaction_inside_transaction_still_joins(db: Database) -> None:
    with db.transaction() as conn:
        with db.read_transaction() as read_conn:
            assert read_conn is conn


# ---------------------------------------------------------------- P10 索引


@pytest.mark.parametrize(
    ("sql", "index"),
    [
        ("SELECT novel_id FROM novels ORDER BY total_bookmarks DESC, novel_id DESC LIMIT 24", "idx_novels_bookmarks"),
        (
            "SELECT series_id FROM series WHERE is_subscribed = 1 "
            "ORDER BY last_seen_at DESC, series_id DESC LIMIT 12",
            "idx_series_subscribed_seen",
        ),
        (
            "SELECT id FROM recommendation_items ORDER BY run_id DESC, score DESC, id DESC LIMIT 10",
            "idx_recommendation_items_run_score",
        ),
        ("DELETE FROM recommendation_items WHERE novel_id = 1", "idx_recommendation_items_novel_id"),
    ],
)
def test_hot_queries_use_their_index(db: Database, sql: str, index: str) -> None:
    plan = _plan(db, sql)
    assert index in plan, plan
    assert "TEMP B-TREE" not in plan, plan


# ---------------------------------------------------------------- P10 推荐清理


def _run(db: Database) -> int:
    return db.create_recommendation_run(1, {}, status="succeeded")


def _item(db: Database, run_id: int, novel_id: int, status: str = "new") -> None:
    db.upsert_recommendation_item({
        "run_id": run_id, "profile_id": 1, "item_type": "novel", "novel_id": novel_id,
        "title": f"推荐{novel_id}", "score": 1.0, "status": status,
    })


def test_prune_recommendations_is_conservative_and_keeps_dedup(db: Database) -> None:
    first = _run(db)
    _item(db, first, 101)                      # 旧、new、无反馈 → 清
    _item(db, first, 102)                      # 旧、但有反馈 → 留
    _item(db, first, 103, status="dismissed")  # 旧、用户表过态 → 留
    db.create_recommendation_feedback({"item_type": "novel", "novel_id": 102, "feedback_type": "interested"})
    for _ in range(3):
        latest = _run(db)
    _item(db, latest, 201)                     # 最近一轮 → 留

    assert db.prune_recommendation_items(keep_runs=3) == 1
    remaining = {row[0] for row in db.conn.execute("SELECT novel_id FROM recommendation_items")}
    assert remaining == {102, 103, 201}
    # 清掉的 101 照样算「推荐过」，不会被再次推荐
    assert 101 in db.get_recommendation_filter_state()["recommended_novel_ids"]
    # 幂等
    assert db.prune_recommendation_items(keep_runs=3) == 0


def test_prune_recommendations_noop_with_few_runs(db: Database) -> None:
    run_id = _run(db)
    _item(db, run_id, 1)
    assert db.prune_recommendation_items(keep_runs=30) == 0


# ---------------------------------------------------------------- P11 / P12


def test_unanalyzed_count_uses_has_content(db: Database) -> None:
    """P11：统计改用 has_content，不再读整段 text_raw；只含空白的正文与 has_content 判定一致。"""
    _novel(db, 1)
    _novel(db, 2, text="   \n　 " * 200)  # 只有空白：has_content = 0，不算
    assert db.count_unanalyzed_preference_rows(100) == 1


def test_init_schema_backfill_probes_use_partial_indexes(db: Database) -> None:
    for sql in (
        "SELECT 1 FROM novels WHERE source_url IS NULL OR source_url = '' LIMIT 1",
        "SELECT 1 FROM novels WHERE archive_dir IS NULL OR archive_dir = '' LIMIT 1",
    ):
        plan = _plan(db, sql)
        assert "idx_novels_missing_" in plan, plan


def test_backfill_still_runs_for_legacy_rows(db: Database) -> None:
    _novel(db, 5)
    db.conn.execute("UPDATE novels SET source_url = NULL, archive_dir = NULL WHERE novel_id = 5")
    db.init_schema()
    row = db.conn.execute("SELECT source_url, archive_dir FROM novels WHERE novel_id = 5").fetchone()
    assert row[0] and row[0].endswith("5")
    assert row[1]


# ---------------------------------------------------------------- P13


def test_catalog_candidate_prefilter_matches_full_scan(db: Database) -> None:
    db.conn.execute("INSERT INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')")
    db.conn.execute("INSERT INTO series (series_id, title, user_id, total_novels, status) VALUES (30, '删系列', 1, 2, 'deleted')")
    db.conn.execute("INSERT INTO series (series_id, title, user_id, total_novels, status) VALUES (31, '正常系列', 1, 1, 'normal')")
    db.conn.execute("INSERT INTO series (series_id, title, user_id, total_novels, status) VALUES (32, '手动系列', 1, 1, 'normal')")
    _novel(db, 1, status="deleted")
    _novel(db, 2, status="restricted")
    _novel(db, 3, status="normal")
    _novel(db, 4, status="normal")            # 人工 include
    _novel(db, 5, status="deleted", series_id=30)
    _novel(db, 6, status="normal", series_id=30)
    _novel(db, 7, status="deleted", series_id=31)
    _novel(db, 8, status="normal", series_id=32)
    db.conn.execute("INSERT INTO rescue_overrides (item_type, item_id, action) VALUES ('novel', 4, 'include')")
    db.conn.execute("INSERT INTO rescue_overrides (item_type, item_id, action) VALUES ('series', 32, 'include')")

    full_series = db._catalog_series_rows()
    full_ids = {int(row["series_id"]) for row in full_series}
    assert db._catalog_series_rows(candidates_only=True) == full_series
    assert db._catalog_novel_rows(full_ids, candidates_only=True) == db._catalog_novel_rows(full_ids)

    db.rebuild_rescue_catalog()
    catalog = {(row[0], row[1]) for row in db.conn.execute("SELECT item_type, item_id FROM rescue_catalog")}
    assert catalog == {("series", 30), ("series", 32), ("novel", 1), ("novel", 2), ("novel", 4), ("novel", 7)}

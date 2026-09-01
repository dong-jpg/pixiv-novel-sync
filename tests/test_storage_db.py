from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from pixiv_novel_sync.models import AssetRecord, NovelRecord, NovelTextRecord, SourceRecord, UserRecord
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    db.init_schema()
    return db


def test_recommendation_item_migration_adds_risk_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-recommendations.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE recommendation_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                profile_id INTEGER NOT NULL,
                item_type TEXT NOT NULL,
                novel_id INTEGER,
                series_id INTEGER,
                title TEXT NOT NULL,
                author_id INTEGER,
                author_name TEXT,
                caption TEXT,
                tags_json TEXT NOT NULL,
                text_length INTEGER NOT NULL DEFAULT 0,
                series_total_text_length INTEGER NOT NULL DEFAULT 0,
                series_total_novels INTEGER NOT NULL DEFAULT 0,
                total_bookmarks INTEGER NOT NULL DEFAULT 0,
                total_views INTEGER NOT NULL DEFAULT 0,
                score REAL NOT NULL DEFAULT 0,
                reason TEXT,
                matched_json TEXT NOT NULL,
                source_query TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO recommendation_items (
                run_id, profile_id, item_type, novel_id, title, tags_json, matched_json
            ) VALUES (1, 1, 'novel', 11, '旧推荐', '[]', '{}');
            """
        )

    db = Database(db_path)
    db.init_schema()
    columns = {
        row[1]
        for row in db.conn.execute("PRAGMA table_info(recommendation_items)").fetchall()
    }
    row = db.conn.execute(
        "SELECT x_restrict, risk_notes_json FROM recommendation_items WHERE id = 1"
    ).fetchone()

    assert {"x_restrict", "risk_notes_json"} <= columns
    assert tuple(row) == (0, "[]")
    db.close()


def _insert_user_and_novel(db: Database, novel_id: int = 100, user_id: int = 1, series_id: int | None = None) -> None:
    db.upsert_user(UserRecord(user_id=user_id, name="u", account="acc", raw_json="{}"))
    db.upsert_novel(
        NovelRecord(
            novel_id=novel_id,
            user_id=user_id,
            series_id=series_id,
            title="title",
            caption="caption",
            visible=True,
            restrict="public",
            x_restrict=0,
            text_length=10,
            total_bookmarks=1,
            total_views=2,
            cover_url="https://i.pximg.net/test.jpg",
            tags_json="[]",
            create_date="2026-01-01T00:00:00+00:00",
            raw_json="{}",
            meta_hash="meta",
        )
    )


def test_foreign_keys_enabled(db: Database) -> None:
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_read_transaction_joins_existing_write_transaction(db: Database) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO users (user_id, name, raw_json) VALUES (901, '事务用户', '{}')"
        )
        with pytest.raises(RuntimeError, match="inner"):
            with db.read_transaction() as read_conn:
                assert read_conn is conn
                assert read_conn.in_transaction
                raise RuntimeError("inner")

        assert conn.in_transaction
        assert conn.execute(
            "SELECT name FROM users WHERE user_id = 901"
        ).fetchone()[0] == "事务用户"

    assert not db.conn.in_transaction
    assert db.conn.execute(
        "SELECT name FROM users WHERE user_id = 901"
    ).fetchone()[0] == "事务用户"


def test_read_transaction_cleans_up_owned_transaction_after_error(db: Database) -> None:
    with pytest.raises(KeyboardInterrupt):
        with db.read_transaction() as conn:
            assert conn.in_transaction
            assert conn.execute("SELECT 1").fetchone()[0] == 1
            raise KeyboardInterrupt

    assert not db.conn.in_transaction
    with db.read_transaction() as conn:
        assert conn.execute("SELECT 2").fetchone()[0] == 2
    assert not db.conn.in_transaction


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_read_transaction_base_exception_rolls_back_outer_write(
    db: Database,
    interrupt: type[BaseException],
) -> None:
    with pytest.raises(interrupt):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO users (user_id, name, raw_json) "
                "VALUES (902, '中断用户', '{}')"
            )
            with db.read_transaction():
                raise interrupt

    assert db._transaction_depth == 0
    assert not db.conn.in_transaction
    assert db.conn.execute(
        "SELECT 1 FROM users WHERE user_id = 902"
    ).fetchone() is None
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO users (user_id, name, raw_json) "
            "VALUES (903, '恢复用户', '{}')"
        )
    assert db.conn.execute(
        "SELECT name FROM users WHERE user_id = 903"
    ).fetchone()[0] == "恢复用户"


def test_read_transaction_joins_implicit_write_transaction(db: Database) -> None:
    db.conn.execute(
        "INSERT INTO users (user_id, name, raw_json) "
        "VALUES (904, '隐式事务用户', '{}')"
    )
    assert db._transaction_depth == 0
    assert db.conn.in_transaction

    with pytest.raises(RuntimeError, match="inner"):
        with db.read_transaction():
            raise RuntimeError("inner")

    assert db.conn.in_transaction
    db.conn.rollback()
    assert db.conn.execute(
        "SELECT 1 FROM users WHERE user_id = 904"
    ).fetchone() is None


def test_child_tables_reject_orphan_rows(db: Database) -> None:
    with pytest.raises(Exception):
        db.upsert_novel_text(NovelTextRecord(novel_id=999, text_raw="x", text_markdown=None, text_hash="h"))
    with pytest.raises(Exception):
        db.record_asset(999, "cover", "https://i.pximg.net/x.jpg", "x.jpg", "hash")
    with pytest.raises(Exception):
        db.upsert_source(SourceRecord(novel_id=999, source_type="bookmark_public", source_key="1"))


def test_delete_novel_cascades_child_rows_and_cleans_satellites(db: Database) -> None:
    _insert_user_and_novel(db)
    db.upsert_novel_text(NovelTextRecord(novel_id=100, text_raw="body", text_markdown="md", text_hash="text"))
    db.record_asset(100, "cover", "https://i.pximg.net/x.jpg", "x.jpg", "hash")
    db.upsert_source(SourceRecord(novel_id=100, source_type="bookmark_public", source_key="1"))
    db.replace_fts(100, "title", "caption", "author", "body")
    db.init_sync_check_table()
    db.upsert_sync_check_item(100, True)
    db.conn.execute("INSERT INTO pending_deletions (item_type, item_id, title, reason, status) VALUES ('novel', 100, 't', 'r', 'pending')")
    db.conn.execute("INSERT INTO recommendation_items (run_id, profile_id, item_type, novel_id, title, tags_json, matched_json, status) VALUES (1, 1, 'novel', 100, 't', '[]', '{}', 'pending')")
    db.conn.execute("INSERT INTO recommendation_feedback (item_type, feedback_type, novel_id) VALUES ('novel', 'dismiss', 100)")
    db.conn.commit()

    db.delete_novel(100)

    assert db.conn.execute("SELECT 1 FROM novels WHERE novel_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM novel_texts WHERE novel_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM assets WHERE novel_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM sources WHERE novel_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM novel_fts WHERE novel_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM sync_check_list WHERE novel_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM pending_deletions WHERE item_type = 'novel' AND item_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM recommendation_items WHERE novel_id = 100").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM recommendation_feedback WHERE novel_id = 100").fetchone() is None


def test_delete_user_removes_owned_novels_and_children(db: Database) -> None:
    _insert_user_and_novel(db, novel_id=101, user_id=5)
    db.upsert_novel_text(NovelTextRecord(novel_id=101, text_raw="body", text_markdown=None, text_hash="text"))
    db.record_asset(101, "cover", "https://i.pximg.net/x.jpg", "x.jpg", "hash")
    db.upsert_source(SourceRecord(novel_id=101, source_type="bookmark_public", source_key="5"))
    db.replace_fts(101, "title", "caption", "author", "body")
    db.init_sync_check_table()
    db.upsert_sync_check_item(101, True)
    db.conn.execute("INSERT INTO pending_deletions (item_type, item_id, title, reason, status) VALUES ('user', 5, 'u', 'r', 'pending')")
    db.conn.execute("INSERT INTO pending_deletions (item_type, item_id, title, reason, status) VALUES ('novel', 101, 'n', 'r', 'pending')")
    db.conn.execute("INSERT INTO recommendation_feedback (item_type, feedback_type, novel_id, author_id) VALUES ('novel', 'dismiss', 101, 5)")
    db.conn.commit()

    db.delete_user(5)

    assert db.conn.execute("SELECT 1 FROM users WHERE user_id = 5").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM novels WHERE novel_id = 101").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM novel_texts WHERE novel_id = 101").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM assets WHERE novel_id = 101").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM sources WHERE novel_id = 101").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM novel_fts WHERE novel_id = 101").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM pending_deletions WHERE item_type = 'user' AND item_id = 5").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM pending_deletions WHERE item_type = 'novel' AND item_id = 101").fetchone() is None
    assert db.conn.execute("SELECT 1 FROM recommendation_feedback WHERE author_id = 5 OR novel_id = 101").fetchone() is None


def test_delete_series_only_nulls_series_id(db: Database) -> None:
    db.conn.execute("INSERT INTO series (series_id, title, description, user_id, cover_url, total_novels, is_subscribed, status) VALUES (9, 's', '', 1, NULL, 0, 0, 'unknown')")
    db.conn.commit()
    _insert_user_and_novel(db, novel_id=102, user_id=1, series_id=9)

    db.delete_series(9)

    row = db.conn.execute("SELECT series_id FROM novels WHERE novel_id = 102").fetchone()
    assert row is not None
    assert row[0] is None
    assert db.conn.execute("SELECT 1 FROM series WHERE series_id = 9").fetchone() is None


def test_cleanup_old_pending_deletions_does_not_auto_confirm_pending(db: Database) -> None:
    db.conn.execute(
        """
        INSERT INTO pending_deletions (item_type, item_id, title, reason, status, detected_at)
        VALUES ('novel', 200, 'old pending', 'missing', 'pending', datetime('now', '-365 days'))
        """
    )
    db.conn.execute(
        """
        INSERT INTO pending_deletions (item_type, item_id, title, reason, status, confirmed_at)
        VALUES ('novel', 201, 'old confirmed', 'missing', 'confirmed', datetime('now', '-30 days'))
        """
    )
    db.conn.commit()

    result = db.cleanup_old_pending_deletions(grace_period_days=1, cleanup_confirmed_days=7)

    assert result["auto_confirmed"] == 0
    assert result["cleaned_up"] == 1
    pending = db.conn.execute("SELECT status FROM pending_deletions WHERE item_id = 200").fetchone()
    assert pending is not None
    assert pending[0] == "pending"
    assert db.conn.execute("SELECT 1 FROM pending_deletions WHERE item_id = 201").fetchone() is None


def test_batch_sync_check_upsert(db: Database) -> None:
    db.init_sync_check_table()
    db.upsert_sync_check_items([(1, True), (2, False), (3, True)], scope="scope")
    assert db.get_sync_check_list("scope") == {1: True, 2: False, 3: True}


def test_batch_record_assets(db: Database) -> None:
    _insert_user_and_novel(db, novel_id=103)
    db.record_assets(
        [
            AssetRecord(103, "cover", "https://i.pximg.net/a.jpg", "a.jpg", "h1"),
            AssetRecord(103, "image", "https://i.pximg.net/b.jpg", "b.jpg", "h2"),
        ]
    )
    assert db.get_recorded_asset_urls(103) == {"https://i.pximg.net/a.jpg", "https://i.pximg.net/b.jpg"}


# ── 状态检查轮转顺序 ─────────────────────────────────────────────


def _seed_user_with_check_time(db: Database, user_id: int, checked_at: str | None) -> None:
    db.upsert_user(UserRecord(user_id=user_id, name=f"u{user_id}", account=f"a{user_id}", raw_json="{}"))
    db.conn.execute(
        "UPDATE users SET last_checked_at = ? WHERE user_id = ?", (checked_at, user_id)
    )
    db.conn.commit()


def test_get_users_for_status_check_rotates_by_last_checked_at(db: Database) -> None:
    """最久未检查的排最前，从未检查过的（NULL）排在最最前。

    这是 user_status 分轮覆盖的前提：熔断中止后，本轮没轮到的用户下一轮必须排到
    队首。生产事故是这里曾复用 list_users（status 分桶 + updated_at DESC），顺序
    每轮固定，导致队尾 105/298 个用户超过 3 天从未被检查。
    """
    _seed_user_with_check_time(db, 1, "2026-08-27 10:00:00")
    _seed_user_with_check_time(db, 2, "2026-08-20 10:00:00")
    _seed_user_with_check_time(db, 3, None)
    _seed_user_with_check_time(db, 4, "2026-08-25 10:00:00")

    order = [item["user_id"] for item in db.get_users_for_status_check()]

    assert order == [3, 2, 4, 1]
    assert db.get_users_for_status_check(limit=2) == [
        {"user_id": 3, "name": "u3"},
        {"user_id": 2, "name": "u2"},
    ]


def test_get_series_ids_for_status_check_rotates_by_last_checked_at(db: Database) -> None:
    for series_id, checked_at in ((11, "2026-08-27 10:00:00"), (12, None), (13, "2026-08-21 10:00:00")):
        db.conn.execute(
            "INSERT INTO series (series_id, title, description, user_id, cover_url, total_novels,"
            " is_subscribed, last_checked_at) VALUES (?, '', '', 1, '', 0, 1, ?)",
            (series_id, checked_at),
        )
    db.conn.commit()

    assert db.get_series_ids_for_status_check() == [12, 13, 11]
    assert db.get_series_ids_for_status_check(limit=1) == [12]


def test_get_known_missing_novel_ids_returns_only_deleted(db: Database) -> None:
    _insert_user_and_novel(db, novel_id=201)
    _insert_user_and_novel(db, novel_id=202)
    _insert_user_and_novel(db, novel_id=203)
    db.upsert_novel_status(201, "deleted")
    db.upsert_novel_status(202, "restricted")

    assert db.get_known_missing_novel_ids() == {201}


# ── novel_fts 的 rowid 对齐 ───────────────────────────────────────


def _legacy_fts_db(db_path: Path) -> None:
    """构造一个 rowid 与 novel_id 错位的旧库（复刻生产状态）。"""
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                account TEXT,
                raw_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',
                last_checked_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE novels (
                novel_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                series_id INTEGER,
                title TEXT NOT NULL,
                caption TEXT,
                visible INTEGER NOT NULL,
                restrict_value TEXT NOT NULL,
                x_restrict INTEGER NOT NULL,
                text_length INTEGER NOT NULL,
                total_bookmarks INTEGER NOT NULL,
                total_views INTEGER NOT NULL,
                cover_url TEXT,
                tags_json TEXT NOT NULL,
                create_date TEXT,
                raw_json TEXT NOT NULL,
                meta_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE novel_texts (
                novel_id INTEGER PRIMARY KEY,
                text_raw TEXT NOT NULL,
                has_content INTEGER NOT NULL DEFAULT 0,
                text_markdown TEXT,
                text_hash TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE VIRTUAL TABLE novel_fts USING fts5(
                novel_id UNINDEXED, title, caption, author_name, body
            );
            """
        )
        conn.execute(
            "INSERT INTO users (user_id, name, raw_json) VALUES (7, '作者甲', '{}')"
        )
        for novel_id, title, body in (
            (25310744, "沉默的观测者", "第一篇正文内容"),
            (27380872, "海边的图书馆", "第二篇正文内容"),
        ):
            conn.execute(
                "INSERT INTO novels (novel_id, user_id, title, caption, visible, "
                "restrict_value, x_restrict, text_length, total_bookmarks, total_views, "
                "tags_json, raw_json, meta_hash) "
                "VALUES (?, 7, ?, '简介', 1, 'public', 0, 10, 0, 0, '[]', '{}', 'h')",
                (novel_id, title),
            )
            conn.execute(
                "INSERT INTO novel_texts (novel_id, text_raw, has_content, text_hash) "
                "VALUES (?, ?, 1, 'th')",
                (novel_id, body),
            )
            # 关键：不指定 rowid，让它自增成 1/2，与 novel_id 错位（生产实测 0 行对齐）
            conn.execute(
                "INSERT INTO novel_fts (novel_id, title, caption, author_name, body) "
                "VALUES (?, ?, '简介', '作者甲', ?)",
                (novel_id, title, body),
            )
    conn.close()


def test_fts_migration_realigns_rowid_to_novel_id(tmp_path: Path) -> None:
    """历史库的 FTS rowid 全部错位，init_schema 必须整表重建并对齐。

    回归：novel_id 是 UNINDEXED 列，rowid 是自增值，两者零重合（生产 7627 行中
    对齐的有 0 行）。于是 DELETE ... WHERE novel_id = ? 要全表扫描 1 GB 索引，
    实测单次 39 秒，每同步一篇小说付一次。
    """
    db_path = tmp_path / "legacy-fts.db"
    _legacy_fts_db(db_path)

    with sqlite3.connect(db_path) as conn:
        before = conn.execute("SELECT rowid, novel_id FROM novel_fts ORDER BY rowid").fetchall()
    conn.close()
    assert before == [(1, 25310744), (2, 27380872)]

    db = Database(db_path)
    db.init_schema()
    try:
        rows = db.conn.execute("SELECT rowid, novel_id FROM novel_fts ORDER BY rowid").fetchall()
        assert [(row[0], row[1]) for row in rows] == [
            (25310744, 25310744),
            (27380872, 27380872),
        ]
    finally:
        db.close()


def test_fts_migration_preserves_match_results(tmp_path: Path) -> None:
    """重建后全文检索必须仍能命中，且返回的 rowid 就是 novel_id。

    三个查询分别覆盖回填的三个来源列：author_name 取自 users.name、title 取自
    novels.title、body 取自 novel_texts.text_raw，任一 JOIN 写错都会漏命中。
    注意查询词必须是完整的连续中文串——FTS5 默认的 unicode61 分词器不切分中日韩
    文字，一整段中文就是一个 token，"图书馆" 这种子串永远匹配不上（既有行为，
    与 rowid 无关）。
    """
    db_path = tmp_path / "legacy-fts-match.db"
    _legacy_fts_db(db_path)

    db = Database(db_path)
    db.init_schema()
    try:
        hits = db.conn.execute(
            "SELECT rowid FROM novel_fts WHERE novel_fts MATCH ? ORDER BY rowid",
            ('"作者甲"',),
        ).fetchall()
        assert [row[0] for row in hits] == [25310744, 27380872]

        title_hits = db.conn.execute(
            "SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?",
            ('"海边的图书馆"',),
        ).fetchall()
        assert [row[0] for row in title_hits] == [27380872]

        body_hits = db.conn.execute(
            "SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?",
            ('"第一篇正文内容"',),
        ).fetchall()
        assert [row[0] for row in body_hits] == [25310744]
    finally:
        db.close()


def test_fts_migration_is_idempotent(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """已对齐的库上重复 init_schema 必须是 no-op：不重建、不丢数据。

    重建整表在生产要 1-3 分钟且阻塞启动，绝不能每次启动都跑一遍。
    """
    db_path = tmp_path / "aligned-fts.db"
    _legacy_fts_db(db_path)

    db = Database(db_path)
    db.init_schema()
    db.close()

    caplog.clear()
    db2 = Database(db_path)
    with caplog.at_level(logging.WARNING, logger="pixiv_novel_sync.storage.schema"):
        db2.init_schema()
    try:
        assert "重建 novel_fts" not in caplog.text
        rows = db2.conn.execute("SELECT rowid, novel_id FROM novel_fts ORDER BY rowid").fetchall()
        assert [(row[0], row[1]) for row in rows] == [
            (25310744, 25310744),
            (27380872, 27380872),
        ]
    finally:
        db2.close()


def test_fts_migration_handles_empty_index(db: Database) -> None:
    """空索引（全新库）不需要重建，也不能抛异常。"""
    assert db.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0] == 0

    db.init_schema()  # 再跑一次

    assert db.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0] == 0


def test_fts_migration_tolerates_pending_implicit_transaction(tmp_path: Path) -> None:
    """迁移开始前若已有未提交的隐式事务，重建不能崩在 BEGIN IMMEDIATE 上。

    回归：init_schema 的迁移链里，前一个迁移可能刚跑过 UPDATE（例如
    _migrate_novel_texts_table 给老库补 has_content 列后回填），Python sqlite3
    为它开了隐式事务且没提交。此时 transaction() 的 BEGIN IMMEDIATE 会抛
    "cannot start a transaction within a transaction"，让 init_schema 直接崩在
    启动阶段——而缺列的老库正是本迁移的目标人群。
    """
    db_path = tmp_path / "legacy-fts-pending-tx.db"
    _legacy_fts_db(db_path)

    db = Database(db_path)
    try:
        # 复刻前一个迁移留下的未提交写：同一个连接上的 UPDATE 会打开隐式事务
        db.conn.execute("UPDATE novels SET meta_hash = 'pending'")
        assert db.conn.in_transaction

        db._migrate_novel_fts_rowid()

        rows = db.conn.execute("SELECT rowid, novel_id FROM novel_fts ORDER BY rowid").fetchall()
        assert [(row[0], row[1]) for row in rows] == [
            (25310744, 25310744),
            (27380872, 27380872),
        ]
    finally:
        db.close()


def test_replace_fts_writes_rowid_equal_to_novel_id(db: Database) -> None:
    """replace_fts 必须显式写 rowid，否则又会退回自增错位。"""
    db.replace_fts(31415926, "标题甲", "简介甲", "作者甲", "正文甲")

    rows = db.conn.execute("SELECT rowid, novel_id FROM novel_fts").fetchall()
    assert [(row[0], row[1]) for row in rows] == [(31415926, 31415926)]


def test_replace_fts_twice_keeps_single_row(db: Database) -> None:
    """重复写同一篇不能产生重复索引项（DELETE 必须真的命中旧行）。"""
    db.replace_fts(31415926, "旧标题", "简介", "作者甲", "旧正文")
    db.replace_fts(31415926, "新标题", "简介", "作者甲", "新正文")

    assert db.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0] == 1
    hits = db.conn.execute(
        "SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?", ('"新正文"',)
    ).fetchall()
    assert [row[0] for row in hits] == [31415926]
    # 旧正文必须已从索引里消失
    stale = db.conn.execute(
        "SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?", ('"旧正文"',)
    ).fetchall()
    assert stale == []


def test_delete_novel_removes_fts_row(db: Database) -> None:
    """按 rowid 删除必须命中 replace_fts 写下的对齐行。"""
    _insert_user_and_novel(db, novel_id=31415926, user_id=7)
    db.replace_fts(31415926, "标题甲", "简介甲", "作者甲", "正文甲")

    db.delete_novel(31415926)

    assert db.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0] == 0


def test_delete_user_removes_fts_rows(db: Database) -> None:
    """删除作者要清掉其名下所有小说的索引行（子查询同样走 rowid）。"""
    for novel_id in (31415926, 27182818):
        _insert_user_and_novel(db, novel_id=novel_id, user_id=7)
        db.replace_fts(novel_id, f"标题{novel_id}", "简介", "作者甲", "正文")

    db.delete_user(7)

    assert db.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0] == 0


def test_fts_search_subqueries_select_rowid_not_novel_id() -> None:
    """三处搜索子查询必须走 rowid。

    SELECT novel_id FROM novel_fts 会全表扫描（novel_id 是 UNINDEXED 列），
    生产实测 0.22 秒；SELECT rowid 是 0.002 秒。行为等价，所以只能靠源码断言
    守住——一旦有人改回 novel_id，性能悄悄退化而测试全绿。
    """
    from pixiv_novel_sync.storage import bookmarks, novels, series

    for module in (novels, bookmarks, series):
        path = Path(module.__file__)
        source = path.read_text(encoding="utf-8")
        assert "SELECT novel_id FROM novel_fts" not in source, (
            f"{path.name} 仍在用 SELECT novel_id FROM novel_fts（全表扫描）"
        )
        assert "SELECT rowid FROM novel_fts" in source, f"{path.name} 缺少走 rowid 的搜索子查询"


def _seed_searchable_novel(
    db: Database,
    novel_id: int,
    user_id: int,
    title: str,
    series_id: int | None = None,
) -> None:
    """入库一篇带 FTS 索引行的小说，供三条搜索路径复用。

    搜索词必须是完整的连续中文串：FTS5 默认的 unicode61 分词器不切分中日韩文字，
    一整段中文就是一个 token，子串匹配不上（既有行为）。
    """
    db.upsert_user(
        UserRecord(user_id=user_id, name=f"作者{user_id}", account=f"acc{user_id}", raw_json="{}")
    )
    db.upsert_novel(
        NovelRecord(
            novel_id=novel_id,
            user_id=user_id,
            series_id=series_id,
            title=title,
            caption="简介",
            visible=True,
            restrict="public",
            x_restrict=0,
            text_length=10,
            total_bookmarks=0,
            total_views=0,
            cover_url=None,
            tags_json="[]",
            create_date="2026-01-01T00:00:00+00:00",
            raw_json="{}",
            meta_hash="meta",
        )
    )
    db.replace_fts(novel_id, title, "简介", f"作者{user_id}", f"正文{novel_id}")


def test_list_recent_novels_search_matches_via_fts_rowid(db: Database) -> None:
    """小说库搜索仍按标题命中：rowid 子查询与旧的 novel_id 子查询语义等价。"""
    _seed_searchable_novel(db, 31415926, 7, "沉默的观测者")
    _seed_searchable_novel(db, 27182818, 7, "海边的图书馆")

    result = db.list_recent_novels(search="海边的图书馆")

    assert result["total"] == 1
    assert [item["novel_id"] for item in result["items"]] == [27182818]
    assert db.list_recent_novels(search="不存在的词")["total"] == 0


def test_list_bookmark_novels_search_matches_via_fts_rowid(db: Database) -> None:
    """收藏列表搜索仍按标题命中。"""
    _seed_searchable_novel(db, 31415926, 7, "沉默的观测者")
    _seed_searchable_novel(db, 27182818, 7, "海边的图书馆")
    for novel_id in (31415926, 27182818):
        db.upsert_source(
            SourceRecord(novel_id=novel_id, source_type="bookmark_public", source_key="1")
        )

    result = db.list_bookmark_novels(search="沉默的观测者")

    assert result["total"] == 1
    assert [item["novel_id"] for item in result["items"]] == [31415926]


def test_list_following_series_search_falls_back_to_chapter_fts(db: Database) -> None:
    """系列标题为空时靠章节全文命中，这条 EXISTS 子查询同样走 rowid。

    作者名是「作者7」，与搜索词无关，所以两处 LIKE 都不可能命中——只有 FTS
    分支能把这个系列捞出来。
    """
    db.conn.execute(
        "INSERT INTO series (series_id, title, description, user_id, cover_url, total_novels,"
        " is_subscribed, status) VALUES (900, '', '', 7, NULL, 1, 1, 'unknown')"
    )
    db.conn.commit()
    _seed_searchable_novel(db, 31415926, 7, "沉默的观测者", series_id=900)

    result = db.list_following_series(search="沉默的观测者")

    assert result["total"] == 1
    assert [item["series_id"] for item in result["items"]] == [900]
    assert db.list_following_series(search="不存在的词")["total"] == 0

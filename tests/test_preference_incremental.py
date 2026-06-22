"""增量偏好分析累加器测试。"""
from pathlib import Path

from pixiv_novel_sync.models import NovelRecord, NovelTextRecord, SourceRecord, UserRecord
from pixiv_novel_sync.preferences import PreferenceAnalyzer
from pixiv_novel_sync.storage_db import Database


def _add_novel(db: Database, novel_id: int, user_id: int, tags: str, body_unit: str,
               text_length: int = 6000, series_id: int | None = None) -> None:
    db.upsert_novel(NovelRecord(
        novel_id=novel_id,
        user_id=user_id,
        series_id=series_id,
        title="甜蜜 冒险",
        caption="温柔 关系",
        visible=True,
        restrict="public",
        x_restrict=0,
        text_length=text_length,
        total_bookmarks=10,
        total_views=100,
        cover_url=None,
        tags_json=tags,
        create_date=None,
        raw_json="{}",
        meta_hash=f"h{novel_id}",
    ))
    db.upsert_novel_text(NovelTextRecord(
        novel_id=novel_id,
        text_raw=body_unit * 200,
        text_markdown=None,
        text_hash=f"t{novel_id}",
    ))
    db.upsert_source(SourceRecord(novel_id=novel_id, source_type="bookmark", source_key="public"))


def test_incremental_accumulation_and_skip(tmp_path: Path):
    db = Database(tmp_path / "prefs.db")
    db.init_schema()
    db.upsert_user(UserRecord(user_id=1, name="作者A", account="a", raw_json="{}"))
    for nid in range(100, 105):  # 5 篇
        _add_novel(db, nid, 1, '["甜文", "冒险"]', "温柔的冒险故事")

    analyzer = PreferenceAnalyzer(db)

    # 第一批: batch_size=2 -> 处理 2 篇
    r1 = analyzer.analyze_incremental(batch_size=2, max_batches=1)
    assert r1["processed_this_run"] == 2
    assert r1["analyzed_total"] == 2
    assert r1["remaining"] == 3
    assert r1["done"] is False

    # 第二批: 再处理 2 篇,跳过已分析的
    r2 = analyzer.analyze_incremental(batch_size=2, max_batches=1)
    assert r2["processed_this_run"] == 2
    assert r2["analyzed_total"] == 4
    assert r2["remaining"] == 1

    # 第三批: 剩 1 篇
    r3 = analyzer.analyze_incremental(batch_size=2, max_batches=1)
    assert r3["processed_this_run"] == 1
    assert r3["analyzed_total"] == 5
    assert r3["remaining"] == 0
    assert r3["done"] is True

    # 再跑: 无新内容,处理 0
    r4 = analyzer.analyze_incremental(batch_size=2, max_batches=1)
    assert r4["processed_this_run"] == 0
    assert r4["analyzed_total"] == 5

    db.close()


def test_incremental_matches_full_analysis(tmp_path: Path):
    """增量累加结果应与一次性全量分析一致(标量与 top tag)。"""
    db = Database(tmp_path / "prefs.db")
    db.init_schema()
    db.upsert_user(UserRecord(user_id=1, name="作者A", account="a", raw_json="{}"))
    _add_novel(db, 100, 1, '["甜文", "冒险"]', "温柔的冒险故事", text_length=6000)
    _add_novel(db, 101, 1, '["甜文", "校园"]', "甜蜜的校园生活", text_length=8000, series_id=5)

    analyzer = PreferenceAnalyzer(db)
    full = analyzer.analyze_local({"min_text_length": 1000})

    # 增量分批(每批 1 篇)
    analyzer.analyze_incremental(batch_size=1, max_batches=10)
    rebuilt = analyzer.rebuild_profile_from_accumulator()

    assert rebuilt["stats"]["novel_count"] == full["stats"]["novel_count"] == 2
    assert rebuilt["stats"]["total_chars"] == full["stats"]["total_chars"] == 14000
    assert rebuilt["stats"]["series_novel_count"] == full["stats"]["series_novel_count"] == 1
    # 甜文 出现 2 次,应为 top tag
    rebuilt_tags = {t["name"]: t["count"] for t in rebuilt["stats"]["top_tags"]}
    full_tags = {t["name"]: t["count"] for t in full["stats"]["top_tags"]}
    assert rebuilt_tags.get("甜文") == full_tags.get("甜文") == 2
    assert rebuilt_tags.get("冒险") == 1

    db.close()


def test_new_novels_analyzed_after_initial(tmp_path: Path):
    """全部分析完后新增小说,只分析新增。"""
    db = Database(tmp_path / "prefs.db")
    db.init_schema()
    db.upsert_user(UserRecord(user_id=1, name="作者A", account="a", raw_json="{}"))
    _add_novel(db, 100, 1, '["甜文"]', "温柔的冒险故事")

    analyzer = PreferenceAnalyzer(db)
    r1 = analyzer.analyze_incremental(batch_size=50, max_batches=10)
    assert r1["analyzed_total"] == 1 and r1["done"] is True

    # 新增 1 篇
    _add_novel(db, 200, 1, '["热血"]', "激烈的战斗场面")
    r2 = analyzer.analyze_incremental(batch_size=50, max_batches=10)
    assert r2["processed_this_run"] == 1  # 只分析新增
    assert r2["analyzed_total"] == 2

    rebuilt = analyzer.rebuild_profile_from_accumulator()
    tags = {t["name"] for t in rebuilt["stats"]["top_tags"]}
    assert "甜文" in tags and "热血" in tags  # 累计保留历史

    db.close()


def test_reset_accumulator(tmp_path: Path):
    db = Database(tmp_path / "prefs.db")
    db.init_schema()
    db.upsert_user(UserRecord(user_id=1, name="作者A", account="a", raw_json="{}"))
    _add_novel(db, 100, 1, '["甜文"]', "温柔的冒险故事")

    analyzer = PreferenceAnalyzer(db)
    analyzer.analyze_incremental(batch_size=50, max_batches=10)
    assert db.count_analyzed_preference_rows() == 1

    db.reset_preference_accumulator()
    assert db.count_analyzed_preference_rows() == 0
    acc = db.get_preference_accumulator()
    assert acc["novel_count"] == 0
    assert db.top_preference_terms("tag", 10) == []

    # 重置后可重新分析
    r = analyzer.analyze_incremental(batch_size=50, max_batches=10)
    assert r["analyzed_total"] == 1

    db.close()


def test_min_text_length_filter(tmp_path: Path):
    """低于 min_text_length 的小说不计入。"""
    db = Database(tmp_path / "prefs.db")
    db.init_schema()
    db.upsert_user(UserRecord(user_id=1, name="作者A", account="a", raw_json="{}"))
    _add_novel(db, 100, 1, '["甜文"]', "短", text_length=500)    # 太短
    _add_novel(db, 101, 1, '["冒险"]', "够长的正文内容", text_length=6000)

    analyzer = PreferenceAnalyzer(db)
    r = analyzer.analyze_incremental(batch_size=50, max_batches=10, min_text_length=1000)
    assert r["processed_this_run"] == 1  # 只有 6000 那篇
    assert r["remaining"] == 0

    db.close()

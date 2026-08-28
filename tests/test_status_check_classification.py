"""状态检查的响应判定与限流防护测试。

生产事故背景：pixivpy 被限流时不抛异常，而是返回 {"error": {...}} 形态的 JsonDict
（dict 子类），旧实现把「没有 novel 键」直接当成已删除，一轮任务把 6971 篇小说中的
5499 篇误判为 deleted。这里锁定修复后的 fail-safe 语义：只有明确的「不存在/已删除」
才判删除，其余一切都是 unknown，且 unknown 不得覆盖数据库里的已有状态。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pixiv_novel_sync.jobs import services
from pixiv_novel_sync.models import NovelRecord, UserRecord
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.web import utils as web_utils


class JsonDict(dict):
    """复刻 pixivpy 的 JsonDict：既是 dict，属性访问缺键时返回 None。"""

    def __getattr__(self, attr: str) -> Any:
        return self.get(attr)


RATE_LIMIT_RESPONSE = JsonDict(
    {
        "error": {
            "user_message": "",
            "message": "Rate Limit",
            "reason": "",
            "user_message_details": {},
        }
    }
)

DELETED_NOVEL_RESPONSE = JsonDict(
    {
        "error": {
            "user_message": "該当作品は削除されたか、存在しない作品IDです。",
            "message": "",
            "reason": "",
        }
    }
)


class FakeApi:
    """按预设结果/异常应答的假 API。"""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[int] = []

    def _respond(self, item_id: int) -> Any:
        self.calls.append(int(item_id))
        if self.error is not None:
            raise self.error
        return self.result

    novel_detail = _respond
    novel_series = _respond
    user_detail = _respond


# --------------------------------------------------------------------------
# 小说状态判定
# --------------------------------------------------------------------------


def test_novel_rate_limit_response_is_unknown_not_deleted() -> None:
    """限流响应必须是 unknown —— 这正是误判 5499 篇的根因。"""
    assert web_utils._check_novel_status(FakeApi(RATE_LIMIT_RESPONSE), 123) == "unknown"


@pytest.mark.parametrize(
    "message",
    [
        # 中文（生产实例带 lang=zh 时的真实文案）
        "尚无此页",
        # 日文
        "該当作品は削除されたか、存在しない作品IDです。",
        # 英文
        "This novel has been deleted",
        "Novel not found",
        "The work does not exist",
    ],
)
def test_novel_explicit_missing_error_is_deleted(message: str) -> None:
    response = JsonDict({"error": {"user_message": message, "message": "", "reason": ""}})
    assert web_utils._check_novel_status(FakeApi(response), 123) == "deleted"


def test_novel_none_response_is_unknown() -> None:
    assert web_utils._check_novel_status(FakeApi(None), 123) == "unknown"


def test_novel_exception_is_unknown() -> None:
    api = FakeApi(error=RuntimeError("connection reset"))
    assert web_utils._check_novel_status(api, 123) == "unknown"


def test_novel_empty_dict_response_is_unknown() -> None:
    """没有 novel 也没有错误文案时同样保守判 unknown。"""
    assert web_utils._check_novel_status(FakeApi(JsonDict({})), 123) == "unknown"


@pytest.mark.parametrize(
    "response",
    [
        JsonDict({"novel": {"id": 123, "visible": True}}),
        SimpleNamespace(novel=SimpleNamespace(id=123, visible=True)),
    ],
)
def test_novel_normal_response(response: Any) -> None:
    assert web_utils._check_novel_status(FakeApi(response), 123) == "normal"


@pytest.mark.parametrize(
    "response",
    [
        JsonDict({"novel": {"id": 123, "visible": False}}),
        SimpleNamespace(novel=SimpleNamespace(id=123, visible=False)),
    ],
)
def test_novel_invisible_response_is_restricted(response: Any) -> None:
    assert web_utils._check_novel_status(FakeApi(response), 123) == "restricted"


def test_novel_rate_limit_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        web_utils._check_novel_status(FakeApi(RATE_LIMIT_RESPONSE), 123)
    assert "Rate Limit" in caplog.text


# --------------------------------------------------------------------------
# 系列状态判定
# --------------------------------------------------------------------------


def test_series_rate_limit_response_is_unknown() -> None:
    assert web_utils._check_series_status(FakeApi(RATE_LIMIT_RESPONSE), 55) == "unknown"


def test_series_none_response_is_unknown() -> None:
    assert web_utils._check_series_status(FakeApi(None), 55) == "unknown"


def test_series_exception_is_unknown() -> None:
    assert web_utils._check_series_status(FakeApi(error=OSError("boom")), 55) == "unknown"


def test_series_explicit_missing_error_is_deleted() -> None:
    response = JsonDict(
        {"error": {"user_message": "該当作品は削除されたか、存在しない作品IDです。", "message": ""}}
    )
    assert web_utils._check_series_status(FakeApi(response), 55) == "deleted"


def test_series_chinese_missing_error_is_deleted() -> None:
    """生产实测（lang=zh）：不存在的系列返回这段中文长文案。"""
    response = JsonDict(
        {
            "error": {
                "user_message": (
                    "抱歉，您所指定的系列已经从个人信息删除，或者不存在。 "
                    "请确认您所输入的系列ID是否正确。"
                ),
                "message": "",
                "reason": "",
            }
        }
    )
    assert web_utils._check_series_status(FakeApi(response), 55) == "deleted"


@pytest.mark.parametrize(
    "response",
    [
        JsonDict({"novel_series_detail": {"id": 55}}),
        SimpleNamespace(novel_series_detail=SimpleNamespace(id=55)),
    ],
)
def test_series_normal_response(response: Any) -> None:
    assert web_utils._check_series_status(FakeApi(response), 55) == "normal"


# --------------------------------------------------------------------------
# 用户状态判定
# --------------------------------------------------------------------------


def test_user_rate_limit_response_is_unknown_not_suspended() -> None:
    assert web_utils._check_pixiv_user_status(FakeApi(RATE_LIMIT_RESPONSE), 7) == "unknown"


def test_user_none_response_is_unknown() -> None:
    assert web_utils._check_pixiv_user_status(FakeApi(None), 7) == "unknown"


def test_user_exception_is_unknown() -> None:
    api = FakeApi(error=RuntimeError("token expired"))
    assert web_utils._check_pixiv_user_status(api, 7) == "unknown"


def test_user_explicit_missing_error_is_suspended() -> None:
    response = JsonDict({"error": {"user_message": "該当ユーザーは存在しません。", "message": ""}})
    assert web_utils._check_pixiv_user_status(FakeApi(response), 7) == "suspended"


def test_user_chinese_missing_error_is_suspended() -> None:
    """生产实测（lang=zh）：不存在的用户同样返回「尚无此页」。"""
    response = JsonDict({"error": {"user_message": "尚无此页", "message": "", "reason": ""}})
    assert web_utils._check_pixiv_user_status(FakeApi(response), 7) == "suspended"


def test_user_normal_and_no_novels() -> None:
    normal = JsonDict({"user": {"id": 7}, "profile": {"total_novels": 3}})
    empty = JsonDict({"user": {"id": 7}, "profile": {"total_novels": 0}})
    assert web_utils._check_pixiv_user_status(FakeApi(normal), 7) == "normal"
    assert web_utils._check_pixiv_user_status(FakeApi(empty), 7) == "no_novels"


# --------------------------------------------------------------------------
# 限流特征优先于「不存在」关键词（与 sync_engine 的判定保持一致）
# --------------------------------------------------------------------------


MIXED_RATE_LIMIT_RESPONSE = JsonDict(
    {
        "error": {
            # 限流响应里混着泛化的「尚无此页」文案：
            # sync_engine._pixiv_content_unavailable_reason 会先排除限流，
            # web/utils._is_missing_error 若不排除，两者结论就相反。
            "user_message": "尚无此页",
            "message": "Rate Limit",
            "reason": "",
        }
    }
)


@pytest.mark.parametrize(
    "error_text",
    [
        "尚无此页 Rate Limit",
        "尚无此页 rate-limit",
        "not found ratelimit",
        "已删除 Too Many Requests",
        "does not exist 429",
    ],
)
def test_is_missing_error_excludes_rate_limit_text(error_text: str) -> None:
    """文本带限流特征时一律不判删除，宁可漏判也绝不误判。"""
    assert web_utils._is_missing_error(error_text) is False


def test_is_missing_error_still_matches_pure_missing_text() -> None:
    assert web_utils._is_missing_error("尚无此页") is True
    assert web_utils._is_missing_error("該当作品は削除されたか、存在しない作品IDです。") is True


def test_mixed_rate_limit_and_missing_text_is_unknown_for_all_entities() -> None:
    assert web_utils._check_novel_status(FakeApi(MIXED_RATE_LIMIT_RESPONSE), 123) == "unknown"
    assert web_utils._check_series_status(FakeApi(MIXED_RATE_LIMIT_RESPONSE), 55) == "unknown"
    assert web_utils._check_pixiv_user_status(FakeApi(MIXED_RATE_LIMIT_RESPONSE), 7) == "unknown"


def test_rate_limit_tokens_match_sync_engine() -> None:
    """两处限流 token 必须保持一致，否则同一响应会被判成相反结论。"""
    from pixiv_novel_sync import sync_engine

    assert web_utils._RATE_LIMIT_ERROR_TOKENS == sync_engine._RATE_LIMIT_ERROR_TOKENS


# --------------------------------------------------------------------------
# unknown 不得覆盖已有状态
# --------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "status.db")
    database.init_schema()
    yield database
    database.close()


def _insert_novel(db: Database, novel_id: int, user_id: int = 1) -> None:
    db.upsert_user(UserRecord(user_id=user_id, name="u", account="acc", raw_json="{}"))
    db.upsert_novel(
        NovelRecord(
            novel_id=novel_id,
            user_id=user_id,
            series_id=None,
            title=f"t{novel_id}",
            caption="",
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


def _novel_row(db: Database, novel_id: int) -> tuple[str, str | None]:
    row = db.conn.execute(
        "SELECT status, last_checked_at FROM novels WHERE novel_id = ?", (novel_id,)
    ).fetchone()
    return row[0], row[1]


def test_unknown_novel_status_keeps_status_but_refreshes_timestamp(db: Database) -> None:
    _insert_novel(db, 101)
    db.upsert_novel_status(101, "normal")
    status_before, checked_before = _novel_row(db, 101)
    assert (status_before, checked_before is not None) == ("normal", True)

    db.conn.execute("UPDATE novels SET last_checked_at = '2000-01-01 00:00:00' WHERE novel_id = 101")
    db.upsert_novel_status(101, "unknown")

    status_after, checked_after = _novel_row(db, 101)
    assert status_after == "normal"
    assert checked_after != "2000-01-01 00:00:00"


def test_known_novel_status_still_overwrites(db: Database) -> None:
    _insert_novel(db, 102)
    db.upsert_novel_status(102, "normal")
    db.upsert_novel_status(102, "deleted")
    assert _novel_row(db, 102)[0] == "deleted"


def test_unknown_series_status_keeps_status(db: Database) -> None:
    db.upsert_subscribed_series(31, "系列", "", 1, None, 2)
    db.upsert_series_status(31, "normal")
    db.conn.execute("UPDATE series SET last_checked_at = '2000-01-01 00:00:00' WHERE series_id = 31")

    db.upsert_series_status(31, "unknown")

    row = db.conn.execute(
        "SELECT status, last_checked_at FROM series WHERE series_id = 31"
    ).fetchone()
    assert row[0] == "normal"
    assert row[1] != "2000-01-01 00:00:00"


def test_unknown_user_status_keeps_status(db: Database) -> None:
    db.upsert_user(UserRecord(user_id=9, name="u", account="acc", raw_json="{}"))
    db.upsert_user_status(9, "normal")
    db.conn.execute("UPDATE users SET last_checked_at = '2000-01-01 00:00:00' WHERE user_id = 9")

    db.upsert_user_status(9, "unknown")

    row = db.conn.execute("SELECT status, last_checked_at FROM users WHERE user_id = 9").fetchone()
    assert row[0] == "normal"
    assert row[1] != "2000-01-01 00:00:00"


# --------------------------------------------------------------------------
# 增量分批选取
# --------------------------------------------------------------------------


def test_status_check_batch_prefers_never_checked(db: Database) -> None:
    for novel_id in (201, 202, 203):
        _insert_novel(db, novel_id)
    # 201/202 已检查过（202 更早），203 从未检查
    db.conn.execute("UPDATE novels SET last_checked_at = '2026-01-02 00:00:00' WHERE novel_id = 201")
    db.conn.execute("UPDATE novels SET last_checked_at = '2026-01-01 00:00:00' WHERE novel_id = 202")

    assert db.get_novel_ids_for_status_check() == [203, 202, 201]


def test_status_check_batch_respects_limit(db: Database) -> None:
    for novel_id in (301, 302, 303):
        _insert_novel(db, novel_id)
    db.conn.execute("UPDATE novels SET last_checked_at = '2026-01-01 00:00:00' WHERE novel_id = 301")
    db.conn.execute("UPDATE novels SET last_checked_at = '2026-01-02 00:00:00' WHERE novel_id = 302")
    db.conn.execute("UPDATE novels SET last_checked_at = '2026-01-03 00:00:00' WHERE novel_id = 303")

    assert db.get_novel_ids_for_status_check(limit=2) == [301, 302]


def test_status_check_batches_rotate_without_repeating(db: Database) -> None:
    for novel_id in (401, 402, 403, 404):
        _insert_novel(db, novel_id)

    first = db.get_novel_ids_for_status_check(limit=2)
    for novel_id in first:
        db.upsert_novel_status(novel_id, "normal")
    second = db.get_novel_ids_for_status_check(limit=2)

    assert len(first) == 2
    assert set(first) & set(second) == set()
    assert set(first) | set(second) == {401, 402, 403, 404}


def test_unknown_status_still_advances_rotation(db: Database) -> None:
    """unknown 不改 status，但刷新 last_checked_at，下一批不会卡在同一批。"""
    for novel_id in (501, 502):
        _insert_novel(db, novel_id)

    first = db.get_novel_ids_for_status_check(limit=1)
    db.upsert_novel_status(first[0], "unknown")
    second = db.get_novel_ids_for_status_check(limit=1)

    assert first != second


def test_status_check_without_limit_returns_all_novels(db: Database) -> None:
    for novel_id in (601, 602, 603):
        _insert_novel(db, novel_id)

    assert sorted(db.get_novel_ids_for_status_check()) == sorted(db.get_all_novel_ids())
    assert db.get_novel_ids_for_status_check(limit=0) == db.get_novel_ids_for_status_check()


def test_count_novels_pending_status_check(db: Database) -> None:
    for novel_id in (701, 702, 703):
        _insert_novel(db, novel_id)
    db.conn.execute("UPDATE novels SET last_checked_at = '2026-01-01 00:00:00' WHERE novel_id = 701")
    db.conn.execute("UPDATE novels SET last_checked_at = '2026-08-01 00:00:00' WHERE novel_id = 702")

    assert db.count_novels_pending_status_check() == 3
    # 703 从未检查 + 701 早于分界点
    assert db.count_novels_pending_status_check("2026-06-01 00:00:00") == 2


# --------------------------------------------------------------------------
# 连续 unknown 熔断
# --------------------------------------------------------------------------


class RecordingDb:
    def __init__(self) -> None:
        self.upserts: list[tuple[Any, str]] = []


class RecordingReporter:
    """只记录日志级别的假 reporter（JobReporter 的最小接口）。"""

    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []

    def add_log(self, level: str, message: str) -> None:
        self.logs.append((level, message))

    def update_progress(self, **kwargs: Any) -> None:
        pass


def _run_items(
    statuses: list[str],
    settings: Any = None,
    reporter: Any = None,
    already_missing: Any = None,
) -> dict[str, Any]:
    db = RecordingDb()
    items = list(range(len(statuses)))
    status_map = dict(zip(items, statuses))
    return services._process_status_items(
        settings=settings or SimpleNamespace(sync=SimpleNamespace(delay_seconds_between_skips=0)),
        reporter=reporter,
        stop_requested=None,
        db=db,
        items=items,
        check_status=lambda item: status_map[item],
        upsert_status=lambda db, item, status: db.upserts.append((item, status)),
        item_label="小说",
        item_name=lambda item: str(item),
        total_key="total_novels",
        already_missing=already_missing,
    )


def test_already_missing_items_do_not_trigger_missing_streak() -> None:
    """本来就已知 deleted 的条目再次被确认，不算「限流伪装成不存在」的证据。

    生产事故：11911679–11961577 这段 2010 年连号老作品确实全被删且已入库为 deleted，
    却因为连号聚集凑满 30 连续，每次轮到都把整轮 novel_status 熔断掉。
    """
    count = services.MAX_CONSECUTIVE_MISSING + 20
    stats = _run_items(["deleted"] * count, already_missing=lambda item: True)

    assert "aborted_reason" not in stats
    assert stats["stopped"] is False
    assert stats["checked_count"] == count
    assert stats["confirmed_missing"] == count


def test_newly_missing_items_still_trigger_streak_despite_known_ones() -> None:
    """已知 deleted 只是不计数，不能把真正的「突然全变删除」也一起放过。"""
    known = {0, 1, 2}
    statuses = ["deleted"] * (services.MAX_CONSECUTIVE_MISSING + len(known) + 5)
    stats = _run_items(statuses, already_missing=lambda item: item in known)

    assert stats["aborted_reason"] == "suspicious_missing_streak"
    assert stats["stopped"] is True
    # 前 3 个是已知删除（不计数），之后 MAX_CONSECUTIVE_MISSING 个新删除触发熔断
    assert stats["checked_count"] == len(known) + services.MAX_CONSECUTIVE_MISSING
    assert stats["confirmed_missing"] == len(known)


def test_already_missing_item_does_not_reset_new_missing_streak() -> None:
    """已知删除穿插在新删除之间时，不能把新删除的连续计数清零。

    否则「每 29 个新删除插一个已知删除」就能永久绕过熔断。
    """
    half = services.MAX_CONSECUTIVE_MISSING // 2
    statuses = ["deleted"] * (half + 1 + services.MAX_CONSECUTIVE_MISSING)
    known = {half}  # 中间插一个已知删除
    stats = _run_items(statuses, already_missing=lambda item: item in known)

    assert stats["aborted_reason"] == "suspicious_missing_streak"
    assert stats["confirmed_missing"] == 1


def test_process_status_items_aborts_after_consecutive_unknown() -> None:
    stats = _run_items(["unknown"] * (services.MAX_CONSECUTIVE_UNKNOWN + 5))

    assert stats["aborted_reason"] == "rate_limited"
    assert stats["stopped"] is True
    assert stats["checked_count"] == services.MAX_CONSECUTIVE_UNKNOWN
    assert stats["status_counts"]["unknown"] == services.MAX_CONSECUTIVE_UNKNOWN


def test_process_status_items_resets_counter_on_valid_status() -> None:
    """穿插的正常结果会重置计数，不应触发熔断。"""
    statuses = ["unknown"] * 4 + ["normal"] + ["unknown"] * 4
    stats = _run_items(statuses)

    assert "aborted_reason" not in stats
    assert stats["stopped"] is False
    assert stats["checked_count"] == len(statuses)
    assert stats["status_counts"] == {"unknown": 8, "normal": 1}


def test_process_status_items_normal_run_has_no_abort_marker() -> None:
    stats = _run_items(["normal", "deleted", "restricted"])

    assert "aborted_reason" not in stats
    assert stats == {
        "checked_count": 3,
        "total_novels": 3,
        "status_counts": {"normal": 1, "deleted": 1, "restricted": 1},
        "stopped": False,
    }


def test_process_status_items_logs_rate_limit_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        _run_items(["unknown"] * services.MAX_CONSECUTIVE_UNKNOWN)
    assert "疑似触发 Pixiv 限流" in caplog.text


# --------------------------------------------------------------------------
# 连续 deleted 熔断（关键词兜底：限流可能伪装成「尚无此页」）
# --------------------------------------------------------------------------


def test_process_status_items_aborts_after_consecutive_deleted() -> None:
    stats = _run_items(["deleted"] * (services.MAX_CONSECUTIVE_MISSING + 10))

    assert stats["aborted_reason"] == "suspicious_missing_streak"
    assert stats["stopped"] is True
    assert stats["checked_count"] == services.MAX_CONSECUTIVE_MISSING
    assert stats["status_counts"]["deleted"] == services.MAX_CONSECUTIVE_MISSING


def test_process_status_items_aborts_after_consecutive_suspended_users() -> None:
    """用户任务的「不存在」判定是 suspended，同样受该熔断保护。"""
    stats = _run_items(["suspended"] * (services.MAX_CONSECUTIVE_MISSING + 1))

    assert stats["aborted_reason"] == "suspicious_missing_streak"
    assert stats["checked_count"] == services.MAX_CONSECUTIVE_MISSING


def test_process_status_items_deleted_streak_resets_on_normal() -> None:
    """中间穿插一次 normal 就重置计数，零星删除不会被误判为异常。"""
    statuses = (
        ["deleted"] * (services.MAX_CONSECUTIVE_MISSING - 1)
        + ["normal"]
        + ["deleted"] * (services.MAX_CONSECUTIVE_MISSING - 1)
    )
    stats = _run_items(statuses)

    assert "aborted_reason" not in stats
    assert stats["stopped"] is False
    assert stats["checked_count"] == len(statuses)


def test_two_breakers_count_independently() -> None:
    """unknown 与 deleted 各自独立计数：交替出现时谁也不会累积到阈值。"""
    statuses = ["unknown", "deleted"] * (services.MAX_CONSECUTIVE_MISSING + 10)
    stats = _run_items(statuses)

    assert "aborted_reason" not in stats
    assert stats["checked_count"] == len(statuses)


def test_deleted_streak_does_not_trip_unknown_breaker() -> None:
    """连续 deleted 只应命中 missing 熔断，不应被报成限流。"""
    stats = _run_items(["deleted"] * services.MAX_CONSECUTIVE_MISSING)

    assert stats["aborted_reason"] == "suspicious_missing_streak"


def test_unknown_streak_does_not_trip_missing_breaker() -> None:
    stats = _run_items(["unknown"] * services.MAX_CONSECUTIVE_UNKNOWN)

    assert stats["aborted_reason"] == "rate_limited"


def test_process_status_items_logs_missing_streak_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        _run_items(["deleted"] * services.MAX_CONSECUTIVE_MISSING)
    assert "疑似 API 限流伪装成不存在" in caplog.text


# --------------------------------------------------------------------------
# 熔断必须以 error 级别写进任务日志（执行日志里标红，运维一眼能看见）
# --------------------------------------------------------------------------


def test_rate_limit_abort_is_reported_as_error_level() -> None:
    reporter = RecordingReporter()
    _run_items(["unknown"] * services.MAX_CONSECUTIVE_UNKNOWN, reporter=reporter)

    abort_logs = [(level, msg) for level, msg in reporter.logs if "中止" in msg]
    assert abort_logs, "熔断必须往任务日志里写中止说明"
    assert all(level == "error" for level, _ in abort_logs), abort_logs
    assert not any(level == "success" for level, _ in reporter.logs)


def test_missing_streak_abort_is_reported_as_error_level() -> None:
    reporter = RecordingReporter()
    _run_items(["deleted"] * services.MAX_CONSECUTIVE_MISSING, reporter=reporter)

    abort_logs = [(level, msg) for level, msg in reporter.logs if "中止" in msg]
    assert abort_logs
    assert all(level == "error" for level, _ in abort_logs), abort_logs


def test_normal_run_still_reports_success_level() -> None:
    reporter = RecordingReporter()
    _run_items(["normal", "deleted"], reporter=reporter)

    assert reporter.logs[-1][0] == "success"
    assert not any(level == "error" for level, _ in reporter.logs)

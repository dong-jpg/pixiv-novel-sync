"""重试补丁的覆盖面、幂等与取消绑定（审查 S7）。

`apply_pixiv_retry_patch` 以前在第一次打补丁时就把取消回调定死：`jobs/services._login`
先以 `stop_requested=None` 打一次，`BookmarkNovelSyncService` 带取消回调再打时被幂等
判定整体跳过，于是全量备份、三种状态检查、待删检测的 429/网络退避都是不可取消的整段
sleep（单次调用最长 35 秒）。状态检查那条路还要求 `web/utils.py` 的三个检查函数把
InterruptedError 原样抛出，否则取消会被宽 except 吞成 unknown。
"""

from __future__ import annotations

import ast
import time as real_time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pixiv_novel_sync import rate_limiter
from pixiv_novel_sync.jobs import services
from pixiv_novel_sync.sync.utils import RETRY_PATCHED_METHODS, apply_pixiv_retry_patch
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "pixiv_novel_sync"

# 不进重试名单的 api 方法：认证与本地工具方法不是数据请求；search_novel 只在
# recommendations.py 里用，调用点自己包了 retry_on_pixiv_error(stop_requested=...)。
EXEMPT_API_METHODS = {
    "auth": "认证请求，失败直接报登录错误",
    "set_auth": "本地设置 token，不发请求",
    "set_accept_language": "本地设置请求头，不发请求",
    "parse_qs": "本地解析 next_url，不发请求",
    "search_novel": "recommendations.py 调用点自带 retry_on_pixiv_error",
}


def _api_method_calls() -> dict[str, list[str]]:
    """收集 src 里所有 `api.xxx(...)` / `self.api.xxx(...)` 调用点。"""
    found: dict[str, list[str]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            is_api = (isinstance(owner, ast.Name) and owner.id == "api") or (
                isinstance(owner, ast.Attribute) and owner.attr == "api"
            )
            if is_api:
                location = f"{path.relative_to(SRC_ROOT).as_posix()}:{node.lineno}"
                found.setdefault(node.func.attr, []).append(location)
    return found


def test_every_pixiv_api_call_site_is_retry_patched_or_explicitly_exempt() -> None:
    calls = _api_method_calls()
    unprotected = {
        name: sites
        for name, sites in calls.items()
        if name not in RETRY_PATCHED_METHODS and name not in EXEMPT_API_METHODS
    }
    assert unprotected == {}, "新增的 api 调用要登记进 RETRY_PATCHED_METHODS 或豁免表"
    # 名单里的方法都确实有调用点，避免名单与代码各自漂移
    assert set(RETRY_PATCHED_METHODS) <= set(calls)


class _FlakyApi:
    """每个数据方法都抛网络错误；可选地在第一次失败时打开取消标志。"""

    def __init__(self, cancel_flag: dict[str, bool] | None = None) -> None:
        self.calls: list[str] = []
        self._cancel_flag = cancel_flag

    def _fail(self, name: str) -> None:
        self.calls.append(name)
        if self._cancel_flag is not None:
            # 模拟「请求进行中用户点了取消」：取消在请求失败之后、退避之前生效
            self._cancel_flag["cancel"] = True
        raise RuntimeError("network timeout")

    def novel_detail(self, novel_id):
        self._fail("novel_detail")

    def novel_series(self, series_id):
        self._fail("novel_series")

    def user_detail(self, user_id):
        self._fail("user_detail")


@pytest.fixture
def recorded_sleeps(monkeypatch) -> list[float]:
    """把 cancellable_sleep 的 time.sleep 换成记录器，退避不真的睡。"""
    slept: list[float] = []
    monkeypatch.setattr(
        rate_limiter,
        "time",
        SimpleNamespace(sleep=slept.append, time=real_time.time, monotonic=real_time.monotonic),
    )
    return slept


def test_repeated_patch_is_idempotent_and_does_not_stack_backoff(recorded_sleeps) -> None:
    api = _FlakyApi()
    apply_pixiv_retry_patch(api)
    wrapped = api.novel_detail
    apply_pixiv_retry_patch(api, stop_requested=lambda: False)

    assert api.novel_detail is wrapped
    with pytest.raises(RuntimeError, match="network timeout"):
        api.novel_detail(1)
    # 1 次首调 + 3 次重试；叠加包装会变成 16 次
    assert api.calls == ["novel_detail"] * 4
    assert recorded_sleeps  # 有退避，且用的是重绑后的回调（轮询分段）


def test_later_patch_rebinds_cancel_callback_on_already_wrapped_api(recorded_sleeps) -> None:
    flag = {"cancel": False}
    api = _FlakyApi(cancel_flag=flag)
    apply_pixiv_retry_patch(api)  # _login 的顺序：先不带回调
    apply_pixiv_retry_patch(api, stop_requested=lambda: flag["cancel"])

    with pytest.raises(InterruptedError):
        api.novel_detail(1)
    assert api.calls == ["novel_detail"]
    assert sum(recorded_sleeps) == 0


def test_sync_service_binds_its_cancel_callback_after_login_patch(recorded_sleeps) -> None:
    """全量备份与待删检测的顺序：_login 打补丁 → 构造 service → 赋 stop_requested。"""
    flag = {"cancel": False}
    api = _FlakyApi(cancel_flag=flag)
    apply_pixiv_retry_patch(api)
    service = BookmarkNovelSyncService(
        api=api,
        db=None,
        storage=None,
        settings=SimpleNamespace(sync=SimpleNamespace(delay_seconds_between_pages=0)),
    )
    service.stop_requested = lambda: flag["cancel"]

    with pytest.raises(InterruptedError):
        api.novel_detail(1)
    assert api.calls == ["novel_detail"]
    assert sum(recorded_sleeps) == 0


class _StatusDb:
    def __init__(self) -> None:
        self.upserts: list[tuple[str, object, str]] = []
        self.closed = False

    def get_users_for_status_check(self):
        return [{"user_id": 101, "name": "u101"}]

    def get_novel_ids_for_status_check(self, limit=None):
        return [11]

    def get_series_ids_for_status_check(self):
        return [21]

    def get_known_missing_novel_ids(self):
        return set()

    def count_novels_pending_status_check(self, started_at):
        return 0

    def upsert_user_status(self, user_id, status):
        self.upserts.append(("user", user_id, status))

    def upsert_novel_status(self, novel_id, status):
        self.upserts.append(("novel", novel_id, status))

    def upsert_series_status(self, series_id, status):
        self.upserts.append(("series", series_id, status))

    def rebuild_rescue_catalog(self):
        return {"items": 0, "sources": 0}

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("runner", "api_method"),
    [
        (services.run_user_status_task, "user_detail"),
        (services.run_novel_status_task, "novel_detail"),
        (services.run_series_status_task, "novel_series"),
    ],
)
def test_status_checks_cancel_during_retry_backoff(monkeypatch, tmp_path, recorded_sleeps, runner, api_method) -> None:
    flag = {"cancel": False}
    api = _FlakyApi(cancel_flag=flag)
    db = _StatusDb()

    class _Auth:
        def __init__(self, pixiv_settings) -> None:
            pass

        def login(self):
            return api, SimpleNamespace(user_id=999)

    monkeypatch.setattr(services, "PixivAuthManager", _Auth)
    monkeypatch.setattr(services, "Database", lambda db_path: db)
    monkeypatch.setattr(services, "prepare_schema", lambda database: None)
    monkeypatch.setattr(services, "_ensure_storage_dirs", lambda settings: None)
    settings = SimpleNamespace(
        pixiv=SimpleNamespace(user_id=999),
        storage=SimpleNamespace(db_path=tmp_path / "x.db"),
        sync=SimpleNamespace(delay_seconds_between_skips=0),
    )

    # 取消必须一路抛到 JobRunner（转 CANCELLED），而不是被吞成 unknown 写进库
    with pytest.raises(InterruptedError):
        runner(settings, stop_requested=lambda: flag["cancel"])

    assert api.calls == [api_method]
    assert db.upserts == []
    assert sum(recorded_sleeps) == 0
    assert db.closed is True

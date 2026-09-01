from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import logging
import time
from time import perf_counter
from typing import Any

from pixiv_novel_sync.auth import PixivAuthManager
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.storage_files import FileStorage
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService

logger = logging.getLogger(__name__)

# 连续多少个条目判定为 unknown 就认定「疑似被限流」并中止整轮检查。
# 背景：pixivpy 被限流时不抛异常而是返回错误响应，历史事故里限流持续 3 小时，
# 每一批 290 篇全部被误判。中止后本轮未检查的条目保持原状态，等下一轮再补。
#
# 阈值从 5 放宽到 15 的依据（2026-08-27 生产实测）：user_status 每轮 193/298 个用户
# 里只有 6 个 unknown，但其中 5 个恰好连续，于是每一轮都在同一处熔断。真限流的形态
# 是「此后全部 unknown」（290/290），15 连续足以捕获，而零星聚集不会误触发。
MAX_CONSECUTIVE_UNKNOWN = 15

# 连续多少个条目判定为「已删除」就认定异常并中止整轮检查。
# 这是判定关键词的兜底：中文文案「尚无此页」很泛，无法排除限流也复用它，一旦如此，
# 限流就会伪装成「不存在」绕过 unknown 熔断，重演 5499 篇误判。
# 阈值依据生产实测：正常 10 分钟分桶里是 152~279 篇 normal 混着 8~85 篇 deleted，
# 零星分布不会出现长连续段；限流时则是连续 290/290 全 deleted。连续 30 篇全删除
# 是极强的异常信号，正常情况下（哪怕某作者整批作品被删）也很难连续命中。
MAX_CONSECUTIVE_MISSING = 30

# 判定为「不存在/已删除」的状态取值：小说/系列是 deleted，用户是 suspended，
# 三者共用同一套熔断逻辑。
MISSING_STATUSES = frozenset({"deleted", "suspended"})

# 小说状态检查每轮的批大小。全量 6971 篇（每篇间隔 2 秒）单轮要跑满 4 小时，
# 既占死任务槽又是限流诱因；分批后单轮耗时可控，多轮按 last_checked_at 轮转覆盖全库。
NOVEL_STATUS_BATCH_SIZE = 800


class JobReporter:
    def __init__(self, manager: Any = None, job_id: str | None = None) -> None:
        self.manager = manager
        self.job_id = str(job_id) if job_id else None

    def add_log(self, level: str, message: str) -> None:
        if self.manager is None or not self.job_id or not hasattr(self.manager, "add_log"):
            return
        self.manager.add_log(self.job_id, level, message)

    def update_progress(self, **kwargs: Any) -> None:
        if self.manager is None or not self.job_id or not hasattr(self.manager, "update_progress"):
            return
        self.manager.update_progress(self.job_id, **kwargs)


StopRequested = Callable[[], bool]
ClaimFinalization = Callable[[], bool]


def _report_catalog_log(reporter: JobReporter | None, level: str, message: str) -> None:
    if reporter is None:
        return
    try:
        reporter.add_log(level, message)
    except Exception as exc:
        logger.warning("救援目录日志记录失败: %s", exc)


def _rebuild_rescue_catalog(db: Any, reporter: JobReporter | None = None) -> dict[str, int]:
    try:
        started_at = perf_counter()
        result = db.rebuild_rescue_catalog()
        duration_ms = int(round((perf_counter() - started_at) * 1000))
        stats = {
            "rescue_catalog_items": int(result.get("items", 0) or 0),
            "rescue_catalog_sources": int(result.get("sources", 0) or 0),
            "rescue_catalog_duration_ms": duration_ms,
        }
        message = (
            "救援目录刷新完成: "
            f"条目 {stats['rescue_catalog_items']}, "
            f"来源 {stats['rescue_catalog_sources']}, "
            f"耗时 {duration_ms} ms"
        )
        logger.info(message)
        _report_catalog_log(reporter, "success", message)
        return stats
    except Exception as exc:
        message = f"救援目录刷新失败: {exc}"
        logger.warning(message)
        _report_catalog_log(reporter, "warning", message)
        return {}


def _sleep_with_cancel(
    seconds: float,
    stop_requested: StopRequested | None,
    interval: float = 0.2,
) -> bool:
    if seconds <= 0:
        return stop_requested is not None and stop_requested()

    remaining = float(seconds)
    while remaining > 0:
        if stop_requested is not None and stop_requested():
            return True
        sleep_for = min(interval, remaining)
        time.sleep(sleep_for)
        remaining -= sleep_for

    return stop_requested is not None and stop_requested()


def run_user_backup_task(
    settings: Any,
    user_id: int,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
    *,
    rebuild_catalog: bool = True,
    claim_finalization: ClaimFinalization | None = None,
) -> dict[str, Any]:
    if stop_requested is not None and stop_requested():
        _report_log(reporter, "info", "用户全量备份已停止")
        return {
            "user_id": user_id,
            "novels": 0,
            "skipped": 0,
            "assets_downloaded": 0,
            "stopped": True,
        }

    api = _login(settings)
    storage = _ensure_storage_dirs(settings)

    db = Database(settings.storage.db_path)
    db.init_schema()
    try:
        service = BookmarkNovelSyncService(
            api=api,
            db=db,
            storage=storage,
            settings=settings,
        )
        service.stop_requested = stop_requested
        user_name = _lookup_user_name(db, user_id)
        _report_log(reporter, "info", f"开始用户全量备份: {user_name} ({user_id})")

        total_novels = 0
        total_skipped = 0
        total_assets = 0
        total_failed = 0
        processed = 0
        total_seen = 0
        stopped = False
        next_query: dict[str, Any] | None = {"user_id": user_id}
        # 翻页上限兜底：防止 API 返回自引用 next_url 导致死循环
        max_pages = getattr(getattr(settings, "sync", None), "max_pages_per_run", None) or 200
        page_count = 0

        _report_progress(reporter, phase="user_backup", current=0, total=0, current_novel=user_name, author=user_name)

        while next_query:
            if stop_requested is not None and stop_requested():
                stopped = True
                break
            if page_count >= max_pages:
                message = f"用户全量备份翻页达到兜底上限 {max_pages} 页，提前停止: {user_name} ({user_id})"
                logger.warning(message)
                _report_log(reporter, "warning", message)
                break

            result = api.user_novels(**next_query)
            page_count += 1
            novels = getattr(result, "novels", []) or []
            total_seen += len(novels)
            for novel in novels:
                if stop_requested is not None and stop_requested():
                    stopped = True
                    break

                counters = service._sync_novel(
                    novel,
                    "public",
                    settings.sync.download_assets,
                    settings.sync.write_markdown,
                    settings.sync.write_raw_text,
                    source_type="user_backup",
                    source_key=str(user_id),
                )
                failed = counters.get("failed", 0)
                if failed:
                    total_failed += failed
                    # 3.1容错:单本失败累计,超20%或绝对10本再中止,保留已同步部分
                    if total_failed >= 10 or (processed > 0 and total_failed / processed > 0.2):
                        raise RuntimeError(f"User backup aborted for user {user_id}: {total_failed}/{processed} novels failed (threshold exceeded)")
                processed += 1
                total_novels += counters.get("novels", 0)
                total_skipped += counters.get("skipped", 0)
                total_assets += counters.get("assets_downloaded", 0)
                _report_progress(
                    reporter,
                    phase="user_backup",
                    current=processed,
                    total=total_seen,
                    current_novel=str(getattr(novel, "title", getattr(novel, "id", ""))),
                    author=user_name,
                )

            if stopped:
                break

            next_query = api.parse_qs(getattr(result, "next_url", None))
            if next_query and settings.sync.delay_seconds_between_pages > 0:
                if _sleep_with_cancel(settings.sync.delay_seconds_between_pages, stop_requested):
                    stopped = True
                    break

        if stopped:
            _report_log(reporter, "info", f"用户全量备份已停止: {user_name} ({user_id})")
        else:
            _report_log(reporter, "success", f"用户全量备份完成: {user_name} ({user_id}), 同步 {total_novels} 本")
        stats = {
            "user_id": user_id,
            "novels": total_novels,
            "skipped": total_skipped,
            "assets_downloaded": total_assets,
            "stopped": stopped,
        }
        if not stats.get("stopped") and stop_requested is not None and stop_requested():
            stats["stopped"] = True
        if rebuild_catalog and not stats.get("stopped"):
            if claim_finalization is not None and not claim_finalization():
                stats["stopped"] = True
            else:
                stats.update(_rebuild_rescue_catalog(db, reporter))
        return stats
    finally:
        db.close()


def run_user_status_task(
    settings: Any,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
) -> dict[str, Any]:
    return _run_user_status_like_task(
        settings=settings,
        reporter=reporter,
        stop_requested=stop_requested,
        task_label="用户状态检查",
        total_label="用户",
        list_items=_list_users_for_status_check,
        check_status=_check_pixiv_user_status,
        upsert_status=lambda db, user, status: db.upsert_user_status(user["user_id"], status),
        progress_name=lambda user: str(user.get("name") or user.get("user_id")),
        progress_id=lambda user: user.get("user_id"),
        total_key="total_users",
    )


def run_novel_status_task(
    settings: Any,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
    *,
    claim_finalization: ClaimFinalization | None = None,
) -> dict[str, Any]:
    batch_size = _resolve_novel_status_batch_size(settings)
    # 本轮起始时间用于统计「还剩多少篇没轮到」，与 SQLite CURRENT_TIMESTAMP 同格式
    started_at = _db_utc_now()

    def finalize_stats(db: Database, stats: dict[str, Any]) -> None:
        remaining = int(db.count_novels_pending_status_check(started_at))
        stats["batch_size"] = batch_size
        stats["remaining"] = remaining
        _report_log(
            reporter,
            "info",
            f"本轮批次上限 {batch_size} 篇，全库仍有 {remaining} 篇待轮转检查",
        )

    def build_already_missing(db: Database) -> Callable[[Any], bool]:
        known = db.get_known_missing_novel_ids()
        return lambda novel_id: int(novel_id) in known

    return _run_status_task(
        settings=settings,
        reporter=reporter,
        stop_requested=stop_requested,
        task_label="小说状态检查",
        total_label="小说",
        list_ids=lambda db: db.get_novel_ids_for_status_check(limit=batch_size),
        check_status=_check_novel_status,
        upsert_status=lambda db, item_id, status: db.upsert_novel_status(item_id, status),
        total_key="total_novels",
        rebuild_catalog=True,
        claim_finalization=claim_finalization,
        finalize_stats=finalize_stats,
        build_already_missing=build_already_missing,
    )


def run_series_status_task(
    settings: Any,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
    *,
    claim_finalization: ClaimFinalization | None = None,
) -> dict[str, Any]:
    return _run_status_task(
        settings=settings,
        reporter=reporter,
        stop_requested=stop_requested,
        task_label="系列状态检查",
        total_label="系列",
        list_ids=lambda db: db.get_series_ids_for_status_check(),
        check_status=_check_series_status,
        upsert_status=lambda db, item_id, status: db.upsert_series_status(item_id, status),
        total_key="total_series",
        rebuild_catalog=True,
        claim_finalization=claim_finalization,
    )


def run_pending_deletion_detection_task(
    settings: Any,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
) -> dict[str, Any]:
    if stop_requested is not None and stop_requested():
        _report_log(reporter, "info", "待删除检测已停止")
        return {
            "bookmark": {},
            "series": {},
            "new_pending": 0,
            "stopped": True,
        }

    _report_log(reporter, "info", "=== 开始检测取消收藏/追更 ===")

    api = _login(settings)
    auth_user_id = settings.pixiv.user_id
    _report_log(reporter, "success", f"登录成功, 用户ID: {auth_user_id}")

    if stop_requested is not None and stop_requested():
        _report_log(reporter, "info", "待删除检测已停止")
        return {
            "bookmark": {},
            "series": {},
            "new_pending": 0,
            "stopped": True,
        }

    db = Database(settings.storage.db_path)
    try:
        db.init_schema()
        storage = _ensure_storage_dirs(settings)
        service = BookmarkNovelSyncService(
            api=api,
            db=db,
            storage=storage,
            settings=settings,
        )
        service.stop_requested = stop_requested

        def on_progress(event_type: str, data: dict[str, Any]) -> None:
            if stop_requested is not None and stop_requested():
                raise InterruptedError("Task stopped by user")
            if event_type == "phase":
                _report_log(reporter, "info", str(data.get("phase", "")))
            elif event_type == "rate_limit":
                _report_log(reporter, "warning", f"等待 {data.get('seconds', 1)} 秒")

        _report_progress(reporter, phase="pending_deletion_detection", current=0, total=0)

        if stop_requested is not None and stop_requested():
            _report_log(reporter, "info", "待删除检测已停止")
            return {
                "bookmark": {},
                "series": {},
                "new_pending": 0,
                "stopped": True,
            }

        try:
            result = service.run_detection(
                user_id=auth_user_id,
                restricts=getattr(settings.sync, "bookmark_restricts", ["public"]),
                progress_callback=on_progress,
            )
        except InterruptedError:
            _report_log(reporter, "info", "待删除检测已停止")
            return {
                "bookmark": {},
                "series": {},
                "new_pending": 0,
                "stopped": True,
            }

        stats = dict(result)
        stats.setdefault("stopped", False)
        _report_log(reporter, "success", f"检测完成: 发现 {stats.get('new_pending', 0)} 条新的待确认记录")

        # Phase 3.2: 清理过期的pending_deletions记录
        try:
            cleanup_result = db.cleanup_old_pending_deletions(
                grace_period_days=getattr(settings.sync, "pending_deletion_grace_period_days", 30),
                cleanup_confirmed_days=getattr(settings.sync, "pending_deletion_cleanup_confirmed_days", 7)
            )
            if cleanup_result["auto_confirmed"] > 0 or cleanup_result["cleaned_up"] > 0:
                _report_log(
                    reporter, "info",
                    f"自动确认 {cleanup_result['auto_confirmed']} 条过期pending, 清理 {cleanup_result['cleaned_up']} 条旧记录"
                )
            stats.update(cleanup_result)
        except Exception as e:
            _report_log(reporter, "warning", f"清理过期记录失败: {e}")

        return stats
    finally:
        db.close()


def _run_user_status_like_task(
    settings: Any,
    reporter: JobReporter | None,
    stop_requested: StopRequested | None,
    task_label: str,
    total_label: str,
    list_items: Callable[[Database], list[dict[str, Any]]],
    check_status: Callable[[Any, int], str],
    upsert_status: Callable[[Database, dict[str, Any], str], None],
    progress_name: Callable[[dict[str, Any]], str],
    progress_id: Callable[[dict[str, Any]], Any],
    total_key: str,
) -> dict[str, Any]:
    api = _login(settings)
    _ensure_storage_dirs(settings)

    db = Database(settings.storage.db_path)
    db.init_schema()
    try:
        items = list_items(db)
        _report_log(reporter, "info", f"开始{task_label}")
        _report_log(reporter, "info", f"共 {len(items)} 个{total_label}需要检查")
        return _process_status_items(
            settings=settings,
            reporter=reporter,
            stop_requested=stop_requested,
            db=db,
            items=items,
            check_status=lambda item: check_status(api, int(progress_id(item))),
            upsert_status=upsert_status,
            item_label=total_label,
            item_name=progress_name,
            total_key=total_key,
        )
    finally:
        db.close()


def _run_status_task(
    settings: Any,
    reporter: JobReporter | None,
    stop_requested: StopRequested | None,
    task_label: str,
    total_label: str,
    list_ids: Callable[[Database], list[int]],
    check_status: Callable[[Any, int], str],
    upsert_status: Callable[[Database, int, str], None],
    total_key: str,
    rebuild_catalog: bool = False,
    claim_finalization: ClaimFinalization | None = None,
    finalize_stats: Callable[[Database, dict[str, Any]], None] | None = None,
    build_already_missing: Callable[[Database], Callable[[Any], bool]] | None = None,
) -> dict[str, Any]:
    api = _login(settings)
    _ensure_storage_dirs(settings)

    db = Database(settings.storage.db_path)
    db.init_schema()
    try:
        item_ids = list_ids(db)
        _report_log(reporter, "info", f"开始{task_label}")
        _report_log(reporter, "info", f"共 {len(item_ids)} 个{total_label}需要检查")
        already_missing: Callable[[Any], bool] | None = None
        if build_already_missing is not None:
            # 快照失败不该拖垮整轮：退化成 None 即恢复旧的严格熔断行为
            try:
                already_missing = build_already_missing(db)
            except Exception as exc:
                logger.warning("%s已知删除快照失败，熔断退回严格模式: %s", task_label, exc)
        stats = _process_status_items(
            settings=settings,
            reporter=reporter,
            stop_requested=stop_requested,
            db=db,
            items=item_ids,
            check_status=lambda item_id: check_status(api, item_id),
            upsert_status=upsert_status,
            item_label=total_label,
            item_name=lambda item_id: str(item_id),
            total_key=total_key,
            already_missing=already_missing,
        )
        if finalize_stats is not None:
            # 批次统计只是运维观测信息，失败不应拖垮整轮任务
            try:
                finalize_stats(db, stats)
            except Exception as exc:
                logger.warning("%s批次统计失败: %s", task_label, exc)
        if not stats.get("stopped") and stop_requested is not None and stop_requested():
            stats["stopped"] = True
        if rebuild_catalog and not stats.get("stopped"):
            if claim_finalization is not None and not claim_finalization():
                stats["stopped"] = True
            else:
                stats.update(_rebuild_rescue_catalog(db, reporter))
        return stats
    finally:
        db.close()


def _resolve_novel_status_batch_size(settings: Any) -> int:
    """批大小优先读 settings.sync.novel_status_batch_size，缺省用模块常量。"""
    raw = getattr(getattr(settings, "sync", None), "novel_status_batch_size", None)
    try:
        value = int(raw) if raw not in (None, "") else 0
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else NOVEL_STATUS_BATCH_SIZE


def _db_utc_now() -> str:
    """返回与 SQLite CURRENT_TIMESTAMP 同格式的 UTC 时间串，用于比较 last_checked_at。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _login(settings: Any) -> Any:
    auth = PixivAuthManager(settings.pixiv)
    api, auth_result = auth.login()
    if auth_result.user_id is None:
        raise RuntimeError("Unable to determine user ID")
    if getattr(settings.pixiv, "user_id", None) is None:
        settings.pixiv.user_id = auth_result.user_id
    _persist_self_profile(settings, auth_result)
    return api


def _persist_self_profile(settings: Any, auth_result: Any) -> None:
    """登录成功后刷新本人账号资料（含会员状态），供侧边栏展示。

    users 表只存被关注的作者，本人账号不在其中，因此侧边栏必须依赖这份数据。
    落库失败不影响同步任务本身。
    """
    try:
        profile = auth_result.self_profile() if hasattr(auth_result, "self_profile") else None
        if not profile:
            return
        db = Database(settings.storage.db_path)
        try:
            db.init_schema()
            db.save_self_profile(profile)
        finally:
            db.close()
    except Exception:  # pragma: no cover - 落库失败不能影响同步主流程
        logger.debug("保存本人账号资料失败", exc_info=True)


def _ensure_storage_dirs(settings: Any) -> FileStorage:
    storage = FileStorage(settings)
    storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])
    return storage


def _process_status_items(
    settings: Any,
    reporter: JobReporter | None,
    stop_requested: StopRequested | None,
    db: Database,
    items: list[Any],
    check_status: Callable[[Any], str],
    upsert_status: Callable[[Database, Any, str], None],
    item_label: str,
    item_name: Callable[[Any], str],
    total_key: str,
    already_missing: Callable[[Any], bool] | None = None,
) -> dict[str, Any]:
    checked_count = 0
    status_counts: dict[str, int] = {}
    stopped = False
    consecutive_unknown = 0
    consecutive_missing = 0
    aborted_reason: str | None = None
    confirmed_missing = 0
    total = len(items)

    _report_progress(reporter, phase=item_label, current=0, total=total)

    for item in items:
        if stop_requested is not None and stop_requested():
            stopped = True
            break

        status = check_status(item)
        upsert_status(db, item, status)
        checked_count += 1
        status_counts[status] = status_counts.get(status, 0) + 1

        _report_log(reporter, "info", f"[{checked_count}/{total}] {item_label} {item_name(item)}: {status}")
        _report_progress(reporter, phase=item_label, current=checked_count, total=total, current_novel=item_name(item), author="")

        # 两个熔断计数各自独立：任一状态只要不属于该类，就把该类计数清零
        consecutive_unknown = consecutive_unknown + 1 if status == "unknown" else 0
        # 「已删除」里要把「本来就已知是 deleted，这次只是再次确认」剔掉：那是一致的
        # 结论，不是限流伪装成不存在的证据。只有原本不是 missing 状态的条目突然变成
        # missing，才是需要警惕的信号。见 get_known_missing_novel_ids 的注释。
        if status in MISSING_STATUSES:
            if already_missing is not None and already_missing(item):
                confirmed_missing += 1
            else:
                consecutive_missing += 1
        else:
            consecutive_missing = 0

        # unknown 说明这次调用没拿到可信结果；连续多次即认定被限流，立刻收手，
        # 否则会顶着限流把剩余条目全部刷成无效结果（还会进一步加重限流）。
        if consecutive_unknown >= MAX_CONSECUTIVE_UNKNOWN:
            message = (
                f"连续 {consecutive_unknown} 个{item_label}状态无法判定，疑似触发 Pixiv 限流，"
                f"已中止本轮检查（已检查 {checked_count}/{total}，未检查的保持原状态）"
            )
            logger.warning(message)
            # error 级别：任务日志里会标红，别让风控中止淹没在一片 info 里
            _report_log(reporter, "error", message)
            aborted_reason = "rate_limited"
            stopped = True
            break

        # 删除判定的兜底：正常情况下 deleted 是零星分布的，连续一大段全是删除，
        # 更可能是 API 限流伪装成「不存在」，而不是真有这么多作品同时消失。
        if consecutive_missing >= MAX_CONSECUTIVE_MISSING:
            message = (
                f"连续 {consecutive_missing} 个{item_label}判定为已删除，疑似 API 限流伪装成不存在，"
                f"已中止本轮检查以防误判（已检查 {checked_count}/{total}）"
            )
            logger.warning(message)
            _report_log(reporter, "error", message)
            aborted_reason = "suspicious_missing_streak"
            stopped = True
            break

        if _sleep_with_cancel(settings.sync.delay_seconds_between_skips, stop_requested):
            stopped = True
            break

    if aborted_reason:
        _report_log(reporter, "error", f"{item_label}状态检查提前中止: 已检查 {checked_count} 个")
    else:
        _report_log(reporter, "success", f"{item_label}状态检查完成: {checked_count} 个")
    stats: dict[str, Any] = {
        "checked_count": checked_count,
        total_key: total,
        # status_counts 里含 unknown 计数，运维据此判断限流影响面
        "status_counts": status_counts,
        "stopped": stopped,
    }
    if confirmed_missing:
        # 「早就知道没了、这次只是再次确认」的数量。单独暴露出来，避免运维看到
        # status_counts.deleted 很大就以为又出了批量误判。
        stats["confirmed_missing"] = confirmed_missing
    if aborted_reason:
        stats["aborted_reason"] = aborted_reason
    return stats


def _list_all_users(db: Database) -> list[dict[str, Any]]:
    users: list[dict[str, Any]] = []
    page_num = 1
    while True:
        page_data = db.list_users(page=page_num, page_size=500)
        items = page_data.get("items", [])
        if not items:
            break
        users.extend(items)
        if page_num >= page_data.get("total_pages", 1):
            break
        page_num += 1
    return users


def _list_users_for_status_check(db: Database) -> list[dict[str, Any]]:
    """状态检查专用的用户清单：按 last_checked_at 轮转，最久未检查的排最前。

    不能用 ``_list_all_users``（走 ``list_users`` 的列表页排序），否则每轮顺序固定，
    熔断一次就把队尾永久饿死。见 ``get_users_for_status_check`` 的注释——清单里还会
    剔除降频中的已知受限用户，所以 total_users 可能小于全部用户数。
    """
    return db.get_users_for_status_check()


def _lookup_user_name(db: Database, user_id: int) -> str:
    row = db.conn.execute("SELECT name FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row and row[0]:
        return str(row[0])
    return str(user_id)


def _check_pixiv_user_status(api: Any, user_id: int) -> str:
    from pixiv_novel_sync.webapp import _check_pixiv_user_status as check_status

    return check_status(api, user_id)


def _check_novel_status(api: Any, novel_id: int) -> str:
    from pixiv_novel_sync.webapp import _check_novel_status as check_status

    return check_status(api, novel_id)


def _check_series_status(api: Any, series_id: int) -> str:
    from pixiv_novel_sync.webapp import _check_series_status as check_status

    return check_status(api, series_id)


def _report_log(reporter: JobReporter | None, level: str, message: str) -> None:
    if reporter is not None:
        reporter.add_log(level, message)


def _report_progress(reporter: JobReporter | None, **kwargs: Any) -> None:
    if reporter is not None:
        reporter.update_progress(**kwargs)

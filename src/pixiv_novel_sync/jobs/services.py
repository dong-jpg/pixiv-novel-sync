from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import logging
import time
from time import perf_counter
from typing import Any

from pixiv_novel_sync.auth import PixivAuthManager
from pixiv_novel_sync.models import SourceRecord
from pixiv_novel_sync.storage_db import Database, prepare_schema
from pixiv_novel_sync.storage_files import FileStorage
from pixiv_novel_sync.sync.author_history import AuthorHistoryCursor
from pixiv_novel_sync.sync.utils import apply_pixiv_retry_patch, next_user_page_query, positive_pixiv_id, response_is_pixiv_error_page, response_is_rate_limited
from pixiv_novel_sync.sync_engine import (
    BookmarkNovelSyncService,
    SyncAuthFailed,
    SyncRateLimited,
    _iso_age_days,
    _recently_confirmed_deleted_novel_ids,
)

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

# 认证失败的状态值（web/utils._check_*_status 在 token 失效时返回）。
# 与 unknown 的区别：它是确定性错误——该次调用重试无意义，等 15 个条目烧完
# 「疑似限流」熔断只会让日志归因错误（2026-11-20~23 生产事故即此形态）。
AUTH_FAILED_STATUS = "auth_failed"

# 全量备份按失败比例中止单个用户之前，至少要处理过这么多本。以前没有最小样本：
# 第 1 本失败就是 1/1 > 20%，整个用户中止，在轮转里被跳过一整圈。失败累计到
# USER_BACKUP_MAX_FAILED_NOVELS 本仍然立即中止（那是确定的坏信号，与样本量无关）。
USER_BACKUP_FAILURE_MIN_SAMPLE = 10
USER_BACKUP_MAX_FAILED_NOVELS = 10
USER_BACKUP_MAX_FAILURE_RATIO = 0.2

# 已确认 deleted 的系列多久复查一次（天）。与小说侧 KNOWN_DELETED_RECHECK_DAYS
# 同语义——降频而非永久跳过：作者可能重新上传或恢复系列。
KNOWN_DELETED_SERIES_RECHECK_DAYS = 7
SERIES_STATUS_BATCH_SIZE = 300

# 手动作者备份每 7 天允许全部重抓一次；定时备份始终只补缺失归档。
# 全部重抓保留为唯一会重抓已归档正文的路径：作者改了正文、或 S2 那种被错误页覆写成
# 占位数据（title=novel_<id>、user_id=0，正文仍在所以 archive_complete 为真）的作品，
# 只能靠它修回。上次全部重抓的时间按作者记在这个水位里，只有该作者那一轮完整跑完
# （没停、没失败、没触顶）才更新。
USER_BACKUP_FULL_REFRESH_DAYS = 7
USER_BACKUP_FULL_REFRESH_WATERMARK = "user_backup_full_refresh"

# user_backup 翻页的专用兜底上限（每页 30 本 ⇒ 200 页 = 6000 本）。历史事故
# （2026-11 生产）：未配置 user_backup_max_pages_per_run 时回退到共享的
# max_pages_per_run=2，整库导出被砍成每作者 60 本。绝不复用 max_pages_per_run。
USER_BACKUP_MAX_PAGES = 200

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


def _catalog_inputs_changed(stats: dict[str, Any]) -> bool:
    """收藏、备份、关注作者同步只有真正写下小说或资源时才刷新救援目录。"""
    return any(int(stats.get(key) or 0) > 0 for key in ("novels", "texts_updated", "assets_downloaded"))


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
    scheduled_owner: int | None = None,
    api: Any = None,
    private_author: bool = False,
    body_recheck_budget: int = 0,
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

    if api is None:
        api = _login(settings, stop_requested=stop_requested)
    storage = _ensure_storage_dirs(settings)

    db = Database(settings.storage.db_path)
    try:
        prepare_schema(db)
        service = BookmarkNovelSyncService(
            api=api,
            db=db,
            storage=storage,
            settings=settings,
        )
        service.stop_requested = stop_requested
        user_name = _lookup_user_name(db, user_id)
        full_refresh = scheduled_owner is None and _needs_full_refresh(_load_full_refresh_marks(db), user_id)
        mode_label = "全部重抓" if full_refresh else "增量：已归档的跳过"
        _report_log(reporter, "info", f"开始用户全量备份: {user_name} ({user_id})（{mode_label}）")
        item_delay = float(getattr(settings.sync, "delay_seconds_between_items", 0) or 0)

        total_novels = 0
        total_skipped = 0
        total_skipped_archived = 0
        visited = 0
        total_assets = 0
        total_failed = 0
        processed = 0
        total_seen = 0
        stopped = False
        truncated_by_page_cap = False
        total_failed_page = False
        next_query: dict[str, Any] | None = {"user_id": user_id}
        history = AuthorHistoryCursor(db, scheduled_owner, user_id) if scheduled_owner is not None else None
        if history and history.resumed:
            next_query["offset"] = history.offset
        unavailable_ids = history.unavailable if history else set()
        rate_limited = False
        metrics = {name: 0 for name in ("novels_created", "metadata_updated", "texts_updated", "failed_assets", "assets_repaired", "body_rechecks")}
        # 翻页上限兜底：防止 API 返回自引用 next_url 导致死循环。
        # 备份是整库导出，绝不能回退到 max_pages_per_run（生产事故 2026-11：
        # 该字段缺省时回落共享值 2，每作者只备到最新 ~60 本，第 61 本起永久缺失；
        # 截断又让 full_refresh 水位写不进，同批作者每轮重抓前 60 本）。
        # 仿 FOLLOWING_LIST_MAX_PAGES 的模式：专用常量兜底，YAML 的
        # user_backup_max_pages_per_run 仍然可以覆盖。
        max_pages = getattr(getattr(settings, "sync", None), "user_backup_max_pages_per_run", None)
        if max_pages is None:
            max_pages = USER_BACKUP_MAX_PAGES
        try:
            max_pages = int(max_pages)
        except (TypeError, ValueError):
            max_pages = USER_BACKUP_MAX_PAGES
        if max_pages <= 0:
            max_pages = USER_BACKUP_MAX_PAGES
        if scheduled_owner is not None:
            max_pages = min(max_pages, 2)
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
                truncated_by_page_cap = True
                break

            try:
                result = api.user_novels(**next_query)
            except InterruptedError:
                raise
            except Exception as exc:
                if not response_is_rate_limited(exc):
                    raise
                rate_limited = True
                break
            page_count += 1
            if response_is_rate_limited(result):
                rate_limited = True
                break
            if response_is_pixiv_error_page(result) or not isinstance(getattr(result, "novels", None), (list, tuple)):
                from pixiv_novel_sync.web.utils import _is_auth_error, _pixiv_error_text

                if _is_auth_error(_pixiv_error_text(result)):
                    _report_log(reporter, "error", "用户备份中止：Pixiv 认证失败（token 已失效）")
                    raise SyncAuthFailed("Pixiv authentication failed while listing user novels")
                # A14-01：Pixiv 限流/鉴权失败时 pixivpy 不抛异常而是回
                # {"error": {...}}。旧代码 getattr(result, "novels", []) 把它当
                # 「空的最后一页」记成功，备份没拉到数据却是绿灯。错误页必须按
                # 失败收口：标记 incomplete 让任务日志显示 partial，并停止翻页。
                message = (
                    f"用户全量备份遇到上游错误页（第 {page_count} 页），停止翻页: "
                    f"{user_name} ({user_id})"
                )
                logger.error("%s: %s", message, result)
                _report_log(reporter, "error", message)
                total_failed_page = True
                break
            novels = getattr(result, "novels", []) or []
            page_offset = next_query.get("offset", 0)
            if history is not None:
                try:
                    next_url = result["next_url"] if isinstance(result, dict) else result.next_url
                    following_query = next_user_page_query(api, next_url, next_query)
                    following_offset = following_query["offset"] if following_query else None
                    page_ids = [positive_pixiv_id(novel.id) for novel in novels]
                    history.verify(page_offset, page_ids, following_offset)
                except (AttributeError, KeyError, TypeError, ValueError):
                    total_failed_page = True
                    break
            total_seen += len(novels)
            for novel in novels:
                if stop_requested is not None and stop_requested():
                    stopped = True
                    break
                visited += 1
                novel_id = int(novel.id)

                archived = not full_refresh and service._archive_complete_on_disk(novel_id, settings.sync.download_assets)
                recheck = False
                if archived and body_recheck_budget > 0:
                    from ..sync.series_inventory import needs_refresh
                    recheck = needs_refresh(db, novel_id, getattr(settings.sync, "archive_rescan_days", 7))
                if archived and not recheck:
                    # 纯本地判定，不发请求、不间隔；来源照常登记（与以前重访时一致）
                    with db.transaction():
                        db.upsert_source(
                            SourceRecord(novel_id=novel_id, source_type="user_backup", source_key=str(user_id))
                        )
                        db.touch_novel(novel_id)
                    total_skipped_archived += 1
                    _report_progress(
                        reporter,
                        phase="user_backup",
                        current=visited,
                        total=total_seen,
                        current_novel=str(getattr(novel, "title", novel_id)),
                        author=user_name,
                    )
                    continue

                if recheck:
                    body_recheck_budget -= 1
                    metrics["body_rechecks"] += 1
                try:
                    counters = service._sync_novel(
                        novel,
                        # 全量备份不知道作品在哪个收藏列表：沿用已归档的 restrict，
                        # 新作品落 public。写死 "public" 会把私密收藏搬进公开树（S10）
                        "private" if private_author else None,
                        settings.sync.download_assets,
                        settings.sync.write_markdown,
                        settings.sync.write_raw_text,
                        source_type="user_backup",
                        source_key=str(user_id),
                    )
                except InterruptedError:
                    raise
                except SyncRateLimited:
                    rate_limited = True
                    break
                except SyncAuthFailed:
                    # token 已失效：按中止收口（不堆 failed、不算该作者完成）。
                    message = f"用户全量备份中止：Pixiv 认证失败（token 已失效）: {user_name} ({user_id})"
                    logger.error(message)
                    _report_log(reporter, "error", message)
                    raise
                processed += 1
                if counters.get("content_unavailable"):
                    unavailable_ids.add(novel_id)
                elif not counters.get("failed"):
                    unavailable_ids.discard(novel_id)
                for metric in metrics:
                    metrics[metric] += counters.get(metric, 0)
                failed = counters.get("failed", 0)
                if failed:
                    total_failed += failed
                # A success can reach the minimum sample after earlier failures.
                ratio_exceeded = (
                    processed >= USER_BACKUP_FAILURE_MIN_SAMPLE
                    and total_failed / processed > USER_BACKUP_MAX_FAILURE_RATIO
                )
                if total_failed >= USER_BACKUP_MAX_FAILED_NOVELS or ratio_exceeded:
                    raise RuntimeError(f"User backup aborted for user {user_id}: {total_failed}/{processed} novels failed (threshold exceeded)")
                total_novels += counters.get("novels", 0)
                total_skipped += counters.get("skipped", 0)
                total_assets += counters.get("assets_downloaded", 0)
                _report_progress(
                    reporter,
                    phase="user_backup",
                    current=visited,
                    total=total_seen,
                    current_novel=str(getattr(novel, "title", getattr(novel, "id", ""))),
                    author=user_name,
                )
                # 以前逐本没有间隔，是仓库里唯一不遵守请求间隔的批量路径（P19）
                if item_delay > 0 and _sleep_with_cancel(item_delay, stop_requested):
                    stopped = True
                    break

            if stopped or rate_limited or (scheduled_owner is not None and total_failed):
                break

            next_url = getattr(result, "next_url", None)
            if history is None:
                next_query = api.parse_qs(next_url)
            else:
                if stop_requested is not None and stop_requested():
                    stopped = True
                    break
                next_query = following_query
                history.advance(page_offset, page_ids, following_offset)
                if next_query and page_count >= max_pages:
                    truncated_by_page_cap = True
                    break
            if next_query and settings.sync.delay_seconds_between_pages > 0:
                if _sleep_with_cancel(settings.sync.delay_seconds_between_pages, stop_requested):
                    stopped = True
                    break

        if not stopped and stop_requested is not None and stop_requested():
            stopped = True
        completed = not (stopped or rate_limited or total_failed or total_failed_page or truncated_by_page_cap)
        if full_refresh and completed:
            _mark_full_refresh(db, user_id)
        if not full_refresh:
            mode_label = f"增量：跳过已归档 {total_skipped_archived} 本"
        if stopped:
            _report_log(reporter, "info", f"用户全量备份已停止: {user_name} ({user_id})")
        elif rate_limited:
            _report_log(reporter, "warning", "Pixiv 请求已被限流，本轮备份停止，保留未完成页")
        elif truncated_by_page_cap and scheduled_owner is not None and not total_failed and not total_failed_page:
            _report_log(reporter, "info", f"本轮完成 {page_count} 页，下轮续页: {user_name} ({user_id})")
        elif total_failed or total_failed_page or truncated_by_page_cap:
            _report_log(reporter, "warning", f"用户全量备份部分完成: {user_name} ({user_id}), 同步 {total_novels} 本, 失败 {total_failed} 本（{mode_label}）")
        else:
            _report_log(reporter, "success", f"用户全量备份完成: {user_name} ({user_id}), 同步 {total_novels} 本（{mode_label}）")
        stats = {
            **metrics,
            "user_id": user_id,
            "novels": total_novels,
            "skipped": total_skipped,
            # 已归档、本轮没发请求就跳过的；全部重抓的轮次恒为 0
            "skipped_archived": total_skipped_archived,
            "full_refresh": full_refresh,
            "assets_downloaded": total_assets,
            "stopped": stopped,
        }
        if total_failed:
            stats.update(failed=total_failed, incomplete=True, aborted_reason="novel_backup_errors")
        if total_failed_page:
            # 上游错误页：数据没拉全却没有任何单本失败，必须单独标记
            stats.update(incomplete=True, aborted_reason="upstream_error_page")
        if truncated_by_page_cap:
            stats["incomplete"] = True
            if scheduled_owner is not None and not total_failed and not total_failed_page:
                stats.update(rotation_pending=True, more_pages=True)
            else:
                stats["truncated"] = True
        if rate_limited:
            stats.update(incomplete=True, aborted_reason="rate_limited")
        if history is not None:
            enumerated = bool(completed and history.enumerated)
            stats.update(pages_read=page_count, boundary_pages=0, resumed=history.resumed, unavailable_count=len(unavailable_ids),
                         history_enumerated=enumerated, history_verified=enumerated and history.verified,
                         history_complete=enumerated and history.verified and not unavailable_ids)
            if enumerated and not history.verified:
                stats.update(incomplete=True, rotation_pending=True, more_pages=True, history_verification_pending=True)
            if unavailable_ids:
                stats.update(incomplete=True)
                stats.setdefault("aborted_reason", "history_content_unavailable")
        if not stats.get("stopped") and stop_requested is not None and stop_requested():
            stats["stopped"] = True
        if rebuild_catalog and not stats.get("stopped") and _catalog_inputs_changed(stats):
            if claim_finalization is not None and not claim_finalization():
                stats["stopped"] = True
            else:
                stats.update(_rebuild_rescue_catalog(db, reporter))
        return stats
    finally:
        db.close()


def _load_full_refresh_marks(db: Any) -> dict[str, str]:
    """读各作者上次全部重抓的时间；读不到一律当没有（于是全部重抓，宁多勿漏）。"""
    try:
        raw = db.get_watermark(USER_BACKUP_FULL_REFRESH_WATERMARK)
    except Exception as exc:
        logger.warning("读取全量备份刷新水位失败，本轮全部重抓: %s", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    marks = raw.get("full_refresh_at")
    return {str(k): str(v) for k, v in marks.items()} if isinstance(marks, dict) else {}


def _needs_full_refresh(marks: dict[str, str], user_id: int) -> bool:
    return _iso_age_days(marks.get(str(int(user_id)))) >= USER_BACKUP_FULL_REFRESH_DAYS


def _mark_full_refresh(db: Any, user_id: int) -> None:
    marks = _load_full_refresh_marks(db)
    marks[str(int(user_id))] = datetime.now(timezone.utc).isoformat()
    try:
        db.update_watermark(USER_BACKUP_FULL_REFRESH_WATERMARK, {"full_refresh_at": marks})
    except Exception as exc:
        # 写不进去只会让下一轮再全部重抓一次，不影响本轮结果
        logger.warning("记录全量备份刷新时间失败: %s", exc)


def run_user_status_task(
    settings: Any,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
) -> dict[str, Any]:
    def followed_users(db):
        from ..sync.following_snapshot import known_following_members

        members = known_following_members(db, getattr(settings.pixiv, "user_id", None))
        if members is None:
            return None
        return [user for user in _list_users_for_status_check(db) if user["user_id"] in members]

    def build_already_missing(db: Database) -> Callable[[Any], bool]:
        # 已知 suspended 的再次确认是持续结论，不该推进 30 连熔断（MISSING_STATUSES
        # 同时含 suspended；series 侧 2026-11 同类假熔断的泛化修复）。生产上封禁
        # 用户零星（1-2 个）凑不满 30 连，但豁免保证边界形态（批量封禁聚集）下
        # 也不误报。
        known = db.get_known_suspended_user_ids()
        return lambda user: int(user["user_id"]) in known

    return _run_user_status_like_task(
        settings=settings,
        reporter=reporter,
        stop_requested=stop_requested,
        task_label="用户状态检查",
        total_label="用户",
        list_items=followed_users,
        check_status=_check_pixiv_user_status,
        upsert_status=lambda db, user, status: db.upsert_user_status(user["user_id"], status),
        progress_name=lambda user: str(user.get("name") or user.get("user_id")),
        progress_id=lambda user: user.get("user_id"),
        total_key="total_users",
        build_already_missing=build_already_missing,
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
    deferred: set[int] = set()

    def list_ids(db: Database) -> list[int]:
        # 已确认 deleted、近期复查过的不进本批（P21）。在取批时排除而不是取完再丢：
        # 丢掉的条目 last_checked_at 不会刷新，会每轮都占着队首的批次名额。
        # 多取 len(deferred) 条，保证排除后仍能凑满一批。
        deferred.update(_recently_confirmed_deleted_novel_ids(db))
        ids = db.get_novel_ids_for_status_check(limit=batch_size + len(deferred))
        return [novel_id for novel_id in ids if int(novel_id) not in deferred][:batch_size]

    def finalize_stats(db: Database, stats: dict[str, Any]) -> None:
        # 降频中的已删除作品本轮本来就不查，不算「待轮转」
        remaining = max(int(db.count_novels_pending_status_check(started_at)) - len(deferred), 0)
        if deferred:
            stats["deleted_recheck_deferred"] = len(deferred)
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
        list_ids=list_ids,
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
    # 已删系列 7 天内复查过的不进本批；deferred 同时进 stats 供运维观测
    deferred: set[int] = set()
    remaining = 0

    def list_ids(db: Database) -> list[int]:
        nonlocal remaining
        # 已删系列 7 天内复查过的不进本批（与 novel_status 的 P21 降频同语义）。
        # 这同时省掉重复确认的请求：生产上 45 个已删系列每轮都吃 ~15% 检查量。
        # 查询失败退回全查（fail-safe），与小说侧一致。
        try:
            cutoff = (
                datetime.now(timezone.utc)
                - timedelta(days=KNOWN_DELETED_SERIES_RECHECK_DAYS)
            ).strftime("%Y-%m-%d %H:%M:%S")
            rows = db.conn.execute(
                "SELECT series_id FROM series "
                "WHERE status IN ('deleted', 'suspended') "
                "AND datetime(last_checked_at) >= datetime(?)",
                (cutoff,),
            ).fetchall()
            deferred.update(int(row[0]) for row in rows)
        except InterruptedError:
            raise
        except Exception as exc:
            logger.warning("读取近期已确认删除的系列失败，本轮全部重查: %s", exc)
        ids = db.get_series_ids_for_status_check()
        eligible = [series_id for series_id in ids if int(series_id) not in deferred]
        remaining = max(0, len(eligible) - SERIES_STATUS_BATCH_SIZE)
        return eligible[:SERIES_STATUS_BATCH_SIZE]

    def finalize_stats(db: Database, stats: dict[str, Any]) -> None:
        if deferred:
            stats["deleted_recheck_deferred"] = len(deferred)
        if remaining:
            stats.update(series_status_remaining=remaining, rotation_pending=True)

    def build_already_missing(db: Database) -> Callable[[Any], bool]:
        # known-deleted 再次确认是一致结论，不能推进 30 连熔断（生产 2026-11 事故：
        # 45 个已删系列聚集凑满 30 连，99% 完成度处把整轮打成 suspicious_missing_streak）。
        known = db.get_known_missing_series_ids()
        return lambda series_id: int(series_id) in known

    return _run_status_task(
        settings=settings,
        reporter=reporter,
        stop_requested=stop_requested,
        task_label="系列状态检查",
        total_label="系列",
        list_ids=list_ids,
        check_status=_check_series_status,
        upsert_status=lambda db, item_id, status: db.upsert_series_status(item_id, status),
        total_key="total_series",
        rebuild_catalog=True,
        claim_finalization=claim_finalization,
        finalize_stats=finalize_stats,
        build_already_missing=build_already_missing,
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

    api = _login(settings, stop_requested=stop_requested)
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
        prepare_schema(db)
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
        # 子检测的中止标记嵌在 bookmark / series 子字典里，而任务日志的 partial 判定
        # （webapp.py:_task_log_status_for_stats）只看顶层：不提上来，收藏翻页失败或
        # 追更清单拉不到时这一轮会记成绿色的「成功」。
        sub_reasons = [
            (name, sub.get("aborted_reason"), sub.get("truncated"))
            for name, sub in (("bookmark", stats.get("bookmark")), ("series", stats.get("series")))
            if isinstance(sub, dict)
        ]
        failed_parts = [name for name, reason, truncated in sub_reasons if reason or truncated]
        if failed_parts:
            stats["aborted_reason"] = next(
                (reason for _name, reason, _truncated in sub_reasons if reason), "fetch_failed"
            )
            if any(truncated for _name, _reason, truncated in sub_reasons):
                stats["truncated"] = True
            stats["incomplete"] = True
            _report_log(reporter, "warning", f"检测未完整: {'、'.join(failed_parts)}（{stats['aborted_reason']}）")

        # Phase 3.2: 清理已确认/已恢复的历史记录。pending 记录不在此列——它们必须由
        # 用户手动确认或恢复，没有"宽限期到了就自动确认"这回事（否则待确认列表会静默消失）。
        try:
            cleanup_result = db.cleanup_old_pending_deletions(
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

        # 本任务改的是 pending_deletions，而拯救目录按「有没有 pending 行」排除条目：
        # 不重建的话，新取消收藏的作品会继续挂在拯救页上，直到下一个同步/状态检查
        # 任务顺手重建为止——用户点完「检测取消收藏」马上去看拯救页，看到的还是旧状态。
        # 同理，cleanup 把 restored 记录清掉后也要重建，让「我要留着」的作品回到目录。
        # 中止（stopped）时跳过：与 _run_status_task 的 rebuild_catalog 分支同一语义。
        if not stats.get("stopped"):
            stats.update(_rebuild_rescue_catalog(db, reporter))

        return stats
    finally:
        db.close()


def _make_reauth_handler(
    settings: Any,
    stop_requested: StopRequested | None,
    task_label: str,
) -> tuple[Callable[[], Any], Callable[[], bool]]:
    """构造可中途重登的 (取 api, 认证失败回调) 对。

    pixivpy3 登录后 access token 硬 1 小时过期且无自动刷新（api.py 只在
    token 为 None 时抛错），单轮任务逼近 60 分钟就会整体失效。状态检查的
    ``check_status`` 回调闭包持有旧 api 对象，因此重登必须换新实例并把
    重试补丁原样打上；回调返回 True 表示已重登、当前条目可重查一次。

    每任务最多重登一次：refresh token 若也失效，第二次 ``auth()`` 同样失败，
    无限重试只会把熔断额度烧在认证端点上。
    """
    state = {"api": _login(settings, stop_requested=stop_requested), "used": False}

    def get_api() -> Any:
        return state["api"]

    def on_auth_failed() -> bool:
        if state["used"] or getattr(state["api"], "__dict__", {}).get("_pixiv_reauthenticate"):
            return False
        state["used"] = True
        api = _login(settings, stop_requested=stop_requested)
        apply_pixiv_retry_patch(api, stop_requested=stop_requested)
        state["api"] = api
        logger.warning("%s：access token 已失效，已重新登录并继续", task_label)
        return True

    return get_api, on_auth_failed


def _run_user_status_like_task(
    settings: Any,
    reporter: JobReporter | None,
    stop_requested: StopRequested | None,
    task_label: str,
    total_label: str,
    list_items: Callable[[Database], list[dict[str, Any]] | None],
    check_status: Callable[[Any, int], str],
    upsert_status: Callable[[Database, dict[str, Any], str], None],
    progress_name: Callable[[dict[str, Any]], str],
    progress_id: Callable[[dict[str, Any]], Any],
    total_key: str,
    build_already_missing: Callable[[Database], Callable[[Any], bool]] | None = None,
) -> dict[str, Any]:
    get_api, on_auth_failed = _make_reauth_handler(settings, stop_requested, task_label)
    api = get_api()
    # 状态检查不经过 BookmarkNovelSyncService，取消回调只能在这里绑到重试补丁上，
    # 否则 user_detail 的 429/网络退避是一整段不可取消的 sleep（S7）。
    apply_pixiv_retry_patch(api, stop_requested=stop_requested)
    _ensure_storage_dirs(settings)

    db = Database(settings.storage.db_path)
    try:
        prepare_schema(db)
        items = list_items(db)
        if items is None:
            _report_log(reporter, "warning", "尚未取得完整的公开和私密关注名单，本轮不检查其他作者")
            return {total_key: 0, "checked_count": 0, "incomplete": True, "aborted_reason": "following_snapshot_unavailable"}
        _report_log(reporter, "info", f"开始{task_label}")
        _report_log(reporter, "info", f"共 {len(items)} 个{total_label}需要检查")
        already_missing: Callable[[Any], bool] | None = None
        if build_already_missing is not None:
            # 快照失败不该拖垮整轮：退化成 None 即恢复旧的严格熔断行为
            try:
                already_missing = build_already_missing(db)
            except Exception as exc:
                logger.warning("%s已知封禁快照失败，熔断退回严格模式: %s", task_label, exc)
        return _process_status_items(
            settings=settings,
            reporter=reporter,
            stop_requested=stop_requested,
            db=db,
            items=items,
            check_status=lambda item: check_status(get_api(), int(progress_id(item))),
            upsert_status=upsert_status,
            item_label=total_label,
            item_name=progress_name,
            total_key=total_key,
            already_missing=already_missing,
            on_auth_failed=on_auth_failed,
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
    get_api, on_auth_failed = _make_reauth_handler(settings, stop_requested, task_label)
    api = get_api()
    # 同 _run_user_status_like_task：把任务的取消回调绑到重试补丁上
    apply_pixiv_retry_patch(api, stop_requested=stop_requested)
    _ensure_storage_dirs(settings)

    db = Database(settings.storage.db_path)
    try:
        prepare_schema(db)
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
            check_status=lambda item_id: check_status(get_api(), item_id),
            upsert_status=upsert_status,
            item_label=total_label,
            item_name=lambda item_id: str(item_id),
            total_key=total_key,
            already_missing=already_missing,
            on_auth_failed=on_auth_failed,
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


def _login(settings: Any, stop_requested: StopRequested | None = None) -> Any:
    auth = PixivAuthManager(settings.pixiv)
    api, auth_result = auth.login(stop_requested=stop_requested)
    if auth_result.user_id is None:
        raise RuntimeError("Unable to determine user ID")
    if getattr(settings.pixiv, "user_id", None) is None:
        settings.pixiv.user_id = auth_result.user_id
    _persist_self_profile(settings, auth_result)
    # 统一给 api 打重试补丁：状态检查（user_detail）等不经过
    # BookmarkNovelSyncService 的调用点以前完全没有重试/退避保护，
    # 瞬时网络错误直接进 unknown 计数或空页路径。补丁幂等，service
    # 构造时会再打一次但不会叠加；这里不带取消回调，调用方随后重绑
    # （service 构造或 apply_pixiv_retry_patch(api, stop_requested=...)）。
    apply_pixiv_retry_patch(api)
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
            prepare_schema(db)
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
    on_auth_failed: Callable[[], bool] | None = None,
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
        if status == AUTH_FAILED_STATUS:
            # access token 过期是确定性错误：重试该次调用无意义。给调用方一次
            # 重登机会（成功则重查当前条目），否则立即中止——不再烧 15 个条目
            # 走「疑似限流」熔断（2026-11-20~23 生产事故的误报形态）。
            _report_log(reporter, "warning", f"[{checked_count + 1}/{total}] {item_label} {item_name(item)}: 认证失败（token 已失效）")
            reauthenticated = False
            if on_auth_failed is not None:
                try:
                    reauthenticated = bool(on_auth_failed())
                except InterruptedError:
                    raise
                except Exception as exc:  # pragma: no cover - 重登失败按中止处理
                    logger.warning("%s重新登录失败: %s", item_label, exc)
            if reauthenticated:
                status = check_status(item)
            if status == AUTH_FAILED_STATUS:
                message = (
                    f"Pixiv 认证失败（access token 已失效），已中止本轮{item_label}检查"
                    f"（已检查 {checked_count}/{total}，未检查的保持原状态）"
                )
                logger.warning(message)
                _report_log(reporter, "error", message)
                aborted_reason = "auth_failed"
                stopped = True
                break
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


def _list_users_for_status_check(db: Database) -> list[dict[str, Any]]:
    """状态检查专用的用户清单：按 last_checked_at 轮转，最久未检查的排最前。

    不能使用 ``list_users`` 的列表页排序，否则每轮顺序固定，
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

"""
Web application utility functions.

This module contains helper functions extracted from webapp.py for better modularity.
"""

from __future__ import annotations

import copy
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from ..settings import Settings
from ..storage_files import FileStorage

logger = logging.getLogger(__name__)


def _atomic_write_yaml(path: Path, data: Any) -> None:
    """Write YAML to ``path`` atomically (temp file in the same dir + os.replace).

    Avoids truncating/corrupting config.yaml if the process crashes mid-write, and
    keeps a single serialization style across every config writer.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as file:
        yaml.safe_dump(data, file, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)


def _oauth_task_public_payload(task: Any, mode: str) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "status": task.status,
        "message": task.message,
        "has_refresh_token": bool(task.refresh_token),
        "has_access_token": bool(task.access_token),
        "user_id": task.user_id,
        "mode": mode,
    }


def _settings_to_dict(settings: Settings) -> dict[str, Any]:
    return {
        "enabled": settings.sync.enabled,
        "initial_manual_only": settings.sync.initial_manual_only,
        "download_assets": settings.sync.download_assets,
        "write_markdown": settings.sync.write_markdown,
        "write_raw_text": settings.sync.write_raw_text,
        "bookmark_restricts": settings.sync.bookmark_restricts,
        "max_items_per_run": settings.sync.max_items_per_run,
        "max_pages_per_run": settings.sync.max_pages_per_run,
        "delay_seconds_between_items": settings.sync.delay_seconds_between_items,
        "delay_seconds_between_pages": settings.sync.delay_seconds_between_pages,
        "sync_bookmarks": settings.sync.sync_bookmarks,
        "sync_following_users": settings.sync.sync_following_users,
        "sync_following_novels": settings.sync.sync_following_novels,
        "sync_subscribed_series": settings.sync.sync_subscribed_series,
        "series_sync_limit": settings.sync.series_sync_limit,
        "delay_seconds_between_series": settings.sync.delay_seconds_between_series,
        "delay_seconds_between_chapters": settings.sync.delay_seconds_between_chapters,
        "delay_seconds_between_skips": settings.sync.delay_seconds_between_skips,
        "auto_sync_enabled": settings.sync.auto_sync_enabled,
        "auto_sync_timezone": settings.sync.auto_sync_timezone,
        "auto_sync_bookmarks_enabled": settings.sync.auto_sync_bookmarks_enabled,
        "auto_sync_bookmarks_interval_hours": settings.sync.auto_sync_bookmarks_interval_hours,
        "auto_sync_bookmarks_cron": settings.sync.auto_sync_bookmarks_cron,
        "auto_sync_following_list_enabled": settings.sync.auto_sync_following_list_enabled,
        "auto_sync_following_list_interval_hours": settings.sync.auto_sync_following_list_interval_hours,
        "auto_sync_following_list_cron": settings.sync.auto_sync_following_list_cron,
        "auto_sync_following_novels_enabled": settings.sync.auto_sync_following_novels_enabled,
        "auto_sync_following_novels_interval_hours": settings.sync.auto_sync_following_novels_interval_hours,
        "auto_sync_following_novels_cron": settings.sync.auto_sync_following_novels_cron,
        "auto_sync_following_novels_users_limit": settings.sync.auto_sync_following_novels_users_limit,
        "auto_sync_user_status_enabled": settings.sync.auto_sync_user_status_enabled,
        "auto_sync_user_status_interval_hours": settings.sync.auto_sync_user_status_interval_hours,
        "auto_sync_user_status_cron": settings.sync.auto_sync_user_status_cron,
        "auto_sync_novel_status_enabled": settings.sync.auto_sync_novel_status_enabled,
        "auto_sync_novel_status_interval_hours": settings.sync.auto_sync_novel_status_interval_hours,
        "auto_sync_novel_status_cron": settings.sync.auto_sync_novel_status_cron,
        "auto_sync_series_status_enabled": settings.sync.auto_sync_series_status_enabled,
        "auto_sync_series_status_interval_hours": settings.sync.auto_sync_series_status_interval_hours,
        "auto_sync_series_status_cron": settings.sync.auto_sync_series_status_cron,
        "auto_sync_subscribed_series_enabled": settings.sync.auto_sync_subscribed_series_enabled,
        "auto_sync_subscribed_series_interval_hours": settings.sync.auto_sync_subscribed_series_interval_hours,
        "auto_sync_subscribed_series_cron": settings.sync.auto_sync_subscribed_series_cron,
        "auto_sync_user_backup_enabled": settings.sync.auto_sync_user_backup_enabled,
        "auto_sync_user_backup_interval_hours": settings.sync.auto_sync_user_backup_interval_hours,
        "auto_sync_user_backup_cron": settings.sync.auto_sync_user_backup_cron,
        "auto_sync_pending_detection_enabled": settings.sync.auto_sync_pending_detection_enabled,
        "auto_sync_pending_detection_interval_hours": settings.sync.auto_sync_pending_detection_interval_hours,
        "auto_sync_pending_detection_cron": settings.sync.auto_sync_pending_detection_cron,
        "auto_sync_preference_analyze_enabled": settings.sync.auto_sync_preference_analyze_enabled,
        "auto_sync_preference_analyze_interval_hours": settings.sync.auto_sync_preference_analyze_interval_hours,
        "auto_sync_preference_analyze_cron": settings.sync.auto_sync_preference_analyze_cron,
        "preference_analyze_batch_size": settings.sync.preference_analyze_batch_size,
        "auto_sync_recommendation_run_enabled": settings.sync.auto_sync_recommendation_run_enabled,
        "auto_sync_recommendation_run_interval_hours": settings.sync.auto_sync_recommendation_run_interval_hours,
        "auto_sync_recommendation_run_cron": settings.sync.auto_sync_recommendation_run_cron,
        "pending_deletion_grace_period_days": settings.sync.pending_deletion_grace_period_days,
        "pending_deletion_cleanup_confirmed_days": settings.sync.pending_deletion_cleanup_confirmed_days,
    }


def _safe_snapshot(value: Any) -> Any:
    """对 worker 线程持续写入的 dict/list 取一份稳定快照。

    worker 无锁写入 stats/progress 时，深拷贝在迭代途中可能撞上
    "dictionary changed size during iteration"。写入是瞬时的，重试几次即可
    拿到稳定快照；仍失败则退回浅拷贝（顶层 key 至少稳定），最终退回原值。
    """
    for _ in range(5):
        try:
            return copy.deepcopy(value)
        except RuntimeError:
            continue
    try:
        return dict(value) if isinstance(value, dict) else list(value)
    except (RuntimeError, TypeError):
        return value


def _job_to_dict_unified(job: Any) -> dict[str, Any] | None:
    """6.9: 统一两套job序列化"""
    from ..jobs.models import JobSource, JobState

    if job is None:
        return None
    elapsed = None
    if job.started_at:
        end = job.finished_at or time.time()
        elapsed = round(end - job.started_at, 1)

    # 通用字段
    # stats/progress 由 worker 线程持续写入，Flask 请求线程在此读取序列化。
    # 用 deepcopy 取一次快照，避免 jsonify 迭代这两个 dict 时 worker 并发改动
    # 触发 "dictionary changed size during iteration"（stats 含嵌套 dict，浅拷贝
    # 不足以隔离子层）。
    result = {
        "job_id": job.job_id,
        "status": job.status.value if hasattr(job.status, "value") else job.status,
        "message": job.message,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "elapsed": elapsed,
        "stats": _safe_snapshot(job.stats),
        "error": job.error,
        "progress": _safe_snapshot(job.progress),
    }

    # JobState专用字段
    if isinstance(job, JobState):
        result["logs"] = [{"time": entry.time, "level": entry.level, "message": entry.message} for entry in job.logs]
        result["task_list"] = list(job.task_types)
        result["current_task_index"] = int(job.progress.get("current_task_index", 0) or 0)
        result["is_auto_sync"] = job.spec.source == JobSource.SCHEDULER
        result["source"] = job.spec.source.value
        result["job_type"] = job.spec.job_type.value
    # SyncJobState专用字段
    else:
        result["logs"] = job.logs
        result["task_list"] = job.task_list
        result["current_task_index"] = job.current_task_index
        result["is_auto_sync"] = job.is_auto_sync

    return result


def _shared_job_to_dict(job: Any) -> dict[str, Any] | None:
    """向后兼容wrapper"""
    return _job_to_dict_unified(job)


def _job_to_dict(job: Any) -> dict[str, Any] | None:
    """向后兼容wrapper"""
    return _job_to_dict_unified(job)


def _job_spec(
    task_list: list[str] | None,
    source: Any,
    params: dict[str, Any] | None = None,
) -> Any:
    from ..jobs.models import JobSource, JobSpec, JobType

    if not isinstance(source, JobSource):
        source = JobSource(source)
    tasks = list(task_list or [])
    job_params = dict(params or {})
    if len(tasks) == 1 and tasks[0].startswith("user_backup:"):
        user_id = int(tasks[0].split(":", 1)[1])
        job_params.setdefault("user_id", user_id)
        return JobSpec(
            source=source,
            job_type=JobType.USER_BACKUP,
            task_types=tasks,
            params=job_params,
        )
    if tasks == ["user_backup"]:
        return JobSpec(source=source, job_type=JobType.USER_BACKUP, task_types=tasks, params=job_params)
    if tasks == ["sync_check"]:
        return JobSpec(source=source, job_type=JobType.SYNC_CHECK, task_types=tasks, params=job_params)
    if tasks == ["pending_deletion_detection"]:
        return JobSpec(
            source=source,
            job_type=JobType.PENDING_DELETION_DETECTION,
            task_types=tasks,
            params=job_params,
        )
    if tasks == ["preference_analyze"]:
        return JobSpec(source=source, job_type=JobType.PREFERENCE_ANALYZE, task_types=tasks, params=job_params)
    if tasks == ["recommendation_run"]:
        return JobSpec(source=source, job_type=JobType.RECOMMENDATION_RUN, task_types=tasks, params=job_params)
    if len(tasks) == 1 and tasks[0] in {"user_status", "novel_status", "series_status"}:
        return JobSpec(source=source, job_type=JobType.STATUS_CHECK, task_types=tasks, params=job_params)
    return JobSpec(source=source, job_type=JobType.SYNC, task_types=tasks, params=job_params)


def _web_job_spec(task_list: list[str] | None, params: dict[str, Any] | None = None) -> Any:
    from ..jobs.models import JobSource

    return _job_spec(task_list, JobSource.WEB, params)


def _scheduler_job_spec(task_type: str, params: dict[str, Any] | None = None) -> Any:
    from ..jobs.models import JobSource

    normalized_task = {
        "bookmarks": "bookmark",
        "following_list": "following_users",
    }.get(task_type, task_type)
    return _job_spec([normalized_task], JobSource.SCHEDULER, params)


def _build_web_sync_job_spec(settings: Settings) -> Any:
    from ..jobs.tasks import build_default_task_list

    return _web_job_spec(build_default_task_list(settings))


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式错误：{path}")
    return data


def _safe_int(value: Any, default: int) -> int:
    """安全解析整数参数，无效值返回 default。"""
    try:
        return int(value) if value not in (None, "") else default
    except (ValueError, TypeError):
        return default


def _normalize_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    result = int(value)
    if result <= 0:
        raise ValueError("整数值必须大于 0")
    return result


def _normalize_int(value: Any, default: int) -> int:
    """容错地把任意输入转成整数；空串/None/非法输入返回 default。"""
    if value in (None, ""):
        return int(default)
    try:
        return int(value)
    except (ValueError, TypeError):
        return int(default)


def _normalize_float(value: Any, min_value: float = 0.0) -> float:
    if value in (None, ""):
        return float(min_value)
    result = float(value)
    if result < min_value:
        raise ValueError(f"数值不能小于 {min_value}")
    return result


def _restricts_to_label(restricts: list[str]) -> str:
    mapping = {"public": "公开收藏", "private": "私密收藏"}
    labels = [mapping[item] for item in restricts if item in mapping]
    return " / ".join(labels) if labels else "无"


def _external_base_url(req) -> str:
    # L3: 显式配置的外部地址优先，彻底避免依赖客户端可控的 Host / base_url
    # 构造 OAuth 回调（否则投毒 Host 头可劫持回调、泄露 code）。
    explicit = os.environ.get("PIXIV_EXTERNAL_BASE_URL", "").strip()
    if explicit:
        parsed = urlparse(explicit if "://" in explicit else f"https://{explicit}")
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        logger.warning("Invalid PIXIV_EXTERNAL_BASE_URL: %s (ignored)", explicit)

    forwarded_proto = req.headers.get("X-Forwarded-Proto")
    forwarded_host = req.headers.get("X-Forwarded-Host")
    if forwarded_proto and forwarded_host:
        # 仅信任白名单中的 forwarded host，防止 OAuth 回调被劫持
        trusted_hosts = os.environ.get("TRUSTED_FORWARDED_HOSTS", "")
        if trusted_hosts:
            allowed = {h.strip().lower() for h in trusted_hosts.split(",") if h.strip()}
            if forwarded_host.lower() in allowed:
                return f"{forwarded_proto}://{forwarded_host}"
            logger.warning("Untrusted X-Forwarded-Host: %s (allowed: %s)", forwarded_host, allowed)

    parsed = urlparse(req.base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


# pixivpy 被限流时不抛异常，而是返回形如
# {"error": {"user_message": "", "message": "Rate Limit", "reason": ""}} 的 JsonDict
# （dict 子类），里面没有 novel/user 等目标键。历史事故：这种响应被当成「已删除」，
# 一轮任务把 6971 篇小说中的 5499 篇误判为 deleted。
#
# 因此判定原则是 fail-safe：**宁可漏判删除，也绝不误判删除**——只有错误文本明确命中
# 下面的「不存在/已删除」关键词才判定为删除，其余一切（限流、429、鉴权失效、空响应、
# 结构异常）统统返回 unknown，交由上层熔断并保留数据库里的原状态。
#
# 注意：文案随请求的 lang 变化（生产实例带 lang=zh 时返回中文），所以中/日/英三套
# 关键词必须并存。其中中文「尚无此页」相当泛，无法排除限流也复用该文案的可能，
# 因此 jobs/services.py 里另有「连续大量 deleted」熔断作为兜底。
_MISSING_ERROR_KEYWORDS: tuple[str, ...] = (
    # 中文（lang=zh，生产实测）：
    #   novel_detail/user_detail 不存在 → 「尚无此页」
    #   novel_series 不存在 → 「抱歉，您所指定的系列已经从个人信息删除，或者不存在。」
    "尚无此页",
    "已经从个人信息删除",
    "不存在",  # 同时覆盖「或者不存在」
    "已删除",
    # 日文：「該当作品は削除されたか、存在しない作品IDです。」
    "削除",
    "存在しない",
    "存在しません",
    # 英文接口偶尔返回的等价文案
    "deleted",
    "does not exist",
    "doesn't exist",
    "no longer exists",
    "not found",
)

_VERDICT_OK = "ok"  # 拿到目标数据
_VERDICT_MISSING = "missing"  # Pixiv 明确回复「不存在/已删除」
_VERDICT_UNKNOWN = "unknown"  # 其余一切情况，不可据此改写状态

# 限流/风控的错误特征。必须与 sync_engine._RATE_LIMIT_ERROR_TOKENS 保持一致
# （tests/test_status_check_classification.py 里有防漂移断言），否则同一个响应
# 会在两处得到相反结论：例如 {"user_message": "尚无此页", "message": "Rate Limit"}
# 在 sync_engine 里被排除为临时故障，在这里却被判成「已删除」。
_RATE_LIMIT_ERROR_TOKENS = ("rate limit", "rate-limit", "ratelimit", "too many requests", "429")


def _response_field(result: Any, key: str) -> Any:
    """读取 pixivpy 响应字段，兼容 dict 与属性两种访问方式。"""
    if isinstance(result, dict):
        # JsonDict 既是 dict 又支持属性访问，dict 取值已经覆盖；这里不再 getattr，
        # 免得把 dict 自带的方法（items/keys 等）当成响应字段
        return result.get(key)
    try:
        return getattr(result, key, None)
    except Exception:  # pragma: no cover - 异常属性访问的兜底
        return None


def _pixiv_error_text(result: Any) -> str:
    """拼接响应中的错误文案（user_message / message / reason），供关键词判定使用。"""
    error = _response_field(result, "error")
    if error is None:
        return ""
    if isinstance(error, str):
        return error
    parts: list[str] = []
    for field in ("user_message", "message", "reason"):
        value = _response_field(error, field)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    if parts:
        return " ".join(parts)
    return str(error)


def _is_missing_error(error_text: str) -> bool:
    """错误文案是否明确表示「作品/用户不存在或已被删除」。

    限流特征优先：只要文本里出现 rate limit / 429 等，无论是否同时命中「尚无此页」
    这类泛化文案，一律不判删除（返回 False → 上层落到 unknown）。
    """
    lowered = error_text.lower()
    if any(token in lowered for token in _RATE_LIMIT_ERROR_TOKENS):
        return False
    return any(keyword in lowered for keyword in _MISSING_ERROR_KEYWORDS)


def _classify_pixiv_response(result: Any, data_keys: tuple[str, ...]) -> tuple[str, Any]:
    """判定 pixivpy 响应属于哪一类，返回 (判定, 目标数据)。

    - 命中 ``data_keys`` 之一 → (``ok``, 该数据)
    - 错误文案明确表示不存在 → (``missing``, None)
    - 其余（含 result 为 None）→ (``unknown``, None)
    """
    if result is None:
        return _VERDICT_UNKNOWN, None
    for key in data_keys:
        value = _response_field(result, key)
        if value is not None:
            return _VERDICT_OK, value
    if _is_missing_error(_pixiv_error_text(result)):
        return _VERDICT_MISSING, None
    return _VERDICT_UNKNOWN, None


def _log_unknown_status(label: str, item_id: int, result: Any) -> None:
    detail = _pixiv_error_text(result) or "响应为空或缺少预期字段"
    logger.warning("%s %s 状态无法判定，保持原状态: %s", label, item_id, detail[:200])


def _check_pixiv_user_status(api: Any, user_id: int) -> str:
    """检查 Pixiv 用户状态：normal/no_novels/suspended/unknown。

    只有 Pixiv 明确回复「用户不存在/已删除」才判 suspended，限流等一律 unknown。
    """
    try:
        result = api.user_detail(user_id)
        verdict, _user = _classify_pixiv_response(result, ("user",))
        if verdict == _VERDICT_MISSING:
            return "suspended"
        if verdict != _VERDICT_OK:
            _log_unknown_status("用户", user_id, result)
            return "unknown"
        profile = _response_field(result, "profile")
        if profile:
            try:
                total_novels = int(_response_field(profile, "total_novels") or 0)
            except (TypeError, ValueError):
                total_novels = 0
            if total_novels == 0:
                return "no_novels"
        return "normal"
    except Exception as e:
        logger.warning("检查用户 %s 状态失败: %s", user_id, e)
        return "unknown"


def _check_novel_status(api: Any, novel_id: int) -> str:
    """检查小说状态：normal/restricted/deleted/unknown。

    只有 Pixiv 明确回复「作品不存在/已删除」才判 deleted，限流等一律 unknown。
    """
    try:
        result = api.novel_detail(novel_id)
        verdict, novel = _classify_pixiv_response(result, ("novel",))
        if verdict == _VERDICT_MISSING:
            return "deleted"
        if verdict != _VERDICT_OK:
            _log_unknown_status("小说", novel_id, result)
            return "unknown"
        visible = _response_field(novel, "visible")
        if visible is None:
            visible = True
        if not visible:
            return "restricted"
        return "normal"
    except Exception as e:
        logger.warning("检查小说 %s 状态失败: %s", novel_id, e)
        return "unknown"


def _check_series_status(api: Any, series_id: int) -> str:
    """检查系列状态：normal/deleted/unknown。

    只有 Pixiv 明确回复「系列不存在/已删除」才判 deleted，限流等一律 unknown。
    """
    try:
        result = api.novel_series(series_id)
        verdict, _detail = _classify_pixiv_response(result, ("novel_series_detail",))
        if verdict == _VERDICT_MISSING:
            return "deleted"
        if verdict != _VERDICT_OK:
            _log_unknown_status("系列", series_id, result)
            return "unknown"
        return "normal"
    except Exception as e:
        logger.warning("检查系列 %s 状态失败: %s", series_id, e)
        return "unknown"


def _remove_archive_files(settings: Settings, archive_refs: list[dict[str, Any]]) -> dict[str, int]:
    """Remove local archive files for DB rows that are about to be deleted."""
    storage = FileStorage(settings)
    novel_dirs: list[Path] = []
    asset_paths: list[Path] = []
    for ref in archive_refs:
        try:
            novel_id = int(ref.get("novel_id") or 0)
            user_id = int(ref.get("user_id") or 0)
        except (TypeError, ValueError):
            continue
        if not novel_id:
            continue
        novel_dirs.append(
            storage.novel_dir(
                str(ref.get("restrict_value") or "public"),
                user_id,
                str(ref.get("author_name") or "unknown"),
                novel_id,
                str(ref.get("title") or f"novel_{novel_id}"),
            )
        )
        for path in ref.get("asset_paths") or []:
            if path:
                asset_path = Path(path)
                asset_paths.append(asset_path)
                # pathlib.Path.parent 永不抛 IndexError——到根返回自身。
                # 原 try/except IndexError 是死代码；直接计算即可。
                if asset_path.parent.parent.name == "assets":
                    novel_dirs.append(asset_path.parent.parent.parent)
    return storage.remove_novel_archive(novel_dirs, asset_paths)

"""
Web application utility functions.

This module contains helper functions extracted from webapp.py for better modularity.
"""

from __future__ import annotations

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
    }


def _job_to_dict_unified(job: Any) -> dict[str, Any] | None:
    """6.9: 统一两套job序列化"""
    from ..jobs.models import JobState

    if job is None:
        return None
    elapsed = None
    if job.started_at:
        end = job.finished_at or time.time()
        elapsed = round(end - job.started_at, 1)

    # 通用字段
    result = {
        "job_id": job.job_id,
        "status": job.status.value if hasattr(job.status, "value") else job.status,
        "message": job.message,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "elapsed": elapsed,
        "stats": job.stats,
        "error": job.error,
        "progress": job.progress,
    }

    # JobState专用字段
    if isinstance(job, JobState):
        result["logs"] = [{"time": entry.time, "level": entry.level, "message": entry.message} for entry in job.logs]
        result["task_list"] = list(job.task_types)
        result["current_task_index"] = int(job.progress.get("current_task_index", 0) or 0)
        result["is_auto_sync"] = job.spec.source.value == "scheduler"
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


def _web_job_spec(task_list: list[str] | None, params: dict[str, Any] | None = None) -> Any:
    from ..jobs.models import JobSource, JobSpec, JobType

    tasks = list(task_list or [])
    job_params = dict(params or {})
    if len(tasks) == 1 and tasks[0].startswith("user_backup:"):
        user_id = int(tasks[0].split(":", 1)[1])
        job_params.setdefault("user_id", user_id)
        return JobSpec(
            source=JobSource.WEB,
            job_type=JobType.USER_BACKUP,
            task_types=tasks,
            params=job_params,
        )
    if tasks == ["sync_check"]:
        return JobSpec(source=JobSource.WEB, job_type=JobType.SYNC_CHECK, task_types=tasks, params=job_params)
    if tasks == ["pending_deletion_detection"]:
        return JobSpec(
            source=JobSource.WEB,
            job_type=JobType.PENDING_DELETION_DETECTION,
            task_types=tasks,
            params=job_params,
        )
    if len(tasks) == 1 and tasks[0] in {"user_status", "novel_status", "series_status"}:
        return JobSpec(source=JobSource.WEB, job_type=JobType.STATUS_CHECK, task_types=tasks, params=job_params)
    return JobSpec(source=JobSource.WEB, job_type=JobType.SYNC, task_types=tasks, params=job_params)


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


def _check_pixiv_user_status(api: Any, user_id: int) -> str:
    """检查 Pixiv 用户状态"""
    try:
        result = api.user_detail(user_id)
        if result is None:
            return "suspended"
        user = getattr(result, "user", None)
        if user is None:
            return "suspended"
        profile = getattr(result, "profile", None)
        if profile:
            total_novels = getattr(profile, "total_novels", 0) or 0
            if total_novels == 0:
                return "no_novels"
        return "normal"
    except Exception as e:
        logger.warning("Failed to check user %s status: %s", user_id, e)
        return "unknown"


def _check_novel_status(api: Any, novel_id: int) -> str:
    """检查小说状态：normal/deleted/restricted"""
    try:
        result = api.novel_detail(novel_id)
        if result is None:
            return "deleted"
        novel = getattr(result, "novel", None)
        if novel is None:
            if isinstance(result, dict):
                novel = result.get("novel")
            if novel is None:
                return "deleted"
        visible = getattr(novel, "visible", True)
        if isinstance(novel, dict):
            visible = novel.get("visible", True)
        if not visible:
            return "restricted"
        return "normal"
    except Exception as e:
        logger.warning("Failed to check novel %s status: %s", novel_id, e)
        return "unknown"


def _check_series_status(api: Any, series_id: int) -> str:
    """检查系列状态：normal/deleted"""
    try:
        result = api.novel_series(series_id)
        if result is None:
            return "deleted"
        detail = None
        if isinstance(result, dict):
            detail = result.get("novel_series_detail")
        if detail is None:
            detail = getattr(result, "novel_series_detail", None)
        if detail is None:
            return "deleted"
        return "normal"
    except Exception as e:
        logger.warning("Failed to check series %s status: %s", series_id, e)
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
                try:
                    if asset_path.parent.parent.name == "assets":
                        novel_dirs.append(asset_path.parent.parent.parent)
                except IndexError:
                    pass
    return storage.remove_novel_archive(novel_dirs, asset_paths)

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os
import re

import yaml
from dotenv import load_dotenv


@dataclass(slots=True)
class PixivSettings:
    refresh_token: str
    access_token: str | None
    proxy: str | None
    timeout: int
    verify_ssl: bool
    user_id: int | None
    web_cookie: str | None = None


@dataclass(slots=True)
class SyncSettings:
    enabled: bool
    initial_manual_only: bool
    download_assets: bool
    write_markdown: bool
    write_raw_text: bool
    bookmark_restricts: list[str]
    max_items_per_run: int | None
    max_pages_per_run: int | None
    delay_seconds_between_items: float
    delay_seconds_between_pages: float
    sync_bookmarks: bool = True
    sync_following_series: bool = True
    sync_following_users: bool = True
    sync_following_novels: bool = True
    sync_subscribed_series: bool = True
    series_sync_limit: int = 0  # 0=全部，>0=限制数量
    delay_seconds_between_series: float = 3.0  # 每个系列之间的间隔
    delay_seconds_between_chapters: float = 1.0  # 系列下每章节间隔
    delay_seconds_between_skips: float = 0.1  # 跳过内容时的间隔
    # 定时任务配置
    auto_sync_enabled: bool = False
    auto_sync_bookmarks_enabled: bool = True
    auto_sync_bookmarks_interval_hours: int = 6  # 收藏同步间隔（小时）
    auto_sync_bookmarks_cron: str = ""  # 收藏同步cron表达式，优先于interval_hours
    auto_sync_following_enabled: bool = True
    auto_sync_following_interval_hours: int = 6  # 关注用户间隔（小时）
    auto_sync_following_cron: str = ""  # 关注用户cron表达式，优先于interval_hours
    auto_sync_user_status_enabled: bool = True  # 关注用户的用户状态更新
    auto_sync_user_status_cron: str = ""  # 用户状态检查cron表达式
    auto_sync_subscribed_series_enabled: bool = True  # 我的追更系列
    auto_sync_subscribed_series_interval_hours: int = 6  # 追更系列间隔（小时）
    auto_sync_subscribed_series_cron: str = ""  # 追更系列cron表达式，优先于interval_hours


@dataclass(slots=True)
class StorageSettings:
    public_dir: Path
    private_dir: Path
    db_path: Path


@dataclass(slots=True)
class Settings:
    pixiv: PixivSettings
    sync: SyncSettings
    storage: StorageSettings
    log_level: str = "INFO"


DEFAULT_CONFIG_PATH = Path("config/config.yaml")


def load_settings(config_path: str | Path | None = None, env_path: str | Path | None = None) -> Settings:
    if env_path is not None:
        load_dotenv(env_path)
    else:
        load_dotenv()

    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    raw_config = _load_yaml(path) if path.exists() else {}

    pixiv_raw = raw_config.get("pixiv", {})
    sync_raw = raw_config.get("sync", {})
    storage_raw = raw_config.get("storage", {})

    refresh_token = os.getenv("PIXIV_REFRESH_TOKEN", "").strip()
    access_token = os.getenv("PIXIV_ACCESS_TOKEN", "").strip() or None
    proxy = os.getenv("PIXIV_PROXY") or pixiv_raw.get("proxy")
    timeout = int(os.getenv("PIXIV_TIMEOUT", pixiv_raw.get("timeout", 30)))
    verify_ssl = _parse_bool(os.getenv("PIXIV_VERIFY_SSL"), default=pixiv_raw.get("verify_ssl", True))
    user_id = _parse_optional_int(os.getenv("PIXIV_USER_ID"))
    web_cookie = os.getenv("PIXIV_WEB_COOKIE") or pixiv_raw.get("web_cookie")

    public_dir = Path(os.getenv("PIXIV_PUBLIC_DIR", storage_raw.get("public_dir", "./data/library/public")))
    private_dir = Path(os.getenv("PIXIV_PRIVATE_DIR", storage_raw.get("private_dir", "./data/library/private")))
    db_path = Path(os.getenv("PIXIV_DB_PATH", storage_raw.get("db_path", "./data/state/pixiv_sync.db")))
    log_level = os.getenv("PIXIV_LOG_LEVEL", "INFO").upper()

    return Settings(
        pixiv=PixivSettings(
            refresh_token=refresh_token,
            access_token=access_token,
            proxy=proxy,
            timeout=timeout,
            verify_ssl=verify_ssl,
            user_id=user_id,
            web_cookie=web_cookie,
        ),
        sync=SyncSettings(
            enabled=bool(sync_raw.get("enabled", False)),
            initial_manual_only=bool(sync_raw.get("initial_manual_only", True)),
            download_assets=bool(sync_raw.get("download_assets", True)),
            write_markdown=bool(sync_raw.get("write_markdown", True)),
            write_raw_text=bool(sync_raw.get("write_raw_text", True)),
            bookmark_restricts=list(sync_raw.get("bookmark_restricts", ["public", "private"])),
            max_items_per_run=_coerce_optional_int(sync_raw.get("max_items_per_run")),
            max_pages_per_run=_coerce_optional_int(sync_raw.get("max_pages_per_run")),
            delay_seconds_between_items=_coerce_float(sync_raw.get("delay_seconds_between_items"), 0.0),
            delay_seconds_between_pages=_coerce_float(sync_raw.get("delay_seconds_between_pages"), 0.0),
            sync_bookmarks=bool(sync_raw.get("sync_bookmarks", True)),
            sync_following_series=bool(sync_raw.get("sync_following_series", True)),
            sync_following_users=bool(sync_raw.get("sync_following_users", True)),
            sync_following_novels=bool(sync_raw.get("sync_following_novels", True)),
            series_sync_limit=int(sync_raw.get("series_sync_limit", 0)),
            delay_seconds_between_series=_coerce_float(sync_raw.get("delay_seconds_between_series"), 3.0),
            delay_seconds_between_chapters=_coerce_float(sync_raw.get("delay_seconds_between_chapters"), 1.0),
            delay_seconds_between_skips=_coerce_float(sync_raw.get("delay_seconds_between_skips"), 0.1),
            # 定时任务配置
            auto_sync_enabled=bool(sync_raw.get("auto_sync_enabled", False)),
            auto_sync_bookmarks_enabled=bool(sync_raw.get("auto_sync_bookmarks_enabled", True)),
            auto_sync_bookmarks_interval_hours=int(sync_raw.get("auto_sync_bookmarks_interval_hours", 6)),
            auto_sync_bookmarks_cron=str(sync_raw.get("auto_sync_bookmarks_cron", "")),
            auto_sync_following_enabled=bool(sync_raw.get("auto_sync_following_enabled", True)),
            auto_sync_following_interval_hours=int(sync_raw.get("auto_sync_following_interval_hours", 6)),
            auto_sync_following_cron=str(sync_raw.get("auto_sync_following_cron", "")),
            auto_sync_user_status_enabled=bool(sync_raw.get("auto_sync_user_status_enabled", True)),
            auto_sync_user_status_cron=str(sync_raw.get("auto_sync_user_status_cron", "")),
            auto_sync_subscribed_series_enabled=bool(sync_raw.get("auto_sync_subscribed_series_enabled", True)),
            auto_sync_subscribed_series_interval_hours=int(sync_raw.get("auto_sync_subscribed_series_interval_hours", 6)),
            auto_sync_subscribed_series_cron=str(sync_raw.get("auto_sync_subscribed_series_cron", "")),
        ),
        storage=StorageSettings(
            public_dir=public_dir,
            private_dir=private_dir,
            db_path=db_path,
        ),
        log_level=log_level,
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a mapping: {path}")
    return data


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return bool(default)
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _parse_optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value)



def _coerce_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None



def _coerce_float(value: Any, default: float) -> float:
    if value in (None, ""):
        return float(default)
    return float(value)


def parse_cron_expression(cron_expr: str) -> dict[str, Any] | None:
    """解析cron表达式，返回解析结果
    
    支持的格式：
    - 标准5位: 分 时 日 月 周
    - 简化格式: @hourly, @daily, @weekly, @monthly
    - 带秒的6位: 秒 分 时 日 月 周
    
    返回:
        dict: 包含解析后的cron字段，或None如果解析失败
    """
    if not cron_expr or not cron_expr.strip():
        return None
    
    cron_expr = cron_expr.strip()
    
    # 处理简化格式
    shortcuts = {
        "@hourly": "0 * * * *",
        "@daily": "0 0 * * *",
        "@weekly": "0 0 * * 0",
        "@monthly": "0 0 1 * *",
    }
    if cron_expr.lower() in shortcuts:
        cron_expr = shortcuts[cron_expr.lower()]
    
    # 分割cron表达式
    parts = cron_expr.split()
    
    if len(parts) == 5:
        # 标准5位: 分 时 日 月 周
        minute, hour, day, month, weekday = parts
        return {
            "minute": minute,
            "hour": hour,
            "day": day,
            "month": month,
            "weekday": weekday,
            "second": "0",
        }
    elif len(parts) == 6:
        # 6位: 秒 分 时 日 月 周
        second, minute, hour, day, month, weekday = parts
        return {
            "second": second,
            "minute": minute,
            "hour": hour,
            "day": day,
            "month": month,
            "weekday": weekday,
        }
    else:
        return None


def cron_to_next_run(cron_expr: str, base_time: float | None = None) -> float | None:
    """计算cron表达式的下次运行时间
    
    Args:
        cron_expr: cron表达式
        base_time: 基础时间戳，默认为当前时间
        
    Returns:
        float: 下次运行的时间戳，或None如果解析失败
    """
    import time
    from datetime import datetime, timedelta
    
    parsed = parse_cron_expression(cron_expr)
    if not parsed:
        return None
    
    if base_time is None:
        base_time = time.time()
    
    base_dt = datetime.fromtimestamp(base_time)
    
    # 简单实现：查找下一个匹配的时间
    # 这里使用简化的实现，实际项目中建议使用croniter库
    try:
        # 尝试导入croniter
        from croniter import croniter
        cron = croniter(cron_expr, base_dt)
        next_dt = cron.get_next(datetime)
        return next_dt.timestamp()
    except ImportError:
        # 如果没有croniter，使用简化的实现
        return _simple_cron_next_run(parsed, base_dt)


def _simple_cron_next_run(parsed: dict[str, Any], base_dt: datetime) -> float | None:
    """简化的cron下次运行时间计算
    
    注意：这是一个简化的实现，只支持基本的cron表达式
    对于复杂的cron表达式，建议安装croniter库
    """
    from datetime import datetime, timedelta
    
    # 解析各个字段
    def parse_field(field: str, min_val: int, max_val: int) -> list[int]:
        if field == "*":
            return list(range(min_val, max_val + 1))
        elif "," in field:
            return [int(x) for x in field.split(",")]
        elif "-" in field:
            start, end = field.split("-")
            return list(range(int(start), int(end) + 1))
        elif "/" in field:
            base, step = field.split("/")
            if base == "*":
                base_val = min_val
            else:
                base_val = int(base)
            return list(range(base_val, max_val + 1, int(step)))
        else:
            return [int(field)]
    
    try:
        minutes = parse_field(parsed["minute"], 0, 59)
        hours = parse_field(parsed["hour"], 0, 23)
        days = parse_field(parsed["day"], 1, 31)
        months = parse_field(parsed["month"], 1, 12)
        weekdays = parse_field(parsed["weekday"], 0, 6)
    except (ValueError, IndexError):
        return None
    
    # 查找下一个匹配的时间
    current = base_dt + timedelta(minutes=1)
    current = current.replace(second=0, microsecond=0)
    
    for _ in range(366 * 24 * 60):  # 最多检查一年
        if (current.minute in minutes and
            current.hour in hours and
            current.day in days and
            current.month in months and
            current.weekday() in weekdays):
            return current.timestamp()
        current += timedelta(minutes=1)
    
    return None

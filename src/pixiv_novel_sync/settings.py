from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import logging
import os

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PixivSettings:
    refresh_token: str
    access_token: str | None
    proxy: str | None
    timeout: int
    verify_ssl: bool
    user_id: int | None
    web_cookie: str | None = None
    username: str | None = None
    password: str | None = None


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
    sync_following_users: bool = True
    sync_following_novels: bool = True
    sync_subscribed_series: bool = True
    series_sync_limit: int = 0  # 0=全部，>0=限制数量
    delay_seconds_between_series: float = 3.0  # 每个系列之间的间隔
    delay_seconds_between_chapters: float = 1.0  # 系列下每章节间隔
    delay_seconds_between_skips: float = 0.1  # 跳过内容时的间隔
    # 定时任务配置
    auto_sync_enabled: bool = False
    auto_sync_timezone: str = "UTC"  # 时区，如 "Asia/Seoul", "UTC", "Asia/Shanghai"
    auto_sync_bookmarks_enabled: bool = True
    auto_sync_bookmarks_interval_hours: int = 6  # 收藏同步间隔（小时）
    auto_sync_bookmarks_cron: str = ""  # 收藏同步cron表达式，优先于interval_hours
    auto_sync_following_list_enabled: bool = True  # 同步关注用户列表
    auto_sync_following_list_interval_hours: int = 24  # 关注用户列表同步间隔（小时）
    auto_sync_following_list_cron: str = ""  # 关注用户列表cron表达式
    auto_sync_following_novels_enabled: bool = True  # 同步关注用户小说
    auto_sync_following_novels_interval_hours: int = 6  # 关注用户小说同步间隔（小时）
    auto_sync_following_novels_cron: str = ""  # 关注用户小说cron表达式
    auto_sync_following_novels_users_limit: int = 0  # 每轮同步的用户数限制，0=全部
    auto_sync_user_status_enabled: bool = True  # 关注用户的用户状态更新
    auto_sync_user_status_interval_hours: int = 6  # 用户状态检查间隔（小时）
    auto_sync_user_status_cron: str = ""  # 用户状态检查cron表达式
    auto_sync_novel_status_enabled: bool = True  # 小说状态检查
    auto_sync_novel_status_interval_hours: int = 6  # 小说状态检查间隔（小时）
    auto_sync_novel_status_cron: str = ""  # 小说状态检查cron表达式
    auto_sync_series_status_enabled: bool = True  # 系列状态检查
    auto_sync_series_status_interval_hours: int = 6  # 系列状态检查间隔（小时）
    auto_sync_series_status_cron: str = ""  # 系列状态检查cron表达式
    auto_sync_subscribed_series_enabled: bool = True  # 我的追更系列
    auto_sync_subscribed_series_interval_hours: int = 6  # 追更系列间隔（小时）
    auto_sync_subscribed_series_cron: str = ""  # 追更系列cron表达式，优先于interval_hours
    auto_sync_user_backup_enabled: bool = False  # 定时全量备份关注用户小说
    auto_sync_user_backup_interval_hours: int = 24  # 全量备份间隔（小时）
    auto_sync_user_backup_cron: str = ""  # 全量备份cron表达式
    auto_sync_pending_detection_enabled: bool = True  # 检测取消收藏/追更
    auto_sync_pending_detection_interval_hours: int = 12  # 检测间隔（小时）
    auto_sync_pending_detection_cron: str = ""  # 检测cron表达式
    # 增量偏好分析: 少量多次分析本地归档,跳过已分析,新增自动续
    auto_sync_preference_analyze_enabled: bool = False  # 自动增量分析本地偏好
    auto_sync_preference_analyze_interval_hours: int = 1  # 分析间隔（小时）
    auto_sync_preference_analyze_cron: str = "*/30 * * * *"  # 分析cron表达式,默认每30分钟
    preference_analyze_batch_size: int = 200  # 每批分析小说数量
    # Phase 3.2: pending_deletions表清理配置
    pending_deletion_grace_period_days: int = 30  # pending状态保留天数,给用户充足恢复时间
    pending_deletion_cleanup_confirmed_days: int = 7  # 已确认记录清理天数


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
    dashboard_token: str | None = None


DEFAULT_CONFIG_PATH = Path("config/config.yaml")
ALLOWED_BOOKMARK_RESTRICTS = {"public", "private"}


def load_settings(config_path: str | Path | None = None, env_path: str | Path | None = None) -> Settings:
    if env_path is not None:
        os.environ["ENV_PATH"] = str(env_path)
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
    try:
        timeout = int(os.getenv("PIXIV_TIMEOUT", pixiv_raw.get("timeout", 30)))
    except (ValueError, TypeError):
        timeout = 30
    verify_ssl = _parse_bool(os.getenv("PIXIV_VERIFY_SSL"), default=pixiv_raw.get("verify_ssl", True))
    user_id = _parse_optional_int(os.getenv("PIXIV_USER_ID"))
    web_cookie = os.getenv("PIXIV_WEB_COOKIE") or pixiv_raw.get("web_cookie")
    username = os.getenv("PIXIV_USERNAME") or pixiv_raw.get("username")
    password = os.getenv("PIXIV_PASSWORD") or pixiv_raw.get("password")

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
            username=username,
            password=password,
        ),
        sync=SyncSettings(
            enabled=_coerce_bool(sync_raw.get("enabled"), False),
            initial_manual_only=_coerce_bool(sync_raw.get("initial_manual_only"), True),
            download_assets=_coerce_bool(sync_raw.get("download_assets"), True),
            write_markdown=_coerce_bool(sync_raw.get("write_markdown"), True),
            write_raw_text=_coerce_bool(sync_raw.get("write_raw_text"), True),
            bookmark_restricts=_coerce_bookmark_restricts(sync_raw.get("bookmark_restricts")),
            max_items_per_run=_coerce_optional_int(sync_raw.get("max_items_per_run")),
            max_pages_per_run=_coerce_optional_int(sync_raw.get("max_pages_per_run")),
            delay_seconds_between_items=_coerce_float(sync_raw.get("delay_seconds_between_items"), 0.0),
            delay_seconds_between_pages=_coerce_float(sync_raw.get("delay_seconds_between_pages"), 0.0),
            sync_bookmarks=_coerce_bool(sync_raw.get("sync_bookmarks"), True),
            sync_following_users=_coerce_bool(sync_raw.get("sync_following_users"), True),
            sync_following_novels=_coerce_bool(sync_raw.get("sync_following_novels"), True),
            sync_subscribed_series=_coerce_bool(sync_raw.get("sync_subscribed_series"), True),
            series_sync_limit=max(_coerce_int(sync_raw.get("series_sync_limit"), 0), 0),
            delay_seconds_between_series=_coerce_float(sync_raw.get("delay_seconds_between_series"), 3.0),
            delay_seconds_between_chapters=_coerce_float(sync_raw.get("delay_seconds_between_chapters"), 1.0),
            delay_seconds_between_skips=_coerce_float(sync_raw.get("delay_seconds_between_skips"), 0.1),
            # 定时任务配置
            auto_sync_enabled=_coerce_bool(sync_raw.get("auto_sync_enabled"), False),
            auto_sync_timezone=str(sync_raw.get("auto_sync_timezone", "UTC")),
            auto_sync_bookmarks_enabled=_coerce_bool(sync_raw.get("auto_sync_bookmarks_enabled"), True),
            auto_sync_bookmarks_interval_hours=_coerce_positive_int(sync_raw.get("auto_sync_bookmarks_interval_hours"), 6),
            auto_sync_bookmarks_cron=str(sync_raw.get("auto_sync_bookmarks_cron", "")),
            auto_sync_following_list_enabled=_coerce_bool(sync_raw.get("auto_sync_following_list_enabled"), True),
            auto_sync_following_list_interval_hours=_coerce_positive_int(sync_raw.get("auto_sync_following_list_interval_hours"), 24),
            auto_sync_following_list_cron=str(sync_raw.get("auto_sync_following_list_cron", "")),
            auto_sync_following_novels_enabled=_coerce_bool(sync_raw.get("auto_sync_following_novels_enabled"), True),
            auto_sync_following_novels_interval_hours=_coerce_positive_int(sync_raw.get("auto_sync_following_novels_interval_hours"), 6),
            auto_sync_following_novels_cron=str(sync_raw.get("auto_sync_following_novels_cron", "")),
            auto_sync_following_novels_users_limit=max(_coerce_int(sync_raw.get("auto_sync_following_novels_users_limit"), 0), 0),
            auto_sync_user_status_enabled=_coerce_bool(sync_raw.get("auto_sync_user_status_enabled"), True),
            auto_sync_user_status_interval_hours=_coerce_positive_int(sync_raw.get("auto_sync_user_status_interval_hours"), 6),
            auto_sync_user_status_cron=str(sync_raw.get("auto_sync_user_status_cron", "")),
            auto_sync_novel_status_enabled=_coerce_bool(sync_raw.get("auto_sync_novel_status_enabled"), True),
            auto_sync_novel_status_interval_hours=_coerce_positive_int(sync_raw.get("auto_sync_novel_status_interval_hours"), 6),
            auto_sync_novel_status_cron=str(sync_raw.get("auto_sync_novel_status_cron", "")),
            auto_sync_series_status_enabled=_coerce_bool(sync_raw.get("auto_sync_series_status_enabled"), True),
            auto_sync_series_status_interval_hours=_coerce_positive_int(sync_raw.get("auto_sync_series_status_interval_hours"), 6),
            auto_sync_series_status_cron=str(sync_raw.get("auto_sync_series_status_cron", "")),
            auto_sync_subscribed_series_enabled=_coerce_bool(sync_raw.get("auto_sync_subscribed_series_enabled"), True),
            auto_sync_subscribed_series_interval_hours=_coerce_positive_int(sync_raw.get("auto_sync_subscribed_series_interval_hours"), 6),
            auto_sync_subscribed_series_cron=str(sync_raw.get("auto_sync_subscribed_series_cron", "")),
            auto_sync_user_backup_enabled=_coerce_bool(sync_raw.get("auto_sync_user_backup_enabled"), False),
            auto_sync_user_backup_interval_hours=_coerce_positive_int(sync_raw.get("auto_sync_user_backup_interval_hours"), 24),
            auto_sync_user_backup_cron=str(sync_raw.get("auto_sync_user_backup_cron", "")),
            auto_sync_pending_detection_enabled=_coerce_bool(sync_raw.get("auto_sync_pending_detection_enabled"), True),
            auto_sync_pending_detection_interval_hours=_coerce_positive_int(sync_raw.get("auto_sync_pending_detection_interval_hours"), 12),
            auto_sync_pending_detection_cron=str(sync_raw.get("auto_sync_pending_detection_cron", "")),
            auto_sync_preference_analyze_enabled=_coerce_bool(sync_raw.get("auto_sync_preference_analyze_enabled"), False),
            auto_sync_preference_analyze_interval_hours=_coerce_positive_int(sync_raw.get("auto_sync_preference_analyze_interval_hours"), 1),
            auto_sync_preference_analyze_cron=str(sync_raw.get("auto_sync_preference_analyze_cron", "*/30 * * * *")),
            preference_analyze_batch_size=_coerce_positive_int(sync_raw.get("preference_analyze_batch_size"), 200),
            pending_deletion_grace_period_days=_coerce_positive_int(sync_raw.get("pending_deletion_grace_period_days"), 30),
            pending_deletion_cleanup_confirmed_days=_coerce_positive_int(sync_raw.get("pending_deletion_cleanup_confirmed_days"), 7),
        ),
        storage=StorageSettings(
            public_dir=public_dir,
            private_dir=private_dir,
            db_path=db_path,
        ),
        log_level=log_level,
        dashboard_token=(
            os.getenv("DASHBOARD_TOKEN")
            or os.getenv("PIXIV_DASHBOARD_TOKEN")
            or raw_config.get("dashboard_token")
            or sync_raw.get("dashboard_token")  # 向后兼容：早期文档建议放在 sync 块下
            or None
        ),
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
    try:
        return int(value)
    except (ValueError, TypeError):
        return None



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
    try:
        return float(value)
    except (ValueError, TypeError):
        return float(default)


def _coerce_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _coerce_bookmark_restricts(value: Any) -> list[str]:
    if value in (None, ""):
        return ["public", "private"]
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        return ["public", "private"]

    normalized: list[str] = []
    for item in candidates:
        restrict = str(item).strip().lower()
        if restrict in ALLOWED_BOOKMARK_RESTRICTS and restrict not in normalized:
            normalized.append(restrict)
    return normalized or ["public", "private"]


def _coerce_bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "y"}:
        return True
    if normalized in {"0", "false", "no", "off", "n"}:
        return False
    return bool(default)


def _coerce_positive_int(value: Any, default: int) -> int:
    result = _coerce_int(value, default)
    return result if result > 0 else default


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


def cron_to_next_run(cron_expr: str, base_time: float | None = None, timezone: str = "UTC") -> float | None:
    """计算cron表达式的下次运行时间
    
    Args:
        cron_expr: cron表达式
        base_time: 基础时间戳，默认为当前时间
        timezone: 时区，如 "Asia/Seoul", "UTC", "Asia/Shanghai"
        
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
    
    # 尝试导入时区库
    try:
        from zoneinfo import ZoneInfo
        try:
            tz = ZoneInfo(timezone)
        except Exception:
            # 无效时区名（ZoneInfoNotFoundError 不是 ImportError 的子类），回退 UTC，
            # 否则异常会一路冒泡到调度循环，导致所有 cron 任务永远不触发。
            logger.warning("未知时区 %r，回退到 UTC", timezone)
            tz = ZoneInfo("UTC")
        base_dt = datetime.fromtimestamp(base_time, tz=tz)
    except ImportError:
        # Python < 3.9 或没有zoneinfo，尝试使用pytz
        try:
            import pytz
            try:
                tz = pytz.timezone(timezone)
            except Exception:
                logger.warning("未知时区 %r，回退到 UTC", timezone)
                tz = pytz.UTC
            base_dt = datetime.fromtimestamp(base_time, tz=tz)
        except ImportError:
            # 没有时区库，使用本地时间（不推荐）
            base_dt = datetime.fromtimestamp(base_time)

    # 简单实现：查找下一个匹配的时间
    # 这里使用简化的实现，实际项目中建议使用croniter库
    try:
        # 尝试导入croniter
        from croniter import croniter
    except ImportError:
        # 如果没有croniter，使用简化的实现
        return _simple_cron_next_run(parsed, base_dt)

    try:
        # 项目约定 6 段 cron 为 "秒 分 时 日 月 周"。croniter 默认把第 6 段当作
        # 年份，必须显式 second_at_beginning=True，否则 6 段表达式会被解析到完全
        # 错误的时间（且仍能通过校验，难以察觉）。
        if len(cron_expr.split()) == 6:
            cron = croniter(cron_expr, base_dt, second_at_beginning=True)
        else:
            cron = croniter(cron_expr, base_dt)
        next_dt = cron.get_next(datetime)
        return next_dt.timestamp()
    except Exception as exc:
        # croniter 对畸形表达式抛 CroniterNotAlphaError / CroniterBadCronError（构造时），
        # 对语法合法但永不出现的日期（如 "0 0 30 2 *" 2月30日）抛 CroniterBadDateError
        # （get_next 时）。这些都不是 ImportError，若放任冒泡会违反本函数「失败返回 None」
        # 的契约：保存设置时用户会收到 500 而非友好校验提示，调度循环也会整轮中断。
        logger.warning("无法解析 cron 表达式 %r: %s", cron_expr, exc)
        return None


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
        cron_weekdays = parse_field(parsed["weekday"], 0, 6)
        # cron 约定: 0=Sunday, 1=Monday, ..., 6=Saturday
        # Python datetime.weekday(): 0=Monday, ..., 6=Sunday
        # 转换: cron day -> python day: (cron - 1) % 7
        python_weekdays = [(d - 1) % 7 for d in cron_weekdays]
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
            current.weekday() in python_weekdays):
            return current.timestamp()
        current += timedelta(minutes=1)
    
    return None

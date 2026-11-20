"""Task metadata shared by logs, dispatch and the existing scheduler."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskDefinition:
    key: str
    label: str
    job_label: str
    scheduler_name: str | None = None
    setting_prefix: str | None = None
    priority: int = 3
    preemptible: bool = False


TASKS = (
    TaskDefinition("bookmark", "同步收藏小说", "收藏小说", "bookmarks", "bookmarks", 1),
    TaskDefinition(
        "subscribed_series", "同步追更系列", "订阅系列", "subscribed_series", "subscribed_series", 2
    ),
    TaskDefinition("following_users", "同步关注用户列表", "关注用户", "following_list", "following_list"),
    TaskDefinition(
        "following_novels",
        "同步关注用户小说",
        "关注用户小说",
        "following_novels",
        "following_novels",
        preemptible=True,
    ),
    TaskDefinition(
        "user_status", "检查用户状态", "用户状态检查", "user_status", "user_status", preemptible=True
    ),
    TaskDefinition(
        "novel_status", "检查小说状态", "小说状态检查", "novel_status", "novel_status", preemptible=True
    ),
    TaskDefinition(
        "series_status", "检查系列状态", "系列状态检查", "series_status", "series_status", preemptible=True
    ),
    TaskDefinition(
        "user_backup", "全量备份关注用户小说", "用户全量备份", "user_backup", "user_backup", preemptible=True
    ),
    TaskDefinition(
        "pending_deletion_detection",
        "检测取消收藏/追更",
        "待删除检测",
        "pending_deletion_detection",
        "pending_detection",
    ),
    TaskDefinition(
        "preference_analyze",
        "增量分析本地偏好",
        "偏好分析",
        "preference_analyze",
        "preference_analyze",
        preemptible=True,
    ),
    TaskDefinition("recommendation_run", "生成推荐", "生成推荐", "recommendation_run", "recommendation_run"),
    TaskDefinition("preference_summarize", "生成个人偏好画像", "生成个人偏好画像"),
    TaskDefinition("work_analysis", "分析作品", "分析作品"),
    TaskDefinition("keyword_clean", "关键词清洗", "关键词清洗"),
    TaskDefinition("manual", "手工同步", "手工同步"),
)
TASK_BY_KEY = {task.key: task for task in TASKS}
JOB_LABELS = {task.key: task.job_label for task in TASKS}
TASK_LABELS = {task.key: task.label for task in TASKS}
SCHEDULER_TASK_TYPE_ALIASES = {
    task.scheduler_name: task.key for task in TASKS if task.scheduler_name and task.scheduler_name != task.key
}
TASK_LABELS.update({name: TASK_LABELS[key] for name, key in SCHEDULER_TASK_TYPE_ALIASES.items()})
SCHEDULER_TASK_CONFIGS = tuple(
    {
        "name": task.scheduler_name,
        "setting_check": f"auto_sync_{task.setting_prefix}_enabled",
        "interval_setting": f"auto_sync_{task.setting_prefix}_interval_hours",
        "cron_setting": f"auto_sync_{task.setting_prefix}_cron",
        "priority": task.priority,
        "preemptible": task.preemptible,
    }
    for task in TASKS
    if task.scheduler_name
)

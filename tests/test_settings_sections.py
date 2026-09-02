"""设置页按分区独立保存的回归测试。

设置页拆成五个一级页面后，每页的表单只加载自己那一区的字段。此时若仍走全量保存，
`save_sync_settings` 里逐字段的 `payload.get(k, 既有值)` 会把「表单里根本没有的字段」
按默认值写回——同步页点一次保存就能把系统页的保留期从 30 天悄悄改成默认值。
所以分区保存必须先按白名单过滤 payload，让未声明的字段自动沿用 YAML 里的旧值。
"""
from __future__ import annotations

from itertools import combinations

import yaml

from pixiv_novel_sync.settings import load_settings
from pixiv_novel_sync.web.managers import (
    SCHEDULER_TASK_CONFIGS,
    SETTINGS_SECTIONS,
    scheduler_task_log_type,
)
from pixiv_novel_sync.web.utils import _settings_to_dict
from pixiv_novel_sync.webapp import SettingsManager


def _write_config(tmp_path, **sync_values):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"sync": sync_values}, allow_unicode=True), encoding="utf-8"
    )
    return config_path


def test_section_save_only_touches_its_own_fields(tmp_path):
    """分区保存不能把其它分区的字段写回默认值。

    回归意图：设置页拆分后，同步页的表单里没有 pending_deletion_grace_period_days，
    若保存时仍走全量路径，payload.get(k, 默认值) 会把它从 30 覆盖成默认值。
    """
    config_path = _write_config(
        tmp_path,
        max_items_per_run=20,
        pending_deletion_grace_period_days=30,
    )

    saved = SettingsManager(str(config_path)).save_sync_settings(
        {"max_items_per_run": 50}, section="sync"
    )

    assert saved["max_items_per_run"] == 50
    assert saved["pending_deletion_grace_period_days"] == 30
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["sync"]["pending_deletion_grace_period_days"] == 30


def test_section_save_ignores_fields_outside_the_section(tmp_path):
    """payload 里夹带别区字段时必须被忽略，而不是静默写入。"""
    config_path = _write_config(
        tmp_path,
        max_items_per_run=20,
        pending_deletion_grace_period_days=30,
    )

    SettingsManager(str(config_path)).save_sync_settings(
        {"max_items_per_run": 50, "pending_deletion_grace_period_days": 999},
        section="sync",
    )

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["sync"]["pending_deletion_grace_period_days"] == 30


def test_system_section_save_keeps_sync_fields(tmp_path):
    """反向同理：系统页保存不能把同步页的限速/分页字段冲掉。"""
    config_path = _write_config(
        tmp_path,
        max_items_per_run=20,
        delay_seconds_between_items=4.5,
        pending_deletion_grace_period_days=30,
    )

    saved = SettingsManager(str(config_path)).save_sync_settings(
        {"pending_deletion_grace_period_days": 45}, section="system"
    )

    assert saved["pending_deletion_grace_period_days"] == 45
    assert saved["max_items_per_run"] == 20
    assert saved["delay_seconds_between_items"] == 4.5


def test_task_log_retention_is_configurable_and_defaults_to_two_weeks(tmp_path):
    """任务日志保留天数必须可配，且默认给足两周。

    原来 `cleanup_old_task_logs(days=3)` 是硬编码：耗时趋势、调度预算、限速调参
    全都只能回看 3 天，而「上线后观察一周再调参数」这种计划因此永远无法执行。
    """
    default_path = _write_config(tmp_path, max_items_per_run=20)
    assert load_settings(str(default_path)).sync.task_log_retention_days == 14

    configured = _write_config(tmp_path, task_log_retention_days=30)
    assert load_settings(str(configured)).sync.task_log_retention_days == 30

    # 归属系统区：和「待删除保留期」放在一起，同步页保存不该动它
    assert "task_log_retention_days" in SETTINGS_SECTIONS["system"]
    saved = SettingsManager(str(configured)).save_sync_settings(
        {"task_log_retention_days": 7}, section="system"
    )
    assert saved["task_log_retention_days"] == 7
    assert saved["max_items_per_run"] == load_settings(str(configured)).sync.max_items_per_run

    # 0 / 负数会让清理把整张表删空，必须夹到至少 1 天
    floored = SettingsManager(str(configured)).save_sync_settings(
        {"task_log_retention_days": 0}, section="system"
    )
    assert floored["task_log_retention_days"] >= 1


def test_scheduler_cleanup_reads_retention_from_settings(tmp_path):
    """清理循环必须读配置，两张表用同一个值。

    源码断言：清理埋在调度线程的 while 循环里，行为测试要么起线程要么改结构，
    而这条约束的实质是「别再写死 3」，grep 就够。
    """
    from pathlib import Path

    from pixiv_novel_sync.web import managers as managers_module

    source = Path(managers_module.__file__).read_text(encoding="utf-8")

    assert "settings.sync.task_log_retention_days" in source
    assert "cleanup_old_task_logs(days=retention_days)" in source
    assert "cleanup_ai_jobs(keep_days=retention_days)" in source
    assert "cleanup_old_task_logs(days=3)" not in source
    assert "cleanup_ai_jobs(keep_days=3)" not in source


def test_full_save_without_section_keeps_legacy_behaviour(tmp_path):
    """section=None 必须保持原有全量行为（CLI 与既有测试依赖它）。"""
    config_path = _write_config(tmp_path, max_items_per_run=20)

    saved = SettingsManager(str(config_path)).save_sync_settings(
        {"max_items_per_run": 50, "pending_deletion_grace_period_days": 999}
    )

    assert saved["max_items_per_run"] == 50
    assert saved["pending_deletion_grace_period_days"] == 999


def test_invalid_section_is_rejected(tmp_path):
    config_path = _write_config(tmp_path, max_items_per_run=20)

    try:
        SettingsManager(str(config_path)).save_sync_settings({}, section="nope")
    except ValueError as exc:
        assert "分区" in str(exc)
    else:
        raise AssertionError("无效 section 必须抛 ValueError")


def test_sections_partition_every_settings_field(tmp_path):
    """每个 _settings_to_dict 字段必须落在且只落在一个分区里。

    漏一个字段的后果是它永远无法通过分区端点保存（过滤时被丢掉，页面上改了也存不进
    去）；多写一个不存在的字段则是拼写错误，同样只能靠这条断言发现。
    """
    config_path = _write_config(tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    exposed = set(_settings_to_dict(load_settings(config_path, env_path)))

    declared: set[str] = set()
    for fields in SETTINGS_SECTIONS.values():
        declared |= set(fields)

    assert exposed - declared == set(), "以下字段不属于任何分区，分区端点永远存不进去"
    assert declared - exposed == set(), "以下分区字段不在 _settings_to_dict 里，疑似拼写错误"

    for left, right in combinations(sorted(SETTINGS_SECTIONS), 2):
        overlap = SETTINGS_SECTIONS[left] & SETTINGS_SECTIONS[right]
        assert not overlap, f"{left} 与 {right} 重复声明了字段: {sorted(overlap)}"


def _dashboard_app(tmp_path, **sync_values):
    """建一个只连 tmp 目录的 dashboard app。

    必须显式 start_scheduler=False（tests/test_test_isolation.py 会检查）：默认会真的
    拉起调度线程，泄漏后每轮都重新 load_dotenv 那份 tmp .env，污染后续测试的环境变量。
    env_path 也必须显式给一份 tmp 文件，否则 load_dotenv() 会读进仓库根目录的真实
    .env——本机有没有 DASHBOARD_TOKEN 会决定鉴权门开不开，测试结果就不可复现了。
    """
    from pixiv_novel_sync.webapp import create_app

    config_path = _write_config(tmp_path, **sync_values)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(
        config_path=str(config_path), env_path=str(env_path), start_scheduler=False
    )
    return app, app.test_client(), config_path


def _csrf_headers(client):
    return {"X-CSRF-Token": client.get("/api/csrf-token").get_json()["csrf_token"]}


def test_section_endpoint_saves_only_its_section(tmp_path):
    """PUT /api/dashboard/settings/sync 不能动 system 区字段。"""
    _app, client, _config_path = _dashboard_app(
        tmp_path, max_items_per_run=20, pending_deletion_grace_period_days=30
    )

    res = client.put(
        "/api/dashboard/settings/sync",
        json={"max_items_per_run": 50, "pending_deletion_grace_period_days": 999},
        headers=_csrf_headers(client),
    )

    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["sync"]["max_items_per_run"] == 50
    assert body["sync"]["pending_deletion_grace_period_days"] == 30


def test_unknown_section_endpoint_returns_400(tmp_path):
    _app, client, _config_path = _dashboard_app(tmp_path, max_items_per_run=20)

    res = client.put(
        "/api/dashboard/settings/nope", json={}, headers=_csrf_headers(client)
    )

    assert res.status_code == 400
    assert res.get_json()["ok"] is False


def test_section_endpoint_reports_validation_failure(tmp_path):
    """校验失败要走 error/detail 结构（前端靠 window.errorText 读它）。"""
    _app, client, _config_path = _dashboard_app(tmp_path)

    res = client.put(
        "/api/dashboard/settings/sync",
        json={"auto_sync_bookmarks_cron": "不是 cron"},
        headers=_csrf_headers(client),
    )

    assert res.status_code == 400
    body = res.get_json()
    assert body["error"] == "保存设置失败"
    assert "auto_sync_bookmarks_cron" in body["detail"]


def test_settings_pages_all_render(tmp_path):
    _app, client, _config_path = _dashboard_app(tmp_path, max_items_per_run=20)

    for section in ("sync", "models", "agents", "adult", "system"):
        res = client.get(f"/dashboard/settings/{section}")
        assert res.status_code == 200, section

    assert client.get("/dashboard/settings/nope").status_code == 404

    legacy = client.get("/dashboard/settings")
    assert legacy.status_code == 302
    assert legacy.headers["Location"].endswith("/dashboard/settings/sync")


def test_manual_trigger_covers_every_scheduled_task_type(tmp_path):
    """回归：手动触发「同步追更系列」曾因 task_map 缺键必然 400。

    凡是调度器会自动跑的任务，设置页都得能手动触发一次，否则改完配置只能干等下一个
    周期。这里先占住唯一的 job 槽，让请求停在「已有任务运行」上——既证明 task_type
    通过了 task_map 校验，又不会真的拉起一个去打 Pixiv 的后台线程。
    """
    from pixiv_novel_sync.web.utils import _web_job_spec

    app, client, _config_path = _dashboard_app(tmp_path, max_items_per_run=20)
    app.config["job_manager"].submit(_web_job_spec(["bookmark"]))
    headers = _csrf_headers(client)

    for config in SCHEDULER_TASK_CONFIGS:
        task_type = scheduler_task_log_type(config["name"])
        res = client.post(f"/api/dashboard/sync/{task_type}", headers=headers)
        error = (res.get_json() or {}).get("error", "")
        assert "不支持的任务类型" not in error, task_type
        assert "已有同步任务" in error, task_type

    # 控制组：真正没登记的 task_type 仍必须被拒，否则上面的断言是空过
    res = client.post("/api/dashboard/sync/nope", headers=headers)
    assert (res.get_json() or {}).get("error") == "不支持的任务类型"


# ── 调度预算聚合 ──────────────────────────────────────────────────────
#
# 设置页的调度表要回答「这个任务上一轮跑了多久、每天要占掉多少时间」。这两个数字
# 一个来自 task_logs，一个要把 cron 频率乘上单轮耗时——都在后端算，前端不重复实现
# cron 解析，也不用去翻分页的 /api/dashboard/logs（20 条一页，11 个任务根本凑不齐）。


def _insert_finished_log(
    db, task_type: str, duration_seconds: float, hours_ago: float, status: str = "succeeded"
) -> None:
    """直接写一条终态历史日志。

    create_task_log/update_task_log 只能写「现在」且 duration 由 SQL 现算（测试里恒
    为 0），所以耗时相关的断言必须自己插行。
    """
    from datetime import datetime, timedelta, timezone as _tz

    started = (datetime.now(_tz.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    db.conn.execute(
        """
        INSERT INTO task_logs
            (task_type, task_name, job_id, status, started_at, finished_at,
             duration_seconds, is_auto_sync)
        VALUES (?, ?, NULL, ?, ?, ?, ?, 1)
        """,
        (task_type, task_type, status, started, started, duration_seconds),
    )
    db.conn.commit()


def _open_db(_app):
    """打开 dashboard app 实际使用的那个库。

    conftest 的 autouse fixture 把 PIXIV_DB_PATH 指到 tmp 目录，而环境变量优先于
    YAML，所以这就是 create_app 里 _open_database 打开的同一个文件。
    """
    import os
    from pathlib import Path

    from pixiv_novel_sync.storage_db import Database

    db = Database(Path(os.environ["PIXIV_DB_PATH"]))
    db.init_schema()
    return db


def test_budget_covers_every_scheduled_task(tmp_path):
    """调度表里每个任务都要有一行预算数据，缺一个就是界面上的空洞。"""
    _app, client, _config_path = _dashboard_app(tmp_path)

    body = client.get("/api/dashboard/auto-sync/budget").get_json()

    assert body["ok"] is True
    for config in SCHEDULER_TASK_CONFIGS:
        assert config["name"] in body["tasks"], config["name"]
        entry = body["tasks"][config["name"]]
        assert entry["task_type"] == scheduler_task_log_type(config["name"])
        assert entry["priority"] == config["priority"]
        assert entry["preemptible"] == config.get("preemptible", False)


def test_budget_reports_last_duration_and_daily_estimate(tmp_path):
    """每日预算 = 最近一轮耗时 × cron 的每天次数。

    观测窗口里的总耗时不能直接当预算：cron 一改，历史数据立刻失真。
    """
    app, client, _config_path = _dashboard_app(
        tmp_path,
        auto_sync_bookmarks_enabled=True,
        auto_sync_bookmarks_cron="20 0,4,8,12,16,20 * * *",
        auto_sync_timezone="Asia/Seoul",
    )
    db = _open_db(app)
    try:
        _insert_finished_log(db, "bookmark", 90.0, hours_ago=30)
        _insert_finished_log(db, "bookmark", 100.0, hours_ago=2)
    finally:
        db.close()

    entry = client.get("/api/dashboard/auto-sync/budget").get_json()["tasks"]["bookmarks"]

    assert entry["last_duration_seconds"] == 100.0
    assert entry["runs"] == 2
    assert entry["avg_duration_seconds"] == 95.0
    assert entry["runs_per_day"] == 6.0
    assert entry["schedule_source"] == "cron"
    # 6 次/天 × 100 秒
    assert entry["estimated_daily_seconds"] == 600.0


def test_budget_falls_back_to_interval_when_cron_is_unparsable(tmp_path):
    """cron 解析失败时调度器静默按 interval 跑，预算也必须按 interval 算。"""
    app, client, _config_path = _dashboard_app(
        tmp_path,
        auto_sync_novel_status_enabled=True,
        auto_sync_novel_status_cron="99 99 * * *",
        auto_sync_novel_status_interval_hours=6,
    )
    db = _open_db(app)
    try:
        _insert_finished_log(db, "novel_status", 120.0, hours_ago=1)
    finally:
        db.close()

    entry = client.get("/api/dashboard/auto-sync/budget").get_json()["tasks"][
        "novel_status"
    ]

    assert entry["cron_valid"] is False
    assert entry["schedule_source"] == "interval"
    assert entry["runs_per_day"] == 4.0
    assert entry["estimated_daily_seconds"] == 480.0


def test_budget_total_only_counts_enabled_tasks(tmp_path):
    """关掉的任务不占预算，否则合计数会把用户吓退。"""
    app, client, _config_path = _dashboard_app(
        tmp_path,
        auto_sync_bookmarks_enabled=True,
        auto_sync_bookmarks_cron="0 * * * *",
        auto_sync_novel_status_enabled=False,
        auto_sync_novel_status_cron="0 * * * *",
    )
    db = _open_db(app)
    try:
        _insert_finished_log(db, "bookmark", 10.0, hours_ago=1)
        _insert_finished_log(db, "novel_status", 3600.0, hours_ago=1)
    finally:
        db.close()

    body = client.get("/api/dashboard/auto-sync/budget").get_json()

    assert body["tasks"]["bookmarks"]["enabled"] is True
    assert body["tasks"]["novel_status"]["enabled"] is False
    # 24 次/天 × 10 秒；被停用的 novel_status（24×3600）不计入
    assert body["total_estimated_daily_seconds"] == 240.0
    assert 0 < body["total_duty_ratio"] < 0.01


def test_budget_tolerates_empty_history(tmp_path):
    """没有任何日志时不能 500，字段要给 None 而不是 0（0 会被读成「不耗时」）。"""
    _app, client, _config_path = _dashboard_app(tmp_path)

    entry = client.get("/api/dashboard/auto-sync/budget").get_json()["tasks"]["bookmarks"]

    assert entry["runs"] == 0
    assert entry["last_duration_seconds"] is None
    assert entry["estimated_daily_seconds"] is None


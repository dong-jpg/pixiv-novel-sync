from __future__ import annotations

import os
import re
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent

# 只有这些文件是在**测调度器自身的启动/去重语义**，必须真的把调度器拉起来；
# 它们自己负责 stop()（或用 FLASK_DEBUG 让 create_app 不自动启动）。
SCHEDULER_LIFECYCLE_TESTS = {"test_webapp_jobs.py"}

CREATE_APP_CALL = re.compile(r"create_app\(")


def test_runtime_paths_are_isolated_from_repository(tmp_path: Path) -> None:
    for env_name in ("PIXIV_DB_PATH", "PIXIV_PUBLIC_DIR", "PIXIV_PRIVATE_DIR"):
        runtime_path = Path(os.environ.get(env_name, ".")).resolve()
        assert runtime_path.is_relative_to(tmp_path), f"{env_name} 未隔离到 tmp_path: {runtime_path}"


def test_tests_do_not_leak_auto_sync_scheduler_threads() -> None:
    """除调度器生命周期测试外，测试里的 create_app 必须显式关掉调度器。

    `create_app` 默认会真的启动 AutoSyncScheduler（`start_scheduler=None` 时只有
    Werkzeug debug reloader 才会跳过）。测试不停它就会泄漏线程，而泄漏的线程每轮
    都调 `load_settings(config_path, env_path)` → `load_dotenv(那份 tmp .env)`，
    把 `DASHBOARD_TOKEN` 之类的键重新注入 `os.environ`。

    实测后果：`test_webapp_security.py` 里三个写了 `DASHBOARD_TOKEN=secret-token`
    到 tmp `.env` 的测试留下的线程，会在后续「未配置 token」的测试跑到一半时把
    token 塞回环境，于是 `/dashboard` 返回 302 而不是预期的 403——间歇性失败，
    单独跑却总是通过。
    """
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name in SCHEDULER_LIFECYCLE_TESTS:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if not CREATE_APP_CALL.search(line):
                continue
            # 调用可能跨多行，往后看几行找 start_scheduler
            window = "\n".join(lines[index : index + 6])
            if "start_scheduler" not in window:
                offenders.append(f"{path.name}:{index + 1}")
    assert not offenders, (
        "以下 create_app 调用没有 start_scheduler=False，会泄漏调度线程并污染后续测试环境：\n  "
        + "\n  ".join(offenders)
    )

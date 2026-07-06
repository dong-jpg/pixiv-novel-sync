# CLI Job Services Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Extract shared job services so CLI `user-backup`, `status-check`, and `pending-deletion-detection` execute real logic instead of returning “not available yet”.

**Architecture:** Add `pixiv_novel_sync.jobs.services` as a focused service layer for user backup, status checks, and pending deletion detection. Keep `JobManager`, `JobRunner`, Web routes, DB schema, and dashboard response shape unchanged. Route CLI through `jobs.tasks.execute_task()` and route existing `SyncWorker` methods through the same service functions.

**Tech Stack:** Python 3.11, pytest, dataclasses/protocol-like duck typing, existing Pixiv auth, SQLite `Database`, `BookmarkNovelSyncService`, Flask legacy worker.

---

## File Structure

- Create: `src/pixiv_novel_sync/jobs/services.py` — shared service functions, reporter helpers, Pixiv/DB/service setup, status checker helpers.
- Modify: `src/pixiv_novel_sync/jobs/tasks.py` — dispatch the five currently unavailable CLI tasks into `jobs.services`.
- Modify: `src/pixiv_novel_sync/webapp.py` — delegate `SyncWorker` status/user-backup/pending-detection methods to `jobs.services` while preserving legacy logging/progress.
- Modify: `tests/test_jobs_tasks.py` — TDD dispatch tests for new CLI task types.
- Create: `tests/test_jobs_services.py` — focused service tests using fakes/monkeypatches.
- Modify: `tests/test_webapp_jobs.py` — regression tests proving `SyncWorker` delegates to service.
- Modify: `tests/test_cli_jobs.py` — CLI runner test for a previously unavailable task.

---

### Task 1: Dispatch New CLI Tasks to Services

**Files:**
- Modify: `tests/test_jobs_tasks.py`
- Modify: `src/pixiv_novel_sync/jobs/tasks.py`

- [x] **Step 1: Write failing dispatch tests**

Append to `tests/test_jobs_tasks.py`:

```python

def test_execute_task_dispatches_user_backup_service(monkeypatch):
    calls = []

    def fake_run_user_backup_task(settings, user_id, reporter=None, stop_requested=None):
        calls.append((settings, user_id, reporter, stop_requested))
        return {"novels": 2}

    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_user_backup_task", fake_run_user_backup_task)
    settings = object()
    manager = object()

    result = execute_task("user_backup:123", settings, {"manager": manager, "job_id": "job-1"})

    assert result == {"novels": 2}
    assert calls[0][0] is settings
    assert calls[0][1] == 123
    assert calls[0][2] is not None
    assert calls[0][3] is not None


def test_execute_task_dispatches_status_services(monkeypatch):
    calls = []

    def fake_user_status(settings, reporter=None, stop_requested=None):
        calls.append(("user_status", settings, reporter, stop_requested))
        return {"checked_users": 1}

    def fake_novel_status(settings, reporter=None, stop_requested=None):
        calls.append(("novel_status", settings, reporter, stop_requested))
        return {"checked_novels": 2}

    def fake_series_status(settings, reporter=None, stop_requested=None):
        calls.append(("series_status", settings, reporter, stop_requested))
        return {"checked_series": 3}

    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_user_status_task", fake_user_status)
    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_novel_status_task", fake_novel_status)
    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_series_status_task", fake_series_status)
    settings = object()

    assert execute_task("user_status", settings, {"manager": object(), "job_id": "job-1"}) == {"checked_users": 1}
    assert execute_task("novel_status", settings, {"manager": object(), "job_id": "job-2"}) == {"checked_novels": 2}
    assert execute_task("series_status", settings, {"manager": object(), "job_id": "job-3"}) == {"checked_series": 3}
    assert [call[0] for call in calls] == ["user_status", "novel_status", "series_status"]
    assert all(call[2] is not None for call in calls)
    assert all(call[3] is not None for call in calls)


def test_execute_task_dispatches_pending_deletion_detection_service(monkeypatch):
    calls = []

    def fake_pending_detection(settings, reporter=None, stop_requested=None):
        calls.append((settings, reporter, stop_requested))
        return {"new_pending": 4}

    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_pending_deletion_detection_task", fake_pending_detection)
    settings = object()

    result = execute_task("pending_deletion_detection", settings, {"manager": object(), "job_id": "job-1"})

    assert result == {"new_pending": 4}
    assert calls[0][0] is settings
    assert calls[0][1] is not None
    assert calls[0][2] is not None
```

- [x] **Step 2: Run tests and verify they fail**

Run:

```bash
python -m pytest tests/test_jobs_tasks.py::test_execute_task_dispatches_user_backup_service tests/test_jobs_tasks.py::test_execute_task_dispatches_status_services tests/test_jobs_tasks.py::test_execute_task_dispatches_pending_deletion_detection_service -q
```

Expected: FAIL because `pixiv_novel_sync.jobs.services` does not exist and `execute_task()` still raises “not available yet”.

- [x] **Step 3: Create minimal services module**

Create `src/pixiv_novel_sync/jobs/services.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(slots=True)
class JobReporter:
    add_log: Callable[[str, str], None] | None = None
    update_progress: Callable[..., None] | None = None

    def info(self, message: str) -> None:
        self._log("info", message)

    def warning(self, message: str) -> None:
        self._log("warning", message)

    def error(self, message: str) -> None:
        self._log("error", message)

    def success(self, message: str) -> None:
        self._log("success", message)

    def progress(self, **kwargs: Any) -> None:
        if self.update_progress is not None:
            self.update_progress(**kwargs)

    def _log(self, level: str, message: str) -> None:
        if self.add_log is not None:
            self.add_log(level, message)


def run_user_backup_task(settings: Any, user_id: int | None = None, reporter: JobReporter | None = None, stop_requested: Callable[[], bool] | None = None) -> dict[str, Any]:
    return {}


def run_user_status_task(settings: Any, reporter: JobReporter | None = None, stop_requested: Callable[[], bool] | None = None) -> dict[str, Any]:
    return {}


def run_novel_status_task(settings: Any, reporter: JobReporter | None = None, stop_requested: Callable[[], bool] | None = None) -> dict[str, Any]:
    return {}


def run_series_status_task(settings: Any, reporter: JobReporter | None = None, stop_requested: Callable[[], bool] | None = None) -> dict[str, Any]:
    return {}


def run_pending_deletion_detection_task(settings: Any, reporter: JobReporter | None = None, stop_requested: Callable[[], bool] | None = None) -> dict[str, Any]:
    return {}
```

- [x] **Step 4: Route `execute_task()` to services**

In `src/pixiv_novel_sync/jobs/tasks.py`, add helper above `execute_task()`:

```python
def _job_reporter_from_context(context: dict[str, Any]):
    from pixiv_novel_sync.jobs.services import JobReporter

    manager = context.get("manager")
    job_id = str(context.get("job_id") or "")

    def add_log(level: str, message: str) -> None:
        if manager is not None and job_id:
            manager.add_log(job_id, level, message)

    def update_progress(**kwargs: Any) -> None:
        if manager is not None and job_id:
            manager.update_progress(job_id, **kwargs)

    return JobReporter(add_log=add_log, update_progress=update_progress)


def _stop_requested_from_context(context: dict[str, Any]):
    manager = context.get("manager")
    job_id = str(context.get("job_id") or "")

    def stop_requested() -> bool:
        if manager is None or not job_id or not hasattr(manager, "is_cancel_requested"):
            return False
        return bool(manager.is_cancel_requested(job_id))

    return stop_requested
```

Replace the unavailable block in `execute_task()` with:

```python
    if task_type.startswith("user_backup:"):
        from pixiv_novel_sync.jobs.services import run_user_backup_task

        user_id = int(task_type.split(":", 1)[1])
        return run_user_backup_task(
            settings,
            user_id=user_id,
            reporter=_job_reporter_from_context(context),
            stop_requested=_stop_requested_from_context(context),
        )

    if task_type == "user_status":
        from pixiv_novel_sync.jobs.services import run_user_status_task

        return run_user_status_task(settings, _job_reporter_from_context(context), _stop_requested_from_context(context))

    if task_type == "novel_status":
        from pixiv_novel_sync.jobs.services import run_novel_status_task

        return run_novel_status_task(settings, _job_reporter_from_context(context), _stop_requested_from_context(context))

    if task_type == "series_status":
        from pixiv_novel_sync.jobs.services import run_series_status_task

        return run_series_status_task(settings, _job_reporter_from_context(context), _stop_requested_from_context(context))

    if task_type == "pending_deletion_detection":
        from pixiv_novel_sync.jobs.services import run_pending_deletion_detection_task

        return run_pending_deletion_detection_task(settings, _job_reporter_from_context(context), _stop_requested_from_context(context))
```

- [x] **Step 5: Run dispatch tests**

Run:

```bash
python -m pytest tests/test_jobs_tasks.py -q
```

Expected: PASS.

---

### Task 2: Implement Status Check Services

**Files:**
- Create/Modify: `src/pixiv_novel_sync/jobs/services.py`
- Create: `tests/test_jobs_services.py`

- [x] **Step 1: Write failing service tests for status checks**

Create `tests/test_jobs_services.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

from pixiv_novel_sync.jobs import services
from pixiv_novel_sync.jobs.services import JobReporter


class FakeAuthResult:
    user_id = 999


class FakeAuthManager:
    def __init__(self, pixiv_settings):
        self.pixiv_settings = pixiv_settings

    def login(self):
        return object(), FakeAuthResult()


class FakeSettings:
    pixiv = object()

    class Sync:
        delay_seconds_between_skips = 0
        bookmark_restricts = ["public"]

    class Storage:
        db_path = "fake.db"
        public_dir = "public"
        private_dir = "private"

    sync = Sync()
    storage = Storage()


class FakeStatusDb:
    instances = []

    def __init__(self, path):
        self.path = path
        self.users = [
            {"user_id": 1, "name": "Alice"},
            {"user_id": 2, "name": "Bob"},
        ]
        self.user_statuses = []
        self.novel_statuses = []
        self.series_statuses = []
        FakeStatusDb.instances.append(self)

    def init_schema(self):
        pass

    def close(self):
        pass

    def list_users(self, page=1, page_size=500):
        if page == 1:
            return {"items": self.users, "total_pages": 1}
        return {"items": [], "total_pages": 1}

    def upsert_user_status(self, user_id, status):
        self.user_statuses.append((user_id, status))

    def get_all_novel_ids(self):
        return [10, 11]

    def upsert_novel_status(self, novel_id, status):
        self.novel_statuses.append((novel_id, status))

    def get_all_series_ids(self):
        return [20]

    def upsert_series_status(self, series_id, status):
        self.series_statuses.append((series_id, status))


def test_run_user_status_task_updates_each_user(monkeypatch):
    FakeStatusDb.instances.clear()
    monkeypatch.setattr(services, "PixivAuthManager", FakeAuthManager)
    monkeypatch.setattr(services, "Database", FakeStatusDb)
    monkeypatch.setattr(services, "check_pixiv_user_status", lambda api, user_id: f"status-{user_id}")
    logs = []
    reporter = JobReporter(add_log=lambda level, message: logs.append((level, message)))

    result = services.run_user_status_task(FakeSettings(), reporter=reporter)

    db = FakeStatusDb.instances[-1]
    assert result == {"checked_users": 2}
    assert db.user_statuses == [(1, "status-1"), (2, "status-2")]
    assert any("用户状态检查完成" in message for _level, message in logs)


def test_run_novel_status_task_updates_each_novel(monkeypatch):
    FakeStatusDb.instances.clear()
    monkeypatch.setattr(services, "PixivAuthManager", FakeAuthManager)
    monkeypatch.setattr(services, "Database", FakeStatusDb)
    monkeypatch.setattr(services, "check_novel_status", lambda api, novel_id: "normal" if novel_id == 10 else "deleted")

    result = services.run_novel_status_task(FakeSettings())

    db = FakeStatusDb.instances[-1]
    assert result == {"checked_novels": 2, "normal": 1, "deleted": 1}
    assert db.novel_statuses == [(10, "normal"), (11, "deleted")]


def test_run_series_status_task_updates_each_series(monkeypatch):
    FakeStatusDb.instances.clear()
    monkeypatch.setattr(services, "PixivAuthManager", FakeAuthManager)
    monkeypatch.setattr(services, "Database", FakeStatusDb)
    monkeypatch.setattr(services, "check_series_status", lambda api, series_id: "normal")

    result = services.run_series_status_task(FakeSettings())

    db = FakeStatusDb.instances[-1]
    assert result == {"checked_series": 1, "normal": 1}
    assert db.series_statuses == [(20, "normal")]
```

- [x] **Step 2: Run service tests and verify they fail**

Run:

```bash
python -m pytest tests/test_jobs_services.py::test_run_user_status_task_updates_each_user tests/test_jobs_services.py::test_run_novel_status_task_updates_each_novel tests/test_jobs_services.py::test_run_series_status_task_updates_each_series -q
```

Expected: FAIL because service functions still return empty dicts and imports do not exist.

- [x] **Step 3: Implement status check imports and helpers**

At top of `src/pixiv_novel_sync/jobs/services.py`, add:

```python
import logging
import time

from pixiv_novel_sync.auth import PixivAuthManager
from pixiv_novel_sync.storage_db import Database

logger = logging.getLogger(__name__)
```

Add helper functions below `JobReporter`:

```python
def _reporter(reporter: JobReporter | None) -> JobReporter:
    return reporter or JobReporter()


def _should_stop(stop_requested: Callable[[], bool] | None) -> bool:
    return bool(stop_requested and stop_requested())


def _login(settings: Any) -> tuple[Any, Any]:
    auth = PixivAuthManager(settings.pixiv)
    api, auth_result = auth.login()
    if auth_result.user_id is None:
        raise RuntimeError("Unable to determine user ID")
    return api, auth_result


def check_pixiv_user_status(api: Any, user_id: int) -> str:
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
    except Exception as exc:
        logger.warning("Failed to check user %s status: %s", user_id, exc)
        return "unknown"


def check_novel_status(api: Any, novel_id: int) -> str:
    try:
        result = api.novel_detail(novel_id)
        if result is None:
            return "deleted"
        novel = getattr(result, "novel", None)
        if novel is None and isinstance(result, dict):
            novel = result.get("novel")
        if novel is None:
            return "deleted"
        visible = novel.get("visible", True) if isinstance(novel, dict) else getattr(novel, "visible", True)
        if not visible:
            return "restricted"
        return "normal"
    except Exception as exc:
        logger.warning("Failed to check novel %s status: %s", novel_id, exc)
        return "unknown"


def check_series_status(api: Any, series_id: int) -> str:
    try:
        result = api.novel_series(series_id)
        if result is None:
            return "deleted"
        detail = result.get("novel_series_detail") if isinstance(result, dict) else getattr(result, "novel_series_detail", None)
        if detail is None:
            return "deleted"
        return "normal"
    except Exception as exc:
        logger.warning("Failed to check series %s status: %s", series_id, exc)
        return "unknown"
```

- [x] **Step 4: Implement status service functions**

Replace empty status functions in `src/pixiv_novel_sync/jobs/services.py` with:

```python
def run_user_status_task(settings: Any, reporter: JobReporter | None = None, stop_requested: Callable[[], bool] | None = None) -> dict[str, Any]:
    reporter = _reporter(reporter)
    reporter.info("开始检查用户状态")
    api, _auth_result = _login(settings)
    db = Database(settings.storage.db_path)
    db.init_schema()
    checked_count = 0
    try:
        user_list: list[dict[str, Any]] = []
        page_num = 1
        while True:
            page_data = db.list_users(page=page_num, page_size=500)
            items = page_data.get("items", [])
            if not items:
                break
            user_list.extend(items)
            if page_num >= page_data.get("total_pages", 1):
                break
            page_num += 1
        reporter.info(f"共 {len(user_list)} 个用户需要检查")
        for user in user_list:
            if _should_stop(stop_requested):
                break
            user_id = user.get("user_id")
            if not user_id:
                continue
            status = check_pixiv_user_status(api, int(user_id))
            db.upsert_user_status(int(user_id), status)
            checked_count += 1
            reporter.info(f"[{checked_count}/{len(user_list)}] 用户 {user.get('name', user_id)}: {status}")
            time.sleep(settings.sync.delay_seconds_between_skips)
        reporter.success(f"用户状态检查完成: {checked_count} 个用户")
        return {"checked_users": checked_count}
    finally:
        db.close()


def run_novel_status_task(settings: Any, reporter: JobReporter | None = None, stop_requested: Callable[[], bool] | None = None) -> dict[str, Any]:
    reporter = _reporter(reporter)
    reporter.info("开始检查小说状态")
    api, _auth_result = _login(settings)
    db = Database(settings.storage.db_path)
    db.init_schema()
    checked_count = 0
    status_counts: dict[str, int] = {}
    try:
        novel_ids = db.get_all_novel_ids()
        reporter.info(f"共 {len(novel_ids)} 本小说需要检查")
        for novel_id in novel_ids:
            if _should_stop(stop_requested):
                break
            status = check_novel_status(api, int(novel_id))
            db.upsert_novel_status(int(novel_id), status)
            checked_count += 1
            status_counts[status] = status_counts.get(status, 0) + 1
            if status != "normal":
                reporter.warning(f"[{checked_count}/{len(novel_ids)}] 小说 {novel_id}: {status}")
            elif checked_count % 50 == 0:
                reporter.info(f"[{checked_count}/{len(novel_ids)}] 已检查...")
            time.sleep(settings.sync.delay_seconds_between_skips)
        summary = ", ".join(f"{key}: {value}" for key, value in status_counts.items())
        reporter.success(f"小说状态检查完成: {checked_count} 本 ({summary})")
        return {"checked_novels": checked_count, **status_counts}
    finally:
        db.close()


def run_series_status_task(settings: Any, reporter: JobReporter | None = None, stop_requested: Callable[[], bool] | None = None) -> dict[str, Any]:
    reporter = _reporter(reporter)
    reporter.info("开始检查系列状态")
    api, _auth_result = _login(settings)
    db = Database(settings.storage.db_path)
    db.init_schema()
    checked_count = 0
    status_counts: dict[str, int] = {}
    try:
        series_ids = db.get_all_series_ids()
        reporter.info(f"共 {len(series_ids)} 个系列需要检查")
        for series_id in series_ids:
            if _should_stop(stop_requested):
                break
            status = check_series_status(api, int(series_id))
            db.upsert_series_status(int(series_id), status)
            checked_count += 1
            status_counts[status] = status_counts.get(status, 0) + 1
            if status != "normal":
                reporter.warning(f"[{checked_count}/{len(series_ids)}] 系列 {series_id}: {status}")
            elif checked_count % 20 == 0:
                reporter.info(f"[{checked_count}/{len(series_ids)}] 已检查...")
            time.sleep(settings.sync.delay_seconds_between_skips)
        summary = ", ".join(f"{key}: {value}" for key, value in status_counts.items())
        reporter.success(f"系列状态检查完成: {checked_count} 个 ({summary})")
        return {"checked_series": checked_count, **status_counts}
    finally:
        db.close()
```

- [x] **Step 5: Run status service tests**

Run:

```bash
python -m pytest tests/test_jobs_services.py -q
```

Expected: PASS.

---

### Task 3: Implement User Backup Service

**Files:**
- Modify: `src/pixiv_novel_sync/jobs/services.py`
- Modify: `tests/test_jobs_services.py`

- [x] **Step 1: Write failing user backup service test**

Append to `tests/test_jobs_services.py`:

```python

class FakeUserBackupDb(FakeStatusDb):
    def __init__(self, path):
        super().__init__(path)
        self.conn = self
        self.watermark = None
        self.updated_watermark = None

    def execute(self, sql, params=()):
        if "SELECT user_id FROM users" in sql:
            return SimpleNamespace(fetchall=lambda: [(1,), (2,)])
        if "SELECT name FROM users" in sql:
            return SimpleNamespace(fetchone=lambda: (f"User {params[0]}",))
        raise AssertionError(sql)

    def get_watermark(self, key):
        return self.watermark

    def update_watermark(self, key, value):
        self.updated_watermark = (key, value)


class FakeStorage:
    def __init__(self, settings):
        self.settings = settings

    def ensure_dirs(self, paths):
        self.paths = paths


class FakeNovelService:
    def __init__(self, api, db, storage, settings):
        self.api = api
        self.db = db
        self.storage = storage
        self.settings = settings

    def _sync_novel(self, novel, restrict, download_assets, write_markdown, write_raw_text, source_type, source_key):
        return {"skipped": False, "assets_downloaded": 1}


class FakeUserNovelResult:
    novels = [object(), object()]
    next_url = None


class FakeUserBackupApi:
    def user_novels(self, **kwargs):
        return FakeUserNovelResult()

    def parse_qs(self, next_url):
        return None


def test_run_user_backup_task_syncs_requested_user(monkeypatch):
    FakeStatusDb.instances.clear()

    class Auth(FakeAuthManager):
        def login(self):
            return FakeUserBackupApi(), FakeAuthResult()

    monkeypatch.setattr(services, "PixivAuthManager", Auth)
    monkeypatch.setattr(services, "Database", FakeUserBackupDb)
    monkeypatch.setattr(services, "FileStorage", FakeStorage)
    monkeypatch.setattr(services, "BookmarkNovelSyncService", FakeNovelService)
    logs = []
    progress = []
    reporter = JobReporter(
        add_log=lambda level, message: logs.append((level, message)),
        update_progress=lambda **kwargs: progress.append(kwargs),
    )

    result = services.run_user_backup_task(FakeSettings(), user_id=2, reporter=reporter)

    assert result == {"novels": 2, "skipped": 0, "assets_downloaded": 2, "users": 1}
    assert any("User 2" in message for _level, message in logs)
    assert progress[-1]["author"] == "User 2"


def test_run_user_backup_task_uses_rotation_when_no_user_id(monkeypatch):
    FakeStatusDb.instances.clear()

    class Auth(FakeAuthManager):
        def login(self):
            return FakeUserBackupApi(), FakeAuthResult()

    monkeypatch.setattr(services, "PixivAuthManager", Auth)
    monkeypatch.setattr(services, "Database", FakeUserBackupDb)
    monkeypatch.setattr(services, "FileStorage", FakeStorage)
    monkeypatch.setattr(services, "BookmarkNovelSyncService", FakeNovelService)

    result = services.run_user_backup_task(FakeSettings(), user_id=None)

    db = FakeStatusDb.instances[-1]
    assert result["users"] == 2
    assert db.updated_watermark[0] == "user_backup_rotation"
```

- [x] **Step 2: Run user backup tests and verify they fail**

Run:

```bash
python -m pytest tests/test_jobs_services.py::test_run_user_backup_task_syncs_requested_user tests/test_jobs_services.py::test_run_user_backup_task_uses_rotation_when_no_user_id -q
```

Expected: FAIL because `run_user_backup_task()` still returns `{}`.

- [x] **Step 3: Add imports for storage and sync service**

Add to `src/pixiv_novel_sync/jobs/services.py` imports:

```python
from datetime import datetime, timezone

from pixiv_novel_sync.storage_files import FileStorage
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService
```

- [x] **Step 4: Implement `run_user_backup_task()`**

Replace the empty `run_user_backup_task()` with:

```python
def run_user_backup_task(settings: Any, user_id: int | None = None, reporter: JobReporter | None = None, stop_requested: Callable[[], bool] | None = None) -> dict[str, Any]:
    reporter = _reporter(reporter)
    reporter.info("加载配置完成")
    api, auth_result = _login(settings)
    reporter.success(f"登录成功, 用户ID: {auth_result.user_id}")

    db = Database(settings.storage.db_path)
    db.init_schema()
    storage = FileStorage(settings)
    storage.ensure_dirs([settings.storage.public_dir, settings.storage.private_dir, settings.storage.db_path.parent])
    try:
        service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)
        if user_id is not None:
            batch = [int(user_id)]
            next_offset = 0
            total_users = 1
            offset = 0
        else:
            all_user_ids = [row[0] for row in db.conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()]
            total_users = len(all_user_ids)
            if total_users == 0:
                reporter.info("没有关注用户，跳过")
                return {"novels": 0, "skipped": 0, "assets_downloaded": 0, "users": 0}
            watermark = db.get_watermark("user_backup_rotation")
            offset = watermark.get("offset", 0) if watermark else 0
            if offset >= total_users:
                offset = 0
            users_limit = settings.sync.auto_sync_following_novels_users_limit
            if users_limit <= 0:
                users_limit = total_users
            batch = all_user_ids[offset:offset + users_limit]
            next_offset = offset + len(batch)
            if next_offset >= total_users:
                next_offset = 0

        reporter.info(f"=== 全量备份关注用户小说: 用户 {offset + 1}-{offset + len(batch)}/{total_users}, 本轮 {len(batch)} 人 ===")
        total_novels = 0
        total_skipped = 0
        total_assets = 0
        for index, uid in enumerate(batch):
            if _should_stop(stop_requested):
                break
            user_row = db.conn.execute("SELECT name FROM users WHERE user_id = ?", (uid,)).fetchone()
            user_name = user_row[0] if user_row else str(uid)
            reporter.info(f"[{index + 1}/{len(batch)}] {user_name} (ID: {uid})")
            reporter.progress(phase="全量备份", current=index + 1, total=len(batch), author=user_name)
            next_query: dict[str, Any] | None = {"user_id": uid}
            user_novels = 0
            user_skipped = 0
            while next_query:
                result = api.user_novels(**next_query)
                for novel in getattr(result, "novels", []) or []:
                    if _should_stop(stop_requested):
                        break
                    counters = service._sync_novel(
                        novel,
                        "public",
                        settings.sync.download_assets,
                        settings.sync.write_markdown,
                        settings.sync.write_raw_text,
                        source_type="user_backup",
                        source_key=str(uid),
                    )
                    if counters.get("skipped"):
                        user_skipped += 1
                        if settings.sync.delay_seconds_between_skips > 0:
                            time.sleep(settings.sync.delay_seconds_between_skips)
                    else:
                        user_novels += 1
                        total_assets += counters.get("assets_downloaded", 0)
                        if settings.sync.delay_seconds_between_items > 0:
                            time.sleep(settings.sync.delay_seconds_between_items)
                next_query = api.parse_qs(getattr(result, "next_url", None))
                if next_query and settings.sync.delay_seconds_between_pages > 0:
                    time.sleep(settings.sync.delay_seconds_between_pages)
            total_novels += user_novels
            total_skipped += user_skipped
            reporter.info(f"  同步 {user_novels} 本, 跳过 {user_skipped} 本")

        if user_id is None:
            db.update_watermark("user_backup_rotation", {
                "offset": next_offset,
                "last_sync_time": datetime.now(timezone.utc).isoformat(),
            })
        reporter.success(f"全量备份完成: 同步 {total_novels} 本, 跳过 {total_skipped} 本, 资源 {total_assets} 个")
        return {"novels": total_novels, "skipped": total_skipped, "assets_downloaded": total_assets, "users": len(batch)}
    finally:
        db.close()
```

- [x] **Step 5: Run user backup service tests**

Run:

```bash
python -m pytest tests/test_jobs_services.py -q
```

Expected: PASS.

---

### Task 4: Implement Pending Deletion Detection Service

**Files:**
- Modify: `src/pixiv_novel_sync/jobs/services.py`
- Modify: `tests/test_jobs_services.py`

- [x] **Step 1: Write failing pending detection service test**

Append to `tests/test_jobs_services.py`:

```python

class FakePendingDetectionService:
    def __init__(self, api, db, storage, settings):
        self.api = api
        self.db = db
        self.storage = storage
        self.settings = settings

    def run_detection(self, user_id, restricts, progress_callback=None):
        if progress_callback:
            progress_callback("phase", {"phase": "检测中"})
            progress_callback("rate_limit", {"seconds": 1})
        return {"total_checked": 3, "new_pending": 2, "cleaned": 1}


def test_run_pending_deletion_detection_task_uses_sync_service(monkeypatch):
    FakeStatusDb.instances.clear()

    class Auth(FakeAuthManager):
        def login(self):
            return object(), FakeAuthResult()

    monkeypatch.setattr(services, "PixivAuthManager", Auth)
    monkeypatch.setattr(services, "Database", FakeStatusDb)
    monkeypatch.setattr(services, "FileStorage", FakeStorage)
    monkeypatch.setattr(services, "BookmarkNovelSyncService", FakePendingDetectionService)
    logs = []
    reporter = JobReporter(add_log=lambda level, message: logs.append((level, message)))

    result = services.run_pending_deletion_detection_task(FakeSettings(), reporter=reporter)

    assert result == {"total_checked": 3, "new_pending": 2, "cleaned": 1}
    assert ("info", "检测中") in logs
    assert any("检测完成" in message for level, message in logs if level == "success")
```

- [x] **Step 2: Run pending detection test and verify it fails**

Run:

```bash
python -m pytest tests/test_jobs_services.py::test_run_pending_deletion_detection_task_uses_sync_service -q
```

Expected: FAIL because `run_pending_deletion_detection_task()` still returns `{}`.

- [x] **Step 3: Implement `run_pending_deletion_detection_task()`**

Replace the empty function in `src/pixiv_novel_sync/jobs/services.py` with:

```python
def run_pending_deletion_detection_task(settings: Any, reporter: JobReporter | None = None, stop_requested: Callable[[], bool] | None = None) -> dict[str, Any]:
    reporter = _reporter(reporter)
    reporter.info("=== 开始检测取消收藏/追更 ===")
    api, auth_result = _login(settings)
    reporter.success(f"登录成功, 用户ID: {auth_result.user_id}")
    if _should_stop(stop_requested):
        return {"total_checked": 0, "new_pending": 0}

    db = Database(settings.storage.db_path)
    db.init_schema()
    storage = FileStorage(settings)
    try:
        service = BookmarkNovelSyncService(api=api, db=db, storage=storage, settings=settings)

        def on_progress(event_type: str, data: dict[str, Any]) -> None:
            if _should_stop(stop_requested):
                raise InterruptedError("Task stopped by user")
            if event_type == "phase":
                reporter.info(data.get("phase", ""))
            elif event_type == "rate_limit":
                reporter.warning(f"等待 {data.get('seconds', 1)} 秒")

        result = service.run_detection(
            user_id=auth_result.user_id,
            restricts=settings.sync.bookmark_restricts,
            progress_callback=on_progress,
        )
        reporter.success(f"检测完成: 发现 {result.get('new_pending', 0)} 条新的待确认记录")
        return result
    finally:
        db.close()
```

- [x] **Step 4: Run service tests**

Run:

```bash
python -m pytest tests/test_jobs_services.py -q
```

Expected: PASS.

---

### Task 5: Delegate Web SyncWorker Methods to Services

**Files:**
- Modify: `src/pixiv_novel_sync/webapp.py`
- Modify: `tests/test_webapp_jobs.py`

- [x] **Step 1: Write failing Web delegation tests**

Append to `tests/test_webapp_jobs.py`:

```python

def test_sync_worker_user_status_delegates_to_service(monkeypatch):
    calls = []

    def fake_run_user_status_task(settings, reporter=None, stop_requested=None):
        calls.append((settings, reporter, stop_requested))
        reporter.info("service called")
        return {"checked_users": 1}

    from pixiv_novel_sync.webapp import AutoSyncScheduler, SyncJobManager

    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_user_status_task", fake_run_user_status_task)
    manager = SyncJobManager(config_path=None, env_path=None)
    job = manager.start_auto_job("user_status", "用户状态检查")
    scheduler = AutoSyncScheduler(config_path=None, env_path=None, sync_job_manager=manager)

    scheduler._sync_user_status(object(), job.job_id)

    assert calls
    assert any(entry[2] == "service called" for entry in job.logs)


def test_sync_worker_pending_detection_delegates_to_service(monkeypatch):
    calls = []

    def fake_run_pending_detection(settings, reporter=None, stop_requested=None):
        calls.append((settings, reporter, stop_requested))
        reporter.success("pending service called")
        return {"new_pending": 1}

    from pixiv_novel_sync.webapp import AutoSyncScheduler, SyncJobManager

    monkeypatch.setattr("pixiv_novel_sync.jobs.services.run_pending_deletion_detection_task", fake_run_pending_detection)
    manager = SyncJobManager(config_path=None, env_path=None)
    job = manager.start_auto_job("pending_deletion_detection", "待删除检测")
    scheduler = AutoSyncScheduler(config_path=None, env_path=None, sync_job_manager=manager)

    scheduler._sync_pending_detection(object(), job.job_id)

    assert calls
    assert any(entry[2] == "pending service called" for entry in job.logs)
```

- [x] **Step 2: Run Web delegation tests and verify they fail**

Run:

```bash
python -m pytest tests/test_webapp_jobs.py::test_sync_worker_user_status_delegates_to_service tests/test_webapp_jobs.py::test_sync_worker_pending_detection_delegates_to_service -q
```

Expected: FAIL because Web worker methods still execute inline logic instead of monkeypatched services.

- [x] **Step 3: Add legacy reporter helper in `webapp.py`**

Near `AutoSyncScheduler` methods, add method inside the class:

```python
    def _legacy_job_reporter(self, job_id: str | None):
        from .jobs.services import JobReporter

        def add_log(level: str, message: str) -> None:
            if job_id and self.sync_job_manager:
                self.sync_job_manager.add_log(job_id, level, message)

        def update_progress(**kwargs: Any) -> None:
            if job_id and self.sync_job_manager:
                self.sync_job_manager.update_progress(job_id, **kwargs)

        return JobReporter(add_log=add_log, update_progress=update_progress)
```

- [x] **Step 4: Replace five worker methods with service delegation**

Replace bodies of these methods in `src/pixiv_novel_sync/webapp.py`:

```python
    def _sync_user_status(self, settings: Settings, job_id: str | None) -> None:
        from .jobs.services import run_user_status_task

        run_user_status_task(settings, reporter=self._legacy_job_reporter(job_id), stop_requested=self._check_stop)

    def _sync_novel_status(self, settings: Settings, job_id: str | None) -> None:
        from .jobs.services import run_novel_status_task

        run_novel_status_task(settings, reporter=self._legacy_job_reporter(job_id), stop_requested=self._check_stop)

    def _sync_series_status(self, settings: Settings, job_id: str | None) -> None:
        from .jobs.services import run_series_status_task

        run_series_status_task(settings, reporter=self._legacy_job_reporter(job_id), stop_requested=self._check_stop)

    def _sync_user_backup(self, settings: Settings, job_id: str | None) -> None:
        from .jobs.services import run_user_backup_task

        run_user_backup_task(settings, reporter=self._legacy_job_reporter(job_id), stop_requested=self._check_stop)

    def _sync_pending_detection(self, settings: Settings, job_id: str | None) -> None:
        from .jobs.services import run_pending_deletion_detection_task

        run_pending_deletion_detection_task(settings, reporter=self._legacy_job_reporter(job_id), stop_requested=self._check_stop)
```

Keep `_check_pixiv_user_status`, `_check_novel_status`, `_check_series_status` top-level helpers for now if other code or tests still import them; do not remove them in this task.

- [x] **Step 5: Run Web delegation tests**

Run:

```bash
python -m pytest tests/test_webapp_jobs.py -q
```

Expected: PASS.

---

### Task 6: CLI Runner Regression for New Task

**Files:**
- Modify: `tests/test_cli_jobs.py`

- [x] **Step 1: Add CLI run test for status-check**

Append to `tests/test_cli_jobs.py`:

```python

def test_run_job_command_executes_status_check_task(monkeypatch, capsys):
    calls = []

    def fake_execute_task(task_type, settings, context):
        calls.append((task_type, settings, context["job_id"]))
        return {"checked_users": 2}

    monkeypatch.setattr("pixiv_novel_sync.cli.execute_task", fake_execute_task)
    fake_settings = object()
    parser = build_parser()
    args = parser.parse_args(["status-check", "user_status"])

    exit_code = run_job_command(args, fake_settings)

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "succeeded"
    assert output["stats"] == {"checked_users": 2}
    assert calls[0][0] == "user_status"
    assert calls[0][1] is fake_settings
    assert calls[0][2]
```

- [x] **Step 2: Run CLI tests**

Run:

```bash
python -m pytest tests/test_cli_jobs.py -q
```

Expected: PASS. This may already pass because CLI uses injected `execute_task`; keep it as regression coverage.

---

### Task 7: Focused and Full Verification

**Files:**
- Test only.

- [x] **Step 1: Run focused job tests**

Run:

```bash
python -m pytest tests/test_jobs_tasks.py tests/test_jobs_services.py tests/test_cli_jobs.py -q
```

Expected: all tests PASS.

- [x] **Step 2: Run Web regression tests**

Run:

```bash
python -m pytest tests/test_webapp_jobs.py tests/test_webapp_security.py -q
```

Expected: all tests PASS.

- [x] **Step 3: Run full test suite**

Run:

```bash
python -m pytest -q
```

Expected: all tests PASS.

- [x] **Step 4: Run CLI smoke tests**

Run:

```bash
python -m pixiv_novel_sync.cli user-backup --help
python -m pixiv_novel_sync.cli status-check --help
python -m pixiv_novel_sync.cli pending-deletion-detection --help
```

Expected: all commands exit 0 and display help.

---

## Self-Review

- Spec coverage: Tasks 1-4 implement the shared service layer and CLI dispatch for all five unavailable tasks. Task 5 routes Web legacy worker methods to the same service layer. Task 6-7 verify CLI and Web behavior.
- Placeholder scan: No TBD/TODO placeholders remain; every code-changing step includes concrete code.
- Type consistency: `JobReporter`, `run_user_backup_task`, `run_user_status_task`, `run_novel_status_task`, `run_series_status_task`, and `run_pending_deletion_detection_task` names match across tasks, services, CLI dispatch, and Web delegation.
- Scope control: The plan does not change DB schema, Web route response shape, `JobManager`, `JobRunner`, or full `sync_engine.py` architecture.

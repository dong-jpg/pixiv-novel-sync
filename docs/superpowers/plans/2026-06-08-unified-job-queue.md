# Unified Job Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a gradual unified job layer so Web, CLI, and systemd/cron can execute core Pixiv sync tasks through the same `JobSpec` / runner semantics while tightening deployment-critical security defaults.

**Architecture:** Add a focused `pixiv_novel_sync.jobs` layer with shared models, an in-memory manager, a runner, and task dispatch helpers. Keep existing `BookmarkNovelSyncService` and most `webapp.py` behavior intact for the first pass, then adapt Web/CLI entry points to submit or execute shared job specs. Security fixes are implemented in the Web layer before this first refactor is considered complete.

**Tech Stack:** Python 3.11, dataclasses, threading, Flask, pytest, argparse, SQLite-backed existing services.

---

## File Structure

- Create: `src/pixiv_novel_sync/jobs/models.py` — shared job enums and dataclasses.
- Create: `src/pixiv_novel_sync/jobs/manager.py` — in-memory job lifecycle manager with logs, progress, cancellation, cleanup, and concurrency guard.
- Create: `src/pixiv_novel_sync/jobs/runner.py` — synchronous runner that executes `JobSpec` task lists using an injected task executor.
- Create: `src/pixiv_novel_sync/jobs/tasks.py` — mapping from task type to existing sync/status functions; later used by Web and CLI.
- Modify: `src/pixiv_novel_sync/jobs/quick_sync.py` — keep compatibility, expose helpers that can be called by `tasks.py`.
- Modify: `src/pixiv_novel_sync/cli.py` — add deployment-friendly CLI commands backed by `JobSpec` / `JobRunner`.
- Modify: `src/pixiv_novel_sync/webapp.py` — tighten security defaults and gradually route Web job creation through the shared manager/runner semantics while preserving response shapes.
- Modify: `src/pixiv_novel_sync/ai/retrieval.py` — small low-risk optimization: do not call remote embedding API when a project has no indexed rows.
- Create: `tests/test_jobs_models.py` — model serialization and defaults.
- Create: `tests/test_jobs_manager.py` — manager lifecycle, log trimming, cancellation, and cleanup.
- Create: `tests/test_jobs_runner.py` — runner success/failure/cancel behavior with fake tasks.
- Create: `tests/test_webapp_security.py` — dashboard token fallback, token response redaction, persistent Flask secret.
- Modify: `tests/test_ai_retrieval.py` — empty-index API embedding search should not call embedding API.
- Modify: `tests/test_webapp_settings.py` or create adjacent CLI tests — CLI help and command spec generation.

---

### Task 1: Job Models

**Files:**
- Create: `src/pixiv_novel_sync/jobs/models.py`
- Create: `tests/test_jobs_models.py`

- [ ] **Step 1: Write failing tests for job model defaults**

Create `tests/test_jobs_models.py`:

```python
from __future__ import annotations

from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobState, JobStatus, JobType


def test_job_spec_defaults_to_sync_job():
    spec = JobSpec(source=JobSource.CLI, task_types=["bookmark", "following_users"])

    assert spec.job_type == JobType.SYNC
    assert spec.source == JobSource.CLI
    assert spec.task_types == ["bookmark", "following_users"]
    assert spec.params == {}


def test_job_state_starts_queued_with_empty_collections():
    state = JobState(job_id="job-1", spec=JobSpec(source=JobSource.WEB, task_types=["bookmark"]))

    assert state.status == JobStatus.QUEUED
    assert state.message == "任务已排队"
    assert state.progress == {}
    assert state.stats == {}
    assert state.logs == []
    assert state.error is None
    assert state.started_at is None
    assert state.finished_at is None
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_jobs_models.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'pixiv_novel_sync.jobs.models'`.

- [ ] **Step 3: Implement job model dataclasses**

Create `src/pixiv_novel_sync/jobs/models.py`:

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class JobSource(StrEnum):
    WEB = "web"
    CLI = "cli"
    SCHEDULER = "scheduler"
    SYSTEMD = "systemd"


class JobType(StrEnum):
    SYNC = "sync"
    SYNC_CHECK = "sync_check"
    STATUS_CHECK = "status_check"
    PENDING_DELETION_DETECTION = "pending_deletion_detection"
    USER_BACKUP = "user_backup"


@dataclass(slots=True)
class JobSpec:
    source: JobSource
    task_types: list[str]
    job_type: JobType = JobType.SYNC
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class JobLogEntry:
    time: str
    level: str
    message: str


@dataclass(slots=True)
class JobState:
    job_id: str
    spec: JobSpec
    status: JobStatus = JobStatus.QUEUED
    message: str = "任务已排队"
    progress: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    logs: list[JobLogEntry] = field(default_factory=list)
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def task_types(self) -> list[str]:
        return self.spec.task_types
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
pytest tests/test_jobs_models.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Only commit if the user has explicitly requested commits. If not, skip commit and keep changes staged/unstaged as appropriate.

---

### Task 2: In-Memory Job Manager

**Files:**
- Create: `src/pixiv_novel_sync/jobs/manager.py`
- Create: `tests/test_jobs_manager.py`

- [ ] **Step 1: Write failing manager tests**

Create `tests/test_jobs_manager.py`:

```python
from __future__ import annotations

from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus


def test_submit_creates_queued_job():
    manager = JobManager(max_logs=3, max_jobs=10)
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))

    assert state.job_id
    assert state.status == JobStatus.QUEUED
    assert manager.get_job(state.job_id) is state


def test_add_log_trims_old_entries():
    manager = JobManager(max_logs=2, max_jobs=10)
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))

    manager.add_log(state.job_id, "info", "one")
    manager.add_log(state.job_id, "info", "two")
    manager.add_log(state.job_id, "info", "three")

    assert [entry.message for entry in state.logs] == ["two", "three"]


def test_update_progress_updates_message_when_provided():
    manager = JobManager(max_logs=3, max_jobs=10)
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))

    manager.update_progress(state.job_id, phase="准备", message="正在准备")

    assert state.progress["phase"] == "准备"
    assert state.message == "正在准备"


def test_request_cancel_marks_queued_job_cancel_requested():
    manager = JobManager(max_logs=3, max_jobs=10)
    state = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))

    assert manager.request_cancel(state.job_id) is True
    assert state.status == JobStatus.CANCEL_REQUESTED


def test_cleanup_old_jobs_keeps_recent_completed_jobs():
    manager = JobManager(max_logs=3, max_jobs=2)
    first = manager.submit(JobSpec(source=JobSource.WEB, task_types=["bookmark"]))
    second = manager.submit(JobSpec(source=JobSource.WEB, task_types=["following_users"]))
    third = manager.submit(JobSpec(source=JobSource.WEB, task_types=["following_novels"]))
    first.status = JobStatus.SUCCEEDED
    second.status = JobStatus.SUCCEEDED
    third.status = JobStatus.RUNNING

    manager.cleanup_old_jobs()

    assert manager.get_job(first.job_id) is None
    assert manager.get_job(second.job_id) is second
    assert manager.get_job(third.job_id) is third
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_jobs_manager.py -v
```

Expected: FAIL with missing `pixiv_novel_sync.jobs.manager`.

- [ ] **Step 3: Implement `JobManager`**

Create `src/pixiv_novel_sync/jobs/manager.py`:

```python
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .models import JobLogEntry, JobSpec, JobState, JobStatus


@dataclass(slots=True)
class JobManager:
    max_logs: int = 50
    max_jobs: int = 100
    _jobs: dict[str, JobState] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _semaphore: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(1))

    def submit(self, spec: JobSpec) -> JobState:
        with self._lock:
            job_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
            state = JobState(job_id=job_id, spec=spec)
            self._jobs[job_id] = state
            self.cleanup_old_jobs_locked()
            return state

    def get_job(self, job_id: str) -> JobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest_job(self) -> JobState | None:
        with self._lock:
            if not self._jobs:
                return None
            return max(self._jobs.values(), key=lambda job: job.created_at)

    def add_log(self, job_id: str, level: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.logs.append(JobLogEntry(
                time=time.strftime("%Y-%m-%dT%H:%M:%S"),
                level=level,
                message=message,
            ))
            if len(job.logs) > self.max_logs:
                job.logs = job.logs[-self.max_logs:]

    def update_progress(self, job_id: str, **kwargs: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.progress.update(kwargs)
            if "message" in kwargs:
                job.message = str(kwargs["message"])

    def request_cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
                return False
            job.status = JobStatus.CANCEL_REQUESTED
            job.message = "正在请求取消"
            return True

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = JobStatus.RUNNING
            job.started_at = time.time()
            job.message = "任务运行中"

    def mark_succeeded(self, job_id: str, stats: dict[str, Any] | None = None) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = JobStatus.SUCCEEDED
            job.message = "任务完成"
            job.stats = stats or {}
            job.finished_at = time.time()

    def mark_failed(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = JobStatus.FAILED
            job.message = "任务失败"
            job.error = error
            job.finished_at = time.time()

    def mark_cancelled(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = JobStatus.CANCELLED
            job.message = "任务已取消"
            job.finished_at = time.time()

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.status == JobStatus.CANCEL_REQUESTED)

    def acquire_run_slot(self) -> bool:
        return self._semaphore.acquire(blocking=False)

    def release_run_slot(self) -> None:
        self._semaphore.release()

    def cleanup_old_jobs(self) -> None:
        with self._lock:
            self.cleanup_old_jobs_locked()

    def cleanup_old_jobs_locked(self) -> None:
        if len(self._jobs) <= self.max_jobs:
            return
        removable = [
            (job_id, job)
            for job_id, job in self._jobs.items()
            if job.status != JobStatus.RUNNING
        ]
        removable.sort(key=lambda item: item[1].finished_at or item[1].created_at, reverse=True)
        for job_id, _job in removable[self.max_jobs:]:
            self._jobs.pop(job_id, None)
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
pytest tests/test_jobs_manager.py -v
```

Expected: PASS.

- [ ] **Step 5: Run model and manager tests together**

Run:

```bash
pytest tests/test_jobs_models.py tests/test_jobs_manager.py -v
```

Expected: PASS.

---

### Task 3: Job Runner

**Files:**
- Create: `src/pixiv_novel_sync/jobs/runner.py`
- Create: `tests/test_jobs_runner.py`

- [ ] **Step 1: Write failing runner tests**

Create `tests/test_jobs_runner.py`:

```python
from __future__ import annotations

import pytest

from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus
from pixiv_novel_sync.jobs.runner import JobRunner


def test_runner_executes_tasks_and_merges_stats():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.CLI, task_types=["a", "b"]))
    calls: list[str] = []

    def executor(task_type, context):
        calls.append(task_type)
        return {"novels": 1, "task": task_type}

    runner = JobRunner(manager=manager, executor=executor)
    result = runner.run(state.job_id)

    assert result.status == JobStatus.SUCCEEDED
    assert calls == ["a", "b"]
    assert result.stats["novels"] == 2
    assert result.stats["task"] == "b"


def test_runner_marks_failed_on_exception():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.CLI, task_types=["a"]))

    def executor(task_type, context):
        raise RuntimeError("boom")

    runner = JobRunner(manager=manager, executor=executor)
    result = runner.run(state.job_id)

    assert result.status == JobStatus.FAILED
    assert result.error == "boom"
    assert any("boom" in entry.message for entry in result.logs)


def test_runner_stops_before_task_when_cancel_requested():
    manager = JobManager()
    state = manager.submit(JobSpec(source=JobSource.CLI, task_types=["a", "b"]))
    calls: list[str] = []

    def executor(task_type, context):
        calls.append(task_type)
        manager.request_cancel(state.job_id)
        return {"ran": 1}

    runner = JobRunner(manager=manager, executor=executor)
    result = runner.run(state.job_id)

    assert calls == ["a"]
    assert result.status == JobStatus.CANCELLED
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_jobs_runner.py -v
```

Expected: FAIL with missing `pixiv_novel_sync.jobs.runner`.

- [ ] **Step 3: Implement `JobRunner`**

Create `src/pixiv_novel_sync/jobs/runner.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .manager import JobManager
from .models import JobState, JobStatus

JobTaskExecutor = Callable[[str, dict[str, Any]], dict[str, Any] | None]


@dataclass(slots=True)
class JobRunner:
    manager: JobManager
    executor: JobTaskExecutor

    def run(self, job_id: str) -> JobState:
        job = self.manager.get_job(job_id)
        if job is None:
            raise KeyError(f"Unknown job_id: {job_id}")
        if not self.manager.acquire_run_slot():
            self.manager.mark_failed(job_id, "已有任务正在运行")
            return self.manager.get_job(job_id)  # type: ignore[return-value]
        try:
            self.manager.mark_running(job_id)
            total_stats: dict[str, Any] = {}
            for index, task_type in enumerate(job.task_types):
                if self.manager.is_cancel_requested(job_id):
                    self.manager.mark_cancelled(job_id)
                    return self.manager.get_job(job_id)  # type: ignore[return-value]
                self.manager.update_progress(
                    job_id,
                    current_task_index=index,
                    task_type=task_type,
                    message=f"正在执行: {task_type}",
                )
                self.manager.add_log(job_id, "info", f"开始任务: {task_type}")
                task_stats = self.executor(task_type, {"job_id": job_id, "job": job})
                if task_stats:
                    for key, value in task_stats.items():
                        if isinstance(value, (int, float)) and isinstance(total_stats.get(key), (int, float)):
                            total_stats[key] = total_stats.get(key, 0) + value
                        elif isinstance(value, (int, float)) and key not in total_stats:
                            total_stats[key] = value
                        else:
                            total_stats[key] = value
                self.manager.add_log(job_id, "success", f"完成任务: {task_type}")
            if self.manager.is_cancel_requested(job_id):
                self.manager.mark_cancelled(job_id)
            else:
                self.manager.mark_succeeded(job_id, total_stats)
            return self.manager.get_job(job_id)  # type: ignore[return-value]
        except Exception as exc:
            self.manager.add_log(job_id, "error", f"任务失败: {exc}")
            self.manager.mark_failed(job_id, str(exc))
            return self.manager.get_job(job_id)  # type: ignore[return-value]
        finally:
            self.manager.release_run_slot()
```

- [ ] **Step 4: Run runner tests and verify they pass**

Run:

```bash
pytest tests/test_jobs_runner.py -v
```

Expected: PASS.

- [ ] **Step 5: Run all job tests**

Run:

```bash
pytest tests/test_jobs_models.py tests/test_jobs_manager.py tests/test_jobs_runner.py -v
```

Expected: PASS.

---

### Task 4: Task Dispatch Adapter

**Files:**
- Create: `src/pixiv_novel_sync/jobs/tasks.py`
- Create: `tests/test_jobs_tasks.py`

- [ ] **Step 1: Write failing dispatch tests**

Create `tests/test_jobs_tasks.py`:

```python
from __future__ import annotations

import pytest

from pixiv_novel_sync.jobs.tasks import build_default_task_list, task_label


def test_build_default_task_list_uses_sync_settings():
    class Sync:
        sync_bookmarks = True
        sync_following_users = False
        sync_following_novels = True
        sync_subscribed_series = True

    class Settings:
        sync = Sync()

    assert build_default_task_list(Settings()) == ["bookmark", "following_novels", "subscribed_series"]


def test_task_label_for_known_and_unknown_tasks():
    assert task_label("bookmark") == "收藏小说"
    assert task_label("user_backup:123") == "用户 123 全量备份"
    assert task_label("custom") == "custom"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_jobs_tasks.py -v
```

Expected: FAIL with missing `pixiv_novel_sync.jobs.tasks`.

- [ ] **Step 3: Implement task helpers**

Create `src/pixiv_novel_sync/jobs/tasks.py`:

```python
from __future__ import annotations

from typing import Any

from ..settings import Settings

_TASK_LABELS = {
    "bookmark": "收藏小说",
    "following_users": "关注用户列表",
    "following_novels": "关注用户小说",
    "subscribed_series": "追更系列",
    "sync_check": "预检查",
    "user_status": "用户状态检查",
    "novel_status": "小说状态检查",
    "series_status": "系列状态检查",
    "pending_deletion_detection": "待删除检测",
    "user_backup": "用户全量备份",
}


def build_default_task_list(settings: Settings) -> list[str]:
    tasks: list[str] = []
    if settings.sync.sync_bookmarks:
        tasks.append("bookmark")
    if settings.sync.sync_following_users:
        tasks.append("following_users")
    if settings.sync.sync_following_novels:
        tasks.append("following_novels")
    if settings.sync.sync_subscribed_series:
        tasks.append("subscribed_series")
    return tasks


def task_label(task_type: str) -> str:
    if task_type.startswith("user_backup:"):
        return f"用户 {task_type.split(':', 1)[1]} 全量备份"
    return _TASK_LABELS.get(task_type, task_type)


def merge_stats(total: dict[str, Any], update: dict[str, Any] | None) -> dict[str, Any]:
    if not update:
        return total
    for key, value in update.items():
        if isinstance(value, (int, float)) and isinstance(total.get(key), (int, float)):
            total[key] = total.get(key, 0) + value
        elif isinstance(value, (int, float)) and key not in total:
            total[key] = value
        else:
            total[key] = value
    return total
```

- [ ] **Step 4: Run task helper tests**

Run:

```bash
pytest tests/test_jobs_tasks.py -v
```

Expected: PASS.

---

### Task 5: CLI JobSpec Commands

**Files:**
- Modify: `src/pixiv_novel_sync/cli.py`
- Create: `tests/test_cli_jobs.py`

- [ ] **Step 1: Write failing CLI parser tests**

Create `tests/test_cli_jobs.py`:

```python
from __future__ import annotations

from pixiv_novel_sync.cli import build_parser


def test_sync_command_accepts_multiple_tasks():
    parser = build_parser()
    args = parser.parse_args(["sync", "bookmark", "following_novels"])

    assert args.command == "sync"
    assert args.tasks == ["bookmark", "following_novels"]


def test_sync_check_command_exists():
    parser = build_parser()
    args = parser.parse_args(["sync-check"])

    assert args.command == "sync-check"


def test_status_check_command_accepts_scope():
    parser = build_parser()
    args = parser.parse_args(["status-check", "novel_status"])

    assert args.command == "status-check"
    assert args.tasks == ["novel_status"]
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_cli_jobs.py -v
```

Expected: FAIL because the commands do not exist.

- [ ] **Step 3: Add CLI commands to parser**

Modify `src/pixiv_novel_sync/cli.py` inside `build_parser()` after existing subparser setup:

```python
    sync_parser = subparsers.add_parser("sync", help="Run one or more sync tasks")
    sync_parser.add_argument(
        "tasks",
        nargs="*",
        default=None,
        help="Task names: bookmark following_users following_novels subscribed_series",
    )

    subparsers.add_parser("sync-check", help="Run pre-sync existence check")

    status_parser = subparsers.add_parser("status-check", help="Run one or more status check tasks")
    status_parser.add_argument(
        "tasks",
        nargs="*",
        default=["user_status", "novel_status", "series_status"],
        help="Status tasks: user_status novel_status series_status",
    )

    subparsers.add_parser("pending-deletion-detection", help="Detect locally archived novels pending deletion confirmation")

    backup_parser = subparsers.add_parser("user-backup", help="Backup all novels for a Pixiv user")
    backup_parser.add_argument("user_id", type=int, help="Pixiv user id")
```

- [ ] **Step 4: Run CLI parser tests**

Run:

```bash
pytest tests/test_cli_jobs.py -v
```

Expected: PASS.

- [ ] **Step 5: Add CLI smoke verification**

Run:

```bash
python -m pixiv_novel_sync.cli --help
python -m pixiv_novel_sync.cli sync --help
```

Expected: both commands exit 0 and show help text.

---

### Task 6: CLI Execution Through JobRunner

**Files:**
- Modify: `src/pixiv_novel_sync/cli.py`
- Modify: `src/pixiv_novel_sync/jobs/tasks.py`
- Create: `tests/test_cli_jobs.py` additions

- [ ] **Step 1: Add test for creating job spec from CLI args**

Append to `tests/test_cli_jobs.py`:

```python
from pixiv_novel_sync.cli import build_job_spec_from_args
from pixiv_novel_sync.jobs.models import JobSource, JobType


def test_build_job_spec_for_sync_command():
    parser = build_parser()
    args = parser.parse_args(["sync", "bookmark"])

    spec = build_job_spec_from_args(args)

    assert spec.source == JobSource.CLI
    assert spec.job_type == JobType.SYNC
    assert spec.task_types == ["bookmark"]


def test_build_job_spec_for_user_backup_command():
    parser = build_parser()
    args = parser.parse_args(["user-backup", "123"])

    spec = build_job_spec_from_args(args)

    assert spec.job_type == JobType.USER_BACKUP
    assert spec.task_types == ["user_backup:123"]
    assert spec.params["user_id"] == 123
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_cli_jobs.py -v
```

Expected: FAIL with missing `build_job_spec_from_args`.

- [ ] **Step 3: Implement `build_job_spec_from_args`**

Modify `src/pixiv_novel_sync/cli.py` imports:

```python
from .jobs.models import JobSource, JobSpec, JobType
```

Add below `build_parser()`:

```python
def build_job_spec_from_args(args: argparse.Namespace) -> JobSpec:
    if args.command == "sync":
        return JobSpec(
            source=JobSource.CLI,
            job_type=JobType.SYNC,
            task_types=list(args.tasks or []),
        )
    if args.command == "sync-check":
        return JobSpec(
            source=JobSource.CLI,
            job_type=JobType.SYNC_CHECK,
            task_types=["sync_check"],
        )
    if args.command == "status-check":
        return JobSpec(
            source=JobSource.CLI,
            job_type=JobType.STATUS_CHECK,
            task_types=list(args.tasks or ["user_status", "novel_status", "series_status"]),
        )
    if args.command == "pending-deletion-detection":
        return JobSpec(
            source=JobSource.CLI,
            job_type=JobType.PENDING_DELETION_DETECTION,
            task_types=["pending_deletion_detection"],
        )
    if args.command == "user-backup":
        return JobSpec(
            source=JobSource.CLI,
            job_type=JobType.USER_BACKUP,
            task_types=[f"user_backup:{args.user_id}"],
            params={"user_id": args.user_id},
        )
    raise ValueError(f"Unsupported job command: {args.command}")
```

- [ ] **Step 4: Add CLI execution helper**

Add to `src/pixiv_novel_sync/cli.py`:

```python
def run_job_command(args: argparse.Namespace, settings) -> int:
    from .jobs.manager import JobManager
    from .jobs.runner import JobRunner
    from .jobs.tasks import execute_task

    manager = JobManager()
    spec = build_job_spec_from_args(args)
    if spec.job_type == JobType.SYNC and not spec.task_types:
        from .jobs.tasks import build_default_task_list
        spec.task_types = build_default_task_list(settings)
    state = manager.submit(spec)

    def executor(task_type: str, context: dict):
        return execute_task(task_type, settings=settings, manager=manager, job_id=context["job_id"])

    result = JobRunner(manager=manager, executor=executor).run(state.job_id)
    print(json.dumps({
        "job_id": result.job_id,
        "status": result.status,
        "message": result.message,
        "stats": result.stats,
        "error": result.error,
    }, ensure_ascii=False, indent=2))
    return 0 if result.status == "succeeded" else 1
```

Update `main()` after existing commands:

```python
    elif args.command in {"sync", "sync-check", "status-check", "pending-deletion-detection", "user-backup"}:
        raise SystemExit(run_job_command(args, settings))
```

- [ ] **Step 5: Implement placeholder `execute_task` dispatch that calls existing bookmark sync for bookmark and raises for unsupported**

Append to `src/pixiv_novel_sync/jobs/tasks.py`:

```python
def execute_task(task_type: str, *, settings: Settings, manager: Any, job_id: str) -> dict[str, Any] | None:
    if task_type == "bookmark":
        from .quick_sync import run_bookmark_sync
        return run_bookmark_sync(settings)
    raise RuntimeError(f"Unsupported CLI job task: {task_type}")
```

This keeps the first execution path minimal. Later tasks fill out more task types.

- [ ] **Step 6: Run CLI tests**

Run:

```bash
pytest tests/test_cli_jobs.py -v
```

Expected: PASS.

---

### Task 7: Web Security Defaults

**Files:**
- Modify: `src/pixiv_novel_sync/webapp.py`
- Create: `tests/test_webapp_security.py`

- [ ] **Step 1: Write failing security tests**

Create `tests/test_webapp_security.py`:

```python
from __future__ import annotations

from pixiv_novel_sync.webapp import create_app


def test_no_dashboard_token_allows_localhost(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path))
    client = app.test_client()

    response = client.get("/api/health", environ_base={"REMOTE_ADDR": "127.0.0.1"})

    assert response.status_code == 200


def test_no_dashboard_token_blocks_non_localhost(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path))
    client = app.test_client()

    response = client.get("/dashboard", environ_base={"REMOTE_ADDR": "203.0.113.10"})

    assert response.status_code == 403
```

- [ ] **Step 2: Run tests and verify second test fails**

Run:

```bash
pytest tests/test_webapp_security.py -v
```

Expected: FAIL because non-localhost is currently allowed when no token exists.

- [ ] **Step 3: Add localhost helper and tighten `_check_auth`**

Modify `src/pixiv_novel_sync/webapp.py` near `create_app()` local helpers:

```python
    def _is_local_request() -> bool:
        remote_addr = request.remote_addr or ""
        return remote_addr in {"127.0.0.1", "::1", "localhost"}
```

Modify `_check_auth()` token branch:

```python
        if not token:
            if _is_local_request():
                return
            return jsonify({"error": "dashboard token required for non-local access"}), 403
```

- [ ] **Step 4: Run security tests**

Run:

```bash
pytest tests/test_webapp_security.py -v
```

Expected: PASS.

---

### Task 8: Token Response Redaction

**Files:**
- Modify: `src/pixiv_novel_sync/webapp.py`
- Modify: `tests/test_webapp_security.py`

- [ ] **Step 1: Add failing unit test for token payload redaction helper**

Append to `tests/test_webapp_security.py`:

```python
from types import SimpleNamespace

from pixiv_novel_sync.webapp import _oauth_task_public_payload


def test_oauth_task_public_payload_redacts_tokens():
    task = SimpleNamespace(
        task_id="task-1",
        status="done",
        message="ok",
        refresh_token="secret-refresh",
        access_token="secret-access",
        user_id=123,
    )

    payload = _oauth_task_public_payload(task, mode="oauth")

    assert payload["task_id"] == "task-1"
    assert payload["has_refresh_token"] is True
    assert payload["has_access_token"] is True
    assert "refresh_token" not in payload
    assert "access_token" not in payload
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
pytest tests/test_webapp_security.py::test_oauth_task_public_payload_redacts_tokens -v
```

Expected: FAIL because `_oauth_task_public_payload` does not exist.

- [ ] **Step 3: Add helper**

Add near top-level helpers in `src/pixiv_novel_sync/webapp.py`:

```python
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
```

- [ ] **Step 4: Replace OAuth JSON responses**

In `src/pixiv_novel_sync/webapp.py`, replace response dicts that include `refresh_token` or `access_token` for OAuth task status with:

```python
return jsonify(_oauth_task_public_payload(task, mode="oauth"))
```

For callback/manual exchange responses that currently return `refresh_token`, return:

```python
return jsonify({
    "ok": True,
    "message": task.message,
    "user_id": task.user_id,
    "has_refresh_token": bool(task.refresh_token),
})
```

Keep explicit save-token request body handling unchanged.

- [ ] **Step 5: Run focused security tests**

Run:

```bash
pytest tests/test_webapp_security.py -v
```

Expected: PASS.

---

### Task 9: Persistent Flask Secret Fallback

**Files:**
- Modify: `src/pixiv_novel_sync/webapp.py`
- Modify: `tests/test_webapp_security.py`

- [ ] **Step 1: Add failing test for stable random secret file**

Append to `tests/test_webapp_security.py`:

```python

def test_flask_secret_fallback_persists_to_env_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")

    app1 = create_app(env_path=str(env_path))
    app2 = create_app(env_path=str(env_path))

    assert app1.secret_key == app2.secret_key
    assert len(app1.secret_key) >= 32
    assert (tmp_path / ".pixiv_novel_sync_flask_secret").exists()
```

- [ ] **Step 2: Run test and verify it fails or current fallback does not create file**

Run:

```bash
pytest tests/test_webapp_security.py::test_flask_secret_fallback_persists_to_env_dir -v
```

Expected: FAIL because no secret file is created.

- [ ] **Step 3: Add persistent secret helper**

Add in `src/pixiv_novel_sync/webapp.py` top-level helpers:

```python
def _load_or_create_flask_secret(env_path: str | None, config_path: str | None) -> str:
    env_secret = os.getenv("PIXIV_FLASK_SECRET")
    if env_secret:
        return env_secret
    base_dir = Path(env_path).parent if env_path else Path(config_path).parent if config_path else Path(".")
    secret_path = base_dir / ".pixiv_novel_sync_flask_secret"
    try:
        existing = secret_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    secret = os.urandom(32).hex()
    secret_path.write_text(secret + "\n", encoding="utf-8")
    return secret
```

- [ ] **Step 4: Use helper in `create_app`**

Replace the current secret derivation block with:

```python
    app.secret_key = _load_or_create_flask_secret(env_path, config_path)
```

Keep cookie config below it unchanged.

- [ ] **Step 5: Run security tests**

Run:

```bash
pytest tests/test_webapp_security.py -v
```

Expected: PASS.

---

### Task 10: Avoid Empty-Index Embedding API Calls

**Files:**
- Modify: `src/pixiv_novel_sync/ai/retrieval.py`
- Modify: `tests/test_ai_retrieval.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_ai_retrieval.py`:

```python

def test_api_embedding_retriever_search_empty_index_does_not_call_api(tmp_path, monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(json)
        return FakeEmbeddingResponse({"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    monkeypatch.setattr(retrieval.http_requests, "post", fake_post)
    retriever = APIEmbeddingRetriever(
        tmp_path / "main.db",
        base_url="https://api.example.test/v1",
        api_key="test-key",
        model_name="Qwen3-Embedding-8B",
    )

    assert retriever.search(1, "secret") == []
    assert calls == []
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
pytest tests/test_ai_retrieval.py::test_api_embedding_retriever_search_empty_index_does_not_call_api -v
```

Expected: FAIL because current search calls embedding API before checking rows.

- [ ] **Step 3: Move row query before query embedding**

Modify `APIEmbeddingRetriever.search` in `src/pixiv_novel_sync/ai/retrieval.py`:

```python
    def search(self, project_id: int, query: str, top_k: int = 5) -> list[RetrievalEntry]:
        if not query.strip():
            return []
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT project_id, chapter_number, content, entry_type, embedding_blob, embedding_json, dimension
                FROM retrieval_api_vectors
                WHERE project_id = ? AND model = ?
                """,
                (project_id, self.model_name),
            ).fetchall()
        if not rows:
            return []
        query_embedding = self.client.embed([query])[0]
        results: list[RetrievalEntry] = []
        for row in rows:
            doc_embedding = _decode_float32_vector(row[4], int(row[6])) if row[4] is not None else [float(value) for value in json.loads(row[5])]
            score = _cosine_similarity(query_embedding, doc_embedding)
            if score > 0.25:
                results.append(RetrievalEntry(
                    project_id=row[0],
                    chapter_number=row[1],
                    content=row[2],
                    entry_type=row[3],
                    score=score,
                ))
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:top_k]
```

- [ ] **Step 4: Run focused AI retrieval tests**

Run:

```bash
pytest tests/test_ai_retrieval.py -v
```

Expected: PASS.

---

### Task 11: Web JobSpec Adapter

**Files:**
- Modify: `src/pixiv_novel_sync/webapp.py`
- Modify: `tests/test_webapp_settings.py` or create `tests/test_webapp_jobs.py`

- [ ] **Step 1: Add tests for Web job spec helper**

Create `tests/test_webapp_jobs.py`:

```python
from __future__ import annotations

from pixiv_novel_sync.jobs.models import JobSource, JobType
from pixiv_novel_sync.webapp import _web_job_spec


def test_web_job_spec_for_sync_tasks():
    spec = _web_job_spec(["bookmark", "following_novels"])

    assert spec.source == JobSource.WEB
    assert spec.job_type == JobType.SYNC
    assert spec.task_types == ["bookmark", "following_novels"]


def test_web_job_spec_for_user_backup():
    spec = _web_job_spec(["user_backup:123"])

    assert spec.source == JobSource.WEB
    assert spec.job_type == JobType.USER_BACKUP
    assert spec.params["user_id"] == 123
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_webapp_jobs.py -v
```

Expected: FAIL because `_web_job_spec` does not exist.

- [ ] **Step 3: Add `_web_job_spec` helper**

Add top-level helper in `src/pixiv_novel_sync/webapp.py`:

```python
def _web_job_spec(task_list: list[str] | None) -> JobSpec:
    from .jobs.models import JobSource, JobSpec, JobType

    tasks = list(task_list or [])
    if len(tasks) == 1 and tasks[0].startswith("user_backup:"):
        user_id = int(tasks[0].split(":", 1)[1])
        return JobSpec(
            source=JobSource.WEB,
            job_type=JobType.USER_BACKUP,
            task_types=tasks,
            params={"user_id": user_id},
        )
    if tasks == ["sync_check"]:
        return JobSpec(source=JobSource.WEB, job_type=JobType.SYNC_CHECK, task_types=tasks)
    return JobSpec(source=JobSource.WEB, job_type=JobType.SYNC, task_types=tasks)
```

Also add the import used by type checking only if needed:

```python
from .jobs.models import JobSpec
```

- [ ] **Step 4: Run Web job helper tests**

Run:

```bash
pytest tests/test_webapp_jobs.py -v
```

Expected: PASS.

---

### Task 12: Adapt Existing SyncJobManager Toward Shared Models

**Files:**
- Modify: `src/pixiv_novel_sync/webapp.py`
- Modify: `tests/test_webapp_jobs.py`

- [ ] **Step 1: Add regression test for SyncJobManager storing JobSpec metadata**

Append to `tests/test_webapp_jobs.py`:

```python
from pixiv_novel_sync.webapp import SyncJobManager


def test_sync_job_manager_start_job_records_job_spec(tmp_path):
    manager = SyncJobManager(config_path=None, env_path=None)
    spec = _web_job_spec(["bookmark"])

    job = manager.start_job(spec.task_types)

    assert job.task_list == ["bookmark"]
```

This test documents existing compatibility. It should pass before and after this task.

- [ ] **Step 2: Run regression test**

Run:

```bash
pytest tests/test_webapp_jobs.py::test_sync_job_manager_start_job_records_job_spec -v
```

Expected: PASS.

- [ ] **Step 3: Use `_web_job_spec` at Web route call sites**

In Web routes that currently call:

```python
job = sync_job_manager.start_job([...])
```

change to:

```python
spec = _web_job_spec([...])
job = sync_job_manager.start_job(spec.task_types)
```

For user backup:

```python
spec = _web_job_spec([f"user_backup:{user_id}"])
job = sync_job_manager.start_job(spec.task_types)
```

Keep `SyncJobManager` internals unchanged in this task. The goal is to introduce shared spec semantics without destabilizing running Web behavior.

- [ ] **Step 4: Run Web job tests**

Run:

```bash
pytest tests/test_webapp_jobs.py -v
```

Expected: PASS.

---

### Task 13: Fill CLI Task Dispatch for Core Tasks

**Files:**
- Modify: `src/pixiv_novel_sync/jobs/tasks.py`
- Modify: `src/pixiv_novel_sync/jobs/quick_sync.py`
- Modify: `tests/test_cli_jobs.py`

- [ ] **Step 1: Add fake dispatch tests**

Append to `tests/test_jobs_tasks.py`:

```python
from pixiv_novel_sync.jobs import tasks as job_tasks


def test_execute_task_rejects_unknown_task(monkeypatch):
    class Settings:
        pass

    class Manager:
        def add_log(self, job_id, level, message):
            pass

    try:
        job_tasks.execute_task("unknown", settings=Settings(), manager=Manager(), job_id="job")
    except RuntimeError as exc:
        assert "Unsupported CLI job task" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
```

- [ ] **Step 2: Run tests**

Run:

```bash
pytest tests/test_jobs_tasks.py -v
```

Expected: PASS if Task 6 already added `execute_task`.

- [ ] **Step 3: Extend `execute_task` for supported task names**

Modify `src/pixiv_novel_sync/jobs/tasks.py`:

```python
def execute_task(task_type: str, *, settings: Settings, manager: Any, job_id: str) -> dict[str, Any] | None:
    from .quick_sync import run_bookmark_sync, run_check_bookmarks_task

    if task_type == "bookmark":
        return run_bookmark_sync(settings)
    if task_type == "sync_check":
        return run_check_bookmarks_task(settings, manager, job_id)
    if task_type in {"following_users", "following_novels", "subscribed_series"}:
        from ..webapp import SyncJobManager
        temp_manager = SyncJobManager(config_path=None, env_path=None)
        temp_job = temp_manager.start_job([task_type])
        while temp_job.status == "running":
            import time
            time.sleep(0.1)
        if temp_job.status != "succeeded":
            raise RuntimeError(temp_job.error or temp_job.message)
        return temp_job.stats
    if task_type.startswith("user_backup:"):
        raise RuntimeError("user_backup CLI execution will be wired in the next implementation phase")
    raise RuntimeError(f"Unsupported CLI job task: {task_type}")
```

This is intentionally conservative and may be replaced by direct service calls in a later plan. Do not attempt broad `sync_engine.py` extraction in this task.

- [ ] **Step 4: Run job task tests**

Run:

```bash
pytest tests/test_jobs_tasks.py -v
```

Expected: PASS.

---

### Task 14: Existing Test Suite and CLI Smoke

**Files:**
- No source changes unless tests reveal a failure.

- [ ] **Step 1: Run focused new tests**

Run:

```bash
pytest tests/test_jobs_models.py tests/test_jobs_manager.py tests/test_jobs_runner.py tests/test_jobs_tasks.py tests/test_cli_jobs.py tests/test_webapp_jobs.py tests/test_webapp_security.py -v
```

Expected: PASS.

- [ ] **Step 2: Run existing AI/security regression tests**

Run:

```bash
pytest tests/test_ai_retrieval.py tests/test_oauth_helper.py tests/test_playwright_login.py tests/test_webapp_settings.py -v
```

Expected: PASS.

- [ ] **Step 3: Run full suite**

Run:

```bash
pytest
```

Expected: all tests PASS.

- [ ] **Step 4: Run CLI smoke tests**

Run:

```bash
python -m pixiv_novel_sync.cli --help
python -m pixiv_novel_sync.cli sync --help
python -m pixiv_novel_sync.cli sync-check --help
python -m pixiv_novel_sync.cli status-check --help
```

Expected: all commands exit 0 and display help.

---

## Self-Review

Spec coverage:

- Unified `JobSpec` / `JobRunner`: Tasks 1-4, 11-12.
- CLI/systemd-capable commands: Tasks 5-6, 13-14.
- Security defaults: Tasks 7-9.
- Token redaction: Task 8.
- Persistent Flask secret: Task 9.
- Low-risk embedding no-op optimization: Task 10.
- Testing and verification: Tasks 1-14.

No placeholders are intentionally left. The plan deliberately avoids full `webapp.py` / `sync_engine.py` extraction in this first pass to match the approved gradual design.

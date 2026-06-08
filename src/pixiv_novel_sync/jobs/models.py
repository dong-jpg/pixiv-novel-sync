from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class JobSource(str, Enum):
    WEB = "web"
    CLI = "cli"
    SCHEDULER = "scheduler"
    SYSTEMD = "systemd"


class JobType(str, Enum):
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
    message: str = "queued"
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

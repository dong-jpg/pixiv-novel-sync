from __future__ import annotations

from collections.abc import Callable
from typing import Any


class JobReporter:
    def __init__(self, manager: Any = None, job_id: str | None = None) -> None:
        self.manager = manager
        self.job_id = str(job_id) if job_id else None

    def add_log(self, level: str, message: str) -> None:
        if self.manager is None or not self.job_id or not hasattr(self.manager, "add_log"):
            return
        self.manager.add_log(self.job_id, level, message)

    def update_progress(self, **kwargs: Any) -> None:
        if self.manager is None or not self.job_id or not hasattr(self.manager, "update_progress"):
            return
        self.manager.update_progress(self.job_id, **kwargs)


StopRequested = Callable[[], bool]


def run_user_backup_task(
    settings: Any,
    user_id: int,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
) -> dict[str, Any]:
    return {}


def run_user_status_task(
    settings: Any,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
) -> dict[str, Any]:
    return {}


def run_novel_status_task(
    settings: Any,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
) -> dict[str, Any]:
    return {}


def run_series_status_task(
    settings: Any,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
) -> dict[str, Any]:
    return {}


def run_pending_deletion_detection_task(
    settings: Any,
    reporter: JobReporter | None = None,
    stop_requested: StopRequested | None = None,
) -> dict[str, Any]:
    return {}

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .auth import PixivAuthManager
from .jobs.manager import JobManager
from .jobs.models import JobSource, JobSpec, JobStatus, JobType
from .jobs.runner import JobRunner
from .jobs.tasks import build_default_task_list, execute_task
from .logging_utils import configure_logging
from .settings import load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pixiv-novel-sync")
    parser.add_argument("--config", default="config/config.yaml", help="Path to config YAML")
    parser.add_argument("--env-file", default=None, help="Path to .env file")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("auth-check", help="Validate Pixiv auth settings")
    subparsers.add_parser("sync-bookmarks", help="Sync bookmarked novels")
    subparsers.add_parser("db-stats", help="Show database statistics")

    sync_parser = subparsers.add_parser("sync", help="Build sync job specs")
    sync_parser.add_argument("tasks", nargs="*", default=None)
    subparsers.add_parser("sync-check", help="Build sync check job specs")
    status_check_parser = subparsers.add_parser("status-check", help="Build status check job specs")
    status_check_parser.set_defaults(tasks=None)
    status_check_parser.add_argument(
        "tasks",
        nargs="*",
        default=argparse.SUPPRESS,
    )
    subparsers.add_parser("pending-deletion-detection", help="Build pending deletion detection job spec")
    user_backup_parser = subparsers.add_parser("user-backup", help="Build user backup job spec")
    user_backup_parser.add_argument("user_id", type=int)

    web_parser = subparsers.add_parser("web-token-ui", help="Start local web UI for acquiring Pixiv refresh_token")
    web_parser.add_argument("--host", default="127.0.0.1", help="Bind host for token UI")
    web_parser.add_argument("--port", default=5010, type=int, help="Bind port for token UI")
    return parser


def build_job_spec_from_args(args: argparse.Namespace) -> JobSpec:
    if args.command == "sync":
        return JobSpec(source=JobSource.CLI, job_type=JobType.SYNC, task_types=list(args.tasks or []))
    if args.command == "sync-check":
        return JobSpec(source=JobSource.CLI, job_type=JobType.SYNC_CHECK, task_types=["sync_check"])
    if args.command == "status-check":
        tasks = args.tasks if args.tasks is not None else ["user_status", "novel_status", "series_status"]
        return JobSpec(source=JobSource.CLI, job_type=JobType.STATUS_CHECK, task_types=list(tasks))
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
    raise RuntimeError(f"Unsupported job command: {args.command}")


def run_job_command(args: argparse.Namespace, settings: object) -> int:
    spec = build_job_spec_from_args(args)
    if spec.job_type == JobType.SYNC and not spec.task_types:
        spec.task_types = build_default_task_list(settings)

    manager = JobManager()
    state = manager.submit(spec)
    runner = JobRunner(manager=manager, executor=lambda task_type, context: execute_task(task_type, settings, context))
    result = runner.run(state.job_id)

    output = {
        "job_id": result.job_id,
        "status": result.status.value,
        "message": result.message,
        "stats": result.stats,
        "error": result.error,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if result.status == JobStatus.SUCCEEDED else 1


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    settings = load_settings(config_path=args.config, env_path=args.env_file)
    configure_logging(settings.log_level)

    if args.command == "auth-check":
        run_auth_check(args.config, args.env_file)
    elif args.command == "sync-bookmarks":
        from .jobs.quick_sync import run_bookmark_sync

        run_bookmark_sync(settings)
    elif args.command == "db-stats":
        from .storage_db import Database

        db = Database(settings.storage.db_path)
        db.init_schema()
        try:
            print(db.export_stats())
        finally:
            db.close()
    elif args.command == "web-token-ui":
        from .webapp import create_app

        app = create_app(config_path=args.config, env_path=args.env_file)
        app.run(host=args.host, port=args.port, debug=False, threaded=True)
    elif args.command in {
        "sync",
        "sync-check",
        "status-check",
        "pending-deletion-detection",
        "user-backup",
    }:
        raise SystemExit(run_job_command(args, settings))
    else:
        parser.error(f"Unsupported command: {args.command}")


def run_auth_check(config_path: str | Path, env_path: str | Path | None) -> None:
    settings = load_settings(config_path=config_path, env_path=env_path)
    auth = PixivAuthManager(settings.pixiv)
    _, result = auth.login()

    output = {
        "user_id": result.user_id,
        "has_access_token": bool(result.access_token),
        "has_refresh_token": bool(result.refresh_token),
    }
    logging.getLogger(__name__).info("Auth check passed")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

from __future__ import annotations

import json

from pixiv_novel_sync.cli import build_job_spec_from_args, build_parser, run_job_command
from pixiv_novel_sync.jobs.models import JobSource, JobType


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


def test_status_check_without_tasks_defaults_to_none():
    parser = build_parser()
    args = parser.parse_args(["status-check"])

    assert args.command == "status-check"
    assert args.tasks is None


def test_build_job_spec_for_sync_command():
    parser = build_parser()
    args = parser.parse_args(["sync", "bookmark"])

    spec = build_job_spec_from_args(args)

    assert spec.source == JobSource.CLI
    assert spec.job_type == JobType.SYNC
    assert spec.task_types == ["bookmark"]


def test_build_job_spec_for_status_check_defaults():
    parser = build_parser()
    args = parser.parse_args(["status-check"])

    spec = build_job_spec_from_args(args)

    assert spec.job_type == JobType.STATUS_CHECK
    assert spec.task_types == ["user_status", "novel_status", "series_status"]


def test_build_job_spec_for_user_backup_command():
    parser = build_parser()
    args = parser.parse_args(["user-backup", "123"])

    spec = build_job_spec_from_args(args)

    assert spec.job_type == JobType.USER_BACKUP
    assert spec.task_types == ["user_backup:123"]
    assert spec.params["user_id"] == 123


def test_existing_commands_are_still_registered():
    parser = build_parser()

    for command in ["auth-check", "sync-bookmarks", "db-stats", "web-token-ui"]:
        args = parser.parse_args([command])
        assert args.command == command


def test_run_job_command_returns_success_and_json_output(monkeypatch, capsys):
    def fake_execute_task(task_type, settings, context):
        assert task_type == "bookmark"
        assert settings is fake_settings
        assert context["job_id"]
        return {"novels": 3}

    monkeypatch.setattr("pixiv_novel_sync.cli.execute_task", fake_execute_task)
    fake_settings = object()
    parser = build_parser()
    args = parser.parse_args(["sync", "bookmark"])

    exit_code = run_job_command(args, fake_settings)

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "succeeded"
    assert output["stats"] == {"novels": 3}
    assert output["error"] is None

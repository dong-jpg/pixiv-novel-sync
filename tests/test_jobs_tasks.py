from __future__ import annotations

import pytest

from pixiv_novel_sync.jobs.tasks import build_default_task_list, execute_task, merge_stats, task_label


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


def test_merge_stats_adds_numbers_and_overwrites_non_numbers():
    total = {"novels": 1, "status": "old"}

    result = merge_stats(total, {"novels": 2, "status": "new", "failed": 1})

    assert result is total
    assert result == {"novels": 3, "status": "new", "failed": 1}


def test_merge_stats_does_not_add_booleans():
    total = {"ok": True}

    result = merge_stats(total, {"ok": True})

    assert result["ok"] is True


def test_execute_task_dispatches_bookmark(monkeypatch):
    calls = []

    def fake_run_bookmark_sync(settings):
        calls.append(settings)
        return {"novels": 1}

    monkeypatch.setattr("pixiv_novel_sync.jobs.quick_sync.run_bookmark_sync", fake_run_bookmark_sync)
    settings = object()

    result = execute_task("bookmark", settings)

    assert result == {"novels": 1}
    assert calls == [settings]


def test_execute_task_dispatches_sync_check_without_releasing_runner_slot(monkeypatch):
    calls = []

    def fake_run_check_bookmarks_task(settings, manager, job_id, release_semaphore=True, raise_on_error=False):
        calls.append((settings, manager, job_id, release_semaphore, raise_on_error))
        return {"total_checked": 2}

    monkeypatch.setattr("pixiv_novel_sync.jobs.quick_sync.run_check_bookmarks_task", fake_run_check_bookmarks_task)
    settings = object()
    manager = object()

    result = execute_task("sync_check", settings, {"manager": manager, "job_id": "job-1"})

    assert result == {"total_checked": 2}
    assert calls == [(settings, manager, "job-1", False, True)]


def test_execute_task_propagates_sync_check_failure(monkeypatch):
    def fake_run_check_bookmarks_task(settings, manager, job_id, release_semaphore=True, raise_on_error=False):
        assert release_semaphore is False
        assert raise_on_error is True
        raise RuntimeError("sync check failed")

    monkeypatch.setattr("pixiv_novel_sync.jobs.quick_sync.run_check_bookmarks_task", fake_run_check_bookmarks_task)

    with pytest.raises(RuntimeError, match="sync check failed"):
        execute_task("sync_check", object(), {"manager": object(), "job_id": "job-1"})


def test_execute_task_rejects_unknown_task_with_clear_error():
    with pytest.raises(RuntimeError, match="Unsupported task type for CLI execution: custom_task"):
        execute_task("custom_task", object())

# Job Cancellation Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close remaining cancellability gaps in job execution paths found during the 2026-06-26 quality pass.

**Architecture:** Keep cancellation propagation at the job/task boundary and expose optional `stop_requested` callbacks to concrete job implementations. Long service sleeps should use one shared helper that polls cancellation in small intervals.

**Tech Stack:** Python 3, pytest, existing `pixiv_novel_sync.jobs` modules.

## Global Constraints

- Do not SSH or deploy; the user will update the server manually.
- Do not revert unrelated existing changes in the dirty worktree.
- Use TDD for behavior changes.
- Keep changes scoped to job cancellation and the optimization review document.

---

### Task 1: Propagate Cancellation Into Quick Sync Tasks

**Files:**
- Modify: `tests/test_jobs_tasks.py`
- Modify: `src/pixiv_novel_sync/jobs/tasks.py`
- Modify: `src/pixiv_novel_sync/jobs/quick_sync.py`

**Interfaces:**
- Consumes: `_stop_requested_from_context(context: dict[str, Any]) -> Callable[[], bool]`
- Produces: `run_bookmark_sync(settings: Settings, stop_requested: Callable[[], bool] | None = None) -> dict[str, int]`
- Produces: `run_check_bookmarks_task(..., stop_requested: Callable[[], bool] | None = None) -> dict[str, Any] | None`

- [x] **Step 1: Write failing dispatch tests**

```python
def test_execute_task_dispatches_bookmark_with_stop_requested(monkeypatch):
    calls = []

    def fake_run_bookmark_sync(settings, stop_requested=None):
        calls.append((settings, stop_requested))
        return {"novels": 1}

    monkeypatch.setattr("pixiv_novel_sync.jobs.quick_sync.run_bookmark_sync", fake_run_bookmark_sync)
    manager = object()
    settings = object()

    result = execute_task("bookmark", settings, {"manager": manager, "job_id": "job-1"})

    assert result == {"novels": 1}
    assert calls[0][0] is settings
    assert calls[0][1] is not None
```

- [x] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_jobs_tasks.py::test_execute_task_dispatches_bookmark_with_stop_requested -q`

Expected: FAIL because `execute_task("bookmark")` does not pass `stop_requested`.

- [x] **Step 3: Implement minimal dispatch changes**

```python
return run_bookmark_sync(settings, stop_requested=stop_requested)
```

and pass `stop_requested=stop_requested` into `run_check_bookmarks_task(...)`.

- [x] **Step 4: Add quick sync cancellation tests**

Add tests that monkeypatch auth/database/storage/service objects and assert:
- `run_bookmark_sync(..., stop_requested=lambda: True)` raises `InterruptedError` before login.
- `run_check_bookmarks_task(..., stop_requested=lambda: True, raise_on_error=True)` raises `InterruptedError` before login.
- Progress callbacks in both quick sync functions raise `InterruptedError` when cancellation is requested.

- [x] **Step 5: Implement quick sync cancellation**

Add optional `stop_requested` parameters, pre-login checks, and progress callbacks that raise `InterruptedError("Task stopped by user")`.

- [x] **Step 6: Verify focused tests**

Run: `python -m pytest tests/test_jobs_tasks.py tests/test_jobs_quick_sync.py -q`

Expected: PASS.

### Task 2: Make Service-Level Sleeps Cancellable

**Files:**
- Modify: `tests/test_jobs_services.py`
- Modify: `src/pixiv_novel_sync/jobs/services.py`

**Interfaces:**
- Produces: `_sleep_with_cancel(seconds: float, stop_requested: StopRequested | None, interval: float = 0.2) -> bool`

- [x] **Step 1: Write failing helper tests**

```python
def test_sleep_with_cancel_returns_true_when_cancel_requested(monkeypatch):
    calls = iter([False, True])
    slept = []
    monkeypatch.setattr(services.time, "sleep", lambda seconds: slept.append(seconds))

    assert services._sleep_with_cancel(1.0, lambda: next(calls), interval=0.25) is True
    assert slept == [0.25]
```

- [x] **Step 2: Run test to verify failure**

Run: `python -m pytest tests/test_jobs_services.py::test_sleep_with_cancel_returns_true_when_cancel_requested -q`

Expected: FAIL because helper is missing.

- [x] **Step 3: Implement helper and replace service sleeps**

Use `_sleep_with_cancel()` after user-backup page delay and status-check item delay. When it returns `True`, mark the task as stopped and exit the loop.

- [x] **Step 4: Verify focused service tests**

Run: `python -m pytest tests/test_jobs_services.py -q`

Expected: PASS.

### Task 3: Update Review Documentation And Verify

**Files:**
- Modify: `docs/OPTIMIZATION_REVIEW_2026-06-26.md`

**Interfaces:**
- Consumes: `rg -n "def _settings_to_dict|_settings_to_dict" src tests`
- Produces: Updated status notes for completed and remaining optimization areas.

- [x] **Step 1: Confirm settings serializer status**

Run: `rg -n "def _settings_to_dict|_settings_to_dict" src tests`

Expected: one production definition in `web/utils.py` and imports/usages elsewhere.

- [x] **Step 2: Update optimization review**

Mark settings serialization dedupe as complete and cancellation quick-sync/service-sleep improvements as advanced.

- [x] **Step 3: Full verification**

Run:
- `git diff --check`
- `python -m pytest -q`

Expected: both pass.

> **2026-06-30 扩展说明：** 经用户确认，本次在原 plan 范围外额外完成：(1) 实现 `RateLimiter.wait()`/`handle_response()` 的 `stop_requested`+`interval` 可取消参数并接通 `sync_engine.py` 5 处调用点（闭合 `tests/test_rate_limiter.py`）；(2) 修复定时自动同步路径（`web/managers.py`）把 `InterruptedError` 吞成 failed 的 Bug，改为标记 cancelled；(3) `run_check_bookmarks_task` 补 `except InterruptedError: raise`。`python -m pytest -q` 全量 209 项通过。

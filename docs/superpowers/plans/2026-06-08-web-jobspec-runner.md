# Web JobSpec Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route Web manual dashboard sync jobs through the shared `JobSpec` / `JobManager` / `JobRunner` pipeline while preserving the existing dashboard API response shape.

**Architecture:** Add a Web adapter in `webapp.py` that builds `JobSpec(source=WEB)` from current sync settings, submits it to the shared `JobManager`, and runs it in a background thread with `JobRunner`. Keep legacy `SyncJobManager` for auto sync, pre-check, user backup, and legacy status fallback so this change stays focused.

**Tech Stack:** Python 3.11, Flask, threading, dataclasses, pytest, existing `pixiv_novel_sync.jobs` package.

---

## File Structure

- Modify: `src/pixiv_novel_sync/webapp.py` — create shared Web job manager/runner, add Web `JobSpec` builder, serialize shared `JobState`, route `/api/dashboard/sync/start` and status lookup through shared jobs.
- Modify: `tests/test_webapp_security.py` — add focused Flask tests proving Web manual sync creates `JobSpec(source=WEB)` and status returns the shared job.

---

### Task 1: Add Web JobSpec start test

**Files:**
- Modify: `tests/test_webapp_security.py`

- [ ] **Step 1: Write the failing test**

Add imports:

```python
from pixiv_novel_sync.jobs.models import JobSource, JobStatus
```

Add test:

```python
def test_dashboard_sync_start_submits_web_jobspec(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")

    submitted = []
    ran = []

    def fake_run(self, job_id):
        ran.append(job_id)
        state = self.manager.get_job(job_id)
        self.manager.mark_running(job_id, "running")
        self.manager.mark_succeeded(job_id, "succeeded")
        return state

    monkeypatch.setattr("pixiv_novel_sync.webapp.JobRunner.run", fake_run)
    app = create_app(env_path=str(env_path))
    client = app.test_client()

    response = client.post("/api/dashboard/sync/start")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["job"]["source"] == JobSource.WEB.value
    assert payload["job"]["task_list"] == ["bookmark", "following_users", "following_novels", "subscribed_series"]
    assert ran == [payload["job"]["job_id"]]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_webapp_security.py::test_dashboard_sync_start_submits_web_jobspec -q
```

Expected: FAIL because `/api/dashboard/sync/start` still returns legacy job data without `source`, and it does not call shared `JobRunner.run`.

---

### Task 2: Implement Web shared job submission

**Files:**
- Modify: `src/pixiv_novel_sync/webapp.py`

- [ ] **Step 1: Add imports**

Near existing imports add:

```python
from .jobs.manager import JobManager
from .jobs.models import JobSource, JobSpec, JobState, JobType
from .jobs.runner import JobRunner
from .jobs.tasks import build_default_task_list, execute_task
```

- [ ] **Step 2: Add shared job serializer and adapter helpers**

Add near `_job_to_dict` helpers or before `create_app`:

```python
def _shared_job_to_dict(job: JobState | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "message": job.message,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "stats": job.stats,
        "error": job.error,
        "progress": job.progress,
        "logs": [entry.__dict__ for entry in job.logs],
        "task_list": list(job.task_types),
        "current_task_index": int(job.progress.get("current_task_index", 0) or 0),
        "source": job.spec.source.value,
        "job_type": job.spec.job_type.value,
    }


def _build_web_sync_job_spec(settings: Settings) -> JobSpec:
    return JobSpec(
        source=JobSource.WEB,
        job_type=JobType.SYNC,
        task_types=build_default_task_list(settings),
    )
```

- [ ] **Step 3: Create shared manager and runner inside `create_app`**

After `sync_job_manager = SyncJobManager(...)`, add:

```python
    shared_job_manager = JobManager()

    def run_web_task(task_type: str, context: dict[str, Any]) -> dict[str, Any] | None:
        current_settings = settings_manager.load(env_path=env_path)
        return execute_task(task_type, current_settings, context)

    shared_job_runner = JobRunner(shared_job_manager, run_web_task)
```

- [ ] **Step 4: Route manual start through shared jobs**

Replace the body of `dashboard_sync_start()` after loading `current_settings` with:

```python
        spec = _build_web_sync_job_spec(current_settings)
        db = Database(current_settings.storage.db_path)
        db.init_schema()
        try:
            job = shared_job_manager.submit(spec)
            log_id = db.create_task_log(
                task_type="manual",
                task_name="全量手动同步",
                job_id=job.job_id,
                is_auto_sync=False,
            )
            job.progress["log_id"] = log_id
            thread = threading.Thread(target=shared_job_runner.run, args=(job.job_id,), daemon=True)
            thread.start()
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            db.close()
        return jsonify({"ok": True, "message": job.message, "job": _shared_job_to_dict(job)})
```

- [ ] **Step 5: Run the start test**

Run:

```bash
python -m pytest tests/test_webapp_security.py::test_dashboard_sync_start_submits_web_jobspec -q
```

Expected: PASS.

---

### Task 3: Add shared status lookup test

**Files:**
- Modify: `tests/test_webapp_security.py`

- [ ] **Step 1: Write the failing test**

Add:

```python
def test_dashboard_sync_status_reads_shared_web_job(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")

    def fake_run(self, job_id):
        self.manager.mark_running(job_id, "running")
        self.manager.update_progress(job_id, phase="同步收藏", current_task_index=0)
        self.manager.mark_succeeded(job_id, "succeeded")
        return self.manager.get_job(job_id)

    monkeypatch.setattr("pixiv_novel_sync.webapp.JobRunner.run", fake_run)
    app = create_app(env_path=str(env_path))
    client = app.test_client()

    started = client.post("/api/dashboard/sync/start").get_json()
    job_id = started["job"]["job_id"]
    response = client.get(f"/api/dashboard/sync/status?job_id={job_id}")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["job"]["job_id"] == job_id
    assert payload["job"]["source"] == "web"
    assert payload["job"]["status"] == JobStatus.SUCCEEDED.value
    assert payload["job"]["progress"]["phase"] == "同步收藏"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_webapp_security.py::test_dashboard_sync_status_reads_shared_web_job -q
```

Expected: FAIL because status still only reads legacy `sync_job_manager`.

---

### Task 4: Implement shared status lookup with legacy fallback

**Files:**
- Modify: `src/pixiv_novel_sync/webapp.py`

- [ ] **Step 1: Update `dashboard_sync_status()`**

Replace the body with:

```python
        job_id = request.args.get("job_id", "").strip()
        shared_job = shared_job_manager.get_job(job_id) if job_id else shared_job_manager.latest_job()
        if shared_job is not None:
            return jsonify({"job": _shared_job_to_dict(shared_job)})
        legacy_job = sync_job_manager.get_job(job_id) if job_id else sync_job_manager.latest_job()
        return jsonify({"job": _job_to_dict(legacy_job)})
```

- [ ] **Step 2: Run status test**

Run:

```bash
python -m pytest tests/test_webapp_security.py::test_dashboard_sync_status_reads_shared_web_job -q
```

Expected: PASS.

---

### Task 5: Verify focused Web and job tests

**Files:**
- Test only.

- [ ] **Step 1: Run Web security tests**

Run:

```bash
python -m pytest tests/test_webapp_security.py -q
```

Expected: all tests PASS.

- [ ] **Step 2: Run shared job tests**

Run:

```bash
python -m pytest tests/test_jobs_models.py tests/test_jobs_manager.py tests/test_jobs_runner.py tests/test_jobs_tasks.py -q
```

Expected: all tests PASS.

- [ ] **Step 3: Run CLI job tests**

Run:

```bash
python -m pytest tests/test_cli_jobs.py -q
```

Expected: all tests PASS.

---

## Self-Review

- Spec coverage: The plan implements scheme 2 for Web manual sync start/status through `JobSpec`, shared `JobManager`, and shared `JobRunner`; it intentionally leaves auto sync, pre-check, and user backup on legacy code as agreed.
- Placeholder scan: No TODO/TBD placeholders remain.
- Type consistency: `JobSpec`, `JobSource`, `JobType`, `JobState`, `JobRunner`, and `JobManager` names match existing files in `src/pixiv_novel_sync/jobs/`.

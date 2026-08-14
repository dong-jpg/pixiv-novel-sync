# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Install (editable) and run:

```bash
pip install -e .              # install package + deps
pip install -e ".[test]"      # + pytest
pixiv-novel-sync web-token-ui  # start Flask UI on http://localhost:5010 (default host 127.0.0.1)
pixiv-novel-sync sync bookmark following_novels subscribed_series  # manual sync
pixiv-novel-sync auth-check    # validate Pixiv auth
pixiv-novel-sync db-stats      # print DB counts
```

Tests (pytest is the only configured tooling; `testpaths=tests`, `pythonpath=src` set in `pyproject.toml`):

```bash
pytest                                   # all tests
pytest tests/test_preferences.py         # one file
pytest tests/test_jobs_runner.py::test_x # one test
pytest -k "rescue"                        # by keyword
```

`tests/conftest.py` has an autouse fixture that redirects `PIXIV_DB_PATH` / `PIXIV_PUBLIC_DIR` / `PIXIV_PRIVATE_DIR` to a tmp dir and clears `DASHBOARD_TOKEN`, so tests never touch real data. Rely on this rather than mocking paths manually.

Note: `black`/`flake8`/`pylint`/`mypy` are not configured or declared as deps in this repo — do not assume they run.

## Architecture

Python package lives under `src/pixiv_novel_sync/`. CLI entry point is `cli.py:main`; the web server is a single Flask app built by `webapp.py:create_app`.

### Job system (central to both sync and background work)

All non-AI background work flows through one pipeline, whether triggered from CLI, web, or the scheduler:

```
JobSpec (source, job_type, task_types, params)
  → JobManager.submit()  → JobState
  → JobRunner.run(job_id) → calls executor per task_type
  → execute_task(task_type, settings, context)  # jobs/tasks.py dispatches by string task_type
```

`jobs/models.py` defines `JobStatus`, `JobSource`, `JobType`, `JobSpec`, `JobState`. Task types are plain strings (`"bookmark"`, `"following_novels"`, `"user_status"`, etc.) dispatched inside `execute_task`. Cancellation is cooperative: tasks poll `stop_requested()` derived from `manager.is_cancel_requested(job_id)` via the `context` dict. When adding a task type, register it in both `execute_task` and `_TASK_LABELS` in `jobs/tasks.py`.

The web layer runs jobs on daemon threads and mirrors state into the `task_logs` DB table (see `_submit_shared_web_job` / `_run_shared_web_job` in `webapp.py`). It enforces a single-active-job constraint via `_has_any_running_web_job()`.

### Storage

One `Database` class (`storage_db.py`) is composed from mixins in `storage/` — each mixin owns a domain (`novels`, `users`, `series`, `bookmarks`, `tasks`, `recommendations`, `rescue`, `reading_progress`, `pending_and_watermarks`, and `storage/ai/`). It's a single SQLite DB (`data/state/pixiv_sync.db`); schema is managed by `SchemaMixin`. Call `db.init_schema()` before use and `db.close()` when done. `storage_files.py` handles the on-disk library layout (`data/library/{public,private}/authors/.../novels/...`).

### Settings

`settings.py:load_settings(config_path, env_path)` merges `config/config.yaml` with environment variables — **env vars always override YAML**. Returns a frozen `Settings` dataclass (`pixiv`, `sync`, `storage`, `log_level`, `dashboard_token`). Most auto-sync scheduling knobs live in `SyncSettings` as `auto_sync_*` fields (each task supports both an interval-hours and a cron expression, cron taking priority). Copy `.env.example` → `.env` and `config/config.yaml.example` → `config/config.yaml` to start.

### Web routing

`create_app` builds the Flask app and then calls `register_ai_routes`, `register_preference_routes`, `register_rescue_routes` to attach routes onto the same app object — these are registration functions, **not Flask blueprints**. Templates in `templates/` use Vue 3 on the frontend, so Jinja variable delimiters are remapped to `{[ ]}` (`app.jinja_env.variable_start_string`) to avoid clashing with Vue's `{{ }}`.

The auto-sync scheduler is guarded by a module-level registry keyed on the resolved DB path (`_scheduler_registry_key`) plus Werkzeug-reloader detection, so it never double-starts across the debug reloader's parent/child processes.

The rescue flow (`rescue_web.py`) pairs with a Tampermonkey userscript at `userscripts/pixiv-rescue.user.js` — changes to rescue upload endpoints must stay compatible with it.

### AI subsystem (`ai/`)

`ai/service.py` exposes `AIWritingService`, composed from mixins in `ai/services/` (`core`, `generation`, `projects`, `chat_wizard`, `admin`). `ai/providers.py` implements OpenAI-compatible, Anthropic, and xAI adapters; `ai/model_sync.py` coordinates safe model discovery, while `ai/model_pools.py` and `ai/model_router.py` own ordered candidates, PromptBudget, failover, and auditable attempts. Business generation methods must use `ModelRouter`; only Provider implementations, Router internals, and the explicit connection test may call `stream_generate()` directly. Supporting modules include `retrieval.py` (TF-IDF + embedding search), `chunking.py`, `detection.py`, `prompts.py`, and `crypto.py`. `ai/model_catalog.py` is the normalization/validation boundary for provider model catalogs — `model_key` is treated as an opaque upstream identifier (byte-exact, no NFC/case-folding, control chars rejected), while display names and metadata are NFC-normalized against a whitelist. **Provider API keys are encrypted at rest** using `PIXIV_NOVEL_SYNC_AI_SECRET_KEY`; keep this value stable. AI creation and routing data share the SQLite DB through `storage/ai/`.

## Conventions

- Every module starts with `from __future__ import annotations`; dataclasses use `slots=True`.
- Code comments and user-facing strings are in Chinese — match the surrounding language.
- Prefer heavy, deferred imports inside functions (as `cli.py` and `jobs/tasks.py` do) to keep CLI startup fast.
- `docs/INDEX.md` maps active-vs-archived docs. Current behavior is defined by code + `README.md` + `docs/frontend-api-contract.md` (endpoint contract) + `docs/frontend-pages.md` (page/template/route inventory); frontend visual work should follow `docs/library-os-style-guide.md`. In-progress designs live in `docs/superpowers/`; older docs (`KNOWLEDGE_GRAPH.md`, `API_COMPLETE.md`, `docs/archive/`) are historical snapshots, not sources of truth.

## Deploy

`./deploy.sh` is the single supported web deploy entry (venv + Nginx + systemd). `scripts/install_server.sh` is legacy timer-based sync only. `DASHBOARD_TOKEN` must be set for any non-localhost exposure — without it the dashboard only allows local access.

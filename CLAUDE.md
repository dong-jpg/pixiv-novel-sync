# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -e ".[test]"       # package + pytest (the only extra)
pixiv-novel-sync web-token-ui  # Flask UI on http://127.0.0.1:5010 (--host/--port to override)
```

Every subcommand takes the global `--config` (default `config/config.yaml`) and `--env-file`. Job subcommands print a JSON result and exit non-zero unless the job succeeded:

| Command | Task types submitted |
|---|---|
| `sync [tasks...]` | no args → `build_default_task_list(settings)` from the `sync_*` toggles |
| `sync-check` | `sync_check` |
| `status-check [tasks...]` | default `user_status novel_status series_status` |
| `pending-deletion-detection` | `pending_deletion_detection` |
| `user-backup <user_id>` | `user_backup:<id>` |
| `sync-bookmarks` | bypasses the job pipeline; calls `run_bookmark_sync` directly |
| `auth-check`, `db-stats` | not jobs |

Tests (`testpaths=tests`, `pythonpath=src` in `pyproject.toml`):

```bash
pytest                                    # full suite: ~5 min, 1302 passed / 4 skipped
pytest tests/test_preferences.py           # one file
pytest tests/test_jobs_runner.py::test_x   # one test
pytest -k "rescue"                         # by keyword
```

`tests/conftest.py` has an autouse fixture that points `PIXIV_DB_PATH` / `PIXIV_PUBLIC_DIR` / `PIXIV_PRIVATE_DIR` at a tmp dir and clears `DASHBOARD_TOKEN` / `ENV_PATH`, so tests never touch real data — rely on it rather than mocking paths. `tests/test_test_isolation.py` fails if that isolation regresses.

`black` / `flake8` / `pylint` / `mypy` are not configured or declared anywhere — do not assume they run. `python -m compileall -q src` is the only static check the repo uses.

## Architecture

Package lives under `src/pixiv_novel_sync/`. CLI entry point is `cli.py:main`; the web server is a single Flask app built by `webapp.py:create_app`.

### Two independent job systems

Do not conflate them:

| | Sync / preference jobs | AI generation jobs |
|---|---|---|
| Orchestration | `jobs/` — `JobManager` + `JobRunner` | `ai/model_router.py` + `ai/services/core.py` route jobs |
| State | in-memory `JobState`, mirrored into `task_logs` | persisted in `ai_jobs` (owner token, candidate snapshot, 30-min deadline) |
| Transport | poll `GET /api/dashboard/sync/status` | SSE stream, resumable via `POST /api/dashboard/ai/jobs/<id>/continue` |
| Concurrency | one at a time, process-wide (`BoundedSemaphore(1)`) | not gated by that semaphore; guarded per job by owner token + idempotency key |

`GET /api/dashboard/logs?category=sync|ai` is the only place they meet: it reads whichever table matches the category and projects `ai_jobs` into the same shape. Nothing merges them in storage. The scheduler loop trims both to 3 days (`cleanup_old_task_logs` / `cleanup_ai_jobs`).

### Sync job pipeline

All non-AI background work flows through one pipeline, whether triggered from CLI, web, or the scheduler:

```
JobSpec (source, job_type, task_types, params)
  → JobManager.submit()  → JobState
  → JobRunner.run(job_id) → calls executor per task_type
  → execute_task(task_type, settings, context)  # jobs/tasks.py dispatches by string task_type
```

`jobs/models.py` defines `JobStatus`, `JobSource`, `JobType`, `JobSpec`, `JobState`. Task types are plain strings (`"bookmark"`, `"following_novels"`, `"user_status"`, …) dispatched inside `execute_task`; an unregistered string raises `RuntimeError`. Adding a task type touches four registries (dispatch, two independent label dicts, scheduler config + `SyncSettings` triple) — follow the checklist in `docs/JOB_SYSTEM.md` §4 rather than guessing.

Two protocols in `jobs/manager.py` are easy to break:

- **Cooperative cancellation.** Tasks poll `stop_requested()` (derived from `manager.is_cancel_requested(job_id)` via the `context` dict) and raise `InterruptedError`; `JobRunner` converts that into `CANCELLED`. Never `time.sleep()` a whole rate-limit delay — sleeps are chunked and poll for cancel, via `rate_limiter.cancellable_sleep` (raises), `jobs/services._sleep_with_cancel` (returns bool), or `sync_engine._sleep_with_progress_cancel` (polls through the progress callback) depending on what the call site has in hand.
- **`FinalizationClaim`.** Before committing terminal side effects a task calls `context["claim_finalization"]()`; `request_cancel` is refused while a claim is open, and a lost claim turns the job into `CANCELLED` instead. This is what makes "cancel" and "finish" mutually exclusive rather than racy.

The web layer runs jobs on daemon threads and mirrors state into `task_logs` (`_submit_shared_job` / `_run_shared_web_job`), enforcing a single active job via `_has_any_running_web_job()` inside the `JobManager` lock (closing a TOCTOU window). A succeeded job whose stats carry `aborted_reason` or `incomplete` is written as **`partial`**, not `succeeded` (`_task_log_status_for_stats`) — this exists because a rate-limited status check once reported green while checking 30 of 800 novels. Relatedly, `web/utils.py:_classify_pixiv_response` is three-state (`ok` / `missing` / `unknown`): only an explicit "does not exist" marks something deleted; rate limits fall through to `unknown` and leave the record alone.

The actual Pixiv work sits below this layer: `sync_engine.py:BookmarkNovelSyncService` does the API calls, pagination, watermarks and file writes, and the task functions in `jobs/services.py` / `jobs/quick_sync.py` are thin wrappers that adapt it to the job `context`.

### Storage

One `Database` class (`storage_db.py`) composed from mixins in `storage/` — each owns a domain (`novels`, `users`, `series`, `bookmarks`, `tasks`, `recommendations`, `rescue`, `reading_progress`, `pending_and_watermarks`, plus `storage/ai/`: `core`, `documents`, `writing`, `catalog`, `model_sync`, `pools`, `adult`). Single SQLite file (`data/state/pixiv_sync.db`).

- **Connections are thread-local** (`storage/connection.py`): WAL, `busy_timeout=30000`, `foreign_keys=ON` per connection. Writes go through `db.transaction()` (`BEGIN IMMEDIATE`, safely nestable); grouped reads use `db.read_transaction()`. `close()` bumps a generation counter so other threads rebuild rather than reuse a closed handle. Instances are short-lived: construct → `init_schema()` → work → `close()` in a `finally`.
- **Migrations are idempotent DDL re-run on every `init_schema()`** — no version table, no migration files. Add a column by extending the relevant `_migrate_*` method in `storage/schema.py` with a `PRAGMA table_info` guard plus `ALTER TABLE ... ADD COLUMN`. `init_schema()` deliberately does *not* clean stale `running` task logs (concurrent web requests would kill live jobs); `create_app` calls `fail_stale_task_logs()` once at startup instead.

`storage_files.py` owns the on-disk library: `{public,private}/authors/{user_id}_{safe_name}/novels/{novel_id}_{sha256(title)[:12]}`. All file writes are atomic (tmp + `os.replace`); config writes use `web/utils.py:_atomic_write_yaml` and secrets use `utils_env.py:secure_atomic_write`.

### Settings

`settings.py:load_settings(config_path, env_path)` merges `config/config.yaml` with environment variables — **env vars always override YAML**. Returns a `Settings` dataclass (`pixiv`, `sync`, `storage`, `log_level`, `dashboard_token`); it is `slots=True` but *not* frozen, and a few call sites do mutate it (e.g. backfilling `settings.pixiv.user_id` after login). Most scheduling knobs live in `SyncSettings` as `auto_sync_*` fields: each task has both `*_interval_hours` and `*_cron`, with cron taking priority and evaluated in `auto_sync_timezone` (default UTC). Copy `.env.example` → `.env` and `config/config.yaml.example` → `config/config.yaml` to start; `.env.example` is the authoritative list of env vars.

The web layer never calls `load_settings` directly — `web/managers.py:SettingsManager` caches it and `save_sync_settings` validates cron expressions before writing YAML back.

### Web app assembly and auth

`create_app` builds the Flask app, then calls `register_ai_routes`, `register_preference_routes`, `register_rescue_routes` to attach routes onto the same app object — these are registration functions, **not Flask blueprints**, so everything shares one URL map and one set of before/after hooks.

Auth is a single `@app.before_request` gate, which any new route inherits automatically:

- `DASHBOARD_TOKEN` acts as the login password; success sets a session cookie. Unset token ⇒ loopback-only, and any request carrying proxy headers is rejected outright unless `DASHBOARD_TRUST_PROXY=1` (with `DASHBOARD_TRUSTED_PROXY_HOPS` deciding which `X-Forwarded-For` entry is trusted — counted from the right).
- Mutating methods (`POST`/`PUT`/`PATCH`/`DELETE`) require a matching `X-CSRF-Token` header or `csrf_token` form field.
- `/api/rescue/v1/*` is exempt from the whole gate; it authenticates separately with a bearer rescue token plus its own rate limiter in `rescue_web.py`. Adult endpoints answer `403`, other APIs `401`, page requests redirect.

The auto-sync scheduler is guarded by a module-level registry keyed on the resolved DB path (`_scheduler_registry_key`) plus Werkzeug-reloader detection, so it never double-starts across the debug reloader's parent/child processes. That registry is per-process, so the app must stay single-process — a multi-worker WSGI server would run one scheduler and one `JobManager` per worker and break the single-active-job invariant. Scheduler behaviour worth knowing: it restores each task's last-run time from `task_logs` on startup (so a restart doesn't defer everything a full period), staggers overdue tasks, and picks among all due tasks by `(priority, most overdue)` — a due higher-priority task can preempt a running lower-priority one that is watermark-resumable. Priority/preemption lives in `SCHEDULER_TASK_CONFIGS`; see `docs/JOB_SYSTEM.md` §3.6 before changing it.

The rescue flow (`rescue_web.py`) pairs with a Tampermonkey userscript at `userscripts/pixiv-rescue.user.js` — changes to rescue endpoints must stay compatible with it, and `tests/test_rescue_userscript.py` asserts on the script's contents.

### Frontend

There is no JS build step and no `package.json`. Templates in `templates/` are server-rendered Jinja; Tailwind and the Vue 3 global build are loaded from CDNs in `base.html`, and Vue apps mount in-page. Because Vue owns `{{ }}`, Jinja's variable delimiters are remapped to `{[ ]}` (`app.jinja_env.variable_start_string`). Visual work follows `docs/library-os-style-guide.md`; the `library-*` CSS custom properties and classes in `base.html` / `vue_components.html` are asserted by `tests/test_frontend_library_os.py`.

### AI subsystem (`ai/`)

`ai/service.py` exposes `AIWritingService`, composed from mixins in `ai/services/` (`core`, `generation`, `projects`, `chat_wizard`, `admin`, `adult`). Routes live in `ai_web.py`, which wraps the service in a per-DB-path lazy proxy and reconciles stale `ai_jobs` / model-sync operations at startup.

- `ai/providers.py` implements OpenAI-compatible, Anthropic, and xAI adapters. Provider `base_url`s go through `validate_base_url` and are then **DNS-pinned** for the request (`_PinnedHostAdapter`); loopback/private/link-local targets are refused so a decrypted key can never be sent to cloud metadata or an internal host — `PIXIV_AI_ALLOW_PRIVATE_HOSTS=1` opts in to private/loopback for self-hosted models.
- `ai/model_pools.py` + `ai/model_router.py` own ordered candidates, `PromptBudget`, failover, and auditable attempts. Hard ceilings per job: 16 candidate attempts, 32 network requests, 64 resolved candidates, 8 pool nodes. **Business generation must go through `ModelRouter`**; only Provider implementations, Router internals, and the explicit connection test may call `stream_generate()` directly.
- `ai/model_catalog.py` is the normalization/validation boundary for provider catalogs: `model_key` is an opaque upstream identifier (byte-exact, no NFC/case-folding, control chars rejected), while display names and metadata are NFC-normalized against a whitelist. `ai/model_sync.py` coordinates safe discovery.
- Supporting modules: `retrieval.py` (TF-IDF by default, falling back from API embeddings → local sentence-transformers → TF-IDF), `chunking.py`, `detection.py`, `prompts.py`, `preference_context.py`, `crypto.py`.
- **Provider API keys are encrypted at rest** using `PIXIV_NOVEL_SYNC_AI_SECRET_KEY`; keep that value stable or saved keys become unreadable.

Generation methods are generators (`stream_*` → `Iterator[AIStreamChunk]`) wrapped by the route-job lifecycle in `ai/services/core.py` (`_start_route_job` → `_stream_route` → `_finish_route_job`). See `docs/MODEL_ROUTING_GUIDE.md`.

### Adult polish subsystem

`ai/adult_*.py` + `ai/services/adult.py` + `storage/ai/adult.py` implement the local adult-polish agent, and are ~20% of the test suite. It is deliberately fail-closed and does **not** follow the normal AI conventions: its own owner-scoped HMAC auth (`adult_auth.py`), immutable safety/fact-guard policies (`adult_policies.py`), prompt boundary tokens and strict candidate parsing (`adult_prompt.py`), deterministic local validation plus provider-scope hashing (`adult_validation.py`), and request contracts that reject raw text fields (`adult_types.py`). Any of policy mismatch, character-fact failure, locked-term drift, range mismatch, or revision change aborts the job. Read `docs/ADULT_POLISH_USER_GUIDE.md` before touching it; loosening a check is a behaviour change, not a cleanup.

## Conventions

- Modules start with `from __future__ import annotations`; dataclasses use `slots=True`.
- Code comments and user-facing strings are in Chinese — match the surrounding language. Commit subjects follow `type: subject` (Conventional Commits), Chinese or English.
- Prefer heavy, deferred imports inside functions (as `cli.py` and `jobs/tasks.py` do) to keep CLI startup fast.
- Some tests assert on non-Python files, so behaviour changes can require doc/asset edits to stay green: `test_ai_model_docs.py` (README + `docs/`), `test_frontend_library_os.py` (templates + style guide), `test_rescue_userscript.py` (userscript), `test_deployment_contract.py` (`deploy/systemd/*` vs `scripts/install_server.sh`), `test_recommendation_scheduling.py` (every scheduler task has a web label).
- Source-of-truth order when docs disagree (from `docs/UNIFIED_PROJECT_REQUIREMENTS.md` §1.2): code and tests > `README.md` > `docs/frontend-api-contract.md` > `docs/frontend-pages.md` / `docs/library-os-style-guide.md` > this file. `docs/INDEX.md` maps active vs archived docs.
- `docs/superpowers/plans/` and `specs/` describe **target** state; as of 2026-08-24 the four "进行中" plans are verified unimplemented. `KNOWLEDGE_GRAPH.md`, `API_COMPLETE.md`, and `docs/archive/` are historical snapshots. None of these describe current behaviour.

## Deploy

`./deploy.sh` is the single supported web deploy entry (venv + Nginx + systemd, service `pixiv-novel-sync` on 127.0.0.1 behind Nginx); `./update.sh` is the in-place update path on an already-deployed host (pull, reinstall, refresh Nginx/systemd, restart). `scripts/install_server.sh` is legacy timer-based sync only. `DASHBOARD_TOKEN` must be set for any non-localhost exposure — without it the dashboard only allows loopback access.

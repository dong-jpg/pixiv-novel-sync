# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -e ".[test]"       # package + pytest (the only extra)
pixiv-novel-sync web-token-ui  # Flask UI on http://127.0.0.1:5010 (--host/--port to override)
```

`cli.py:build_parser` registers exactly nine subcommands, all taking the global `--config` (default `config/config.yaml`) and `--env-file`. Only the first five go through the job pipeline; those print a JSON result and exit non-zero unless the job succeeded (`cli.py:89` → `SystemExit` at `cli.py:126`). The other four always exit 0 unless an exception escapes.

| Command | Notes |
|---|---|
| `sync [tasks...]` | no args → `build_default_task_list(settings)` from the `sync_*` toggles |
| `sync-check` | `sync_check`; needs `manager` + `job_id` in the context, so it cannot run outside a job |
| `status-check [tasks...]` | no args → `user_status novel_status series_status` |
| `pending-deletion-detection` | `pending_deletion_detection` |
| `user-backup <user_id>` | `user_backup:<id>` |
| `sync-bookmarks` | bypasses the pipeline; calls `run_bookmark_sync` directly |
| `auth-check` | validates Pixiv credentials, prints JSON |
| `db-stats` | prints `db.export_stats()` |
| `web-token-ui` | runs the Flask app |

`sync` defaults its empty task list inside `run_job_command`, but `status-check` must yield `tasks is None` for the same trick — hence the `set_defaults(tasks=None)` + `default=argparse.SUPPRESS` pair at `cli.py:31-36`. Replacing that with a plain `default=[]` submits a job with zero tasks.

Tests (`testpaths=tests`, `pythonpath=src` in `pyproject.toml`):

```bash
pytest                                    # full suite: ~6 min, 1308 passed / 4 skipped
pytest tests/test_preferences.py           # one file
pytest tests/test_jobs_runner.py::test_x   # one test
pytest -k "rescue"                         # by keyword
```

`tests/conftest.py` has an autouse fixture that points `PIXIV_DB_PATH` / `PIXIV_PUBLIC_DIR` / `PIXIV_PRIVATE_DIR` at a tmp dir and clears `DASHBOARD_TOKEN` / `ENV_PATH`, so tests never touch real data — rely on it rather than mocking paths. `tests/test_test_isolation.py` fails if that isolation regresses, including if a test leaks a scheduler thread.

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

`jobs/models.py` defines `JobStatus`, `JobSource`, `JobType`, `JobSpec`, `JobState`. Task types are plain strings (`"bookmark"`, `"following_novels"`, `"user_status"`, …) dispatched inside `execute_task`; an unregistered string raises `RuntimeError`. Adding a task type touches four registries (dispatch, two independent label dicts, scheduler config + `SyncSettings` triple) — follow the checklist in `docs/JOB_SYSTEM.md` §4 rather than guessing. Missing a registry fails silently rather than loudly: no `web/utils.py:_job_spec` branch means the spec quietly becomes `JobType.SYNC`, and a missing `web/managers.py:TASK_LABELS` entry shows the raw English key in the log page (`tests/test_recommendation_scheduling.py` guards the label case).

Two protocols in `jobs/manager.py` are easy to break:

- **Cooperative cancellation.** Tasks poll `stop_requested()` (derived from `manager.is_cancel_requested(job_id)` via the `context` dict) and raise `InterruptedError`; `JobRunner` converts that into `CANCELLED`. Never `time.sleep()` a whole rate-limit delay — sleeps are chunked and poll for cancel, via `rate_limiter.cancellable_sleep` (raises), `jobs/services._sleep_with_cancel` (returns bool), or `sync_engine._sleep_with_progress_cancel` (polls through the progress callback) depending on what the call site has in hand. `tests/test_sync_engine_incremental.py:283` greps `sync_engine.py` and fails on any raw `time.sleep(` outside those helpers, and a companion test fails if a broad `except Exception` swallows the `InterruptedError`.
- **`FinalizationClaim`.** Before committing terminal side effects a task calls `context["claim_finalization"]()`; `request_cancel` is refused while a claim is open, and a lost claim turns the job into `CANCELLED` instead. This is what makes "cancel" and "finish" mutually exclusive rather than racy.

The web layer runs jobs on daemon threads and mirrors state into `task_logs` (`_submit_shared_job` / `_run_shared_web_job`), enforcing a single active job via `_has_any_running_web_job()` inside the `JobManager` lock (closing a TOCTOU window). A succeeded job whose stats carry `aborted_reason` or `incomplete` is written as **`partial`**, not `succeeded` (`_task_log_status_for_stats`) — this exists because a rate-limited status check once reported green while checking 30 of 800 novels. Relatedly, `web/utils.py:_classify_pixiv_response` is three-state (`ok` / `missing` / `unknown`): only an explicit "does not exist" marks something deleted; rate limits fall through to `unknown` and leave the record alone.

The actual Pixiv work sits below this layer: `sync_engine.py:BookmarkNovelSyncService` does the API calls, pagination, watermarks and file writes, and the task functions in `jobs/services.py` / `jobs/quick_sync.py` are thin wrappers that adapt it to the job `context`.

### Rate limiting, circuit breakers, and resumability

`rate_limiter.py:RateLimiter.wait()` is a **minimum-interval spacer, not a token bucket** — it sleeps only the remainder of `delay_seconds_between_pages` since `_last_request_time`, with no jitter and no backoff. Exponential backoff lives separately in `sync/utils.py:retry_on_pixiv_error` (`min(base_delay * 2**attempt, 60.0)` for 429s, flat delay for network errors), which `BookmarkNovelSyncService.__init__` monkey-patches onto six `pixivpy3` methods at `sync_engine.py:212` — a new API call is unprotected unless its name is added to that list. `Retry-After` is only parsed for AI providers, never for Pixiv.

Two independent circuit breakers set `stats["aborted_reason"]`, which is what turns a green job amber:

- subscribed-series sync trips at 5 consecutive fetch failures (`sync_engine.py:1285`) → `"rate_limited"` + `incomplete`;
- status checks trip at `MAX_CONSECUTIVE_UNKNOWN = 15` → `"rate_limited"`, or `MAX_CONSECUTIVE_MISSING = 30` → `"suspicious_missing_streak"` (`jobs/services.py:_process_status_items`).

**In production every `aborted_reason` observed so far has been a false positive** — there were no real 429s. Commit `923dfd0` fixed the main causes: a series that Pixiv coherently reports as deleted now *resets* the failure streak instead of incrementing it (`_classify_series_response` → `sync_engine.py:1491`), re-confirmations of already-`deleted` novels are diverted into `stats["confirmed_missing"]`, and both status checks and following-user scans rotate by `last_checked_at` / `user_last_synced` so an abort no longer starves the tail of the list forever. When touching this code: `_classify_series_response` must return `"unknown"` on any exception, the two streak counters must stay independent, an already-missing item must not zero `consecutive_missing`, and following-list enumeration must use `FOLLOWING_LIST_MAX_PAGES = 50` rather than `max_pages_per_run` — each of these has a dedicated regression test.

Resumability is what makes preemption safe. Per-novel work is content-hash incremental (`meta_hash` / `text_hash` skip writes but still repair missing assets); `sync_following_novels` keeps a real watermark under `sync_watermarks` key `following_novels` (`user_last_synced`), and status checks page through `get_*_ids_for_status_check(limit=...)` ordered by `last_checked_at`. Bookmarks have their own page cap, `sync.bookmark_max_pages_per_run` (falling back to `max_pages_per_run`), because the shared cap of 2 meant only the newest ~60 bookmarks were ever visited before the run was marked `truncated`.

### Storage

One `Database` class (`storage_db.py`) composed from 17 mixins in `storage/` — each owns a domain (`novels`, `users`, `series`, `bookmarks`, `tasks`, `recommendations`, `rescue`, `reading_progress`, `pending_and_watermarks`, plus `storage/ai/`: `core`, `documents`, `writing`, `catalog`, `model_sync`, `pools`, `adult`). Single SQLite file (`data/state/pixiv_sync.db`), ~50 tables plus the `novel_fts` FTS5 index.

- **Connections are thread-local** (`storage/connection.py`): WAL, `busy_timeout=30000`, `foreign_keys=ON` per connection. Writes go through `db.transaction()` (`BEGIN IMMEDIATE`, safely nestable); grouped reads use `db.read_transaction()`. `close()` bumps a generation counter so other threads rebuild rather than reuse a closed handle. Instances are short-lived: construct → `init_schema()` → work → `close()` in a `finally`.
- **Migrations are idempotent DDL re-run on every `init_schema()`** — no version table, no migration files. Add a column by extending the relevant `_migrate_*` method in `storage/schema.py` with a `PRAGMA table_info` guard plus `ALTER TABLE ... ADD COLUMN`. `init_schema()` deliberately does *not* clean stale `running` task logs (concurrent web requests would kill live jobs); `create_app` calls `fail_stale_task_logs()` once at startup instead.

`storage_files.py` owns the on-disk library: `{public,private}/authors/{user_id}_{safe_name}/novels/{novel_id}_{sha256(title)[:12]}`. All file writes are atomic (tmp + `os.replace`); config writes use `web/utils.py:_atomic_write_yaml` and secrets use `utils_env.py:secure_atomic_write`.

### Settings

`settings.py:load_settings(config_path, env_path)` merges `config/config.yaml` with environment variables — **env vars always override YAML**. Returns a `Settings` dataclass (`pixiv`, `sync`, `storage`, `log_level`, `dashboard_token`); it is `slots=True` but *not* frozen, and a few call sites do mutate it (e.g. backfilling `settings.pixiv.user_id` after login). Most scheduling knobs live in `SyncSettings` as `auto_sync_*` fields: each task has both `*_interval_hours` and `*_cron`, with cron taking priority and evaluated in `auto_sync_timezone` (default UTC). Copy `.env.example` → `.env` and `config/config.yaml.example` → `config/config.yaml` to start; `.env.example` is the authoritative list of env vars.

The web layer never calls `load_settings` directly — `web/managers.py:SettingsManager` caches it and `save_sync_settings` validates cron expressions before writing YAML back.

### Web app assembly and auth

`create_app` builds the Flask app, then calls `register_ai_routes`, `register_preference_routes`, `register_rescue_routes` to attach routes onto the same app object — these are registration functions, **not Flask blueprints**, so everything shares one URL map and one set of before/after hooks.

Auth is a single `@app.before_request` gate (`webapp.py:704`), which any new route inherits automatically:

- `DASHBOARD_TOKEN` acts as the login password; success sets a session cookie. Unset token ⇒ loopback-only, and any request carrying proxy headers is rejected outright unless `DASHBOARD_TRUST_PROXY=1` (with `DASHBOARD_TRUSTED_PROXY_HOPS` deciding which `X-Forwarded-For` entry is trusted — counted from the right).
- Mutating methods (`POST`/`PUT`/`PATCH`/`DELETE`) require a matching `X-CSRF-Token` header or `csrf_token` form field.
- `/api/rescue/v1/*` is exempt from the whole gate; it authenticates separately with a bearer rescue token plus its own rate limiter in `rescue_web.py`. Adult endpoints answer `403`, other APIs `401`, page requests redirect.

**The token branch and the loopback branch enforce different rules, and that asymmetry has already shipped one production-only bug.** CSRF is only checked on the authenticated-session path, so local development without a `DASHBOARD_TOKEN` never exercises it and a missing header turns into a blanket `403` the moment the app is deployed (commit `fb91da3` fixed 12 such call sites across 6 templates). Frontend code must therefore use `window.csrfFetch` from `base.html` for every mutating request rather than a hand-rolled `fetch`, and must read failures through `window.errorText` — the gate's error body uses `error` / `detail`, so a handler that only reads `data.message` renders a failure as success.

The auto-sync scheduler is guarded by a module-level registry keyed on the resolved DB path (`_scheduler_registry_key`) plus Werkzeug-reloader detection, so it never double-starts across the debug reloader's parent/child processes. That registry is per-process, so the app must stay single-process — a multi-worker WSGI server would run one scheduler and one `JobManager` per worker and break the single-active-job invariant. Scheduler behaviour worth knowing: it restores each task's last-run time from `task_logs` on startup (so a restart doesn't defer everything a full period), staggers overdue tasks, and picks among all due tasks by `(priority, most overdue)` — a due higher-priority task can preempt a running lower-priority one that is watermark-resumable. `preemptible=True` therefore belongs only to tasks that resume from a watermark; `subscribed_series` restarts from the watchlist head each round, so interrupting it wastes the whole run. Priority, preemption and the anti-starvation guardrails live in `SCHEDULER_TASK_CONFIGS`; read `docs/JOB_SYSTEM.md` §3.6 before changing them.

The rescue flow (`rescue_web.py`) pairs with a Tampermonkey userscript at `userscripts/pixiv-rescue.user.js` — changes to rescue endpoints must stay compatible with it, and `tests/test_rescue_userscript.py` asserts on the script's contents. The catalog it serves is a denormalized projection, not a live query: `db.rebuild_rescue_catalog()` does a full rebuild after sync tasks (`jobs/services.py`, `jobs/quick_sync.py`, `jobs/tasks.py`) and once on the scheduler's first loop if `rescue_catalog_meta` is empty, while `db.refresh_rescue_item()` does targeted refreshes for manual overrides, deletions and chapter writes. Before that first rebuild the read API raises `CatalogNotReadyError` → 503 instead of degrading to a live query, so a new deployment's rescue endpoints stay 503 until the scheduler thread has run one iteration.

### Pixiv authentication

Tokens come from Pixiv's OAuth PKCE flow in `oauth_helper.py:OAuthManager`: `create_task` mints state plus an S256 challenge, `exchange_code` POSTs to `oauth.secure.pixiv.net/auth/token` with the well-known Android client credentials, and `save_to_env` is the **single** function that persists the result — it upserts `PIXIV_REFRESH_TOKEN` into `.env` at mode 0o600 and also mutates `os.environ`. `.env` is the only persistence point; `auth.py:PixivAuthManager` reads the token and never writes a rotated one back. `redirect_uri` is always the fixed Pixiv-registered value, so the per-task `/oauth/callback` route effectively never fires from Pixiv — the real paths are pasting the callback URL or pasting the token. Playwright is declared in `pyproject.toml` but imported lazily and fully optional: auto-login only runs when `PIXIV_USERNAME`/`PIXIV_PASSWORD` are set, and a missing browser degrades to an error string. Note that token responses are redacted to `has_refresh_token` (asserted by `tests/test_webapp_security.py`), so any UI that gates on a plaintext `refresh_token` field will misreport a successful login.

### Preferences and recommendations

Everything here is plain dicts and SQL aggregation — there are no dataclasses in this subsystem, and the two task types (`preference_analyze`, `recommendation_run`) ride the same `JobSpec`/`JobRunner` pipeline as sync.

`preferences.py:PreferenceAnalyzer.analyze_incremental()` batches unanalyzed novels, accumulates six term types (tag / tag_pair / keyword / title_kw / caption_kw / author) and commits each batch as one transaction over `preference_term_counts` + `preference_accumulator` + `preference_analyzed_novels`. The profile itself is never stored per run: `rebuild_profile_from_accumulator()` re-reads top-N from SQL and `_build_profile()` derives `search_strategy` / `reading_bias` / `confidence`. `recommendations.py:RecommendationService.build_search_plan()` turns that strategy into ≤20 Pixiv queries; `run()` scores candidates with pure rules and buffers everything in `pending_items`, publishing all items **plus** the run's terminal status in a single transaction — so a cancelled or failed run persists zero items. Dedup is three-layered: in-run `seen_series`, a series-length memo, and `_is_similar_to_existing` (difflib title match on the same author, or ≥3 shared tags).

Load-bearing details: the README's claim that AI is used only for keyword cleanup is accurate — the sole call is `AIWritingService.clean_keywords` at `jobs/tasks.py:359`, wrapped in try/except with graceful degradation, and the `analyzer.build_profile()` call immediately after it is what folds `refined_keywords` into the profile (drop it and the AI result is silently discarded). `recommendation_run` raises `RuntimeError("需要先生成默认偏好画像")` with no default profile. The analyze task always rewrites the default profile with `is_default: True`, so manual edits via `PUT` are clobbered by the next run. User intent lives in `recommendation_feedback` (interested / dismissed / saved / muted) and `recommendation_mutes` (author | tag); `get_recommendation_filter_state()` unions both with item history so a dismissal survives an item being reset to `new`, and its `archived_novel_ids` is a lazy membership object — set operators on it raise.

### Frontend

There is no JS build step and no `package.json`. Templates in `templates/` are server-rendered Jinja; Tailwind and the Vue 3 global build are loaded from CDNs in `base.html`, and Vue apps mount in-page. Because Vue owns `{{ }}`, Jinja's variable delimiters are remapped to `{[ ]}` (`app.jinja_env.variable_start_string`). Visual work follows `docs/library-os-style-guide.md`; the `library-*` CSS custom properties and classes in `base.html` / `vue_components.html` are asserted by `tests/test_frontend_library_os.py`.

`base.html` is the shared frontend layer, and using it is mandatory rather than conventional: `window.csrfFetch` / `ensureCsrfToken` / `errorText` for every request, and `window.streamSSE(url, options, handlers)` for SSE (it replaced seven near-identical hand-rolled `getReader` loops — the two survivors, in `dashboard_logs.html` and the model-catalog sync in `dashboard_settings_models.html`, keep their own loops for whitelist filtering and resumable framing). `window.aiApi` holds the shared agent/profile loaders. A template that re-implements any of these is a bug, not a style deviation: the duplicates all read `data.error` only, so `detail`-shaped failures rendered as "请求失败", and every hand-rolled fetch is a chance to miss the CSRF header that only matters in production.

Settings is five first-class pages (`/dashboard/settings/{sync,models,agents,adult,system}`, `dashboard_settings_*.html` plus the shared `dashboard_settings_nav.html`); bare `/dashboard/settings` 302s to `sync`. The old single 124 KB template is gone — six test files assert on the new paths, so a further split means migrating those assertions in the same commit. Only `sync` and `system` have field partitions in `web/managers.py:SETTINGS_SECTIONS` (saved via `PUT /api/dashboard/settings/<section>`, which writes only that partition and leaves the rest of the YAML alone); the other three pages configure database-backed AI state through the existing `ai_web.py` endpoints, so `PUT /api/dashboard/settings/models` deliberately 400s. `SETTINGS_SECTIONS` must stay a total partition of `web/utils.py:_settings_to_dict` — a field in no section can never be saved, and `tests/test_settings_sections.py` asserts both directions.

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
- Some tests assert on non-Python files, so behaviour changes can require doc/asset edits to stay green: `test_ai_model_docs.py` (README + `docs/`), `test_frontend_library_os.py` (templates + style guide), `test_rescue_userscript.py` (userscript), `test_deployment_contract.py` (`deploy/systemd/*` vs `scripts/install_server.sh`), `test_recommendation_scheduling.py` (every scheduler task has a web label), `test_sync_engine_incremental.py` (greps `sync_engine.py` source for raw sleeps).
- Source-of-truth order when docs disagree (from `docs/UNIFIED_PROJECT_REQUIREMENTS.md` §1.2): code and tests > `README.md` > `docs/frontend-api-contract.md` > `docs/frontend-pages.md` / `docs/library-os-style-guide.md` > this file. `docs/INDEX.md` maps active vs archived docs.
- `docs/superpowers/plans/` and `specs/` describe **target** state; as of 2026-08-28 the four "进行中" plans remain unimplemented (`refresh_rescue_entities`, `recommendation_search_plans`, `JobType.RECOMMENDATION_SYNC` and `explanation_source` appear nowhere in `src/` or `tests/`). `KNOWLEDGE_GRAPH.md`, `API_COMPLETE.md`, and `docs/archive/` are historical snapshots. None of these describe current behaviour.

## Deploy

`./deploy.sh` is the single supported web deploy entry (venv + Nginx + systemd, service `pixiv-novel-sync` on 127.0.0.1 behind Nginx); `./update.sh` is the in-place update path on an already-deployed host (pull, reinstall, refresh Nginx/systemd, restart). `scripts/install_server.sh` is legacy timer-based sync only. `DASHBOARD_TOKEN` must be set for any non-localhost exposure — without it the dashboard only allows loopback access, and its absence also hides the CSRF gate described above.

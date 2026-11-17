# CLAUDE.md

Current capabilities, test baseline and deployment boundaries: [docs/STATUS.md](docs/STATUS.md). The only active development plan is [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md). Keep current guides and contracts updated; do not create audit, deployment, acceptance or completion reports. Historical material is available from Git.

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **分支分工**：长期仅维护 `main` 与 `ai-writing`。main 包含书库全部功能、共享 AI 配置与关键词清洗、作品全文分析、个人偏好学习、全库自动分析和远程执行试点；ai-writing 合并 main 后额外提供创作项目 / 章节 / 向导 / 蒸馏 / 检索 / 成人润色。共享修复先进入 main，再合并到 ai-writing，禁止用旧写作分支整树覆盖 main。迁移保留存量写作表及通用 owner/idempotency 列。11-12 偏好学习扩展已覆盖旧的个人画像 OUT 决策，详见统一需求 §12.6。

## Commands

```bash
pip install -e ".[test]"       # package + pytest (the only extra)
pixiv-novel-sync web-token-ui  # Flask UI on http://127.0.0.1:5010 (--host/--port to override)
```

`cli.py:build_parser` registers eight subcommands, all taking the global `--config` (default `config/config.yaml`) and `--env-file`. The first four go through the job pipeline and return nonzero on failure or incomplete work. `sync-bookmarks` calls the sync service directly and exits 1 on incomplete, truncated, aborted or failed fetches; the remaining three exit 0 unless an exception escapes.

| Command | Notes |
|---|---|
| `sync [tasks...]` | no args → `build_default_task_list(settings)` from the `sync_*` toggles |
| `status-check [tasks...]` | no args → `user_status novel_status series_status` |
| `pending-deletion-detection` | `pending_deletion_detection` |
| `user-backup <user_id>` | `user_backup:<id>` |
| `sync-bookmarks` | bypasses the pipeline; calls `run_bookmark_sync` directly |
| `auth-check` | validates Pixiv credentials, prints JSON |
| `db-stats` | prints `db.export_stats()` |
| `web-token-ui` | runs the Flask app |

`sync` defaults its empty task list inside `run_job_command`, but `status-check` must yield `tasks is None` for the same trick — hence the `set_defaults(tasks=None)` + `default=argparse.SUPPRESS` pair at `cli.py:29-35`. Replacing that with a plain `default=[]` submits a job with zero tasks.

Tests (`testpaths=tests`, `pythonpath=src` in `pyproject.toml`):

```bash
pytest                                    # branch-specific baselines: docs/STATUS.md
pytest tests/test_preferences.py           # one file
pytest tests/test_jobs_runner.py::test_x   # one test
pytest -k "rescue"                         # by keyword
```

`tests/conftest.py` has an autouse fixture that points `PIXIV_DB_PATH` / `PIXIV_PUBLIC_DIR` / `PIXIV_PRIVATE_DIR` at a tmp dir and clears `DASHBOARD_TOKEN` / `ENV_PATH`, so tests never touch real data — rely on it rather than mocking paths. `tests/test_test_isolation.py` fails if that isolation regresses, including if a test leaks a scheduler thread.

`black` / `flake8` / `pylint` / `mypy` are not configured or declared anywhere — do not assume they run. `python -m compileall -q src` is the static check the repo relies on, but it cannot see undefined names: the 10-20 fix batch shipped a `logger` reference in `ai/providers.py` with no `logger` defined, and only `python -m pyflakes src` (installed locally, not declared in `pyproject.toml`) caught it. Run both after editing code; `tests/test_static_undefined_names.py` now guards that class of bug with a stdlib `symtable` check.

## Architecture

Package lives under `src/pixiv_novel_sync/`. CLI entry point is `cli.py:main`; the web server is a single Flask app built by `webapp.py:create_app`.

### Two independent job systems

Do not conflate them:

| | Sync / preference jobs | AI generation jobs |
|---|---|---|
| Orchestration | `jobs/` — `JobManager` + `JobRunner` | `ai/model_router.py` + `ai/services/core.py` route jobs |
| State | in-memory `JobState`, mirrored into `task_logs` | persisted in `ai_jobs` (owner token, candidate snapshot, 30-min deadline) |
| Transport | poll `GET /api/dashboard/sync/status` | main 的统一调用与业务检查点；旧 `/continue` 返回 409，写作分支有独立继续流程 |
| Concurrency | one at a time, process-wide (`BoundedSemaphore(1)`) | not gated by that semaphore; guarded per job by owner token + idempotency key |

`GET /api/dashboard/logs?category=sync|ai` is the only place they meet: it reads whichever table matches the category and projects `ai_jobs` into the same shape. Nothing merges them in storage. The scheduler loop trims both to `sync.task_log_retention_days` (default 14; `cleanup_old_task_logs` / `cleanup_ai_jobs` share the one value, because the log page presents the two tables as one page with two categories). What lands in `task_logs.stats_json` is capped at 8 KB by `webapp.py:_prune_stats_for_log` — size-based, not a field blacklist, with a `_pruned` receipt naming what was dropped; `recommendation_run` used to write 351 KB per round by embedding its whole candidate list. Only the persisted copy is pruned, never the in-memory `JobState.stats` the live progress endpoint reads.

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

- **Cooperative cancellation.** Tasks poll `stop_requested()` (derived from `manager.is_cancel_requested(job_id)` via the `context` dict) and raise `InterruptedError`; `JobRunner` converts that into `CANCELLED`. Never `time.sleep()` a whole rate-limit delay — sleeps are chunked and poll for cancel, via `rate_limiter.cancellable_sleep` (raises), `jobs/services._sleep_with_cancel` (returns bool), or `sync_engine._sleep_with_progress_cancel` (polls through the progress callback) depending on what the call site has in hand. `tests/test_sync_engine_incremental.py::test_sync_engine_sleep_calls_are_routed_through_cancellable_helper` greps `sync_engine.py` and fails on any raw `time.sleep(` outside those helpers, and a companion test fails if a broad `except Exception` swallows the `InterruptedError`.
- **`FinalizationClaim`.** Before committing terminal side effects a task calls `context["claim_finalization"]()`; `request_cancel` is refused while a claim is open, and a lost claim turns the job into `CANCELLED` instead. This is what makes "cancel" and "finish" mutually exclusive rather than racy.

The web layer runs jobs on daemon threads and mirrors state into `task_logs` (`_submit_shared_job` / `_run_shared_web_job`), enforcing a single active job via `_has_any_running_web_job()` inside the `JobManager` lock (closing a TOCTOU window). A succeeded job whose stats carry `aborted_reason` or `truncated` is written as **`partial`**, not `succeeded` (`_task_log_status_for_stats`) — this exists because a rate-limited status check once reported green while checking 30 of 800 novels. A bare `incomplete` also counts as `partial`; the one exception is `rotation_pending`, which marks "not finishing is by design" (following-author rotation covers `users_limit` authors per round, ~12.8 days per full cycle, so every round is `incomplete`) and stays green. The direction matters: new `stats["incomplete"] = True` sites default to `partial` and only an explicit `rotation_pending` downgrades them, so amber stays rare enough to mean something — `tests/test_sync_engine_incremental.py` greps `sync_engine.py` to make sure every `incomplete` site declares which of the three it is. Relatedly, `web/utils.py:_classify_pixiv_response` is three-state (`ok` / `missing` / `unknown`): only an explicit "does not exist" marks something deleted; rate limits fall through to `unknown` and leave the record alone.

The actual Pixiv work sits below this layer: `sync_engine.py:BookmarkNovelSyncService` does the API calls, pagination, watermarks and file writes, and the task functions in `jobs/services.py` / `jobs/quick_sync.py` are thin wrappers that adapt it to the job `context`.

### Rate limiting, circuit breakers, and resumability

`rate_limiter.py:RateLimiter.wait()` is a **minimum-interval spacer, not a token bucket** — it sleeps only the remainder of `delay_seconds_between_pages` since `_last_request_time`, with no jitter and no backoff. Exponential backoff lives separately in `sync/utils.py:retry_on_pixiv_error` (`min(base_delay * 2**attempt, 60.0)` for 429s, flat delay for network errors), which `sync/utils.py:apply_pixiv_retry_patch` monkey-patches onto the seven `pixivpy3` methods in `RETRY_PATCHED_METHODS` (idempotent; called from both `jobs/services.py:_login` and `BookmarkNovelSyncService.__init__`) — a new API call is unprotected unless its name is added to that tuple. `Retry-After` is parsed for AI providers and the wait is `min(60, max(2**attempt, retry_after or 0))`. Pixiv retries do not parse `Retry-After`.

Two independent circuit breakers set `stats["aborted_reason"]`, which is what turns a green job amber:

- subscribed-series sync trips at 5 consecutive fetch failures (`max_consecutive_fetch_failures` in `sync_engine.py`) → `"rate_limited"` + `incomplete`;
- status checks trip at `MAX_CONSECUTIVE_UNKNOWN = 15` → `"rate_limited"`, or `MAX_CONSECUTIVE_MISSING = 30` → `"suspicious_missing_streak"` (`jobs/services.py:_process_status_items`).

A series that Pixiv coherently reports as deleted resets the fetch failure streak (`_classify_series_response` returning `"missing"`); re-confirmations of already-deleted novels use `confirmed_missing`. Status checks and following-user scans rotate by last-check/sync time so aborts do not starve the tail. Preserve the distinction between confirmed deletion, unknown transport failure and actual rate limiting; old production observations do not classify a new failure. `_classify_series_response` must return `"unknown"` on exceptions, streak counters stay independent, and following-list enumeration uses `FOLLOWING_LIST_MAX_PAGES = 50` rather than the per-task page cap.

Resumability is what makes preemption safe. Per-novel work is content-hash incremental (`meta_hash` / `text_hash` skip writes but still repair missing assets), and the skip decision in both `sync()` and `sync_following_novels` reads `db.novel_archive_complete(novel_id)` directly — the old `sync_check` precheck (its result was never consumed) is gone entirely, including the CLI `sync-check` subcommand, `SyncJobManager`, `check_bookmarks_existence`, and the `sync_check_list` writes; the table itself stays in existing databases. `sync_following_novels` keeps a real watermark under `sync_watermarks` key `following_novels` (`user_last_synced`), and status checks page through `get_*_ids_for_status_check(limit=...)` ordered by `last_checked_at`. Bookmarks have their own page cap, `sync.bookmark_max_pages_per_run` (falling back to `max_pages_per_run`), because the shared cap of 2 meant only the newest ~60 bookmarks were ever visited before the run was marked `truncated`; `user_backup` has its own `sync.user_backup_max_pages_per_run` for the same reason.

Rotation slots — not time — are the scarce resource in `sync_following_novels`: `users_limit` authors per round, so a wasted slot delays everyone behind it. `_order_following_users_for_rotation` therefore sorts on `(deprioritized, has_timestamp, timestamp)`, where authors confirmed `no_novels` (Pixiv follows don't separate illustration from novels; production had 87 of 298) are pushed to the back for `NO_NOVELS_RECHECK_DAYS`. Deprioritize, never exclude: a missing timestamp yields an age of infinity so a never-scanned empty author still gets one pass, and after the window they rejoin the normal order. Progress is reported as `rotation_never_synced` / `rotation_oldest_age_days`; `users_remaining` is `len(candidates) - users_limit`, constant every round, and must not be used as a progress indicator.

### Storage

One `Database` class (`storage_db.py`) composes domain mixins on top of `DatabaseConnection`: library, synchronization, recommendations, rescue, reading progress, authentication, shared AI, work analysis, preference learning and library analysis. The business SQLite file (`data/state/pixiv_sync.db`) includes the `novel_fts` FTS5 index; remote control has its own database. Tables created by `ai-writing` — drafts / documents / profiles / prompt templates / adult polish — survive in existing databases; `init_schema()` on main never drops them.

- **Connections are thread-local** (`storage/connection.py`): WAL, `busy_timeout=30000`, `foreign_keys=ON` per connection. Writes go through `db.transaction()` (`BEGIN IMMEDIATE`, safely nestable); grouped reads use `db.read_transaction()`. `close()` bumps a generation counter so other threads rebuild rather than reuse a closed handle. Instances are short-lived: construct → `init_schema()` → work → `close()` in a `finally`.
- **Migrations are idempotent DDL re-run on every `init_schema()`** — no version table, no migration files. Add a column by extending the relevant `_migrate_*` method in `storage/schema.py` with a `PRAGMA table_info` guard plus `ALTER TABLE ... ADD COLUMN`. `init_schema()` deliberately does *not* clean stale `running` task logs (concurrent web requests would kill live jobs); `create_app` calls `fail_stale_task_logs()` once at startup instead.

`storage_files.py` owns the on-disk library: `{public,private}/authors/{user_id}_{safe_name}/novels/{novel_id}_{sha256(title)[:12]}`. All file writes are atomic (tmp + `os.replace`); config writes use `web/utils.py:_atomic_write_yaml` and secrets use `utils_env.py:secure_atomic_write`.

### Settings

`settings.py:load_settings(config_path, env_path)` merges `config/config.yaml` with environment variables — **env vars always override YAML**. Returns a `Settings` dataclass (`pixiv`, `sync`, `storage`, `log_level`, `dashboard_token`); it is `slots=True` but *not* frozen, and a few call sites do mutate it (e.g. backfilling `settings.pixiv.user_id` after login). Most scheduling knobs live in `SyncSettings` as `auto_sync_*` fields: each task has both `*_interval_hours` and `*_cron`, with cron taking priority and evaluated in `auto_sync_timezone` (default UTC). Copy `.env.example` → `.env` and `config/config.yaml.example` → `config/config.yaml` to start; `.env.example` is the authoritative list of env vars.

The web layer never calls `load_settings` directly — `web/managers.py:SettingsManager` caches it and `save_sync_settings` validates cron expressions before writing YAML back.

### Web app assembly and auth

`create_app` builds the Flask app, then calls `register_ai_routes`, `register_preference_routes`, `register_rescue_routes` to attach routes onto the same app object — these are registration functions, **not Flask blueprints**, so everything shares one URL map and one set of before/after hooks.

Auth is a single `@app.before_request` gate (`webapp.py:_check_auth`), which any new route inherits automatically:

- `DASHBOARD_TOKEN` acts as the login password; success sets a session cookie. Unset token ⇒ loopback-only, and any request carrying proxy headers is rejected outright unless `DASHBOARD_TRUST_PROXY=1` (with `DASHBOARD_TRUSTED_PROXY_HOPS` deciding which `X-Forwarded-For` entry is trusted — counted from the right).
- Mutating methods (`POST`/`PUT`/`PATCH`/`DELETE`) require a matching `X-CSRF-Token` header or `csrf_token` form field.
- `/api/rescue/v1/*` is exempt from the whole gate; it authenticates separately with a bearer rescue token plus its own rate limiter in `rescue_web.py`. Unauthenticated API calls answer `401`, page requests redirect.

**Both authenticated and tokenless loopback requests enforce CSRF.** Tokenless mode also validates Host and proxy trust. Authenticated sessions have persisted IDs, fixed expiry and a credential version, so rotating the password invalidates old sessions. Frontend code must use `window.csrfFetch` from `base.html` for every mutating request and read failures through `window.errorText`; the gate's error body uses `error` / `detail`. Tests must exercise both authentication modes.

The auto-sync scheduler is guarded by a module-level registry keyed on the resolved DB path (`_scheduler_registry_key`) plus Werkzeug-reloader detection, so it never double-starts across the debug reloader's parent/child processes. That registry is per-process, so the app must stay single-process — a multi-worker WSGI server would run one scheduler and one `JobManager` per worker and break the single-active-job invariant. Scheduler behaviour worth knowing: it restores each task's last-run time from `task_logs` on startup (so a restart doesn't defer everything a full period), staggers overdue tasks, and picks among all due tasks by `(priority, most overdue)` — a due higher-priority task can preempt a running lower-priority one that is watermark-resumable. `preemptible=True` therefore belongs only to tasks that resume from a watermark; `subscribed_series` restarts from the watchlist head each round, so interrupting it wastes the whole run. Priority, preemption and the anti-starvation guardrails live in `SCHEDULER_TASK_CONFIGS`; read `docs/JOB_SYSTEM.md` §3.6 before changing them.

The rescue flow (`rescue_web.py`) pairs with a Tampermonkey userscript at `userscripts/pixiv-rescue.user.js` — changes to rescue endpoints must stay compatible with it, and `tests/test_rescue_userscript.py` asserts on the script's contents. The catalog it serves is a denormalized projection, not a live query: `db.rebuild_rescue_catalog()` does a full rebuild after sync tasks (`jobs/services.py`, `jobs/quick_sync.py`, `jobs/tasks.py`) and once on the scheduler's first loop if `rescue_catalog_meta` is empty, while `db.refresh_rescue_item()` does targeted refreshes for manual overrides and deletions (chapter writes are picked up by the end-of-task full rebuild). Before that first rebuild the two catalog *list* endpoints (`/api/dashboard/rescues`, `/api/rescue/v1/series`) raise `CatalogNotReadyError` → 503 instead of degrading to a live query, so a new deployment's rescue lists stay 503 until the scheduler thread has run one iteration; the by-ID reads (`get_rescue_novel` / `get_rescue_series` / lookup) evaluate live and are unaffected.

Two eligibility rules are easy to get wrong because they look like they belong to the same subsystem but don't. First, the catalog **excludes anything with a `pending` row in `pending_deletions`**: unbookmarking / unsubscribing writes only that row (it neither clears the `bookmark_*` `sources` rows nor flips `series.is_subscribed` — both wait for the user's "确认移除"), so an item the user removed themselves *and* that Pixiv also took down would otherwise sit in both lists with the rescue page still labelling it "我的收藏". Only `pending` excludes: `confirmed` really deletes the local rows, `restored` is the user explicitly keeping it. A pending *series* also keeps its chapters out of the catalog (they would otherwise leak in as `series_chapter` rows labelled "我的追更"), unless a chapter has its own `bookmark_*` source. A manual `include` override beats the exclusion. Second, `personal_relation` on list items separates "you bookmarked/subscribed this" from "a bulk scan happened to grab it" — measured on production, all 365 rescue successes came from `following_user_scan` / `user_backup` with zero `bookmark` sources, so without the flag there's no way to tell which losses you actually cared about. The read API (`get_rescue_novel` / `get_rescue_series`, what the userscript calls by ID) is deliberately *not* subject to the pending exclusion: the catalog decides what to browse, the API serves the backup for an ID you already have.

### Pixiv authentication

Tokens come from Pixiv's OAuth PKCE flow in `oauth_helper.py:OAuthManager`: `create_task` mints state plus an S256 challenge, `exchange_code` POSTs to `oauth.secure.pixiv.net/auth/token` with the well-known Android client credentials, and `save_to_env` is the **single** function that persists the result — it upserts `PIXIV_REFRESH_TOKEN` into `.env` at mode 0o600 and also mutates `os.environ`. `.env` is the only persistence point; `auth.py:PixivAuthManager` reads the token and never writes a rotated one back. `redirect_uri` is always the fixed Pixiv-registered value. The legacy `/oauth/start`, `/oauth/callback` and `/oauth/sync-callback/{task_id}` routes have been removed from the current main worktree. Create OAuth tasks through `/api/token-jobs`; the real input paths are pasting the callback URL or pasting the token. Playwright is declared in `pyproject.toml` but imported lazily and fully optional: auto-login only runs when `PIXIV_USERNAME`/`PIXIV_PASSWORD` are set, and a missing browser degrades to an error string. Note that token responses are redacted to `has_refresh_token` (asserted by `tests/test_webapp_security.py`), so any UI that gates on a plaintext `refresh_token` field will misreport a successful login.

### Preferences and recommendations

Everything here is plain dicts and SQL aggregation — there are no dataclasses in this subsystem, and the two task types (`preference_analyze`, `recommendation_run`) ride the same `JobSpec`/`JobRunner` pipeline as sync.

`preferences.py:PreferenceAnalyzer.analyze_incremental()` batches unanalyzed novels, accumulates six term types (tag / tag_pair / keyword / title_kw / caption_kw / author) and commits each batch as one transaction over `preference_term_counts` + `preference_accumulator` + `preference_analyzed_novels`. The profile itself is never stored per run: `rebuild_profile_from_accumulator()` re-reads top-N from SQL and `_build_profile()` derives `search_strategy` / `reading_bias` / `confidence`. `recommendations.py:RecommendationService.build_search_plan()` turns that strategy into ≤20 Pixiv queries; `run()` scores candidates with pure rules and buffers everything in `pending_items`, publishing all items **plus** the run's terminal status in a single transaction — so a cancelled or failed run persists zero items. Dedup is three-layered: in-run `seen_series`, a series-length memo, and `_is_similar_to_existing` (difflib title match on the same author, or ≥3 shared tags).

传统统计画像可选调用 `AIWritingService.clean_keywords`，失败保留原统计词；清洗后的 `analyzer.build_profile()` 负责采用精炼结果。`work_analysis/` 另提供已归档作品分析，`preference_learning/` 根据有效报告与明确评价生成版本化个人画像，`storage/library_analysis.py` 管理全库 / 新增分析累计授权。新画像必须显式启用到推荐；正文、系列成员、评价变化应失效旧依据。传统统计重算不会覆盖新学习层。推荐发布须先领取共享任务 `FinalizationClaim`，取消与发布不能同时成功。屏蔽优先于评分，`archived_novel_ids` 是惰性成员查询对象，不可转换成全库集合。

### Frontend

There is no JS build step for users and no `package.json`. Templates in `templates/` are server-rendered Jinja; the Vue 3 global build is loaded from unpkg (pinned version + SRI) in `base.html`, and Vue apps mount in-page. Because Vue owns `{{ }}`, Jinja's variable delimiters are remapped to `{[ ]}` (`app.jinja_env.variable_start_string`). Visual work follows `docs/library-os-style-guide.md`; the `library-*` CSS custom properties and classes in `base.html` / `vue_components.html` are asserted by `tests/test_frontend_library_os.py`.

Tailwind is a **committed, pre-generated stylesheet**, not the Play CDN: `static/css/tailwind.css` (Tailwind v3.4.17, theme in `scripts/tailwind.config.js`, content = `templates/*.html`). Install and deploy never build it. When you add, change or remove a Tailwind class in a template, regenerate it yourself, in the same commit:

```bash
python scripts/build_tailwind_css.py          # rewrites tailwind.css and the ?v= hash in base.html
python scripts/build_tailwind_css.py --check  # exit 1 if the committed CSS is stale
```

The script downloads the pinned standalone CLI (a single binary, no Node), checks its SHA-256 against the release's `sha256sums.txt` values hard-coded in the script, and caches it under `~/.cache/pixiv-novel-sync/`; `--cli PATH` / `TAILWINDCSS_BIN` points it at an existing v3.4.17 binary for offline use. `tests/test_tailwind_static_css.py` fails when a template class is missing from the CSS, when `?v=` doesn't match the file, or when a template assembles a Tailwind class at runtime (`'bg-' + color`, `` `text-${x}` ``): the CLI only scans source text, so write full literals or add them to `safelist` in the config. Two details are load-bearing. The `<link>` must stay the **last** stylesheet in `<head>` (after `extra_head`), because the Play CDN appended its `<style>` there and equal-specificity utilities win ties against `library-*` rules by coming last. Versioned `/static/…?v=` URLs get `Cache-Control: immutable` from `webapp.py:_cache_versioned_static`; `/static/` is exempt from the login gate because `token_login.html` extends `base.html`.

Pixiv body markup (`[newpage]`, `[chapter:]`, `[[rb:]]`, `[[jumpuri:]]`, …) has two implementations of one grammar: `templates/pixiv_markup.html` for the reader and `pixiv_markup.py` for EPUB export. They must produce the same tree — `tests/fixtures/pixiv_markup_cases.json` feeds both, and `tests/test_pixiv_markup.py` diffs them on a 3000-case fuzz corpus — so a rule change touches both files in the same commit.

`base.html` is the shared frontend layer, and using it is mandatory rather than conventional: `window.csrfFetch` / `ensureCsrfToken` / `errorText` for every request, and `window.streamSSE(url, options, handlers)` for SSE (it replaced the near-identical hand-rolled `getReader` loops; `dashboard_logs.html` moved over too, and the one survivor, the model-catalog sync in `dashboard_settings_models.html`, keeps its own loop for whitelist filtering and resumable framing). `window.aiApi` holds the shared agent/profile loaders. A template that re-implements any of these is a bug, not a style deviation: the duplicates all read `data.error` only, so `detail`-shaped failures rendered as "请求失败", and every hand-rolled fetch is a chance to miss the CSRF header that only matters in production.

Settings has five first-class pages on ai-writing (`/dashboard/settings/{sync,models,agents,system,adult}`, `dashboard_settings_*.html` plus the shared `dashboard_settings_nav.html`); bare `/dashboard/settings` 302s to `sync`. The old single 124 KB template is gone — test files assert on the new paths, so a further split means migrating those assertions in the same commit. Only `sync` and `system` have field partitions in `web/managers.py:SETTINGS_SECTIONS` (saved via `PUT /api/dashboard/settings/<section>`, which writes only that partition and leaves the rest of the YAML alone); the other two pages configure database-backed AI state through the existing `ai_web.py` endpoints, so `PUT /api/dashboard/settings/models` deliberately 400s. `SETTINGS_SECTIONS` must stay a total partition of `web/utils.py:_settings_to_dict` — a field in no section can never be saved, and `tests/test_settings_sections.py` asserts both directions. The two AI settings pages (`models`/`agents`) share `dashboard_ai_health_band.html`, a read-only config-health banner driven by a zero-network projection (`GET /api/dashboard/ai/health`). (A fifth page, `/dashboard/settings/adult`, exists on the `ai-writing` branch only.)

The AI writing pages (`/dashboard/ai` project list and its chapters/notes subpages, `/dashboard/wizard`, `/dashboard/novels?category=ai`, the AI reader) live on the `ai-writing` branch; main's sidebar has no such entries and those routes 404. Conventions shared by both branches: `project_id` is injected by the route rather than parsed from `location.search` in JS, and Jinja delimiters stay `{[ ]}` because Vue owns `{{ }}`.

### AI subsystem (`ai/`)

`ai/service.py` exposes `AIWritingService`: shared `core`, `admin` and `keyword_clean`, plus writing generation, projects, chat wizard and adult mixins on this branch. Unified v2 invocations serve work analysis and preference learning with metadata_only retention; legacy writing uses its owned replayable inputs and v1 candidate snapshots. Routes in `ai_web.py` use a per-DB-path lazy proxy and reconcile stale jobs / model-sync operations at startup. Closing a service forbids reopening its Provider pool; failed schema setup closes its database connection.

- `ai/providers.py` implements OpenAI-compatible, Anthropic, and xAI adapters. Provider `base_url`s go through `validate_base_url` and are then **DNS-pinned** for the request (`_PinnedHostAdapter`); loopback/private/link-local targets are refused so a decrypted key can never be sent to cloud metadata or an internal host — `PIXIV_AI_ALLOW_PRIVATE_HOSTS=1` opts in to private/loopback for self-hosted models.
- `ai/model_pools.py` + `ai/model_router.py` own ordered candidates, `PromptBudget`, failover, and auditable attempts. Hard ceilings per job: 16 candidate attempts, 32 network requests, 64 resolved candidates, 8 pool nodes. **Business generation must go through `ModelRouter`**; only Provider implementations, Router internals, and the explicit connection test may call `stream_generate()` directly. `tests/test_ai_model_docs.py` walks `ai/` AST-wise and fails on any other call site.
- `ai/model_catalog.py` is the normalization/validation boundary for provider catalogs: `model_key` is an opaque upstream identifier (byte-exact, no NFC/case-folding, control chars rejected), while display names and metadata are NFC-normalized against a whitelist. `ai/model_sync.py` coordinates safe discovery.
- `ai/services/keyword_clean.py` implements optional keyword cleanup; work reports and personal profiles use unified text invocations with `metadata_only` retention, validated evidence, cumulative budgets and business checkpoints. Never route those requests through legacy writing resume or store their full inputs in ordinary job history.
- **Provider API keys are encrypted at rest** using `PIXIV_NOVEL_SYNC_AI_SECRET_KEY`; keep that value stable or saved keys become unreadable.

Writing jobs with an eligible v1 snapshot and an untried candidate expose `can_continue=True`; generic metadata_only analysis remains ineligible. The durable dashboard gate supplies verified authentication time to adult owner checks; a signed cookie alone is insufficient. All writing template scripts require the shared CSP nonce.

Generation flows through `ai/services/core.py` (`_start_route_job` → `_stream_route` → `_finish_route_job`). main disables generic job continue; work analysis resumes through its owned checkpoints without resetting cumulative budgets. `remote/` has a separate control database and session/lease protocol: only diagnostic and single EPUB kinds are registered. Pixiv fetch and check-in adapters are not remote task implementations. See `docs/MODEL_ROUTING_GUIDE.md` and `docs/REMOTE_EXECUTION.md`.

## Conventions

- Modules start with `from __future__ import annotations`; dataclasses use `slots=True`.
- Code comments and user-facing strings are in Chinese — match the surrounding language. Commit subjects follow `type: subject` (Conventional Commits), Chinese or English.
- Prefer heavy, deferred imports inside functions (as `cli.py` and `jobs/tasks.py` do) to keep CLI startup fast.
- Some tests assert on non-Python files, so behaviour changes can require doc/asset edits to stay green: `test_ai_model_docs.py` (README + `docs/`), `test_frontend_library_os.py` (templates + style guide), `test_rescue_userscript.py` (userscript), `test_deployment_contract.py` (`deploy/systemd/*` vs `scripts/install_server.sh`), `test_recommendation_scheduling.py` (every scheduler task has a web label), `test_sync_engine_incremental.py` (greps `sync_engine.py` source for raw sleeps), `test_main_readme_branding.py` (README sections, wording and local links), `test_rescue_userscript_install.py` (install links in README + `docs/RESCUE_USER_GUIDE.md`), `test_tailwind_static_css.py` (template classes vs the committed `tailwind.css`).
- Source-of-truth order when docs disagree (from `docs/UNIFIED_PROJECT_REQUIREMENTS.md` §1.2): code and tests > `README.md` > `docs/frontend-api-contract.md` > `docs/frontend-pages.md` / `docs/library-os-style-guide.md` > this file. `docs/INDEX.md` maps the active guides, designs and plan.
- `docs/AI_ROUTING_DESIGN.md`, `docs/WORK_ANALYSIS_DESIGN.md` and `docs/REMOTE_EXECUTION_DESIGN.md` preserve current design constraints. The excluded items remain **OUT** in unified requirements §1.3; do not reintroduce `refresh_rescue_entities`, `recommendation_search_plans`, `JobType.RECOMMENDATION_SYNC` or `explanation_source`. Update the single implementation plan and STATUS in place. Writing/adult tasks remain on ai-writing. Keep generated verification output outside tracked documentation.

## Deploy

`./deploy.sh` is the single supported web deploy entry (venv + Nginx + systemd, service `pixiv-novel-sync` on 127.0.0.1 behind Nginx); `./update.sh` is the in-place update path on an already-deployed host (pull, reinstall, refresh Nginx/systemd, restart). `scripts/install_server.sh` is legacy timer-based sync only. `DASHBOARD_TOKEN` must be set for any non-localhost exposure; tokenless loopback mode still enforces Host and CSRF checks.

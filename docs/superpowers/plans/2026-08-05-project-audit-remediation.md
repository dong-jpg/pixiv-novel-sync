# 项目审计问题完整修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 本轮未获授权使用子代理，必须在当前任务内顺序执行。

**Goal:** 修复审计确认的取消/循环、推荐、AI 偏好注入、自动调度和 API 文档缺口，并与既有成人描写局部润色 Agent 完整计划合并交付。

**Architecture:** 运行时任务统一经过 `JobSpec`、`JobManager`、`JobRunner` 和 `execute_task`；取消信号传到实际等待点。推荐和 AI 项目通过幂等 SQLite 迁移补齐字段，偏好 prompt 由独立纯函数构造并在服务层统一注入。成人功能不在本文件重复定义，完整执行已批准的 [`2026-07-23-adult-polish-agent.md`](2026-07-23-adult-polish-agent.md)，复用当前 `ModelRouter`。

**Tech Stack:** Python 3.10+、Flask 3、SQLite/WAL、Vue 3 CDN、pytest、现有 `pixivpy3` 与 AI `ModelRouter`。

## Global Constraints

- 遵守根目录 `AGENTS.md`；所有生产行为变更严格先写失败测试并确认 RED，再写最小实现。
- 不新增第三方依赖；SQLite 迁移必须幂等，旧数据使用保守默认值。
- 测试只能使用 `tests/conftest.py` 提供的临时数据库和目录，不读取真实 `data/`、`.env` 或密钥。
- 保持现有 CLI、Dashboard URL、JSON 成功/错误外壳和同步状态字段兼容。
- 成人功能只允许已认证 Dashboard 会话，固定安全/事实审查不可编辑、不可跳过，任何不确定状态 fail-closed。
- 日志不得保存 API key、完整 Prompt、成人目标片段、上下文或候选正文副本。
- 不提交无关格式化或重构；每个任务只提交其列出的文件。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `src/pixiv_novel_sync/rate_limiter.py` | 唯一可取消 sleep 原语 |
| `src/pixiv_novel_sync/sync/utils.py` | Pixiv retry 分类与可取消退避 |
| `src/pixiv_novel_sync/sync_engine.py` | 同步服务取消桥接与分页安全上限 |
| `src/pixiv_novel_sync/recommendations.py` | 推荐抓取、过滤、评分、风险和取消 |
| `src/pixiv_novel_sync/storage/recommendations.py` | 推荐字段序列化与历史 ID 集合 |
| `src/pixiv_novel_sync/ai/preference_context.py` | 四档偏好 prompt 的纯函数构造 |
| `src/pixiv_novel_sync/ai/services/core.py` | 从 payload/项目解析画像并统一注入消息 |
| `src/pixiv_novel_sync/web/managers.py` | 只负责定时计算、生命周期和共享 job 提交/取消 |
| `src/pixiv_novel_sync/web/utils.py` | Web/Scheduler `JobSpec` 构造和统一状态序列化 |
| `src/pixiv_novel_sync/webapp.py` | 共享 job 提交、执行、日志持久化和路由适配 |
| `docs/frontend-api-contract.md` | 当前前端依赖的唯一 API 契约 |

---

### Task 1: 可取消等待与分页安全上限

**Files:**
- Modify: `src/pixiv_novel_sync/rate_limiter.py`
- Modify: `src/pixiv_novel_sync/sync/utils.py`
- Modify: `src/pixiv_novel_sync/sync_engine.py`
- Modify: `src/pixiv_novel_sync/recommendations.py`
- Modify: `src/pixiv_novel_sync/jobs/tasks.py`
- Modify: `src/pixiv_novel_sync/jobs/services.py`
- Test: `tests/test_rate_limiter.py`
- Create: `tests/test_sync_utils.py`
- Test: `tests/test_sync_engine_incremental.py`
- Test: `tests/test_recommendations.py`

**Interfaces:**
- Produces `cancellable_sleep(seconds: float, stop_requested: Callable[[], bool] | None, interval: float = 0.2) -> None`.
- Extends `retry_on_pixiv_error(max_retries: int = 3, base_delay: float = 5.0, stop_requested: Callable[[], bool] | None = None)` without changing existing callers.
- Extends `BookmarkNovelSyncService(api: AppPixivAPI, db: Database, storage: FileStorage, settings: Settings, sync_check_scope: str = "_", stop_requested: Callable[[], bool] | None = None)`.
- Extends `RecommendationService(db: Database, settings: Settings, api: AppPixivAPI | None = None, stop_requested: Callable[[], bool] | None = None)`.

- [ ] **Step 1: Write the failing cancellation and pagination tests**

```python
def test_retry_backoff_raises_when_cancelled():
    calls = 0

    @retry_on_pixiv_error(max_retries=1, base_delay=30, stop_requested=lambda: True)
    def operation():
        nonlocal calls
        calls += 1
        raise RuntimeError("network timeout")

    with pytest.raises(InterruptedError, match="Task stopped by user"):
        operation()
    assert calls == 1


def test_recommendation_page_delay_forwards_stop_requested(tmp_path, monkeypatch):
    stop_requested = lambda: True
    service = RecommendationService(_db(tmp_path), _settings(tmp_path), stop_requested=stop_requested)
    observed = []
    monkeypatch.setattr(service.rate_limiter, "wait", lambda **kwargs: observed.append(kwargs))
    service._page_delay()
    assert observed == [{"stop_requested": stop_requested}]


def test_check_bookmarks_existence_stops_at_page_safety_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("pixiv_novel_sync.sync_engine._CHECK_PAGE_SAFETY_LIMIT", 2)
    api = EndlessBookmarkPagesApi()
    service = BookmarkNovelSyncService(api, _db(tmp_path), _Storage(), _settings(tmp_path))
    service.check_bookmarks_existence(1, ["public"])
    assert api.calls == 2
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```powershell
python -m pytest tests/test_rate_limiter.py tests/test_sync_utils.py tests/test_sync_engine_incremental.py tests/test_recommendations.py -q
```

Expected: FAIL because retry/service constructors do not accept `stop_requested`, recommendation wait drops it, and legacy bookmark pagination has no cap.

- [ ] **Step 3: Implement the shared cancellation path**

```python
def cancellable_sleep(seconds: float, stop_requested: Callable[[], bool] | None, interval: float = 0.2) -> None:
    if seconds <= 0:
        return
    if stop_requested is None:
        time.sleep(seconds)
        return
    remaining = float(seconds)
    while remaining > 0:
        if stop_requested():
            raise InterruptedError(_INTERRUPT_MESSAGE)
        sleep_for = min(interval, remaining)
        time.sleep(sleep_for)
        remaining -= sleep_for
    if stop_requested():
        raise InterruptedError(_INTERRUPT_MESSAGE)
```

Use this function in `RateLimiter.wait` and retry backoff. Pass the job callback into recommendation and sync service constructors. Change the bookmark loop to:

```python
while next_query and page_count < _CHECK_PAGE_SAFETY_LIMIT:
    result = self.api.user_bookmarks_novel(**next_query)
    page_count += 1
    novels = getattr(result, "novels", []) or []
    all_novel_ids.extend(int(novel.id) for novel in novels)
    next_query = self.api.parse_qs(getattr(result, "next_url", None))
    if next_query and page_count < _CHECK_PAGE_SAFETY_LIMIT:
        self.rate_limiter.wait(stop_requested=_stop_requested_from_progress(progress_callback))
if next_query:
    logger.warning("Bookmark existence pagination stopped at safety limit %d", _CHECK_PAGE_SAFETY_LIMIT)
```

- [ ] **Step 4: Run focused and caller regression tests**

```powershell
python -m pytest tests/test_rate_limiter.py tests/test_sync_utils.py tests/test_sync_engine_incremental.py tests/test_recommendations.py tests/test_jobs_tasks.py tests/test_jobs_services.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/rate_limiter.py src/pixiv_novel_sync/sync/utils.py src/pixiv_novel_sync/sync_engine.py src/pixiv_novel_sync/recommendations.py src/pixiv_novel_sync/jobs/tasks.py src/pixiv_novel_sync/jobs/services.py tests/test_rate_limiter.py tests/test_sync_utils.py tests/test_sync_engine_incremental.py tests/test_recommendations.py
git commit -m "fix: make sync waits cancellable and bounded"
```

---

### Task 2: 推荐字段与跨 run 系列去重

**Files:**
- Modify: `src/pixiv_novel_sync/storage/schema.py`
- Modify: `src/pixiv_novel_sync/storage/recommendations.py`
- Modify: `src/pixiv_novel_sync/recommendations.py`
- Test: `tests/test_recommendations.py`
- Test: `tests/test_storage_db.py`

**Interfaces:**
- `recommendation_items.x_restrict` maps to `item["x_restrict"]: int`.
- `recommendation_items.risk_notes_json` maps to `item["risk_notes"]: list[str]`.
- `get_recommendation_filter_state()` adds `recommended_series_ids` and `dismissed_series_ids`.

- [ ] **Step 1: Write failing migration, round-trip and cross-run tests**

```python
def test_recommendation_item_round_trips_restriction_and_risks(tmp_path):
    db = _db(tmp_path)
    item_id = db.upsert_recommendation_item({**_item(), "x_restrict": 1, "risk_notes": ["包含成人限制内容"]})
    item = db.get_recommendation_item(item_id)
    assert item["x_restrict"] == 1
    assert item["risk_notes"] == ["包含成人限制内容"]


def test_filter_state_tracks_series_across_runs(tmp_path):
    db = _db(tmp_path)
    db.upsert_recommendation_item({**_item(), "item_type": "series", "novel_id": 11, "series_id": 99})
    state = db.get_recommendation_filter_state()
    assert state["recommended_series_ids"] == {99}


def test_previous_series_filters_different_member_novel(tmp_path):
    service, api, db = _service_with_existing_series(tmp_path, existing_series_id=99)
    result = service.run(search_plan=_one_query_plan())
    assert result["stats"]["saved"] == 0
    assert api.series_detail_calls == 0
```

- [ ] **Step 2: Run tests to verify RED**

```powershell
python -m pytest tests/test_recommendations.py tests/test_storage_db.py -q
```

Expected: FAIL on missing columns/keys and saved duplicate series.

- [ ] **Step 3: Add the idempotent migration and serialization**

After `CREATE TABLE IF NOT EXISTS recommendation_items`, inspect `PRAGMA table_info(recommendation_items)` and add absent columns:

```python
if "x_restrict" not in item_columns:
    self.conn.execute("ALTER TABLE recommendation_items ADD COLUMN x_restrict INTEGER NOT NULL DEFAULT 0")
if "risk_notes_json" not in item_columns:
    self.conn.execute("ALTER TABLE recommendation_items ADD COLUMN risk_notes_json TEXT NOT NULL DEFAULT '[]'")
```

Extend insert/update values and parse `risk_notes_json` with malformed JSON fallback to `[]`. Build risk notes deterministically from `x_restrict` and negative preference matches. Filter `series_id` against the union of historical recommended/dismissed series IDs before calling `_series_length`.

- [ ] **Step 4: Run recommendation/storage regressions**

```powershell
python -m pytest tests/test_recommendations.py tests/test_storage_db.py tests/test_preferences.py tests/test_preference_incremental.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/storage/schema.py src/pixiv_novel_sync/storage/recommendations.py src/pixiv_novel_sync/recommendations.py tests/test_recommendations.py tests/test_storage_db.py
git commit -m "fix: persist recommendation risks and series history"
```

---

### Task 3: 偏好上下文构造与项目存储

**Files:**
- Create: `src/pixiv_novel_sync/ai/preference_context.py`
- Modify: `src/pixiv_novel_sync/storage/schema.py`
- Modify: `src/pixiv_novel_sync/storage/ai/writing.py`
- Modify: `src/pixiv_novel_sync/ai/services/core.py`
- Create: `tests/test_ai_preference_context.py`
- Modify: `tests/test_preferences.py`

**Interfaces:**
- Produces `normalize_preference_strength(value: Any) -> Literal['off','light','standard','strong']`.
- Produces `build_preference_context(profile: Mapping[str, Any], strength: str) -> str | None`.
- Produces `inject_preference_context(messages: list[dict[str, str]], context: str | None) -> list[dict[str, str]]` without mutating input.
- `ai_writing_projects` gains nullable `preference_profile_id` and non-null `preference_injection_strength DEFAULT 'off'`.
- `AIServiceBase._resolve_preference_context(db, payload, project=None) -> tuple[int | None, str, str | None]` is the only DB-aware resolver.

- [ ] **Step 1: Write failing pure-function and storage tests**

```python
@pytest.mark.parametrize("strength, expected", [
    ("off", None),
    ("light", "偏好标签"),
    ("standard", "负向排除"),
    ("strong", "叙事偏好"),
])
def test_preference_context_strengths(profile, strength, expected):
    result = build_preference_context(profile, strength)
    assert (result is None) if expected is None else (expected in result)
    if result:
        assert "sample text" not in result


def test_project_preference_fields_round_trip(tmp_path):
    db = _db(tmp_path)
    profile_id = db.create_preference_profile(_profile_payload())
    project_id = db.create_ai_writing_project({
        "name": "p",
        "preference_profile_id": profile_id,
        "preference_injection_strength": "standard",
    })
    project = db.get_ai_writing_project(project_id)
    assert project["preference_profile_id"] == profile_id
    assert project["preference_injection_strength"] == "standard"
```

- [ ] **Step 2: Run tests to verify RED**

```powershell
python -m pytest tests/test_ai_preference_context.py tests/test_preferences.py -q
```

Expected: FAIL because module and project columns do not exist.

- [ ] **Step 3: Implement bounded context and idempotent columns**

Use field whitelists and budgets, never serialize the whole profile:

```python
_ITEM_BUDGETS = {"light": 3, "standard": 8, "strong": 16}
_ALLOWED_STRENGTHS = frozenset({"off", "light", "standard", "strong"})

def build_preference_context(profile, strength):
    strength = normalize_preference_strength(strength)
    if strength == "off":
        return None
    data = profile.get("profile") or {}
    limit = _ITEM_BUDGETS[strength]
    positive = data.get("positive_preferences") or {}
    negative = data.get("negative_preferences") or {}
    lines = ["【用户偏好画像】"]
    summary = str(data.get("summary") or profile.get("description") or "").strip()
    if summary:
        lines.append(f"- 摘要：{summary[:500]}")
    tags = [str(value) for value in positive.get("tags") or []][:limit]
    keywords = [str(value) for value in positive.get("keywords") or []][:limit]
    excluded = [str(value) for value in negative.get("excluded_tags") or []][:limit]
    if tags:
        lines.append("- 偏好标签：" + "、".join(tags))
    if keywords:
        lines.append("- 偏好关键词：" + "、".join(keywords))
    if strength in {"standard", "strong"} and excluded:
        lines.append("- 负向排除：" + "、".join(excluded))
    if strength == "strong":
        narrative = [str(value) for value in positive.get("narrative_patterns") or []][:limit]
        if narrative:
            lines.append("- 叙事偏好：" + "、".join(narrative))
    return "\n".join(lines) if len(lines) > 1 else None
```

Validate profile existence in `_resolve_preference_context`; explicit payload values override project fields. A missing selected profile raises `AIServiceError("偏好画像不存在")`; no selection returns `(None, 'off', None)`.

- [ ] **Step 4: Run context, storage and schema tests**

```powershell
python -m pytest tests/test_ai_preference_context.py tests/test_preferences.py tests/test_ai_model_schema.py tests/test_style_control.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/preference_context.py src/pixiv_novel_sync/storage/schema.py src/pixiv_novel_sync/storage/ai/writing.py src/pixiv_novel_sync/ai/services/core.py tests/test_ai_preference_context.py tests/test_preferences.py
git commit -m "feat: add bounded AI preference context"
```

---

### Task 4: 全部 AI 创作入口与前端接入偏好画像

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/generation.py`
- Modify: `src/pixiv_novel_sync/ai/services/projects.py`
- Modify: `src/pixiv_novel_sync/ai/services/chat_wizard.py`
- Modify: `src/pixiv_novel_sync/templates/dashboard_ai.html`
- Modify: `src/pixiv_novel_sync/templates/dashboard_wizard.html`
- Modify: `tests/test_ai_prompts.py`
- Modify: `tests/test_ai_model_router_integration.py`
- Modify: `tests/test_frontend_library_os.py`

**Interfaces:**
- Payload accepts `preference_profile_id` and `preference_injection_strength` at wizard, plan, continue, pipeline, polish, rewrite/de-AI and audit entry points.
- Project values are defaults; explicit request values override them for one operation without writing back.
- Every task calls `inject_preference_context` after building its normal messages and before route budget fitting.

- [ ] **Step 1: Write failing service and frontend contract tests**

```python
@pytest.mark.parametrize("method,payload", [
    ("stream_longform_plan", {"project_id": 1, "target_words": 10000}),
    ("stream_chapter_continue", {"project_id": 1, "chapter_id": 1}),
    ("stream_polish", {"chapter_id": 1, "polish_type": "dialogue"}),
    ("stream_audit", {"text": "正文"}),
])
def test_ai_entrypoint_routes_bounded_preference_context(service, router, method, payload):
    payload.update({"agent_id": 1, "preference_profile_id": 7, "preference_injection_strength": "standard"})
    list(getattr(service, method)(payload))
    rendered = "\n".join(message["content"] for message in router.requests[-1].messages)
    assert "【用户偏好画像】" in rendered
    assert "样本文本" not in rendered


def test_ai_templates_expose_preference_profile_and_strength():
    for name in ("dashboard_ai.html", "dashboard_wizard.html"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "preference_profile_id" in text
        assert "preference_injection_strength" in text
        assert "/api/dashboard/preferences/profiles" in text
```

Add focused cases for `off`, missing/deleted profile, longform details, wizard chat, rewrite/de-AI, audit and every Pipeline step that invokes an AI writer/reviewer.

- [ ] **Step 2: Run tests to verify RED**

```powershell
python -m pytest tests/test_ai_prompts.py tests/test_ai_model_router_integration.py tests/test_frontend_library_os.py -q
```

Expected: FAIL because services and templates ignore both fields.

- [ ] **Step 3: Inject context through the common helper**

At each entry point:

```python
project = db.get_ai_writing_project(project_id) if project_id else None
_, preference_strength, preference_context = self._resolve_preference_context(db, payload, project)
messages = inject_preference_context(messages, preference_context)
```

Pass both preference fields into nested Pipeline payloads. Load profiles once per page and use a select plus four-value option set; keep `off` as the old-project default. The preview response reports profile ID/strength and the bounded injected block, never source samples.

- [ ] **Step 4: Run AI service, route and page regressions**

```powershell
python -m pytest tests/test_ai_prompts.py tests/test_ai_model_router_integration.py tests/test_ai_web_stream.py tests/test_style_control.py tests/test_frontend_library_os.py tests/test_ai_page_routes.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/services/generation.py src/pixiv_novel_sync/ai/services/projects.py src/pixiv_novel_sync/ai/services/chat_wizard.py src/pixiv_novel_sync/templates/dashboard_ai.html src/pixiv_novel_sync/templates/dashboard_wizard.html tests/test_ai_prompts.py tests/test_ai_model_router_integration.py tests/test_frontend_library_os.py
git commit -m "feat: inject preferences into AI writing flows"
```

---

### Task 5: 自动调度迁移到共享 JobRunner

**Files:**
- Modify: `src/pixiv_novel_sync/web/utils.py`
- Modify: `src/pixiv_novel_sync/web/managers.py`
- Modify: `src/pixiv_novel_sync/web/__init__.py`
- Modify: `src/pixiv_novel_sync/webapp.py`
- Modify: `src/pixiv_novel_sync/jobs/quick_sync.py`
- Modify: `tests/test_webapp_jobs.py`
- Modify: `tests/test_jobs_runner.py`

**Interfaces:**
- Produces `_job_spec(task_list, source, params=None) -> JobSpec`; `_web_job_spec` delegates with `JobSource.WEB`, `_scheduler_job_spec` delegates with `JobSource.SCHEDULER`.
- `AutoSyncScheduler` consumes callbacks `submit_task(settings, task_name) -> JobState | None`, `run_task(job_id) -> None`, `get_task(job_id) -> JobState | None`, and `cancel_task(job_id) -> bool`.
- Scheduler no longer contains `_sync_*` business methods or owns `SyncJobState` terminal transitions.

- [ ] **Step 1: Write failing shared-scheduler tests**

```python
def test_scheduler_job_spec_uses_shared_source_and_type():
    spec = _scheduler_job_spec("pending_deletion_detection")
    assert spec.source == JobSource.SCHEDULER
    assert spec.job_type == JobType.PENDING_DELETION_DETECTION
    assert spec.task_types == ["pending_deletion_detection"]


def test_auto_scheduler_runs_submitted_shared_job():
    calls = []
    state = SimpleNamespace(job_id="shared-1", status=JobStatus.QUEUED)
    scheduler = AutoSyncScheduler(
        None,
        None,
        submit_task=lambda settings, task: calls.append(("submit", task)) or state,
        run_task=lambda job_id: calls.append(("run", job_id)),
        get_task=lambda job_id: state,
        cancel_task=lambda job_id: calls.append(("cancel", job_id)) or True,
    )
    scheduler._running = True
    scheduler._run_single_task(object(), "bookmarks")
    assert calls == [("submit", "bookmarks"), ("run", "shared-1")]


def test_stop_current_auto_task_requests_shared_cancel():
    cancelled = []
    scheduler = AutoSyncScheduler(
        None,
        None,
        cancel_task=lambda job_id: cancelled.append(job_id) or True,
    )
    scheduler._current_task_job_id = "shared-1"
    assert scheduler.stop_current_task() is True
    assert cancelled == ["shared-1"]
```

Also update route tests to assert `/auto-sync/status` serializes a shared `JobState` and health running count reads only the shared manager.

- [ ] **Step 2: Run tests to verify RED**

```powershell
python -m pytest tests/test_webapp_jobs.py tests/test_jobs_runner.py -q
```

Expected: FAIL because scheduler still starts `SyncJobState` and calls private `_sync_*` methods.

- [ ] **Step 3: Extract shared submission and remove duplicate execution**

Generalize submission so automatic jobs can be created without spawning another scheduler thread:

```python
def _submit_shared_job(spec, settings, task_type, task_name, *, is_auto_sync=False, run_async=True):
    if _has_any_running_web_job():
        raise RuntimeError("已有同步任务正在运行，请稍后再试")
    db = Database(settings.storage.db_path)
    try:
        db.init_schema()
        job = shared_job_manager.submit(spec)
        job.progress["log_id"] = db.create_task_log(
            task_type=task_type,
            task_name=task_name,
            job_id=job.job_id,
            is_auto_sync=is_auto_sync,
        )
    finally:
        db.close()
    if run_async:
        threading.Thread(target=_run_shared_web_job, args=(job.job_id,), daemon=True).start()
    return job
```

The scheduler callback creates `JobSource.SCHEDULER`, `is_auto_sync=True`, `run_async=False`; its scheduler thread calls `_run_shared_web_job(job_id)` synchronously. `stop_current_task()` calls shared `request_cancel`. Remove the `_sync_*` methods and production-zero `SyncJobManager` worker state after all task types are represented by `execute_task`. Keep `_job_to_dict_unified` compatibility fields for shared states.

- [ ] **Step 4: Run scheduler, job, CLI and log regressions**

```powershell
python -m pytest tests/test_webapp_jobs.py tests/test_jobs_runner.py tests/test_jobs_tasks.py tests/test_jobs_services.py tests/test_cli_jobs.py tests/test_unified_task_logs.py tests/test_task_logs_routes.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/web/utils.py src/pixiv_novel_sync/web/managers.py src/pixiv_novel_sync/web/__init__.py src/pixiv_novel_sync/webapp.py src/pixiv_novel_sync/jobs/quick_sync.py tests/test_webapp_jobs.py tests/test_jobs_runner.py
git commit -m "refactor: run scheduled syncs through shared jobs"
```

---

### Task 6: 完整执行成人描写局部润色 Agent 计划

**Plan:** `docs/superpowers/plans/2026-07-23-adult-polish-agent.md`

**Dependency check:** 当前 `src/pixiv_novel_sync/ai/model_router.py` 已提供 `ModelCandidate`、`CandidateSnapshot`、`RouteRequest`、`RouteResult`、`stage='validation'`、`resolve_candidates()` 和 `execute()/execute_stream()`。实现时以当前 DTO 的额外 `capabilities/context_window/fallback_depth/candidate_index` 字段为事实来源，不在成人模块复制 DTO。

- [ ] **Step 1: Execute adult plan Tasks 1-5 with their RED/GREEN commands and commits**

Expected deliverables: domain types/policies, atomic schema/storage, adult fictional-character confirmation, isolated bindings, prompt construction and deterministic local validation.

- [ ] **Step 2: Execute adult plan Tasks 6-8 with their RED/GREEN commands and commits**

Expected deliverables: buffered three-stage `ModelRouter` orchestration, fixed safety/fact reviews, atomic candidate finalization, optimistic apply and cleanup.

- [ ] **Step 3: Execute adult plan Tasks 9-10 with their RED/GREEN commands and commits**

Expected deliverables: authenticated owner-scoped routes/SSE and the chapter reader/settings UI. Add `preference_profile_id` and strength to the adult writing request using Task 3's bounded context helper; fixed reviews do not receive preference instructions.

- [ ] **Step 4: Run the complete adult test family**

```powershell
python -m pytest tests/test_ai_adult_*.py -q
```

Expected: PASS with no candidate text in captured logs, blocked SSE events or failed/partial job storage.

---

### Task 7: API 契约、需求状态和完整验收

**Files:**
- Modify: `docs/frontend-api-contract.md`
- Modify: `docs/frontend-pages.md`
- Modify: `docs/UNIFIED_PROJECT_REQUIREMENTS.md`
- Modify: `README.md`
- Modify: `tests/test_frontend_library_os.py`
- Modify: `tests/test_ai_model_docs.py`
- Create: `tests/test_api_contract_routes.py`

**Interfaces:**
- Current contract documents every frontend-used route registered by `webapp.py`, `ai_web.py`, `preference_web.py` and adult routes.
- Requirement statuses change from `PARTIAL/PLANNED` only when corresponding executable tests pass.

- [ ] **Step 1: Write failing route/contract parity test**

```python
REQUIRED_CURRENT_ROUTES = {
    "/api/health",
    "/api/dashboard/novels/export-epub",
    "/api/dashboard/ai/style-profiles",
    "/api/dashboard/ai/novel-profiles",
    "/api/dashboard/ai/projects/<int:project_id>/context-preview",
    "/api/dashboard/ai/polish/adult/stream",
}

def test_frontend_contract_mentions_required_current_routes():
    contract = CONTRACT.read_text(encoding="utf-8")
    missing = {route for route in REQUIRED_CURRENT_ROUTES if route not in contract}
    assert not missing
```

Include reading-progress CRUD, export stats, adult review bindings/candidate/apply routes and every route referenced by Dashboard templates.

- [ ] **Step 2: Run tests to verify RED**

```powershell
python -m pytest tests/test_api_contract_routes.py tests/test_frontend_library_os.py tests/test_ai_model_docs.py -q
```

Expected: FAIL listing undocumented current routes.

- [ ] **Step 3: Update current docs and status markers**

Document method, path, auth/CSRF, request fields, response envelope and relevant `403/409` behavior. Mark recommendation injection and adult Agent complete only after their focused tests pass; retain truthful notes for any unrelated requirement still partial.

- [ ] **Step 4: Run full verification**

```powershell
python -m pytest -q
python -m compileall -q src
git diff --check
git status --short
```

Expected: pytest reports zero failures (environment-only skips may remain), compileall and diff check exit 0, status contains only intended tracked changes before the final commit.

- [ ] **Step 5: Commit**

```powershell
git add README.md docs/frontend-api-contract.md docs/frontend-pages.md docs/UNIFIED_PROJECT_REQUIREMENTS.md tests/test_frontend_library_os.py tests/test_ai_model_docs.py tests/test_api_contract_routes.py
git commit -m "docs: close audited requirements and API contract"
```

## Self-review checklist

- Spec coverage: Tasks 1-5 cover cancellation/loops, recommendation fields/dedupe, preference injection and scheduler unification; Task 6 incorporates all 11 adult tasks; Task 7 closes docs and full verification.
- Placeholder scan: every code block contains executable statements or exact signatures; no product decision remains unresolved.
- Type consistency: uses existing `JobSource.SCHEDULER`, current `ModelRouter` DTOs, `risk_notes`, and four exact preference strengths.
- Ordering: preference context exists before adult prompt integration; shared scheduler exists before legacy removal; documentation is last.
- Security: adult safety/fact policies remain fixed and fail-closed, preference context excludes source text, logs exclude secrets and candidate bodies.

# Recommendation Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成偏好分析范围、搜索计划 CRUD、推荐原子发布、反馈/屏蔽/队列、立即同步和流式任务接口。

**Architecture:** SQL 层执行范围过滤；版本化 search plan 使用 CAS；候选在内存 staging，成功时单事务发布。所有耗时操作复用共享 JobRunner，SSE 只投影 job 事件。

**Tech Stack:** Python、SQLite、Flask、Vue CDN 模板、pytest。

## Global Constraints

- AI 不可用时本计划所有本地能力仍可用。
- 推荐 score 始终由确定性规则控制。
- API 遵循现有认证、CSRF、分页和 `{ok,data,error}` 契约。
- 所有行为先有 RED 测试。

---

### Task 1: 版本化分析范围

**Files:**
- Modify: `src/pixiv_novel_sync/preferences.py`
- Modify: `src/pixiv_novel_sync/storage/recommendations.py`
- Modify: `src/pixiv_novel_sync/jobs/tasks.py`
- Modify: `src/pixiv_novel_sync/preference_web.py`
- Modify: `src/pixiv_novel_sync/templates/dashboard_preferences.html`
- Test: `tests/test_preferences.py`
- Test: `tests/test_preference_jobs.py`

**Interfaces:**
- Produces: `PreferenceAnalysisScope.from_payload(payload) -> PreferenceAnalysisScope`
- Produces: `fetch_unanalyzed_preference_rows(scope, batch_size) -> list[dict]`

- [ ] **Step 1: 写 source/author/tag/date/visibility 组合过滤 RED**

```python
def test_preference_scope_filters_sources_author_tag_date_and_visibility(db):
    seed_preference_scope_rows(db)
    scope = PreferenceAnalysisScope.from_payload({
        "source_types": ["bookmark_private"], "author_ids": [7],
        "tags": ["恋爱"], "created_from": "2026-01-01",
        "exclude_unavailable": True, "min_text_length": 1000,
    })
    assert [row["novel_id"] for row in db.fetch_preference_source_rows(scope)] == [101]
```

- [ ] **Step 2: 写 scope fingerprint 改变时重建累加器 RED**

不同 scope 不得共享 `preference_analyzed_novels` 进度。

- [ ] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_preferences.py tests/test_preference_jobs.py -q`
Expected: FAIL，当前仅支持 min length/batch size。

- [ ] **Step 4: 实现 frozen dataclass、严格上限和参数化 SQL**

scope version 固定为 1；作者最多 100、标签最多 50；日期 ISO 校验；source 类型枚举由现有来源常量生成。

- [ ] **Step 5: UI 增加范围控件并提交完整 scope**

使用 checkbox/select/date input，不允许自由字符串伪造 source 类型。

- [ ] **Step 6: 运行 GREEN 并提交**

Run: `python -m pytest tests/test_preferences.py tests/test_preference_jobs.py tests/test_frontend_library_os.py -q`

```bash
git add src/pixiv_novel_sync/preferences.py src/pixiv_novel_sync/storage/recommendations.py src/pixiv_novel_sync/jobs/tasks.py src/pixiv_novel_sync/preference_web.py src/pixiv_novel_sync/templates/dashboard_preferences.html tests
git commit -m "feat: add complete preference analysis scopes"
```

### Task 2: 搜索计划存储和 CAS CRUD

**Files:**
- Modify: `src/pixiv_novel_sync/storage/schema.py`
- Modify: `src/pixiv_novel_sync/storage/recommendations.py`
- Modify: `src/pixiv_novel_sync/preference_web.py`
- Modify: `src/pixiv_novel_sync/templates/dashboard_preferences.html`
- Test: `tests/test_recommendation_search_plans.py`

**Interfaces:**
- Produces: `create_recommendation_search_plan(profile_id, name, plan) -> int`
- Produces: `cas_update_recommendation_search_plan(plan_id, expected_version, data) -> dict`

- [ ] **Step 1: 写迁移、CRUD、去重和 stale version RED**

```python
def test_search_plan_cas_and_query_dedup(db):
    plan_id = db.create_recommendation_search_plan(profile_id, "默认", duplicated_plan())
    saved = db.get_recommendation_search_plan(plan_id)
    assert [q["query"] for q in saved["plan"]["queries"]] == ["A", "B"]
    with pytest.raises(ConflictError):
        db.cas_update_recommendation_search_plan(plan_id, 0, {"name": "stale"})
```

- [ ] **Step 2: 运行 RED**

Run: `python -m pytest tests/test_recommendation_search_plans.py -q`
Expected: FAIL，表和方法不存在。

- [ ] **Step 3: 实现表、normalizer、CRUD 路由和编辑 UI**

plan schema 限制 50 queries、query 200 字符、exclude terms 50、limit 1..100；删除运行中引用返回 409。

- [ ] **Step 4: 运行 GREEN 并提交**

Run: `python -m pytest tests/test_recommendation_search_plans.py tests/test_recommendations.py -q`

```bash
git add src/pixiv_novel_sync/storage/schema.py src/pixiv_novel_sync/storage/recommendations.py src/pixiv_novel_sync/preference_web.py src/pixiv_novel_sync/templates/dashboard_preferences.html tests/test_recommendation_search_plans.py
git commit -m "feat: persist editable recommendation search plans"
```

### Task 3: 推荐取消和 run 原子发布

**Files:**
- Modify: `src/pixiv_novel_sync/recommendations.py`
- Modify: `src/pixiv_novel_sync/storage/recommendations.py`
- Modify: `src/pixiv_novel_sync/jobs/tasks.py`
- Test: `tests/test_recommendations.py`
- Test: `tests/test_jobs_tasks.py`

**Interfaces:**
- Produces: `publish_recommendation_run(run_id, profile_id, items, stats) -> list[int]`

- [ ] **Step 1: 写取消最终为 CANCELLED 的 RED**

推荐 progress 首次触发取消；断言 runner status 为 `cancelled` 而非 `succeeded`。

- [ ] **Step 2: 写失败/取消不改变旧 item 的 RED**

```python
def test_failed_run_never_changes_visible_recommendations(db, service):
    old = seed_visible_recommendation(db, title="old")
    service.api = failing_after_first_candidate_api()
    with pytest.raises(RuntimeError):
        service.run()
    assert db.get_recommendation_item(old)["title"] == "old"
```

- [ ] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_recommendations.py tests/test_jobs_tasks.py -q`
Expected: FAIL，当前逐项 commit 且取消被吞掉。

- [ ] **Step 4: staging 到有界 list，成功时单事务 publish**

publish 验证 run=`running`，批量 upsert 时不覆盖 status/反馈，再在同一事务更新 succeeded。异常只更新 run failed；`InterruptedError` 原样传播。

- [ ] **Step 5: 运行 GREEN 并提交**

Run: `python -m pytest tests/test_recommendations.py tests/test_jobs_tasks.py tests/test_jobs_runner.py -q`

```bash
git add src/pixiv_novel_sync/recommendations.py src/pixiv_novel_sync/storage/recommendations.py src/pixiv_novel_sync/jobs/tasks.py tests/test_recommendations.py tests/test_jobs_tasks.py
git commit -m "fix: publish recommendation runs atomically"
```

### Task 4: 反馈枚举、队列和立即同步

**Files:**
- Modify: `src/pixiv_novel_sync/jobs/models.py`
- Modify: `src/pixiv_novel_sync/jobs/tasks.py`
- Modify: `src/pixiv_novel_sync/storage/recommendations.py`
- Modify: `src/pixiv_novel_sync/preference_web.py`
- Test: `tests/test_recommendation_feedback.py`
- Test: `tests/test_recommendation_sync.py`

- [ ] **Step 1: 写非法 feedback 400、tag mute、跨 run series 去重 RED**

允许枚举固定为 `interested|dismissed|read_later|sync_later|synced`；未知值必须 400 且不写库。

- [ ] **Step 2: 写立即同步返回 job 且不阻塞 RED**

```python
def test_sync_recommendation_returns_shared_job(client, seeded_item):
    response = client.post(f"/api/dashboard/recommendations/items/{seeded_item}/sync", json={})
    assert response.status_code == 200
    assert response.json["data"]["job_id"]
```

- [ ] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_recommendation_feedback.py tests/test_recommendation_sync.py -q`
Expected: FAIL，枚举和 sync route 不完整。

- [ ] **Step 4: 实现 feedback CRUD、屏蔽 CRUD、历史删除、状态筛选和 `RECOMMENDATION_SYNC` job**

反馈列表/创建/更新/删除与 author/tag mute 创建/删除都执行固定枚举校验；推荐历史、反馈和屏蔽分别提供用户可控删除入口。任务复用 Pixiv auth、单篇/系列归档与救援刷新；成功后状态为 synced，取消保持 sync_later。

- [ ] **Step 5: 运行 GREEN 并提交**

Run: `python -m pytest tests/test_recommendation_feedback.py tests/test_recommendation_sync.py tests/test_recommendations.py -q`

```bash
git add src/pixiv_novel_sync/jobs src/pixiv_novel_sync/storage/recommendations.py src/pixiv_novel_sync/preference_web.py tests/test_recommendation_feedback.py tests/test_recommendation_sync.py
git commit -m "feat: complete recommendation feedback and sync queues"
```

### Task 5: run 详情、分析/计划/推荐 SSE 与页面完整工作流

**Files:**
- Modify: `src/pixiv_novel_sync/preference_web.py`
- Modify: `src/pixiv_novel_sync/jobs/manager.py`
- Modify: `src/pixiv_novel_sync/templates/dashboard_preferences.html`
- Modify: `docs/frontend-api-contract.md`
- Test: `tests/test_preference_streams.py`
- Test: `tests/test_ai_adult_frontend.py`

- [ ] **Step 1: 写 run 详情、SSE 白名单、终态和断开不取消 RED**

新增 `GET /api/dashboard/recommendations/runs/<run_id>`；不存在返回 404。流端点为 `/profiles/analyze/stream`、`/search-plan/stream`、`/run/stream`；事件只允许 metadata/progress/done/cancelled/error。

- [ ] **Step 2: 运行 RED**

Run: `python -m pytest tests/test_preference_streams.py -q`
Expected: FAIL，路由不存在。

- [ ] **Step 3: 为 JobManager 增加有界 event snapshot 和 condition wait**

不轮询固定 sleep；断开只关闭订阅。显式 cancel 继续走共享取消接口。

- [ ] **Step 4: 页面改用 EventSource/fetch stream 并补齐 plan、tag mute、read/sync 操作**

所有按钮有 busy/error/empty/terminal 状态；移动端控件换行且无嵌套卡片。

- [ ] **Step 5: 运行 GREEN 并提交**

Run: `python -m pytest tests/test_preference_streams.py tests/test_frontend_library_os.py -q`

```bash
git add src/pixiv_novel_sync/preference_web.py src/pixiv_novel_sync/jobs/manager.py src/pixiv_novel_sync/templates/dashboard_preferences.html docs/frontend-api-contract.md tests/test_preference_streams.py tests/test_frontend_library_os.py
git commit -m "feat: stream preference and recommendation jobs"
```

### Task 6: 域验证

- [ ] Run: `python -m pytest tests/test_preferences.py tests/test_preference_jobs.py tests/test_recommendation_search_plans.py tests/test_recommendations.py tests/test_recommendation_feedback.py tests/test_recommendation_sync.py tests/test_preference_streams.py -q`
- [ ] Run: `python -m compileall -q src tests`
- [ ] Run: `git diff --check`

# AI Preference And Adult Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现可选 AI 偏好总结/推荐解释、成人偏好注入、成人实时 progress 和端到端取消。

**Architecture:** 两个专用 Agent 均通过 ModelRouter 输出严格 JSON并可降级。成人流程直接消费 `execute_stream()`，只实时转发 progress，delta 始终在服务端缓冲；三阶段共享 owner-scoped cancel callback。

**Tech Stack:** Python、ModelRouter、SQLite、Flask SSE、Vue 模板、pytest。

## Global Constraints

- 未配置 AI 时本地画像和推荐 run 成功。
- AI explanation 不得修改 score、过滤或 identity。
- 成人候选只有全部审查通过后才能发送和持久化。
- 所有新增行为先有 RED。

---

### Task 1: 可选结构化 AI 偏好总结

**Files:**
- Create: `src/pixiv_novel_sync/ai/preference_summary.py`
- Modify: `src/pixiv_novel_sync/jobs/tasks.py`
- Modify: `src/pixiv_novel_sync/preferences.py`
- Modify: `src/pixiv_novel_sync/storage/tasks.py`
- Test: `tests/test_ai_preference_summary.py`

**Interfaces:**
- Produces: `build_preference_summary_request(profile, samples, agent_id) -> RouteRequest`
- Produces: `merge_ai_preference_summary(local_profile, raw_json) -> dict`

- [ ] **Step 1: 写合法 JSON 合并、非法 JSON 降级、无 Agent 降级 RED**

```python
def test_invalid_ai_summary_preserves_local_profile(local_profile):
    result = merge_ai_preference_summary(local_profile, "not-json")
    assert result["summary"] == local_profile["summary"]
    assert result["ai_summary_status"] == "invalid"
```

- [ ] **Step 2: 写主题/关系/情境/语气/节奏/叙事字段均有消费者 RED**

`build_preference_context()` 必须按 strength 有界消费这些字段。

- [ ] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_ai_preference_summary.py tests/test_ai_preference_context.py -q`
Expected: FAIL，新模块和字段不存在。

- [ ] **Step 4: 实现 64KiB strict schema、采样预算和 ModelRouter 调用**

Provider 失败记录 `unavailable`，取消记录 `cancelled`，但已完成本地画像仍保存。

- [ ] **Step 5: 运行 GREEN 并提交**

Run: `python -m pytest tests/test_ai_preference_summary.py tests/test_ai_preference_context.py tests/test_preference_jobs.py -q`

```bash
git add src/pixiv_novel_sync/ai/preference_summary.py src/pixiv_novel_sync/jobs/tasks.py src/pixiv_novel_sync/preferences.py src/pixiv_novel_sync/storage/tasks.py tests
git commit -m "feat: add optional structured preference summaries"
```

### Task 2: 可选 AI 推荐解释

**Files:**
- Create: `src/pixiv_novel_sync/ai/recommendation_explanation.py`
- Modify: `src/pixiv_novel_sync/recommendations.py`
- Modify: `src/pixiv_novel_sync/storage/schema.py`
- Modify: `src/pixiv_novel_sync/storage/recommendations.py`
- Test: `tests/test_ai_recommendation_explanations.py`

- [ ] **Step 1: 写 explanation 不得修改 score/identity/risk RED**

```python
def test_ai_explanation_can_only_replace_explanation(candidate):
    enriched = apply_ai_explanations([candidate], {str(candidate["novel_id"]): {"reason": "AI说明"}})[0]
    assert enriched["score"] == candidate["score"]
    assert enriched["novel_id"] == candidate["novel_id"]
    assert enriched["risk_notes"] == candidate["risk_notes"]
```

- [ ] **Step 2: 写 Provider/parse/缺项时逐条 local fallback RED**

- [ ] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_ai_recommendation_explanations.py -q`
Expected: FAIL。

- [ ] **Step 4: 实现批次 JSON、`explanation_source` 和脱敏 route snapshot**

每批最多 20 项；输出按 item key 关联；任何未知字段丢弃。

- [ ] **Step 5: 运行 GREEN 并提交**

Run: `python -m pytest tests/test_ai_recommendation_explanations.py tests/test_recommendations.py -q`

```bash
git add src/pixiv_novel_sync/ai/recommendation_explanation.py src/pixiv_novel_sync/recommendations.py src/pixiv_novel_sync/storage tests/test_ai_recommendation_explanations.py
git commit -m "feat: add optional AI recommendation explanations"
```

### Task 3: 成人偏好端到端注入

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/adult.py`
- Modify: `src/pixiv_novel_sync/ai/adult_prompt.py`
- Modify: `src/pixiv_novel_sync/storage/ai/adult.py`
- Modify: `src/pixiv_novel_sync/templates/dashboard_ai_reader.html`
- Test: `tests/test_ai_adult_prompt.py`
- Test: `tests/test_ai_adult_frontend.py`
- Test: `tests/test_ai_adult_apply.py`

- [ ] **Step 1: 写 off/light/standard/strong Prompt 差异 RED**

```python
@pytest.mark.parametrize("strength,expected", [("off", False), ("light", True), ("standard", True), ("strong", True)])
def test_adult_prompt_injects_bounded_preference_context(strength, expected):
    prompt = build_adult_prompt(preference_profile=profile(), preference_strength=strength)
    assert ("偏好上下文" in prompt) is expected
```

- [ ] **Step 2: 写画像变化后 apply 409 RED**

job 快照保存 context hash；生成后修改/删除画像，apply 必须冲突。

- [ ] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_ai_adult_prompt.py tests/test_ai_adult_apply.py tests/test_ai_adult_frontend.py -q`
Expected: FAIL，字段当前未加载/发送。

- [ ] **Step 4: 复用 preference context builder 并写入审计 hash**

只注入摘要/结构字段；缺失画像与 off 保持旧 Prompt；页面发送 profile ID 和 strength。

- [ ] **Step 5: 运行 GREEN 并提交**

Run: `python -m pytest tests/test_ai_adult_prompt.py tests/test_ai_adult_apply.py tests/test_ai_adult_frontend.py -q`

```bash
git add src/pixiv_novel_sync/ai/services/adult.py src/pixiv_novel_sync/ai/adult_prompt.py src/pixiv_novel_sync/storage/ai/adult.py src/pixiv_novel_sync/templates/dashboard_ai_reader.html tests
git commit -m "feat: inject preferences into adult polish"
```

### Task 4: 成人实时 progress 和取消传播

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/adult.py`
- Modify: `src/pixiv_novel_sync/ai_web.py`
- Modify: `src/pixiv_novel_sync/storage/ai/core.py`
- Test: `tests/test_ai_adult_generation.py`
- Test: `tests/test_ai_adult_review.py`
- Test: `tests/test_ai_adult_web.py`

- [ ] **Step 1: 写 main progress 在 completion 前可见 RED**

fake router 依次 yield progress、delta、completion；读取第一个 service chunk 必须是 progress，且 candidate 尚未出现。

- [ ] **Step 2: 写 main/safety/fact_guard 全部收到 is_cancelled RED**

捕获每个 `AdultRouteRequest.is_cancelled`，显式取消后都返回 True；iterator.close 被调用。

- [ ] **Step 3: 写断连不泄漏 delta 且 job/attempt cancelled RED**

- [ ] **Step 4: 运行 RED**

Run: `python -m pytest tests/test_ai_adult_generation.py tests/test_ai_adult_review.py tests/test_ai_adult_web.py -q`
Expected: FAIL，当前同步 execute 缓冲 progress 且 request 未带 callback。

- [ ] **Step 5: 成人服务直接消费 `execute_stream()`**

progress 白名单 yield；delta 追加有界缓冲；completion 后才进入审查。review runner 接收 progress callback 和 cancel callback。

- [ ] **Step 6: Web GeneratorExit 关闭底层 iterator 并 owner-CAS 取消**

重复取消幂等；终态 job 不被覆盖。

- [ ] **Step 7: 运行 GREEN 并提交**

Run: `python -m pytest tests/test_ai_adult_generation.py tests/test_ai_adult_review.py tests/test_ai_adult_web.py tests/test_ai_model_router.py -q`

```bash
git add src/pixiv_novel_sync/ai/services/adult.py src/pixiv_novel_sync/ai_web.py src/pixiv_novel_sync/storage/ai/core.py tests
git commit -m "fix: stream adult progress and propagate cancellation"
```

### Task 5: 域验证和文档

- [ ] 更新 `docs/ADULT_POLISH_USER_GUIDE.md`、`docs/frontend-api-contract.md`、`docs/PREFERENCE_RECOMMENDER_REQUIREMENTS.md`。
- [ ] Run: `python -m pytest tests/test_ai_preference_summary.py tests/test_ai_recommendation_explanations.py tests/test_ai_adult_prompt.py tests/test_ai_adult_apply.py tests/test_ai_adult_generation.py tests/test_ai_adult_review.py tests/test_ai_adult_web.py -q`
- [ ] Run: `python -m compileall -q src tests`
- [ ] Run: `git diff --check`


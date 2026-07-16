# 偏好与任务日志闭环实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AI 清洗关键词进入真实搜索策略，并让同步任务与 AI 创作任务在统一任务日志页获得完整筛选和详情能力。

**Architecture:** 保留 `task_logs` 与 `ai_jobs` 两张表，通过共享偏好分析入口消除手动/定时重复实现，通过统一投影和现有 AI 详情接口完成日志页闭环。AI 不可用时偏好分析按原始关键词降级，不改变任务成功语义。

**Tech Stack:** Python 3.10+、Flask、SQLite、Vue 3、pytest

## Global Constraints

- 不迁移历史 `recommendation_runs` 或旧版 AI 日志。
- 不新增前端构建工具或 SPA 路由。
- AI 清洗失败必须降级，不能让偏好分析失败。
- 日志默认保留周期固定为 3 天。
- 除专有名词、代码标识和协议缩写外，用户可见文本使用中文。
- 严格执行 TDD：先失败测试，再最小实现，再回归和提交。

---

## 文件结构

- 修改 `src/pixiv_novel_sync/preferences.py`：统一最终关键词选择并公开画像重建入口。
- 修改 `src/pixiv_novel_sync/jobs/tasks.py`：清洗后重新生成画像，作为手动和定时分析的唯一业务入口。
- 修改 `src/pixiv_novel_sync/web/managers.py`：定时分析委托给统一任务入口。
- 修改 `src/pixiv_novel_sync/templates/dashboard_settings.html`：补充 `keyword_clean` Agent 类型。
- 修改 `src/pixiv_novel_sync/storage/tasks.py`：完善 AI 日志投影、状态过滤和类型映射。
- 修改 `src/pixiv_novel_sync/webapp.py`：把 AI 状态筛选传入存储层。
- 修改 `src/pixiv_novel_sync/templates/dashboard_logs.html`：补状态筛选和完整 AI 详情。
- 修改 `src/pixiv_novel_sync/ai/services/admin.py`：统一 3 天默认值。
- 修改 `tests/test_preferences.py`、`tests/test_preference_jobs.py`、`tests/test_recommendations.py`：覆盖清洗词进入画像和搜索计划。
- 修改 `tests/test_unified_task_logs.py`，创建 `tests/test_task_logs_routes.py`：覆盖 AI 列表、状态和详情。

### Task 1: 让画像和搜索计划使用清洗关键词

**Files:**
- Modify: `src/pixiv_novel_sync/preferences.py:153-215`
- Modify: `src/pixiv_novel_sync/jobs/tasks.py:262-345`
- Test: `tests/test_preferences.py`
- Test: `tests/test_recommendations.py`

**Interfaces:**
- Produces: `PreferenceAnalyzer.build_profile(stats: dict[str, Any]) -> dict[str, Any]`
- Produces: `PreferenceAnalyzer.effective_keywords(stats: dict[str, Any]) -> list[str]`
- Consumes: `stats.refined_keywords`，为空时回退 `stats.top_keywords[*].name`

- [ ] **Step 1: 写画像优先使用清洗词的失败测试**

```python
def test_build_profile_prefers_refined_keywords(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.init_schema()
    analyzer = PreferenceAnalyzer(db)
    stats = {
        "novel_count": 10,
        "total_chars": 100_000,
        "series_novel_count": 0,
        "single_novel_count": 10,
        "avg_text_length": 10_000,
        "top_tags": [{"name": "百合", "count": 8}],
        "top_keywords": [{"name": "她的", "count": 50}, {"name": "了一", "count": 40}],
        "refined_keywords": ["校园恋爱", "百合"],
    }

    profile = analyzer.build_profile(stats)

    assert profile["positive_preferences"]["keywords"] == ["校园恋爱", "百合"]
    assert "她的" not in profile["summary"]
    assert all("她的" not in query for query in profile["search_strategy"]["precise_queries"])
    db.close()
```

- [ ] **Step 2: 写搜索计划使用清洗词的失败测试**

```python
def test_search_plan_uses_profile_rebuilt_from_refined_keywords(db, settings):
    analyzer = PreferenceAnalyzer(db)
    stats = {
        "novel_count": 5,
        "total_chars": 50_000,
        "series_novel_count": 0,
        "single_novel_count": 5,
        "avg_text_length": 10_000,
        "top_tags": [{"name": "校园", "count": 5}],
        "top_keywords": [{"name": "她的", "count": 20}],
        "refined_keywords": ["秘密恋爱"],
    }
    profile = {"id": 1, "profile": analyzer.build_profile(stats)}

    plan = RecommendationService(db, settings).build_search_plan(profile)

    queries = [item["query"] for item in plan["queries"]]
    assert any("秘密恋爱" in query for query in queries)
    assert all("她的" not in query for query in queries)
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_preferences.py::test_build_profile_prefers_refined_keywords tests/test_recommendations.py::test_search_plan_uses_profile_rebuilt_from_refined_keywords -q`

Expected: FAIL，提示 `PreferenceAnalyzer` 没有公开的 `build_profile`，或画像仍包含原始噪声词。

- [ ] **Step 4: 实现最终关键词选择和公开画像构建入口**

```python
def effective_keywords(self, stats: dict[str, Any]) -> list[str]:
    refined = stats.get("refined_keywords")
    if isinstance(refined, list):
        cleaned = [str(item).strip() for item in refined if str(item).strip()]
        if cleaned:
            return cleaned
    return [
        str(item.get("name") or "").strip()
        for item in stats.get("top_keywords", [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]

def build_profile(self, stats: dict[str, Any]) -> dict[str, Any]:
    return self._build_profile(stats)
```

同时把 `_build_profile()` 中的 `top_keywords` 初始化改为：

```python
top_keywords = self.effective_keywords(stats)[:30]
```

- [ ] **Step 5: 清洗成功后重新构建画像**

在 `jobs/tasks.py` 清洗块之后、保存画像之前加入：

```python
rebuilt["profile"] = analyzer.build_profile(rebuilt["stats"])
```

该语句无论清洗是否成功都执行，保证 `stats` 与 `profile` 来自同一份最终数据。

- [ ] **Step 6: 运行画像和推荐测试**

Run: `python -m pytest tests/test_preferences.py tests/test_recommendations.py tests/test_keyword_clean.py -q`

Expected: PASS。

- [ ] **Step 7: 提交**

```powershell
git add src/pixiv_novel_sync/preferences.py src/pixiv_novel_sync/jobs/tasks.py tests/test_preferences.py tests/test_recommendations.py
git commit -m "fix: use refined keywords in recommendation profiles"
```

### Task 2: 让定时与手动分析复用同一流程

**Files:**
- Modify: `src/pixiv_novel_sync/web/managers.py:716-767`
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings.html:350-410`
- Test: `tests/test_preference_jobs.py`
- Test: `tests/test_webapp_settings.py`

**Interfaces:**
- Consumes: `execute_task("preference_analyze", settings, context)`
- Produces: 定时任务上下文 `{"manager": SyncJobManager, "job_id": str, "params": {"scope": {"max_batches": 1}}}`

- [ ] **Step 1: 写定时分析委托统一入口的失败测试**

```python
from types import SimpleNamespace

from pixiv_novel_sync.web.managers import AutoSyncScheduler


def test_scheduled_preference_analysis_uses_shared_task(monkeypatch):
    class FakeSyncJobManager:
        def add_log(self, _job_id, _level, _message):
            return None

        def is_cancel_requested(self, _job_id):
            return False

    calls = []
    settings = SimpleNamespace(sync=SimpleNamespace(preference_analyze_batch_size=50))
    manager = AutoSyncScheduler(config_path=None, env_path=None, sync_job_manager=FakeSyncJobManager())

    def fake_execute(task_type, current_settings, context):
        calls.append((task_type, current_settings, context))
        return {"processed_this_run": 1}

    monkeypatch.setattr("pixiv_novel_sync.web.managers.execute_task", fake_execute, raising=False)
    manager._sync_preference_analyze(settings, "job-1")

    assert calls[0][0] == "preference_analyze"
    assert calls[0][2]["params"]["scope"]["max_batches"] == 1
```

- [ ] **Step 2: 写设置页包含关键词清洗 Agent 类型的失败测试**

```python
from pathlib import Path


def test_settings_template_exposes_keyword_clean_agent_type():
    html = Path("src/pixiv_novel_sync/templates/dashboard_settings.html").read_text(encoding="utf-8")
    assert '<option value="keyword_clean">关键词清洗</option>' in html
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_preference_jobs.py::test_scheduled_preference_analysis_uses_shared_task tests/test_webapp_settings.py::test_settings_template_exposes_keyword_clean_agent_type -q`

Expected: FAIL，定时路径仍包含独立实现，设置页缺少选项。

- [ ] **Step 4: 把定时实现改为统一任务调用**

在 `web/managers.py` 模块导入 `execute_task`，并把 `_sync_preference_analyze()` 缩减为：

```python
def _sync_preference_analyze(self, settings: Settings, job_id: str | None) -> None:
    execute_task(
        "preference_analyze",
        settings,
        {
            "manager": self.sync_job_manager,
            "job_id": job_id,
            "params": {
                "scope": {
                    "batch_size": int(getattr(settings.sync, "preference_analyze_batch_size", 200) or 200),
                    "max_batches": 1,
                }
            },
        },
    )
```

- [ ] **Step 5: 设置页补充 Agent 类型**

在 Agent 类型下拉中加入：

```html
<option value="keyword_clean">关键词清洗</option>
```

- [ ] **Step 6: 运行偏好任务回归**

Run: `python -m pytest tests/test_preference_jobs.py tests/test_jobs_tasks.py tests/test_webapp_settings.py tests/test_keyword_clean.py -q`

Expected: PASS。

- [ ] **Step 7: 提交**

```powershell
git add src/pixiv_novel_sync/web/managers.py src/pixiv_novel_sync/templates/dashboard_settings.html tests/test_preference_jobs.py tests/test_webapp_settings.py
git commit -m "refactor: share scheduled preference analysis flow"
```

### Task 3: 完善 AI 日志投影和筛选

**Files:**
- Modify: `src/pixiv_novel_sync/storage/tasks.py:130-220`
- Modify: `src/pixiv_novel_sync/webapp.py:1033-1080`
- Modify: `src/pixiv_novel_sync/ai/services/admin.py:286-291`
- Test: `tests/test_unified_task_logs.py`
- Create: `tests/test_task_logs_routes.py`

**Interfaces:**
- Changes: `TasksMixin.get_ai_task_logs(..., status: str | None = None, days: int = 3)`
- Produces: AI 列表项不含伪造数字 `id`，以 `job_id` 为唯一标识。
- Consumes: `Database.get_ai_job(job_id)` 产生的 `input`、`output_text`、`output`。

- [ ] **Step 1: 扩展投影测试**

```python
def test_get_ai_task_logs_filters_status_and_maps_real_types(db: Database) -> None:
    db.create_ai_job("ok", "polish_dialogue", 1, {})
    db.update_ai_job("ok", "succeeded", output_text="done")
    db.create_ai_job("running", "polish_psychology", 1, {})

    result = db.get_ai_task_logs(status="succeeded", days=3)

    assert [item["job_id"] for item in result["items"]] == ["ok"]
    assert result["items"][0]["task_name"] == "对话润色"
    assert "id" not in result["items"][0]
```

- [ ] **Step 2: 写统一路由状态过滤测试**

```python
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.webapp import create_app


def test_global_logs_route_passes_ai_status_filter(tmp_path, monkeypatch):
    captured = {}

    def fake_get_ai_task_logs(self, **kwargs):
        captured.update(kwargs)
        return {"items": [], "page": 1, "page_size": 20, "total": 0, "total_pages": 0}

    monkeypatch.setenv("PIXIV_FLASK_SECRET", "task-log-test-secret")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "storage:\n"
        f"  public_dir: {(tmp_path / 'public').as_posix()}\n"
        f"  private_dir: {(tmp_path / 'private').as_posix()}\n"
        f"  db_path: {(tmp_path / 'task-logs.db').as_posix()}\n"
        "sync:\n"
        "  auto_sync_enabled: false\n",
        encoding="utf-8",
    )
    app = create_app(config_path=str(config_path), env_path=str(env_path))
    monkeypatch.setattr(Database, "get_ai_task_logs", fake_get_ai_task_logs)
    response = app.test_client().get(
        "/api/dashboard/logs?category=ai&status=failed&days=3",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert response.status_code == 200
    assert captured["status"] == "failed"
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_unified_task_logs.py tests/test_task_logs_routes.py -q`

Expected: FAIL，方法不接收 `status` 或真实类型仍显示原始标识。

- [ ] **Step 4: 实现状态过滤和完整类型映射**

给 `get_ai_task_logs()` 增加参数并拼接条件：

```python
if status:
    conditions.append("status = ?")
    params.append(status)
```

补充映射：

```python
"polish_dialogue": "对话润色",
"polish_psychology": "心理描写润色",
"keyword_clean": "关键词清洗",
```

投影字典删除 `"id": None`。`webapp.py` 读取：

```python
status = request.args.get("status") or None
```

并在 AI 分支传入 `status=status`。

- [ ] **Step 5: 统一服务默认保留周期**

```python
def cleanup_jobs(self, keep_days: int = 3, keep_failed_days: int | None = None) -> int:
```

- [ ] **Step 6: 运行存储和路由测试**

Run: `python -m pytest tests/test_unified_task_logs.py tests/test_task_logs_routes.py -q`

Expected: PASS。

- [ ] **Step 7: 提交**

```powershell
git add src/pixiv_novel_sync/storage/tasks.py src/pixiv_novel_sync/webapp.py src/pixiv_novel_sync/ai/services/admin.py tests/test_unified_task_logs.py tests/test_task_logs_routes.py
git commit -m "fix: complete unified AI task log projection"
```

### Task 4: 完成任务日志前端详情

**Files:**
- Modify: `src/pixiv_novel_sync/templates/dashboard_logs.html:15-455`
- Modify: `tests/test_frontend_library_os.py`
- Test: `tests/test_task_logs_routes.py`

**Interfaces:**
- Consumes: `GET /api/dashboard/logs?category=ai&status=...`
- Consumes: `GET /api/dashboard/ai/jobs/<job_id>`，响应主体为 `{ok: true, data: job}`。
- Produces: AI 详情弹窗使用 `job_id`，展示 `input`、`output_text`、`output`。

- [ ] **Step 1: 写模板结构失败测试**

```python
def test_task_logs_template_has_ai_status_and_full_detail_fetch():
    html = read(TEMPLATES / "dashboard_logs.html")
    assert "filters.status" in html
    assert "'/api/dashboard/ai/jobs/'" in html
    assert "selectedLog.job_id || selectedLog.id" in html
    assert '<option value="7">7 天</option>' not in html
    assert "polish_dialogue" in html
    assert "polish_psychology" in html
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_frontend_library_os.py::test_task_logs_template_has_ai_status_and_full_detail_fetch -q`

Expected: FAIL，模板仍直接显示残缺列表行。

- [ ] **Step 3: 添加 AI 状态筛选并移除 7 天选项**

```html
<div v-if="filters.category === 'ai'" class="flex items-center gap-2">
  <label class="text-xs font-medium text-pixiv-gray uppercase whitespace-nowrap">状态</label>
  <select v-model="filters.status" class="pl-3 pr-8 py-1.5 bg-white border border-gray-200 rounded-lg text-sm custom-select">
    <option value="">全部</option>
    <option value="running">运行中</option>
    <option value="succeeded">成功</option>
    <option value="failed">失败</option>
    <option value="cancelled">已取消</option>
  </select>
</div>
```

`filters` 增加 `status: ''`，分类切换时同时清空 `task_type` 和 `status`，两处请求参数构造都在 AI 分类下附加 `status`。

- [ ] **Step 4: AI 行按 job_id 加载完整详情**

把 `showDetail()` 的 AI 分支替换为：

```javascript
if (log?.category === 'ai') {
  detailLoading.value = true;
  try {
    const res = await fetch('/api/dashboard/ai/jobs/' + encodeURIComponent(log.job_id));
    const payload = await res.json();
    if (!res.ok || payload.ok === false) throw new Error(payload.error || '加载详情失败');
    selectedLog.value = { ...log, ...payload.data, category: 'ai' };
  } catch (e) {
    selectedLog.value = { ...log, detail_error: e.message || '详情已清理或不存在' };
  } finally {
    detailLoading.value = false;
  }
  return;
}
```

任务标识改为：

```html
{{ selectedLog.job_id || selectedLog.id }}
```

输入和结构化输出使用 `JSON.stringify(value, null, 2)` 放在 `pre` 中，文本输出使用 `v-text`，不得使用 `v-html`。

- [ ] **Step 5: 补充类型和状态中文映射**

```javascript
'polish_dialogue': '对话润色',
'polish_psychology': '心理描写润色',
'keyword_clean': '关键词清洗',
```

状态表增加：

```javascript
'cancelled': { label: '已取消', type: 'yellow' },
```

- [ ] **Step 6: 运行前端和日志回归**

Run: `python -m pytest tests/test_frontend_library_os.py tests/test_unified_task_logs.py tests/test_task_logs_routes.py tests/test_preference_jobs.py -q`

Expected: PASS。

- [ ] **Step 7: 提交**

```powershell
git add src/pixiv_novel_sync/templates/dashboard_logs.html tests/test_frontend_library_os.py tests/test_task_logs_routes.py
git commit -m "feat: show complete AI task details in task logs"
```

### Task 5: 阶段回归

**Files:**
- Verify only

**Interfaces:**
- Consumes: Tasks 1-4 的全部接口。
- Produces: 可独立交付的偏好与任务日志闭环。

- [ ] **Step 1: 运行定向测试**

Run: `python -m pytest tests/test_preferences.py tests/test_recommendations.py tests/test_keyword_clean.py tests/test_preference_jobs.py tests/test_jobs_tasks.py tests/test_unified_task_logs.py tests/test_task_logs_routes.py tests/test_frontend_library_os.py -q`

Expected: PASS，无新增跳过。

- [ ] **Step 2: 检查差异质量**

Run: `git diff --check HEAD~4..HEAD`

Expected: 无输出。

- [ ] **Step 3: 检查阶段状态**

Run: `git status --short --branch`

Expected: 只保留进入工作区前就存在的用户文件，不出现未提交业务代码。

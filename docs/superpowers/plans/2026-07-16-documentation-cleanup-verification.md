# 文档清理与最终验证实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让文档与最终实现一致，清理确定可再生的缓存，并用完整验证证明四个阶段可以安全推送。

**Architecture:** 文档更新只描述已经落地的接口和页面，不保留误导性的旧设计。缓存清理放在测试之后，最终通过定向测试、完整测试、差异检查和 Git 状态检查形成交付证据。

**Tech Stack:** Markdown、PowerShell、pytest、Git

## Global Constraints

- 不删除 `.env`、`config/config.yaml`、`db.sqlite`、`data/`、`memory/` 或 `.claude/` 个人文件。
- 只清理 `.pytest_cache/`、`**/__pycache__/` 和 `src/pixiv_novel_sync.egg-info/`。
- 文档默认日志保留周期统一为 3 天。
- 文档只写已经实现和验证的行为。
- 推送前必须完整测试通过，既有环境跳过项不得无故增加。

---

## 文件结构

- 修改 `README.md`：日志保留周期、AI 页面和小说库入口。
- 修改 `docs/frontend-api-contract.md`：完整页面路由与接口契约。
- 修改 `docs/frontend-pages.md`：拆分后的页面职责。
- 修改 `docs/AI_WRITING_STUDIO_PLAN.md`：标记旧 AI 内嵌任务历史为历史设计。
- 验证所有源文件、测试和 Git 状态。

### Task 1: 更新 README 和前端接口契约

**Files:**
- Modify: `README.md:140-280,410-425`
- Modify: `docs/frontend-api-contract.md:1-220`
- Test: `tests/test_frontend_library_os.py`

**Interfaces:**
- Consumes: 已实现的 `/dashboard/wizard`、`/dashboard/novels/ai/<id>`、封面接口和统一任务日志接口。
- Produces: 与生产路由一致的文档路由表。

- [ ] **Step 1: 写文档一致性失败测试**

```python
def test_current_frontend_docs_describe_task_logs_and_ai_pages():
    readme = Path("README.md").read_text(encoding="utf-8")
    contract = Path("docs/frontend-api-contract.md").read_text(encoding="utf-8")
    assert "默认保留 3 天" in readme
    assert "保留最近 7 天" not in readme
    assert "| `/dashboard/logs` | `dashboard_logs.html` | 任务日志 |" in contract
    assert "/dashboard/wizard" in contract
    assert "/dashboard/novels/ai/<project_id>" in contract
    assert "/api/dashboard/ai/projects/<project_id>/cover" in contract
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_frontend_library_os.py::test_current_frontend_docs_describe_task_logs_and_ai_pages -q`

Expected: FAIL，README 仍写 7 天且契约缺少新路由。

- [ ] **Step 3: 更新 README**

把清理说明改为：

```markdown
# 清理过期日志（同步任务与 AI 创作任务默认保留最近 3 天）
```

AI 功能入口补充：

```markdown
- `/dashboard/ai`：自动写作项目、全书规划、章节和 Pipeline。
- `/dashboard/wizard`：创作向导与蒸馏档案。
- `/dashboard/novels?category=ai`：AI 创作小说库。
```

- [ ] **Step 4: 更新接口契约页面路由表**

路由表至少包含：

```markdown
| `/dashboard/logs` | `dashboard_logs.html` | 任务日志 |
| `/dashboard/ai` | `dashboard_ai.html` | AI 自动写作 |
| `/dashboard/wizard` | `dashboard_wizard.html` | 创作向导与蒸馏档案 |
| `/dashboard/novels/ai/<project_id>` | `dashboard_ai_reader.html` | AI 创作小说阅读 |
```

补充以下接口的请求方式、主要返回字段和错误语义：

```markdown
- `GET /api/dashboard/logs?category=sync|ai&task_type=&status=&days=1|3`
- `GET /api/dashboard/ai/jobs/<job_id>`
- `POST /api/dashboard/ai/projects/<project_id>/cover`
- `GET /api/dashboard/ai/projects/<project_id>/cover`
- `DELETE /api/dashboard/ai/projects/<project_id>/cover`
```

- [ ] **Step 5: 运行文档一致性测试**

Run: `python -m pytest tests/test_frontend_library_os.py::test_current_frontend_docs_describe_task_logs_and_ai_pages -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add README.md docs/frontend-api-contract.md tests/test_frontend_library_os.py
git commit -m "docs: align task log and AI page contracts"
```

### Task 2: 更新页面说明和 AI 历史设计文档

**Files:**
- Modify: `docs/frontend-pages.md:1-250`
- Modify: `docs/AI_WRITING_STUDIO_PLAN.md:65-110,860-1050`
- Modify: `tests/test_frontend_library_os.py`

**Interfaces:**
- Produces: 当前页面职责说明。
- Produces: 旧“AI 页面内任务历史”明确标记为历史设计。

- [ ] **Step 1: 扩展文档一致性失败测试**

```python
def test_frontend_pages_document_current_ai_boundaries():
    pages = Path("docs/frontend-pages.md").read_text(encoding="utf-8")
    studio = Path("docs/AI_WRITING_STUDIO_PLAN.md").read_text(encoding="utf-8")
    assert "AI 创作小说" in pages
    assert "`/dashboard/wizard`" in pages
    assert "`dashboard_ai_reader.html`" in pages
    assert "AI 创作任务已迁移到全局任务日志" in studio
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_frontend_library_os.py::test_frontend_pages_document_current_ai_boundaries -q`

Expected: FAIL，页面说明仍是旧综合工作台。

- [ ] **Step 3: 更新 frontend-pages.md**

页面总览和详情增加：

```markdown
### `/dashboard/ai`

用途：AI 自动写作项目、全书规划、章节工作区、伏笔、状态记忆和 Pipeline。

### `/dashboard/wizard`

用途：创作向导会话、素材导入、项目导入和蒸馏档案管理。

### `/dashboard/novels?category=ai`

用途：按小说库卡片样式展示 AI 创作项目。

### `/dashboard/novels/ai/<project_id>`

用途：显示 AI 作品封面、目录和章节正文。
```

任务日志说明必须包含同步任务和 AI 创作任务两个分类。

- [ ] **Step 4: 更新 AI_WRITING_STUDIO_PLAN.md**

在旧任务历史章节上方加入：

```markdown
> 历史说明：早期版本在 AI 创作页内提供“任务历史”Tab。当前实现已删除该 Tab，AI 创作任务统一在 `/dashboard/logs` 的“AI 创作任务”分类中查询；完整详情仍由 `/api/dashboard/ai/jobs/<job_id>` 提供。
```

把现状清单中的“任务历史查看”改成：

```markdown
- AI 创作任务已迁移到全局任务日志，支持类型、状态、时间筛选和完整详情。
```

- [ ] **Step 5: 运行文档测试**

Run: `python -m pytest tests/test_frontend_library_os.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add docs/frontend-pages.md docs/AI_WRITING_STUDIO_PLAN.md tests/test_frontend_library_os.py
git commit -m "docs: update AI library and wizard page guidance"
```

### Task 3: 完整验证

**Files:**
- Verify only

**Interfaces:**
- Consumes: 前三个实施计划的全部功能。
- Produces: 最终测试和差异证据。

- [ ] **Step 1: 运行四组定向测试**

Run: `python -m pytest tests/test_preferences.py tests/test_recommendations.py tests/test_keyword_clean.py tests/test_preference_jobs.py tests/test_unified_task_logs.py tests/test_task_logs_routes.py -q`

Expected: PASS。

Run: `python -m pytest tests/test_ai_project_covers.py tests/test_style_control.py tests/test_ai_prompts.py tests/test_ai_service_stream_continue.py -q`

Expected: PASS。

Run: `python -m pytest tests/test_ai_page_routes.py tests/test_frontend_library_os.py tests/test_ai_import_atomicity.py tests/test_ai_security_hardening.py -q`

Expected: PASS。

- [ ] **Step 2: 运行完整测试**

Run: `python -m pytest -q`

Expected: 全部测试通过；基线的 4 个环境相关跳过允许保留，不得新增无解释跳过。

- [ ] **Step 3: 检查差异格式**

Run: `git diff --check origin/main...HEAD`

Expected: 无输出。

- [ ] **Step 4: 检查提交历史**

Run: `git log --oneline --decorate origin/main..HEAD`

Expected: 按偏好日志、封面风格、页面拆分、文档的顺序显示独立提交。

### Task 4: 清理缓存并交付

**Files:**
- Delete ignored cache files only

**Interfaces:**
- Produces: 不包含可再生缓存的本地工作区。
- Preserves: 所有运行数据、个人配置和进入工作区前存在的未跟踪文件。

- [ ] **Step 1: 列出将要清理的路径**

Run: `git clean -ndX`

Expected: 输出包含缓存和本地运行数据；只从中选择 `.pytest_cache/`、`__pycache__/`、`src/pixiv_novel_sync.egg-info/`，不得直接执行无过滤的 `git clean -fdX`。

- [ ] **Step 2: 使用 PowerShell 安全删除缓存**

```powershell
$root = (Resolve-Path '.').Path
$targets = @(
  (Join-Path $root '.pytest_cache'),
  (Join-Path $root 'src\pixiv_novel_sync.egg-info')
)
$targets += Get-ChildItem -Path $root -Directory -Filter '__pycache__' -Recurse | Select-Object -ExpandProperty FullName
$safeTargets = $targets | Sort-Object -Unique | Where-Object {
  $_ -eq (Join-Path $root '.pytest_cache') -or
  $_ -eq (Join-Path $root 'src\pixiv_novel_sync.egg-info') -or
  (Split-Path $_ -Leaf) -eq '__pycache__'
}
$safeTargets | ForEach-Object {
  $resolvedParent = (Resolve-Path (Split-Path $_ -Parent)).Path
  if ($resolvedParent.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $_)) {
    Remove-Item -LiteralPath $_ -Recurse -Force
  }
}
```

Expected: 只删除列出的缓存目录。

- [ ] **Step 3: 最终 Git 状态检查**

Run: `git status --short --branch`

Expected: 没有未提交业务代码；进入工作区前存在的用户未跟踪文件仍存在。

- [ ] **Step 4: 推送前核对远端和当前提交**

Run: `git rev-parse HEAD`

Expected: 输出最终本地提交哈希。

Run: `git rev-parse origin/main`

Expected: 输出实施前远端基线哈希；只有在前述验证全部通过后才执行推送。

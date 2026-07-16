# AI 页面拆分与首页布局实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将自动写作与创作向导拆成独立页面状态边界，并完成首页模块对齐、推荐错误态和关键前端回归。

**Architecture:** 保留 Flask/Jinja 服务端路由和 Vue 3 CDN，通过两个独立模板分别挂载自动写作与创作向导应用。共享的小型展示组件使用顶层 Jinja include，页面不再通过 `pageMode` 隐藏整套无关 DOM 和状态。

**Tech Stack:** Flask、Jinja、Vue 3、Tailwind CDN、pytest

## Global Constraints

- `/dashboard/ai?project_id=<id>` 和 `/dashboard/wizard?tab=distill` 深链接必须兼容。
- 不新增前端框架、打包器或 SPA 路由。
- 自动写作页不得请求创作向导会话。
- 创作向导页不得初始化章节工作区和 Pipeline 状态。
- 首页不使用固定卡片高度；桌面等高、移动端自然堆叠。
- 严格执行 TDD 和独立提交。

---

## 文件结构

- 修改 `src/pixiv_novel_sync/ai_web.py`：两个路由渲染不同模板。
- 修改 `src/pixiv_novel_sync/templates/dashboard_ai.html`：只保留自动写作。
- 创建 `src/pixiv_novel_sync/templates/dashboard_wizard.html`：创作向导和蒸馏档案。
- 创建 `src/pixiv_novel_sync/templates/dashboard_ai_output_panel.html`：两页确实共用的输出组件定义。
- 修改 `src/pixiv_novel_sync/templates/dashboard.html`：卡片等高和推荐错误态。
- 修改 `tests/test_frontend_library_os.py`：模板边界和布局结构测试。
- 创建 `tests/test_ai_page_routes.py`：路由模板与深链接测试。

### Task 1: 锁定两个页面的模板边界

**Files:**
- Create: `tests/test_ai_page_routes.py`
- Modify: `tests/test_frontend_library_os.py`

**Interfaces:**
- Produces: 自动写作模板标记 `data-page="ai-writing"`
- Produces: 创作向导模板标记 `data-page="writing-wizard"`
- Consumes: `/dashboard/ai`、`/dashboard/wizard`

- [ ] **Step 1: 写路由边界失败测试**

```python
from pixiv_novel_sync.webapp import create_app


def test_ai_and_wizard_routes_render_distinct_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "ai-page-route-test-secret")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "storage:\n"
        f"  public_dir: {(tmp_path / 'public').as_posix()}\n"
        f"  private_dir: {(tmp_path / 'private').as_posix()}\n"
        f"  db_path: {(tmp_path / 'routes.db').as_posix()}\n"
        "sync:\n"
        "  auto_sync_enabled: false\n",
        encoding="utf-8",
    )
    client = create_app(config_path=str(config_path), env_path=str(env_path)).test_client()
    ai = client.get("/dashboard/ai", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    wizard = client.get("/dashboard/wizard", environ_base={"REMOTE_ADDR": "127.0.0.1"})

    ai_html = ai.get_data(as_text=True)
    wizard_html = wizard.get_data(as_text=True)
    assert 'data-page="ai-writing"' in ai_html
    assert 'data-page="writing-wizard"' not in ai_html
    assert 'data-page="writing-wizard"' in wizard_html
    assert 'data-page="ai-writing"' not in wizard_html
```

- [ ] **Step 2: 写静态职责失败测试**

```python
def test_ai_and_wizard_templates_do_not_embed_other_workspace():
    ai = read(TEMPLATES / "dashboard_ai.html")
    wizard = read(TEMPLATES / "dashboard_wizard.html")
    assert "loadChatSessions" not in ai
    assert "openNewWizardSession" not in ai
    assert "loadChapterDashboard" not in wizard
    assert "startChapterPipeline" not in wizard
    assert "pageMode" not in ai
    assert "pageMode" not in wizard
```

该测试替换 `tests/test_frontend_library_os.py` 中原来断言 `pageMode === 'wizard'` 的 `test_dashboard_ai_wizard_has_section_navigation()`。

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_ai_page_routes.py tests/test_frontend_library_os.py -q`

Expected: FAIL，两个路由仍渲染同一模板，且创作向导模板不存在。

- [ ] **Step 4: 提交测试基线**

不提交红灯测试，继续 Task 2 完成最小拆分后一起提交。

### Task 2: 拆分自动写作与创作向导模板

**Files:**
- Modify: `src/pixiv_novel_sync/ai_web.py:142-162`
- Modify: `src/pixiv_novel_sync/templates/dashboard_ai.html`
- Create: `src/pixiv_novel_sync/templates/dashboard_wizard.html`
- Create: `src/pixiv_novel_sync/templates/dashboard_ai_output_panel.html`
- Test: `tests/test_ai_page_routes.py`
- Test: `tests/test_frontend_library_os.py`

**Interfaces:**
- Produces: `dashboard_ai.html` 自动写作 Vue 应用。
- Produces: `dashboard_wizard.html` 创作向导/蒸馏 Vue 应用。
- Produces: `dashboard_ai_output_panel.html` 中的 `output-panel` 组件注册函数。

- [ ] **Step 1: 让两个路由渲染不同模板**

```python
@app.get("/dashboard/ai")
def dashboard_ai_page():
    return render_template("dashboard_ai.html")

@app.get("/dashboard/wizard")
def dashboard_wizard_page():
    return render_template("dashboard_wizard.html")
```

- [ ] **Step 2: 抽取共用输出组件**

创建顶层 include，内容只包含当前 `output-panel` 的 Vue 组件定义：

```html
<script>
function registerAiOutputPanel(app) {
  app.component('output-panel', {
    props: ['title', 'text', 'jobId', 'streaming', 'showDetect'],
    emits: ['save', 'detect'],
    template: `
      <div v-if="text || streaming" class="bg-white rounded-xl border border-gray-100 shadow-sm p-5 flex flex-col">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-lg font-bold text-gray-900">{{ title }}</h2>
          <div class="flex items-center gap-2">
            <span v-if="text" class="text-xs text-pixiv-gray">{{ text.length.toLocaleString() }} 字</span>
            <span v-if="jobId" class="text-xs text-pixiv-gray">Job {{ jobId.substring(0, 8) }}</span>
          </div>
        </div>
        <pre ref="contentRef" class="whitespace-pre-wrap text-sm text-gray-800 font-serif overflow-y-auto bg-gray-50 rounded-lg p-3 border border-gray-100"
             style="max-height: 60vh; min-height: 8rem;">{{ text || '等待输出...' }}</pre>
        <div class="flex gap-2 mt-3 flex-wrap">
          <button @click="$emit('save')" :disabled="!text" class="px-3 py-1.5 bg-brand-500 text-white rounded-lg text-xs disabled:opacity-50">保存草稿</button>
          <button @click="copyText" :disabled="!text" class="px-3 py-1.5 bg-gray-100 rounded-lg text-xs disabled:opacity-50">复制</button>
          <button v-if="showDetect" @click="$emit('detect')" :disabled="!text" class="px-3 py-1.5 bg-amber-50 text-amber-700 border border-amber-200 rounded-lg text-xs disabled:opacity-50">AI 痕迹检测</button>
          <button v-if="text" @click="scrollToBottom" class="px-3 py-1.5 bg-gray-100 rounded-lg text-xs ml-auto" title="滚到底部">↓ 底部</button>
        </div>
      </div>`,
    watch: {
      text() { this.$nextTick(() => this.autoScroll()); }
    },
    methods: {
      autoScroll() {
        const el = this.$refs.contentRef;
        if (!el || !this.streaming || this._userScrolled) return;
        el.scrollTop = el.scrollHeight;
      },
      scrollToBottom() {
        const el = this.$refs.contentRef;
        if (el) {
          el.scrollTop = el.scrollHeight;
          this._userScrolled = false;
        }
      },
      async copyText() {
        if (this.text && window.navigator?.clipboard) {
          await window.navigator.clipboard.writeText(this.text);
        }
      }
    },
    mounted() {
      const el = this.$refs.contentRef;
      if (el) {
        el.addEventListener('scroll', () => {
          this._userScrolled = (el.scrollHeight - el.scrollTop - el.clientHeight) > 100;
        });
      }
    }
  });
}
</script>
```

在两个页面初始化 Vue 应用前调用 `registerAiOutputPanel(app)`。

- [ ] **Step 3: 把自动写作页面收敛为项目状态**

`dashboard_ai.html` 保留：

- 项目列表和项目总览。
- 长篇规划、章节、伏笔、状态、检索。
- 章节工作区和 Pipeline 弹窗。
- 项目/章节/Agent/档案加载函数。

删除：

- `tabs`、`activeTab`、`pageMode`、`switchTab`。
- 创作向导会话、素材导入和 READY 导入状态。
- 蒸馏表单和档案管理区。
- `loadChatSessions()` 的挂载调用。

根节点固定为：

```html
<div class="library-page px-4 sm:px-6 lg:px-8 py-6 max-w-7xl mx-auto" data-page="ai-writing">
```

- [ ] **Step 4: 创建创作向导专用页面**

`dashboard_wizard.html` 包含：

- 标题和“创作向导/蒸馏档案”顶部 Tab。
- 会话列表、对话区、实时产物区。
- 素材导入和 READY 导入弹窗。
- 蒸馏表单、风格档案和小说档案管理。
- Provider/Agent 列表只用于下拉选择，不显示设置管理界面。

根节点固定为：

```html
<div class="library-page px-4 sm:px-6 lg:px-8 py-6 max-w-7xl mx-auto" data-page="writing-wizard">
```

初始化只执行：

```javascript
onMounted(async () => {
  await Promise.all([loadAgents(), loadStyleProfiles(), loadNovelProfiles(), loadProjects(), loadChatSessions()]);
  applyBestAgentDefaults();
  if (activeTab.value === 'distill') {
    await Promise.all([loadStyleProfiles(), loadNovelProfiles()]);
  }
});
```

- [ ] **Step 5: 保持导入后的深链接行为**

创作向导导入成功后执行：

```javascript
window.location.href = '/dashboard/ai?project_id=' + encodeURIComponent(projectId);
```

自动写作页挂载时继续读取 `project_id` 并调用 `openProject(project)`。

- [ ] **Step 6: 运行边界测试**

Run: `python -m pytest tests/test_ai_page_routes.py tests/test_frontend_library_os.py -q`

Expected: PASS。

- [ ] **Step 7: 运行 AI 路由和导入回归**

Run: `python -m pytest tests/test_ai_web_stream.py tests/test_ai_import_atomicity.py tests/test_ai_security_hardening.py -q`

Expected: PASS。

- [ ] **Step 8: 提交**

```powershell
git add src/pixiv_novel_sync/ai_web.py src/pixiv_novel_sync/templates/dashboard_ai.html src/pixiv_novel_sync/templates/dashboard_wizard.html src/pixiv_novel_sync/templates/dashboard_ai_output_panel.html tests/test_ai_page_routes.py tests/test_frontend_library_os.py
git commit -m "refactor: split AI writing and wizard pages"
```

### Task 3: 首页卡片等高和推荐错误态

**Files:**
- Modify: `src/pixiv_novel_sync/templates/dashboard.html:160-355,430-610`
- Modify: `tests/test_frontend_library_os.py`

**Interfaces:**
- Consumes: `GET /api/dashboard/recommendations/items?limit=6`
- Produces: `recommendationError: string`
- Produces: 桌面同一行卡片使用 `h-full flex flex-col`，移动端无固定高度。

- [ ] **Step 1: 写首页结构失败测试**

```python
def test_dashboard_cards_stretch_and_recommendations_have_error_state():
    html = read(TEMPLATES / "dashboard.html")
    assert 'data-dashboard-card="activity"' in html
    assert 'data-dashboard-card="scheduler"' in html
    assert html.count("h-full flex flex-col") >= 2
    assert "recommendationError" in html
    assert "推荐结果加载失败" in html
    assert "retryRecommendationItems" in html
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_frontend_library_os.py::test_dashboard_cards_stretch_and_recommendations_have_error_state -q`

Expected: FAIL，缺少稳定标记和可见错误态。

- [ ] **Step 3: 统一两张卡片的拉伸结构**

活动卡片：

```html
<div class="lg:col-span-2" data-dashboard-card="activity">
  <div class="bg-white rounded-xl border border-gray-100 shadow-sm h-full flex flex-col">
    <div class="px-5 pt-5 pb-3 border-b border-gray-50">...</div>
    <div class="px-5 py-4 flex-1 flex flex-col">...</div>
  </div>
</div>
```

定时任务卡片使用同样的 `h-full flex flex-col`，内容主体增加 `flex-1`。不得添加像素固定高度。

- [ ] **Step 4: 添加推荐错误状态**

```javascript
const recommendationError = ref('');

const loadRecommendationItems = async () => {
  loadingRecommendations.value = recommendationItems.value.length === 0;
  recommendationError.value = '';
  try {
    const res = await fetch('/api/dashboard/recommendations/items?limit=6');
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.error || '推荐结果加载失败');
    recommendationItems.value = data.data || [];
  } catch (e) {
    recommendationError.value = e.message || '推荐结果加载失败';
  } finally {
    loadingRecommendations.value = false;
  }
};

const retryRecommendationItems = () => loadRecommendationItems();
```

模板顺序为加载态、错误态、结果、空态：

```html
<div v-else-if="recommendationError" class="text-center py-8 border border-red-100 bg-red-50 rounded-xl">
  <p class="text-sm text-red-600">{{ recommendationError }}</p>
  <button @click="retryRecommendationItems" class="mt-3 px-3 py-1.5 bg-white border border-red-200 text-red-600 rounded-lg text-xs">重试</button>
</div>
```

- [ ] **Step 5: 运行首页模板测试**

Run: `python -m pytest tests/test_frontend_library_os.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add src/pixiv_novel_sync/templates/dashboard.html tests/test_frontend_library_os.py
git commit -m "fix: align dashboard cards and surface recommendation errors"
```

### Task 4: 页面拆分阶段回归

**Files:**
- Verify only

**Interfaces:**
- Consumes: Tasks 1-3。
- Produces: 可独立交付的页面边界和首页布局。

- [ ] **Step 1: 运行前端与 AI 路由定向测试**

Run: `python -m pytest tests/test_ai_page_routes.py tests/test_frontend_library_os.py tests/test_ai_web_stream.py tests/test_ai_import_atomicity.py tests/test_html_cache_headers.py -q`

Expected: PASS，无新增跳过。

- [ ] **Step 2: 检查模板中已移除旧模式**

Run: `rg -n "pageMode|PAGE_MODE|ai_page_mode" src/pixiv_novel_sync/templates/dashboard_ai.html src/pixiv_novel_sync/templates/dashboard_wizard.html src/pixiv_novel_sync/ai_web.py`

Expected: 无输出。

- [ ] **Step 3: 检查差异和状态**

Run: `git diff --check HEAD~2..HEAD`

Expected: 无输出。

Run: `git status --short --branch`

Expected: 没有未提交业务代码。

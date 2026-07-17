# AI 创作项目总览单面板实施计划

> **执行者必读：** 必须使用 `superpowers:executing-plans` 按任务执行；每一步使用复选框跟踪。本项目明确不使用子代理。

**目标：** 将 AI 创作页面“项目总览”的四张等高卡片改为方案 A 的单一大面板，并完整保留封面、项目资料、蒸馏档案和风格控制功能。

**架构：** 只修改 Vue 模板结构和对应的静态前端契约测试。一个 `data-overview-panel` 包含“作品资料与进度、蒸馏内容、风格控制”三个 `data-overview-section`；现有响应式 Tailwind CSS 类负责桌面分栏和移动端单列，不新增 JavaScript 状态、API 或组件层。

**技术栈：** Flask、Jinja、Vue 3、Tailwind CSS、pytest、浏览器自动化

## 全局约束

- 只修改 `src/pixiv_novel_sync/templates/dashboard_ai.html` 和 `tests/test_frontend_library_os.py`。
- 不修改后端路由、数据库、API、保存函数、Prompt 注入或其他 `Tab`。
- 项目资料、蒸馏档案、风格控制继续使用三组独立保存操作。
- 项目总览只能有一个主要外层面板，内部不得创建嵌套卡片。
- 不使用固定内容高度，不使用横向滚动处理移动端布局。
- 桌面端顺序为“作品资料与进度 → 蒸馏内容 → 风格控制”，移动端保持相同信息顺序。
- 保留现有中文文案风格和 Library OS 视觉语言。
- 严格执行测试驱动开发：先看到新测试按预期失败，再修改模板。

---

## 文件结构

- 修改 `tests/test_frontend_library_os.py`：定义项目总览单面板结构、分区边界和独立操作的静态契约。
- 修改 `src/pixiv_novel_sync/templates/dashboard_ai.html`：实现一个外层面板和三个内部内容分区。

不创建新的生产代码文件。该改动仅重新组织一个已有页面片段，抽取新组件会扩大范围并增加无必要的加载边界。

### 任务 1：用单一分区面板替换四张等高卡片

**文件：**

- 修改：`tests/test_frontend_library_os.py:198`
- 修改：`src/pixiv_novel_sync/templates/dashboard_ai.html:137`

**接口：**

- 使用：现有 `currentProject`、`projectMetaForm`、`chapters`、`foreshadows`、`longformPlan`、`STYLE_SLIDERS` 和封面状态。
- 使用：现有 `saveProjectMeta()`、`saveProjectProfiles()`、`saveProjectStyleControl()`、`uploadProjectCover()`、`deleteProjectCover()`。
- 产出：一个 `data-overview-panel`，以及值为 `project`、`profiles`、`style` 的三个 `data-overview-section`。
- 不产出：新的 Vue 状态、函数、事件或后端接口。

- [ ] **步骤 1：把旧的等高网格测试改为单面板契约测试**

将 `test_ai_project_overview_has_independent_style_save_and_balanced_grid` 替换为以下测试：

```python
def test_ai_project_overview_uses_single_panel_and_preserves_independent_actions():
    html = read(TEMPLATES / "dashboard_ai.html")
    overview = html.split('v-show="projectDetailTab === \'overview\'"', 1)[1].split(
        "<!-- 长篇规划 -->",
        1,
    )[0]
    profile_save = html.split("async function saveProjectProfiles()", 1)[1].split(
        "function addStyleTag",
        1,
    )[0]

    assert overview.count("data-overview-panel") == 1
    assert overview.count("data-overview-section") == 3
    assert 'data-overview-section="project"' in overview
    assert 'data-overview-section="profiles"' in overview
    assert 'data-overview-section="style"' in overview
    assert "data-overview-card" not in overview
    assert "items-stretch" not in overview
    assert "h-full" not in overview

    project_section = overview.split('data-overview-section="project"', 1)[1].split(
        'data-overview-section="profiles"',
        1,
    )[0]
    profiles_section = overview.split('data-overview-section="profiles"', 1)[1].split(
        'data-overview-section="style"',
        1,
    )[0]
    style_section = overview.split('data-overview-section="style"', 1)[1]

    assert '@click="$refs.coverInput.click()"' in project_section
    assert '@click="deleteProjectCover"' in project_section
    assert '@click="saveProjectMeta"' in project_section
    assert '@click="saveProjectProfiles"' in profiles_section
    assert '/dashboard/wizard?tab=distill' in profiles_section
    assert '@click="saveProjectStyleControl"' in style_section
    assert "async function saveProjectStyleControl()" in html
    assert "settings:" not in profile_save
    assert html.count("await saveProjectStyleControl()") >= 2
```

- [ ] **步骤 2：运行新测试并确认按预期失败**

运行：

```powershell
python -m pytest tests/test_frontend_library_os.py::test_ai_project_overview_uses_single_panel_and_preserves_independent_actions -q
```

预期：测试失败，首个失败断言为 `overview.count("data-overview-panel") == 1`，实际值为 `0`。失败原因必须是新结构尚未实现，而不是导入、编码或测试环境错误。

- [ ] **步骤 3：把项目总览替换为一个外层面板和三个分区**

在 `dashboard_ai.html` 中，用以下结构替换从“概览”注释后的 `v-show="projectDetailTab === 'overview'"` 容器到“长篇规划”注释之前的内容：

```html
      <!-- 概览 -->
      <div v-show="projectDetailTab === 'overview'" data-overview-panel class="bg-white rounded-xl border border-gray-100 shadow-sm overflow-hidden">
        <section data-overview-section="project" class="p-5 space-y-4">
          <div>
            <h3 class="font-bold text-gray-900 text-sm">作品资料与进度</h3>
            <p class="text-xs text-pixiv-gray mt-1">集中维护作品资料，并查看当前写作进度与全书目标。</p>
          </div>
          <div class="grid grid-cols-1 xl:grid-cols-[7rem_minmax(0,1.35fr)_minmax(16rem,1fr)] gap-5 items-start">
            <div class="space-y-2">
              <div class="relative w-28 aspect-[3/4] flex-shrink-0 overflow-hidden rounded-lg shadow-sm" :style="projectCoverGradient(currentProject?.name)">
                <img v-if="currentProject?.cover_url" :src="currentProject.cover_url" class="absolute inset-0 w-full h-full object-cover" @error="currentProject.cover_url = null">
                <div v-else class="absolute inset-0 flex items-center justify-center">
                  <span class="text-4xl font-black text-white/90 drop-shadow select-none">{{ projectFirstChar(currentProject?.name) }}</span>
                </div>
              </div>
              <input ref="coverInput" type="file" accept="image/jpeg,image/png,image/webp" class="hidden" @change="uploadProjectCover">
              <div class="flex flex-wrap gap-2">
                <button @click="$refs.coverInput.click()" :disabled="coverBusy" class="px-3 py-1.5 bg-brand-500 text-white rounded-lg text-xs font-medium hover:bg-brand-600 disabled:opacity-50">
                  {{ coverBusy ? '处理中…' : (currentProject?.cover_url ? '替换封面' : '上传封面') }}
                </button>
                <button v-if="currentProject?.cover_url" @click="deleteProjectCover" :disabled="coverBusy" class="px-3 py-1.5 bg-red-50 text-red-600 rounded-lg text-xs font-medium hover:bg-red-100 disabled:opacity-50">删除封面</button>
              </div>
            </div>

            <div class="space-y-3 min-w-0">
              <div>
                <label class="block text-xs font-medium text-pixiv-gray mb-1">项目名称</label>
                <input v-model="projectMetaForm.name" class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm" placeholder="项目名称">
              </div>
              <div>
                <label class="block text-xs font-medium text-pixiv-gray mb-1">项目描述</label>
                <textarea v-model="projectMetaForm.description" rows="3" class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm" placeholder="项目描述（可选）"></textarea>
              </div>
              <div class="flex justify-end">
                <button @click="saveProjectMeta" class="px-4 py-2 bg-brand-500 text-white rounded-lg text-sm font-medium hover:bg-brand-600">保存信息</button>
              </div>
            </div>

            <div class="space-y-3 min-w-0">
              <h4 class="text-xs font-bold text-gray-700">项目概况</h4>
              <div class="grid grid-cols-2 gap-2 text-xs">
                <div class="rounded bg-gray-50 border border-gray-100 p-2.5 text-center">
                  <div class="font-mono text-lg font-bold text-gray-900">{{ chapters.length }}</div>
                  <div class="text-pixiv-gray mt-0.5">章节</div>
                </div>
                <div class="rounded bg-gray-50 border border-gray-100 p-2.5 text-center">
                  <div class="font-mono text-lg font-bold text-gray-900">{{ Math.round(chapters.reduce((s,c)=>s+(c.word_count||0),0)/1000) }}k</div>
                  <div class="text-pixiv-gray mt-0.5">字数</div>
                </div>
                <div class="rounded bg-gray-50 border border-gray-100 p-2.5 text-center">
                  <div class="font-mono text-lg font-bold text-gray-900">{{ foreshadows.length }}</div>
                  <div class="text-pixiv-gray mt-0.5">伏笔</div>
                </div>
                <div class="rounded bg-gray-50 border border-gray-100 p-2.5 text-center">
                  <div class="font-mono text-lg font-bold text-gray-900">{{ foreshadows.filter(f=>f.status==='pending').length }}</div>
                  <div class="text-pixiv-gray mt-0.5">待回收</div>
                </div>
              </div>
              <div v-if="longformPlan.target_words || longformPlan.expected_chapter_count" class="pt-2 border-t border-gray-100">
                <div class="text-xs font-medium text-pixiv-gray mb-2">规划目标</div>
                <div class="flex flex-wrap gap-1.5">
                  <span v-if="longformPlan.target_words" class="text-xs px-2 py-1 rounded-full bg-brand-50 border border-brand-100 text-brand-700">目标 {{ Number(longformPlan.target_words).toLocaleString() }} 字</span>
                  <span v-if="longformPlan.expected_chapter_count" class="text-xs px-2 py-1 rounded-full bg-brand-50 border border-brand-100 text-brand-700">预计 {{ longformPlan.expected_chapter_count }} 章</span>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section data-overview-section="profiles" class="p-5 border-t border-gray-100 space-y-3">
          <div>
            <h3 class="font-bold text-gray-900 text-sm">套用蒸馏内容</h3>
            <p class="text-xs text-pixiv-gray mt-1">为本项目绑定文风、角色与世界观档案，自动写作时会持续注入。</p>
          </div>
          <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] gap-3 items-end">
            <div class="min-w-0">
              <label class="block text-xs font-medium text-pixiv-gray mb-1">风格档案（文风 / 笔触）</label>
              <select v-model.number="projectMetaForm.style_profile_id" class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm bg-white">
                <option :value="0">不套用</option>
                <option v-for="p in styleProfiles" :key="p.id" :value="p.id">{{ p.name }}</option>
              </select>
            </div>
            <div class="min-w-0">
              <label class="block text-xs font-medium text-pixiv-gray mb-1">小说档案（角色 / 世界观 / 设定）</label>
              <select v-model.number="projectMetaForm.novel_profile_id" class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm bg-white">
                <option :value="0">不套用</option>
                <option v-for="p in novelProfiles" :key="p.id" :value="p.id">{{ p.name }}</option>
              </select>
            </div>
            <div class="flex flex-wrap items-center gap-2 xl:justify-end">
              <a href="/dashboard/wizard?tab=distill" class="px-3 py-2 bg-white border border-gray-200 text-gray-700 rounded-lg text-sm font-medium hover:bg-gray-50">管理档案</a>
              <button @click="saveProjectProfiles" class="px-4 py-2 bg-brand-500 text-white rounded-lg text-sm font-medium hover:bg-brand-600">保存套用</button>
            </div>
          </div>
        </section>

        <section data-overview-section="style" class="p-5 border-t border-gray-100 space-y-4">
          <div>
            <h3 class="font-bold text-gray-900 text-sm">风格控制</h3>
            <p class="text-xs text-pixiv-gray mt-1">滑块居中（50）时不干预；向两端调整后，续写会持续采用对应倾向。</p>
          </div>
          <div class="grid grid-cols-1 xl:grid-cols-[minmax(0,3fr)_minmax(18rem,2fr)] gap-6 items-start">
            <div class="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-4 min-w-0">
              <div v-for="(s, index) in STYLE_SLIDERS" :key="s.key" class="space-y-1" :class="STYLE_SLIDERS.length % 2 && index === STYLE_SLIDERS.length - 1 ? 'md:col-span-2' : ''">
                <div class="flex items-center justify-between text-xs gap-3">
                  <span class="font-medium text-gray-700">{{ s.label }}</span>
                  <span class="font-mono text-pixiv-gray">{{ projectMetaForm.style_control.sliders[s.key] }}</span>
                </div>
                <input type="range" min="0" max="100" step="5" v-model.number="projectMetaForm.style_control.sliders[s.key]" class="w-full accent-brand-500">
                <div class="flex justify-between gap-3 text-[10px] text-gray-400">
                  <span>{{ s.left }}</span><span class="text-right">{{ s.right }}</span>
                </div>
              </div>
            </div>

            <div class="space-y-4 min-w-0">
              <div>
                <label class="block text-xs font-medium text-pixiv-gray mb-1">风格标签</label>
                <div class="flex flex-wrap gap-1.5 mb-2">
                  <span v-for="t in projectMetaForm.style_control.tags" :key="t" class="inline-flex items-center gap-1 text-xs px-2 py-1 rounded-full bg-brand-50 text-brand-700">
                    {{ t }}<button @click="removeStyleTag(t)" class="hover:text-brand-900">×</button>
                  </span>
                  <span v-if="!projectMetaForm.style_control.tags.length" class="text-xs text-gray-400">未添加标签</span>
                </div>
                <div class="flex flex-col sm:flex-row gap-2">
                  <input v-model="styleTagInput" @keydown.enter.prevent="addStyleTag" class="flex-1 min-w-0 px-3 py-1.5 border border-gray-200 rounded-lg text-sm" placeholder="如：NTR、病娇、治愈、第一人称">
                  <button @click="addStyleTag" class="px-3 py-1.5 bg-brand-500 text-white rounded-lg text-sm hover:bg-brand-600">添加</button>
                </div>
              </div>
              <div>
                <label class="block text-xs font-medium text-pixiv-gray mb-1">额外要求（可选）</label>
                <textarea v-model="projectMetaForm.style_control.custom" rows="3" class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm" placeholder="补充风格指令，如：用第一人称、多用对话推进、避免说教"></textarea>
              </div>
              <div class="flex justify-end">
                <button @click="saveProjectStyleControl" class="px-4 py-2 bg-brand-500 text-white rounded-lg text-sm font-medium hover:bg-brand-600">保存风格设定</button>
              </div>
            </div>
          </div>
        </section>
      </div>
```

实现时保留“长篇规划”注释及其后全部内容，不修改上述替换区间之外的模板。

- [ ] **步骤 4：运行定向测试并确认通过**

运行：

```powershell
python -m pytest tests/test_frontend_library_os.py::test_ai_project_overview_uses_single_panel_and_preserves_independent_actions -q
```

预期：`1 passed`，退出码为 `0`。

- [ ] **步骤 5：运行前端契约测试文件**

运行：

```powershell
python -m pytest tests/test_frontend_library_os.py -q
```

预期：该文件全部测试通过，退出码为 `0`。

- [ ] **步骤 6：检查差异并提交实现**

运行：

```powershell
git diff --check
git diff -- tests/test_frontend_library_os.py src/pixiv_novel_sync/templates/dashboard_ai.html
git add tests/test_frontend_library_os.py src/pixiv_novel_sync/templates/dashboard_ai.html
git commit -m "refactor: 合并 AI 项目总览面板"
```

预期：`git diff --check` 无输出；提交只包含上述两个文件。

### 任务 2：完成完整回归与响应式视觉验收

**文件：**

- 验证：`src/pixiv_novel_sync/templates/dashboard_ai.html`
- 验证：`tests/test_frontend_library_os.py`
- 不创建或提交截图文件。

**接口：**

- 使用：任务 1 产出的 `data-overview-panel` 和三个 `data-overview-section`。
- 产出：桌面端、窄桌面端、移动端的布局验收结果，以及完整测试结果。

- [ ] **步骤 1：运行完整自动化测试**

运行：

```powershell
python -m pytest -q
```

预期：全部测试通过，退出码为 `0`；既有环境相关跳过项数量不增加。

- [ ] **步骤 2：启动本地 Web 服务并确认页面可访问**

先检查端口 `5010`；若已占用则使用 `5011`。在隐藏后台进程中运行：

```powershell
pixiv-novel-sync web-token-ui --port 5011
```

打开 `/dashboard/ai`，进入任意现有项目的“项目总览”。只读取现有项目，不创建、保存或删除数据。

预期：页面加载成功，浏览器控制台没有 Vue 模板编译错误；项目总览中存在一个 `[data-overview-panel]` 和三个 `[data-overview-section]`。

- [ ] **步骤 3：检查桌面端 1440 × 1000**

使用浏览器自动化设置视口为 `1440 × 1000` 并截图到系统临时目录。检查：

- 首区为“封面、项目资料、统计”三部分。
- 蒸馏内容为紧凑横向设置栏。
- 风格控制占据完整面板宽度，滑块与补充设置清晰分区。
- 页面没有四张独立外层卡片，没有由等高拉伸产生的大块空白。
- 封面、文字、输入框和按钮没有重叠。

预期：全部检查通过，页面水平方向无溢出。

- [ ] **步骤 4：检查窄桌面端 1024 × 900**

使用浏览器自动化设置视口为 `1024 × 900` 并截图到系统临时目录。检查：

- 首区可以按 Tailwind 断点切换为单列，但内容顺序保持正确。
- 蒸馏档案操作区允许换行，不挤压选择框。
- 风格滑块标签、数值和两端说明不互相遮挡。

预期：全部检查通过，页面水平方向无溢出。

- [ ] **步骤 5：检查移动端 390 × 844**

使用浏览器自动化设置视口为 `390 × 844` 并截图到系统临时目录。检查：

- 顺序为“封面 → 项目资料 → 统计 → 蒸馏内容 → 风格滑块 → 标签与额外要求”。
- 封面保持 `3:4` 比例且不铺满容器。
- 输入框、选择框、文本域和操作按钮不超出面板。
- 页面不存在横向滚动条，最长中文文案没有被裁切。

预期：全部检查通过，`document.documentElement.scrollWidth === document.documentElement.clientWidth`。

- [ ] **步骤 6：执行最终仓库检查**

运行：

```powershell
git diff --check
git status --short --branch
git log -3 --oneline
```

预期：`git diff --check` 无输出；工作区干净；`main` 只比 `origin/main` 多出本次规格、计划和实现提交。

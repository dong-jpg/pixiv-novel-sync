# `/dashboard/ai` 拆分为一级页面 设计文档

- 日期：2026-09-02
- 状态：待评审
- 依据：`templates/dashboard_ai.html` 实测 1946 行 / 130936 字节（markup 1–842，内联脚本 846–1945 共 1100 行 / 60347 字节，全部在**一个** `setup()` 里）；三层嵌套 tab；68 个反应式声明；42 个 distinct API 端点
- 前置：`docs/superpowers/specs/2026-08-28-sync-budget-and-settings-redesign-design.md` §6.3 把本项列为「体量足以单开一份设计」，本文档即那份设计

## 1. 目标与非目标

用户诉求原文：「AI 相关的页面和设置逻辑也不好用」。上一轮（commit `fa7ee2e`）只解决了**设置**那一半——`/dashboard/settings/{models,agents,adult}` 三页 + 候选模型链预览。本轮解决**工作页**那一半。

治的是与设置页同型的三个病：

1. 七个子 tab 全部用 `v-show`，进入任何项目就一次性挂载 569 行 markup。
2. `onMounted` 无条件打 5 个请求（agents + 三个 profiles + projects），不管你要看哪个 tab；开项目再连打 3 个。
3. 一个 `setup()` 持 52 个 `ref` + 10 个 `reactive` + 6 个 `computed`，返回一个对象供 842 行 markup 共享。

**非目标**（明确不做）：

- 不改任何 AI 后端行为。42 个端点、`ModelRouter` 调度、`ai_jobs` 生命周期一行不动。
- 不把 pipeline 拆成独立页面。理由见 §5，这是本文档最重要的一条否决。
- 不动 `/dashboard/wizard`、`/dashboard/novels/ai/<id>` 两个页面的内部结构（只改 wizard 的一行跳转 URL，见 §4.4）。
- 不做视觉改版。沿用 `library-*` 约定与 `vue_components.html` 既有组件。

## 2. 现状测绘

### 2.1 三层 tab

| 层 | 变量（行） | 取值 | 说明 |
|---|---|---|---|
| 一 | `projectView`（872） | `list` / `detail` | `list` 只有 24 行 markup；`detail` 是 704 行 |
| 二 | `projectDetailTab`（873） | `overview` / `longform` / `chapters` / `chapter` / `foreshadow` / `states` / `search` | 七块全 `v-show`；`chapter` 带 `onlyIfSelected` |
| 三 | `dashboardTab`（1611） | `meta` / `state` / `foreshadow` / `detect` / `audit` / `pipeline` | **不是真的第三层** |

第二层各块体量：

| tab | 中文名 | 行范围 | 行数 | 字节 |
|---|---|---|---|---|
| `chapter` | 本章节 | 522–718 | **197** | 16407 |
| `overview` | 项目总览 | 138–293 | **156** | 11551 |
| `longform` | 全书规划 | 296–389 | 94 | 9198 |
| `chapters` | 自动写作 | 392–439 | 48 | 4583 |
| `foreshadow` | 伏笔 | 442–485 | 44 | 3771 |
| `search` | 语义检索 | 501–519 | 19 | 1285 |
| `states` | 状态记忆 | 488–498 | **11** | 983 |

**第三层不参与拆分。** `dashboardTab` 的 6 个取值全部渲染同一个 `GET /api/dashboard/ai/chapters/<id>/dashboard` 响应对象的不同切面，用 `v-if/v-else-if` 串联，没有独立请求。它是一个响应的 6 个视图，不是页面。唯一副作用：`step_failed` 事件强制切到 `pipeline`（1723）。

### 2.2 耦合强度

按「状态在哪些 markup 区块被引用」逐个扫描的结果，七块分三档：

**强耦合三角（不可拆）**：`chapters`（列表+批量）↔ `chapter`（工作区）↔ pipeline 弹窗。
共享 `currentChapter`、`chapters`、`streamOutput`、`pipelineSteps`、`pipelineRunning`、`pipelineSelectedSteps`、`pipelineAgentIds`；`openChapter`(1436) / `backToChapterList`(1412) / `adjacentChapter`(1402) 在列表与工作区之间来回跳；`startChapterPipeline` 与 `startBatchChapterPipeline` 共享同一套步骤配置与 agent 映射；`retryPipelineStep` 从三个不同 UI 位置被调。

**中耦合**：`overview` ↔ `longform`（共享 `currentProject`、`longformPlan`、`refreshCurrentProject`、RawImport 弹窗）；`longform` → `chapters`（`createPlannedChapters` 造完章节要 `loadChapters`，`plannedChapterExists` 要读 `chapters`）。

**弱耦合（近乎独立）**：`foreshadow`（44 行 / 5 个端点，只经 computed 读 `chapters.length`）、`states`（11 行 / 1 个端点）、`search`（19 行 / 1 个端点）。

### 2.3 端点分布：34/42 是单 tab 专属

跨 tab 共用的只有 8 个：5 个启动期请求（`GET /agents`、`/style-profiles`、`/novel-profiles`、`/preferences/profiles`、`/projects`）+ `GET /projects`（保存后刷新列表）+ `GET /projects/<id>`（`refreshCurrentProject`，longform 与 RawImport 共用）+ `GET /projects/<id>/foreshadows`（foreshadow tab 增删改、chapter 的 `autoUpdateState`、pipeline 完成后各自重载）。

**其余 34 个端点单 tab 专属。这是拆分最有利的信号。**

8 个 SSE 端点分布在 4 个 tab：longform ×2、chapter ×3、chapters ×1（批量）、pipeline 弹窗 ×3（含单步 2 个）、foreshadow ×1。SSE 的分布决定了拆分边界。

### 2.4 公共层已完全统一，但正好是测试陷阱

上一轮的整改在这个模板上是彻底的：零 `getReader()`、零 `await fetch(`、零自建 `csrfFetch`。

| 公共层 | 真实调用 | 位置（另有注释提及，见下） |
|---|---|---|
| `window.aiApi` | 7 | 1021（`api()` 是全页 22 处 REST 调用的唯一出口）、1022、1078–1080 |
| `window.streamSSE` | **5** | 1028（`routeStream`，服务 5 条流）、1560、1691、1765、1810 |
| `window.csrfFetch` | **1** | 1065（`detectAITells`） |
| `window.errorText` | **1** | 1067（同上） |

`window.csrfFetch` / `window.errorText` 各只有一处真实调用，都在 `detectAITells`（1062–1075）里——而那 14 行本可缩成 3 行（`api()` 返回的就是 `data.data`）。**但它是测试 #2/#3/#11 正向断言的唯一真实来源，删掉会连挂 3 个测试。**

**比这更要紧的一点**：这四个名字在文件里各多出 1–2 次出现，全部来自 1019–1024 那段「请求、错误文案与 SSE 解析全部走 base.html 的公共层……本页不再自建副本」的注释。而测试 #2/#3/#4 是**朴素子串断言**——所以新页面只要把这段注释一起抄过去，就能在完全没调用公共层的情况下通过断言。**这三条断言的真实风险不是「会失败」，而是「会假通过」。** §6.1 的处置必须同时修掉这一点，否则拆分后我们得到的是四个绿灯却无人守护的页面。

## 3. 拆分方案：4 个页面

| 路由 | 模板 | 承载 | markup 约 | 导航 |
|---|---|---|---|---|
| `/dashboard/ai` | `dashboard_ai_projects.html` | 项目列表 + 新建/删除 | 40 行 | **侧栏单条扁平项**，渲染真页面 |
| `/dashboard/ai/projects/<int:project_id>` | `dashboard_ai_project.html` | overview 三个 section + longform + RawImport 弹窗 | 290 行 | 页内导航「项目与规划」 |
| `/dashboard/ai/projects/<int:project_id>/chapters` | `dashboard_ai_chapters.html` | chapters 列表 + 批量 pipeline + chapter 工作区 + chapterDashboard + pipeline modal + 上下文预览弹窗 | 460 行 | 页内导航「章节与自动写作」 |
| `/dashboard/ai/projects/<int:project_id>/notes` | `dashboard_ai_notes.html` | foreshadow + states + search | 80 行 | 页内导航「伏笔与记忆」 |

### 3.0 导航不能照抄设置页的侧栏 children

设置页的五个子页是**全局**的，所以能静态写进 `vue_components.html:NAV_ITEMS` 的 `children`。**AI 的三个子页不行**——它们的路径带 `<project_id>`，侧栏不知道当前是哪个项目，而没开项目时这三条根本无处可去。

因此：

- **侧栏**：`/dashboard/ai` 保持现在的单条扁平项，不加 `children`，一行不改。`isActive` 的前缀匹配（`currentPath.startsWith('/dashboard/ai')`）对所有子页天然生效，且现有路由里没有别的路径以 `/dashboard/ai` 开头（`/dashboard/novels/ai/<id>` 不匹配）。
- **项目内导航**：新建 Jinja partial `dashboard_ai_project_nav.html`，接收 `project_id` 渲染三条项目内链接，按 `request.path` 精确高亮。与 `dashboard_settings_nav.html` 同型，区别是**带参数**。三个项目内页面各 `{% include %}` 一次。
- 项目列表页不含这个 partial（没有 project_id 可传）。

两个后果：上一轮那条 `test_sidebar_expands_settings_into_five_subpages`（`test_frontend_library_os.py:493`）不受影响，也**不需要**照抄一份 AI 版；`NAV_ITEMS` 零改动，§7 的步 2 相应缩减为只建 partial。

### 3.1 两个合并决定

两个合并决定：

- **第三页 460 行是最大的，但它就是 §2.2 那个不可拆的强耦合三角。** 460 行 markup + 约 500 行 JS，与已接受的 `dashboard_settings_models.html`（685 行）同量级。
- **第四页把三个 11–44 行的小 tab 合并。** `states` 只有 11 行 markup 和 1 个端点，单独一页毫无意义。三者纵向排布，不再套页内 tab。

pipeline 弹窗的 markup 抽成 Jinja partial `dashboard_ai_pipeline_modal.html`（照 `dashboard_ai_output_panel.html` 的既有做法 `{% include %}`），**JS 留在章节页同一个 `setup()` 里**。体积下来了，运行态不变。

### 3.2 按需加载（本次拆分的主要收益）

现状：进任何页面都打 5 个请求，开项目再打 3 个。拆后：

| 页面 | 请求数 | 内容 |
|---|---|---|
| 项目列表 | **1** | `GET /projects` |
| 项目页 | 6 | agents + style/novel/preference profiles + project + chapters |
| 章节页 | 3 | agents + project + chapters |
| 笔记页 | 4 | project + foreshadows + states + chapters |

`chapters` 被三页以三种用途读取（项目页算统计与 `plannedChapterExists`、章节页要完整列表、笔记页只要 `chapters.length` 判伏笔到期），所以三页都得打 `GET /projects/<id>/chapters`——**这是拆分后唯一真正新增的重复请求**，一次列表查询，可以接受。**不要试图用 localStorage 缓存它**：`createPlannedChapters` / `createChapter` / `deleteChapter` / 批量 pipeline 四处都会让它失效。

## 4. 跨页状态的处置

按严重度排列。这一节是拆分的真正成本所在。

### 4.1 `currentProject`（899）→ 进 URL 路径

现状是从 `projectList` 里 `find` 出来的完整对象（`openProject(p)` @1092 直接 `currentProject.value = p`），带 `settings.longform_plan`、`settings.style_control`、`style_profile_id`、`cover_url` 一堆字段。

**做法**：路径段 `<int:project_id>` 由 Jinja 注入（照抄 `ai_web.py:703` 的 reader 先例），每页 `onMounted` 打一次 `GET /projects/<id>`（`refreshCurrentProject` @1264 已有这个调用，直接复用）。

代价是多一个请求，收益是**修掉现有的两个隐式缺陷**：深链不再依赖 `loadProjects()` 先完成；项目不在列表里时不再静默什么都不做。

### 4.2 `currentChapter`（902）→ 留在页内，用 query param 做可选深链

不进路径段。理由：`openChapter` / `backToChapterList` / `openAdjacentChapter` 全是页内切换，而 `adjacentChapter()` 提供上一章/下一章连续跳转——若每跳一次全量重载页面，体验倒退且会打断 pipeline。

**做法**：`?chapter=<id>` 可选深链 + `history.replaceState` 同步（`token_login.html:247` 有先例），页内切换仍是纯前端。

### 4.3 `streamOutput`（868）+ `currentJobId`（869）→ 每页一份，需在文档里写明

现状三处共用同一个缓冲区并互相覆盖（`step_start` 在 1704 直接 `streamOutput.value = ''`）。拆页后天然变成项目页一份、章节页一份——**这其实是改善**，但必须在页面上写明「跨页不保留输出」，否则用户会以为内容丢了。

### 4.4 `projectMetaForm`（883）的双份 UI → 收回写权限

pipeline 弹窗第②区（749–768）与 overview 的 profiles section（207–247）绑同一个对象；`startChapterPipeline`(1664–1665) 与 `startBatchChapterPipeline`(1788–1789) 在启动前 `await saveProjectProfiles()` + `await saveProjectStyleControl()` 落库。

拆页后这成为真正的跨页写冲突：章节页的弹窗改了档案 → 落库 → 项目页若已打开则显示陈旧值。

**做法**：章节页弹窗②区改为**只读展示 + 「去项目页修改」链接**，写权限收回项目页一处。注意这会动到测试 #12 的 `html.count("await saveProjectStyleControl()") >= 2` 断言——那条断言守的是「保存风格设定必须在启动 pipeline 前落库」这个行为契约，拆页后要改成跨文件断言（在章节页断言它调了这个函数，同时保证该函数在章节页也存在）。

### 4.5 `?project_id=` 兼容层

`dashboard_wizard.html:232` 的 `confirmImportWizard()` 成功后跳 `/dashboard/ai?project_id=<id>`，这是「创作向导导入 → 跳去写作页」的闭环，且 `docs/frontend-pages.md:272` 把它写成了公开契约。

**做法**：`/dashboard/ai` 带 `project_id` 时服务端 302 到 `/dashboard/ai/projects/<id>`，wizard 一行不改；同时更新 `docs/frontend-pages.md:272` 的契约描述为「兼容旧深链，302 重定向」。

### 4.6 `beforeunload` 守卫（拆分引入的**新**风险）

全站目前没有任何 unload 拦截（`dashboard_novel_detail.html:662` 那处是存阅读进度）。拆分后侧栏多出 3 个可点的 AI 子项，误点导航丢掉一个跑了 5 分钟的 pipeline 的概率显著上升。

**做法**：章节页在 `pipelineRunning || batchPipelineRunning` 时装 `beforeunload` 守卫。这是本次拆分必须新增的验收条件，不是既有问题。

## 5. 为什么 pipeline 不能单独成页（否决记录）

pipeline 弹窗的真实体量：**markup 118 行 / 10134 字节 + JS 334 行 / 16042 字节 = 452 行 / 26 KB，占整个模板 23%**；加上它写回的 `chapterDashboard` 面板（87 行 markup）是 539 行 / 33 KB。它本身就比 `dashboard_settings_system.html`（304 行）大一倍。体量上完全够单独成页。

**但在当前架构下拆它是错的**：

- `onPipelineModalClose`（1848）的注释明确写着「运行中允许后台运行——只关弹窗，不停 pipeline」。
- `POST /api/dashboard/ai/jobs/<id>/continue`（`ai_web.py:1547`）是**换下一个模型重试**，不是「重连一条在跑的流」。
- 因此 **pipeline 的运行态 100% 活在浏览器内存里，没有任何重连机制**。`pipelineSteps` / `streamOutput` / `retryFailedPipelineSteps` 的自动重试循环全在前端。

从工作区导航到独立的 pipeline 页 = 丢掉刚触发的流。服务端每步已独立写库所以**产出不丢**，但 UI 无法恢复，用户看到的是任务凭空消失。

**前置条件**：要先给 pipeline 做服务端可重连——新端点回放 `ai_jobs` 状态，或照抄 `dashboard_settings_models.html:380/444/468-472` 的 localStorage + `operation_id` 范式（那是全站唯一的「刷新后接回未完成长任务」现成实现）。做完才谈得上拆页。

**本轮采取的中间态**：markup 抽成 `{% include %}` partial，JS 留在同一 `setup()`。

## 6. 测试断言迁移（本次最大风险）

`grep -rn "dashboard_ai.html" tests/` 命中 9 行、2 个文件，实际牵动 **14 个模板测试函数 + 4 个文档测试函数**。基线 `pytest tests/test_frontend_library_os.py tests/test_ai_page_routes.py -q` → 38 passed。

比上一轮设置页的 12 处更多，且**带 4 个上一轮没有的硬陷阱**。

### 6.1 四个硬陷阱

`tests/test_ai_page_routes.py:10` 的 `AI_PAGES` tuple 被 4 个函数循环消费——新增的每个 AI 页面都必须进这个 tuple 并**逐条满足**：

| 陷阱 | 测试 | 为什么会挂 | 处置 |
|---|---|---|---|
| **#2** | `test_ai_pages_route_every_request_through_shared_helpers`（L35） | 正向要求字面量 `window.csrfFetch`，而它全模板只有 1 处真实调用（`detectAITells` @1065）。除章节页外，新页面只用 `window.aiApi` / `window.streamSSE`（两者内部才调 csrfFetch），**直接失败** | 见下方统一处置 |
| **#3** | `test_ai_pages_use_shared_error_text`（L44） | 同上，`window.errorText` 的唯一真实调用在 1067 | 同上 |
| **#4** | `test_ai_pages_do_not_hand_roll_sse_parsing`（L51） | 正向要求 `window.streamSSE`，但笔记页的 states/search 和项目列表页没有任何流式请求 | 同上 |
| **#12b** | `test_ai_project_overview_uses_single_panel_...`（L232） | 两处：(a) 它按 `v-show="projectDetailTab === 'overview'"` 和字面注释 `<!-- 长篇规划 -->` 切片（实测两个锚点共 3 处出现），拆页后都不存在；(b) `html.count("await saveProjectStyleControl()") >= 2`（测试 L274）的两处调用在 `startChapterPipeline`(1665) 和 `startBatchChapterPipeline`(1789)——**都在章节页** | (a) 切片逻辑重写为直接读项目页全文；(b) 改成跨文件断言，见 §4.4 |

**#2/#3/#4 的统一处置**（三条同病，必须一起改）：

病根有两层。表层是把「公共层用得对」的**正向**断言绑在「每个 AI 页面」上，而这些名字在原模板里各只有 1–5 处真实调用、集中在少数函数——拆页后大多数页面天然不满足。深层是它们都是**朴素子串断言**，所以 1019–1024 那段注释同样能满足它们（§2.4）：抄注释即可假通过。

处置：

1. **负向断言对所有页面保留且加强**——不得出现自建 `csrfFetch` / `ensureCsrfToken` / `'/api/csrf-token'` / `getReader()` / 裸 `await fetch(` / `window.fetch(`。这部分本来就对，且拆页后依然成立。
2. **正向断言改为条件式**：先剥掉注释再判定「该页是否存在需要该助手的行为」（有直接 REST 调用 → 必须出现 `window.csrfFetch` 或 `window.aiApi`；有流式请求 → 必须出现 `window.streamSSE`；有错误提示 → 必须出现 `window.errorText`），只对满足前提的页面断言。
3. **剥注释**是这次必须新增的一步：断言前把 `//` 行注释与 `<!-- -->` 块注释去掉再 grep。否则第 2 步照样能被注释骗过。

**正确做法是改断言语义，不是给每页硬塞一次调用。** 必须与拆分在同一个 commit。

### 6.2 其余 10 个模板测试

| # | 测试 | 行 | 迁往 |
|---|---|---|---|
| 1 | `test_ai_pages_do_not_redefine_csrf_helpers` | L26 | 全部新页面（纯负向，安全） |
| 5 | `test_ai_and_wizard_share_one_profile_loader` | L58（tuple 在 L60） | 项目页（加载档案的那页） |
| 6 | `test_ai_and_wizard_routes_render_distinct_pages` | L68（`client.get("/dashboard/ai")` 在 L84） | ⚠️ `client.get` 默认不跟 302。**这就是 §3 表格里「渲染真页面」那一栏的原因**——`/dashboard/ai` 不能像设置页那样裸路径 302 |
| 7 | `test_dashboard_pages_are_marked_as_library_pages` | L39（列表项 L55） | 一行换四个新模板名（与上一轮 L49–53 同型，可照抄） |
| 8 | `test_ai_and_wizard_templates_do_not_embed_other_workspace` | L65（L66） | 改成对每个新模板循环。**顺手删掉 L80–92 三段完全重复的历史复制粘贴** |
| 9 | `test_ai_pages_share_complete_output_panel_component` | L100（L101） | 章节页——`<output-panel>` 全模板只用 1 处（616），组件注册在 848 |
| 10 | `test_ai_project_pages_prefer_cover_url_with_gradient_fallback` | L200（L203） | 项目页（对应 147/152/157、1143/1175/1165） |
| 11 | `test_ai_dashboard_api_adds_csrf_to_mutating_requests` | L215（L216） | 与 #1+#2 内容基本重复，**合并到一处** |
| 13 | `test_ai_project_overview_keeps_project_summary_compact_at_narrow_desktop` | L277（L278） | 同 #12 的切片陷阱；断言本体迁项目页 |
| 14 | `test_ai_templates_expose_preference_profile_and_strength_controls` | L424（tuple L425） | 四个强度字符串（`off`/`light`/`standard`/`strong`）只在 `preferenceStrengths`(856) 定义 → 只有项目页满足；但 `preference_profile_id` 经 `preferencePayload()`(1210) 进了 longform/chapter/pipeline 的所有 payload → 断言按「谁定义 / 谁引用」拆开 |

### 6.3 四个文档测试

L137 / L151 / L363 三条要求 `docs/frontend-pages.md` 与 `docs/frontend-api-contract.md` 含特定字符串；L515 是上一轮设置页拆分留下的同型测试模板，**AI 拆分照抄一份**。

需改的文档位置：`docs/frontend-pages.md:24`（路由表行）、`:266-276`（`### /dashboard/ai` 小节含 `?project_id=` 深链契约）、`docs/frontend-api-contract.md:34`、`README.md:122`。

### 6.4 新增守卫测试

现有 18 个测试函数里**没有一条检查 `@click` 绑定的函数是否在 `setup()` 返回值里**。`window.initVueApp`（`base.html:322`）只做 `createApp(setupFunc)` + `mount`，返回对象是唯一渲染作用域——漏导出就是静默失效的按钮。

原模板已有 3 个这样的坏按钮（`@447 autoResolveForeshadows`、`@790/@791 runSingleStep`，三个后端端点都健在），**正由独立任务修复，不在本轮范围**。但拆成 4 个页面 = 4 份 `exported` 对象，漏导出概率翻 4 倍。

**新增断言**：每个 AI 页面 markup 里出现的每个 `@click` / `@keyup` 处理器名，必须出现在该页 `setup()` 的返回对象中。这条测试要覆盖全部四个新页面。

## 7. 实施顺序与文件清单

前置：等「修 3 个坏按钮」的独立任务合入 main 后再开始——两者都改 `dashboard_ai.html`，并行会冲突。

| 步 | 内容 | 关键约束 |
|---|---|---|
| 1 | 加固 §6.1 的三条公共层断言（剥注释 + 条件式正向） | **独立于拆分，可先落**。落之前它们在现有三页上必须仍绿 |
| 2 | `ai_web.py` 加 4 个页面路由 + `?project_id=` 302 兼容；建 `dashboard_ai_project_nav.html` partial | 四条都在 `register_ai_routes` 内，自动继承 `webapp.py:704` 鉴权门；`/dashboard/ai` 必须渲染真页面（陷阱 #6）；`NAV_ITEMS` 零改动（§3.0） |
| 3 | 项目列表页 + 项目页（overview 三 section + longform + RawImport） | 每页只加载自己的数据（§3.2）；`{[ ]}` 是 Jinja 变量分隔符 |
| 4 | 章节页 + `dashboard_ai_pipeline_modal.html` partial + `beforeunload` 守卫 | pipeline JS 留在同一 `setup()`（§5）；弹窗②区改只读（§4.4） |
| 5 | 笔记页；**删 `dashboard_ai.html`；迁移 14 + 4 处断言；新增 `@click` 导出守卫** | **必须同一个 commit**，否则一删旧模板就是一片红 |
| 6 | 更新 `docs/frontend-pages.md`、`frontend-api-contract.md`、`README.md`、`CLAUDE.md` | §6.3 列的四个位置 |

每个模板的 `<script>` 块改完用 `node --check` 校验（先替换 Jinja 占位符）——语法错在 pytest 里抓不到，只会在浏览器里白屏。

## 8. 验收标准

- `pytest` 全绿，基线 1427 passed / 4 skipped（本轮应更多）。
- 四个新路由在**已认证**会话下 HTTP 200；`/dashboard/ai?project_id=<id>` 302 到 `/dashboard/ai/projects/<id>`；不存在的 project_id 返回 404 而非白屏。
- `grep -rn "dashboard_ai.html" src/ tests/` 除「断言其不存在」外为空。
- 每页请求数符合 §3.2 的表（用浏览器 network 或 `preview_network` 核对，不是靠读代码推断）。
- 章节页在 pipeline 运行中触发导航会被 `beforeunload` 拦截。
- 从 `/dashboard/wizard` 导入项目仍能跳到正确的项目页（§4.5 的闭环）。
- pipeline 从章节页触发、关闭弹窗后仍在后台跑、重开弹窗能看到进度——**与拆分前行为一致**（§5 的否决前提）。

## 9. 明确不在本轮范围

- pipeline 的服务端可重连（§5 的前置条件）。做完它才能考虑 pipeline 独立成页。
- `detectAITells`(1062–1075) 那 14 行缩成 3 行的清理——它是三个测试正向断言的唯一字面量来源，要与 §6.1 的断言语义改动一起做，或干脆留着。
- 三个坏按钮与死状态 `pipelineExpanded`（1605 声明、1937 导出、markup 零引用）——独立任务处理。
- `/dashboard/wizard`、`/dashboard/novels/ai/<id>` 的内部结构。

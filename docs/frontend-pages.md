# Frontend Pages

本文档记录 Library OS 前端页面、模板、主要接口与交互。前端重写保持 Flask/Jinja 页面路由不变。

## 页面总览

| Route | Template | 页面用途 | Library OS 状态 |
| --- | --- | --- | --- |
| `/token-login` | `src/pixiv_novel_sync/templates/token_login.html` | Token/OAuth 授权 | 待独立视觉适配 |
| `/dashboard` | `src/pixiv_novel_sync/templates/dashboard.html` | 同步控制台、统计、任务状态 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/follows` | `src/pixiv_novel_sync/templates/dashboard_follows.html` | 关注作者列表 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/novels` | `src/pixiv_novel_sync/templates/dashboard_novels.html` | 小说库、追更系列、AI 创作与拯救成功列表 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/novels/<id>` | `src/pixiv_novel_sync/templates/dashboard_novel_detail.html` | 小说详情和阅读页 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/series/<id>` | `src/pixiv_novel_sync/templates/dashboard_series_detail.html` | 系列详情 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/users/<id>` | `src/pixiv_novel_sync/templates/dashboard_user_detail.html` | 作者详情和作者小说 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/pending-deletions` | `src/pixiv_novel_sync/templates/dashboard_pending_deletions.html` | 待确认删除队列 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/logs` | `src/pixiv_novel_sync/templates/dashboard_logs.html` | 同步任务与 AI 创作任务日志 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/settings/sync` | `src/pixiv_novel_sync/templates/dashboard_settings_sync.html` | 同步开关、限速分组、调度表与手动触发 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/settings/models` | `src/pixiv_novel_sync/templates/dashboard_settings_models.html` | Provider、模型目录、模型池 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/settings/agents` | `src/pixiv_novel_sync/templates/dashboard_settings_agents.html` | 普通 Agent 绑定与候选模型链 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/settings/adult` | `src/pixiv_novel_sync/templates/dashboard_settings_adult.html` | 成人润色 Agent、review binding、项目角色 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/settings/system` | `src/pixiv_novel_sync/templates/dashboard_settings_system.html` | 图片缓存、救援 Token、导出、待删除保留期 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/preferences` | `src/pixiv_novel_sync/templates/dashboard_preferences.html` | 偏好画像与推荐 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/ai` | `src/pixiv_novel_sync/templates/dashboard_ai_projects.html` | AI 创作项目列表（新建 / 打开 / 删除） | 已接入 `library-page` / `library-page-header` |
| `/dashboard/ai/projects/<project_id>` | `src/pixiv_novel_sync/templates/dashboard_ai_project.html` | 作品资料、封面、蒸馏档案套用、风格控制、长篇规划 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/ai/projects/<project_id>/chapters` | `src/pixiv_novel_sync/templates/dashboard_ai_chapters.html` | 章节列表与单章工作区、自动写作 Pipeline | 已接入 `library-page` / `library-page-header` |
| `/dashboard/ai/projects/<project_id>/notes` | `src/pixiv_novel_sync/templates/dashboard_ai_notes.html` | 伏笔追踪、状态记忆、语义检索 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/wizard` | `src/pixiv_novel_sync/templates/dashboard_wizard.html` | 创作向导与蒸馏档案 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/novels?category=ai` | `src/pixiv_novel_sync/templates/dashboard_novels.html` | AI 创作小说库 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/novels?category=rescue` | `src/pixiv_novel_sync/templates/dashboard_novels.html` | 拯救成功小说与系列 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/novels/ai/<project_id>` | `dashboard_ai_reader.html` | AI 创作小说阅读 | 已接入 `library-page` / `library-page-header` |

## Shared layout

### `base.html`

职责：

- 加载 Tailwind CDN。
- 加载 Vue 3 CDN。
- 定义 Library OS 全局 CSS tokens。
- 提供 `library-shell`、`library-sidebar`、`library-main`。
- 保留 `window.initVueApp(setupFunc)`。
- Include `vue_components.html`。

关键 CSS/DOM：

- `data-theme="library-os"`
- `--library-bg`
- `--library-surface`
- `--library-accent`
- `library-shell`
- `library-sidebar`
- `library-main`
- `library-card`
- `library-table`

### `vue_components.html`

组件：

- `app-sidebar-nav`
- `app-sidebar-footer`
- `app-mobile-bar`
- `app-pagination`
- `app-badge`
- `app-modal`

Shared APIs：

- `GET /api/dashboard/shell-data`
- `GET /api/dashboard/status`
- `GET /api/dashboard/auto-sync/status`

## 页面详情

### `/dashboard`

Template: `dashboard.html`

用途：同步操作入口、统计卡片、运行中任务、最近活动、定时任务状态。

APIs:

- `GET /api/dashboard/status`
- `GET /api/dashboard/sync/status`
- `GET /api/dashboard/auto-sync/status`
- `GET /api/dashboard/logs`
- `POST /api/dashboard/check-bookmarks`
- `POST /api/dashboard/sync/subscribed-series`
- `POST /api/dashboard/sync/start`
- `POST /api/dashboard/auto-sync/toggle`
- `POST /api/dashboard/auto-sync/stop-task`

关键交互：

- 手动同步。
- 收藏预检查。
- 追更系列同步。
- 自动同步启停。
- 日志轮询和任务进度展示。

### `/dashboard/novels`

Template: `dashboard_novels.html`

用途：展示收藏小说和追更系列。

APIs:

- `GET /api/dashboard/novels`

关键交互：

- 搜索。
- 分类切换。
- 排序。
- 分页。
- 跳转小说详情或系列详情。

### `/dashboard/novels/<id>`

Template: `dashboard_novel_detail.html`

用途：小说详情、阅读、系列章节导航。

APIs:

- `GET /api/dashboard/novels/{novel_id}`
- `GET /api/dashboard/series/{series_id}`

关键交互：

- 阅读进度。
- 字号切换。
- 系列上一章/下一章。
- 返回小说库/系列。

### `/dashboard/follows`

Template: `dashboard_follows.html`

用途：关注作者列表。

APIs:

- `GET /api/dashboard/users`

关键交互：

- 状态 tab。
- 分页。
- 作者详情跳转。

### `/dashboard/users/<id>`

Template: `dashboard_user_detail.html`

用途：作者资料、作者小说列表、作者检查/同步。

APIs:

- `GET /api/dashboard/users/{user_id}`
- `GET /api/dashboard/users/{user_id}/novels`
- `POST /api/dashboard/users/{user_id}/check`
- `POST /api/dashboard/users/{user_id}/sync`

### `/dashboard/series/<id>`

Template: `dashboard_series_detail.html`

用途：系列资料和章节列表。

APIs:

- `GET /api/dashboard/series/{series_id}`
- `DELETE /api/dashboard/series/{series_id}`
- `PUT /api/dashboard/rescue-overrides/series/{series_id}`
- `DELETE /api/dashboard/rescue-overrides/series/{series_id}`

小说和系列详情接口都附带 `rescue` 评估对象。详情页可将 Pixiv 可用性人工标记为 `include` 或 `exclude`，也可删除人工纠错并恢复自动判断；写请求必须携带 `X-CSRF-Token`。人工纠错只影响远端可用性判断，不能绕过本地正文完整性检查。

### `/dashboard/pending-deletions`

Template: `dashboard_pending_deletions.html`

用途：展示本地归档中疑似已取消收藏/追更的项目。

APIs:

- `GET /api/dashboard/pending-deletions`
- `POST /api/dashboard/pending-deletions/detect`
- `POST /api/dashboard/pending-deletions/{deletion_id}/confirm`
- `POST /api/dashboard/pending-deletions/{deletion_id}/restore`
- `GET /api/dashboard/sync/status`

### `/dashboard/logs`

Template: `dashboard_logs.html`

用途：任务日志列表和详情弹窗。任务类型分为“同步任务”和“AI 创作任务”，默认保留最近 3 天；AI 任务支持类型、状态和时间筛选。AI 详情展示候选快照 hash、PromptBudget、实际 Provider/模型、模型池、attempt 错误与耗时；`partial` 单独标记为“部分完成”。存在未尝试候选时，用户可显式选择“使用下一个模型继续”，页面不会自动重试。

APIs:

- `GET /api/dashboard/logs`
- `GET /api/dashboard/logs/{log_id}`
- `GET /api/dashboard/ai/jobs/<job_id>`
- `POST /api/dashboard/ai/jobs/<job_id>/continue`

### 设置（五个一级页面）

`/dashboard/settings` 只做 302，一律落到 `/dashboard/settings/sync`。旧的单页 + URL hash 分区导航已经废弃，改为五个独立路由，各自一个模板、一个 Vue 应用，共享 Jinja 导航条 `dashboard_settings_nav.html`（纯静态链接，高亮取 `request.path`）。侧栏「设置」在 Operations 分组下展开为同名的五个二级项。

| Route | Template | 内容 |
|---|---|---|
| `/dashboard/settings/sync` | `dashboard_settings_sync.html` | 基础设置、限速与分页（按收藏 / 关注作者 / 系列 / 巡检分组）、定时同步调度表、手动触发 |
| `/dashboard/settings/models` | `dashboard_settings_models.html` | Provider CRUD（`#ai-api`）、模型目录、模型池（`#ai-model-pools`）与最近尝试记录 |
| `/dashboard/settings/agents` | `dashboard_settings_agents.html` | 普通 Agent 的绑定 / 提示词 / 采样参数（`#ai-agents`）、候选模型链预览 |
| `/dashboard/settings/adult` | `dashboard_settings_adult.html` | 成人润色 Agent、`safety` / `fact_guard` review binding、项目角色与成人确认 |
| `/dashboard/settings/system` | `dashboard_settings_system.html` | 图片缓存、救援 API Token（`#rescue-api`）、统计导出、待删除保留期 |

保存按分区独立进行：同步页调 `PUT /api/dashboard/settings/sync`，系统页调 `PUT /api/dashboard/settings/system`。分区端点只采纳本区字段，其余字段沿用磁盘上的旧值——每页表单只含自己那一区，走全量端点会把没加载的字段写成默认值。AI 三页的配置存在数据库里，走 `ai_web.py` 的端点，不经过 `/api/dashboard/settings`。

`agents` 页的**候选模型链**回答「这个 Agent 实际会依次调用哪些模型」：选中 Agent 后按真实路由顺序列出 `① Provider / model_key（来源）`，来源区分固定绑定、池成员第 N 位与第 N 级后备池成员，并显示响应里的四个硬上限（候选尝试 / 网络请求 / 解析候选数 / 池节点数）。数据来自只读端点 `GET /api/dashboard/ai/agents/<agent_id>/candidates`，它只做候选解析、不发起任何生成请求，也不回传 Provider 的连接地址与密钥。固定绑定的链只有一个元素，此时不显示模型池区块。

`models` 页的模型池编辑器额外展示该池**最近的真实尝试记录**（`GET /api/dashboard/ai/model-pools/<pool_id>/attempts`）：每条显示状态、Provider / 模型、池内位置、阶段、耗时、错误 scope/category/message 与所属 job。`partial` 与 `failed` 分开显示——`partial` 是已经开始输出正文之后才失败，路由不会再转移到下一个候选。

`sync` 页的调度表展示**优先级**（P1 收藏 / P2 追更系列 / P3 其余）、**可让位**标记、**下次运行时间**（`GET /api/dashboard/auto-sync/status` 的 `task_priorities` / `task_preemptible` / `task_next_run`），以及**上一轮耗时**与**预估每日占用**（`GET /api/dashboard/auto-sync/budget`）。优先级的唯一事实来源是后端 `web/managers.py:SCHEDULER_TASK_CONFIGS`，前端不另存一份；行顺序即抢槽次序。cron 输入框改动后调 `POST /api/dashboard/settings/cron-preview` 校验并列出下次 5 次触发时刻——cron 写错时调度器会静默回落到按 interval 跑，不预览就发现不了。限速区的「收藏最大页数」对应 `sync.bookmark_max_pages_per_run`，留空时回落到 `max_pages_per_run`。

APIs:

- `GET /api/dashboard/settings`
- `PUT /api/dashboard/settings/<section>`（`section` 为 `sync` 或 `system`）
- `POST /api/dashboard/settings`（全量端点，保留兼容）
- `POST /api/dashboard/settings/reload`
- `POST /api/dashboard/settings/cron-preview`
- `GET /api/dashboard/auto-sync/status`（优先级、可让位、下次运行时间）
- `GET /api/dashboard/auto-sync/budget?days=3`（上一轮耗时、每日预算、占空比）
- `GET /api/cache/status`、`POST /api/cache/clear`
- `POST /api/dashboard/sync/{task_type}`
- `GET /api/dashboard/rescue-token/status`、`POST /api/dashboard/rescue-token/rotate`
- `GET /api/dashboard/export/stats`
- Provider / Agent CRUD。
- `GET|POST /api/dashboard/ai/providers/<provider_id>/models`
- `POST /api/dashboard/ai/providers/<provider_id>/models/sync`
- `GET|DELETE /api/dashboard/ai/model-sync-operations/<operation_id>`
- `GET /api/dashboard/ai/model-sync-operations/<operation_id>/events`
- `POST /api/dashboard/ai/model-sync-operations/<operation_id>/confirm-empty`
- `GET /api/dashboard/ai/agents/<agent_id>/candidates`
- 模型池 CRUD、`PUT /api/dashboard/ai/model-pools/<pool_id>/members` 与 `GET /api/dashboard/ai/model-pools/<pool_id>/attempts`，详见 `frontend-api-contract.md`。

`system` 页的救援 API 只展示 Token 前缀与轮换时间。完整救援 Token 只在生成或轮换成功后显示一次，关闭窗口时立即清空页面中的明文。`models` 页不回显 API Key；模型池编辑器列出所有可能接收 Prompt 的 Provider，并明确提示跨 Provider 故障转移的隐私范围。

### 成人配置（`/dashboard/settings/adult`）

成人 Agent 不走普通 Agent 的生命周期入口。设置页先选择项目，维护结构化的虚构角色（年龄、年龄依据、别名和 revision），再在 adult confirmation 中打开成人内容并勾选当前角色 revision。`safety` 与 `fact_guard` 两个固定 review binding 必须分别绑定支持 `json` 的 Provider 模型或模型池；binding、角色或确认 revision 变化后，阅读页会要求重新获取 Provider scope。

推荐配置顺序是 Provider 模型目录/模型池、`adult_polish` Agent、两个 JSON review binding、项目角色记录、成人确认。没有 `DASHBOARD_TOKEN` 时成人 API 即使来自 localhost 也返回 `403`；成人功能不是普通 Pipeline 的自动步骤。

### `/dashboard/preferences`

Template: `dashboard_preferences.html`

用途：偏好画像、推荐搜索计划、推荐反馈、屏蔽管理。

APIs:

- Preference profile APIs。
- Recommendation APIs。

### AI 创作（四个一级页面）

`dashboard_ai.html` 已删除。原来那一页（项目列表 + 作品资料 + 章节工作区 + 伏笔/状态记忆，靠 `pageMode` 与内层 tab 切换）拆成四个独立路由，各自一个模板、一个 Vue 应用；三个项目内页面共享 Jinja 导航条 `dashboard_ai_project_nav.html`（纯静态链接，高亮取 `request.path`）。侧栏「AI 创作」仍是单个一级项、指向项目列表——项目内导航依赖 `project_id`，没法照设置页那样写成侧栏的静态 children。

| Route | Template | 内容 |
|---|---|---|
| `/dashboard/ai` | `dashboard_ai_projects.html` | 项目列表：新建、打开、删除 |
| `/dashboard/ai/projects/<project_id>` | `dashboard_ai_project.html` | 作品资料、封面、蒸馏档案套用、风格控制与偏好画像、长篇规划 |
| `/dashboard/ai/projects/<project_id>/chapters` | `dashboard_ai_chapters.html` | 章节列表、单章工作区、自动写作 Pipeline（弹窗为 `dashboard_ai_pipeline_modal.html`） |
| `/dashboard/ai/projects/<project_id>/notes` | `dashboard_ai_notes.html` | 伏笔追踪、状态记忆、语义检索 |

`project_id` 由路由注入而非前端解析 URL：模板里写 `const projectId = {[ project_id ]};`（Jinja 定界符是 `{[ ]}`，写成 `{{ }}` 会被 Vue 当插值吃掉，JS 直接语法错）。三个项目内页面都过 `ai_web.py:_require_project`，项目不存在时返回 404，而不是渲染一张所有请求都失败的空白页。

旧深链 `/dashboard/ai?project_id=<id>` 仍然可用（创作向导导入完成后跳的就是它）：值为正整数时 302 到 `/dashboard/ai/projects/<id>`，其余取值（`0`、`abc`、`-1`、空）一律回落到项目列表，不制造 `/dashboard/ai/projects/0` 这种 404 深链。

关键约束：

- 四个页面都不初始化创作向导会话或蒸馏表单，也不互相内嵌另一页的作用域。
- 公共层一律取自 `base.html`，页面不自建副本：`window.csrfFetch`、`window.errorText`、`window.streamSSE`、`window.aiApi`。流式写请求统一附加 CSRF Token。
- 章节页在启动 pipeline 前幂等写回一次风格设定与档案绑定（`saveProjectStyleControl` / `saveProjectProfiles`，单章与批量两个入口都写）。后端 `ai/services/projects.py:_project_style_control_prompt` 读的是**已落库**的项目记录，所以编辑 UI 在项目页、写回动作必须留在章节页。
- 章节页的伏笔改为在 `openChapter()` 里按需加载（`if (!foreshadows.value.length)`），不在 mount 时拉：既守住首屏 3 个请求的载入预算，又保留到期伏笔提醒。

### `/dashboard/wizard`

Template: `dashboard_wizard.html`

用途：创作向导会话、素材导入、READY 项目导入和蒸馏档案管理。蒸馏来源支持手动文本、归档小说、归档系列和文档。

### `/dashboard/novels?category=ai`

Template: `dashboard_novels.html`

用途：按小说库卡片样式展示 AI 创作小说，复用项目封面并进入统一阅读页。

### `/dashboard/novels?category=rescue`

模板：`dashboard_novels.html`

用途：展示本地已完整或部分备份、但 Pixiv 小说或系列已经失效的数据。系列按一个卡片展示，不把系列章节重复平铺成单篇卡片。

筛选项（页面共三个下拉，均在变化时重置分页）：

- 救援状态 `state`：`all` / `success`（完整救援）/ `partial`（部分救援）。
- 内容类型 `content_kind`：`all` / `series`（系列）/ `series_chapter`（系列单章）/ `standalone`（独立小说）。
- 救援来源 `source_kind`：`all` / `bookmark`（我的收藏）/ `subscribed_series`（我的追更）/ `following_user`（关注用户）/ `user_backup`（用户备份）。
- 标题/作者搜索 `search`，排序 `sort` 取 `checked_desc`（最近检查，默认）或 `updated_desc`（最近更新）；救援分类下不提供收藏数/浏览数排序。

接口另外支持 `item_type`（`novel` / `series`），但仅在未指定 `content_kind` 时生效；页面固定发送 `content_kind`，因此 `item_type` 实际不参与筛选。

页面还展示目录的 `refreshed_at`，`stale` 为真时提示「数据可能已过期」。

API：`GET /api/dashboard/rescues`。

### Pixiv 救援油猴脚本

文件：`userscripts/pixiv-rescue.user.js`（v0.1.0，380 行，无外部依赖）。

用途：当 Pixiv 原小说或系列页面明确删除、受限或不存在时，通过只读救援 API 在原页面追加私人备份内容，并以“拯救数据”醒目标记来源。

生效范围与部署：

- `@match` 限定 `https://www.pixiv.net/novel/show.php*` 与 `https://www.pixiv.net/novel/series/*`。
- 脚本内 `API_ORIGIN` 常量硬编码为 `https://pixiv.dongboapp.com`。**换域名部署时必须同时改 `API_ORIGIN`、`@connect` 和 `@namespace`**，否则 Tampermonkey 会拦截跨域请求。
- 通过油猴菜单「设置或更新救援 Token」/「清除救援 Token」维护 Token，存在 `GM_setValue`（键 `pixivRescueToken`）。

失效判定（保守策略，正常页面不介入）：

- 页面文本命中 `UNAVAILABLE_MARKERS`（中/日/英三套文案，如「この作品は削除されています」「该作品已被删除」「Page not found」）。
- 且在约 1.2 秒轮询窗口内始终没有渲染出正常正文（`.novel-text`、`article` 等选择器下 ≥12 字符）或章节链接。

安全边界：

- 正常可阅读的 Pixiv 页面不请求救援 API，也不改写原 DOM。
- 救援 Token 只通过 `Authorization: Bearer` 请求头发送，不写入页面或 Cookie。
- API 域名固定，不接受页面、响应或用户输入提供的其他来源地址。
- 正文只通过 `textContent` 和新建文本节点渲染，不解释备份正文中的 HTML。
- 系列先加载目录，超过 100 章时可继续加载后续目录页；只有点击某一章时才请求该章正文。
- 请求超时 15 秒；接口失败时保留 Pixiv 原错误页面。

### `/dashboard/novels/ai/<project_id>`

Template: `dashboard_ai_reader.html`

用途：显示 AI 作品封面、目录和章节正文，视觉与小说库详情页一致。阅读页的“成人润色”页签只在原始正文上通过 `v-text` 提供连续片段选择，不提交正文或上下文；选择后依次确认 Provider scope、成人 Agent 和已确认的虚构成年人角色。

成人润色交互顺序：

1. 在原始章节文本中选择连续范围；范围以 Unicode code point 计数，中文、emoji 和组合字符不能被拆开。
2. 调用 `POST /api/dashboard/ai/polish/adult/scope`，确认返回的完整 Provider/model 分组及 `provider_scope_hash`。
3. 调用 stream，观察 `metadata`、`progress`、两阶段 `validation`、`candidate` 和 `done`；若连接中断，使用 signed `/events` 恢复同一 job 的 validation/candidate/done 状态；warning 候选只有拿到 scoped `warning_ack_hash` 后才允许应用。
4. 仅对当前章节 revision、角色确认 revision、Provider scope 和校验 hash 都未变化的候选调用 apply。发生 `409` 时重新选择/确认并生成，不在前端重放旧正文。

候选展示只提供只读前后文、原片段、Unicode-safe diff 和校验 code；应用成功后重新加载章节，后端只替换目标区间。未应用候选按三天清理，应用后不保留任务正文。

### `/token-login`

Template: `token_login.html`

用途：保存 refresh token 或走 OAuth 登录任务。

APIs:

- `GET /api/token-config`
- `POST /api/token-jobs`
- `GET /api/token-jobs/{job_id}`
- `POST /api/save-token`
- OAuth APIs。

## 共享 partial

以下模板不是独立页面，而是被多个页面 `{% include %}` 的共享片段；修改时需同时回归所有引用页面：

| Partial | 被引用于 | 用途 |
| --- | --- | --- |
| `dashboard_ai_output_panel.html` | `dashboard_ai_chapters.html`、`dashboard_wizard.html` | AI 生成输出面板（流式输出、阶段/进度展示、结果操作） |
| `dashboard_ai_source_search.html` | `dashboard_wizard.html` | 创作向导的素材来源搜索面板 |
| `dashboard_ai_project_nav.html` | `dashboard_ai_project.html`、`dashboard_ai_chapters.html`、`dashboard_ai_notes.html` | 项目内导航条（资料 / 章节 / 笔记），纯静态链接 |
| `dashboard_ai_pipeline_modal.html` | `dashboard_ai_chapters.html` | 自动写作 Pipeline 弹窗的 markup；对应的 `setup()` 在章节页里 |
| `dashboard_settings_nav.html` | 五个 `dashboard_settings_*.html` | 设置页导航条，纯静态链接 |

## Validation checklist

每页改动后检查：

- 页面能打开。
- Vue 能 mount。
- 导航高亮正确。
- 按钮仍调用原 API。
- 图片仍走 `/proxy/image?url=...`。
- loading/error/empty/success 状态可见。
- 移动端主导航可用。

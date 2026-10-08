# Frontend Pages

本文档记录 Library OS 前端页面、模板、主要接口与交互。前端重写保持 Flask/Jinja 页面路由不变。

> AI 写作相关页面（`/dashboard/ai` 项目/章节/笔记、`/dashboard/wizard`、AI 阅读页、成人润色设置页）随 AI 写作模块移到 `ai-writing` 分支维护，main 分支不再包含；main 只保留模型目录 / 模型池 / Agent 绑定等 AI 基础设施页面。

## 本轮交互更新（2026-10-08，main）

移动优先补充于2026-10-09，位于本地 `codex/mobile-first-main` 分支；登录、推荐、阅读与管理的实际验收和未部署边界见 [移动改造报告](MOBILE_IMPLEMENTATION_REPORT_2026-10-08.md)。下文契约描述当前分支代码，不等同于生产站点已经更新。

- 偏好页增量分析不再发送固定 `name`；未提供的名称/描述由服务端保留。
- Agent 设置表单可编辑 `context_window` 和 `top_p`；编辑时保留合法的零值，不用默认值覆盖 `temperature=0` / `top_p=0`。
- 定时备份的不完整状态进入“部分完成”，取消不计失败用户；旧待删除计数与推荐历史轮次路由已移除。
- main 不引入写作章节 revision、Pipeline 或成人交互；这些仅在 ai-writing。最新自动化验证与浏览器/移动端实际验收分别见 [整改状态](REMEDIATION_STATUS_2026-09-30.md)。

## 页面总览

10-08 共享入口补齐：小说阅读页可确认后重置阅读进度、移除本地收藏记录，不取消 Pixiv 收藏；请求失败保持当前位置。阅读恢复在无本机记录时按服务端百分比滚动到对应位置，保留本机位置优先级；任何手动滚动或显式保存/重置使旧恢复失效。无生产调用、可能与后台备份竞争的批量用户删除 API 按 T3-01 允许方案移除，不新增其按钮。以上有 Node 运行期回归，不代替真机视觉验收。

| Route | Template | 页面用途 | Library OS 状态 |
| --- | --- | --- | --- |
| `/token-login` | `src/pixiv_novel_sync/templates/token_login.html` | Token/OAuth 授权 | 待独立视觉适配 |
| `/dashboard` | `src/pixiv_novel_sync/templates/dashboard.html` | 同步控制台、统计、任务状态 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/follows` | `src/pixiv_novel_sync/templates/dashboard_follows.html` | 关注作者列表 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/novels` | `src/pixiv_novel_sync/templates/dashboard_novels.html` | 小说库、追更系列与拯救成功列表 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/novels/<id>` | `src/pixiv_novel_sync/templates/dashboard_novel_detail.html` | 小说详情和阅读页 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/series/<id>` | `src/pixiv_novel_sync/templates/dashboard_series_detail.html` | 系列详情 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/users/<id>` | `src/pixiv_novel_sync/templates/dashboard_user_detail.html` | 作者详情和作者小说 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/pending-deletions` | `src/pixiv_novel_sync/templates/dashboard_pending_deletions.html` | 待确认删除队列 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/logs` | `src/pixiv_novel_sync/templates/dashboard_logs.html` | 同步任务与 AI 创作任务日志 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/settings/sync` | `src/pixiv_novel_sync/templates/dashboard_settings_sync.html` | 同步开关、限速分组、调度表与手动触发 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/settings/models` | `src/pixiv_novel_sync/templates/dashboard_settings_models.html` | Provider、模型目录、模型池 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/settings/agents` | `src/pixiv_novel_sync/templates/dashboard_settings_agents.html` | 普通 Agent 绑定与候选模型链 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/settings/system` | `src/pixiv_novel_sync/templates/dashboard_settings_system.html` | 图片缓存、救援 Token、导出、数据保留期 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/preferences` | `src/pixiv_novel_sync/templates/dashboard_preferences.html` | 偏好画像与推荐 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/novels?category=rescue` | `src/pixiv_novel_sync/templates/dashboard_novels.html` | 拯救成功小说与系列 | 已接入 `library-page` / `library-page-header` |

## Shared layout

### `base.html`

职责：

- 加载 Tailwind CDN。
- 加载 Vue 3 CDN。
- 定义 Library OS 全局 CSS tokens。
- 提供 `library-shell`、`library-sidebar`、`library-main`。
- 保留 `window.initVueApp(setupFunc)`。
- Include `vue_components.html`。
- 提供 `body_class` Jinja block；`pns-reader-page` 在手机断点隐藏全局底栏，阅读页负责自己的工具栏与底部留白。
- `window.registerPageComponents(app)` 为可选页面钩子，在全局组件注册之后、`app.mount('#app')` 之前执行，不改变现有 `initVueApp(options)` 调用方式。

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

手机底栏固定为首页、书库、作者与系列、任务、更多，不继续增加底栏项目。更多收纳偏好、待确认删除、四个设置页和退出；桌面保留侧栏。退出须检查 HTTP 与 `{ok:true}`，错误保留并允许重试。

`app-modal` 保留 `title` / `isOpen` / `close` / 默认与 footer 插槽，新增默认 `true` 的 `closeOnBackdrop`。弹窗 Teleport 到 body，支持焦点保持/返回、Escape、背景滚动锁、多个弹窗的锁持有与卸载清理；窄屏可滚动、footer 可换行。一次性 Token 弹窗必须传 `:close-on-backdrop="false"`，显式关闭仍由页面清空明文。

Shared APIs：

- `GET /api/dashboard/shell-data`
- `GET /api/dashboard/status`
- `GET /api/dashboard/auto-sync/status`

## 页面详情

### `/api/auth/login`

Template: `login.html`（独立模板，不继承 base）。访问密码输入16px，主控件至少48px，记住选项默认不勾选。错误、限流和存储故障在页内显示；不将密码写入 HTML 回显或 localStorage，允许浏览器密码管理器。勾选后固定30天；未勾选为浏览器会话且服务端最长7天。普通访问不滚动续期，退出后服务端撤销，旧版 Cookie 需要首次重新登录。详细字段与部署前提见 [认证契约](frontend-api-contract.md#认证与健康检查-apis)。

### `/dashboard`

Template: `dashboard.html`

用途：统计卡片、运行状态条（定时同步开关 + 当前活动任务 + 停止当前）、最近推书结果。手动同步与预检查的入口在设置页（`/dashboard/settings/sync` 的手动触发区），本页不再承载。

布局：标题 + 四项核心统计（小说总数 / 关注作者 / 追更系列 / 待确认）是页内第一张 `library-card`，与下方区块同一套表面（22px 圆角 + library 阴影）；不再是通栏 sticky 直角横条。卡片底部是一条**运行状态条**：左侧是定时同步全局开关（「启用/停用定时同步」，这是全局启停的唯一 UI 入口——设置页只有逐任务的 cron / 开关），右侧是有定时任务在跑时才出现的「停止当前」，中间在任务运行时显示「任务执行中 + 当前任务名（`currentTaskName`，按 `task_type` 映射成中文）+ 进度百分比」，点击进入任务日志页。

原先下方的「最近活动」时间线与「定时任务」列表面板已移除：推书列表上来后它们信息重复，任务历史归「任务日志」页、逐项调度状态归设置页的调度表。

「最近推书结果」复用 `recommendation_components.html` 的单列卡片与分页控制器（每页10条）。手机统计为紧凑四列，任务控制可展开；区块标题和筛选操作可换行，不挤占正文。卡片标题至少两行，理由可展开，标签限量；只有标题链接跳转 Pixiv，反馈按钮不包在外链内。

隐藏已反馈默认启用，分页请求传 `status=new`，服务端过滤/计数/有效页保持一致。显式翻页立即定位结果标题；反馈刷新、重试与过滤不偷偷延迟滚动。反馈请求防重复，错误留在卡片/结果区域；末页消失采用服务端回落页。

APIs:

- `GET /api/dashboard/status`
- `GET /api/dashboard/sync/status`
- `GET /api/dashboard/auto-sync/status`
- `GET /api/dashboard/recommendations/items?page=&page_size=`
- `POST /api/dashboard/auto-sync/toggle`
- `POST /api/dashboard/auto-sync/stop-task`

关键交互：

- 定时同步全局启停与停止当前任务（均在运行状态条）。
- 推书列表翻页并定位结果标题；刷新按钮回到第一页但不强制移动当前视口。
- 任务状态轮询（`fetchJobStatus` 每 3 秒、`fetchAutoSync` 每 10 秒）。

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

手机使用独立“保存进度 / 字号 / 章节 / 更多”工具栏，隐藏全站底栏。保存采样当前位置，不跳回页首；重置、移除和删除仍须确认。章节与管理使用共享弹窗，管理结果在弹窗内部持续可见；失败不关闭正文、不清除进度。

五个阅读/列表页面使用 `navigation_helpers.html`：`safeDashboardReturn` / `withDashboardReturn` 校验站内 dashboard 路径，`createDashboardScrollState` 仅在 sessionStorage 保存路径+查询对应的像素位置。显式返回和浏览器返回保留筛选/页码，延迟恢复遇到用户手动操作就取消；不依赖 referrer，不缓存密码或正文。存储被禁用时仍保留 URL 上下文，但不能保证像素位置持久化。

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

HTTP/业务/JSON错误与真正空列表区分。失败保留已选择的筛选和页码，提供原地“重试加载”；重试只发同一列表 GET，不启动检测、确认或恢复。请求代次保护避免旧响应覆盖新筛选。

APIs:

- `GET /api/dashboard/pending-deletions`
- `POST /api/dashboard/pending-deletions/detect`
- `POST /api/dashboard/pending-deletions/{deletion_id}/confirm`
- `POST /api/dashboard/pending-deletions/{deletion_id}/restore`
- `GET /api/dashboard/sync/status`

### `/dashboard/logs`

Template: `dashboard_logs.html`

用途：任务日志列表和详情弹窗。任务类型分为“同步任务”和“AI 创作任务”，默认保留最近 14 天（`sync.task_log_retention_days`，两张表共用），天数下拉提供 1 / 3 / 7 / 14 天；AI 任务支持类型、状态和时间筛选。AI 详情展示候选快照 hash、PromptBudget、实际 Provider/模型、模型池、attempt 错误与耗时；`partial` 单独标记为“部分完成”。存在未尝试候选时，用户可显式选择“使用下一个模型继续”，页面不会自动重试。

同步任务的黄色“部分完成”只留给**真出了问题**的轮次：`aborted_reason`（熔断中止）或 `truncated`（分页触顶）。关注作者按 `user_last_synced` 轮转，每轮只覆盖 `users_limit` 个作者、必然带 `incomplete`，这种轮次由 `rotation_pending` 标记为绿色“成功”——否则耗时最大的任务永远是黄色，真正的中止就被淹没了。判定在 `webapp.py:_task_log_status_for_stats`，只置 `incomplete` 而不说明原因的站点仍然算 partial。

轮转进度显示 `rotation_never_synced`（“还有 N 位作者从未同步”，单调递减到 0）或 `rotation_oldest_age_days`（“最久 X 天未同步”），**不用 `users_remaining`**——后者等于候选数减 `users_limit`，每轮恒定（生产上永远是 251），当进度看没有信息量。详情弹窗另外展示 `rotation_deprioritized`：已确认没有小说的作者（Pixiv 关注不区分插画与小说）会被降频，`NO_NOVELS_RECHECK_DAYS` 天内不占轮转槽位。

单条日志的 `stats` 有 8 KB 上限（`webapp.py:_prune_stats_for_log`）。超限时按字段体积裁剪，被裁掉的字段在 `_pruned` 里留下“类型 + 元素数 + 字节数”。这条存在的原因是 `recommendation_run` 曾把整个候选列表（连正文摘要）写进 stats，单轮 351 KB。裁剪只作用于落库那份，运行中的进度接口读的仍是完整对象。

APIs:

- `GET /api/dashboard/logs`
- `GET /api/dashboard/logs/{log_id}`
- `GET /api/dashboard/ai/jobs/<job_id>`
- `POST /api/dashboard/ai/jobs/<job_id>/continue`

### 设置（四个一级页面）

移动管理约定：Provider/模型池编辑会定位并聚焦正确表单；池读取失败保持旧快照不可写，可只读重试或取消新建。池写操作独占编辑状态直到回读/错误处理完成，期间禁止改选、新建和池删除，独立Provider编辑不被锁定。错误在操作区域持续显示，不能把上一操作成功留给下一次失败。Agent候选选择器在窄屏堆叠限宽；一次性Token复制反馈在弹窗内部，遮罩不关闭，明确关闭后清空明文。

`/dashboard/settings` 只做 302，一律落到 `/dashboard/settings/sync`。旧的单页 + URL hash 分区导航已经废弃，改为四个独立路由，各自一个模板、一个 Vue 应用，共享 Jinja 导航条 `dashboard_settings_nav.html`（纯静态链接，高亮取 `request.path`）。侧栏「设置」在 Operations 分组下展开为同名的四个二级项。成人润色设置页随 AI 写作模块移到 `ai-writing` 分支。

| Route | Template | 内容 |
|---|---|---|
| `/dashboard/settings/sync` | `dashboard_settings_sync.html` | 基础设置、限速与分页（按收藏 / 关注作者 / 系列 / 巡检分组）、定时同步调度表、手动触发 |
| `/dashboard/settings/models` | `dashboard_settings_models.html` | Provider CRUD（`#ai-api`）、模型目录、模型池（`#ai-model-pools`）与最近尝试记录 |
| `/dashboard/settings/agents` | `dashboard_settings_agents.html` | 普通 Agent 的绑定 / 提示词 / 采样参数（`#ai-agents`）、候选模型链预览 |
| `/dashboard/settings/system` | `dashboard_settings_system.html` | 图片缓存、救援 API Token（`#rescue-api`）、统计导出、数据保留期（已确认删除记录 + 任务日志） |

`system` 页的数据保留期只有两个字段：`pending_deletion_cleanup_confirmed_days`（已确认/已恢复记录留多久）与 `task_log_retention_days`（任务日志，两张表共用）。**待确认（pending）记录没有保留期**——它们必须由用户手动确认或恢复，不会到期自动消失。曾经存在的 `pending_deletion_grace_period_days` 从未被任何代码读取（`cleanup_old_pending_deletions` 第一行就丢弃该参数），却在页面上写着「等待人工确认的宽限天数」，已于 2026-09-03 移除；`save_sync_settings` 会在下次保存时把这个遗留键从 YAML 里擦掉。

保存按分区独立进行：同步页调 `PUT /api/dashboard/settings/sync`，系统页调 `PUT /api/dashboard/settings/system`。分区端点只采纳本区字段，其余字段沿用磁盘上的旧值——每页表单只含自己那一区，走全量端点会把没加载的字段写成默认值。AI 两页的配置存在数据库里，走 `ai_web.py` 的端点，不经过 `/api/dashboard/settings`。

AI 两页（models / agents）页顶共享 **AI 配置总览横幅**（`dashboard_ai_health_band.html`），数据来自只读端点 `GET /api/dashboard/ai/health`——纯数据库投影、零网络请求，可随意刷新。它把「现在到底在用什么、坏没坏」说到页面上：Provider 数、必失败数、可路由模型数、绑在坏 Provider 上的 Agent 数，以及按任务类型归并的 AI 任务失败次数（其中 `keyword_clean` 会标注为静默降级）。Agent 的红点是**继承**自它绑定的 Provider / 模型池，不是 Agent 自己坏了。

`agents` 页的**候选模型链**回答「这个 Agent 实际会依次调用哪些模型」：选中 Agent 后按真实路由顺序列出 `① Provider / model_key（来源）`，来源区分固定绑定、池成员第 N 位与第 N 级后备池成员，并显示响应里的四个硬上限（候选尝试 / 网络请求 / 解析候选数 / 池节点数）。数据来自只读端点 `GET /api/dashboard/ai/agents/<agent_id>/candidates`，它只做候选解析、不发起任何生成请求，也不回传 Provider 的连接地址与密钥。固定绑定的链只有一个元素，此时不显示模型池区块。

`models` 页的模型池编辑器额外展示该池**最近的真实尝试记录**（`GET /api/dashboard/ai/model-pools/<pool_id>/attempts`）：每条显示状态、Provider / 模型、池内位置、阶段、耗时、错误 scope/category/message 与所属 job。`partial` 与 `failed` 分开显示——`partial` 是已经开始输出正文之后才失败，路由不会再转移到下一个候选。

`sync` 页的调度表展示**优先级**（P1 收藏 / P2 追更系列 / P3 其余）、**可让位**标记、**下次运行时间**（`GET /api/dashboard/auto-sync/status` 的 `task_priorities` / `task_preemptible` / `task_next_run`），以及**上一轮耗时**与**预估每日占用**（`GET /api/dashboard/auto-sync/budget`）。优先级的唯一事实来源是后端 `web/managers.py:SCHEDULER_TASK_CONFIGS`，前端不另存一份；行顺序即抢槽次序。cron 输入框改动后调 `POST /api/dashboard/settings/cron-preview` 校验并列出下次 5 次触发时刻——cron 写错时调度器会静默回落到按 interval 跑，不预览就发现不了。限速区的「收藏最大页数」对应 `sync.bookmark_max_pages_per_run`，留空时回落到 `max_pages_per_run`。

APIs:

- `GET /api/dashboard/settings`
- `PUT /api/dashboard/settings/<section>`（`section` 为 `sync` 或 `system`）
- `POST /api/dashboard/settings`（全量端点，保留兼容）
- `POST /api/dashboard/settings/cron-preview`
- `GET /api/dashboard/auto-sync/status`（优先级、可让位、下次运行时间）
- `GET /api/dashboard/auto-sync/budget?days=3`（上一轮耗时、每日预算、占空比）
- `GET /api/cache/status`、`POST /api/cache/clear`
- `POST /api/dashboard/sync/{task_type}`
- `GET /api/dashboard/rescue-token/status`、`POST /api/dashboard/rescue-token/rotate`
- `GET /api/dashboard/export/stats`
- Provider / Agent CRUD。
- `GET /api/dashboard/ai/health?days=7`
- `POST /api/dashboard/ai/providers/probe-models`
- `GET|POST /api/dashboard/ai/providers/<provider_id>/models`
- `POST /api/dashboard/ai/providers/<provider_id>/models/sync`
- `GET|DELETE /api/dashboard/ai/model-sync-operations/<operation_id>`
- `GET /api/dashboard/ai/model-sync-operations/<operation_id>/events`
- `POST /api/dashboard/ai/model-sync-operations/<operation_id>/confirm-empty`
- `GET /api/dashboard/ai/agents/<agent_id>/candidates`
- `PUT /api/dashboard/ai/agents/bindings`
- 模型池 CRUD、`PUT /api/dashboard/ai/model-pools/<pool_id>/members` 与 `GET /api/dashboard/ai/model-pools/<pool_id>/attempts`，详见 `frontend-api-contract.md`。

`system` 页的救援 API 只展示 Token 前缀与轮换时间。完整救援 Token 只在生成或轮换成功后显示一次，关闭窗口时立即清空页面中的明文。`models` 页不回显 API Key；模型池编辑器列出所有可能接收 Prompt 的 Provider，并明确提示跨 Provider 故障转移的隐私范围。

### `/dashboard/preferences`

Template: `dashboard_preferences.html`

用途：偏好画像、推荐搜索计划、推荐反馈、屏蔽管理。

默认先展示与首页相同的推荐卡片/过滤/分页；画像、增量分析和搜索管理折叠在结果之后。画像读取独立于推荐，不因画像失败挡住推荐；画像错误在折叠内容之外持续提示，并提供只读重试。推荐部分失败（例如作者屏蔽已成功但反馈失败）必须明确说明并重新读取实际列表。

APIs:

- Preference profile APIs。
- Recommendation APIs。

### `/dashboard/novels?category=rescue`

模板：`dashboard_novels.html`

用途：展示本地已完整或部分备份、但 Pixiv 小说或系列已经失效的数据。系列按一个卡片展示，不把系列章节重复平铺成单篇卡片。

筛选项（页面共三个下拉，均在变化时重置分页）：

- 救援状态 `state`：`all` / `success`（完整救援）/ `partial`（部分救援）。
- 内容类型 `content_kind`：`all` / `series`（系列）/ `series_chapter`（系列单章）/ `standalone`（独立小说）。
- 救援来源 `source_kind`：`all` / `bookmark`（我的收藏）/ `subscribed_series`（我的追更）/ `following_user`（关注用户）/ `user_backup`（用户备份）。
- 标题/作者搜索 `search`，排序 `sort` 取 `checked_desc`（最近检查，默认）或 `updated_desc`（最近更新）；救援分类下不提供收藏数/浏览数排序。

接口另外支持 `item_type`（`novel` / `series`），但仅在未指定 `content_kind` 时生效；页面固定发送 `content_kind`，因此 `item_type` 实际不参与筛选。

卡片右上角的徽标除内容类型与救援状态外，还会在 `personal_relation` 为真时显示「我收藏/追更过」：该字段区分「本人收藏 / 追更」与「关注扫描 / 全量备份」两类来源。生产实测拯救成功的条目全部来自批量扫描（`bookmark` 来源为 0），不标出来就无法分辨哪些丢失内容是自己真正在乎的。

已进入「待确认删除」（`pending`）的条目不出现在本列表：本人取消收藏 / 追更的作品以「等你决定」为准，避免同一作品同时挂在待确认与拯救两个列表里。

页面还展示目录的 `refreshed_at`，`stale` 为真时提示「数据可能已过期」。

来源摘要保留紧凑显示，另有44px“展开全部来源”按钮，展开项按类型+ID独立，不依赖 hover，也不会误触作品链接。

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
| `dashboard_settings_nav.html` | 四个 `dashboard_settings_*.html` | 设置页导航条，纯静态链接 |
| `dashboard_ai_health_band.html` | `dashboard_settings_models.html`、`dashboard_settings_agents.html` | AI 配置只读健康横幅，数据来自 `GET /api/dashboard/ai/health` |
| `recommendation_components.html` | `dashboard.html`、`dashboard_preferences.html` | 推荐卡片、共享列表/反馈控制器、页内注册钩子与局部样式 |
| `navigation_helpers.html` | 小说库、关注作者、作者详情、系列详情、小说阅读 | 安全返回 URL、列表滚动保存/取消/恢复，不读取正文或凭据 |

## Validation checklist

每页改动后检查：

- 页面能打开。
- Vue 能 mount。
- 导航高亮正确。
- 按钮仍调用原 API。
- 图片仍走 `/proxy/image?url=...`。
- loading/error/empty/success 状态可见。
- 移动端主导航可用。

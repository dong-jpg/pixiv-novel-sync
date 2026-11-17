# Frontend Pages

本文档记录 Library OS 前端页面、模板、主要接口与交互。前端重写保持 Flask/Jinja 页面路由不变。

> AI 写作相关页面（`/dashboard/ai` 项目/章节/笔记、`/dashboard/wizard`、AI 阅读页、成人润色设置页）随 AI 写作模块移到 `ai-writing` 分支维护。当前代码保留模型目录 / 模型池 / Agent 绑定、手动作品分析，并在偏好页内新增“偏好学习”：明确阅读评价、个人 AI 偏好 / XP 画像、全库及新同步作品分析、推荐反馈消费。实现与生产部署分开记录，见 [项目现状](STATUS.md)。

## 本轮交互更新（2026-10-08，main）

### AI 设置第一阶段（2026-11-03，重构分支）

- `/dashboard/settings/models` 保留 URL，`view=providers|models|groups` 三视图与 `provider=<id>` 筛选随普通导航/刷新/历史往返保存；缺失 Provider 显示明确错误，不伪造空目录。路由组仍使用原模型池整数 ID/API，仅支持顺序尝试。
- 共享模型选择器保留不可用当前值，名称相同也按 Provider/模型身份区分；打开、过滤、目录刷新、关闭都不写配置。组内上/下移动使用真实有序 members 和 `expected_version` CAS；保存/丢弃/取消草稿确认、冲突保留与读回失败分开处理。
- Provider onboarding 区分保存成功、同步 pending、同步失败、启停应用冲突和成功。初始启停只对完整已保存目录 revision 应用；空目录须确认，运行时可取消。Key、表单和选择不写浏览器持久存储；只有既存 operation recovery key 保存 operation ID。
- `/dashboard/settings/agents` 标题为“AI 任务”，编辑仅 PUT 用户触碰且改变的允许字段；绑定作为完整互斥 tuple 提交，未触碰不重写。手工固定模型明确显示未经验证，保留零/false 和旧设置。
- 配置候选链 GET 与输入预检 POST 分开。两处已保存任务选择器共享候选链读取：切换立即清除旧链/错误，空选项和离开使在途读取失效，旧响应不能覆盖新任务；编辑、显式刷新和保存后的刷新保持单次预期读取。预检显示已保存 system prompt 和完整测试文本，只显式覆盖勾选参数；输入改变、任务切换、清除或离开使迟到结果失效。不读取小说、不调用 Provider、不创建 job；大窗口/短窗口逐候选分别显示。
- `/dashboard/logs` 显示服务器 v1/v2 安全预算、validation_status、已报告/未知/不完整用量；不显示私有快照 hash/输入指纹、不自行累计 token。main 无可执行 Continue；旧 job 详情响应不能覆盖新选项或重开关闭的弹窗。

真实离线 Vue/浏览器375/768/1440验证与真机软键盘、真实 Provider1M准入验收分别记录在 [项目现状](STATUS.md)，本页不把 Node VM 当作浏览器测量。

移动优先改造于2026-10-09从 `codex/mobile-first-main` 集成到 `main`；登录、推荐、阅读与管理的实际验收和未部署边界见 [移动改造报告](archive/MOBILE_IMPLEMENTATION_REPORT_2026-10-08.md)。下文记录 `main` 功能范围及明确标注的审计分支后续修复，不等同于已合并或生产站点已经更新，当前版本以 [项目现状](STATUS.md) 为准。

- 偏好页增量分析不再发送固定 `name`；未提供的名称/描述由服务端保留。
- Agent 设置表单可编辑 `context_window` 和 `top_p`；编辑时保留合法的零值，不用默认值覆盖 `temperature=0` / `top_p=0`。
- 定时备份的不完整状态进入“部分完成”，取消不计失败用户；旧待删除计数与推荐历史轮次路由已移除。
- main 不引入写作章节 revision、Pipeline 或成人交互；这些仅在 ai-writing。最新自动化验证与浏览器/移动端实际验收分别见 [项目现状](STATUS.md)。

## 页面总览

小说阅读页可确认后重置阅读进度、移除本地收藏记录，不取消 Pixiv 收藏；请求失败保持当前位置。阅读页每次打开都核对服务端版本，五秒节流保存；本机离线记录和旧缓存保留，冲突由用户明确选择。重置留下递增版本标记，任何手动滚动或显式保存/重置使旧恢复失效。无生产调用、可能与后台备份竞争的批量用户删除 API 按 T3-01 允许方案移除，不新增其按钮。Node 与离线 Chromium 证据见 [项目现状](STATUS.md)，不代替真机体验验收。

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
| `/dashboard/logs` | `src/pixiv_novel_sync/templates/dashboard_logs.html` | 同步任务与 AI 任务日志 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/remote` | `src/pixiv_novel_sync/templates/dashboard_remote.html` | 显式诊断/单本 EPUB、任务状态、结果下载与人工保留 | 复用 Library OS；默认关闭时显示说明 |
| `/dashboard/settings/sync` | `src/pixiv_novel_sync/templates/dashboard_settings_sync.html` | 同步开关、限速分组、调度表与手动触发 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/settings/models` | `src/pixiv_novel_sync/templates/dashboard_settings_models.html` | Provider、模型目录、模型池 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/settings/agents` | `src/pixiv_novel_sync/templates/dashboard_settings_agents.html` | 普通 Agent 绑定与候选模型链 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/settings/system` | `src/pixiv_novel_sync/templates/dashboard_settings_system.html` | 图片缓存、救援 Token、导出、数据保留期 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/preferences` | `src/pixiv_novel_sync/templates/dashboard_preferences.html` | 偏好画像与推荐 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/preferences?view=work-analysis` | `dashboard_preferences.html` 内 `dashboard_work_analysis.html` | 手动已归档单作品内容报告 | 复用 Library OS；不替换默认偏好页 |
| `/dashboard/preferences?view=learning` | `dashboard_preferences.html` 内 `dashboard_preference_learning.html` | 阅读喜恶、AI 画像、全库批次与持续自动分析 | 与默认推荐 / 单作品视图通过页内导航互通 |
| `/dashboard/novels?category=rescue` | `src/pixiv_novel_sync/templates/dashboard_novels.html` | 拯救成功小说与系列 | 已接入 `library-page` / `library-page-header` |

## Shared layout

### `base.html`

职责：

- 引用仓库里预生成的 Tailwind 样式 `/static/css/tailwind.css?v=<内容哈希>`（放在 `<head>` 最后；改模板类名后运行 `python scripts/build_tailwind_css.py` 重生成）。
- 加载 Vue 3 CDN（锁定版本，带 SRI）。
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

用途：统计卡片、运行状态条（定时同步开关 + 当前活动任务 + 停止当前）、最近推书结果。手动同步的入口在设置页（`/dashboard/settings/sync` 的手动触发区），本页不再承载。

布局：标题 + 四项核心统计（小说总数 / 关注作者 / 追更系列 / 待确认）是页内第一张 `library-card`，与下方区块同一套表面（22px 圆角 + library 阴影）；不再是通栏 sticky 直角横条。卡片底部是一条**运行状态条**：左侧是定时同步全局开关（「启用/停用定时同步」，这是全局启停的唯一 UI 入口——设置页只有逐任务的 cron / 开关），右侧是有定时任务在跑时才出现的「停止当前」，中间在任务运行时显示「任务执行中 + 当前任务名（`currentTaskName`，按 `task_type` 映射成中文）+ 进度百分比」，点击进入任务日志页。

原先下方的「最近活动」时间线与「定时任务」列表面板已移除：推书列表上来后它们信息重复，任务历史归「任务日志」页、逐项调度状态归设置页的调度表。

「最近推书结果」复用 `recommendation_components.html` 的单列卡片与分页控制器（每页10条）。手机统计为紧凑四列，任务控制可展开；区块标题和筛选操作可换行，不挤占正文。卡片标题至少两行，理由可展开，标签限量；只有标题链接跳转 Pixiv，反馈按钮不包在外链内。

隐藏已反馈默认启用，分页请求传 `status=new`，服务端过滤/计数/有效页保持一致。显式翻页立即定位结果标题；反馈刷新、重试与过滤不偷偷延迟滚动。反馈请求防重复，错误留在卡片/结果区域；末页消失采用服务端回落页。

初次加载与已有列表刷新分开：刷新/失败提示不卸载已有卡片，保持同ID卡片的展开状态及用户交互；失败可以只读GET重试，不能用清空列表或延迟滚动代替状态协调。

APIs:

- `GET /api/dashboard/status`
- `GET /api/dashboard/sync/status`
- `GET /api/dashboard/auto-sync/status`
- `GET /api/dashboard/recommendations/items?page=&page_size=`
- `POST /api/dashboard/auto-sync/toggle`
- `POST /api/dashboard/auto-sync/stop-task`
- `POST /api/dashboard/sync/cancel?job_id=`（「停止当前」对手动任务走这里，定时任务走 `stop-task`）

关键交互：

- 定时同步全局启停与停止当前任务（均在运行状态条）。
- 推书列表翻页并定位结果标题；刷新按钮回到第一页但不强制移动当前视口。
- 任务状态轮询（`fetchJobStatus` 有任务排队/运行/取消中时每 3 秒、空闲时每 20 秒；`fetchAutoSync` 每 10 秒；标签页隐藏时跳过请求）。

### `/dashboard/novels`

Template: `dashboard_novels.html`

用途：展示收藏小说和追更系列；`category=rescue` 时展示拯救成功列表（见下文）。

APIs:

- `GET /api/dashboard/novels`
- `GET /api/dashboard/rescues`（`category=rescue`）

关键交互：

- 搜索。
- 分类切换。
- 排序。
- 分页。
- 跳转小说详情或系列详情。

### `/dashboard/novels/<id>`

Template: `dashboard_novel_detail.html`

用途：小说详情、阅读、系列章节导航。进度按服务端版本同步，支持离线重试、冲突选择和重置保护；界面行为与 HTTP 契约见[跨设备阅读进度](READING_PROGRESS_SYNC.md)。

APIs:

- `GET /api/dashboard/novels/{novel_id}`
- `GET /api/dashboard/series/{series_id}`
- `GET|POST|DELETE /api/dashboard/novels/{novel_id}/progress`
- `POST /api/dashboard/novels/export-epub`
- `DELETE /api/dashboard/bookmarks/{novel_id}`
- `DELETE /api/dashboard/novels/{novel_id}`
- `PUT|DELETE /api/dashboard/rescue-overrides/novel/{novel_id}`

关键交互：

- 阅读进度。
- 字号切换。
- 系列上一章/下一章。
- 返回小说库/系列。

手机使用独立“保存进度 / 字号 / 章节 / 更多”工具栏，隐藏全站底栏。保存采样当前位置，不跳回页首；重置、移除和删除仍须确认。章节与管理使用共享弹窗，管理结果在弹窗内部持续可见；失败不关闭正文、不清除进度。

正文由 `pixiv_markup.html`（`window.pixivMarkup.renderHtml`）渲染 Pixiv 标记：`[newpage]` 显示为「k / N 页」分隔条（整篇仍连续滚动，进度照旧按滚动位置计），`[chapter:]` 显示为章节标题，`[[rb:]]` 用 `<ruby><rt>`，`[[jumpuri:]]` 只放行 http(s)，`[jump:N]` 在页内滚到第 N 页且不改 URL hash（带 hash 打开会跳过进度恢复），插图标记显示为占位说明。所有文字与属性都经转义，只产出白名单标签；解析规则与 EPUB 导出的 `pixiv_markup.py` 共用夹具与模糊比对测试。

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

用途：任务日志列表和详情弹窗。任务类型分为“同步任务”和“AI 任务”，默认保留最近 14 天（`sync.task_log_retention_days`，两张表共用），天数下拉提供 1 / 3 / 7 / 14 天；AI 任务支持类型、状态和时间筛选。AI 详情展示服务器安全 v1/v2 计划、Provider/模型、路由组、attempt 错误/耗时、业务校验和用量摘要；`partial` 单独标记为“部分完成”。不显示快照 hash 或输入指纹；main continue 不可用，不提供可执行继续按钮。

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

Agent批量改绑的确认快照固定所选ID和绑定；失败在活动确认弹窗内持续显示alert并允许明确重试。待处理请求防重复，关闭保留当前选择，晚响应不能把旧错误或旧选择覆盖回新状态；已确认写入但回读失败要说明“写入成功、刷新失败”，不能引导重复PUT。

`/dashboard/settings` 只做 302，一律落到 `/dashboard/settings/sync`。旧的单页 + URL hash 分区导航已经废弃，改为四个独立路由，各自一个模板、一个 Vue 应用，共享 Jinja 导航条 `dashboard_settings_nav.html`（纯静态链接，高亮取 `request.path`）。侧栏「设置」在 Operations 分组下展开为同名的四个二级项。成人润色设置页随 AI 写作模块移到 `ai-writing` 分支。

| Route | Template | 内容 |
|---|---|---|
| `/dashboard/settings/sync` | `dashboard_settings_sync.html` | 基础设置、限速与分页（按收藏 / 关注作者 / 系列 / 巡检分组）、定时同步调度表、手动触发 |
| `/dashboard/settings/models` | `dashboard_settings_models.html` | Provider CRUD（`#ai-api`）、模型目录、模型池（`#ai-model-pools`）与最近尝试记录 |
| `/dashboard/settings/agents` | `dashboard_settings_agents.html` | 普通 Agent 的绑定 / 提示词 / 采样参数（`#ai-agents`）、候选模型链预览 |
| `/dashboard/settings/system` | `dashboard_settings_system.html` | 图片缓存、救援 API Token（`#rescue-api`）、统计导出、数据保留期（已确认删除记录 + 任务日志） |

`system` 页的数据保留期只有两个字段：`pending_deletion_cleanup_confirmed_days`（已确认/已恢复记录留多久）与 `task_log_retention_days`（任务日志，两张表共用）。**待确认（pending）记录没有保留期**——它们必须由用户手动确认或恢复，不会到期自动消失。曾经存在的 `pending_deletion_grace_period_days` 从未被任何代码读取（当时 `cleanup_old_pending_deletions` 第一行就丢弃该参数，现在该函数已不再接收它），却在页面上写着「等待人工确认的宽限天数」，已于 2026-09-03 移除；`save_sync_settings` 会在下次保存时把这个遗留键从 YAML 里擦掉。

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

默认用途：本地统计画像、推荐搜索计划、推荐反馈、屏蔽管理。无 `view`（或非 `work-analysis/learning` 值）保留默认页面。三种视图共用页内导航，分别只 mount 自己的 Vue setup，不把新分析请求自动加到旧页。

默认先展示与首页相同的推荐卡片/过滤/分页；画像、增量分析和搜索管理折叠在结果之后。画像读取独立于推荐，不因画像失败挡住推荐；画像错误在折叠内容之外持续提示，并提供只读重试。推荐部分失败（例如作者屏蔽已成功但反馈失败）必须明确说明并重新读取实际列表。

每次“按当前画像推书”先重建搜索计划，包含最新的有效学习信号；构建失败不得拿旧缓存继续提交，构建期间双击只发起一次推书流程。卡片匹配到明确阅读评价时显示来源，使用启用 AI 画像时可通过 `?view=learning&profile_run_id=<id>` 核对该画像。推荐项感兴趣 / 不感兴趣仍是原有推荐反馈，不自动等同于读后喜欢 / 不喜欢。

APIs:

- Preference profile APIs。
- Recommendation APIs。

#### 作品分析独立视图

`/dashboard/preferences?view=work-analysis` 包含 `dashboard_work_analysis.html`、`dashboard_work_analysis_scripts.html`，由 `static/js/work_analysis.js` 驱动；使用 [Work Analysis APIs](frontend-api-contract.md#work-analysis-apis)。小说/系列详情链接用 `source_kind/source_id` 预选单篇或系列，`analysis_id` 可选择已有任务；默认偏好视图不加载作品报告或自动启动分析。

- 显示 Agent 当前绑定及不可用原因，名称相同也不混淆身份，不静默替换当前选择。用户显式填请求数/计划 token/活跃秒预算（初始为空），核对正文范围、章序、缺章/空章、连载/unknown、全部可能外发的 Provider/模型，再确认外发和预算。预览不解密 Key、不联网或创建 job，不能当作真实服务可用证明。
- 源、章序、Agent、输出上限或预算变化使旧预览及同意失效；主动刷新预览也需重新确认。不用收藏/追更推断已读或喜欢，不把输入覆盖当成原作完整、汇总完成或解释正确。
- 任务/单元分页和轮询只展示安全身份、计数、状态/reason、已报告/未知用量与覆盖；不在进度里输出正文或报告。暂停、取消、同步让位分开，增额不重置已花预算。新恢复是作品业务检查点，不调用旧 AI job continue。
- 写入结果未知时保留原请求；创建仅能显式重试同一 body/幂等 key，不能另起重复分析。控制写入先 GET 核对当前状态，GET 不是原命令的回执，也不能证明写入所有权；仅版本仍为 `expected_version` 时允许明确重试同一 CAS。较旧读取不得回退状态；写成功、读回失败只重试读取。未知状态下离页有提示，重载后须先核对已有任务，原请求不持久保存。
- 报告/短证据默认折叠、主动读取、文本节点渲染；不使用 `v-html`，正文/报告/Key/同意不进 URL 或 localStorage。接受删除墓碑后清空报告/单元/证据并撤销在途读取，迟到响应不得复活敏感内容。
- 证据链接为 `/dashboard/novels/<id>?analysis_id=<run>&evidence_id=<id>#work-analysis-evidence`，只携带 ID。阅读页经受保护接口校验同 hash 与服务器 code point 偏移，渲染有界前文/引文/后文；不以浏览器 UTF-16 下标推算。正文改变或删除返回 410，不高亮新版本；同 hash 元数据刷新仍可定位，系列新增章另标 `archive_outdated`。证据标题不能被阅读顶栏遮挡。

实际三宽度浏览器、视觉与真机/软键盘验收边界见 [项目现状](STATUS.md)；Node VM 不代替真实浏览器或设备。

#### 偏好学习独立视图

`/dashboard/preferences?view=learning` 使用 `dashboard_preference_learning.html`、`dashboard_preference_learning_scripts.html`、`static/js/preference_learning.js`；契约见 [Preference Learning APIs](frontend-api-contract.md#preference-learning-apis)，操作步骤见 [使用指南](PREFERENCE_LEARNING_GUIDE.md)。模型选择器复用 `GET /api/dashboard/ai/agents` 的数值 Agent ID，不新增自动模型探测。

- 顶部统计明确喜欢 / 不喜欢、有报告和评价的作品数、缺有效报告数；读取 `GET /preference-learning/state`。state 仅是摘要，初始不加载画像正文。显式 `profile_run_id` 深链会读取指定详情，但画像内容仍折叠。`admission_ready=false` 时显示执行不可用，禁止新创建 / 自动启用 / 批次恢复；读取、停止、取消和删除仍按当前状态可用。
- “这部作品合你的口味吗”组件允许选择本地单篇 / 系列 ID，读取现有评价后保存或撤回。明确评价是独立版本对象；未评价版本为 0。每次保存带 observed `expected_version`，不能在读取前默认覆盖旧评价。保存本身不调用模型，成功后刷新当前学习概览。
- AI 画像表单包含 Agent、输出预留和三个初始为空的支出预算。预览显示来源作品数、缺报告数、最低预留和所有可能目标；明确外发 / 预算同意后才创建。预算、Agent、输出上限变化或主动刷新预览都清除旧同意，按钮与方法入口同时检查，不能沿用旧确认。
- 画像记录显示 `status,version,active,stale,source_count`。选中后主动读取结果，summary / preferences / counterexamples / uncertainties 使用文本节点；来源和短证据逐层折叠。polarity 区分偏好 / 回避 / 混合 / 中立，certainty 区分暂时推测和有依据，不显示为统计概率。来源链接进入原作品报告，短证据进入已有同版本阅读定位。
- queued / running 的画像可取消；只有 succeeded 且未过期的画像，在再次确认后才能“启用到推荐”。首个 AI 画像启用时会确保基础默认画像存在，因此新用户也能直接回默认页推书。删除需要显式确认，清理结果并停止使用该版；state 不列出已删除项时，当前已开结果先清空，再读取详情核对墓碑。
- 全库 / 自动分析共用另一套独立表单：手动单批次或持续发现、`all|new` 范围、Agent、每作品预算、累计预算、作品数及发现间隔。scope / mode / Agent / 任一预算变化均使预览失效。预览用 `purpose:manual|policy` 区分新增起点；create / policy 不发送 purpose。
- 自动 new 从首次启用后新增 / 正文更新算起，暂停 / 恢复 / 增额不重置起点；手动 new 从本功能跟踪开始算起。全库按系列归并、跳过已有同版本匹配结果。归档不完整与现有章序须单独确认，不能从批次数量推断完整原作。
- 手动批次的总预算是本批上限；持续策略的总预算和作品数跨批次累计，不随定时 tick、关闭 / 开启重置。页面展示已分配额度、总授权、已纳入作品和下次发现时间；没有默认支出授权。到达额度需要重新预览并增加上限。
- 批次详情分页显示 target status 和真实 `run_status`，链接保留到原单作品视图。`completed` 明确展示“批次发现已结束”，不是所有作品报告成功。暂停 / 取消作用于本批后续发现及在执行的相关子任务；恢复只续原累计预算和由批次停止的有效工作，原先失败的作品须另行核对。
- 停止自动分析同时暂停未完成的自动批次；当前调用须等待实际退出，已进入最终保存的结果仍可完成。重启后的未完成工作不自动重发可能已付费的请求。状态原因和未知用量可见，不把缺失用量当作零。

整个学习页面的未知创建只允许原 body / 原幂等键重试；未知控制或策略写入须先 GET 核对。版本未变才允许同一 CAS 重试，版本变了只显示当前状态，不声称已证明原写入成功。读取有请求所有权、旧版本保护及删除墓碑保护；浏览器隐藏 / 恢复、页面卸载会停止或重新建立相应读取。画像 / 评价 / 原请求 / 确认不写 localStorage 或 sessionStorage，不执行 HTML，不用 SSE 驱动任务执行。

#### 小说与系列的阅读评价

两种详情页 include `preference_feedback.html` 和 `preference_feedback_scripts.html`，仅在“评价与修改”展开后 GET 对应 `feedback/<kind>/<source_id>`。普通阅读不开新增偏好 API 请求，也不自动外发正文。表单提供喜欢 / 不喜欢 / 中立、最多 500 字说明、保存与撤回；当前版本读取失败时禁止保存，提交未知时锁定并核对，防止用新默认值覆盖另一页已修改的评价。关闭 / 重新打开会重新核对，读请求切换或失效后旧说明不得填进新作品。

现有阅读进度缓存仍保留其原有约定；新增评价与画像不使用浏览器持久缓存。评价会影响后续推荐，但不会仅因保存就触发付费画像生成。

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

文件：`userscripts/pixiv-rescue.user.js`（v0.12.2，无外部依赖）。安装、Token、故障排查与各版本行为见 [救援功能用户指南](RESCUE_USER_GUIDE.md)，当前发布、部署与验收状态统一见 [项目现状](STATUS.md)。

用途：当 Pixiv 原小说或系列页面删除、受限或不存在时，通过只读救援 API 在脚本自己的全屏阅读层里显示私人备份内容，并以“拯救数据”醒目标记来源；收藏 / 追更列表页把失效卡片重建为备份卡片；手机站新作页的「好P友新作」tab 改为「拯救系列」。

生效范围与部署：

- `@match` 覆盖小说页、系列页、收藏列表、追更列表与新作页，另有一条 `https://www.pixiv.net/*` 兜底；脚本内部 `routeRescue` 按路径严格判断，0.12.2 除 `/en/`、`/zh/` 外也识别 `/zh-tw/` 等带地区的语言前缀，其余页面立即返回。
- 默认服务地址是 `DEFAULT_API_ORIGIN`（`https://pixiv.dongboapp.com`），脚本存储键 `pixivRescueOrigin` 有值时覆盖它。**换域名部署时必须同时改 `DEFAULT_API_ORIGIN`（或设置 `pixivRescueOrigin`）和 `@connect`**，否则脚本管理器会拦截跨域请求；`@name` / `@namespace` 保持不变，否则升级会被当成另一份脚本。
- 通过油猴菜单「设置或更新救援 Token」/「清除救援 Token」维护 Token，存在 `GM_setValue`（键 `pixivRescueToken`）。

失效判定（保守策略，正常原内容保持不变）：

- 小说 / 系列页先请求 Pixiv 自己的 `/ajax/novel/<id>`、`/ajax/novel/series/<id>`：作品还在就不请求备份；只有 404/410 才算确认失效，403 等歧义状态按「状态不明」静默查备份，查到才打开阅读层。
- 从救援卡片点进来的链接带 `pixiv_rescue=1`，直接打开阅读层。
- 列表页只改 Pixiv 已经画成失效 / 空白的卡片（页面文案 `UNAVAILABLE_MARKERS` 与 `invisible-work-container` 等结构信号），健康卡片不动。

安全边界：

- 正常可阅读的小说 / 系列页不请求备份内容，也不替换原内容；列表页可做一次 Token 探测请求（`lookup?novel_ids=1`），用于提示 Token 缺失或失效，只重建失效卡片。手机新作页的「拯救系列」tab 是专门展示救援系列的例外，不将它描述为零请求页面。
- 救援 Token 只保存在脚本特权存储、通过 `Authorization: Bearer` 请求头发送，不写入 Pixiv 页面 DOM、Cookie 或查询参数；`pixiv_rescue=1` 只是打开阅读层的标记，不携带 Token。
- API 地址只来自 `DEFAULT_API_ORIGIN` 或脚本存储 `pixivRescueOrigin`，不接受页面或响应提供的其他来源地址。
- 正文只通过 `textContent` 和新建节点渲染，只解析 Pixiv 自身的标记（`[newpage]`、`[[rb:]]`、`[[jumpuri:]]` 等，链接只放行 http/https），不解释备份正文中的 HTML。
- 系列先加载目录，超过 100 章时可继续加载后续目录页；只有点击某一章时才请求该章正文。
- 正文接口超时 30 秒，其余请求 15 秒；接口失败时在阅读层显示错误与重试，Pixiv 原页面保持不变。

0.12.2 交互与性能修复（不改版式，尚未完成本版设备验收）：正文分页首尾空行用线性扫描裁剪，避免大量空行卡顿；lookup 应答落到卡片前重新核对作品 ID，丢弃已被 SPA 复用卡片的旧应答；系列目录第一次加载失败也提供「重试」；列表页每 500ms 快速扫描仅限进入该页后的初始 15 秒。缺少可选 `GM_registerMenuCommand` / `GM_setValue` 时只在脚本内部降级，不往页面挂空函数；缺少后者时明确提示无法保存 Token。

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
| `preference_feedback.html`、`preference_feedback_scripts.html` | 偏好学习、小说详情、系列详情 | 按需读取版本化读后评价、保存 / 撤回及未知写入核对；普通阅读不自动请求偏好 API |

## Validation checklist

每页改动后检查：

- 页面能打开。
- Vue 能 mount。
- 导航高亮正确。
- 按钮仍调用原 API。
- 图片仍走 `/proxy/image?url=...`。
- loading/error/empty/success 状态可见。
- 移动端主导航可用。


## ai-writing 分支补充

以下页面与接口仅在 ai-writing 提供；上文共享书库能力与 main 保持同步。

### 成人配置（`/dashboard/settings/adult`）

模板为 `dashboard_settings_adult.html`，在此分支作为第五个设置页面提供。

成人 Agent 不走普通 Agent 的生命周期入口。设置页先选择项目，维护结构化的虚构角色（年龄、年龄依据、别名和 revision），再在 adult confirmation 中打开成人内容并勾选当前角色 revision。`safety` 与 `fact_guard` 两个固定 review binding 必须分别绑定支持 `json` 的 Provider 模型或模型池；binding、角色或确认 revision 变化后，阅读页会要求重新获取 Provider scope。

推荐配置顺序是 Provider 模型目录/模型池、`adult_polish` Agent、两个 JSON review binding、项目角色记录、成人确认。没有 `DASHBOARD_TOKEN` 时成人 API 即使来自 localhost 也返回 `403`；成人功能不是普通 Pipeline 的自动步骤。

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

### `/dashboard/novels/ai/<project_id>`

Template: `dashboard_ai_reader.html`

用途：显示 AI 作品封面、目录和章节正文，视觉与小说库详情页一致。阅读页的“成人润色”页签只在原始正文上通过 `v-text` 提供连续片段选择，不提交正文或上下文；选择后依次确认 Provider scope、成人 Agent 和已确认的虚构成年人角色。

成人润色交互顺序：

1. 在原始章节文本中选择连续范围；范围以 Unicode code point 计数，中文、emoji 和组合字符不能被拆开。
2. 调用 `POST /api/dashboard/ai/polish/adult/scope`，确认返回的完整 Provider/model 分组及 `provider_scope_hash`。
3. 调用 stream，观察 `metadata`、`progress`、两阶段 `validation`、`candidate` 和 `done`；若连接中断，使用 signed `/events` 恢复同一 job 的 validation/candidate/done 状态；warning 候选只有拿到 scoped `warning_ack_hash` 后才允许应用。
4. 仅对当前章节 revision、角色确认 revision、Provider scope 和校验 hash 都未变化的候选调用 apply。发生 `409` 时重新选择/确认并生成，不在前端重放旧正文。

候选展示只提供只读前后文、原片段、Unicode-safe diff 和校验 code；应用成功后重新加载章节，后端只替换目标区间。未应用候选由调度器按 `sync.task_log_retention_days` 清理，默认 14 天；手工清理不传天数时仍是 3 天。应用后不保留任务正文。

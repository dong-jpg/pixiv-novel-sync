# Frontend API Contract

> **契约更新（2026-10-08，main）**：本页只更新共享 API。增量分析保留未提供的画像字段，推荐区分查询/候选错误；已删除的旧路由不再列为可调用端点。最新验证及环境边界见 [项目现状](STATUS.md)。

> **移动优先补充（2026-10-09，main）**：登录新增可撤销的可选30天会话，首页/偏好统一使用已有分页接口。字段与失败响应见下文；合并代码不代表现网已经更新，见 [移动报告](archive/MOBILE_IMPLEMENTATION_REPORT_2026-10-08.md)。

> **审计分支补充（2026-10-30）**：只读救援 API 的三桶限流按本分支代码校准；待合并 / 部署，不代表当前线上配置已验证。版本与验收边界见 [项目现状](STATUS.md)。

本文档记录 Library OS 前端当前依赖的后端接口。后端重构时应优先保持路径、方法和主要字段兼容；如需调整，请在对接时同步更新前端适配层。

> AI 写作相关端点（创作项目 / 章节 / 草稿 / 文档 / 蒸馏档案 / Prompt 模板 / chat / 写作类 SSE stream / 成人润色）随 AI 写作模块移到 `ai-writing` 分支维护，main 分支契约不再记载。main 保留 AI 基础设施端点，另有手动作品分析业务；旧 AI job continue 仍不支持，不等于新的作品级恢复。分支实现、整阶段验收与部署分别见 [项目现状](STATUS.md)。

## 通用约定

- 页面仍由 Flask/Jinja 渲染，前端通过 Vue 3 CDN 增强交互。
- 导航仍使用服务端路由和普通 `<a>` 跳转，不是 SPA router。
- 图片资源统一通过 `GET /proxy/image?url=...` 代理加载。
- Core dashboard 接口存在 raw JSON 与 envelope 混用。
- AI 与偏好推荐接口主要使用 `{ ok: true, data: ... }` 与 `{ ok: false, error: string }`。

## 页面路由

| Route | Template | 说明 |
| --- | --- | --- |
| `/` | redirect/dashboard | 入口 |
| `/token-login` | `token_login.html` | Token / OAuth 授权 |
| `/dashboard` | `dashboard.html` | 控制台 |
| `/dashboard/follows` | `dashboard_follows.html` | 作者列表 |
| `/dashboard/novels` | `dashboard_novels.html` | 小说库 / 追更系列 / 拯救成功列表 |
| `/dashboard/novels/<novel_id>` | `dashboard_novel_detail.html` | 小说详情 / 阅读页 |
| `/dashboard/series/<series_id>` | `dashboard_series_detail.html` | 系列详情 |
| `/dashboard/users/<user_id>` | `dashboard_user_detail.html` | 作者详情 |
| `/dashboard/pending-deletions` | `dashboard_pending_deletions.html` | 待确认删除 |
| `/dashboard/logs` | `dashboard_logs.html` | 任务日志 |
| `/dashboard/settings` | 302 → `/dashboard/settings/sync` | 旧书签与旧 `#hash` 链接的落点 |
| `/dashboard/settings/sync` | `dashboard_settings_sync.html` | 同步与调度 |
| `/dashboard/settings/models` | `dashboard_settings_models.html` | 模型与 Provider |
| `/dashboard/settings/agents` | `dashboard_settings_agents.html` | Agent 绑定 |
| `/dashboard/settings/system` | `dashboard_settings_system.html` | 系统维护 |
| `/dashboard/preferences` | `dashboard_preferences.html` | 默认偏好画像与推荐；`view=work-analysis` 为独立作品分析视图 |

## 认证与健康检查 APIs

### GET/POST /api/auth/login

Used by: 浏览器登录流程（配置 `DASHBOARD_TOKEN` 时）。

- `GET`：返回独立 `login.html`，不加载受保护的公共框架或业务 API；未配置 `DASHBOARD_TOKEN` 时仍重定向到 `/`。
- `POST`：表单编码 `application/x-www-form-urlencoded`。`token` 为访问密码；只有 `remember_device=1` 才启用记住设备，默认不勾选；`next` 为经校验的站内返回路径。
- 未勾选：浏览器会话 Cookie，服务端最长 **7天**。勾选：Cookie 与服务端记录均固定 **30天**，普通访问和 CSRF 刷新不延长期限。浏览器的“恢复上次会话”可能保留会话 Cookie，但不能突破服务端期限。
- 成功时旋转随机标识并撤销本浏览器旧记录，302 返回安全的 `next`（默认 `/`）。拒绝外域、协议相对地址、反斜杠、控制字符及登录自循环。页面认证重定向保留路径和查询参数。
- 密码错误 `401`、限流 `429`、认证存储故障 `503` 均返回同一登录页内的错误提示，绝不回显密码。限流仍为同一客户端5分钟内5次失败，`429` 带 `Retry-After`。
- `web_auth_sessions` 只存随机标识 SHA-256、以 Flask secret 为密钥的口令版本 HMAC、创建/到期时间和持久标记；不存密码或原始随机标识。Cookie 使用签名、HttpOnly、SameSite=Lax；HTTPS 部署需 `PIXIV_COOKIE_SECURE=1` 或既有可信 HTTPS 代理配置。
- 生效访问密码改变后旧会话失效；稳定的 Flask secret/数据库允许未过期会话跨服务重启使用。旧版无服务端记录的 Cookie 需要重新登录一次。
- 认证存储不可用时受保护请求返回 `503` 并保留 Cookie 供恢复后重试，不能降级为放行、密码错误或成功退出。无口令的本机边界、CSRF、独立救援 Bearer 认证和 Referrer-Policy 保持不变。

### POST /api/auth/logout

桌面侧栏或手机「更多 → 退出登录」调用，仍需 CSRF。先撤销当前服务端会话记录，再清除 Cookie；旧 Cookie 无法重放，其他设备不受影响。仅 HTTP 成功且返回以下确认时，前端才跳转登录页；存储故障 `503` 必须保持当前页并显示可重试错误。

```json
{ "ok": true }
```

### GET /api/csrf-token

Used by: 所有需要携带 `X-CSRF-Token` 的写请求前端封装。响应：

```json
{ "csrf_token": "..." }
```

### GET /api/health

服务健康检查，无需认证前提以外的特殊参数。响应字段：

```json
{
  "status": "ok",
  "version": "x.y.z"
}
```

探活只回答进程是否在，不打开数据库，也不统计正在跑的任务。

## Shared shell APIs

### GET /api/dashboard/shell-data

Used by: `app-sidebar-nav`。

Response fields:

```json
{
  "pending_count": 3
}
```

### GET /api/dashboard/status

Used by: sidebar footer, dashboard home。

Expected fields:

```json
{
  "current_user": { "user_id": 123, "name": "作者名", "avatar_url": "https://..." },
  "stats": { "novels_count": 100, "users_count": 20, "series_count": 8, "pending_count": 1 },
  "latest_job": { "job_id": "...", "status": "running", "message": "...", "progress": {} },
  "series_sync_limit": 50
}
```

### GET /api/dashboard/auto-sync/status

Used by: sidebar footer, dashboard home。

Expected fields:

```json
{
  "running": true,
  "current_task_job_id": null,
  "current_task_name": null,
  "task_intervals": {},
  "task_crons": {},
  "task_last_run": {},
  "task_next_run": {},
  "task_priorities": {},
  "task_preemptible": {},
  "task_preempted_at": {}
}
```

有正在执行的定时任务时额外带 `current_job`（该 job 的状态快照）。

## Dashboard sync APIs

### GET /api/dashboard/auto-sync/budget

Used by: `/dashboard/settings/sync` 的调度表（耗时与每日预算两列）。

Query：`days`（观测窗口，夹到 1–30，默认 3）。

响应以调度任务名为键（与 `auto-sync/status` 同一套键）：

```json
{
  "ok": true,
  "days": 3,
  "timezone": "UTC",
  "day_seconds": 86400,
  "tasks": {
    "bookmarks": {
      "task_type": "bookmark",
      "label": "同步收藏小说",
      "enabled": true,
      "priority": 1,
      "preemptible": false,
      "cron": "20 0,4,8,12,16,20 * * *",
      "cron_valid": true,
      "interval_hours": 4,
      "schedule_source": "cron",
      "runs_per_day": 6,
      "runs": 12,
      "last_status": "succeeded",
      "last_started_at": "2026-08-28T00:20:00",
      "last_duration_seconds": 312.5,
      "avg_duration_seconds": 298.4,
      "observed_daily_seconds": 1193.6,
      "estimated_daily_seconds": 1875.0
    }
  },
  "total_estimated_daily_seconds": 4200.0,
  "total_duty_ratio": 0.048611
}
```

预算 = 单轮耗时 × 每天触发次数；次数按当前 cron 现算（cron 一改，历史累计时长立刻失真），cron 解析不了时按 `interval_hours` 折算——与调度器静默回落的行为一致，`schedule_source` 标出用的是哪一种。耗时优先取最近一轮实测值，没有终态记录时退回窗口均值。`total_duty_ratio` 是所有启用任务的每日预算除以一天：同步任务全局只有一个执行槽，这个比例就是那个槽的忙碌程度。前端不要用分页的 `/api/dashboard/logs` 自己聚合（20 条一页凑不齐一轮全部任务），也不要自己实现 cron 解析。

### POST /api/dashboard/sync/start

仅 API。页面用按任务类型启动的同步接口，这个全量入口留给脚本和测试。

Start full manual sync. Body may be `{}`.

### POST /api/dashboard/sync/subscribed-series

Body:

```json
{ "limit": 50 }
```

### GET /api/dashboard/sync/status

Query:

- `job_id` optional。

Expected response:

```json
{
  "job": {
    "job_id": "...",
    "status": "queued|running|cancel_requested|succeeded|failed|cancelled",
    "message": "...",
    "elapsed": 12,
    "task_list": ["..."],
    "current_task_index": 0,
    "progress": { "phase": "...", "current": 1, "total": 10, "current_novel": "..." },
    "logs": [{ "time": "...", "level": "info|success|warning|error", "message": "..." }]
  }
}
```

`job` 另含 `stats`、`error`、`started_at`、`finished_at`、`is_auto_sync`、`source`、`job_type`。没有任何任务时返回 `{ "job": null }`。这里是内存中的 `JobStatus`；`partial` 只出现在 `task_logs`（任务日志接口）里。

### POST /api/dashboard/auto-sync/toggle

Body:

```json
{ "enabled": true }
```

### POST /api/dashboard/auto-sync/stop-task

Stops the active auto-sync task. 成功返回 `{ "ok": true, "message": "正在停止当前任务" }`；没有正在执行的定时任务时同样是 HTTP 200，返回 `{ "ok": false, "message": "当前没有正在执行的定时任务" }`（字段是 `message`，不是 `error`）。

### POST /api/dashboard/sync/cancel

Used by: 首页「停止」按钮（手动任务；定时任务走上面的 `stop-task`）。`job_id` 可放在 query 或 JSON body，缺省取最新任务。成功返回 `{ "ok": true, "job_id": "...", "message": "已请求取消" }`；没有可取消的任务 `404`；任务不存在、已结束或正在收尾 `409`。

## Archive APIs

### GET /api/dashboard/follows

仅 API。关注页实际读取 `/api/dashboard/users`。

Used by: 无页面。保留给脚本。

Query params:

- `page`（页大小固定为 10，不接受 `page_size`）。

返回 `db.list_followed_users` 的分页 payload（关注作者列表及分页元数据）。

### GET /api/dashboard/novels

Used by: novels list。

Common query params:

- `page`
- `page_size`（默认 10，上限 100）
- `category=all|bookmark|following`（`following` 返回追更系列；救援列表走 `GET /api/dashboard/rescues`）
- `search`
- `sort=updated_desc|bookmarks_desc|views_desc`，留空为默认排序

`category` / `sort` 取值非法时静默回落到默认值，不返回 400。

Response should include items and pagination metadata. Current frontend accepts shapes with `items`, `total`, `pages` / `total_pages`.

### GET /api/dashboard/novels/{novel_id}

Used by: novel detail。

Expected detail fields include:

- `novel_id`
- `title`
- `caption`
- `text_raw`：正文。以 UTF-8 输出中文，不转成 `\uXXXX`
- `text_markdown`：只在 `text_raw` 为空时回（标题/作者/简介 + 同一份正文）；有 `text_raw` 时为 `null`，避免整本书传两遍
- `text_length`
- `user_id`
- `user_name`
- `series_id`
- `tags`
- `cover_url`
- `source_url`：Pixiv 原站地址（`https://www.pixiv.net/novel/show.php?id=<novel_id>`），由主键派生，前端不必自己拼。`series` 表同名列用 `https://www.pixiv.net/novel/series/<series_id>`。
- bookmark/view counts where available。
- `rescue`：实时救援评估、远端状态、完整度和人工纠错状态。

### GET /api/dashboard/novels/{novel_id}/progress

Used by: 小说阅读页阅读进度恢复。本机无记录时按服务端百分比恢复实际滚动位置；已有本机位置优先。异步读取期间用户主动滚动时不强行跳回旧位置。

无进度记录时返回默认值：

```json
{ "novel_id": 123, "progress": 0, "status": "unread" }
```

有记录时返回存储的进度对象（含 `progress`、`status` 等字段）。

### POST /api/dashboard/novels/{novel_id}/progress

保存阅读进度。Body：

```json
{ "progress": 42, "status": "unread|reading|completed" }
```

`progress` 会被裁剪到 0–100；`status` 非法时返回 `400 { "error": "invalid status" }`。成功返回 `{ "success": true }`。

### DELETE /api/dashboard/novels/{novel_id}/progress

删除阅读进度记录。成功返回 `{ "success": true }`。阅读页“重置阅读进度”在确认后调用；仅成功时清除本机记录并回到页首，失败保留当前位置。

### POST /api/dashboard/novels/export-epub

导出 EPUB。Body：

```json
{ "novel_ids": [123, 456] }
```

- `novel_ids` 缺失或不是数组时返回 `400 { "error": "novel_ids required" }`。
- 单本：直接返回 `application/epub+zip` 附件；小说不存在返回 `404`，正文缺失返回 `400`。
- 多本：最多处理前 50 个 ID（重复 ID 只导一次），跳过无正文条目，返回 `application/zip` 附件 `novels.zip`；条目名为 `{标题}_{novel_id}.epub`（无标题时为 `{novel_id}.epub`），同名作品不再互相覆盖。
- EPUB 内容：正文按 Pixiv 标记解析（`pixiv_markup.py`）。`[newpage]` 与 `[chapter:]` 各起一个 XHTML 文件并进目录（超过 500 个文件后，其余分页并入最后一个文件，页内锚点仍可跳转）；`[[rb:]]` 转 `<ruby><rt>`；`[[jumpuri:]]` 只保留 http(s) 链接，其余只留文字；`[jump:N]` 链到第 N 页所在文件；插图标记降级为文字说明（备份未收录插图与正文的对应关系）。语言按正文判断（ja / zh / ko / en）；封面按文件头识别 JPEG / PNG / GIF / WebP，认不出就不放封面。

### GET /api/dashboard/export/stats

导出同步统计数据。响应字段：

```json
{
  "total_novels": 100,
  "total_users": 20,
  "total_series": 8,
  "novels_by_status": { "active": 90 },
  "users_by_status": { "active": 18 },
  "recent_tasks": [
    {
      "id": 1, "task_type": "...", "task_name": "...", "job_id": "...",
      "status": "...", "is_auto_sync": false,
      "started_at": "...", "finished_at": "...", "error_message": null
    }
  ]
}
```

`recent_tasks` 为最近 10 条任务记录。查询失败返回 `500 { "error": "..." }`。

### GET /api/dashboard/series/{series_id}

Used by: novel detail and series detail。

Expected fields include series metadata and novels/chapters list.

响应额外包含 `rescue`，其中 `expected_count`、`local_count`、`complete_count` 用于展示系列备份覆盖率。

## 救援归档 API

救援管理接口沿用 Dashboard 会话认证。所有 `PUT`、`POST`、`DELETE` 请求必须携带 `X-CSRF-Token`；返回格式为 `{ ok, data, error }`。

### GET /api/dashboard/rescues

查询拯救成功列表。

查询参数：

- `page`、`page_size`
- `state=all|success|partial`
- `item_type=all|novel|series`
- `content_kind=all|series|series_chapter|standalone`
- `source_kind=all|bookmark|subscribed_series|following_user|user_backup`
- `search`
- `sort=checked_desc|updated_desc`

参数取值非法时返回 `400 { "ok": false, "error": "..." }`；救援目录首次重建完成前返回 `503`。

响应 `data` 除 `items` 与分页元数据外，还包含目录级 `stale` 布尔字段（依据 `refreshed_at` 与当前配置判定救援目录是否过期）。

列表项字段：`item_type`、`item_id`、`title`、`author_name`、`cover_url`、`rescue_state`、`remote_status`、`eligibility_reason`、`expected_count`、`local_count`、`complete_count`、`last_checked_at`、`updated_at`、`content_kind`、`content_kind_label`、`series_id`、`sources`、`personal_relation`。

`personal_relation` 是布尔值，表示该条目是否带有本人主动关联的来源（`bookmark` / `subscribed_series`）。`following_user`（原始来源 `following_user_scan`）与 `user_backup` 属于批量扫描，本人未必在乎，该字段为 `false`。字段由已取出的 `sources` 派生，不额外查库。

已进入「待确认删除」（`pending_deletions.status = 'pending'`）的条目不出现在本列表中：本人取消收藏 / 追更的作品以「等你决定」为准。`confirmed` 会真删本地备份，`restored` 表示用户明确保留，两者都不触发排除。人工 `include` 纠错优先于这条排除。

### PUT /api/dashboard/rescue-overrides/<item_type>/<item_id>

保存人工纠错。`item_type` 只能是 `novel` 或 `series`。

```json
{
  "action": "include|exclude",
  "note": "可选备注，最多 500 字"
}
```

`include` 表示人工确认 Pixiv 已失效，`exclude` 表示人工确认仍可访问。人工动作不能绕过正文完整性检查。

### DELETE /api/dashboard/rescue-overrides/<item_type>/<item_id>

删除人工纠错并恢复自动判断。

### GET /api/dashboard/rescue-token/status

仅返回 `configured`、`token_prefix`、`rotated_at`，不返回完整 Token 或摘要。

### POST /api/dashboard/rescue-token/rotate

生成新的独立救援 Token，旧 Token 立即失效。完整 Token 只在本次响应的 `data.token` 中出现一次；数据库只保存 SHA-256 摘要和前缀。

Token 状态与轮换响应均包含 `Cache-Control: no-store`，禁止浏览器或中间代理缓存。

## 只读救援 API

只读接口供 `userscripts/pixiv-rescue.user.js` 使用，与 `DASHBOARD_TOKEN` 完全隔离。

认证头：

```http
Authorization: Bearer <救援 Token>
```

Token 不接受 URL 查询参数或 Cookie。只允许 `GET` / `HEAD`。限流使用三个互相独立、按客户端 IP 计数的 60 秒滑动窗口桶：未认证或 Token 无效为 120 次；认证成功的正文、目录和 lookup 共用 120 次；认证成功的封面为 600 次。不按 Token 前缀分桶；先校验 Token 再选桶，认证错误只消耗未认证桶，超额返回 `429`（否则返回 `401`）。实际客户端 IP 是否正确取到，取决于可信代理与 Nginx real_ip 配置，部署状态见 [项目现状](STATUS.md)。

### GET /api/rescue/v1/novels/<novel_id>

返回可救援单篇或失效父系列中的完整章节。字段白名单包括：

- `novel_id`、`title`、`caption`、`user_id`、`author_name`、`series_id`
- `cover_url`、`tags`、`create_date`、`text_raw`
- `rescue_state`、`remote_status`、`eligibility_reason`
- `expected_count`、`local_count`、`complete_count`
- `last_checked_at`、`updated_at`、`personal_hold`、`source_notice`
- `total_bookmarks`、`total_views`（同步时的快照）、`x_restrict`、`is_original`、`ai_type`
- `series_nav`：所属系列可救援时为 `{series_id, series_title, chapter_number, chapter_total, prev, next}`（`prev`/`next` 为 `{novel_id, title, chapter_number}` 或 `null`），否则为 `null`。章节序号与目录接口同一口径。

### GET /api/rescue/v1/novels/<novel_id>/cover 与 GET /api/rescue/v1/series/<series_id>/cover

返回本地归档的封面图片，供用户脚本把失效卡片重建为带封面的卡片（系列本身不下载封面，取系列下第一篇有本地封面的小说）。没有本地封面时返回 `404`。

### GET /api/rescue/v1/series/<series_id>

返回系列元数据、救援状态和覆盖率（含 `personal_hold`），不在响应中展开章节正文。

### GET /api/rescue/v1/series/<series_id>/chapters

查询参数：`page`、`page_size`。返回有完整正文的章节目录（按发布时间排序）；目录项包含 `novel_id`、`title`、`chapter_number`、`create_date`、`remote_status`、`text_length`、`tags`、`total_bookmarks`、`total_views`、`x_restrict`、`is_original`、`ai_type`、`has_cover` 与 `api_path`，正文通过小说接口按需读取。分页之外另给全部已备份章节的 `total_text_length` 与 `last_create_date`（系列页头部「共N话 · 更新 · 字数 · 时长」用）。

### GET /api/rescue/v1/series

救援目录里的系列分页列表，供油猴脚本 0.12.0 的「拯救系列」tab（接管 Pixiv 手机站新作页的「好P友新作」）使用。查询参数：`page`、`page_size`（默认 24，上限 100；脚本每页取 10）、`state`、`search`、`sort`，取值与 `GET /api/dashboard/rescues` 相同。返回 `{items, page, total, total_pages}`。读的是救援目录快照：目录首建完成前回 `503`「救援目录尚未生成」，`pending` 待确认删除的系列不出现。

每项包含 `series_id`、`title`、`author_name`、`cover_url`、`rescue_state`、`expected_count`、`local_count`、`complete_count`、`personal_hold`、`last_checked_at`，以及下面的系列卡片字段。

### GET /api/rescue/v1/lookup

收藏 / 追更列表页按 ID 批量反查备份：`novel_ids`、`series_ids` 为逗号分隔的数字 ID，合计最多 40 个，两者都为空回 `400`。返回 `{novels, series}`，只含有备份且符合救援条件的条目，不返回正文；不受 `pending` 排除约束（同明细接口）。

- `novels` 每项：`novel_id`、`title`、`author_name`、`cover_url`、`series_id`、`rescue_state`、`personal_hold`、`has_cover`。
- `series` 每项：`series_id`、`title`、`author_name`、`cover_url`、`rescue_state`、`personal_hold`、`expected_count`，以及下面的系列卡片字段（追更列表空白框原位替换用）。

系列卡片字段（两个接口相同，照搬 Pixiv 追更列表的系列卡）：

- `user_id`；`chapter_count`：已备份章节数（`novel_texts.has_content = 1`，与章节目录同一口径）。
- `last_create_date`、`latest_novel_id`：已备份章节里发布时间最晚的一章（「某日更新」「阅读最新话」）。
- `x_restrict`：系列内各章的最大值（1 为 R-18，2 为 R-18G）；`has_cover`：与 `GET /api/rescue/v1/series/<series_id>/cover` 同一口径。
- `author_avatar_url`：作者头像，取自同步时保存的 `users.raw_json` 的 `profile_image_urls.medium`，只在以 `https://i.pximg.net/` 开头时给出，否则为 `null`；原始 JSON 不外传。

所有只读救援明细响应（小说、系列、章节目录）包含：

```json
{
  "source_notice": "内容来自私人备份，并非 Pixiv 官方恢复"
}
```

安全响应头：

- `Cache-Control: no-store`
- `X-Robots-Tag: noindex, nofollow, noarchive`
- `X-Content-Type-Options: nosniff`

状态码：

- `400`：查询参数非法（如 `lookup` 超过 40 个 ID 或两组 ID 都为空、`series` 列表的 `state` / `sort` 取值非法）。
- `401`：缺少或使用无效救援 Token。
- `404`：对象不存在、不符合救援条件或正文不完整。
- `405`：对只读接口使用写方法。
- `429`：超过限流阈值。
- `503`：救援目录尚未生成（新部署在调度器首轮重建目录前）。
- `500`：读取异常；响应只返回通用中文错误，不泄露数据库路径或内部异常。

### GET /api/dashboard/users

Used by: follows page。

Query params include status/page/search-style filters currently used by template.

### GET /api/dashboard/users/{user_id}

Used by: user detail。

### GET /api/dashboard/users/{user_id}/novels

Used by: user detail novel list。

### POST /api/dashboard/users/{user_id}/check

Checks author status.

### POST /api/dashboard/users/{user_id}/sync

Starts author sync.

## 数据删除 APIs

以下删除接口成功返回 `{ "ok": true, "message": "..." }`，失败返回 `500 { "error": "..." }`。

### DELETE /api/dashboard/novels/{novel_id}

删除小说记录并清理磁盘归档文件；响应额外包含 `archive_cleanup` 清理结果。

原 `DELETE /api/dashboard/users/{user_id}` 已移除：没有生产页面调用，且与后台备份同时运行时不能保证删除结果。T3-01 采用原计划允许的删接口方案，不新增批量用户删除按钮；存储层内部级联删除及其测试保留。

### DELETE /api/dashboard/series/{series_id}

删除系列（事务内删除并刷新受影响章节的救援目录状态）。

### DELETE /api/dashboard/bookmarks/{novel_id}

仅删除本地收藏记录，不删除小说本体、不取消 Pixiv 收藏；下一次同步可能重新加入。阅读页“移除本地收藏记录”经确认后调用，并显示失败或成功结果。

## Logs APIs

### GET /api/dashboard/logs

Used by: dashboard recent activity and task logs page。同步任务与 AI 创作任务默认保留 14 天（`sync.task_log_retention_days`，两张表共用）。

请求示例：`GET /api/dashboard/logs?category=sync|ai&task_type=&status=&days=1|3`，其中竖线表示二选一。

Query params:

- `page`
- `page_size`
- `category=sync|ai`
- `days`：整数，夹到 1–90，默认 3；任务日志页提供 1 / 3 / 7 / 14 天
- `task_type`
- `status`（AI 创作任务）
- `is_auto=true|false`（同步任务）

返回统一的 `items`、`page`、`page_size`、`total` 和 `total_pages`。每项包含任务标识、类型、名称、状态、开始/结束时间、`stats` 和 `category`；`category=ai` 时数据来自 `ai_jobs` 的只读投影，其他值按同步任务查询。同步任务列表**不含日志行**（`logs`），日志行只在详情接口返回；`stats_json` / `logs_json` 原串一律不外发。

### GET /api/dashboard/logs/{log_id}

返回单条任务日志，含解析后的 `stats` 与 `logs`（最多 500 行）。与列表接口一样以 UTF-8 输出中文，不转成 `\uXXXX`。

## Settings and cache APIs

### GET /api/dashboard/settings

Returns settings object consumed by settings form.

### POST /api/dashboard/settings

仅 API。Saves settings. Body is the edited settings object。全量端点仅为脚本/兼容调用保留；页面必须使用分区端点，不能将未加载字段写成默认值。

### PUT /api/dashboard/settings/<section>

Used by: `/dashboard/settings/sync`（`section=sync`）与 `/dashboard/settings/system`（`section=system`）。

按分区保存：只有该分区声明的字段会从 body 取值，其余字段沿用 `config/config.yaml` 里的既有值。字段白名单是 `web/managers.py:SETTINGS_SECTIONS`。设置页拆分后每页表单只含自己那一区，走全量端点会把没加载的字段写成默认值。

Body 是该分区的部分设置对象；夹带别区字段会被忽略而不是写入。响应：

```json
{ "ok": true, "message": "设置已保存", "sync": { "...": "保存后的完整 sync 配置" } }
```

未知 `section`（包括 `models` / `agents`，这两页的配置在数据库里、走 AI 配置接口）以及字段校验失败都返回 `400 { "ok": false, "error": "保存设置失败", "detail": "具体原因" }`，界面应展示 `detail`。变更类请求需带 `X-CSRF-Token`。

保存成功后，调度器只重算 cron、间隔或时区发生变化的任务的下次运行时间，其余任务保持原时刻。

### POST /api/dashboard/settings/cron-preview

Used by: `/dashboard/settings/sync` 的 cron 输入框（保存前校验）。

Body：

```json
{ "cron": "20 0,4,8,12,16,20 * * *", "timezone": "Asia/Seoul", "count": 5 }
```

`count` 夹到 1–10，默认 5；`timezone` 默认 `UTC`，未知时区按 UTC 处理（与 `cron_to_next_run` 一致）。响应：

```json
{
  "ok": true,
  "data": {
    "valid": true,
    "empty": false,
    "falls_back_to_interval": false,
    "next_runs": ["2026-08-28T00:20:00+09:00"],
    "runs_per_day": 6,
    "timezone": "Asia/Seoul",
    "timezone_valid": true,
    "effective_timezone": "Asia/Seoul"
  }
}
```

`timezone` 原样回显请求值；时区无效时 `timezone_valid: false`，预览按 `effective_timezone`（`UTC`）计算，而保存设置会因该时区被拒绝。

非法或超长（>200 字符）表达式返回 `200` 且 `valid: false`、`falls_back_to_interval: true`——这是校验结果而不是请求错误。空表达式额外带 `empty: true`。两种情况调度器都会静默回落到按 `*_interval_hours` 跑，界面必须把这个差别显示出来。

### GET /api/cache/status

Returns cache size/count status.

### POST /api/cache/clear

Clears cache. Mutation endpoint; UI should show confirmation/status.

### POST /api/dashboard/sync/{task_type}

Settings page task shortcuts。`task_type` 白名单与 `webapp.py` 的 `task_map` 一致，当前 11 项：

- `bookmark`
- `following_users`
- `following_novels`
- `subscribed_series`（页面按钮发下划线形式，专门的追更系列路由是连字符的 `/api/dashboard/sync/subscribed-series`；缺这个键会 400）
- `user_status`
- `novel_status`
- `series_status`
- `user_backup`
- `pending_deletion_detection`
- `preference_analyze`
- `recommendation_run`

未列出的 task_type 返回 `400` 与 `{ "error": "不支持的任务类型" }`。

## Pending deletion APIs

### GET /api/dashboard/pending-deletions

Query:

- `page`、`page_size`（默认 20）
- `item_type=novel|series`（其他值视为不过滤）。只返回 `status = 'pending'` 的记录，没有 status 参数。

侧栏待删除数量来自 `GET /api/dashboard/shell-data`。旧 `/api/dashboard/pending-deletions/count` 已从当前 main 工作区移除，不应新增调用。

### POST /api/dashboard/pending-deletions/detect

Starts detection job.

### POST /api/dashboard/pending-deletions/{deletion_id}/confirm

Confirms local deletion.

### POST /api/dashboard/pending-deletions/{deletion_id}/restore

Restores/keeps local archive.

## Preference and recommendation APIs

### GET /api/dashboard/preferences/profiles

### GET /api/dashboard/preferences/profiles/{profile_id}

### POST /api/dashboard/preferences/profiles/analyze

Body:

```json
{
  "name": "本地偏好画像",
  "description": "...",
  "scope": {},
  "is_default": true
}
```

增量分析不传 `name` / `description` 时保留已有值；仅显式提供字段才覆盖。前端不要每次发送固定默认名称覆盖用户命名。

### PUT /api/dashboard/preferences/profiles/{profile_id}

### POST /api/dashboard/preferences/profiles/{profile_id}/default

### DELETE /api/dashboard/preferences/profiles/{profile_id}

画像设默认和删除仅 API，保留既有脚本兼容接口；完整多画像页面工作流按 UNIFIED §1.3 为 OUT，不为消除旧计划空勾而新增该功能。

### POST /api/dashboard/recommendations/search-plan

Body:

```json
{ "profile_id": 1, "filters": {} }
```

### POST /api/dashboard/recommendations/run

推荐统计分别记录 `query_errors`（搜索请求错误）与 `candidate_errors`（单个候选异常），`errors` 保留总数；只有所有已尝试查询都失败才判定“全部搜索失败”，单个坏候选不会误判整轮失败。

旧 `GET /api/dashboard/recommendations/runs` 已从当前 main 工作区移除。页面不展示历史轮次；多画像的设默认和删除也不做页面。

### GET /api/dashboard/recommendations/items

Query:

- `status`
- `limit`
- `page`、`page_size`（可选）

响应外层保持 `{ok:true,data:...}`。不带 `page` 时 `data` 为平铺数组（按 `score DESC, updated_at DESC`，受 `limit` 截断），保留供旧调用方与 `recommendations.run` 使用；当前首页和偏好页均改用分页接口。

带 `page` 时返回分页信封 `{ items, page, page_size, total, total_pages }`，排序为 `run_id DESC, score DESC, id DESC`——最新一轮推书整体排最前、轮内按分数。排序键刻意用 `run_id`：同一本书被新一轮重推时 upsert 会刷新 `run_id` 与 `updated_at`，而 `created_at` 停在首次入库；反馈操作也会刷 `updated_at`，拿那两个排序会让老结果跳回第一页。`page_size` 夹到 1–50，越界 `page` 夹回最后一页。`page` 非数字返回 `400`。

两页固定每页10条；“隐藏已反馈”传 `status=new`，由服务端先过滤再计数/分页，不能先取一页再在客户端隐藏。取消勾选时不传 `status`，仍遵循后端默认排除 dismissed/muted 的规则，并非完整历史查询。过滤改变回第一页；前端拒绝迟到响应、采用服务端有效页。确认反馈或作者屏蔽后重新读取最新筛选/页码；屏蔽成功但后续反馈失败也必须刷新列表并明确提示部分失败，不自动重复 POST。

### POST /api/dashboard/recommendations/items/{item_id}/feedback

Body:

```json
{ "feedback_type": "interested|dismissed|saved|muted", "note": "..." }
```

### GET /api/dashboard/recommendations/mutes

### POST /api/dashboard/recommendations/mutes

Body:

```json
{ "mute_type": "author|tag", "mute_value": "...", "reason": "..." }
```

### DELETE /api/dashboard/recommendations/mutes/{mute_id}

## Work Analysis APIs

用于 `/dashboard/preferences?view=work-analysis`，一次只分析用户明确选择的已归档单篇/系列，不更新个人画像或推荐。源码入口：`work_analysis_web.py`、`work_analysis/service.py`、`storage/work_analysis/`。默认偏好页不调用这些接口。

### 路径与请求

| Method / Path | 请求 | `data` |
| --- | --- | --- |
| `POST /api/dashboard/work-analyses/preview` | 下列预览对象 | 预览 |
| `POST /api/dashboard/work-analyses` | 预览对象 + `preview_fingerprint`、`idempotency_key` | 任务详情 |
| `GET /api/dashboard/work-analyses` | 可选 `offset/limit` | `items,total,offset,limit` |
| `GET /api/dashboard/work-analyses/<run_id>` | 无 query | 任务详情，含删除墓碑 |
| `POST /api/dashboard/work-analyses/<run_id>/pause` | `expected_version` | 任务详情 |
| `POST /api/dashboard/work-analyses/<run_id>/cancel` | `expected_version` | 任务详情 |
| `POST /api/dashboard/work-analyses/<run_id>/resume` | `expected_version` | 任务详情 |
| `POST /api/dashboard/work-analyses/<run_id>/budget` | `expected_version,budget,budget_confirmed:true` | 任务详情 |
| `DELETE /api/dashboard/work-analyses/<run_id>` | `expected_version` | 删除墓碑详情 |
| `GET /api/dashboard/work-analyses/<run_id>/units` | 可选 `offset/limit` | `items,total,offset,limit` |
| `GET /api/dashboard/work-analyses/<run_id>/report` | 无 query | 报告 envelope |
| `GET /api/dashboard/work-analyses/<run_id>/evidence/<evidence_id>` | 无 query | `evidence,jump` |

集合路径另接受末尾 `/`。成功一律 HTTP 200（含创建、删除），返回 `{ok:true,data:...}`；端点失败返回 `{ok:false,error:<安全错误码>,code:<安全错误码>}`。认证/CSRF 中间件沿用共享错误形状，不保证有 `code`。

预览的五个字段必填且不接受额外字段；示例数字不是默认预算或支出授权：

```json
{
  "selection": {"source_kind":"novel", "source_id":123, "publication_state":"unknown"},
  "agent_id":1,
  "output_tokens":4000,
  "budget":{"request_limit":10,"token_limit":100000,"active_seconds_limit":600},
  "confirmations":{"order_confirmed":false,"current_archive_only":false,"external_send_confirmed":false,"budget_confirmed":false}
}
```

- `selection` 只接受必填 `source_kind=novel|series`、正整数 `source_id`，可选 `publication_state=unknown|ongoing|completed`（默认 unknown）和 `ordered_novel_ids`（1–2048 个不重复正整数）。单篇的显式列表必须只含自己的 ID。系列顺序须覆盖当前归档章清单，不能通过删 ID 隐藏缺章；chapter `position` 从 0 开始。
- `agent_id` 为正整数；`output_tokens` 为 1–1000000。预算必须完整给出 `request_limit` 1–512、`token_limit` 1–100000000、`active_seconds_limit` 30–14400，均不接受 bool/字符串/浮点数。
- 四个 confirmations 为严格 bool；预览返回 `confirmation_requirements`，创建须满足全部必需确认。当前归档不能证明远端清单完整，须确认 archived-only；系列/显式章序还需顺序确认。`can_create` 仅检查已知下限，不保证预算足以完成所有调用。
- `run_id/idempotency_key` 是小写 hex32；`preview_fingerprint/unit_id/evidence_id` 是小写 hex64。`expected_version` 是 1–2^63-1 的整数，公开控制严格 CAS，不自动改成新版本重发。
- 写请求 JSON 对象上限 256 KiB；拒绝重复键、非有限数、数组/null、未知/缺失字段和非 JSON Content-Type。写请求、详情、报告、证据不接受 query；仅列表/单元接受 `offset`（默认 0，0–1000000）、`limit`（默认 20，1–100），重复参数、前导零和额外参数拒绝。

### 预览、幂等与恢复

预览在本地读事务捕获清单和配置，正文仅在内存；不解密 Key、不调用 Provider、不创建 AI job。返回 `selection,manifest,plan,agent_id,agent_name,output_tokens,targets,budget,preview_fingerprint,can_create,confirmation_requirements,warnings`。每条 target 只含 `provider_id,provider_name,provider_model_id,model_key,display_name,pool_id,pool_name,pool_position,fallback_depth`，涵盖后续汇总可能使用的小模型；不返回 URL、prompt、密文或配置 fingerprint。`work_targets/work_can_segment` 仅为内部规划数据，不新增到全局预检 v2。

`preview_fingerprint` 绑定选择、语义源清单、计划、配置和预算，不包含 consent flag 或观测时间；同 hash 时间戳刷新不改变它。创建在事务内复核，过期预览返回 409。legacy 密文在真实调用时升级导致配置改变，也会在发送前停止并要求重新预览。

创建幂等 identity 是原始规范化请求（含 confirmations，不含幂等 key 本身），不会随之后预算增额改变。同 key/同请求在未删除时返回已有任务；已删除的原任务返回 410 `work_deleted`，不重新创建或复活。同 key/不同请求返回 `idempotency_conflict`。创建结果未知时只能显式重试同一原请求/key，不另建自动重试；原请求/key 只在当前页面内存保留，重载后先核对已有任务，不能据空表单自动新建。

状态为 `queued/running/pausing/paused/cancelling/cancelled/failed/invalidated/succeeded`；暂停、取消、同步让位的 `reason` 分开。`resume` 只把仍有效且可恢复的任务排队，复用已验证业务检查点和累计预算，不重放旧 `/ai/jobs/<job_id>/continue`。预算只允许确认后的增额，不重置计数、不自动恢复。重启不自动重发付费调用；配置/源版本变化不得扩展到未经确认的新目标。删除清理业务产物；列表不再列出，详情仍可读墓碑，重复删除返回墓碑现状。

### 安全投影与证据

任务详情包含身份/时间、`version,status,reason,deleted`、源信息/标题/`manifest`、`manifest_fingerprint`、`report_stale/archive_outdated`、`budget`、`request_count/planned_tokens/active_seconds_debited`、`usage_summary,coverage,unit_counts,can_resume,has_report`；集合 items 省略 manifest。单元页返回类型/状态、范围和子单元数量、实际安全目标、用量等元数据，不返回 artifact 或原始 prompt。共享 job 参数仅 `run_id`，进度/日志仅安全 ID、计数与状态，不复制正文、引文、结论或报告；通用 AI 调用保持 `metadata_only`，不得用关键词清洗的 `keyword_compat` 保存本业务。

报告 `schema_version=1` 包含 `run_id,manifest,coverage,artifact,units,usage_summary,created_at,published_at,report_stale,archive_outdated,synthesis_complete`；每个持久化单元 artifact 最多 128 KiB，完整报告 envelope 最多 1 MiB，超限拒绝而非截断。单条引文最多 240 Unicode code point、每结论最多 4 条、最终最多 128 个结论；不保存完整输入重放副本，有界短证据可能与极短章节重合。输入覆盖不等于原作完整或模型解释正确；未知用量不补零。

证据返回存储的 `evidence` 和 `jump:{novel_id,text_hash,start,end,context_start,context_text}`。服务器以正文 hash 和精确唯一子串核对；`start/end` 为 Unicode code point 左闭右开偏移，context 最多前后各 128 code point，不由浏览器 UTF-16 下标推算。已发布报告可带 `report_stale` 返回；源改变/删除后证据定位拒绝 410，不能拿旧偏移高亮新正文。同 hash 元数据刷新不旧版化报告，新增系列章只标 `archive_outdated`，不混入冻结清单。

报告和证据默认折叠、文本节点渲染，不进 URL、localStorage、公开救援/FTS 或普通调用历史。删除撤权后旧 worker 不得复活业务产物；应用删除不承诺备份/WAL 物理清除。所有路径复用 Dashboard 认证；包括预览在内的写请求须 `X-CSRF-Token`。成功和拒绝均带 `Cache-Control: private, no-store, no-cache, max-age=0, must-revalidate`、`Pragma: no-cache`、`Expires: 0`。

### 状态码

| HTTP | 典型错误 / 边界 |
| --- | --- |
| 400 | strict JSON/字段/ID/query 校验、`confirmation_required`；非 JSON 也是 400，不是 415 |
| 401 / 403 | 认证 / CSRF 拒绝，沿用共享中间件 |
| 404 | `not_found` |
| 409 | `stale_preview/preview_conflict/idempotency_conflict/version_conflict/state_conflict/finalizing/configuration_changed/budget_exhausted/report_not_ready`；`synthesis_capacity_exceeded` 也为 409 |
| 410 | `work_deleted`，或证据源 `source_deleted/source_changed` |
| 413 | `payload_too_large/input_too_large/too_large` |
| 503 | `admission_unavailable/control_unavailable` |
| 500 | 非预期错误统一 `work_analysis_unavailable`，不返回异常正文 |

## AI configuration APIs

所有 AI 配置接口返回 `{ ok, data }` 或 `{ ok, error }`。写接口沿用 Dashboard 会话认证，并必须携带 `X-CSRF-Token`。Provider 响应只返回 `has_api_key`，不得返回 API Key 或密文。

### Provider、模型目录与同步 operation

- `GET /api/dashboard/ai/health?days=7`：AI 配置的只读健康投影，零网络。
- `GET /api/dashboard/ai/providers`
- `POST /api/dashboard/ai/providers`
- `PUT /api/dashboard/ai/providers/{provider_id}`
- `DELETE /api/dashboard/ai/providers/{provider_id}`
- `POST /api/dashboard/ai/providers/{provider_id}/test`
- `POST /api/dashboard/ai/providers/probe-models`：预览上游模型列表，不落库、不建 Provider。
- `GET /api/dashboard/ai/providers/<provider_id>/models?search=&routable_only=&enabled_only=`
- `POST /api/dashboard/ai/providers/<provider_id>/models`：创建人工模型。
- `PUT /api/dashboard/ai/provider-models/<model_id>`
- `DELETE /api/dashboard/ai/provider-models/<model_id>`
- `POST /api/dashboard/ai/providers/<provider_id>/models/sync`：返回 `202` 和 operation。
- `GET /api/dashboard/ai/model-sync-operations/<operation_id>`
- `GET /api/dashboard/ai/model-sync-operations/<operation_id>/events`：SSE 事件仅为 `started`、`page`、`empty_confirmation_required`、`completed`、`failed`、`cancelled`。
- `DELETE /api/dashboard/ai/model-sync-operations/<operation_id>`：请求取消。
- `POST /api/dashboard/ai/model-sync-operations/<operation_id>/confirm-empty`

目录响应包含 `total`、`discovered_available`、`routable`、`models_synced_at`、`models_sync_error` 和模型的有效显示名、能力、上下文窗口及 `source`。人工字段不会被后续同步覆盖。同步失败、取消、超时或分页不完整时保留旧目录。非权威空结果进入 `needs_empty_confirmation`，确认请求必须原样提交 operation 返回的版本信息：

```json
{
  "generation": 2,
  "result_digest": "64 位小写十六进制摘要"
}
```

发现阶段限制为单页 4 MiB、累计 20 MiB、100 页、5000 个模型和 10 分钟；超限时不写入部分目录。

### 模型池与 Agent 绑定

- `GET /api/dashboard/ai/model-pools`
- `POST /api/dashboard/ai/model-pools`
- `GET /api/dashboard/ai/model-pools/<pool_id>`
- `PUT /api/dashboard/ai/model-pools/<pool_id>`
- `DELETE /api/dashboard/ai/model-pools/<pool_id>`
- `PUT /api/dashboard/ai/model-pools/<pool_id>/members`
- `GET /api/dashboard/ai/model-pools/<pool_id>/attempts?limit=50`
- `GET /api/dashboard/ai/agents`
- `POST /api/dashboard/ai/agents`
- `PUT /api/dashboard/ai/agents/{agent_id}`
- `DELETE /api/dashboard/ai/agents/{agent_id}`
- `GET /api/dashboard/ai/agents/<agent_id>/candidates`
- `PUT /api/dashboard/ai/agents/bindings`：批量改绑 / 批量启停。单事务。
- `POST /api/dashboard/ai/agents/seed`

成员替换是全量、有序写入，body 为 `{"expected_version": 3, "members": [{"provider_model_id": 10, "enabled": true}]}`；陈旧版本返回 `409`。后备池按链顺序展开并按 `(provider_id, model_key)` 去重。Agent 的 `binding_type=fixed|pool` 互斥：`fixed` 提交 `provider_id`/`model`，`pool` 提交 `model_pool_id`。`required_capabilities` 只接受 `streaming`、`json`、`vision`、`tools`、`long_context`。

`GET /api/dashboard/ai/model-pools/<pool_id>/attempts?limit=50` 返回该池最近的真实尝试记录（跨 job，按 `started_at` 倒序，`limit` 夹到 1–200）。字段是重命名过的投影，不是 job 详情里的 `*_snapshot`：

```json
[
  {
    "job_id": "...", "attempt_index": 0, "pool_id": 3, "pool_version": 5,
    "pool_position": 1, "pool_name": "主池", "provider_id": 2,
    "provider_model_id": 10, "provider_name": "deepseek", "model_key": "deepseek-v3",
    "stage": "main", "status": "failed", "error_scope": "provider",
    "error_message": "...", "error_category": "provider_error",
    "finish_reason": "error", "output_started": false,
    "started_at": "...", "finished_at": "...", "latency_ms": 1234
  }
]
```

`status` 取 `running` / `succeeded` / `failed` / `partial` / `cancelled`。`partial` 与 `failed` 必须分开显示：`partial` 是已经开始输出正文之后才失败，路由不会再转移到下一个候选（否则正文重复），`output_started=true` 就是这条约束的依据。

`GET /api/dashboard/ai/agents/<agent_id>/candidates` 是只读预览：按真实路由顺序解析该 Agent 的候选模型链，**不发起任何生成请求**，也不回传 Provider 的连接地址、密钥或配置哈希。

```json
{
  "ok": true,
  "data": {
    "agent_id": 4, "agent_name": "关键词清洗", "task_type": "keyword_clean",
    "binding_type": "pool", "pool_id": 3, "pool_name": "主池",
    "candidates": [
      {
        "order": 1, "provider_id": 2, "provider_name": "deepseek",
        "model_key": "deepseek-v3", "provider_model_id": 10,
        "pool_id": 3, "pool_name": "主池", "pool_position": 1,
        "fallback_depth": 0, "source": "主池",
        "capabilities": ["streaming"], "context_window": 64000
      }
    ],
    "limits": {
      "max_candidate_attempts": 16, "max_network_requests": 32,
      "max_resolved_candidates": 64, "max_pool_nodes": 8
    }
  }
}
```

`source` 是 `pool_name` 或固定绑定的 `"fixed"`；`fallback_depth` 为 0 表示主池成员，`N` 表示第 N 级后备池。固定绑定的链只有一个元素且 `pool_id` 为 `null`，界面不应显示模型池区块。`limits` 直接来自 `ai/model_router.py` 的常量，前端必须显示响应值而不是自己抄一份数字。未知 agent 返回 `400`。

单池和完整后备链最多 64 个候选，链深度最多 8；每个 job 最多尝试 16 个候选、32 次网络请求和 30 分钟。模型池可能把同一 Prompt 发送给多个 Provider，前端必须展示完整 Provider 范围及跨 Provider 隐私提示。

## AI job APIs

### POST /api/dashboard/ai/agents/<agent_id>/preflight

2026-11-03 第一阶段：认证与 CSRF 规则不变，JSON body 为完整 `messages`（1..256 条、role 仅 system/user/assistant、content 仅文本），可显式提供 `temperature`、`top_p`、`max_tokens`、`allowed_provider_ids`。服务端不注入 Agent system prompt；UI 可将显示的已保存提示词包含一次。请求体上限 16 MiB（UTF-8 JSON 字节），不截断文本。未知字段/类型或数值错误为 400，过大为 413，任务不存在为 404；有效但无合格候选为 200 / `can_run=false`。预检不触发 Provider/Key 解密、同步或 job 写入。

成功 `data` 含 `version:2`、`planner_version`、`estimator_version`、`can_run`、`allowed_provider_ids`、`output_reserve`、`eligible`、`excluded`、`limits`，可附 `excluded_count/excluded_truncated`。逐候选行含原始 `source_index`、可空冻结 `candidate_index`、Provider/模型/池身份、有效窗口及 Agent/Provider/模型窗口、可空 `max_output_tokens`、`limiting_factors`、输入估计/预算、输出预留、消息开销、安全余量、估算器来源/exact、安全 reason/reason_message。未知为 null，前端不得重算预算、汇总最小窗口或把排除行当成 attempt。

### PUT /api/dashboard/ai/providers/<provider_id>/models/enabled

原子启停 body 为 `{"catalog_revision":"64位小写SHA-256", "changes":[{"id":41,"enabled":true}]}`，最多 5,000 行；不写入目录发现事实。revision 对完整配置投影计算，与搜索/分页无关，不是数据库自增列。陈旧 revision 返回 409，保持草稿，需重读完整目录并显式确认；校验失败 400，缺失资源 404。probe 结果不能直接当成已保存目录；保存 Provider 后需确认同步终态，再用完整目录 revision 应用初始选择。

main 分支只保留 AI job 的读取、清理与手动续接端点；写作端点（documents / drafts / detect-ai-tells / prompt-templates / series/search / 蒸馏档案 style-profiles & novel-profiles）随 AI 写作模块移到 `ai-writing` 分支。

- `GET /api/dashboard/ai/jobs`
- `GET /api/dashboard/ai/jobs/{job_id}`
- `POST /api/dashboard/ai/jobs/cleanup`（仅 API，调度器直接清过期任务）

### GET /api/dashboard/ai/jobs/<job_id>

返回 AI 任务详情与原有身份/时间/attempt/route_summary 字段，增加 `validation_status`（历史为 unknown）、`can_continue:false`、`continue_unavailable_reason`、`usage_summary`。usage 包含可空 `known_input_tokens/known_output_tokens/known_total_tokens`、`reported_requests/unreported_requests`、`complete`；服务器汇总是事实，前端不累加 attempt。零报告 token 与未知不同；零请求数显示未发出请求。

`snapshot_version` 为 1/2 或 null；v1 沿用 `prompt_budget`，v2 用服务端安全解码的 `invocation_plan`（与预检同一逐候选投影），不可用时提供 `plan_unavailable_reason`。UI 不解码私有 candidate_snapshot、不显示 hash/指纹或 metadata_only 审计输入。仅旧关键词清洗保留有限输入/输出视图。任务不存在为 404。

### POST /api/dashboard/ai/jobs/<job_id>/cancel

给仍在 `running` 的任务置 `cancel_requested` 标记（独立布尔列，`status` 仍是 `running`），成功返回 `data: {job_id, cancel_requested: true}`。路由心跳读到后停止，任务终态变为 `cancelled`。任务不存在返回 404，已经结束返回 409。重复请求同一次取消仍返回成功。

### POST /api/dashboard/ai/jobs/<job_id>/continue

main 第一阶段没有支持的业务重放处理器；此端点保留历史请求校验，但当前任务类型返回 409“任务类型暂不支持”，不创建 child job、不调用 Provider。详情 `can_continue=false`，UI 允许列表为空，按钮与动作入口均禁止继续。新 [作品级恢复](#work-analysis-apis) 使用独立 `/work-analyses/<run_id>/resume`，不改变本端点。以下是兼容请求形状，不表示已提供业务续写：

```json
{
  "parent_job_id": "与路径 job_id 完全一致",
  "idempotency_key": "16-128 位可打印 ASCII",
  "candidate_snapshot_hash": "64 位小写十六进制摘要",
  "resume_candidate_index": 2
}
```

底层仍保留原快照/hash/配置版本校验；无 version 历史快照按 v1，不用当前配置重建。`partial` 表示任务不完整；metadata_only 正文仅通过内存结果返回，不自动换模型，main 也没有可执行的手工续写入口。

## AI SSE stream contract

以下为共享生成层保留的 SSE 事件契约，不表示 main 的 continue API 可执行：main continue 当前返回上述 409。模型目录同步另有既存 operation events SSE。写作类 stream 端点随 AI 写作模块移到 `ai-writing` 分支。

Required event names:

| Event | Payload |
| --- | --- |
| `delta` | `{ "text": "..." }` |
| `progress` | arbitrary progress object |
| `metadata` | metadata object |
| `done` | terminal success payload |
| `error` | `{ "message": "..." }` or equivalent |

Frontend expects streams to terminate with `done` or `error`.

## Token/OAuth APIs

当前 main 工作区已删除 `/oauth/start`、`/oauth/callback`、`/oauth/sync-callback/{task_id}`。OAuth 任务统一从 `POST /api/token-jobs` 创建；工作区修改不表示远端旧版本或生产部署已更新。

- `GET /api/token-config`
- `POST /api/token-jobs`
- `GET /api/token-jobs/{job_id}`
- `POST /api/save-token`
- `GET /oauth/task/{task_id}`
- `POST /oauth/exchange/{task_id}`
- `POST /oauth/save/{task_id}`

## Backend refactor notes

1. 保持 `ok/error` envelope 一致会降低前端分支判断复杂度。
2. 分页字段建议统一为 `items,total,page,page_size,total_pages`。
3. 长任务建议统一 job shape：`job_id,status,message,progress,logs,started_at,finished_at`。
4. AI SSE 必须保留 `delta/progress/metadata/done/error`。
5. 图片仍需 `GET /proxy/image?url=...`，除非前端和后端共同迁移到新资源代理策略。

# Frontend API Contract

本文档记录 Library OS 前端当前依赖的后端接口。后端重构时应优先保持路径、方法和主要字段兼容；如需调整，请在对接时同步更新前端适配层。

> AI 写作相关端点（创作项目 / 章节 / 草稿 / 文档 / 蒸馏档案 / Prompt 模板 / chat / 写作类 SSE stream / 成人润色）随 AI 写作模块移到 `ai-writing` 分支维护，main 分支契约不再记载；main 只保留 Provider / 模型目录 / 模型同步 / 模型池 / Agent 绑定 / AI job 读取与续接等基础设施端点。

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
| `/dashboard/preferences` | `dashboard_preferences.html` | 偏好画像与推荐 |

## 认证与健康检查 APIs

### GET/POST /api/auth/login

Used by: 浏览器登录流程（配置 `DASHBOARD_TOKEN` 时）。

- `GET`：返回内置登录表单 HTML；未配置 `DASHBOARD_TOKEN` 时直接重定向到 `/`。
- `POST`：表单字段 `token`（`application/x-www-form-urlencoded`）。校验成功后写入会话并重定向到 `/`；失败返回 `401` 纯文本「密码错误」。同一客户端 5 分钟内失败 5 次后返回 `429` 与 `{ "error": "too many login attempts" }`。

### POST /api/auth/logout

清除登录会话。响应：

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
  "version": "x.y.z",
  "uptime_seconds": 123.45,
  "db_accessible": true,
  "running_jobs": 0
}
```

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
  "task_intervals": {},
  "task_crons": {},
  "task_last_run": {},
  "task_next_run": {}
}
```

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

Start full manual sync. Body may be `{}`.

### POST /api/dashboard/check-bookmarks

Start bookmark pre-check job. Body may be `{}`.

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
    "status": "running|succeeded|failed",
    "message": "...",
    "elapsed": 12,
    "task_list": ["..."],
    "current_task_index": 0,
    "progress": { "phase": "...", "current": 1, "total": 10, "current_novel": "..." },
    "logs": [{ "time": "...", "level": "info|success|warning|error", "message": "..." }]
  }
}
```

### POST /api/dashboard/auto-sync/toggle

Body:

```json
{ "enabled": true }
```

### POST /api/dashboard/auto-sync/stop-task

Stops the active auto-sync task.

## Archive APIs

### GET /api/dashboard/follows

Used by: follows page（`/dashboard/follows`）。

Query params:

- `page`（页大小固定为 10，不接受 `page_size`）。

返回 `db.list_followed_users` 的分页 payload（关注作者列表及分页元数据）。

### GET /api/dashboard/novels

Used by: novels list, AI search widgets。

Common query params:

- `page`
- `page_size`
- `category`
- `search`
- `sort`

Response should include items and pagination metadata. Current frontend accepts shapes with `items`, `total`, `pages` / `total_pages`.

### GET /api/dashboard/novels/{novel_id}

Used by: novel detail。

Expected detail fields include:

- `novel_id`
- `title`
- `caption`
- `text`
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

Used by: 小说阅读页阅读进度恢复。

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

删除阅读进度记录。成功返回 `{ "success": true }`。

### POST /api/dashboard/novels/export-epub

导出 EPUB。Body：

```json
{ "novel_ids": [123, 456] }
```

- `novel_ids` 缺失或不是数组时返回 `400 { "error": "novel_ids required" }`。
- 单本：直接返回 `application/epub+zip` 附件；小说不存在返回 `404`，正文缺失返回 `400`。
- 多本：最多处理前 50 本，跳过无正文条目，返回 `application/zip` 附件 `novels.zip`。

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

参数取值非法时返回 `400 { "ok": false, "error": "..." }`。

响应 `data` 除 `items` 与分页元数据外，还包含目录级 `stale` 布尔字段（依据 `refreshed_at` 与当前配置判定救援目录是否过期）。

列表项字段：`item_type`、`item_id`、`title`、`author_name`、`cover_url`、`rescue_state`、`remote_status`、`eligibility_reason`、`expected_count`、`local_count`、`complete_count`、`last_checked_at`、`updated_at`、`content_kind`、`content_kind_label`、`series_id`、`sources`、`personal_relation`。

`personal_relation` 是布尔值，表示该条目是否带有本人主动关联的来源（`bookmark` / `subscribed_series`）。`following_user_scan` 与 `user_backup` 属于批量扫描，本人未必在乎，该字段为 `false`。字段由已取出的 `sources` 派生，不额外查库。

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

Token 不接受 URL 查询参数或 Cookie。只允许 `GET` / `HEAD`；每个来源地址与 Token 组合每分钟最多 120 次请求。

### GET /api/rescue/v1/novels/<novel_id>

返回可救援单篇或失效父系列中的完整章节。字段白名单包括：

- `novel_id`、`title`、`caption`、`user_id`、`author_name`、`series_id`
- `cover_url`、`tags`、`create_date`、`text_raw`
- `rescue_state`、`remote_status`、`eligibility_reason`
- `expected_count`、`local_count`、`complete_count`
- `last_checked_at`、`updated_at`、`source_notice`

### GET /api/rescue/v1/series/<series_id>

返回系列元数据、救援状态和覆盖率，不在响应中展开章节正文。

### GET /api/rescue/v1/series/<series_id>/chapters

查询参数：`page`、`page_size`。返回有完整正文的章节目录；目录项只包含章节元数据与 `api_path`，正文通过小说接口按需读取。

所有只读救援响应包含：

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

- `401`：缺少或使用无效救援 Token。
- `404`：对象不存在、不符合救援条件或正文不完整。
- `405`：对只读接口使用写方法。
- `429`：超过限流阈值。
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

### DELETE /api/dashboard/users/{user_id}

删除用户及其所有小说与归档文件；响应额外包含 `archive_cleanup`。

### DELETE /api/dashboard/series/{series_id}

删除系列（事务内删除并刷新受影响章节的救援目录状态）。

### DELETE /api/dashboard/bookmarks/{novel_id}

仅删除收藏记录，不删除小说本体。

## Logs APIs

### GET /api/dashboard/logs

Used by: dashboard recent activity and task logs page。同步任务与 AI 创作任务默认保留 14 天（`sync.task_log_retention_days`，两张表共用）。

请求示例：`GET /api/dashboard/logs?category=sync|ai&task_type=&status=&days=1|3`，其中竖线表示二选一。

Query params:

- `page`
- `page_size`
- `category=sync|ai`
- `days=1|3`
- `task_type`
- `status`（AI 创作任务）
- `is_auto=true|false`（同步任务）

返回统一的 `items`、`page`、`page_size`、`total` 和 `total_pages`。每项包含任务标识、类型、名称、状态、开始/结束时间和 `category`；`category=ai` 时数据来自 `ai_jobs` 的只读投影，其他值按同步任务查询。

### GET /api/dashboard/logs/{log_id}

Returns detailed log payload.

## Settings and cache APIs

### GET /api/dashboard/settings

Returns settings object consumed by settings form.

### POST /api/dashboard/settings

Saves settings. Body is the edited settings object。全量端点，与分区端点并存。

### PUT /api/dashboard/settings/<section>

Used by: `/dashboard/settings/sync`（`section=sync`）与 `/dashboard/settings/system`（`section=system`）。

按分区保存：只有该分区声明的字段会从 body 取值，其余字段沿用 `config/config.yaml` 里的既有值。字段白名单是 `web/managers.py:SETTINGS_SECTIONS`。设置页拆分后每页表单只含自己那一区，走全量端点会把没加载的字段写成默认值。

Body 是该分区的部分设置对象；夹带别区字段会被忽略而不是写入。响应：

```json
{ "ok": true, "message": "设置已保存", "sync": { "...": "保存后的完整 sync 配置" } }
```

未知 `section` 返回 `400`。变更类请求需带 `X-CSRF-Token`。

### POST /api/dashboard/settings/reload

Reloads settings from backend config source.

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
    "timezone": "Asia/Seoul"
  }
}
```

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

- `page`
- type/status filters where available。

### GET /api/dashboard/pending-deletions/count

Sidebar/count use if needed.

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

### PUT /api/dashboard/preferences/profiles/{profile_id}

### POST /api/dashboard/preferences/profiles/{profile_id}/default

### DELETE /api/dashboard/preferences/profiles/{profile_id}

### POST /api/dashboard/recommendations/search-plan

Body:

```json
{ "profile_id": 1, "filters": {} }
```

### POST /api/dashboard/recommendations/run

### GET /api/dashboard/recommendations/runs

### GET /api/dashboard/recommendations/items

Query:

- `status`
- `limit`
- `page`、`page_size`（可选）

不带 `page` 时返回平铺数组（按 `score DESC, updated_at DESC`，受 `limit` 截断），偏好页与 `recommendations.run` 的返回值依赖这个旧形状。

带 `page` 时返回分页信封 `{ items, page, page_size, total, total_pages }`，排序为 `run_id DESC, score DESC, id DESC`——最新一轮推书整体排最前、轮内按分数。排序键刻意用 `run_id`：同一本书被新一轮重推时 upsert 会刷新 `run_id` 与 `updated_at`，而 `created_at` 停在首次入库；反馈操作也会刷 `updated_at`，拿那两个排序会让老结果跳回第一页。`page_size` 夹到 1–50，越界 `page` 夹回最后一页。`page` 非数字返回 `400`。

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

`source` 是 `pool_name` 或固定绑定的 `"fixed"`；`fallback_depth` 为 0 表示主池成员，`N` 表示第 N 级后备池。固定绑定的链只有一个元素且 `pool_id` 为 `null`，界面不应显示模型池区块。`limits` 直接来自 `ai/model_pools.py` 的常量，前端必须显示响应值而不是自己抄一份数字。未知 agent 返回 `400`。

单池和完整后备链最多 64 个候选，链深度最多 8；每个 job 最多尝试 16 个候选、32 次网络请求和 30 分钟。模型池可能把同一 Prompt 发送给多个 Provider，前端必须展示完整 Provider 范围及跨 Provider 隐私提示。

## AI job APIs

main 分支只保留 AI job 的读取、清理与手动续接端点；写作端点（documents / drafts / detect-ai-tells / prompt-templates / series/search / 蒸馏档案 style-profiles & novel-profiles）随 AI 写作模块移到 `ai-writing` 分支。

- `GET /api/dashboard/ai/jobs`
- `GET /api/dashboard/ai/jobs/{job_id}`
- `POST /api/dashboard/ai/jobs/cleanup`

### GET /api/dashboard/ai/jobs/<job_id>

返回 AI 任务完整详情，包括 `job_id`、`task_type`、`status`、`input`、`output_text`、`output`、`error_message`、`candidate_snapshot_hash`、`candidate_snapshot`、`prompt_budget`、`attempts`、`route_summary` 和时间字段。attempt 包含实际 Provider/模型、池快照、stage、状态、错误分类及耗时；快照和 attempt 不含 API Key、Prompt、正文或完整请求/响应。任务不存在时返回 404。

### POST /api/dashboard/ai/jobs/<job_id>/continue

从终态父 job 的不可变候选快照创建 child job，并从第一个未尝试候选继续。接口返回常规 AI SSE；重复 `idempotency_key` 复用同一 child job，不重复调用 Provider。

```json
{
  "parent_job_id": "与路径 job_id 完全一致",
  "idempotency_key": "16-128 位可打印 ASCII",
  "candidate_snapshot_hash": "64 位小写十六进制摘要",
  "resume_candidate_index": 2
}
```

索引不是首个未尝试候选，或 Agent/池版本、剩余 Provider 配置已变化时返回 `409`。`partial` 表示正文已保留但任务不完整；不会自动切换模型，必须由用户显式执行“下一个模型继续”。

## AI SSE stream contract

main 分支上唯一返回 `text/event-stream` 的 AI 端点是 `POST /api/dashboard/ai/jobs/<job_id>/continue`（手动「下一个模型继续」）。写作类 stream 端点（continue / rewrite / distill / audit / plan / longform / chapters / pipeline / chat）随 AI 写作模块移到 `ai-writing` 分支。

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

- `GET /api/token-config`
- `POST /api/token-jobs`
- `GET /api/token-jobs/{job_id}`
- `POST /api/save-token`
- `POST /oauth/start`
- `GET /oauth/task/{task_id}`
- `GET /oauth/callback`
- `POST /oauth/sync-callback/{task_id}`
- `POST /oauth/exchange/{task_id}`
- `POST /oauth/save/{task_id}`

## Backend refactor notes

1. 保持 `ok/error` envelope 一致会降低前端分支判断复杂度。
2. 分页字段建议统一为 `items,total,page,page_size,total_pages`。
3. 长任务建议统一 job shape：`job_id,status,message,progress,logs,started_at,finished_at`。
4. AI SSE 必须保留 `delta/progress/metadata/done/error`。
5. 图片仍需 `GET /proxy/image?url=...`，除非前端和后端共同迁移到新资源代理策略。

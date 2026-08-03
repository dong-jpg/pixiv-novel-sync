# Frontend API Contract

本文档记录 Library OS 前端当前依赖的后端接口。后端重构时应优先保持路径、方法和主要字段兼容；如需调整，请在对接时同步更新前端适配层。

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
| `/dashboard/novels` | `dashboard_novels.html` | 小说库 / 追更系列 / AI 创作 / 拯救成功列表 |
| `/dashboard/novels/<novel_id>` | `dashboard_novel_detail.html` | 小说详情 / 阅读页 |
| `/dashboard/series/<series_id>` | `dashboard_series_detail.html` | 系列详情 |
| `/dashboard/users/<user_id>` | `dashboard_user_detail.html` | 作者详情 |
| `/dashboard/pending-deletions` | `dashboard_pending_deletions.html` | 待确认删除 |
| `/dashboard/logs` | `dashboard_logs.html` | 任务日志 |
| `/dashboard/settings` | `dashboard_settings.html` | 设置 |
| `/dashboard/preferences` | `dashboard_preferences.html` | 偏好画像与推荐 |
| `/dashboard/ai` | `dashboard_ai.html` | AI 自动写作 |
| `/dashboard/wizard` | `dashboard_wizard.html` | 创作向导与蒸馏档案 |
| `/dashboard/novels/ai/<project_id>` | `dashboard_ai_reader.html` | AI 创作小说阅读 |

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
- bookmark/view counts where available。
- `rescue`：实时救援评估、远端状态、完整度和人工纠错状态。

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
- `search`
- `sort=checked_desc|updated_desc`

列表项字段：`item_type`、`item_id`、`title`、`author_name`、`cover_url`、`rescue_state`、`remote_status`、`eligibility_reason`、`expected_count`、`local_count`、`complete_count`、`last_checked_at`、`updated_at`。

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

## Logs APIs

### GET /api/dashboard/logs

Used by: dashboard recent activity and task logs page。同步任务与 AI 创作任务默认保留 3 天。

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

Saves settings. Body is the edited settings object.

### POST /api/dashboard/settings/reload

Reloads settings from backend config source.

### GET /api/cache/status

Returns cache size/count status.

### POST /api/cache/clear

Clears cache. Mutation endpoint; UI should show confirmation/status.

### POST /api/dashboard/sync/{task_type}

Settings page task shortcuts. Current task types include:

- `bookmark`
- `following_users`
- `following_novels`
- `user_status`
- `novel_status`
- `series_status`

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

- `GET /api/dashboard/ai/providers`
- `POST /api/dashboard/ai/providers`
- `PUT /api/dashboard/ai/providers/{provider_id}`
- `DELETE /api/dashboard/ai/providers/{provider_id}`
- `POST /api/dashboard/ai/providers/{provider_id}/test`
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
- `POST /api/dashboard/ai/agents/seed`

成员替换是全量、有序写入，body 为 `{"expected_version": 3, "members": [{"provider_model_id": 10, "enabled": true}]}`；陈旧版本返回 `409`。后备池按链顺序展开并按 `(provider_id, model_key)` 去重。Agent 的 `binding_type=fixed|pool` 互斥：`fixed` 提交 `provider_id`/`model`，`pool` 提交 `model_pool_id`。`required_capabilities` 只接受 `streaming`、`json`、`vision`、`tools`、`long_context`。

单池和完整后备链最多 64 个候选，链深度最多 8；每个 job 最多尝试 16 个候选、32 次网络请求和 30 分钟。模型池可能把同一 Prompt 发送给多个 Provider，前端必须展示完整 Provider 范围及跨 Provider 隐私提示。

## AI content and job APIs

- `POST /api/dashboard/ai/documents/upload`
- `POST /api/dashboard/ai/documents/manual`
- `GET /api/dashboard/ai/drafts`
- `POST /api/dashboard/ai/drafts`
- `PUT /api/dashboard/ai/drafts/{draft_id}`
- `DELETE /api/dashboard/ai/drafts/{draft_id}`
- `GET /api/dashboard/ai/drafts/{draft_id}/history`
- `POST /api/dashboard/ai/drafts/{draft_id}/fork`
- `GET /api/dashboard/ai/jobs`
- `GET /api/dashboard/ai/jobs/{job_id}`
- `POST /api/dashboard/ai/jobs/cleanup`
- `POST /api/dashboard/ai/detect-ai-tells`
- `GET /api/dashboard/ai/prompt-templates`
- `GET /api/dashboard/ai/prompt-templates/{template_id}`
- `POST /api/dashboard/ai/prompt-templates`
- `PUT /api/dashboard/ai/prompt-templates/{template_id}`
- `DELETE /api/dashboard/ai/prompt-templates/{template_id}`
- `POST /api/dashboard/ai/prompt-templates/seed`
- `GET /api/dashboard/ai/series/search`

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

The following endpoints return `text/event-stream`:

- `POST /api/dashboard/ai/continue/stream`
- `POST /api/dashboard/ai/rewrite/stream`
- `POST /api/dashboard/ai/distill/style/stream`
- `POST /api/dashboard/ai/distill/novel/stream`
- `POST /api/dashboard/ai/audit/stream`
- `POST /api/dashboard/ai/plan/stream`
- `POST /api/dashboard/ai/projects/{project_id}/longform-plan/stream`
- `POST /api/dashboard/ai/projects/{project_id}/longform-plan/details/stream`
- `POST /api/dashboard/ai/chapters/continue/stream`
- `POST /api/dashboard/ai/projects/{project_id}/states/auto-update/stream`
- `POST /api/dashboard/ai/chat/stream`
- `POST /api/dashboard/ai/chapters/pipeline/stream`
- `POST /api/dashboard/ai/chapters/pipeline/batch/stream`
- `POST /api/dashboard/ai/chapters/extract-summary/stream`
- `POST /api/dashboard/ai/chapters/polish/stream`
- `POST /api/dashboard/ai/projects/{project_id}/foreshadows/auto-resolve/stream`

Required event names:

| Event | Payload |
| --- | --- |
| `delta` | `{ "text": "..." }` |
| `progress` | arbitrary progress object |
| `metadata` | metadata object |
| `done` | terminal success payload |
| `error` | `{ "message": "..." }` or equivalent |
| custom | backend-specific event name and payload |

Frontend expects streams to terminate with `done` or `error`.

## AI longform project APIs

- `GET /api/dashboard/ai/projects`
- `GET /api/dashboard/ai/projects/{project_id}`
- `POST /api/dashboard/ai/projects`
- `PUT /api/dashboard/ai/projects/{project_id}`
- `DELETE /api/dashboard/ai/projects/{project_id}`
- `POST /api/dashboard/ai/projects/<project_id>/cover`
- `GET /api/dashboard/ai/projects/<project_id>/cover`
- `DELETE /api/dashboard/ai/projects/<project_id>/cover`
- `GET /api/dashboard/ai/projects/{project_id}/reader`
- `GET /api/dashboard/ai/projects/{project_id}/download`
- `GET /api/dashboard/ai/projects/{project_id}/chapters`
- `GET /api/dashboard/ai/chapters/{chapter_id}`
- `POST /api/dashboard/ai/chapters`
- `PUT /api/dashboard/ai/chapters/{chapter_id}`
- `DELETE /api/dashboard/ai/chapters/{chapter_id}`
- `POST /api/dashboard/ai/projects/{project_id}/chapters/batch`
- `GET /api/dashboard/ai/projects/{project_id}/states`
- `PUT /api/dashboard/ai/projects/{project_id}/states/{state_type}`
- `GET /api/dashboard/ai/projects/{project_id}/foreshadows`
- `POST /api/dashboard/ai/foreshadows`
- `PUT /api/dashboard/ai/foreshadows/{foreshadow_id}`
- `DELETE /api/dashboard/ai/foreshadows/{foreshadow_id}`
- `POST /api/dashboard/ai/projects/{project_id}/chapters/{chapter_id}/index`
- `GET /api/dashboard/ai/projects/{project_id}/search`
- `GET /api/dashboard/ai/chapters/{chapter_id}/dashboard`

封面上传使用 `multipart/form-data` 的 `cover` 字段，支持 JPEG、PNG、WebP，最大 10 MiB；成功返回 `cover_url`。文件类型、扩展名或文件头不一致返回 400，项目或封面不存在返回 404。读取接口直接返回图片内容，删除成功返回 `cover_url: null`。

## AI chat/session APIs

- `GET /api/dashboard/ai/chat/sessions`
- `POST /api/dashboard/ai/chat/sessions`
- `GET /api/dashboard/ai/chat/sessions/{session_id}`
- `PUT /api/dashboard/ai/chat/sessions/{session_id}`
- `DELETE /api/dashboard/ai/chat/sessions/{session_id}`
- `GET /api/dashboard/ai/chat/sessions/{session_id}/preview`
- `POST /api/dashboard/ai/chat/sessions/{session_id}/import-to-project`
- `POST /api/dashboard/ai/chat/sessions/{session_id}/import-raw-to-project`

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

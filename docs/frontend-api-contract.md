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
| `/dashboard/novels` | `dashboard_novels.html` | 小说库 / 追更系列列表 |
| `/dashboard/novels/<novel_id>` | `dashboard_novel_detail.html` | 小说详情 / 阅读页 |
| `/dashboard/series/<series_id>` | `dashboard_series_detail.html` | 系列详情 |
| `/dashboard/users/<user_id>` | `dashboard_user_detail.html` | 作者详情 |
| `/dashboard/pending-deletions` | `dashboard_pending_deletions.html` | 待确认删除 |
| `/dashboard/logs` | `dashboard_logs.html` | 同步日志 |
| `/dashboard/settings` | `dashboard_settings.html` | 设置 |
| `/dashboard/preferences` | `dashboard_preferences.html` | 偏好画像与推荐 |
| `/dashboard/ai` | `dashboard_ai.html` | AI 创作 |

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

### GET /api/dashboard/series/{series_id}

Used by: novel detail and series detail。

Expected fields include series metadata and novels/chapters list.

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

Used by: dashboard recent activity and logs page。

Query params:

- `page`
- `page_size`
- `days`
- `task_type`
- `source`

Expected response supports `items` or `logs`, plus pagination metadata.

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

All AI configuration APIs generally return `{ ok, data }` or `{ ok, error }`.

- `GET /api/dashboard/ai/providers`
- `POST /api/dashboard/ai/providers`
- `PUT /api/dashboard/ai/providers/{provider_id}`
- `DELETE /api/dashboard/ai/providers/{provider_id}`
- `POST /api/dashboard/ai/providers/{provider_id}/test`
- `GET /api/dashboard/ai/agents`
- `POST /api/dashboard/ai/agents`
- `PUT /api/dashboard/ai/agents/{agent_id}`
- `DELETE /api/dashboard/ai/agents/{agent_id}`
- `POST /api/dashboard/ai/agents/seed`

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

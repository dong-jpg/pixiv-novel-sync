# Frontend Pages

本文档记录 Library OS 前端页面、模板、主要接口与交互。前端重写保持 Flask/Jinja 页面路由不变。

## 页面总览

| Route | Template | 页面用途 | Library OS 状态 |
| --- | --- | --- | --- |
| `/token-login` | `src/pixiv_novel_sync/templates/token_login.html` | Token/OAuth 授权 | 待独立视觉适配 |
| `/dashboard` | `src/pixiv_novel_sync/templates/dashboard.html` | 同步控制台、统计、任务状态 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/follows` | `src/pixiv_novel_sync/templates/dashboard_follows.html` | 关注作者列表 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/novels` | `src/pixiv_novel_sync/templates/dashboard_novels.html` | 小说库和追更系列列表 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/novels/<id>` | `src/pixiv_novel_sync/templates/dashboard_novel_detail.html` | 小说详情和阅读页 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/series/<id>` | `src/pixiv_novel_sync/templates/dashboard_series_detail.html` | 系列详情 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/users/<id>` | `src/pixiv_novel_sync/templates/dashboard_user_detail.html` | 作者详情和作者小说 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/pending-deletions` | `src/pixiv_novel_sync/templates/dashboard_pending_deletions.html` | 待确认删除队列 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/logs` | `src/pixiv_novel_sync/templates/dashboard_logs.html` | 同步任务与 AI 创作任务日志 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/settings` | `src/pixiv_novel_sync/templates/dashboard_settings.html` | 同步、缓存、AI provider/agent 设置 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/preferences` | `src/pixiv_novel_sync/templates/dashboard_preferences.html` | 偏好画像与推荐 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/ai` | `src/pixiv_novel_sync/templates/dashboard_ai.html` | AI 自动写作项目、章节和 Pipeline | 已接入 `library-page` / `library-page-header` |
| `/dashboard/wizard` | `src/pixiv_novel_sync/templates/dashboard_wizard.html` | 创作向导与蒸馏档案 | 已接入 `library-page` / `library-page-header` |
| `/dashboard/novels?category=ai` | `src/pixiv_novel_sync/templates/dashboard_novels.html` | AI 创作小说库 | 已接入 `library-page` / `library-page-header` |
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

用途：任务日志列表和详情弹窗。任务类型分为“同步任务”和“AI 创作任务”，默认保留最近 3 天；AI 任务支持类型、状态和时间筛选。

APIs:

- `GET /api/dashboard/logs`
- `GET /api/dashboard/logs/{log_id}`

### `/dashboard/settings`

Template: `dashboard_settings.html`

用途：同步设置、缓存管理、AI provider/agent 管理。

APIs:

- `GET /api/dashboard/settings`
- `POST /api/dashboard/settings`
- `POST /api/dashboard/settings/reload`
- `GET /api/cache/status`
- `POST /api/cache/clear`
- `POST /api/dashboard/sync/{task_type}`
- AI provider/agent APIs，详见 `frontend-api-contract.md`。

### `/dashboard/preferences`

Template: `dashboard_preferences.html`

用途：偏好画像、推荐搜索计划、推荐反馈、屏蔽管理。

APIs:

- Preference profile APIs。
- Recommendation APIs。

### `/dashboard/ai`

Template: `dashboard_ai.html`

用途：AI 自动写作项目、全书规划、章节工作区、伏笔、状态记忆、语义检索和 Pipeline。

关键约束：

- 不初始化创作向导会话或蒸馏表单。
- `/dashboard/ai?project_id=<id>` 可直接打开指定项目。
- 流式写请求统一附加 CSRF Token。

### `/dashboard/wizard`

Template: `dashboard_wizard.html`

用途：创作向导会话、素材导入、READY 项目导入和蒸馏档案管理。蒸馏来源支持手动文本、归档小说、归档系列和文档。

### `/dashboard/novels?category=ai`

Template: `dashboard_novels.html`

用途：按小说库卡片样式展示 AI 创作小说，复用项目封面并进入统一阅读页。

### `/dashboard/novels/ai/<project_id>`

Template: `dashboard_ai_reader.html`

用途：显示 AI 作品封面、目录和章节正文，视觉与小说库详情页一致。

### `/token-login`

Template: `token_login.html`

用途：保存 refresh token 或走 OAuth 登录任务。

APIs:

- `GET /api/token-config`
- `POST /api/token-jobs`
- `GET /api/token-jobs/{job_id}`
- `POST /api/save-token`
- OAuth APIs。

## Validation checklist

每页改动后检查：

- 页面能打开。
- Vue 能 mount。
- 导航高亮正确。
- 按钮仍调用原 API。
- 图片仍走 `/proxy/image?url=...`。
- loading/error/empty/success 状态可见。
- 移动端主导航可用。

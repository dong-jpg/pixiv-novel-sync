# Pixiv Novel Sync - 完整 API 文档

> [!WARNING]
> **历史快照，不是当前事实来源。** 本文档保留 2026-06-16 的接口记录，端点数量、字段和版本可能已经变化。当前前端依赖请查阅 [frontend-api-contract.md](frontend-api-contract.md)，最终行为以代码为准。

**版本**: v0.2.0  
**更新日期**: 2026-06-16  
**端点总数**: 71

---

## 📋 目录

1. [认证与会话](#1-认证与会话)
2. [仪表盘与状态](#2-仪表盘与状态)
3. [同步任务](#3-同步任务)
4. [小说管理](#4-小说管理)
5. [用户管理](#5-用户管理)
6. [系列管理](#6-系列管理)
7. [任务日志](#7-任务日志)
8. [设置管理](#8-设置管理)
9. [待删除管理](#9-待删除管理)
10. [AI 创作](#10-ai-创作)
11. [偏好与推荐](#11-偏好与推荐)
12. [缓存管理](#12-缓存管理)
13. [OAuth 与 Token](#13-oauth-与-token)
14. [健康检查](#14-健康检查)

---

## 通用说明

### 认证方式

- **Dashboard 访问**: 需要 `DASHBOARD_TOKEN`（在 `.env` 中配置）
- **API 认证**: Session Cookie（通过 `/api/auth/login` 登录）
- **CSRF 保护**: POST/PUT/DELETE 请求需要 `X-CSRF-Token` 头

### 统一响应格式

#### 成功响应
```json
{
  "ok": true,
  "...": "其余字段随接口而定（message / job / sync 等）"
}
```
> 绝大多数接口已统一为 `{ok: true, ...}`。个别旧接口（如阅读进度 POST/DELETE
> `/api/dashboard/novels/<id>/progress`）仍返回 `{"success": true}`，后续会继续统一。

#### 错误响应
```json
{
  "ok": false,
  "error": "错误消息",
  "detail": "可选的详细原因"
}
```

### 分页参数

大多数列表接口支持分页：
- `page`: 页码（从 1 开始，默认 1）
- `page_size`: 每页数量（默认 20，最大 100）

### 排序参数

支持的排序字段：
- `updated_desc`: 按更新时间降序
- `updated_asc`: 按更新时间升序
- `bookmarks_desc`: 按收藏数降序
- `views_desc`: 按浏览数降序

---

## 1. 认证与会话

### 1.1 登录
**POST** `/api/auth/login`

请求体：
```json
{
  "token": "your_dashboard_token"
}
```

响应：
- 成功：HTTP 302 重定向到 `/`（设置 session）
- 密码错误：HTTP 401，纯文本 `密码错误`
- 触发限流（5 分钟内失败 ≥5 次）：HTTP 429，`{"error": "too many login attempts"}`

---

### 1.2 登出
**POST** `/api/auth/logout`

响应：
```json
{
  "ok": true
}
```

---

### 1.3 获取 CSRF Token
**GET** `/api/csrf-token`

响应：
```json
{
  "csrf_token": "..."
}
```

---

## 2. 仪表盘与状态

### 2.1 获取仪表盘状态
**GET** `/api/dashboard/status`

响应：
```json
{
  "user_id": 12345,
  "sync_enabled": true,
  "initial_manual_only": false,
  "bookmark_restricts": ["public", "private"],
  "bookmark_restricts_label": "公开+私密",
  "max_items_per_run": 50,
  "max_pages_per_run": 5,
  "delay_seconds_between_items": 1.0,
  "delay_seconds_between_pages": 1.0,
  "series_sync_limit": 0,
  "latest_job": {
    "job_id": "...",
    "status": "succeeded",
    "message": "同步完成"
  },
  "stats": {
    "total_novels": 1234,
    "total_users": 56,
    "total_series": 12
  }
}
```

---

### 2.2 获取全局 Shell 数据
**GET** `/api/dashboard/shell-data`

响应：
```json
{
  "pending_count": 5
}
```

---

### 2.3 导出统计数据
**GET** `/api/dashboard/export/stats`

响应：
```json
{
  "exported_at": "2026-06-16T10:30:00Z",
  "novels": {
    "total": 1234,
    "by_restrict": {
      "public": 800,
      "private": 434
    },
    "by_series": {
      "single": 1000,
      "series": 234
    }
  },
  "users": {
    "total": 56,
    "by_status": {
      "normal": 50,
      "no_novels": 4,
      "suspended": 2
    }
  }
}
```

---

## 3. 同步任务

### 3.1 获取同步状态
**GET** `/api/dashboard/sync/status`

查询参数：
- `job_id`（可选）: 指定任务 ID

响应：
```json
{
  "job": {
    "job_id": "1718519400000_abc123",
    "status": "running",
    "message": "正在同步...",
    "started_at": 1718519400.0,
    "finished_at": null,
    "task_list": ["同步收藏"],
    "stats": {
      "users": 10,
      "novels": 100,
      "assets_downloaded": 50
    },
    "logs": [
      "[2026-06-16 10:30:00] 开始同步",
      "[2026-06-16 10:30:05] 已同步 10 本小说"
    ]
  }
}
```

---

### 3.2 开始手动同步
**POST** `/api/dashboard/sync/start`

请求体：
```json
{
  "task_list": [
    "sync_bookmarks",
    "sync_following_novels"
  ]
}
```

响应：
```json
{
  "ok": true,
  "job_id": "1718519400000_abc123"
}
```

---

### 3.3 同步收藏（快捷）
**POST** `/api/dashboard/sync/bookmarks`

响应：同 3.2

---

### 3.4 同步关注用户小说
**POST** `/api/dashboard/sync/following-novels`

响应：同 3.2

---

### 3.5 同步追更系列
**POST** `/api/dashboard/sync/subscribed-series`

请求体（可选）：
```json
{
  "limit": 10,
  "enable_precheck": true
}
```

响应：同 3.2

---

### 3.6 检查收藏状态
**POST** `/api/dashboard/check-bookmarks`

响应：同 3.2

---

### 3.7 获取定时同步状态
**GET** `/api/dashboard/auto-sync/status`

响应：
```json
{
  "enabled": true,
  "tasks": [
    {
      "name": "auto_sync_bookmarks",
      "label": "同步收藏",
      "enabled": true,
      "cron": "0 */6 * * *",
      "next_run_at": "2026-06-16T16:00:00Z"
    }
  ]
}
```

---

### 3.8 切换定时同步
**POST** `/api/dashboard/auto-sync/toggle`

请求体：
```json
{
  "enabled": true
}
```

响应：
```json
{
  "ok": true,
  "enabled": true
}
```

---

### 3.9 停止定时任务
**POST** `/api/dashboard/auto-sync/stop-task`

请求体：
```json
{
  "task_name": "auto_sync_bookmarks"
}
```

响应：
```json
{
  "ok": true
}
```

---

## 4. 小说管理

### 4.1 获取小说列表
**GET** `/api/dashboard/novels`

查询参数：
- `page`: 页码（默认 1）
- `page_size`: 每页数量（默认 20，最大 100）
- `category`: 分类（`all`/`bookmark`/`following`/`series`/`single`）
- `search`: 搜索关键词（全文搜索）
- `sort`: 排序方式（`updated_desc`/`bookmarks_desc`/`views_desc`）

响应：
```json
{
  "items": [
    {
      "novel_id": 123456,
      "title": "小说标题",
      "user_id": 789,
      "user_name": "作者名",
      "series_id": null,
      "series_title": null,
      "restrict": "public",
      "is_original": true,
      "total_bookmarks": 1234,
      "total_view": 5678,
      "text_length": 12000,
      "page_count": 1,
      "create_date": "2025-01-01T00:00:00+00:00",
      "last_seen_at": "2026-06-16T10:00:00Z",
      "tags": ["タグ1", "タグ2"],
      "caption": "小说简介..."
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1234,
  "total_pages": 62,
  "empty_message": null
}
```

---

### 4.2 获取小说详情
**GET** `/api/dashboard/novels/<novel_id>`

响应：
```json
{
  "novel_id": 123456,
  "title": "小说标题",
  "user_id": 789,
  "user_name": "作者名",
  "caption": "简介...",
  "tags": ["タグ1", "タグ2"],
  "text_length": 12000,
  "total_bookmarks": 1234,
  "total_view": 5678,
  "create_date": "2025-01-01T00:00:00+00:00",
  "last_seen_at": "2026-06-16T10:00:00Z",
  "text_preview": "正文前 500 字...",
  "sources": [
    {
      "source_type": "bookmark_public",
      "discovered_at": "2026-06-16T10:00:00Z"
    }
  ]
}
```

---

### 4.3 删除小说
**DELETE** `/api/dashboard/novels/<novel_id>`

响应：
```json
{
  "ok": true
}
```

---

### 4.4 导出 EPUB
**POST** `/api/dashboard/novels/export-epub`

请求体：
```json
{
  "novel_ids": [123456, 234567]
}
```

响应：二进制 EPUB 文件

---

### 4.5 获取阅读进度
**GET** `/api/dashboard/novels/<novel_id>/progress`

响应：
```json
{
  "novel_id": 123456,
  "position": 5000,
  "total_length": 12000,
  "progress_percent": 41.67,
  "last_read_at": "2026-06-16T10:00:00Z"
}
```

---

### 4.6 保存阅读进度
**POST** `/api/dashboard/novels/<novel_id>/progress`

请求体：
```json
{
  "position": 5000
}
```

响应：
```json
{
  "ok": true
}
```

---

### 4.7 删除阅读进度
**DELETE** `/api/dashboard/novels/<novel_id>/progress`

响应：
```json
{
  "ok": true
}
```

---

## 5. 用户管理

### 5.1 获取关注用户列表
**GET** `/api/dashboard/follows`

查询参数：
- `page`: 页码
- `page_size`: 每页数量
- `status`: 状态筛选（`all`/`normal`/`no_novels`/`suspended`）

响应：
```json
{
  "items": [
    {
      "user_id": 789,
      "name": "作者名",
      "account": "username",
      "profile_image_url": "https://...",
      "status": "normal",
      "novel_count": 25,
      "last_checked_at": "2026-06-16T10:00:00Z"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 56,
  "total_pages": 3
}
```

---

### 5.2 获取用户详情
**GET** `/api/dashboard/users/<user_id>`

响应：
```json
{
  "user_id": 789,
  "name": "作者名",
  "account": "username",
  "profile_image_url": "https://...",
  "comment": "个人简介...",
  "status": "normal",
  "novel_count": 25,
  "last_checked_at": "2026-06-16T10:00:00Z"
}
```

---

### 5.3 获取用户的小说列表
**GET** `/api/dashboard/users/<user_id>/novels`

查询参数：
- `page`: 页码
- `tab`: 分类（`all`/`single`/`series`）

响应：格式同 4.1

---

### 5.4 检查用户状态
**POST** `/api/dashboard/users/<user_id>/check`

响应：
```json
{
  "ok": true,
  "status": "normal",
  "novel_count": 25
}
```

---

### 5.5 备份用户全部小说
**POST** `/api/dashboard/users/<user_id>/sync`

响应：
```json
{
  "ok": true,
  "job_id": "1718519400000_abc123"
}
```

---

### 5.6 删除用户
**DELETE** `/api/dashboard/users/<user_id>`

响应：
```json
{
  "ok": true
}
```

---

## 6. 系列管理

### 6.1 获取系列详情
**GET** `/api/dashboard/series/<series_id>`

响应：
```json
{
  "series_id": 456789,
  "title": "系列标题",
  "user_id": 789,
  "user_name": "作者名",
  "caption": "系列简介...",
  "cover_image_url": "https://...",
  "total_character_count": 120000,
  "content_count": 12,
  "is_original": true,
  "novels": [
    {
      "novel_id": 123456,
      "title": "第1章",
      "series_order": 1,
      "text_length": 10000,
      "create_date": "2025-01-01T00:00:00+00:00"
    }
  ]
}
```

---

### 6.2 删除系列
**DELETE** `/api/dashboard/series/<series_id>`

响应：
```json
{
  "ok": true
}
```

---

## 7. 任务日志

### 7.1 获取日志列表
**GET** `/api/dashboard/logs`

查询参数：
- `page`: 页码
- `page_size`: 每页数量
- `task_type`: 任务类型筛选
- `source`: 来源筛选（`manual`/`auto`）
- `status`: 状态筛选（`running`/`succeeded`/`failed`）

响应：
```json
{
  "items": [
    {
      "log_id": 1,
      "task_type": "sync_bookmarks",
      "task_label": "同步收藏",
      "source": "manual",
      "status": "succeeded",
      "message": "同步完成",
      "stats": {
        "novels": 100,
        "users": 10
      },
      "started_at": "2026-06-16T10:00:00Z",
      "finished_at": "2026-06-16T10:05:00Z",
      "duration_seconds": 300
    }
  ],
  "page": 1,
  "total": 50,
  "total_pages": 3
}
```

---

### 7.2 获取日志详情
**GET** `/api/dashboard/logs/<log_id>`

响应：
```json
{
  "log_id": 1,
  "task_type": "sync_bookmarks",
  "logs": [
    "[2026-06-16 10:00:00] 开始同步",
    "[2026-06-16 10:00:05] 已同步 10 本小说",
    "[2026-06-16 10:05:00] 同步完成"
  ],
  "stats": { ... },
  "started_at": "...",
  "finished_at": "..."
}
```

---

## 8. 设置管理

### 8.1 获取设置
**GET** `/api/dashboard/settings`

响应：
```json
{
  "sync": {
    "download_assets": true,
    "write_markdown": true,
    "max_items_per_run": 50,
    "delay_seconds_between_items": 1.0,
    "auto_sync_enabled": false,
    "auto_sync_bookmarks_cron": "0 */6 * * *"
  },
  "pixiv": {
    "refresh_token": "***"
  }
}
```

---

### 8.2 更新设置
**POST** `/api/dashboard/settings`

请求体：
```json
{
  "sync.max_items_per_run": 100,
  "sync.auto_sync_enabled": true
}
```

响应：
```json
{
  "ok": true
}
```

---

### 8.3 重载设置
**POST** `/api/dashboard/settings/reload`

响应：
```json
{
  "ok": true
}
```

---

## 9. 待删除管理

### 9.1 获取待删除列表
**GET** `/api/dashboard/pending-deletions`

查询参数：
- `page`: 页码
- `item_type`: 类型筛选（`novel`/`series`/`user`）

响应：
```json
{
  "items": [
    {
      "id": 1,
      "item_type": "novel",
      "item_id": 123456,
      "title": "小说标题",
      "detected_at": "2026-06-16T10:00:00Z",
      "reason": "404 Not Found"
    }
  ],
  "page": 1,
  "total": 5
}
```

---

### 9.2 获取待删除数量
**GET** `/api/dashboard/pending-deletions/count`

响应：
```json
{
  "count": 5
}
```

---

### 9.3 检测待删除项
**POST** `/api/dashboard/pending-deletions/detect`

响应：
```json
{
  "ok": true,
  "detected_count": 3
}
```

---

### 9.4 确认删除
**POST** `/api/dashboard/pending-deletions/<deletion_id>/confirm`

响应：
```json
{
  "ok": true
}
```

---

### 9.5 恢复项目
**POST** `/api/dashboard/pending-deletions/<deletion_id>/restore`

响应：
```json
{
  "ok": true
}
```

---

## 10. AI 创作

> **注**: AI 创作相关 API 约 20+ 个端点，这里列出核心接口。完整文档请参考 `docs/AI_WRITING_STUDIO_PLAN.md`

### 10.1 创建 AI Agent
**POST** `/api/ai/agents`

### 10.2 续写
**POST** `/api/ai/drafts/continue`

### 10.3 改写
**POST** `/api/ai/drafts/rewrite`

### 10.4 创建项目
**POST** `/api/ai/projects`

### 10.5 生成长篇规划
**POST** `/api/ai/projects/<project_id>/plans`

---

## 11. 偏好与推荐

### 11.1 创建偏好画像
**POST** `/api/preferences/profiles`

请求体：
```json
{
  "name": "我的偏好",
  "description": "描述..."
}
```

响应：
```json
{
  "profile_id": 1,
  "name": "我的偏好"
}
```

---

### 11.2 分析偏好
**POST** `/api/preferences/profiles/<profile_id>/analyze`

响应：
```json
{
  "tag_frequencies": {
    "恋愛": 45,
    "ファンタジー": 30
  },
  "author_preferences": [
    {"user_id": 789, "count": 10}
  ],
  "length_distribution": {
    "short": 10,
    "medium": 30,
    "long": 5
  }
}
```

---

### 11.3 生成搜索计划
**POST** `/api/preferences/profiles/<profile_id>/search-plan`

响应：
```json
{
  "primary_tags": ["恋愛", "ファンタジー"],
  "broad_queries": ["恋愛 ファンタジー"],
  "precise_queries": ["異世界 恋愛 魔法"],
  "experimental_queries": ["ダークファンタジー 恋愛"]
}
```

---

### 11.4 运行推荐
**POST** `/api/recommendations/run`

请求体：
```json
{
  "profile_id": 1,
  "limit": 50
}
```

响应：
```json
{
  "run_id": 1,
  "status": "running"
}
```

---

### 11.5 获取推荐结果
**GET** `/api/recommendations/runs/<run_id>`

响应：
```json
{
  "run_id": 1,
  "status": "completed",
  "items": [
    {
      "novel_id": 999888,
      "title": "推荐小说",
      "score": 0.85,
      "reason": "匹配你喜欢的标签：恋愛、ファンタジー"
    }
  ]
}
```

---

## 12. 缓存管理

### 12.1 获取缓存状态
**GET** `/api/cache/status`

响应：
```json
{
  "log_count": 150,
  "old_log_count": 100,
  "cache_size_mb": 25.6
}
```

---

### 12.2 清理缓存
**POST** `/api/cache/clear`

请求体：
```json
{
  "clear_logs": true,
  "keep_days": 7
}
```

响应：
```json
{
  "ok": true,
  "cleared_logs": 100
}
```

---

## 13. OAuth 与 Token

### 13.1 开始 OAuth 授权
**POST** `/oauth/start`

### 13.2 OAuth 回调
**GET** `/oauth/callback`

### 13.3 保存 Token
**POST** `/api/save-token`

请求体：
```json
{
  "refresh_token": "..."
}
```

响应：
```json
{
  "ok": true,
  "message": "已写入 .env"
}
```
错误：HTTP 400，`{"error": "missing refresh_token"}`

---

## 14. 健康检查

### 14.1 应用健康检查
**GET** `/api/health`

响应：
```json
{
  "status": "ok",
  "uptime_seconds": 3600,
  "db_connected": true,
  "running_jobs": 0
}
```

---

### 14.2 Nginx 健康检查
**GET** `/nginx-health`

响应：
```
OK
```

---

## 📌 错误码参考

| 错误码 | HTTP 状态 | 说明 |
|--------|-----------|------|
| `AUTH_REQUIRED` | 401 | 需要登录 |
| `CSRF_INVALID` | 403 | CSRF Token 无效 |
| `NOT_FOUND` | 404 | 资源不存在 |
| `JOB_RUNNING` | 409 | 已有任务运行中 |
| `VALIDATION_ERROR` | 400 | 参数验证失败 |
| `SERVER_ERROR` | 500 | 服务器内部错误 |

---

## 🔧 使用示例

### Python 示例
```python
import requests

# 登录
session = requests.Session()
session.post('http://localhost:5010/api/auth/login', json={
    'token': 'your_dashboard_token'
})

# 获取 CSRF Token
csrf_token = session.get('http://localhost:5010/api/csrf-token').json()['csrf_token']

# 开始同步
response = session.post(
    'http://localhost:5010/api/dashboard/sync/bookmarks',
    headers={'X-CSRF-Token': csrf_token}
)
job_id = response.json()['job_id']

# 查询状态
status = session.get(
    f'http://localhost:5010/api/dashboard/sync/status?job_id={job_id}'
).json()
print(status)
```

### JavaScript 示例
```javascript
// 使用 fetch API
async function syncBookmarks() {
  // 获取 CSRF Token
  const csrfResp = await fetch('/api/csrf-token');
  const { csrf_token } = await csrfResp.json();

  // 开始同步
  const syncResp = await fetch('/api/dashboard/sync/bookmarks', {
    method: 'POST',
    headers: {
      'X-CSRF-Token': csrf_token,
      'Content-Type': 'application/json'
    }
  });

  const { job_id } = await syncResp.json();
  console.log('Job started:', job_id);

  // 轮询状态
  const interval = setInterval(async () => {
    const statusResp = await fetch(`/api/dashboard/sync/status?job_id=${job_id}`);
    const { job } = await statusResp.json();

    if (job.status !== 'running') {
      clearInterval(interval);
      console.log('Job completed:', job);
    }
  }, 2000);
}
```

---

## 📚 相关文档

- [README.md](../README.md) - 项目总览
- [AI_WRITING_STUDIO_PLAN.md](AI_WRITING_STUDIO_PLAN.md) - AI 创作功能详解
- [PREFERENCE_RECOMMENDER_REQUIREMENTS.md](PREFERENCE_RECOMMENDER_REQUIREMENTS.md) - 推荐系统需求

---

**文档维护者**: 项目团队  
**反馈渠道**: GitHub Issues

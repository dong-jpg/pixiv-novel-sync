# Pixiv Novel Sync API Documentation

本文档描述了重构后基于 Vue 3 的前端所使用的接口结构。

## 1. 仪表盘 (Dashboard)

### 获取仪表盘概览数据
`GET /api/dashboard/status`

返回包含当前用户信息、任务概览、基本统计信息的数据。

```json
{
  "user": {
    "name": "Username",
    "account": "user_id_or_account",
    "avatar_url": "url",
    "status": "normal"
  },
  "stats": {
    "total_novels": 100,
    "total_users": 20,
    "total_series": 5,
    "pending_deletions": 2
  }
}
```

### 获取同步任务状态
`GET /api/dashboard/sync/status`

获取最近一次任务的状态。

```json
{
  "job_id": "uuid",
  "status": "running|completed|failed|pending",
  "task_type": "bookmark_sync",
  "message": "正在同步...",
  "error": null,
  "elapsed_seconds": 12.5,
  "progress": {
    "percent": 45,
    "current_novel": "Novel Title",
    "synced_novels": 10,
    "downloaded_assets": 45,
    "total_novels_estimated": 100
  },
  "tasks_queue": [
    { "id": "t1", "name": "Sync Books", "status": "completed" }
  ],
  "logs": [
    "2023-05-25 10:00:00 - Start",
    "2023-05-25 10:00:01 - Syncing..."
  ]
}
```

## 2. 小说与用户 (Novels & Users)

### 获取小说列表
`GET /api/dashboard/novels?page=1&category=bookmark&search=keyword&sort=updated_desc`

```json
{
  "items": [
    {
      "id": 123,
      "title": "Novel Title",
      "author_name": "Author",
      "cover_url": "url",
      "kind": "single|series",
      "restrict": "public|private",
      "total_bookmarks": 100,
      "total_views": 1000,
      "last_seen_date": "2023-05-25"
    }
  ],
  "total": 50,
  "page": 1,
  "pages": 5
}
```

### 获取关注列表
`GET /api/dashboard/users?page=1&status=all`

```json
{
  "items": [
    {
      "id": 456,
      "name": "Author Name",
      "account": "account",
      "avatar_url": "url",
      "status": "normal|no_novels|suspended|unknown",
      "novel_count": 15,
      "updated_date": "2023-05-25"
    }
  ],
  "total": 30,
  "page": 1,
  "pages": 3
}
```

## 3. 全局状态扩展 (需后端增加或现有组合)

为了在顶层导航栏（Navbar）更方便地获取未读提示或用户状态，我们将需要一个全局状态聚合接口。

### 获取全局 Shell 数据 (建议新增)
`GET /api/dashboard/shell-data`

```json
{
  "user": {
    "name": "CurrentUser"
  },
  "pending_deletions_count": 3
}
```

(详细的设置 API 结构以及其他的在后续开发中补全)

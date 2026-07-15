# pixiv-novel-sync 知识图谱

> [!WARNING]
> **历史快照，不是当前事实来源。** 本文档保留特定时间点的项目结构、模块和数据流描述，行数、模板数量与接口可能已经变化。当前入口请查阅 [README.md](README.md)，前端接口请查阅 [docs/frontend-api-contract.md](docs/frontend-api-contract.md)，最终行为以代码为准。

> AI 友好的项目结构化文档，覆盖架构、模块、数据流、API 和关键决策。

---

## 1. 项目概览

| 字段 | 值 |
|------|-----|
| 名称 | pixiv-novel-sync |
| 版本 | 0.1.0 |
| 类型 | 后端工具 + Web 管理界面 |
| 语言 | Python >= 3.10 |
| 用途 | Pixiv 小说增量归档：自动同步收藏/关注/追更的小说到本地 SQLite + 文件系统 |
| 入口 | CLI (`pixiv-novel-sync`) 或 Web (`create_app()`) |
| 总代码量 | ~7,500 行 Python + 11 个 Jinja2 HTML 模板 |

---

## 2. 技术栈

```
┌─────────────────────────────────────────────────┐
│                   前端层                          │
│  Jinja2 模板 + Vue 3 (CDN) + Tailwind CSS (CDN)   │
├─────────────────────────────────────────────────┤
│                   Web 层                         │
│  Flask >= 3.0.3 (SSR, REST API, 图片代理)       │
├─────────────────────────────────────────────────┤
│                  业务逻辑层                       │
│  sync_engine.py (BookmarkNovelSyncService)       │
│  jobs/quick_sync.py (任务入口)                   │
│  webapp.py (SyncJobManager + AutoSyncScheduler)  │
├─────────────────────────────────────────────────┤
│                  数据访问层                       │
│  storage_db.py (Database, SQLite WAL)            │
│  storage_files.py (FileStorage, 原子写入)        │
├─────────────────────────────────────────────────┤
│                  外部接口层                       │
│  pixivpy3 (App API) + requests (Web Cookie API)  │
│  Playwright (OAuth + Cookie 自动刷新)            │
├─────────────────────────────────────────────────┤
│                  基础设施层                       │
│  PyYAML + python-dotenv (配置)                   │
│  croniter (定时调度)                             │
│  Nginx (反代 + 图片缓存) + systemd (部署)        │
└─────────────────────────────────────────────────┘
```

---

## 3. 目录结构

```
pixiv-novel-sync/
├── config/
│   ├── config.yaml.example   # 运行时配置模板 (YAML)
│   └── nginx/                # Nginx 反代配置
├── deploy/
│   └── systemd/              # service + timer 单元
├── scripts/
│   ├── check_series.py       # 调试: 检查系列同步状态
│   ├── test_web_login.py     # 调试: 测试 Playwright 登录
│   ├── install_server.sh     # 服务器一键安装
│   └── clear-cache.sh        # 清理 Nginx 图片缓存
├── src/pixiv_novel_sync/     # 核心 Python 包
│   ├── __init__.py           # version = "0.1.0"
│   ├── cli.py                # CLI 入口 (argparse)
│   ├── webapp.py             # Flask 应用 + 调度器 (2626 行, 最大)
│   ├── sync_engine.py        # 同步引擎核心 (1734 行)
│   ├── storage_db.py         # SQLite 数据库层 (1401 行)
│   ├── settings.py           # 配置加载 + cron 解析 (402 行)
│   ├── models.py             # 数据模型 dataclass (64 行)
│   ├── auth.py               # Pixiv API 认证 (71 行)
│   ├── oauth_helper.py       # OAuth PKCE 流程 (196 行)
│   ├── playwright_login.py   # Playwright 自动登录 (499 行)
│   ├── storage_files.py      # 文件存储 + 资源下载 (108 行)
│   ├── logging_utils.py      # 日志配置 (10 行)
│   ├── utils_hashing.py      # SHA256 + JSON 序列化 (13 行)
│   ├── utils_naming.py       # 文件名安全处理 (18 行)
│   ├── utils_text.py         # 文本清洗 + Markdown 转换 (28 行)
│   ├── jobs/
│   │   └── quick_sync.py     # 同步任务入口 (112 行)
│   └── templates/            # Jinja2 HTML 模板 (11 个)
├── pyproject.toml            # 构建配置
├── deploy.sh                 # 一键部署脚本
├── update.sh                 # 一键更新脚本
├── .env.example              # 环境变量模板
└── README.md                 # 项目文档
```

---

## 4. 核心模块依赖关系

```
cli.py ──────────────────────────┐
                                 ▼
webapp.py (create_app) ──► jobs/quick_sync.py
   │                           │
   │  ┌────────────────────────┘
   │  ▼
   │  sync_engine.py (BookmarkNovelSyncService)
   │     │
   │     ├──► storage_db.py (Database)
   │     │       └──► models.py (dataclass 定义)
   │     │
   │     ├──► storage_files.py (FileStorage)
   │     │
   │     ├──► settings.py (Settings)
   │     │
   │     ├──► utils_hashing.py
   │     ├──► utils_text.py
   │     └──► utils_naming.py
   │
   ├──► auth.py (PixivAuthManager)
   │       └──► pixivpy3.AppPixivAPI
   │
   ├──► oauth_helper.py (OAuthManager)
   │       └──► Playwright (OAuth PKCE)
   │
   └──► playwright_login.py (PlaywrightLoginHelper)
           └──► Playwright Firefox (自动登录)
```

---

## 5. 数据模型

### 5.1 Python Dataclass (models.py)

```python
UserRecord(user_id: int, name: str, account: str|None, raw_json: str)

NovelRecord(
    novel_id: int, user_id: int, series_id: int|None, title: str,
    caption: str|None, visible: bool, restrict: str, x_restrict: int,
    text_length: int, total_bookmarks: int, total_views: int,
    cover_url: str|None, tags_json: str, create_date: str|None,
    raw_json: str, meta_hash: str
)

NovelTextRecord(novel_id: int, text_raw: str, text_markdown: str|None, text_hash: str)

AssetRecord(novel_id: int, asset_type: str, remote_url: str, local_path: str, file_hash: str|None)

SourceRecord(novel_id: int, source_type: str, source_key: str)
```

### 5.2 SQLite 表结构

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│    users     │     │    novels    │     │ novel_texts  │
├──────────────┤     ├──────────────┤     ├──────────────┤
│ user_id (PK) │◄────│ user_id (FK) │     │ novel_id (PK)│
│ name         │     │ novel_id (PK)│◄────│ text_raw     │
│ account      │     │ series_id    │     │ text_markdown│
│ raw_json     │     │ title        │     │ text_hash    │
│ status       │     │ caption      │     └──────────────┘
│ last_checked │     │ visible      │
│ updated_at   │     │ restrict     │     ┌──────────────┐
└──────────────┘     │ x_restrict   │     │   sources    │
                     │ text_length  │     ├──────────────┤
                     │ total_*      │     │ novel_id (PK)│
                     │ cover_url    │     │ source_type  │
                     │ tags_json    │     │ source_key   │
                     │ create_date  │     └──────────────┘
                     │ raw_json     │
                     │ meta_hash    │     ┌──────────────┐
                     │ status       │     │   assets     │
                     └──────────────┘     ├──────────────┤
                            │             │ asset_id (PK)│
                            ▼             │ novel_id (FK)│
                     ┌──────────────┐     │ asset_type   │
                     │   series     │     │ remote_url   │
                     ├──────────────┤     │ local_path   │
                     │ series_id(PK)│     │ file_hash    │
                     │ title        │     └──────────────┘
                     │ description  │
                     │ user_id (FK) │     ┌──────────────┐
                     │ cover_url    │     │  task_logs   │
                     │ total_novels │     ├──────────────┤
                     │ is_subscribed│     │ id (PK)      │
                     │ status       │     │ task_type    │
                     └──────────────┘     │ task_name    │
                                          │ job_id       │
┌───────────────────┐                     │ status       │
│ pending_deletions │                     │ started_at   │
├───────────────────┤                     │ duration     │
│ id (PK)           │                     │ stats_json   │
│ item_type         │                     │ logs_json    │
│ item_id           │                     │ is_auto_sync │
│ reason            │                     └──────────────┘
│ status            │
│ detected_at       │     ┌──────────────┐
│ confirmed_at      │     │  watermarks  │
└───────────────────┘     ├──────────────┤
                          │ sync_type(PK)│
┌───────────────────┐     │ key (PK)     │
│ sync_check_list   │     │ value (JSON) │
├───────────────────┤     │ updated_at   │
│ novel_id (PK)     │     └──────────────┘
│ exists_local      │
│ checked_at        │     ┌──────────────┐
└───────────────────┘     │  novel_fts   │
                          │ (FTS5 虚拟表)│
                          │ novel_id     │
                          │ title        │
                          │ caption      │
                          │ author_name  │
                          │ body         │
                          └──────────────┘
```

### 5.3 文件系统结构

```
data/
├── state/
│   └── pixiv_sync.db          # SQLite 数据库
└── library/
    ├── public/                 # 公开收藏
    │   └── authors/
    │       └── {user_id}_{name}/
    │           └── novels/
    │               └── {novel_id}_{hash}/
    │                   ├── meta.json
    │                   ├── text.txt
    │                   ├── text.md
    │                   └── assets/
    │                       ├── cover/
    │                       └── inline_image/
    └── private/                # 私密收藏 (同结构)
```

---

## 6. 配置体系

### 6.1 配置来源优先级

```
.env (环境变量)  ──优先──►  config/config.yaml (运行时配置)  ──回退──►  硬编码默认值
```

### 6.2 关键环境变量 (.env)

| 变量 | 用途 |
|------|------|
| `PIXIV_REFRESH_TOKEN` | Pixiv API 认证 (主要方式) |
| `PIXIV_ACCESS_TOKEN` | 备用认证 |
| `PIXIV_USER_ID` | 当前用户 ID |
| `PIXIV_USERNAME` / `PIXIV_PASSWORD` | Playwright 自动获取 token |
| `PIXIV_WEB_COOKIE` | Web Cookie (追更列表) |
| `PIXIV_PROXY` | HTTP 代理 |
| `DASHBOARD_TOKEN` | Web 仪表盘访问密码 |
| `PIXIV_FLASK_SECRET` | Flask session 密钥 |

### 6.3 config.yaml 三层结构

```yaml
pixiv:           # 超时、SSL、代理、Web Cookie、账号密码
sync:            # 同步开关、限速参数、8 个定时任务配置
  auto_sync_*:   # 每个任务: enabled + interval_hours + cron (三选二)
storage:         # 公开/私密目录、数据库路径
```

---

## 7. 同步引擎 (sync_engine.py)

### 7.1 核心类: BookmarkNovelSyncService

**构造依赖**: `AppPixivAPI`, `Database`, `FileStorage`, `Settings`

### 7.2 同步能力矩阵

| 方法 | 数据源 | 输出 | 限速策略 |
|------|--------|------|----------|
| `sync()` | Pixiv API `user_bookmarks_novel` | novels + texts + assets + sources | 每页间隔 + 每项间隔 |
| `sync_following_list()` | Pixiv API `user_following` | users | 每页间隔 |
| `sync_following_novels()` | Pixiv API `user_novels` (逐用户) | novels + texts + assets | 水位线 + 每项间隔 + 用户间隔 |
| `sync_subscribed_series()` | Web Cookie API `watchList` | series + novels + texts + assets | 每系列间隔 + 每章节间隔 |
| `check_all_existence()` | 上述全部 | sync_check_list | 仅获取 ID，不下载内容 |
| `detect_unbookmarked_novels()` | 本地 DB vs 远程收藏 | pending_deletions | 每页间隔 |
| `detect_unfollowed_series()` | 本地 DB vs 远程追更 | pending_deletions | 每页间隔 |

### 7.3 单本小说同步流程 (`_sync_novel`)

```
1. 检查 sync_check_list 是否标记为已存在 → 跳过
2. 检查 DB 中 novel_exists() → 跳过
3. 获取小说详情 (API)
4. 获取正文 (Web API webview)
5. 构建 NovelRecord + NovelTextRecord
6. upsert 到 DB (novels + novel_texts + sources + FTS5)
7. 写入文件 (text.txt + text.md + meta.json)
8. 下载资源 (cover + inline_image) → 记录 assets 表
9. 更新水位线
```

### 7.4 限速机制

- **顺延机制**: 跳过已存在内容时不计入配额 (max_items_per_run)
- **水位线**: `watermarks` 表记录每种同步类型的最后位置，避免重复遍历
- **信号量**: 最多 1 个同步任务并发运行

---

## 8. 数据库层 (storage_db.py)

### 8.1 核心类: Database

**线程安全模型**: `RLock` + `BEGIN IMMEDIATE` 串行化写入；WAL 模式允许并发读

### 8.2 方法分类

| 类别 | 方法 |
|------|------|
| **CRUD** | `upsert_user`, `upsert_novel`, `upsert_novel_text`, `upsert_source`, `record_asset` |
| **状态管理** | `upsert_user_status`, `upsert_novel_status`, `upsert_series_status` |
| **系列** | `upsert_series`, `upsert_subscribed_series`, `clear_subscribed_series` |
| **查询** | `novel_exists`, `get_novel_meta_hash`, `get_novel_text_hash`, `get_existing_novel_ids` |
| **列表** | `list_followed_users`, `list_recent_novels`, `list_bookmark_novels`, `list_following_series`, `list_users`, `list_user_novels` |
| **详情** | `get_novel_detail`, `get_series_detail`, `get_user_detail`, `get_user_summary` |
| **全文搜索** | `replace_fts` (FTS5) |
| **同步检查** | `init_sync_check_table`, `upsert_sync_check_item`, `get_sync_check_list` |
| **水位线** | `get_watermark`, `update_watermark`, `clear_watermark` |
| **待删除** | `add_pending_deletion`, `list_pending_deletions`, `confirm_pending_deletion`, `restore_pending_deletion` |
| **任务日志** | `create_task_log`, `update_task_log`, `get_task_logs`, `cleanup_old_task_logs` |
| **删除** | `delete_novel`, `delete_user`, `delete_series`, `delete_bookmark` |
| **统计** | `export_stats` |

---

## 9. Web 应用 (webapp.py)

### 9.1 Flask 应用结构

```
create_app()
├── SyncJobManager          # 后台同步任务管理 (信号量并发控制)
├── AutoSyncScheduler       # 8 个独立定时任务调度器 (daemon 线程)
├── ConfigManager           # 配置热加载 + 持久化
└── 路由注册
```

### 9.2 REST API 路由表

| 路由 | 方法 | 用途 |
|------|------|------|
| `/` | GET | 重定向到 dashboard |
| `/login` | GET | Token 登录页 |
| `/api/auth/login` | GET/POST | 认证 (Token 或 Playwright 自动登录) |
| `/api/auth/logout` | POST | 登出 |
| `/oauth/callback` | GET | OAuth PKCE 回调 |
| `/proxy/image` | GET | Pixiv 图片代理 (绕防盗链) |
| **仪表盘页面** | | |
| `/dashboard` | GET | 首页仪表盘 |
| `/dashboard/follows` | GET | 关注用户列表 |
| `/dashboard/novels` | GET | 小说归档 |
| `/dashboard/novels/<id>` | GET | 小说详情 |
| `/dashboard/series/<id>` | GET | 系列详情 |
| `/dashboard/users/<id>` | GET | 用户详情 |
| `/dashboard/logs` | GET | 任务日志 |
| `/dashboard/settings` | GET | 设置页 |
| `/dashboard/pending-deletions` | GET | 待确认删除 |
| **API** | | |
| `/api/dashboard/status` | GET | 系统状态 (统计 + 调度器) |
| `/api/dashboard/shell-data` | GET | 聚合的 Web Shell 状态数据 (Navbar 等) |
| `/api/dashboard/follows` | GET | 关注用户列表 (JSON) |
| `/api/dashboard/novels` | GET | 小说列表 (JSON, 支持分页/筛选) |
| `/api/dashboard/novels/<id>` | GET | 小说详情 (JSON) |
| `/api/dashboard/series/<id>` | GET | 系列详情 (JSON) |
| `/api/dashboard/users` | GET | 用户列表 (JSON) |
| `/api/dashboard/users/<id>` | GET | 用户详情 (JSON) |
| `/api/dashboard/users/<id>/novels` | GET | 用户小说列表 |
| `/api/dashboard/users/<id>/check` | POST | 检查用户状态 |
| `/api/dashboard/users/<id>/sync` | POST | 同步用户小说 |
| `/api/dashboard/settings` | GET/POST | 获取/保存设置 |
| `/api/dashboard/settings/reload` | POST | 重载配置 |
| `/api/dashboard/sync/start` | POST | 启动完整同步 |
| `/api/dashboard/check-bookmarks` | POST | 启动预检查 |
| `/api/dashboard/sync/status` | GET | 当前任务状态 |
| `/api/dashboard/sync/<task_type>` | POST | 启动单个同步任务 |
| `/api/dashboard/sync/subscribed-series` | POST | 同步追更系列 |
| `/api/dashboard/auto-sync/status` | GET | 定时调度器状态 |
| `/api/dashboard/auto-sync/toggle` | POST | 开关定时调度器 |
| `/api/dashboard/auto-sync/stop-task` | POST | 停止当前定时任务 |
| `/api/dashboard/logs` | GET | 任务日志 (JSON, 分页) |
| `/api/dashboard/logs/<id>` | GET | 日志详情 |
| `/api/cache/status` | GET | Nginx 缓存状态 |
| `/api/cache/clear` | POST | 清理缓存 |
| `/api/dashboard/shell-data` | GET | 聚合的 Web Shell 状态数据 |
| `/api/dashboard/export/stats` | GET | 导出统计 (JSON) |
| **删除操作** | | |
| `/api/dashboard/novels/<id>` | DELETE | 删除小说 |
| `/api/dashboard/users/<id>` | DELETE | 删除用户 |
| `/api/dashboard/series/<id>` | DELETE | 删除系列 |
| `/api/dashboard/bookmarks/<id>` | DELETE | 删除收藏 |
| **待删除管理** | | |
| `/api/dashboard/pending-deletions` | GET | 待删除列表 |
| `/api/dashboard/pending-deletions/count` | GET | 待删除数量 |
| `/api/dashboard/pending-deletions/detect` | POST | 触发检测 |
| `/api/dashboard/pending-deletions/<id>/confirm` | POST | 确认删除 |
| `/api/dashboard/pending-deletions/<id>/restore` | POST | 恢复 |
| **系统** | | |
| `/api/health` | GET | 健康检查 |
| **OAuth Token 获取** | | |
| `/api/token-config` | GET | Token 配置状态 |
| `/api/token-jobs` | POST | 创建 Token 获取任务 |
| `/api/token-jobs/<id>` | GET | Token 任务状态 |
| `/api/save-token` | POST | 保存 Token |
| `/oauth/start` | POST | 启动 OAuth PKCE |
| `/oauth/exchange/<id>` | POST | 交换 Token |

### 9.3 定时任务调度器 (AutoSyncScheduler)

9 个独立运行的定时任务，每个支持 cron 表达式或固定间隔：

| 任务名 | 方法 | 默认间隔 | 用途 |
|--------|------|----------|------|
| `bookmarks` | `_sync_bookmarks` | 6h | 收藏同步 |
| `following_list` | `_sync_following_list` | 24h | 关注用户列表 |
| `following_novels` | `_sync_following_novels` | 6h | 关注用户新小说（水位线增量，扫描全部用户） |
| `subscribed_series` | `_sync_subscribed_series` | 6h | 追更系列 |
| `user_status` | `_sync_user_status` | 6h | 用户状态检查 |
| `novel_status` | `_sync_novel_status` | 6h | 小说状态检查 |
| `series_status` | `_sync_series_status` | 6h | 系列状态检查 |
| `user_backup` | `_sync_user_backup` | 24h | 全量备份关注用户小说（按 users_limit 轮询） |
| `pending_deletion_detection` | `_sync_pending_detection` | 12h | 取消检测 |

---

## 10. 认证体系

### 10.1 认证方式

```
┌─────────────────────────────────────────────────────┐
│                    认证入口                           │
├─────────────────────────────────────────────────────┤
│ 1. refresh_token (主要)                              │
│    .env: PIXIV_REFRESH_TOKEN                         │
│    → auth.py: PixivAuthManager.login()               │
│    → pixivpy3.AppPixivAPI.auth(refresh_token)        │
│                                                     │
│ 2. access_token (备用)                               │
│    .env: PIXIV_ACCESS_TOKEN                          │
│    → auth.py: PixivAuthManager.login()               │
│    → pixivpy3.AppPixivAPI.set_auth(token, None)      │
│                                                     │
│ 3. OAuth PKCE (交互式获取)                           │
│    → oauth_helper.py: OAuthManager                   │
│    → 浏览器打开 Pixiv 授权页 → 回调 /oauth/callback  │
│                                                     │
│ 4. Playwright 自动登录 (获取 refresh_token)          │
│    → playwright_login.py: PlaywrightLoginHelper      │
│    → Firefox headed + Xvfb → 辅助常规浏览器登录       │
│    → 输入用户名密码 → 获取 Cookie + Token            │
│                                                     │
│ 5. Web Cookie (追更列表专用)                         │
│    .env: PIXIV_WEB_COOKIE                            │
│    → sync_engine.py: sync_subscribed_series()        │
│    → requests + Cookie → Pixiv Web API               │
│    → 支持自动刷新 (playwright_login.py)              │
└─────────────────────────────────────────────────────┘
```

### 10.2 Web 仪表盘认证

- Token 模式: `DASHBOARD_TOKEN` 环境变量 → session 验证
- `_check_auth()` before_request 钩子

---

## 11. 数据流

### 11.1 收藏同步流程

```
用户触发 (Web/API/CLI)
    │
    ▼
SyncJobManager.start_job(["sync_bookmarks"])
    │
    ▼
jobs/quick_sync.py::run_bookmark_sync()
    │
    ├── PixivAuthManager.login() → AppPixivAPI
    ├── Database.init_schema()
    ├── FileStorage.ensure_dirs()
    │
    ▼
BookmarkNovelSyncService.sync(user_id, restricts)
    │
    ├── API: user_bookmarks_novel() → 分页获取收藏
    │   ├── 对每本小说:
    │   │   ├── 检查 sync_check_list → 跳过已存在
    │   │   ├── 检查 DB novel_exists() → 跳过
    │   │   ├── API: novel_detail() → 获取元数据
    │   │   ├── Web API: webview → 获取正文
    │   │   ├── DB: upsert_novel + upsert_novel_text + upsert_source + replace_fts
    │   │   ├── FileStorage: 写入 text.txt + text.md + meta.json
    │   │   ├── 下载 cover + inline_image → assets 表
    │   │   └── 限速: delay_seconds_between_items
    │   └── 限速: delay_seconds_between_pages
    │
    └── 返回统计: {synced, skipped, errors}
```

### 11.2 追更系列同步流程

```
Web Cookie 认证
    │
    ▼
requests + Cookie → Pixiv Web API: watchList
    │
    ├── 获取追更系列列表
    │   ├── DB: upsert_subscribed_series
    │   └── 对每个系列:
    │       ├── API: series_detail → 章节列表
    │       ├── 对每个章节:
    │       │   ├── _sync_novel() → 同步单本
    │       │   └── delay_seconds_between_chapters
    │       └── delay_seconds_between_series
    │
    └── 更新水位线
```

### 11.3 取消检测流程

```
定时触发 / 手动触发
    │
    ├── detect_unbookmarked_novels()
    │   ├── 远程: 获取全部收藏 ID 集合
    │   ├── 本地: 查询 sources 表中 bookmark_* 来源的小说
    │   ├── 差集: 本地有但远程无 → pending_deletions
    │   └── 清理: 远程仍存在的旧 pending 记录
    │
    └── detect_unfollowed_series()
        ├── 远程: 获取全部追更系列 ID 集合
        ├── 本地: 查询 is_subscribed=1 的系列
        ├── 差集 → pending_deletions
        └── 清理旧记录
```

---

## 12. 部署架构

```
┌─────────────────────────────────────────────────────┐
│                    Linux 服务器                       │
├─────────────────────────────────────────────────────┤
│                                                     │
│  Nginx (:80/:5010)                                  │
│  ├── /proxy/image → Pixiv CDN (缓存 365天, 2GB)    │
│  └── /* → Flask                                     │
│                                                     │
│  Flask (systemd service, :5011)                     │
│  ├── Web 仪表盘 (Gunicorn / dev server)             │
│  ├── AutoSyncScheduler (daemon 线程)                │
│  └── SyncJobManager (后台任务)                      │
│                                                     │
│  CLI 定时任务 (systemd timer, 每30分钟)             │
│  └── pixiv-novel-sync sync-bookmarks               │
│                                                     │
│  数据目录:                                          │
│  ├── data/state/pixiv_sync.db (SQLite)              │
│  └── data/library/{public,private}/ (文件)          │
│                                                     │
└─────────────────────────────────────────────────────┘
```

---

## 13. 关键设计决策

| 决策 | 原因 |
|------|------|
| SQLite WAL 模式 | 并发读 + 单写，适合 Web + 后台任务并行 |
| `BEGIN IMMEDIATE` + RLock | 避免 SQLITE_BUSY，显式事务管理 |
| 文件系统 + DB 双存储 | 文件便于直接阅读，DB 便于查询/搜索 |
| 水位线机制 | 避免每次从头遍历，增量同步优化 |
| 待确认删除 (pending_deletions) | 防止误删，用户确认后才执行 |
| 信号量限并发 | 最多 1 个同步任务，防止 API 限速 |
| 顺延机制 | 跳过已存在内容不计入配额，确保新内容都能被同步 |
| FTS5 全文搜索 | 高效的小说标题/正文搜索 |
| Nginx 图片缓存 | 减少对 Pixiv CDN 的请求，绕过防盗链 |
| `sync_check_list` scoped by `scope` | 预检查结果按任务隔离，避免自动/手动任务互相污染 |
| `_sync_novel` 失败返回 `failed` | 避免 API/网络失败被误报为“跳过已存在” |
| 关注用户小说不再用 novel_id 硬停 | 防止旧 ID/排序异常内容被水位线漏同步 |
| 订阅系列按正文存在性跳过 | 仅有元数据但缺正文的章节会被补同步 |
| 事务感知 commit | 单本小说 DB 写入合并提交，减少提交次数并提升一致性 |

---

## 14. 模块文件行数索引

| 文件 | 行数 | 职责 |
|------|------|------|
| `webapp.py` | 2781 | Flask 应用、路由、调度器、任务管理 |
| `sync_engine.py` | 1679 | 同步业务逻辑核心 |
| `storage_db.py` | 1455 | SQLite 数据库操作 |
| `playwright_login.py` | 499 | Playwright 自动登录 |
| `settings.py` | 412 | 配置加载、cron 解析 |
| `oauth_helper.py` | 196 | OAuth PKCE 流程 |
| `jobs/quick_sync.py` | 112 | 同步任务入口 |
| `storage_files.py` | 108 | 文件存储、资源下载 |
| `cli.py` | 77 | CLI 入口 |
| `auth.py` | 71 | Pixiv API 认证 |
| `models.py` | 64 | 数据模型 dataclass |
| `utils_text.py` | 28 | 文本清洗、Markdown |
| `utils_naming.py` | 18 | 文件名安全处理 |
| `utils_hashing.py` | 13 | SHA256 + JSON |
| `logging_utils.py` | 10 | 日志配置 |

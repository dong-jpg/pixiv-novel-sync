# Pixiv Novel Sync

Pixiv 小说增量归档工具。自动同步收藏小说、关注用户作品、追更系列，提供 Web 管理界面，支持定时任务和状态监控。

## 功能概览

### 同步能力

| 任务 | 手动 | 自动 | 说明 |
|------|------|------|------|
| 同步收藏小说 | ✅ | ✅ | 公开 + 私密收藏，支持限速和顺延 |
| 同步关注用户列表 | ✅ | ✅ | 获取关注用户信息并入库 |
| 同步关注用户小说 | ✅ | ✅ | 同步所有关注用户的单本和系列小说 |
| 同步追更系列 | ✅ | ✅ | 同步订阅系列的封面、章节和正文 |
| 检查用户状态 | ✅ | ✅ | 检测关注用户是否封号/无小说 |
| 检查小说状态 | ✅ | ✅ | 检测小说是否被删除/受限 |
| 检查系列状态 | ✅ | ✅ | 检测系列是否被删除 |

### 同步内容

- 小说元数据（标题、简介、标签、收藏数、浏览数）
- 小说正文（原始文本 + Markdown 格式）
- 封面与插图资源下载
- 系列信息（系列名、封面、总字数）
- 用户信息与存续状态

### 限速与顺延

所有同步任务严格遵循设置页面的限速参数：
- 单次最大条数 / 最大页数
- 每次请求间隔 / 每页间隔
- 跳过内容时延迟
- 追更系列：每章节间隔、每系列间隔
- 关注用户小说：每轮同步用户数限制

顺延机制：跳过的已存在内容不计入同步配额，确保最终同步数量达标。

### 定时任务

7 个独立定时任务，每个支持：
- 开关控制
- Cron 表达式自定义执行时间
- 间隔小时数（Cron 优先）

### Web 管理界面

| 页面 | 功能 |
|------|------|
| 首页 | 系统状态、手动同步、定时同步控制、追更系列同步 |
| 关注 | 关注用户列表，支持按状态筛选（有小说/无小说/封号） |
| 小说 | 小说归档，分收藏/追更两个 tab，支持分页 |
| 日志 | 任务执行记录，支持按类型/来源筛选，保留 3 天 |
| 设置 | 同步参数、限速参数、定时任务配置、手动操作 |

### 详情页

- **用户详情**：用户信息、状态 badge、小说列表（全部/单本/系列 tab）
- **小说详情**：元数据、状态 badge、正文预览
- **系列详情**：封面、章节数、总字数、每章状态 badge

## 技术栈

- **后端**：Python / Flask / SQLite
- **前端**：原生 HTML/CSS/JS（无框架）
- **API**：pixivpy3（Pixiv App API）+ Web Cookie（追更列表）
- **存储**：SQLite（元数据/日志）+ 文件系统（正文/资源）

## 项目结构

```
src/pixiv_novel_sync/
├── webapp.py              # Flask 应用、API 路由、定时调度器
├── sync_engine.py         # 同步引擎（收藏/关注/追更/状态检查）
├── storage_db.py          # SQLite 数据库操作
├── storage_files.py       # 文件存储与资源下载
├── settings.py            # 配置加载与 Cron 解析
├── auth.py                # Pixiv 认证管理
├── models.py              # 数据模型（NovelRecord/UserRecord 等）
├── jobs/quick_sync.py     # 同步任务入口
├── templates/             # HTML 模板
│   ├── dashboard.html         # 首页
│   ├── dashboard_follows.html # 关注列表
│   ├── dashboard_novels.html  # 小说归档
│   ├── dashboard_logs.html    # 任务日志
│   ├── dashboard_settings.html# 设置页
│   ├── dashboard_user_detail.html   # 用户详情
│   ├── dashboard_novel_detail.html  # 小说详情
│   ├── dashboard_series_detail.html # 系列详情
│   └── token_login.html     # Token 登录页
└── utils_*.py             # 工具函数（哈希/文本/命名）
```

## 快速开始

```bash
# 安装
python -m venv .venv && source .venv/bin/activate
pip install .

# 配置
cp .env.example .env
# 编辑 .env 填入 Pixiv refresh_token

# 启动 Web 服务
python -m pixiv_novel_sync.webapp
# 访问 http://localhost:5010/dashboard
```

## 配置说明

配置文件 `config.yaml` + `.env`：

```yaml
sync:
  download_assets: true      # 下载封面与插图
  write_markdown: true        # 输出 txt 文件
  write_raw_text: true        # 输出原始文本文件
  bookmark_restricts:         # 同步收藏类型
    - public
    - private
  max_items_per_run: 50       # 单次最大条数
  max_pages_per_run: 5        # 单次最大页数
  delay_seconds_between_items: 1.0   # 每次请求间隔
  delay_seconds_between_pages: 1.0   # 每页间隔
  delay_seconds_between_skips: 0.1   # 跳过内容时延迟
  delay_seconds_between_series: 3.0  # 每个系列间隔
  delay_seconds_between_chapters: 1.0 # 每章节间隔
  series_sync_limit: 0        # 追更系列同步数量限制（0=全部）
  auto_sync_following_novels_users_limit: 0 # 每轮同步用户数（0=全部）

  # 定时任务（每个任务独立 cron/interval）
  auto_sync_enabled: false
  auto_sync_bookmarks_cron: "0 */6 * * *"
  auto_sync_following_novels_cron: "0 */6 * * *"
  auto_sync_subscribed_series_cron: "0 */6 * * *"
  auto_sync_user_status_cron: "0 0 * * *"
  auto_sync_novel_status_cron: ""
  auto_sync_series_status_cron: ""
```

## 输出说明

默认输出目录：

- 公开内容：`data/library/public/`
- 私密内容：`data/library/private/`
- 数据库：`data/state/pixiv_sync.db`

每本小说目录包含：

- `meta.json` — 元数据
- `text.txt` — 原始文本
- `text.md` — Markdown 格式
- `assets/` — 封面与插图

## Web Token 获取

### 方式一：OAuth 浏览器授权（推荐）

1. 启动服务后访问 `http://服务器:5010/token-login`
2. 点击"开始 Pixiv 浏览器登录"
3. 在本地浏览器完成 Pixiv 登录
4. 回调自动获取 `refresh_token` 并写入 `.env`

### 方式二：gppt fallback

如果 OAuth 失败，点击"使用旧版 gppt fallback"手动获取 token。

## 后续优化方向

### 功能增强
- [ ] 小说/系列状态检查定时任务默认开启
- [ ] 支持按作者批量同步指定用户的小说
- [ ] 支持导出同步统计数据（JSON/CSV）
- [ ] 小说全文搜索（FTS 已实现，前端搜索 UI 待做）
- [ ] 支持多账号管理

### 性能优化
- [ ] 批量事务支持（减少 SQLite commit 次数）
- [ ] Database 类实现上下文管理器协议
- [ ] 已完成任务的内存清理（`_jobs` 字典无限增长）
- [ ] 并发同步限制改为信号量

### 前端改进
- [ ] 小说列表页支持搜索和排序
- [ ] 用户详情页支持直接触发同步
- [ ] 日志页面支持实时刷新（WebSocket 或轮询）
- [ ] 移动端适配优化

### 运维改进
- [ ] Docker 部署支持
- [ ] 健康检查 API
- [ ] 同步任务进度条改进（百分比）
- [ ] 配置文件热重载

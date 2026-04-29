# 小说标签调整与用户详情页

Feature Name: novel-tabs-and-user-detail
Updated: 2026-04-29

## Description

优化小说页面分类标签（全部/收藏/追更），增加系列详情页，增强关注页面用户状态展示，新增用户详情页。

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        前端页面                              │
├─────────────────────────────────────────────────────────────┤
│  dashboard_novels.html    │  dashboard_follows.html         │
│  - 全部/收藏/追更 标签     │  - 用户列表 + 状态标识           │
│                          │  - 点击进入用户详情               │
├─────────────────────────────────────────────────────────────┤
│  dashboard_series_detail.html  │  dashboard_user_detail.html │
│  - 系列信息                    │  - 用户信息 + 状态           │
│  - 章节列表                    │  - 小说列表                  │
│                              │  - 备份按钮                  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                        API 层                               │
├─────────────────────────────────────────────────────────────┤
│  /api/dashboard/novels?category=bookmark|following          │
│  /api/dashboard/series/<series_id>                          │
│  /api/dashboard/users                                       │
│  /api/dashboard/users/<user_id>                             │
│  /api/dashboard/users/<user_id>/novels                      │
│  /api/dashboard/users/<user_id>/sync                        │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      数据库层                                │
├─────────────────────────────────────────────────────────────┤
│  novels 表        │  users 表        │  sources 表          │
│  - series_id      │  - user_id       │  - source_type       │
│  - 标记收藏/追更   │  - status 字段   │  - bookmark_*        │
│                  │                  │  - following_*        │
└─────────────────────────────────────────────────────────────┘
```

## Components and Interfaces

### 1. 数据库变更

#### users 表新增字段

```sql
ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'unknown';
-- 可选值: 'normal', 'suspended', 'cleared', 'unknown'

ALTER TABLE users ADD COLUMN last_checked_at TEXT;
-- 最后一次检查状态的时间
```

#### 新增 series 表

```sql
CREATE TABLE IF NOT EXISTS series (
    series_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    user_id INTEGER NOT NULL,
    cover_url TEXT,
    total_novels INTEGER DEFAULT 0,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

#### sources 表扩展

- source_type 增加 `bookmark_public`, `bookmark_private`, `following_series` 等值

### 2. 后端 API

#### 小说分类 API 修改

```python
# /api/dashboard/novels?category=all|bookmark|following
@app.get("/api/dashboard/novels")
def dashboard_novels():
    category = request.args.get("category", "all")
    # category: 'all' | 'bookmark' | 'following'
    # bookmark: 来源为 bookmark_public 或 bookmark_private
    # following: 来源为 following_user_scan 或 follow_feed_*
```

#### 系列详情 API

```python
# /api/dashboard/series/<series_id>
@app.get("/api/dashboard/series/<int:series_id>")
def dashboard_series_detail(series_id: int):
    # 返回系列信息 + 该系列的所有小说列表
```

#### 用户列表 API

```python
# /api/dashboard/users?page=1&status=all
@app.get("/api/dashboard/users")
def dashboard_users():
    # 返回用户列表，包含状态信息
```

#### 用户详情 API

```python
# /api/dashboard/users/<user_id>
@app.get("/api/dashboard/users/<int:user_id>")
def dashboard_user_detail(user_id: int):
    # 返回用户详情 + 状态 + 小说数量
```

#### 用户小说列表 API

```python
# /api/dashboard/users/<user_id>/novels?page=1
@app.get("/api/dashboard/users/<int:user_id>/novels")
def dashboard_user_novels(user_id: int):
    # 返回该用户的小说列表
```

#### 用户状态检查 API

```python
# POST /api/dashboard/users/<user_id>/check
@app.post("/api/dashboard/users/<int:user_id>/check")
def check_user_status(user_id: int):
    # 调用 Pixiv API 检查用户状态
    # 更新数据库中的 status 字段
```

#### 用户小说同步 API

```python
# POST /api/dashboard/users/<user_id>/sync
@app.post("/api/dashboard/users/<int:user_id>/sync")
def sync_user_novels(user_id: int):
    # 启动后台任务同步该用户的所有小说
```

### 3. 前端页面

#### 小说页面 (dashboard_novels.html)

- 标签改为：全部 | 收藏 | 追更
- "追更"标签下按系列分组显示
- 系列项显示：系列标题、作者、章节数、最新更新时间
- 点击系列跳转到系列详情页

#### 系列详情页 (dashboard_series_detail.html) - 新增

- 顶部：系列标题、简介、作者、封面
- 下方：章节列表（按顺序）
- 点击章节跳转到小说详情页

#### 关注页面 (dashboard_follows.html)

- 用户列表增加状态标识（彩色徽章）
- 点击用户跳转到用户详情页

#### 用户详情页 (dashboard_user_detail.html) - 新增

- 顶部：用户头像、名称、账号、状态标识
- 统计：小说数量、最后同步时间
- 操作：检查状态、备份全部小说
- 下方：该用户的小说列表（分页）

### 4. 用户状态检查逻辑

```python
def check_user_status(api, user_id: int) -> str:
    """检查 Pixiv 用户状态"""
    try:
        result = api.user_detail(user_id)
        if result is None:
            return 'suspended'
        user = getattr(result, 'user', None)
        if user is None:
            return 'suspended'
        # 检查是否有作品
        novels = getattr(result, 'novels', [])
        if not novels:
            return 'cleared'
        return 'normal'
    except Exception:
        return 'unknown'
```

## Data Models

### 状态枚举

```python
USER_STATUS = {
    'normal': '正常',
    'suspended': '封号',
    'cleared': '资源清空',
    'unknown': '未知',
}
```

### 系列数据结构

```python
@dataclass
class SeriesInfo:
    series_id: int
    title: str
    description: str
    user_id: int
    author_name: str
    cover_url: str | None
    total_novels: int
    novels: list[dict]  # 该系列的小说列表
```

## Error Handling

1. **Pixiv API 调用失败**: 返回 `unknown` 状态，显示重试按钮
2. **用户不存在**: 显示"用户不存在"提示
3. **同步失败**: 显示错误信息，保留已同步的数据
4. **网络超时**: 自动重试 3 次后失败

## Test Strategy

1. 测试小说分类筛选功能
2. 测试系列详情页显示
3. 测试用户状态检查（正常/封号/清空）
4. 测试用户详情页和小说列表
5. 测试用户小说同步功能

## Implementation Steps

### Phase 1: 数据库变更
1. 修改 users 表添加 status 字段
2. 创建 series 表
3. 修改 storage_db.py 添加新查询方法

### Phase 2: 后端 API
1. 修改小说列表 API 支持新分类
2. 添加系列详情 API
3. 添加用户相关 API
4. 添加用户状态检查 API

### Phase 3: 前端页面
1. 修改小说页面标签
2. 创建系列详情页
3. 修改关注页面添加状态标识
4. 创建用户详情页

### Phase 4: 同步功能
1. 实现用户小说同步逻辑
2. 添加同步进度展示

## References

- [storage_db.py](src/pixiv_novel_sync/storage_db.py) - 数据库访问层
- [webapp.py](src/pixiv_novel_sync/webapp.py) - Flask 应用
- [sync_engine.py](src/pixiv_novel_sync/sync_engine.py) - 同步引擎

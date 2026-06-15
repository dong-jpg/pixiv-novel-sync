# 巨型文件拆分计划

> 创建日期：2026-06-15  
> 状态：规划中  
> 目标：将 storage_db.py (3742行)、webapp.py (3011行)、sync_engine.py (1905行) 拆分为可维护的模块

## 1. storage_db.py 拆分方案（3742 行 → 多模块）

### 1.1 拆分维度分析

当前 `Database` 类职责过多，包含：
- 连接管理（50行）
- Schema 初始化与迁移（~800行）
- 核心 CRUD（用户/小说/系列/收藏，~600行）
- 任务日志（~200行）
- Sync Check 表（~100行）
- AI 写作工作台（~1200行）
- 推荐系统（~300行）
- 待删除项管理（~200行）
- 阅读进度（~50行）

### 1.2 拆分结构

```
src/pixiv_novel_sync/storage/
├── __init__.py           # 导出 Database facade
├── connection.py         # Database 连接管理 + transaction context
├── schema.py             # init_schema + 所有迁移方法
├── novels.py             # Novel/NovelText/Asset/Source CRUD
├── users.py              # User CRUD + following
├── series.py             # Series + subscribed_series CRUD
├── bookmarks.py          # Bookmark + sync_check 操作
├── tasks.py              # Task logs CRUD
├── pending_deletions.py  # Pending deletions + watermarks
├── reading_progress.py   # Reading progress CRUD
└── ai/
    ├── __init__.py
    ├── writing.py        # AI writing projects/chapters/foreshadows/states
    ├── jobs.py           # AI jobs + documents
    ├── profiles.py       # AI style/novel profiles
    ├── templates.py      # AI prompt templates
    └── chat.py           # AI chat sessions/messages
```

### 1.3 重构策略

**Phase 1: 提取辅助类（无依赖）**
- `_LazyNovelMembership` → `storage/utils.py`

**Phase 2: 连接层独立**
- `Database.__init__`, `conn`, `_transaction_depth`, `transaction()`, `close()` → `storage/connection.py`
- 创建 `DatabaseConnection` 基类

**Phase 3: Schema 层独立**
- 所有 `init_schema`, `_migrate_*`, `_rebuild_*` 方法 → `storage/schema.py`
- 创建 `SchemaManager` mixin

**Phase 4: 业务层拆分（按领域）**
- 每个领域一个 mixin 类
- `Database` 继承所有 mixin + `DatabaseConnection`

**Phase 5: 测试迁移**
- 更新 `tests/test_storage_db.py` 导入路径
- 确保所有测试通过

### 1.4 兼容性保证

```python
# storage/__init__.py
from .connection import DatabaseConnection
from .novels import NovelsMixin
from .users import UsersMixin
# ...

class Database(
    NovelsMixin,
    UsersMixin,
    SeriesMixin,
    # ...
    DatabaseConnection
):
    """向后兼容的 Database facade。"""
    pass

__all__ = ["Database"]
```

---

## 2. webapp.py 拆分方案（3011 行 → 多模块）

### 2.1 拆分维度分析

当前包含：
- AutoSyncScheduler（~250行）
- SyncJobManager（~500行）
- SettingsManager（~150行）
- Flask 路由（~2000行，100+ 路由）
- 认证/CSRF/安全中间件（~100行）

### 2.2 拆分结构

```
src/pixiv_novel_sync/webapp/
├── __init__.py           # 导出 create_app()
├── app.py                # Flask app 创建 + 中间件注册
├── auth.py               # 认证/CSRF/安全中间件
├── settings_manager.py   # SettingsManager
├── sync_scheduler.py     # AutoSyncScheduler
├── sync_jobs.py          # SyncJobManager
└── routes/
    ├── __init__.py
    ├── auth_routes.py        # /auth/*, /oauth/*
    ├── dashboard_routes.py   # /dashboard/* 主页/状态
    ├── novels_routes.py      # /api/novels/*, /api/reading_progress/*
    ├── series_routes.py      # /api/series/*
    ├── users_routes.py       # /api/users/*
    ├── settings_routes.py    # /api/settings/*
    ├── tasks_routes.py       # /api/tasks/*, /api/logs/*
    ├── pending_routes.py     # /api/pending_deletions/*
    ├── ai_routes.py          # /api/ai/* (由 ai_web.py 提供)
    └── export_routes.py      # /api/export/*
```

### 2.3 重构策略

**Phase 1: 提取管理器类**
- `AutoSyncScheduler` → `webapp/sync_scheduler.py`
- `SyncJobManager` → `webapp/sync_jobs.py`
- `SettingsManager` → `webapp/settings_manager.py`

**Phase 2: 提取认证/中间件**
- `_check_auth`, `_add_security_headers`, `_get_csrf_token` 等 → `webapp/auth.py`

**Phase 3: 路由按领域拆分**
- 每个领域一个 Blueprint
- 使用 `@bp.route()` 注册路由

**Phase 4: 主 app 组装**
```python
# webapp/app.py
def create_app(settings_manager: SettingsManager, ...) -> Flask:
    app = Flask(__name__)
    
    # 注册中间件
    register_auth_middleware(app)
    
    # 注册 Blueprint
    from .routes import (
        auth_bp, dashboard_bp, novels_bp, 
        series_bp, users_bp, settings_bp,
        tasks_bp, pending_bp, export_bp
    )
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    # ...
    
    return app
```

**Phase 5: 向后兼容**
- 原 `webapp.py` 保留为 facade：
  ```python
  from .webapp import create_app
  # ... 原有全局变量和启动逻辑
  ```

---

## 3. sync_engine.py 拆分方案（1905 行 → 多模块）

### 3.1 拆分维度分析

当前 `BookmarkNovelSyncService` 包含：
- 收藏存在性检查（~100行）
- 收藏同步（~150行）
- 关注列表同步（~100行）
- 关注小说同步（~200行）
- 追更系列同步（~450行）
- 待删除项检测（~300行）
- 小说下载核心逻辑（~400行）
- 资产下载（~100行）
- 工具方法（~100行）

### 3.2 拆分结构

```
src/pixiv_novel_sync/sync/
├── __init__.py               # 导出 BookmarkNovelSyncService
├── service.py                # 主服务类 facade
├── bookmarks.py              # 收藏同步 + 存在性检查
├── following.py              # 关注列表 + 关注小说同步
├── series.py                 # 追更系列同步
├── detection.py              # 待删除项检测（novels + series）
├── novel_downloader.py       # _sync_novel + _sync_novel_inner
├── asset_downloader.py       # _download_and_record_assets
└── utils.py                  # _empty_stats, _merge_stats, rate_limit decorator
```

### 3.3 重构策略

**Phase 1: 提取无依赖工具**
- `rate_limit` decorator → `sync/utils.py`
- `_empty_stats`, `_merge_stats` → `sync/utils.py`

**Phase 2: 提取下载器**
- `_sync_novel`, `_sync_novel_inner` → `sync/novel_downloader.py`
  - 创建 `NovelDownloader` 类
- `_download_and_record_assets` → `sync/asset_downloader.py`
  - 创建 `AssetDownloader` 类

**Phase 3: 领域拆分**
- 收藏相关 → `sync/bookmarks.py` (`BookmarksSyncMixin`)
- 关注相关 → `sync/following.py` (`FollowingSyncMixin`)
- 系列相关 → `sync/series.py` (`SeriesSyncMixin`)
- 检测相关 → `sync/detection.py` (`DetectionMixin`)

**Phase 4: 组合 Facade**
```python
# sync/service.py
class BookmarkNovelSyncService(
    BookmarksSyncMixin,
    FollowingSyncMixin,
    SeriesSyncMixin,
    DetectionMixin,
):
    def __init__(self, ...):
        self.api = api
        self.db = db
        self.storage = storage
        self.settings = settings
        self.sync_check_scope = sync_check_scope
        self.rate_limiter = RateLimiter(...)
        self.novel_downloader = NovelDownloader(...)
        self.asset_downloader = AssetDownloader(...)
```

**Phase 5: 向后兼容**
```python
# sync_engine.py (保留)
from .sync import BookmarkNovelSyncService
__all__ = ["BookmarkNovelSyncService"]
```

---

## 4. 实施优先级

### Phase A: storage_db.py 拆分（最高优先级）
- **理由**：最大（3742行），职责最复杂，AI 模块已有独立性
- **预期收益**：3742行 → ~300行 facade + 10个模块
- **风险**：测试覆盖需同步更新

### Phase B: webapp.py 拆分（中优先级）
- **理由**：第二大（3011行），路由众多但耦合较低
- **预期收益**：3011行 → ~200行 facade + 15个模块
- **风险**：Blueprint 注册需测试

### Phase C: sync_engine.py 拆分（低优先级）
- **理由**：最小（1905行），内部耦合较高
- **预期收益**：1905行 → ~150行 facade + 8个模块
- **风险**：Mixin 间依赖需仔细设计

---

## 5. 验收标准

### 5.1 功能验收
- ✅ 所有现有测试通过（164 passed）
- ✅ 导入路径向后兼容（`from pixiv_novel_sync.storage_db import Database` 仍可用）
- ✅ Web 应用正常启动并响应所有路由
- ✅ 同步任务正常执行

### 5.2 质量验收
- ✅ 每个拆分后的文件 ≤ 500 行
- ✅ 每个类职责单一（遵循 SRP）
- ✅ 无循环依赖
- ✅ 公开 API 无破坏性变更

### 5.3 文档验收
- ✅ 每个新模块包含 docstring
- ✅ 更新 `docs/API.md`（如有必要）
- ✅ 更新 `IMPLEMENTATION_RECORD.md` 记录拆分进度

---

## 6. 实施计划（分批提交）

### Batch 1: storage_db.py Phase 1-2（连接层）
- 提取 `_LazyNovelMembership` → `storage/utils.py`
- 提取连接管理 → `storage/connection.py`
- 测试：`test_storage_db.py` 中的连接/事务测试

### Batch 2: storage_db.py Phase 3（Schema 层）
- 提取所有迁移方法 → `storage/schema.py`
- 测试：`test_storage_db.py` 中的外键测试

### Batch 3: storage_db.py Phase 4（核心业务层）
- 拆分 novels/users/series/bookmarks/tasks
- 测试：对应 CRUD 测试

### Batch 4: storage_db.py Phase 4（AI 业务层）
- 拆分 AI 相关 5 个模块
- 测试：AI 相关测试

### Batch 5: storage_db.py Phase 5（收尾）
- 创建 `Database` facade
- 全量测试回归

### Batch 6-10: webapp.py 拆分（类似流程）

### Batch 11-13: sync_engine.py 拆分（类似流程）

---

## 7. 风险与缓解

### 7.1 风险：测试覆盖不足
- **缓解**：每次拆分前运行全量测试建立基线
- **缓解**：每批次拆分后立即运行回归测试

### 7.2 风险：循环依赖
- **缓解**：严格遵循依赖层级：utils → connection → schema → business
- **缓解**：使用 `TYPE_CHECKING` 打破类型检查循环

### 7.3 风险：性能退化
- **缓解**：拆分后保持方法内联（不增加调用层级）
- **缓解**：拆分前后性能基准对比

### 7.4 风险：破坏现有代码
- **缓解**：保留原文件作为 facade，提供向后兼容导入
- **缓解**：搜索全项目引用，确认无直接内部访问

---

## 8. 下一步行动

1. ✅ 创建本计划文档
2. ⏳ 执行 Batch 1: `storage_db.py` 连接层拆分
3. ⏳ 执行 Batch 2: `storage_db.py` Schema 层拆分
4. ⏳ ...（按计划推进）

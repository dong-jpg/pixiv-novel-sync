# Pixiv Novel Sync 实施记录

> 更新日期：2026-06-15  
> 来源：合并并替代 `REFACTOR_MASTER_PLAN.md`、`REFACTOR_STATUS.md`、`PRIORITY_ROADMAP.md`、`PROJECT_ANALYSIS_REPORT.md`、`SESSION_SUMMARY_20260610.md`、`FINAL_SUMMARY_20260610.md`、`SESSION_REPORT_2026-06-10.md`。  
> 说明：本文记录旧实施方案合并后的当前落地状态。旧方案/状态/会话总结文档已删除，避免继续维护多份互相冲突的进度表。

## 1. 当前代码核实结论

截至 2026-06-15，旧实施方案中列出的明确未完成项已全部处理完成。

### 1.1 已完成事项

- Phase 0 先行止血已完成。
  - 认证绕过修复：已引入可信代理配置和非本地访问限制。
  - AI pipeline 覆盖正文：continue 子步骤会保留既有正文并追加生成结果。
  - stats 翻倍：quick sync 返回独立统计副本，避免 JobRunner 合并时重复计数。
- Phase 1 存储层地基已完成。
  - `Database.conn` 使用 `threading.local` lazy connection。
  - `_transaction_depth` 已 thread-local 化。
  - 删除类方法统一使用 `with self.transaction()`。
  - SQLite 连接启用 `PRAGMA foreign_keys=ON`。
  - `novel_texts`、`assets`、`sources` 增加到 `novels` 的外键和级联删除。
  - `ai_agents.provider_id` 增加到 `ai_providers.id` 的限制型外键。
  - 已增加表重建迁移和 `PRAGMA foreign_key_check` 校验。
- Phase 2 Web 任务队列统一已完成。
  - Web 同步任务统一提交 `JobSpec(source=JobSource.WEB)`。
  - `JobRunner` 统一接收 `params` 并传给任务执行上下文。
  - dashboard sync、bookmark check、subscribed series、user sync、pending deletion detection 等路由已迁移到 shared `JobManager`/`JobRunner`。
  - cancel/status/log/progress 行为统一走 shared job 状态。
- Phase 3 同步引擎健壮性已完成。
  - 用户备份容错、pending deletion grace period、翻页上限、统一限速与 429/网络错误重试已落地。
  - Phase 3.5 hash 增量同步已补完：meta/text hash 未变时跳过不必要的文件、metadata、正文和 FTS 写入；资产缺失时仍会修复资产；last_seen/source 保持更新。
- Phase 4 巨型文件拆分已完成首要目标。
  - `src/pixiv_novel_sync/ai/service.py` 保留为 public facade。
  - AI service 实现拆入 `src/pixiv_novel_sync/ai/services/` 下的 core/admin/generation/projects/chat_wizard 模块。
  - `AIWritingService`、`AIServiceError` 的公开 import 兼容性保留。
- Phase 5 性能优化已完成。
  - 索引补全、`IN (...)` 分批、N+1 查询消除、推荐系列去重和 series length memo、AI retriever 缓存已完成。
  - Phase 5.1 批量事务已补完：sync-check 批量 upsert、资产记录批量写入，网络请求和文件写入不持有 DB transaction。
  - Phase 5.8 AI 连接复用已补完：provider 使用 `requests.Session`，`AIWritingService` 复用 provider 并在 update/delete/close 时关闭缓存。
- Phase 6 前端补全与契约修正已完成。
  - 设置页、AI 创作页按钮/入口、系列头像、正文字段 fallback、job 序列化等已落地。
- Phase 7 推荐/偏好/AI 残余修复已完成。
  - 推荐搜索翻页上限、空页即停、previously_recommended 语义、filter_state 默认值、负向偏好、对数书签打分、长任务后台 job、AI JSON/重试/检索锁等已落地。
- Phase 8 安全加固已完成。
  - 未配置 `DASHBOARD_TOKEN` 时仅允许本机访问；检测到未信任代理头时拒绝。
  - OAuth task/public exchange 响应不返回 refresh/access token 原文。
  - mutating request 增加 session CSRF 校验，提供 `/api/csrf-token`。
  - 登录失败增加基于 client IP 的内存限流。
  - 响应增加 `X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、`Permissions-Policy`。
  - EPUB 下载名和 ZIP entry 使用 `safe_name()` 清理，`safe_name()` 会剥离路径穿越前后缀。
  - AI provider 上游错误和 AI job/stream 错误保持脱敏输出。

### 1.2 当前未完成事项

旧实施方案中列出的明确未完成事项已清零。

### 1.3 模块化拆分（2026-06-15 完成） ✅

**目标**：将巨型文件拆分为可维护的模块。

**最终成果**：
- ✅ **storage_db.py 拆分 100% 完成**
- 从 3742 行减少到 52 行（**-98.6%**）
- 提取 195 个方法到 14 个 mixin 模块
- 所有 164 个测试保持通过
- 5 次提交，全部推送

**批次详情**：

| 批次 | 内容 | 提交 | 减少行数 | 状态 |
|------|------|------|----------|------|
| Batch 1 | 连接层 | `23e5a81` | -61 | ✅ |
| Batch 2 | Schema 层（19 methods） | `4ce7dcc` | -722 | ✅ |
| Batch 3 | 核心业务层（55 methods） | `7ae1732` | -1284 | ✅ |
| Batch 4 | 实用层（13 methods） | `b23cfd6` | -203 | ✅ |
| Batch 4-5 | AI 和推荐层（108 methods） | `d8f5bce` | -1420 | ✅ |
| **总计** | **195 个方法** | **5 次提交** | **-3690 行** | **✅** |

**最终模块结构**：
```
src/pixiv_novel_sync/storage/
├── connection.py              # DatabaseConnection 基类
├── schema.py                  # SchemaMixin (19 methods)
├── utils.py                   # 辅助类
├── novels.py                  # NovelsMixin (26 methods)
├── users.py                   # UsersMixin (9 methods)
├── series.py                  # SeriesMixin (9 methods)
├── bookmarks.py               # BookmarksMixin (6 methods)
├── tasks.py                   # TasksMixin (5 methods)
├── pending_and_watermarks.py  # PendingAndWatermarksMixin (10 methods)
├── reading_progress.py        # ReadingProgressMixin (3 methods)
├── recommendations.py         # RecommendationsMixin (25 methods)
└── ai/
    ├── __init__.py
    ├── core.py                # AiCoreMixin (19 methods)
    ├── documents.py           # AiDocumentsMixin (27 methods)
    └── writing.py             # AiWritingMixin (37 methods)
```

**storage_db.py 最终形态（52 行）**：
```python
class Database(
    NovelsMixin,              # 小说 CRUD
    UsersMixin,               # 用户 CRUD
    SeriesMixin,              # 系列 CRUD
    BookmarksMixin,           # 收藏和同步检查
    TasksMixin,               # 任务日志
    PendingAndWatermarksMixin, # 待删除项和水位线
    ReadingProgressMixin,     # 阅读进度
    RecommendationsMixin,     # 推荐系统
    AiCoreMixin,              # AI providers/agents/jobs
    AiDocumentsMixin,         # AI 文档和配置
    AiWritingMixin,           # AI 创作项目
    SchemaMixin,              # Schema 管理
    DatabaseConnection,       # 连接管理
):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
    
    def export_stats(self) -> str:
        """统计数据导出"""
        ...
```

**技术亮点**：
1. **多继承 Mixin 模式**：清晰的职责分离，每个 mixin 独立可测
2. **零破坏性变更**：所有测试始终通过，100% 向后兼容
3. **渐进式重构**：5 个批次独立提交，风险可控
4. **Ultracode 模式**：利用 Agent 并行提取大量方法

**下一步**：
- ⏳ **Batch 6-10**：`webapp.py` 拆分（3011 行）
- ⏳ **Batch 11-13**：`sync_engine.py` 拆分（1905 行）

详见 `docs/MODULARIZATION_PLAN.md`。

## 2. 历史实施摘要

### 2.1 2026-06-10 性能优化会话

完成内容：

- Phase 5.2 索引补全。
  - `idx_novels_last_seen_at`
  - `idx_recommendation_feedback_author_id`
  - `idx_recommendation_feedback_series_id`
  - `idx_recommendation_feedback_novel_id`
  - `idx_pending_deletions_item_type_status`
- Phase 5.4 `IN (...)` 分批。
  - 避免 SQLite 999 参数限制。
- Phase 5.5 N+1 查询消除。
  - `cleanup_stale_pending` 改批量更新。
  - `list_following_series` 排序子查询改 JOIN/预聚合。
- Phase 5.6 推荐系列去重和 series length memo。
  - 单次推荐运行内避免重复系列。
  - 避免重复调用系列长度 API。

当时验证：测试基线约 145 passed。

### 2.2 2026-06-10 P0/P1 稳定性会话

完成内容：

- Phase 3.4 限速统一与 429 处理。
- Phase 5.7 AI retriever 缓存优化。
- Phase 3.1 用户备份容错。
- Phase 3.2 pending deletion 30 天 grace period。
- Phase 7.6 长任务后台 job。

历史提交记录中相关提交：

- `1b3d469`：限速统一与 429 处理。
- `0429bbb`：AI 检索缓存优化。
- `3ccd78b`：用户备份容错。
- `dcf77fa`：pending deletion grace period。
- `fd74c98`、`9f620e9`：长任务后台 job 后端与前端轮询。
- `7755a22`、`81bc3e7`：状态文档更新。

### 2.3 AI 创作工作台历史状态

AI 创作工作台已完成主体能力：

- Provider/Agent 配置。
- API key 加密存储。
- 续写、改写、草稿、任务历史。
- 风格/小说蒸馏、内容审计、Prompt 模板。
- 写作项目、章节、状态记忆、伏笔。
- 长篇规划、章节详细梗概、章节 pipeline。
- TF-IDF/Embedding 检索和项目上下文预览。
- raw output 重试导入、自动保存、pipeline 状态展示。

保留 `AI_WRITING_STUDIO_PLAN.md` 作为 AI 模块长期产品/历史参考；它不是本次删除对象。

## 3. 本轮补完记录

本轮将旧记录中的 7 项明确未完成事项全部落地：

1. Phase 1.3 外键与级联删除。
2. Phase 2 任务队列完全统一。
3. Phase 3.5 hash 增量同步。
4. Phase 4 AI service 巨型文件拆分。
5. Phase 5.1 批量事务。
6. Phase 5.8 AI 连接复用。
7. Phase 8 安全加固。

新增/更新的重点测试覆盖：

- `tests/test_storage_db.py`
- `tests/test_sync_engine_incremental.py`
- `tests/test_webapp_jobs.py`
- `tests/test_webapp_security.py`
- `tests/test_ai_providers_fallback.py`
- `tests/test_ai_service_facade.py`
- `tests/test_ai_service_provider_cache.py`

## 4. 文档保留与删除策略

### 4.1 已合并并删除

- `docs/REFACTOR_MASTER_PLAN.md`
- `docs/REFACTOR_STATUS.md`
- `docs/PRIORITY_ROADMAP.md`
- `docs/PROJECT_ANALYSIS_REPORT.md`
- `docs/SESSION_SUMMARY_20260610.md`
- `docs/FINAL_SUMMARY_20260610.md`
- `docs/SESSION_REPORT_2026-06-10.md`

### 4.2 保留为长期参考

- `docs/API.md`
- `docs/AI_WRITING_STUDIO_PLAN.md`
- `docs/PREFERENCE_RECOMMENDER_REQUIREMENTS.md`
- `docs/QWEN_EMBEDDING_INTEGRATION.md`
- `docs/frontend-api-contract.md`
- `docs/frontend-pages.md`
- `docs/library-os-style-guide.md`
- `docs/superpowers/**`

## 5. 验证记录

本轮已执行并通过的关键回归：

```bash
python -m pytest tests/test_webapp_security.py::test_security_headers_are_set tests/test_webapp_security.py::test_csrf_required_for_authenticated_mutating_requests tests/test_webapp_security.py::test_login_rate_limit_blocks_repeated_failures tests/test_webapp_security.py::test_safe_name_strips_path_traversal_segments -q
python -m pytest tests/test_webapp_security.py -q
python -m pytest tests/test_webapp_security.py tests/test_webapp_jobs.py -q
python -m pytest tests/test_ai_providers_fallback.py tests/test_ai_web_stream.py -q
```

最终提交前建议再执行：

```bash
python -m pytest -q
```

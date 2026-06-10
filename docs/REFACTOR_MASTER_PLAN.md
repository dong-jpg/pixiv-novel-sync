# Pixiv Novel Sync 修复优化总方案

> 状态：进行中
> 创建日期：2026-06-09
> 用途：记录全量修复优化的分阶段计划，作为后续逐项执行的主线。每项执行时更新进度勾选。

## 组织原则

- **架构重构优先**：先把地基（连接模型、任务队列）做对，再在其上修业务 bug、拆文件、调性能。
- **每个 Phase 结束必须 `pytest` 全绿**（基线 141 passed），作为回归防线。
- **Phase 0 先行止血**：3 个 P0（认证绕过 / AI 覆盖正文 / stats 翻倍）是隔离的单点修复，无重构依赖，但一旦部署即造成隐私泄漏或数据丢失，必须最先做。
- 拆分巨型文件用「移动 + re-export」保持公共 import 路径不变，前端与外部调用零感知。

## 基线

- 测试基线：`pytest` → 141 passed（2026-06-09 确认）。
- 巨型文件：`ai/service.py` 3504 行、`storage_db.py` 3416 行、`webapp.py` 2889 行。
- `storage_db.py` 中 `self.conn` 引用 264 处 → 连接模型改造用 `@property` 实现零调用点改动。

---

## Phase 0 — 先行止血（隔离 P0）

- [ ] 0.1 认证绕过（`webapp.py:1498-1518`）：`_is_local_request` 解析可信代理链；未配置 `dashboard_token` 时启动期 WARNING 且非 localhost 一律 403；新增 `TRUSTED_PROXY` 配置项控制是否信任 `X-Forwarded-For`。补 `test_webapp_security.py`。
- [ ] 0.2 AI pipeline 覆盖正文（`ai/service.py:3036-3045`）：continue 子步骤保存 `existing_content + step_output`，与单步 `stream_chapter_continue` 对齐。补回归测试。
- [ ] 0.3 stats 翻倍（`jobs/quick_sync.py:102-118`）：返回 `dict(check_stats)` 独立副本，移除函数内对 `JobState.status/finished_at` 的直接赋值（交还 JobRunner）。补 `test_jobs_runner.py`。

---

## Phase 1 — 存储层架构（地基）

- [ ] 1.1 连接模型重构（`storage_db.py:13-26`）：`threading.local` 每线程连接，`self.conn` 改 `@property` lazy 建连 + PRAGMA；`_all_conns` 弱引用集合供 `close()` 关闭全部线程连接；`_transaction_depth` thread-local 化。
- [ ] 1.2 事务统一：4 个手写 `BEGIN IMMEDIATE` 删除方法（`delete_novel/user/series/ai_writing_project`，`:1219-1273/2994`）改用 `with self.transaction()`。
- [ ] 1.3 外键 + 级联：`PRAGMA foreign_keys=ON`；从表加 `FOREIGN KEY ... ON DELETE CASCADE`（新表迁移；FTS 用触发器）。
- [ ] 1.4 FTS 原子化：`upsert_novel` + `replace_fts` 同事务，或建 FTS 同步触发器。
- [ ] 1.5 回归：`test_archive_integrity.py` 扩充并发读写用例。

---

## Phase 2 — 统一任务队列（地基）

- [ ] 2.1 以 `jobs/` 的 `JobManager`/`JobRunner`/`JobSpec` 为唯一队列，`SyncJobManager` 逐步退役（保留旧 API 响应形状）。
- [ ] 2.2 信号量收口：acquire/release 仅在 `JobRunner` 一处。
- [ ] 2.3 接通取消：cancel endpoint → `request_cancel`；`stop_requested` 贯穿 `service.sync`/`run_bookmark_sync`，资源下载循环与 `time.sleep` 加细粒度检查。
- [ ] 2.4 调度器锁（`webapp.py:158-260`）：`_task_next_run` 全部读写纳入 `_lock`；跳过分支也更新 next_run。
- [ ] 2.5 回归：`test_jobs_*`、`test_webapp_jobs.py` 扩充取消/并发用例。

---

## Phase 3 — 同步引擎健壮性

- [ ] 3.1 用户备份容错（`jobs/services.py:91-94`）：单本失败累计，超阈值再中止，保留已同步部分。
- [ ] 3.2 误删防护（`sync_engine.py:1252-1281`）：加「远端/本地数量骤降比例」阈值。
- [ ] 3.3 翻页上限：`sync_following_list`（`:480`）、系列章节（`:1003`）补 `max_pages` 兜底。
- [ ] 3.4 限速统一 + 429：统一失败是否计入配额口径；API 异常区分 429 加指数退避重试。
- [ ] 3.5 hash 增量：读旧 `meta_hash`/`text_hash` 比对，未变更跳过写盘写库。

---

## Phase 4 — 拆分巨型文件

手法：纯移动 + re-export，每拆一个领域跑全测。

### 4A. `ai/service.py` → `ai/services/` 包
- [ ] provider_service / draft_service / distill_service / project_service / planning_service / pipeline_service / chat_service / context_service
- [ ] `AIWritingService` 退化为聚合门面；`ai/service.py` re-export。先拆 pipeline 和 project。

### 4B. `storage_db.py` → `storage/` mixin
- [ ] base / novels_repo / users_series_repo / logs_repo / pending_repo / recommend_repo / ai_repo
- [ ] `Database(NovelsRepo, ...)` 多继承组合 + re-export。

### 4C. `webapp.py` → `web/` Blueprint
- [ ] auth / scheduler / oauth_routes / dashboard_routes / api_routes / serializers
- [ ] 统一 `_job_to_dict` 与 `_shared_job_to_dict` 两套序列化。

---

## Phase 5 — 性能优化

- [ ] 5.1 批量事务：同步主循环每本小说 upsert 包单事务；多本批处理。
- [ ] 5.2 索引补全：`idx_novels_last_seen_at`、`recommendation_feedback(author_id/series_id/novel_id)`、`pending_deletions(item_type,status)`。`EXPLAIN QUERY PLAN` 验证。
- [ ] 5.3 推荐过滤去全表载入（`storage_db.py:1863-1874`）：改 `EXISTS`/`IN`。
- [ ] 5.4 `IN (...)` 分批（`list_novel_archive_refs:628`）：复用 500 批处理。
- [ ] 5.5 N+1 消除：`cleanup_stale_pending` 单条 UPDATE；`list_following_series` 排序子查询改 JOIN。
- [ ] 5.6 推荐系列去重 + `_series_length` memo。
- [ ] 5.7 AI 检索：TF-IDF 向量/范数 index 时预存；embedding query 短期缓存。
- [ ] 5.8 AI 连接 churn：pipeline 内复用连接（配合 Phase 1 thread-local）。

---

## Phase 6 — 前端补全与契约修正

设置页：
- [ ] 6.1 手动同步加 `subscribed_series` 触发按钮。
- [ ] 6.2 基础设置加 `sync_subscribed_series` 开关。
- [ ] 6.3 加「导出统计」入口（`/api/dashboard/export/stats`）。

AI 创作页：
- [ ] 6.4 章节单步 `extract-summary/stream`、`polish/stream` 独立触发按钮。
- [ ] 6.5 伏笔 `auto-resolve/stream` 正向触发。
- [ ] 6.6 `jobs/cleanup` 清理按钮；风格/小说档案 `PUT` 编辑；`documents/manual` 手动录入。

字段契约修正：
- [ ] 6.7 `list_following_series` 系列卡头像兜底 / 后端补头像字段。
- [ ] 6.8 `dashboard_novel_detail.html` 正文 fallback `.markdown` → `.text_markdown`。
- [ ] 6.9 统一两套 job 序列化（并入 4C serializers）。

---

## Phase 7 — 推荐/偏好/AI 残余修复

- [ ] 7.1 推荐搜索翻页上限 + 空页即停（`recommendations.py:98`）。
- [ ] 7.2 `previously_recommended` 空集回退语义修正（`:124`）。
- [ ] 7.3 `filter_state` 取值统一 `.get(..., set())`。
- [ ] 7.4 `exclude_terms` 真正消费或移除死字段。
- [ ] 7.5 打分模型：打分/过滤共用阈值；书签对数归一化；接入负向偏好。
- [ ] 7.6 长任务（analyze/run）改后台 job。
- [ ] 7.7 AI：`_get_retriever` 加锁；`_extract_json_object` 括号配平；`stream_chat` 校验前置；尊重 `max_retries`；Anthropic base_url 解析统一。

---

## Phase 8 — 安全加固

- [ ] 8.1 CSRF：状态变更接口加 token。
- [ ] 8.2 AI job 参数不落原文（`create_ai_job` 只存长度/摘要）。
- [ ] 8.3 收窄裸 `except: pass` 为 `sqlite3.OperationalError` + 日志。
- [ ] 8.4 偏好/推荐 API 纳入统一鉴权。

---

## 验收

- 每个 Phase 独立分支 → 跑 `pytest`（基线 141）→ 合入。
- Phase 1/2 为后续所有改动的地基，务必先行且测试加厚。
- 工作量估算：约 19-26 个工作日。

# AI 写作模块拆分 + 2026-09-14 审计整改 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把尚不成熟的 AI 写作模块（项目 / 章节 / 向导 / 蒸馏 / 成人润色 / 检索）从 `main` 剥离到 `ai-writing` 分支，`main` 只保留偏好关键词清洗所需的 AI 基础设施；随后按优先级修掉 [AUDIT_REPORT_2026-09-14.md](../../AUDIT_REPORT_2026-09-14.md) 的全部发现。

**Architecture:** 六个阶段，阶段 0 必须先做并单独提交，之后 `main` 上的修复经 `git merge main` 流入 `ai-writing`；写作 / 成人专属修复只在 `ai-writing` 做。每个任务标注落地分支。任务编号 `T<阶段>-<序号>`，验收时按编号逐条核对。

**Tech Stack:** Python 3.11 / Flask（注册函数，非 blueprint）/ SQLite（mixin 组合的 `Database`）/ Vue 3 CDN + Jinja（定界符 `{[ ]}`）/ pytest。

**Source:** `docs/AUDIT_REPORT_2026-09-14.md`（每条任务的证据行号都在报告里，本计划只写「改什么、怎么验」）。

## Global Constraints

- 变更类前端请求一律 `window.csrfFetch`，错误文案一律 `window.errorText`；不得自建 `ensureCsrfToken` 或手拼 `X-CSRF-Token`。
- Jinja 变量定界符 `{[ ]}`，`{{ }}` 归 Vue。模块首行 `from __future__ import annotations`；dataclass `slots=True`。注释与文案中文。Commit `type: subject`。
- 成人润色子系统是 fail-closed 的：本计划里成人任务全部是「该通过的没通过」或「恢复非同构」，**不得放宽任何校验**。
- 存储迁移只加不减：不写任何 `DROP TABLE` / `DROP COLUMN`，`main` 与 `ai-writing` 必须能打开同一个生产库。
- 每个任务完成后 `python -m compileall -q src tests`；每个阶段结束跑全量 `pytest -q`（`main` 基线 1492 passed / 4 skipped，阶段 0 后会减少）。
- 修 bug 时同步补回归测试；已有测试把��误行为钉死的（`tests/test_webapp_jobs.py:112`、`tests/test_frontend_library_os.py:395-407`）要一起改断言方向。
- 分支纪律：阶段 0 之后 **基础设施修复只在 `main` 做**，定期 `git checkout ai-writing && git merge main`；写作 / 成人修复只在 `ai-writing` 做，永不反向合并。

---

## 阶段 0：拆分（分支：main 删、ai-writing 保全）

现状：`ai-writing` 已建在 `b579fac`，游离工作树已删。工作树里 `token_login.html` 的改动属于 T1-01，先提交它（两分支都要）再开始本阶段。

### 0.1 依赖边界（已核实，照此切）

`main` 上非写作代码对 AI 的引用只有：`jobs/tasks.py:355-359`（`AIWritingService.clean_keywords`，保留）、`storage/schema.py:702`（`migrate_model_routing_schema`，保留）、`storage/schema.py:993-994`（成人迁移，删）、`storage/tasks.py:7` 与 `webapp.py:1756-1762`（成人日志过滤，删）。

`clean_keywords` 依赖链：`generation.py:766-830` → `generation.py:26-117` 三个私有 helper → `core._start_route_job` → `prompts.build_keyword_clean_messages`（`prompts.py:929-963`）→ `admin._load_agent_config`。不依赖 retrieval / chunking / preference_context / projects。

`ai_jobs.owner_scope` / `idempotency_key_hash` 由成人迁移添加（`schema.py:1044-1045`），`storage/ai/core.py:865,941,1460,1482` 的通用查询引用它们。**两列保留**，挪进 `_migrate_ai_tables`。

### 0.2 任务

- [ ] **T0-01 提交工作树、确认分支拓扑**
  `git add src/pixiv_novel_sync/templates/token_login.html && git commit -m "fix: 登录页按 has_refresh_token 判定并走 /oauth/save 落盘"`；`git checkout ai-writing && git merge --ff-only main && git checkout main`。验收：两分支 HEAD 相同。

- [ ] **T0-02 删除整文件（main）**
  - `ai/services/{generation,projects,chat_wizard,adult}.py`
  - `ai/{retrieval,chunking,detection,preference_context,adult_auth,adult_policies,adult_prompt,adult_types,adult_validation}.py`
  - `storage/ai/{documents,writing,adult}.py`
  - 模板：`dashboard_ai_{projects,project,chapters,notes,reader,project_nav,pipeline_modal,output_panel,source_search}.html`、`dashboard_wizard.html`、`dashboard_settings_adult.html`
  - 测试：`tests/ai_adult_testkit.py`、`tests/test_ai_adult_*.py`（13 个）、`test_ai_prompts.py`、`test_ai_retrieval.py`、`test_ai_page_routes.py`、`test_ai_import_atomicity.py`、`test_ai_project_covers.py`、`test_style_control.py`、`test_ai_service_parsing.py`、`test_ai_web_int_parsing.py`、`test_ai_service_stream_continue.py`、`test_ai_web_stream.py`、`test_ai_multibatch_routing.py`
  - 文档：`docs/AI_WRITING_STUDIO_PLAN.md`、`docs/ADULT_POLISH_USER_GUIDE.md`、`docs/QWEN_EMBEDDING_INTEGRATION.md`；`docs/superpowers/` 下 `2026-07-16-ai-cover-style-controls`、`2026-07-16-ai-page-layout-refactor`、`2026-07-17-ai-project-overview-single-panel*`、`2026-07-23-adult-polish-agent*`、`2026-08-14-ai-preference-adult-remediation`、`2026-09-02-dashboard-ai-page-split-design`（模型目录 / 池 / 路由 / 设置页可操作性的 spec 与 plan 保留）

- [ ] **T0-03 新建 `ai/services/keyword_clean.py`**
  `AIKeywordCleanMixin`：原样搬 `generation.py` 的 `_forward_route` / `_route_result_message` / `_conclude_route`（:26-117）与 `clean_keywords`（:766-830）。`ai/prompts.py` 瘦身为 `DEFAULT_KEYWORD_CLEAN_PROMPT` + `build_keyword_clean_messages`（:929-963）+ `safe_prompt_preview`（:228，若 admin 仍用）。

- [ ] **T0-04 改 `ai/service.py` 与 `ai/services/__init__.py`**
  `AIWritingService(AIKeywordCleanMixin, AIAdminMixin, AIServiceCore)`，类名不改（`jobs/tasks.py` 与 8 个测试引用它，也减少日后合并冲突），docstring 注明「main 上只承载关键词清洗」。`__init__` 去掉四个写作 mixin 与 `AdultRouteRequest` / `PreparedAdultJob` 导出。

- [ ] **T0-05 改 `ai/services/core.py`**
  删 `_get_retriever`（:92-126）、`_resolve_preference_context` / `_preference_project` / `_fit_preference_messages`（:136-252）、`RouteJobContext.preference_context`、`_start_route_job` 的 `preference_payload` / `preference_project` 参数与 :358-374 注入、`_stream_route` :586-595 注入、retrieval / preference_context 导入、`close()` 的 retriever 关闭。

- [ ] **T0-06 改 `ai/services/admin.py`**
  删 `create_document`、drafts 四个、`_resolve_input_text`、style / novel profile 各五个、prompt template 五个 + `seed_builtin_templates`、`get_draft_history` / `fork_draft`（:1258-1306, 1519-1554, 1590-1742, 1902-1929）。`seed_builtin_agents`（:1743-1900）只留 `general` 与 `keyword_clean`。`_normalize_agent_payload` :1404 白名单缩为 `{None, "general", "keyword_clean"}`。`update_agent_bindings` :1221-1229 去掉成人拒绝。删 `ADULT_AI_TASK_TYPES` 与五个写作 `DEFAULT_*_PROMPT` 导入（:40-50）。`stream_job_with_next_model` 保留。

- [ ] **T0-07 改存储层**
  - `storage/ai/core.py`：删 `_ADULT_AI_TASK_TYPES_SQL` 及四处 WHERE 片段（:865,941,1460,1482），删 `request_adult_job_cancel` / `bind_adult_application_access`（:1497-1609），`ADULT_AI_TASK_TYPES` 常量整个删掉。
  - `storage/tasks.py`：删 :7-11 与 :253。
  - `storage_db.py` / `storage/ai/__init__.py`：去掉 `AiDocumentsMixin` / `AiWritingMixin` / `AdultStorageMixin`。
  - `storage/schema.py`：删 `_migrate_ai_writing_tables`（:878-990）、`_migrate_adult_polish_tables`（:991-1198）及 `init_schema` :161/:164 的调用；`_migrate_ai_tables` 删 `ai_drafts` / `ai_documents` / `ai_style_profiles` / `ai_novel_profiles` / `ai_prompt_templates` 建表（:755-809）与 :814-816 三个索引；**新增** `owner_scope` / `idempotency_key_hash` 两个带 `PRAGMA table_info` 守卫的 `ADD COLUMN`。

- [ ] **T0-08 改 `ai_web.py`**
  只保留 :745-1015（health / probe / providers / model-sync / provider-models / model-pools / agents / bindings）、:1568-1648（jobs list / get / continue / cleanup）、:1874（agents/seed）。删页面路由 :714-744、成人全部、documents、四个裸 `*/stream`、drafts、distill、profiles、detect、prompt-templates、series/search、projects / chapters / notes / states / foreshadows / retrieval / chat / pipeline；删对应 helper（`_safe_ai_cover_*`、`_validated_ai_cover`、`adult_*`、`_require_project`、`stream_response`）与导入 :25-50 中的写作项。

- [ ] **T0-09 改 `webapp.py`**
  `_SETTINGS_PAGES`（:944）去掉 `adult`；`/api/dashboard/logs` :1755-1770 去掉成人 owner 判定，`category == "ai"` 直接 `owner_scope = ""` 或让 `get_ai_task_logs` 不再接收该参数。

- [ ] **T0-10 改模板**
  `vue_components.html` 删 :24-25 与 :39；`dashboard_settings_nav.html` 删 :11；`dashboard_novels.html` 删 `ai` tab（:60-76, :124, :410, :485-500）与 `/dashboard/novels/ai/` 链接；`base.html` `aiApi` 删 `styleProfiles` / `novelProfiles`（:305-312）；`dashboard_logs.html` `AI_TASK_OPTIONS`（:398 附近）与 :692 标签表只留 `keyword_clean`，`RESUMABLE_TASK_TYPES` 同步；`dashboard_settings_agents.html` task_type 两处下拉（:42-54, :100-111）只留 general / keyword_clean，删全部 adult 过滤；`dashboard_ai_health_band.html` 保留。

- [ ] **T0-11 改混合测试**
  `test_frontend_library_os.py`（删 AI 三页 / 向导 / 成人页存在性断言）、`test_ai_model_router_integration.py`（AST 守卫改为遍历 `ai/services/` 现存文件，删依赖写作方法的用例）、`test_settings_sections.py`（`_SETTINGS_PAGES` 去 `adult`）、`test_unified_task_logs.py` / `test_webapp_security.py` / `test_ai_model_ui.py` / `test_preferences.py` / `test_ai_agent_batch_bindings.py` / `test_ai_web_stream_response.py`（各 1–5 处）。`test_ai_model_docs.py` 读 `docs/frontend-pages.md`，删节后确认被断言字符串仍在。

- [ ] **T0-12 改文档**
  `README.md` 删「AI 创作」（:120）「成人本地润色 Agent」（:134）两节，补「AI 写作模块在 `ai-writing` 分支」。`docs/frontend-pages.md` 删 :268-320、:368 起 AI 节；`docs/frontend-api-contract.md` 删 :786-823、:824-861 写作端点、:882 起 SSE / longform / chat 节。`docs/INDEX.md`、`docs/UNIFIED_PROJECT_REQUIREMENTS.md`、`CLAUDE.md`（AI subsystem / Adult / Frontend 三节）同步删节，`CLAUDE.md` 顶部注明分支分工。`docs/AUDIT_REPORT_2026-09-14.md` §0 加一行「AI 创作与成人润色条目在 `ai-writing` 分支处理」。

- [ ] **T0-13 提交并标记合并**
  `main` 上提交 `chore: 剥离 AI 写作模块到 ai-writing 分支`。然后 `git checkout ai-writing && git merge -s ours main -m "merge: 记录 main 的写作模块剥离，不生效" && git checkout main`。**没有这一步，以后 `git merge main` 会把写作模块当删除合并掉。**

### 0.3 验收（我来做）

- `python -m compileall -q src tests`；`pytest -q` 全绿。
- `pytest tests/test_keyword_clean.py tests/test_preferences.py tests/test_ai_model_docs.py tests/test_settings_sections.py` 单独通过。
- Web：侧栏无「AI 创作 / 创作向导 / 成人润色」；`/dashboard/settings/{sync,models,agents,system}` 200，`/dashboard/settings/adult`、`/dashboard/ai`、`/dashboard/wizard` 404；小说库只有收藏 / 追更 / 拯救三个 tab；Agent 页 task_type 下拉两项；`/api/dashboard/ai/health` 200；日志页 AI 类别可筛 `keyword_clean`。
- 用含写作表的生产库副本跑 `Database(path).init_schema()` 不报错，`sqlite3 .tables` 写作表仍在。
- `git log --graph --oneline main ai-writing -6` 拓扑正确；在 `ai-writing` 上 `git merge main` 为 up to date。

---

## 阶段 1：main 止血（P0 / P1，各几行到几十行）

- [x] **T1-01 登录页 Token 落盘**（进行中，工作树已改）
  `token_login.html:182,221,251` 改判 `has_refresh_token`，兑换成功调 `POST /oauth/save/<id>`。验收：补 `tests/test_webapp_security.py` 用例 grep 模板不含 `data.refresh_token`；手动走「粘贴回调 URL」流程 `.env` 出现 `PIXIV_REFRESH_TOKEN`。

- [ ] **T1-02 调度器 stop / submit 竞态**
  `web/managers.py:802-818`：`stopped_during_submit` 分支仍调用 `run_task`（runner 会立即 `mark_cancelled`），或直接 `shared_job_manager.mark_cancelled(job_id)` 并回写 `task_logs` 为 `cancelled`。改 `tests/test_webapp_jobs.py:112` 的断言（现钉死为只 cancel）。验收：新用例「stop 后 submit 一次 → `_has_active_shared_jobs()` 为 False，task_log 终态 cancelled」。

- [ ] **T1-03 `_submit_shared_job` 幽灵 QUEUED**
  `webapp.py:578-595`：先 `create_task_log` 再 `submit`；或 except 里 `mark_failed`。顺手把 DB IO 挪出 `shared_job_manager._lock`。验收：mock `create_task_log` 抛异常后 `_has_active_shared_jobs()` 为 False。

- [ ] **T1-04 `following_novels` 取消不落水位**
  `sync_engine.py:1164-1175` 作者循环包 `try/finally: _save_watermark()`。验收：用例「第 2 个作者抛 `InterruptedError` → `sync_watermarks.following_novels` 含第 1 个作者时间戳」。

- [ ] **T1-05 `cleanup_stale_pending` 零命中不提交**
  `storage/pending_and_watermarks.py:172-192` 无条件 `_commit_if_needed()`。验收：用例「0 行后 `conn.in_transaction` 为 False」。

- [ ] **T1-06 Provider / Agent 的 `or 默认值`**
  `storage/ai/core.py:220-223,370-372`、`ai/services/admin.py:1487-1490,1506-1509` 改 `default if x is None else x`。验收：`create_ai_provider({"max_retries":0})` → `_load_provider_config().max_retries == 0`；`temperature=0` 同理。

- [ ] **T1-07 `localhost` opt-in 被拒**
  `ai/providers.py:93-111 _is_blocked_ip` 先判 `is_loopback`（`allow_private` 放行）再判 `is_reserved`；`_resolve_target :177-181` 对回环主机名放行。验收：`PIXIV_AI_ALLOW_PRIVATE_HOSTS=1` 下 `validate_base_url("http://localhost:11434", resolve=True)` 与 `http://[::1]:11434` 通过，`http://169.254.169.254` 仍拒。

- [ ] **T1-08 Anthropic `/v1/v1/messages`**
  `ai/providers.py:1060-1065` 提炼 `_resolve_base_url()`，`:1094-1095` 生成路径共用。验收：`base_url` 以 `/v1` 结尾时 discovery 与 generate URL 前缀一致。

- [ ] **T1-09 推荐候选级异常 + 全失败判黄**
  `recommendations.py:116` 包 `try / except InterruptedError: raise / except Exception: errors += 1; continue`；`_series_length` 失败返 `(0, 0)`；`search_novel` / `novel_series` 加 `retry_on_pixiv_error`。`:132-135` 前：`errors > 0` 置顶层 `incomplete=True` + `aborted_reason="search_errors"`；`searched > 0 and errors == searched` 记 `failed` 并抛。验收：用例「一个候选 `novel_series` 抛 `PixivError` → run succeeded 且 items 含其余候选」「全部 search 抛 → task_log partial/failed」。

- [ ] **T1-10 日志页两处**
  `dashboard_logs.html:369-370` 下拉值改 `bookmark` / `following_users`；`:642-645 formatDate` 用 T2-20 的公共 `formatDbTime`（先本地补 `Z`）。验收：选「收藏同步」有结果；Safari / Chrome 时间与首页一致。

- [ ] **T1-11 首页状态条数据源**
  `dashboard.html:255-265` 以 `autoSyncStatus.current_job` 为准，或 `latestJob` 非 running 时不带 `job_id`。验收：调度器起新任务后 3 s 内状态条显示任务名。

- [ ] **T1-12 收藏卡片头像 / R-18**
  `storage/bookmarks.py:54-72` 补 `n.x_restrict, u.raw_json AS author_raw_json` 并做 `list_following_series` 同款头像提取。验收：收藏 tab 出现头像与 R-18 徽标。

- [ ] **T1-13 `user_backup` 截断不标 truncated**
  `jobs/services.py:161-174` 独立上限（新增 `SyncSettings.user_backup_max_pages_per_run`，或固定 200）；触顶 `stats["truncated"] = True; stats["incomplete"] = True`。注意 `test_every_incomplete_marker_declares_why` 只 grep `sync_engine.py`，这里在 `services.py`，补一条同款守卫。验收：task_log 为 partial。

- [ ] **T1-14 `deploy.sh` `rm -rf` 数据路径**
  `deploy.sh:38-51` else 分支改为 clone 到临时目录再 `rsync -a --exclude data --exclude .env --exclude config/config.yaml`，或 `rm -rf` 前检测 `data/` 存在即中止。验收：目录含 `data/` 且无 `.git` 时脚本拒绝。

- [ ] **T1-15 sync_check 去留**
  推荐**删掉整套**：`sync_check.py`、`web/managers.py:920-992 SyncJobManager` + `:245-259 SyncJobState`、`sync_engine.py:302-372 check_bookmarks_existence`、`POST /api/dashboard/check-bookmarks`、`webapp.py:34` 再导出、`web/utils.py:170-175` 死分支、`jobs/quick_sync.py` 的 `run_check_bookmarks_task`、CLI `sync-check`。`sync_engine.py:1053-1060` 的 `existing_streak` 改为 `db.novel_archive_complete(novel_id)` 判定。验收：grep `sync_check` 在 `src/` 为空；CLI 子命令表 8 个；`pytest -k rescue or bookmark` 绿。

---

## 阶段 2：main 结构性修复

### 2.1 存储

- [x] **T2-01 `init_schema` 进程级一次**
  新增 `Database.ensure_schema(path)` 按 `db_path` 记忆（参考 `ai/services/core.py:128-134 _initialized_paths`）；`webapp.py:70-82 _open_database`、`rescue_web.py:151`、`preference_web.py:28`、`web/managers.py:445/656/735` 改为不再每次 `init_schema()`；`create_app`、CLI 入口、job 入口各调一次。`_migrate_*` 里的 UPDATE / `INSERT OR IGNORE` / `CREATE INDEX` 先 SELECT 判断有行才写（`schema.py:508-516` 回填、`_fix_cleared_status`），`foreign_key_check` 只在真正建表 / 重建时跑。验收：7k 篇库 `_open_database` < 20 ms；另一线程持 `BEGIN IMMEDIATE` 时页面请求不阻塞。

- [x] **T2-02 连接改 `isolation_level=None`**
  `storage/connection.py:120`；所有写方法确认走 `transaction()` 或显式 `commit()`；删 `_migrate_novel_fts_rowid:383` 的补丁式 `_commit_if_needed()` 与 `test_fts_migration_tolerates_pending_implicit_transaction`；`transaction()` :184 的 `rollback()` 隐患随之消失。验收：全量测试绿；`grep -n "_commit_if_needed" storage/` 只剩 transaction 内部。

- [x] **T2-03 `_rebuild_table_with_foreign_key` 事务化**
  `schema.py:263-273` 先 `_commit_if_needed()`，`PRAGMA foreign_keys=OFF` 放事务外，rename / create / copy / drop 包进 `with self.transaction()`；启动发现 `*_old` 表则报错。

- [x] **T2-04 `delete_user` 删 series + 孤儿表**
  `storage/users.py:356-417` 对 `SELECT series_id FROM series WHERE user_id=?` 走 `delete_series` 逻辑；`delete_novel` / `delete_user` 补清 `reading_progress`、`preference_analyzed_novels`。验收：删作者后 `list_following_series` 不含其系列，两张表无孤儿行。

- [x] **T2-05 归档目录持久化**
  `novels.archive_dir TEXT` 列（`PRAGMA table_info` 守卫，回填 `novel_dir(...)`）；同步时写入；`storage_files.py:127-147 get_novel_cover_path`、`webapp.py:142-175 _ArchiveTrash.__init__` 改读列。验收：改作者名后删除仍清掉旧目录，封面仍可找到。

- [x] **T2-06 分页 tiebreaker**
  `novels.py:441-447`（`updated_desc` 补回 `novel_id DESC`）、`bookmarks.py:39-45`、`users.py:232,302,344`、`series.py:594`、`pending_and_watermarks.py:507`、`tasks.py:205,393` 末尾统一补主键。

- [x] **T2-07 `logs` `page_size` 夹紧**
  `storage/tasks.py:176-211,350-398` `page = max(page,1); page_size = max(min(page_size,200),1)`；`webapp.py:1739` 同。验收：`page_size=0` 与 `-1` 返回 400 或夹到 1。

- [x] **T2-08 `_lock` 与 `transaction()` 锁序**
  写方法去掉 `with self._lock`（`_lock` 只保护 `_all_conns`）。验收：`grep -c "with self._lock" storage/` 显著下降；全量测试绿。

- [x] **T2-09 `upsert_subscribed_series.cover_url` 空串覆盖**
  `series.py:44` 改 `CASE WHEN excluded.cover_url IS NOT NULL AND excluded.cover_url != '' THEN ... ELSE series.cover_url END`。

- [x] **T2-10 `upsert_user` `'"{}"'` 绕过**
  `users.py:26` 守卫改 `json_valid(...) AND json_type(...)='object' AND excluded.raw_json != '{}'`；`sync_engine.py:2278` 传 `"{}"` 字面量。

- [x] **T2-11 索引整理**
  删 `idx_assets_novel_id`、`idx_sources_novel_id`、`idx_ai_jobs_job_id`、`idx_sources_source_type`、`idx_rescue_overrides_action`、`idx_reading_progress_status`、`idx_reading_progress_last_read`、`idx_task_logs_auto_sync`（迁移里 `DROP INDEX IF EXISTS`，索引可删）。新增 `novels/users/series (last_checked_at, id)` 三个索引，轮转查询 `ORDER BY last_checked_at, id` 去掉 `(x IS NOT NULL)` 前缀；`novels(status)` 索引。`_migrate_ai_tables` :819-830 的 `try/except: pass` 改 `PRAGMA table_info` 守卫。验收：`EXPLAIN QUERY PLAN` 轮转查询走索引。

- [x] **T2-12 FTS 回滚场景探测**
  `schema.py:362-370` 同时取 `ORDER BY rowid DESC LIMIT 1`。

- [x] **T2-13 `get_ai_task_logs` N+1**
  `storage/tasks.py:401-403` 一次 `WHERE job_id IN (...)` 后分组。

- [x] **T2-14 WAL 上限**
  连接初始化加 `PRAGMA journal_size_limit=67108864`。验收：长任务后 `-wal` 文件回落。

- [x] **T2-15 `.trash` 启动清扫 + confirm 顺序**
  `create_app` 清理 `public_dir.parent/.trash` 下超 24 h 目录；`webapp.py:1973-2001` confirm 改为先 stage 再进事务（同 `_remove_archive_files_atomic`），失败回滚文件并把状态改回 pending。

### 2.2 同步 / 任务

- [x] **T2-16 9 处 `except Exception` 吞 `InterruptedError`**
  `sync_engine.py:645,1020,846,1234,406,441,464,1943,2038` 前加 `except InterruptedError: raise`；把 `test_sync_subscribed_series_propagates_interrupted_error` 的模式推广到 `sync()` 与 `_sync_author`。

- [x] **T2-17 翻页失败静默绿**
  `sync_engine.py:645,1020,1234` 失败时 `stats["failed"] += 1; stats["incomplete"] = True; aborted_reason = "fetch_failed"`（同一行附近声明三标记之一以过 `test_every_incomplete_marker_declares_why`）。

- [ ] **T2-18 系列熔断只对 `novel_series` 计数**
  `sync_engine.py:1473-1708` 把 `series_data = self.api.novel_series(int(sid))` 单独 try 计数；其余异常 `stats["failed"]` + `continue`。

- [ ] **T2-19 `meta_hash` 剔除易变字段**
  `sync_engine.py:2223-2224` 计算前删 `total_bookmarks / total_view / total_comments / …`；或 `text_unchanged` 时跳过 `upsert_novel_text` + `replace_fts` + 正文写盘。验收：同一小说第二轮 `stats["skipped"]` 命中。

- [x] **T2-20 `run_scheduled_user_backup` 单用户失败阻塞轮转**
  `jobs/quick_sync.py:135-162` 循环内捕获单用户异常记 `failed_users`；watermark 按 `completed + failed` 前进；`services.py:194-199` 的失败率检查改在 `processed` 自增之后。

- [ ] **T2-21 手动任务取消入口**
  新增 `POST /api/dashboard/sync/cancel`（可选 `job_id`，默认 latest）调 `shared_job_manager.request_cancel`；`dashboard.html` 停止按钮按 `is_auto_sync` 分流。文档 `JOB_SYSTEM.md:82` 同步。

- [x] **T2-22 不限量模式 `max_items` 返回不标 truncated**
  `sync_engine.py:1247-1250` 置 `truncated + incomplete`。

- [x] **T2-23 让位判定**
  `web/managers.py:876-880,903-911` `_request_yield` 返回 `cancel_task()` 布尔，只有 True 才 `_note_preemption`。

- [x] **T2-24 `_fetch_remote_bookmark_ids` 200 页上限**
  `sync_engine.py:2079-2093` 触顶时写 `stats["truncated"]` + 明确错误文案，而不是裸 `RuntimeError`。

- [ ] **T2-25 杂项**
  `jobs/runner.py:644-645` claim 丢失时 `mark_failed`；`jobs/quick_sync.py:72` 设 `service.stop_requested`，`:86` 删 `print`；`jobs/services.py:136` 登录一次传 `api`；`storage_files.py:116` 与 `generation` 蒸馏 sleep 改可取消（后者在 ai-writing）；`sync_engine.py:756-816` 删「防误删」死逻辑。

### 2.3 Web / 设置 / 部署

- [ ] **T2-26 本机模式 CSRF + Host**
  `webapp.py:776-794` CSRF 检查提到 `if not token` 分支之外；Host 白名单 `localhost / 127.0.0.1 / [::1]`。更新直接 POST 无头的测试。验收：无 token 下不带 `X-CSRF-Token` 的 POST 403，`csrfFetch` 正常。

- [ ] **T2-27 配置空值语义**
  `settings.py:270-274 _parse_bool` 空串回 default；`PIXIV_TIMEOUT=` 空回落 YAML；`PIXIV_PROXY` 用 `is None` 判断。验收：`_parse_bool('', default=True) is True`。

- [ ] **T2-28 `/api/save-token` 白名单**
  `webapp.py:1022` `re.fullmatch(r"[A-Za-z0-9_\-]{10,}")` 否则 400；`user_id` 用 `_safe_int`；`sync_engine.py:1834 _save_web_cookie_to_env` 同样过滤换行。

- [ ] **T2-29 `compare_digest` 非 ASCII**
  `webapp.py:872,793` 两处先 `.encode("utf-8")`。验收：中文 `DASHBOARD_TOKEN` 可登录，输入中文 401 且计入限流。

- [ ] **T2-30 时区校验**
  `web/managers.py:1096` `ZoneInfo(tz)` 失败即 `ValueError`；`webapp.py:1475-1479` 预览响应加 `timezone_valid` / `effective_timezone`；`dashboard_settings_sync.html:386-395` 下拉支持自由输入。

- [ ] **T2-31 `SESSION_COOKIE_SECURE` 顺序**
  `webapp.py:486-492` 挪到 `settings_manager.load()` 之后。

- [ ] **T2-32 `/proxy/image` 3xx**
  `webapp.py:911-913` 非 200 返回 502，透传 `Content-Length` / `Cache-Control`。

- [ ] **T2-33 部署脚本**
  `update.sh:14-17,51` 备份放 `$INSTALL_DIR/.backup/`（`umask 077`）成功后删除；`deploy.sh:127` / `update.sh:102` unit `PATH` 补 `/usr/local/bin:/usr/bin:/bin`；`deploy.sh` apt 列表加 `acl`，执行 `playwright install chromium`；nginx 配置 `server_name` / 证书路径用 `envsubst` 渲染，`nginx -t` 失败中止；`scripts/install_server.sh` 与 `deploy/systemd/*` 二选一：修到自洽或整体删除并同步 `test_deployment_contract.py`。

- [ ] **T2-34 设置保存后重算 `next_run`**
  `save_sync_settings` 后对 cron 变更任务重算；删 `POST /api/dashboard/settings/reload`；`dashboard_settings_sync.html:310` 文案改「新间隔 / cron 在该任务下一次运行后生效，无需重启」。

- [ ] **T2-35 杂项 Web**
  `auto_sync_toggle`（:1707-1714）走 `SettingsManager` 并 `invalidate()`；「任务已在运行」统一 409；`check_user_status` :1374 `login()` 进 try；登录时 `session.clear()` 并按 `authenticated_at` 7 天过期；`/api/health` 只返回 `{"status":"ok"}`；`.env` 空值行原地替换（:451-461）；删 `_AUTH_EXEMPT_PATHS` 的 `/nginx-health`；`dashboard_status` / `export/stats` 裸 SQL 下沉 storage；生产改 `waitress`。

### 2.4 AI 基础设施

- [ ] **T2-36 池校验只看受影响子图**
  `ai/model_pools.py:92-98 validate_pool_graph` 增加 `changed_pool_ids` 参数只校验本池 + 引用祖先，或错误信息带池名并把预存在坏池降 warning；`admin.py:721-728 update_provider(enabled=False)` 走一次 lint 提示。

- [ ] **T2-37 `_import_available_models` 每次重跑**
  `storage/ai/model_schema.py:484-527` 导入后把 `available_models_json` 置 NULL；`_normalize_provider_payload` 不再接受 `available_models`；`dashboard_settings_models.html:788-791 copyFromProvider` 显式挑字段。验收：删掉的人工模型重启后不复活。

- [ ] **T2-38 Provider 无法删除**
  `storage/ai/core.py:286-297` 只拒绝「被固定 Agent 引用」与「模型被池引用」，目录行交给 CASCADE。

- [ ] **T2-39 `_adapter_lock` 内发网络请求**
  `ai/providers.py:500-524` 锁只包 `session.mount` get-or-create，请求移到锁外；`close()` 同理。

- [ ] **T2-40 普通 AI job 取消 + deadline**
  `ai_jobs` 加 `cancel_requested` 列；新增 `POST /api/dashboard/ai/jobs/<id>/cancel`；heartbeat 线程读取后 set `threading.Event`，`RouteRequest.is_cancelled` 检查它；`model_router.py:886-889 _request_cancelled` 同时检查 `route_deadline_at`；调度器循环每小时调 `fail_stale_ai_jobs()`。日志页加取消按钮。

- [ ] **T2-41 429 按模型判 scope**（用户已确认非有意）
  `ai/providers.py:1451-1454` 429 带模型标记时 `scope="model"`；5xx / 网络错误保持 provider 级但 GUIDE §4.3 写清楚。

- [ ] **T2-42 健康投影三处假绿**
  `admin.py:487-489` 增加 `api_key_undecryptable`（try `decrypt`）；`:686-693` fixed Agent 复用 `_resolve_fixed` 三条判据；`:660-684` pool Agent 过 `required_capabilities`。

- [ ] **T2-43 PromptBudget 估算器**
  `model_router.py:747-751,957-965` 退化估算改用 `chunking.estimate_token_count`（需把 `chunking.py` 的这个函数留在 main 或搬到 `model_router`），`estimator="heuristic"`；GUIDE §4.1 同步。

- [ ] **T2-44 密钥字面值脱敏**
  `providers.py:1509 _http_provider_error` / `_request_provider_error` / `_event_provider_error` 先 `replace(api_key, "[REDACTED]")` 再正则（同 `model_sync.py:195-200`）。

- [ ] **T2-45 杂项 AI**
  `Retry-After` 消费：重试延迟 `max(2**attempt, error.retry_after or 0)` 上限 60；`_iter_sse_lines` 单行 1 MiB 上限、错误响应体 1 MiB 上限；`ModelListResult` 常量字段与 `empty_authoritative` 死分支删除或真实区分；`probe_provider_models` 带 `proxy`；`_resolve_base_url` :735 去掉 `api.anthropic.com`；`ModelSyncCoordinator.events()` 复用一个 db；`default_model` / Agent `model` 保存时 `normalize_model_key`；`cleanup_model_sync_operations` 接到调度器；`DELETE /model-sync-operations/<id>` 补「取消同步」按钮。

### 2.5 偏好推荐

- [ ] **T2-46 默认列表排除 dismissed / muted + 徽标**
  `storage/recommendations.py:411-461` 不传 `status` 时 `WHERE status NOT IN ('dismissed','muted')`；`muteAuthor` 后端批量置同作者 `new` 为 `muted`；两个模板显示状态并灰化。

- [ ] **T2-47 分析任务合并而非覆盖**
  `jobs/tasks.py:373-387`：existing 存在时 `name / description` 只在 params 显式给出时覆盖；`negative_preferences` 从 existing 合并；AI 失败或 `processed_this_run == 0` 时沿用 `existing.stats.refined_keywords` 且跳过 AI 调用。中期：`overrides_json` 列。验收：用例「AI 抛异常 + 0 篇新小说 → 画像名称与精炼词不变」。

- [ ] **T2-48 搜索计划**
  `preferences.py:209-216` `precise_queries` 用 `zip(primary_tags[:6], keywords)`；`broad_queries` 改用 `secondary_tags` 或精炼词；`build_search_plan` 产出 `exclude_terms` 并在 `_search_novels` 应用；客户端 `search_plan` 服务端归一化（queries ≤ 20、limit 1..100、query ≤ 200 字）。

- [ ] **T2-49 去重与评分**
  `recommendations.py:190` 增加系列级归档排除（`SELECT 1 FROM novels WHERE series_id=?`）；`:400-432` 标签重合剔除画像 `primary_tags` 与通用标签后再计数；`_score` 给 `author_id ∈ preferred_authors` 加分；热度上限降到 < 12；字数按 `text_length / preferred_min_length` 对数分级；`existing` 在 `run()` 开头加载一次。

- [ ] **T2-50 前端字段与超时**
  `dashboard_preferences.html` `result.stats?.stats?.saved` 与 `result.error || result.message`；去掉 5 分钟硬超时改「仍在后台运行」；任务状态中文化；初始 loading。

- [ ] **T2-51 杂项**
  `run()` 返回值去掉 `items`；`create_recommendation_mute` upsert 后 `SELECT id`；`PUT /profiles/<id>` 校验 payload 且拒绝把唯一默认画像置非默认；`min_text_length` 变更重建累加器；`_row_to_recommendation_item` 补 `source_url`，两模板改用；`preferences.py:223-224` themes / scenes 语义与 `preference_context` 对齐或改标签。

### 2.6 救援

- [ ] **T2-52 增量刷新与全量一致**
  `storage/rescue.py:761-777` 删 `existing_series_ids` 过滤，`should_rebuild = bool(novel_ids) or bool(existing_series_ids)`；补「`refresh_rescue_item` 结果 == `rebuild_rescue_catalog` 结果」等价断言。

- [ ] **T2-53 认证不碰库 + 限速前置**
  `rescue_web.py:158-175` 顺序改 IP 限速 → `app.extensions` 缓存的 `(token_hash, prefix)` 比对 → 视图单次 `open_db()`；`rotate` 时刷新缓存。补 `DELETE /api/dashboard/rescue-token`（撤销）。

- [ ] **T2-54 重建触发点收敛**
  `quick_sync.py:84`（bookmark）、`:169` / `services.py:239`（user_backup）、`tasks.py:209`（following_novels）改为「有状态或来源变更才重建」，让三种状态检查与 pending 检测负责；`_replace_catalog_memberships` 改差量。

- [ ] **T2-55 杂项**
  `_catalog_stale` 对非法 `refreshed_at` 视为 stale；`dashboard_novel_detail.html:355-362` 三态显示 partial；`evaluate_rescue_*` 加 `pending_removal` 字段并在详情页提示；`dashboard_pending_deletions.html:218-233` 终态集合加 `cancelled` 且 unmount 清 interval，列表加载失败显示错误；恢复后刷新失败返回固定文案 + `restored: true`；实时路径也读 `has_content`；`cleanup_old_pending_deletions` 删无用参数；确认移除弹窗说明「会连同其它来源一起删」。

### 2.7 前端公共

- [ ] **T2-56 五处手写 fetch 迁 `csrfFetch`**
  `dashboard_novel_detail.html:439-490`、`dashboard_series_detail.html:203-254`、`dashboard_logs.html:540-603`；改 `tests/test_frontend_library_os.py:395-407` 断言为「含 `window.csrfFetch`、不含 `ensureCsrfToken` 副本」。

- [ ] **T2-57 公共 `formatDbTime` / `toast`**
  `base.html` 提供 `window.formatDbTime(value, opts)` 与 `window.toast()`；替换 `user_detail.html:79`、`series_detail.html:58`、`pending_deletions.html:87`、`follows.html:104`、`settings_models.html:132` 及全部 `alert()` / 页顶 message。

- [ ] **T2-58 任务命名统一**
  后端在 `shell-data` 或 `base.html` 下发 `TASK_LABELS`，删 `dashboard.html:181`、`dashboard_logs.html:369/677`、`settings_sync.html:402/604` 三份副本与 `following_series` 死键。

- [ ] **T2-59 `app-modal` 无障碍**
  `vue_components.html:186-200` 加 `role="dialog" aria-modal="true" :aria-labelledby`、`@keydown.esc`、焦点进出、关闭按钮 `aria-label`。16 个 `<img>` 补 `alt`；AI 设置三页 14 个输入补 label。

- [ ] **T2-60 Provider 探测复选框**
  `dashboard_settings_models.html:807-843` 保存后把勾选项写为人工模型或触发同步；或改只读预览。

- [ ] **T2-61 杂项前端**
  登录页去空壳侧栏（`token_login.html:7-10` 用 `initVueApp` 空 setup 或 base 加 `{% block shell %}`）、加 `<form>`、失败原因经 `errorText`；Tailwind / Vue vendor 到 `/static/` 加 `integrity`；首页 `isTaskRunning` 含 `queued / cancel_requested`，`stopAutoTask` 处理 200 `{ok:false}`，不可让位任务加确认，空闲轮询放慢到 15–30 s，footer 改 `shell-data`；用户详情网络错误不报「未找到」并加 `AbortController`；向导页七个 async 加 try/catch（ai-writing）；Agent 批量改绑复用 `agentBindingValid`，筛选补 `extract_summary / resolve_foreshadow`（ai-writing）；系统页 Token 复制反馈进弹窗；成人页 403 文案（ai-writing）；删 `darkMode: 'class'`、`.line-clamp-2` / `.custom-select` 重复、硬编码 `#0096fa`；移动端底栏补「待确认删除」「任务日志」。

---

## 阶段 3：main 清理与文档

- [ ] **T3-01 删无生产调用方路由**（保留在契约里的标「仅 API」）
  `POST /oauth/start`、`GET /oauth/callback`、`POST /oauth/sync-callback/<id>`、`GET /api/dashboard/follows`、`POST /api/dashboard/settings`（全量）、`POST /api/dashboard/sync/start`、`GET /api/dashboard/pending-deletions/count`、`POST /api/dashboard/settings/reload`（T2-34）、`POST /api/dashboard/ai/jobs/cleanup`、`GET /api/dashboard/recommendations/runs`。**接上而不是删**：`/novels/<id>/progress` 三端点（阅读进度换设备可用）、`POST /novels/export-epub`（加按钮）、`DELETE /novels/<id>` / `/users/<id>` / `/bookmarks/<id>`（详情页加入口或删）、`POST /api/auth/logout`（侧栏加退出）、`POST /profiles/<id>/default` / `DELETE /profiles/<id>`（偏好页加多画像 UI 或删）。

- [ ] **T3-02 死代码**
  `settings.py:437-449` pytz 分支与 `:456-530 _simple_cron_next_run`；`storage/pending_and_watermarks.py:101 restore_pending_deletion`；`storage/schema.py:316 _fix_stale_running_logs`；`storage/novels.py:542 export_stats` 与 `storage_db.py:50` 二选一；`web/utils.py:577 _remove_archive_files` + `storage_files.py:149 remove_novel_archive`（抽 `_collect_archive_paths` 两处共用）；`storage/rescue.py:795,821` 与 `storage/recommendations.py:333` 仅测试方法移到测试 helper；`jobs/tasks.py:417-418 rate_limit` 分支；`cli.py:128` 不可达 `parser.error`，`cli.py:104` `sync-bookmarks` 按 `truncated` 退出非零；`web/__init__.py` 急切导入清空；`webapp.py:34,54,55` 与 `ai/service.py:3` 未使用导入；`storage/ai/model_sync.py:334 empty_authoritative` 路径。

- [ ] **T3-03 文档修正**（报告 §6 表逐条）
  重点：`JOB_SYSTEM.md` :65/:75/:82/:95/:138/:159/§3.5/§5 `novel_status_batch_size`；`UNIFIED` §5.3/§7.2/§10.2/§18/:201/:310/§12.1；`PREFERENCE` §7.2/§7.4/§13.5/§10；`RESCUE_USER_GUIDE` :129/:135/:137/§3.3/§4.1；`MODEL_ROUTING_GUIDE` :100/:112/:141；`frontend-api-contract` `follows` / `progress` / `logs days` / `users search` / `novels/{id}` 字段 / 三种信封形状 / `detail` 形状 / `preference_web.fail` 400 / 目录响应字段 / 成人 cancel body；`frontend-pages` partial 表补 `dashboard_ai_health_band.html`、`source_url`、系统页「立刻」→「一小时内」；`library-os-style-guide` Buttons / Forms / Tables 节与实现对齐或删 `library-*` 类；`INDEX.md` 把 4 份 08-14 计划改为「部分以其它方式实现，剩余：task_logs lease、pagination guard、trash manifest、AI 总结 / 解释、成人偏好注入、审查阶段实时 progress」，`2026-08-28-sync-throughput-and-budget.md` 标「已拆为 phase1-3」；`CLAUDE.md` 测试数、`except Exception` 描述、content-hash 描述、`Retry-After` 描述。

- [ ] **T3-04 需求缺口决策**（做或明确不做，写进 UNIFIED）
  偏好：分析范围 + scope fingerprint、热度分布、`relationship_dynamics / tone / pacing / narrative_patterns`（或删 `preference_context` strong 档消费）、AI 结构化总结、三个 stream 端点、`runs/<id>`、`items/<id>/sync` + `RECOMMENDATION_SYNC`、屏蔽标签 / 待阅读 / 取消屏蔽 UI、多画像 UI、删除历史。同步：task_logs owner lease / heartbeat、seen-cursor、trash manifest + 启动重放、`novel_status_batch_size` 入 `SyncSettings` 或删文档、用户备份任务级重试。救援：`source_url` 进列表 / API / 模板，4593 项 fixture 与 `verify_rescue_performance.py`。

---

## 阶段 4：ai-writing 分支专属

先 `git checkout ai-writing && git merge main`，再做本阶段。

### 4.1 AI 创作 P0 / P1

- [ ] **T4-01 预算单位统一（P0）**
  把 `projects.py:42-90` 的 `_utf8_tail / _fit_route_messages / _fit_tail_text_messages` 挪到 `services/core.py`；`generation.py:155-158,174,241,578,650-653,709-723` 五个流一律改用；`_smart_context` 段长与蒸馏批大小按字节；`chat_wizard.py:107-146` 按字节从最旧轮丢历史，用户消息在 `_start_route_job` 成功后再写；`tests/test_ai_model_router_integration.py:1246` AST 守卫覆盖 `services/` 全部 `stream_*`。验收：22000 字中文 `stream_continue / rewrite / audit / plan` 不 overflow；6 轮长对话后向导仍可发。

- [ ] **T4-02 向导弹窗 prop（P0）**
  `dashboard_wizard.html:146,162` `:is-open`，删 `size`。补 prop 名守卫测试。

- [ ] **T4-03 死按钮**
  `dashboard_ai_notes.html:260-270` 带 `chapter_id`（下拉复用 `rawImportChapterId`）或后端无 `chapter_id` 时整本回顾；`dashboard_ai_chapters.html:881-911` `agent_id` 用 `pipelineAgentIds.*` / `bestAgentId`，`onEvent` 处理 `error`。

- [ ] **T4-04 Pipeline 工作区与收口**
  `dashboard_ai_chapters.html:589-623,866-873` 拆 `resetForChapterSwitch()` / `reloadCurrentChapter()`，pipeline 完成只调后者，`failed` 列表在 reset 前快照；`projects.py:1828-2226` 顶层 `try/finally` 未写终态则 `cancelled`；`dashboard_ai_pipeline_modal.html` 加取消按钮；`ai_web.py:687-691 stream_response` 对 `AIServiceError` 透传原文。

- [ ] **T4-05 章节乐观锁与并发**
  `storage/ai/writing.py:171-199 update_ai_chapter` 加 `expected_revision` + `WHERE chapter_revision=?`，不匹配 409；`ai_web.py:2064-2070` PUT 带 revision；`projects.py:1055,1127 save_generated` 重读当前 content；前端 `streaming || pipelineRunning` 时禁用正文编辑与保存；「追加到正文」在 `autosaved` 后隐藏。

- [ ] **T4-06 项目页写回 `style_control`**
  `dashboard_ai_project.html:639-663` 两个生成前 `saveProjectStyleControl()`。

- [ ] **T4-07 杂项**
  `retrieval.py:78-101 index_chapter` 清缓存；`projects.py:432-460 _extract_json_object` 用 `json.JSONDecoder.raw_decode`；草稿加读取 UI 或下线草稿 API；Agent 表单加 `context_window / top_p`，task_type 下拉补三项；删项目在事务里清 `ai_chat_sessions.imported_project_id`，索引删除放事务提交后；`new_foreshadows` 过滤「无」；`PUT /states/<type>` 白名单；章节归属校验；`create_chapters_from_plan` 事务化；章节列表接口不拉全量正文；`ai_jobs.input_json` 不存粘贴全文；蒸馏 sleep 可取消；Pipeline 默认 Agent 不回退到 wizard / adult；向导导入 `chapter_number` 容忍字符串、`:423` 条件优先级、伏笔回收不覆盖 `notes`、`notes.html:252` 用章节号；`dashboard_ai_source_search.html:37` 裸 fetch；`_get_retriever` 传 `use_embeddings` 或删本地模型分支；embedding 请求走 `validate_base_url`。

### 4.2 成人润色 P1 / P2（不放宽校验）

- [ ] **T4-08 别名掩码恢复同构（P1）**
  `adult_prompt.py:181-189,279-287,488-489`：每个 (character, name-variant) 一个 token，恢复时写回被掩码的原表面字串；`services/adult.py:247 _normalize_character` 拒绝长度 < 2 的别名；`build_adult_prompt` 对「别名命中数 > 名字独立词命中数」fail-closed。验收：「森林里，林舟…」恢复后逐字相同；「小安握住他的手」不被改成「安娜」。

- [ ] **T4-09 占位符尾随空白（P1）**
  `adult_prompt.py:448-454` 改 `"[\\s…]*".join(re.escape(c) for c in token)`；补 token 后跟换行 / 空格用例。

- [ ] **T4-10 参与者规则统一（P1）**
  `services/adult.py:2089-2103,2181-2185 _verify_named_participants` 只扫 `target`；「参与者必须在目标片段被点名」前移到 `prepare_adult_job` 预检并给可操作文案；testkit 默认 payload 恢复真实角色名。

- [ ] **T4-11 审查 / 主 Agent 预算（P1）**
  `services/adult.py:387-388,2851-2852`：`prepare_adult_job` 同时预估两个审查阶段预算（`len(target)·3` 上界）并在开 SSE 前拒绝；审查窗口取 `min(候选模型窗口, 常量)`；`dashboard_settings_adult.html` 暴露 `context_window / max_tokens`；`ModelRouteError` 映射为带原文的 400。依赖 T4-01 / T2-43 的估算器修正。

- [ ] **T4-12 阅读页重新生成 403（P1）**
  `dashboard_ai_reader.html:423-436` regenerate 带 `X-Adult-Access-Token`，`resetAdultCandidate()` 延到新 metadata 到达后；加取消按钮（调 `/cancel`）与「重新查询」按钮。

- [ ] **T4-13 断连与进度**
  `services/adult.py:2538-2539` delta 循环每 2 s yield 一个不含文本的 `progress {"phase":"generate","action":"keepalive"}`；审查阶段（`:808,1224-1306`）改 `execute_stream` 实时转发；`:2527-2569` 加 `finally: stream.close()`。

- [ ] **T4-14 阻断与取消语义**
  本地校验出现任何非可见阻断立即 fail-closed（`:1194-1222`），错误码 `local_blocked` 并附可展示 code；`_run_adult_review:810-817` 对 `finish_state == "cancelled"` 抛 `AdultCancelled`，`finish_adult_candidate` 失败分支后读 job 状态为 `cancelled` 则改发 `cancelled`。

- [ ] **T4-15 deadline 与回收**
  `storage/ai/adult.py:666-674,750-762` 主 / 子任务写 `route_deadline_at`；调度器周期 `fail_stale_ai_jobs()`（与 T2-40 共用）。

- [ ] **T4-16 `_diff_summary` 复杂度**
  `adult_validation.py:208-227` 先阻断判定再算 diff；超阈值改段级 diff 或 `quick_ratio`。验收：12000×36000 随机中文 < 1 s。

- [ ] **T4-17 策略升级路径**（用户已确认非有意）
  `ai_adult_policy_state` 增加受控迁移：`version` 升级时写入新行并把旧候选标 `policy_upgrade_required`；设置页显示升级入口。

- [ ] **T4-18 杂项成人**
  `dashboard_settings_adult.html:190,291` 年龄默认留空、编辑显示原值；`ai_web.py:428-454 validate_adult_stream_preflight` 删除；`access_token_hash` 同义反复二选一（删 bind + 比较，或签发时写入 nonce 哈希）；`/events` 重放不再签发新 token；`_adult_cancel_checker` 异常记日志；已应用任务 `/events` 返回 `applied` 而非 `adult_polish_failed`；审查绑定保存失败不静默停用旧绑定；确认接口支持子集；非 applicable 候选不写 `output_text`；词表检查 NFKC；`_run_stored_revalidation` 传取消回调；成人偏好注入（`preference_context` 复用 + context hash 进审计输入）或从 `adult_types.py:261-275` 删掉字段并更新契约。

---

## 阶段 5：体验优化 backlog（两分支各自）

按报告 §7 逐页，优先级从高到低：

- [ ] **T5-01** 列表状态进 URL：小说库 / 作者 / 日志 / 待删除 / 用户详情的筛选、页码、搜索 `replaceState`；详情页「返回」按 `?from=` 或 `document.referrer`。
- [ ] **T5-02** 首页状态条显示 `task_name · phase · current/total`，点击进日志详情；推书列表加快捷反馈与「隐藏已反馈」。
- [ ] **T5-03** 日志页筛选即改即查；窄屏折叠卡片；执行日志四色；行 `tabindex`；自动刷新暂停提示。
- [ ] **T5-04** 同步设置页：手动触发后给进度 / 日志链接；两个「启用」开关说明；未保存提示；数字输入「不限」占位。
- [ ] **T5-05** 模型 / Agent 页：反馈就地显示；Provider 删除确认显示 `bound_agent_count`；「同步模型」取消按钮；Agent「编辑」滚到表单；「一键初始化」说明。
- [ ] **T5-06** 小说库 AI tab 排序下拉（ai-writing）、拯救 `item_type` 死过滤删除、每页条数下拉位置、空态行动按钮。
- [ ] **T5-07** 小说 / 系列详情：阅读进度接后端三端点；救援按钮解释；字号档位提示；删除成功不用 `alert`。
- [ ] **T5-08** 待删除页：确认框显示本地章数 / 字数；「忽略恢复」文案；按钮 loading；复用 `app-pagination`。
- [ ] **T5-09** 关注作者页：错误态重试；状态 tab 补 `cleared / unknown`；搜索框。
- [ ] **T5-10** 偏好页：合并「生成搜索计划」与「执行推书」；`scope` 参数与设置页关系说明。
- [ ] **T5-11** AI 创作（ai-writing）：SSE 中断引导到日志页续跑；路由 progress 渲染 `action/reason`；`detectAITells` 不用 `alert`；阅读页应用候选不跳回第 1 章；伏笔空态二次加载；pipeline 弹窗移动端布局。
- [ ] **T5-12** userscript（main）：章节级错误不清空面板；按 `response.status` 分支文案；失效判定只匹配 Pixiv 错误容器；SPA 路由变化重新触发；`API_ORIGIN` 可配置。

---

## 验收清单（我按此核对）

1. 阶段 0 §0.3 全部项。
2. 每个 T 编号对应的「验收」语句可复现；有回归测试的任务，测试文件名写在 commit message 里。
3. `pytest -q` 两分支各自全绿；`python -m compileall -q src tests` 通过；`pyflakes src` 无未使用导入。
4. `git log --graph main ai-writing`：`main` 的每个修复提交在 `ai-writing` 上都能 `git branch --contains` 找到；`ai-writing` 的提交不出现在 `main`。
5. 生产库副本在两分支上 `init_schema()` 均成功且表未减少。
6. `docs/AUDIT_REPORT_2026-09-14.md` 每条 P0 / P1 在本计划有对应 T 编号；完成后在报告 §1 表加「状态」列。

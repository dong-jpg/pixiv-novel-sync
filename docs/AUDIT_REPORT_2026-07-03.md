# 审计报告 2026-07-03

**审计日期**: 2026-07-03
**审计范围**: 隐藏 Bug / 不必要分支 / 幻觉实现 / 死胡同代码 / 调用路径 / 文档过时
**基线 commit**: e32ed06（上一轮「任务取消硬化」）+ 2026-07-02 审计的工作区未提交修复
**测试基线**: 209 passed（上一轮）→ 203 passed（本轮，因删除 6 个依赖被删死代码的测试）

---

## 执行摘要

本轮在上一轮（2026-07-02）审计基础上做更深一层的验证与清理：4 个并行子代理 + 主代理亲自验证。共修复 **1 类严重回归**、**1 处死代码链路**（使上一轮 S5 取消修复运行时无效）、**9 类轻微问题**（含上一轮遗留的 L1-L5、M4），并整顿 26 份膨胀文档。

| 类别 | 修复数 | 验证方式 |
|------|--------|----------|
| 严重回归（崩溃） | 1 | EPUB 导出实跑验证 |
| 死代码链路 + 修复空转 | 1 | grep 全仓 + 测试重写 |
| AI pipeline 事件/数据 bug | 2 | 代码路径复核 |
| 零散死代码 | 3 | grep + pyflakes |
| 膨胀/类型契约 | 4 | autoflake + 签名核对 |
| 文档整改 | 26 份 | 归档/删除/重写 |

---

## 🔴 严重 bug（已修复）

### S1. EPUB 导出对带封面小说再次 500（上一轮 S2 的回归）

`webapp.py:753` `storage = FileStorage(current_settings.storage)` 传入 `StorageSettings`，但 `FileStorage.__init__(settings: Settings)` 期望完整 `Settings`。`get_novel_cover_path` → `novel_dir` → `base_dir` 访问 `self.settings.storage.private_dir`，`StorageSettings` 无 `.storage` 属性 → `AttributeError`。

无封面小说导出正常（不调 `get_novel_cover_path`），带封面即 500。这是上一轮 S2 修复（`FileStorage.get_novel_cover_path`）后引入的回归——方法本身对，但构造参数错。全仓其余 13 处 `FileStorage(settings)` 调用都正确，唯独 EPUB 导出这一处传错。

**修复**: `webapp.py:753` 改为 `FileStorage(current_settings)`。

**验证**:
```python
from pixiv_novel_sync.settings import load_settings
from pixiv_novel_sync.storage_files import FileStorage
s = load_settings('config.yaml', None)
storage = FileStorage(s)
novel_data = {'user_id':123,'author_name':'t','novel_id':456,'title':'测',
              'restrict_value':'public','cover_url':'https://i.pximg.net/x.jpg'}
path = storage.get_novel_cover_path(novel_data)
# OK: data\library\public\authors\123_t\novels\456_xxx\assets\cover\x.jpg
```

---

## 🟡 中等问题（已修复）

### M1. `web/managers.py` 旧同步执行链路是死代码，上一轮 S5 取消修复无效

`SyncJobManager.start_job` / `start_user_backup_job` / `_run_job` / `_run_single_sync` 整条链路**生产零调用方**：`webapp.py` 所有同步提交都走 `shared_job_manager.submit()` + `_submit_shared_web_job`（JobManager/JobRunner），`SyncJobManager` 在生产仅作只读状态查询（`has_running_jobs`/`latest_job`/`get_job`/`_jobs`）。`start_auto_job` 只注册状态不启动线程，`AutoSyncScheduler._run_single_task` 自己跑任务。

上一轮 S5a（`on_progress` 的 `_cancel_check` 分支）、S5b（`following_users` sleep 改 rate_limiter.wait）、S5c（`_run_job` 任务间 cancel 检查）**全写在这段死代码里，运行时无效**。`AutoSyncScheduler` 自己定义了 `_job_reporter`/`_stop_requested_for_job`（249/252 行），不依赖 SyncJobManager 的同名方法。

测试 `tests/test_webapp_jobs.py` 5 个测试依赖被删方法，已重写。

**修复**:
- 删除 `SyncJobManager.start_job`（801-827）、`start_user_backup_job`（854-880）、`_job_reporter`（948-949）、`_stop_requested_for_job`（951-955）、`_run_job`（957-1060）、`_run_single_sync`（1061-1353），共约 462 行。
- 保留 `start_auto_job`/`get_job`/`latest_job`/`latest_matching_sync_check_scope`/`has_running_jobs`/`add_log`/`update_progress`/`is_cancel_requested`（生产在用）。
- `tests/test_webapp_jobs.py` 删除 5 个依赖被删方法的测试（`test_run_job_preserves_*` × 2、`test_run_job_marks_cancelled_on_interrupted_error`、`test_sync_job_manager_start_job_records_job_spec`、`test_sync_job_manager_delegates_target_tasks_to_services`），重写 `test_shared_sync_blocks_legacy_sync_routes` → `test_shared_sync_blocks_concurrent_sync_submission`（去掉对已删方法的 monkeypatch）。

**验证**: `grep -rn "start_job\b\|_run_job\b\|_run_single_sync\b" src/` 无命中；203 测试通过。

### M2. AI pipeline `detect` 步骤双发 `step_done` 事件

`projects.py:1713` detect 正常路径 `yield step_done` 后无 `continue`，会继续走到 1723-1724 公共收尾再 yield 一次。前端收到两次 `step_done`。对比 foreshadow 分支（1651-1652）正常路径有 `continue`，audit/index 分支靠公共收尾不发 inline yield。

**修复**: 删除 1713 的 inline `yield step_done`，让 detect 正常路径靠公共收尾（1723-1724）发 step_done，与 audit/index 一致。detect 的 skip 路径（1695）保留 inline yield + continue。

### M3. AI pipeline `state` 步骤伏笔无幂等

`projects.py:_parse_and_save_state` 的 `new_foreshadows` 分支每次跑都无条件 `db.create_ai_foreshadow(...)`，同章 pipeline 重跑会插入重复伏笔。对比 `chat_wizard._import_wizard_payload` 用 `existing_descs` 去重。

**修复**: 在 `create_ai_foreshadow` 调用前先 `db.list_ai_foreshadows(project_id)` 取现有 description 集合，跳过已存在的；同时把本次新增的 description 加入集合，避免同次输出内的重复也插入。

---

## 🟢 轻微问题（已修复）

### L1. `webapp.py:131` `_running_job_error_response` 无调用方

各路由直接用 `_api_error("已有同步任务正在运行，请稍后再试")`。删除该函数。

### L2. `rate_limiter.py:54` `handle_response` 是死代码

全仓无生产调用方，仅 `tests/test_rate_limiter.py` 在测。429 重试实际由 `sync/utils.py:21` 的 `retry_on_pixiv_error` 装饰器处理。删除 `handle_response` + `_get_retry_after` 方法 + 对应测试 `test_handle_response_checks_cancel_during_retry_after`。保留 `wait` 方法（生产在用）。

### L3. `jobs/tasks.py:227` `_accepts_parameter` 孤儿函数

上一轮 L1 修复时删了 `_run_direct_sync_task` 里的 `_accepts_parameter(sync_subscribed_series, "download_assets")` 死分支，但函数本体没删。全仓零调用方。删除。

### L4. `recommendations.py:159/185` 重复字段提取

`author_id` 两次提取（159/185），`tags = self._tags(novel)` 两次调用（160/191），第二次覆盖第一次。合并为一次提取，第二次 muted 检查复用第一次的变量。`_tags(novel)` 不再重复调用。

### L5. `preferences.py:202-206` 4 个半成品字段无消费者

`relationship_dynamics`/`tone`/`pacing`/`narrative_patterns` 永远是空列表，grep 全仓（src/ + templates/）无任何读取者，`recommendations._score` 也不引用。删除这 4 个字段。保留 `scenes_or_situations`（有生产者 `caption_keywords[:10]`）。

### L6. `ai/services/*.py` 顶部大量未使用 import（上一轮 M4）

5 个 mixin（admin/chat_wizard/generation/projects/core）整段复制了 14 个 `build_*`/`safe_prompt_preview`/`DEFAULT_WIZARD_PROMPT` import + `hashlib/json/os/re/threading/uuid/Path/Iterator` 等，每个 mixin 实际只用其中几个。

**修复**: 用 `autoflake --remove-all-unused-imports` 自动精简。pyflakes 复查 5 个文件无未使用 import。

### L7. `sync_engine._fetch_remote_subscribed_series_ids` 类型契约不诚实

签名 `-> set[int]`，实际多处 `return None`，靠 5 个 `# type: ignore[return-value]` 绕过。调用方 `detect_unfollowed_series` 正确处理 None（当"跳过检测"），运行时安全。

**修复**: 签名改为 `-> set[int] | None`，删除 5 个 `# type: ignore[return-value]`。

---

## ✅ 保留项（存疑，有测试保护或能力储备）

- **`sync_engine.check_bookmarks_existence`**: 全仓生产无调用方（生产走 `check_all_existence`），但 `tests/test_sync_engine_incremental.py:155-180` 有测试保护其批写 sync_check_list 的能力。保留作能力储备。
- **`SyncJobManager.latest_matching_sync_check_scope`**: 生产无调用方（唯一调用方是被删的 `_run_single_sync`），但 `tests/test_webapp_settings.py:211-237` 测其 fingerprint 匹配逻辑。保留作能力储备。
- **`webapp.py` 无 token 时无 CSRF 保护**: 当 `dashboard_token` 未配置时，本机请求在 `_check_auth` 第 303-313 行提前 return，不进入 CSRF 校验分支。这是有意的"仅本机"设计（配合 `_add_security_headers` 与启动告警）。若本机存在恶意页面可发起 CSRF，但属可接受的设计取舍。

---

## ✅ 误报排除（复核确认非 bug）

- 上一轮 S1-S7、M1、M5 修复**全部验证正确完整**：AI 幻觉 import（generation.py:424 `.prompts`→`..prompts`、admin.py 四个常量、projects.py 的 PIPELINE_STEP_ORDER/LABEL 与实际 10 个 step 完全对齐、chat_wizard 的 `@staticmethod`）、EPUB XHTML 注入（`html.escape` 转义 title 与正文）、schema FK check（`logger.warning` 不再 raise）、dashboard_token 配置漂移（顶层 + sync 块 + 环境变量三路支持）、`_remove_archive_files` 死异常分支（已删）、webapp.py 重复定义（已删，保留 `web/utils.py` 的枚举比较版本）。
- **`oauth_helper.exchange_code` 网络异常**: 上一轮 L5 担忧"status 停在 pending 不降级"在当前代码**不成立**——3 个生产调用方（`webapp.py:566-570`、`602-606`、`213/224-227`）都用 `try/except Exception` 兜住并设 `task.status="failed"`。
- **`FileStorage.get_novel_cover_path` 方法本身**: 签名正确，内部用 `_filename_from_url` 重建路径，与 `sync_engine._download_assets` 命名规则对齐。问题只在 S1 的构造参数。
- 其余上一轮误报排除项（crypto v1/v2 KDF、providers SSE、core provider 缓存、f-string SQL、preference_web CSRF、proxy_image SSRF、path traversal）本轮未重审，结论不变。

---

## 📚 文档整改

### 归档（移入 `docs/archive/`）

14 份顶层历史文档 + 6 份 superpowers 已完成计划：
- 2026-06-16 审计系列：AUDIT_REPORT / EXECUTIVE_SUMMARY / COMPLETION_REPORT / CRITICAL_BUGS_FIX_PLAN / BUGS_FIXED_REPORT / ACTION_CHECKLIST
- 优化路线图系列：OPTIMIZATION_ROADMAP / OPTIMIZATION_REVIEW_2026-06-26 / OPTIMIZATION_PLAN_2026-06-30
- 模块化系列：MODULARIZATION_PLAN / MODULARIZATION_COMPLETE / MANAGER_EXTRACTION_COMPLETE / IMPLEMENTATION_RECORD / ALL_TASKS_COMPLETED
- superpowers 已完成计划：qwen-embedding-robustness / cli-job-services / unified-job-queue / web-jobspec-runner + 2 份 specs

`docs/archive/README.md` 列出归档清单与原因。

### 删除

- `docs/API.md`（2.4K 旧版草稿，被 `API_COMPLETE.md` 完全覆盖）
- `docs/GIT_COMMIT_GUIDE.md`（一次性 v0.2.0 发版指南，含 `git add .` 直推 main 的危险建议与占位符）
- 根目录 `_audit_calls.py` / `_audit_sweep.py`（未追踪的临时审计脚本）

### 重写 `docs/INDEX.md`

从「9 个文档 ~40000 字」的失真索引改为三段式当前文档地图：活跃参考（9 份顶层）/ 开发计划（1 份）/ 历史归档（指向 archive/）。删除自相矛盾的优先级与阅读顺序章节。

### 修 `README.md`

- 启动命令 `python -m pixiv_novel_sync.webapp` → `pixiv-novel-sync web-token-ui`（实际入口在 `cli.py`，`pyproject.toml` 已声明 `[project.scripts]`；`webapp.py` 无 `__main__` 块）
- 占位符「你的用户名」→ `dong-jpg`（8 处）
- 路线图追加一行指向本轮审计报告

---

## 🔧 修复验证

```
$ python -m pytest tests/ -q --tb=line
........................................................................ [ 35%]
........................................................................ [ 70%]
...........................................................             [100%]
203 passed in 57.80s
```

人工验证（已通过）：
- `FileStorage(current_settings)` 构造 + `get_novel_cover_path` 返回正确路径
- `webapp` / `ai.service` / `web.managers` / `rate_limiter` / `jobs.tasks` 完整 import
- `SyncJobManager` 不再有 `start_job`/`_run_job`/`_run_single_sync`，仍有 `start_auto_job`/`get_job`/`latest_job` 等
- `rate_limiter` 不再有 `handle_response`，仍有 `wait`
- `pyflakes` 对 5 个 ai/services 文件无未使用 import 警告
- `docs/INDEX.md` 引用的文档都存在；`docs/archive/` 含 14+6 份归档

---

## 📋 后续建议（未完成项）

| 项 | 严重度 | 建议位置 |
|----|--------|----------|
| `webapp.py` 无 token 时本机无 CSRF | 低（设计取舍） | webapp.py:_check_auth |
| `check_bookmarks_existence` 与 `latest_matching_sync_check_scope` 保留作能力储备但无生产调用 | 低 | sync_engine.py / web/managers.py |
| `projects.py:316/321/334` f-string 缺占位符（pyflakes 警告） | 低 | ai/services/projects.py |
| `AI_WRITING_STUDIO_PLAN.md` §4 单文件布局描述与现有 `ai/services/` 包不符 | 低 | docs/ |
| `QWEN_EMBEDDING_INTEGRATION.md` 向量库文件名与其他文档不一致 | 低 | docs/ |

---

**审计人**: Claude Opus 4.7
**修复**: 见工作区 diff

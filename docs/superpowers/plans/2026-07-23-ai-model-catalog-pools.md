# AI Model Catalog and Pools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不破坏既有固定 Agent 的前提下，增加 Provider 模型目录、有序模型池、后备池、统一跨 Provider 路由、可审计尝试记录和管理页面。

**Architecture:** SQLite 保存结构化模型、池成员、同步 operation、候选快照和尝试状态；Provider 只负责一个 Provider 的安全模型发现和单模型请求，`ModelRouter` 负责候选解析、PromptBudget、阶段化故障转移和 SSE 事件。所有生成服务通过同一个路由入口，设置页和任务日志页消费目录、池、尝试摘要 API。

**Tech Stack:** Python 3.10、Flask 3、SQLite/WAL、requests 2.32、Vue 3 CDN、SSE、pytest；不新增第三方依赖。

## Current Execution Status

- 已批准的收尾设计：`docs/superpowers/specs/2026-07-28-ai-model-routing-completion-design.md`。
- Task 1 已由 `c31791d` 完成，并由 `16b8587` 补充 legacy model key 约束。
- Task 2 已由 `816e690` 完成，并由 `9458cfe` 修正 canonical digest。
- Task 3 已由 `6c3cc3a` 完成，并由 `c63ac95` 固化 canonical metadata 存储边界。
- Task 0 已由 `0d881c1` 完成；Task 4-16 已分别由 `67beceb`、`b387578`、`3992bb8`、`c2b0fa3`、`b3c743f`、`58ae24b`、`16fc73a`、`1e5e969`、`7d1f560`、`d225bc4`、`65a2e8a`、`9dda110`、`ad52187` 完成；当前从 Task 17 继续，Task 17-22 必须按下方 TDD 步骤推进。

## Global Constraints

- 只实施规格第一阶段；连续失败健康计数、跨任务冷却、后台定时模型刷新、权重轮询和成本排序不在本计划范围内。
- Provider 类型只保留 `openai_compatible`、`anthropic`、`xai`；模型池 `pool_kind` 只接受 `primary`、`secondary`、`grok`、`custom`，`pool_kind` 不参与路由判断。
- 固定 Agent 继续优先使用 Agent `model`，为空时使用 Provider `default_model`；两者都为空在任何网络请求前返回中文配置错误，并且固定绑定不自动切换 Provider。
- 池候选按成员 `position` 和后备链顺序展开；同一 `(provider_id, model_key)` 在一条链中只出现一次；运行时访问集合阻止循环。
- 只有尚未产生用户正文的非空 `delta` 才允许切换；`metadata`、`progress` 和内部摘要 delta 不触发正文 pin；首个正文 delta 之后的 Provider 错误、`finish_reason=length`、`content_filter`、缺失结束标记或传输中断统一保留部分结果并收口为 `partial`，不得自动换模型。
- Provider 自身现有重试和流式转非流式降级保留；模型池切换只发生在单个 Provider 调用及其内部重试彻底失败之后。取消、客户端断开和 `GeneratorExit` 记录 `cancelled`，不切换。
- 一个池最多 64 个成员；展开去重后的整条后备链最多 64 个候选；后备链最多 8 个池节点（根节点计 1）；每个 job 最多实际尝试 16 个候选、最多 32 次网络请求、总 deadline 不超过 30 分钟。
- `model_key` 原样保留、按原始 UTF-8 字节去重，最多 300 个 Unicode 码点和 1200 个 UTF-8 字节，只拒绝控制字符；不做 NFC、大小写折叠或空白改写。显示名最多 200 个码点/800 字节；能力标签最多 64 项、每项最多 64 个码点；白名单元数据 canonical JSON 最多 8 KiB；超限整次同步失败，不截断。
- 有效模型条件固定为 `enabled=1 AND (manual=1 OR discovered_available=1)`；同步只更新 `discovered_*`、`discovered_available`、`last_seen_at`，永不覆盖 `manual_*`、`manual` 或用户 `enabled`；上游消失只标记不可用，不删除池引用行。
- Agent `required_capabilities_json` 只接受 `streaming`、`json`、`vision`、`tools`、`long_context` 的去重数组，最多 32 项；未知发现能力可展示但不参与路由；能力要求无满足候选时保存/启用 Agent 在事务内拒绝，运行时再次过滤并记录 `missing_capability`。
- Provider 模型同步单页响应体最多 4 MiB、单次累计最多 20 MiB、最多 100 页和 5000 个模型；同步总时限 10 分钟；分页未完整结束、游标循环、部分响应、结构化信封错误或上限耗尽均保留旧目录且不写缺失 tombstone。
- 非权威空数组进入 `needs_empty_confirmation`，释放网络租约；确认必须精确匹配 `operation_id`、最新 `generation`、Provider 配置哈希和 `result_digest`，否则返回 409；确认成功才将缺失模型标记不可用。同步失败、取消或超时只更新尝试时间和脱敏错误，不覆盖最近成功时间或旧目录。
- 所有模型同步请求复用现有 URL 校验、DNS 解析/IP 固定、Host/SNI 校验、代理、超时、禁止 3xx 重定向和密钥脱敏；不得写裸 `requests.get()`，API Key 永不回显。
- `candidate_snapshot_json` 最多 256 KiB；快照和 attempt 不保存 API Key、Prompt、正文、请求体或完整响应头。错误摘要最多 2000 个 Unicode 码点/8000 个字节，attempt `error_message` 最多 2000 字符；所有用户输入拒绝 NUL 和其他控制字符。
- `PromptBudget` 使用 `input_budget = effective_context_window - output_reserve - message_overhead - safety_margin`，`safety_margin` 固定 256；上下文窗口为 256 至 10,000,000 token，`max_tokens` 为 1 至 1,000,000；无有效窗口或预算不为正时调用前失败，候选切换不得扩大已截断输入。
- Schema 迁移、模型目录成功落库、池图校验、成员整体替换、Agent 绑定和 owner/generation 状态收口均使用 SQLite `BEGIN IMMEDIATE`；网络请求不持有写事务；所有 CAS 失败返回中文 409 或丢弃迟到 worker 响应。
- 旧库中的所有 Agent 迁移为 `binding_type='fixed'`，保留原 ID、名称、Prompt、参数、启用状态和 Provider/模型；旧前端提交的固定格式继续可用；无模型目录时固定 Agent 仍可使用手填模型。
- `available_models_json` 只在一次原子迁移中导入为 `manual` 目录项；新同步不再写入该字段。非法旧元素跳过并记录告警，全部非法时保留空人工目录且不标记 discovered。
- `ModelRouter` 必须提供成人润色计划依赖的只读契约：`ModelRouter.resolve_candidates(agent, stage='main', snapshot=None) -> CandidateSnapshot`、`ModelRouter.execute(request: RouteRequest) -> RouteResult`，并提供可流式消费的 `execute_stream()` 扩展；候选/配置哈希不包含 API Key、Prompt 或正文。
- 所有业务生成路径（续写、改写、蒸馏、审计、摘要、关键词清洗、向导聊天、全书规划、详细梗概、章节续写、对话/心理润色、去 AI 味、状态更新、伏笔回收、章节 Pipeline 内部调用）都必须经统一路由；仅 Provider 连接测试可在指定固定模型上直接调用 Provider。
- API 写接口沿用 Dashboard 会话、现有 CSRF 中间件和中文错误；模型池编辑页及 Agent 绑定摘要必须列出可能接收 Prompt 的 Provider 并显示跨 Provider 故障转移隐私提示。
- 每个任务严格先写失败测试、运行红灯、写最小实现、运行局部测试并提交；提交信息唯一、具体；不提交 API Key、数据库文件、服务器配置或生成正文。

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `src/pixiv_novel_sync/storage/ai/model_schema.py` | 模型目录、模型池、同步 operation、job routing 列和旧库原子迁移 DDL |
| `src/pixiv_novel_sync/storage/schema.py` | 在既有初始化流程中调用模型路由迁移并执行严格 FK 检查 |
| `src/pixiv_novel_sync/storage/ai/catalog.py` | Provider 模型目录 CRUD、派生字段、同步 upsert 和统计 |
| `src/pixiv_novel_sync/storage/ai/pools.py` | 模型池 CRUD、成员整体替换、版本 CAS、引用检查和尝试摘要 |
| `src/pixiv_novel_sync/storage/ai/model_sync.py` | 同步 operation 租约、heartbeat、generation、空确认和对账存储 |
| `src/pixiv_novel_sync/storage/ai/core.py` | Agent/job 绑定字段、owner CAS、attempt 分配、partial 清理和详情投影 |
| `src/pixiv_novel_sync/storage/tasks.py` | 统一日志投影支持 `partial` 和路由摘要 |
| `src/pixiv_novel_sync/ai/models.py` | Provider/Agent/ModelListResult、候选和流式完成状态的数据类型 |
| `src/pixiv_novel_sync/ai/model_catalog.py` | 模型 key、能力、显示名、元数据、canonical digest 和长度校验 |
| `src/pixiv_novel_sync/ai/model_pools.py` | 无副作用后备图、循环、深度和候选数校验 |
| `src/pixiv_novel_sync/ai/providers.py` | 安全 `_request`、三类 Provider 模型发现、完成原因和错误分类 |
| `src/pixiv_novel_sync/ai/model_sync.py` | 异步同步 coordinator、worker、SSE 状态和取消 |
| `src/pixiv_novel_sync/ai/model_router.py` | 成人契约 DTO、候选解析、PromptBudget、执行、切换、lease 和尝试日志 |
| `src/pixiv_novel_sync/ai/services/core.py` | AI 服务共享路由会话、job owner 和 SSE 适配辅助方法 |
| `src/pixiv_novel_sync/ai/services/admin.py` | Provider/Agent/目录/池/续接业务校验和配置哈希 |
| `src/pixiv_novel_sync/ai/services/generation.py` | 续写、改写、蒸馏、审计、规划、摘要内部调用迁移 |
| `src/pixiv_novel_sync/ai/services/chat_wizard.py` | 向导聊天统一路由迁移 |
| `src/pixiv_novel_sync/ai/services/projects.py` | 长篇规划、章节、状态、伏笔、润色和 Pipeline 迁移 |
| `src/pixiv_novel_sync/ai/services/__init__.py`、`src/pixiv_novel_sync/ai/service.py` | 暴露新 mixin 并保持 facade API |
| `src/pixiv_novel_sync/ai_web.py` | 模型同步/目录/池/续接 API、SSE、状态码和 startup 对账 |
| `src/pixiv_novel_sync/templates/dashboard_settings.html` | Provider 模型目录、模型池和 Agent 绑定设置页 |
| `src/pixiv_novel_sync/templates/dashboard_logs.html` | 实际 Provider/模型、尝试列表、partial 和“下一个模型继续” |
| `tests/test_ai_model_schema.py` | DDL、旧库迁移、FK、约束和 available_models 导入 |
| `tests/test_ai_model_catalog.py` | 归一化、目录 CRUD、同步 upsert 和统计 |
| `tests/test_ai_model_pools.py` | 后备图、成员排序、引用、版本 CAS 和能力约束 |
| `tests/test_ai_model_discovery.py` | 三类 Provider、分页、body 上限和安全 GET |
| `tests/test_ai_model_sync.py` | operation 租约、空确认、失败保留、取消和迟到 worker |
| `tests/test_ai_job_routing_storage.py` | owner/attempt/partial/cleanup/CAS 存储行为 |
| `tests/test_ai_model_router.py` | DTO、候选解析、预算、切换、阶段和 Provider 短路 |
| `tests/test_ai_model_router_integration.py` | 全调用链统一入口、固定 Agent 回归和多批 pin |
| `tests/test_ai_model_api.py` | 模型目录/同步/池/Agent/续接 API、CSRF、409 和脱敏 |
| `tests/test_ai_model_ui.py` | 设置页、日志页和隐私提示静态契约 |
| `README.md`、`docs/frontend-api-contract.md`、`docs/frontend-pages.md`、`docs/INDEX.md` | 新接口、配置、路由语义和旧 fallback 说明 |

---

## Test Fixture Contract

The snippets below use file-local fixtures/helpers defined before the first test in the named test module. Keep these names and signatures exact so tasks can be reviewed independently:

```python
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import Any, Generic, TypeVar

T = TypeVar("T")
R = TypeVar("R")

MESSAGES = [{"role": "user", "content": "hello"}]

def make_old_ai_database(path: Path) -> Database: ...
def seed_provider(db: Database, *, available_models: list[Any] | None = None) -> int: ...
def seed_models(db: Database, count: int = 2) -> tuple[int, ...]: ...
def seed_enabled_pool(db: Database) -> int: ...
def seed_agent_bound_to_pool(db: Database, pool_id: int) -> int: ...
def seed_synced_provider(db: Database, *, model_key: str) -> tuple[int, int]: ...
def seed_expired_job_and_attempt(db: Database, *, stage: str, output_started: int) -> None: ...
def seed_partial_job_with_snapshot_and_attempts(db: Database, *, attempted: tuple[int, ...], next_index: int) -> str: ...
def seed_fixed_agent(db: Database) -> AIAgentConfig: ...
def run_concurrently(callable_: Callable[[], T], *, count: int) -> list[T]: ...
def wait_for_operation(coordinator: ModelSyncCoordinator, operation_id: str) -> dict[str, Any]: ...
def expire_operation_lease(db: Database, operation_id: str) -> None: ...
def valid_attempt_data() -> dict[str, Any]: ...
def discovery_page(provider_type: str, model_ids: list[str], *, has_more: bool, cursor: str | None = None) -> dict[str, Any]: ...
def make_discovery_provider(provider_type: str, monkeypatch, *, pages: list[dict[str, Any]]) -> tuple[AIProvider, list[dict[str, Any]]]: ...
def make_openai_provider(monkeypatch, responses: Any) -> OpenAICompatibleProvider: ...
def make_openai_provider_with_choice(monkeypatch, *, text: str, finish_reason: str | None) -> OpenAICompatibleProvider: ...
def make_retry_then_nonstream_provider(monkeypatch) -> OpenAICompatibleProvider: ...
def make_error_provider(monkeypatch, status: int) -> OpenAICompatibleProvider: ...
def normal_done() -> AIStreamChunk: ...
def provider_failure() -> AIProviderError: ...
def success_result(text: str) -> RouteResult: ...
def partial_result(text: str) -> RouteResult: ...
def failed_before_output_result(*, stage: str) -> RouteResult: ...
def snapshot_with_context_windows(*windows: int) -> CandidateSnapshot: ...
class GeneratorCapture(Generic[T, R]):
    items: list[T]
    return_value: R

def collect_generator_return(generator: Generator[T, None, R]) -> GeneratorCapture[T, R]: ...
def collected_delta(chunks: Sequence[AIStreamChunk]) -> str: ...
def get_job_from_metadata(chunks: Sequence[AIStreamChunk]) -> dict[str, Any]: ...
def long_text_payload() -> dict[str, Any]: ...
def valid_longform_payload() -> dict[str, Any]: ...
def valid_multibatch_payload() -> dict[str, Any]: ...
def valid_continue_payload(parent_job_id: str, *, index: int) -> dict[str, Any]: ...
```

`StreamingResponse`, `FakeProviderRegistry`, `FakeModelRouter`, `route_request`, `internal_request`, `route_context`, `fixed_agent`, `pool_agent`, `pool_agent_session`, and `chapter_db` are concrete classes/pytest fixtures local to the module that first uses them. `FakeModelRouter` must implement the same `resolve_candidates`, `execute`, and `execute_stream` signatures as Task 11, store `requests` and `provider_calls`, and return FIFO `RouteResult` values; it may not silently accept a different DTO shape.

---

### Task 0: Isolate Scheduler Lifecycle in Web Tests

**Files:**
- Modify: `src/pixiv_novel_sync/webapp.py`
- Modify: `tests/test_webapp_jobs.py`
- Modify: `tests/test_rescue_api.py`

**Interfaces:**
- `create_app(config_path=None, env_path=None, *, start_scheduler: bool | None = None) -> Flask` keeps the existing production auto-detection when `start_scheduler is None`; `False` prevents scheduler startup for isolated tests and `True` starts it explicitly.
- The rescue API fixture passes `start_scheduler=False`, so its monkeypatches cannot race with a background catalog initialization worker.

- [x] **Step 1: Write the failing scheduler-isolation test**

```python
def test_create_app_can_disable_scheduler(monkeypatch, tmp_path):
    starts = []
    monkeypatch.setattr(AutoSyncScheduler, "start", lambda self: starts.append(self))
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    create_app(env_path=str(env_path), start_scheduler=False)
    assert starts == []
```

Update the `tests/test_rescue_api.py` app fixture to call `create_app(..., start_scheduler=False)`; do not suppress `PytestUnhandledThreadExceptionWarning` with a filter.

- [x] **Step 2: Run tests to verify the new API is absent and the warning is reproducible**

Run:

```powershell
python -m pytest tests/test_webapp_jobs.py::test_create_app_can_disable_scheduler -q
python -m pytest tests/test_rescue_api.py::test_dashboard_rescue_list_returns_503_before_first_refresh -q -W error::pytest.PytestUnhandledThreadExceptionWarning
```

Expected: the first command fails because `create_app` lacks `start_scheduler`; before fixture isolation, the second command fails when the scheduler worker observes the monkeypatched rebuild method.

- [x] **Step 3: Add the explicit scheduler startup override**

Change only the startup decision:

```python
auto_start_scheduler = not _is_debug or _is_werkzeug_reload
should_start_scheduler = (
    auto_start_scheduler if start_scheduler is None else bool(start_scheduler)
)
```

Keep registry ownership, production reloader detection and scheduler construction unchanged. Update the rescue fixture to pass `False`; do not add an environment-variable escape hatch.

- [x] **Step 4: Run focused and full tests with thread warnings promoted to errors**

Run:

```powershell
python -m pytest tests/test_webapp_jobs.py tests/test_rescue_api.py -q -W error::pytest.PytestUnhandledThreadExceptionWarning
python -m pytest -q -W error::pytest.PytestUnhandledThreadExceptionWarning
```

Expected: `608 passed, 4 skipped` or a higher pass count after adding the regression test, with no warning summary.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/webapp.py tests/test_webapp_jobs.py tests/test_rescue_api.py
git commit -m "test: isolate scheduler lifecycle in web fixtures"
```

### Task 1: Atomic Schema Migration and Routing Tables (Completed)

**Files:**
- Create: `src/pixiv_novel_sync/storage/ai/model_schema.py`
- Modify: `src/pixiv_novel_sync/storage/schema.py`
- Test: `tests/test_ai_model_schema.py`

**Interfaces:**
- Produces `migrate_model_routing_schema(conn: sqlite3.Connection) -> None` and `assert_model_routing_foreign_keys(conn: sqlite3.Connection) -> None`.
- `SchemaMixin._migrate_ai_tables()` calls the migration once after the existing AI base tables exist; the migration is idempotent and does not change existing fixed Agent IDs.

- [x] **Step 1: Write the failing migration tests**

```python
def test_old_ai_database_migrates_fixed_agents_and_imports_available_models(tmp_path):
    db = make_old_ai_database(tmp_path / "old.db")
    db.init_schema()
    agent = db.get_ai_agent(7)
    assert agent["id"] == 7
    assert agent["binding_type"] == "fixed"
    assert agent["provider_id"] == 3
    assert agent["model"] == "legacy-model"
    model = db.conn.execute(
        "SELECT model_key, manual, discovered, discovered_available FROM ai_provider_models"
    ).fetchone()
    assert tuple(model) == ("legacy-model", 1, 0, 0)
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_routing_schema_has_strict_pool_and_attempt_constraints(db):
    tables = {row[0] for row in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"ai_provider_models", "ai_model_pools", "ai_model_pool_members",
            "ai_model_sync_operations", "ai_job_model_attempts"} <= tables
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("INSERT INTO ai_model_pools(name, pool_kind, version) VALUES ('', 'custom', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("INSERT INTO ai_agents(name, task_type, binding_type, provider_id, model_pool_id, system_prompt) "
                        "VALUES ('bad', 'general', 'pool', 3, NULL, 's')")


def test_failed_model_schema_migration_rolls_back(tmp_path):
    db = make_old_ai_database(tmp_path / "rollback.db")
    db.conn.execute("PRAGMA foreign_keys=OFF")
    db.conn.execute("INSERT INTO ai_agents(id, name, task_type, provider_id, system_prompt) VALUES (99, 'orphan', 'general', 999, 's')")
    db.conn.commit()
    with pytest.raises(RuntimeError, match="foreign_key_check"):
        migrate_model_routing_schema(db.conn)
    assert db.conn.execute(
        "SELECT name FROM sqlite_master WHERE name='ai_provider_models'"
    ).fetchone() is None
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_model_schema.py -q`

Expected: FAIL because the migration helper, routing tables, binding columns and old-list import do not exist.

- [x] **Step 3: Implement the single-transaction migration**

Use `with self.transaction():` from `SchemaMixin` and issue individual `conn.execute()` calls; do not use `executescript()` inside the migration because it can commit before a later DDL error. Create these tables and constraints exactly:

```sql
CREATE TABLE ai_provider_models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider_id INTEGER NOT NULL REFERENCES ai_providers(id) ON DELETE CASCADE,
  model_key TEXT NOT NULL CHECK(length(model_key) > 0 AND length(model_key) <= 300
    AND length(CAST(model_key AS BLOB)) <= 1200),
  discovered INTEGER NOT NULL DEFAULT 0 CHECK(discovered IN (0,1)),
  manual INTEGER NOT NULL DEFAULT 0 CHECK(manual IN (0,1)),
  discovered_available INTEGER NOT NULL DEFAULT 0 CHECK(discovered_available IN (0,1)),
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
  discovered_display_name TEXT,
  manual_display_name TEXT,
  discovered_capabilities_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(discovered_capabilities_json)),
  manual_capabilities_json TEXT CHECK(manual_capabilities_json IS NULL OR json_valid(manual_capabilities_json)),
  discovered_context_window INTEGER CHECK(discovered_context_window IS NULL OR (discovered_context_window BETWEEN 256 AND 10000000)),
  manual_context_window INTEGER CHECK(manual_context_window IS NULL OR (manual_context_window BETWEEN 256 AND 10000000)),
  discovered_metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(discovered_metadata_json) AND length(CAST(discovered_metadata_json AS BLOB)) <= 8192),
  last_seen_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(provider_id, model_key)
)
```

Create `ai_model_pools(id, name TEXT NOT NULL CHECK(length(name)>0 AND length(name)<=100), description TEXT NOT NULL DEFAULT '', pool_kind TEXT NOT NULL CHECK(pool_kind IN ('primary','secondary','grok','custom')), fallback_pool_id INTEGER REFERENCES ai_model_pools(id) ON DELETE RESTRICT, enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)), version INTEGER NOT NULL DEFAULT 1 CHECK(version>0), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(name))`; create `ai_model_pool_members(pool_id INTEGER NOT NULL REFERENCES ai_model_pools(id) ON DELETE CASCADE, provider_model_id INTEGER NOT NULL REFERENCES ai_provider_models(id) ON DELETE RESTRICT, position INTEGER NOT NULL CHECK(position>0), enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(pool_id, provider_model_id), UNIQUE(pool_id, position))`.

Add the following nullable/owner columns to `ai_agents` by rebuilding the table in the same transaction: `binding_type TEXT NOT NULL DEFAULT 'fixed' CHECK(binding_type IN ('fixed','pool'))`, `model_pool_id INTEGER REFERENCES ai_model_pools(id) ON DELETE RESTRICT`, `required_capabilities_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(required_capabilities_json))`, and `binding_version INTEGER NOT NULL DEFAULT 1 CHECK(binding_version>0)`. Preserve every old column, ID and timestamp. The table-level binding check must be:

```sql
CHECK ((binding_type='fixed' AND provider_id IS NOT NULL AND model_pool_id IS NULL)
    OR (binding_type='pool' AND provider_id IS NULL AND model IS NULL AND model_pool_id IS NOT NULL))
```

Rebuild `ai_jobs` to add `candidate_snapshot_json TEXT CHECK(candidate_snapshot_json IS NULL OR length(CAST(candidate_snapshot_json AS BLOB)) <= 262144)`, `candidate_snapshot_hash TEXT`, `next_attempt_index INTEGER NOT NULL DEFAULT 0 CHECK(next_attempt_index>=0)`, `owner_token`, `lease_until`, `heartbeat_at`, `stage TEXT NOT NULL DEFAULT 'main' CHECK(stage IN ('internal','main','validation'))`, `pinned_candidate_index`, `network_request_count INTEGER NOT NULL DEFAULT 0 CHECK(network_request_count>=0)`, `candidate_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(candidate_attempt_count>=0)`, `route_deadline_at`, `prompt_budget_json`, `parent_job_id`, and `idempotency_key`; preserve legacy rows and allow `candidate_snapshot_json` to be NULL. Add a partial unique index on `(parent_job_id, idempotency_key)` when both are non-NULL. Keep the existing job statuses and enforce `CHECK(status IN ('running','succeeded','failed','partial','cancelled'))`. Create `ai_job_model_attempts` with `job_id TEXT NOT NULL REFERENCES ai_jobs(job_id) ON DELETE CASCADE`, `attempt_index INTEGER NOT NULL CHECK(attempt_index>=0)`, numeric snapshot columns, `model_key TEXT`, `pool_name_snapshot TEXT`, `provider_name_snapshot TEXT`, `agent_config_hash TEXT NOT NULL`, `provider_config_hash TEXT NOT NULL`, `candidate_list_hash TEXT NOT NULL`, `stage CHECK(stage IN ('internal','main','validation'))`, `status CHECK(status IN ('running','succeeded','failed','partial','cancelled'))`, `error_scope CHECK(error_scope IS NULL OR error_scope IN ('model','provider'))`, `error_message TEXT`, `error_category TEXT`, `finish_reason TEXT CHECK(finish_reason IS NULL OR finish_reason IN ('stop','complete','length','content_filter','missing','cancelled','error'))`, `output_started INTEGER NOT NULL DEFAULT 0 CHECK(output_started IN (0,1))`, owner/lease/heartbeat timestamps, started/finished timestamps and `latency_ms INTEGER`; add `UNIQUE(job_id, attempt_index)`.

Create `ai_model_sync_operations(operation_id TEXT PRIMARY KEY, provider_id INTEGER NOT NULL, provider_name_snapshot TEXT NOT NULL, provider_config_hash TEXT NOT NULL, owner_token TEXT, status TEXT NOT NULL CHECK(status IN ('queued','running','needs_empty_confirmation','succeeded','failed','cancelled')), pages INTEGER NOT NULL DEFAULT 0, discovered_count INTEGER NOT NULL DEFAULT 0, result_digest TEXT, partial_reason TEXT, error_code TEXT, error_message TEXT, generation INTEGER NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)), lease_until TEXT, heartbeat_at TEXT, started_at TEXT, finished_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)` with indexes on provider/status/lease/generation. Add provider sync columns `models_synced_at`, `models_sync_attempted_at`, `models_sync_error`, `models_sync_generation INTEGER NOT NULL DEFAULT 0`, `models_sync_owner`, `models_sync_lease_until` using `ALTER TABLE` only when absent.

Import only valid strings or object `id` strings from old `available_models_json` into manual rows using the model-key validator; log one warning per skipped element and never set `discovered_available=1`. Finish with `PRAGMA foreign_keys=ON` and a non-empty `PRAGMA foreign_key_check` raising `RuntimeError`, so the transaction rolls back. Add `prepare_model_routing_downgrade(conn)` that raises `RuntimeError("存在模型池 Agent，请先转换为固定绑定")` when any `binding_type='pool'` row exists; otherwise it returns a read-only fixed-agent compatibility report rather than pretending a lossless downgrade.

- [x] **Step 4: Run migration tests to verify they pass**

Run: `python -m pytest tests/test_ai_model_schema.py -q`

Expected: PASS, including rollback with no partially created routing tables and an empty foreign-key check.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/storage/ai/model_schema.py src/pixiv_novel_sync/storage/schema.py tests/test_ai_model_schema.py
git commit -m "feat: add atomic model routing schema migration"
```

### Task 2: Model Normalization and Shared Routing DTOs (Completed)

**Files:**
- Create: `src/pixiv_novel_sync/ai/model_catalog.py`
- Modify: `src/pixiv_novel_sync/ai/models.py`
- Test: `tests/test_ai_model_catalog.py`

**Interfaces:**
- Produces `ModelListResult`, `normalize_model_record(raw: Mapping[str, Any]) -> dict[str, Any]`, `normalize_model_key(value: str) -> str`, `normalize_capabilities(value: Any, *, reject_unknown: bool = False) -> tuple[str, ...]`, `canonical_model_digest(models: Sequence[Mapping[str, Any]]) -> str`, and `validate_text_field(value, field, codepoint_limit, byte_limit) -> str`.
- `ModelListResult` has exactly `models: list[dict[str, Any]]`, `complete: bool`, `empty_authoritative: bool`, `pages: int`, `result_digest: str`, and `partial_reason: str | None` fields.
- The domain-error contract is fixed across modules: `model_catalog.py` produces `ModelCatalogValidationError(ValueError)` and `ModelCatalogConflictError(RuntimeError)`, `model_pools.py` produces `ModelPoolValidationError(ValueError)` and `ModelPoolConflictError(RuntimeError)`, `model_sync.py` produces `ModelSyncConflictError(RuntimeError)`, and `model_router.py` produces `ModelRouteError(RuntimeError)` and `ModelRouteConflictError(ModelRouteError)`; storage/service layers translate conflict errors to `AIConflictError` rather than matching message strings.

- [x] **Step 1: Write the failing normalization tests**

```python
def test_model_key_is_opaque_but_display_fields_are_nfc_normalized():
    raw = {"id": "  A\u0301/模型  ", "name": "e\u0301", "capabilities": ["streaming", "unknown"]}
    item = normalize_model_record(raw)
    assert item["model_key"] == "  A\u0301/模型  "
    assert item["display_name"] == "é"
    assert item["capabilities"] == ["streaming", "unknown"]


def test_invalid_control_character_or_length_fails_without_truncation():
    with pytest.raises(ModelCatalogValidationError, match="model_key"):
        normalize_model_key("ok\n")
    with pytest.raises(ModelCatalogValidationError, match="display_name"):
        normalize_model_record({"id": "m", "name": "x" * 201})


def test_digest_deduplicates_by_original_model_key_and_is_stable():
    first = canonical_model_digest([{"model_key": "B"}, {"model_key": "A"}])
    second = canonical_model_digest([{"model_key": "A"}, {"model_key": "B"}])
    assert first == second
    with pytest.raises(ModelCatalogValidationError):
        normalize_model_record({"id": "m\u0000"})
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_model_catalog.py -q`

Expected: FAIL because normalization and `ModelListResult` are absent.

- [x] **Step 3: Implement exact validation rules**

Use `unicodedata.normalize('NFC', value)` only for display names, capability labels and the whitelist metadata strings `owned_by`, `context_window`, `created`, and `capabilities`; keep model keys byte-for-byte unchanged. Reject NUL and every Unicode control category, enforce both code-point and UTF-8 byte limits, require a non-empty `id`/`model_key`, and reject non-object model records. Normalize capabilities by preserving first occurrence order and retaining unknown labels for display; `required_capabilities` uses a separate whitelist validator that rejects unknown labels and duplicates. Construct metadata from only `owned_by`, `capabilities`, `context_window`, and `created`, serialize with compact sorted JSON, and reject a serialized value over 8192 bytes. `canonical_model_digest` sorts by the original model key and hashes compact UTF-8 JSON with SHA-256 lowercase hex. Do not use heuristic secret filtering as a substitute for the whitelist.

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ai_model_catalog.py -q`

Expected: PASS, including opaque Unicode key, NFC display normalization, unknown capability display and hard limits.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/model_catalog.py src/pixiv_novel_sync/ai/models.py tests/test_ai_model_catalog.py
git commit -m "feat: add model catalog normalization contracts"
```

### Task 3: Provider Model Catalog Storage (Completed)

**Files:**
- Create: `src/pixiv_novel_sync/storage/ai/catalog.py`
- Modify: `src/pixiv_novel_sync/storage_db.py`
- Test: `tests/test_ai_model_catalog.py`

**Interfaces:**
- `Database.list_ai_provider_models(provider_id: int, *, search: str | None = None, routable_only: bool = False, enabled_only: bool = False) -> dict[str, Any]` returns `items`, `total`, `discovered_available`, and `routable` counts.
- `Database.get_ai_provider_model(model_id: int) -> dict[str, Any] | None`, `create_ai_provider_model(data: Mapping[str, Any]) -> int`, `update_ai_provider_model(model_id: int, patch: Mapping[str, Any]) -> None`, `remove_ai_provider_model_manual(model_id: int) -> None`, and `upsert_discovered_models(provider_id: int, models: Sequence[Mapping[str, Any]], generation: int) -> dict[str, int]`.
- Returned rows include derived `source` (`discovered`, `manual`, or `both`), `display_name`, effective `capabilities`, effective `context_window`, and `routable`; API responses never include encrypted Provider fields.

- [x] **Step 1: Write the failing storage tests**

```python
def test_catalog_upsert_preserves_manual_overrides_and_marks_missing_unavailable(db):
    provider_id = seed_provider(db, available_models=["legacy"])
    model_id = db.create_ai_provider_model({
        "provider_id": provider_id, "model_key": "manual", "manual_display_name": "手工名",
        "manual_capabilities": ["json"], "enabled": 1,
    })
    db.upsert_discovered_models(provider_id, [
        {"model_key": "manual", "display_name": "上游名", "capabilities": ["streaming"]},
        {"model_key": "new", "display_name": "新模型"},
    ], generation=1)
    db.upsert_discovered_models(provider_id, [{"model_key": "new"}], generation=2)
    row = db.get_ai_provider_model(model_id)
    assert row["manual_display_name"] == "手工名"
    assert row["manual_capabilities"] == ["json"]
    assert row["discovered_available"] is False
    assert db.list_ai_provider_models(provider_id)["routable"] == 2


def test_manual_model_delete_is_blocked_when_pool_member_references_it(db):
    provider_id = seed_provider(db)
    model_id = db.create_ai_provider_model({"provider_id": provider_id, "model_key": "m"})
    pool_id = db.create_ai_model_pool({"name": "p", "pool_kind": "custom"})
    db.replace_ai_model_pool_members(pool_id, [{"provider_model_id": model_id, "position": 1}], expected_version=1)
    with pytest.raises(ModelCatalogConflictError, match="模型池"):
        db.remove_ai_provider_model_manual(model_id)
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_model_catalog.py -q`

Expected: FAIL because `CatalogMixin` and its tables/methods are not connected to `Database`.

- [x] **Step 3: Implement transaction-safe catalog CRUD**

Add `CatalogMixin` to `Database` without changing the order of existing AI mixins. `upsert_discovered_models` must run one `BEGIN IMMEDIATE`: insert new rows, update only discovered fields for existing rows, set all previously discovered rows not in the complete result to `discovered_available=0`, and leave `manual`, every `manual_*`, and `enabled` untouched. `remove_ai_provider_model_manual` clears `manual` and manual overrides; delete the row only when `discovered=0` and no pool member references it, otherwise retain the discovered row or return the Chinese conflict error. `list_ai_provider_models` computes the three counts independently: `total` all rows, `discovered_available` rows with that flag, and `routable` rows satisfying the effective-source and enabled conditions plus enabled Provider. Search uses a bound `LIKE` parameter and never returns `api_key_encrypted` or `available_models_json` as a source of truth.

- [x] **Step 4: Run storage tests to verify they pass**

Run: `python -m pytest tests/test_ai_model_catalog.py -q`

Expected: PASS, including duplicate upsert, manual-field preservation, missing-model marking and reference protection.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/storage/ai/catalog.py src/pixiv_novel_sync/storage_db.py tests/test_ai_model_catalog.py
git commit -m "feat: add provider model catalog storage"
```

### Task 4: Ordered Pools, Fallback Graphs and Version CAS

**Files:**
- Create: `src/pixiv_novel_sync/ai/model_pools.py`
- Create: `src/pixiv_novel_sync/storage/ai/pools.py`
- Modify: `src/pixiv_novel_sync/storage_db.py`
- Test: `tests/test_ai_model_pools.py`

**Interfaces:**
- Pure functions: `validate_pool_graph(pools: Sequence[Mapping[str, Any]], members: Mapping[int, Sequence[Mapping[str, Any]]], root_pool_id: int | None = None) -> None` and `expand_pool_ids(root_pool_id: int, pools: Mapping[int, Mapping[str, Any]]) -> tuple[int, ...]`; failures raise `ModelPoolValidationError` with a stable Chinese message.
- `Database.list_ai_model_pools()`, `get_ai_model_pool(pool_id)`, `create_ai_model_pool(data)`, `update_ai_model_pool(pool_id, data, expected_version)`, `delete_ai_model_pool(pool_id)`, `replace_ai_model_pool_members(pool_id, members, expected_version)`, and `list_ai_model_pool_attempts(pool_id, limit=50)`.
- `replace_ai_model_pool_members` accepts a complete ordered list of `{provider_model_id, enabled}` and atomically rewrites positions `1..n`; it returns the incremented pool version.

- [ ] **Step 1: Write the failing graph and CAS tests**

```python
def test_pool_graph_rejects_direct_and_indirect_cycles(db):
    a = db.create_ai_model_pool({"name": "一级", "pool_kind": "primary"})
    b = db.create_ai_model_pool({"name": "二级", "pool_kind": "secondary"})
    db.update_ai_model_pool(a, {"fallback_pool_id": b}, expected_version=1)
    with pytest.raises(ModelPoolValidationError, match="循环"):
        db.update_ai_model_pool(b, {"fallback_pool_id": a}, expected_version=1)


def test_members_replace_in_one_transaction_and_version_conflict_is_409(db):
    pool_id = db.create_ai_model_pool({"name": "顺序池", "pool_kind": "custom"})
    first, second = seed_models(db, count=2)
    db.replace_ai_model_pool_members(pool_id, [
        {"provider_model_id": second, "enabled": 1},
        {"provider_model_id": first, "enabled": 1},
    ], expected_version=1)
    rows = db.conn.execute(
        "SELECT provider_model_id, position FROM ai_model_pool_members WHERE pool_id=? ORDER BY position",
        (pool_id,),
    ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [(second, 1), (first, 2)]
    with pytest.raises(ModelPoolConflictError, match="版本"):
        db.replace_ai_model_pool_members(pool_id, [], expected_version=1)


def test_referenced_pool_cannot_be_disabled_or_deleted(db):
    pool_id = seed_enabled_pool(db)
    seed_agent_bound_to_pool(db, pool_id)
    with pytest.raises(ModelPoolConflictError, match="引用"):
        db.update_ai_model_pool(pool_id, {"enabled": 0}, expected_version=1)
    with pytest.raises(ModelPoolConflictError, match="引用"):
        db.delete_ai_model_pool(pool_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_model_pools.py -q`

Expected: FAIL because graph validation, pool storage and conflict types do not exist.

- [ ] **Step 3: Implement graph validation and storage transactions**

In `model_pools.py`, traverse fallback edges with a `visiting` set and `visited` set, reject self-edge and any repeated node in the current path, reject depth greater than 8, count each pool's members and the deduplicated `(provider_id, model_key)` candidates across the expanded chain, and reject more than 64. Permit an empty disabled pool only; reject enabling, Agent binding or use as an enabled pool's fallback when empty. In `pools.py`, every create/update/fallback/member replacement starts `BEGIN IMMEDIATE`, re-reads all pools and members, validates the complete graph, checks `expected_version`, checks pool and model references, then writes members and `version = version + 1` in the same transaction. A referenced pool (Agent `model_pool_id` or another pool `fallback_pool_id`) cannot be deleted, disabled or emptied. `list_ai_model_pool_attempts` joins only immutable attempt snapshots and never requires current configuration rows.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ai_model_pools.py -q`

Expected: PASS, including depth/candidate limits, empty-pool rules, ordering, rollback and stale-version conflict.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/model_pools.py src/pixiv_novel_sync/storage/ai/pools.py src/pixiv_novel_sync/storage_db.py tests/test_ai_model_pools.py
git commit -m "feat: add ordered model pools and fallback graph validation"
```

### Task 5: Agent Binding Migration and Administration

**Files:**
- Modify: `src/pixiv_novel_sync/ai/models.py`
- Modify: `src/pixiv_novel_sync/storage/ai/core.py`
- Modify: `src/pixiv_novel_sync/ai/services/admin.py`
- Modify: `src/pixiv_novel_sync/ai/services/core.py`
- Modify: `tests/test_ai_model_pools.py`
- Create: `tests/test_ai_agent_bindings.py`

**Interfaces:**
- `AIAgentConfig` adds `binding_type: Literal['fixed','pool']`, `model_pool_id: int | None`, `required_capabilities: tuple[str, ...]`, and `binding_version: int`; `provider_id` becomes `int | None` while existing constructor calls with fixed fields remain valid.
- `AIAdminMixin.create_agent`, `update_agent`, `_normalize_agent_payload`, and `_load_agent_config` accept/return the new fields and reject fixed+pool mixed payloads, invalid capability labels, duplicate labels, and more than 32 requirements.
- `AIAdminMixin.delete_provider` checks fixed Agent references and pool/catalog references in one transaction and raises `AIConflictError` with a Chinese message before deletion.

- [ ] **Step 1: Write the failing binding tests**

```python
def test_pool_agent_cannot_submit_provider_or_model(service, db):
    pool_id = seed_enabled_pool(db)
    with pytest.raises(AIServiceError, match="固定模型和模型池不能同时提交"):
        service.create_agent({
            "name": "混合", "task_type": "general", "provider_id": 1,
            "model": "m", "model_pool_id": pool_id, "binding_type": "pool",
            "system_prompt": "s",
        })


def test_fixed_agent_with_required_capability_needs_catalog_model(service, db):
    with pytest.raises(AIServiceError, match="能力"):
        service.create_agent({
            "name": "JSON", "task_type": "general", "provider_id": 1,
            "model": "unknown", "required_capabilities": ["json"],
            "system_prompt": "s",
        })


def test_old_agent_values_and_id_are_preserved_after_reload(tmp_path):
    db = make_old_ai_database(tmp_path / "legacy.db")
    db.init_schema()
    row = db.get_ai_agent(7)
    assert (row["id"], row["binding_type"], row["provider_id"], row["model"]) == (7, "fixed", 3, "legacy-model")
    assert row["required_capabilities"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_agent_bindings.py tests/test_ai_model_pools.py -q`

Expected: FAIL because `AIAgentConfig` and Agent storage still require `provider_id` and do not validate binding type or capabilities.

- [ ] **Step 3: Implement compatibility-aware Agent CRUD**

Parse `required_capabilities` from either the new list field or legacy `required_capabilities_json`, canonicalize to a sorted unique tuple for storage, and retain an empty array for all migrated Agents. For `fixed`, require an existing enabled Provider, allow an unknown hand-entered model only when requirements are empty, and set `model_pool_id=NULL`. For `pool`, require an enabled non-empty pool whose expanded candidates cover every required capability, set `provider_id=NULL` and `model=NULL`, and increment `binding_version` on each update. Return `model_pool_name`, `binding_summary`, and parsed `required_capabilities` in list/get responses. Keep `seed_builtin_agents(provider_id)` fixed and preserve all existing task types.

Implement `AIConflictError(AIServiceError)` in `ai/services/core.py`; make `ai_web.fail()` map it to HTTP 409 while retaining 400 for validation errors. Provider deletion must use one database transaction to query fixed Agents, pool members, and model rows; if any reference exists, return a conflict before attempting `DELETE`. Do not use a fake Provider ID for pool bindings.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ai_agent_bindings.py tests/test_ai_model_pools.py tests/test_ai_service_facade.py -q`

Expected: PASS, including legacy fixed Agent behavior, mutual exclusion, capability checks and Provider deletion conflicts.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/models.py src/pixiv_novel_sync/storage/ai/core.py src/pixiv_novel_sync/ai/services/admin.py src/pixiv_novel_sync/ai/services/core.py tests/test_ai_model_pools.py tests/test_ai_agent_bindings.py
git commit -m "feat: support fixed and model-pool Agent bindings"
```

### Task 6: Secure Provider Model Discovery

**Files:**
- Modify: `src/pixiv_novel_sync/ai/providers.py`
- Modify: `src/pixiv_novel_sync/ai/models.py`
- Create: `tests/test_ai_model_discovery.py`
- Modify: `tests/test_ai_security_hardening.py`
- Modify: `tests/test_ai_providers_fallback.py`

**Interfaces:**
- `AIProvider._request(method: str, url: str, *, max_body_bytes: int | None = None, byte_budget: ResponseByteBudget | None = None, **kwargs) -> requests.Response` replaces `_post`; `_post` remains a compatibility wrapper calling `_request('POST', ...)`.
- `AIProvider.list_models(*, on_page: Callable[[int, int], None] | None = None, is_cancelled: Callable[[], bool] | None = None) -> ModelListResult` is callable with no arguments as required by the design.
- `ResponseByteBudget(limit: int = 20 * 1024 * 1024)` exposes `consume(count: int) -> None`; a page passes `max_body_bytes=4 * 1024 * 1024`.

- [ ] **Step 1: Write the failing discovery and security tests**

```python
@pytest.mark.parametrize("provider_type", ["openai_compatible", "xai", "anthropic"])
def test_provider_lists_all_pages_with_canonical_digest(provider_type, monkeypatch):
    provider, calls = make_discovery_provider(provider_type, monkeypatch, pages=[
        discovery_page(provider_type, ["m-1"], has_more=True, cursor="next-1"),
        discovery_page(provider_type, ["m-2"], has_more=False),
    ])
    result = provider.list_models()
    assert [item["model_key"] for item in result.models] == ["m-1", "m-2"]
    assert result.complete is True
    assert result.pages == 2
    assert len(result.result_digest) == 64
    assert all(call["method"] == "GET" for call in calls)
    assert all(call["allow_redirects"] is False for call in calls)


def test_model_list_body_limit_fires_before_json_decode(monkeypatch):
    response = StreamingResponse([b"x" * (4 * 1024 * 1024), b"x"])
    provider = make_openai_provider(monkeypatch, response)
    with pytest.raises(AIProviderError, match="4 MiB"):
        provider.list_models()
    assert response.json_calls == 0


def test_cursor_loop_and_malformed_envelope_are_not_empty_success(monkeypatch):
    provider = make_openai_provider(monkeypatch, responses=[
        {"data": [{"id": "m"}], "has_more": True, "next": "same"},
        {"data": [{"id": "m"}], "has_more": True, "next": "same"},
    ])
    with pytest.raises(AIProviderError, match="分页游标循环"):
        provider.list_models()
    provider = make_openai_provider(monkeypatch, responses=[{"object": "list"}])
    with pytest.raises(AIProviderError, match="模型数组"):
        provider.list_models()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_model_discovery.py tests/test_ai_security_hardening.py tests/test_ai_providers_fallback.py -q`

Expected: FAIL because Providers have no GET/list interface and `_post` cannot enforce model-list byte budgets.

- [ ] **Step 3: Refactor one safe request path and implement adapters**

Move the existing target resolution, IP pinning, Host header, TLS hostname, proxy, timeout, response hook, lazy body handling and 3xx rejection into `_request`. Dispatch through `self.session.request` or the method-specific session function while retaining `_post` tests; always set `allow_redirects=False` and `stream=True` at transport level. When `max_body_bytes` is set, consume `iter_content` into a bounded `bytes` buffer, charge both the page and cumulative `ResponseByteBudget` before JSON decoding, close the response on overflow, and parse UTF-8 JSON only after the cap succeeds.

For OpenAI-compatible and xAI, request `<resolved_base_url>/models` with Bearer auth and accept only an object containing a `data` array. For Anthropic, request `<base_url>/v1/models` with `x-api-key`, `anthropic-version: 2023-06-01`, and require its documented array envelope. Every array element must be an object with a valid string ID; any invalid element fails the whole sync page. Support explicit pagination fields `has_more`, `next`, `after`, and `last_id`; reject wrong types, partial markers, repeated cursor, more than 100 pages, more than 5000 normalized models, or an upstream next-page declaration at either limit. Default all three adapters to `empty_authoritative=False`; an adapter may return true only from an explicitly tested structured provider capability, never from list length. Call `on_page(page_count, discovered_count)` after each validated page and check `is_cancelled()` before every network request.

- [ ] **Step 4: Run discovery and existing Provider tests**

Run: `python -m pytest tests/test_ai_model_discovery.py tests/test_ai_security_hardening.py tests/test_ai_providers_fallback.py -q`

Expected: PASS, including GET DNS pinning, redirect rejection before body read, cumulative 20 MiB cap, strict envelopes and all three Provider formats.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/providers.py src/pixiv_novel_sync/ai/models.py tests/test_ai_model_discovery.py tests/test_ai_security_hardening.py tests/test_ai_providers_fallback.py
git commit -m "feat: add secure paginated Provider model discovery"
```

### Task 7: Asynchronous Model Sync Operations and Leases

**Files:**
- Create: `src/pixiv_novel_sync/storage/ai/model_sync.py`
- Create: `src/pixiv_novel_sync/ai/model_sync.py`
- Modify: `src/pixiv_novel_sync/storage_db.py`
- Modify: `src/pixiv_novel_sync/ai/services/core.py`
- Test: `tests/test_ai_model_sync.py`

**Interfaces:**
- Storage produces `create_model_sync_operation(provider_id, provider_name, provider_config_hash, owner_token) -> dict`, `claim_model_sync_operation(operation_id, owner_token, generation) -> bool`, `heartbeat_model_sync_operation(...) -> bool`, `update_model_sync_progress(...) -> bool`, `finish_model_sync_success(...) -> bool`, `finish_model_sync_failure(...) -> bool`, `request_model_sync_cancel(operation_id) -> bool`, `confirm_model_sync_empty(...) -> dict[str, int]`, `reconcile_model_sync_operations(now: datetime | None = None) -> int`, and `cleanup_model_sync_operations(keep_days=3) -> int`.
- `ModelSyncCoordinator.start(provider_id: int) -> dict[str, Any]`, `get(operation_id)`, `cancel(operation_id)`, `confirm_empty(operation_id, generation, result_digest)`, `events(operation_id, poll_interval=0.25) -> Iterator[dict[str, Any]]`, `reconcile()`, and `close()`.
- `start` returns a queued operation immediately; one `ThreadPoolExecutor(max_workers=2, thread_name_prefix='ai-model-sync')` per `AIWritingService` runs workers and is closed by `AIServiceCore.close()`.
- `AIServiceCore` exposes the coordinator through `start_model_sync`, `get_model_sync_operation`, `cancel_model_sync`, `confirm_model_sync_empty`, and `iter_model_sync_events`; Task 8 adds HTTP wrappers without changing these signatures.

- [ ] **Step 1: Write the failing operation tests**

```python
def test_sync_failure_preserves_previous_catalog_and_success_time(service, db, fake_provider):
    provider_id, old_model_id = seed_synced_provider(db, model_key="old")
    before = db.get_ai_provider(provider_id)
    fake_provider.list_error = AIProviderError("sk-secret upstream failed")
    operation = service.start_model_sync(provider_id)
    final = wait_for_operation(service, operation["operation_id"])
    assert final["status"] == "failed"
    assert "sk-secret" not in final["error_message"]
    assert db.get_ai_provider_model(old_model_id)["discovered_available"] is True
    assert db.get_ai_provider(provider_id)["models_synced_at"] == before["models_synced_at"]


def test_non_authoritative_empty_requires_exact_confirmation(service, db, fake_provider):
    provider_id, old_model_id = seed_synced_provider(db, model_key="old")
    fake_provider.model_result = ModelListResult([], True, False, 1, EMPTY_DIGEST, None)
    operation = service.start_model_sync(provider_id)
    waiting = wait_for_operation(service, operation["operation_id"])
    assert waiting["status"] == "needs_empty_confirmation"
    assert db.get_ai_provider_model(old_model_id)["discovered_available"] is True
    with pytest.raises(ModelSyncConflictError):
        service.confirm_model_sync_empty(operation["operation_id"], waiting["generation"] + 1, EMPTY_DIGEST)
    service.confirm_model_sync_empty(operation["operation_id"], waiting["generation"], EMPTY_DIGEST)
    assert db.get_ai_provider_model(old_model_id)["discovered_available"] is False


def test_late_worker_cannot_overwrite_new_generation(db):
    first = db.create_model_sync_operation(1, "p", "a" * 64, "owner-a")
    expire_operation_lease(db, first["operation_id"])
    second = db.create_model_sync_operation(1, "p", "b" * 64, "owner-b")
    assert db.finish_model_sync_success(first["operation_id"], "owner-a", first["generation"], [], "c" * 64) is False
    assert db.get_model_sync_operation(second["operation_id"])["status"] == "queued"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_model_sync.py -q`

Expected: FAIL because sync operation storage, worker and generation/owner CAS are absent.

- [ ] **Step 3: Implement operation lifecycle and reconciliation**

Acquire a Provider lease in a short `BEGIN IMMEDIATE`: reject an unexpired queued/running operation with `ModelSyncConflictError(existing_operation_id)`, otherwise increment `models_sync_generation`, assign a random 32-byte URL-safe owner, set a 45-second lease, and insert `queued`. The worker claims only `queued -> running` with matching owner/generation, calls `list_models` outside transactions, updates page/count and heartbeat through callbacks, and enforces an absolute 10-minute deadline. Final success performs one CAS transaction that rechecks Provider config hash/generation/owner, upserts the complete directory, marks missing discovered rows unavailable, clears sync error, writes `models_synced_at`, and changes only `running -> succeeded`.

For a complete non-authoritative empty result, write digest/config/generation and transition `running -> needs_empty_confirmation`, clear Provider owner/lease immediately, and do not mark anything missing. `confirm_empty` re-reads the exact operation and latest Provider config/generation under `BEGIN IMMEDIATE`; mismatch or a newer sync is 409. Cancellation sets `cancel_requested=1`; callbacks stop before a new page/request and terminal CAS writes `cancelled` without modifying the directory. Reconciliation marks queued rows older than 5 minutes `failed/queue_timeout`, and running rows whose lease and heartbeat exceed the grace period or whose Provider owner/generation no longer match `failed/process_interrupted`; terminal rows never change. Keep operation rows for 3 days and save no upstream body.

- [ ] **Step 4: Run operation tests to verify they pass**

Run: `python -m pytest tests/test_ai_model_sync.py -q`

Expected: PASS, including failure preservation, manual-field preservation, cancellation, empty confirmation, queue timeout, crashed worker and late CAS.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/storage/ai/model_sync.py src/pixiv_novel_sync/ai/model_sync.py src/pixiv_novel_sync/storage_db.py src/pixiv_novel_sync/ai/services/core.py tests/test_ai_model_sync.py
git commit -m "feat: add leased asynchronous model sync operations"
```

### Task 8: Model Catalog, Sync and Pool APIs

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/admin.py`
- Modify: `src/pixiv_novel_sync/ai/services/__init__.py`
- Modify: `src/pixiv_novel_sync/ai/service.py`
- Modify: `src/pixiv_novel_sync/ai_web.py`
- Create: `tests/test_ai_model_api.py`

**Interfaces:**
- Service exposes `list_provider_models`, `create_manual_model`, `update_provider_model`, `delete_provider_model`, `start_model_sync`, `get_model_sync_operation`, `cancel_model_sync`, `confirm_model_sync_empty`, `iter_model_sync_events`, pool CRUD/member methods, and `list_model_pool_attempts`.
- Routes are exactly those in design sections 9.1 and 9.2; sync start returns HTTP 202, active-operation and version/reference conflicts return 409, missing IDs return 404, and validation errors return 400.

- [ ] **Step 1: Write failing API contract tests**

```python
def test_sync_start_is_202_and_duplicate_is_409(client, csrf, seeded_provider):
    first = client.post(f"/api/dashboard/ai/providers/{seeded_provider}/models/sync",
                        headers={"X-CSRF-Token": csrf})
    assert first.status_code == 202
    operation_id = first.get_json()["data"]["operation_id"]
    duplicate = client.post(f"/api/dashboard/ai/providers/{seeded_provider}/models/sync",
                            headers={"X-CSRF-Token": csrf})
    assert duplicate.status_code == 409
    assert duplicate.get_json()["data"]["operation_id"] == operation_id


def test_provider_model_api_exposes_three_counts_and_never_secret(client, seeded_provider):
    payload = client.get(f"/api/dashboard/ai/providers/{seeded_provider}/models").get_json()["data"]
    assert {"total", "discovered_available", "routable", "items"} <= payload.keys()
    assert "api_key" not in json.dumps(payload).lower()


def test_pool_member_stale_version_returns_409(client, csrf, seeded_pool, seeded_models):
    response = client.put(
        f"/api/dashboard/ai/model-pools/{seeded_pool}/members",
        json={"expected_version": 0, "members": [{"provider_model_id": seeded_models[0]}]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 409
    assert "版本" in response.get_json()["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_model_api.py -q`

Expected: FAIL with 404 because none of the model catalog, operation or pool endpoints are registered.

- [ ] **Step 3: Add service validation and exact routes**

Register:

```text
POST   /api/dashboard/ai/providers/<id>/models/sync
GET    /api/dashboard/ai/model-sync-operations/<operation_id>
GET    /api/dashboard/ai/model-sync-operations/<operation_id>/events
DELETE /api/dashboard/ai/model-sync-operations/<operation_id>
POST   /api/dashboard/ai/model-sync-operations/<operation_id>/confirm-empty
GET    /api/dashboard/ai/providers/<id>/models
POST   /api/dashboard/ai/providers/<id>/models
PUT    /api/dashboard/ai/provider-models/<id>
DELETE /api/dashboard/ai/provider-models/<id>
GET    /api/dashboard/ai/model-pools
POST   /api/dashboard/ai/model-pools
GET    /api/dashboard/ai/model-pools/<id>
PUT    /api/dashboard/ai/model-pools/<id>
DELETE /api/dashboard/ai/model-pools/<id>
PUT    /api/dashboard/ai/model-pools/<id>/members
GET    /api/dashboard/ai/model-pools/<id>/attempts
```

Use the existing response envelope and add `require_json_object()` that rejects non-object JSON instead of silently using `{}` for new write routes. `PUT provider-models` accepts only `enabled`, `manual_display_name`, `manual_capabilities`, and `manual_context_window`; reject any client `discovered_*` field. Sync SSE emits only `started`, `page`, `empty_confirmation_required`, `completed`, `failed`, and `cancelled`, and its empty event contains only operation ID, generation and digest. The service singleton starts one coordinator, calls job/sync reconciliation at startup, and `close()` stops the executor; do not start a duplicate worker per request.

- [ ] **Step 4: Run API and CSRF tests**

Run: `python -m pytest tests/test_ai_model_api.py tests/test_webapp_security.py::test_csrf_required_for_authenticated_mutating_requests tests/test_ai_web_stream.py -q`

Expected: PASS, including 202/409, exact writable fields, SSE event whitelist, CSRF enforcement and API-key redaction.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/services/admin.py src/pixiv_novel_sync/ai/services/__init__.py src/pixiv_novel_sync/ai/service.py src/pixiv_novel_sync/ai_web.py tests/test_ai_model_api.py
git commit -m "feat: expose model catalog sync and pool APIs"
```

### Task 9: Job Ownership, Attempt Allocation and Partial State

**Files:**
- Modify: `src/pixiv_novel_sync/storage/ai/core.py`
- Modify: `src/pixiv_novel_sync/storage/tasks.py`
- Modify: `src/pixiv_novel_sync/ai_web.py`
- Create: `tests/test_ai_job_routing_storage.py`
- Modify: `tests/test_unified_task_logs.py`

**Interfaces:**
- `create_ai_job(..., *, owner_token: str | None = None, stage: str = 'main', route_deadline_at: str | None = None, parent_job_id: str | None = None, idempotency_key: str | None = None)` remains backward compatible.
- Adds `set_ai_job_candidate_snapshot(job_id, owner_token, snapshot_json, snapshot_hash) -> bool`, `allocate_ai_model_attempt(job_id, owner_token, data) -> int`, `heartbeat_ai_job(job_id, owner_token, lease_until) -> bool`, `finish_ai_model_attempt(job_id, attempt_index, owner_token, status, **fields) -> bool`, `finish_ai_job_cas(job_id, owner_token, status, **fields) -> bool`, `list_ai_job_model_attempts(job_id)`, and owner-aware `fail_stale_ai_jobs()`.
- `get_ai_job` returns `route_summary`, `attempts`, parsed `candidate_snapshot`, `candidate_snapshot_hash`, and `prompt_budget` without exposing `owner_token`.

- [ ] **Step 1: Write failing owner/CAS tests**

```python
def test_attempt_indices_are_unique_under_concurrency(db):
    db.create_ai_job("job", "continue", 1, {}, owner_token="owner")
    indexes = run_concurrently(lambda: db.allocate_ai_model_attempt(
        "job", "owner", valid_attempt_data()
    ), count=8)
    assert sorted(indexes) == list(range(8))


def test_terminal_job_state_is_monotonic(db):
    db.create_ai_job("job", "continue", 1, {}, owner_token="owner")
    assert db.finish_ai_job_cas("job", "owner", "partial", output_text="半截") is True
    assert db.finish_ai_job_cas("job", "owner", "succeeded", output_text="迟到完成") is False
    assert db.get_ai_job("job")["status"] == "partial"


@pytest.mark.parametrize(
    ("stage", "output_started", "expected"),
    [("main", 1, "partial"), ("main", 0, "failed"),
     ("internal", 1, "failed"), ("validation", 1, "failed")],
)
def test_stale_recovery_maps_stage_and_output(stage, output_started, expected, db):
    seed_expired_job_and_attempt(db, stage=stage, output_started=output_started)
    db.fail_stale_ai_jobs()
    assert db.get_ai_job("job")["status"] == expected
    assert db.list_ai_job_model_attempts("job")[0]["error_category"] == "process_interrupted"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_job_routing_storage.py tests/test_unified_task_logs.py -q`

Expected: FAIL because jobs do not own leases, allocate attempts or support `partial`.

- [ ] **Step 3: Implement short transactions and monotonic terminal CAS**

Allocate attempts in one `BEGIN IMMEDIATE`: require `ai_jobs.status='running' AND owner_token=?`, read `next_attempt_index`, increment it, insert the attempt with the old value, and increment `candidate_attempt_count`; reject the 17th attempt with `route_budget_exhausted`. Network claims atomically increment `network_request_count` and reject the 33rd. Heartbeat updates only a running matching-owner row. Attempt/job terminal updates use `WHERE status='running' AND owner_token=?`; terminal states cannot overwrite one another. Sanitize error strings before storage and enforce snapshot/hash/length limits before SQL.

Update `cleanup_ai_jobs` so `partial` follows the same 3-day retention as other terminal rows and cascades attempts. Rewrite stale recovery to require an expired lease plus heartbeat beyond grace, finish matching running attempts first, apply the exact stage mapping from the test, and preserve an already committed cancellation. Add `partial` to unified-log filtering/labels and stop treating it as running. The startup call in `ai_web.py` invokes the owner-aware function without the old creation-time-only threshold.

- [ ] **Step 4: Run storage and log tests**

Run: `python -m pytest tests/test_ai_job_routing_storage.py tests/test_unified_task_logs.py tests/test_task_logs_routes.py -q`

Expected: PASS, including parallel indices, owner mismatch, terminal races, partial cleanup and stale-stage mapping.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/storage/ai/core.py src/pixiv_novel_sync/storage/tasks.py src/pixiv_novel_sync/ai_web.py tests/test_ai_job_routing_storage.py tests/test_unified_task_logs.py
git commit -m "feat: add leased AI jobs and auditable model attempts"
```

### Task 10: Provider Completion, Error Scope and Request Budget Contract

**Files:**
- Modify: `src/pixiv_novel_sync/ai/providers.py`
- Modify: `src/pixiv_novel_sync/ai/models.py`
- Create: `tests/test_ai_provider_completion.py`
- Modify: `tests/test_ai_providers_fallback.py`

**Interfaces:**
- `AIProviderError(message, *, category: str, scope: Literal['model','provider'], retry_after: float | None = None, finish_reason: str | None = None)` retains `RuntimeError` compatibility.
- `stream_generate(..., *, request_guard: Callable[[], None] | None = None, is_cancelled: Callable[[], bool] | None = None)` remains callable by old positional/keyword callers.
- Every normal provider completion emits `AIStreamChunk(type='done', data={'finish_reason': 'stop'|'complete'})`; incomplete reasons are exposed as typed errors, not silently converted to success.

- [ ] **Step 1: Write failing completion tests**

```python
@pytest.mark.parametrize("finish_reason", ["length", "content_filter", None])
def test_openai_non_normal_finish_is_typed_failure(finish_reason, monkeypatch):
    provider = make_openai_provider_with_choice(monkeypatch, text="正文", finish_reason=finish_reason)
    chunks = []
    with pytest.raises(AIProviderError) as caught:
        for chunk in provider.stream_generate(MESSAGES, model="m", temperature=0, top_p=1, max_tokens=10):
            chunks.append(chunk)
    assert [chunk.text for chunk in chunks if chunk.type == "delta"] == ["正文"]
    assert caught.value.finish_reason == (finish_reason or "missing")


def test_request_guard_counts_retries_and_fallback(monkeypatch):
    provider = make_retry_then_nonstream_provider(monkeypatch)
    calls = 0
    def guard():
        nonlocal calls
        calls += 1
    list(provider.stream_generate(MESSAGES, model="m", temperature=0, top_p=1,
                                  max_tokens=10, request_guard=guard))
    assert calls == provider.network_call_count


@pytest.mark.parametrize("status, scope", [(401, "provider"), (429, "provider"), (404, "model")])
def test_error_scope_is_structured(status, scope, monkeypatch):
    provider = make_error_provider(monkeypatch, status)
    with pytest.raises(AIProviderError) as caught:
        list(provider.stream_generate(MESSAGES, model="m", temperature=0, top_p=1, max_tokens=10))
    assert caught.value.scope == scope
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_provider_completion.py tests/test_ai_providers_fallback.py -q`

Expected: FAIL because Provider errors have no category/scope and done events do not preserve finish reasons.

- [ ] **Step 3: Implement completion and failure classification**

Call `request_guard()` immediately before every POST network request, including retries and stream-to-nonstream fallback; check `is_cancelled()` before requests, retry sleeps and yielded chunks, close the active response and raise `cancelled/model` when set. Parse OpenAI `choices[0].finish_reason` and Anthropic stop reasons; map `stop`, `complete`, `end_turn`, and `stop_sequence` to normal completion, `max_tokens` to `length`, refusal/content filters to `content_filter`, and no terminal marker to `missing`. An empty normal response raises `empty_response/model`.

Classify 401/403, disabled/configuration failures, account quota, 429/`Retry-After`, DNS/certificate/connect errors, unscoped 5xx and unknown failures as Provider scope. Classify an explicit unsupported-model/not-found response, a model-tagged timeout, context overflow and model-specific gateway rejection as model scope. Keep existing no-retry-after-partial guarantee inside each Provider and redact all upstream messages before constructing the error.

- [ ] **Step 4: Run Provider tests**

Run: `python -m pytest tests/test_ai_provider_completion.py tests/test_ai_providers_fallback.py tests/test_ai_security_hardening.py -q`

Expected: PASS with unchanged retry/fallback behavior plus structured finish and error metadata.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/providers.py src/pixiv_novel_sync/ai/models.py tests/test_ai_provider_completion.py tests/test_ai_providers_fallback.py
git commit -m "feat: expose Provider completion and failure scope"
```

### Task 11: Candidate Resolution DTOs and PromptBudget

**Files:**
- Create: `src/pixiv_novel_sync/ai/model_router.py`
- Modify: `src/pixiv_novel_sync/ai/providers.py`
- Create: `tests/test_ai_model_router.py`

**Interfaces:**
- Produces the adult-plan contract below, with only the listed optional extensions after the required fields.

```python
@dataclass(frozen=True, slots=True)
class ModelCandidate:
    provider_id: int
    provider_name: str
    model_key: str
    provider_model_id: int | None
    pool_id: int | None
    pool_name: str | None
    pool_version: int | None
    pool_position: int | None
    provider_config_hash: str
    capabilities: tuple[str, ...] = ()
    context_window: int | None = None
    fallback_depth: int = 0
    candidate_index: int = 0


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    candidates: tuple[ModelCandidate, ...]
    snapshot_hash: str
    agent_config_hash: str
    binding_version: int


@dataclass(slots=True)
class RouteRequest:
    job_id: str
    stage: Literal["internal", "main", "validation"]
    messages: list[dict[str, str]]
    candidate_snapshot: CandidateSnapshot
    max_tokens: int
    owner_token: str
    on_delta: Callable[[str], None]
    on_progress: Callable[[dict[str, Any]], None]
    temperature: float = 0.8
    top_p: float = 0.9
    resume_candidate_index: int = 0
    is_cancelled: Callable[[], bool] | None = None


@dataclass(frozen=True, slots=True)
class RouteResult:
    job_id: str
    output_text: str
    candidate_snapshot_hash: str
    attempts: tuple[dict[str, Any], ...]
    finish_state: Literal["succeeded", "failed_before_output", "partial", "cancelled"]
```

- Produces `PromptBudget(effective_context_window, input_budget, output_reserve, message_overhead, safety_margin, estimator)` and `ModelRouter.resolve_candidates(agent, stage='main', snapshot=None) -> CandidateSnapshot`, `build_prompt_budget(agent, snapshot, messages, max_tokens) -> PromptBudget`, `execute(request) -> RouteResult`, and `execute_stream(request) -> Generator[AIStreamChunk, None, RouteResult]`.
- `AIProvider.estimate_message_tokens(messages) -> int | None` defaults to `None`; adapters with a verified tokenizer may override it. `None` uses UTF-8 bytes plus the common message-overhead estimator.

- [x] **Step 1: Write failing resolution and budget tests**

```python
def test_fixed_agent_resolves_one_legacy_candidate(router, fixed_agent):
    snapshot = router.resolve_candidates(fixed_agent)
    assert [(c.provider_id, c.model_key, c.pool_id) for c in snapshot.candidates] == [(1, "fixed-m", None)]
    assert len(snapshot.snapshot_hash) == 64
    assert snapshot.binding_version == fixed_agent.binding_version


def test_pool_resolution_preserves_member_then_fallback_order(router, pool_agent):
    snapshot = router.resolve_candidates(pool_agent)
    assert [(c.provider_name, c.model_key) for c in snapshot.candidates] == [
        ("p1", "m1"), ("p2", "m2"), ("p3", "m3")
    ]
    assert [c.candidate_index for c in snapshot.candidates] == [0, 1, 2]
    assert len({(c.provider_id, c.model_key) for c in snapshot.candidates}) == 3


def test_capability_filter_and_empty_pool_fail_before_provider(router, pool_agent):
    pool_agent.required_capabilities = ("json",)
    with pytest.raises(ModelRouteError, match="模型池没有可用模型"):
        router.resolve_candidates(pool_agent)


def test_prompt_budget_uses_smallest_candidate_window(router, pool_agent):
    snapshot = snapshot_with_context_windows(32000, 8000)
    budget = router.build_prompt_budget(
        pool_agent, snapshot, [{"role": "user", "content": "正文"}], max_tokens=1000
    )
    assert budget.effective_context_window == 8000
    assert budget.safety_margin == 256
    assert budget.input_budget == 8000 - 1000 - budget.message_overhead - 256
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_model_router.py -q`

Expected: FAIL because routing DTOs, resolver and budget do not exist.

- [x] **Step 3: Implement deterministic snapshot resolution**

`ModelRouter` receives `db_factory`, `_load_provider_config`, and `_get_provider` callbacks so it uses the service's decryption and Provider cache without duplicating secrets. Resolve all rows inside one `db.read_transaction()` snapshot. Fixed bindings produce exactly one candidate using Agent model or Provider default; when non-empty required capabilities are configured, require a matching structured catalog row. Pool bindings traverse enabled pools and members in stored order, skip disabled Provider/pool/member/model, skip invalid source rows, filter required capabilities, and deduplicate by `(provider_id, model_key)` before the 64-candidate check.

Hash Agent effective configuration, Provider effective configuration (including an irreversible digest of encrypted-key ciphertext but never the key), pool IDs/versions/positions and ordered candidates using compact sorted canonical JSON. `snapshot_hash` must be reconstructable from stored fields and contain no Prompt/body. When `snapshot` is passed, re-read current pool versions, Agent `binding_version`/hash and every not-yet-attempted Provider config hash; mismatch raises `ModelRouteConflictError` before a Provider call. Build one immutable budget from the smallest effective candidate/Provider/Agent context window. `message_overhead = 4 * len(messages) + 2`; use a provider estimator only when it returns a positive integer, otherwise use total UTF-8 content bytes plus overhead. Reject invalid ranges or `input_budget <= 0` with a Chinese configuration error.

- [x] **Step 4: Run resolver tests**

Run: `python -m pytest tests/test_ai_model_router.py -q`

Expected: PASS for fixed compatibility, pool/fallback order, deduplication, capability filtering, stable hashes and conservative budget.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/model_router.py src/pixiv_novel_sync/ai/providers.py tests/test_ai_model_router.py
git commit -m "feat: add deterministic model candidate resolution and budgets"
```

### Task 12: ModelRouter Execution and Failover State Machine

**Files:**
- Modify: `src/pixiv_novel_sync/ai/model_router.py`
- Modify: `src/pixiv_novel_sync/storage/ai/core.py`
- Modify: `tests/test_ai_model_router.py`

**Interfaces:**
- `ModelRouter.execute_stream(request)` yields Provider `progress` and user-visible `delta` chunks, invokes callbacks, and returns `RouteResult` through the generator return value; `execute(request)` consumes the same generator and returns the identical result for non-SSE callers such as the adult-polish plan.
- Main-stage first success stores `pinned_candidate_index`; later main calls for the same job/owner use only that candidate. Internal and validation stages have independent attempts and never change the main pin.

- [x] **Step 1: Write failing routing state tests**

```python
def test_failure_before_first_delta_switches_across_providers(router, route_request, fake_providers):
    fake_providers.fail("p1", AIProviderError("down", category="gateway", scope="provider"))
    fake_providers.succeed("p2", [AIStreamChunk(type="delta", text="正文"), normal_done()])
    result = router.execute(route_request)
    assert result.finish_state == "succeeded"
    assert result.output_text == "正文"
    assert [a["status"] for a in result.attempts] == ["failed", "succeeded"]


def test_failure_after_first_main_delta_is_partial_and_never_switches(router, route_request, fake_providers, db):
    fake_providers.partial_then_fail("p1", "半截", AIProviderError("drop", category="network", scope="provider"))
    fake_providers.succeed("p2", [AIStreamChunk(type="delta", text="不应调用"), normal_done()])
    result = router.execute(route_request)
    assert result.finish_state == "partial"
    assert result.output_text == "半截"
    assert fake_providers.calls == [("p1", "m1")]
    assert db.get_ai_job(route_request.job_id)["status"] == "partial"


def test_provider_scope_failure_skips_remaining_models_for_same_provider(router, route_request, fake_providers):
    fake_providers.fail("p1", AIProviderError("429", category="rate_limited", scope="provider", retry_after=60))
    fake_providers.succeed("p2", [AIStreamChunk(type="delta", text="ok"), normal_done()])
    result = router.execute(route_request)
    assert fake_providers.calls == [("p1", "m1"), ("p2", "m3")]
    assert result.attempts[0]["error_scope"] == "provider"


def test_internal_failure_never_creates_partial_or_pins_main(router, internal_request, fake_providers):
    fake_providers.partial_then_fail("p1", "内部摘要", AIProviderError("drop", category="network", scope="model"))
    result = router.execute(internal_request)
    assert result.finish_state == "failed_before_output"
    assert get_job(internal_request.job_id)["pinned_candidate_index"] is None
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_model_router.py -q`

Expected: FAIL because execution, attempt persistence and stage-aware state do not exist.

- [x] **Step 3: Implement the route loop and heartbeat**

Start a daemon heartbeat helper per active `execute_stream` that opens its own `Database`, renews job and current-attempt leases every 15 seconds, and stops/join-with-timeout in `finally`. Before each candidate, check deadline, 16-attempt and 32-network limits; context-overflow candidates write a failed attempt without calling Provider. Allocate the attempt before Provider invocation and pass a storage-backed network `request_guard` into `stream_generate`.

For each chunk: forward `progress` unchanged; a first non-empty main `delta` atomically sets attempt/job `output_started=1` and pins that candidate, invokes `on_delta`, then yields it. Internal/validation delta stays in the returned output buffer and callback but does not set the user-body pin. Normal done with non-empty output finishes the attempt `succeeded`; empty output is `empty_response`. A typed error finishes the attempt with category/scope/reason and either short-circuits all later candidates from that Provider or advances one candidate. Emit a Chinese route `progress` event before every candidate and switch, including provider/model/pool snapshots and skipped reasons but no Prompt.

On main failure after body start, finish attempt and job `partial` with preserved output and stop. On validation failure, finish job `failed` with `validation_failed` or `review_unavailable`; never convert it to partial. On internal exhaustion, return `failed_before_output` without closing the parent job so callers can apply their explicit fallback. On cancellation/GeneratorExit, close the current provider iterator, finish attempt/job `cancelled`, and never advance. On total main exhaustion, finish job `failed/route_exhausted`; on a hard resource limit, use `route_budget_exhausted`. Any late callback sees a terminal CAS failure and must discard its delta/result.

- [x] **Step 4: Run routing tests**

Run: `python -m pytest tests/test_ai_model_router.py tests/test_ai_job_routing_storage.py -q`

Expected: PASS for pre-output switch, post-output partial, cancellation, provider short circuit, attempt/network/deadline limits, heartbeat and terminal races.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/model_router.py src/pixiv_novel_sync/storage/ai/core.py tests/test_ai_model_router.py
git commit -m "feat: execute model routes with stage-aware failover"
```

### Task 13: Shared AI Service Route Session Adapter

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/core.py`
- Modify: `src/pixiv_novel_sync/ai/service.py`
- Modify: `src/pixiv_novel_sync/ai/services/__init__.py`
- Create: `tests/test_ai_model_router_integration.py`

**Interfaces:**
- Produces `RouteJobContext(job_id, owner_token, agent, candidate_snapshot, prompt_budget)` and helpers `_start_route_job(db, task_type, agent, input_data, *, messages, max_tokens, parent_job_id=None, idempotency_key=None, snapshot=None, resume_candidate_index=0) -> RouteJobContext`, `_stream_route(context, messages, *, stage='main', temperature=None, top_p=None, max_tokens=None) -> Generator[AIStreamChunk, None, RouteResult]`, `_finish_route_job(db, context, status, output_text, output_json=None, error_message=None) -> bool`, and `_cancel_route_job(...)`.
- `AIWritingService.model_router` is initialized once per service with the existing `_db`, `_load_provider_config`, and `_get_provider` callbacks; `close()` stops heartbeat/sync resources and cached Providers.

- [x] **Step 1: Write failing adapter tests**

```python
def test_start_route_job_persists_snapshot_before_provider_call(service, db, fake_router):
    agent = seed_fixed_agent(db)
    context = service._start_route_job(
        db, "continue", agent, {"source_type": "manual"},
        messages=[{"role": "user", "content": "正文"}], max_tokens=100,
    )
    job = db.get_ai_job(context.job_id)
    assert job["candidate_snapshot_hash"] == context.candidate_snapshot.snapshot_hash
    assert "owner_token" not in job
    assert fake_router.provider_calls == []


def test_stream_route_forwards_progress_delta_and_result(service, route_context, fake_router):
    chunks = collect_generator_return(service._stream_route(
        route_context, MESSAGES, stage="main"
    ))
    assert [chunk.type for chunk in chunks.items] == ["progress", "delta"]
    assert chunks.return_value.finish_state == "succeeded"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_model_router_integration.py -q`

Expected: FAIL because the facade has no shared router or route-job helper.

- [x] **Step 3: Implement the adapter without leaking owners**

Generate a random owner token, create the job with a deadline no later than 30 minutes, resolve candidates, compute/persist the PromptBudget and candidate snapshot before the first network request, then return the context. Public `get_job` strips owner fields; only the in-memory context carries owner. `_stream_route` builds a `RouteRequest` with callbacks that preserve existing SSE behavior and returns `yield from model_router.execute_stream(request)`. `_finish_route_job` uses owner CAS and refuses to overwrite router-written partial/cancelled/failed terminal states. Keep a path for an internal stage to return failure without closing the job. Add no alternate Provider/model selection helper.

- [x] **Step 4: Run adapter and facade tests**

Run: `python -m pytest tests/test_ai_model_router_integration.py tests/test_ai_service_facade.py tests/test_ai_service_provider_cache.py -q`

Expected: PASS with unchanged Provider cache invalidation and no owner token in API-facing rows.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/services/core.py src/pixiv_novel_sync/ai/service.py src/pixiv_novel_sync/ai/services/__init__.py tests/test_ai_model_router_integration.py
git commit -m "feat: add shared AI route session adapter"
```

### Task 14: Core Generation Paths and Internal Smart Context

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/generation.py`
- Modify: `tests/test_ai_service_stream_continue.py`
- Modify: `tests/test_keyword_clean.py`
- Modify: `tests/test_ai_model_router_integration.py`

**Interfaces:**
- `stream_continue`, `stream_rewrite`, `stream_audit`, `stream_plan`, and `clean_keywords` call `_start_route_job`/`_stream_route` and never load a Provider directly.
- `_smart_context(text, prompt_budget, route_context) -> Iterator[AIStreamChunk | str]` runs each summary through `stage='internal'`; all internal candidates failing returns the existing tail-truncated context rather than failing the main job.

- [x] **Step 1: Write failing integration tests**

```python
def test_continue_uses_internal_route_then_main_without_internal_pin(service, fake_router):
    chunks = list(service.stream_continue(long_text_payload()))
    assert [call.stage for call in fake_router.requests] == ["internal", "main"]
    assert fake_router.requests[0].job_id == fake_router.requests[1].job_id
    assert chunks[-1].type == "done"


def test_internal_summary_exhaustion_falls_back_to_tail(service, fake_router):
    fake_router.queue_result(failed_before_output_result(stage="internal"))
    fake_router.queue_result(success_result("续写"))
    chunks = list(service.stream_continue(long_text_payload()))
    assert chunks[-1].type == "done"
    assert "续写" == collected_delta(chunks)


def test_keyword_clean_pool_agent_keeps_graceful_degradation(service, fake_router):
    fake_router.queue_result(failed_before_output_result(stage="main"))
    assert service.clean_keywords(["噪声", "关键词"]) is None
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_service_stream_continue.py tests/test_keyword_clean.py tests/test_ai_model_router_integration.py -q`

Expected: FAIL because generation methods still call `_load_provider_config` and `provider.stream_generate` directly.

- [x] **Step 3: Migrate the five paths**

Create each job before smart-context/model execution, emit the existing metadata event, and use the returned PromptBudget to cap/tail the input before building final messages. `_smart_context` emits only `progress` events for summary progress and never forwards internal summary delta as user body; one internal route call per segment may switch candidates independently. Preserve existing output parsing, rule detection, job `output_json`, GeneratorExit behavior and keyword-clean `None` degradation. A router `partial` result keeps the exact partial output in the job and sends one error/progress conclusion rather than a second Provider call.

Add an AST assertion in `test_ai_model_router_integration.py` that rejects `.stream_generate(` in `generation.py`; the only service-layer exception in the repository remains `AIAdminMixin.test_provider`.

- [x] **Step 4: Run migrated generation tests**

Run: `python -m pytest tests/test_ai_service_stream_continue.py tests/test_keyword_clean.py tests/test_ai_service_parsing.py tests/test_ai_model_router_integration.py -q`

Expected: PASS for fixed and pool Agents, smart-context fallback, parsing and graceful keyword-clean failure.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/services/generation.py tests/test_ai_service_stream_continue.py tests/test_keyword_clean.py tests/test_ai_model_router_integration.py
git commit -m "refactor: route core AI generation through ModelRouter"
```

### Task 15: Wizard, Longform Planning and Chapter Continuation Paths

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/chat_wizard.py`
- Modify: `src/pixiv_novel_sync/ai/services/projects.py`
- Modify: `tests/test_ai_service_stream_continue.py`
- Modify: `tests/test_ai_model_router_integration.py`

**Interfaces:**
- Migrates `stream_chat`, `stream_longform_plan`, `stream_longform_plan_details`, `stream_chapter_continue`, and `stream_update_project_state` to the shared route adapter.
- Chapter autosave uses router terminal state: `succeeded` writes final content, `partial` writes the preserved partial content with status `partial`, and `cancelled` writes only the already received content; no fallback candidate is used after body start.

- [x] **Step 1: Write failing behavior tests**

```python
def test_wizard_pool_agent_routes_without_provider_id(service, fake_router, pool_agent_session):
    chunks = list(service.stream_chat({"session_id": pool_agent_session, "user_message": "继续"}))
    assert collected_delta(chunks) == "回答"
    assert fake_router.requests[-1].stage == "main"


def test_chapter_partial_autosave_uses_partial_status(service, fake_router, chapter_db):
    fake_router.queue_result(partial_result("半截"))
    chunks = list(service.stream_chapter_continue({"agent_id": 1, "project_id": 4, "chapter_id": 3}))
    assert chapter_db.updated_chapters[-1] == (3, {"content": "已有正文半截", "status": "draft"})
    assert chapter_db.metadata_patches[-1][1]["continue_autosave"]["status"] == "partial"
    assert chunks[-1].type == "error"


def test_longform_plan_uses_candidate_snapshot_and_normal_finish(service, fake_router):
    chunks = list(service.stream_longform_plan(valid_longform_payload()))
    assert fake_router.requests[-1].candidate_snapshot.snapshot_hash
    assert chunks[-1].type == "done"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_service_stream_continue.py tests/test_ai_model_router_integration.py -q`

Expected: FAIL because these paths still resolve `agent.provider_id` and call a Provider directly.

- [x] **Step 3: Replace direct selection while preserving side effects**

For every method, load only the Agent and domain inputs first, create the route job, then execute messages through `_stream_route`. Preserve assistant-message writes, longform JSON parsing/apply transactions, project-state parsing and chapter autosave metadata. Do not expose Provider internal progress as assistant content. Use the route snapshot's conservative context window when calculating longform/chapter context size; do not read one Provider's context window before pool resolution. Add AST coverage for `chat_wizard.py` and the migrated project methods.

- [x] **Step 4: Run wizard and project tests**

Run: `python -m pytest tests/test_ai_service_stream_continue.py tests/test_ai_service_parsing.py tests/test_ai_import_atomicity.py tests/test_ai_model_router_integration.py -q`

Expected: PASS, including pool-bound chat sessions, partial autosave, longform parsing and transaction rollback.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/services/chat_wizard.py src/pixiv_novel_sync/ai/services/projects.py tests/test_ai_service_stream_continue.py tests/test_ai_model_router_integration.py
git commit -m "refactor: route wizard planning and chapter generation"
```

### Task 16: Summary, Foreshadow, State, Polish and Audit-Adjacent Paths

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/projects.py`
- Modify: `tests/test_ai_service_parsing.py`
- Modify: `tests/test_ai_model_router_integration.py`

**Interfaces:**
- Migrates `stream_extract_chapter_summary`, `stream_auto_resolve_foreshadows`, `stream_polish`, and every LLM branch of `stream_chapter_pipeline` through `_stream_route`.
- `stream_extract_chapter_summary` and `stream_auto_resolve_foreshadows` keep their existing parse/apply transactions; a route `validation` result is never treated as user正文 or as a main partial.

- [x] **Step 1: Write failing route-coverage tests**

```python
@pytest.mark.parametrize(
    ("method", "payload", "expected_stage"),
    [
        ("stream_extract_chapter_summary", {"agent_id": 1, "chapter_id": 3}, "main"),
        ("stream_auto_resolve_foreshadows", {"agent_id": 1, "project_id": 1, "chapter_id": 3}, "main"),
        ("stream_polish", {"agent_id": 1, "chapter_id": 3, "text": "正文"}, "main"),
    ],
)
def test_project_ai_path_uses_router(method, payload, expected_stage, service, fake_router):
    chunks = list(getattr(service, method)(payload))
    assert fake_router.requests[-1].stage == expected_stage
    assert chunks[-1].type in {"done", "error"}


def test_foreshadow_parse_failure_keeps_existing_warning_behavior(service, fake_router):
    fake_router.queue_result(success_result("不是 JSON"))
    chunks = list(service.stream_auto_resolve_foreshadows({"agent_id": 1, "project_id": 1, "chapter_id": 3}))
    assert chunks[-1].data["warnings"] == ["模型返回的伏笔回收 JSON 无法解析，未更新伏笔状态"]
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_service_parsing.py tests/test_ai_model_router_integration.py -q`

Expected: FAIL because these methods still call `provider.stream_generate` and require a single Provider config.

- [x] **Step 3: Migrate domain side effects after route completion**

Build messages and create jobs exactly as today, then route them with the Agent's candidate snapshot. Keep `output_parts` separate from progress; only non-empty main delta is appended to user output. Run `_apply_foreshadow_resolution_output`, `_parse_summary_output`, `_parse_and_save_state`, chapter updates and warning construction only after a normal route result. If the route returns `partial`, do not parse or mutate structured project state from an incomplete response; store the partial job state and send the existing error event. Preserve the project style-control prompt and all current JSON warning text.

- [x] **Step 4: Run project path tests and static direct-call check**

Run: `python -m pytest tests/test_ai_service_parsing.py tests/test_ai_import_atomicity.py tests/test_ai_model_router_integration.py -q`

Expected: PASS; the AST test reports no `stream_generate` call in `projects.py`.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/services/projects.py tests/test_ai_service_parsing.py tests/test_ai_model_router_integration.py
git commit -m "refactor: route project summaries state and polish"
```

### Task 17: Multi-Batch Pinning and Progress Semantics

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/generation.py`
- Modify: `src/pixiv_novel_sync/ai/services/projects.py`
- Modify: `tests/test_ai_model_router_integration.py`
- Create: `tests/test_ai_multibatch_routing.py`

**Interfaces:**
- A multi-batch main job creates one `RouteJobContext` and reuses its `CandidateSnapshot` for every batch; after the first non-empty main delta, `ModelRouter` pins that candidate for all later batches.
- Batch status is emitted as `AIStreamChunk(type='progress', data={'phase':'batch','batch': n,'total': total})`; synthetic batch labels are never emitted as `delta` and never enter `output_text`.

- [ ] **Step 1: Write failing multi-batch tests**

```python
def test_distill_batches_pin_first_successful_main_candidate(service, fake_router):
    fake_router.providers["m1"].queue([AIStreamChunk(type="delta", text="第一批"), normal_done()])
    fake_router.providers["m1"].queue([AIStreamChunk(type="delta", text="第二批"), normal_done()])
    chunks = list(service.stream_distill_style({
        "agent_id": 1, "text": "长文" * 10000, "full_text": True,
        "chunk_chars": 1000, "batch_size": 1,
    }))
    assert fake_router.provider_calls == [("p1", "m1"), ("p1", "m1")]
    assert all(chunk.type != "delta" or "正在分析第" not in chunk.text for chunk in chunks)
    assert any(chunk.type == "progress" and (chunk.data or {}).get("phase") == "batch" for chunk in chunks)


def test_later_batch_failure_is_partial_without_fallback(service, fake_router):
    fake_router.providers["m1"].queue([AIStreamChunk(type="delta", text="第一批"), normal_done()])
    fake_router.providers["m1"].queue([AIStreamChunk(type="delta", text="半截"), provider_failure()])
    fake_router.providers["m2"].queue([AIStreamChunk(type="delta", text="不应调用"), normal_done()])
    chunks = list(service.stream_distill_style(valid_multibatch_payload()))
    assert "不应调用" not in collected_delta(chunks)
    assert get_job_from_metadata(chunks)["status"] == "partial"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_multibatch_routing.py tests/test_ai_model_router_integration.py -q`

Expected: FAIL because current distillation sends progress as delta, creates no persistent main pin and reuses no route session.

- [ ] **Step 3: Reuse one route context across batches**

Move context creation and job start before the batch loop, pass the same context to every `_stream_route` call, and let the router enforce the pinned index. Internal summaries use a separate `stage='internal'` context and cannot pin main. Emit batch progress before each call and append only actual final/main output to `output_parts`; retain the existing intermediate profile reduction in memory. Apply the same rule to any future detailed-plan batch branch and add a code assertion that no `AIStreamChunk(type='delta', text=progress_text)` remains. Pipeline wrapper methods may continue to invoke already-migrated subservices, but must forward their `progress` events as pipeline custom progress events rather than relabeling them as正文 delta.

- [ ] **Step 4: Run multi-batch and pipeline tests**

Run: `python -m pytest tests/test_ai_multibatch_routing.py tests/test_ai_model_router_integration.py tests/test_ai_service_stream_continue.py -q`

Expected: PASS for pinning, post-output partial, progress semantics and unchanged pipeline step metadata.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/services/generation.py src/pixiv_novel_sync/ai/services/projects.py tests/test_ai_multibatch_routing.py tests/test_ai_model_router_integration.py
git commit -m "fix: pin multi-batch AI output and separate progress events"
```

### Task 18: Manual Next-Candidate Continuation

**Files:**
- Modify: `src/pixiv_novel_sync/ai/model_router.py`
- Modify: `src/pixiv_novel_sync/ai/services/core.py`
- Modify: `src/pixiv_novel_sync/ai/services/admin.py`
- Modify: `src/pixiv_novel_sync/ai_web.py`
- Modify: `tests/test_ai_model_router.py`
- Modify: `tests/test_ai_model_api.py`

**Interfaces:**
- `RouteResumeSpec(parent_job_id: str, idempotency_key: str, candidate_snapshot_hash: str, resume_candidate_index: int)` is an internal immutable value; JSON clients cannot inject it into ordinary generation payloads.
- `AIWritingService.stream_job_with_next_model(job_id: str, payload: Mapping[str, Any]) -> Iterator[AIStreamChunk]` validates the parent, creates/reuses an idempotent child job and dispatches the original task through the saved snapshot.
- Adds `POST /api/dashboard/ai/jobs/<job_id>/continue`; request body must contain `parent_job_id`, `idempotency_key`, `candidate_snapshot_hash`, and `resume_candidate_index`; response is the normal SSE stream with metadata/progress/delta/done/error events.

- [ ] **Step 1: Write failing continuation tests**

```python
def test_continue_uses_saved_snapshot_and_skips_attempted_candidates(service, db, fake_router):
    parent = seed_partial_job_with_snapshot_and_attempts(db, attempted=(0, 1), next_index=2)
    chunks = list(service.stream_job_with_next_model(parent, {
        "parent_job_id": parent,
        "idempotency_key": "continue-00000001",
        "candidate_snapshot_hash": db.get_ai_job(parent)["candidate_snapshot_hash"],
        "resume_candidate_index": 2,
    }))
    assert fake_router.requests[-1].candidate_snapshot.candidates[0].candidate_index == 2
    assert fake_router.provider_calls == [("p3", "m3")]
    assert chunks[-1].type == "done"


def test_continue_rejects_snapshot_or_config_change_with_409(client, csrf, partial_job):
    response = client.post(
        f"/api/dashboard/ai/jobs/{partial_job}/continue",
        json={"parent_job_id": partial_job, "idempotency_key": "continue-00000002",
              "candidate_snapshot_hash": "0" * 64, "resume_candidate_index": 1},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 409
    assert "候选快照" in response.get_json()["error"]


def test_duplicate_idempotency_key_does_not_call_provider_twice(service, db, fake_router):
    parent = seed_partial_job_with_snapshot_and_attempts(db, attempted=(0,), next_index=1)
    payload = valid_continue_payload(parent, index=1)
    list(service.stream_job_with_next_model(parent, payload))
    calls_after_first = len(fake_router.provider_calls)
    list(service.stream_job_with_next_model(parent, payload))
    assert len(fake_router.provider_calls) == calls_after_first
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_model_router.py tests/test_ai_model_api.py -q`

Expected: FAIL because no continuation route, replay context or idempotency key exists.

- [ ] **Step 3: Implement saved-snapshot replay and endpoint**

Accept only a terminal parent job, an exact path/body `parent_job_id`, a 16-128 ASCII idempotency key, a matching lowercase 64-hex snapshot hash, and an index equal to the first candidate after all parent attempts and within the snapshot. Re-read the Agent binding version, every remaining pool version and each remaining Provider config hash; any mismatch is 409. In one transaction create a child job with `parent_job_id`, idempotency key, the parent candidate snapshot/hash, a copied immutable input reference and `resume_candidate_index`; repeated key returns the existing child without creating a Provider call. Dispatch only the migrated stream methods through an internal `RouteResumeSpec`; each method passes the saved snapshot and start index to `_start_route_job`, so it never reparses a changed pool or retries an attempted candidate. A partial parent body is read-only context and is not concatenated into the child output unless the task's existing prompt builder explicitly accepts it. Owner tokens and terminal states are never shared between parent and child.

- [ ] **Step 4: Run continuation/API tests**

Run: `python -m pytest tests/test_ai_model_router.py tests/test_ai_model_api.py tests/test_ai_web_stream.py -q`

Expected: PASS for exact snapshot/config checks, idempotency, skipped candidates, CSRF and SSE cancellation.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/model_router.py src/pixiv_novel_sync/ai/services/core.py src/pixiv_novel_sync/ai/services/admin.py src/pixiv_novel_sync/ai_web.py tests/test_ai_model_router.py tests/test_ai_model_api.py
git commit -m "feat: add explicit next-model continuation"
```

### Task 19: Provider Settings Model Directory UI

**Files:**
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings.html`
- Create: `tests/test_ai_model_ui.py`

**Interfaces:**
- Provider cards call the catalog endpoints and render `total`, `discovered_available`, `routable`, `models_synced_at`, `models_sync_error`, model search/filter, effective display name/capabilities/context, and `source` without rendering any secret.
- The sync button disables while an operation is queued/running, consumes the six-event SSE whitelist, supports page refresh recovery via operation GET, and displays “旧目录仍可使用” after failure/cancel/timeout.

- [ ] **Step 1: Write failing static UI tests**

```python
def test_settings_template_contains_catalog_counts_sync_and_empty_confirmation():
    html = Path("src/pixiv_novel_sync/templates/dashboard_settings.html").read_text(encoding="utf-8")
    for text in ("/models/sync", "discovered_available", "routable", "needs_empty_confirmation", "旧目录仍可使用"):
        assert text in html
    assert "api_key_encrypted" not in html


def test_model_sync_mutations_use_csrf_fetch():
    html = Path("src/pixiv_novel_sync/templates/dashboard_settings.html").read_text(encoding="utf-8")
    assert "X-CSRF-Token" in html
    assert "confirm-empty" in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_model_ui.py -q`

Expected: FAIL because the current Provider card has only default-model text and no catalog/sync controls.

- [ ] **Step 3: Add the Provider catalog controls**

Extend the existing `ai-api` Vue state with `providerModels`, `modelSearch`, `modelSyncOperations`, and per-provider error/status fields. Add a “同步模型” control, counts with distinct labels, last-success/error display, a collapsible catalog list and manual-model form. Use the existing `ensureCsrfToken` helper for every POST/PUT/DELETE; update `aiApi` so mutating requests automatically attach `X-CSRF-Token`. Parse SSE `page` and terminal events, never display upstream response text, and keep the prior catalog/counts in state on any failure. A non-authoritative empty event opens a confirmation dialog that submits only operation ID/generation/digest. Escape model names with Vue interpolation and show the cross-Provider privacy warning from the API summary.

- [ ] **Step 4: Run UI contract and page tests**

Run: `python -m pytest tests/test_ai_model_ui.py tests/test_ai_page_routes.py -q`

Expected: PASS, including secret absence, CSRF usage, refresh recovery and old-directory messaging.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/templates/dashboard_settings.html tests/test_ai_model_ui.py
git commit -m "feat: add Provider model catalog controls"
```

### Task 20: Model Pool and Agent Binding Settings UI

**Files:**
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings.html`
- Modify: `tests/test_ai_model_ui.py`

**Interfaces:**
- Adds an `ai-model-pools` tab with pool list/editor, Provider/model search, member reorder controls, fallback selector, enabled/version state, Agent-reference summary, candidate count and privacy warning.
- Agent form adds mutually exclusive `fixed`/`pool` binding controls, model catalog selection with hand-entry fallback, pool chain summary and required-capability checkboxes; Agent rows show binding summary and selected capabilities.

- [ ] **Step 1: Write failing pool/Agent UI tests**

```python
def test_settings_template_contains_pool_editor_and_mutual_binding_controls():
    html = Path("src/pixiv_novel_sync/templates/dashboard_settings.html").read_text(encoding="utf-8")
    for text in ("ai-model-pools", "fallback_pool_id", "expected_version", "binding_type",
                 "required_capabilities", "streaming", "long_context", "隐私"):
        assert text in html


def test_pool_save_sends_complete_member_order_not_incremental_positions():
    html = Path("src/pixiv_novel_sync/templates/dashboard_settings.html").read_text(encoding="utf-8")
    assert "/members" in html
    assert "expected_version" in html
    assert "members" in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_model_ui.py -q`

Expected: FAIL because no pool tab, binding selector or capability controls exist.

- [ ] **Step 3: Implement complete pool and Agent editing flows**

Load pools, providers and models independently; keep a local ordered member array and submit the full list plus the server version in one PUT. Disable enable/save when the pool is empty or API reports cycle/depth/candidate/reference errors; on 409 reload the pool and show the Chinese conflict. Display every Provider that may receive the Prompt through the expanded chain and a fixed “每任务最多尝试 16 个候选” note. Agent fixed mode enables Provider/model fields and disables pool fields; pool mode does the reverse; the submitter sends only one valid binding, but the server remains authoritative. Render capability checkboxes from the fixed five-label whitelist and show the pool member count/chain summary in Agent rows. All mutating calls use CSRF and all values use Vue text interpolation.

- [ ] **Step 4: Run UI tests**

Run: `python -m pytest tests/test_ai_model_ui.py tests/test_ai_model_api.py -q`

Expected: PASS for complete member replacement, stale-version handling, mutual form state, capability labels and privacy disclosure.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/templates/dashboard_settings.html tests/test_ai_model_ui.py
git commit -m "feat: add model pool and Agent binding settings"
```

### Task 21: Task Log Route Details and Continue Action

**Files:**
- Modify: `src/pixiv_novel_sync/templates/dashboard_logs.html`
- Modify: `src/pixiv_novel_sync/storage/tasks.py`
- Modify: `tests/test_unified_task_logs.py`
- Modify: `tests/test_ai_model_ui.py`

**Interfaces:**
- AI job detail displays route summary (final Provider/model/pool), immutable candidate snapshot hash, conservative PromptBudget, attempt index/status/scope/category/error/latency and the “最多尝试 16 个候选” limit.
- `partial` has a distinct Chinese status and is not shown as running; when a terminal job has an untried candidate, the detail modal exposes a CSRF-protected “使用下一个模型继续” action that opens the continuation SSE endpoint.

- [ ] **Step 1: Write failing log/UI tests**

```python
def test_unified_ai_log_includes_partial_and_route_summary(db):
    db.create_ai_job("partial", "continue", 1, {})
    db.update_ai_job("partial", "partial", output_text="半截")
    row = db.get_ai_task_logs(status="partial", days=3)["items"][0]
    assert row["status"] == "partial"
    assert row["task_name"] == "续写"


def test_logs_template_contains_attempts_budget_and_continue_endpoint():
    html = Path("src/pixiv_novel_sync/templates/dashboard_logs.html").read_text(encoding="utf-8")
    for text in ("attempts", "candidate_snapshot_hash", "input_budget", "/continue", "partial"):
        assert text in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_unified_task_logs.py tests/test_ai_model_ui.py -q`

Expected: FAIL because `partial` is not retained by the log projection and the modal only displays generic job fields.

- [ ] **Step 3: Add route audit rendering and explicit continuation**

Extend the AI projection with `route_summary` and `attempt_count` while retaining the existing pagination shape. In the modal, fetch `/api/dashboard/ai/jobs/<job_id>`, render attempts from the server response with escaped interpolation, display pool/provider/model snapshots and error scope, and add a candidate-index selector that only offers the first untried snapshot entry. Send a fresh idempotency key plus the exact hash/index, attach CSRF, stream SSE progress/delta/done/error and refresh detail after completion. Add `partial` to status labels/result colors and ensure a terminal job never displays the spinner or an auto-retry button.

- [ ] **Step 4: Run logs and UI tests**

Run: `python -m pytest tests/test_unified_task_logs.py tests/test_task_logs_routes.py tests/test_ai_model_ui.py -q`

Expected: PASS for partial filtering, route/attempt display and explicit continuation only.

- [ ] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/templates/dashboard_logs.html src/pixiv_novel_sync/storage/tasks.py tests/test_unified_task_logs.py tests/test_ai_model_ui.py
git commit -m "feat: show AI route attempts in task logs"
```

### Task 22: Documentation, Static Direct-Call Gate and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/frontend-api-contract.md`
- Modify: `docs/frontend-pages.md`
- Modify: `docs/INDEX.md`
- Create: `tests/test_ai_model_docs.py`
- Modify: `tests/test_ai_model_router_integration.py`

**Interfaces:**
- Documentation states the model catalog/pool API and the exact first-phase limits, and no longer claims that each Agent only supports one Provider or that cross-Provider fallback is unsupported.
- The static gate permits direct `provider.stream_generate()` only in `ai/providers.py`, `AIAdminMixin.test_provider`, and router internals; all other business service files fail the test.

- [ ] **Step 1: Write failing documentation/static tests**

```python
def test_readme_no_longer_describes_old_single_provider_fallback():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "尚不支持跨 Provider fallback" not in text
    assert "模型池" in text
    assert "/api/dashboard/ai/providers/<id>/models/sync" in text


def test_no_business_service_selects_provider_directly():
    root = Path("src/pixiv_novel_sync/ai/services")
    offenders = []
    for path in root.glob("*.py"):
        if "stream_generate(" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_model_docs.py tests/test_ai_model_router_integration.py -q`

Expected: FAIL because README still contains the old fallback sentence and service modules retain direct Provider calls.

- [ ] **Step 3: Update docs and add release checks**

Document Provider sync, empty confirmation, catalog counts, pool/fallback order, capability filtering, snapshot/attempt details, continuation input and privacy disclosure in the frontend contract/page docs. Replace the README setup paragraph with the fixed/pool binding behavior and exact limits; retain API-key encryption and manual-model compatibility. Add static checks for forbidden secret names in templates/log payloads and for `candidate_snapshot_json` not containing `prompt`, `messages`, `api_key`, or `output_text` keys. Add a migration test that runs `PRAGMA foreign_key_check` on a legacy fixture and a router test for 16/32/30-minute hard limits.

- [ ] **Step 4: Run complete verification before claiming completion**

Run, in order:

```powershell
python -m pytest tests/test_ai_model_schema.py tests/test_ai_model_catalog.py tests/test_ai_model_pools.py tests/test_ai_model_discovery.py tests/test_ai_model_sync.py tests/test_ai_job_routing_storage.py tests/test_ai_provider_completion.py tests/test_ai_model_router.py tests/test_ai_model_router_integration.py tests/test_ai_model_api.py tests/test_ai_model_ui.py tests/test_ai_model_docs.py -q
python -m pytest -q
python -m compileall -q src
git diff --check
```

Expected: all targeted and full tests pass, compileall emits no output, and `git diff --check` exits 0. Start the local Flask app with `pixiv-novel-sync web --port 5010`, open `/dashboard/settings#ai-api`, `/dashboard/settings#ai-model-pools`, `/dashboard/ai`, and `/dashboard/logs` in a browser, verify nonblank catalog/pool/log states and a failed-first-candidate switch with a fake Provider, then stop the server. Do not mark the plan complete until the old fixed-Agent migration, three Provider discovery paths, full AI call-chain AST gate, concurrency tests and privacy checks all pass.

Run the complete suite with `-W error::pytest.PytestUnhandledThreadExceptionWarning` as an additional final gate. The Task 0 rescue scheduler regression must remain warning-free; do not hide it with warning filters.

- [ ] **Step 5: Commit documentation and verification tests**

```powershell
git add README.md docs/frontend-api-contract.md docs/frontend-pages.md docs/INDEX.md tests/test_ai_model_docs.py tests/test_ai_model_router_integration.py
git commit -m "docs: document model catalog pools and routing limits"
```

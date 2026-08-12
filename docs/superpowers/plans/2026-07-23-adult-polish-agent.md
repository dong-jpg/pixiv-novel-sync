# 成人描写局部润色 Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在已认证的 Dashboard 章节详情中提供一个只处理单一连续片段的 `adult_polish` Agent；候选文本必须经过服务端固定安全策略、事实保护校验和差异预览，用户明确确认后才以乐观锁事务写回章节。

**Architecture:** 成人功能使用独立的领域 DTO、不可编辑策略资源、确定性校验器和 `AIAdultPolishMixin`，复用现有 SQLite/Flask/Vue 结构。写作、`adult_safety_review` 和 `adult_fact_guard` 三个阶段都通过模型池计划提供的 `ModelRouter.resolve_candidates(agent, stage='main', snapshot=None) -> CandidateSnapshot` 与 `ModelRouter.execute(request: RouteRequest) -> RouteResult`，但成人模块不读取模型池表。生成阶段只在服务端内存缓冲 Provider delta；应用阶段由 `AdultStorageMixin.apply_adult_polish()` 在 `BEGIN IMMEDIATE` 内重新读取所有事实、版本和授权后替换码点范围。

**Tech Stack:** Python 3.10 标准库（`dataclasses`、`hashlib`、`hmac`、`json`、`re`、`secrets`）、Flask 3、SQLite/WAL、现有 `AIWritingService`/`AIStreamChunk`、Vue 3 CDN、pytest。

## Global Constraints

- 本计划必须在模型池计划完成并提供 `ModelRouter.resolve_candidates(agent, stage='main', snapshot=None) -> CandidateSnapshot`、`ModelRouter.execute(request: RouteRequest) -> RouteResult` 后执行；成人代码不得直接调用 `provider.stream_generate()` 或读取模型池 SQL。
- 新增 `task_type` 只有用户可配置的 `adult_polish`；`adult_safety_review` 与 `adult_fact_guard` 是服务端固定 validation 阶段，不得进入普通 Agent CRUD。
- 成人接口只接受已配置 `dashboard_token` 且当前 Flask 会话 `session['authenticated']` 为真的 Dashboard 请求；未配置 token、未认证或 owner 不匹配统一拒绝，不能以本机来源或可猜 job ID 放行。
- 章节正文、目标片段和候选哈希使用原始 Python 字符串 UTF-8 SHA-256；保留换行、组合字符和空白，不做 NFC、CRLF/LF 或空白归一化。项目事实、角色清单和策略摘要使用 Unicode NFC、排序键、紧凑 JSON、UTF-8 SHA-256；哈希统一为小写 64 位十六进制。
- `target_start`/`target_end` 是 Unicode 码点偏移；目标长度 20-12000 字，前后文各最多 4000 字，指令最多 1000 字，`locked_terms` 最多 64 项且每项 1-100 码点，参与者最多 20 个，幂等键为 16-128 个 ASCII 字符，强度为 0-100。
- 候选缓冲上限为 36,000 个 Unicode 码点且 144,000 个 UTF-8 字节；每个 delta 到达时增量检查。`finish_reason=length`、`content_filter`、缺失正常结束标记、传输异常、空输出、校验异常或安全审查失败均 fail-closed。
- 只有主生成在首个非空正文 delta 后失败才标记 `partial`；`validation` 阶段失败标记 `failed`，不切换写作候选、不发送候选正文。安全阻断候选不进入 SSE、`ai_jobs.output_text`、日志或长期快照。
- `adult_safety_review` 和 `adult_fact_guard` 的策略文本、JSON Schema、Prompt、策略哈希和本地规则是只读代码资源；普通 Agent CRUD、Prompt 模板 CRUD 和客户端字段不能修改、删除、停用或绕过它们。
- 应用必须同时匹配章节 `chapter_revision`、章节哈希、目标哈希、项目事实哈希、成人确认 revision、角色 revision、三个阶段 `provider_scope_hash`/binding hash、validation/policy hash 和 warning acknowledgment；任一变化返回 `409`，不调用 Provider。
- 成人功能不加入章节 Pipeline、不支持整章或多片段批处理；应用后必须把摘要、状态、审计和检索等正文派生数据标记为过期或排队重建。
- 不新增依赖；错误、界面和文档文案使用中文，API Key、Prompt、上下文、正文和原始 Provider 响应不得写入日志。
- 每个任务严格先写失败测试、运行红灯、写最小实现、运行局部测试并提交；每项任务下方给出唯一、具体的提交信息。

## 模型池 DTO 依赖契约

模型池计划必须提供下列只读 DTO 字段；成人计划只消费这些字段，不读取其存储实现：

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


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    candidates: tuple[ModelCandidate, ...]
    snapshot_hash: str
    agent_config_hash: str
    binding_version: int


@dataclass(slots=True)
class RouteRequest:
    job_id: str
    stage: Literal["main", "validation"]
    messages: list[dict[str, str]]
    candidate_snapshot: CandidateSnapshot
    max_tokens: int
    owner_token: str
    on_delta: Callable[[str], None]
    on_progress: Callable[[dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class RouteResult:
    job_id: str
    output_text: str
    candidate_snapshot_hash: str
    attempts: tuple[dict[str, Any], ...]
    finish_state: Literal["succeeded", "failed_before_output", "partial", "cancelled"]
```

`ModelRouter.resolve_candidates(agent, stage='main', snapshot=None)` returns this `CandidateSnapshot`; `ModelRouter.execute(request)` returns this `RouteResult`. Candidate/order/config hashes exclude API keys, Prompt and正文。成人模块自行把三个 snapshot canonicalize 成 `provider_scope_hash`，并把 `stage='validation'` 用于两个内置审查。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `src/pixiv_novel_sync/ai/adult_types.py` | 请求、角色、策略、校验和路由快照 DTO；输入边界与哈希函数 |
| `src/pixiv_novel_sync/ai/adult_policies.py` | `adult_safety_policy`、`adult_fact_guard_policy` 固定正文、Schema、版本和预期哈希 |
| `src/pixiv_novel_sync/ai/adult_prompt.py` | 随机边界、角色占位符、风格合并、四段 Prompt 和严格候选解析 |
| `src/pixiv_novel_sync/ai/adult_validation.py` | 本地确定性安全检查、事实/结构差异、warning/blocking 和确认哈希 |
| `src/pixiv_novel_sync/ai/adult_auth.py` | Dashboard owner scope、签名恢复凭证和成人路由授权检查 |
| `src/pixiv_novel_sync/ai/services/adult.py` | 角色确认、绑定管理、成人生成、validation 阶段和应用服务编排 |
| `src/pixiv_novel_sync/storage/ai/adult.py` | 成人表读写、owner 过滤、幂等、终态 CAS、应用事务和清理 |
| `src/pixiv_novel_sync/storage/schema.py` | 成人列、角色/绑定/application/派生失效表和原库原子迁移 |
| `src/pixiv_novel_sync/storage_db.py` | 将 `AdultStorageMixin` 接入 `Database` |
| `src/pixiv_novel_sync/storage/ai/core.py` | 通用 job 查询、清理、孤儿候选和 owner_scope 过滤适配 |
| `src/pixiv_novel_sync/storage/ai/writing.py` | 章节 revision 单调递增、项目成人设置和派生缓存失效 |
| `src/pixiv_novel_sync/ai/services/admin.py` | `adult_polish` Agent 模板和普通 CRUD 的内部策略隔离 |
| `src/pixiv_novel_sync/ai/services/__init__.py`、`src/pixiv_novel_sync/ai/service.py` | 导出 `AIAdultPolishMixin` 并保持 facade 兼容 |
| `src/pixiv_novel_sync/ai_web.py` | 成人角色、绑定、流式生成、候选读取、重试、取消和应用 API |
| `src/pixiv_novel_sync/webapp.py` | 向成人路由提供认证 owner、CSRF 和安全响应头上下文 |
| `src/pixiv_novel_sync/templates/dashboard_ai_reader.html` | 章节详情页签、码点选择、进度、差异和应用交互 |
| `src/pixiv_novel_sync/templates/dashboard_settings.html` | 成人声明、角色档案和两个审查绑定设置区 |
| `tests/ai_adult_testkit.py` | 成人测试的固定项目/角色/payload/application 构造器、FakeModelRouter 和并发助手 |
| `tests/test_ai_adult_*.py` | 领域、迁移、路由、并发、隐私和前端契约测试 |
| `README.md`、`docs/frontend-api-contract.md`、`docs/frontend-pages.md`、`docs/INDEX.md` | 中文配置、API、页面和开发计划说明 |

---

### Task 1: 成人领域 DTO、规范化哈希与固定策略

**Files:**
- Create: `src/pixiv_novel_sync/ai/adult_types.py`
- Create: `src/pixiv_novel_sync/ai/adult_policies.py`
- Create: `tests/ai_adult_testkit.py`
- Test: `tests/test_ai_adult_types.py`
- Test: `tests/test_ai_adult_policies.py`

**Interfaces:**
- Produces `AdultIntensity`, `AdultPolishRequest`, `AdultCharacterFact`, `AdultPolicyBundle`, `AdultValidationResult`, `AdultInputError(ValueError)`, and `PolicyMismatchError(RuntimeError)`; service methods translate these domain errors to public `AIServiceError`/HTTP codes without creating a circular import.
- Produces `raw_sha256(text: str) -> str`, `canonical_sha256(value: Any) -> str`, `parse_adult_request(payload: Mapping[str, Any]) -> AdultPolishRequest` and `warning_ack_hash(validation_hash, safety_policy_hash, validator_policy_hash, warning_codes) -> str`.
- Produces `load_adult_policy(kind: Literal['safety', 'fact_guard']) -> AdultPolicyBundle` and `verify_adult_policy_bundle() -> None`.
- Produces shared test helpers `valid_adult_payload`, `application_row`, `character_fact`, `safe_validation`, `structural_validation`, `make_legacy_ai_database`, `seed_adult_project`, `FakeModelRouter`, and `run_concurrently`; later test modules import these names rather than redefining implicit fixtures.

- [x] **Step 1: Write the failing tests**

```python
def test_raw_hash_preserves_crlf_and_combining_characters():
    assert raw_sha256("é\r\n") != raw_sha256("e\u0301\n")


def test_canonical_hash_normalizes_only_structured_values():
    assert canonical_sha256({"b": "é", "a": ["e\u0301"]}) == canonical_sha256(
        {"a": ["é"], "b": "é"}
    )
    assert raw_sha256("e\u0301") != raw_sha256("é")


def test_parse_request_rejects_utf16_style_or_out_of_range_input():
    with pytest.raises(AdultInputError, match="target_start"):
        parse_adult_request({"target_start": -1, "target_end": 20})
    with pytest.raises(AdultInputError, match="幂等键"):
        parse_adult_request({"target_start": 0, "target_end": 20, "idempotency_key": "短"})


def test_policy_hash_tamper_fails_closed(monkeypatch):
    monkeypatch.setattr(adult_policies, "SAFETY_POLICY_TEXT", "被修改")
    with pytest.raises(PolicyMismatchError):
        verify_adult_policy_bundle()
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_adult_types.py tests/test_ai_adult_policies.py -q`

Expected: FAIL because the DTO, parser and policy bundle do not exist.

- [x] **Step 3: Implement the domain layer**

Implement the following exact primitives in `adult_types.py`:

```python
@dataclass(frozen=True, slots=True)
class AdultIntensity:
    explicitness: int
    lyricism: int
    vulgarity: int


@dataclass(frozen=True, slots=True)
class AdultPolishRequest:
    project_id: int
    chapter_id: int
    agent_id: int
    target_start: int
    target_end: int
    chapter_content_hash: str
    target_text_hash: str
    chapter_revision: int
    participant_character_ids: tuple[str, ...]
    adult_characters_confirmed: bool
    intensity: AdultIntensity
    locked_terms: tuple[str, ...]
    instruction: str
    idempotency_key: str
    provider_scope_hash: str
    parent_job_id: str | None = None


@dataclass(frozen=True, slots=True)
class AdultCharacterFact:
    character_id: str
    revision: int
    canonical_name: str
    aliases: tuple[str, ...]
    age_years: int | None
    age_basis: str
    fictional: bool
    active: bool


@dataclass(frozen=True, slots=True)
class AdultPolicyBundle:
    policy_id: str
    version: int
    prompt_template: str
    output_schema: Mapping[str, Any]
    policy_text: str
    expected_hash: str


@dataclass(frozen=True, slots=True)
class AdultValidationResult:
    applicable: bool
    warnings: tuple[str, ...]
    blocking_issues: tuple[str, ...]
    protected_terms_missing: tuple[str, ...]
    paragraph_delta: int
    length_ratio: float
    perspective_warning: bool
    new_number_tokens: tuple[str, ...]
    diff_summary: Mapping[str, int]
    validation_hash: str
```

`parse_adult_request` must require positive IDs, two lowercase 64-hex hashes, `target_start < target_end`, exact integer ranges, unique stable character IDs, no control characters in locked terms, and ASCII-only 16-128 character idempotency keys. It must reject unknown fields that could carry client正文 (`target_text`, `before`, `after`, `prompt`, `system_prompt`) rather than silently retaining them. `raw_sha256` hashes the exact input string; `canonical_sha256` recursively NFC-normalizes strings, sorts object keys, uses `json.dumps(normalized_value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)`, and returns lowercase SHA-256. `warning_ack_hash` hashes an object with keys `validation_hash`, `safety_policy_hash`, `validator_policy_hash`, and sorted unique `warning_codes`.

In `adult_policies.py`, define immutable `AdultPolicyBundle(policy_id, version, prompt_template, output_schema, policy_text, expected_hash)` constants. Use fixed IDs `adult_safety_policy.v1` and `adult_fact_guard_policy.v1`; the safety schema accepts only `safe: boolean` and enum `issues` values `minor_present`, `age_unknown`, `real_person`, `new_character`, `schema_invalid`, while the fact schema accepts only `safe: boolean` and enum `issues` values `age_changed`, `pregnancy_changed`, `relationship_changed`, `consent_changed`, `participant_changed`, `locked_fact_changed`, `unknown`. Compute each expected hash once from the exact policy object at release time and store the resulting 64-hex literal beside the constant. `verify_adult_policy_bundle()` recomputes the object hash and raises `PolicyMismatchError` on any mismatch; no database or user value can replace a policy field.

Create `tests/ai_adult_testkit.py` with deterministic constructors. `valid_adult_payload` returns all request fields with a 20-code-point target and hashes computed by `raw_sha256`; `application_row` returns every non-null application field using fixed 64-hex hashes; `character_fact(name='安娜', age_years=25, fictional=True)` returns a stable UUID/revision record; `safe_validation()` and `structural_validation(code='length_ratio')` return concrete `AdultValidationResult` values. `make_legacy_ai_database(path)` creates the current pre-feature `ai_providers`/`ai_agents`/`ai_jobs`/project/chapter tables with fixed Agent ID 7 and chapter ID 9. `seed_adult_project(db)` inserts project ID 1, chapter ID 9, Agent ID 7, a running `adult-job` owned by `owner-a`, and the two confirmed character rows needed by `application_row()`. `FakeModelRouter` stores `CandidateSnapshot` objects and a FIFO list of `RouteResult` values, records every stage/message, invokes callbacks, and increments `execute_count`. `run_concurrently(callable_, count=2)` uses `threading.Barrier`, starts exactly `count` threads, joins them and returns values/exceptions in call order.

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ai_adult_types.py tests/test_ai_adult_policies.py -q`

Expected: PASS, including CRLF/combining-character and policy-tamper cases.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/adult_types.py src/pixiv_novel_sync/ai/adult_policies.py tests/ai_adult_testkit.py tests/test_ai_adult_types.py tests/test_ai_adult_policies.py
git commit -m "feat: add adult polish domain contracts and fixed policies"
```

### Task 2: SQLite schema、迁移和成人存储边界

**Files:**
- Modify: `src/pixiv_novel_sync/storage/schema.py`
- Modify: `src/pixiv_novel_sync/storage_db.py`
- Create: `src/pixiv_novel_sync/storage/ai/adult.py`
- Test: `tests/test_ai_adult_storage.py`

**Interfaces:**
- `AdultStorageMixin` exposes `list_adult_characters(project_id, include_inactive=False)`, `get_adult_character(character_id)`, `get_adult_confirmation(project_id)`, `get_adult_review_bindings()`, and owner-filtered `get_adult_job(job_id, owner_scope)`.
- It also exposes `create_adult_character`, `cas_update_adult_character`, `set_adult_confirmation`, `cas_update_review_binding`, `find_job_by_idempotency`, `create_adult_job`, `cas_finish_adult_job`, `save_candidate_application`, `get_application_for_owner(source_job_id, owner_scope)`, and `cleanup_adult_jobs`.
- Existing `Database` callers remain valid; `AdultStorageMixin` is added to `Database` without changing mixin order for existing methods.

- [x] **Step 1: Write the failing migration tests**

```python
def test_adult_schema_defaults_are_fail_closed(db):
    project_id = db.create_ai_writing_project({"name": "p", "settings": {}})
    project = db.get_ai_writing_project(project_id)
    assert project["adult_content_enabled"] is False
    assert project["adult_characters_confirmed"] is False
    assert project["adult_characters_json"] == []
    assert project["adult_confirmation_revision"] == 0
    assert db.get_adult_review_bindings() == {
        "safety": {"enabled": False},
        "fact_guard": {"enabled": False},
    }
    assert {row["policy_kind"] for row in db.list_adult_policy_state()} == {"safety", "fact_guard"}


def test_adult_application_does_not_fk_delete_with_job(db):
    seed_adult_project(db)
    application_id = db.save_candidate_application(application_row())
    db.delete_ai_job("adult-job")
    assert db.get_application_for_owner("adult-job", "owner-a")["id"] == application_id


def test_old_database_migration_preserves_agent_and_chapter_ids(tmp_path):
    db = make_legacy_ai_database(tmp_path / "old.db")
    db.init_schema()
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.get_ai_agent(7)["id"] == 7
    assert db.get_ai_chapter(9)["id"] == 9
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_adult_storage.py -q`

Expected: FAIL because the adult columns/tables and `AdultStorageMixin` methods are absent.

- [x] **Step 3: Add the atomic schema migration and storage methods**

Call `_migrate_adult_polish_tables()` from `SchemaMixin.init_schema()` after the model-pool migration. The migration must run in one `BEGIN IMMEDIATE` transaction, preserve existing IDs/timestamps, and execute `PRAGMA foreign_key_check` before commit. Rebuild `ai_writing_projects` and `ai_chapters` when SQLite cannot add the required checks in place. Add these exact fields:

```sql
ALTER TABLE ai_writing_projects ADD COLUMN adult_content_enabled INTEGER NOT NULL DEFAULT 0 CHECK (adult_content_enabled IN (0,1));
ALTER TABLE ai_writing_projects ADD COLUMN adult_characters_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (adult_characters_confirmed IN (0,1));
ALTER TABLE ai_writing_projects ADD COLUMN fictional_characters_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (fictional_characters_confirmed IN (0,1));
ALTER TABLE ai_writing_projects ADD COLUMN adult_characters_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(adult_characters_json));
ALTER TABLE ai_writing_projects ADD COLUMN adult_confirmation_revision INTEGER NOT NULL DEFAULT 0 CHECK (adult_confirmation_revision >= 0);
ALTER TABLE ai_writing_projects ADD COLUMN adult_confirmation_updated_at TEXT;
ALTER TABLE ai_chapters ADD COLUMN chapter_revision INTEGER NOT NULL DEFAULT 0 CHECK (chapter_revision >= 0);
```

Create `ai_project_characters(character_id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES ai_writing_projects(id) ON DELETE CASCADE, revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0), canonical_name TEXT NOT NULL, aliases_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(aliases_json)), age_years INTEGER CHECK(age_years >= 0), age_basis TEXT NOT NULL, fictional INTEGER NOT NULL CHECK(fictional IN (0,1)), active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)` with `(project_id, active)` index. Deactivation updates `active=0` and increments `revision`; rows are never physically deleted during normal CRUD.

Create `ai_adult_review_bindings(review_kind TEXT PRIMARY KEY CHECK(review_kind IN ('safety','fact_guard')), binding_type TEXT CHECK(binding_type IN ('fixed','pool')), provider_id INTEGER REFERENCES ai_providers(id) ON DELETE RESTRICT, model TEXT, model_pool_id INTEGER REFERENCES ai_model_pools(id) ON DELETE RESTRICT, required_capabilities_json TEXT NOT NULL DEFAULT '["json"]' CHECK(json_valid(required_capabilities_json)), enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)), version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0), updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)`; a trigger/check enforced by the service requires disabled rows to have all route fields NULL and enabled fixed/pool rows to satisfy the same mutual exclusion as `ai_agents`. Insert exactly two disabled rows on migration.

Create `ai_adult_policy_state(policy_kind TEXT PRIMARY KEY CHECK(policy_kind IN ('safety','fact_guard')), policy_id TEXT NOT NULL UNIQUE, policy_version INTEGER NOT NULL CHECK(policy_version > 0), policy_hash TEXT NOT NULL, prompt_hash TEXT NOT NULL, schema_hash TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)`. The migration inserts the exact release hashes from `adult_policies.py`; no public storage/service/API write method is exposed. Service startup compares this row, the code literal and the runtime-rendered policy bundle, and disables adult generation on any mismatch.

Create `ai_polish_applications` with independent `id INTEGER PRIMARY KEY`, unique `source_job_id TEXT`, owner/project/chapter IDs, target offsets, before/after chapter and target hashes, project/character/participant/provider/policy/validation hashes, Agent/pool/Provider/model/reviewer/fact-guard snapshots, `validation_json TEXT NOT NULL`, `applicable INTEGER NOT NULL CHECK(applicable IN (0,1))`, timestamps and nullable `applied_at`, `chapter_hash_after`, `chapter_revision_after`. Do not add an FK from `source_job_id` to `ai_jobs`; project/chapter FKs use `ON DELETE CASCADE`. Add `(owner_scope, source_job_id)` and `(project_id, chapter_id, created_at)` indexes.

Create `ai_chapter_derivative_invalidations(chapter_id INTEGER PRIMARY KEY REFERENCES ai_chapters(id) ON DELETE CASCADE, chapter_revision INTEGER NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','queued','rebuilt')), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)`.

Add `owner_scope` (nullable for legacy jobs but required when `task_type='adult_polish'`), `idempotency_key_hash`, `parent_job_id`, and the model-router lease/snapshot columns supplied by the model-pool plan to `ai_jobs`; create the adult index `(owner_scope, created_at)`. `AdultStorageMixin` must serialize only scrubbed input metadata and validation summaries, never正文. `save_candidate_application` writes `ai_jobs.output_text`, application metadata and the owner-CAS job terminal state in one transaction; `cleanup_adult_jobs` deletes expired unapplied applications before their jobs and retains applied metadata while allowing the three-day job output cleanup.

- [x] **Step 4: Run migration and storage tests**

Run: `python -m pytest tests/test_ai_adult_storage.py -q`

Expected: PASS; old fixtures keep IDs/configuration, disabled bindings are present, `foreign_key_check` is empty, and deleting a job does not delete an application.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/storage/schema.py src/pixiv_novel_sync/storage_db.py src/pixiv_novel_sync/storage/ai/adult.py tests/test_ai_adult_storage.py
git commit -m "feat: add adult polish storage schema and migrations"
```

### Task 3: 成年虚构角色档案、项目确认和章节 revision

**Files:**
- Modify: `src/pixiv_novel_sync/storage/ai/writing.py`
- Modify: `src/pixiv_novel_sync/ai/services/projects.py`
- Create: `src/pixiv_novel_sync/ai/services/adult.py`
- Test: `tests/test_ai_adult_characters.py`

**Interfaces:**
- `AIAdultPolishMixin.list_adult_characters(project_id) -> list[dict[str, Any]]`.
- `create_adult_character(project_id, payload) -> dict[str, Any]`, `update_adult_character(character_id, payload, expected_revision) -> dict[str, Any]`, `deactivate_adult_character(character_id, expected_revision) -> dict[str, Any]`.
- `update_adult_confirmation(project_id, payload, expected_revision) -> dict[str, Any]` and `get_adult_confirmation(project_id) -> dict[str, Any]`.
- `build_project_facts_snapshot(db, project_id) -> tuple[dict[str, Any], str]` returns server-read facts and `project_facts_hash`; it never accepts client-supplied ages, names or relationship facts.

- [x] **Step 1: Write the failing tests**

```python
def test_character_id_is_server_generated_and_revision_is_cas(db, service):
    row = service.create_adult_character(1, {"canonical_name": "安娜", "aliases": ["安"] , "age_years": 25, "age_basis": "项目设定", "fictional": True})
    assert row["character_id"] and row["revision"] == 1
    with pytest.raises(AIServiceError, match="revision"):
        service.update_adult_character(row["character_id"], {"canonical_name": "新名"}, expected_revision=2)
    updated = service.update_adult_character(row["character_id"], {"canonical_name": "安娜2"}, expected_revision=1)
    assert updated["revision"] == 2


def test_confirmation_rejects_minor_or_real_character(service):
    minor = service.create_adult_character(1, {"canonical_name": "乙", "aliases": [], "age_years": 17, "age_basis": "设定", "fictional": True})
    with pytest.raises(AIServiceError, match="18"):
        service.update_adult_confirmation(1, {"adult_content_enabled": True, "adult_characters_confirmed": True, "fictional_characters_confirmed": True, "character_ids": [minor["character_id"]]}, expected_revision=0)


def test_chapter_revision_increments_for_content_and_metadata(db):
    chapter_id = db.create_ai_chapter({"project_id": 1, "chapter_number": 1, "content": "甲"})
    assert db.get_ai_chapter(chapter_id)["chapter_revision"] == 0
    db.update_ai_chapter(chapter_id, {"content": "乙"})
    assert db.get_ai_chapter(chapter_id)["chapter_revision"] == 1
    db.patch_ai_chapter_metadata(chapter_id, {"style": "x"})
    assert db.get_ai_chapter(chapter_id)["chapter_revision"] == 2
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_adult_characters.py -q`

Expected: FAIL because character APIs, confirmation fields and `chapter_revision` updates do not exist.

- [x] **Step 3: Implement character and revision rules**

Validate canonical names to 200 code points, aliases to at most 32 entries/100 code points each, non-empty `age_basis`, explicit boolean `fictional`, non-negative `age_years`, and server-generated UUID4 `character_id`. `update_adult_character` and confirmation use `WHERE character_id=? AND revision=?` or `WHERE project_id=? AND adult_confirmation_revision=?`; a zero-row update raises a conflict error. Any name/alias/age/basis/fictional/active change increments both character revision and project `adult_confirmation_revision`, clears `adult_characters_confirmed`, and sets `adult_confirmation_updated_at`.

`update_adult_confirmation` must read active rows in the same transaction, require all selected IDs to belong to the project, have current revisions, `age_years >= 18`, `fictional=1`, and no duplicates; it stores only sorted `{character_id, character_revision, confirmed_at}` entries in `adult_characters_json`. Each serialized entry is at most 2 KiB and the complete JSON at most 64 KiB. It increments the project revision and returns a canonical `adult_characters_hash`. Unknown or inactive IDs, malformed/oversized JSON, more than 100 characters, or stale expected revision are rejected. The service never searches the network or infers real-person/age facts; a project slider value alone is not confirmation.

Change every chapter mutation path (`update_ai_chapter`, `patch_ai_chapter_metadata`, `update_ai_chapters_outlines_and_metadata`, and the adult apply path) to increment `chapter_revision` exactly once per transaction that changes content or related metadata. Preserve raw content and update `word_count`/`updated_at` in the same SQL statement. `build_project_facts_snapshot` includes project outline/settings, active character facts, project states and foreshadow rows in a sorted canonical object; it returns no正文.

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ai_adult_characters.py -q`

Expected: PASS, including tombstone ID non-reuse, stale CAS rejection and revision invalidation.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/storage/ai/writing.py src/pixiv_novel_sync/ai/services/projects.py src/pixiv_novel_sync/ai/services/adult.py tests/test_ai_adult_characters.py
git commit -m "feat: add adult character confirmation and chapter revisions"
```

### Task 4: `adult_polish` Agent 模板与专用审查绑定

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/admin.py`
- Modify: `src/pixiv_novel_sync/ai/services/__init__.py`
- Modify: `src/pixiv_novel_sync/ai/service.py`
- Modify: `src/pixiv_novel_sync/ai_web.py`
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings.html`
- Test: `tests/test_ai_adult_admin.py`

**Interfaces:**
- `ensure_adult_polish_agent(binding: dict[str, Any]) -> dict[str, Any]` creates/updates only a user-owned `adult_polish` Agent and never selects a Provider implicitly.
- `list_adult_review_bindings() -> dict[str, dict[str, Any]]` and `update_adult_review_binding(review_kind, payload, expected_version) -> dict[str, Any]` edit only route fields.
- Dedicated routes are `GET/PUT /api/dashboard/ai/adult-review-bindings/<review_kind>` and `POST /api/dashboard/ai/agents/adult-polish/seed`; ordinary Agent CRUD must reject internal task types and policy fields.

- [x] **Step 1: Write the failing tests**

```python
def test_normal_agent_crud_cannot_create_internal_review_agent(service):
    with pytest.raises(AIServiceError, match="内部"):
        service.create_agent({"name": "x", "task_type": "adult_safety_review", "provider_id": 1, "system_prompt": "改策略"})


def test_review_binding_uses_expected_version_and_fixed_json_capability(service):
    saved = service.update_adult_review_binding("safety", {"binding_type": "fixed", "provider_id": 3, "model": "json-model", "enabled": True}, expected_version=1)
    assert saved["required_capabilities"] == ["json"]
    with pytest.raises(AIServiceError, match="409"):
        service.update_adult_review_binding("safety", {"enabled": False}, expected_version=1)


def test_adult_agent_template_has_no_provider_default(service):
    result = service.ensure_adult_polish_agent({"name": "成人描写润色", "binding_type": "pool", "model_pool_id": 8})
    assert result["task_type"] == "adult_polish"
    assert "Provider" not in result["system_prompt"]
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_adult_admin.py -q`

Expected: FAIL because `adult_polish` is not an allowed task type and the dedicated binding methods/routes do not exist.

- [x] **Step 3: Implement isolated admin paths**

Add `adult_polish` to the user task-type allowlist with the fixed template constraints: preserve characters/plot/facts/perspective, process only the selected target, follow inherited style plus per-operation intensity, and output replacement text only. Reject `adult_safety_review` and `adult_fact_guard` in `_normalize_agent_payload`, ignore/deny `policy_id`, `policy_text`, `output_schema`, `safety_policy_hash`, `validator_policy_hash`, and `binding_version` in every ordinary CRUD payload. `delete_agent` and `update_agent` must refuse any row marked internal.

`update_adult_review_binding` accepts only `binding_type`, `provider_id`, `model`, `model_pool_id`, `enabled`, and `expected_version`; it forces `required_capabilities_json='["json"]'`, validates fixed/pool mutual exclusion through the model-router capability resolver, and performs `UPDATE ai_adult_review_bindings SET binding_type=?, provider_id=?, model=?, model_pool_id=?, enabled=?, version=version+1, updated_at=CURRENT_TIMESTAMP WHERE review_kind=? AND version=?`. Disabled rows clear route fields. A missing route candidate or policy hash mismatch leaves the row disabled and raises a Chinese configuration error. Settings UI must show policy IDs/hashes and `json` as read-only text.

- [x] **Step 4: Run tests and route smoke checks**

Run: `python -m pytest tests/test_ai_adult_admin.py -q`

Expected: PASS; ordinary CRUD cannot mutate either policy, CAS conflicts return 409, and the template is provider-neutral.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/services/admin.py src/pixiv_novel_sync/ai/services/__init__.py src/pixiv_novel_sync/ai/service.py src/pixiv_novel_sync/ai_web.py src/pixiv_novel_sync/templates/dashboard_settings.html tests/test_ai_adult_admin.py
git commit -m "feat: isolate adult polish agent and review bindings"
```

### Task 5: Prompt 构造、角色占位符与确定性候选校验

**Files:**
- Create: `src/pixiv_novel_sync/ai/adult_prompt.py`
- Create: `src/pixiv_novel_sync/ai/adult_validation.py`
- Test: `tests/test_ai_adult_prompt.py`
- Test: `tests/test_ai_adult_validation.py`

**Interfaces:**
- `AdultPrompt` contains `boundary: str`, `sections: Mapping[str, str]`, `user_messages: list[dict[str, str]]`, `token_map: Mapping[str, AdultCharacterFact]`, and `protected_terms: tuple[str, ...]`.
- `CandidateParseResult` contains `text: str` and `blocking_issues: tuple[str, ...]`; it never returns guessed/concatenated正文.
- `build_adult_prompt(*, agent_prompt, project_facts, before, target, after, style_control, intensity, instruction, protected_terms, characters) -> AdultPrompt`.
- `parse_adult_candidate(raw: str, boundary: str) -> CandidateParseResult` and `restore_character_tokens(candidate, token_map) -> str`.
- `run_local_adult_checks(original, candidate, request, protected_terms, characters) -> AdultValidationResult`.
- `compute_validation_hash(result) -> str` and `compute_provider_scope_hash(scopes) -> str`.

- [x] **Step 1: Write the failing tests**

```python
def test_prompt_has_four_unambiguous_sections_and_random_boundary():
    prompt = build_adult_prompt(agent_prompt="保真", project_facts={"x": 1}, before="前", target="目标片段至少二十个码点用于完整边界测试", after="后", style_control=None, intensity=AdultIntensity(50, 50, 50), instruction="只改措辞", protected_terms=("安娜",), characters=(character_fact("安娜"),))
    assert {"system", "project_facts", "readonly_context", "target"} <= set(prompt.sections)
    assert prompt.boundary not in "前目标后只改措辞"
    assert "安娜" not in prompt.user_messages[0]["content"]


def test_candidate_parser_marks_structure_block_without_guessing_prefixes():
    parsed = parse_adult_candidate("说明：\n正文", "B")
    assert parsed.text == "说明：\n正文"
    assert "explanation_prefix" in parsed.blocking_issues
    parsed = parse_adult_candidate("B1\n正文\nB2\n另一个", "B")
    assert "multiple_blocks" in parsed.blocking_issues


def test_local_checks_cover_terms_length_numbers_and_minor_risk():
    request = parse_adult_request(valid_adult_payload())
    original = "安娜握住他的手，停顿片刻后仍保持原来的称呼和视角。"
    result = run_local_adult_checks(original, original, request, ("安娜",), (character_fact("安娜", 25),))
    assert result.applicable is True
    blocked = run_local_adult_checks(original, "一个十七岁女孩加入，改变了目标片段中的参与者和年龄事实。", request, ("安娜",), (character_fact("安娜", 25),))
    assert "minor_present" in blocked.blocking_issues
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_adult_prompt.py tests/test_ai_adult_validation.py -q`

Expected: FAIL because prompt/placeholder/parser and local checks are absent.

- [x] **Step 3: Implement prompt and local validation**

Generate a fresh `secrets.token_hex(16)` boundary per request and retry if it occurs in raw chapter text, project facts or instruction. Replace every confirmed adult fictional canonical name and alias with a job-local ASCII token `ADULT_<128-bit-hex>_<ordinal>` before sending the writing prompt; maintain a one-to-one token map in memory only. Reject target/context names not present in the confirmed whitelist before any Provider call. Construct messages in this order: immutable system rules, canonical project facts, read-only before/after blocks, target block, and a separate user-instruction block that explicitly cannot override facts or output scope. Merge project slider values from `compose_style_control_prompt` with per-operation `explicitness`, `lyricism`, and `vulgarity` without mutating project settings.

`parse_adult_candidate` returns `CandidateParseResult(text, blocking_issues)` rather than guessing a repair. It may remove exactly one Markdown fence surrounding the complete response. An explanation prefix, more than one candidate block, missing boundary/closing marker, heading, analysis or empty body remains verbatim in `text` and adds a deterministic blocking code; it must never trim guessed prefixes or concatenate fragments. This permits a non-safety structural block to be displayed after the atomic save, while the application endpoint still rejects it. `restore_character_tokens` scans both directions for unknown/split/variant tokens and rejects any token mapped to multiple identities.

`run_local_adult_checks` verifies target range, protected names/aliases, exact `locked_terms`, Markdown/HTML/Pixiv markers, numeric/date tokens, paragraph and perspective changes, 30%-300% length ratio, unknown/new participants, age/underage words and configured real-person flags. It returns only codes/counts/hashes in `AdultValidationResult`; no missing text or diff excerpt is persisted. Safety-critical uncertainty (`age`, identity, consent, participant mapping, pregnancy, relationship) is blocking, while pure paragraph/format changes may be warnings. `compute_provider_scope_hash` hashes sorted three-stage candidate/provider/pool/binding/version/config summaries, never secrets.
Before Prompt construction, require every deduplicated `locked_term` to occur exactly in the raw target, raw before/after context, or the server-owned project protection list; a missing term is a 422 preflight error and must not become an arbitrary user-supplied fact.

The local validator rule set has a fixed `VALIDATOR_POLICY_ID='adult_validator.v1'` and `VALIDATOR_POLICY_HASH` computed from its canonical rule/version object; Task 7 stores it as `validator_policy_hash` and treats any code/runtime mismatch as a blocking configuration error.

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ai_adult_prompt.py tests/test_ai_adult_validation.py -q`

Expected: PASS, including Chinese, emoji, combining-character, CRLF/LF, placeholder and length-boundary cases.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/adult_prompt.py src/pixiv_novel_sync/ai/adult_validation.py tests/test_ai_adult_prompt.py tests/test_ai_adult_validation.py
git commit -m "feat: add adult prompt boundaries and deterministic validation"
```

### Task 6: 成人生成编排与模型池 validation 契约

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/adult.py`
- Modify: `src/pixiv_novel_sync/ai/service.py`
- Test: `tests/test_ai_adult_generation.py`

**Interfaces:**
- `stream_adult_polish(payload: Mapping[str, Any], owner_scope: str, owner_token: str) -> Iterator[AIStreamChunk]`.
- `prepare_adult_job(payload, owner_scope) -> PreparedAdultJob` performs all preflight checks before routing.
- `PreparedAdultJob` contains the parsed request, job/owner tokens, raw chapter and target, before/after context, project/character snapshots and hashes, Prompt/token map, and immutable `main_snapshot`, `safety_snapshot`, `fact_guard_snapshot`; none of its正文 fields are serialized to job input or logs.
- `AdultRouteRequest` is a local DTO/protocol adapter containing `job_id`, `stage`, messages, immutable `CandidateSnapshot`, `max_tokens`, `owner_token`, and an `on_delta(text)` callback; it is converted to the model-pool `RouteRequest`.
- `RouteResult` is consumed only through `output_text`, `candidate_snapshot_hash`, `attempts`, and `finish_state`; the service maps attempts to `progress` without exposing request bodies.

- [x] **Step 1: Write the failing tests**

```python
def test_preflight_failure_never_calls_router(monkeypatch, service, adult_payload):
    calls = []
    failed = RouteResult(job_id="job", output_text="", candidate_snapshot_hash="a" * 64, attempts=(), finish_state="failed_before_output")
    monkeypatch.setattr(service.router, "execute", lambda request: calls.append(request) or failed)
    adult_payload["adult_characters_confirmed"] = False
    events = list(service.stream_adult_polish(adult_payload, "owner-a", "lease-a"))
    assert calls == []
    assert events[-1].type == "error"


def test_first_delta_failure_is_partial_and_never_emits_candidate(service, fake_router, adult_payload):
    fake_router.result = RouteResult(job_id="job", output_text="未完成", candidate_snapshot_hash="a" * 64, attempts=(), finish_state="partial")
    events = list(service.stream_adult_polish(adult_payload, "owner-a", "lease-a"))
    assert any(e.type == "error" and e.data["code"] == "partial" for e in events)
    assert not any(e.type == "candidate" for e in events)
    job_id = next(e.data["job_id"] for e in events if e.type == "metadata")
    assert service.get_job(job_id).get("output_text") is None


def test_same_idempotency_key_reuses_job_without_second_execute(service, fake_router, adult_payload):
    list(service.stream_adult_polish(adult_payload, "owner-a", "lease-a"))
    list(service.stream_adult_polish(adult_payload, "owner-a", "lease-b"))
    assert fake_router.execute_count == 1
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_adult_generation.py -q`

Expected: FAIL because `AIAdultPolishMixin`, preflight, idempotency and router adapter do not exist.

- [x] **Step 3: Implement preflight and buffered main routing**

`prepare_adult_job` must use one consistent read snapshot to verify owner access to project/chapter/Agent, project/chapter relationship, enabled `adult_polish` Agent, enabled review bindings, valid policy hashes, adult project flags, active confirmed fictional characters and revisions, participant exact mapping, chapter/target raw hashes, valid offsets, and the client `provider_scope_hash`. It reads the chapter正文 itself and stores only lengths/hashes in `ai_jobs.input_json`. A failed check returns a Chinese configuration/validation error before `ModelRouter.resolve_candidates` or `execute` is called.

Create the job with a random `owner_token`, persistent HMAC `owner_scope`, idempotency hash and optional `parent_job_id` in the same short transaction. If `(owner_scope, idempotency_key_hash)` already exists, return its job and do not call a Provider. Resolve the writing snapshot with `resolve_candidates(agent, stage='main')`; resolve safety and fact-guard snapshots from their dedicated binding DTOs. Compare all three scopes against the request hash before any network call.

Pass an `AdultRouteRequest` to `ModelRouter.execute` for `stage='main'`. The callback appends each non-empty delta to a bounded in-memory buffer and flips `output_started`; it emits no `delta` event. Router progress becomes `AIStreamChunk(type='progress')` with candidate/provider/model names and attempt status only. If `finish_state='failed_before_output'`, continue according to the router snapshot; if `finish_state='partial'`, clear the buffer, CAS `running -> partial`, persist only error/attempt summaries and emit an error. A successful main result proceeds to Task 7 validation; it never writes the chapter.

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ai_adult_generation.py -q`

Expected: PASS; preflight has zero Provider calls, partial output is invisible, and duplicate idempotency keys reuse one job.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/services/adult.py src/pixiv_novel_sync/ai/service.py tests/test_ai_adult_generation.py
git commit -m "feat: route adult polish generation through model router"
```

### Task 7: 固定安全审查、事实保护和原子候选收口

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/adult.py`
- Modify: `src/pixiv_novel_sync/storage/ai/adult.py`
- Test: `tests/test_ai_adult_review.py`

**Interfaces:**
- `ReviewResult` contains `safe: bool`, `issue_codes: tuple[str, ...]`, `policy_hash: str`, `prompt_hash: str`, `binding_hash: str`, `provider_snapshot: Mapping[str, Any]`, and `model_snapshot: str`; it contains no free-text reasoning.
- `run_adult_safety_review(prepared: PreparedAdultJob, candidate: str) -> ReviewResult` constructs the complete `RouteRequest` defined in the dependency contract with `stage='validation'` and the fixed safety policy, then calls `ModelRouter.execute(request)`.
- `run_adult_fact_guard(prepared: PreparedAdultJob, original: str, candidate: str) -> ReviewResult` uses the fixed fact-guard policy and canonical participant facts.
- `finalize_adult_candidate(prepared, candidate, local_result, safety_result, fact_result) -> AdultValidationResult`.
- `AdultStorageMixin.save_candidate_application(job_id: str, owner_scope: str, candidate: str | None, validation: AdultValidationResult, snapshots: Mapping[str, Any], terminal_status: Literal['succeeded', 'failed']) -> int` atomically stores a safe/structural result and changes `running` to `succeeded` or `failed` with owner CAS.

- [x] **Step 1: Write the failing tests**

```python
def test_safety_review_receives_restored_server_buffer_not_nonce(service, fake_router, prepared):
    result = service.run_adult_safety_review(prepared, "安娜握住他的手。")
    request = fake_router.validation_requests[-1]
    assert "安娜" in request.messages[-1]["content"]
    assert "ADULT_" not in request.messages[-1]["content"]
    assert request.participant_facts[0]["age_years"] >= 18


def test_review_schema_unknown_or_timeout_blocks_without_candidate(service, db, fake_router, prepared):
    fake_router.next_result = RouteResult(job_id=prepared.job_id, output_text='{"safe": true, "issues": ["自由文本"]}', candidate_snapshot_hash="a" * 64, finish_state="succeeded", attempts=())
    events = list(service.finish_adult_candidate(prepared, "候选"))
    assert events[-1].data["code"] == "review_unavailable"
    assert "候选" not in (db.get_ai_job(prepared.job_id).get("output_text") or "")


def test_structural_block_is_visible_but_not_applicable(service, db, prepared):
    result = service.finalize_adult_candidate(prepared, "解释前缀\n正文", local_result=structural_validation("explanation_prefix"), safety_result=safe_validation(), fact_result=safe_validation())
    assert result.applicable is False
    assert db.get_application_for_owner(prepared.job_id, "owner-a")["applicable"] is False
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_adult_review.py -q`

Expected: FAIL because validation-stage routing, strict schemas and atomic candidate persistence do not exist.

- [x] **Step 3: Implement both fixed validation stages**

Restore the candidate only inside the service buffer before review, then pass the normalized candidate, ordered participant `character_id` list, `age_years`, `fictional`, allowed names/aliases, original target and project protection list to the review prompt. Never ask a generic writing Agent to perform either review. Parse JSON with `json.loads`, require exact object keys/types, `safe is True`, and reject any issue outside the fixed enum. Any timeout, router error, missing policy/hash mismatch, `safe=false`, minor/unknown-age/real-person/new-character issue, or fact-guard `unknown` becomes a blocking code.

Run local checks first, then safety review, then fact guard. A safety blocking result clears the buffer and finishes `failed/safety_blocked` without `candidate` or application. A review transport/parse failure finishes `failed/review_unavailable`. Fact/structure blocking after a valid candidate finishes `failed/validation_failed`; only non-safety structural warnings may be persisted as `applicable=false` and sent after commit for manual inspection. A fully safe result is persisted with `applicable=true`, `applied_at=NULL`, `validation_hash`, policy hashes, three-stage snapshots and sanitized `validation_json` in one `BEGIN IMMEDIATE` transaction before emitting `validation`, `candidate` and `done` events.

The transaction must use `WHERE status='running' AND owner_token=?`; terminal CAS failure discards the buffer and emits no candidate. `validation_json` contains only codes, counts, ratios and hashes; it never contains target/candidate/diff text. Enforce the 36,000-code-point/144,000-byte limits incrementally and discard the whole buffer on overflow.

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ai_adult_review.py -q`

Expected: PASS; both review attempts are stage `validation`, strict schema failures fail closed, safe candidates are atomically persisted before SSE exposure, and structural blocks are non-applicable.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/services/adult.py src/pixiv_novel_sync/storage/ai/adult.py tests/test_ai_adult_review.py
git commit -m "feat: add fixed adult safety and fact validation stages"
```

### Task 8: 乐观锁应用、派生数据失效和清理

**Files:**
- Modify: `src/pixiv_novel_sync/storage/ai/adult.py`
- Modify: `src/pixiv_novel_sync/storage/ai/core.py`
- Modify: `src/pixiv_novel_sync/ai/services/adult.py`
- Test: `tests/test_ai_adult_apply.py`

**Interfaces:**
- `apply_adult_polish(job_id: str, owner_scope: str, warning_ack_hash: str | None, access_token: str) -> dict[str, Any]`.
- `revalidate_stored_candidate(job_id: str, owner_scope: str) -> AdultValidationResult`.
- `ApplySnapshot` contains the stored chapter/project/participant/provider/policy/validation hashes and revisions required by the storage method; `AdultConflictError` carries a public Chinese `code` and never includes正文.
- `AdultStorageMixin.apply_adult_polish(job_id: str, owner_scope: str, warning_ack_hash: str | None, access_token_hash: str, expected_snapshot: ApplySnapshot) -> dict[str, Any]` returns `{application_id, chapter_revision_after, chapter_hash_after, already_applied}` and raises `AdultConflictError` for all 409 cases.

- [x] **Step 1: Write the failing tests**

```python
def test_apply_rejects_chapter_revision_aba_and_leaves_content(db, service, prepared):
    original = db.get_ai_chapter(prepared.chapter_id)["content"]
    db.update_ai_chapter(prepared.chapter_id, {"content": "临时"})
    db.update_ai_chapter(prepared.chapter_id, {"content": original})
    with pytest.raises(AdultConflictError, match="409"):
        service.apply_adult_polish(prepared.job_id, "owner-a", "", prepared.access_token)
    assert db.get_ai_chapter(prepared.chapter_id)["content"] == original


def test_two_concurrent_apply_calls_replace_once(db, service, prepared):
    results = run_concurrently(lambda: service.apply_adult_polish(prepared.job_id, "owner-a", prepared.warning_ack_hash, prepared.access_token), count=2)
    assert sum(not r.get("already_applied") for r in results if isinstance(r, dict)) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM ai_polish_applications WHERE source_job_id=?", (prepared.job_id,)).fetchone()[0] == 1


def test_scope_or_binding_change_returns_409_without_provider_call(service, db, prepared, fake_router):
    db.cas_update_review_binding("safety", expected_version=1, route={"binding_type": "fixed", "provider_id": 99, "model": "changed", "enabled": True})
    with pytest.raises(AdultConflictError, match="Provider 范围"):
        service.apply_adult_polish(prepared.job_id, "owner-a", prepared.warning_ack_hash, prepared.access_token)
    assert fake_router.execute_count == 0
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_adult_apply.py -q`

Expected: FAIL because no locked apply transaction, warning hash check or derivative invalidation exists.

- [x] **Step 3: Implement the write-lock transaction**

For the normal unchanged-policy path, use one `with db.transaction() as conn` block: re-read application, job, chapter, project facts, current owner authorization, character revisions, policy hashes, review binding hashes and the three-stage provider scope, validate them, then apply. Validate job status `succeeded`, `applicable=1`, owner/access token, current chapter/project relationship, exact `chapter_revision`, raw chapter hash, target slice hash, offsets, project facts hash, adult confirmation revision/hash, participant hash, binding/policy/validator hashes and `warning_ack_hash`.

For a policy-upgrade path, use three explicit phases so no network call holds a SQLite write lock: (1) acquire `BEGIN IMMEDIATE`, recompute provider scope/binding hashes and all content revisions, construct an immutable in-memory `ApplySnapshot` from those values, then release; a scope or binding mismatch returns 409 with zero Provider calls; (2) run both current validation stages outside the transaction and invalidate the old warning acknowledgment; (3) acquire `BEGIN IMMEDIATE` again, re-read every value from phase 1, require exact equality with `ApplySnapshot`, save the new validation hashes, and only then apply. Any phase-3 change returns 409 without writing the chapter.

After all checks hold under the write lock, construct `new_content = old_content[:start] + candidate + old_content[end:]`, update `content`, `word_count`, `chapter_revision = chapter_revision + 1`, `updated_at`, application `applied_at`, `chapter_hash_after`, `chapter_revision_after`, and job metadata in the same transaction. Insert/update one `ai_chapter_derivative_invalidations` row with reason `adult_polish_applied` so summaries, project states, audit output and retrieval indexes cannot be treated as current. `source_job_id` uniqueness makes repeated apply return the existing success without replacing text again. Never store the full chapter or candidate in the application row.

The apply JSON must contain the `warning_ack_hash` key: its value is `""` when there are no warnings and the exact canonical hash when warnings exist; a missing key is invalid. If a policy upgrade requires revalidation but `ai_jobs.output_text` has already expired, return 409 and require regeneration rather than reconstructing正文 from application metadata.

Update generic `cleanup_ai_jobs` to call the adult cleanup service: delete expired `ai_polish_applications` with `applied_at IS NULL` before their jobs, skip jobs referenced by unapplied applications, then apply the existing three-day policy to output text. Startup orphan repair must clear any legacy adult `output_text` lacking an application and mark its job failed.

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ai_adult_apply.py -q`

Expected: PASS; ABA, stale target, warning hash, policy/provider scope, owner and concurrent apply cases are rejected safely, while one successful transaction updates only the target and queues derivative rebuild.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/storage/ai/adult.py src/pixiv_novel_sync/storage/ai/core.py src/pixiv_novel_sync/ai/services/adult.py tests/test_ai_adult_apply.py
git commit -m "feat: apply adult candidates with revision and derivative locks"
```

### Task 9: 成人认证、owner scope、SSE 和 API 路由

**Files:**
- Create: `src/pixiv_novel_sync/ai/adult_auth.py`
- Modify: `src/pixiv_novel_sync/ai_web.py`
- Modify: `src/pixiv_novel_sync/webapp.py`
- Modify: `src/pixiv_novel_sync/storage/ai/core.py`
- Modify: `src/pixiv_novel_sync/storage/tasks.py`
- Test: `tests/test_ai_adult_web.py`
- Test: `tests/test_ai_adult_auth.py`

**Interfaces:**
- `AdultOwner` contains `scope: str` and `authenticated_at: int`; it never contains the Dashboard token or session cookie. `require_adult_owner(settings: Settings) -> AdultOwner` checks configured Dashboard token and `session['authenticated']`; `AdultOwner.scope` is an HMAC, never the raw token/session ID.
- `sign_adult_access(owner: AdultOwner, job_id: str) -> str` and `verify_adult_access(token, owner, job_id) -> None` use `hmac.compare_digest` and an expiry-bound payload.
- Add routes: `POST /api/dashboard/ai/polish/adult/stream`, `GET /api/dashboard/ai/polish/adult/<job_id>`, `GET /api/dashboard/ai/polish/adult/<job_id>/events`, `POST /api/dashboard/ai/polish/adult/<job_id>/cancel`, `POST /api/dashboard/ai/polish/adult/<job_id>/regenerate`, and `POST /api/dashboard/ai/polish/adult/<job_id>/apply`.
- Add `POST /api/dashboard/ai/polish/adult/scope` to return the three sanitized Provider candidate groups plus their canonical `provider_scope_hash` before generation.
- Add `GET/POST /api/dashboard/ai/projects/<project_id>/characters`, `PUT/DELETE /api/dashboard/ai/projects/<project_id>/characters/<character_id>`, `GET/PUT /api/dashboard/ai/projects/<project_id>/adult-confirmation`, and `GET/PUT /api/dashboard/ai/adult-review-bindings/<review_kind>`. Generic job list/detail/SSE/cancel/cleanup must pass owner scope when `task_type='adult_polish'`.

- [x] **Step 1: Write the failing route/security tests**

```python
def test_adult_route_requires_configured_token_even_from_loopback(client_without_token):
    response = client_without_token.post("/api/dashboard/ai/polish/adult/stream", json={})
    assert response.status_code == 403


def test_other_authenticated_session_cannot_read_adult_job(authenticated_client, seeded_adult_job):
    response = authenticated_client.get(f"/api/dashboard/ai/polish/adult/{seeded_adult_job}")
    assert response.status_code in {404, 403}
    assert "output_text" not in response.get_data(as_text=True)


def test_adult_sse_buffers_delta_and_sets_no_store_headers(authenticated_client, adult_payload):
    response = authenticated_client.post("/api/dashboard/ai/polish/adult/stream", json=adult_payload, buffered=True)
    assert response.headers["Cache-Control"].startswith("no-store")
    assert b"event: delta" not in response.data
    assert b"event: candidate" in response.data
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_adult_auth.py tests/test_ai_adult_web.py -q`

Expected: FAIL because adult routes, owner scope and signed access tokens do not exist.

- [x] **Step 3: Implement route-level authorization and responses**

`require_adult_owner` must reject when `settings.dashboard_token` is empty, regardless of loopback, and reject a missing/false session authentication flag. Derive `scope = HMAC-SHA256(app.secret_key, b'adult-owner:' + configured_dashboard_token)`; do not persist the token or session cookie. Generate a signed, short-lived access token containing only owner scope, job ID, issue/expiry and a random nonce. Every candidate read, copy, retry, cancel, event resume and apply verifies both owner scope and project/chapter authorization; an unknown owner receives a non-enumerating 404.

Use the existing CSRF middleware for all mutating routes. Adult stream responses emit only `metadata`, `progress`, `validation`, `candidate`, `done` or sanitized `error` events and set `Cache-Control: no-store, Pragma: no-cache, X-Robots-Tag: noindex, nofollow, noarchive, X-Content-Type-Options: nosniff`. `GET /api/dashboard/ai/polish/adult/<job_id>/events` requires the signed resume token bound to owner/job. Map malformed input to 400/422, missing authentication to 403, stale versions/provider scope to 409, and owner-hidden jobs to 404.

On `GeneratorExit` or SSE socket failure, request cancellation only while the job remains `running` and has no committed application; the owner-token CAS cannot overwrite `partial`, `failed`, `cancelled` or `succeeded`. A late disconnect after `candidate` commit leaves the terminal job/application intact.

Extend `AiCoreMixin.list_ai_jobs`, `get_ai_job`, unified `TasksMixin.list_ai_task_logs`, cancellation and cleanup to add `owner_scope=?` whenever the task is adult; no existing non-adult job behavior changes. Startup `fail_stale_ai_jobs` must honor model-router owner/lease CAS and never overwrite terminal adult jobs.

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ai_adult_auth.py tests/test_ai_adult_web.py tests/test_webapp_security.py -q`

Expected: PASS; tokenless loopback is denied for adult routes, CSRF and owner isolation hold, SSE never exposes raw delta/partial output, and generic logs do not cross owners.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/ai/adult_auth.py src/pixiv_novel_sync/ai_web.py src/pixiv_novel_sync/webapp.py src/pixiv_novel_sync/storage/ai/core.py src/pixiv_novel_sync/storage/tasks.py tests/test_ai_adult_web.py tests/test_ai_adult_auth.py
git commit -m "feat: secure adult polish routes with owner-scoped SSE"
```

### Task 10: 章节详情页签、码点选择和设置界面

**Files:**
- Modify: `src/pixiv_novel_sync/templates/dashboard_ai_reader.html`
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings.html`
- Modify: `tests/test_ai_adult_frontend.py`

**Interfaces:**
- Reader UI calls the adult stream/apply endpoints and keeps `selectedRange`, `candidate`, `validation`, `warnings`, `blockingIssues`, `providerScopes`, and `accessToken` in Vue state.
- Browser selection conversion is `selectionToCodePointRange(root: Node, selection: Selection) -> {start: number, end: number}`; it counts `Array.from(textContent)` code points and never UTF-16 units.
- UI exposes one continuous target, read-only before/after context, intensity overrides, locked terms, explicit participant IDs, provider-scope confirmation, warning acknowledgment and one disabled-until-valid “应用到章节” control.

- [x] **Step 1: Write the failing frontend contract tests**

```python
def test_reader_contains_adult_tab_and_codepoint_conversion_contract():
    html = Path("src/pixiv_novel_sync/templates/dashboard_ai_reader.html").read_text(encoding="utf-8")
    assert "成人描写润色" in html
    assert "selectionToCodePointRange" in html
    assert "Array.from" in html
    assert "innerHTML" not in html


def test_reader_does_not_normalize_content_before_offset_submission():
    html = Path("src/pixiv_novel_sync/templates/dashboard_ai_reader.html").read_text(encoding="utf-8")
    assert "replace(/\\r\\n/g" not in html
    assert "/api/dashboard/ai/polish/adult/stream" in html


def test_settings_shows_fixed_policy_hashes_and_binding_capability():
    html = Path("src/pixiv_novel_sync/templates/dashboard_settings.html").read_text(encoding="utf-8")
    assert "adult_safety_policy" in html
    assert "adult_fact_guard_policy" in html
    assert "json" in html
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ai_adult_frontend.py -q`

Expected: FAIL because the reader and settings templates have no adult tab or binding controls.

- [x] **Step 3: Implement the reader interaction**

Add a sibling tab beside正文/章节设置/普通润色 in `dashboard_ai_reader.html`. Render the chapter’s raw content in text nodes; do not call `replace`, `innerHTML`, or a normalizing formatter for selection. Implement the exact conversion algorithm:

```javascript
function selectionToCodePointRange(root, selection) {
  if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) throw new Error('请选择连续片段');
  const range = selection.getRangeAt(0);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let start = -1, end = -1, offset = 0, node;
  while ((node = walker.nextNode())) {
    const length = Array.from(node.nodeValue || '').length;
    if (node === range.startContainer) start = offset + Array.from(node.nodeValue || '').slice(0, range.startOffset).length;
    if (node === range.endContainer) end = offset + Array.from(node.nodeValue || '').slice(0, range.endOffset).length;
    offset += length;
  }
  if (start < 0 || end <= start) throw new Error('请选择连续片段');
  return {start, end};
}
```

On generate, submit only IDs, code-point offsets, client hashes/revision, participant IDs, intensity/instruction/locked terms, idempotency key and confirmed `provider_scope_hash`; never submit target text. Show read-only context and candidate only after `candidate` event. Display progress/attempt model names without keys, validation warnings/blocking codes and a diff produced from escaped text nodes. Require `warning_ack_hash` and signed access token for apply; disable the apply button for every blocking code, non-succeeded state, stale warning or missing provider-scope confirmation. Regeneration uses a new idempotency key and `parent_job_id`.

In `dashboard_settings.html`, add adult content/fictional-character toggles, character CRUD with expected revision, sorted confirmation list, and a separate review-binding section that displays immutable policy IDs/hashes and fixed `json` capability. Do not expose policy text editing or internal Agent delete/disable controls.

- [x] **Step 4: Run frontend tests**

Run: `python -m pytest tests/test_ai_adult_frontend.py tests/test_frontend_library_os.py -q`

Expected: PASS; the page uses raw text nodes, code-point offsets, existing CSRF helpers and no unsafe HTML injection.

- [x] **Step 5: Commit**

```powershell
git add src/pixiv_novel_sync/templates/dashboard_ai_reader.html src/pixiv_novel_sync/templates/dashboard_settings.html tests/test_ai_adult_frontend.py
git commit -m "feat: add adult polish chapter tab and confirmation controls"
```

### Task 11: API 文档、中文说明和全链路验收

**Files:**
- Modify: `README.md`
- Modify: `docs/frontend-api-contract.md`
- Modify: `docs/frontend-pages.md`
- Modify: `docs/INDEX.md`
- Create: `tests/test_ai_adult_integration.py`

**Interfaces:**
- Document exact adult routes, request/response envelopes, 403/404/409 semantics, no-store headers, retention and configuration prerequisites.
- Integration fixture wires a fake `ModelRouter` through main/safety/fact stages and verifies the complete stream-to-apply lifecycle without a real Provider.

- [x] **Step 1: Write the failing integration test**

```python
def test_adult_polish_end_to_end_changes_only_target_and_records_snapshots(app, seeded_project, fake_router):
    login_dashboard(app)
    before = get_chapter(app, seeded_project.chapter_id)["content"]
    payload = valid_adult_payload(seeded_project)
    response = app.test_client().post("/api/dashboard/ai/polish/adult/stream", json=payload, buffered=True)
    assert b"event: candidate" in response.data
    job_id = parse_event(response.data, "metadata")["job_id"]
    apply = app.test_client().post(f"/api/dashboard/ai/polish/adult/{job_id}/apply", json={"warning_ack_hash": ""}, headers={"X-CSRF-Token": csrf(app)})
    assert apply.status_code == 200
    after = get_chapter(app, seeded_project.chapter_id)["content"]
    assert after[:payload["target_start"]] == before[:payload["target_start"]]
    assert after[payload["target_end"]:] == before[payload["target_end"]:]
    assert fake_router.stages == ["main", "validation", "validation"]
```

- [x] **Step 2: Run the integration test to verify it fails**

Run: `python -m pytest tests/test_ai_adult_integration.py -q`

Expected: FAIL until all storage, service, route and UI contracts are wired together.

- [x] **Step 3: Document and close the contract**

Document configuration order: configure Dashboard token; create/sync or manually add Provider models through the model-pool feature; create a fixed/pool `adult_polish` Agent; configure both review bindings with `json`; create structured fictional character records; enable adult content and confirm current character revisions. State explicitly that no adult route works in tokenless single-user mode, no automatic Pipeline step exists, output is retained for three days only when unapplied, applied metadata retains hashes/snapshots without正文, and all policy/provider-scope/revision conflicts require regeneration. Update the README’s old “single Provider/no fallback” wording to refer to the model-router contract while keeping the adult-specific fail-closed rules. Add endpoint tables and page navigation to `frontend-api-contract.md`/`frontend-pages.md`; mark this plan as the active implementation plan in `docs/INDEX.md`.

- [x] **Step 4: Run focused, full and static verification**

Run: `python -m pytest -q -k adult`

Expected: PASS for all adult unit, storage, route, concurrency and frontend tests.

Run: `python -m pytest -q`

Expected: PASS for the full existing suite with no regression in non-adult Agent/Provider behavior.

Run: `git diff --check`

Expected: no whitespace errors. Also run `rg -n "stream_generate\\(" src/pixiv_novel_sync/ai/services/adult.py src/pixiv_novel_sync/ai_web.py` and expect no matches; run `rg -n "target_text|before|after|system_prompt|prompt" tests -g 'test_ai_adult*.py'` to ensure tests cover rejection rather than persistence.

Start the local Flask app with a temporary database and configured Dashboard token, then use Playwright at desktop `1440x900` and mobile `390x844` to capture the chapter reader adult tab and settings binding view. Verify no overlap, clipped labels, nested cards, blank panels or horizontal overflow; exercise selection containing Chinese, emoji and a combining character, and confirm the submitted code-point range selects exactly the highlighted text. Stop the local server after verification.

- [x] **Step 5: Commit**

```powershell
git add README.md docs/frontend-api-contract.md docs/frontend-pages.md docs/INDEX.md tests/test_ai_adult_integration.py
git commit -m "docs: document adult polish agent and verify end to end"
```

## Self-review checklist

- Spec coverage: Tasks 1-5 cover input limits, raw/canonical hashes, prompt boundaries, locked terms, placeholders, style inheritance and fixed policies; Tasks 6-7 cover model routing, buffering, partial/failed states, both validation stages and snapshots; Tasks 8-9 cover revision/provider-scope CAS, warning acknowledgment, owner authorization, retention and no-store/SSE; Task 10 covers chapter selection/diff/settings; Task 11 covers acceptance, docs and full tests.
- Placeholder scan: every task names concrete files, public signatures, test commands, expected red/green behavior and a commit message; no unspecified implementation marker or deferred decision is required.
- Type consistency: `AdultPolishRequest`, `AdultValidationResult`, `AdultRouteRequest`, `CandidateSnapshot`, `RouteRequest`, `RouteResult`, `AdultOwner` and storage method names are introduced before later tasks consume them. The only external implementation is the model-pool `ModelRouter` contract stated in the header and Task 6.
- Dependency boundary: no adult task depends on model-pool table names or SQL; only the router DTO and validation-stage behavior are required.
- Safety boundary: ordinary CRUD cannot reach either policy or binding, local checks cannot be disabled, and every unknown/changed policy or review result fails closed.

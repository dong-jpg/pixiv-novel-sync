# AI Settings And Invocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在独立运行的 Pixiv Novel Sync 中交付 Magpie 风格的 AI 配置流程、逐模型容量预检、统一文本调用、可信用量与安全诊断，同时保持旧配置和路由语义的兼容边界。

**Architecture:** 不新增网关或平行任务系统。围绕既有 `AIWritingService -> AIServiceCore -> ModelRouter -> Provider` 调用链增加小型请求/规划/快照/用量模块，使用现有 SQLite 表的加列迁移。两张旧设置页复用小型共享 JavaScript 控制器和选择器，后端负责能力、容量、CAS 与实际用量事实。

**Tech Stack:** Python 3.10+, Flask 3.0.3+, requests, SQLite, pytest, Jinja/Vue 3.4.38, existing static Tailwind CSS, Node built-in tests, existing optional Playwright dependency.

**Approved Spec:** `docs/superpowers/specs/2026-10-31-ai-settings-and-invocation-design.md`；用户于 2026-11-01 确认。

**Status:** 本文件是实施计划，不是实现报告。所有步骤初始未执行；真正的测试结果、提交和审查结论记入执行 worktree 的 `.superpowers/sdd/progress.md`。

## Global Constraints

以下约束继承批准稿；每个任务都必须遵守，不能用局部示例削弱：

- 维持 Python 3.10+、现有 requests/Flask/SQLite 依赖和无独立前端构建器的部署方式。本阶段不增加必装 tokenizer、Agent 框架或第三方服务。
- 保留 `/dashboard/settings/models` 与 `/dashboard/settings/agents`，不改全站导航、前端框架或 Library OS 视觉语言。
- “路由组”是现有模型池的用户文案，数据库表、整数 ID 和 `/model-pools` API 不重命名。第一阶段只提供顺序尝试；组内成员顺序和后备池顺序直接对应真实候选展开顺序。
- 保留：单池 64 个成员、展开后 64 个候选、最多 8 个池节点；每 job 最多 16 个候选尝试、32 次实际网络请求、总 deadline 30 分钟。调用方可以设置更紧限制，不能调大服务器硬上限。
- 保留已有 Provider/Agent 数值及默认值，不在迁移时把 128k/16k 自动抬到 1M。UI 解释真正的瓶颈及可编辑位置，由用户显式扩大上限。
- 保留逐候选预算。禁止在筛选后再次用 `max(所有输入估计)` 对比 `min(所有窗口)`：不同模型分词不同，该交叉比较可能错误拒绝原本分别装得下的请求。
- 无 version 的历史快照视为 version 1，沿用原解码、hash、预算语义和底层续接校验；不能重新解析当前配置把旧 job 升成另一条候选链。这不意味着 main 的 continue API 已有业务处理器。
- 本阶段不新增业务重放处理器。main 的 continue API 保留“任务类型暂不支持”的 409 行为，不创建新 job、不发网络请求；详情追加 `can_continue=false` 和原因，UI 不展示可执行的继续按钮。
- main 的第一个正文 delta 对外发布后锁定目标，失败进入 partial，不拼接另一模型输出。internal/validation 的输出只有完整成功才交给业务，失败残片不混入下次结果。usage/progress 元数据不视为正文 delta。
- 发送前在领取网络序号的同一事务中写入该 attempt 的 pending/用量未知记录，提交成功后才允许发送。
- 在既有 `ai_job_model_attempts` 增加可空 `usage_json`，只保存数值记录和白名单状态，单 attempt 不超过 16 KiB；最多 32 个请求记录。老 attempt 保持 null。
- 统一调用采用内部 `metadata_only` 留存策略；成功、partial、取消和异常路径均不得把原始输入/输出写入 `ai_jobs.input_json/output_text/output_json`、attempt、错误字段或普通日志。业务正文通过内存结果返回。
- 唯一兼容例外是旧 `keyword_clean`：保留其前 80 个词/40 个标签输入及现有有限清洗输出/解析结果记录。该例外只由关键词清洗服务内部选择，不是公共 API 参数，也不能供未来小说正文调用借用。
- 默认回归全部使用临时数据库、合成文本和 fake HTTP/DNS，不访问真实 Pixiv/Provider 或本地小说。
- 不新增 Gemini/Responses 原生协议、工具执行循环、订阅/插件系统、整书任务/表、金额硬预算、向量库或写作/成人模块。

## Execution Workspace And Baseline

- 计划编写时 checkout 为 `D:\gitcode\pixiv-novel-sync`，HEAD 为 `baf9e21`。实施从包含本计划的提交开始，不从远端旧默认分支开始。
- 原 checkout 中 README、项目状态/契约等六份文档及 `.probe012/`、`docs/codex-for-oss-application.md` 有既存用户修改；不重置、不覆盖、不夹带提交。
- 原 checkout 的 `.superpowers/sdd/progress.md` 是 2026-09 的其他任务账本，不能当作本计划进度，也不能清空或覆盖。
- 后续独立 worktree 的安排需用户确认；确认后先查本聊天的 attached worktrees，优先使用合适的现有 worktree，否则用 Codex 原生 create_worktree，`ref` 指向计划提交。不得手工建立 app 无法管理的 worktree。
- 本计划所有源码/测试路径按执行 worktree 根目录解析。片段中出现的 `D:/gitcode/pixiv-novel-sync` 只是定位基线，必须替换为实际 worktree 根；工具调用显式设置 workdir，不能在原 `main` 中实施。
- 若原生工具返回 detached HEAD，可以在该受管理 checkout 提交；不在原 main 开始功能实现。分支需要创建时使用 `codex/` 前缀，不擅自推送、合并或部署。
- 检查现有依赖，不重绑共享环境里的 editable install。pytest 使用当前 checkout 的 `pyproject.toml` 中 `pythonpath=src`；独立 CLI/浏览器测试进程显式设置其 PYTHONPATH 为该 worktree 的 `src`。

开始 Task 1 前，在执行 worktree 运行并记录：

```powershell
git status --short
git rev-parse HEAD
python --version
python -m pytest -q
python -m compileall -q src
python scripts/build_tailwind_css.py --check
```

基线失败时保留原始命令/结果，在写功能代码前报告并确认处理范围；不把历史报告的通过数当成当前证据。默认测试不能因为本计划而发起真实模型请求。

## Dependency Order And File Ownership

执行顺序为 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8。一个任务通过规格与代码质量审查后才标完成；反馈修复重跑覆盖测试。前后任务共享文件时顺序执行，不让两个实现者同时编辑同一 Router、服务或模板。

| Task | 独立交付 | 主要所有权 | 依赖 |
|---|---|---|---|
| 1 | 不可变文本契约与纯逐模型规划 | `ai/invocation.py`, `ai/invocation_planner.py` | 无 |
| 2 | 输出上限与原子选模配置 | model catalog/schema/storage/admin | 1 的命名约定 |
| 3 | v2 快照与原 Router 集成 | `ai/invocation_candidates.py`, `ai/invocation_snapshot.py`, Router/core | 1, 2 |
| 4 | 协议归一用量与逐发送观察 | `ai/usage.py`, Provider adapters | 1, 3 |
| 5 | 可恢复的用量记录、隐私留存、关键词接入 | AI storage/core/services/keyword | 1-4 |
| 6 | 有界 preflight、原子选模 API、任务诊断 | web limits, ai_web, admin, Nginx | 1-5 |
| 7 | 共享选模器与模型设置工作台 | shared JS/partials, models page, CSS/package | 2, 6 |
| 8 | AI 任务设置、日志与整体验收 | agents/logs pages, preflight/diagnostics JS, tests/docs | 1-7 |

## Shared Contract Decisions

- 唯一规范请求为 Task 1 的 `TextInvocation`。`normalize_text_invocation` 不注入系统提示词；HTTP 客户端发送的就是完整消息。
- `InvocationValidationError` 暴露固定的安全 `.code` 和本地说明，不回显未知字段值或正文。
- `InvocationPlan.to_public_dict()` 的行字段由 Task 1 定义；UI 只展示，不再计算 token 或容量。可返回 `excluded_count/excluded_truncated` 解释有界详情，不因此隐藏配置/预算失败。任务详情中的 v2 计划经服务器解码后以 `invocation_plan` 提供同一安全投影，不让 UI 解码私有快照或把 v2 当作旧的单值 PromptBudget。
- v1 snapshot 的 dataclass 字段和 hash 不动。v2 用 `PlannedCandidateSnapshot`；JSON 统一使用数组而非在恢复时混淆 tuple/list。
- 纯预检不解密/升级 Key；实际 v2 调用仅在冻结前对合格 Provider 尝试旧密文升级，冻结后使用只解密、不回写的 loader。不能让透明升级在第一次发送时改变自身的配置 hash。
- `RouteResult` 的原五字段保持，按 Task 3 附加默认字段。`finish_state` 仍是传输四态；Task 5 设置 `job_status` 为整体业务终态，并设置 `validation_status` 与 `usage_summary`。传输成功但业务校验失败不能伪造成功 job。
- Catalogue revision 是 64 位小写 SHA-256 字符串，不是递增整数或新增数据库列。它对完整配置投影计算，与界面的筛选/分页无关。
- Provider `request_guard()` 返回本次网络 ordinal 或 legacy None；`on_usage(ordinal, mapping)` 只传该次请求的归一化累计快照，不是增量 token。
- `usage_summary` 为 `{known_input_tokens, known_output_tokens, known_total_tokens, reported_requests, unreported_requests, complete}`；前三项为整数或 null，未完整报告的请求计入 unreported，零请求的 UI 显示“未发出请求”。
- `keyword_compat` 是私有服务策略，不是用户可传入的请求字段。新通用调用始终由服务端生成 metadata_only 审计信封，DB 最终写路径也必须执行该策略。
- 新 preflight 缺 Agent 返回 404；有效但无合格候选返回 200/can_run=false。旧 GET candidates 的既有状态码不顺带修改。
- Shared `window.aiApi.request()` 成功返回解包的 data，失败 Error 保留 status/data。Onboarding 不能把 probe 返回值当成已落库目录事实。

## Runtime Interface Inventory

Task 1 owns the definitions below. Do not create competing `UnifiedRequest`, `AIRequest`, or planner DTOs in later tasks.

```python
# ai/invocation.py
class InvocationValidationError(ValueError):
    code: str

@dataclass(frozen=True, slots=True)
class TextMessage:
    role: Literal["system", "user", "assistant"]
    content: str

@dataclass(frozen=True, slots=True)
class TextInvocation:
    messages: tuple[TextMessage, ...]
    temperature: float
    top_p: float
    max_tokens: int
    allowed_provider_ids: tuple[int, ...] | None = None

def normalize_text_invocation(
    payload: Mapping[str, Any], *, agent: AIAgentConfig,
) -> TextInvocation: ...

def invocation_messages(request: TextInvocation) -> list[dict[str, str]]: ...

def invocation_fingerprint(
    request: TextInvocation, *, provider_ids: tuple[int, ...],
) -> str: ...

# ai/invocation_planner.py
TokenEstimator = Callable[[TextInvocation, CandidateProfile], TokenEstimate | None]

def plan_invocation(
    request: TextInvocation,
    candidates: tuple[CandidateProfile, ...],
    *,
    agent_context_window: int,
    required_capabilities: tuple[str, ...] = (),
    estimate: TokenEstimator = estimate_text_invocation,
    estimator_version: str = ESTIMATOR_VERSION,
) -> InvocationPlan: ...

# ai/model_router.py -- Task 3 adds these methods, not a second router.
def plan_invocation(self, agent: AIAgentConfig, request: TextInvocation) -> InvocationPlan: ...
def resolve_invocation(self, agent: AIAgentConfig, request: TextInvocation) -> PlannedCandidateSnapshot: ...
```

The ellipses in this interface inventory are signatures, not implementation placeholders. The task steps supply the algorithms and changed code below.

Task 3 adds `PlannedCandidateSnapshot` in `ai/invocation_snapshot.py`:

```python
@dataclass(frozen=True, slots=True)
class PlannedCandidateSnapshot:
    candidates: tuple[ModelCandidate, ...]
    snapshot_hash: str
    agent_config_hash: str
    binding_version: int
    plan: InvocationPlan
    version: int = 2

def planned_snapshot_payload(snapshot: PlannedCandidateSnapshot) -> dict[str, Any]: ...
def planned_snapshot_from_payload(payload: Mapping[str, Any], expected_hash: str) -> PlannedCandidateSnapshot: ...
```

Avoid a module cycle: `invocation_snapshot.py` imports `ModelCandidate` under `TYPE_CHECKING`, and locally inside its construction/decoding functions. `model_router.py` imports/re-exports `PlannedCandidateSnapshot` normally; the new module does not import the Router at module initialization.

Task 3 appends these defaulted fields to `RouteResult`, after its five existing fields:

```python
snapshot_version: int = 1
actual_provider_id: int | None = None
actual_model_key: str | None = None
finish_reason: str | None = None
validation_status: Literal["not_requested", "passed", "failed"] | None = None
usage_summary: dict[str, Any] | None = None
job_status: Literal["succeeded", "failed", "partial", "cancelled"] | None = None
```

The Router fills version and actual target/finish information from its latest matching-stage attempt that succeeded or started output. No-output failures leave `actual_*` null and expose the already-existing attempt list. Task 5 fills validation/usage and the overall `job_status` through `dataclasses.replace`, not another result envelope. `finish_state` remains the transport enum; a successfully transported but invalid business result has `finish_state="succeeded"`, `validation_status="failed"`, and `job_status="failed"`.

Other task contracts consumed, not implemented here:

- Task 2 adds nullable effective `max_output_tokens` to catalog rows while preserving manual/discovered fields.
- Task 4 owns `ai/usage.py` and `ProviderUsage`; Provider `request_guard` becomes `Callable[[], int | None] | None`, and `on_usage: Callable[[int, Mapping[str, Any]], None] | None` receives cumulative normalized usage for one ordinal.
- Task 5 owns `claim_ai_job_network_request(job_id, owner_token, attempt_index=None) -> int`, atomic unknown-usage reservations, usage merges, audit-envelope construction, DB-level retention, safe persisted errors, validation, and `invoke_text`/the private keyword compatibility wrapper.
- Task 5's public method is `invoke_text(self, agent_id: int, payload: Mapping[str, Any], *, stop_requested: Callable[[], bool] | None = None, deadline_at: str | None = None, on_delta: Callable[[str], None] | None = None, on_progress: Callable[[dict[str, Any]], None] | None = None) -> RouteResult`. It accepts no retention or validator option.
- The server audit marker is `audit_version=1`, `retention_policy="metadata_only"`. Only the private `_invoke_keyword_clean` helper may choose `"keyword_compat"`. A missing policy remains solely the old v1 low-level/history behavior, not a new public option.
- Task 6 consumes `normalize_text_invocation(..., agent=...)`, `ModelRouter.plan_invocation(...)`, and `InvocationPlan.to_public_dict()` for preflight. Its HTTP wrapper enforces 16 MiB and 256 messages; the pure request normalizer does not impose the HTTP-specific count limit or inject the Agent system prompt.

## UI Interface Inventory

All HTTP dependencies below resolve the unwrapped `data` returned by `window.aiApi.request`, not the `{ok,data}` envelope. Failed requests reject with an `Error` carrying `status` and `data`.

```javascript
// A model's provider_model_id may be null for a legacy hand-entered fixed model.
// availability: ready | disabled | unavailable | unverified.
// All labels are plain strings rendered by Vue interpolation, never v-html.
// kind=model: provider_id, provider_model_id, model_key, provider_name, label.
// kind=pool: pool_id, label. Both kinds also carry availability and reason.

AISettings.optionKey(option); // stable opaque string, never a wire model ID
AISettings.retainCurrent(options, current); // cloned rows; current remains first
AISettings.filterOptions(options, query); // matches ID/name/provider/status
AISettings.readWorkspace(search); // {view, providerId, invalidProvider}
AISettings.workspaceSearch(search, patch); // query string, preserves other keys
AISettings.touchedPatch(original, draft, touchedFields); // no default coercion
AISettings.moveItem(items, from, to); // reordered copy, source untouched
AISettings.initialModelSelection(catalogue, selectedKeys); // {revision,changes,missing}
AISettings.parseNullableOutput(value); // integer 1..1000000 or explicit null
AISettings.createDraftGuard({read, save, discard, confirm}); // markSaved/isDirty/leave

AIPicker.createModelPicker(Vue); // Vue component definition
// props: options, current, label, id, disabled; emits: select(option).
// Opening, filtering, refreshing options, and closing emit no select event.

AIProviderOnboarding.createOnboarding({request, waitForSync, readCatalog, onChange});
// save(form, selectedKeys), retry(), confirmRevision(), dispose(), getState().
// waitForSync(providerId, operationId) resolves the final operation projection;
// needs_empty_confirmation is not success. Reuses the existing SSE whitelist.
// readCatalog(providerId) must GET the FULL catalogue, without a search filter.
```

## Task Details

### Task 1: Canonical Text Contract And Pure Candidate Planner

**Dependencies:** None. Run this task before catalog/runtime/provider/UI consumers. No DB schema changes or Provider edits in this task.

**Files owned by Task 1:**

- Create: `src/pixiv_novel_sync/ai/invocation.py` -- immutable request values, safe validation, canonical fingerprint.
- Create: `src/pixiv_novel_sync/ai/invocation_planner.py` -- secret-free profile/budget values, local estimator, pure planner, private/public serialization.
- Create: `tests/test_ai_invocation_contract.py`.
- Create: `tests/test_ai_invocation_planner.py`.
- Do not modify `ai/models.py`, `ai/providers.py`, or the existing v1 estimator/Router during this task.

#### 1A. Immutable Request Boundary

- [ ] Add the following first failing tests to `test_ai_invocation_contract.py`; use synthetic text only.

```python
from dataclasses import FrozenInstanceError

import pytest

from pixiv_novel_sync.ai.invocation import (
    InvocationValidationError, invocation_fingerprint, invocation_messages,
    normalize_text_invocation,
)
from pixiv_novel_sync.ai.models import AIAgentConfig


def agent():
    return AIAgentConfig(
        id=1, name="test", task_type="general", provider_id=1, model="m",
        system_prompt="must-not-be-injected", temperature=0.8, top_p=0.9,
        max_tokens=4000, context_window=1_000_000,
    )


def test_request_preserves_text_and_zero_parameters_without_aliasing():
    payload = {"messages": [{"role": "user", "content": "  START\n\u4e2d\U0001f642\nEND  "}],
               "temperature": 0, "top_p": 0, "max_tokens": 100,
               "allowed_provider_ids": []}
    request = normalize_text_invocation(payload, agent=agent())
    original = request.messages[0].content
    payload["messages"][0]["content"] = "mutated"
    outgoing = invocation_messages(request)
    outgoing[0]["content"] = "also mutated"
    assert request.messages[0].content == original
    assert request.temperature == request.top_p == 0.0
    assert request.allowed_provider_ids == ()
    assert len(request.messages) == 1
    with pytest.raises(FrozenInstanceError):
        request.max_tokens = 1


@pytest.mark.parametrize("patch", [
    {"messages": []},
    {"messages": [{"role": "tool", "content": "x"}]},
    {"messages": [{"role": "user", "content": {"secret": "x"}}]},
    {"messages": [{"role": "user", "content": "x", "tool_calls": []}]},
    {"temperature": True}, {"temperature": float("nan")},
    {"top_p": None}, {"top_p": 1.1}, {"max_tokens": False},
    {"max_tokens": 0}, {"max_tokens": 1_000_001},
    {"allowed_provider_ids": None}, {"allowed_provider_ids": [True]},
    {"allowed_provider_ids": [0]}, {"allowed_provider_ids": ["1"]},
    {"tools": []}, {"response_format": {"type": "json_schema"}},
])
def test_invalid_request_has_local_safe_error(patch):
    payload = {"messages": [{"role": "user", "content": "DO-NOT-ECHO"}], **patch}
    with pytest.raises(InvocationValidationError) as caught:
        normalize_text_invocation(payload, agent=agent())
    assert caught.value.code
    assert "DO-NOT-ECHO" not in str(caught.value)
    assert "must-not-be-injected" not in str(caught.value)


def test_fingerprint_covers_order_content_parameters_and_effective_scope():
    base = {"messages": [{"role": "user", "content": "abc"}], "max_tokens": 100}
    first = normalize_text_invocation(base, agent=agent())
    omitted = invocation_fingerprint(first, provider_ids=(1, 2))
    assert omitted == invocation_fingerprint(first, provider_ids=(2, 1, 2))
    for patch in ({"messages": [{"role": "assistant", "content": "abc"}]},
                  {"messages": [{"role": "user", "content": "abc "}]},
                  {"max_tokens": 101}, {"temperature": 0}):
        other = normalize_text_invocation({**base, **patch}, agent=agent())
        assert invocation_fingerprint(other, provider_ids=(1, 2)) != omitted
    empty = normalize_text_invocation({**base, "allowed_provider_ids": []}, agent=agent())
    assert invocation_fingerprint(empty, provider_ids=()) != omitted
```

- [ ] Run `python -m pytest tests/test_ai_invocation_contract.py -q` from the execution worktree. Expected initial result: collection fails because `pixiv_novel_sync.ai.invocation` does not exist.

- [ ] Implement `invocation.py` with these complete normalization rules and functions. The dataclasses are frozen and callers must use `normalize_text_invocation` at service/API boundaries.

```python
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .models import AIAgentConfig


class InvocationValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TextMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class TextInvocation:
    messages: tuple[TextMessage, ...]
    temperature: float
    top_p: float
    max_tokens: int
    allowed_provider_ids: tuple[int, ...] | None = None


def _sampling(value: Any, field: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvocationValidationError("invalid_parameter", field + " must be numeric")
    if isinstance(value, int):
        if not 0 <= value <= maximum:
            raise InvocationValidationError("invalid_parameter", field + " is out of range")
        result = float(value)
    else:
        result = value
    if not math.isfinite(result) or not 0 <= result <= maximum:
        raise InvocationValidationError("invalid_parameter", field + " is out of range")
    return result

修正（2026-11-02）：整数采样值必须先按原值检查范围再转 `float`，避免 `10**400` 触发 `OverflowError`；错误仍固定为不包含输入值的 `invalid_parameter`。


def normalize_text_invocation(payload: Mapping[str, Any], *, agent: AIAgentConfig) -> TextInvocation:
    if not isinstance(payload, Mapping):
        raise InvocationValidationError("invalid_request", "Request must be an object")
    allowed = {"messages", "temperature", "top_p", "max_tokens", "allowed_provider_ids"}
    if set(payload) - allowed:
        raise InvocationValidationError("unsupported", "Request contains unsupported fields")
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, (list, tuple)) or not raw_messages:
        raise InvocationValidationError("invalid_messages", "At least one text message is required")
    messages = []
    for item in raw_messages:
        if not isinstance(item, Mapping) or set(item) != {"role", "content"}:
            raise InvocationValidationError("invalid_messages", "Message fields must be role and content")
        role, content = item["role"], item["content"]
        if role not in ("system", "user", "assistant") or not isinstance(content, str):
            raise InvocationValidationError("invalid_messages", "Message role or content is invalid")
        try:
            content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise InvocationValidationError("invalid_messages", "Message is not valid UTF-8 text") from exc
        messages.append(TextMessage(role, content))
    maximum = payload.get("max_tokens", agent.max_tokens)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 1_000_000:
        raise InvocationValidationError("invalid_parameter", "max_tokens must be an integer from 1 to 1000000")
    provider_ids = None
    if "allowed_provider_ids" in payload:
        raw_ids = payload["allowed_provider_ids"]
        if not isinstance(raw_ids, (list, tuple)) or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in raw_ids
        ):
            raise InvocationValidationError("invalid_scope", "Provider scope must contain positive integer IDs")
        provider_ids = tuple(sorted(set(raw_ids)))
    return TextInvocation(
        tuple(messages),
        _sampling(payload.get("temperature", agent.temperature), "temperature", 2.0),
        _sampling(payload.get("top_p", agent.top_p), "top_p", 1.0),
        maximum,
        provider_ids,
    )


def invocation_messages(request: TextInvocation) -> list[dict[str, str]]:
    return [{"role": message.role, "content": message.content} for message in request.messages]


def invocation_fingerprint(request: TextInvocation, *, provider_ids: tuple[int, ...]) -> str:
    payload = {
        "messages": invocation_messages(request),
        "temperature": request.temperature,
        "top_p": request.top_p,
        "max_tokens": request.max_tokens,
        "allowed_provider_ids": sorted(set(provider_ids)),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
```

The temperature upper bound is a canonical text-API bound, not a promise that every upstream accepts every sampling combination. A protocol rejection remains a structured attempt error; do not silently clamp values or introduce model-name heuristics here.

- [ ] Run `python -m pytest tests/test_ai_invocation_contract.py -q`; all tests pass. Also run `python -m pytest tests/test_ai_agent_bindings.py -q` to demonstrate no existing Agent constructor or persistence was changed.

#### 1B. Candidate Profiles, Independent Budgets, And Safe Projection

- [ ] Add these DTO definitions to `invocation_planner.py`. `CandidateProfile` is a safe copy of configuration, never a Provider instance. It deliberately does not inherit `ModelCandidate` or alter its v1 fields.

```python
PLANNER_VERSION = "capacity-v2.1"
ESTIMATOR_VERSION = "local-heuristic-v1"
MAX_PROFILE_OCCURRENCES = 8 * 64
MAX_EXCLUSION_DETAILS = 64

@dataclass(frozen=True, slots=True)
class CandidateProfile:
    source_index: int
    provider_id: int
    provider_name: str
    provider_type: str
    model_key: str
    provider_context_window: int
    provider_config_hash: str
    model_config_hash: str
    provider_model_id: int | None = None
    display_name: str | None = None
    model_context_window: int | None = None
    max_output_tokens: int | None = None
    capabilities: tuple[str, ...] = ()
    pool_id: int | None = None
    pool_name: str | None = None
    pool_version: int | None = None
    pool_position: int | None = None
    fallback_depth: int = 0
    unavailable_reason: str | None = None

@dataclass(frozen=True, slots=True)
class TokenEstimate:
    count: int
    source: str
    exact: bool
    includes_overhead: bool

@dataclass(frozen=True, slots=True)
class CandidateBudget:
    profile: CandidateProfile
    candidate_index: int | None
    agent_context_window: int
    context_window: int
    input_tokens: int | None
    input_budget: int | None
    output_reserve: int
    message_overhead: int | None
    safety_margin: int | None
    estimator_source: str | None
    exact: bool | None
    reason: str | None

@dataclass(frozen=True, slots=True)
class InvocationPlan:
    request_fingerprint: str
    allowed_provider_ids: tuple[int, ...]
    output_reserve: int
    eligible: tuple[CandidateBudget, ...]
    excluded: tuple[CandidateBudget, ...]
    excluded_count: int
    planner_version: str = PLANNER_VERSION
    estimator_version: str = ESTIMATOR_VERSION

    @property
    def can_run(self) -> bool:
        return bool(self.eligible)
```

`excluded_count` may exceed `len(excluded)` because up to 512 configured occurrences can include disabled/duplicate entries; retain at most 64 exclusion details. This does not enlarge the 64 unique structurally available candidate limit. `to_public_dict()` adds `excluded_count` and `excluded_truncated` so UI cannot mistake a bounded list for the complete count. These are additive to the shared public projection.

- [ ] Write the failing planner tests below. Their helper construction is self-contained in `test_ai_invocation_planner.py`.

```python
from dataclasses import replace
import json

from pixiv_novel_sync.ai.invocation import TextInvocation, TextMessage
from pixiv_novel_sync.ai.invocation_planner import (
    CandidateProfile, TokenEstimate, estimate_text_invocation, plan_invocation,
)


def request(text="START\nEND", *, scope=None, output=1000):
    return TextInvocation((TextMessage("user", text),), 0.0, 0.9, output, scope)


def profile(index, window, *, model=None, provider_id=None, protocol="openai_compatible"):
    return CandidateProfile(
        source_index=index, provider_id=provider_id or index + 1,
        provider_name="provider-" + str(index), provider_type=protocol,
        model_key=model or "model-" + str(index),
        provider_context_window=1_000_000, model_context_window=window,
        provider_config_hash="a" * 64, model_config_hash="b" * 64,
        provider_model_id=index + 1,
    )


def test_long_input_excludes_short_fallback_without_global_minimum():
    profiles = (profile(0, 1_000_000), profile(1, 128_000))
    plan = plan_invocation(request(), profiles, agent_context_window=1_000_000,
                           estimate=lambda req, p: TokenEstimate(200_000, "test", True, True))
    assert [row.profile.source_index for row in plan.eligible] == [0]
    assert plan.eligible[0].candidate_index == 0
    assert plan.excluded[0].reason == "context_overflow"
    assert plan.excluded[0].candidate_index is None


def test_same_provider_models_use_independent_estimates_not_global_cross_product():
    calls = []
    def estimator(req, p):
        calls.append(p.model_key)
        return TokenEstimate({"a": 90_000, "b": 200_000}[p.model_key], "test", True, True)
    profiles = (profile(0, 100_000, model="a", provider_id=7),
                profile(1, 1_000_000, model="b", provider_id=7))
    plan = plan_invocation(request(), profiles, agent_context_window=1_000_000, estimate=estimator)
    assert calls == ["a", "b"]
    assert [row.input_tokens for row in plan.eligible] == [90_000, 200_000]
    assert len(plan.eligible) == 2


def test_empty_scope_does_not_estimate_or_default_to_all():
    def forbidden(req, p):
        raise AssertionError("out-of-scope candidates must not be estimated")
    plan = plan_invocation(request(scope=()), (profile(0, 1_000_000),),
                           agent_context_window=1_000_000, estimate=forbidden)
    assert not plan.can_run
    assert plan.allowed_provider_ids == ()
    assert plan.excluded[0].reason == "provider_not_allowed"


def test_output_limit_unknown_cap_and_framing_are_distinct():
    profiles = (replace(profile(0, 100_000), max_output_tokens=500), profile(1, 100_000))
    plan = plan_invocation(request(output=1000), profiles, agent_context_window=1_000_000,
                           estimate=lambda req, p: TokenEstimate(100, "test", True, True))
    assert plan.excluded[0].reason == "output_limit"
    assert plan.eligible[0].profile.max_output_tokens is None
    assert plan.eligible[0].message_overhead == 0
    assert plan.eligible[0].safety_margin == 256


def test_bad_external_estimate_falls_back_without_losing_whitespace_or_tail():
    req = request(" " * 4000 + "\u4e2d\U0001f642TAIL")
    p = profile(0, 100_000)
    estimated = estimate_text_invocation(req, p)
    assert estimated.count > 0
    plan = plan_invocation(req, (p,), agent_context_window=1_000_000,
                           estimate=lambda req, p: TokenEstimate(True, "bad", True, True))
    row = plan.eligible[0]
    assert row.exact is False
    assert row.estimator_source == "local_heuristic"
    assert row.safety_margin == max(256, (row.input_tokens + row.message_overhead + 9) // 10)
    assert req.messages[0].content.endswith("TAIL")


def test_public_plan_contains_only_safe_decision_facts():
    plan = plan_invocation(request("PRIVATE-BODY-TAIL"), (profile(0, 100_000),),
                           agent_context_window=1_000_000)
    public = plan.to_public_dict()
    rendered = json.dumps(public)
    assert public["can_run"] is True
    assert public["eligible"][0]["model_key"] == "model-0"
    for secret in ("PRIVATE-BODY-TAIL", "request_fingerprint", "config_hash", "base_url", "api_key"):
        assert secret not in rendered
```

- [ ] Run `python -m pytest tests/test_ai_invocation_planner.py -q`. Initial failure is missing planner behavior, not an environment/provider error.

- [ ] Implement local framing/estimation and independent budgets using the following functions in `invocation_planner.py`. Import `Callable`, `dataclass`, `replace`, `Any`, and the request types from `invocation.py`. Use protocol type, not Provider ID, for framing; call the supplied estimator separately for every distinct model.

```python
def _framed_contents(request: TextInvocation, profile: CandidateProfile) -> tuple[str, ...]:
    if profile.provider_type != "anthropic":
        return tuple(message.content for message in request.messages)
    system = tuple(message.content for message in request.messages if message.role == "system")
    normal = tuple(message.content for message in request.messages if message.role != "system")
    return (("\n\n".join(system),) if system else ()) + normal


def protocol_message_overhead(request: TextInvocation, profile: CandidateProfile) -> int:
    return 4 * len(_framed_contents(request, profile)) + 2


def _heuristic_text_tokens(text: str) -> int:
    cjk = ascii_count = other = 0
    for char in text:
        code = ord(char)
        if (0x3400 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF
                or 0xAC00 <= code <= 0xD7AF or 0x20000 <= code <= 0x3134F):
            cjk += 1
        elif code < 128:
            ascii_count += 1  # Includes whitespace; never silently zero a long blank input.
        else:
            other += 1
    return (2 * cjk + 2) // 3 + (ascii_count + 3) // 4 + other


def estimate_text_invocation(request: TextInvocation, profile: CandidateProfile) -> TokenEstimate:
    return TokenEstimate(sum(_heuristic_text_tokens(text) for text in _framed_contents(request, profile)),
                         "local_heuristic", False, False)


def _valid_estimate(value: Any) -> bool:
    return (isinstance(value, TokenEstimate)
            and isinstance(value.count, int) and not isinstance(value.count, bool) and value.count >= 0
            and isinstance(value.source, str) and 0 < len(value.source) <= 80
            and value.source.isascii() and all(ch.isalnum() or ch in "_:/.-" for ch in value.source)
            and isinstance(value.exact, bool) and isinstance(value.includes_overhead, bool))


def _estimate(request: TextInvocation, profile: CandidateProfile, estimator: TokenEstimator) -> TokenEstimate | None:
    try:
        candidate = estimator(request, profile)
    except Exception:
        candidate = None  # Never expose estimator exception text: it may contain the input.
    if _valid_estimate(candidate):
        return candidate
    try:
        fallback = estimate_text_invocation(request, profile)
    except Exception:
        return None
    return fallback if _valid_estimate(fallback) else None


def _window(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 256 <= value <= 10_000_000:
        raise InvocationValidationError("invalid_configuration", "Context window must be from 256 to 10000000")
    return value


def plan_invocation(request, candidates, *, agent_context_window, required_capabilities=(),
                    estimate=estimate_text_invocation, estimator_version=ESTIMATOR_VERSION):
    agent_window = _window(agent_context_window)
    if len(candidates) > MAX_PROFILE_OCCURRENCES:
        raise InvocationValidationError("invalid_configuration", "Too many configured model occurrences")
    configured = {profile.provider_id for profile in candidates}
    permitted = configured if request.allowed_provider_ids is None else configured.intersection(request.allowed_provider_ids)
    permitted_ids = tuple(sorted(permitted))
    eligible, excluded = [], []
    excluded_count = 0
    seen = set()
    for profile in candidates:
        windows = [agent_window, _window(profile.provider_context_window)]
        if profile.model_context_window is not None:
            windows.append(_window(profile.model_context_window))
        window = min(windows)
        row = CandidateBudget(profile, None, agent_window, window, None, None,
                              request.max_tokens, None, None, None, None, None)
        reason = profile.unavailable_reason
        if reason is None and set(required_capabilities).difference(profile.capabilities):
            reason = "capability_missing"
        identity = (profile.provider_id, profile.model_key)
        if reason is None:
            if identity in seen:
                reason = "duplicate_candidate"
            else:
                seen.add(identity)
                if len(seen) > 64:
                    raise InvocationValidationError("invalid_configuration", "More than 64 resolved candidates")
        if reason is None and profile.provider_id not in permitted:
            reason = "provider_not_allowed"
        if reason is None and profile.max_output_tokens is not None and profile.max_output_tokens < request.max_tokens:
            reason = "output_limit"
        if reason is None:
            estimate_value = _estimate(request, profile, estimate)
            if estimate_value is None:
                reason = "estimation_failed"
            else:
                overhead = 0 if estimate_value.includes_overhead else protocol_message_overhead(request, profile)
                margin = 256 if estimate_value.exact else max(256, (estimate_value.count + overhead + 9) // 10)
                input_budget = window - request.max_tokens - overhead - margin
                row = replace(row, input_tokens=estimate_value.count, input_budget=input_budget,
                              message_overhead=overhead, safety_margin=margin,
                              estimator_source=estimate_value.source, exact=estimate_value.exact)
                if estimate_value.count > input_budget:
                    reason = "context_overflow"
        if reason is None:
            eligible.append(replace(row, candidate_index=len(eligible)))
        else:
            excluded_count += 1
            if len(excluded) < MAX_EXCLUSION_DETAILS:
                excluded.append(replace(row, reason=reason))
    return InvocationPlan(invocation_fingerprint(request, provider_ids=permitted_ids), permitted_ids,
                          request.max_tokens, tuple(eligible), tuple(excluded), excluded_count,
                          PLANNER_VERSION, estimator_version)
```

Do not call `ModelRouter.build_prompt_budget` or `_estimate_with_providers` from this module. Their old maximum-estimate/minimum-window algorithm remains only on the v1 path.

- [ ] Implement a fixed local reason dictionary and the exact public projection below. Never interpolate an exception, message, arbitrary upstream metadata, URL, or key into `reason_message`.

```python
REASON_MESSAGES = {
    "provider_not_allowed": "Provider is outside the allowed scope",
    "provider_disabled": "Provider is disabled",
    "pool_disabled": "Routing group is disabled",
    "member_disabled": "Routing group member is disabled",
    "model_disabled": "Catalog model is disabled",
    "model_unavailable": "Catalog model is unavailable",
    "capability_missing": "Required model capability is missing",
    "output_limit": "Requested output exceeds the model output limit",
    "context_overflow": "Complete input and output reserve exceed the effective context limit",
    "estimation_failed": "Local token estimation failed",
    "duplicate_candidate": "An earlier eligible configuration already identifies this model",
}


def _public_budget(row: CandidateBudget) -> dict[str, Any]:
    p = row.profile
    factors = {"agent": row.agent_context_window, "provider": p.provider_context_window}
    if p.model_context_window is not None:
        factors["model"] = p.model_context_window
    return {
        "source_index": p.source_index, "candidate_index": row.candidate_index,
        "provider_id": p.provider_id, "provider_name": p.provider_name,
        "provider_model_id": p.provider_model_id, "model_key": p.model_key,
        "display_name": p.display_name, "pool_id": p.pool_id, "pool_name": p.pool_name,
        "pool_position": p.pool_position, "fallback_depth": p.fallback_depth,
        "context_window": row.context_window, "agent_context_window": row.agent_context_window,
        "provider_context_window": p.provider_context_window, "model_context_window": p.model_context_window,
        "max_output_tokens": p.max_output_tokens,
        "limiting_factors": [name for name, value in factors.items() if value == row.context_window],
        "input_tokens": row.input_tokens, "input_budget": row.input_budget,
        "output_reserve": row.output_reserve, "message_overhead": row.message_overhead,
        "safety_margin": row.safety_margin, "estimator_source": row.estimator_source,
        "exact": row.exact, "reason": row.reason,
        "reason_message": REASON_MESSAGES[row.reason] if row.reason else None,
    }


# Put this method inside InvocationPlan.
def to_public_dict(self) -> dict[str, Any]:
    return {
        "version": 2, "planner_version": self.planner_version, "estimator_version": self.estimator_version,
        "can_run": self.can_run, "allowed_provider_ids": list(self.allowed_provider_ids),
        "output_reserve": self.output_reserve,
        "eligible": [_public_budget(row) for row in self.eligible],
        "excluded": [_public_budget(row) for row in self.excluded],
        "excluded_count": self.excluded_count, "excluded_truncated": self.excluded_count > len(self.excluded),
        "limits": {"max_candidate_attempts": 16, "max_network_requests": 32,
                   "max_resolved_candidates": 64, "max_pool_nodes": 8},
    }
```

- [ ] Add parameterized tests for: exact count including framing versus excluding framing; Anthropic joining multiple system messages; negative/None/bool/NaN external estimates; required capabilities; disabled-first duplicate followed by enabled occurrence; scope IDs outside the configured range; 65 structurally available unique models; 512 disabled occurrences with 64 retained exclusion details; Agent 16k or Provider 128k remaining the actual bottleneck. Construct the disabled duplicate with `replace(profile(0, 100_000), unavailable_reason="member_disabled")` followed by the same identity at `source_index=1`; assert the second survives with `candidate_index=0`.

- [ ] Add strict private serialization needed by Task 3: `plan_to_payload(plan: InvocationPlan) -> dict[str, Any]` and `plan_from_payload(payload: Mapping[str, Any]) -> InvocationPlan`. `plan_to_payload` uses `asdict`; the decoder reconstructs tuple fields explicitly and rejects unknown keys using `dataclasses.fields` before construction. Apply the following exact validation table at decode and at the candidate-profile boundary (source code must not rely on dataclass annotations to enforce types):

| Value | Required validation |
|---|---|
| provider/model/pool IDs and pool positions/versions | non-bool integer > 0; only the declared nullable fields may be null |
| source_index/fallback_depth | non-bool integer; source 0..511, fallback 0..7 |
| provider_type | exactly openai_compatible / xai / anthropic |
| context windows | 256..10000000, model may be null |
| max_output_tokens/output_reserve | 1..1000000, only model limit may be null |
| provider_name/display_name/pool_name/model_key | safe UTF-8 strings; reuse `validate_text_field` (200/800, 200/800, 100/400 limits) and opaque `normalize_model_key`; never strip model keys |
| provider_config_hash/model_config_hash/request_fingerprint | lowercase `[0-9a-f]{64}` |
| capabilities | tuple of strings normalized by existing `normalize_capabilities`; no arbitrary mappings |
| unavailable_reason/reason | null or a key of REASON_MESSAGES |
| token/overhead/margin fields | null only when unestimated; otherwise non-bool integers >= 0 |
| estimated row | input_budget equals context_window - output_reserve - message_overhead - safety_margin; reason-null rows must fit; exact bool required |
| eligible indices | exactly 0..len(eligible)-1; excluded indices all null; eligible has no reason, excluded has one |
| plan scope | sorted unique positive IDs; every eligible Provider belongs to it |
| details/limits | eligible <= 64, excluded <= 64, excluded_count between len(excluded) and 512; unique source indices across retained decisions |
| version strings | nonempty ASCII safe identifiers, <= 80 characters; do not reject a historical planner identifier during decode, only at execution/resume policy validation |

Use this decode pattern for each nested dataclass instead of accepting arbitrary `**payload`:

```python
def _exact_fields(value, cls):
    if not isinstance(value, Mapping) or set(value) != {field.name for field in fields(cls)}:
        raise InvocationValidationError("invalid_snapshot", "Planning fields are invalid")
    return dict(value)


def plan_to_payload(plan):
    # A stable JSON-shaped object also works when the caller has not round-tripped through SQLite.
    return json.loads(json.dumps(asdict(plan), ensure_ascii=False, allow_nan=False))


def plan_from_payload(payload):
    raw = _exact_fields(payload, InvocationPlan)
    for key in ("eligible", "excluded"):
        if not isinstance(raw[key], list):
            raise InvocationValidationError("invalid_snapshot", "Planning decisions must be arrays")
        decisions = []
        for item in raw[key]:
            decision = _exact_fields(item, CandidateBudget)
            profile = _exact_fields(decision["profile"], CandidateProfile)
            if not isinstance(profile["capabilities"], list):
                raise InvocationValidationError("invalid_snapshot", "Model capabilities must be an array")
            profile["capabilities"] = tuple(profile["capabilities"])
            decision["profile"] = CandidateProfile(**profile)
            decisions.append(CandidateBudget(**decision))
        raw[key] = tuple(decisions)
    if not isinstance(raw["allowed_provider_ids"], list):
        raise InvocationValidationError("invalid_snapshot", "Planning scope must be an array")
    raw["allowed_provider_ids"] = tuple(raw["allowed_provider_ids"])
    plan = InvocationPlan(**raw)
    validate_plan(plan)
    return plan
```

`validate_plan(plan: InvocationPlan) -> None` is the private validator below. Import `normalize_capabilities`, `normalize_model_key`, and `validate_text_field` from the existing model catalog module, and `re` from the standard library. Call `validate_profile` for every raw profile before planning, and `validate_plan` for the assembled result before returning it. Add a test for every category in the table by mutating a JSON-round-tripped valid payload and expecting `InvocationValidationError`; specifically test `True` where an ID/token count is expected and `"messages"` as an extra nested field. This is a strict fixed schema, not a permissive migration parser.

```python
def _require(condition):
    if not condition:
        raise InvocationValidationError("invalid_snapshot", "Planning value is invalid")


def _integer(value, minimum, maximum, *, optional=False):
    if optional and value is None:
        return
    _require(type(value) is int and minimum <= value <= maximum)


def _identifier(value):
    _require(isinstance(value, str) and 0 < len(value) <= 80 and value.isascii()
             and all(ch.isalnum() or ch in "_:/.-" for ch in value))


def validate_profile(profile):
    _require(isinstance(profile, CandidateProfile))
    _integer(profile.source_index, 0, 511)
    _integer(profile.fallback_depth, 0, 7)
    _integer(profile.provider_id, 1, 2**63 - 1)
    for name in ("provider_model_id", "pool_id", "pool_version", "pool_position"):
        _integer(getattr(profile, name), 1, 2**63 - 1, optional=True)
    _require(
        isinstance(profile.provider_type, str)
        and profile.provider_type in {"openai_compatible", "xai", "anthropic"}
    )
    _window(profile.provider_context_window)
    if profile.model_context_window is not None:
        _window(profile.model_context_window)
    _integer(profile.max_output_tokens, 1, 1_000_000, optional=True)
    for value in (profile.provider_config_hash, profile.model_config_hash):
        _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None)
    _require(normalize_model_key(profile.model_key) == profile.model_key)
    for name, codepoints, byte_limit, optional in (
        ("provider_name", 200, 800, False), ("display_name", 200, 800, True),
        ("pool_name", 100, 400, True),
    ):
        value = getattr(profile, name)
        if value is not None or not optional:
            validate_text_field(value, name, codepoints, byte_limit)
    _require(isinstance(profile.capabilities, tuple))
    _require(tuple(normalize_capabilities(profile.capabilities)) == profile.capabilities)
    _require(
        profile.unavailable_reason is None
        or (
            isinstance(profile.unavailable_reason, str)
            and profile.unavailable_reason in REASON_MESSAGES
        )
    )
    if profile.pool_id is None:
        _require(profile.pool_name is None and profile.pool_version is None and profile.pool_position is None)
    else:
        _require(profile.pool_name is not None and profile.pool_version is not None and profile.pool_position is not None)


def validate_plan(plan):
    _require(isinstance(plan, InvocationPlan))
    _require(isinstance(plan.request_fingerprint, str)
             and re.fullmatch(r"[0-9a-f]{64}", plan.request_fingerprint) is not None)
    _identifier(plan.planner_version)
    _identifier(plan.estimator_version)
    _integer(plan.output_reserve, 1, 1_000_000)
    _require(isinstance(plan.allowed_provider_ids, tuple))
    for provider_id in plan.allowed_provider_ids:
        _integer(provider_id, 1, 2**63 - 1)
    _require(tuple(sorted(set(plan.allowed_provider_ids))) == plan.allowed_provider_ids)
    _require(isinstance(plan.eligible, tuple) and len(plan.eligible) <= 64)
    _require(isinstance(plan.excluded, tuple) and len(plan.excluded) <= 64)
    _integer(plan.excluded_count, len(plan.excluded), 512)
    seen_sources = set()
    for eligible, rows in ((True, plan.eligible), (False, plan.excluded)):
        for index, row in enumerate(rows):
            _require(isinstance(row, CandidateBudget))
            validate_profile(row.profile)
            _require(row.profile.source_index not in seen_sources)
            seen_sources.add(row.profile.source_index)
            _window(row.agent_context_window)
            windows = [row.agent_context_window, row.profile.provider_context_window]
            if row.profile.model_context_window is not None:
                windows.append(row.profile.model_context_window)
            _require(type(row.context_window) is int and row.context_window == min(windows))
            _require(type(row.output_reserve) is int and row.output_reserve == plan.output_reserve)
            if eligible:
                _require(type(row.candidate_index) is int and row.candidate_index == index and row.reason is None)
                _require(row.profile.unavailable_reason is None and row.profile.provider_id in plan.allowed_provider_ids)
            else:
                _require(
                    row.candidate_index is None
                    and isinstance(row.reason, str)
                    and row.reason in REASON_MESSAGES
                )

修正（2026-11-02）：枚举字段必须先证明是 `str` 再做集合成员判断；JSON 数组/对象一律得到固定的 `invalid_snapshot`，不能触发 `TypeError`。

            if row.input_tokens is None:
                _require(not eligible and all(value is None for value in (
                    row.input_budget, row.message_overhead, row.safety_margin, row.estimator_source, row.exact)))
                continue
            _integer(row.input_tokens, 0, 2**63 - 1)
            _integer(row.message_overhead, 0, 2**63 - 1)
            _require(type(row.exact) is bool)
            _identifier(row.estimator_source)
            margin = 256 if row.exact else max(256, (row.input_tokens + row.message_overhead + 9) // 10)
            _require(type(row.safety_margin) is int and row.safety_margin == margin)
            _require(type(row.input_budget) is int and row.input_budget == (
                row.context_window - row.output_reserve - row.message_overhead - margin))
            if eligible:
                _require(row.input_tokens <= row.input_budget)
                _require(row.profile.max_output_tokens is None or row.output_reserve <= row.profile.max_output_tokens)
            if row.reason == "context_overflow":
                _require(row.input_tokens > row.input_budget)
```

- [ ] Run `python -m pytest tests/test_ai_invocation_contract.py tests/test_ai_invocation_planner.py -q`, then `python -m compileall -q src/pixiv_novel_sync/ai` and `git diff --check`. Expected: all pass with no Provider/DNS calls.

- [ ] Commit only this task's four files during approved execution: `git add src/pixiv_novel_sync/ai/invocation.py src/pixiv_novel_sync/ai/invocation_planner.py tests/test_ai_invocation_contract.py tests/test_ai_invocation_planner.py`; `git commit -m "feat(ai): add immutable text requests and capacity planner"`.

### Task 2: Model Output Limits and Atomic Catalogue Selection

**Files:**
- Modify: `src/pixiv_novel_sync/ai/model_catalog.py` (`normalize_model_record`, `_build_metadata`, `canonical_model_digest`).
- Modify: `src/pixiv_novel_sync/storage/ai/model_schema.py` (additive catalogue-column migration only).
- Modify: `src/pixiv_novel_sync/storage/ai/catalog.py` (model projection/manual CRUD/discovery upsert/revision/bulk enabled).
- Modify: `src/pixiv_novel_sync/ai/services/admin.py` (manual-field allowlists, probe projection, bulk wrapper).
- Test: `tests/test_ai_model_catalog.py`, `tests/test_ai_model_schema.py`, `tests/test_ai_model_discovery.py`, `tests/test_ai_model_sync.py`, `tests/test_ai_model_api.py`.

**Interfaces:**
- Consumes: existing `ModelCatalogValidationError`, `ModelCatalogConflictError`, `read_transaction`, `transaction`, and the normalization/canonical-digest pipeline.
- Produces: effective `max_output_tokens` plus `discovered_max_output_tokens`/`manual_max_output_tokens` in model projections; the revision/bulk signatures above. Task 3 consumes the effective model limit; Task 6 owns the HTTP route; Tasks 7/8 consume the wrapper and revision.

- [ ] **Step 2.1: Add failing normalization, manual override and missing/null tests.** Append to `tests/test_ai_model_catalog.py`, using its existing `db` fixture and `seed_provider` helper:

```python
@pytest.mark.parametrize("bad", [True, False, "", "8192", 0, -1, 1000001])
def test_max_output_tokens_rejects_invalid_discovered_values(bad):
    with pytest.raises(ModelCatalogValidationError):
        normalize_model_record({"id": "m", "max_output_tokens": bad})


def test_output_limit_normalization_digest_and_manual_override(db):
    provider_id = seed_provider(db)
    small = normalize_model_record({"id": "m", "max_output_tokens": 4096})
    large = normalize_model_record({"id": "m", "max_output_tokens": 8192})
    assert small["max_output_tokens"] == 4096
    assert canonical_model_digest([small]) != canonical_model_digest([large])
    db.upsert_discovered_models(provider_id, [small], generation=1)
    model_id = db.list_ai_provider_models(provider_id)["items"][0]["id"]
    db.update_ai_provider_model(model_id, {"manual_max_output_tokens": 20000})
    db.upsert_discovered_models(provider_id, [large], generation=2)
    assert db.get_ai_provider_model(model_id)["max_output_tokens"] == 20000
    db.update_ai_provider_model(model_id, {"enabled": False})
    assert db.get_ai_provider_model(model_id)["manual_max_output_tokens"] == 20000
    db.update_ai_provider_model(model_id, {"manual_max_output_tokens": None})
    assert db.get_ai_provider_model(model_id)["max_output_tokens"] == 8192
    assert normalize_model_record({"id": "unknown"})["max_output_tokens"] is None


@pytest.mark.parametrize("bad", [True, "", "4096", 0, -1, 1000001])
def test_manual_output_limit_bad_patch_has_no_effect(db, bad):
    provider_id = seed_provider(db)
    model_id = db.create_ai_provider_model({
        "provider_id": provider_id, "model_key": "m",
        "manual_max_output_tokens": 4096,
    })
    with pytest.raises(ModelCatalogValidationError):
        db.update_ai_provider_model(model_id, {"manual_max_output_tokens": bad})
    assert db.get_ai_provider_model(model_id)["manual_max_output_tokens"] == 4096
```

Run `python -m pytest -q tests/test_ai_model_catalog.py -k output`. Expected RED: missing returned field or invalid values accepted.

- [ ] **Step 2.2: Add the two nullable columns without triggering the old table-rebuild path.** Add the same two definitions to fresh table creation and an idempotent helper called from `_migrate_model_routing_schema_in_transaction` after catalogue creation. Do not add these columns to the legacy `_JOB_ROUTING_COLUMNS` rebuild discriminator.

```python
def _ensure_model_output_columns(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "ai_provider_models")
    for name in ("discovered_max_output_tokens", "manual_max_output_tokens"):
        if name not in columns:
            conn.execute(
                f"ALTER TABLE ai_provider_models ADD COLUMN {name} INTEGER "
                f"CHECK ({name} IS NULL OR "
                f"(typeof({name}) = 'integer' AND {name} BETWEEN 1 AND 1000000))"
            )
```

In `tests/test_ai_model_schema.py`, use `make_old_ai_database`, initialize twice, assert the imported legacy model has both limits NULL, Agent 7 still has `system_prompt='legacy prompt'` and `max_tokens=2048`, Provider 3 still has `api_key_encrypted='ciphertext'`, and `PRAGMA foreign_key_check` returns no rows. Retain the existing failed-migration rollback test and add a failure injected immediately after this helper to prove both columns roll back together.

- [ ] **Step 2.3: Implement one nullable-limit validator and thread the field through both discovered-record shapes.** Define this public pure helper in `ai/model_catalog.py`; catalogue manual CRUD imports it rather than copying validation.

```python
def normalize_max_output_tokens(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelCatalogValidationError("max_output_tokens must be an integer or null")
    if not 1 <= value <= 1_000_000:
        raise ModelCatalogValidationError("max_output_tokens must be between 1 and 1000000")
    return value
```

Add `max_output_tokens: None` to the normalized record; normalize only the explicitly named upstream field, never aliases inferred from context size. `_build_metadata` accepts the normalized limit and includes it only when raw `max_output_tokens` is non-null. `canonical_model_digest` includes the normalized limit. `_normalize_discovered_record` validates its `model_key` shape and rebuilds `max_output_tokens` when it exists in canonical metadata, preserving the existing equality check.

Use these concrete catalogue substitutions:

```python
# Projection: manual None means fall back; 0 is invalid, not a fallback sentinel.
effective_output = item.get("manual_max_output_tokens")
if effective_output is None:
    effective_output = item.get("discovered_max_output_tokens")
output_fields = {
    "max_output_tokens": effective_output,
    "manual_max_output_tokens": item.get("manual_max_output_tokens"),
    "discovered_max_output_tokens": item.get("discovered_max_output_tokens"),
}

# Manual PATCH, before the existing dynamic UPDATE is executed.
if "manual_max_output_tokens" in patch:
    fields.extend(["manual_max_output_tokens = ?", "manual = 1"])
    params.append(normalize_max_output_tokens(patch["manual_max_output_tokens"]))
```

Manual INSERT adds `manual_max_output_tokens`; removing the manual part of a discovered model clears it. Discovery INSERT/UPDATE adds only `discovered_max_output_tokens`, leaving `manual_*` and enabled unchanged. Add `manual_max_output_tokens` to both admin manual allowlists and `max_output_tokens` to probe projection. Update exact-shape tests to include the new nullable field instead of weakening them to subset assertions.

Run `python -m pytest -q tests/test_ai_model_catalog.py tests/test_ai_model_schema.py tests/test_ai_model_discovery.py tests/test_ai_model_sync.py`. Expected GREEN, including canonical metadata rejection and unchanged manual/disabled state.

- [ ] **Step 2.4: Add failing revision and all-or-nothing selection tests.** The tests below use the existing catalogue fixtures. `updated` counts actual value changes, not the number of submitted IDs; an empty change list is a valid revision-checked no-op.

```python
def test_catalog_revision_is_full_and_bulk_enabled_is_atomic(db):
    provider_id = seed_provider(db)
    ids = [db.create_ai_provider_model({
        "provider_id": provider_id, "model_key": key,
    }) for key in ("alpha", "beta")]
    initial = db.list_ai_provider_models(provider_id)
    revision = initial["catalog_revision"]
    assert len(revision) == 64
    assert db.list_ai_provider_models(provider_id, search="alpha")["catalog_revision"] == revision
    assert db.list_ai_provider_models(provider_id, enabled_only=True)["catalog_revision"] == revision
    with pytest.raises(ModelCatalogValidationError):
        db.update_ai_provider_models_enabled(provider_id, revision, [
            {"id": ids[0], "enabled": False}, {"id": 999999, "enabled": False},
        ])
    assert all(db.get_ai_provider_model(i)["enabled"] for i in ids)
    result = db.update_ai_provider_models_enabled(provider_id, revision, [
        {"id": i, "enabled": False} for i in ids
    ])
    assert result["updated"] == 2
    assert result["catalog_revision"] != revision
    with pytest.raises(ModelCatalogConflictError):
        db.update_ai_provider_models_enabled(provider_id, revision, [{"id": ids[0], "enabled": True}])
    assert not any(db.get_ai_provider_model(i)["enabled"] for i in ids)


@pytest.mark.parametrize("changes", [
    [{"id": True, "enabled": False}],
    [{"id": 1, "enabled": 0}],
    [{"id": 1, "enabled": False}, {"id": 1, "enabled": True}],
    [{"id": n, "enabled": False} for n in range(1, 5002)],
])
def test_bulk_enabled_rejects_invalid_changes_without_writes(db, changes):
    provider_id = seed_provider(db)
    revision = db.get_ai_provider_catalog_revision(provider_id)
    with pytest.raises(ModelCatalogValidationError):
        db.update_ai_provider_models_enabled(provider_id, revision, changes)
    assert db.get_ai_provider_catalog_revision(provider_id) == revision
```

Also create two Providers and submit the second Provider's model ID with the first one's revision; require validation failure and no changed rows. Test 5000 unique owned IDs successfully in one transaction, using chunks of at most 900 SQL parameters for ownership lookup. Use two connections/threads and a `Barrier` to submit conflicting changes against one revision; exactly one state-changing request succeeds. Inject a SQLite `BEFORE UPDATE` trigger that raises on the second model and assert the first change is rolled back.

Run `python -m pytest -q tests/test_ai_model_catalog.py -k 'revision or bulk_enabled'`. Expected RED: missing revision/methods.

- [ ] **Step 2.5: Implement a complete-catalogue digest and transactional enabled update.** Add `hashlib`/`re` imports and this bounded projection. Hash one row at a time so a large catalogue does not require constructing another complete JSON list. Timestamps, secrets and arbitrary discovered metadata are not revision inputs; all identity, availability, editable and effective routing fields are.

```python
_CATALOG_REVISION_FIELDS = (
    "id", "model_key", "discovered", "manual", "discovered_available", "enabled",
    "discovered_display_name", "manual_display_name",
    "discovered_capabilities_json", "manual_capabilities_json",
    "discovered_context_window", "manual_context_window",
    "discovered_max_output_tokens", "manual_max_output_tokens",
)


def _catalog_revision(self, conn: sqlite3.Connection, provider_id: int) -> str:
    provider = conn.execute("SELECT enabled FROM ai_providers WHERE id = ?", (provider_id,)).fetchone()
    if provider is None:
        raise ModelCatalogValidationError("Provider does not exist")
    digest = hashlib.sha256()
    header = {"version": 1, "provider_id": provider_id, "provider_enabled": bool(provider[0])}
    digest.update(json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    columns = ", ".join(_CATALOG_REVISION_FIELDS)
    for row in conn.execute(
        f"SELECT {columns} FROM ai_provider_models WHERE provider_id = ? ORDER BY id", (provider_id,)
    ):
        item = dict(row)
        for key in ("discovered_capabilities_json", "manual_capabilities_json"):
            item[key] = json.loads(item[key]) if item[key] is not None else None
        digest.update(b"\n")
        digest.update(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()


def get_ai_provider_catalog_revision(self, provider_id: int) -> str:
    with self.read_transaction() as conn:
        return self._catalog_revision(conn, provider_id)
```

Calculate `catalog_revision` inside `list_ai_provider_models`' existing read transaction, not after its exit. Validate the submitted shape before taking the write lock:

```python
if not isinstance(expected_catalog_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_catalog_revision):
    raise ModelCatalogValidationError("catalog_revision must be SHA-256")
if isinstance(changes, (str, bytes)) or not isinstance(changes, Sequence) or len(changes) > 5000:
    raise ModelCatalogValidationError("changes must contain at most 5000 items")
normalized_changes: dict[int, bool] = {}
for change in changes:
    if not isinstance(change, Mapping) or set(change) != {"id", "enabled"}:
        raise ModelCatalogValidationError("each change requires only id and enabled")
    model_id, enabled = change["id"], change["enabled"]
    if isinstance(model_id, bool) or not isinstance(model_id, int) or model_id <= 0:
        raise ModelCatalogValidationError("model id must be a positive integer")
    if type(enabled) is not bool or model_id in normalized_changes:
        raise ModelCatalogValidationError("enabled must be boolean and model ids unique")
    normalized_changes[model_id] = enabled
```

Then execute the revision comparison, ownership validation and writes in one transaction:

```python
with self.transaction() as conn:
    if self._catalog_revision(conn, provider_id) != expected_catalog_revision:
        raise ModelCatalogConflictError("Model catalogue changed; reload and confirm selection")
    current: dict[int, bool] = {}
    ids = list(normalized_changes)
    for offset in range(0, len(ids), 900):
        batch = ids[offset:offset + 900]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"SELECT id, enabled FROM ai_provider_models WHERE provider_id = ? AND id IN ({placeholders})",
            [provider_id, *batch],
        ):
            current[int(row["id"])] = bool(row["enabled"])
    if set(current) != set(ids):
        raise ModelCatalogValidationError("Every model must belong to this Provider")
    updates = [(int(value), model_id, provider_id) for model_id, value in normalized_changes.items()
               if current[model_id] != value]
    conn.executemany(
        "UPDATE ai_provider_models SET enabled = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND provider_id = ?", updates,
    )
    return {"provider_id": provider_id, "updated": len(updates),
            "catalog_revision": self._catalog_revision(conn, provider_id)}
```

The admin wrapper rejects unknown payload keys, requires the Provider via `_require_provider_row`, forwards `catalog_revision/changes`, maps `ModelCatalogConflictError` to `AIConflictError`, and maps validation errors to `AIServiceError`. It does not probe, sync or bind Agents. Root Task 6 adds the HTTP route and its 400/409 tests.

Run `python -m pytest -q tests/test_ai_model_catalog.py tests/test_ai_model_schema.py tests/test_ai_model_sync.py tests/test_ai_model_api.py tests/test_ai_model_discovery.py`; then `git diff --check`. Expected GREEN. The eventual implementation commit contains only this task's catalogue/admin files and tests; no commit is made during this planning turn.

### Task 3: Version-Two Snapshots And Runtime Eligibility

**Interfaces carried into an isolated task brief:**
- Task 1 provides `TextInvocation`, `CandidateProfile`, `CandidateBudget`, `TokenEstimate`, and `InvocationPlan` in the modules named there.
- Task 3 produces `PlannedCandidateSnapshot(candidates, snapshot_hash, agent_config_hash, binding_version, plan, version=2)` in `ai/invocation_snapshot.py`, plus its strict payload encode/decode helpers.
- `RouteResult` preserves the original positional fields `(job_id, output_text, candidate_snapshot_hash, attempts, finish_state)` and appends these defaulted fields in order:

```python
snapshot_version: int = 1
actual_provider_id: int | None = None
actual_model_key: str | None = None
finish_reason: str | None = None
validation_status: Literal["not_requested", "passed", "failed"] | None = None
usage_summary: dict[str, Any] | None = None
job_status: Literal["succeeded", "failed", "partial", "cancelled"] | None = None
```

Task 5 fills `job_status`, validation and usage after service finalization; `finish_state` stays the Router transport enum.


**Dependencies:** Task 1 and Task 2's effective `max_output_tokens` catalog read field. This task is independently tested with fake Providers. Task 4 later adds usage callbacks, and Task 5 adds audit/retention DB enforcement and the public business facade; do not write either task's persistence policy here.

**Files owned by Task 3:**

- Create: `src/pixiv_novel_sync/ai/invocation_candidates.py` -- read-transaction configuration projection only.
- Create: `src/pixiv_novel_sync/ai/invocation_snapshot.py` -- v2 frozen envelope and strict codec.
- Modify: `src/pixiv_novel_sync/ai/model_router.py` -- appended request/result fields, plan/freeze/validate dispatch, fresh per-attempt request copies, memory-aware terminal result seam. Leave the existing execution loop structure intact.
- Modify: `src/pixiv_novel_sync/ai/services/core.py` -- optional v2 branch only in `_start_route_job`, `_snapshot_payload`, `_persist_prompt_budget`, `_stream_route`, and appended `RouteJobContext` fields.
- Modify: `src/pixiv_novel_sync/ai/services/admin.py` -- only append the private `upgrade_legacy=True` loader switch; keep old calls' default behavior.
- Modify: `src/pixiv_novel_sync/storage/ai/core.py` -- add the small compare-and-swap ciphertext-upgrade helper; Task 5 owns the later usage/retention changes.
- Create: `tests/test_ai_invocation_snapshots.py`.
- Create: `tests/test_ai_invocation_runtime.py`.
- Modify: `tests/test_ai_model_router.py` and `tests/test_ai_model_router_integration.py` only for appended request/result/context field assertions and additional compatibility guards. Do not weaken v1 snapshot field/hash assertions.

#### 3A. Preserve v1 Hashes Before Integrating Anything

- [ ] Add a golden v1 payload/hash regression to `test_ai_invocation_snapshots.py`. Copy the explicit field list below, not `asdict()` of a new dataclass, to ensure the test detects future key additions.

```python
import hashlib
import json
from dataclasses import asdict

from pixiv_novel_sync.ai.model_router import CandidateSnapshot, ModelRouter


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def test_v1_unversioned_payload_keeps_exact_shape_and_hash():
    payload = {"agent_config_hash": "a" * 64, "binding_version": 1, "candidates": [{
        "provider_id": 1, "provider_name": "old", "model_key": "manual/old ",
        "provider_model_id": None, "pool_id": None, "pool_name": None,
        "pool_version": None, "pool_position": None, "provider_config_hash": "b" * 64,
        "capabilities": [], "context_window": 16_000, "fallback_depth": 0, "candidate_index": 0,
    }]}
    digest = canonical_hash(payload)
    restored = ModelRouter.candidate_snapshot_from_payload(payload, digest)
    assert type(restored) is CandidateSnapshot
    assert restored.snapshot_hash == digest
    encoded = {"agent_config_hash": restored.agent_config_hash,
               "binding_version": restored.binding_version,
               "candidates": [asdict(item) for item in restored.candidates]}
    assert json.loads(json.dumps(encoded)) == payload
    assert canonical_hash(encoded) == digest
    assert "version" not in encoded and "max_output_tokens" not in encoded["candidates"][0]
```

- [ ] Run `python -m pytest tests/test_ai_invocation_snapshots.py tests/test_ai_model_router.py::test_route_contract_field_order_and_immutability -q`; both pass against the baseline. This establishes the invariant before new v2 tests go red.

#### 3B. Safe Configuration Projection With Known-Disabled Fixed Models

- [ ] Implement `collect_candidate_profiles(db: Database, agent: AIAgentConfig) -> tuple[CandidateProfile, ...]` in `invocation_candidates.py`, called only while the Router holds `db.read_transaction()`. It must not call `_load_provider_config`, `create_provider`, the secret manager, `validate_base_url(resolve=True)`, or any tokenizer instance requiring a key.

Use the following collection algorithm verbatim in behavior:

1. Fixed: obtain the provider row, choose `agent.model` if not None else `provider.default_model`, and normalize the opaque key. Query the complete catalog with `routable_only=False`, not `_matching_catalog_model` (which hides disabled rows). An existing disabled row has `unavailable_reason="model_disabled"`; an existing row not manual and not discovered_available has `"model_unavailable"`. A key absent from the catalog is retained with null catalog ID/caps/window/output limit. Disabled Provider wins with `"provider_disabled"`.
2. Pool: walk `fallback_pool_id` in existing order, reject cycles or a ninth node, and enumerate each pool's members in position order. Keep safe profiles even for disabled pool/member/model so the planner can explain them. Reason precedence is Provider disabled, pool disabled, member disabled, model disabled, model unavailable. Follow a disabled pool's fallback rather than stopping there. Missing referenced pool/provider/model is `InvocationValidationError("invalid_configuration", local fixed message)`.
3. Do not deduplicate before disabled/capability filtering: a disabled early occurrence must not hide a usable later occurrence. Task 1's planner performs first-structurally-eligible deduplication and the 64-candidate check; raw source_index can range 0..511.
4. For provider digest use the unchanged `provider_model_sync_config_hash(row)`; it may hash the existing ciphertext internally but must not pass that ciphertext into any profile/plan/response. For model digest use `model_execution_config_hash(model_key, model_row)` below, excluding names/timestamps and including the exact execution facts.

```python
def model_execution_config_hash(model_key: str, model: Mapping[str, Any] | None) -> str:
    fields = {
        "model_key": model_key,
        "registered": model is not None,
        "id": model.get("id") if model else None,
        "enabled": bool(model["enabled"]) if model else True,
        "manual": bool(model.get("manual")) if model else False,
        "discovered_available": bool(model.get("discovered_available")) if model else False,
        "capabilities": sorted(model.get("capabilities") or []) if model else [],
        "context_window": model.get("context_window") if model else None,
        "max_output_tokens": model.get("max_output_tokens") if model else None,
    }
    raw = json.dumps(fields, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
```

Construct each profile with the explicit public/config-safe values; no row-spreading:

```python
def make_profile(source_index, provider, model_key, model, *, pool=None, member=None, depth=0):
    reason = None
    if not bool(provider["enabled"]):
        reason = "provider_disabled"
    elif pool is not None and not bool(pool["enabled"]):
        reason = "pool_disabled"
    elif member is not None and not bool(member["enabled"]):
        reason = "member_disabled"
    elif model is not None and not bool(model["enabled"]):
        reason = "model_disabled"
    elif model is not None and not (model.get("manual") or model.get("discovered_available")):
        reason = "model_unavailable"
    return CandidateProfile(
        source_index=source_index, provider_id=int(provider["id"]), provider_name=provider["name"],
        provider_type=provider["provider_type"], model_key=model_key,
        provider_context_window=provider["context_window"],
        provider_config_hash=provider_model_sync_config_hash(provider),
        model_config_hash=model_execution_config_hash(model_key, model),
        provider_model_id=int(model["id"]) if model else None,
        display_name=model.get("display_name") if model else None,
        model_context_window=model.get("context_window") if model else None,
        max_output_tokens=model.get("max_output_tokens") if model else None,
        capabilities=tuple(model.get("capabilities") or ()) if model else (),
        pool_id=int(pool["id"]) if pool else None, pool_name=pool["name"] if pool else None,
        pool_version=int(pool["version"]) if pool else None,
        pool_position=int(member["position"]) if member else None,
        fallback_depth=depth, unavailable_reason=reason,
    )
```

- [ ] Add runtime tests using an isolated `Database(tmp_path / "invocation.db")` and synthetic provider ciphertext. Reuse the existing seed functions/fixtures by placing tests that need them in `test_ai_model_router.py`, or copy their small fixture into the new runtime file; never import real configuration. Required concrete assertions:

```python
def test_planning_never_loads_credentials_or_provider(router, fixed_agent, monkeypatch):
    from pixiv_novel_sync.ai.invocation import normalize_text_invocation
    monkeypatch.setattr(router, "_load_provider_config", lambda *a: (_ for _ in ()).throw(AssertionError("decrypt")))
    monkeypatch.setattr(router, "_get_provider", lambda *a: (_ for _ in ()).throw(AssertionError("HTTP")))
    req = normalize_text_invocation({"messages": [{"role": "user", "content": "test"}]}, agent=fixed_agent)
    assert router.plan_invocation(fixed_agent, req).can_run


def test_known_disabled_fixed_is_rejected_but_never_registered_key_survives(db, router, fixed_agent):
    from pixiv_novel_sync.ai.invocation import normalize_text_invocation
    req = normalize_text_invocation({"messages": [{"role": "user", "content": "test"}]}, agent=fixed_agent)
    assert router.plan_invocation(fixed_agent, req).can_run
    model_id = db.create_ai_provider_model({"provider_id": fixed_agent.provider_id,
                                            "model_key": fixed_agent.model, "enabled": False})
    plan = router.plan_invocation(fixed_agent, req)
    assert not plan.can_run
    assert plan.excluded[0].profile.provider_model_id == model_id
    assert plan.excluded[0].reason == "model_disabled"
```

- [ ] Run those two nodes explicitly before and after integration. Initial red: `ModelRouter.plan_invocation` is absent. After implementing the projection and Router methods below, both pass without decrypted credentials.

#### 3C. v2 Envelope And Strict Version Dispatch

- [ ] Implement `invocation_snapshot.py` with the separate DTO already specified. Its top-level serialized fields are exactly `version`, `agent_config_hash`, `binding_version`, `candidates`, `plan`. Do not serialize its `snapshot_hash` field into itself. Use these helpers:

```python
def _digest(payload):
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > 256 * 1024:
        raise InvocationValidationError("invalid_snapshot", "Planning snapshot exceeds 256 KiB")
    return hashlib.sha256(encoded).hexdigest()


def planned_snapshot_payload(snapshot):
    payload = {"version": 2, "agent_config_hash": snapshot.agent_config_hash,
               "binding_version": snapshot.binding_version,
               "candidates": [asdict(candidate) for candidate in snapshot.candidates],
               "plan": plan_to_payload(snapshot.plan)}
    return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))


def freeze_plan(agent_config_hash: str, binding_version: int, plan: InvocationPlan) -> PlannedCandidateSnapshot:
    from .model_router import ModelCandidate
    candidates = tuple(ModelCandidate(
        provider_id=row.profile.provider_id, provider_name=row.profile.provider_name,
        model_key=row.profile.model_key, provider_model_id=row.profile.provider_model_id,
        pool_id=row.profile.pool_id, pool_name=row.profile.pool_name,
        pool_version=row.profile.pool_version, pool_position=row.profile.pool_position,
        provider_config_hash=row.profile.provider_config_hash, capabilities=row.profile.capabilities,
        context_window=row.context_window, fallback_depth=row.profile.fallback_depth,
        candidate_index=row.candidate_index,
    ) for row in plan.eligible)
    draft = PlannedCandidateSnapshot(candidates, "", agent_config_hash, binding_version, plan)
    return replace(draft, snapshot_hash=_digest(planned_snapshot_payload(draft)))


def planned_snapshot_from_payload(payload, expected_hash):
    from .model_router import ModelRouter
    expected_fields = {"version", "agent_config_hash", "binding_version", "candidates", "plan"}
    if not isinstance(payload, Mapping) or set(payload) != expected_fields or type(payload["version"]) is not int or payload["version"] != 2:
        raise InvocationValidationError("invalid_snapshot", "Version-two snapshot fields are invalid")
    if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None or _digest(payload) != expected_hash:
        raise InvocationValidationError("invalid_snapshot", "Version-two snapshot digest mismatch")
    v1_fields = {key: payload[key] for key in ("agent_config_hash", "binding_version", "candidates")}
    # Reuse the existing strict candidate field decoder without changing its v1 format.
    legacy = ModelRouter.candidate_snapshot_from_payload(v1_fields, _digest(v1_fields))
    plan = plan_from_payload(payload["plan"])
    rebuilt = freeze_plan(legacy.agent_config_hash, legacy.binding_version, plan)
    if planned_snapshot_payload(rebuilt) != dict(payload) or rebuilt.snapshot_hash != expected_hash:
        raise InvocationValidationError("invalid_snapshot", "Snapshot candidates and planning decisions disagree")
    return rebuilt
```

The legacy decoder accepts an empty list at decode time; its old execution validator still rejects it. v2 permits an empty eligible list to persist an explanatory plan and finish without a network attempt.

- [ ] In `ModelRouter.candidate_snapshot_from_payload`, insert one guarded dispatch before the old `try` block. Leave every original v1 field set and hash branch untouched:

```python
if isinstance(payload, Mapping) and "version" in payload:
    try:
        return planned_snapshot_from_payload(payload, snapshot_hash)
    except InvocationValidationError as exc:
        raise ModelRouteConflictError(str(exc)) from exc
```

- [ ] Add v2 tests that freeze a pure plan, JSON round-trip it, decode it through `ModelRouter.candidate_snapshot_from_payload`, and assert exact payload/hash equality. Mutate each of: output reserve, allowed scope, fingerprint, effective window, eligible index, candidate/model key, model config hash, version=True, extra nested `messages`; recomputing only the outer hash must not bypass shape/formula/candidate consistency checks. A changed valid configuration hash alone is structurally decodable but must fail resume against the real DB, not be mistaken for a cryptographic signature.

- [ ] Run `python -m pytest tests/test_ai_invocation_snapshots.py tests/test_ai_model_router.py -q`. The original v1 hash/immutability tests must remain passing.

#### 3D. Router Planning, Resume, And Execution Binding

- [ ] Append keyword-only estimator injection to `ModelRouter.__init__` without changing its three existing positional arguments:

```text
*, token_estimator: TokenEstimator = estimate_text_invocation,
estimator_version: str = ESTIMATOR_VERSION,
load_invocation_provider_config: Callable[[Any, int], AIProviderConfig] | None = None,
prepare_invocation_providers: Callable[[tuple[int, ...]], None] | None = None,
```

Set `self._token_estimator`, `self._estimator_version`, `self._load_invocation_provider_config`, and `self._prepare_invocation_providers`; none is a Provider instance. v2 execution requires the explicitly supplied read-only loader (the service supplies it); a missing loader is a configuration error before any send, not permission to reuse a mutating legacy loader. Existing v1 callers keep the original loader and constructor positional arguments. Add the following methods; credential preparation is outside the short read transaction used for the final freeze:

```python
def _plan_invocation(self, db, agent, request):
    profiles = collect_candidate_profiles(db, agent)
    return plan_invocation(request, profiles, agent_context_window=agent.context_window,
                           required_capabilities=agent.required_capabilities,
                           estimate=self._token_estimator, estimator_version=self._estimator_version)


def plan_invocation(self, agent, request):
    db = self._db_factory()
    try:
        with db.read_transaction():
            return self._plan_invocation(db, agent, request)
    finally:
        db.close()


def resolve_invocation(self, agent, request):
    initial = self.plan_invocation(agent, request)
    if initial.eligible and self._prepare_invocation_providers is not None:
        provider_ids = tuple(dict.fromkeys(row.profile.provider_id for row in initial.eligible))
        self._prepare_invocation_providers(provider_ids)
    db = self._db_factory()
    try:
        with db.read_transaction():
            plan = self._plan_invocation(db, agent, request)
            return freeze_plan(self._agent_config_hash(agent), int(agent.binding_version), plan)
    finally:
        db.close()
```

`collect_candidate_profiles` rejects `agent.enabled=False` and invalid binding type/required missing binding fields with local configuration errors; it does not synthesize another Agent. Fixed model keys absent from the catalog retain unknown model limits; registered disabled keys do not use the unknown-model fallback.

- [ ] Keep ciphertext upgrades from invalidating a frozen plan. The existing `AIAdminMixin._load_provider_config` may rewrite v1 ciphertext after decrypting, and `provider_model_sync_config_hash` includes that ciphertext. Add the private keyword argument `upgrade_legacy: bool = True`, and guard its existing upgrade branch with `if upgrade_legacy and api_key and self.secret_manager.is_legacy_ciphertext(stored_cipher):`. Default v1/admin behavior remains unchanged, except that an upgrade may not overwrite a concurrently replaced Key.

Add this storage helper and replace only the automatic upgrade write with `db.upgrade_ai_provider_ciphertext(provider_id, stored_cipher, self.secret_manager.encrypt(api_key))`. Retain the existing best-effort exception handling for upgrade writes. A false CAS result leaves the user's new configuration untouched; the final v2 snapshot and its read-only loader read the current row again.

```python
def upgrade_ai_provider_ciphertext(self, provider_id: int, expected: str, replacement: str) -> bool:
    with self.transaction() as conn:
        cursor = conn.execute(
            "UPDATE ai_providers SET api_key_encrypted = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND api_key_encrypted = ?",
            (replacement, provider_id, expected),
        )
        return cursor.rowcount == 1
```

Add these private core callbacks, importing `AISecretError` from `..crypto` and `AIProviderError` from `..providers`. Only the runtime resolver calls preparation; preflight never calls it. Do not wrap the KDF/preparation loop in a long DB write transaction, and do not temporarily change a shared loader or Provider flag:

```python
def _prepare_invocation_providers(self, provider_ids: tuple[int, ...]) -> None:
    db = self._db()
    try:
        for provider_id in provider_ids:
            row = db.get_ai_provider(provider_id, include_secret=True)
            if row is None or not self.secret_manager.is_legacy_ciphertext(row.get("api_key_encrypted")):
                continue
            try:
                self._load_provider_config(db, provider_id)
            except (AISecretError, AIServiceError):
                # Execution reports configuration failure as a Provider-scoped attempt.
                continue
    finally:
        db.close()


def _load_invocation_provider_config(self, db, provider_id: int) -> AIProviderConfig:
    try:
        return self._load_provider_config(db, provider_id, upgrade_legacy=False)
    except (AISecretError, AIServiceError):
        raise AIProviderError("Provider credentials are unavailable", category="configuration", scope="provider") from None
```

The service's existing `ModelRouter(...)` construction supplies these two new callbacks explicitly. Every v2 execution-time load in Router uses `_load_invocation_provider_config`; the legacy loader remains only in v1/admin paths. A preparation write failure may leave old ciphertext, but the read-only execution loader must not retry that write after freezing. Real configuration changes still fail existing snapshot checks; do not weaken hashes to hide this problem.

Use a real synthetic legacy ciphertext in the runtime test fixture, not a made-up Fernet prefix:

```python
@pytest.fixture
def legacy_key_provider(db, monkeypatch):
    from pixiv_novel_sync.ai.crypto import AISecretManager

    monkeypatch.setenv("PIXIV_NOVEL_SYNC_AI_SECRET_KEY", "synthetic-master")
    manager = AISecretManager()
    ciphertext = manager._fernet_v1().encrypt(b"synthetic-provider-key").decode("ascii")
    provider_id = db.create_ai_provider({
        "name": "legacy-test", "provider_type": "openai_compatible",
        "base_url": "https://provider.example.test/v1", "api_key_encrypted": ciphertext,
        "default_model": "synthetic-model", "context_window": 128000, "enabled": True,
    })
    agent_id = db.create_ai_agent({
        "name": "legacy-key-agent", "task_type": "general", "binding_type": "fixed",
        "provider_id": provider_id, "model": "synthetic-model", "system_prompt": "synthetic",
        "context_window": 16000,
    })
    return manager, provider_id, agent_id, ciphertext
```

Add these three regressions to the Task 3 real-DB/fake-Provider suite; run `python -m pytest tests/test_ai_invocation_runtime.py -k credential -q` before and after the fix:

| Test | Required concrete assertions |
|---|---|
| `test_v2_credential_upgrade_precedes_frozen_hash` | pure preflight leaves fixture ciphertext unchanged; before the fake's first guarded send the stored value starts with `v2$`, its `provider_model_sync_config_hash` equals the frozen candidate hash, and the hash remains unchanged after successful completion |
| `test_v2_credential_upgrade_failure_does_not_retry_after_freeze` | monkeypatch `Database.upgrade_ai_provider_ciphertext` to raise; the write is attempted once during preparation; v2 still decrypts/uses the valid old key successfully, and stored/frozen hashes stay unchanged during execution |
| `test_v2_credential_failure_allows_scoped_fallback` | first Provider has ciphertext encrypted with another synthetic master, second has valid ciphertext; first gets a Provider-scope configuration attempt and zero HTTP, second succeeds; a scope-excluded Provider is never decrypted/upgraded |

Add a fourth storage race regression: save a new synthetic ciphertext between the legacy row read and `upgrade_ai_provider_ciphertext`; the helper returns false and a fresh row still contains the user's new ciphertext, not the stale decrypted key re-encrypted by the preparer.

New v2 fake-Router fixtures explicitly pass a read-only loader; do not alter legacy three-argument fixtures that test v1 behavior. These tests use `_start_route_job`/`_stream_route` until Task 5 adds `invoke_text`, and must not depend on an unimplemented public facade.

- [ ] Append to `RouteRequest` (after all existing fields) `invocation: TextInvocation | None = None` and `retention_policy: Literal["metadata_only", "keyword_compat"] | None = None`. Widen `candidate_snapshot` to `CandidateSnapshot | PlannedCandidateSnapshot`; this annotation does not add v1 snapshot keys. Append the result fields from the shared contract and update only the corresponding field-order expectations in existing tests.

- [ ] Add `_validate_invocation_request(request: RouteRequest) -> None`, called at execution entry before heartbeat/attempt allocation and once immediately before each new Provider call. It handles v2 only; v1 remains the old branch.

```python
def _validate_invocation_request(self, request):
    snapshot = request.candidate_snapshot
    if not isinstance(snapshot, PlannedCandidateSnapshot):
        return
    invocation = request.invocation
    if invocation is None:
        raise ModelRouteConflictError("Version-two route requires its immutable text request")
    if (snapshot.plan.planner_version != PLANNER_VERSION
            or snapshot.plan.estimator_version != self._estimator_version):
        raise ModelRouteConflictError("Invocation planning rules changed")
    if (request.messages != invocation_messages(invocation)
            or request.temperature != invocation.temperature
            or request.top_p != invocation.top_p
            or request.max_tokens != invocation.max_tokens):
        raise ModelRouteConflictError("Route request changed after planning")
    effective = snapshot.plan.allowed_provider_ids
    if invocation.allowed_provider_ids is not None and not set(effective).issubset(invocation.allowed_provider_ids):
        raise ModelRouteConflictError("Route Provider scope changed")
    if invocation_fingerprint(invocation, provider_ids=effective) != snapshot.plan.request_fingerprint:
        raise ModelRouteConflictError("Route input fingerprint changed")
```

Do not pass `request.messages` directly to a Provider for v2: create a new `invocation_messages(request.invocation)` list for every candidate attempt. This protects the canonical tuple even if a fake/adapter mutates its received list. V1 continues using its current call shape, avoiding a silent new invocation requirement for old callers.

- [ ] Dispatch `_candidate_fits`: v1 executes its original estimator/overhead code; v2 uses its already-validated frozen budget and does not recompute a shared Provider estimate.

```python
if isinstance(request.candidate_snapshot, PlannedCandidateSnapshot):
    self._validate_invocation_request(request)
    row = request.candidate_snapshot.plan.eligible[candidate.candidate_index]
    return (row.profile.provider_id == candidate.provider_id
            and row.profile.model_key == candidate.model_key
            and row.reason is None and row.input_tokens is not None
            and row.input_budget is not None and row.input_tokens <= row.input_budget)
```

- [ ] Keep existing Agent/Provider/pool validation at initial execution. Do not reload model capacity or substitute candidates during a running v2 job. Extend `validate_resume_snapshot(agent, snapshot, resume_candidate_index, *, invocation: TextInvocation | None = None)` for low-level v2 recovery: verify strict v2 envelope; validate existing Agent/Provider/pool checks using the snapshot's common fields without calling the v1 `_snapshot_hash`; compare planner/estimator versions; require matching reconstructed invocation; re-read each remaining model by exact provider/key and compare `model_execution_config_hash`. Display-name/timestamp-only changes do not conflict, but registered/unregistered transition, enabled/capabilities/context/output changes do. Never build a new candidate list or renumber old indices during this validation.

Extract the existing common current-configuration checks from `_validate_saved_snapshot` into `_validate_snapshot_configuration(db, agent, snapshot, resume_candidate_index)` without altering their conditions. The v1 wrapper still runs its old hash check then this helper; the v2 wrapper runs `planned_snapshot_from_payload(planned_snapshot_payload(snapshot), snapshot.snapshot_hash)` then the helper and model/request checks. This is a small extraction, not a rewrite of the Router.

- [ ] For an empty v2 candidate tuple, persist its plan and finish failed-before-output through `_finish_job_failure(..., category="no_eligible_candidate", message="No model satisfies the complete request")` before heartbeat/attempt creation. Preflight still returns 200/can_run=false in Task 6; this runtime branch is an auditable failed invocation, not a preflight exception.

- [ ] Add these concrete regression nodes to `test_ai_invocation_runtime.py` (or the existing fixture-bearing Router test module) and verify the stated assertions:

| Node | Setup and exact assertion |
|---|---|
| `test_v2_long_request_uses_only_large_eligible_model` | Agent/Provider caps 1M, catalog 1M and 128k, exact synthetic estimate 200k; registry network list contains only the large model; one attempt and one guard claim |
| `test_v2_no_candidate_records_plan_and_zero_attempts` | empty allowed scope; persisted snapshot has version 2 and rejection reason; job failed; attempt/network counts both 0 |
| `test_v2_mutated_messages_fail_before_guard` | freeze request A, pass request.messages with a tail added; `ModelRouteConflictError`, registry.calls empty, no allocation |
| `test_v2_changed_parameter_or_scope_fails_before_guard` | change max_tokens, temperature, or canonical explicit scope after freezing; same zero-send conflict |
| `test_v2_provider_mutation_does_not_pollute_fallback` | first fake mutates its received list then raises before a delta; second captures the original full text and original tail marker |
| `test_v2_runtime_does_not_reestimate_or_reload_model_limits` | freeze plan, monkeypatch estimator to raise and update catalog context; current invocation uses frozen facts; a separate resume check rejects changed model config |
| `test_v2_resume_rejects_model_or_estimator_change` | update model output/context/capability/enable or use router with changed estimator_version; low-level resume conflicts without Provider creation |
| `test_v1_resume_still_uses_legacy_hash_and_budget` | pass original unversioned payload through old checks; no v2 field required; existing minimum-window test remains unchanged |

Use existing `FakeProviderRegistry`, `seed_provider`, `seed_pool_setup`, `agent_from_row`, and the tests' DB fixture only in the file where they are defined. For tests in a new file, copy those small relevant fixture definitions rather than relying on importing pytest's internal module loading state.

- [ ] Run `python -m pytest tests/test_ai_invocation_runtime.py tests/test_ai_invocation_snapshots.py tests/test_ai_model_router.py -q`. Expected all pass with temporary DB/fake Providers only.

#### 3E. Narrow Core Handoff And In-Memory Partial Result Seam

- [ ] Append to `RouteJobContext` (leaving existing positional fields first): `invocation: TextInvocation | None = None` and `retention_policy: Literal["metadata_only", "keyword_compat"] | None = None`. Widen its snapshot and budget types to `CandidateSnapshot | PlannedCandidateSnapshot` and `PromptBudget | InvocationPlan`. Both budget types have `output_reserve`; v2 does not synthesize a shared minimum input budget.

- [ ] Append these keyword-only parameters to the existing `_start_route_job` signature:

```python
invocation: TextInvocation | None = None,
retention_policy: Literal["metadata_only", "keyword_compat"] | None = None,
```

For new non-resume work, branch at the current `resolve_candidates`/`build_prompt_budget` block:

```python
if invocation is not None:
    if messages != invocation_messages(invocation) or max_tokens != invocation.max_tokens:
        raise AIServiceError("Text invocation and legacy request arguments disagree")
    candidate_snapshot = self.model_router.resolve_invocation(agent, invocation)
    prompt_budget = candidate_snapshot.plan
else:
    candidate_snapshot = self.model_router.resolve_candidates(agent, stage="main", snapshot=snapshot)
    prompt_budget = self.model_router.build_prompt_budget(agent, candidate_snapshot, budget_messages, max_tokens)
```

Leave the existing v1 resume branch unchanged. A v2 `_ROUTE_RESUME_CONTEXT` uses the new low-level `validate_resume_snapshot(..., invocation=invocation)` and its persisted frozen plan; never call `build_prompt_budget` on v2. No public endpoint starts such a business continuation in this phase.

Return the context with the two appended fields. `_snapshot_payload` uses `planned_snapshot_payload` only for `PlannedCandidateSnapshot`, otherwise the exact current three-field v1 payload. `_persist_prompt_budget` accepts the union and keeps its existing one-time owner-CAS write; `asdict(InvocationPlan)` is a safe bounded private budget record.

Task 5 constructs the initial server-safe audit marker before calling `_start_route_job` and finalizes the input_json audit fingerprint/scope after plan creation, still before any send. This task must not place runtime messages into `job_input_data`, turn on a new public v1 route, or make the keyword exception the default for all old callers.

- [ ] In `_stream_route`, preserve the old branch when context.invocation is None. For v2, reject nonmatching messages or explicit parameter overrides rather than lowering/changing the planned request. Set `RouteRequest.invocation` and retention from context, and use its canonical sampling values rather than the Agent defaults:

```python
invocation = context.invocation
if invocation is not None:
    if (messages != invocation_messages(invocation)
            or (temperature is not None and temperature != invocation.temperature)
            or (top_p is not None and top_p != invocation.top_p)
            or (max_tokens is not None and max_tokens != invocation.max_tokens)):
        raise AIConflictError("Text request changed after planning")
    route_messages = invocation_messages(invocation)
    output_reserve = invocation.max_tokens
    route_temperature, route_top_p = invocation.temperature, invocation.top_p
else:
    route_messages = [dict(message) for message in messages]
    route_temperature = context.agent.temperature if temperature is None else temperature
    route_top_p = context.agent.top_p if top_p is None else top_p
```

Use `route_temperature`, `route_top_p`, `route_messages`, and `output_reserve` in the existing `RouteRequest` constructor, plus `invocation=invocation` and `retention_policy=context.retention_policy`. Preserve the old v1 output-reserve upper-bound check.

- [ ] Add `_owned_terminal_result` below, plus `memory_output: str | None = None` as an optional keyword-only argument on `_terminal_race_result`. When `memory_output` is provided, use it instead of `job.output_text`; when omitted, retain the old DB-owned race behavior. An owned terminal result may use memory only after the corresponding `finish_ai_job_cas` returned True. This prevents a competing owner/late generator from publishing discarded text.

```python
def _owned_terminal_result(self, db, request, *, cas_won: bool, memory_output: str):
    if cas_won and request.retention_policy == "metadata_only":
        return self._terminal_race_result(db, request, memory_output=memory_output)
    return self._terminal_race_result(db, request)
```

At the existing main-after-delta partial branch, replace the final two statements with this surgical change; Task 5's DB-level policy is responsible for suppressing the submitted output and upstream error in persistence:

```python
cas_won = db.finish_ai_job_cas(
    request.job_id, request.owner_token, "partial",
    output_text="".join(output_parts), error_message=str(error),
)
return self._owned_terminal_result(db, request, cas_won=cas_won, memory_output="".join(output_parts))
```

Apply the same owned-memory pattern to cancellation and the existing route-budget/generic-exception branch when that branch has already accumulated main output. Do not change first-delta pinning, attempt status, or the condition that validation/internal failures are not `partial`. Successful results already return `output_parts` directly and should continue doing so. `GeneratorExit` still closes the iterator and records cancellation; it must not emit an extra delta/result to a disconnected caller.

- [ ] Add a focused seam test without waiting for Task 5's DB implementation: wrap `db.finish_ai_job_cas` to deliberately drop `output_text` only for a metadata_only test job, then make a fake Provider yield `"PRIVATE-PARTIAL"` and fail. Assert `result.output_text == "PRIVATE-PARTIAL"`, DB output remains null/empty, `finish_state == "partial"`, and no fallback Provider is called. In a second test make the CAS lose to an already terminal job and assert the late in-memory text is not returned. Task 5 adds the full DB/API/privacy matrix later.

- [ ] Fill result metadata without a new result envelope:

```python
attempts = self._attempts(db, request.job_id)
actual = next((item for item in reversed(attempts)
               if item.get("stage") == request.stage
               and (item.get("status") == "succeeded" or item.get("output_started"))), None)
return RouteResult(
    job_id=request.job_id, output_text=output_text,
    candidate_snapshot_hash=request.candidate_snapshot.snapshot_hash,
    attempts=attempts, finish_state=finish_state,
    snapshot_version=2 if isinstance(request.candidate_snapshot, PlannedCandidateSnapshot) else 1,
    actual_provider_id=actual.get("provider_id") if actual else None,
    actual_model_key=actual.get("model_key") if actual else None,
    finish_reason=actual.get("finish_reason") if actual else None,
)
```

- [ ] Extend core integration tests: new v2 context persists version 2/plan before any provider call; canonical sampling overrides are not replaced with Agent defaults; empty allowed scope never sends; changed messages/parameters fail before guard; old `_start_route_job` tests still observe `FakeModelRouter.resolve_candidates` and `build_prompt_budget` for invocation=None; unchanged main continue remains 409/zero new jobs. Do not replace the old FakeModelRouter with a v2-only fake and thereby stop exercising v1 compatibility.

- [ ] Run deterministic final gates for Task 3:

```powershell
python -m pytest tests/test_ai_invocation_contract.py tests/test_ai_invocation_planner.py tests/test_ai_invocation_snapshots.py tests/test_ai_invocation_runtime.py tests/test_ai_model_router.py tests/test_ai_model_router_integration.py tests/test_ai_agent_candidates.py tests/test_ai_agent_bindings.py tests/test_ai_job_routing_storage.py -q
python -m compileall -q src/pixiv_novel_sync/ai
git diff --check
```

Expected: all pass. No live-provider test or real stored novel is part of the gate.

- [ ] Commit only Task 3 files after its tests pass: `git add src/pixiv_novel_sync/ai/invocation_candidates.py src/pixiv_novel_sync/ai/invocation_snapshot.py src/pixiv_novel_sync/ai/model_router.py src/pixiv_novel_sync/ai/services/core.py src/pixiv_novel_sync/ai/services/admin.py src/pixiv_novel_sync/storage/ai/core.py tests/test_ai_invocation_snapshots.py tests/test_ai_invocation_runtime.py tests/test_ai_model_router.py tests/test_ai_model_router_integration.py`; `git commit -m "feat(ai): integrate immutable capacity-aware route plans"`.

### Task 4: Protocol Usage Parsing and Per-Send Observations

**Files:**
- Create: `src/pixiv_novel_sync/ai/usage.py` (normalized values, cumulative merge and summary; no database or HTTP).
- Create: `src/pixiv_novel_sync/ai/provider_usage.py` (per-request observation lifecycle; no database).
- Modify: `src/pixiv_novel_sync/ai/providers.py` (`_before_network_request`, base/Chat/Anthropic generation signatures and all four send loops).
- Test: `tests/test_ai_provider_completion.py`, `tests/test_ai_providers_fallback.py`.
- Create: `tests/test_ai_usage.py`.

**Interfaces:**
- Depends on Task 3's unchanged route lifecycle and completion semantics. This task introduces optional callbacks without coupling Provider adapters to job storage; Task 5 supplies the durable guard/observer.
- Produces `ProviderUsage`, `normalize_openai_usage`, `normalize_anthropic_usage`, `merge_usage_snapshot`, and `summarize_usage` in `ai/usage.py`.
- Produces `RequestUsageObserver(protocol, request_ordinal, on_usage)` with `observe(raw_usage, *, final=False)` and `finish(*, terminal, missing_reason=None)` in `ai/provider_usage.py`.
- Every `stream_generate` and protocol `_non_stream_generate` takes optional keyword-only `on_usage`; its existing positional parameters and `request_guard`/`is_cancelled` names remain valid. A legacy guard returning None still works and emits no ordinal callback.

- [ ] **Step 4.1: Add failing pure normalization tests, including cache and cumulative cases.** Create `tests/test_ai_usage.py` with these tests:

```python
import pytest

from pixiv_novel_sync.ai.usage import (
    ProviderUsage, merge_usage_snapshot,
    normalize_openai_usage, normalize_anthropic_usage,
)


def test_chat_usage_includes_cache_and_reasoning_without_adding_them_twice():
    usage = normalize_openai_usage({
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 80},
        "completion_tokens_details": {"reasoning_tokens": 5},
    })
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (100, 20, 120)
    assert usage.cached_input_tokens == 80
    assert usage.reasoning_output_tokens == 5


def test_anthropic_input_adds_only_reported_cache_components():
    usage = normalize_anthropic_usage({
        "input_tokens": 12, "cache_creation_input_tokens": 8,
        "cache_read_input_tokens": 80, "output_tokens": 20,
    })
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (100, 20, 120)
    assert usage.cached_input_tokens == 80
    assert usage.cache_creation_input_tokens == 8


def test_usage_distinguishes_missing_from_explicit_zero_and_replay_is_idempotent():
    unknown = ProviderUsage().to_dict()
    zero = ProviderUsage(input_tokens=0, output_tokens=0, total_tokens=0,
                         complete=True, missing_reason=None).to_dict()
    assert unknown["input_tokens"] is None
    assert zero["input_tokens"] == 0
    first = ProviderUsage(input_tokens=10, output_tokens=3, total_tokens=13,
                          missing_reason="partial_report").to_dict()
    final = ProviderUsage(input_tokens=10, output_tokens=7, total_tokens=17,
                          complete=True, missing_reason=None).to_dict()
    assert merge_usage_snapshot(first, final) == final
    assert merge_usage_snapshot(final, final) == final
    assert merge_usage_snapshot(final, unknown) == final


@pytest.mark.parametrize("bad", [True, False, -1, "12", 1.5])
def test_invalid_counter_is_rejected_not_persisted_as_zero(bad):
    with pytest.raises(ValueError):
        normalize_openai_usage({"prompt_tokens": bad})
    with pytest.raises(ValueError):
        normalize_anthropic_usage({"output_tokens": bad})
```

Run `python -m pytest -q tests/test_ai_usage.py`. Expected RED: missing module/types.

- [ ] **Step 4.2: Implement the bounded, protocol-neutral value and pure normalizers.** Use the following value schema; no upstream free-form text is retained in it.

```python
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
    "cache_creation_input_tokens", "reasoning_output_tokens",
)
MISSING_REASONS = {None, "pending", "not_reported", "partial_report", "invalid_usage", "interrupted"}


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    complete: bool = False
    missing_reason: str | None = "pending"

    def __post_init__(self) -> None:
        for field in TOKEN_FIELDS:
            value = getattr(self, field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError("usage counter must be a non-negative integer or null")
        if type(self.complete) is not bool or self.missing_reason not in MISSING_REASONS:
            raise ValueError("invalid usage completeness")
        if self.complete and (self.missing_reason is not None or any(
            getattr(self, field) is None for field in ("input_tokens", "output_tokens", "total_tokens")
        )):
            raise ValueError("complete usage requires all totals")
        for part, total in ((self.cached_input_tokens, self.input_tokens),
                            (self.cache_creation_input_tokens, self.input_tokens),
                            (self.reasoning_output_tokens, self.output_tokens)):
            if part is not None and total is not None and part > total:
                raise ValueError("usage subdivision exceeds inclusive total")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _counter(raw: Mapping[str, Any], key: str) -> int | None:
    value = raw.get(key)
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError("invalid usage counter")
    return value


def _details(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("invalid usage details")
    return value


def normalize_openai_usage(raw: Mapping[str, Any]) -> ProviderUsage:
    if not isinstance(raw, Mapping):
        raise ValueError("usage must be an object")
    inp, out = _counter(raw, "prompt_tokens"), _counter(raw, "completion_tokens")
    total = _counter(raw, "total_tokens")
    if inp is not None and out is not None:
        if total is not None and total != inp + out:
            raise ValueError("inconsistent usage totals")
        total = inp + out
    return ProviderUsage(
        input_tokens=inp, output_tokens=out, total_tokens=total,
        cached_input_tokens=_counter(_details(raw, "prompt_tokens_details"), "cached_tokens"),
        reasoning_output_tokens=_counter(_details(raw, "completion_tokens_details"), "reasoning_tokens"),
        missing_reason="partial_report",
    )


def normalize_anthropic_usage(raw: Mapping[str, Any]) -> ProviderUsage:
    if not isinstance(raw, Mapping):
        raise ValueError("usage must be an object")
    base, out = _counter(raw, "input_tokens"), _counter(raw, "output_tokens")
    read = _counter(raw, "cache_read_input_tokens")
    creation = _counter(raw, "cache_creation_input_tokens")
    # Absent optional cache subdivisions are not displayed as reported zero.
    inp = None if base is None else base + (read or 0) + (creation or 0)
    return ProviderUsage(
        input_tokens=inp, output_tokens=out,
        total_tokens=None if inp is None or out is None else inp + out,
        cached_input_tokens=read, cache_creation_input_tokens=creation,
        missing_reason="partial_report",
    )
```

`merge_usage_snapshot(previous, incoming)` accepts exactly the eight schema keys (not arbitrary raw JSON), validates both via `ProviderUsage`, preserves known counters when incoming is null, and never sums counters from the same ordinal. Cumulative counters must not move backwards: retain the previous value for stale/replayed smaller counters. Recompute total from merged input/output when both are available. Once a complete final snapshot is stored, incomplete replay cannot downgrade it. Optional subdivisions remain subdivisions, not additions to total.

```python
def merge_usage_snapshot(previous: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    old = ProviderUsage(**dict(previous))
    new = ProviderUsage(**dict(incoming))
    values: dict[str, Any] = {}
    for key in TOKEN_FIELDS:
        known = [value for value in (getattr(old, key), getattr(new, key)) if value is not None]
        values[key] = max(known) if known else None
    if values["input_tokens"] is not None and values["output_tokens"] is not None:
        values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
    values["complete"] = old.complete or new.complete
    values["missing_reason"] = None if values["complete"] else new.missing_reason
    return ProviderUsage(**values).to_dict()
```

Add tests for inconsistent Chat total, malformed details, missing input with known output, cache-only reports, and no fabricated reasoning/cache values. Add the rule that when a subdivision exceeds its corresponding known inclusive total it is invalid usage, rather than silently displaying contradictory accounting.

- [ ] **Step 4.3: Add failing real-adapter tests for ordinals, trailing usage-only SSE frames and empty-stream fallback.** Append to `tests/test_ai_provider_completion.py`, using its existing `make_config`, `FakeResponse`, `attach_responses` and `MESSAGES` helpers:

```python
def test_chat_usage_only_frame_after_finish_reason_keeps_original_request_ordinal(monkeypatch):
    provider = OpenAICompatibleProvider(make_config("openai_compatible"))
    response = FakeResponse(lines=[
        'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}',
        'data: [DONE]',
    ])
    attach_responses(monkeypatch, provider, [response])
    seen = []
    chunks = list(provider.stream_generate(
        MESSAGES, "m", 0.0, 0.9, 50,
        request_guard=lambda: 7,
        on_usage=lambda ordinal, usage: seen.append((ordinal, dict(usage))),
    ))
    assert "".join(chunk.text for chunk in chunks if chunk.type == "delta") == "ok"
    assert chunks[-1].data == {"finish_reason": "stop"}
    assert seen[-1][0] == 7
    assert seen[-1][1]["input_tokens"] == 10
    assert seen[-1][1]["complete"] is True
    assert response.closed


def test_empty_stream_fallback_has_two_independent_usage_records(monkeypatch):
    provider = OpenAICompatibleProvider(make_config("openai_compatible"))
    attach_responses(monkeypatch, provider, [
        FakeResponse(lines=[
            'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":0,"total_tokens":10}}',
            'data: [DONE]',
        ]),
        FakeResponse(payload={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }),
    ])
    ordinals = iter([1, 2])
    latest = {}
    list(provider.stream_generate(
        MESSAGES, "m", 0.0, 0.9, 50,
        request_guard=lambda: next(ordinals),
        on_usage=lambda ordinal, usage: latest.__setitem__(ordinal, dict(usage)),
    ))
    assert set(latest) == {1, 2}
    assert latest[1]["output_tokens"] == 0
    assert latest[2]["output_tokens"] == 2
    assert sum(item["input_tokens"] for item in latest.values()) == 20
```

Add an Anthropic fixture sequence containing `message_start.usage` (base input 12, creation 8, read 80, initial output 1), `message_delta.usage` output 5 then output 7, and `message_stop`. Assert the final normalized counts are 100/7/107, not 100/13/113; repeated final events do not add counts. Repeat usage extraction for non-stream Chat and Anthropic responses and xAI's inherited Chat adapter. Reuse existing HTTP-503 retry/cancellation fixtures, checking every new actual send receives a fresh guard ordinal.

Run `python -m pytest -q tests/test_ai_provider_completion.py -k 'usage or ordinal'`. Expected RED: optional callback not accepted or usage ignored.

- [ ] **Step 4.4: Implement request observation and attach it to every send loop.** `_before_network_request` now returns the guard value while preserving cancellation-before-allocation:

```python
def _before_network_request(
    request_guard: Callable[[], int | None] | None,
    is_cancelled: Callable[[], bool] | None,
) -> int | None:
    _check_cancelled(is_cancelled)
    return request_guard() if request_guard is not None else None
```

`RequestUsageObserver` maintains only whitelisted raw usage fields in memory. For Anthropic, merge `message_start` input/cache fields with later `message_delta` output fields before normalization; do not overwrite the start snapshot with an output-only object. `observe` marks whether a final-usage event has occurred, emits incomplete cumulative snapshots, and catches counter/shape validation failures as the fixed `invalid_usage` reason without logging the raw value. A final valid terminal marker plus complete totals is required before `complete=True`; a message-start output count alone is not a final report. `finish` is idempotent and emits fixed reasons for interrupted/not-reported/partial/invalid observations. With no ordinal or callback it remains a local no-op for persistence.

Implement the observer in `ai/provider_usage.py` (imports: `Mapping`, `Callable`, `Any`, `replace`, and the four usage helpers from `ai/usage.py`):

```python
class RequestUsageObserver:
    def __init__(self, protocol: str, request_ordinal: int | None,
                 on_usage: Callable[[int, Mapping[str, Any]], None] | None):
        self.protocol = protocol
        self.ordinal = request_ordinal
        self.callback = on_usage
        self.raw: dict[str, Any] = {}
        self.usage = ProviderUsage()
        self.final_seen = False
        self.invalid = False
        self.finished = False

    def _emit(self) -> None:
        if self.callback is not None and self.ordinal is not None:
            self.callback(self.ordinal, self.usage.to_dict())

    def observe(self, raw_usage: Any, *, final: bool = False) -> None:
        if raw_usage is None or self.finished:
            return
        fields = ({"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"}
                  if self.protocol == "anthropic" else
                  {"prompt_tokens", "completion_tokens", "total_tokens", "prompt_tokens_details", "completion_tokens_details"})
        try:
            if not isinstance(raw_usage, Mapping):
                raise ValueError("usage must be an object")
            candidate = {**self.raw, **{key: value for key, value in raw_usage.items() if key in fields}}
            normalizer = normalize_anthropic_usage if self.protocol == "anthropic" else normalize_openai_usage
            normalized = normalizer(candidate)
            self.usage = ProviderUsage(**merge_usage_snapshot(self.usage.to_dict(), normalized.to_dict()))
            self.raw = candidate
            self.final_seen = self.final_seen or final
        except (TypeError, ValueError):
            self.invalid = True
            self.usage = replace(self.usage, complete=False, missing_reason="invalid_usage")
        self._emit()

    def finish(self, *, terminal: bool, missing_reason: str | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        totals_present = all(getattr(self.usage, name) is not None
                             for name in ("input_tokens", "output_tokens", "total_tokens"))
        complete = terminal and self.final_seen and totals_present and not self.invalid
        if complete:
            reason = None
        elif self.invalid:
            reason = "invalid_usage"
        elif missing_reason is not None:
            reason = missing_reason
        elif not self.raw:
            reason = "not_reported"
        else:
            reason = "partial_report"
        self.usage = replace(self.usage, complete=complete, missing_reason=reason)
        self._emit()
```

At each existing Chat stream/non-stream and Anthropic stream/non-stream `_post` site, allocate the ordinal and create this request-local observer:

```python
request_ordinal = _before_network_request(request_guard, is_cancelled)
usage = RequestUsageObserver(protocol="openai_compatible", request_ordinal=request_ordinal, on_usage=on_usage)
```

Insert `usage.observe(event.get("usage"), final=True)` after Chat event JSON/Mapping validation and before its empty `choices` early continue. Insert `usage.finish(terminal=True)` before `[DONE]` completion and before the empty-stream `yield from` fallback. Wrap each request attempt in `finally: usage.finish(terminal=False, missing_reason="interrupted")` so HTTP errors, cancellation and generator close keep an incomplete observation; the explicit earlier terminal finish prevents a successful observation from being downgraded. Use protocol `anthropic` for that adapter. No changes to `_post`/DNS pinning or the existing response context managers are needed.

For Anthropic, `message_start` calls `observe(message.get("usage"), final=False)`, `message_delta` calls `observe(event.get("usage"), final=True)`, and `message_stop` finishes before completion/fallback. Non-stream adapters observe `data.get("usage")` after JSON decode and finish before yielding content/completion, even when the completion reason is length/content-filter. A response that reports valid final usage can be an unsuccessful generation; usage completeness is not generation success.

Propagate `on_usage` through every `yield from _non_stream_generate` path. Finish the old request's observation before reserving a fallback ordinal. Keep request-local objects inside the retry loop; never reuse the previous request's usage state. Do not mutate caller messages/payload during accounting, change completion markers, count metadata as a delta, add speculative protocol fields, or automatically retry solely to obtain usage. Servers that do not report usage remain honestly unknown.

If the observer's database callback later raises, propagate it so the Router stops the route; do not swallow it and send another unrecorded request. No new requests after partial body emission, cancellation or guard failure. Retain all existing DNS pinning, redirection rejection, bounded reads, per-request resolution and retry-delay checks.

Run `python -m pytest -q tests/test_ai_usage.py tests/test_ai_provider_completion.py tests/test_ai_providers_fallback.py tests/test_ai_security_hardening.py tests/test_ai_service_provider_cache.py`; then `python -m compileall -q src` and `git diff --check`. Expected GREEN. This task is independently tested with integer-returning fake guards; SQLite binding belongs to Task 5.

### Task 5: Durable Usage, Validation State, Metadata Retention and Keyword Compatibility

**Files:**
- Modify: `src/pixiv_novel_sync/storage/ai/model_schema.py` (additive `validation_status` and bounded `usage_json` columns/constraints/index-free migration).
- Modify: `src/pixiv_novel_sync/storage/ai/core.py` (safe audit envelope, request reservations/usage writes, usage summary, retention-enforced terminal writes and public projections).
- Modify: `src/pixiv_novel_sync/ai/model_router.py` (pass attempt index to guard, on-usage callback, call safe terminal methods, retain output only in memory for metadata-only routes; Task 3 owns the result/plan code).
- Modify: `src/pixiv_novel_sync/ai/services/core.py` (server-built metadata audit marker, `invoke_text`, private keyword compatibility helper, validation/status replacement, usage summary).
- Modify: `src/pixiv_novel_sync/ai/services/keyword_clean.py` (select only private `keyword_compat`, preserve current parser/output contract and degradation).
- Modify: `src/pixiv_novel_sync/storage/tasks.py` (show validation and bounded usage summary in existing AI log projection without error body/prompt).
- Test: `tests/test_ai_job_routing_storage.py`, `tests/test_ai_model_schema.py`, `tests/test_ai_model_router.py`, `tests/test_ai_model_router_integration.py`, `tests/test_ai_service_facade.py`, `tests/test_keyword_clean.py`, `tests/test_ai_task_log_attempts.py`.

**Interfaces:**
- Consumes Task 3 v2 plan/RouteJobContext/RouteResult and Task 4 `ProviderUsage`/`on_usage(ordinal, mapping)` callbacks.
- Produces the storage methods in the cross-task interface, plus `AIWritingService.invoke_text` and private `_invoke_keyword_clean`/`_build_metadata_audit` helpers. The public method accepts only `agent_id`, payload, cancellation/deadline and output/progress callbacks; retention and validators are not caller parameters.
- No new business task type, parent/book table, replay handler, global budget, or shared sync slot is introduced.

- [ ] **Step 5.1: Write failing schema/storage tests before migration.** Extend `tests/test_ai_model_schema.py` and `tests/test_ai_job_routing_storage.py` with these concrete checks:

```python
def test_new_columns_migrate_and_old_jobs_project_unknown(db):
    db.conn.execute("INSERT INTO ai_jobs(job_id, task_type, input_json) VALUES ('old', 'general', '{}')")
    db.conn.commit()
    db.init_schema()
    columns = {row[1] for row in db.conn.execute("PRAGMA table_info(ai_jobs)")}
    attempt_columns = {row[1] for row in db.conn.execute("PRAGMA table_info(ai_job_model_attempts)")}
    assert "validation_status" in columns and "usage_json" in attempt_columns
    assert db.conn.execute("SELECT validation_status FROM ai_jobs WHERE job_id='old'").fetchone()[0] is None
    assert db.get_ai_job("old")["validation_status"] == "unknown"


def test_metadata_only_terminal_and_error_writes_never_persist_text(db):
    owner = "owner"
    db.create_ai_job("safe", "general", 1, {
        "audit_version": 1,
        "business_type": "test", "input_chars": 3,
        "message_count": 1, "input_fingerprint": "a" * 64,
    }, owner_token=owner, route_deadline_at="2099-01-01 00:00:00", retention_policy="metadata_only")
    attempt = db.allocate_ai_model_attempt("safe", owner, valid_attempt_data())
    assert db.mark_ai_job_output_started("safe", attempt, owner, 0)
    assert db.finish_ai_job_cas(
        "safe", owner, "partial", output_text="秘密半截",
        error_message="upstream body has secret", output_json={"raw": "秘密"},
    )
    row = db.conn.execute(
        "SELECT input_json, output_text, output_json, error_message FROM ai_jobs WHERE job_id='safe'"
    ).fetchone()
    assert "秘密" not in json.dumps(dict(row), ensure_ascii=False)
    assert row["output_text"] is None and row["output_json"] is None
    assert "upstream body" not in (row["error_message"] or "")


def test_metadata_only_attempt_error_is_safe_and_partial_uses_output_started(db):
    db.create_ai_job("attempt-safe", "general", 1, {"input_chars": 0, "message_count": 0},
                     owner_token="owner", retention_policy="metadata_only")
    attempt = db.allocate_ai_model_attempt("attempt-safe", "owner", valid_attempt_data())
    assert db.finish_ai_model_attempt(
        "attempt-safe", attempt, "owner", "partial", output_started=True,
        error_message="raw provider body secret",
    )
    row = db.conn.execute(
        "SELECT error_message, status, output_started FROM ai_job_model_attempts "
        "WHERE job_id='attempt-safe' AND attempt_index=?", (attempt,)
    ).fetchone()
    assert "raw provider body" not in (row["error_message"] or "")
    assert row["status"] == "partial" and row["output_started"] == 1


def test_request_ordinal_reservation_and_usage_are_owner_scoped_idempotent(db):
    create_owned_job(db, "usage-job")
    attempt = db.allocate_ai_model_attempt("usage-job", "owner", valid_attempt_data())
    assert db.claim_ai_job_network_request("usage-job", "owner", attempt) == 1
    assert db.claim_ai_job_network_request("usage-job", "owner", attempt) == 2
    assert db.record_ai_job_request_usage("usage-job", "owner", attempt, 1, {
        "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
        "complete": True, "missing_reason": None,
    })
    assert db.record_ai_job_request_usage("usage-job", "owner", attempt, 1, {
        "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
        "complete": True, "missing_reason": None,
    })
    assert db.record_ai_job_request_usage("usage-job", "owner", attempt, 2, {
        "input_tokens": None, "output_tokens": None, "total_tokens": None,
        "complete": False, "missing_reason": "not_reported",
    })
    row = db.conn.execute("SELECT usage_json FROM ai_job_model_attempts WHERE job_id='usage-job' AND attempt_index=?", (attempt,)).fetchone()
    usage = json.loads(row["usage_json"])
    assert len(usage["requests"]) == 2 and usage["requests"][0]["input_tokens"] == 10
    assert db.record_ai_job_request_usage("usage-job", "wrong", attempt, 1, {
        "input_tokens": 99, "output_tokens": 99, "total_tokens": 198,
        "complete": True, "missing_reason": None,
    }) is False


def test_usage_payload_rejects_over_limit_and_invalid_counters(db):
    create_owned_job(db, "usage-limit")
    attempt = db.allocate_ai_model_attempt("usage-limit", "owner", valid_attempt_data())
    for ordinal in range(1, 33):
        assert db.claim_ai_job_network_request("usage-limit", "owner", attempt) == ordinal
    with pytest.raises(AIRouteBudgetExhausted):
        db.claim_ai_job_network_request("usage-limit", "owner", attempt)
    with pytest.raises(ValueError):
        db.record_ai_job_request_usage("usage-limit", "owner", attempt, 1, {
            "input_tokens": -1, "complete": False, "missing_reason": "invalid_usage",
        })
    before = db.conn.execute("SELECT usage_json FROM ai_job_model_attempts WHERE job_id='usage-limit'").fetchone()[0]
    huge = 10 ** 4000
    with pytest.raises(ValueError):
        db.record_ai_job_request_usage("usage-limit", "owner", attempt, 1, {
            "input_tokens": huge, "output_tokens": huge, "total_tokens": huge * 2,
            "cached_input_tokens": huge, "reasoning_output_tokens": huge,
            "complete": True, "missing_reason": None,
        })
    after = db.conn.execute("SELECT usage_json FROM ai_job_model_attempts WHERE job_id='usage-limit'").fetchone()[0]
    assert after == before and len(after.encode("utf-8")) <= 16384
```

The first test must also run against `make_old_ai_database`, then call `init_schema()` twice. Add a direct assertion that 16 KiB is enforced with a large `missing_reason`/unknown key payload and that unknown keys never survive. Add foreign-key and rollback assertions to the existing migration suite. Run `python -m pytest -q tests/test_ai_model_schema.py tests/test_ai_job_routing_storage.py -k 'metadata or usage or schema'`. Expected RED.

- [ ] **Step 5.2: Add additive schema and bounded usage codec.** In `storage/ai/model_schema.py`, add `validation_status TEXT CHECK(validation_status IS NULL OR validation_status IN ('not_requested','passed','failed'))` to fresh/rebuilt `ai_jobs`; add `usage_json TEXT CHECK(usage_json IS NULL OR (json_valid(usage_json) AND length(CAST(usage_json AS BLOB)) <= 16384))` to fresh attempts. Add idempotent ALTER helpers after `_ensure_ai_job_cancel_column`; do not make either field part of old job-rebuild detection. Existing jobs remain NULL.

In `storage/ai/core.py`, add constants and helpers near `_safe_error_text`:

```python
_RETENTION_POLICIES = {"metadata_only", "keyword_compat"}
_USAGE_LIMIT = 16 * 1024
_MAX_USAGE_REQUESTS = 32
_SAFE_AUDIT_KEYS = {
    "audit_version", "retention_policy", "business_type", "input_chars",
    "message_count", "input_fingerprint", "planner_version", "estimator_version", "snapshot_version",
    "allowed_provider_ids", "request_schema_version",
}


def _retention_policy(input_json: str | None) -> str | None:
    try:
        value = json.loads(input_json or "{}")
    except (TypeError, ValueError):
        return None
    policy = value.get("retention_policy") if isinstance(value, dict) else None
    return policy if policy in _RETENTION_POLICIES else None


def _safe_audit_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    policy = data.get("retention_policy")
    if policy not in _RETENTION_POLICIES:
        raise ValueError("invalid server retention policy")
    safe: dict[str, Any] = {key: data[key] for key in _SAFE_AUDIT_KEYS if key in data}
    for key in ("input_chars", "message_count", "snapshot_version", "request_schema_version"):
        if key in safe and (isinstance(safe[key], bool) or not isinstance(safe[key], int) or safe[key] < 0):
            raise ValueError(f"{key} must be a non-negative integer")
    if "input_fingerprint" in safe and not _HASH_PATTERN.fullmatch(str(safe["input_fingerprint"])):
        raise ValueError("input_fingerprint must be SHA-256")
    if "allowed_provider_ids" in safe:
        values = safe["allowed_provider_ids"]
        if not isinstance(values, list) or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in values):
            raise ValueError("allowed_provider_ids must be positive integer list")
    serialized = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > 256 * 1024:
        raise ValueError("audit payload too large")
    return safe


def _normalize_usage_payload(usage: Mapping[str, Any]) -> dict[str, Any]:
    from ...ai.usage import ProviderUsage
    allowed = {"input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
               "cache_creation_input_tokens", "reasoning_output_tokens", "complete", "missing_reason"}
    if not isinstance(usage, Mapping) or set(usage) - allowed:
        raise ValueError("invalid usage payload")
    return ProviderUsage(**dict(usage)).to_dict()
```

`create_ai_job` appends keyword-only `retention_policy: Literal['metadata_only','keyword_compat'] | None = None` and `validation_status: Literal['not_requested','passed','failed'] | None = None`; old arguments remain intact. It constructs the policy marker from this server-supplied keyword, not from `input_data`. For metadata-only, merge the server marker into the safe numeric/version envelope and call `_safe_audit_payload`. For keyword compatibility, require `task_type='keyword_clean'`, validate only `raw_keywords/tags` input lists, retain at most 80/40 entries and attach the server marker; do not run keyword payload through the metadata-only whitelist. New v2 calls explicitly initialize `validation_status='not_requested'`. Existing callers omitting both keywords keep legacy input semantics and NULL validation; this route is never selected by public `invoke_text`. Reject an input-data marker when no server policy is supplied. Add internal `replace_ai_job_audit(job_id: str, owner_token: str, audit_payload: Mapping[str, Any]) -> bool`, called once after Task 3 freezes the plan. It preserves the already-stored server policy, accepts only metadata-only safe fields, and updates only the safe input JSON while the owning job is running. Keyword input remains its bounded compatibility envelope; its fingerprint is already present in the v2 candidate snapshot.

- [ ] **Step 5.3: Implement ordinal reservation and idempotent usage persistence.** Change the existing method signature to `claim_ai_job_network_request(job_id, owner_token, attempt_index: int | None = None) -> int`. In its current `BEGIN IMMEDIATE` transaction, validate running owner and 32 limit, increment `network_request_count`, and when `attempt_index` is not None insert/update a pending request object in the attempt's compact usage JSON under the new ordinal before commit. Keep old two-argument callers working without a usage row. The pending record is:

```json
{"request_ordinal": 1, "input_tokens": null, "output_tokens": null,
 "total_tokens": null, "cached_input_tokens": null,
 "cache_creation_input_tokens": null, "reasoning_output_tokens": null,
 "complete": false, "missing_reason": "pending"}
```

`record_ai_job_request_usage` validates job/attempt owner, a previously reserved ordinal and normalized `ProviderUsage`; it loads at most 32 records, merges by ordinal using `merge_usage_snapshot`, serializes compactly and rejects >16 KiB before updating. It must never insert a new ordinal: only the claim method does that. Equal/final replay returns True without increments; stale lower cumulative values never downgrade known values. An existing request's late numeric observation may update after its job/attempt has ended if the same owner still matches, but cannot change either terminal state, allocate requests or resurrect deleted rows. Missing/deleted/owner-mismatched rows return False. The insert/update and counter/owner checks are one transaction, so a crash after commit but before HTTP send leaves pending unknown. A crash after send leaves the same pending record. `claim_ai_job_network_request` returns only after commit; Provider sends only afterward. If called with an attempt inside an outer uncommitted transaction, reject the call so the send cannot precede durable reservation.

Implement pure `summarize_usage(attempts: Sequence[Mapping[str, Any]], *, network_request_count: int) -> dict[str, Any]` in `ai/usage.py`. It uses the attempt projection's decoded `usage` envelope, deduplicates by job-level request ordinal, sums each field only across known snapshots (null if no known value), counts `reported_requests` only for `complete=True`, and counts `unreported_requests = network_request_count - reported_requests`. Duplicate/corrupt/out-of-range records are treated as unknown rather than double-counted. Add `Database.get_ai_job_usage_summary(job_id: str) -> dict[str, Any]` as a thin read wrapper using this helper. There is no per-attempt optional counter because the existing authoritative network counter is per job. Return exactly:

```python
{
    "known_input_tokens": int | None,
    "known_output_tokens": int | None,
    "known_total_tokens": int | None,
    "reported_requests": int,
    "unreported_requests": int,
    "complete": bool,  # reserved request count > 0 and unreported_requests == 0
}
```

Do not sum cache/reasoning subdivisions into totals. An explicit upstream zero is known zero; absent fields remain unknown. Legacy attempts with null usage count as unreported when the job has a reserved network request but have no fabricated pending rows.

Wire the Router's existing `_claim_network_request` to return the DB ordinal and accept `attempt_index`; retain its cancellation check before opening the short-lived DB connection. Capture the current attempt index into a local stable value before creating Provider callbacks (do not close over a variable later set to None):

```python
attempt_index = current_attempt

def request_guard() -> int:
    return self._claim_network_request(request, attempt_index)

def on_usage(ordinal: int, usage: Mapping[str, Any]) -> None:
    usage_db = self._db_factory()
    try:
        stored = usage_db.record_ai_job_request_usage(
            request.job_id, request.owner_token, attempt_index, ordinal, usage,
        )
        if not stored:
            raise AIJobConflictError("AI usage owner or request reservation no longer exists")
    finally:
        usage_db.close()
```

Pass `request_guard=request_guard, on_usage=on_usage` alongside the existing Provider cancellation callback. No network send can proceed on a failed reservation, and no new request ordinal is invented by the usage callback. Update strict fake Provider signatures in Router/service tests to accept the optional keyword instead of catching unexpected kwargs in production. Add a guard-spy test opening a second SQLite connection during fake `_post`; the pending ordinal must already be visible there, proving it was committed before send.

Run `python -m pytest -q tests/test_ai_job_routing_storage.py tests/test_ai_task_log_attempts.py -k 'usage or metadata'`. Expected GREEN after implementation.

- [ ] **Step 5.4: Enforce retention in every DB write path and expose safe validation state.** Read `input_json` inside the same terminal-write transaction and apply `_retention_policy` before constructing assignments in `update_ai_job`, `finish_ai_job_cas`, `finish_ai_model_attempt`, and stale-job recovery. For `metadata_only`, force `output_text/output_json` to SQL NULL, not the JSON string `null`. Replace any supplied error body/message with a fixed local safe summary derived only from an allowlisted category and optional HTTP status. For attempt writes also whitelist `error_category`; reject arbitrary category strings instead of using them as an alternate leak path. For a job validation failure use the exact local message `validation_failed`; other metadata-only errors may use `AI invocation failed` when no safe category is available. Use `HTTP NNN` only when the status is a non-bool integer in 100..599. Never concatenate `str(error)` or raw callback values into the generic summary. Success without error remains NULL. Router-owned in-memory output is unaffected. Keep the existing legacy/keyword redaction policy for their compatibility records. Do not add an error-category column: optional keyword-only category/status inputs to terminal methods are normalized into existing fields.

`finish_ai_job_cas` gains keyword-only `validation_status: Literal['not_requested','passed','failed'] | None = None`; `update_ai_job` gains the same. For metadata-only partial validation, determine whether content started from `ai_job_model_attempts.output_started`, not persisted `output_text`; preserve the Router's partial status and in-memory text. A completed attempt can remain transport `succeeded` while its parent job transitions to `failed` with safe category `validation_failed` and `validation_status='failed'`. Never infer legacy NULL validation from historical output: public projection maps it to `unknown` while raw DB remains NULL. Task 5 returns `job_status` from the persisted terminal row: validation failure has `finish_state='succeeded'`, `job_status='failed'`, `validation_status='failed'`; cancellation/partial/error preserve their corresponding overall status. Add explicit tests for both the transport-success/validation-failed split and a true transport partial.

Extend `_attempt_from_row` with decoded, validated `usage` (legacy NULL -> None), removing the raw duplicate `usage_json` from projection. Extend `_ai_job_from_row` with `validation_status` (`None -> 'unknown'`), `can_continue=False` and `continue_unavailable_reason='业务重放处理器尚未实现'` for main jobs. When attempts are already loaded, pass them and `network_request_count` directly to pure `summarize_usage`, avoiding a second query. For `get_ai_task_logs`, add `validation_status/network_request_count` to its SELECT and use its existing single `attempts_by_job` batch load to compute each row's summary; never call `get_ai_job_usage_summary` once per log row. Keep existing labels/pagination and `test_ai_task_logs_load_attempts_without_per_row_queries` intact. No prompt/output/error-body debug endpoint is added.

Run `python -m pytest -q tests/test_ai_job_routing_storage.py tests/test_ai_task_log_attempts.py tests/test_ai_model_router.py -k 'partial or cancel or stale or validation or usage'`. Expected GREEN. Add regression cases for Router partial directly calling `finish_ai_job_cas`, cancellation, error, and owner-CAS race; each must have null persisted text/error under metadata-only.

- [ ] **Step 5.5: Add service invocation and preserve keyword-only compatibility.** In `src/pixiv_novel_sync/ai/services/core.py`, add `_build_metadata_audit` that constructs safe data from normalized `TextInvocation`, `InvocationPlan.to_public_dict()` and the server-selected policy. It includes only business type/reference (if supplied by trusted service code), character/token estimate, message count, fingerprint, planner/estimator/snapshot versions and allowed Provider IDs. It never includes messages, raw output, API keys, response bodies or arbitrary caller keys. Start the route through the existing `_start_route_job(..., invocation=..., retention_policy='metadata_only')`, and call `replace_ai_job_audit` with the final plan fingerprint/snapshot before the first Provider send.

Implement a private `_invoke_text` shared by the public facade and keyword wrapper. Its inputs are a loaded Agent, normalized TextInvocation, server retention policy, bounded compatibility input when appropriate, optional trusted local validator, and cancellation/deadline/progress hooks; its return is `(RouteResult, validated_output_or_none)`. Only the keyword module passes a validator or `keyword_compat`. Extend `_stream_route` with private `stop_requested`/deadline forwarding to RouteRequest; `_start_route_job` stores the earlier of the caller's validated deadline and the existing 30-minute server deadline. A consumer of the returned generator dispatches delta/progress once; do not also wire the same callback into Router and accidentally deliver twice.

The shared lifecycle uses existing start/stream/finish methods as follows (inside `_invoke_text`, `db` is its freshly opened Database and is closed in `finally`; `input_data` is the safe audit or bounded keyword data):

```python
context = self._start_route_job(
    db, agent.task_type, agent, input_data,
    messages=invocation_messages(invocation), max_tokens=invocation.max_tokens,
    invocation=invocation, retention_policy=retention_policy,
    deadline_at=deadline_at,
)
stream = self._stream_route(
    context, invocation_messages(invocation),
    temperature=invocation.temperature, top_p=invocation.top_p,
    max_tokens=invocation.max_tokens, stop_requested=stop_requested,
)
while True:
    try:
        chunk = next(stream)
    except StopIteration as stopped:
        result = stopped.value
        break
    if chunk.type == "delta" and on_delta is not None:
        on_delta(chunk.text)
    elif chunk.type == "progress" and on_progress is not None:
        on_progress(chunk.data or {})

parsed = None
validation = "not_requested"
job_status = {"succeeded": "succeeded", "partial": "partial",
              "cancelled": "cancelled", "failed_before_output": "failed"}[result.finish_state]
safe_error = None
if result.finish_state == "succeeded" and validator is not None:
    try:
        parsed = validator(result.output_text)
        validation = "passed"
    except (TypeError, ValueError):
        validation, job_status, safe_error = "failed", "failed", "validation_failed"
finished_here = self._finish_route_job(
    db, context, job_status, result.output_text,
    output_json=parsed, error_message=safe_error, validation_status=validation,
)
saved = db.get_ai_job(context.job_id)
if saved is None:
    raise AIConflictError("AI job no longer exists")
if not finished_here and result.finish_state == "succeeded":
    transport = {"succeeded": "succeeded", "partial": "partial", "cancelled": "cancelled",
                 "failed": "failed_before_output"}[saved["status"]]
    result = replace(result, output_text=str(saved.get("output_text") or ""), finish_state=transport)
    parsed = None
final_validation = saved["validation_status"]
result = replace(
    result, job_status=saved["status"],
    validation_status=None if final_validation == "unknown" else final_validation,
    usage_summary=db.get_ai_job_usage_summary(context.job_id),
)
return result, parsed if result.job_status == "succeeded" else None
```

Handle exceptions in this helper with the existing owner-CAS failure path but only a fixed safe error string for metadata-only. Close the generator in `finally` when the consumer/callback raises, so Router's GeneratorExit cancellation and heartbeat cleanup still run. If a competing terminal owner wins, follow Task 3's authoritative stored outcome instead of publishing this worker's late successful text or parsed value.

The public facade body is:

```python
def invoke_text(
    self, agent_id: int, payload: Mapping[str, Any], *,
    stop_requested: Callable[[], bool] | None = None,
    deadline_at: str | None = None,
    on_delta: Callable[[str], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> RouteResult:
    db = self._db()
    try:
        agent = self._load_agent_config(db, agent_id)
        if not agent.enabled:
            raise AIServiceError("Agent is disabled")
        invocation = normalize_text_invocation(payload, agent=agent)
    finally:
        db.close()
    result, _parsed = self._invoke_text(
        agent, invocation, retention_policy="metadata_only", validator=None,
        input_data=self._build_metadata_audit(agent.task_type, invocation),
        stop_requested=stop_requested, deadline_at=deadline_at,
        on_delta=on_delta, on_progress=on_progress,
    )
    return result
```

Do not expose `retention_policy`, `validator`, `tools`, raw messages in a DB API, or an implicit enabled-Agent fallback. If plan preflight fails before a job exists, return the existing typed error with no DB row/network. If start/finalization fails after a row exists, keep the safe audit envelope and safe error category. Preserve cancellation/deadline callback behavior; no shared sync semaphore is acquired.

Keep `AIWritingService.clean_keywords` as the only caller of a private `_invoke_keyword_clean` path. Its no-explicit-ID selection retains `keyword_clean -> general -> first-enabled`; an explicitly supplied missing/disabled Agent returns None rather than selecting another Agent. It passes `retention_policy='keyword_compat'` internally, retains only its current input (80 words/40 tags) and parsed output (30 keywords/10 dropped samples), and never passes that policy through `invoke_text` or an HTTP payload. Extract current fenced/bare/prose JSON parsing into local `_validate_keyword_output(text: str) -> dict[str, list[str]]`, requiring an object and lists of strings; preserve valid existing shapes, trim empty entries, and raise ValueError for bad JSON, wrong types or empty keywords. `_invoke_keyword_clean` calls `_invoke_text` with this validator and returns its parsed component only if overall job_status is succeeded. Unavailable Agent, transport failure, invalid JSON or empty keywords still return None. The completed attempt's transport state is not overwritten when local validation fails. Existing parent preference cancellation/finalization guards stay unchanged.

Add to `tests/test_ai_service_facade.py`/`tests/test_keyword_clean.py`:

```python
@pytest.mark.parametrize("mode", ["succeeded", "partial", "failed", "cancelled"])
def test_invoke_text_enforces_retention_in_sql_not_just_projection(monkeypatch, service, mode):
    import json
    from pixiv_novel_sync.ai.models import AIProviderConfig, AIStreamChunk
    from pixiv_novel_sync.ai.providers import AIProvider, AIProviderError

    database = service._db()
    provider_id = database.create_ai_provider({
        "name": "fake", "provider_type": "openai_compatible", "enabled": True,
        "context_window": 100000,
    })
    agent_id = database.create_ai_agent({
        "name": "fake", "task_type": "general", "provider_id": provider_id,
        "model": "fake-model", "system_prompt": "", "context_window": 100000,
    })
    stopped = False

    class FakeProvider(AIProvider):
        def stream_generate(self, messages, model, temperature, top_p, max_tokens, *,
                            request_guard=None, is_cancelled=None, on_usage=None):
            ordinal = request_guard()
            if mode == "failed":
                raise AIProviderError("INPUT_SENTINEL echoed by upstream", category="network")
            yield AIStreamChunk(type="delta", text="OUTPUT_SENTINEL")
            if mode == "partial":
                raise AIProviderError("OUTPUT_SENTINEL upstream failure", category="network")
            if on_usage is not None:
                on_usage(ordinal, {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                                   "complete": True, "missing_reason": None})
            yield AIStreamChunk(type="done", data={"finish_reason": "stop"})

    fake = FakeProvider(AIProviderConfig(
        id=provider_id, name="fake", provider_type="openai_compatible",
        base_url="https://example.com/v1", api_key="synthetic-key", default_model="fake-model",
    ))
    monkeypatch.setattr(service.model_router, "_get_provider", lambda _config: fake)

    def on_delta(_text):
        nonlocal stopped
        stopped = mode == "cancelled"

    try:
        result = service.invoke_text(
            agent_id, {"messages": [{"role": "user", "content": "INPUT_SENTINEL"}], "max_tokens": 50},
            on_delta=on_delta, stop_requested=lambda: stopped,
        )
        assert result.job_status == mode
        if mode in {"succeeded", "partial"}:
            assert result.output_text == "OUTPUT_SENTINEL"
        all_values = [dict(row) for row in database.conn.execute("SELECT * FROM ai_jobs")]
        all_values += [dict(row) for row in database.conn.execute("SELECT * FROM ai_job_model_attempts")]
        serialized = json.dumps(all_values, ensure_ascii=False)
        assert "INPUT_SENTINEL" not in serialized and "OUTPUT_SENTINEL" not in serialized
        row = database.get_ai_job(result.job_id)
        assert row["input"]["retention_policy"] == "metadata_only"
        assert row["output_text"] is None
    finally:
        fake.close()
        database.close()


def test_keyword_clean_uses_private_compatibility_only(monkeypatch, service):
    fake_db = FakeDB([_agent_row()])
    calls = _wire(monkeypatch, service, fake_db, '{"keywords":["校园"]}')
    assert service.clean_keywords(["校园"]) == {"keywords": ["校园"], "dropped_sample": []}
    assert calls["start"][0][4]["retention_policy"] == "keyword_compat"
    assert calls["finish"][-1]["status"] == "succeeded"
```

The integration fake above exercises real storage and routing; the existing keyword `_wire` FakeDB is only for business parsing assertions. Add `caplog` and an assertion that neither sentinel appears in captured ordinary logs. Update the existing keyword fake finish method to accept/capture `validation_status`; add invalid-JSON assertions that the private result has transport succeeded/overall failed/validation failed, SQLite job is failed, and the completed attempt remains succeeded. Task 6 must ADD a main-continue 409 regression using a valid saved v1 snapshot/attempt, asserting no new job and zero Provider requests; no existing test currently proves this behavior. Keep public legacy response fields compatible; additive validation/usage fields require updating exact-shape tests, not deleting those assertions.

Run `python -m pytest -q tests/test_ai_service_facade.py tests/test_keyword_clean.py tests/test_ai_model_router_integration.py tests/test_ai_job_routing_storage.py tests/test_ai_task_log_attempts.py`; then `python -m compileall -q src` and `git diff --check`. Expected GREEN. No public main-continue replay route is added; Task 6's new API regression verifies the current 409 behavior.

### Task 6: Bounded Preflight API And Existing Job Diagnostics

**Depends on:** Tasks 1-5. Do not reproduce token estimation or catalogue CAS in the web layer.

**Files:**
- Create: `src/pixiv_novel_sync/web/request_limits.py`
- Create: `src/pixiv_novel_sync/ai/job_diagnostics.py`
- Modify: `src/pixiv_novel_sync/ai/services/admin.py` (`preview_agent_candidates` neighborhood)
- Modify: `src/pixiv_novel_sync/ai_web.py` (JSON helpers; provider-model routes; Agent routes; job list/detail routes)
- Modify: `config/nginx/pixiv-novel-sync.conf` (new preflight-only location before existing regex locations)
- Create: `tests/test_ai_preflight_api.py`
- Modify: `tests/test_deployment_config.py`
- Modify: `tests/test_ai_agent_candidates.py` (preserve legacy GET preview expectations)

**Interfaces:**
- Consumes: `normalize_text_invocation(payload, *, agent) -> TextInvocation` and `InvocationValidationError(ValueError)` from Task 1.
- Consumes: `ModelRouter.plan_invocation(agent, request) -> InvocationPlan`; `InvocationPlan.to_public_dict()` contains `can_run`, `eligible`, `excluded`, per-candidate budgets, and limits. No Provider construction or Key decryption is required.
- Consumes: Task 2 `AIAdminMixin.update_provider_models_enabled(provider_id, payload)` with `{catalog_revision: str, changes: [{id: int, enabled: bool}]}`.
- Consumes: Task 5 stored job `validation_status` and `usage_summary`, with nullable known totals, `reported_requests`, `unreported_requests`, and `complete`.
- Produces: `AIAdminMixin.preflight_agent(agent_id: int, payload: Mapping[str, Any]) -> dict[str, Any]`.
- Produces: `POST /api/dashboard/ai/agents/<int:agent_id>/preflight` returning the standard `{ok, data}` envelope, where a valid but unusable plan is HTTP 200 with `data.can_run=false`.
- Produces: `PUT /api/dashboard/ai/providers/<int:provider_id>/models/enabled` returning the standard envelope around the Task 2 result.
- Produces: `project_main_job_diagnostics(job: Mapping[str, Any]) -> dict[str, Any]`, adding `can_continue=false` and `continue_unavailable_reason`, without changing the existing job `status` or result fields.
- Produces: `snapshot_version`, `invocation_plan` (v2 safe `to_public_dict()` projection or null), and a local `plan_unavailable_reason` on invalid stored plans; old v1 PromptBudget display remains separate.

**Binding details:**
- The API takes COMPLETE supplied messages. Neither this route nor the service prepends `Agent.system_prompt`. The UI explicitly shows and includes the saved system prompt once when constructing its test messages.
- The new POST has a 16 MiB body limit and a maximum of 256 messages. The pure Python invocation contract remains independent of this HTTP message-count limit.
- Reject oversized bodies before JSON parsing. Support the repository's Flask minimum 3.0.3; do not rely on the per-request `max_content_length` setter introduced later.
- Preserve the existing `GET /agents/<id>/candidates` route and its current status behavior, including the legacy missing-Agent 400. The NEW preflight returns 404 for missing Agent.
- Reuse existing auth/CSRF hooks. Never mark this route exempt or use `json_payload()`'s silent empty-object fallback.
- No preflight job, token decryption, secret migration, DNS call, remote count endpoint, or input persistence.

- [ ] **Step 1: Add concrete API and security regressions.**

Create `tests/test_ai_preflight_api.py` with the following fixture and initial tests. The fixture creates a deliberately undecryptable Provider key: the test succeeds only if planning never needs it.

```python
from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.ai_web import register_ai_routes
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.webapp import create_app


@pytest.fixture
def preflight_api(tmp_path: Path):
    path = tmp_path / "preflight.db"
    app = Flask(__name__)
    app.secret_key = "preflight-fixture"
    app.config["TESTING"] = True
    register_ai_routes(app, SimpleNamespace(storage=SimpleNamespace(db_path=path)))
    db = Database(path)
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "private-test-provider",
        "provider_type": "openai_compatible",
        "base_url": "https://private-provider.example.test/v1",
        "api_key_encrypted": "not-a-decryptable-key",
        "context_window": 1_000_000,
        "enabled": True,
    })
    long_id = db.create_ai_provider_model({
        "provider_id": provider_id,
        "model_key": "long-model",
        "manual_context_window": 1_000_000,
        "enabled": True,
    })
    short_id = db.create_ai_provider_model({
        "provider_id": provider_id,
        "model_key": "short-model",
        "manual_context_window": 128_000,
        "enabled": True,
    })
    pool_id = db.create_ai_model_pool({"name": "reading-route", "pool_kind": "custom"})
    db.replace_ai_model_pool_members(
        pool_id,
        [{"provider_model_id": long_id, "enabled": True},
         {"provider_model_id": short_id, "enabled": True}],
        expected_version=1,
    )
    agent_id = db.create_ai_agent({
        "name": "preflight-agent",
        "task_type": "keyword_clean",
        "binding_type": "pool",
        "model_pool_id": pool_id,
        "provider_id": None,
        "model": None,
        "system_prompt": "SAVED_SYSTEM_NOT_AUTOMATICALLY_PREPENDED",
        "context_window": 1_000_000,
        "max_tokens": 1000,
        "temperature": 0.2,
        "top_p": 0.9,
    })
    try:
        yield app, app.test_client(), db, agent_id, provider_id
    finally:
        app.extensions["pixiv_novel_sync.ai_service"].close()
        db.close()


def test_preflight_filters_short_fallback_without_external_work(preflight_api, monkeypatch):
    app, client, db, agent_id, _ = preflight_api
    service = app.extensions["pixiv_novel_sync.ai_service"]._current()

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight attempted external/secret work")

    monkeypatch.setattr(AIWritingService, "_get_provider", forbidden)
    monkeypatch.setattr(type(service.secret_manager), "decrypt", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    before = db.conn.total_changes
    body = "x" * 800_000 + "PRIVATE_INPUT_TAIL_SENTINEL"
    response = client.post(
        f"/api/dashboard/ai/agents/{agent_id}/preflight",
        json={"messages": [{"role": "user", "content": body}], "max_tokens": 1000},
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["can_run"] is True
    assert [row["model_key"] for row in data["eligible"]] == ["long-model"]
    assert data["excluded"][0]["reason"] == "context_overflow"
    public = json.dumps(data)
    for secret in ("PRIVATE_INPUT_TAIL_SENTINEL", "not-a-decryptable-key",
                   "private-provider.example.test", "request_fingerprint"):
        assert secret not in public
    assert db.conn.total_changes == before
    assert db.conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 0


def test_valid_preflight_with_empty_scope_is_not_a_generation_error(preflight_api):
    _, client, _, agent_id, _ = preflight_api
    response = client.post(
        f"/api/dashboard/ai/agents/{agent_id}/preflight",
        json={"messages": [{"role": "user", "content": "test"}],
              "allowed_provider_ids": []},
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert response.get_json()["data"]["can_run"] is False
    assert response.get_json()["data"]["eligible"] == []


@pytest.mark.parametrize("payload", [
    [], {}, {"messages": []},
    {"messages": [{"role": "tool", "content": "test"}]},
    {"messages": [{"role": "user", "content": 2}]},
    {"messages": [{"role": "user", "content": "test"}], "tools": []},
    {"messages": [{"role": "user", "content": "test"}], "allowed_provider_ids": None},
    {"messages": [{"role": "user", "content": "test"}]} | {"max_tokens": True},
])
def test_preflight_rejects_invalid_contract(preflight_api, payload):
    _, client, _, agent_id, _ = preflight_api
    response = client.post(f"/api/dashboard/ai/agents/{agent_id}/preflight", json=payload)
    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_preflight_http_limits_and_missing_agent(preflight_api):
    _, client, _, agent_id, _ = preflight_api
    path = f"/api/dashboard/ai/agents/{agent_id}/preflight"
    too_many = client.post(path, json={"messages": [{"role": "user", "content": "x"}] * 257})
    assert too_many.status_code == 400
    too_large = client.post(path, data=b"{}", content_type="application/json",
                            environ_overrides={"CONTENT_LENGTH": str(16 * 1024 * 1024 + 1)})
    assert too_large.status_code == 413
    missing = client.post("/api/dashboard/ai/agents/99999999/preflight",
                          json={"messages": [{"role": "user", "content": "test"}]})
    assert missing.status_code == 404


def test_bounded_reader_checks_unknown_length_stream_before_json_parse():
    from pixiv_novel_sync.web.request_limits import JSONBodyError, read_bounded_json_object

    class TrackedStream(io.BytesIO):
        sizes = []

        def read(self, size=-1):
            self.sizes.append(size)
            return super().read(size)

    stream = TrackedStream(b"x" * 100)
    request = SimpleNamespace(is_json=True, content_length=None, stream=stream)
    with pytest.raises(JSONBodyError) as raised:
        read_bounded_json_object(request, limit_bytes=8)
    assert raised.value.status_code == 413
    assert stream.sizes == [9]


def test_real_dashboard_auth_and_csrf_apply_to_preflight(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "preflight-test-token")
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "preflight-test-secret")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\nDASHBOARD_TOKEN=preflight-test-token\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    app.config["RAW_HTTP"] = True
    client = app.test_client()
    path = "/api/dashboard/ai/agents/99999999/preflight"
    payload = {"messages": [{"role": "user", "content": "test"}]}
    try:
        assert client.post(path, json=payload).status_code == 401
        assert client.post("/api/auth/login", data={"token": "preflight-test-token"}).status_code == 302
        assert client.post(path, json=payload).status_code == 403
        token = client.get("/api/csrf-token").get_json()["csrf_token"]
        allowed = client.post(path, json=payload, headers={"X-CSRF-Token": token})
        assert allowed.status_code == 404
    finally:
        app.extensions["pixiv_novel_sync.ai_service"].close()
```

Add these exact same-planner and actual PUT route tests to that file. They consume the already-proven Task 2 transaction implementation rather than duplicate its SQL:

```python
def test_service_preflight_uses_exact_supplied_messages(preflight_api):
    from pixiv_novel_sync.ai.invocation import normalize_text_invocation

    app, _, db, agent_id, _ = preflight_api
    service = app.extensions["pixiv_novel_sync.ai_service"]._current()
    payload = {"messages": [
        {"role": "system", "content": "EXPLICIT_SYSTEM_ONCE"},
        {"role": "user", "content": "EXPLICIT_USER_ONCE"},
    ]}
    original = json.loads(json.dumps(payload))
    agent = service._load_agent_config(db, agent_id)
    request = normalize_text_invocation(payload, agent=agent)
    expected = service.model_router.plan_invocation(agent, request).to_public_dict()
    assert service.preflight_agent(agent_id, payload) == expected
    assert payload == original


def _enabled_rows(db, provider_id):
    return dict(db.conn.execute(
        "SELECT id, enabled FROM ai_provider_models WHERE provider_id = ? ORDER BY id",
        (provider_id,),
    ).fetchall())


def test_enabled_route_applies_one_atomic_selection_and_rejects_stale_revision(preflight_api):
    _, client, db, _, provider_id = preflight_api
    before = _enabled_rows(db, provider_id)
    first, second = list(before)
    revision = db.get_ai_provider_catalog_revision(provider_id)
    path = f"/api/dashboard/ai/providers/{provider_id}/models/enabled"
    response = client.put(path, json={
        "catalog_revision": revision,
        "changes": [{"id": first, "enabled": False}, {"id": second, "enabled": True}],
    })
    assert response.status_code == 200
    assert _enabled_rows(db, provider_id) == {first: 0, second: 1}
    stale = client.put(path, json={
        "catalog_revision": revision,
        "changes": [{"id": second, "enabled": False}],
    })
    assert stale.status_code == 409
    assert _enabled_rows(db, provider_id) == {first: 0, second: 1}


def test_enabled_route_rejects_foreign_model_without_partial_write(preflight_api):
    _, client, db, _, provider_id = preflight_api
    before = _enabled_rows(db, provider_id)
    other = db.create_ai_provider({"name": "other", "provider_type": "openai_compatible"})
    foreign = db.create_ai_provider_model({"provider_id": other, "model_key": "foreign", "enabled": True})
    response = client.put(f"/api/dashboard/ai/providers/{provider_id}/models/enabled", json={
        "catalog_revision": db.get_ai_provider_catalog_revision(provider_id),
        "changes": [{"id": next(iter(before)), "enabled": False}, {"id": foreign, "enabled": False}],
    })
    assert response.status_code == 400
    assert _enabled_rows(db, provider_id) == before
    assert _enabled_rows(db, other)[foreign] == 1


def test_job_api_exposes_unknown_usage_and_unsupported_continue(preflight_api):
    _, client, db, agent_id, provider_id = preflight_api
    job_id, owner = "preflight-diagnostics-job", "preflight-diagnostics-owner"
    db.create_ai_job(job_id, "keyword_clean", agent_id, {}, owner_token=owner)
    attempt = db.allocate_ai_model_attempt(job_id, owner, {
        "provider_id": provider_id, "model_key": "long-model", "stage": "main",
    })
    assert db.claim_ai_job_network_request(job_id, owner, attempt_index=attempt) == 1
    response = client.get(f"/api/dashboard/ai/jobs/{job_id}")
    assert response.status_code == 200
    job = response.get_json()["data"]
    assert job["usage_summary"]["unreported_requests"] == 1
    assert job["usage_summary"]["complete"] is False
    assert job["usage_summary"]["known_total_tokens"] is None
    assert job["can_continue"] is False
    assert job["continue_unavailable_reason"]
    raw = db.conn.execute("SELECT validation_status FROM ai_jobs WHERE job_id = ?", (job_id,)).fetchone()[0]
    assert raw is None
    assert job["validation_status"] == "unknown"


def test_main_continue_rejects_valid_history_without_starting_a_job(preflight_api, monkeypatch):
    from dataclasses import asdict

    app, client, db, agent_id, _ = preflight_api
    service = app.extensions["pixiv_novel_sync.ai_service"]._current()
    agent = service._load_agent_config(db, agent_id)
    snapshot = service.model_router.resolve_candidates(agent)
    snapshot_payload = {
        "agent_config_hash": snapshot.agent_config_hash,
        "binding_version": snapshot.binding_version,
        "candidates": [asdict(candidate) for candidate in snapshot.candidates],
    }
    job_id, owner = "legacy-unhandled-job", "legacy-unhandled-owner"
    db.create_ai_job(job_id, "keyword_clean", agent_id, {}, owner_token=owner)
    assert db.set_ai_job_candidate_snapshot(job_id, owner, snapshot_payload, snapshot.snapshot_hash)
    assert db.finish_ai_job_cas(job_id, owner, "failed")
    before = db.conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0]

    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported continue dispatched a model")

    monkeypatch.setattr(AIWritingService, "_get_provider", forbidden)
    response = client.post(f"/api/dashboard/ai/jobs/{job_id}/continue", json={
        "parent_job_id": job_id,
        "idempotency_key": "legacy-unsupported-continue-0001",
        "candidate_snapshot_hash": snapshot.snapshot_hash,
        "resume_candidate_index": 0,
    })
    assert response.status_code == 409
    assert "暂不支持手动候选继续" in response.get_json()["error"]
    assert db.conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == before
```

- [ ] **Step 2: Run the tests red.**

Run: `python -m pytest tests/test_ai_preflight_api.py -q`.

Expected: missing preflight route/helper failures (404 or import errors). If any network/real database is contacted, fix fixture isolation before proceeding; do not relax the forbidden-call assertions.

- [ ] **Step 3: Add the bounded reader, service adapter, routes, and main diagnostics projection.**

Create `web/request_limits.py`:

```python
from __future__ import annotations

import json
from typing import Any

from flask import Request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge


class JSONBodyError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def read_bounded_json_object(request: Request, *, limit_bytes: int) -> dict[str, Any]:
    if not request.is_json:
        raise JSONBodyError("invalid_json", "请求体必须是 JSON 对象")
    length = request.content_length
    if length is not None and length > limit_bytes:
        raise JSONBodyError("payload_too_large", "请求体超过允许大小", 413)
    try:
        raw = request.stream.read(limit_bytes + 1)
    except RequestEntityTooLarge:
        raise JSONBodyError("payload_too_large", "请求体超过允许大小", 413) from None
    except BadRequest:
        raise JSONBodyError("invalid_json", "请求体不完整或无法读取") from None
    if len(raw) > limit_bytes:
        raise JSONBodyError("payload_too_large", "请求体超过允许大小", 413)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise JSONBodyError("invalid_json", "请求体必须是有效的 JSON 对象") from None
    if not isinstance(payload, dict):
        raise JSONBodyError("invalid_json", "请求体必须是 JSON 对象")
    return payload
```

Add the following method to the existing administration mixin, importing `normalize_text_invocation` and `InvocationValidationError` from `..invocation`. The task's implementation may keep the final exception conversion in the route so the error code stays available; do not leak raw input in messages.

```python
def preflight_agent(self, agent_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    db = self._db()
    try:
        if db.get_ai_agent(agent_id) is None:
            raise AINotFoundError("Agent 不存在")
        agent = self._load_agent_config(db, agent_id)
    finally:
        db.close()
    invocation = normalize_text_invocation(payload, agent=agent)
    return self.model_router.plan_invocation(agent, invocation).to_public_dict()
```

Create `ai/job_diagnostics.py` (leave stored output/usage decisions to Task 5). Decoding the saved plan is local validation only; it must not reload Providers, keys, or current model settings:

```python
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .invocation_snapshot import PlannedCandidateSnapshot
from .model_router import ModelRouteConflictError, ModelRouter


def project_main_job_diagnostics(job: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(job)
    result["validation_status"] = job.get("validation_status") or "unknown"
    result["can_continue"] = False
    result["continue_unavailable_reason"] = "当前任务类型暂不支持手动候选继续"
    result["snapshot_version"] = None
    result["invocation_plan"] = None
    result["plan_unavailable_reason"] = None
    payload = job.get("candidate_snapshot")
    if payload is not None:
        try:
            snapshot = ModelRouter.candidate_snapshot_from_payload(
                payload, job.get("candidate_snapshot_hash"),
            )
        except ModelRouteConflictError:
            result["plan_unavailable_reason"] = "保存的调用计划无法读取"
        else:
            result["snapshot_version"] = getattr(snapshot, "version", 1)
            if isinstance(snapshot, PlannedCandidateSnapshot):
                result["invocation_plan"] = snapshot.plan.to_public_dict()
    return result
```

In `ai_web.py`, import the reader/error, invocation validation error, `ModelRouteError`, and projection. Add named module constants `_AI_PREFLIGHT_BODY_LIMIT = 16 * 1024 * 1024` and `_AI_PREFLIGHT_MESSAGES_LIMIT = 256` next to the logger. Register these routes inside `register_ai_routes` using the existing `service`, `ok`, and `fail` closures:

```python
@app.put("/api/dashboard/ai/providers/<int:provider_id>/models/enabled")
def update_ai_provider_models_enabled(provider_id: int):
    try:
        return ok(service.update_provider_models_enabled(provider_id, require_json_object()))
    except Exception as exc:
        return fail(exc)


@app.post("/api/dashboard/ai/agents/<int:agent_id>/preflight")
def preflight_ai_agent(agent_id: int):
    try:
        payload = read_bounded_json_object(request, limit_bytes=_AI_PREFLIGHT_BODY_LIMIT)
        messages = payload.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= _AI_PREFLIGHT_MESSAGES_LIMIT:
            raise JSONBodyError("invalid_messages", "messages 必须包含 1 到 256 条文本消息")
        return ok(service.preflight_agent(agent_id, payload))
    except JSONBodyError as exc:
        return jsonify({"ok": False, "error": str(exc), "code": exc.code}), exc.status_code
    except InvocationValidationError as exc:
        return jsonify({"ok": False, "error": str(exc), "code": exc.code}), 400
    except (AIServiceError, ModelRouteError) as exc:
        return fail(exc)
    except Exception as exc:
        logger.warning("AI preflight failed (%s)", type(exc).__name__)
        return fail(AIServiceError("输入预检暂时不可用"), 500)
```

After existing `list_ai_jobs` reads its database result and closes the DB, project the list without additional queries:

```python
result["items"] = [project_main_job_diagnostics(job) for job in result["items"]]
return ok(result)
```

In existing `get_ai_job`, retain the missing-job 404 and return:

```python
return ok(project_main_job_diagnostics(job))
```

Do not change the existing `/continue` service's unsupported 409 behavior. The new `test_main_continue_rejects_valid_history_without_starting_a_job` supplies a correctly formed legacy snapshot and terminal parent; it must reach the unsupported-business branch, not merely fail malformed-input validation. Do not convert it into a 200 SSE error. The UI consumes `can_continue=false` from detail responses.

- [ ] **Step 4: Match the reverse-proxy body limit and verify its scope.**

Insert the following block before the existing SSE regex in `config/nginx/pixiv-novel-sync.conf`. Keep surrounding TLS/real-IP/auth deployment configuration unchanged. This block does not enable a public inference gateway:

```nginx
    location ~ ^/api/dashboard/ai/agents/[0-9]+/preflight$ {
        client_max_body_size 16m;
        proxy_pass http://127.0.0.1:5011;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_cache off;
        proxy_connect_timeout 10s;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
```

Add this deterministic deployment test. Actual `nginx -t` remains a Linux/deployment check; do not claim it from a Windows string test.

```python
def test_preflight_body_limit_is_route_specific():
    import re
    from pathlib import Path

    text = Path("config/nginx/pixiv-novel-sync.conf").read_text(encoding="utf-8")
    pattern = r"location ~ \^/api/dashboard/ai/agents/\[0-9\]\+/preflight\$\s*\{([^}]+)\}"
    match = re.search(pattern, text)
    assert match is not None
    assert "client_max_body_size 16m;" in match.group(1)
    assert "proxy_request_buffering off;" in match.group(1)
    without_preflight = text[:match.start()] + text[match.end():]
    assert "client_max_body_size" not in without_preflight
```

- [ ] **Step 5: Run focused regressions and verify APIs, not only source text.**

Run:

```text
python -m pytest tests/test_ai_preflight_api.py tests/test_ai_agent_candidates.py tests/test_ai_model_catalog.py tests/test_ai_model_schema.py tests/test_ai_web_stream_response.py tests/test_webapp_security.py tests/test_deployment_config.py tests/test_deployment_contract.py -q
python -m compileall -q src
git diff --check
```

Expected: all focused tests pass. Record actual counts in the implementation report. Verify authenticated/unauthenticated and CSRF cases actually ran; a standalone Flask blueprint fixture alone cannot prove the real dashboard gate.

Confirm the diagnostic regression from Step 1 records a pending request via Task 5, reads the actual API, and checks both nullable raw validation state and its `unknown` public projection. It must not replace those assertions with a mocked API response. Add these pure projection checks to the same test file (the Task 3 codec is already implemented):

```python
def test_job_diagnostics_projects_frozen_v2_plan_without_current_provider_access(preflight_api, monkeypatch):
    from pixiv_novel_sync.ai.invocation import normalize_text_invocation
    from pixiv_novel_sync.ai.invocation_snapshot import freeze_plan, planned_snapshot_payload
    from pixiv_novel_sync.ai.job_diagnostics import project_main_job_diagnostics

    app, _, db, agent_id, _ = preflight_api
    service = app.extensions["pixiv_novel_sync.ai_service"]._current()
    agent = service._load_agent_config(db, agent_id)
    request = normalize_text_invocation(
        {"messages": [{"role": "user", "content": "PRIVATE_PLAN_INPUT"}]}, agent=agent,
    )
    plan = service.model_router.plan_invocation(agent, request)
    snapshot = freeze_plan(service.model_router._agent_config_hash(agent), agent.binding_version, plan)

    def forbidden(*args, **kwargs):
        raise AssertionError("saved diagnostics must not reload configuration")

    monkeypatch.setattr(AIWritingService, "_get_provider", forbidden)
    job = {"status": "failed", "validation_status": "failed",
           "candidate_snapshot": planned_snapshot_payload(snapshot),
           "candidate_snapshot_hash": snapshot.snapshot_hash}
    result = project_main_job_diagnostics(job)
    assert result["snapshot_version"] == 2
    assert result["invocation_plan"] == plan.to_public_dict()
    assert result["status"] == "failed"
    assert "PRIVATE_PLAN_INPUT" not in json.dumps(result)
    assert "request_fingerprint" not in json.dumps(result["invocation_plan"])
    corrupt = dict(job, candidate_snapshot_hash="0" * 64)
    invalid = project_main_job_diagnostics(corrupt)
    assert invalid["invocation_plan"] is None
    assert invalid["plan_unavailable_reason"]
```

- [ ] **Step 6: Commit this independently reviewable API deliverable.**

```text
git add src/pixiv_novel_sync/web/request_limits.py src/pixiv_novel_sync/ai/job_diagnostics.py src/pixiv_novel_sync/ai/services/admin.py src/pixiv_novel_sync/ai_web.py config/nginx/pixiv-novel-sync.conf tests/test_ai_preflight_api.py tests/test_deployment_config.py tests/test_ai_agent_candidates.py
git commit -m "feat(ai): expose bounded input preflight and job diagnostics"
```

Acceptance coverage: A05/A06 API edges, A07-A10 preflight reuse, A11 unsupported continuation, A14/A15 diagnostics, A17, A19 no input echo.

### Task 7: Shared Picker, Models Workspace, Provider Onboarding, and Group Editing

**Depends on:** Task 2 catalogue revision/output-limit/atomic-enabled contracts. Independent of generation execution once those contracts are stable.

**Files:**
- Create: `src/pixiv_novel_sync/static/js/ai_settings.js` (pure identity, filtering, URL, draft, and selection helpers).
- Create: `src/pixiv_novel_sync/static/js/ai_picker.js` (shared Vue picker using the existing `app-modal`).
- Create: `src/pixiv_novel_sync/static/js/ai_provider_onboarding.js` (in-memory onboarding state machine; injected HTTP/SSE dependencies).
- Create: `src/pixiv_novel_sync/templates/dashboard_ai_controls.html` (shared static-script includes and scoped picker CSS).
- Create: `src/pixiv_novel_sync/templates/dashboard_ai_model_catalog.html` (move the existing catalogue markup here, not a second catalogue implementation).
- Create: `tests/helpers/management_vm.cjs` (extract the current shared VM mount helper without changing its behavioral assertions).
- Create: `tests/test_ai_settings_runtime.cjs`, `tests/test_ai_settings_runtime.py`.
- Modify: `src/pixiv_novel_sync/templates/base.html:408` (retain HTTP status/data on shared API errors).
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings_models.html:33` (workspace views/forms/catalogue), `src/pixiv_novel_sync/templates/dashboard_settings_models.html:336` (existing page setup).
- Modify: `tests/test_mobile_management_runtime.cjs:11` (import the extracted harness; retain existing R1-R4 regressions).
- Modify: `tests/test_mobile_management.py`, `tests/test_ai_model_ui.py` (update superseded markup assertions; keep security/behavior assertions).
- Modify: `pyproject.toml:50`, `scripts/tailwind.config.js:9`, `tests/test_tailwind_static_css.py`.
- Generated by the existing script, not hand-edited: `src/pixiv_novel_sync/static/css/tailwind.css` and its cache hash in `src/pixiv_novel_sync/templates/base.html`.

**Consumes:**
- `GET /api/dashboard/ai/providers` -> current provider projections, including `has_api_key`, never a stored Key.
- `POST /api/dashboard/ai/providers` -> `{id}`; existing provider `PUT` -> `{warnings}` (do not assume PUT returns an ID).
- Existing probe, model-sync, operation/events, cancel, and confirm-empty endpoints.
- `GET /api/dashboard/ai/providers/<id>/models` -> existing `items` plus `catalog_revision`; items retain `discovered`, `manual`, `discovered_available`, `source`, `enabled`, `routable`, and effective/discovered/manual model fields.
- Task 2 `PUT /api/dashboard/ai/providers/<id>/models/enabled` with `{catalog_revision, changes:[{id,enabled}]}`; at most 5000 changes; all-or-nothing; 409 means reload and explicitly reconfirm.
- Existing model pool metadata/member endpoints with `expected_version`, pool references, and pool attempt history.

**Produces:** The shared interfaces above; a models workspace with honest pending/error states; the picker registration consumed by Task 8. No new configuration table or binding API.

- [ ] **7.1 Red: add executable helper contracts, before creating helper files.** Start `tests/test_ai_settings_runtime.cjs` with these tests. Do not mock the functions under test.

```javascript
const assert = require('node:assert/strict');
const test = require('node:test');
const S = require('../src/pixiv_novel_sync/static/js/ai_settings.js');

const option = (overrides = {}) => ({
  kind: 'model', provider_id: 7, provider_model_id: 31,
  provider_name: 'Fixture relay', model_key: 'org/model/v1',
  label: 'Same display name', availability: 'ready', ...overrides,
});

test('opaque choice identity keeps provider IDs and slash-containing keys distinct', () => {
  assert.notEqual(S.optionKey(option()), S.optionKey(option({provider_id: 8})));
  assert.equal(JSON.parse(S.optionKey(option()))[2], 'org/model/v1');
});
test('unavailable current choice survives refresh and filtering without replacement', () => {
  const current = option({availability: 'unavailable', reason: 'not in catalogue'});
  const other = option({provider_id: 8, provider_model_id: 40});
  const rows = S.filterOptions(S.retainCurrent([other], current), 'not-matching');
  assert.equal(rows.length, 1);
  assert.equal(rows[0].current, true);
  assert.equal(rows[0].availability, 'unavailable');
  assert.equal(current.current, undefined);
  assert.equal(S.retainCurrent([], option({availability: 'unverified'}))[0].availability, 'unverified');
});
test('touched patches distinguish 0, false, missing, and null', () => {
  const old = {temperature: 0.8, max_retries: 2, stream_enabled: true, name: 'A'};
  const draft = {...old, temperature: 0, max_retries: 0, stream_enabled: false, extra: null};
  assert.deepEqual(S.touchedPatch(old, draft, ['temperature', 'max_retries', 'stream_enabled', 'extra']),
    {temperature: 0, max_retries: 0, stream_enabled: false, extra: null});
  assert.deepEqual(S.touchedPatch({}, {temperature: 0.8}, []), {});
});
test('workspace parsing never turns an invalid requested provider into provider zero', () => {
  assert.deepEqual(S.readWorkspace('?view=wrong&provider=missing'),
    {view: 'providers', providerId: null, invalidProvider: 'missing'});
  assert.deepEqual(S.readWorkspace('?view=models&provider=17'),
    {view: 'models', providerId: 17, invalidProvider: null});
  assert.equal(S.workspaceSearch('?provider=17&keep=yes', {view: 'groups'}),
    '?provider=17&keep=yes&view=groups');
});
test('onboarding selects only persisted discovered rows and never changes manual-only rows', () => {
  const catalogue = {catalog_revision: 'a'.repeat(64), items: [
    {id: 1, model_key: 'wanted', discovered: true, discovered_available: true},
    {id: 2, model_key: 'other', discovered: true, discovered_available: true},
    {id: 3, model_key: 'manual', discovered: false, manual: true, enabled: true},
    {id: 4, model_key: 'gone', discovered: true, discovered_available: false},
  ]};
  assert.deepEqual(S.initialModelSelection(catalogue, ['wanted', 'gone']), {
    revision: 'a'.repeat(64), changes: [{id: 1, enabled: true}, {id: 2, enabled: false}], missing: ['gone'],
  });
});
test('reorder is immutable and output override clearing is explicit', () => {
  const rows = [{id: 1}, {id: 2}, {id: 3}];
  assert.deepEqual(S.moveItem(rows, 0, 2).map(x => x.id), [2, 3, 1]);
  assert.deepEqual(rows.map(x => x.id), [1, 2, 3]);
  assert.equal(S.parseNullableOutput(null), null);
  assert.equal(S.parseNullableOutput('1000000'), 1000000);
  for (const bad of ['', true, 0, -1, 1000001, '1.5']) assert.throws(() => S.parseNullableOutput(bad));
});
```

Run: `node --test tests/test_ai_settings_runtime.cjs`.
Expected red: `MODULE_NOT_FOUND` for `ai_settings.js`; do not treat a Node installation failure as the intended red result.

- [ ] **7.2 Green: create the small shared helper module with a browser/CommonJS wrapper.** The following is the complete pure-helper core. Export these functions from a factory; attach it to `window.AISettings` in the browser and `module.exports` under Node. Do not attach helpers to a live application database or import Vue here.

```javascript
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AISettings = api;
})(typeof window === 'object' ? window : globalThis, function () {
  'use strict';
  const copy = value => JSON.parse(JSON.stringify(value));
  const has = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
  const views = new Set(['providers', 'models', 'groups']);
  function optionKey(o) {
    return JSON.stringify(o.kind === 'pool' ? ['pool', o.pool_id]
      : ['model', o.provider_id, o.model_key]);
  }
  function retainCurrent(options, current) {
    const rows = options.map(o => ({...o, current: false}));
    if (!current) return rows;
    const key = optionKey(current), at = rows.findIndex(o => optionKey(o) === key);
    const row = at < 0 ? {...current, availability: current.availability || 'unavailable'} : rows.splice(at, 1)[0];
    return [{...row, current: true}, ...rows];
  }
  function filterOptions(options, query) {
    const words = String(query || '').toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
    return options.filter(o => o.current || words.every(word =>
      [o.label, o.model_key, o.provider_name, o.provider_id, o.pool_id, o.availability, o.reason]
        .map(x => String(x ?? '')).join(' ').toLocaleLowerCase().includes(word)));
  }
  function readWorkspace(search) {
    const p = new URLSearchParams(search), raw = p.get('provider');
    const number = raw !== null && /^[1-9]\d*$/.test(raw) ? Number(raw) : null;
    const valid = number !== null && Number.isSafeInteger(number);
    return {view: views.has(p.get('view')) ? p.get('view') : 'providers',
      providerId: valid ? number : null, invalidProvider: raw !== null && !valid ? raw : null};
  }
  function workspaceSearch(search, patch) {
    const p = new URLSearchParams(search);
    if (has(patch, 'view')) p.set('view', views.has(patch.view) ? patch.view : 'providers');
    if (has(patch, 'providerId')) {
      if (patch.providerId === null) p.delete('provider');
      else if (Number.isSafeInteger(patch.providerId) && patch.providerId > 0) p.set('provider', String(patch.providerId));
      else throw new TypeError('providerId must be a positive integer or null');
    }
    const value = p.toString();
    return value ? '?' + value : '';
  }
  function touchedPatch(original, draft, fields) {
    const patch = {};
    for (const key of new Set(fields)) {
      if (has(draft, key) && draft[key] !== undefined && JSON.stringify(draft[key]) !== JSON.stringify(original[key])) patch[key] = copy(draft[key]);
    }
    return patch;
  }
  function moveItem(items, from, to) {
    const result = items.slice();
    if (!Number.isInteger(from) || !Number.isInteger(to) || from < 0 || to < 0 || from >= result.length || to >= result.length) return result;
    result.splice(to, 0, result.splice(from, 1)[0]);
    return result;
  }
  function initialModelSelection(catalogue, selectedKeys) {
    if (!catalogue || typeof catalogue.catalog_revision !== 'string' || !/^[0-9a-f]{64}$/.test(catalogue.catalog_revision) || !Array.isArray(catalogue.items)) throw new TypeError('A complete catalogue revision is required');
    const wanted = new Set(selectedKeys);
    const discovered = catalogue.items.filter(row => row.discovered === true && row.discovered_available === true);
    const keys = new Set(discovered.map(row => row.model_key));
    const changes = discovered.map(row => ({id: row.id, enabled: wanted.has(row.model_key)}));
    if (changes.length > 5000) throw new RangeError('Atomic model selection exceeds 5000 rows');
    return {revision: catalogue.catalog_revision, changes, missing: [...wanted].filter(key => !keys.has(key))};
  }
  function parseNullableOutput(value) {
    if (value === null) return null;
    const n = typeof value === 'string' && /^[1-9]\d*$/.test(value) ? Number(value) : value;
    if (typeof n !== 'number' || !Number.isInteger(n) || n < 1 || n > 1000000) throw new TypeError('Output tokens must be an integer from 1 to 1000000, or an explicit clear');
    return n;
  }
  function createDraftGuard({read, save, discard, confirm}) {
    let baseline = JSON.stringify(read()), pending = null;
    const isDirty = () => JSON.stringify(read()) !== baseline;
    const markSaved = () => { baseline = JSON.stringify(read()); };
    function leave() {
      if (!isDirty()) return Promise.resolve(true);
      if (pending) return pending;
      pending = (async () => {
        const action = await confirm();
        if (action === 'save') {
          if (await save() !== true) return false;
          markSaved();
          return true;
        }
        if (action === 'discard') { await discard(); markSaved(); return true; }
        return false;
      })().finally(() => { pending = null; });
      return pending;
    }
    return {isDirty, markSaved, leave};
  }
  return {optionKey, retainCurrent, filterOptions, readWorkspace, workspaceSearch,
    touchedPatch, moveItem, initialModelSelection, parseNullableOutput, createDraftGuard};
});
```

Run: `node --test tests/test_ai_settings_runtime.cjs`.
Expected: all six tests pass. Add the >5000-row rejection test and a draft-guard test where a rejected/false save keeps navigation blocked; both must pass before page integration.

- [ ] **7.3 Red/green: wire and test the actual shared picker component.** Add `AIPicker.createModelPicker(Vue)` to `ai_picker.js`; register it as `ai-model-picker` through `window.registerPageComponents` while preserving any existing page registration callback. The CommonJS export must expose the same factory used in the browser.

The picker factory's `AISettings` dependency is `require('./ai_settings.js')` in CommonJS and `window.AISettings` in the browser. Browser registration passes the existing global `Vue` to `createModelPicker`. The VM harness must expose loaded browser globals consistently; application page snippets below explicitly use `window.*`.

Use these concrete controller methods inside its `setup`:

```javascript
const open = Vue.ref(false), query = Vue.ref('');
const rows = Vue.computed(() => AISettings.filterOptions(
  AISettings.retainCurrent(props.options, props.current), query.value));
const choose = option => {
  if (props.disabled || !['ready', 'unverified'].includes(option.availability)) return false;
  emit('select', {...option});
  open.value = false;
  return true;
};
const show = () => { if (!props.disabled) { query.value = ''; open.value = true; } };
const close = () => { open.value = false; };
// The existing app-modal restores focus and traps Tab/Escape. Do not build a second modal stack.
```

The component template is an ordinary trigger button plus an `app-modal` containing an explicitly labelled filter input, a current-value status paragraph, and `ul[role=listbox]` with keyed `button[role=option]` rows. Interpolate provider name and model ID separately. Set `aria-selected`, `aria-disabled`, `aria-controls`, and a deterministic option ID derived from the row index, not an unescaped model key. Both click and Enter call `choose`; ArrowUp/ArrowDown move focus among enabled option buttons; Escape uses the existing modal close. Never add `autofocus` or focus the text input on touch devices; leaving initial focus on the modal/close button is valid for both device kinds. The manual legacy fixed-model input stays outside the picker and is explicitly labelled unverified.

The following tests exercise the shipped component controller (not a fake implementation of it). Real DOM semantics are checked in Task 8's browser fixture.

```javascript
const Picker = require('../src/pixiv_novel_sync/static/js/ai_picker.js');
test('picker opening/filtering/reloading emits nothing; selection emits exact identity', () => {
  const Vue = {ref: value => ({value}), computed: get => ({get value() { return get(); }})};
  const props = {id: 'fixture-model', label: 'Model', disabled: false,
    options: [option()], current: option({model_key: 'old', availability: 'unavailable'})};
  const events = [];
  const picker = Picker.createModelPicker(Vue).setup(props, {emit: (...args) => events.push(args)});
  picker.show();
  picker.query.value = 'same';
  props.options = [option({provider_id: 8})];
  assert.equal(events.length, 0);
  assert.equal(picker.rows.value[0].model_key, 'old');
  assert.equal(picker.choose(picker.rows.value[0]), false);
  assert.equal(picker.choose(props.options[0]), true);
  assert.equal(events[0][0], 'select');
  assert.equal(events[0][1].provider_id, 8);
  assert.equal(events[0][1].model_key, 'org/model/v1');
});
```

Run the new test before creating the component to observe `MODULE_NOT_FOUND`; run it again after implementation. Keep runtime option labels as text: include `<img onerror=...>` in a fixture label and verify it appears as literal text in the browser check.

- [ ] **7.4 Red: test the three-stage provider save with injected HTTP/SSE dependencies.** Use `createOnboarding` rather than calling a duplicate test-only controller. Register each fake endpoint explicitly and throw on every unexpected request.

```javascript
const Onboarding = require('../src/pixiv_novel_sync/static/js/ai_provider_onboarding.js');
test('sync failure retains the created ID and selections; retry never recreates Provider', async () => {
  const calls = [], states = [];
  let synced = false;
  const flow = Onboarding.createOnboarding({
    request: async (url, init) => {
      calls.push({url, method: init.method, body: init.body && JSON.parse(init.body)});
      if (url === '/api/dashboard/ai/providers') return {id: 17};
      if (url === '/api/dashboard/ai/providers/17/models/sync') return {operation_id: 'fixture-operation'};
      if (url === '/api/dashboard/ai/providers/17/models/enabled') return {};
      throw new Error('Unexpected request ' + url);
    },
    waitForSync: async () => synced ? {status: 'succeeded'} : {status: 'failed', error_message: 'fixture failure'},
    readCatalog: async () => ({catalog_revision: 'b'.repeat(64), items: [
      {id: 41, model_key: 'chosen', discovered: true, discovered_available: true},
      {id: 42, model_key: 'not-chosen', discovered: true, discovered_available: true},
    ]}),
    onChange: state => states.push(state),
  });
  await flow.save({id: 0, name: 'Fixture', api_key: 'FAKE-KEY-NOT-A-CREDENTIAL'}, ['chosen']);
  assert.equal(flow.getState().providerId, 17);
  assert.equal(flow.getState().phase, 'sync_failed');
  assert.equal(calls.filter(x => x.url.endsWith('/models/enabled')).length, 0);
  synced = true;
  await flow.retry();
  assert.equal(flow.getState().phase, 'applied');
  assert.equal(calls.filter(x => x.url === '/api/dashboard/ai/providers').length, 1);
  assert.deepEqual(calls.at(-1).body, {catalog_revision: 'b'.repeat(64), changes: [{id: 41, enabled: true}, {id: 42, enabled: false}]});
  assert.equal(JSON.stringify(states).includes('FAKE-KEY'), false);
});
```

Add cases using the same fixture to assert: no chosen keys -> only provider save; `needs_empty_confirmation` -> no enabled PUT; missing chosen key -> explicit `missing` warning; a confirmed empty catalogue with no eligible changes -> `selection_unavailable`, no empty PUT and no false applied message; `source=manual` remains untouched; >5000 changes -> no chunked PUT; 409 -> no automatic reapply; retry of an apply failure reads a fresh catalogue and waits for confirmation; closing/unmounting invalidates late replies; duplicate save clicks share one in-flight operation. The apply-error fixture must assert provider POST and sync POST counts do not increase while refreshing/reconfirming the failed apply stage.

Run: `node --test tests/test_ai_settings_runtime.cjs`.
Expected red: missing `ai_provider_onboarding.js`; once present, failures identify the incorrect request sequence or phase, not a regex mismatch.

- [ ] **7.5 Green: implement the in-memory onboarding controller and hook it into existing save/SSE functions.** Use this transition contract, with `onChange` receiving cloned public state only:

| Phase | Action / next state | Network permission |
|---|---|---|
| idle | `save(form, keys)` snapshots non-secret selection, enters saving | one provider POST |
| saving | confirmed `{id}` -> saved; clear the visible Key input | no repeated provider POST on later-stage retry |
| saved with empty keys | saved_only | no model-sync, no enabled PUT |
| saved with keys | syncing | start model-sync; retain operation ID |
| syncing | succeeded -> load full catalogue -> applying | full catalogue GET then one enabled PUT |
| full catalogue with no applicable rows | selection_unavailable | keep saved Provider and missing choices; no empty PUT |
| syncing | needs_empty_confirmation -> awaiting_empty_confirmation | wait for the existing explicit confirmation/cancel controls |
| syncing | failed/cancelled -> sync_failed | preserve ID/keys; retry may start a new sync operation |
| applying | confirmed response -> applied | only now report applied selection; list missing keys separately |
| applying | any failure -> apply_failed or conflict | preserve ID/keys; do not auto-replay the write |
| apply_failed/conflict | retry -> full catalogue GET -> confirm_revision | no sync/provider POST and no enabled PUT yet |
| confirm_revision | user confirms -> applying | one PUT with the newly read revision |
| any active phase | dispose | prevent later chained requests and UI writes; no automatic cancellation claim |

The core apply path is exactly:

```javascript
async function applySelection(plan, activeGeneration) {
  if (!isCurrent(activeGeneration)) return;
  if (!plan.changes.length) {
    publish({phase: 'selection_unavailable', missing: plan.missing, pending: false, error: ''});
    return;
  }
  publish({phase: 'applying', revision: plan.revision, missing: plan.missing, error: ''});
  await request(`/api/dashboard/ai/providers/${state.providerId}/models/enabled`, {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({catalog_revision: plan.revision, changes: plan.changes}),
  });
  if (!isCurrent(activeGeneration)) return;
  publish({phase: 'applied', pending: false, error: ''});
}
```

In that module, define `state` with only `phase`, `pending`, `providerId`, `operationId`, `selectedKeys`, `revision`, `missing`, and `error`; define `generation`, `disposed`, `pendingPromise`, private `reviewedPlan`, `request`, `publish`, and `isCurrent` in the factory closure. `publish` replaces/snapshots public state and invokes `onChange`; neither state nor emitted snapshots contain the request payload/Key. At each awaited boundary, call `isCurrent` **before** initiating the next request. `save/retry/confirmRevision` return the same `pendingPromise` while active and release it in `finally`; a terminal failed sync clears its operation ID so a retry can start a new operation. A stream that closes without a terminal event is not success: GET that operation and follow its actual status. `getState` returns a clone, `dispose` increments generation and forbids new operations.

On an ambiguous initial provider POST transport failure, report that creation was not confirmed and require the user to refresh providers/check for an existing instance before starting another create. Do not automatically repeat a non-idempotent provider POST. On an ambiguous enabled PUT result, the retry/reconfirmation path reads actual catalogue state; it does not assume no rows changed.

Integrate at these exact existing seams:
- `saveProvider` at models template line 991: keep the returned create ID and form visible; do not immediately call `resetProviderForm`/erase probe selections. Existing-provider PUT still uses its known form ID, not `saved.id`.
- `probeModels` at line 945: add an explicit checkbox per preview row; newly probed choices start unchecked, while `suggested` remains a separate informational badge. Snapshot provider ID/type/base URL/proxy and editor generation. Invalidate old probe results when that connection identity changes. Do not substitute a stored Key into the form. The backend remains responsible for checking stored-Key borrowing.
- `syncProviderModels`, `consumeModelSyncEvents`, and `confirmEmptyCatalog` at lines 536/556/583: return final operation status to onboarding instead of swallowing failure. Keep the event whitelist, CRLF/residual-frame parser, cancellation/empty confirmation, and operation-ID-only recovery. Recovery after page refresh reconnects the operation but never restores or applies stale selections.
- `loadProviderModels` at line 434: keep presentation search separate from `readCatalog`, which always requests an unfiltered full snapshot/revision. The current endpoint is unpaginated; its complete `items` must match `total`. Do not reuse the visible search results, only the first display page, or a provider card's collapsed slice. If a response is incomplete, stop initialization rather than silently disabling only one page. A read-back failure after a confirmed enabled PUT is a refresh warning, not permission to repeat the write.

```javascript
async function readCatalog(providerId) {
  const data = await aiApi(`/api/dashboard/ai/providers/${providerId}/models`);
  if (!data || !Array.isArray(data.items) || !Number.isInteger(data.total) ||
      data.items.length !== data.total || typeof data.catalog_revision !== 'string' ||
      !/^[0-9a-f]{64}$/.test(data.catalog_revision)) throw new Error('A complete committed catalogue is required before applying selections');
  return data;
}
```

Run the helper/controller tests green. Add VM-level save tests to prove the actual page calls this controller and does not retain the old reset-after-create path.

- [ ] **7.6 Red/green: integrate workspace, current value retention, draft protection, and CAS conflict recovery.** Preserve existing page entry methods so the R1-R4 ownership tests still exercise the real page, not a parallel component demo.

Use the shared API error shape by changing only the failure branch of `base.html`'s existing helper, then replace the model page's duplicate HTTP implementation with `const aiApi = (url, options) => window.aiApi.request(url, options)`:

```javascript
if (!response.ok || data.ok === false) {
  const error = new Error(window.errorText(data, response));
  error.status = response.status;
  error.data = data.data;
  throw error;
}
```

Workspace links remain ordinary browser navigation, not a new client router:

```javascript
const workspace = window.AISettings.readWorkspace(window.location.search);
const workspaceHref = patch => '/dashboard/settings/models' +
  window.AISettings.workspaceSearch(window.location.search, patch);
const navigateWorkspace = async patch => {
  try {
    if (!await providerDraftGuard.leave() || !await poolDraftGuard.leave()) return;
    window.location.assign(workspaceHref(patch));
  } catch (error) { showAiMessage(error.message || 'Save failed; edits are retained', 'error'); }
};
```

Create `providerDraftGuard`/`poolDraftGuard` with `createDraftGuard`; the `confirm` dependency opens the existing `app-modal` with explicit Save/Discard/Keep editing actions. Save must return true only after its write is confirmed; a failed or incomplete onboarding save is not permission to navigate. Use native `beforeunload` only while a draft or unapplied onboarding selection exists; remove the listener on unmount. Browser refresh/back/forward then use native history and the unsaved-edit warning without a bespoke history state machine. Do not persist drafts to implement this.

Keep `provider-editor`, `provider-name`, `provider-list`, `pool-editor`, `pool-editor-title`, `pool-name`, and `pool-list` IDs. Split existing markup into the three `view` panels; move the catalogue fragment, not its data source, into `dashboard_ai_model_catalog.html`. A requested missing provider displays a non-destructive not-found state and a clear-filter link; it must not choose the first provider or change a binding. Provider cards link to `view=models&provider=<id>`; the groups view filters to groups containing that provider (direct or existing fallback expansion) without modifying group contents.

For provider edit/copy/reset, introduce an allowlisted form initializer using `??` rather than `||` for numeric/boolean values, including `max_retries: provider.max_retries ?? 2`, `stream_enabled: provider.stream_enabled ?? true`, `proxy: provider.proxy ?? null`. Copy always sets `api_key: ''` and `id: 0`. Track touched fields on input/change; save the changed allowlist, omit an empty existing Key, and never copy unknown server metadata back as a mutation. The basic fields are name, protocol, address, Key, enabled; advanced fields remain timeout/retries/proxy/stream/default model/context limit.

Keep local form checking, probe, saved-catalogue sync, and short generation test as four separately labelled actions. Local checking reuses/extends `lintBaseUrl` and numeric/type checks without HTTP or DNS. Probe remains form-only; short generation uses the existing explicit `/providers/<id>/test` action and reports its actual returned model/timing/error rather than calling it a context-window verification. Mark displayed capability badges as catalogue declarations versus the current client's text/stream support; do not introduce Gemini/Responses/tools/schema controls.

Extend catalogue/manual edit forms at existing `manualModelForm`/`createManualModel` and the row editor with `manual_max_output_tokens`, showing the effective value and whether it comes from `manual_max_output_tokens` or `discovered_max_output_tokens`. Use `parseNullableOutput` for touched values. An explicit Reset to discovered value button sends `{manual_max_output_tokens:null}`; an untouched field is omitted, and an emptied text input is a visible validation error, not an implicit reset. Test create/update/reset independently and assert catalogue sync never overwrites the displayed manual override or enabled flag. Use the full catalogue to construct picker options: filter new selections by server `routable`, but retain the current disabled/unavailable item as a labelled row. A missing raw fixed model with an existing provider is shown as unverified, not silently treated as a newly disabled catalogue model.

Wire shared picker selections into group membership using `provider_model_id`; never infer it from label/key text. Keep native up/down buttons and add keyboard handlers that call the existing guarded `movePoolMember(index, direction)`; after move, focus the same member's button by stable model ID. Keep `poolEditorReady`, request generations, write ownership, and the disabled fieldset. Existing pool conflict handling at lines 867-870 must stop assigning the latest server response into the draft: retain a draft snapshot, fetch the conflicting server state separately, and offer explicit Reload server version or Review/reapply my changes. Reapply sets `expected_version` from the reviewed server state; it is never automatic. Metadata-success/member-failure must not be presented as a complete pool save.

Add VM tests with deferred responses for: old provider probe/catalogue success after changing provider; old pool read after selecting another group; dirty navigation Save failure; explicit discard; reordering with disabled/missing members visible; conflict retains draft and latest server version separately; successful writes followed by failed read-back remain non-retryable write successes; current selected model disappears; same display name across providers; valid 0/false round-trip. Retain and update the existing R1-R4 assertions rather than deleting them to accommodate the new UI.

- [ ] **7.7 Package and test the shipped files, not only their source text.** `dashboard_ai_controls.html` includes `ai_settings.js` then `ai_picker.js` through `/static/js/...`; model-only onboarding is loaded after them in the model page's `extra_scripts` block before the existing inline setup script. Task 8 reuses the shared include in the Agent page. Keep these unversioned scripts on Flask's existing revalidation behavior; do not append a fixed `?v=1` or other never-updated value, because the existing after-request hook makes any versioned static response immutable for a year.

Update package data and Tailwind inputs with these exact entries:

```toml
[tool.setuptools.package-data]
pixiv_novel_sync = ["templates/*.html", "static/css/*.css", "static/js/*.js"]
```

```javascript
files: [
  '../src/pixiv_novel_sync/templates/*.html',
  '../src/pixiv_novel_sync/static/js/*.js',
],
```

Extend the existing CSS scanner to scan the external JS source as both template strings and script literals; retain its existing HTML and dynamic-class checks:

```python
SCRIPTS = PACKAGE / "static" / "js"

def source_class_tokens(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    tokens = template_class_tokens(text)
    if path.suffix == ".js":
        tokens |= set(_script_tokens(text))
    return tokens
```

The generated-class/dynamic-class tests iterate `TEMPLATES.glob('*.html')` and `SCRIPTS.glob('*.js')`; add a synthetic external-JS template/`classList` fixture proving both are detected. Scoped `.ai-model-picker-*` CSS uses existing `--library-*` variables and bounded, scrollable modal content; do not hide page overflow to make a broken layout pass. Keep all Tailwind class names literal.

Extract `mount`, helper response constructors, `source`, and VM setup from `test_mobile_management_runtime.cjs:11` into `tests/helpers/management_vm.cjs`. The helper resolves only local Jinja includes and local `/static/js/ai_*.js` script sources, executes them in document order before the actual page inline script, captures page component registrations, and supplies `TextEncoder`, safe location navigation capture, event listener fakes, and the existing-style simulated watch/unmount callbacks needed by the logs page. Watch callbacks may be explicitly triggered by controller tests; they are not claimed to simulate Vue scheduling. Preserve the existing test's assertions about one real page setup script. Export/import the helper without running another test file through `require`; that would register tests twice. Do not rewrite application functions inside the test harness.

Add the pytest wrapper following the existing `test_mobile_management_runtime` pattern:

```python
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]

def test_ai_settings_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for AI settings runtime tests")
    result = subprocess.run(
        [node, "--test", str(ROOT / "tests" / "test_ai_settings_runtime.cjs")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

Run:

```powershell
node --test tests/test_ai_settings_runtime.cjs tests/test_mobile_management_runtime.cjs
python -m pytest -q tests/test_ai_settings_runtime.py tests/test_ai_model_ui.py tests/test_mobile_management.py
python scripts/build_tailwind_css.py
python scripts/build_tailwind_css.py --check
python -m pytest -q tests/test_tailwind_static_css.py
python -m pip wheel --no-deps --no-build-isolation --wheel-dir .superpowers/verification/ai-ui-wheel .
python -c "from pathlib import Path; from zipfile import ZipFile; p=next(Path('.superpowers/verification/ai-ui-wheel').glob('pixiv_novel_sync-*.whl')); z=ZipFile(p); names=set(z.namelist()); assert {'pixiv_novel_sync/static/js/ai_settings.js','pixiv_novel_sync/static/js/ai_picker.js','pixiv_novel_sync/static/js/ai_provider_onboarding.js','pixiv_novel_sync/templates/dashboard_ai_controls.html'} <= names"
git diff --check
```

Expected: runtime/source/CSS tests pass; generated CSS is current; the actual built wheel contains the scripts/templates. Wheel verification is an execution-time build, not an instruction to install anything while writing this plan. If an offline Tailwind binary is needed, pass the existing `--cli` option; never hand-edit the generated stylesheet.

**Task 7 review checkpoint:** Demonstrate from request traces that opening/filtering/picking/draft navigation does not mutate the backend, that onboarding performs its three stages without duplicate creates, and that confirmed writes versus read-back failures are distinguished. Show the unchanged old URLs and all preserved R1-R4 runtime regressions. Commit only Task 7's explicit paths when the root execution workflow authorizes commits; this planning turn makes no commit.

### Task 8: AI Task Binding, Explicit Preflight, Job Diagnostics, and UI Regression

**Depends on:** Task 7's shipped helpers/picker and Task 6's public preflight/job projections. Do not derive a private API shape from Python dataclass fields.

**Files:**
- Create: `src/pixiv_novel_sync/static/js/ai_preflight.js` (payload validation, bounded encoding, request lifecycle).
- Create: `src/pixiv_novel_sync/static/js/ai_job_diagnostics.js` (safe presentation projection, no token arithmetic).
- Create: `tests/test_ai_task_ui_runtime.cjs`, `tests/test_ai_task_ui_runtime.py`, `tests/test_ai_settings_browser.py`.
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings_agents.html:56` (existing editor and binding controls), `src/pixiv_novel_sync/templates/dashboard_settings_agents.html:167` (configuration preview), `src/pixiv_novel_sync/templates/dashboard_settings_agents.html:228` (page setup).
- Modify: `src/pixiv_novel_sync/templates/dashboard_logs.html:187` (AI detail/attempt diagnostics), `src/pixiv_novel_sync/templates/dashboard_logs.html:415` (continuation gating), `src/pixiv_novel_sync/templates/dashboard_logs.html:499` (detail request lifetime).
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings_nav.html` and `src/pixiv_novel_sync/templates/vue_components.html:37` only for the existing Agent page's user-facing label; retain URL, active-path behavior, and global components.
- Modify: `tests/test_ai_model_ui.py`, `tests/test_mobile_management.py`, `tests/test_mobile_management_runtime.cjs`, `tests/test_tailwind_static_css.py` where previously approved behavior is intentionally superseded.
- Generated by the existing build when class literals change: `src/pixiv_novel_sync/static/css/tailwind.css` and its hash in `src/pixiv_novel_sync/templates/base.html`.

**Consumes:**
- Existing Agent CRUD, seed, transactional batch binding, provider catalogue, health, and pool endpoints. Do not add unsupported task types or restore writing/NSFW controls.
- `GET /api/dashboard/ai/agents/<id>/candidates` remains a zero-network configuration expansion, separate from the input form below.
- `POST /api/dashboard/ai/agents/<id>/preflight` accepts a complete `messages` array and explicit optional `temperature`, `top_p`, `max_tokens`, and `allowed_provider_ids`. Its 16 MiB/256-message limits are server-enforced; client checks only make failures understandable before submission.
- The supplied messages are complete: the backend does **not** prepend `agent.system_prompt`. The UI may include the visibly displayed saved system prompt exactly once, followed by the user's test text. It must show what is included and never read a novel automatically.
- Unwrapped preflight result: `{version:2, planner_version, estimator_version, can_run, allowed_provider_ids, output_reserve, eligible, excluded, limits}`. Limits are `{max_candidate_attempts:16,max_network_requests:32,max_resolved_candidates:64,max_pool_nodes:8}`.
- Every eligible/excluded row has `source_index`, nullable `candidate_index`, `provider_id`, `provider_name`, `provider_model_id`, `model_key`, `display_name`, `pool_id`, `pool_name`, `pool_position`, `fallback_depth`, `context_window`, `agent_context_window`, `provider_context_window`, `model_context_window`, nullable `max_output_tokens`, `limiting_factors`, `input_tokens`, `input_budget`, `output_reserve`, `message_overhead`, `safety_margin`, `estimator_source`, `exact`, nullable `reason`, and `reason_message`. Unestimated facts are null; do not turn them into zero.
- Safe reason codes: `provider_not_allowed`, `provider_disabled`, `pool_disabled`, `member_disabled`, `model_disabled`, `model_unavailable`, `capability_missing`, `output_limit`, `context_overflow`, `estimation_failed`, `duplicate_candidate`.
- Existing job detail additionally exposes `validation_status` (legacy `unknown`), `can_continue:false`, `continue_unavailable_reason`, and `usage_summary` with `known_input_tokens`, `known_output_tokens`, `known_total_tokens`, `reported_requests`, `unreported_requests`, `complete`. Token totals are nullable; zero means reported zero. These are server summaries, not values to sum again in the browser.

**Produces:**

```javascript
AIPreflight.buildPreflightBody(messages, parameters); // validates and returns JSON text
AIPreflight.createPreflight({request, onChange});
// run(agentId, messages, parameters), reset(), dispose(), getState().
// State: {busy, result, error}; neither input messages nor form credentials persist here.
AIJobDiagnostics.projectJobDiagnostics(job);
// {validation_status, can_continue, continue_unavailable_reason,
//  usage_status: unknown | not_sent | reported | incomplete, usage_summary}.
```

- [ ] **8.1 Red: add canonical input and late-response tests against the future shipped module.** Create `tests/test_ai_task_ui_runtime.cjs` using Node's built-ins and the shared VM harness. Use the complete fixtures below, not a live provider.

```javascript
const assert = require('node:assert/strict');
const test = require('node:test');
const P = require('../src/pixiv_novel_sync/static/js/ai_preflight.js');
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
const plan = name => ({version: 2, planner_version: 'v2', estimator_version: 'fixture',
  can_run: true, allowed_provider_ids: [17], output_reserve: 100,
  eligible: [{source_index: 0, candidate_index: 0, provider_id: 17, provider_name: name,
    provider_model_id: 41, model_key: 'org/model/v1', display_name: name,
    pool_id: null, pool_name: null, pool_position: null, fallback_depth: 0,
    context_window: 1000000, agent_context_window: 1000000, provider_context_window: 1000000,
    model_context_window: 1000000, max_output_tokens: null, limiting_factors: ['agent', 'provider', 'model'],
    input_tokens: 100, input_budget: 999644, output_reserve: 100,
    message_overhead: 0, safety_margin: 256, estimator_source: 'fixture', exact: false,
    reason: null, reason_message: ''}], excluded: [],
  limits: {max_candidate_attempts: 16, max_network_requests: 32, max_resolved_candidates: 64, max_pool_nodes: 8}});

test('preflight serializes complete text unchanged, keeps zero, and refuses unsupported input', () => {
  const messages = [{role: 'system', content: 'Visible system'}, {role: 'user', content: '\u5c3e\u90e8\ud83d\ude00'}];
  assert.deepEqual(JSON.parse(P.buildPreflightBody(messages, {temperature: 0, allowed_provider_ids: []})),
    {messages, temperature: 0, allowed_provider_ids: []});
  assert.throws(() => P.buildPreflightBody([{role: 'tool', content: 'x'}], {}));
  assert.throws(() => P.buildPreflightBody([{role: 'user', content: {text: 'x'}}], {}));
  assert.throws(() => P.buildPreflightBody(messages, {tools: []}));
  assert.throws(() => P.buildPreflightBody(Array.from({length: 257}, () => messages[0]), {}));
  assert.throws(() => P.buildPreflightBody([{role: 'user', content: '\u4e2d'.repeat(6 * 1024 * 1024)}], {}));
});
test('an older response cannot replace the new Agent result or clear its busy state', async () => {
  const first = deferred(), second = deferred(), calls = [];
  const control = P.createPreflight({request: (url, options) => {
    calls.push({url, options});
    return calls.length === 1 ? first.promise : second.promise;
  }});
  const a = control.run(1, [{role: 'user', content: 'A'}], {});
  const b = control.run(2, [{role: 'user', content: 'B'}], {});
  first.resolve(plan('old'));
  await a;
  assert.equal(control.getState().busy, true);
  second.resolve(plan('new'));
  await b;
  assert.equal(control.getState().result.eligible[0].provider_name, 'new');
  assert.equal(calls[1].url, '/api/dashboard/ai/agents/2/preflight');
  assert.equal(calls[1].options.method, 'POST');
  assert.equal(calls[0].options.signal.aborted, true);
  assert.equal(Object.hasOwn(control.getState(), 'messages'), false);
});
test('reset/unmount invalidates late results; can_run false is a result, not a transport error', async () => {
  const pending = deferred();
  const control = P.createPreflight({request: () => pending.promise});
  const run = control.run(1, [{role: 'user', content: 'fixture'}], {});
  control.reset();
  pending.resolve({...plan('late'), can_run: false, eligible: []});
  await run;
  assert.equal(control.getState().result, null);
  const rejected = P.createPreflight({request: async () => ({...plan('excluded'), can_run: false, eligible: []})});
  await rejected.run(1, [{role: 'user', content: 'fixture'}], {});
  assert.equal(rejected.getState().result.can_run, false);
  assert.equal(rejected.getState().error, '');
  rejected.dispose();
  assert.equal(await rejected.run(1, [{role: 'user', content: 'fixture'}], {}), undefined);
});
```

Run: `node --test tests/test_ai_task_ui_runtime.cjs`.
Expected red: missing `ai_preflight.js`. Add separate cases for 400/413 safe error text, an aborted old failure after a new success, and exact byte-boundary bodies; do not equate JavaScript string length with UTF-8 bytes.

- [ ] **8.2 Green: implement the complete bounded preflight helper and lifecycle.** Wrap this core as `window.AIPreflight` / CommonJS using Task 7's wrapper pattern. It does not estimate tokens or call a provider.

```javascript
const allowedParameters = new Set(['temperature', 'top_p', 'max_tokens', 'allowed_provider_ids']);
function buildPreflightBody(messages, parameters = {}) {
  if (!Array.isArray(messages) || messages.length < 1 || messages.length > 256) throw new RangeError('Provide 1 to 256 text messages');
  for (const message of messages) {
    if (!message || !['system', 'user', 'assistant'].includes(message.role) || typeof message.content !== 'string' ||
        Object.keys(message).some(key => !['role', 'content'].includes(key))) throw new TypeError('Only complete system/user/assistant text messages are supported');
  }
  for (const [key, value] of Object.entries(parameters)) {
    if (!allowedParameters.has(key)) throw new TypeError('Unsupported preflight parameter: ' + key);
    if (key === 'allowed_provider_ids') {
      if (!Array.isArray(value) || value.some(id => !Number.isSafeInteger(id) || id < 1)) throw new TypeError('Provider restrictions must be positive integer IDs');
    } else if (typeof value !== 'number' || !Number.isFinite(value) ||
        (key === 'max_tokens' && (!Number.isInteger(value) || value < 1))) throw new TypeError('Generation parameters must be finite numbers; max_tokens must be a positive integer');
  }
  const body = JSON.stringify({messages, ...parameters});
  if (new TextEncoder().encode(body).byteLength > 16 * 1024 * 1024) throw new RangeError('Preflight request exceeds 16 MiB');
  return body;
}
function createPreflight({request, onChange = () => {}}) {
  let generation = 0, disposed = false, abort = null;
  let state = {busy: false, result: null, error: ''};
  const getState = () => ({...state});
  const publish = patch => { state = {...state, ...patch}; if (!disposed) onChange(getState()); };
  async function run(agentId, messages, parameters = {}) {
    if (disposed) return;
    const active = ++generation;
    if (abort) abort.abort();
    abort = new AbortController();
    const controller = abort;
    publish({busy: true, result: null, error: ''});
    try {
      if (!Number.isSafeInteger(agentId) || agentId < 1) throw new TypeError('Select a saved AI task');
      const body = buildPreflightBody(messages, parameters);
      const result = await request(`/api/dashboard/ai/agents/${agentId}/preflight`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body, signal: controller.signal,
      });
      if (disposed || active !== generation) return;
      if (!result || typeof result.can_run !== 'boolean' || !Array.isArray(result.eligible) || !Array.isArray(result.excluded)) throw new TypeError('Invalid preflight response');
      publish({result});
    } catch (error) {
      if (!disposed && active === generation && error.name !== 'AbortError') publish({error: error.message || 'Preflight failed'});
    } finally {
      if (!disposed && active === generation) publish({busy: false});
    }
  }
  function reset() {
    ++generation;
    if (abort) abort.abort();
    publish({busy: false, result: null, error: ''});
  }
  function dispose() { disposed = true; reset(); }
  return {run, reset, dispose, getState};
}
```

Export `buildPreflightBody` and `createPreflight`. Run the tests green before hooking the module into the Agent page. Client errors should be localized at the page presentation boundary, without logging the rejected messages or payload.

- [ ] **8.3 Red/green: consume the shared picker and preserve the Agent form contract.** In the Agent page, include `dashboard_ai_controls.html` and then `ai_preflight.js` before its existing `initVueApp` script. Replace the fixed-model and pool dropdowns at lines 66-80 with the shared picker; keep a separate explicitly unverified raw-model input for legacy fixed choices. Selection updates only `binding_type`, `provider_id`, `model`, or `model_pool_id` in the draft, not the server. Pool/member choices use stable IDs and keep unavailable current values visible.

Use a touched-field set and original allowlisted values for edits. The existing `editAgent` at line 462 and `saveAgent` at line 471 must no longer submit a newly defaulted full form when the user changes one field. The binding tuple is a logical unit: if any binding field changes, include its complete valid tuple and omit the irrelevant kind's fields; if no binding field is touched, omit the binding tuple entirely. Keep unknown advanced/legacy server fields out of the PUT rather than overwriting them. Example payload construction:

```javascript
const bindingFields = ['binding_type', 'provider_id', 'model', 'model_pool_id'];
const payload = window.AISettings.touchedPatch(agentOriginal, agentForm, agentTouched);
if (bindingFields.some(key => agentTouched.has(key))) {
  for (const key of bindingFields) delete payload[key];
  payload.binding_type = agentForm.binding_type;
  if (agentForm.binding_type === 'pool') payload.model_pool_id = agentForm.model_pool_id;
  else { payload.provider_id = agentForm.provider_id; payload.model = agentForm.model.trim() || null; }
}
```

Create still supplies the explicit creation defaults; edit uses `??` and does not turn `temperature=0`, `top_p=0`, or a stored false value into a default. Preserve seed and the existing transactional batch-binding preview/in-flight safeguards. Rename user-facing page headings/navigation to AI tasks without renaming Agent API/database identities. Existing non-main task types remain visible as historical/other uses, not newly runnable XP agents.

Extend the actual page VM regression with these assertions:

```javascript
const {mount, envelope} = require('./helpers/management_vm.cjs');
test('editing only a task name does not rewrite its hidden settings or fixed model', async () => {
  const m = mount('dashboard_settings_agents.html');
  const writes = [];
  m.replyWith((url, options) => {
    if (options.method === 'PUT') { writes.push(JSON.parse(options.body)); return envelope({}); }
    if (url === '/api/dashboard/ai/agents') return envelope([]);
    if (url.endsWith('/candidates')) return envelope({candidates: [], limits: {max_candidate_attempts: 16}});
    throw new Error('Unexpected request ' + url);
  });
  await m.state.editAgent({id: 4, name: 'Before', task_type: 'keyword_clean', binding_type: 'fixed',
    provider_id: 17, model: 'old/not-in-catalogue', temperature: 0, top_p: 0, enabled: false});
  m.state.agentForm.name = 'After';
  m.state.markAgentTouched('name');
  await m.state.saveAgent();
  assert.deepEqual(writes, [{name: 'After'}]);
});
```

Expose `markAgentTouched(field)` only as the real page's input/change handler; keep the allowlist internal. Update the VM tests' fake API replies for follow-up health reads as needed, never by accepting arbitrary URLs. Add a test that changing a binding deliberately submits a complete tuple while a missing/disabled current choice is not replaced by a catalogue refresh.

- [ ] **8.4 Wire an explicit input-preflight card without mutating configuration.** Keep the current `agent-candidate-chain` section as configuration expansion. Add a separate card with saved task selector, visibly included saved system prompt, user-entered test text, optional explicit generation overrides, run button, and clear button. Changing the selected task/test text/overrides resets the previous preflight result immediately; changing a saved Agent draft does not pretend to preview an unsaved backend binding. Require saving that binding before testing it, or clearly label that the selected saved configuration is being tested.

Use the actual helper from `setup`:

```javascript
const preflight = reactive({busy: false, result: null, error: ''});
const preflightText = ref('');
const preflightOverrideDraft = reactive({temperature: null, top_p: null, max_tokens: null});
const preflightOverrideTouched = reactive(new Set());
const preflightOverrides = () => Object.fromEntries(
  [...preflightOverrideTouched].map(key => [key, preflightOverrideDraft[key]]));
const preflightControl = window.AIPreflight.createPreflight({
  request: (url, options) => window.aiApi.request(url, options),
  onChange: state => Object.assign(preflight, state),
});
const runPreflight = async () => {
  const agent = agents.value.find(item => item.id === candidateAgentId.value);
  if (!agent || !preflightText.value) return;
  const messages = [];
  if (agent.system_prompt) messages.push({role: 'system', content: agent.system_prompt});
  messages.push({role: 'user', content: preflightText.value});
  await preflightControl.run(agent.id, messages, preflightOverrides());
};
onUnmounted(() => { preflightControl.dispose(); preflightText.value = ''; });
```

Only the three declared numeric override controls can add their keys to `preflightOverrideTouched`; clearing an override's explicit-use checkbox removes that key rather than submitting null. Watch the task ID/test text/override controls and call `preflightControl.reset()` on edits; include `watch` in the existing Vue destructuring. Do not silently add form defaults or unrequested providers. Render the saved system prompt visibly beside the test text so it is clear those exact two messages are submitted. No test button click calls `/test`, generation, `/jobs`, sync, or a novel endpoint: only the preflight POST is allowed.

Render eligible and excluded separately, including provider/model, original source position and frozen candidate index, nullable context/output limits, `limiting_factors`, input estimate, message overhead, output reserve, safety margin, estimator source/exactness, and safe exclusion reason. Use `value === null || value === undefined` for unknown display, not `value || '-'`. Do not compute a combined minimum window or recompute `can_run`. Explain that a successful local estimate is not a provider's 1M-token acceptance test.

The VM test for `runPreflight` must assert its one POST body contains the saved system message once, unchanged user CJK/emoji/tail marker once, no whole-book content, and no automatic model/Agent/provider write. Test a 1M eligible row with a 128k excluded row and assert both rows remain distinct; `candidate_index:null` on an excluded row is not converted into an attempt. Since these are VM controller tests, inspect returned state rather than claiming that a fake Vue object rendered the table.

- [ ] **8.5 Red/green: use the server's usage and validation diagnostics without client summation.** Create `ai_job_diagnostics.js` using this complete pure projection, wrapped for browser/CommonJS exports:

```javascript
function projectJobDiagnostics(job) {
  const raw = job && job.usage_summary;
  const integer = value => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
  const usage = raw ? {
    known_input_tokens: integer(raw.known_input_tokens),
    known_output_tokens: integer(raw.known_output_tokens),
    known_total_tokens: integer(raw.known_total_tokens),
    reported_requests: integer(raw.reported_requests),
    unreported_requests: integer(raw.unreported_requests),
    complete: raw.complete === true,
  } : null;
  const usage_status = !usage ? 'unknown'
    : usage.reported_requests === 0 && usage.unreported_requests === 0 ? 'not_sent'
    : usage.complete ? 'reported' : 'incomplete';
  return {
    validation_status: typeof job?.validation_status === 'string' ? job.validation_status : 'unknown',
    can_continue: job?.can_continue === true,
    continue_unavailable_reason: typeof job?.continue_unavailable_reason === 'string' ? job.continue_unavailable_reason : '',
    snapshot_version: job?.snapshot_version === 1 || job?.snapshot_version === 2 ? job.snapshot_version : null,
    invocation_plan: job?.invocation_plan && typeof job.invocation_plan === 'object' && !Array.isArray(job.invocation_plan) ? job.invocation_plan : null,
    plan_unavailable_reason: typeof job?.plan_unavailable_reason === 'string' ? job.plan_unavailable_reason : '',
    usage_status, usage_summary: usage,
  };
}
```

Use the following regression before writing the helper:

```javascript
const D = require('../src/pixiv_novel_sync/static/js/ai_job_diagnostics.js');
test('usage display preserves reported zero, unknown total, incompleteness, and no-network state', () => {
  const data = D.projectJobDiagnostics({validation_status: 'failed', can_continue: false,
    continue_unavailable_reason: 'No replayable input', usage_summary: {
      known_input_tokens: 0, known_output_tokens: 5, known_total_tokens: null,
      reported_requests: 1, unreported_requests: 1, complete: false,
    }});
  assert.equal(data.usage_summary.known_input_tokens, 0);
  assert.equal(data.usage_summary.known_total_tokens, null);
  assert.equal(data.usage_status, 'incomplete');
  assert.equal(data.can_continue, false);
  assert.equal(data.validation_status, 'failed');
  assert.equal(D.projectJobDiagnostics({}).validation_status, 'unknown');
  assert.equal(D.projectJobDiagnostics({}).usage_status, 'unknown');
  assert.equal(D.projectJobDiagnostics({usage_summary: {reported_requests: 0, unreported_requests: 0, complete: false}}).usage_status, 'not_sent');
});
```

Run: `node --test tests/test_ai_task_ui_runtime.cjs` red, implement, then green.

Load `ai_job_diagnostics.js` in the logs page before its inline script; add a computed `jobDiagnostics` from the currently selected AI job. Display reported input/output/total, reported/unreported request counts, validation status, and unavailable-continuation reason next to the existing attempts. Labels distinguish unknown, no requests sent, incomplete reporting (including partial reports), and complete reporting. Do not add a chart, cost estimate, token sum, or new persistence store.

The projection now carries only server-supplied `snapshot_version`, `invocation_plan`, and `plan_unavailable_reason`; it must not inspect private `candidate_snapshot` or estimate any budget.

For v2, render `invocation_plan.eligible`/`excluded` with the same budget columns as preflight, including `excluded_count/excluded_truncated`. For v1, retain the existing single PromptBudget panel. Do not render v2 as undefined `effective_context_window/input_budget` fields from a v1 object; malformed saved plans display `plan_unavailable_reason`, not fabricated zero budgets. Extend the VM test with one v1 job, a v2 safe-plan fixture, and an unavailable-plan fixture, asserting no local token calculation or private snapshot rendering occurs.

Gate the existing continuation computed/action **before** client candidate arithmetic: a job whose `can_continue !== true` has no continuation action. Keep the current phase-1 supported-task allowlist empty; this task must not enable the dormant write/continue path or create an endpoint. Guard the action entry point too, so calling an exposed method with `can_continue:false` sends no request. Extend detail loading/refresh with a selected-job generation, so an old detail response cannot overwrite a newly opened job or reopen a closed modal.

Add VM tests asserting that null usage remains unknown, explicit zero is shown as zero, failed/partial attempts do not imply zero cost, `can_continue:false` hides and rejects continuation, old job detail responses cannot replace new ones, and the actual page reads `usage_summary` instead of adding attempt usages. Keep all debug rendering whitelisted: new diagnostics never display config hashes, URLs, Key, fingerprint, original messages, or provider response bodies.

- [ ] **8.6 Package/runtime regression and real-browser fixture.** Add `test_ai_task_ui_runtime.py` using Task 7's pytest wrapper with the filename changed to `test_ai_task_ui_runtime.cjs`; run both CJS suites explicitly so a missing Node runtime is visible in the execution report. Keep Flask/Jinja render tests for both old URLs and the logs page; these test packaging/render syntax, not browser interaction.

Use the existing Python Playwright dependency for an **offline, opt-in** browser fixture in `tests/test_ai_settings_browser.py`. It must read an explicitly supplied local Vue 3.4.38 file from `AI_UI_VUE_JS`, verify its SHA-384 against the existing SRI attribute in `base.html`, and skip with a clear reason if no local file/browser exists. Do not fetch Vue, install a browser, launch the user's configured app, read their database, or contact a provider from the default tests.

The fixture loading contract is executable:

```python
import base64
import hashlib
import os
from pathlib import Path
import re
import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "pixiv_novel_sync" / "templates"

@pytest.fixture(scope="module")
def local_vue():
    configured = os.environ.get("AI_UI_VUE_JS")
    if not configured or not Path(configured).is_file():
        pytest.skip("AI_UI_VUE_JS must name an existing offline Vue 3.4.38 runtime")
    data = Path(configured).read_bytes()
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    sri = re.search(r'vue@3\.4\.38/[^" ]+"\s+integrity="sha384-([^" ]+)"', base)
    assert sri is not None
    assert base64.b64encode(hashlib.sha384(data).digest()).decode("ascii") == sri.group(1)
    return data.decode("utf-8")

@pytest.fixture(scope="module")
def browser(local_vue):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        try:
            instance = runtime.chromium.launch(headless=True)
        except playwright.Error as exc:
            pytest.skip("An existing Chromium installation is required: " + str(exc))
        yield instance
        instance.close()
```

Render the actual templates with the isolated Flask/Jinja setup already used by `tests/test_mobile_management.py` (`{[`/`]}` variable delimiters, `task_labels={}`). Route all browser network requests: serve the exact pinned Vue bytes and local packaged static assets, and serve explicit synthetic API envelopes for providers, full catalogues, pools/pool detail/attempts, Agents/candidates/preflight, health, and job detail. Reject all unknown/non-fixture requests. This supplies real Vue and shipped templates instead of substituting a homemade Vue renderer.

Required browser cases, parameterized at 375, 768, and 1440 CSS pixels:
- Both old page URLs render; `view=providers/models/groups`, valid provider filtering, missing provider error, reload, and Back/Forward preserve the correct state without configuration writes.
- Open the real picker; the search input is not focused automatically with `has_touch=True`. The dialog remains inside the viewport and its results scroll; Tab/Shift+Tab/Escape and focus return use the real shared modal.
- Same-label models from two providers are distinguishable; unavailable current choice remains visible after a synthetic catalogue refresh; click/Enter emits the exact provider/model identity only after explicit selection. A malicious-looking label is literal text with no injected image/HTML.
- The real group up/down controls work with keyboard and touch-sized buttons; the submitted `members` order and `expected_version` match the visible draft. Dirty switch/cancel/conflict retains the draft unless the user explicitly discards/reloads.
- Onboarding's saved/pending/sync-error/apply-conflict/success states correspond to the fixture request trace; no Key/form/selection enters browser storage. Only synthetic operation IDs are permitted by the existing recovery key prefix.
- Preflight submits the displayed complete messages once and creates no AI job; log diagnostics show unknown versus zero and do not reveal a Continue action when `can_continue:false`.

Use measurable browser assertions, not screenshots alone:

```python
assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
box = page.get_by_role("dialog").bounding_box()
assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= width + 1
assert not page.locator("input[type=search]").evaluate("el => el === document.activeElement")
page.get_by_role("dialog").press("Escape")
assert page.get_by_role("dialog").count() == 0
```

The fixture/API tables use only synthetic text and fake identifiers. Browser absence is a reported verification gap, not evidence of mobile success. Full on-device soft-keyboard behavior remains a manual acceptance item even when the desktop emulation checks pass.

Run:

```powershell
node --test tests/test_ai_settings_runtime.cjs tests/test_ai_task_ui_runtime.cjs tests/test_mobile_management_runtime.cjs
python -m pytest -q tests/test_ai_settings_runtime.py tests/test_ai_task_ui_runtime.py tests/test_ai_model_ui.py tests/test_mobile_management.py tests/test_mobile_shell.py
python -m pytest -q tests/test_ai_settings_browser.py -rs
python scripts/build_tailwind_css.py
python scripts/build_tailwind_css.py --check
python -m pytest -q tests/test_tailwind_static_css.py
python -m pip wheel --no-deps --no-build-isolation --wheel-dir .superpowers/verification/ai-ui-wheel .
python -c "from pathlib import Path; from zipfile import ZipFile; p=next(Path('.superpowers/verification/ai-ui-wheel').glob('pixiv_novel_sync-*.whl')); z=ZipFile(p); names=set(z.namelist()); assert {'pixiv_novel_sync/static/js/ai_preflight.js','pixiv_novel_sync/static/js/ai_job_diagnostics.js'} <= names"
python -m compileall -q src
git diff --check
```

Expected: runtime/source/package/CSS checks pass; browser checks pass when local prerequisites exist, otherwise their exact skips are listed as outstanding. The root final regression additionally runs `python -m pytest -q`; a source-only or skipped-browser run must not be described as completed real-device/mobile verification.

**Task 8 review checkpoint:** Show the actual preflight POST body and sanitized projection from fixture traces, without printing secrets or production text. Demonstrate server-authoritative usage/continuation and zero/unknown separation. Verify the old URLs, shared registration, wheel assets, and keyboard/mobile observations. Do not present phase-1 AI settings/runtime work as long-novel reading or XP analysis completion. Commit only explicit Task 8 paths if the root execution workflow authorizes it; this planning turn makes no commit.

## Final Acceptance And Release Boundary

上述八个任务合并为同一阶段交付；任务片段中的 focused pass 不等于整个阶段通过。控制器在所有任务审查通过后，对执行 worktree 运行：

```powershell
python -m pytest -q
node --test tests/test_ai_settings_runtime.cjs tests/test_ai_task_ui_runtime.cjs tests/test_mobile_management_runtime.cjs
python -m compileall -q src
python -m pyflakes src
python scripts/build_tailwind_css.py --check
git diff --check
```

`pyflakes` 若当前环境没有安装，记录不可用，不把 compileall 描述成同等的未定义名称检查；现有 `test_static_undefined_names.py` 仍在全量回归中运行。构建 wheel 后从 ZIP 内确认本轮全部新增 JS/partial/CSS 资产，不能只确认源码目录存在。

真实浏览器验收使用 Task 8 的本地 SRI 校验 Vue 与既有 Chromium，生成 375/768/1440 宽度的交互、溢出及键盘结果。缺少先决条件时保留具体 skip/缺口，不把 Node VM 或截图替代真实 Vue 交互证明。现网手机软键盘、Linux `nginx -t`、真实 1M 服务商准入/用量仍作为明确外部验收项，不自动使用私人小说或付费服务进行验证。

### Coverage Map

| Spec acceptance | Plan tasks | 验证落点 |
|---|---|---|
| A01 | 2, 7, 8 | 输出 null/缺省；0/false/Key/代理往返与 touched-field payload |
| A02 | 7, 8 | 两旧 URL、view/provider、历史导航与批量绑定 |
| A03 | 7, 8 | 失效当前项、同名区分、draft 与迟到响应 |
| A04 | 7, 8 | Node 控制逻辑与实际浏览器三宽度/焦点/键盘分别记录 |
| A05 | 2, 6, 7 | probe/sync 分工、SHA revision、一次性启停、冲突保留 |
| A06 | 2, 7 | 借 Key 时绑定地址/协议/代理、复制不带凭据 |
| A07 | 1, 3, 6 | 混合窗口、fixed 不跨 Provider、不绕过模型停用 |
| A08 | 1, 3, 6 | 每模型估算、跨模型 max/min 反例 |
| A09 | 1, 2, 3 | 输出预留/上下文瓶颈/消息开销/估算来源 |
| A10 | 1, 3, 6 | 完整尾部标记、CJK/emoji/多消息 |
| A11 | 3, 6, 8 | v1 金样、v2 指纹/配置校验、不可用 Continue |
| A12 | 3, 4 | 目标协议请求重新构建，消息与采样参数不污染 |
| A13 | 3, 4, 5 | 16/32/30min guard、取消、首正文锁定和内部残片 |
| A14 | 4, 5, 6, 8 | 网络 ordinal、崩溃未知、累计事件去重、空值显示 |
| A15 | 3, 4, 5 | 传输结束与业务校验分离，job/attempt 事实一致 |
| A16 | 5 | 真实 keyword_clean 消费 v2、兼容返回/降级/取消 |
| A17 | 6, 8 | 16 MiB/256、HTTP 状态、认证/CSRF、零网络预检 |
| A18 | 2, 3, 5 | 加列/重复迁移/外键、缓存/hash、留存清理 |
| A19 | 3, 5, 6 | DB 与 API 全路径检查 metadata_only，内存 partial 保留 |

### Documentation In The Last Implementation Task

在 Task 8 的最终实现提交中更新以下现有文件，基于执行 worktree 中内容作局部修改；集成回原 checkout 前必须保留并合并用户原来的文档改动：

- `docs/MODEL_ROUTING_GUIDE.md`：新任务逐候选准入、v1/v2、实际客户端协议/能力、用量未知、main Continue 不可用。
- `docs/frontend-api-contract.md`：preflight/原子 enabled 请求、状态码、大小限制、catalog_revision 和安全诊断字段。
- `docs/frontend-pages.md`：模型三视图、任务选模、draft/CAS/初始化阶段反馈及诊断入口。
- `README.md`：独立运行的 AI 配置与单次调用能力；仍仅关键词清洗接入，不能写成已实现整书 XP 分析。
- `docs/UNIFIED_PROJECT_REQUIREMENTS.md`：只调整本批准稿局部替代的 AI 设置/预算约束；第二阶段未实现项不得标成 DONE。
- `docs/STATUS.md`：实际 commit/测试/缺口，不推测已推送或已部署。记录浏览器及真实 Provider 外部验收与离线测试的区别。
- `docs/superpowers/specs/2026-10-31-ai-settings-and-invocation-design.md` 和本计划：只在实际验收满足后更新实现状态；未完成项保留原状态。

最后进行一次跨任务代码审查：交付从计划提交到实现 HEAD 的完整 diff 包、实际测试报告和账本中的未关闭事项；优先检查路由并发/预算、v1 兼容、消息留存、使用量去重与 UI/API 契约。重要问题修复后重跑对应测试并复审。

完成后按 finishing-a-development-branch 流程报告分支/工作区、变更与验证；未经用户明确要求不自动推送、开 PR、合并到 main 或部署。不能用设计/计划完成或预算/时间已用尽代替代码验收。

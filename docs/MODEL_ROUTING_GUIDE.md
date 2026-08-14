# AI 模型路由使用指南

本指南面向在 Web 界面（AI 设置页）配置 AI 模型的用户，解释模型目录（catalog）、模型池（pool）、Agent 绑定与失败转移（failover）的实际行为。所有描述以当前代码实现为准。

## 1. 核心概念

### 1.1 模型目录（Model Catalog）

每个 Provider 名下有一份"可用模型目录"，条目来自两种途径：

- **自动发现**：点击"同步模型"后，系统调用 Provider 的模型列表 API 拉取（见第 2 节）；
- **手动添加**：在 Provider 模型页手动录入一条模型。

目录条目的关键字段：

- `model_key`：上游模型标识（如 `gpt-4o`、`claude-sonnet-4-5`）。系统把它当作**不透明（opaque）标识**：原样保存，不做大小写折叠、不做 Unicode 归一化、不改写空白；只拒绝控制字符、空串和超长（最多 300 码点 / 1200 UTF-8 字节）。这意味着 `GPT-4o` 和 `gpt-4o` 是两个不同的模型。
- `display_name`：展示名，会做 NFC 归一化，最多 200 码点。
- `capabilities`（能力标签）：参与路由匹配的只有固定枚举 `streaming`、`json`、`vision`、`tools`、`long_context` 五种；上游返回的其他标签会保留但仅用于展示。
- `context_window`：模型级上下文窗口（256 ~ 10,000,000 之间的整数），会与 Provider 级窗口取较小值参与预算计算。
- `routable`：是否可被路由选中。不可路由的模型即使在池里也会被跳过。

### 1.2 模型池（Model Pool）

模型池是一组按顺序排列的目录模型（成员），可以跨 Provider。池有：

- **成员列表**：有序，`position` 决定尝试优先级；每个成员可单独启用/禁用；
- **后备池（fallback pool）**：一个池可以指向另一个池作为后备，形成链；
- **版本号（version）**：每次修改池或成员都会递增，用于乐观锁和运行中一致性校验；
- **启用开关**：禁用的池不能被 Agent 绑定。

### 1.3 Agent 绑定：fixed vs pool

每个 Agent（写作/审查等任务的执行单元）通过 `binding_type` 决定用哪个模型：

- **fixed（固定绑定）**：只用一个模型——Agent 上配置的 `model`，未配置时退回 Provider 的 `default_model`。候选列表恒为 1 项，**没有失败转移**：这个模型失败，任务就失败。固定模型不要求一定存在于目录中；但一旦 Agent 声明了 `required_capabilities`，该模型就必须存在于可路由目录中且具备全部所需能力，否则直接报错。
- **pool（池绑定）**：Agent 指向一个模型池，运行时把池链展开成一个有序候选列表（见第 3 节），失败时按顺序转移到下一个候选。

### 1.4 fallback 语义

池的后备链在**展开候选时**生效，不是"运行时再去查后备池"：

- 从绑定的根池开始，依次把每个池的启用成员按 `position` 追加进候选列表，然后沿 `fallback_pool_id` 进入下一个池；
- 禁用的池整体跳过（但仍占一层深度），继续走它的后备池；
- 同一 `(Provider, model_key)` 组合去重，只保留第一次出现；
- 链上出现循环立即报错；链最多 8 个池节点；展开后候选最多 64 个。

保存池配置时还有一致性约束：启用的池不能没有可用成员；启用池的后备池也必须是启用且非空的。

## 2. Provider 模型发现（model_sync）

### 2.1 流程

在 Provider 卡片上触发"同步模型"（`POST /api/dashboard/ai/providers/<id>/models/sync`）后：

1. 系统创建一个**同步 operation**（返回 `operation_id` 和 `generation`），后台 worker 异步执行（同时最多 2 个同步任务并行）；
2. worker 分页拉取 Provider 的模型列表，每翻一页更新进度（页数、已发现数量）；前端可通过 SSE（`/model-sync-operations/<id>/events`）实时看到 `started` / `page` 事件；
3. 拉取结果逐条归一化校验（见 1.1 的 model_key 规则），并计算整个结果集的 SHA-256 摘要 `result_digest`；
4. 终态之一：
   - `completed`：目录已更新（新增/更新条目，消失的条目按实现处理为不可路由或移除）；
   - `empty_confirmation_required`：见 2.2；
   - `failed`：带 `error_code`（`provider_error` / `deadline_exceeded` / `internal_error`）和已脱敏的错误信息（API Key 一律替换为 `[REDACTED]`）；
   - `cancelled`：用户取消。

其他约束：

- 整个同步硬性限时 **10 分钟**，超时以 `deadline_exceeded` 失败；
- 同步过程有心跳租约（15 秒一次），进程崩溃后遗留的 operation 会在下次启动时被自动回收（reconcile）为失败；
- 可随时取消：`DELETE /api/dashboard/ai/model-sync-operations/<operation_id>`；
- 分页未完整结束（上游中途断掉）不会写入半份目录，而是判定失败。

### 2.2 空目录确认（needs_empty_confirmation）

如果 Provider 返回了**权威的空列表**（确认上游确实一个模型都没有，而不是网络错误），同步不会直接清空你的目录，而是进入 `needs_empty_confirmation` 状态，SSE 发出 `empty_confirmation_required` 事件并附带 `generation` 与 `result_digest`。

此时你有两个选择：

- **确认清空**：`POST /api/dashboard/ai/model-sync-operations/<operation_id>/confirm-empty`，请求体必须且只能包含 `generation`（正整数）和 `result_digest`（64 位小写十六进制）。两者必须与该 operation 当前值完全一致——这是一个 CAS 校验，防止你基于过期结果误清目录。确认后目录被清空。
- **不确认**：什么都不做或取消，目录保持原样。

如果确认时 Provider 配置已变化或又跑了新一轮同步，`generation`/`result_digest` 对不上会返回冲突错误，重新同步一次即可。

## 3. 模型池的创建与匹配

### 3.1 创建与成员排序

在"模型池"页：

1. `POST /api/dashboard/ai/model-pools` 创建池（名称、启用状态、可选后备池）；
2. `PUT /api/dashboard/ai/model-pools/<pool_id>/members` **整体替换**成员列表。请求体只接受 `expected_version` 和 `members` 两个字段；每个成员只接受 `provider_model_id` 和 `enabled`。数组顺序即尝试顺序（position 1, 2, 3, ...）。

限制：单池最多 64 个成员；后备链展开后的有效候选最多 64 个；链深最多 8。

### 3.2 required_capabilities 匹配

Agent 可声明 `required_capabilities`（只能来自 1.1 的五个固定枚举，最多 32 项，不允许重复）。展开池候选时，逐个成员检查：

- 成员对应的目录模型必须**具备全部**所需能力标签，缺任何一项就被静默跳过（不报错，只是不进候选列表）；
- 成员被禁用、模型 `routable=false`、Provider 被禁用同样导致跳过或报错（Provider 禁用会直接报错）。

如果整条链展开后一个候选都不剩，任务启动即失败："模型池没有可用模型"。

## 4. PromptBudget 与失败转移（failover）

### 4.1 PromptBudget（输入预算）

任务启动前，路由器对整个候选快照计算统一预算：

```
有效上下文窗口 = min(Agent 的 context_window, 所有候选的 min(Provider 窗口, 模型窗口))
输入预算 = 有效窗口 - max_tokens(输出预留) - 消息开销(4×消息数+2) - 安全边际(256)
```

- 输入预算必须 > 0，否则直接报错"Prompt 输入预算必须大于 0"（通常是 `max_tokens` 相对窗口设得太大）；
- 输入 token 估算优先用 Provider 自带的估算器，全部候选都能估算时取最大值；否则退化为按 UTF-8 字节数估算；
- 估算超过预算时报错"Prompt 内容超过可用输入预算"，任务不会发起任何网络请求。

此外每个候选在轮到它之前还会做**单模型窗口检查**：如果该候选自己的 `context_window` 装不下当前 Prompt，该候选记一次 `context_overflow` 失败并直接切换到下一个候选。

### 4.2 候选快照与运行中一致性

任务开始时会把展开的候选列表固化成**候选快照**（含 Agent 配置摘要、池版本、Provider 配置摘要）。运行中和续跑（resume）时都会重新校验：Agent 被改过（`binding_version` 或配置哈希变化）、池版本变了、Provider 配置变了、任何一方被禁用或删除——都会以冲突终止，提示"…已变更，请重新开始任务"。这保证一次任务全程用的是同一份配置。

### 4.3 什么错误会转移、什么不会

按候选顺序逐个尝试，遇到失败时的分类处理：

**会转移到下一个候选（switch）：**

- Prompt 超过该候选的上下文窗口（`context_overflow`）；
- Provider 返回错误、空响应（`empty_response`）、流缺少正常结束标记（`incomplete_response`）、finish_reason 异常（如 `length`、`content_filter`）——前提是**主阶段还没有输出任何内容**；
- 错误范围（scope）是 `provider` 级（如鉴权失败、Provider 配置错误）时，转移的同时会**短路屏蔽该 Provider**：后续所有同 Provider 的候选直接跳过（skipped, `provider_short_circuit`），不再浪费尝试。

**不会转移、直接终结任务：**

- **主阶段（main）已经开始输出**后发生的任何错误：任务以 `partial`（部分输出）终结，保留已生成文本。原因是切换模型续写会造成文风/内容断裂；
- **取消**：用户取消或系统关闭，任务以 `cancelled` 终结；
- **路由预算耗尽**：单个任务最多 **16 次候选尝试**、**32 次网络请求**，且有路由截止时间；超限以 `route_budget_exhausted` 失败（若已有输出则为 partial）；
- 候选快照/配置冲突（见 4.2）；
- 所有候选都失败：以 `route_exhausted`（主阶段）或 `validation_failed`（校验阶段）失败。

## 5. 在任务日志中查看路由尝试（route attempts）

每一次候选尝试都会被持久化为一条 attempt 审计记录，包含：候选所属池的名称/版本/位置快照、Provider 与模型标识、Agent/Provider 配置哈希、结果状态（succeeded / failed / partial / cancelled）、错误分类（`error_category`）、finish_reason、是否已开始输出、耗时（latency_ms）。

查看途径：

- **任务日志页**：AI 任务详情中可看到该任务的 attempts 列表和逐次转移原因（progress 事件里的 `attempt` / `switch` / `skipped` 动作及 `reason` 字段）；
- **模型池维度**：`GET /api/dashboard/ai/model-pools/<pool_id>/attempts?limit=N`（默认 50，最多 200）查看某个池最近的路由尝试，用于评估池内模型的失败率和排序是否合理。

排查思路：`switch` 的 `reason` 就是失败分类；`skipped` + `provider_short_circuit` 说明前面某次失败被判定为 Provider 级问题（通常是 Key/网络/配置），应先修 Provider 而不是调整模型顺序。

## 6. 常见错误排查

### 6.1 409 版本冲突（乐观锁）

修改池（`PUT /model-pools/<id>`）、替换成员（`PUT /model-pools/<id>/members`）等写操作都要求携带 `expected_version`，且必须等于服务端当前版本。若期间有其他人（或另一个浏览器标签页）改过该池，版本已递增，请求会返回 409 冲突。

处理：重新 `GET /api/dashboard/ai/model-pools/<pool_id>` 拿到最新数据和 `version`，在最新状态上重做修改再提交。不要盲目重试旧请求体。

同类冲突还会出现在：空目录确认的 `generation`/`result_digest` 不匹配（重新同步一次）、运行中任务提示"配置已变更，请重新开始任务"（这是保护机制，重新发起任务即可）。

### 6.2 needs_empty_confirmation 一直不结束

这不是错误——同步在等你决定是否清空目录（见 2.2）。确认清空或放着不管都可以；不确认时目录不会被动过。

### 6.3 其他常见报错速查

| 报错信息 | 原因与处理 |
| --- | --- |
| "模型池后备链存在循环" | 后备池指回了链上的某个池，改掉 `fallback_pool_id` |
| "启用的模型池不能为空或没有可用成员" | 给池加至少一个启用且可路由的成员，或先禁用池 |
| "启用模型池的后备模型池也必须启用" | 后备池被禁用或为空，先修后备池 |
| "固定模型缺少必需能力：…" | fixed Agent 声明了 `required_capabilities`，但模型目录里该模型没有这些标签；同步目录或去掉能力要求 |
| "模型池没有可用模型" | 能力过滤/禁用/不可路由把候选筛空了，检查成员状态与能力标签 |
| "Prompt 输入预算必须大于 0" / "Prompt 内容超过可用输入预算" | `max_tokens` 相对上下文窗口过大，或输入太长；调小 `max_tokens`、增大窗口配置或缩短输入 |
| "候选尝试次数已达到 16 次上限" / "网络请求次数已达到 32 次上限" | 路由预算耗尽，通常意味着池里大量模型持续失败，先看 attempts 找共性原因 |
| `deadline_exceeded`（模型同步） | 同步超过 10 分钟，检查 Provider 网络/代理后重试 |
| "model_key 不能包含控制字符" 等校验错误 | 手动录入的模型标识不合法，按 1.1 的规则修正 |

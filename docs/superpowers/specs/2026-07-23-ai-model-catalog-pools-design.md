# AI 模型目录与模型池设计

> 状态：规格已确认，实施计划已建立，正在实施
> 日期：2026-07-23
> 依赖：现有 `AIWritingService`、Provider/Agent 配置、SQLite、SSE 流式输出
> 后续规格：[成人描写局部润色 Agent 设计](2026-07-23-adult-polish-agent-design.md)

## 1. 目标

为 AI 创作模块增加可维护的模型目录和模型池，使用户可以：

- 从 Provider 一次同步大量模型，不再逐个手填模型名；
- 建立“一级模型池”“二级模型池”“Grok 模型池”等任意命名的池；
- 调整池内模型顺序，并为一个池指定后备池；
- 让 Agent 绑定固定模型或模型池；
- 在请求尚未输出正文时，按顺序跨模型、跨 Provider 故障转移；
- 查看每次任务最终使用的 Provider、模型、模型池和失败尝试；
- 保留现有 Agent、Provider 和历史任务，不要求重新配置。

本设计不依赖外部 new-api 服务。系统只借鉴其“批量模型目录与路由编排”思路，继续使用当前本地 SQLite 和 Provider 配置。

## 2. 已确认的核心规则

1. 模型池使用有序策略，先尝试当前池成员，再进入后备池。
2. 只有在尚未产生正文 `delta` 时，才允许切换到下一个候选模型。
3. 一旦已经产生正文，后续错误必须保留部分结果并结束任务，不得自动换模型继续拼接。
4. Provider 自身现有的重试及“流式转非流式”逻辑继续保留；模型池切换发生在单个 Provider 调用彻底失败之后。
5. 模型同步失败不得清空上次成功目录。
6. 上游模型消失时只标记为不可用，不直接删除被模型池引用的记录。
7. 所有 AI 生成路径必须经过同一个模型解析与执行入口，不能只改自动写作页面。
8. API Key 继续加密保存、永不回显；模型同步请求必须复用现有 SSRF、DNS 固定、禁重定向和脱敏机制。

## 3. 现有实现与缺口

当前代码已经具备：

- `openai_compatible`、`anthropic`、`xai` 三类 Provider；
- Provider 默认模型；
- Agent 绑定单个 `provider_id`，可填写固定 `model`；
- 同一 Provider 内的请求重试和流式转非流式降级；
- Provider 连接缓存和 API Key 加密；
- `ai_providers.available_models_json` 预留字段。

当前缺失：

- Provider 模型发现接口；
- 结构化模型目录、同步时间和同步错误；
- 模型池、成员顺序、后备池及循环校验；
- Agent 模型池绑定；
- 跨 Provider 故障转移；
- 实际模型尝试记录；
- 模型池管理页面与运行状态展示。

`available_models_json` 只有存储读写，没有运行时或页面使用。本次迁移后不再把它作为事实来源，只保留兼容读取，后续文档清理时标记为废弃字段。

## 4. 方案选择

### 4.1 采用方案：本地结构化模型目录与有序模型池

模型、池、成员和尝试记录使用独立表；Agent 通过明确的绑定类型选择固定模型或模型池。该方案支持外键、顺序调整、失败保留、后备池和可审计路由，最适合当前 SQLite 架构。

### 4.2 未采用：把模型池塞入 JSON

优点是迁移少、实现快；缺点是无法可靠维护成员引用、并发更新、唯一约束、排序和查询，后续健康状态也会变得难以维护。

### 4.3 未采用：直接依赖外部 new-api

优点是现成功能较多；缺点是新增部署、认证和网络故障链，且会把当前本地 Provider/API Key 管理拆成两套。用户仍可把 new-api 作为一个 `openai_compatible` Provider 接入，但本系统不强依赖它。

## 5. 数据模型

### 5.1 `ai_provider_models`

每行表示某个 Provider 下的一个模型。

| 字段 | 说明 |
|---|---|
| `id` | 本地主键 |
| `provider_id` | 所属 Provider，`ON DELETE CASCADE`；删除前由服务检查活跃引用 |
| `model_key` | 上游请求使用的模型标识 |
| `discovered` | 是否曾由上游发现 |
| `manual` | 是否由用户手工保留 |
| `discovered_available` | 最近一次完整同步时上游是否仍返回该模型 |
| `enabled` | 用户是否允许模型参与路由 |
| `discovered_display_name` | 上游显示名，可空 |
| `manual_display_name` | 人工显示名覆盖，可空 |
| `discovered_capabilities_json` | 上游能力标签 |
| `manual_capabilities_json` | 人工能力标签覆盖，可空 |
| `discovered_context_window` | 上游上下文窗口，可空 |
| `manual_context_window` | 人工上下文窗口覆盖，可空 |
| `discovered_metadata_json` | 保留上游非敏感元数据 |
| `last_seen_at` | 最近一次同步发现时间 |
| `created_at`、`updated_at` | 本地时间戳 |

唯一约束：`(provider_id, model_key)`。

页面返回的 `source` 是派生值：仅 `discovered=1` 为 `discovered`，仅 `manual=1` 为 `manual`，两者都为真时为 `both`。有效显示名、能力和上下文优先使用 `manual_*`，为空时再使用 `discovered_*`。同步只更新 `discovered_*` 和 `discovered_available`，永不覆盖人工字段或 `enabled`。

有效可路由条件为：`enabled=1 AND (manual=1 OR discovered_available=1)`。人工模型后来被上游发现时同时保留两类来源；上游后来消失只把 `discovered_available` 设为假，人工覆盖和值仍完整保留。

能力标签使用固定枚举（`streaming`、`json`、`vision`、`tools`、`long_context` 等），未知标签只作展示不参与路由；Agent 增加 `required_capabilities_json`，路由只选择有效能力集合覆盖全部必需标签的模型。池可以混合能力，但绑定需要的能力若没有任何成员满足，保存或启用 Agent 时在事务内拒绝；运行时再次过滤并记录 `missing_capability`。

模型目录不保存 API Key、请求正文或完整上游响应头。`metadata_json` 只由适配器从明确的字段白名单构造（`owned_by`、`capabilities`、`context_window`、`created` 等），不复制任意上游字段，也不依靠“看起来像密钥”的启发式过滤。上游 `model_key` 是 opaque 标识，必须原样保留并按原始 UTF-8 字节去重，只拒绝控制字符；不得对它做 NFC、大小写折叠或空白改写。显示名、能力标签和白名单元数据字符串才做 Unicode NFC 规范化。`model_key` 最多 300 个 Unicode 码点且最多 1200 个 UTF-8 字节，显示名最多 200 个码点且最多 800 个 UTF-8 字节，能力标签每项最多 64 个码点、最多 64 项，白名单元数据序列化后最多 8 KiB。超限或类型不符的字段使本页同步失败，不得静默截断。

### 5.2 `ai_model_pools`

| 字段 | 说明 |
|---|---|
| `id` | 主键 |
| `name` | 唯一名称，例如“一级模型池” |
| `description` | 中文说明 |
| `pool_kind` | `primary`、`secondary`、`grok`、`custom`，仅用于页面分类 |
| `fallback_pool_id` | 可选后备池，自引用外键 |
| `enabled` | 是否启用 |
| `version` | 单调递增配置版本，用于并发更新 CAS |
| `created_at`、`updated_at` | 时间戳 |

`pool_kind` 不参与硬编码路由。运行时只关心 Agent 绑定的池、成员顺序和 `fallback_pool_id`。因此用户也可以创建其他用途的自定义池。

保存时必须检查：

- 不能把池本身作为后备池；
- 后备链不能形成直接或间接循环；
- 后备链最大深度为 8 个池节点（根池计为 1）；
- 单个池最多 64 个成员，整条后备链展开并去重后最多 64 个有效候选；超过时保存/启用事务拒绝，不做静默截断；
- 被 Agent 或其他池引用的池不能删除或禁用；必须先在同一事务中解除全部引用，再执行删除/禁用。

允许先创建空的禁用池，以便随后添加成员；空池不能启用、不能被 Agent 绑定，也不能作为启用池的后备池。已绑定的池若被并发更新为空或禁用，更新事务必须返回 `409`；用户必须先通过 Agent/后备池接口解除全部引用，再单独禁用或清空，不允许留下“启用 Agent 指向不可路由池”的隐含 kill switch。

### 5.3 `ai_model_pool_members`

| 字段 | 说明 |
|---|---|
| `pool_id` | 所属池 |
| `provider_model_id` | 引用 `ai_provider_models.id` |
| `position` | 池内顺序，从 1 开始 |
| `enabled` | 是否参与路由 |
| `created_at`、`updated_at` | 时间戳 |

唯一约束：`(pool_id, provider_model_id)`；同一池内 `position` 唯一。

成员列表使用一次事务整体替换，避免拖动排序时产生重复位置或半成品。

创建/更新池、修改后备池和替换成员都必须在 `BEGIN IMMEDIATE` 内重读完整后备图并验证循环、深度、成员数、空池和引用约束。更新请求携带 `expected_version`；版本不匹配返回 `409`，不能静默覆盖另一个页面刚保存的排序。验证成功后成员替换与 `version = version + 1` 同事务提交。

### 5.4 `ai_agents` 绑定迁移

新增：

- `binding_type`：`fixed` 或 `pool`；
- `model_pool_id`：池绑定时必填；
- `required_capabilities_json`：可选的固定能力要求；
- 原 `provider_id`、`model` 继续用于固定绑定。

约束：

- `fixed`：`provider_id` 必须存在，`model` 可空；为空时使用 Provider 默认模型；
- `pool`：`model_pool_id` 必须存在，`provider_id`、`model` 不参与运行时解析；
- 旧 Agent 全部迁移为 `fixed`，ID、名称、Prompt 和参数不变。

旧 Agent 的 `required_capabilities_json` 默认空数组，因此未知手填模型保持兼容。只要用户为固定 Agent 配置了非空能力要求，最终模型就必须先存在于结构化目录并通过人工或发现能力字段证明满足要求；未知模型不得按猜测放行。

该列使用 `TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(required_capabilities_json))`，服务层再校验固定能力枚举、去重和最多 32 项。

数据库约束固定为：

```sql
CHECK (
  (binding_type = 'fixed'
   AND provider_id IS NOT NULL
   AND model_pool_id IS NULL)
  OR
  (binding_type = 'pool'
   AND provider_id IS NULL
   AND model IS NULL
   AND model_pool_id IS NOT NULL)
)
```

`provider_id` 和 `model_pool_id` 均使用 `ON DELETE RESTRICT`。池绑定不能保留一个无效的旧 Provider 引用，否则会错误阻止 Provider 删除。

SQLite 需要在单一事务中重建 `ai_agents` 表，以允许池绑定不依赖伪造的 Provider。迁移必须保留原始 ID 和时间戳，重建索引，并在提交前执行 `PRAGMA foreign_key_check`。

### 5.5 `ai_job_model_attempts`

每个候选模型调用记录一行：

- `job_id`；
- `attempt_index`；
- `pool_id`、`provider_id`、`provider_model_id`；
- `pool_version_snapshot`、`pool_position_snapshot`；
- `model_key`；
- `agent_config_hash`、`provider_config_hash`；
- `candidate_list_hash`；
- `stage`：`internal`、`main`、`validation`；
- `status`：`running`、`succeeded`、`failed`、`partial`、`cancelled`；
- `error_category`；
- `error_scope`：`model` 或 `provider`，用于审计限流/认证短路范围；
- 脱敏后的 `error_message`；
- `finish_reason`；
- `output_started`；
- `owner_token`、`lease_until`、`heartbeat_at`；
- `started_at`、`finished_at`、`latency_ms`。

`job_id` 明确引用 `ai_jobs.job_id` 文本唯一键，使用 `ON DELETE CASCADE`；唯一约束为 `(job_id, attempt_index)`。

`ai_jobs` 额外保存 `candidate_snapshot_json`：候选 Provider/模型、池 version、Agent/Provider 有效配置哈希和顺序快照，不含 API Key、Prompt 或正文，最多 256 KiB（覆盖 64 个最长模型标识并留有结构余量）；attempt 只保存相同的 `candidate_list_hash`，便于校验其属于该 job 快照。

历史尝试中的 `pool_id`、`provider_id`、`provider_model_id` 只保存当时的数字快照，不建立到当前配置表的外键；同时保存池名、Provider 名和模型标识快照。这样删除已不再使用的配置不会被历史日志永久阻止，历史展示也不会因名称修改而失真。

`ai_model_pool_members` 使用复合主键 `(pool_id, provider_model_id)`；`pool_id` 为 `ON DELETE CASCADE`，`provider_model_id` 为 `ON DELETE RESTRICT`，并有唯一约束 `(pool_id, position)`。`ai_model_pools.fallback_pool_id`、`ai_agents.model_pool_id` 均为 `ON DELETE RESTRICT`。

该表禁止保存 Prompt、正文、API Key 或完整请求体。`error_message` 最多 2000 字符，`finish_reason` 使用固定枚举，快照 JSON 最多 32 KiB。`ai_jobs.output_text` 继续保存现有任务输出；任务详情只增加路由摘要和尝试列表。

`ai_jobs` 同时正式支持终态 `partial`：写入 `finished_at`、参与三天日志清理、可在统一任务日志筛选，并且不能被误显示为仍在运行。

`ai_jobs.candidate_snapshot_json` 允许为空（固定绑定在首次解析后写入），长度最多 256 KiB；`candidate_snapshot_hash` 为其 canonical JSON 的 SHA-256，并在手工续接请求中强制匹配。

为 `ai_jobs` 增加 `next_attempt_index`、`owner_token`、`lease_until`、`heartbeat_at` 和 `stage`。每次创建尝试时在短 `BEGIN IMMEDIATE` 事务内读取并递增该值，用旧值作为 `attempt_index`，保证同一 job 的并行内部调用也不会重复编号。执行器每 15 秒续租，Provider 回调和终态收口都必须携带 owner，并使用 `WHERE status='running' AND owner_token=?` 的单调 CAS；`succeeded`、`failed`、`partial`、`cancelled` 终态不可互相覆盖。服务启动时，`fail_stale_ai_jobs()` 只回收租约已过期且 heartbeat 超过宽限期的 job，在同一事务内把对应 `running` attempt 标记为 `failed`，写入 `process_interrupted`、`finished_at`，再按阶段收口 job 状态；仍有有效租约的其他进程任务不得被误杀。

每个 job 设硬资源边界：展开后的候选最多 64 个，候选尝试最多 16 次，包含 Provider 内部重试的网络请求总数最多 32 次，总路由 deadline 由 Agent/系统配置取最小值且不超过 30 分钟。达到任一上限时停止新调用，按当前阶段写入 `route_budget_exhausted`，并发送可重试提示。取消信号必须传播到当前 Provider，迟到的 delta 不能改变已收口终态。

候选解析始终保留池顺序；先过滤禁用、重复和能力不匹配项，再从剩余序列依次调用，绝不随机截断。第 16 次实际候选尝试失败后，即使后面仍有成员也不再调用；页面必须在池摘要和任务进度中显示“每任务最多尝试 16 个候选”，并标出后备池可能受该上限影响。用户可通过调整顺序决定优先级。

### 5.6 Provider 同步状态

为 `ai_providers` 增加：

- `models_synced_at`：最近一次成功同步时间；
- `models_sync_attempted_at`：最近一次尝试时间；
- `models_sync_error`：最近错误的脱敏中文摘要，最多 2000 个 Unicode 码点且最多 8000 个 UTF-8 字节，去除控制字符。
- `models_sync_generation`：单调递增同步代次；
- `models_sync_owner`：当前同步租约随机标识；
- `models_sync_lease_until`：跨进程同步租约到期时间。

失败时只更新尝试时间和错误，不覆盖 `models_synced_at`，也不修改旧目录可用状态。

同步长任务使用 `ai_model_sync_operations` 保存最小状态：随机 `operation_id`、`provider_id` 快照、Provider 配置哈希、`owner_token`、`status`（`queued`/`running`/`needs_empty_confirmation`/`succeeded`/`failed`/`cancelled`）、页数、已发现数量、规范化结果摘要 `result_digest`、开始/结束时间、错误代码和 `generation`。该表不保存 API 响应正文；状态收口使用 owner/generation CAS，保留 3 天后清理。

模型目录、池配置和尝试快照统一执行长度与字符校验：池名称最多 100 个 Unicode 码点/400 个 UTF-8 字节，池说明最多 2000 个码点/8000 个 UTF-8 字节，Provider/Agent 名称最多 200 个码点/800 个 UTF-8 字节，快照字段沿用对应上限；所有用户输入拒绝 NUL 和其他控制字符。数据库字段长度约束与服务端校验同时存在，错误摘要和 JSON 序列化结果超限时整次写入失败，不截断后继续运行。

## 6. Provider 模型发现

### 6.1 统一接口

`AIProvider` 增加结构化结果类型和接口：

```python
@dataclass
class ModelListResult:
    models: list[dict[str, Any]]
    complete: bool
    empty_authoritative: bool
    pages: int
    result_digest: str
    partial_reason: str | None

def list_models(self) -> ModelListResult: ...
```

`complete=false`、`partial_reason` 非空或 `pages`/`result_digest` 不符合本规格时整次同步失败；`models=[]` 的合法性只由 `empty_authoritative` 和结构化信封共同决定，不能靠空列表长度推断。

同时把现有只支持 POST 的安全请求逻辑提取成可复用的 `_request(method, url, ...)`，GET 和 POST 共用：

- URL 校验；
- DNS 解析与 IP 固定；
- Host/SNI 校验；
- 禁止重定向；
- 代理配置；
- 超时；
- 响应体上限（解析 JSON 前限制总字节数）；
- 错误脱敏。

不得为 `/models` 另写裸 `requests.get()`。模型同步请求的单页响应体最多 4 MiB，单次同步累计最多 20 MiB；超过上限立即中止并保留旧目录，不能先把超大正文全部读入内存再解析。

### 6.2 Provider 适配

- `openai_compatible`：优先请求解析后的 `{base_url}/models`；兼容 `data: [{id: ...}]`。
- `xai`：使用 OpenAI 兼容格式，默认 `https://api.x.ai/v1/models`。
- `anthropic`：使用 Anthropic 对应模型列表端点与现有认证头；若某个兼容网关不支持，允许人工补录模型。

上游结果必须经过统一归一化：去除空 ID、按模型 ID 去重、限制完整结果最多 5000 个模型、限制单个 ID 长度 300 字符、只保留元数据白名单字段。响应必须先通过 Provider 适配器定义的结构化信封校验：HTTP 状态为 2xx、JSON 顶层结构正确、模型数组字段确实存在且为数组、分页字段类型正确。字段缺失、类型错误、数组元素不是对象或元素缺少合法 ID 都使本次同步失败，不能把解析结果当成空目录。

`result_digest` 对按原始 `model_key` 排序、字段白名单和规范化值组成的紧凑 canonical JSON 计算 SHA-256 小写十六进制；它是空确认和 operation CAS 的绑定摘要，不包含 API Key 或响应正文。

Provider 适配器必须完整消费分页：识别 `has_more`、`next`、`after`、`last_id` 等已支持格式，并设置最多 100 页和游标去重。只有明确到达最后一页才算完整同步。若上游仍声明有下一页却达到 5000 个模型/100 页、游标循环、某页解析失败或响应声明为 partial，本次同步整体失败并保留旧目录，不执行缺失模型标记。

空模型数组只有在结构化信封完整、分页明确结束且适配器声明该 Provider 的空目录具有权威性时才直接算成功响应；默认适配器不把空数组视为权威，不会把既有模型批量标记为不可用。非权威但结构合法的空结果把 operation 收口为 `needs_empty_confirmation`，保存该次 generation、Provider 配置哈希和空结果 digest。用户只能确认这个精确 operation；若其后发生新同步、Provider 配置变化、generation 不再最新或 digest 不匹配，确认返回 `409`。确认成功后才在 CAS 事务中执行缺失标记。任何“解析成 0 个模型但无法证明是合法空数组”的情况都按同步失败处理并保留旧目录。

### 6.3 同步事务

同步分两步：

1. 网络请求和解析在事务外完成；
2. 成功结果在一个 SQLite 写事务内 upsert。

成功时：

- 新模型插入；
- 已存在模型更新时间与元数据；
- 完整结果中的模型设置 `discovered=1`、`discovered_available=1` 并更新 `discovered_*`；
- 完整结果未返回的已发现模型设置 `discovered_available=0`；
- `manual` 和所有 `manual_*` 字段不受自动同步影响；
- 清空同步错误并更新成功时间。

失败时：

- 保留旧模型及 `available` 状态；
- 只记录脱敏错误和尝试时间；
- 返回明确中文错误。

同一 Provider 同时只允许一个有效同步任务。开始时在短事务内获取数据库租约：若未过期则返回“模型同步正在进行”；否则递增 `models_sync_generation`，写入随机 owner 和租约时间。网络分页期间定期续租。最终写入时使用 `generation + owner` 做 CAS；租约过期后由其他进程发起的新同步会使旧响应失效，旧请求必须丢弃结果，不能把新目录模型误标为不可用。进程内锁只用于减少本实例重复请求，数据库租约才是多进程正确性的依据。同步总时限为 10 分钟；取消或超时写入终态 `cancelled`/`failed`，不改旧目录。

`needs_empty_confirmation` 是非运行等待状态，进入该状态后立即释放网络租约；它不阻止后续新同步。新同步会递增 generation，使旧 operation 的确认自动返回 `409`。只有精确 operation、generation、Provider 配置哈希和 result digest 全部匹配时，确认事务才可重新取得短写锁并执行缺失标记。

operation 从 `queued` 到 `running` 必须由 worker 使用 owner/generation CAS 领取并开始 heartbeat。服务启动和周期对账器每分钟扫描：排队超过 5 分钟仍未领取的 operation 收口为 `failed/queue_timeout`；`running` 且 lease 与 heartbeat 都超过宽限期、或 owner/generation 已不匹配的 operation 收口为 `failed/process_interrupted`。这些转换同样是只允许从非终态进入终态的 CAS；迟到 worker 不能把已回收 operation 改回成功，旧目录保持不变。

## 7. 模型解析与故障转移

### 7.1 统一组件

新增独立组件 `ai/model_router.py`，职责仅包括：

- 将 Agent 解析为候选模型列表；
- 按池成员顺序和后备池链展开；
- 跳过禁用 Provider、无有效人工/上游来源的模型、`enabled=0` 模型和禁用池；
- 执行候选并记录尝试；
- 产生统一 SSE 进度事件；
- 应用“首个正文前可切换，首个正文后不得切换”的规则。

Provider 实现继续只负责一个具体 Provider 请求，不感知池和 Agent。

### 7.2 固定绑定

固定 Agent 解析为一个候选：

1. 读取 `provider_id`；
2. 优先使用 Agent 的 `model`；
3. 否则使用 Provider 的 `default_model`；
4. 两者都为空时，在任务创建前返回中文配置错误。

该路径保持旧行为，不因引入模型池而自动切换到其他 Provider。

### 7.3 池绑定

1. 展开 Agent 绑定池的启用成员；
2. 按 `position` 排序；
3. 当前池耗尽后进入 `fallback_pool_id`；
4. 使用访问集合阻止运行时循环；
5. 同一 `(provider_id, model_key)` 在整条链中只尝试一次；
6. 没有可用候选时，在调用前返回“模型池没有可用模型”。

### 7.4 流式切换规则

执行器维护 `output_started`：

- `metadata`、`progress` 不视为正文开始；
- 第一个非空 `delta` 将其设置为真；
- `output_started=False` 时调用失败：记录失败，发送中文切换进度，尝试下一个候选；
- `output_started=True` 时调用失败：记录 `partial`，保留已输出正文，结束任务并返回可重试提示；
- 用户取消、客户端断开或 `GeneratorExit`：记录取消，不切换；
- 所有候选失败：任务失败，返回脱敏汇总，不泄露上游响应中的密钥或正文。

Provider 只有报告正常终止原因 `stop`/`complete` 且输出满足当前任务格式时才算成功。`finish_reason=length`、`content_filter`、缺失终止标记或传输提前结束：若 `main.output_started=true` 则记为 `partial` 且不得故障转移；首个正文前则记为当前候选 `failed` 并可切换。`internal`/`validation` 阶段无论是否已有内部 delta 都不产生用户可用 partial，失败统一按阶段错误处理。

模型路由为每个 job 创建一个带 owner lease 的执行会话，并区分调用阶段：

- `internal`：智能摘要等不直接展示给用户的预处理，可独立选择和切换候选，不设置正文 pin；
- `main`：正文、规划、蒸馏结果等用户可见内容，共享一个 `output_started` 和 pinned candidate；
- `validation`：内容审查等后处理，不得改变已经生成的正文候选。

`validation` 阶段有独立的 attempt 和终态：审查成功后才允许主 job 收口为 `succeeded`；审查失败、超时或无法解析收口为 `failed`（固定 `validation_failed`/`review_unavailable` 原因），不改写主生成已经产生的正文，也不把它误记为 `partial`。只有 `main` 在首个正文 `delta` 后发生 Provider 错误才使用 `partial`。空的 Provider 响应若没有任何非空 `delta`，按 `empty_response` 失败处理，不能标记成功。

启动回收映射固定为：过期 `main` attempt 且 `output_started=1` 收口为 `partial/process_interrupted`；过期 `main` 且尚无正文、以及 `internal`/`validation` attempt 均收口为 `failed/process_interrupted`。取消已成功写入时优先保留 `cancelled`，任何迟到恢复或 Provider 回调都不能覆盖现有终态。

多批蒸馏、详细规划等任务的第一段 `main` 实质输出产生后，首个成功候选固定为后续批次模型；后来失败则任务标记 `partial`，不得换模型。现有把批次提示伪装成 `delta` 的路径必须改为 `progress`，进度文本不能触发正文开始。`internal` 阶段的成功或失败不会锁定 `main` 候选。

用户可在失败后手工选择“使用下一个模型继续”，调用 `POST /api/dashboard/ai/jobs/<job_id>/continue`，提交 `parent_job_id`、`idempotency_key`、`candidate_snapshot_hash` 和 `resume_candidate_index`。服务端只接受父 job 已终态、候选快照哈希仍匹配且索引指向父 job 之后的下一个未尝试候选；新任务使用父 job 保存的候选 Provider/模型/池 version 顺序，不重新从已变更的池头解析，也不会再次调用已尝试候选。池 version、Agent 或任何待尝试 Provider 的有效配置哈希不匹配时返回 `409`，要求用户重新确认范围。不属于自动故障转移；已有部分正文只能作为用户明确确认的只读上下文，新 job 不共享旧 job 的 owner lease 或终态。

### 7.5 Provider 级快速跳过

若错误明确属于认证失败、Provider 禁用或配置错误，同一次任务中不再尝试该 Provider 的其他模型，直接进入下一个 Provider。若收到 `429`、`Retry-After`、账户配额耗尽或错误明确标注为账户/Provider 级限流，同一次任务也立即对该 Provider 短路，避免把同一 Prompt 继续发送给其余模型；记录短路原因和截止时间后进入下一个 Provider。只有适配器明确标注为单模型级的网关错误、超时或“不支持该模型”才跳过当前候选。

连接建立失败、DNS/证书失败、未带模型范围的 5xx/网关超时、未知范围限流和其他无法判定范围的错误默认按 Provider 级短路，避免把同一 Prompt 重复发往同一故障端点。短路只作用于当前 job，跨任务的健康冷却留到第二阶段；第一阶段不引入复杂评分。

### 7.6 上下文窗口

池内候选可能具有不同上下文窗口。路由器在展开候选后创建不可变的 `PromptBudget` 快照，后续切换候选不得重新扩大输入。每个候选的有效上下文窗口取以下值的最小值：

- Agent 的 `context_window`；
- 候选模型已知的 `context_window`；
- 模型未知时对应 Provider 的 `context_window`。

`PromptBudget` 明确定义为：`input_budget = effective_context_window - output_reserve - message_overhead - safety_margin`。`output_reserve` 是本次请求最终确定的 `max_tokens`，不得在候选切换时改变；`message_overhead` 使用统一消息封装估算器；`safety_margin` 固定为 256 token。已知 tokenizer 的 Provider 使用其 tokenizer 计算输入 token；未知 tokenizer 使用 UTF-8 字节数加消息封装开销作为保守上界，并在发请求前再次核对，超出预算的候选直接记录为 `context_overflow`，不发送网络请求。所有候选共用最小 `input_budget`，上下文截断/摘要结果和预算快照写入执行会话，不能因切换模型而重新取回被截掉的内容。

上下文窗口、`max_tokens`、字段长度和预算均使用正整数上限校验（上下文窗口 256 至 10,000,000 token，`max_tokens` 1 至 1,000,000）；若无法得到有效窗口或 `input_budget <= 0`，任务在调用前返回中文配置错误。页面必须显示实际采用的保守预算和被跳过的候选原因。

## 8. 全调用链接入

下列路径必须改用统一执行器：

- 续写、改写、蒸馏、审计、摘要和关键词清洗；
- 创作向导与普通聊天；
- 全书规划、详细梗概、章节续写；
- 对话润色、心理润色、去 AI 味；
- 状态更新、伏笔回收及章节 Pipeline 内部调用；
- Provider 连接测试在指定固定模型上运行，不使用池切换。

内部智能摘要使用 `internal` 阶段，可以独立使用同一 Agent 的候选列表并在失败时切换。智能摘要输出不发送为用户正文，也不锁定 `main` 候选；全部失败时按现有规则降级为尾部截断上下文。

不允许保留某些旧路径直接调用：

```python
provider.stream_generate(...)
```

除 Provider 测试和统一路由组件外，业务服务不得直接选择 Provider/模型。

## 9. API 设计

### 9.1 模型目录

- `POST /api/dashboard/ai/providers/<id>/models/sync`：创建异步同步任务；成功返回 `202` 和 `operation_id`，同 Provider 已有有效任务时返回 `409` 及现有 operation；
- `GET /api/dashboard/ai/model-sync-operations/<operation_id>`：查询页数、模型数、状态和脱敏错误；
- `GET /api/dashboard/ai/model-sync-operations/<operation_id>/events`：SSE 进度；事件为 `started`、`page`、`empty_confirmation_required`、`completed`、`failed`、`cancelled`，空确认事件只含 operation ID、generation 和 result digest，不含上游正文；
- `DELETE /api/dashboard/ai/model-sync-operations/<operation_id>`：请求取消仍在运行的同步；
- `POST /api/dashboard/ai/model-sync-operations/<operation_id>/confirm-empty`：只确认该次 `needs_empty_confirmation` 空结果；过期 generation/config/digest 返回 `409`；
- `GET /api/dashboard/ai/providers/<id>/models`：列出模型，支持可用状态和搜索；
- `POST /api/dashboard/ai/providers/<id>/models`：人工补录；
- `PUT /api/dashboard/ai/provider-models/<id>`：修改人工显示名、人工能力/上下文覆盖和用户启用状态；
- `DELETE /api/dashboard/ai/provider-models/<id>`：移除未被池引用的人工保留标记。

`PUT` 只能修改 `enabled` 和 `manual_*` 字段；`discovered_available` 是同步事实，只读且不得由页面伪造。`DELETE` 对 `manual=1` 的模型清除人工标记：若该行从未被发现且无任何引用则删除行；若同时是 discovered 模型则保留发现记录。

### 9.2 模型池

- `GET /api/dashboard/ai/model-pools`；
- `POST /api/dashboard/ai/model-pools`；
- `GET /api/dashboard/ai/model-pools/<id>`；
- `PUT /api/dashboard/ai/model-pools/<id>`；
- `DELETE /api/dashboard/ai/model-pools/<id>`；
- `PUT /api/dashboard/ai/model-pools/<id>/members`：一次替换成员与顺序；
- `GET /api/dashboard/ai/model-pools/<id>/attempts`：查看最近尝试摘要。

所有写接口沿用 Dashboard 会话、CSRF 和统一 JSON 校验。错误文案使用中文。

### 9.3 Agent

现有 Agent CRUD 增加：

- `binding_type`；
- `model_pool_id`；
- `model_pool_name`；
- `required_capabilities_json`：固定能力枚举数组，服务端按白名单校验并在路由时硬过滤；
- 固定模式下的 `provider_id`、`model` 保持兼容。

前端不得同时提交固定模型和模型池作为有效绑定；服务端仍必须再次校验。

任务手工续接接口 `POST /api/dashboard/ai/jobs/<job_id>/continue` 只接受父 job 的候选快照哈希、下一个候选索引和幂等键；服务端从父快照派生新 job，并按上一节规则拒绝配置版本冲突、重复候选或越界索引。

## 10. 页面设计

### 10.1 Provider 设置

每个 Provider 卡片增加：

- “同步模型”按钮；
- 模型总数、可用数；
- 最近成功同步时间；
- 最近失败提示；
- 模型目录抽屉或折叠区；
- 人工补录入口。

模型接口和页面同时返回三个明确计数：`total`（目录行总数）、`discovered_available`（最近完整同步仍返回的行数）和 `routable`（满足模型 `enabled`、有效人工/上游来源且 Provider 启用的行数）；Agent 的能力要求属于运行时过滤，另行显示“适配当前 Agent 的候选数”。不再用一个含义不明的“可用数”混用三者。

同步期间按钮禁用，并使用 operation 状态/SSE 显示已完成页数和已发现数量；页面刷新后可通过 operation 查询恢复状态。失败、取消或 10 分钟总超时均保留旧数量，明确提示“旧目录仍可使用”。

### 10.2 模型池设置

设置页新增“模型池”页签，使用现有卡片设计语言：

- 左侧池列表；
- 右侧当前池配置；
- Provider 和模型搜索；
- 拖动或上移/下移调整顺序；
- 选择后备池；
- 清楚显示被哪些 Agent 引用；
- 保存前显示循环、空池或不可用成员错误。

第一阶段不做权重轮询、成本排序或复杂图形编排。

### 10.3 Agent 设置

Agent 表单增加绑定方式：

- 固定模型：选择 Provider 和模型目录；仍允许手填未发现模型；
- 模型池：选择一个启用池，显示成员数和后备链摘要。
- 能力要求：多选 `streaming`、`json`、`vision`、`tools`、`long_context` 等固定标签；旧 Agent 默认为空数组。

Agent 列表显示最终绑定摘要，例如“模型池：一级模型池 → 二级模型池”或“固定：xAI / grok-…”。

## 11. 第二阶段：健康状态与冷却

第一阶段稳定后增加：

- 连续失败计数；
- `last_success_at`、`last_failure_at`；
- 429、5xx、超时的短期冷却；
- 401/403 配置错误状态；
- 手工恢复；
- 后台定时同步；
- 池运行状态页面。

健康状态应记录在 `ai_provider_models`，让同一模型跨多个池共享状态。模型池成员只保存编排配置。

冷却只改变候选排序/跳过，不删除模型。所有成员都处于冷却时，允许选择最早结束冷却的候选做一次探测，避免永久不可用。

## 12. 并发、事务与安全

- Schema 迁移、池成员替换、模型目录同步落库必须使用 SQLite 事务；
- 网络请求不得持有 SQLite 写事务；
- 同一 Provider 模型同步使用进程内锁和数据库最终唯一约束；
- 池更新与任务解析使用一个稳定读快照；任务开始后使用已解析候选，不受页面同时改顺序影响；
- Provider 缓存仍按配置键复用，切换候选不重复创建相同 Provider 客户端；
- 日志和 API 错误必须经过现有密钥脱敏；
- 模型上游元数据和错误长度设上限，防止数据库膨胀；
- 不在任务尝试表保存正文、Prompt 或请求头；
- 禁止模型同步跟随 3xx 重定向。

模型池可能把同一 Prompt 依次发送给多个 Provider。池编辑页和 Agent 绑定摘要必须列出可能接收内容的 Provider，并明确提示跨 Provider 故障转移的隐私影响；只有实际开始尝试的 Provider 才会收到请求。

## 13. 兼容与迁移

迁移后必须满足：

- 所有现有 Agent 都是 `fixed`；
- 原 Agent 的 Provider、模型、Prompt、参数、启用状态和 ID 不变；
- 现有前端未刷新时提交旧格式仍可工作；
- 无模型目录时，固定 Agent 仍可使用手填模型；
- Provider 删除限制同时考虑固定 Agent、模型目录和池成员引用；
- 删除 Provider 时在一个事务内先检查固定 Agent 和池成员引用；存在引用则返回中文冲突错误，无引用则删除 Provider，并由 `ai_provider_models.provider_id ON DELETE CASCADE` 清理目录。历史 attempt 只保存快照且无配置外键，不阻止删除；
- Schema 降级只承诺不丢失原有 `fixed` Agent。存在 `pool` Agent 时，降级工具必须拒绝执行并提示先把它们转换为固定绑定；旧 Schema 无法无损表达模型池 Agent，不能声称可直接无损回滚；
- `available_models_json` 不再写入新同步结果；已有列表在迁移时一次性导入为 `manual` 目录项，避免未经实时同步就把旧数据误标为上游当前可用。

迁移契约按表明确列约束：`ai_provider_models.id`、`provider_id` 为 `INTEGER NOT NULL`，`model_key` 为 `TEXT NOT NULL CHECK(length(model_key)>0)`，来源/启用字段为 `INTEGER NOT NULL DEFAULT 0 CHECK (value IN (0,1))`，显示名/能力/上下文/元数据可空并受本规格上限，`(provider_id, model_key)` 唯一；`ai_model_pools.id` 为 `INTEGER NOT NULL`，`name` 为非空 `TEXT NOT NULL`（无空字符串默认），`pool_kind` 为固定枚举 CHECK，`fallback_pool_id` 可空 `ON DELETE RESTRICT`，`enabled` 默认 0，`version INTEGER NOT NULL DEFAULT 1 CHECK(version>0)`；`ai_model_pool_members` 使用 `(pool_id, provider_model_id)` 复合主键，`position INTEGER NOT NULL CHECK(position>0)` 且 `(pool_id, position)` 唯一；`ai_job_model_attempts.job_id` 为 `TEXT NOT NULL` 且仅外键到 `ai_jobs`，`attempt_index INTEGER NOT NULL CHECK(attempt_index>=0)`，快照字段按阶段可空，状态/阶段为固定枚举 CHECK；`ai_model_sync_operations` 使用随机 `operation_id TEXT PRIMARY KEY`，Provider 只保存数字快照不设阻止历史删除的外键，状态为固定枚举 CHECK；`ai_agents.binding_type` 默认 `fixed` 并使用互斥 CHECK，`required_capabilities_json` 默认 `[]`。布尔列统一使用 `INTEGER NOT NULL CHECK (value IN (0,1))`，时间列使用现有 UTC ISO 文本格式；所有外键在迁移结束前执行 `PRAGMA foreign_keys=ON` 和 `PRAGMA foreign_key_check`。必须建立 `(provider_id, model_key)`、`(pool_id, position)`、`(job_id, attempt_index)`、同步 operation 状态和 lease 的索引，并把池版本 CAS、Agent 绑定互斥 CHECK 写入 DDL，而不是只在服务层校验。

旧 `available_models_json` 仅接受 JSON 数组；每个元素必须是字符串或含合法 `id` 字符串的对象，按本规格长度/控制字符规则归一化，非法元素跳过并写入迁移告警，全部非法时保留空人工目录但不标为 discovered。迁移在单一事务中执行，任一步骤、约束或 `foreign_key_check` 失败都整体回滚并保持旧表可启动；不得部分创建“看似完成”的模型池表。

## 14. 测试要求

### 14.1 存储与迁移

- 旧库迁移保留 Agent ID 和配置；
- 模型唯一约束、池名称唯一约束；
- 后备池直接和间接循环拒绝；
- 成员顺序事务替换；
- 池 `expected_version` 冲突返回 409；
- 被引用池删除、清空或禁用返回 409，并覆盖并发更新；
- 被引用模型/池禁止删除；
- 迁移后 `foreign_key_check` 无错误。

### 14.2 模型同步

- 三类 Provider 成功解析；
- GET 请求复用安全 DNS 固定与禁重定向；
- 单页 4 MiB、单次 20 MiB 响应体上限在 JSON 解析前生效；
- 同步失败保留旧目录；
- 缺失模型标记不可用；
- 合法空目录默认保留旧目录，显式确认空目录才执行缺失标记；
- 响应信封缺失、字段类型错误或部分页失败时保留旧目录；
- 人工模型不受自动同步影响；
- `manual_*` 和 `enabled` 在同步后逐字段不变；
- 重复模型 upsert；
- 同步数量和字段长度上限；
- 错误不泄露 API Key。
- 合法空数组与非权威空数组策略；
- `needs_empty_confirmation` 释放租约、精确 operation/generation/digest 确认及旧确认 409；
- 异步 operation 的 202/409、状态查询、SSE 事件和取消；
- queued 超时、running 崩溃、租约/heartbeat 过期的对账收口及迟到 worker CAS；

### 14.3 路由

- 固定 Agent 行为不变；
- 池成员顺序；
- 后备池顺序；
- 跨 Provider 切换；
- 首个正文前失败会切换；
- 首个正文后失败绝不切换；
- 取消和断连不切换；
- Provider 认证失败跳过同 Provider 其他候选；
- Provider 级限流短路；
- 候选、尝试、网络请求、总时限硬上限；
- owner lease/heartbeat、启动回收和终态 CAS；
- pool/Agent/provider 配置版本快照可重建候选列表；
- 能力要求过滤和缺失能力失败；
- validation 阶段失败与 main partial 的终态区分；
- 手工“下一个模型继续”使用父候选快照、跳过已尝试候选，并在池/Agent 版本变化时返回 409；
- `finish_reason=length`、`content_filter`、缺结束标记和空响应的终态映射；
- 启动 stale 回收对 main/internal/validation 的阶段映射；
- 同步租约过期后的旧响应 CAS 丢弃、分页循环/上限不写 tombstone、operation 取消与迟到完成 CAS；
- `partial` 三天清理和终态竞争；
- Provider 级 `429`、`Retry-After` 和账户配额错误在同一 job 内短路该 Provider；
- 不同 tokenizer 的统一 PromptBudget、输出预留和上下文溢出不发起网络请求；
- 尝试记录状态、实际模型和脱敏错误正确；
- 所有生成服务路径都经过统一执行器。

### 14.4 API 与页面

- 模型同步、人工补录、池 CRUD、成员排序、Agent 绑定；
- CSRF 和中文错误；
- API Key 不回显；
- 页面同步失败仍展示旧目录；
- 固定/池表单互斥；
- `required_capabilities_json` 枚举校验、保存、清除和候选过滤；
- 后备链和引用摘要展示。

## 15. 验收标准

第一阶段完成的判定：

1. 用户可以从任一已配置 Provider 同步或人工补录模型；
2. 用户可以建立有序池及后备池，并绑定到 Agent；
3. 现有固定 Agent 无需修改即可继续运行；
4. 首个正文前的候选失败会按确定顺序切换；
5. 已产生正文后绝不自动换模型；
6. 任务详情能显示实际模型和所有尝试；
7. 同步失败不清空旧目录；
8. 三类 Provider、安全请求、数据库迁移和所有 AI 调用路径有自动测试；
9. 设置页面可完整管理模型目录、模型池和 Agent 绑定；
10. 全量测试通过且 README 不再声称旧实现已经支持跨 Provider fallback。

## 16. 实施顺序

1. **Schema 与模型目录**：新表、字段、旧库迁移、Provider 安全 GET、分页/租约/异步同步 operation 和目录 API；
2. **模型池与 Agent 绑定**：池 CRUD、成员排序、能力约束、后备图校验、版本 CAS 和 Agent 迁移；
3. **统一路由与任务状态**：PromptBudget、候选/请求预算、owner lease、阶段化终态、尝试日志和跨 Provider 规则；
4. **全调用链迁移与页面**：逐个迁移全部 AI 调用路径，再实现模型目录、模型池和 Agent 设置页面；
5. **验证与收尾**：竞态/安全/资源边界定向测试、全量测试和文档；成人润色规格必须在第 3 阶段接口与终态稳定后另立实施计划。

第二阶段健康状态、跨任务冷却和后台定时刷新另立实施计划。

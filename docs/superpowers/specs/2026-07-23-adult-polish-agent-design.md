# 成人描写局部润色 Agent 设计

> 状态：规格自审完成，等待用户书面确认后编写实施计划
> 日期：2026-07-23
> 前置依赖：[AI 模型目录与模型池设计](2026-07-23-ai-model-catalog-pools-design.md)

## 1. 目标

增加一个面向私人小说的“成人描写润色 Agent”，用于增强用户选中片段的情欲表达、成人对白和感官描写，同时最大限度保护原文的剧情、人物、事实、视角和结构。

该 Agent 可以绑定 Grok 模型池，也可以绑定任意固定模型或其他模型池。系统不把 `xai`、Grok 或成人能力写死在运行时代码中。

现有项目级风格滑块继续控制全局写作方向；新 Agent 只负责用户明确选择的局部片段，不自动重写整章。

## 2. 已确认原则

1. 只处理用户选中的目标片段；前后文只读，不进入输出替换范围。
2. 默认不改变人物关系、剧情事件、时间线、地点、叙事人称和视角。
3. 默认不新增角色、重大动作结果、怀孕、伤亡、关系确认等事实。
4. 模型输出先进入差异预览，用户明确点击应用后才写回章节。
5. 不直接覆盖整章，不作为章节 Pipeline 的默认步骤。
6. 目标章节在生成期间被其他操作修改时，应用必须拒绝并要求重新生成。
7. 已输出部分正文后模型失败，不自动切换模型继续拼接；成人润色的部分缓冲直接丢弃，不能查看或应用。
8. 角色必须明确为成年人；涉及未成年人或年龄不明确的性内容时拒绝执行。
9. 不允许对现实人物进行色情化处理。
10. 日志不保存额外的目标片段副本，只复用现有任务输出并记录哈希、长度和校验摘要。
11. 成人润色是流式进度、非流式候选：模型输出先在服务端缓冲，安全校验通过后才发送到浏览器。
12. `adult_safety_review` 是服务端拥有的不可编辑安全策略，不属于普通 Agent CRUD；本地确定性安全检查始终启用，任何一层缺失、变更或无法判断都按阻断处理。
13. 成人任务只允许已认证 Dashboard 会话访问；未启用认证的单用户/公开模式直接拒绝这些路由，不以可猜的任务 ID 作为授权依据。

## 3. 与现有风格控制的关系

项目已经具有：

- 情色露骨度；
- 抒情浓度；
- 节奏；
- 黑暗/压抑度；
- 粗俗/口语度；
- 风格标签和自定义要求。

职责划分：

- 项目滑块：写作前的全局风格约束，影响规划、续写和现有润色；
- 成人描写润色 Agent：章节生成后的局部重写；
- Agent `system_prompt`：人格、用词偏好、保真规则；
- 本次操作参数：对当前片段的临时强度和具体要求。

局部操作默认继承项目风格设置。用户可以为本次操作覆盖“露骨度、抒情、粗俗/口语度”，但不会回写项目全局设置。

## 4. 页面位置与交互

入口放在 AI 创作项目的章节详情区域，作为与“正文”“章节设置”“普通润色”“审计”同级的内容页签或工具面板，不放回页面最底部。

操作流程：

1. 用户选择章节；
2. 在当前章节正文中选择一个连续片段；
3. 页面显示只读前文、目标片段、只读后文；
4. 用户选择成人描写润色 Agent；
5. 用户调整本次强度和补充指令；
6. 服务端校验成年人声明、目标范围和章节版本；
7. 页面接收模型选择与生成进度，候选文本暂存在服务端内存；
8. 服务端完成安全、事实和结构校验；通过安全校验后，页面才接收候选并展示差异；
9. 用户选择放弃、重新生成、复制候选或应用；
10. 应用成功后写入 `ai_polish_applications` 应用记录，并刷新正文。

第一版只允许一个连续目标片段。多片段批量处理留到后续版本。

## 5. Agent 类型与内置模板

新增 `task_type=adult_polish`，中文标签“成人描写润色”。

同时新增内部 `task_type=adult_safety_review`，只判断候选是否涉及未成年人、年龄不明、现实人物或未确认的新人物，不评价成年人之间内容的露骨程度。它不是用户可编辑的普通 Agent：系统固定其 system policy、输出 JSON Schema、策略版本哈希和本地确定性规则，禁止用户修改 Prompt、Schema、任务类型、启停状态或删除；用户只能为它选择允许使用的固定模型/模型池。缺少该策略、策略哈希不匹配、审查失败或返回无法解析时，成人候选不得展示。审查 Agent 的 Provider/模型尝试同样写入快照，并纳入页面的跨 Provider 隐私确认。

内置 Agent 模板包括以下人格约束：

- 角色和剧情保真优先于辞藻；
- 不擅自推动片段之外的剧情；
- 不添加原文没有的关系、结果或设定；
- 遵守项目叙事人称和视角；
- 成人对白应符合角色身份和已有说话方式；
- 根据本次参数控制直接程度、抒情程度和口语程度；
- 只输出目标片段替换文本，不输出解释、标题、分析、Markdown 围栏或差异表。

内置模板不自动绑定 Provider。用户可以在 Agent 设置中把它绑定到 Grok 模型池、其他模型池或固定模型。

安全与事实审查策略本体作为服务端只读代码资源 `adult_safety_policy`、`adult_fact_guard_policy` 发布：每项包含固定 `policy_id`、策略文本、Prompt 模板、输出 Schema 版本和发布时预期哈希，数据库不保存可编辑策略正文。用户可选的 Provider/模型池只存入独立 `ai_adult_review_bindings` 表；专用绑定接口只能改路由字段，普通 Agent CRUD 无法命中策略或绑定。只有发布新代码/迁移时才能替换策略本体，并且替换后旧候选全部按策略不匹配处理。两项审查都使用统一路由的 `validation` 阶段和独立 attempt，不能静默退回写作 Agent。

## 6. 请求数据

流式生成请求至少包含：

```json
{
  "project_id": 1,
  "chapter_id": 2,
  "agent_id": 3,
  "target_start": 120,
  "target_end": 460,
  "chapter_content_hash": "sha256...",
  "target_text_hash": "sha256...",
  "chapter_revision": 17,
  "participant_character_ids": ["char_anna", "char_bob"],
  "adult_characters_confirmed": true,
  "intensity": {
    "explicitness": 80,
    "lyricism": 45,
    "vulgarity": 70
  },
  "locked_terms": ["固定称呼", "契约名称"],
  "instruction": "保持角色原有称呼和主导关系",
  "idempotency_key": "client-generated-opaque-key",
  "provider_scope_hash": "sha256-of-three-stage-routing-scopes"
}
```

服务端不得信任客户端提交的目标文本。它必须重新读取章节正文，按 `target_start/target_end` 截取，并核对章节哈希和目标哈希。

`chapter_revision` 是章节内容或相关可影响正文替换的元数据每次变更都单调递增的数据库版本；应用时必须同时匹配该版本和哈希，防止内容经过两次修改后回到相同文本（ABA）。章节正文哈希定义为原始 Python 字符串按 UTF-8 编码后的 SHA-256，保留原始换行和组合字符，不做 NFC、CRLF/LF 或空白归一化；目标片段哈希使用同一规则。项目事实、角色清单和安全策略哈希使用排序键、Unicode NFC 字符串、紧凑 JSON（UTF-8）后计算 SHA-256。所有哈希统一使用小写十六进制 64 字符表示。

`participant_character_ids` 必须明确列出目标片段中参与成人行为的全部角色，服务端根据当前角色档案解析并核对；代词、省略主语或无法唯一映射到已确认角色的参与者一律前置拒绝，不允许由模型猜测。`idempotency_key` 在当前会话作用域内唯一，重复提交返回同一个 job，不重复创建 Provider 调用。

`provider_scope_hash` 是用户在页面确认后得到的三阶段隐私范围摘要：写作 Agent、`adult_safety_review`、`adult_fact_guard` 各自的候选 Provider/模型池、后备链、池 version、绑定 version 和 Provider 配置哈希按固定键名排序后 canonical JSON（UTF-8）计算 SHA-256。服务端在同一一致读快照中重算并要求完全相等；不相等返回 `409`，在任何 Provider 调用前终止。该摘要写入 job/application 审计，实际尝试列表仍单独记录。

`target_start/target_end` 统一使用 Unicode 码点偏移，不使用 JavaScript UTF-16 单元或 UTF-8 字节偏移。前端必须把浏览器选择位置转换为码点索引；服务端按 Python 字符串索引验证。偏移基于章节 API 返回的原始正文，不允许前端先做换行或空白归一化。测试必须覆盖中文、emoji、组合字符和 CRLF/LF 差异。

范围约束：

- 目标片段最少 20 字，最多 12000 字；
- 前后文各最多 4000 字，只用于上下文；
- 自定义指令最多 1000 字；
- `locked_terms` 最多 64 项，每项 1 至 100 个 Unicode 码点；按原始码点去重并逐项精确匹配，禁止控制字符；每项必须能在目标片段、只读上下文或项目保护清单中找到，否则前置拒绝，避免把任意用户指令伪装成事实锁定项；
- 参与者最多 20 个稳定角色 ID；`idempotency_key` 为 16 至 128 个 ASCII 字符；
- 强度为 0-100；
- `target_start < target_end` 且必须落在当前章节正文范围内。

## 7. 上下文与 Prompt 构造

消息分为四部分：

1. 系统规则：Agent 人格、保真要求、输出格式和禁止修改项；
2. 项目事实：项目大纲、角色状态、世界观、风格设置及已确认成年人角色；
3. 只读上下文：目标前文和后文，使用明确边界标记；
4. 目标片段：唯一允许替换的文本。

上下文必须使用不可混淆的随机边界标识，避免正文中的普通标题被模型误认为指令。边界生成后先确认它没有出现在正文或用户指令中，冲突时重新生成。用户自定义指令放在独立区块，并再次声明其不能覆盖事实保护和输出范围。

模型只输出候选替换文本。服务端只允许去除恰好包住整个结果的一层 Markdown 围栏；出现解释前缀、多个候选区块或无法唯一识别正文时直接结构阻断，不做启发式删前缀、猜测或拼接。

## 8. 事实与结构保护

### 8.1 自动锁定项

服务端从项目与目标片段中构造保护清单：

- 项目角色名称和别名；
- 明确的日期、时间、年龄、数量和章节内数字；
- Markdown/HTML 标签、Pixiv 标记和占位符；
- 用户提交的 `locked_terms`；
- 项目状态中标记为不可改变的事实。

发送给写作模型前，所有已确认成年虚构角色名称和别名替换为 job 级随机、不可预测的占位符（例如带 128 位随机 nonce 的 `⟦ADULT_<nonce>_<n>⟧`），生成后先确认占位符不出现在章节、项目事实或用户指令中，冲突时重新生成；映射只保存在本次服务端内存中。目标或上下文出现未确认项目角色时前置拒绝。候选只能使用本 job 白名单内已有占位符，不能新增、删减或复用其他任务的占位符，校验通过后才还原名称。

占位符按完整 Unicode 码点序列匹配，禁止拆分、大小写/兼容字符变体、插入零宽字符或把同一 token 映射成多个角色；同一角色 token 可以在候选中重复出现，但其映射始终唯一。还原前后各扫描一次，任何多余 token、未知 token 或映射关系不一致都安全阻断。

不引入大型中文 NLP 依赖。一般措辞、段落或非保护事实无法可靠判断时可以产生警告；人物身份、年龄、参与者、怀孕、关系、同意状态、未成年人或现实人物无法可靠判断时必须阻断，不能降级为普通警告。

### 8.2 校验结果

生成完成后产生：

- `applicable`：是否允许应用；
- `warnings`：可人工判断的风险；
- `blocking_issues`：禁止应用的问题；
- `protected_terms_missing`；
- `paragraph_delta`；
- `length_ratio`；
- `perspective_warning`；
- `new_number_tokens`；
- `diff_summary`。

候选安全闸门必须同时通过：

1. 本地确定性检查：角色占位符白名单、未成年人/年龄词规则、新年龄数字、已知现实人物标记和未确认项目角色；
2. 内部 `adult_safety_review` Agent 结构化审查，必须返回可解析 JSON，并明确 `safe=true` 且不存在未成年人、年龄不明、现实人物或新增人物；
3. 两层任一失败、超时或无法判断都按安全阻断处理。

写作模型返回后，服务端先验证占位符完整性，再在仅存在于服务端的受控缓冲中还原规范角色名称；`adult_safety_review` 接收这份候选文本、规范化的参与者 `character_id` 列表、每个角色的 `age_years`/`fictional` 事实和允许的名称/别名白名单，不能只凭 nonce 猜测身份。审查结束后服务端再次扫描并确认名称只来自白名单、占位符没有拆分/变体/一 token 多角色映射，再决定是否持久化；还原文本和审查请求正文永不进入浏览器、SSE、任务日志或长期快照。

安全审查调用使用服务端内置且不可变的 policy 和 JSON Schema，不读取普通 Agent 的可编辑 `system_prompt`。服务启动时计算 `safety_policy_hash`；代码内预期哈希、持久化配置和运行时模板任一不一致就禁用成人润色并返回配置错误。审查输出只接受严格 Schema 中的枚举问题代码，不允许自由文本决定 `safe`。本地确定性检查不可关闭，安全审查也不能退回写作 Agent 或跳过。

安全闸门通过后还必须运行服务端拥有、不可由普通 Agent CRUD 修改的模型审查阶段 `adult_fact_guard`。它接收原目标、候选、规范参与者事实和项目保护清单，按固定 JSON Schema 报告年龄、怀孕、亲属/伴侣关系、同意状态、参与者和其他锁定事实是否变化；其 Prompt/Schema 哈希并入 `validator_policy_hash`，实际 Provider/模型写入独立 validation attempt 和 application 快照。任何保护事实变化、超时、解析失败或 `unknown` 都生成不可绕过的 `blocking_issue` 并把主 job 收口为 `failed/validation_failed`；只有纯措辞/排版等非保护差异可以降级为 warning。

第一版阻断条件：

- 输出为空；
- 只包含解释而无候选正文；
- 目标范围或章节哈希已变化；
- 关键占位符/标签丢失；
- 明确锁定词丢失；
- 出现未成年人或现实人物色情化风险；
- 参与者、年龄、怀孕、亲属/伴侣关系、同意状态或其他受保护事实新增、删除或改变；
- 出现未确认角色，或参与者无法与请求中的稳定角色 ID 一一对应；
- 输出长度小于原文 30% 或大于原文 300%；长度比例阻断不允许由普通 warning acknowledgment 绕过，第一版不提供放宽字段；
- 生成任务状态为 `partial`、`failed` 或 `cancelled`。

段落数量、纯排版变化、已确认角色的规范名称/已有别名之间切换，以及不涉及年龄、关系、怀孕、同意或身份事实的新数字可以产生警告；新人物、参与者变化和任何安全/事实变化一律阻断，不能降级为警告。

存在 warning 时页面必须显示逐项确认，应用请求携带 `warning_ack_hash`。该值将 `validation_hash`、`safety_policy_hash`、`validator_policy_hash` 和去重排序后的已确认 warning 代码组成固定键名对象，按本规格的 Unicode NFC、排序键、紧凑 JSON、UTF-8 规则序列化后计算 SHA-256；不使用字符串直接拼接。服务端在写锁内重新计算并要求完全相等。候选、校验结果或策略版本变化都会使旧确认失效；阻断项永远不能通过 acknowledgment 绕过。

## 9. 成年角色确认

项目设置增加可选的成人内容声明：

- `adult_content_enabled`；
- `adult_characters_confirmed`；
- `fictional_characters_confirmed`；
- `adult_characters_json`：已确认成年虚构角色列表，每项必须包含不可复用的稳定 `character_id`、对应角色档案 `character_revision` 和确认时间；名称、别名、`fictional`、`age_years` 与年龄依据从规范角色表读取，客户端不能在此 JSON 中另写一套事实；
- `adult_confirmation_revision`：每次启用、撤销或角色清单变化时单调递增；
- `adult_confirmation_updated_at`。

`adult_characters_json` 必须是合法数组，最多 100 个角色；`character_id` 是服务端生成的不可变 UUID 文本，客户端不能自选或修改，`character_revision` 为正整数，单项记录和整个 JSON 分别不超过 2 KiB/64 KiB。规范角色表中的名称最多 200 个 Unicode 码点，别名最多 32 个且每项最多 100 个码点。服务端对数组按 `character_id` 排序后生成哈希；重复/未知 ID、revision 不匹配或超限均拒绝保存。

角色身份与事实由规范化表 `ai_project_characters` 管理，而不是只靠 JSON：包含全局唯一 `character_id TEXT`、`project_id`、`revision`、规范名称/别名、`age_years`、年龄事实来源、`fictional`、`active` 和时间戳。`age_years` 可保存任意非负角色年龄，只有成人确认层要求 `>=18`；`fictional` 必须是显式布尔值。删除角色只设置 `active=0` 并保留 tombstone，原 ID 永不重新分配；名称、别名、年龄、年龄来源或虚构状态变化都在同一事务递增角色 revision 和项目确认 revision。`adult_characters_json` 只能引用当前项目中 active 的规范角色 ID，并保存确认时 revision 快照，不再复制另一套可独立编辑的年龄/虚构事实；项目整体删除时可以按明确删除语义级联清除角色及 tombstone。

每次生成仍必须由客户端提交确认和明确参与者 ID，服务端同时核对项目声明。仅有项目滑块的高露骨度不等同于成年人确认。角色名称、别名、年龄、虚构标记、角色档案 revision 或参与者清单任一变化都必须自动递增 `adult_confirmation_revision` 并使旧确认失效；删除角色后其 `character_id` 永不分配给其他角色。

如果角色没有稳定 ID 和当前 revision、没有明确的 `age_years >= 18`、没有确认相关角色均为虚构人物、项目没有结构化角色数据或目标片段出现未确认角色，服务端拒绝成人润色并提示先完善角色档案。第一版不通过联网搜索猜测现实身份，只接受项目内明确的虚构人物声明，并用请求中的参与者 ID 结合规范名称/别名精确匹配；代词、省略主语或无法唯一判断参与者时一律拒绝。

## 10. 生成、差异与应用

### 10.1 生成任务

新增流式接口：

```text
POST /api/dashboard/ai/polish/adult/stream
```

任务类型为 `adult_polish`，使用模型池设计中的统一路由和尝试日志。调用 Provider 前必须在一致读快照中完成资格前置校验：认证主体拥有 project/chapter，`chapter.project_id` 与请求一致，写作 Agent `enabled=1` 且 `task_type=adult_polish`，安全策略/事实保护策略哈希有效，`adult_safety_review` 与 `adult_fact_guard` 两个审查绑定都存在且可路由，项目启用成人内容，成年人确认与角色档案 revision 有效，参与者全部在成年虚构角色清单内，章节 revision、章节/目标哈希一致，且请求中的 `provider_scope_hash` 与当前三阶段路由范围完全一致。前置校验失败时不得调用任何 Provider。

SSE 至少包含：

- `metadata`：任务、章节、目标范围；
- `progress`：候选模型和故障转移状态；
- `validation`：结构与事实校验；
- `candidate`：校验后允许展示的完整候选文本；
- `done` 或 `error`。

Provider 的 `delta` 只进入服务端缓冲区，不直接转发浏览器。第一个非空 Provider `delta` 仍触发模型路由的 `output_started`，因此后续失败会标记 `partial` 且不切换模型；部分缓冲必须丢弃，不写入 `ai_jobs.output_text`，页面只收到失败说明，不能查看或复制未完成候选。

生成结束后先运行本地安全检查和内部安全审查 Agent，再运行事实/结构校验。Provider 必须报告明确的正常结束原因（`stop`/`complete`）；`finish_reason=length`、`content_filter`、缺少结束原因、传输异常或单个 delta/累计缓冲超限都视为未完成：若主生成已经有正文则 job 为 `partial`，否则为 `failed`，均清空缓冲且不得创建 application。候选缓冲区最多 36,000 个 Unicode 码点且最多 144,000 个 UTF-8 字节，按每个 delta 到达时增量检查；校验器异常或安全审查超时也 fail-closed，立即丢弃缓冲，不写入任何正文列。

- 安全阻断（未成年人、年龄不明、现实人物、引入未确认人物）：丢弃候选，不发送 `candidate`，不持久化候选正文，任务失败并只记录问题代码；
- 非安全的结构阻断：在同一个 `BEGIN IMMEDIATE` 事务中保存候选到 `ai_jobs.output_text`、创建 `applicable=false` 的待清理候选记录并用 CAS 将 job 收口为 `failed/validation_failed`；事务提交后才可以发送候选和警告供人工查看，应用接口仍拒绝；
- 全部通过：在同一个 `BEGIN IMMEDIATE` 事务中保存候选到 `ai_jobs.output_text`、创建 `applicable=true`、`applied_at=NULL` 的待应用记录并用 CAS 收口 job 为 `succeeded`；事务提交后才发送 `candidate`。

`ai_jobs` 使用单调终态状态机：`running -> failed|partial|cancelled|succeeded`，终态不可相互覆盖。只有主生成阶段在 `output_started=true` 后发生 Provider 错误时才是 `partial`；安全审查/事实校验失败属于 `failed`，错误代码区分 `safety_blocked`、`validation_failed` 和 `review_unavailable`，不把已完成主生成误报为 partial。每次终态写入都使用 `WHERE status='running' AND owner_token=?` 的 CAS；客户端断连只触发取消请求，不能覆盖已经提交候选或已经发送 `candidate` 的 `succeeded`。服务端保存 job owner/heartbeat，启动恢复只收回租约过期的 job。

写入 `ai_jobs.input_json` 的内容只包括项目/章节 ID、目标范围、章节 revision、哈希、参与者 ID、`provider_scope_hash`、强度、指令长度、指令哈希和幂等键哈希；不得保存目标原文、前后文、完整 Prompt 或自定义指令正文。重新生成必须创建新的 job，带 `parent_job_id` 和新的幂等键；只能引用旧 job 的失败原因与哈希，不能自动拼接或复用旧候选正文。

### 10.2 差异预览

页面使用字符或行级差异展示：

- 删除内容；
- 新增内容；
- 未变化上下文；
- 保护项警告；
- 字数和段落变化。

差异计算在本地或服务端使用现有标准库能力实现，不新增大型前端编辑器依赖。第一版不做多人协同编辑。

### 10.3 应用接口

```text
POST /api/dashboard/ai/polish/adult/<job_id>/apply
```

应用请求必须携带当前会话的 CSRF、`warning_ack_hash`（无 warning 时为明确的空值标记）和服务器签发的 job 访问凭证；服务端核对 job 的 `owner_scope` 与当前认证主体，不能仅凭可猜的 `<job_id>` 授权。生成和应用都必须在写锁内确认 `chapter.project_id` 与请求 project 一致、写作 Agent `enabled=1` 且 `task_type=adult_polish`、安全策略哈希仍匹配、审查绑定存在且至少有一个可路由候选，并核对当前主体对 project/chapter 的访问权。应用时再次校验：

- job 属于当前章节且状态成功；
- 候选通过阻断校验；
- 当前章节哈希仍等于生成时哈希；
- 当前章节 revision 仍等于生成时 revision；
- 当前目标范围文本哈希仍一致；
- 目标起止位置仍有效；
- 当前项目事实哈希、成年人确认 revision 和成年虚构角色清单哈希仍与生成时一致；
- 当前 `adult_safety_review` 与 `adult_fact_guard` 的 binding hash 仍与生成快照一致；任一绑定变化返回 `409` 并要求重新生成/重新确认 Provider 范围，apply 不得静默把旧候选发给新 Provider；
- job 尚未应用。
- warning acknowledgment 与当前 `validation_hash`、`safety_policy_hash`、`validator_policy_hash` 完全匹配（阻断项不允许确认绕过）。

应用在 `BEGIN IMMEDIATE` 事务内完成：先重读 application、job、章节、项目事实、成年人声明和当前认证归属，再执行全部哈希/revision 校验，然后替换目标范围、更新字数与时间并设置 `applied_at`，同时记录新的章节 revision 和哈希。所有校验必须发生在取得写锁之后，避免检查与更新之间的 TOCTOU；正文替换和 application 收口必须使用同一事务。应用同一事务还必须把章节摘要、状态记忆、审计结果、关键词/检索索引等由正文派生的缓存标记为过期或排入重建队列，禁止后续 AI 继续读取旧派生事实。`source_job_id` 唯一；重复调用若已应用则返回同一成功结果，不得重复插入候选文本。通用 job 清理必须先取得同一写锁并跳过有未应用 application 的 job；专用清理与通用清理共享这套顺序，不能产生孤儿 application。

应用元数据至少记录：`source_job_id`、Agent、实际模型、目标范围旧哈希、新哈希和应用时间，不额外复制整章正文。

## 11. 数据存储

优先复用 `ai_jobs` 保存候选输出。为 `ai_jobs` 增加持久化授权字段 `owner_scope`；它是认证主体的 HMAC，与可轮换的执行租约 `owner_token` 完全不同。`adult_polish` job 必须非空，通用 job 列表/详情/SSE/取消/清理接口都要按它过滤，禁止未匹配主体读取成人输出。新增轻量表 `ai_polish_applications`；它使用独立 `id` 主键，`source_job_id` 保存字符串副本但不建立到 `ai_jobs` 的外键，避免任务清理同时删除已经应用的审计记录：

| 字段 | 说明 |
|---|---|
| `id` | 独立主键 |
| `source_job_id` | 唯一任务 ID 字符串副本，不设外键，不是正文快照 |
| `owner_scope` | 认证主体作用域的不可逆 HMAC，用于授权核对，不保存原始会话标识 |
| `project_id`、`chapter_id` | 目标章节 |
| `target_start`、`target_end` | 生成时范围 |
| `chapter_revision_before` | 生成时章节单调 revision |
| `chapter_hash_before` | 章节乐观锁 |
| `target_hash_before` | 目标片段乐观锁 |
| `project_facts_hash` | 大纲、角色、状态和相关项目事实哈希 |
| `adult_confirmation_revision` | 生成时成年人确认 revision |
| `adult_characters_hash` | 生成时成年虚构角色清单哈希 |
| `participant_ids_hash` | 排序后的明确参与者稳定 ID 哈希 |
| `provider_scope_hash` | 用户确认的三阶段 Provider/池版本范围哈希 |
| `candidate_hash` | 候选哈希 |
| `applicable` | 是否通过全部阻断校验、允许应用 |
| `agent_id_snapshot`、`agent_name_snapshot`、`agent_config_hash` | 写作 Agent 快照 |
| `pool_id_snapshot`、`pool_name_snapshot` | 模型池快照，可空 |
| `provider_id_snapshot`、`provider_name_snapshot` | 实际 Provider 快照 |
| `model_key_snapshot` | 实际模型标识快照 |
| `reviewer_binding_hash`、`reviewer_provider_snapshot`、`reviewer_model_snapshot` | 实际安全审查配置与模型快照 |
| `safety_policy_hash`、`review_prompt_hash` | 不可编辑安全策略及实际审查 Prompt 模板哈希 |
| `fact_guard_binding_hash`、`fact_guard_provider_snapshot`、`fact_guard_model_snapshot` | 实际事实保护审查配置与模型快照 |
| `fact_guard_prompt_hash` | 不可编辑事实保护 Prompt/Schema 哈希 |
| `validator_policy_hash` | 事实/结构校验规则的不可比较构建哈希 |
| `validation_hash`、`warning_ack_hash` | 校验结果及用户确认快照 |
| `validation_json` | 只含问题代码、计数和哈希的校验摘要；禁止缺失词、diff 片段或任何正文 |
| `created_at` | 创建时间 |
| `applied_at` | 应用时间，可空 |
| `chapter_hash_after` | 应用后哈希，可空 |
| `chapter_revision_after` | 应用后章节 revision，可空 |

`ai_adult_review_bindings` 每种审查只保留一行：`review_kind TEXT PRIMARY KEY CHECK(review_kind IN ('safety','fact_guard'))`、`binding_type TEXT CHECK(binding_type IN ('fixed','pool'))`、可空的 `provider_id/model/model_pool_id`、系统写入且用户不可改的 `required_capabilities_json TEXT NOT NULL DEFAULT '["json"]'`、`enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1))`、`version INTEGER NOT NULL DEFAULT 1 CHECK(version>0)` 和更新时间。数据库 CHECK 要求禁用行的绑定字段全空；启用行的固定/池字段使用与 Agent 相同的互斥约束，并且路由候选必须满足策略的 `json` 能力。Provider/池外键均为 `ON DELETE RESTRICT`；专用 `PUT /api/dashboard/ai/adult-review-bindings/<review_kind>` 使用 `expected_version` CAS，只能编辑路由字段，不能改能力要求。策略文本、Schema 和 Prompt 不在该表，也不提供写接口。

迁移契约：项目新增 `adult_content_enabled`、`adult_characters_confirmed`、`fictional_characters_confirmed` 使用 `INTEGER NOT NULL DEFAULT 0 CHECK (value IN (0,1))`；创建 `ai_project_characters`（`character_id TEXT PRIMARY KEY`、`project_id INTEGER NOT NULL`、`revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0)`、规范名称/别名、`age_years INTEGER CHECK(age_years>=0)`、`age_basis TEXT`、`fictional INTEGER NOT NULL CHECK(fictional IN (0,1))`、`active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))`、时间戳），项目外键 `ON DELETE CASCADE`，并建立 `(project_id, active)` 索引；角色清单使用 `TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(adult_characters_json))`，`adult_confirmation_revision` 使用 `INTEGER NOT NULL DEFAULT 0`，更新时间使用现有 UTC 时间格式。章节新增单调 `chapter_revision INTEGER NOT NULL DEFAULT 0`，任何正文/相关元数据更新都在同一写事务递增。`ai_jobs.owner_scope` 对旧 job 可空，但增加 `CHECK(task_type <> 'adult_polish' OR owner_scope IS NOT NULL)` 和 `(owner_scope, created_at)` 索引；创建成人 job 时必须在同一事务写入。迁移创建两行默认禁用的 `ai_adult_review_bindings`，不自动选择 Provider；绑定完成前成人功能 fail-closed。`ai_polish_applications` 的 `id` 为 SQLite `INTEGER PRIMARY KEY`，`source_job_id`、owner、哈希和策略字段 `NOT NULL`，`applicable` 使用布尔 CHECK，`project_id/chapter_id` 分别引用当前项目/章节并 `ON DELETE CASCADE`，`source_job_id` 建唯一索引，`(owner_scope, source_job_id)`、`(project_id, chapter_id, created_at)` 建查询索引。删除项目或章节时级联删除其候选/审计元数据；删除 job 不级联删除已应用 application。

旧项目记录迁移为成人功能关闭、确认清单空、revision 0；旧角色数据不自动视为成年或虚构，必须由用户重新确认。无法解析的清单元素全部丢弃并记录迁移告警。迁移与 `ai_jobs` 新字段在同一事务执行，任一约束、外键检查或索引创建失败整体回滚；禁止留下可用成人 Agent 指向缺失安全策略的半迁移状态。

生成失败、取消、部分输出或安全阻断只保留不含候选正文的 `ai_jobs`，不创建 application。通过安全校验并产生可展示候选时，job 输出、application 和 job 终态必须在同一事务创建：结构阻断为 `applicable=false`，全部通过为 `applicable=true`。服务启动时对早期版本遗留的“有成人候选输出但无 application”孤儿记录 fail-closed：清空正文并标记失败，绝不自动补成可应用候选。

未应用的成功候选及其应用记录与 `ai_jobs` 一样保留 3 天。清理任务在一个事务内先删除过期且 `applied_at IS NULL` 的 application，再删除对应 `ai_jobs`；通用 `cleanup_ai_jobs` 必须调用同一清理服务并遵守相同锁顺序，不能直接绕过 application。只要项目和章节仍存在，已经应用的 application 长期保留哈希、写作/审查 Agent、Provider/模型快照和应用时间；对应 `ai_jobs.output_text` 仍按 3 天清理，不长期保存候选正文，也不会因没有外键而阻止任务清理。用户删除项目或章节时按迁移契约级联删除相关 application 和审计元数据，这是明确的数据删除语义，不与长期保留冲突。

`safety_policy_hash` 和 `validator_policy_hash` 是不可比较的构建哈希，不使用“大于/小于”判断。应用时只要当前任一策略哈希与生成时不相等，就必须先在写锁内重算当前三阶段 `provider_scope_hash`（包含池成员/后备链 version、Provider 配置哈希和 binding version），并要求它与 application 快照完全相等；不相等直接返回 `409`，不调用任何 Provider。范围相等且两个 review binding hash 未变化时，才可对仍在 `ai_jobs.output_text` 中的候选重新运行当前全部安全与事实校验并生成新的 `validation_hash`；无法重新校验、审查 Provider 不可用或出现新阻断时拒绝应用，旧 warning acknowledgment 同时失效。任一 review binding hash 变化不走自动重审，直接返回 `409`，避免绕过原 Provider 隐私确认。

## 12. 与章节 Pipeline 的关系

第一版：

- 不加入默认 Pipeline；
- 不支持整章自动成人润色；
- 不在批量章节 Pipeline 中自动运行；
- 只能由用户从章节详情主动发起并确认应用。

后续若增加 Pipeline 步骤，必须仍要求明确目标范围、成年人确认和差异应用，不得绕过本规格的保护。

## 13. 错误处理

- Agent 或模型池不可用：生成前返回中文配置错误；
- 所有候选首字前失败：任务失败，保留尝试摘要；
- 首字后失败：任务 `partial`，丢弃未完成候选且禁止应用；
- 主生成成功但安全审查/事实校验失败：任务 `failed`，使用 `safety_blocked`、`review_unavailable` 或 `validation_failed` 问题代码，不得标记为 `partial`，不发送候选；
- 章节并发修改：应用返回 `409`，原章节不变；
- 项目事实、成年人确认或角色清单变化：应用返回 `409`，必须重新生成；
- 安全阻断：只返回问题代码并丢弃候选；非安全结构阻断可返回候选和问题，但应用接口拒绝；
- 客户端断开：仅在 job 仍为 `running` 且尚未提交候选时才可通过 owner CAS 标记 `cancelled`；已进入 `partial`、`succeeded` 或已提交 application 的 job 不得被迟到断连回调覆盖；
- 重新生成：必须使用新的幂等键和 `parent_job_id`，旧 job 终态不可重开，旧候选不自动拼接；相同幂等键始终返回原 job；
- 候选缓冲超过上限、SSE 写入失败或校验器异常：fail-closed，清空缓冲并按阶段写入唯一终态；
- API Key、Prompt 和上下文不得出现在错误日志；
- 数据库更新失败：应用事务回滚，job 保持未应用，可重试。

## 14. 页面设计语言

使用现有小说库/AI 创作卡片、页签、按钮和消息样式：

- 不新增与现有页面冲突的颜色体系；
- 目标片段、只读上下文和候选结果使用明确标题；
- 阻断问题用红色，需确认警告用琥珀色，通过项用绿色；
- “应用到章节”是唯一主按钮，默认禁用直到校验成功；
- 成人内容入口只在项目启用成人内容且角色确认完成后显示可用状态；
- 安全审查/事实保护绑定使用独立设置区，显示不可编辑的策略哈希、固定 `json` 能力要求和可能使用的 Provider；未完成两项绑定时 fail-closed，不显示可调用状态；
- 页面文字使用中文，Provider、Agent、Grok、Prompt 等专有名词保留。

## 15. 安全与隐私

- 功能只在已登录 Dashboard 内使用，沿用 CSRF；若实例未启用 Dashboard 认证，所有成人生成、查看、复制、重试和应用路由返回 `403`，不能依赖“只有本机访问”的假设；
- 每个成人 job 保存认证主体的 `owner_scope`，创建、SSE 恢复、候选读取、复制、重新生成和应用都同时校验当前主体对 project、chapter、写作 Agent、审查绑定和 job/application 的访问权；随机 job ID 只是标识符，不是授权凭证；
- 成人接口响应统一设置 `Cache-Control: no-store`、`Pragma: no-cache` 和防索引响应头；SSE 恢复令牌使用服务器签名且绑定 owner/job；
- 不新增公开成人内容接口；
- 不把目标片段发送给模型池中未实际选择的 Provider；
- 候选会依次发送给配置的内部安全审查 Agent 和事实保护 Agent；页面必须同时列出写作 Agent、`adult_safety_review`、`adult_fact_guard` 及其后备池中可能实际使用的 Provider，用户对三个阶段的跨 Provider 发送范围确认后才能发起；
- 故障转移前必须明确记录即将尝试的 Provider，但不在页面显示 API Key；
- 任务日志不新增正文副本；
- 差异预览只存在当前用户会话和现有任务输出；
- 公开救援 API、油猴脚本和小说库接口不暴露 AI 创作正文；
- 服务端必须执行成年人和现实人物边界校验，不能只依赖前端隐藏按钮。

## 16. 测试要求

### 16.1 Prompt 与输入

- 目标范围、前后文边界正确；
- 只读上下文不会进入候选替换；
- 项目滑块和临时覆盖合并正确；
- 自定义指令不能覆盖保护规则；
- 目标长度、指令长度和范围校验。

### 16.2 成年角色边界

- 未启用成人内容时拒绝；
- 未确认成年人时拒绝；
- 目标出现未确认角色时拒绝；
- 角色缺少明确 `age_years >= 18` 时拒绝；
- 未成年人或现实人物风险时阻断；
- 资格前置失败时断言 Provider 从未被调用；
- 安全阻断候选不得出现在 SSE、`ai_jobs.output_text` 或日志；
- 本地规则或安全审查 Agent 失败/无法判断时阻断；
- 审查 Agent 接收还原后的服务端缓冲及 canonical 参与者年龄/虚构事实，新增实体或年龄替换会阻断；
- 安全策略 Prompt/Schema 被普通 Agent CRUD 修改、删除或哈希不匹配时 fail-closed；
- 事实保护策略不可由普通 Agent CRUD 修改，超时/unknown/策略哈希不匹配时阻断；
- 代词/省略主语无法解析为明确参与者 ID 时拒绝；
- 角色 ID 由服务端生成、跨项目不可重复；角色删除保留 tombstone 且旧 ID 不可复用，revision 变化使旧确认失效；
- 普通非成人润色不受影响。

### 16.3 模型与任务

- 固定模型和 Grok 池都可用；
- 首字前故障转移；
- 首字后失败标记 partial、候选不出 SSE 且禁止应用；
- 取消/断连不写回章节；
- 候选超出码点/字节缓冲上限时不落库；
- 单个超大 delta、`finish_reason=length`/`content_filter`、缺失正常结束标记均丢弃候选且不可应用；
- 主生成后的安全审查失败记为 `failed`，不与 `partial` 混淆；
- 终态 CAS、owner heartbeat、迟到回调和启动回收不会覆盖已结束 job；
- 相同幂等键不重复调用 Provider，重新生成拥有新的 parent/幂等键；
- 尝试日志不包含正文和 API Key。

### 16.4 校验与应用

- 锁定词、占位符、数字和段落变化；
- 长度比例阻断；
- 章节哈希变化返回 409；
- 目标文本变化返回 409；
- 项目事实哈希或成年人确认 revision 变化返回 409；
- 章节 revision ABA、角色档案 revision、参与者 ID 或安全/校验策略哈希变化返回 409；
- warning acknowledgment 绑定 validation/policy 哈希，旧确认不可复用；
- 任一安全/事实审查 binding hash 变化时 apply 返回 409，不向新 Provider 发送旧候选；
- 用户确认后写作池、后备链或任一审查绑定变化会使 `provider_scope_hash` 校验返回 409，且零 Provider 调用；
- 策略升级触发重审前也重新核对 `provider_scope_hash`；池成员、后备链或 Provider 配置变化时返回 409 且零 Provider 调用；
- 应用事务回滚；
- apply 幂等；
- 双并发 apply 只有一个事务执行正文替换；
- 候选输出、application 和 job 终态原子提交，崩溃恢复不产生孤儿记录；
- 只有授权 owner 能读取或应用 job；
- 通用 job 列表、详情、SSE、取消和清理接口不会跨 `owner_scope` 暴露成人 job；
- 写作、安全审查、事实保护三个阶段的 Provider 列表均在发起前显示并确认，三组实际模型快照完整；
- 只替换目标范围，前后文逐字不变；
- 应用后字数、更新时间和元数据正确。
- 应用后章节摘要、状态记忆、审计结果和检索索引被标记过期或排队重建。

### 16.5 页面

- 章节详情页签可访问；
- 未确认时按钮禁用并有中文说明；
- 流式进度、校验后候选、差异、警告、阻断和应用状态正确；
- 不在页面最底部展开章节工具；
- 不通过 `innerHTML` 拼接模型输出；
- 与小说库/AI 创作现有设计语言一致。

## 17. 验收标准

1. 用户能给“成人描写润色”Agent 绑定 Grok 池或固定模型；
2. 用户只能选择局部片段，不会误改整章；
3. 项目风格滑块可继承并允许本次覆盖；
4. 未成年人确认、现实人物和范围校验在服务端生效；
5. 候选必须经过差异和事实保护检查；
6. 用户确认前章节正文完全不变；
7. 章节并发修改不会被旧候选覆盖；
8. partial/failed/cancelled 结果无法应用；
9. 应用后前后文逐字不变，只替换目标范围；
10. 定向测试和全量测试通过，任务日志不泄露敏感正文副本或 API Key。
11. 安全审查策略由服务端固定且可审计，普通 Agent 设置无法绕过；所有成人任务读取和应用都经过认证主体校验。

## 18. 实施顺序

1. `adult_polish` 类型、Prompt 和输入规范；
2. 成年角色项目设置与服务端校验；
3. 生成任务和模型池接入；
4. 结构/事实校验；
5. `ai_polish_applications` 与乐观锁应用接口；
6. 章节详情页签和差异预览；
7. 内置 Agent 模板；
8. 安全、并发、断连和页面测试；
9. 全量测试与中文文档。

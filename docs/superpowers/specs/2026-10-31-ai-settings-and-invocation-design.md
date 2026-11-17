# AI 设置与统一调用重构设计（第一阶段）

- 日期：2026-10-31
- 状态：用户于 2026-11-01 确认规格；Tasks1-8 完成，应用 `3c51a6c6` 的独立整阶段代码/规格复审 Approved，Windows/canonical Linux 全量、完整 36 用例 Chromium、Node 和静态/打包验收通过。候选 `793d340d` 已普通推送，于 2026-11-04 15:57（Asia/Shanghai）完成受保护部署及线上缓存修订验证。证据见 [阶段验收记录](../../archive/AI_SETTINGS_ACCEPTANCE_2026-11-03.md)、[部署记录](../../archive/SERVER_DEPLOYMENT_REPORT_2026-11-04.md) 与 [项目现状](../../STATUS.md)。真实 Provider/真机外部验收和第二阶段 XP Agent 未实施。
- 用户决定：借鉴 Magpie 的设置和调用设计，项目独立运行，不要求安装或部署 Magpie；先改 AI 基础设施，再建设长篇小说 XP 分析 Agent。
- 本地源码基线：`0259dfeb3e948b186d0b8a72ba7852ad92751a6d`；工作区已有的其他文档修改不属于本规格交付。
- 参考源码：`yetone/magpie` 的 `c30d3b7b63e818d3b7d9c3eb6fcdada4c7d8c5e0`，不是图像缩放项目。

## 1. 目标、阶段与取舍

第一阶段交付一套可理解、可验证、可追踪的 AI 设置和单次调用边界。用户能回答：配置了哪个服务商，任务选择了什么，实际调用了谁，为什么切换，输入能否放入上下文，以及已经报告了多少用量。

第二阶段另立规格，建设整本/系列小说阅读、分章与跨章综合、原文证据、偏好画像和推荐闭环。本阶段不把底层调用器写成小说专用服务，也不把关键词清洗描述成整书理解。

### 1.1 采用的方案

渐进重构现有 Flask、SQLite、Jinja/Vue 和 `ModelRouter`：保留配置身份、接口兼容和安全边界；调整设置交互；增加统一文本调用契约、按实际输入筛选候选及逐请求用量记录。

维持 Python 3.10+、现有 requests/Flask/SQLite 依赖和无独立前端构建器的部署方式。本阶段不增加必装 tokenizer、Agent 框架或第三方服务。

没有采用的两条路线：

- 只改设置页外观：改动小，但无法解决长输入被短后备模型限制、调用能力与能力标签不一致的问题。
- 直接依赖或复制整个 Magpie 网关：能获得更多协议与账号能力，但引入额外部署、凭据边界和维护范围，不符合项目独立运行的决定。

### 1.2 本阶段范围

- 服务商、模型、路由组和任务绑定的清晰配置流程，共享可搜索模型选择器。
- 实际已支持协议的统一文本请求与结果，不在业务服务中拼上游请求。
- 完整输入的逐候选容量预检、冻结后的执行计划和可解释排除原因。
- 保留顺序故障转移、取消、租约、快照、底层续接校验和既有路由硬上限；不把尚无业务处理器的手动续接显示成可用功能。
- 上游已报告用量的归一化、持久化与诊断展示。
- 通过现有 `keyword_clean` 生产调用点验证新接口真正被消费，而非添加无人调用的框架。

### 1.3 明确不做

- Magpie 安装、网关托管、订阅登录、多账号发现、修改外部 CLI 配置、插件宿主。
- `smart/rotate/usage/pace/weight` 调度、后台健康探测、跨任务自动冷却或费用排序。
- 新增 Gemini/Responses 原生协议、工具执行循环、多模态输入、原生严格 JSON Schema 输出。本阶段只保留明确的扩展边界，不提供不可用的选项。
- 厂商营销预设列表、命名配置方案、向量数据库、RAG 框架。
- 整书检查点、正文理解缓存、自动全文外发、XP 新表、新业务任务类型及推荐计划存储。
- 金额硬预算、整书/整批 token 硬预算、每日配额。本阶段的预算明确指单请求上下文及整条路由的次数/时间限制。
- 恢复 `ai-writing` 的写作或成人模块，或删除其存量表。

### 1.4 与旧规格的关系

本规格取代 `2026-09-03-ai-settings-operability-design.md` 中“本轮不改运行时/Schema、不调整 AI 页面内部结构”的旧边界，以及旧模型路由规格对所有候选取共同最小输入窗口的新任务行为。旧路由快照的解释方式仍保留。

旧规格中的零网络健康检查、probe 与 sync 分工、存量 Key 借用约束、事务化批量绑定，以及 `UNIFIED_PROJECT_REQUIREMENTS.md` 的认证、加密、SSRF、审计与迁移要求继续有效。历史文档不作为当前故障或生产状态的证明。

## 2. Magpie 参考与本项目适配

参考的是职责边界和交互行为，不移植其 Go 网关或账号系统。

| 参考行为 | 固定版本源码 | 本项目决定 |
|---|---|---|
| 独立命名的 Provider、协议与连接配置 | [provider.go](https://github.com/yetone/magpie/blob/c30d3b7b63e818d3b7d9c3eb6fcdada4c7d8c5e0/internal/provider/provider.go#L35) | 继续使用稳定数据库 ID；同类服务商可有多个配置实例 |
| 候选解析与实际发送分离 | [gateway.go](https://github.com/yetone/magpie/blob/c30d3b7b63e818d3b7d9c3eb6fcdada4c7d8c5e0/internal/gateway/gateway.go#L2339) | 扩展现有 Router，先生成执行计划再调用 |
| 每个后备目标单独规范化请求 | [gateway.go](https://github.com/yetone/magpie/blob/c30d3b7b63e818d3b7d9c3eb6fcdada4c7d8c5e0/internal/gateway/gateway.go#L4233) | 各适配器从规范文本请求生成自己的请求体，不互相复用被修改的字典 |
| 当前选择保持可见、分组搜索 | [app.js](https://github.com/yetone/magpie/blob/c30d3b7b63e818d3b7d9c3eb6fcdada4c7d8c5e0/internal/gui/assets/app.js#L4774) | 已失效绑定仍显示并解释，刷新目录不自动换模型 |
| 窄屏选择器不强制弹出键盘 | [app.js](https://github.com/yetone/magpie/blob/c30d3b7b63e818d3b7d9c3eb6fcdada4c7d8c5e0/internal/gui/assets/app.js#L4839) | 桌面弹层、手机可完整操作的选择面板 |
| 顺序编辑与未保存保护 | [routing/client.js](https://github.com/yetone/magpie/blob/c30d3b7b63e818d3b7d9c3eb6fcdada4c7d8c5e0/internal/gui/assets/plugins/routing/client.js#L5991) | 上移/下移为必备操作，顺序就是尝试顺序；不为此引入插件机制 |

上述行为来自文档与源码检查，未在本机运行 Magpie。不移植 Magpie 的凭据处理实现；本项目继续采用加密存储和 Key 只写接口。

## 3. 当前事实与需要守住的边界

- `ai/service.py` 的 `AIWritingService` 名称是历史兼容接口；main 当前只有管理能力和关键词清洗。
- 配置已分为 Provider、模型目录、模型池和 fixed/pool Agent 绑定，AI 配置存 SQLite，不进入普通 YAML 设置保存接口。
- `ai/model_router.py` 已有候选快照、逐次 attempt、owner/lease/CAS 和故障转移，不另建平行路由器。
- 底层存在 `RouteResumeSpec` 与快照校验，但 `ai/services/admin.py:stream_job_with_next_model` 在 main 对所有历史任务返回不支持的冲突错误。现有 continue API 不等于已有可用的关键词清洗续接功能。
- Provider 适配器目前支持 OpenAI-compatible Chat、Anthropic Messages，xAI 复用 Chat 适配。目录中的 `tools/json/vision` 标签不代表传输和业务已经实现相应能力。
- 新任务当前按 Agent、Provider、全部候选窗口取最小值；默认 Provider 128000、Agent 16000。单独把某个目录模型改成 1M 不会自动获得 1M 输入空间。
- 现有 token 估算是启发式；没有证据时不能展示为厂商精确计数。
- 共享同步任务为单执行槽；AI 路由任务另有持久化状态。本次不改变两者的调度关系，也不把路由续接当成整书断点。

## 4. 设置交互

### 4.1 页面结构与兼容 URL

保留 `/dashboard/settings/models` 与 `/dashboard/settings/agents`，不改全站导航、前端框架或 Library OS 视觉语言。

- 模型页内部划分“服务商 / 模型 / 路由组”三个视图，通过 `view=providers|models|groups` 深链接；缺省为服务商，未知值回到服务商。
- Agent 页对用户显示为“AI 任务”，继续使用现有 Agent ID、CRUD 和批量绑定接口。
- 两页共享健康横幅、模型选择器和调用诊断入口；诊断跳到已有 `/dashboard/logs?category=ai`，不再增加一套日志存储或独立用量看板。
- 新增 `provider=<id>` 作为选中/筛选 Provider 的深链接；`view` 决定视图，Provider ID 只决定选中/筛选对象，不写入配置。不存在的 ID 显示明确提示，不改选其他 Provider；无参数的旧 URL 行为继续可用。

### 4.2 服务商流程

基本项只显示名称、上游格式、地址、Key 和启用状态；超时、重试、代理、流式、默认模型及 Provider 上下文上限放在“高级”。上游格式只提供当前适配器真正支持的值。

明确区分四件事：

1. 配置检查：本地校验，不发 DNS/模型请求。
2. 获取模型列表：使用表单配置做 probe，不落库。
3. 更新已保存目录：通过现有 model-sync operation，保留分页、租约、取消和空结果确认。
4. 测试生成：用户主动发起短测试，展示实际模型、耗时和错误；不是 1M 上下文验证。

新建时可先 probe，再勾选模型并保存；没有选模型也能只保存 Provider。保存和目录同步结果分别反馈，失败不能伪装成完整成功。带模型选择的保存采用明确的三个步骤：

1. 保存 Provider 配置，取得 ID。此时反馈“连接配置已保存，模型选择尚未应用”，不把 probe 清单直接写成 discovered 目录。
2. 启动现有 model-sync，等待成功后读取实际落库目录与 `catalog_revision`。probe 中选中的 model key 与实际目录求交集；缺失的选项明确列为未生效，本次新发现但未选中的模型准备设为 disabled，手工独有记录不受这次初始化影响。
3. 通过新增的原子批量 enabled 接口提交实际目录 ID/布尔值和 revision；全部成功后才显示“模型选择已应用”。首轮配置过程中不自动绑定 Agent 或创建路由组。

sync 失败保留已经保存的 Provider 和原目录，不应用勾选；应用 enabled 失败也不把部分模型静默当成完成。表单保留选择和失败阶段，允许从该阶段重试。页面刷新不自动重放未提交选择，用户重新确认；不把 Key 或整个表单写入浏览器持久存储。冲突时先重新读取目录，再确认选择。

编辑存量 Provider 时，空 Key 表示保持原值。借用原 Key 探测必须核对保存的地址、协议类型和代理一致；更改其中任一项时要求重新输入 Key，不把旧凭据发到新目标。复制配置只复制非敏感项，不能复制或读回已保存 Key。

停用保留配置和 Key，并显示受影响任务/路由组；后端原有引用守卫继续生效。未保存的编辑在切换 Provider、视图或关闭表单时要求保存或放弃；后台刷新不覆盖 draft。

### 4.3 模型目录与选择器

- 模型按服务商分组，可按模型 ID、显示名、服务商和启用状态搜索；始终显示足以区分同名模型的服务商信息。
- 当前选项保持可见，即使已经从目录消失或被停用；明确标成不可用，不自动选第一个新模型。
- UI 显示名可用 `服务商 / 模型`，API 仍使用稳定 Provider/模型/池 ID 和原始 `model_key`，不靠拆分 `/` 恢复身份。
- 模型的上游发现值与人工覆盖分开，标明上下文/能力/输出上限的来源；同步不能覆盖人工值或 enabled。
- 已登记并由用户显式 disabled 的模型不能因使用 fixed 绑定而绕过停用；从未登记在目录中的旧手填 fixed model 仍可按 Provider/Agent 配置调用，UI 标明未验证。
- 增加可空的模型级最大输出 token 字段；未知显示“未提供”，不能猜成上下文大小。非空值限制为整数 1 到 1000000；PUT 未提供字段表示保持，显式 null 表示清除人工覆盖并恢复发现值，空字符串和 bool 无效。
- 能力展示区分“目录声明”与“当前客户端支持”。文本调用不因目录出现 tools/json 标签就自动发送工具或原生 schema 参数。
- 窄屏弹层受 viewport 与底栏限制，触摸设备不自动聚焦搜索框；支持键盘选择、明确标签、错误态和当前值播报。

### 4.4 路由组与 AI 任务

“路由组”是现有模型池的用户文案，数据库表、整数 ID 和 `/model-pools` API 不重命名。第一阶段只提供顺序尝试；组内成员顺序和后备池顺序直接对应真实候选展开顺序。

成员使用共享选择器，上移/下移在手机与键盘上均可操作；修改使用既有 `expected_version` CAS，冲突保留 draft 并要求重载或重新应用，不能覆盖服务器新版本。名称修改不改变 ID 或任务绑定。

AI 任务每行显示用途、已选模型/组、启用状态和继承的配置健康。高级参数仍可编辑，但保存只改用户触及字段；0 次重试、0 温度和 false 流式等合法值必须完整往返。

不新建“读书/画像/推荐”空壳 Agent，不自动种入不存在的 task type。保留已有记录和 seed 接口；现有非 main 业务类型标为历史/其他用途，不删除、不假称可运行。

候选预览区分“配置展开结果”和“具体输入预检”。前者是零网络配置事实，后者说明本次完整输入的可用候选、排除原因和实际外发范围。仅填上下文为 1M 不是调用能力验证。

## 5. 统一调用契约与职责

### 5.1 服务与路由边界

复用 `AIServiceCore`、`RouteRequest`、`RouteResult` 和 `AIStreamChunk` 的调用链。新增的小型契约/规划模块服务于该链路，不再提供一个绕过 Router 的业务客户端。

规范文本调用包含：

| 字段 | 语义 |
|---|---|
| `agent_id` / 已解析 Agent | 明确任务配置；通用入口没有“任意启用 Agent”回退 |
| `messages` | 完整的 system/user/assistant 文本消息；不接收二进制、tool messages 或任意上游 body |
| `stage` | 延用 internal/main/validation，不能作为更换隐私范围的后门 |
| `temperature/top_p/max_tokens` | 参数覆盖；区分未提供、0、false 与空值；不可静默降低调用者要求的输出预留 |
| `allowed_provider_ids` | 可选的收紧范围；缺省使用本次冻结绑定展开范围，不允许扩大到其他 Provider |
| `stop_requested` / deadline | 取消与剩余时间，只能收紧服务器既有硬限制 |
| 关联与审计摘要 | 安全的业务引用、长度、摘要和版本标识，与运行时 messages 分开 |

`allowed_provider_ids` 未提供时取冻结的配置范围；显式空数组表示不允许任何 Provider，不得通过默认值逻辑扩大范围。若提供则只接受正整数 ID 数组，null 或非法值返回参数错误。

使用未支持的 tools、原生 JSON Schema、金额硬预算等参数时，在发请求前返回可识别的 unsupported 错误，不能静默忽略。新增协议必须先实现适配和测试，再出现在 UI。

保留 `AIWritingService` 导入路径及既有方法签名的兼容包装。现有关键词清洗的自动选 Agent 优先级只留在该旧业务方法，不成为新通用入口的默认行为；已明确指定 Agent 不应悄悄转用其他 Agent。

### 5.2 每目标规范化与结果

每次实际候选尝试从同一个未修改的规范请求建立目标请求体。Anthropic 的 system 处理、Chat 的消息布局及各协议参数由适配器负责；一个失败目标不得修改后续目标看到的消息或生成参数。

通用结果包含 job ID、快照版本/hash、请求绑定与实际 Provider/模型、终态、终止原因、attempt 引用、完整性、用量及验证状态。实际正文供业务内存消费，数据库留存遵循第 8.2 节，不能仅靠日志 UI 隐藏。Router 的传输终态与服务完成业务校验后的 job 终态分开：正常传输后的验证失败保持 attempt 的传输成功记录，但 job 必须为 failed，原因是 validation_failed。

传输成功不等于业务验证成功。关键词清洗继续接受其已有合法输出形态，通过本地受控验证器检查解析结果；length/missing/partial/cancelled 不能当成完整结构化结果发布。验证失败给出独立状态，不伪造 succeeded，也不默认发起额外修复调用。

本阶段不向远端执行用户提供的 validator、不暴露任意 Python 函数名，也不实现工具循环。后续读书 Agent 可在业务层添加本地结构验证。

## 6. 长上下文候选规划

### 6.1 先检查每个目标，再冻结计划

顺序是：解析绑定并展开候选 -> 检查启用/能力/外发范围 -> 对每个候选估算完整输入与输出预算 -> 排除不满足者 -> 保持相对顺序冻结合格候选 -> 执行。

对候选 i 独立计算：

```text
window_i = min(Agent 配置上限, Provider 配置上限, 非空模型上下文上限)
input_i = 该模型/协议的规范消息 token 计数或估计
overhead_i = 不包含在 input_i 中的协议消息开销
reserve_i = 本次明确要求的最大输出 token
margin_i = 精确计数时 256；启发式时 max(256, ceil((input_i + overhead_i) * 0.10))
fits_i = input_i + overhead_i + reserve_i + margin_i <= window_i
```

已知模型输出上限小于 `reserve_i` 时排除该候选，不能静默缩小输出。模型上下文未知时按公式使用已保存的 Provider/Agent 上下文限制。模型输出上限未知时仍为 null，仅检查服务器最大输出 1000000 token、本次输出预留和输入窗口；不能把 Agent 默认 max_tokens 或上下文大小推断成模型输出能力。

保留已有 Provider/Agent 数值及默认值，不在迁移时把 128k/16k 自动抬到 1M。UI 解释真正的瓶颈及可编辑位置，由用户显式扩大上限。

### 6.2 token 估算不能跨模型混用

估算契约必须包含 model key/协议，并返回 count、来源、是否精确及是否已包括消息开销。只有覆盖当前模型实际消息序列化及 framing 的计数才可标为精确；已计入 framing 时 overhead_i 为 0。估算器不发网络请求；首版沿用本地启发式作为兜底，不把它冒充厂商 tokenizer。外部估算器返回缺失、负数、布尔或非法估计时转入本地启发式并标明来源；本地估算失败则该候选预检失败。客户端不能传入所谓精确计数绕过估算。

保留逐候选预算。禁止在筛选后再次用 `max(所有输入估计)` 对比 `min(所有窗口)`：不同模型分词不同，该交叉比较可能错误拒绝原本分别装得下的请求。旧 `PromptBudget` 单值只能作为旧快照兼容值或展示摘要，不能成为第二道错误的全局准入判断。

示例：A 的输入计数为 90000、窗口 100000；B 的输入计数为 200000、窗口 1000000，输出与开销均能放入时，两者都应保留，而不是拿 200000 与 100000 比较。

1M 主模型搭配 128k 后备、完整输入超过 128k 时，应保留主模型、排除后备，并告知“此输入没有该后备”。如果全部候选不合格，则零网络失败并返回原因；不自动摘要、不丢消息、不截正文。

估计通过只表示本地准入通过，不保证上游实际接受。上游拒绝上下文时记录 context_overflow，按既定规则尝试下一名仍合格且获准的候选；同一完整输入不被偷偷改写。1M 支持的离线测试不替代真实服务商长上下文验收。

### 6.3 快照版本与预览

- 新调用使用 version 2 的规划快照，记录规划规则/估算器版本、Agent/pool/provider 配置版本或 hash、规范完整请求的指纹、逐候选预算与估算来源、合格列表及有界的排除原因。请求指纹覆盖消息 role/顺序/完整 content、生成参数和获准 Provider 范围，不包含回调或运行时 owner token。
- 固定绑定最多一个候选；新增预检不能为它增加跨 Provider 后备。
- 原始配置候选与有效执行候选分开编号。排除项不记作网络失败、不占 candidate attempt 次数；有效候选仍使用稳定的冻结索引续接。
- 无 version 的历史快照视为 version 1，沿用原解码、hash、预算语义和底层续接校验；不能重新解析当前配置把旧 job 升成另一条候选链。这不意味着 main 的 continue API 已有业务处理器。
- version 2 使用冻结的模型上下文、输出上限和估算事实执行；目录刷新不修改正在运行的计划。手动恢复的底层校验除 Provider/Agent/池外，也检查模型执行配置与估算规则版本；发生冲突时要求新调用，不静默重新筛选。
- 恢复时重建的完整规范请求必须与原请求指纹一致，不能用更新后的文本套用旧容量判断。指纹不符返回冲突并零外发；仅有摘要而无法重建时返回不可续接。
- 候选预览与执行共享同一个纯规划器。规划结果包含限制值及来源，不包含消息、Key、请求体或原始响应。

## 7. 执行、故障转移与取消

保留：单池 64 个成员、展开后 64 个候选、最多 8 个池节点；每 job 最多 16 个候选尝试、32 次实际网络请求、总 deadline 30 分钟。调用方可以设置更紧限制，不能调大服务器硬上限。

Provider 内部重试、空流转非流式及跨模型后备共同消耗同一 job 的网络次数和截止时间。每次发送、重试等待和开始下一候选前检查取消与剩余预算，不能每换模型重置预算。

每次发送的连接/读 timeout 取 Provider 已配置 timeout 与剩余 route 时间的较小值；剩余时间耗尽不得启动请求。流式读取在可轮询点复核截止时间。同步阻塞读仍受该次 read timeout 约束，而非承诺瞬时抢占。

main 的第一个正文 delta 对外发布后锁定目标，失败进入 partial，不拼接另一模型输出。internal/validation 的输出只有完整成功才交给业务，失败残片不混入下次结果。usage/progress 元数据不视为正文 delta。

配置/认证类 Provider 错误沿用 Provider 级跳过；用户取消、GeneratorExit 和断连不触发故障转移。适配器关闭响应和会话资源。取消仍为合作式，不能承诺立即中止远端计算或计费。

不新建调度线程或取得共享同步执行槽。单次调用返回明确终态，由其业务调用者处理父任务；不替父任务写“已读完”或“已完成”。

## 8. 用量与诊断

### 8.1 按实际网络请求记账

一个 candidate attempt 可能包含多个 HTTP 请求。复用 `claim_ai_job_network_request` 返回的递增序号，为每次实际发送记录 request ordinal；不能只覆盖最终 attempt 的用量，也不能把流中的累计 usage 事件逐条相加。

发送前在领取网络序号的同一事务中写入该 attempt 的 pending/用量未知记录，提交成功后才允许发送。后续收到 usage 时增量合并。若领取后或发送后进程崩溃，保留“可能已外发、用量未知”，不能从未报告请求数中消失；未知不代表必然计费，也不能当作 0。

每次请求保留有界记录：序号、已报告的 input/output/total token、缓存/推理细分、报告完整性和缺失原因。Token 字段为非负整数或 null，拒绝 bool、负数和不合法类型；上游明确报告的 0 与未报告严格区分。

适配器负责把上游协议口径归一为包含缓存的输入总量、包含推理的输出总量；细分是总量的一部分，不重复加总。Anthropic 与 OpenAI 的字段语义通过协议 fixture 分别验证，不能把字段同名当作同一计费口径。

已知值只表示“上游已报告用量”。只要任一已发送请求未报告，整条路由就标记用量不完整；重试、断线和失败请求不能按 0 计算。预估 token 与已报告 token 分栏，不能互相顶替。

第一阶段不声明准确账单，不计算硬金额预算。日志中显示已报告 token、未报告请求数、估算器来源、实际目标和切换原因；没有数据时显示未知。

### 8.2 持久化与隐私

在既有 `ai_job_model_attempts` 增加可空 `usage_json`，只保存上述数值记录和白名单状态，单 attempt 不超过 16 KiB；最多 32 个请求记录。老 attempt 保持 null。重复终态/回放按 request ordinal 幂等合并，不能重复累计。

在既有 `ai_jobs` 增加可空 `validation_status`，新任务为 not_requested/passed/failed；旧任务 null 投影为 unknown，不通过历史 output_text 猜测校验结果。业务输出验证失败时不修改已完成 attempt 的传输事实。

version 2 规划诊断保存在已有有界快照 JSON，不另建诊断表；快照仍遵守 256 KiB 上限。所有新增数据清理遵循既有 AI job 留存期，不把审计日志当长期小说分析缓存。

统一调用采用内部 `metadata_only` 留存策略：审计输入仅含业务类型/引用、字符数、消息数、输入指纹和规划版本。messages 不自动传入 `create_ai_job` 的 input_data；成功、partial、取消和异常路径均不得把原始输入/输出写入 `ai_jobs.input_json/output_text/output_json`、attempt、错误字段或普通日志。允许的审计数值单独落库，业务正文通过内存结果返回，不能因停止数据库留存而丢失 partial 输出。

上游错误可能回显输入片段；新通用路径只保留白名单错误类别、HTTP 状态及本地安全说明，不落库或返回原始错误 body。验收直接检查数据库所有相关列与 API，而非只检查日志页面。

唯一兼容例外是旧 `keyword_clean`：保留其前 80 个词/40 个标签输入及现有有限清洗输出/解析结果记录。该例外只由关键词清洗服务内部选择，不是公共 API 参数，也不能供未来小说正文调用借用；未来长读默认仍为 metadata_only。

本阶段不新增业务重放处理器。main 的 continue API 保留“任务类型暂不支持”的 409 行为，不创建新 job、不发网络请求；详情追加 `can_continue=false` 和原因，UI 不展示可执行的继续按钮。底层 `RouteResumeSpec`、v1/v2 指纹和冻结计划校验作为兼容接口保留并测试。第二阶段实现业务处理器及输入重建引用时，必须经过这些检查；不能用摘要冒充完整输入。

## 9. API、Schema 与兼容策略

### 9.1 API

- Provider、模型目录、模型池、Agent、AI jobs 既有 URL 与成功/错误信封保持兼容。新增字段为可选或追加响应字段，不改变旧字段类型。
- 模型目录读取追加 `catalog_revision`，基于该 Provider 完整目录的有界配置投影（身份、启停、发现/人工配置）计算，与分页筛选无关，不包含 Key。增加 `PUT /api/dashboard/ai/providers/<id>/models/enabled`，接收 revision 和最多 5000 条唯一模型 ID/布尔 enabled 变更；在单事务内核对 revision 和全部 ID 归属，再一次应用。冲突返回 409，非法 ID/重复 ID/非布尔值返回 400，均零部分写入。只修改 enabled，不写 discovered 事实或改变人工覆盖，不另建目录写入通道。
- 保留 `GET /api/dashboard/ai/agents/<id>/candidates` 作为零网络配置预览。
- 增加 `POST /api/dashboard/ai/agents/<id>/preflight`：仅接受完整规范文本 messages、生成参数和收紧的 Provider 范围；使用无明文 Key 的配置投影和纯规划器，不解密/升级 Key、不发 DNS/上游请求、不创建 AI job、不持久化输入。成功完成预检返回 200，`can_run` 表示是否至少一名候选合格；全部被排除也返回 200/can_run=false 及逐项原因，而不是假装生成成功。Agent 不存在返回 404，参数/配置非法返回 400。
- preflight 限制请求体 16 MiB、最多 256 条文本消息；在解析 JSON 前实施该路由专属的大小限制，未知 role/非字符串 content/未知生成参数返回 400，过大返回 413。不提高全站所有上传入口上限。
- 仓库 Nginx 模板为该 preflight 路径设置匹配的 16m client_max_body_size；其他路由不变。没有应用该配置的现有部署可能仍受更小代理上限限制，部署说明与验收必须单独核对。
- UI 的预检测试框使用用户主动输入的测试文本，不自动从书库读取正文；生产长文本仍由 Python 业务层传入同一规划器。
- 增强已有 job/attempt 详情，追加规划版本、逐候选预算、验证状态、用量完整性和已报告汇总；不新增返回原始 prompt 的调试端点。
- 所有 mutation/POST 继续使用 `window.aiApi` / `window.csrfFetch` 和统一错误处理；认证、CSRF、no-store 及日志脱敏不豁免。

### 9.2 Schema

- 模型目录增加 `discovered_max_output_tokens` 与 `manual_max_output_tokens` 两个可空列；解析后的有效输出上限遵循现有 manual 优先规则。目录同步白名单及 canonical digest 同时扩展，其他人工字段不被覆盖。
- attempt 增加 `usage_json`，job 增加 `validation_status`；规划快照使用 version 2 信封，不强制重写历史 snapshot JSON 或 hash。
- 不重命名 Provider/Agent/池表或 ID，不新增平行账号、模型、路由或任务表。
- 迁移仅加字段，幂等执行；测试旧库、新库、重复初始化、事务回滚及 foreign_key_check。旧字段、旧 Agent Prompt/参数、Key 密文和写作分支表不被删除。
- Provider 缓存键、配置 hash 与快照校验覆盖本次影响调用语义的新增字段；模型输出能力固定在候选快照中，运行中目录刷新不得偷换计划。

## 10. 代码职责落点

具体提交顺序在实现计划中展开，以下约束用于避免继续扩大巨型文件。

| 模块 | 职责 |
|---|---|
| `ai/models.py` 与独立的小型调用契约模块 | 规范文本请求、估算与用量值类型；不调用数据库或 HTTP |
| 独立候选预算/规划模块 | 对已解析候选做逐目标预检，供 preview 与 Router 共用 |
| `ai/model_router.py` | 冻结执行计划、次数/deadline、owner/lease、attempt、故障转移 |
| `ai/providers.py` 与按协议拆分的解析辅助模块 | 规范请求到协议请求、流式/非流式响应及 usage 解析；保留硬化发送路径 |
| `ai/services/core.py` | 统一入口、生命周期、审计策略和兼容包装 |
| `ai/services/keyword_clean.py` | 唯一现有生成业务的接入、本地输出验证和旧返回结构 |
| `storage/ai/*` | 加字段迁移、模型输出上限、逐请求用量与日志投影 |
| `ai_web.py` | preflight 和追加诊断字段，保持配置 API 边界 |
| `templates/dashboard_settings_models.html`、`templates/dashboard_settings_agents.html` 与共享 AI partial/脚本 | 视图编排、统一选择器、draft/错误状态；不复制路由判断 |
| `templates/dashboard_logs.html` | 复用现有 AI 日志，显示规划与用量 |
| `config/nginx/pixiv-novel-sync.conf` 与部署契约测试 | 只为 preflight 匹配请求体上限，不扩大其他入口 |

不为一次重构更换 Flask、Vue 或数据库。抽取仅限这次触及的明确职责，不拆改无关同步、救援和阅读模块。

## 11. 验收矩阵

默认回归全部使用临时数据库、合成文本和 fake HTTP/DNS，不访问真实 Pixiv/Provider 或本地小说。

| 编号 | 必须验证的行为 |
|---|---|
| A01 | 旧 Provider/模型/池/Agent 全字段往返；0 重试、0 温度、false 流式不被默认值覆盖 |
| A02 | 两个旧设置 URL 与批量绑定仍可用；新增 view/provider 深链接、视图刷新与前进后退一致 |
| A03 | 共享选择器不丢失失效当前值，同名模型可区分，保存前不修改后端，draft 切换有保护 |
| A04 | 375/768/1440 CSS 像素宽度均可操作；手机无横向溢出，搜索框不强制唤起键盘，键盘可完成选模和排序 |
| A05 | probe 不写库；sync 才更新发现目录；初始化勾选在成功同步后原子应用，失败/409 不部分写入、不自动绑定；空结果确认、CAS、引用守卫及人工字段保留不回归 |
| A06 | 借存量 Key 时改变地址、协议或代理均拒绝；复制配置和 API 响应无明文 Key |
| A07 | 混合 1M/128k 候选时只排除装不下完整输入者；固定 Agent 不增加后备、不绕过已知模型停用；无目录手填模型保持兼容；无合格候选零网络失败 |
| A08 | 同 Provider 不同模型使用各自估算；不同估算/窗口的交叉示例不会被全局 max/min 再次拒绝 |
| A09 | 输出预留、协议开销、启发式余量、Agent/Provider 上限均参与逐候选预算；未知能力和近边界估算明确展示 |
| A10 | 长文本完整保留，包含尾部标记的合成输入不丢失；CJK、emoji、空白及多消息不被错误当字节/字数预算 |
| A11 | v1 快照读取/hash/底层续接校验兼容；v2 保持冻结顺序，请求指纹或执行配置冲突零外发；main continue API 仍明确 409、不创建新调用 |
| A12 | OpenAI-compatible/xAI 与 Anthropic fallback 各自重新生成请求体，前一次修改不能污染后一次 |
| A13 | 重试、非流式回退和跨候选共用 16/32/30 分钟限制；取消、断连、首正文后失败及内部残片规则保持 |
| A14 | usage 累计事件不重复相加，多个 HTTP 请求不覆盖，回放不重复记账；发送前后崩溃仍保留未知记录；未知和明确 0 可区分，缓存/推理不重复计数 |
| A15 | 正常完成、空流、长度截断、缺结束标记、JSON 无效、取消分别输出正确终态/验证状态，不发布伪完整结果 |
| A16 | keyword_clean 实际使用新链路且保留成功返回结构、失败降级和父任务发布守卫；显式 Agent 失效不换用他人配置，不宣称支持业务续接 |
| A17 | preflight 零网络/不落库/不解密，16 MiB 与 256 消息上限、200/can_run=false、400/404/413、认证/CSRF 均有测试；代理路径上限一致，不回显输入 |
| A18 | 新旧库迁移可重复、外键完整、旧数据不丢；日志清理涵盖 usage，受影响缓存/hash 不陈旧 |
| A19 | metadata_only 在成功/partial/取消/异常路径不将输入/输出或上游回显片段写入任何 DB 日志列或 API 诊断；内存完整/部分结果仍返回；关键词兼容例外不外溢 |

定向回归以现有 `test_ai_model_router*`、`test_ai_provider_completion.py`、`test_ai_security_hardening.py`、`test_ai_model_schema.py`、`test_ai_model_ui.py`、`test_keyword_clean.py`、`test_preference_jobs.py` 和设置/移动端测试为基底补充。

实现验收命令：`python -m pytest -q`、`python -m compileall -q src`、可用时 `python -m pyflakes src`、`python scripts/build_tailwind_css.py --check`、`git diff --check`。模板/脚本使用的新类名必须纳入静态 CSS 构建与运行时测试，不能只靠文本断言证明交互正确。

真实服务商的 1M 长输入、实际 token 账单、网络环境取消效果和手机端操作另行验收，需用户明确选择模型及测试预算。普通短生成成功不作为这些项目通过的证据。

## 12. 交付顺序与第二阶段接口

第一阶段实现计划按可独立测试的结果拆分：兼容与契约 -> 逐候选规划 -> 目标适配与用量 -> 设置交互 -> 关键词接入及端到端回归。设置和后端在稳定契约后可并行开发。

本阶段结束时，第二阶段可以向统一入口提供完整文本与明确 Agent，得到可靠终态、逐次尝试、容量拒绝原因和已报告用量；调用器不自行读小说、改画像或执行搜索。

第二阶段的独立规格必须解决：单篇/系列清单与可靠章序、缺章/连载标识、整本/分章切换、全文覆盖与原文证据、正文与分析版本缓存、父级检查点、整书/整批预算、让位同步，以及“喜欢”样本和普通归档的区别。不得用本阶段完成的 AI 设置与路由宣称这些业务已经实现。

## 13. 文档与审阅状态

本文件是设计交付，不是实现记录。详细规格获用户认可后才编写实现计划；实现后再更新模型路由指南、前端 API 契约、前端页面文档与项目现状，且先合并各文件已有的用户改动。

设计完成的判断：无未决占位要求；范围、API、预算和兼容行为相互一致；每个本阶段需求有验收项。代码完成的判断仍是实现计划逐项完成与实际验证结果，不能由本设计文档替代。

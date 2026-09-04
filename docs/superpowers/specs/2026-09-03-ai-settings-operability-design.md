# AI 设置页可操作性重做 设计

- 日期：2026-09-03
- 状态：设计已确认，待实施
- 触发：用户反馈「AI 相关的几个设置页面和操作逻辑还是不满意…操作逻辑很不容易理解，应该像 CC-switch 和 newapi 那样方便快捷」
- 前置事实：2026-09-03 生产巡检发现 `ai_jobs` 7 条全部 failed，AI 子系统静默不可用至少 4 天（详见 §2.1）

## 1. 目标与非目标

**目标**，按用户确认的优先级顺序，三段各自可独立上线：

1. **状态可见**：Provider 可达性与最近错误、Agent 的坏绑定、一条总体健康横幅。
2. **配置流程**：表单收敛、**未落库即可验证**（探测端点 + 解掉 `default_model` 死循环）、表单内获取模型列表并勾选。
3. **批量操作**：Agent 列表多选 + 搜索，批量改绑 / 启停。

**非目标**（明确不做）：

- 不改任何 AI 运行时行为。`ModelRouter` 调度、`ai_jobs` 生命周期、候选解析顺序一行不动。
- 不重新划分页面边界。`models` / `agents` / `adult` 仍是三个一级页面，理由见 §3.5。
- 不做官方厂商 base_url 预设表。理由见 §4.1——生产三个 Provider 全是自建网关或中转，没有一个官方端点，那张表是照抄 New API 的形而不解决问题。
- 不做 CC-switch 式的命名方案快照（新表 + 应用/回滚语义）。用户确认当前需求是「把 15 个绑在死 Provider 上的 Agent 一次改过来」，多选批量已覆盖；反复切换的场景尚不存在。
- 不动成人润色的 fail-closed 契约。`adult` 页只加健康可见性，policy hash / review binding / 角色 revision 的语义与入口一律不碰。
- 不做视觉改版。沿用 `library-*` 约定与 `vue_components.html` 既有组件。

## 2. 现状测绘

### 2.1 一次真实事故：静默失败四天

2026-09-03 登录生产核对，`ai_jobs` 表 7 条记录**全部 failed**，时间跨度 2026-08-30 → 09-02，无一成功。7 条的 `ai_job_model_attempts` 完全一致，都死在 attempt 0：

```
error_scope=provider  error_category=configuration
base_url 必须使用 https（本机回环地址除外）
```

根因是 Provider 配置：`gpt-5.5`(id 1) 与 `deepseek-v4-pro`(id 3) 的 `base_url` 都是 `http://nas.dongb.xyz:3000`，而 `ai/providers.py:274` 对非回环主机强制 https。进一步核对：该域名解析到公网 IPv6（家庭宽带段），且 **TCP 3000 从生产机根本不可达**——改成 https 也救不回来。

绑定情况把问题放大成全局：16 个 Agent 里 **15 个绑到 provider 3**，1 个（章节摘要师）绑到 provider 1，**没有任何 Agent 绑到唯一可用的 provider 4**（`烁`，`https://elysiver.h-e.top/v1`，实测 `/v1/models` 返回 401 即通、且是唯一同步过模型目录的，25 个模型）。`ai_model_pools` 为 0 行，没有任何故障转移池，单候选失败即 `route_exhausted`。

**为什么四天没人发现**：唯一自动跑 AI 的地方是 `preference_analyze` 里的 `clean_keywords`，它按设计包在 try/except 里优雅降级。于是每 12 小时一次：AI 任务失败 → 偏好分析报绿 → `refined_keywords` 静默丢失。任务日志的 AI 分类里能看到那 7 条红的，但没有任何东西把它们和那条绿色的 `preference_analyze` 关联起来。

**这条事故定义了本设计的重心**：真正的杀伤力不是「配错了」，而是「配错了没有任何地方说话」。

### 2.2 三条具体的操作死结

| 死结 | 现状 | 后果 |
|---|---|---|
| 测试要模型，模型要同步，同步要先保存 | `admin.py:454 test_provider` 在 `default_model` 为空时抛「Provider 未配置默认模型」；`default_model` 要等目录同步回来才填得上；同步端点是 `POST /providers/<id>/models/sync`，必须先有 provider 行 | 想验证一个新 Provider 通不通，必须先把它（可能是错的）存进库 |
| 「刷新目录」与「同步模型」两个按钮并排 | 前者 `loadProviderModels` 只重读库，后者 `syncProviderModels` 才向上游发请求 | 文案分不出差别，用户不知道该点哪个 |
| 新增 Provider 一次面对 10 个字段 | name / provider_type / base_url / default_model / context_window / timeout_seconds / max_retries / stream_enabled / enabled / api_key 全部平铺 | 必填与可选没有层次，`context_window` 这种能留空的字段和 base_url 一样显眼 |

### 2.3 UI 完全触不到的 `enabled` 列

`storage/ai/catalog.py:76` 定义 `routable = enabled AND (manual OR discovered_available) AND provider_enabled`，而 `enabled` 是**用户字段**——注释明确写着「同步只更新 `discovered_*`，永不覆盖人工字段或用户 `enabled`」。

但 `dashboard_settings_models.html` 的目录列表里，每个模型只有 `display_name` / `model_key` / 窗口 / 能力标签 / `source` 的展示，以及仅对人工记录的「删除人工记录」按钮——**没有任何地方能改 `enabled`**。后端 `PUT /api/dashboard/ai/provider-models/<id>` 早就支持它（`_MANUAL_MODEL_UPDATE_FIELDS` 含 `enabled`，并按布尔校验），纯粹是前端没接线。生产 25 个模型全部 `enabled=1`，不是用户选的，是没得选。

### 2.4 参考对象里真正值得抄的东西

- **New API**：`添加渠道 → 选类型 → 填 Key/BaseURL → 「获取模型列表」→ 勾选需要的 → 保存`，以及**保存后「测试」按钮是主要调试回路**，错误文案直接回显（超时 / 证书 / 余额不足 / 404）。值得抄的是「模型是我勾选的清单」而不是「同步回来的目录」，以及「配置未落库就能验证」。
- **CC Switch**：整套配置的命名快照 + 一键切换、列表内搜索、列表上的三态开关做批量启停。本轮只抄**批量与搜索**，快照按 §1 明确不做。

## 3. 阶段一：健康状态

### 3.1 三层判据，全部零网络

用户确认的方案：静态体检 + 被动战绩 + 手动测，**不新增任何定时后台探测**。§2.1 那次事故完全落在前两层的射程内，不需要为它付定期外发请求的代价。

| 层 | 数据来源 | 是否发请求 |
|---|---|---|
| 静态体检 | `ai_providers` 行本身 | 否 |
| 被动战绩 | `ai_providers.models_sync_*`、`ai_job_model_attempts` | 否 |
| 手动探测 | 用户点「测试连接」 | 是，一次 |

### 3.2 静态体检必须调用运行时那个校验器

判据：

| 条件 | 结论 |
|---|---|
| `base_url` 非 https 且非回环 | `will_fail` |
| `has_api_key` 为假 | `will_fail` |
| `enabled` 为假但有 Agent 绑着 | `conflict`；**具体是 `will_fail` 还是仅 `warn`，实施时以 `resolve_candidates` 对 disabled Provider 的真实行为为准**（`routable` 要求 `provider_enabled`，但固定绑定直填 `model_key` 的路径需要现场确认） |
| 目录里 0 个可路由模型 | `warn`（固定绑定直填 `model_key` 仍可用，池成员选不出来） |
| `default_model` 为空 | 不影响健康，但「测试连接」要按 §3.4 处理 |

**实现约束（本设计的成败点）**：第一条判据必须调用 `ai/providers.py:validate_base_url(base_url, resolve=False)`，而不是另写一份规则。`resolve=False` 正好只做 scheme 与回环判断、跳过 DNS，因此零网络，而结论**由构造保证**与运行时一致。另写 lint 必然与 `providers.py` 漂移，届时横幅报「健康」而任务照样失败——比没有横幅更坏。

`base_url` 本来就已经在 `GET /api/dashboard/ai/providers` 的返回里（`_row_to_ai_provider` 只摘掉 `api_key_encrypted`），所以体检放在服务端不构成新的信息暴露；放服务端的唯一理由就是复用真校验器。

### 3.3 被动战绩的聚合口径

- Provider 级：`models_synced_at`（最近一次成功）、`models_sync_attempted_at`、`models_sync_error`。三个字段都已存在，从来没在页面上显眼展示过。
- 尝试级：新增存储方法，按 `provider_id` 聚合 `ai_job_model_attempts` 最近 N 天（默认 7）的尝试数、失败数、最近一次的 `error_scope` / `error_category` / `error_message` / `finished_at`。
- **按 attempts 聚合而不是按 `ai_jobs`**：一个 job 可能跨多个 Provider 做故障转移，算在任何单一 Provider 头上都不对。
- 任务级：横幅另外统计最近 N 天失败的 AI 任务数与其 `task_type`，用来说出「`keyword_clean`（偏好关键词精炼）正在静默降级」这句话——这是 §2.1 里唯一能把红任务和绿任务关联起来的信息。

### 3.4 手动探测：解掉 `default_model` 死循环

`test_provider` 改为：`default_model` 为空时取目录里第一个可路由模型；目录也空时返回可读的引导（「先获取模型列表」），而不是抛 `Provider 未配置默认模型`。这是 §2.2 第一条死结的解法之一，另一半由 §4.2 的探测端点解决。

### 3.5 UI 落点

新增共享 partial `dashboard_ai_health_band.html`，在 `models` / `agents` / `adult` 三页的 `dashboard_settings_nav.html` 之后各 `{% include %}` 一次，**常驻三页顶部**（用户确认，不默认折叠）。

不做第四个页面：这条信息要出现在你正在编辑的那一页上，而不是一个要跳过去的目的地。也不重新合并 `models` + `agents`：刚连着做完两轮拆页（设置 5 页、AI 创作 4 页），再动边界是纯 churn，而「看不清 `Provider → 模型目录 → 模型池 → Agent 绑定` 这条链」的真凶是没有任何一处能看全，横幅正是那一处。

```
┌──────────────────────────────────────────────────────────┐
│ AI 配置总览                                     [重新检查] │
│ 3 个 Provider · ✕ 2 个配置必失败 · 25 个可路由模型         │
│ 16 个 Agent · ⚠ 15 个绑在坏 Provider 上                   │
│ ⚠ 最近 7 天 7 次 AI 任务失败，全部死在 attempt 0           │
│   keyword_clean（偏好关键词精炼）正在静默降级              │
│                                        [展开逐项 ▾]       │
└──────────────────────────────────────────────────────────┘
```

另外两处标记：

- `models` 页 Provider 卡片头部：状态点 + 体检结论 + 战绩 + 「N 个 Agent 绑在这里」。
- `agents` 页每个 Agent 行：显示其绑定目标的健康点，坏的标红。

**Agent 行那个红点是继承来的**（绑定目标的健康），不是 Agent 自身状态。UI 上必须写明，否则用户会去修 Agent。

## 4. 阶段二：Provider 配置流程

### 4.1 表单收敛，不做厂商预设表

必填只留三件：上游格式（`provider_type`，三个真实取值 `openai_compatible` / `anthropic` / `xai`）、地址、Key。`context_window` / `timeout_seconds` / `max_retries` / `stream_enabled` / `default_model` 收进默认折叠的「高级」区。名称从地址 host 自动预填，可改。

**不做官方厂商 base_url 预设表**：生产三个 Provider 全是自建网关或中转，没有一个官方端点，那张表对本项目的实际用法是死重量。替代的三件事：

1. 地址框失焦即跑 §3.2 的静态体检，当场说出 `http://` 非回环会被运行时拒掉。
2. 「从已有 Provider 复制」：生产那两个 nas Provider 几乎一模一样，克隆比重填快。
3. 上游格式下拉每项配一句人话，说明各自期望的地址形态。

### 4.2 探测（预览）与同步（落库）的分工

用户确认的方案：两者分开，**落库目录始终只有 `model_sync` 一个写入方**。

```
新建 Provider
  上游格式 [OpenAI 兼容 ▾]
  地址     https://elysiver.h-e.top/v1   ← 失焦跑静态体检
  Key      ●●●●●●●●
  ▸ 高级（超时 / 重试 / 上下文窗口 / 流式）

  [获取模型列表]   → POST /providers/probe-models   不落库、不建 Provider
   ✓ 通，480ms，找到 25 个        [全选] [全不选] [搜索…]
   ☑ glm-4.7          ctx 200k
   ☐ bge-m3           ctx 8k    ← 无 chat 能力，默认不勾

  [保存] → 建 Provider + 只写入勾选的那几个 + 第一个勾选项设为 default_model
```

一个都不勾也允许保存（先建 Provider、之后再同步目录是合法路径），此时 `default_model` 留空、由 §3.4 兜住。

探测端点的实现是薄的：用请求里的字段构造一个 `ProviderConfig`（不落库）→ 实例化适配器 → 调 `Provider.list_models(deadline=...)` → 过 `ai/model_catalog.py` 归一化 → 返回。全程复用既有硬化路径：`validate_base_url(resolve=True)` 的 DNS pin 与私网拒绝、`ResponseByteBudget`、分页上限、deadline。**不写任何库表**。

编辑既有 Provider 仍走 `POST /providers/<id>/models/sync`：租约、心跳、空目录确认、SSE 进度、`models_sync_generation` 那一套是认真做过的，不能被第二条路径绕开。

### 4.3 借用存量 Key 的安全约束

探测端点接受 `base_url` + 明文 `api_key`。两种调用形态：

- **新建**：表单给了明文 Key，直接用。
- **编辑**：Key 只写不回显，表单里为空，需要借库里那把（请求带 `provider_id`）。

**借用存量 Key 时，请求里的 `base_url` 必须与库里那条逐字相同，否则拒绝。** 否则这个端点等于「把你加密存好的 Key 发到我指定的任意地址」——一条现成的凭据外泄通道，且 `validate_base_url` 拦不住它（目标地址可以是完全合法的 https 公网主机）。这一条要写成独立测试。

### 4.4 默认勾选规则

有 chat / text 能力标签的默认勾上；embedding / rerank / ASR 这类默认不勾。**能力标签缺失时默认勾上**——标签来自上游目录、不保证齐全，宁可多勾也别让用户以为没获取到。

### 4.5 `/v1` 后缀不自动改写

生产两个 Provider 一个带 `/v1` 一个不带，这也是 New API 用户最常踩的坑。**不自动改写用户输入的地址**：静默改写 URL 是更坏的行为。改成探测失败且错误形态像路径不对（404 / 路由不存在）时，在错误下面给一个「试试 `<地址>/v1`」的一键按钮，改不改由用户点。

### 4.6 补上 `enabled` 开关

目录列表每行加一个启用开关，接到已存在的 `PUT /api/dashboard/ai/provider-models/<id>`（见 §2.3，**无需新增后端**）。后续同步行为不变：发现即写入，永不覆盖用户改过的 `enabled`。

## 5. 阶段三：批量改绑

```
Agent 列表        [搜索…] [类型 ▾] [☐ 只看坏绑定]
 ☑ 全选（15 个可选，1 个成人 Agent 不参与）
 ☑ 全能写作助手     ✕ deepseek-v4-pro
 ☐ 章节摘要师       ● 烁 / glm-4.7
已选 15 个   [批量改绑到…] [批量启用] [批量停用]
```

改绑目标是「固定绑定（Provider + 模型）」或「模型池」二选一，与单个 Agent 的 `binding_type` 同构。确认弹窗**逐项列出老值 → 新值**，因为 15 条静默改写没有回头路。

**批量只改绑定相关字段**：`binding_type` / `provider_id` / `model` / `model_pool_id`，以及独立的批量启停改 `enabled`。`system_prompt`、`temperature`、`max_tokens`、`required_capabilities` 一律不动——那些是每个 Agent 的个性，批量覆盖会把十几个精调过的提示词一次抹平。

### 5.1 一个事务，且 `binding_version` 必须涨

新增 `PUT /api/dashboard/ai/agents/bindings`，body `{agent_ids: [...], binding: {...}}`，**在一个事务里改完**。不复用 16 次 `PUT /agents/<id>`：中途失败会留下改了一半的状态，而那正是「说不清现在到底在用什么」的成因。

`binding_version` 要跟着涨——`ai/services/core.py` 的候选快照按 `agent_config_hash` 缓存，不涨版本的话在跑的 job 会继续用旧候选。

### 5.2 成人 Agent 不参与

批量操作不碰 `adult_polish` 那一组。成人 Agent 有独立的生命周期入口和 fail-closed 契约（policy hash、review binding、角色 revision），混进通用批量里等于给一条安全边界开后门。它们在列表里显示但不可勾选，并注明原因。

## 6. 接口与数据变更清单

| 变更 | 类型 | 阶段 |
|---|---|---|
| `GET /api/dashboard/ai/health` | 新增，只读，零网络 | 一 |
| 按 `provider_id` 聚合 `ai_job_model_attempts` 的存储方法 | 新增 | 一 |
| `test_provider` 在 `default_model` 为空时回落到首个可路由模型 | 改行为 | 一 |
| `POST /api/dashboard/ai/providers/probe-models` | 新增，不落库 | 二 |
| `PUT /api/dashboard/ai/agents/bindings` | 新增，单事务 | 三 |
| `dashboard_ai_health_band.html` | 新增 partial，三页 include | 一 |
| `dashboard_settings_models.html` | 表单收敛、探测勾选、`enabled` 开关、卡片健康 | 一 / 二 |
| `dashboard_settings_agents.html` | 行内健康点、多选 + 搜索 + 批量 | 一 / 三 |
| `dashboard_settings_adult.html` | 仅 include 横幅 | 一 |

**没有数据库 schema 变更。** 三个阶段用到的列全部已存在（`ai_providers.models_sync_*`、`ai_provider_models.enabled`、`ai_job_model_attempts.provider_id` 与三个 error 字段、`ai_agents.binding_version`）。

## 7. 实施顺序

| 步 | 内容 | 阶段 | 说明 |
|---|---|---|---|
| 1 | `/health` 端点 + attempts 聚合 + 静态体检（复用 `validate_base_url`） | 一 | 纯后端，可单独测 |
| 2 | 横幅 partial + 三页 include + 两处继承标记 | 一 | 阶段一完成，**此时事故已可见** |
| 3 | `test_provider` 解死循环 | 二 | 小，独立，先做能立刻减少困惑 |
| 4 | 探测端点 + 借 Key 的 base_url 一致性约束 | 二 | 安全约束与端点同一个 commit |
| 5 | 表单收敛 + 勾选落库 + `enabled` 开关 | 二 | 阶段二完成 |
| 6 | 批量改绑端点 + 列表多选/搜索 | 三 | 阶段三完成 |

阶段边界即可上线边界：步 1–2 上线后，横幅立刻会把生产那两个坏 Provider 和 15 个坏绑定说出来，不必等步 4–6。

## 8. 验证

- 静态体检与运行时同源：构造一个 `http://` 非回环 Provider，断言 `/health` 的结论与 `validate_base_url` 抛出的消息一致；再断言体检**不发起网络请求**（mock 掉 socket 层，调用次数为 0）。
- 借 Key 约束：带 `provider_id`、但 `base_url` 与库里不同 → 必须拒绝；逐字相同 → 放行。
- 探测不落库：调用 `probe-models` 后断言 `ai_provider_models` 行数不变、`models_sync_*` 字段未被触碰。
- 批量改绑原子性：让第 3 个 Agent 的写入抛异常，断言前两个也没被改。
- `binding_version` 递增：批量改绑后断言每个受影响 Agent 的 `binding_version` 都 +1。
- 成人 Agent 不可批量：把 `adult_polish` 的 id 混进 `agent_ids` → 必须整体拒绝（fail-closed，不是静默跳过）。
- 三页都 include 了横幅：模板断言，照 `tests/test_frontend_library_os.py` 既有风格。
- 浏览器验证：三页渲染、横幅在坏配置下变红、多选批量的确认弹窗逐项列出差异、零 console error。

## 9. 明确不在本轮范围

- AI Provider 重绑本身（用户明确表示自己调整）。
- 官方厂商 base_url 预设表（§4.1）。
- CC-switch 式命名方案快照（§1）。
- 模型池的可视化编排（拖拽排序、跨池复制）。
- 定时后台健康探测（§3.1 已否决）。
- 成人润色的任何语义变更（§1）。


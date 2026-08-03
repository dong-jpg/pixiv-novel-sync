# AI 模型目录与模型池统一需求文档

> 日期：2026-07-27
> 范围：整合当前这条开发线中，用户已经提出并确认的 AI 模型目录、模型池、统一路由、任务审计与管理页面需求
> 当前状态：第一阶段 Task 1-22 已完成；本文保留需求与实现追溯口径

## 1. 文档目的

这份文档用于把当前分散在设计稿、实施计划、任务 brief、进度记录和已提交代码里的需求收拢成一份统一口径文档，避免后续继续开发时来回翻多份材料。

本文档既包含“最终要实现什么”，也包含“基于当前代码检查，已经做到哪里、还缺什么、下一步应该先做什么”的分析结论。

## 2. 本文档整合的来源

本文件主要整合以下内容：

- `docs/superpowers/specs/2026-07-23-ai-model-catalog-pools-design.md`
- `docs/superpowers/plans/2026-07-23-ai-model-catalog-pools.md`
- `.superpowers/sdd/model-task-3-report.md`
- `.superpowers/sdd/model-task-4-brief.md`
- `.superpowers/sdd/progress.md`
- 当前代码与测试现状：
  - `src/pixiv_novel_sync/storage/ai/model_schema.py`
  - `src/pixiv_novel_sync/storage/ai/catalog.py`
  - `src/pixiv_novel_sync/ai/model_catalog.py`
  - `src/pixiv_novel_sync/storage_db.py`
  - `tests/test_ai_model_schema.py`
  - `tests/test_ai_model_catalog.py`
  - `tests/test_ai_model_catalog_task2_regressions.py`

说明：本文档只整合“AI 模型目录与模型池”这条主线需求，不把救援目录、成人润色 Agent 等相邻项目混入同一份实现范围。

## 3. 统一后的目标

目标可以归并为 6 件事：

1. 让 Provider 支持安全、可审计的模型目录同步，而不是继续靠手填模型名。
2. 让系统支持“有序模型池 + 后备池”的候选链，而不是单一固定模型。
3. 让 Agent 支持固定绑定和池绑定两种模式，并保留旧 Agent 的完全兼容。
4. 让所有 AI 生成路径经过统一路由入口，具备跨模型、跨 Provider 的受控故障转移。
5. 让每个 AI 任务都能记录实际候选、尝试、错误、终态和续接依据，便于排障与审计。
6. 让设置页和日志页具备完整管理与观察能力，而不是只有底层存储能力。

## 4. 需求边界

### 4.1 本期必须实现

- Provider 模型目录的结构化存储、同步状态和安全同步接口
- 模型池、池成员顺序、后备池、循环校验、候选上限和版本 CAS
- Agent 的 `fixed` / `pool` 双绑定模式
- 统一 `ModelRouter` 候选解析与执行契约
- 首个正文前允许故障转移，首个正文后禁止自动切换
- 任务候选快照、尝试记录、`partial` 终态和手工“下一个模型继续”
- 设置页的模型目录、模型池、Agent 绑定管理能力
- 日志页的路由摘要、尝试详情和继续操作
- 文档、静态约束测试和全量验证

### 4.2 明确不在本期

- 跨任务健康冷却
- 连续失败健康评分
- 后台定时自动刷新模型目录
- 权重轮询、成本排序、复杂调度策略
- 第二阶段健康状态页面
- 其他业务线需求

## 5. 统一后的核心规则

以下规则是整个实现的硬约束：

### 5.1 兼容性

- 现有 Agent 必须迁移为 `binding_type='fixed'`
- 旧 Agent 的 ID、名称、Prompt、参数、启用状态、Provider 和固定模型不能被破坏
- 无模型目录时，固定 Agent 仍可使用手填模型
- 旧前端提交的固定绑定格式仍要兼容

### 5.2 模型目录

- 模型目录以 `ai_provider_models` 为事实来源
- `available_models_json` 只用于一次性迁移导入，不再作为运行时事实来源
- 同步只能更新 `discovered_*`、`discovered_available`、`last_seen_at`
- 不得覆盖 `manual_*`、`manual` 和用户 `enabled`
- 上游模型消失只能标记为不可用，不能直接删除被引用行
- `metadata_json` 只能保存白名单 canonical 数据，不能漏出密钥、Prompt、正文或请求体

### 5.3 模型池

- 池成员有顺序，按 `position` 展开
- 先尝试当前池，再进入后备池
- 禁止直接或间接循环
- 后备链深度最多 8
- 单池最多 64 成员
- 整条展开去重后的候选最多 64
- 空池只能是禁用状态
- 被 Agent 或其他池引用的池不能删除、禁用或清空
- 成员替换必须整批原子替换，不能做增量位置更新
- 所有更新必须带 `expected_version` 并通过 CAS

### 5.4 路由与故障转移

- 所有业务 AI 生成路径都必须经统一路由入口
- 固定绑定不自动切换 Provider
- 池绑定允许按候选顺序切换
- 只有在首个用户正文 `delta` 之前失败，才允许切换下一个候选
- 一旦已输出正文，后续错误统一收口为 `partial`，不能自动换模型继续拼接
- 取消、断连、`GeneratorExit` 不触发切换
- Provider 自身已有的重试和流式降级仍然保留

### 5.5 资源与安全

- 单个 job 最多 16 次候选尝试
- 单个 job 最多 32 次网络请求
- 单个 job 总 deadline 不超过 30 分钟
- 模型同步单页响应体最多 4 MiB，单次最多 20 MiB，最多 100 页和 5000 模型
- 模型同步必须复用现有 SSRF / DNS 固定 / Host-SNI 校验 / 禁重定向 / 密钥脱敏能力
- 快照、attempt、日志和 API 均不得写出 API Key、Prompt、正文、请求体或完整响应头

### 5.6 审计与续接

- 每个 job 必须保存候选快照哈希和尝试记录
- `partial` 是正式终态，不是运行中状态
- 手工继续必须使用父 job 的候选快照，不允许重新按新配置解析
- 续接必须校验快照哈希、候选索引、池版本、Agent 版本和 Provider 配置哈希

## 6. 需求拆分后的实现范围

### 6.1 存储层

必须落地以下能力：

- 路由 Schema 迁移
- 模型目录 CRUD 与同步写入
- 模型池 CRUD、成员替换、引用检查、版本冲突
- job candidate snapshot
- job model attempts
- partial / cancel / stale 回收所需字段
- model sync operations 状态存储

### 6.2 领域层

必须落地以下能力：

- 模型 key、能力、显示名、元数据的规范化与 canonical digest
- 模型池图校验与展开
- 路由 DTO、PromptBudget、候选过滤和故障转移策略
- 手工续接的 snapshot replay

### 6.3 服务与接口层

必须落地以下能力：

- 模型同步 API、SSE、空目录确认、取消和状态查询
- 模型目录人工补录/修改/删除接口
- 模型池 CRUD、成员替换、attempt 摘要接口
- Agent 绑定双模式接口
- job 详情与继续接口

### 6.4 前端层

必须落地以下能力：

- Provider 设置区的目录管理和同步状态
- 模型池设置页签
- Agent 绑定模式切换和能力要求配置
- 日志页的路由摘要、尝试列表、partial 展示和继续操作

### 6.5 文档与验证

必须落地以下能力：

- README 与前端 API/页面文档更新
- 业务层禁止直接 `provider.stream_generate()` 的静态约束
- 定向测试、全量测试、`compileall` 和 `git diff --check`

## 7. 基于代码检查的当前完成情况

### 7.1 已完成

#### Task 1：原子 Schema 迁移

已完成并提交：

- `c31791d` `feat: add atomic model routing schema migration`
- `16b8587` `fix: reject control characters in legacy model keys`

从代码可确认已落地：

- `src/pixiv_novel_sync/storage/ai/model_schema.py`
- `migrate_model_routing_schema(...)`
- `assert_model_routing_foreign_keys(...)`
- `ai_model_pools` / `ai_job_model_attempts` / `ai_model_sync_operations` 等表结构
- `ai_agents.binding_type` / `model_pool_id` / `required_capabilities_json`

#### Task 2：规范化与共享 DTO

已完成并提交：

- `816e690` `feat: add model catalog normalization contracts`
- `9458cfe` `fix: bind model digest to normalized catalog data`

从代码可确认已落地：

- `src/pixiv_novel_sync/ai/model_catalog.py`
- `normalize_model_key(...)`
- `normalize_capabilities(...)`
- `normalize_model_record(...)`
- `canonical_model_digest(...)`

并有回归测试：

- `tests/test_ai_model_catalog_task2_regressions.py`

#### Task 3：Provider 模型目录存储

已完成并提交：

- `6c3cc3a` `feat: add provider model catalog storage`
- `c63ac95` `fix: enforce canonical model metadata at storage boundary`

从代码可确认已落地：

- `src/pixiv_novel_sync/storage/ai/catalog.py`
- `Database` 已接入 `CatalogMixin`
- `list_ai_provider_models(...)`
- `create_ai_provider_model(...)`
- `update_ai_provider_model(...)`
- 同步写入、计数统计、人工字段保护、引用感知删除

Task 3 的最终修复点也已经补齐：`metadata_json` 在存储边界重新走 canonical 校验，避免非白名单字段绕过规范化直接入库。

### 7.2 Task 4-22 已完成

后续任务按依赖顺序完成并通过回归验证：

- Task 4-8：池图与版本 CAS、Agent 双绑定、安全模型发现、异步同步 operation 及管理 API。
- Task 9-13：job owner/lease、attempt、Provider 完成语义、候选解析、PromptBudget 和阶段化故障转移。
- Task 14-17：共享 route session、全部 AI 生成调用链迁移、多批次 main pin 和 progress 语义。
- Task 18-22：基于保存快照的手工续接、Provider 目录 UI、模型池/Agent UI、日志审计详情、文档与静态门禁。

主要实现位于 `ai/model_pools.py`、`ai/model_sync.py`、`ai/model_router.py`、`storage/ai/pools.py`、`storage/ai/model_sync.py` 以及对应 service、API 和模板。完成提交与逐任务验证记录保存在 `docs/superpowers/plans/2026-07-23-ai-model-catalog-pools.md`。

## 8. 完成后的需求分析

### 8.1 架构结果

原计划的依赖顺序已经落地：结构化目录作为事实源，模型池负责有序候选与后备图，`ModelRouter` 统一解析预算和执行，业务 service 只消费路由契约，设置页与日志页分别承担管理和审计。

固定 Agent 保持原调用语义；池 Agent 才会跨 Provider 故障转移。首个正文前可切换，正文开始后统一收口 `partial`，避免不同模型正文被静默拼接。

### 8.2 安全与可追溯结果

模型发现复用安全请求边界；快照与 attempt 不保存 API Key、Prompt、正文、请求体或完整响应头。候选、网络请求、deadline、同步响应和池图均有硬限制。手工继续复用父 job 的不可变快照并校验版本与 Provider 配置 hash，不按当前池重新解析。

### 8.3 当前维护入口

第一阶段后续修改应先更新本需求基线和实施计划，再扩展测试。业务生成路径不得新增 `provider.stream_generate()` 直调；只有 Provider 实现、Router 内部和连接测试例外。第二阶段健康计数、冷却、权重与成本排序仍不在当前范围。

## 9. 验收口径

每次影响该主线的变更至少运行相关定向测试；合并前运行完整 `python -m pytest -q`、`python -m compileall -q src` 和 `git diff --check`。Schema 迁移、三类 Provider 发现、池图/CAS、路由终态、续接幂等、CSRF、隐私静态门禁与页面契约属于发布阻断项。

## 10. 当前统一结论

- “模型目录 + 模型池 + 统一路由 + 可审计任务链路 + 管理页面”第一阶段已经完成。
- 当前事实以代码、测试、`README.md`、frontend API/page 契约和已完成实施计划为准。
- 后续业务能力应复用 `ModelRouter` 契约，不直接读取模型池表或绕过路由器。
- 跨任务健康状态、冷却、权重轮询、成本排序和定时目录刷新仍是明确的后续范围，不得误标为已实现。

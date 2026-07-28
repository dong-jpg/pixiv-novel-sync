# AI 模型池与统一路由收尾设计

**状态：** 已确认
**日期：** 2026-07-28
**范围：** 完成模型主线 Task 4 及之后的第一阶段能力

## 1. 定位与边界

本文继承 `2026-07-27-ai-model-catalog-pools-unified-requirements.md`，并以当前已经完成的原子 Schema、规范化 DTO 和 Provider 模型目录存储（Task 1-3）为起点。目标是完成模型池、安全模型同步、统一路由、任务审计、手工续接、设置页和日志页，并把所有业务 AI 生成迁移到统一入口。

现有固定 Agent 的 ID、Provider、模型、Prompt、参数和调用语义保持不变。`ai_provider_models` 是运行时唯一事实源；`available_models_json` 只保留旧库迁移读取能力。第一阶段不实现跨任务健康计数、冷却、权重轮询、成本排序或后台定时模型刷新，也不新增第三方依赖。

## 2. 架构

- `storage/ai/pools.py` 负责模型池、成员、后备链、引用保护和版本 CAS。
- `storage/ai/model_sync.py` 负责同步 operation、generation、owner、lease、heartbeat、空结果确认和目录对账。
- `storage/ai/core.py` 扩展 AI job 的候选快照、attempt、owner CAS、`partial` 和路由详情投影。
- `ai/model_pools.py` 提供无副作用的池图校验、候选展开、循环保护和能力过滤。
- `ai/model_sync.py` 使用最多两个 worker 协调安全模型发现、取消、SSE 和启动恢复。
- `ai/model_router.py` 提供 `resolve_candidates()`、`execute()` 和 `execute_stream()`，统一 fixed/pool 解析、`PromptBudget`、故障转移与终态。
- AI service core 建立共享 route session；generation、chat wizard、projects 和关键词清洗只消费该接口，不读取模型池 SQL，也不直接调用 Provider。

## 3. 路由数据流

服务先读取 Agent 绑定，按成员位置和后备链展开候选，去重并校验能力。路由器以所有候选有效上下文窗口中的安全最小预算生成不可变 `PromptBudget`，候选切换不得扩大已截断输入。任何网络请求前必须创建 AI job，并保存候选快照及其 hash、池/Agent version、Provider 配置 hash、预算、owner、lease 和 deadline。

每次候选调用分配独立 attempt。`metadata` 和 `progress` 不触发正文开始；首个非空正文 `delta` 将 main 阶段固定到当前候选。正文开始前的模型级错误可继续下一候选；认证、配额、Provider 禁用或配置错误会短路同一 Provider 的剩余候选。正文开始后的错误、长度截断、内容过滤、缺失结束标记或传输中断收口为 `partial`，不得自动切换。

取消、客户端断开和 `GeneratorExit` 收口为 `cancelled`，不触发切换。`internal`、`main`、`validation` 分阶段记录；只有 main 正文中断使用 `partial`，validation 失败保持 `failed`。所有终态通过 owner/generation CAS 单调写入，迟到 worker 不得覆盖既有终态。

手工续接从父 job 的原候选快照派生新 job，提交 snapshot hash、下一个未尝试索引和幂等键。服务端不重新解析当前池；池/Agent version 或待尝试 Provider 配置 hash 变化时返回 `409`，要求用户重新确认范围。

## 4. 模型同步

OpenAI-compatible、Anthropic 和 xAI Provider 通过既有安全请求层发现模型。同步限制为单页 4 MiB、累计 20 MiB、最多 100 页、5000 个模型和 10 分钟。分页不完整、游标循环、响应格式错误、取消或超限均保留旧目录，不写缺失标记。

同步以独立 operation 运行，状态为 `queued`、`running`、`needs_empty_confirmation`、`succeeded`、`failed` 或 `cancelled`。非权威空目录必须精确匹配 operation、generation、Provider 配置 hash 和结果 digest 后二次确认。网络期间不持有数据库写事务；成功对账在一个 `BEGIN IMMEDIATE` 中完成。

## 5. API 与界面

设置 API 增加 Provider 模型列表、人工模型 CRUD、异步同步、operation 查询/SSE/取消/空确认、模型池 CRUD、成员整体替换和最近 attempt。Agent CRUD 增加 `binding_type`、`model_pool_id` 与能力要求。`POST /api/dashboard/ai/jobs/<job_id>/continue` 从父快照创建幂等续接任务。

设置页展示目录总数、上游可用数、可路由数、同步状态、人工模型、模型池顺序、后备池、引用关系、Agent fixed/pool 绑定和跨 Provider 隐私提示。日志页展示实际 Provider/模型、候选摘要、预算、attempt、`partial` 及“下一个模型继续”。页面刷新后通过 job 或 operation 查询恢复状态。

## 6. 错误、安全与资源限制

参数校验返回 `400`，缺失资源返回 `404`，版本、引用、owner 或活跃 operation 冲突返回 `409`；认证和 CSRF 复用 Dashboard 中间件。Provider 错误在写入数据库、日志或 SSE 前分类并脱敏，快照和 attempt 不保存 API Key、Prompt、正文、请求体或完整响应头。

模型发现复用 SSRF、DNS/IP 固定、Host/SNI、TLS 校验、代理和禁重定向策略。单池及展开链最多 64 个候选，后备链深度最多 8；每个 job 最多尝试 16 个候选、32 次网络请求、30 分钟，候选快照最多 256 KiB。池写入、成员替换、绑定、快照和终态使用短事务及 CAS，网络请求不得持有 SQLite 写锁。

## 7. 测试与验收

所有新增行为遵循 TDD。存储测试覆盖池图、循环、数量限制、引用保护、成员原子替换、CAS、attempt 分配、lease 和终态竞争；Provider 测试覆盖三类发现格式、分页、响应体限制、安全请求和空确认；Router 测试覆盖 fixed/pool、能力过滤、预算、故障转移、Provider 短路、取消、`partial` 和续接。

集成测试覆盖续写、改写、蒸馏、审计、摘要、向导、规划、章节、润色、状态、伏笔、Pipeline 和关键词清洗。AST/静态门禁确保除 Provider 连接测试外，业务代码不直接调用 `stream_generate()`。API 测试覆盖认证、CSRF、状态码、SSE 恢复、幂等和脱敏；UI 测试覆盖目录、同步、池编辑、绑定、隐私提示和任务详情。

阶段验收要求定向测试、完整 `python -m pytest -q`、`python -m compileall -q src` 以及桌面/移动端浏览器检查全部通过，不新增 skip，并修复当前救援目录后台线程测试 warning。

## 8. 后续依赖

本阶段完成并稳定后，偏好画像注入和成人局部润色只依赖 `ModelRouter` 的 DTO 与执行接口。成人模块不得读取模型池表或绕过 Router；任务/API 收敛可以复用本阶段形成的 operation、SSE、CAS 和统一错误模式。

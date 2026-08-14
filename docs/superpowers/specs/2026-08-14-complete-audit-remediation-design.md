# 全量审计异常与需求缺口整改设计

> 状态：已确认，进入分域实施
> 日期：2026-08-14
> 范围：2026-08-13 全项目审计确认的生产缺陷、静态问题，以及统一需求中有明确验收标准的 `MISSING/PARTIAL` 能力

## 1. 目标

本轮整改必须同时完成以下结果：

1. 修复审计报告中的全部 P0/P1 和可稳定复现的中优先级代码异常。
2. 补齐偏好分析、推荐闭环、AI 总结/解释、成人偏好注入和救援目录的明确需求缺口。
3. 删除确认无调用方的死代码和无用导入；兼容入口必须显式保留或提供迁移说明，不能误删。
4. 更新需求状态、API 契约、用户说明和审计报告，使文档只描述已验证事实。
5. 在隔离分支完成 TDD、迁移回归、全量测试、静态检查和分支核验，再合并 `main` 并推送。

## 2. 非目标与外部边界

- 不自动点赞、收藏、关注、评论或绕过 Pixiv 权限。
- 不实现统一需求明确标记为 `OUT` 的模型健康计数、成本排序、多模型投票等能力。
- 不把响应式、可访问性或生产 Cloudflare/systemd 的实际状态仅凭仓库标成已验收；仓库提供自动检查和人工验收清单，服务器结果仍需现场执行。
- 不把没有产品契约的“持续产品化”描述扩展为新功能。
- 不强制配置 AI Provider。AI 不可用时，本地画像、规则推荐和确定性解释必须继续成功。

## 3. 分支与集成策略

当前事实：仓库只有 `main`，本地 `main` 比 `origin/main` 领先 20 个提交，且存在上一轮审计文档的未提交改动。

整改使用以下分支：

- `codex/audit-remediation`：集成分支，保留审计文档并承载最终文档状态。
- `codex/runtime-integrity`：任务日志、分页、重试、文件/数据库一致性、部署和静态清理。
- `codex/recommendation-completion`：偏好范围、搜索计划、推荐发布、反馈、同步动作和 SSE。
- `codex/ai-preference-adult`：AI 总结/解释、成人偏好、成人取消和实时 progress。
- `codex/rescue-completion`：救援纠错/删除接线、组合筛选、性能和回归。

每个域分支从同一集成基线创建，在独立 worktree 中实现。域分支先合并到 `codex/audit-remediation`；集成结果通过全部门槛后再合并 `main`。合并前执行 `git fetch --prune origin`，逐一检查所有本地和远端分支的独有提交，不能仅按分支名盲目合并。禁止 force push。

## 4. 运行时完整性

### 4.1 task log 租约

`Database.init_schema()` 只负责幂等 DDL/迁移，不再改变任何运行中任务状态。

`task_logs` 增加可空的 `owner_token`、`heartbeat_at` 和 `lease_until`：

- 新任务以随机 owner token 创建日志并取得租约。
- `JobRunner` 在任务执行期间按固定间隔续租；进度/日志写入也顺带刷新 heartbeat。
- 正常、失败和取消收口必须同时匹配 log ID 与 owner token。
- 启动恢复只处理租约过期且 heartbeat 超过宽限期的记录。
- 旧记录没有 owner/lease 时不在任意请求中自动判失败；由保留期清理或显式兼容恢复处理。

恢复入口只在应用启动阶段执行一次，不能由普通路由的 `init_schema()` 间接触发。

### 4.2 分页保护

新增共享分页守卫，统一提供：

- 配置上限和不可关闭的安全上限；
- 标准化 cursor/`next_url` 指纹集合；
- 重复 cursor 立即停止并记录可读告警；
- 空页、无 next、达到上限和取消均是明确终止原因；
- 每次网络请求前后检查取消。

关注用户、用户小说、用户完整备份、收藏和系列分页全部复用该守卫。现有业务配置仍可设置更低上限，`None` 只能表示使用安全默认值，不能表示无限。

### 4.3 Provider 重试

OpenAI-compatible 与 Anthropic 的 stream 空响应 fallback 均继承 `AIProviderConfig.max_retries`，不再硬编码 override。Anthropic 普通非流式请求与 OpenAI-compatible 使用相同的 `max(0, configured)` 语义。

一次流请求切换到 fallback 本身不额外重置重试预算；`max_retries=0` 时最多执行一次 fallback HTTP 请求。请求守卫继续统计每次真实网络请求。

### 4.4 归档删除两阶段恢复

文件系统与 SQLite 无法组成原生事务，因此采用可恢复 trash 协议：

1. 验证所有目标路径位于允许存储根。
2. 将目标移动到同一存储根下 `.trash/<operation_id>/`，写入不含正文的 manifest。
3. 在 SQLite 事务中执行实体/关系/救援目录删除。
4. 数据库提交后清理 trash；清理失败保留 manifest 供后台重试。
5. 数据库失败时按 manifest 恢复原路径。
6. 启动恢复扫描未完成 manifest：数据库实体仍存在则恢复，否则完成清理。

直接小说/用户删除和 pending deletion 确认共用同一协调器。路径冲突、跨根路径、部分移动和恢复失败必须返回明确错误并保留 manifest，不允许静默丢失。

### 4.5 pending deletion 恢复

新增数据库域方法，在单个 `BEGIN IMMEDIATE` 中完成：

- 校验 pending 记录仍可恢复；
- 恢复 novel source 或 series subscription；
- 更新 pending 状态；
- 刷新受影响救援目录。

路由不再分别提交多个步骤。重复请求返回幂等结果或明确冲突。

### 4.6 部署与静态清理

- legacy install script 和 systemd unit 统一使用 `/opt/pixiv-novel-sync/app`、`pixivsync` 用户及 `app/.venv`。
- 增加只读部署契约检查，验证 User、WorkingDirectory、ExecStart、环境文件和安装脚本变量一致。
- 修复 `settings.py` 的可解析 `datetime` 注解。
- 删除确认无调用方的普通未使用导入。
- `SyncJobManager/SyncJobState` 若仍需外部兼容，放入显式兼容模块并由 `__all__` 声明；否则删除实现、重导出和只验证旧入口的测试。

## 5. 推荐发布与任务终态

### 5.1 取消传播

推荐任务检测取消后必须继续抛出 `InterruptedError`，由 `JobRunner` 统一收口为 `CANCELLED`。业务结果不再使用 `{"stopped": true}` 伪装正常返回。

### 5.2 run 级原子发布

推荐候选在 run 成功前仅保存在有界内存 staging 中，不写入正式 `recommendation_items`。搜索计划和单次候选数都设安全上限。

成功时调用一个 `BEGIN IMMEDIATE` 发布方法：

- 重新确认 run 仍为 `running`；
- 批量 upsert 正式推荐；
- 保留已有用户反馈/状态，不让新 run 覆盖用户动作；
- 更新 run 为 `succeeded` 和统计；
- 整笔提交。

异常或取消只更新 run 终态，不改变可见推荐。进程在发布事务中退出由 SQLite 自动回滚。

## 6. 偏好分析与搜索计划

### 6.1 分析范围

定义版本化 `PreferenceAnalysisScope`，支持：

- 全部归档；
- 收藏来源、公开/私密收藏、追更系列；
- 作者 ID、标签、开始/结束时间；
- 最小正文长度；
- 排除删除、不可见或无正文记录。

所有过滤在 SQL 中完成并使用参数绑定。增量累加器的 fingerprint 同时包含 scope 版本；scope 改变时不能误复用旧累计结果。

### 6.2 结构化画像

本地层始终生成完整、非空语义的版本化画像：标签、关键词、作者、长度、来源、系列比例、限制等级、热度、搜索策略和置信度。

可选 AI 层通过专用 `preference_summary` Agent 和 `ModelRouter` 执行：

- 输入只包含有界统计、抽样片段摘要和元数据，不发送整个资料库；
- 输出使用严格 JSON schema；
- 主题、关系、情境、语气、节奏和叙事模式必须有生产者、消费者与测试；
- Provider 不可用、JSON 无效或取消时保留本地画像，并在 job 结果中记录 `ai_summary_status`，不破坏本地成果。

### 6.3 搜索计划 CRUD

新增 `recommendation_search_plans`：profile 外键、名称、version、plan JSON、默认标记和时间戳。保存/编辑使用 CAS version；删除被运行中任务引用的计划返回冲突。

API 支持列表、详情、创建、更新、删除、设为默认和基于画像生成。服务端统一执行 query 去重、类型枚举、exclude terms、limit 和 filter 上限校验。

## 7. 推荐解释与反馈闭环

### 7.1 解释

规则层继续决定 score，并为每条候选生成确定性解释、命中项和风险说明。

可选 `recommendation_explanation` Agent 只批量润色解释：

- 不得修改 score、过滤结果、item identity 或风险字段；
- 严格按候选 ID 返回 JSON；缺项使用规则解释；
- Provider 失败时整个 run 仍可成功；
- 最终保存 `explanation_source=local|ai` 和脱敏模型快照。

### 7.2 反馈、屏蔽和队列

反馈类型使用固定枚举：`interested`、`dismissed`、`read_later`、`sync_later`、`synced`。屏蔽继续使用 `author|tag`，API、存储和 UI 使用同一枚举。

提供：

- 反馈列表、创建/更新、删除；
- 屏蔽作者/标签和取消屏蔽；
- 待阅读、待同步筛选；
- 单篇/系列立即同步；
- 推荐历史、反馈和屏蔽的用户可控删除入口。

立即同步复用共享 JobRunner 和现有 Pixiv 登录/归档/救援刷新能力。HTTP 请求只提交异步 job，不阻塞直到下载完成。下一次推荐必须排除 dismiss 和 mutes，并按 series ID 跨 run 去重。

### 7.3 SSE 与恢复

新增需求声明的分析、搜索计划和推荐 stream 端点。它们订阅共享 job 的有界事件记录，只发送阶段、计数、可读错误和终态，不发送正文、Prompt 或 key。

页面刷新后可通过 job ID 查询状态；SSE 断开不自动取消后台 job，显式取消端点才发出取消请求。所有流以 `done`、`cancelled` 或 `error` 终止。

## 8. 成人润色整改

### 8.1 端到端取消

成人主写作和两项审查的每个 `AdultRouteRequest` 都接收 owner/job/owner-token scoped `is_cancelled` 回调。回调只读取最小状态；取消或 owner 失效时关闭底层 route iterator，ModelRouter 将 job 和 attempt 收口为 `cancelled`。

显式取消、客户端断开、路由取消和 review 阶段取消必须产生一致终态。任何取消路径都不得发送未完成候选。

### 8.2 实时 progress 与候选隔离

成人服务直接消费 `ModelRouter.execute_stream()`：

- `progress` 实时白名单转发；
- `delta` 只追加到服务端有界缓冲，不发送浏览器；
- completion 后执行固定安全、事实和结构校验；
- 通过后才持久化并发送 candidate；
- 断连关闭 iterator 并触发取消。

review 阶段也把路由 progress 标记为 `safety` 或 `fact_guard` 后实时发送。

### 8.3 成人偏好注入

阅读页允许选择画像和 `off|light|standard|strong`。服务端复用现有 preference context 构建器，只注入摘要和结构化偏好，不发送画像正文证据。

profile ID、强度和 preference context hash 进入审计输入与 apply 快照；画像删除或内容变化导致 apply 返回 409 并要求重新生成。`off` 或缺失画像保持当前 Prompt。

## 9. 救援目录完成度

以 `2026-07-21-rescue-catalog-sources-design.md` 为验收事实：

- 人工 novel/series 纠错后刷新对象、父系列和相关章节；
- 小说、系列和用户删除走统一删除协调器并同步清理目录；
- 列表支持 state、content kind、source、search、stale 的组合筛选；
- 多来源采用包含语义；
- 未初始化返回 503，stale 明确返回并展示；
- 列表保持常数查询，不读取正文；
- 增加 4,593 项基准 fixture，第一页查询目标小于 500ms，完整刷新目标小于 10 秒；性能断言使用查询计数和宽松本地时间门槛，生产目标由运维脚本实测。

userscript 和 `/api/rescue/v1/` 字段白名单不变；任何后台目录优化都不能降低单项实时资格校验。

## 10. 错误处理与兼容性

- 新迁移幂等执行并在结束时跑 `PRAGMA foreign_key_check`。
- 所有新增 JSON 字段有大小、类型、深度和枚举限制。
- 旧画像、推荐项和 job 可读取；缺少新字段时使用明确默认值。
- 新 API 使用现有 `{ok, data, error}`、CSRF、分页和状态码约定。
- SQLite busy、Provider 错误、Pixiv 错误和取消在 job 中使用可区分终态，不把内部路径、key、Prompt 或正文写入公共错误。
- userscript/API、数据库迁移和已有外部导入兼容均有回归测试。

## 11. 测试策略

每项行为严格执行 Red-Green-Refactor：先提交能够复现旧错误的测试并确认失败，再写最小实现。

测试分层：

1. 存储：迁移幂等、租约、推荐发布、search plan CAS、反馈枚举、trash 恢复、pending restore。
2. 服务：取消传播、分页重复 cursor、AI 降级、解释不改分数、成人 delta 隔离与实时 progress。
3. Web：新 API/SSE、CSRF、状态码、详情恢复、立即同步异步语义。
4. 前端静态/浏览器：范围选择、计划编辑、反馈/屏蔽/队列、成人偏好、无重叠和移动端基本流程。
5. 救援：组合筛选、增量刷新、删除接线、查询计数和性能 fixture。
6. 部署：install script 与 unit 契约测试。

最终门槛：

- 所有新增定向测试通过；
- `python -m pytest -q` 全量通过；
- `python -m compileall -q src tests` 通过；
- `pyflakes src` 无未定义名，剩余兼容导出有显式说明；
- `git diff --check` 和文档链接检查通过；
- SQLite `foreign_key_check` 为空；
- 浏览器桌面/移动端关键流程截图与控制台无错误；
- 合并到 `main` 后重新运行全量测试；
- push 后确认本地 `main`、`origin/main` 和远端 head 一致。

## 12. 文档与状态收口

- `AUDIT_REPORT_2026-08-13.md` 为每项 finding 增加修复提交、测试和状态，不删除历史证据。
- `PREFERENCE_RECOMMENDER_REQUIREMENTS.md` 和 `UNIFIED_PROJECT_REQUIREMENTS.md` 只在对应验收通过后改为 `IMPLEMENTED/DONE`。
- 更新 `frontend-api-contract.md`、`frontend-pages.md`、README 和成人指南。
- 增加部署验收与推荐/偏好用户操作说明。
- 任何只能由服务器确认的结果继续标为“待现场验证”，并列出命令和预期输出。

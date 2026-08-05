# 项目审计问题完整修复设计

> 状态：用户已确认修复全部审计问题，并要求把成人描写局部润色 Agent 纳入同一轮交付
> 日期：2026-08-05
> 成人功能详细规格：[`2026-07-23-adult-polish-agent-design.md`](2026-07-23-adult-polish-agent-design.md)

## 1. 目标

本轮修复审计发现的真实运行风险、需求缺口和文档契约偏差：统一自动与手动任务执行路径，补齐可取消等待与分页安全上限，完善推荐持久化和跨 run 系列去重，把偏好画像接入全部指定 AI 创作入口，按既有完整规格交付成人描写局部润色 Agent，并让当前 API 文档重新与代码一致。

修复必须保持现有 CLI、Dashboard 页面、同步任务状态响应和用户数据兼容。数据库迁移必须幂等，旧数据使用保守默认值，不能读取真实 `data/`、`.env` 或已保存密钥进行测试。

## 2. 方案选择

### 方案 A：仅做局部补丁

在旧自动调度、推荐和 AI prompt 路径分别增加条件分支。改动较少，但继续保留两套任务实现、重复取消逻辑和分散的画像注入，后续仍容易产生行为漂移。

### 方案 B：共享边界内逐步收敛（采用）

保留现有公开 API 和页面状态形状，把执行、取消、偏好上下文和成人安全检查收敛到可测试的共享服务。自动调度只负责计算何时提交任务；共享 `JobSpec`、`JobRunner` 和 `execute_task` 负责实际执行。成人功能复用现有 `ModelRouter`、AI job、项目/章节和 Provider 管理，不另建孤立系统。

### 方案 C：一次性重写任务与 AI 工作台

可以获得更统一的内部结构，但会扩大回归面，破坏已稳定的 893 项测试基线，也超出本轮审计修复所需范围。

## 3. 修复单元与边界

### 3.1 可取消等待与循环上限

- `RecommendationService` 接收并保存 `stop_requested`，所有分页等待调用 `RateLimiter.wait(stop_requested=...)`；调用方取消时抛出或传播 `InterruptedError`，由共享 runner 收口为 `cancelled`。
- Pixiv 重试退避不再直接执行不可中断的 `time.sleep`。`retry_on_pixiv_error` 接收可选取消回调，并使用小间隔等待；未传回调时保持原有调用兼容。
- 自动调度迁移前仍存在的等待点必须使用同一取消协议；迁移完成后删除不再可达的重复同步实现。
- `BookmarkNovelSyncService.check_bookmarks_existence` 使用与 `check_all_existence` 相同的分页安全上限。达到上限时停止继续请求并返回已确认结果，同时记录清晰警告，不允许异常 `next_query` 导致无限循环。

### 3.2 推荐结果完整性

- `recommendation_items` 增加 `x_restrict INTEGER NOT NULL DEFAULT 0` 与 `risk_notes_json TEXT NOT NULL DEFAULT '[]'`，读取层分别暴露 `x_restrict: int` 和 `risk_notes: list[str]`。
- 推荐候选保存限制等级和确定性风险说明；风险说明属于解释数据，不替代既有限制等级过滤，也不让 LLM 独占排序。
- 历史过滤状态同时返回 novel ID 与 series ID 集合。候选属于已有推荐系列时，即使 novel ID 不同也必须跨 run 排除。
- 同一 run 的 `seen_series` 仍保留，作为批内去重；数据库历史集合负责跨 run 去重。
- 迁移对旧推荐项使用 `x_restrict=0`、空风险数组，维持现有 API 消费者兼容。

### 3.3 偏好画像注入

- AI 写作项目增加可空 `preference_profile_id` 与 `preference_injection_strength`；强度只允许 `off`、`light`、`standard`、`strong`，默认 `off`，旧项目行为不变。
- 建立单一偏好上下文构造器，从指定画像读取摘要和结构化偏好。它不拼接样本文本、正文证据或敏感原文。
- 四种强度使用稳定的字段白名单和长度预算：`off` 不输出画像块；`light` 仅摘要和最强偏好；`standard` 输出主要正向/负向偏好；`strong` 在相同结构基础上提高条目预算，不改变系统与事实约束优先级。
- 创作向导、长篇规划、章节细纲扩写、章节续写、Pipeline、普通润色、去 AI 味、内容审计和成人局部润色都通过同一构造器接入。画像缺失或已删除时返回明确校验错误；Pipeline 未选择画像时继续正常运行。
- Dashboard 的项目设置和向导提供画像选择与强度控制，prompt 预览只显示最终注入摘要，不展开画像采样正文。

### 3.4 自动调度统一

- `AutoSyncScheduler` 继续负责 cron/interval 计算、单实例生命周期、下次运行时间和任务启停，不再拥有同步业务实现。
- 到期任务转换为 `JobSpec(source=JobSource.SCHEDULER)` 并提交共享 `JobManager`；实际任务由 `JobRunner` 顺序调用 `execute_task`。
- 自动任务保留当前任务名、`is_auto_sync` 日志标识、状态查询与取消 API 形状。状态适配层把共享 `JobState` 映射到已有 Dashboard 字段，避免前端迁移与后端执行迁移耦合。
- 只有在所有自动任务类型都有共享 `execute_task` 实现和回归测试后，才删除 `_sync_bookmarks` 等 legacy 私有执行方法和生产零调用的 legacy worker 入口。
- 调度器停止、任务取消、应用关闭和异常收口统一使用共享终态规则，不能把 `InterruptedError` 标成失败，也不能重复写终态日志。

### 3.5 成人描写局部润色 Agent

成人功能完整遵循现有 [`2026-07-23-adult-polish-agent-design.md`](2026-07-23-adult-polish-agent-design.md) 和 [`../plans/2026-07-23-adult-polish-agent.md`](../plans/2026-07-23-adult-polish-agent.md)，不采用删减版。关键不可弱化边界如下：

- 仅处理用户明确选择的一个连续片段，前后文只读，不进入默认章节 Pipeline。
- 只允许已认证 Dashboard 会话；未配置认证、owner 不匹配或仅凭可猜 job ID 的请求全部拒绝。
- 参与角色必须在结构化项目角色档案中确认 `age_years >= 18` 且为虚构人物；年龄、身份、关系或参与者无法确定时 fail-closed。
- 写作、固定 `adult_safety_review`、固定 `adult_fact_guard` 三阶段都通过现有 `ModelRouter`。策略与 JSON Schema 是服务端只读资源，普通 Agent CRUD 不能修改或绕过。
- Provider delta 只在服务端内存缓冲。主生成部分输出、审查不可用或任一安全阶段失败时丢弃候选，SSE、日志和长期存储不得泄露正文副本。
- 候选通过全部检查后才生成可见差异。应用时在 `BEGIN IMMEDIATE` 内重验 owner、章节 revision、正文/片段 hash、角色事实、策略 hash、binding hash、Provider scope 与 warning acknowledgment，只替换目标范围。
- 项目角色确认、章节 revision、审查绑定、成人 job/application 审计元数据使用幂等迁移；旧项目默认关闭成人功能，不自动把旧角色视为成年或虚构。
- 章节详情新增同级工具页签，使用 Unicode 码点偏移，覆盖中文、emoji、组合字符和 CRLF/LF；候选以文本节点渲染，不使用 `innerHTML`。

## 4. 数据流

### 4.1 普通后台任务

`scheduler/route/CLI -> JobSpec -> JobManager -> JobRunner -> execute_task -> domain service -> storage`

所有进度、取消和终态从 domain service 向共享 runner 汇聚，Web 只做鉴权、参数校验和状态适配。

### 4.2 偏好注入

`payload/project settings -> preference profile lookup -> bounded preference context -> task prompt builder -> ModelRouter`

Prompt 构造器必须保持事实、系统安全策略和任务输出格式高于用户偏好；偏好只影响倾向，不得覆盖安全、角色或结构约束。

### 4.3 成人局部润色

`authenticated selection -> deterministic preflight -> buffered writing route -> fixed safety review -> fixed fact guard -> atomic application record -> diff preview -> explicit apply`

只有全部审查通过的完整候选能进入 application 记录与浏览器。任何不确定状态都进入阻断终态，且不保留候选正文。

## 5. 错误与兼容策略

- 请求参数、画像不存在、角色资格或成人认证不满足时，在任何 Provider 调用前返回中文错误。
- 取消统一成为 `cancelled`；首字后失败成为现有允许的 `partial`，但成人候选在 `partial` 下永不可见、不可应用。
- 调度和推荐的存储迁移失败必须整笔回滚；迁移结束执行外键检查。
- 成人应用中的并发修改、策略/binding/Provider scope 变化返回 `409`，不自动把旧候选发送给新 Provider 重审。
- 现有非成人润色、同步 API、CLI 参数和旧数据库读取保持兼容。

## 6. 测试设计

所有行为修改遵循 RED-GREEN-REFACTOR：先加入最小失败回归测试并确认失败原因，再写生产代码。

- 循环与取消：分页上限、分页等待中取消、retry backoff 中取消、自动任务取消终态。
- 推荐：迁移默认值、字段 round-trip、跨 run series ID 去重、同 run 去重及旧数据兼容。
- 偏好注入：四种强度、画像缺失、全部指定 AI 入口、Pipeline 无画像降级、UI payload 与预览。
- 调度：自动任务生成正确 `JobSpec`、使用 `JobSource.SCHEDULER`、复用共享执行、状态映射、停止和重复调度保护。
- 成人 Agent：沿用既有规格第 16 节的 domain、storage、routing、security、concurrency、SSE、apply 和页面测试矩阵，并增加偏好注入组合测试。
- 文档：路由契约测试覆盖代码实际注册的前端依赖 API，避免主契约再次落后。

最终验证包括相关测试分组、完整 pytest、`python -m compileall -q src`、`git diff --check` 和工作区状态检查。全量测试若受外部环境依赖影响，必须准确记录失败而不能以分组通过代替。

## 7. 实施顺序

1. 可取消等待和分页上限。
2. 推荐字段与跨 run 系列去重。
3. 偏好上下文构造器、存储字段和普通 AI 入口接线。
4. 自动调度迁移到共享 job 执行路径，并清理已证实无调用的 legacy 执行代码。
5. 按既有 11 个任务完整实现成人局部润色 Agent，同时接入偏好上下文。
6. 更新当前 API/页面/统一需求文档，运行完整回归与静态检查。

## 8. 验收标准

- 审计列出的循环、取消、推荐、偏好注入、调度重复实现、成人 Agent 和 API 文档问题都有对应测试与代码/文档变更。
- 自动与手动同步的核心任务使用同一执行函数和终态语义。
- 推荐项能够持久化限制等级和风险说明，系列不会在不同 run 中重复推荐。
- 所有需求指定的普通 AI 创作入口支持同一画像选择和四档注入强度。
- 成人 Agent 满足既有设计第 17 节全部验收标准，任何安全或身份不确定情况 fail-closed。
- 当前 API 契约与实际前端依赖及 Flask 路由一致。
- 定向与全量测试无新增失败。

## 9. 自检

- 范围：覆盖审计确认的七类问题，成人功能引用已确认完整规格而非重新定义缩水范围。
- 一致性：推荐字段名采用需求文档的 `risk_notes`；成人路由、策略与迁移继续以既有成人规格为准。
- 兼容性：旧项目默认关闭新行为，公开 API 保持兼容，调度状态通过适配层迁移。
- 安全：成人内容、认证、owner、Provider 隐私范围、日志和原子应用均为 fail-closed。
- 占位符：本文无待定项、临时实现或未选择方案。

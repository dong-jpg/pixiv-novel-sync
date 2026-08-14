# 成人局部润色使用指南

本文说明当前版本成人局部润色 Agent 的配置、使用边界和故障排查。它是面向 Dashboard 用户的操作文档；字段级 API 约定仍以 [frontend-api-contract.md](frontend-api-contract.md) 为准。

## 功能边界

- 只处理章节中用户明确选择的一个连续片段。前后文只作为只读事实和风格上下文，不会自动提交整章、多片段或普通 Pipeline。
- 参与者必须是已确认的成年虚构角色。年龄依据、角色 revision、章节 revision、锁定词和目标范围任一校验失败都会拒绝生成或应用。
- 写作、`adult_safety_review` 和 `adult_fact_guard` 均经统一 ModelRouter。候选正文在服务端完成事实、安全、结构和策略校验前不会发送到浏览器。
- 成人功能始终要求已配置 `DASHBOARD_TOKEN` 的 Dashboard 会话，即使请求来自 localhost。首次获取 scope 和创建 job 不需要 job token；job 创建后，SSE `metadata` 会签发有效期 10 分钟且绑定 owner/job 的 signed access token，后续读取、状态重放、取消、重新生成和应用必须携带它。

## 配置顺序

1. 设置稳定的 `DASHBOARD_TOKEN`，登录 `/dashboard`。
2. 在 `/dashboard/settings` 配置 Provider，确认 key 已加密保存。
3. 同步 Provider 模型目录，或添加手工模型；按需要创建 fixed/pool binding。
4. 创建或启用 `task_type=adult_polish` 的 Agent。
5. 为 `safety` 和 `fact_guard` 分别配置支持 `json` 的固定模型或模型池。两个 binding 都必须存在，且不能通过普通 Agent CRUD 跳过。
6. 在项目中建立结构化虚构角色，填写年龄和年龄依据；打开成人确认并确认当前角色 revision。
7. 在章节阅读页打开“成人润色”页签，选择连续片段，先查看并确认 Provider scope，再提交生成。

Provider scope 可能覆盖多个 Provider。模型池故障转移可能把同一 Prompt 发送给多个 Provider，确认前应检查数据范围和服务商策略。

## 生成、恢复与应用

- 请求使用原始章节文本计算 Unicode code point 范围和 hash；不要先规范化换行，也不要把 `target_text`、前后文或 Prompt 作为请求字段提交。
- SSE 只允许 `metadata`、`progress`、`validation`、`candidate`、`done`、`error` 六类白名单事件。元数据和错误会过滤敏感字段，但 `candidate` 是完成全部校验后的完整候选正文，`metadata`/`candidate` 还可能携带短期 access token；两者都应按敏感数据处理。
- 网络中断后，可在 token 有效期内调用同一 job 的 signed events 接口读取一次当前数据库快照。它可能重放已完成候选，`running` 时只返回当前状态后结束，不会续接原 Provider SSE；仍在运行时需要稍后再次查询。刷新页面或丢失/过期 token 后不能依赖该接口恢复。
- `warning` 只允许在界面展示 scoped `warning_ack_hash` 后确认；blocking code、过期 revision、Provider scope 变化或 409 冲突必须重新生成/重新确认。
- 应用候选前，服务端会在事务内重新验证章节 revision、正文和片段 hash、角色事实、策略、binding、owner 和 warning acknowledgment。应用只替换目标区间，不会自动写入普通 Pipeline。
- 未应用候选默认最多保留三天；启用后台调度时每小时执行一次清理，也可通过 AI job cleanup API 手工触发。应用成功后 job 的候选正文会清理，应用记录只保留 hash、校验摘要、策略和 Provider/model snapshot 等元数据。

## 常见问题

| 现象 | 处理 |
|---|---|
| HTTP `403` / 成人访问凭证无效 | 设置 `DASHBOARD_TOKEN`、登录 Dashboard；访问已有 job 时使用当前 job 返回且尚未过期的 signed access token。 |
| Provider scope 过期或 hash 不匹配 | 回到设置页确认三个阶段的 binding，重新获取 scope 后再生成。 |
| 角色/章节 revision 冲突 | 重新打开章节，确认当前角色 revision，并重新选择片段。 |
| HTTP `409` / warning 确认已失效 | 阅读当前 warning 摘要并使用当前 job 的 `warning_ack_hash`；不要复用其他 job 或旧候选的 hash。 |
| `route_unavailable` / `validation_failed` | 检查 Provider key、模型能力（尤其 `json`）、模型池成员和错误日志中的脱敏摘要。 |
| 连接中断 | 在 token 有效期内用 signed events 查询当前快照；它不是原流续订。候选未完成全部校验时不会持久化或允许应用。 |

成人审计输入和应用记录不会保存原目标片段、前后文、完整 Prompt、Provider 原始响应或 API key。通过校验但尚未应用的候选会临时保存在 owner-scoped `ai_jobs.output_text`，应用成功或保留期清理后删除。生产部署仍需由服务器配置和反向代理实际核验，仓库文档不能代替部署检查。

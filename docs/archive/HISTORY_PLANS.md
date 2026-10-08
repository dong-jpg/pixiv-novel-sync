# 历史执行计划整合

本页单独整合已实施、被替代、停止原文执行的历史计划；不与历史审计报告混成一份“全部完成”总结。

## 状态解释

- **历史已实施**：保留实现与验收记录；空复选框可能是未回写，不能自动补勾。
- **被替代**：旧入口只用于追溯，以后续计划或实际代码为准，不重复开工。
- **OUT**：经现有需求决策明确不做；不能把OUT变成“已实现”。
- **环境验收**：Cloudflare证书、真实服务端、生产灰度/性能观察必须在对应环境执行，不能用离线测试代替。

## 合并后的历史路线

| 时间/计划组 | 归并说明 | 当前依据 |
|---|---|---|
| 06月Job队列/CLI/Web/取消 | 同一任务执行与取消体系的阶段性演进 | JOB_SYSTEM + 回归测试 |
| 06月Qwen/模块化 | 写作检索与存储职责的历史实现 | ai-writing保留写作，main有意剥离 |
| 07月UI/封面/项目页 | ai-writing 的写作页面基础阶段，不等于后续交互已验收；main 不收录 | ai-writing 的页面/接口契约 |
| 07月成人/模型目录 | 成人仅属 ai-writing；模型目录/池/路由是两分支共享基础设施 | 对应分支活跃指南 + 后续整改 |
| 07月HTTPS/救援 | 仓库实现与现网验收必须分开 | 部署/救援指南 |
| 08-04/05文档与审计 | 一次性整理与修复记录 | 本轮整改状态 |
| 08-14四份计划 | 不再按原文执行；部分转09-14，其他按OUT | UNIFIED §1.3 |
| 08-28总计划 | 被phase1/2/3替代 | 吞吐/调度/设置三个阶段 |
| 09-03设置可操作性 | 主体已实现，真实Provider/交互仍需对应验收 | 模型路由与设置指南 |

## 当前执行入口（不归档）

- [09-14原任务与验收要求](../superpowers/plans/2026-09-14-ai-writing-split-and-audit-remediation.md)
- [09-30执行台账](../superpowers/plans/2026-09-30-prioritized-completion.md)
- [当前整改状态](../REMEDIATION_STATUS_2026-09-30.md)

## 原计划索引

main 有意剥离的写作材料只保留跨分支文本位置，不建立指向不存在文件的链接，也不复制回来。下列已存在的共享/归档计划保持原路径。

以下按路径中的历史日期列出用于定位；准确首次提交时间以此前Git审计台账为准，不用文件名日期冒充创建时间。

- ai-writing 分支：AI_WRITING_STUDIO_PLAN.md（原路径 `docs/AI_WRITING_STUDIO_PLAN.md`；main 不收录）
- [CRITICAL_BUGS_FIX_PLAN.md](CRITICAL_BUGS_FIX_PLAN.md)
- [MODULARIZATION_PLAN.md](MODULARIZATION_PLAN.md)
- [OPTIMIZATION_PLAN_2026-06-30.md](OPTIMIZATION_PLAN_2026-06-30.md)
- [ACTION_CHECKLIST.md](ACTION_CHECKLIST.md)
- [2026-06-06-qwen-embedding-robustness.md](superpowers/plans/2026-06-06-qwen-embedding-robustness.md)
- [2026-06-08-cli-job-services.md](superpowers/plans/2026-06-08-cli-job-services.md)
- [2026-06-08-unified-job-queue.md](superpowers/plans/2026-06-08-unified-job-queue.md)
- [2026-06-08-web-jobspec-runner.md](superpowers/plans/2026-06-08-web-jobspec-runner.md)
- [2026-06-26-job-cancellation-hardening.md](../superpowers/plans/2026-06-26-job-cancellation-hardening.md)
- [2026-07-14-release-blocker-fixes.md](../superpowers/plans/2026-07-14-release-blocker-fixes.md)
- ai-writing 分支：2026-07-16-ai-cover-style-controls.md（原路径 `docs/superpowers/plans/2026-07-16-ai-cover-style-controls.md`；main 不收录）
- ai-writing 分支：2026-07-16-ai-page-layout-refactor.md（原路径 `docs/superpowers/plans/2026-07-16-ai-page-layout-refactor.md`；main 不收录）
- [2026-07-16-documentation-cleanup-verification.md](../superpowers/plans/2026-07-16-documentation-cleanup-verification.md)
- [2026-07-16-preference-task-log-closure.md](../superpowers/plans/2026-07-16-preference-task-log-closure.md)
- ai-writing 分支：2026-07-17-ai-project-overview-single-panel.md（原路径 `docs/superpowers/plans/2026-07-17-ai-project-overview-single-panel.md`；main 不收录）
- [2026-07-20-cloudflare-https.md](../superpowers/plans/2026-07-20-cloudflare-https.md)
- [2026-07-21-rescue-library-userscript.md](../superpowers/plans/2026-07-21-rescue-library-userscript.md)
- [2026-07-22-rescue-catalog-sources.md](../superpowers/plans/2026-07-22-rescue-catalog-sources.md)
- ai-writing 分支：2026-07-23-adult-polish-agent.md（原路径 `docs/superpowers/plans/2026-07-23-adult-polish-agent.md`；main 不收录）
- [2026-07-23-ai-model-catalog-pools.md](../superpowers/plans/2026-07-23-ai-model-catalog-pools.md)
- [2026-08-04-github-readme-and-logo-refresh.md](../superpowers/plans/2026-08-04-github-readme-and-logo-refresh.md)
- [2026-08-05-project-audit-remediation.md](../superpowers/plans/2026-08-05-project-audit-remediation.md)
- ai-writing 分支：2026-08-14-ai-preference-adult-remediation.md（原路径 `docs/superpowers/plans/2026-08-14-ai-preference-adult-remediation.md`；main 不收录）
- [2026-08-14-recommendation-completion.md](../superpowers/plans/2026-08-14-recommendation-completion.md)
- [2026-08-14-rescue-completion.md](../superpowers/plans/2026-08-14-rescue-completion.md)
- [2026-08-14-runtime-integrity-remediation.md](../superpowers/plans/2026-08-14-runtime-integrity-remediation.md)
- [2026-08-28-phase1-sync-throughput.md](../superpowers/plans/2026-08-28-phase1-sync-throughput.md)
- [2026-08-28-phase2-schedule-budget.md](../superpowers/plans/2026-08-28-phase2-schedule-budget.md)
- [2026-08-28-phase3-settings-ai-pages.md](../superpowers/plans/2026-08-28-phase3-settings-ai-pages.md)
- [2026-08-28-sync-throughput-and-budget.md](../superpowers/plans/2026-08-28-sync-throughput-and-budget.md)
- [2026-09-03-ai-settings-operability.md](../superpowers/plans/2026-09-03-ai-settings-operability.md)

- [首次提交与全分支审计台账](../PLAN_AUDIT_2026-09-30.md)
- [早期小说页需求](../../.monkeycode/specs/novel-tabs-and-user-detail/requirements.md)
- [早期小说页设计](../../.monkeycode/specs/novel-tabs-and-user-detail/design.md)

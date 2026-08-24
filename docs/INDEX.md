# 项目文档索引

**项目**: Pixiv Novel Sync
**维护者**: dong-jpg
**最近更新**: 2026-08-24

---

当前文档分为三部分：**活跃参考**（顶层，持续维护）、**开发计划**（superpowers/，进行中的设计）、**历史归档**（archive/，已完成不再维护）。

## 活跃参考文档（顶层）

### 入口与审计

| 文档 | 用途 |
|------|------|
| [../README.md](../README.md) | 项目入口：功能介绍、快速开始、配置说明 |
| [../CLAUDE.md](../CLAUDE.md) | 开发约定：命令、架构分层、代码风格（根目录 `AGENTS.md` 已于 2026-08-20 删除，约定统一收在此处） |
| [UNIFIED_PROJECT_REQUIREMENTS.md](UNIFIED_PROJECT_REQUIREMENTS.md) | 全项目统一需求、实现状态与来源追溯 |
| [AUDIT_REPORT_2026-07-02.md](AUDIT_REPORT_2026-07-02.md) | 审计：修复 8 类严重 bug + 5 类中等问题 |
| [AUDIT_REPORT_2026-07-03.md](AUDIT_REPORT_2026-07-03.md) | 审计：EPUB 回归修复 + 死代码清理 + 文档整改 |
| [AUDIT_REPORT_2026-08-13.md](AUDIT_REPORT_2026-08-13.md) | 最新一轮全项目审计：任务终态、推荐发布、成人路由取消、分页边界与需求覆盖 |

> 2026-08-20 的提交 `ed081db` 修复了一次生产事故（`novel_status` 被 Pixiv 限流时把 5499 篇仍存在的小说误判为已删除），引入三态状态判定、双熔断、分批轮转与 `partial` 任务终态。该事故没有独立审计报告，行为说明见 [JOB_SYSTEM.md](JOB_SYSTEM.md) 第 3.5 节。

### API 与前端契约

| 文档 | 用途 |
|------|------|
| [frontend-api-contract.md](frontend-api-contract.md) | 前端依赖的后端端点契约 |
| [frontend-pages.md](frontend-pages.md) | 前端页面/模板/路由清单 |
| [library-os-style-guide.md](library-os-style-guide.md) | 前端视觉设计系统指南 |

### 功能设计

| 文档 | 用途 |
|------|------|
| [PREFERENCE_RECOMMENDER_REQUIREMENTS.md](PREFERENCE_RECOMMENDER_REQUIREMENTS.md) | 偏好推荐系统需求规格 |
| [QWEN_EMBEDDING_INTEGRATION.md](QWEN_EMBEDDING_INTEGRATION.md) | Qwen embedding 检索配置指南 |
| [ADULT_POLISH_USER_GUIDE.md](ADULT_POLISH_USER_GUIDE.md) | 成人局部润色 Agent 的配置、使用、恢复和故障排查 |
| [MODEL_ROUTING_GUIDE.md](MODEL_ROUTING_GUIDE.md) | AI 模型目录/模型池/统一路由用户指南（发现、绑定、failover、排错） |
| [RESCUE_USER_GUIDE.md](RESCUE_USER_GUIDE.md) | 救援功能用户指南（userscript、Token、目录筛选、只读 API、排错） |
| [JOB_SYSTEM.md](JOB_SYSTEM.md) | 任务系统开发者文档（管线、状态机、取消协议、新增 task_type、auto_sync 配置） |

## 开发计划（superpowers/）

归档口径说明：`superpowers/` 下的 plans/specs 一律**留在原目录**、在本索引标注状态（进行中 / 已完成），不再移动到 `archive/`；只有顶层一次性文档才进入 `archive/`。已完成条目仅作实施记录，当前行为仍以代码与活跃参考文档为准。

### 进行中

> **2026-08-24 核实结论：下列 4 份实施计划均未开始实施。** 抽查交付物全部缺失：`refresh_rescue_entities()`、`recommendation_search_plans` 表、`JobType.RECOMMENDATION_SYNC`、task log owner lease、`explanation_source` 字段在代码中均不存在；计划引用的 7 个测试文件（`test_rescue_catalog_refresh` / `test_rescue_catalog_filters` / `test_recommendation_search_plans` / `test_recommendation_feedback` / `test_recommendation_sync` / `test_preference_streams` / `test_task_log_leases`）都没有创建。计划内 checkbox 也全为未勾选。
>
> 提交 `104c717 fix: full audit remediation` 完成的是 2026-08-05 那一轮整改，**不是**这 4 份计划。因此它们描述的是**目标状态，不是当前行为**——阅读代码时不要以此为准。

| 文档 | 说明 |
|------|------|
| [superpowers/specs/2026-08-14-complete-audit-remediation-design.md](superpowers/specs/2026-08-14-complete-audit-remediation-design.md) | 2026-08-13 审计的完整整改设计（本轮主线设计） |
| [superpowers/plans/2026-08-14-runtime-integrity-remediation.md](superpowers/plans/2026-08-14-runtime-integrity-remediation.md) | 运行时完整性整改实施计划（未开始） |
| [superpowers/plans/2026-08-14-rescue-completion.md](superpowers/plans/2026-08-14-rescue-completion.md) | 救援目录收尾实施计划（未开始） |
| [superpowers/plans/2026-08-14-recommendation-completion.md](superpowers/plans/2026-08-14-recommendation-completion.md) | 推荐系统收尾实施计划（未开始） |
| [superpowers/plans/2026-08-14-ai-preference-adult-remediation.md](superpowers/plans/2026-08-14-ai-preference-adult-remediation.md) | AI 偏好注入与成人 Agent 整改实施计划（未开始） |

### 已完成

| 文档 | 说明 |
|------|------|
| [superpowers/plans/2026-06-26-job-cancellation-hardening.md](superpowers/plans/2026-06-26-job-cancellation-hardening.md) | 任务取消硬化计划（已实施，取消协议详见 [JOB_SYSTEM.md](JOB_SYSTEM.md)） |
| [superpowers/specs/2026-07-14-release-blocker-fixes-design.md](superpowers/specs/2026-07-14-release-blocker-fixes-design.md) | 发布阻塞问题修复设计 |
| [superpowers/plans/2026-07-14-release-blocker-fixes.md](superpowers/plans/2026-07-14-release-blocker-fixes.md) | 发布阻塞问题修复实施计划 |
| [superpowers/specs/2026-07-16-nine-optimization-completion-design.md](superpowers/specs/2026-07-16-nine-optimization-completion-design.md) | 九项优化收尾设计 |
| [superpowers/plans/2026-07-16-ai-cover-style-controls.md](superpowers/plans/2026-07-16-ai-cover-style-controls.md) | AI 封面与风格控制实施计划 |
| [superpowers/plans/2026-07-16-ai-page-layout-refactor.md](superpowers/plans/2026-07-16-ai-page-layout-refactor.md) | AI 页面布局重构实施计划 |
| [superpowers/plans/2026-07-16-documentation-cleanup-verification.md](superpowers/plans/2026-07-16-documentation-cleanup-verification.md) | 文档清理与核对实施计划 |
| [superpowers/plans/2026-07-16-preference-task-log-closure.md](superpowers/plans/2026-07-16-preference-task-log-closure.md) | 偏好任务日志收口实施计划 |
| [superpowers/specs/2026-07-17-ai-project-overview-single-panel-design.md](superpowers/specs/2026-07-17-ai-project-overview-single-panel-design.md) | AI 项目总览单面板设计 |
| [superpowers/plans/2026-07-17-ai-project-overview-single-panel.md](superpowers/plans/2026-07-17-ai-project-overview-single-panel.md) | AI 项目总览单面板实施计划 |
| [superpowers/plans/2026-07-20-cloudflare-https.md](superpowers/plans/2026-07-20-cloudflare-https.md) | Cloudflare HTTPS 部署实施计划 |
| [superpowers/specs/2026-07-21-rescue-library-userscript-design.md](superpowers/specs/2026-07-21-rescue-library-userscript-design.md) | 救援库与 userscript 设计 |
| [superpowers/plans/2026-07-21-rescue-library-userscript.md](superpowers/plans/2026-07-21-rescue-library-userscript.md) | 救援库与 userscript 实施计划 |
| [superpowers/specs/2026-07-21-rescue-catalog-sources-design.md](superpowers/specs/2026-07-21-rescue-catalog-sources-design.md) | 救援目录来源筛选设计 |
| [superpowers/plans/2026-07-22-rescue-catalog-sources.md](superpowers/plans/2026-07-22-rescue-catalog-sources.md) | 救援目录来源筛选实施计划 |
| [superpowers/specs/2026-07-23-adult-polish-agent-design.md](superpowers/specs/2026-07-23-adult-polish-agent-design.md) | 成人描写局部润色 Agent 设计 |
| [superpowers/plans/2026-07-23-adult-polish-agent.md](superpowers/plans/2026-07-23-adult-polish-agent.md) | 成人描写局部润色 Agent 实施计划 |
| [superpowers/specs/2026-07-23-ai-model-catalog-pools-design.md](superpowers/specs/2026-07-23-ai-model-catalog-pools-design.md) | AI 模型目录、模型池和故障转移设计 |
| [superpowers/plans/2026-07-23-ai-model-catalog-pools.md](superpowers/plans/2026-07-23-ai-model-catalog-pools.md) | AI 模型目录与模型池实施计划 |
| [superpowers/specs/2026-07-27-ai-model-catalog-pools-unified-requirements.md](superpowers/specs/2026-07-27-ai-model-catalog-pools-unified-requirements.md) | AI 模型目录与统一路由需求基线 |
| [superpowers/specs/2026-07-28-ai-model-routing-completion-design.md](superpowers/specs/2026-07-28-ai-model-routing-completion-design.md) | AI 模型统一路由收尾设计 |
| [superpowers/specs/2026-08-04-github-readme-and-logo-refresh-design.md](superpowers/specs/2026-08-04-github-readme-and-logo-refresh-design.md) | GitHub README 首屏与静态 Logo 刷新设计 |
| [superpowers/plans/2026-08-04-github-readme-and-logo-refresh.md](superpowers/plans/2026-08-04-github-readme-and-logo-refresh.md) | GitHub README 与静态 Logo 刷新实施计划 |
| [superpowers/specs/2026-08-05-project-audit-remediation-design.md](superpowers/specs/2026-08-05-project-audit-remediation-design.md) | 2026-08-05 项目审计整改设计 |
| [superpowers/plans/2026-08-05-project-audit-remediation.md](superpowers/plans/2026-08-05-project-audit-remediation.md) | 2026-08-05 项目审计整改实施计划 |

## 历史参考与归档

以下顶层文档是特定时间点的历史快照，不是当前事实来源。当前行为以代码、[README.md](../README.md) 和 [frontend-api-contract.md](frontend-api-contract.md) 为准。

| 文档 | 历史用途 |
|------|----------|
| [API_COMPLETE.md](API_COMPLETE.md) | 2026-06-16 的完整 API 快照 |
| [AI_WRITING_STUDIO_PLAN.md](AI_WRITING_STUDIO_PLAN.md) | AI 创作工作台的阶段性设计与实施记录 |
| [../KNOWLEDGE_GRAPH.md](../KNOWLEDGE_GRAPH.md) | 旧项目结构、模块和数据流快照 |

### `docs/archive/` 归档

`docs/archive/` 存放已完成的阶段性文档（旧审计报告、一次性完成报告、优化路线图、拆分计划等）。这些文档描述的工作已经做完，不再维护，仅作归档参考。详见 [archive/README.md](archive/README.md)。

## 当前状态说明

当前行为以**代码与测试**为第一来源，其次是 [README.md](../README.md)、[frontend-api-contract.md](frontend-api-contract.md)、[frontend-pages.md](frontend-pages.md) 和 [JOB_SYSTEM.md](JOB_SYSTEM.md)。

成人 Agent 的**已实现**约束（fail-closed、Provider scope、角色确认、两阶段 JSON review）见 [ADULT_POLISH_USER_GUIDE.md](ADULT_POLISH_USER_GUIDE.md) 与 `docs/superpowers/specs/2026-07-23-adult-polish-agent-design.md`（该轮已落地）。`2026-08-14-complete-audit-remediation-design.md` 描述的是**尚未实施**的下一轮目标，不能当作当前行为依据。仓库中不存在的 `.superpowers/sdd/task-11-brief.md` 不再作为活动清单引用。

测试基线：`python -m pytest -q` → 1258 passed, 4 skipped（2026-08-24 实测）。

归档包含 14 份顶层文档 + 6 份 superpowers 已完成计划，涵盖：
- 2026-06-16 全量审计系列（AUDIT_REPORT / EXECUTIVE_SUMMARY / COMPLETION_REPORT / CRITICAL_BUGS_FIX_PLAN / BUGS_FIXED_REPORT / ACTION_CHECKLIST）
- 优化路线图系列（OPTIMIZATION_ROADMAP / OPTIMIZATION_REVIEW_2026-06-26 / OPTIMIZATION_PLAN_2026-06-30）
- 模块化系列（MODULARIZATION_PLAN / MODULARIZATION_COMPLETE / MANAGER_EXTRACTION_COMPLETE / IMPLEMENTATION_RECORD / ALL_TASKS_COMPLETED）
- superpowers 已完成计划（qwen-embedding-robustness / cli-job-services / unified-job-queue / web-jobspec-runner 及对应 specs）

---

如需查找历史信息，先看 [archive/README.md](archive/README.md) 的归档清单。如需当前状态，看 [README.md](../README.md) 与最新审计报告。

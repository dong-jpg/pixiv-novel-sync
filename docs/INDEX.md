# 项目文档索引

**项目**: Pixiv Novel Sync
**维护者**: dong-jpg
**最近更新**: 2026-09-24

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
| [AUDIT_REPORT_2026-08-13.md](AUDIT_REPORT_2026-08-13.md) | 审计：任务终态、推荐发布、成人路由取消、分页边界与需求覆盖 |
| [AUDIT_REPORT_2026-09-14.md](AUDIT_REPORT_2026-09-14.md) | 最新一轮全项目审计：登录落盘、调度器锁死、每请求 init_schema、AI 中文预算、成人别名恢复、需求覆盖矩阵与 35 条无调用路由 |

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
| [MODEL_ROUTING_GUIDE.md](MODEL_ROUTING_GUIDE.md) | AI 模型目录/模型池/统一路由用户指南（发现、绑定、failover、排错） |
| [RESCUE_USER_GUIDE.md](RESCUE_USER_GUIDE.md) | 救援功能用户指南（userscript、Token、目录筛选、只读 API、排错） |
| [JOB_SYSTEM.md](JOB_SYSTEM.md) | 任务系统开发者文档（管线、状态机、取消协议、新增 task_type、auto_sync 配置） |

## 开发计划（superpowers/）

归档口径说明：`superpowers/` 下的 plans/specs 一律**留在原目录**、在本索引标注状态（进行中 / 已完成），不再移动到 `archive/`；只有顶层一次性文档才进入 `archive/`。已完成条目仅作实施记录，当前行为仍以代码与活跃参考文档为准。

### 进行中

> **2026-09-24：** 2026-08-14 三份计划没有按原文整份落地。能用更小改动盖住的，已经进了 2026-09-14 整改（救援增量刷新、推荐搜索计划、目录差量）。明确不做的是 task log owner lease、分页游标、trash manifest、AI 总结 / 解释来源、成人偏好注入、审查阶段实时 progress，以及 `recommendation_search_plans` 表和 `JobType.RECOMMENDATION_SYNC`。2026-08-28 吞吐计划已拆成 phase 1–3。这些文件描述的是当时的目标，不是当前行为。

| 文档 | 说明 |
|------|------|
| [superpowers/specs/2026-08-14-complete-audit-remediation-design.md](superpowers/specs/2026-08-14-complete-audit-remediation-design.md) | 2026-08-13 审计的完整整改设计（本轮主线设计） |
| [superpowers/plans/2026-08-14-runtime-integrity-remediation.md](superpowers/plans/2026-08-14-runtime-integrity-remediation.md) | 运行时完整性整改实施计划（未开始） |
| [superpowers/plans/2026-09-14-ai-writing-split-and-audit-remediation.md](superpowers/plans/2026-09-14-ai-writing-split-and-audit-remediation.md) | **当前主线**：AI 写作模块剥离到 `ai-writing` 分支 + 2026-09-14 审计全部发现的整改任务（T0–T5 编号，含验收清单） |
| [superpowers/plans/2026-08-14-rescue-completion.md](superpowers/plans/2026-08-14-rescue-completion.md) | 救援目录收尾实施计划（未开始） |
| [superpowers/plans/2026-08-14-recommendation-completion.md](superpowers/plans/2026-08-14-recommendation-completion.md) | 推荐系统收尾实施计划（未开始） |

### 已完成

| 文档 | 说明 |
|------|------|
| [superpowers/plans/2026-06-26-job-cancellation-hardening.md](superpowers/plans/2026-06-26-job-cancellation-hardening.md) | 任务取消硬化计划（已实施，取消协议详见 [JOB_SYSTEM.md](JOB_SYSTEM.md)） |
| [superpowers/specs/2026-07-14-release-blocker-fixes-design.md](superpowers/specs/2026-07-14-release-blocker-fixes-design.md) | 发布阻塞问题修复设计 |
| [superpowers/plans/2026-07-14-release-blocker-fixes.md](superpowers/plans/2026-07-14-release-blocker-fixes.md) | 发布阻塞问题修复实施计划 |
| [superpowers/specs/2026-07-16-nine-optimization-completion-design.md](superpowers/specs/2026-07-16-nine-optimization-completion-design.md) | 九项优化收尾设计 |
| [superpowers/plans/2026-07-16-documentation-cleanup-verification.md](superpowers/plans/2026-07-16-documentation-cleanup-verification.md) | 文档清理与核对实施计划 |
| [superpowers/plans/2026-07-16-preference-task-log-closure.md](superpowers/plans/2026-07-16-preference-task-log-closure.md) | 偏好任务日志收口实施计划 |
| [superpowers/plans/2026-07-20-cloudflare-https.md](superpowers/plans/2026-07-20-cloudflare-https.md) | Cloudflare HTTPS 部署实施计划 |
| [superpowers/specs/2026-07-21-rescue-library-userscript-design.md](superpowers/specs/2026-07-21-rescue-library-userscript-design.md) | 救援库与 userscript 设计 |
| [superpowers/plans/2026-07-21-rescue-library-userscript.md](superpowers/plans/2026-07-21-rescue-library-userscript.md) | 救援库与 userscript 实施计划 |
| [superpowers/specs/2026-07-21-rescue-catalog-sources-design.md](superpowers/specs/2026-07-21-rescue-catalog-sources-design.md) | 救援目录来源筛选设计 |
| [superpowers/plans/2026-07-22-rescue-catalog-sources.md](superpowers/plans/2026-07-22-rescue-catalog-sources.md) | 救援目录来源筛选实施计划 |
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
| [../KNOWLEDGE_GRAPH.md](../KNOWLEDGE_GRAPH.md) | 旧项目结构、模块和数据流快照 |

> AI 写作/成人润色相关的历史文档（`AI_WRITING_STUDIO_PLAN.md`、`ADULT_POLISH_USER_GUIDE.md`、`QWEN_EMBEDDING_INTEGRATION.md` 及对应 superpowers 计划/规格）已随 AI 写作模块移到 `ai-writing` 分支，main 分支不再收录。

### `docs/archive/` 归档

`docs/archive/` 存放已完成的阶段性文档（旧审计报告、一次性完成报告、优化路线图、拆分计划等）。这些文档描述的工作已经做完，不再维护，仅作归档参考。详见 [archive/README.md](archive/README.md)。

## 当前状态说明

当前行为以**代码与测试**为第一来源，其次是 [README.md](../README.md)、[frontend-api-contract.md](frontend-api-contract.md)、[frontend-pages.md](frontend-pages.md) 和 [JOB_SYSTEM.md](JOB_SYSTEM.md)。

AI 创作与成人润色功能在 `ai-writing` 分支维护，其文档（含成人 Agent 的 fail-closed、Provider scope、角色确认、两阶段 JSON review 约束）随模块一并迁出，main 分支不再收录。`2026-08-14-complete-audit-remediation-design.md` 描述的是**尚未实施**的下一轮目标，不能当作当前行为依据。仓库中不存在的 `.superpowers/sdd/task-11-brief.md` 不再作为活动清单引用。

测试基线：`python -m pytest -q` → 1150 passed, 4 skipped（2026-09-15 剥离 AI 写作模块后实测）。

归档包含 14 份顶层文档 + 6 份 superpowers 已完成计划，涵盖：
- 2026-06-16 全量审计系列（AUDIT_REPORT / EXECUTIVE_SUMMARY / COMPLETION_REPORT / CRITICAL_BUGS_FIX_PLAN / BUGS_FIXED_REPORT / ACTION_CHECKLIST）
- 优化路线图系列（OPTIMIZATION_ROADMAP / OPTIMIZATION_REVIEW_2026-06-26 / OPTIMIZATION_PLAN_2026-06-30）
- 模块化系列（MODULARIZATION_PLAN / MODULARIZATION_COMPLETE / MANAGER_EXTRACTION_COMPLETE / IMPLEMENTATION_RECORD / ALL_TASKS_COMPLETED）
- superpowers 已完成计划（qwen-embedding-robustness / cli-job-services / unified-job-queue / web-jobspec-runner 及对应 specs）

---

如需查找历史信息，先看 [archive/README.md](archive/README.md) 的归档清单。如需当前状态，看 [README.md](../README.md) 与最新审计报告。

# 项目文档索引

**项目**: Pixiv Novel Sync
**维护者**: dong-jpg
**最近更新**: 2026-07-03

---

当前文档分为三部分：**活跃参考**（顶层，持续维护）、**开发计划**（superpowers/，进行中的设计）、**历史归档**（archive/，已完成不再维护）。

## 活跃参考文档（顶层）

### 入口与审计

| 文档 | 用途 |
|------|------|
| [../README.md](../README.md) | 项目入口：功能介绍、快速开始、配置说明 |
| [AUDIT_REPORT_2026-07-02.md](AUDIT_REPORT_2026-07-02.md) | 上一轮审计：修复 8 类严重 bug + 5 类中等问题 |
| [AUDIT_REPORT_2026-07-03.md](AUDIT_REPORT_2026-07-03.md) | 本轮审计：EPUB 回归修复 + 死代码清理 + 文档整改 |

### API 与前端契约

| 文档 | 用途 |
|------|------|
| [API_COMPLETE.md](API_COMPLETE.md) | 完整后端 API 文档（71 端点） |
| [frontend-api-contract.md](frontend-api-contract.md) | 前端依赖的后端端点契约 |
| [frontend-pages.md](frontend-pages.md) | 前端页面/模板/路由清单 |
| [library-os-style-guide.md](library-os-style-guide.md) | 前端视觉设计系统指南 |

### 功能设计

| 文档 | 用途 |
|------|------|
| [AI_WRITING_STUDIO_PLAN.md](AI_WRITING_STUDIO_PLAN.md) | AI 创作工台设计（注：§4 单文件布局描述与现有 `ai/services/` 包不符，以代码为准） |
| [PREFERENCE_RECOMMENDER_REQUIREMENTS.md](PREFERENCE_RECOMMENDER_REQUIREMENTS.md) | 偏好推荐系统需求规格 |
| [QWEN_EMBEDDING_INTEGRATION.md](QWEN_EMBEDDING_INTEGRATION.md) | Qwen embedding 检索配置指南 |

## 开发计划

| 文档 | 状态 |
|------|------|
| [superpowers/plans/2026-06-26-job-cancellation-hardening.md](superpowers/plans/2026-06-26-job-cancellation-hardening.md) | 任务取消硬化计划（HEAD commit 依赖，当前权威） |

## 历史归档

`docs/archive/` 存放已完成的阶段性文档（旧审计报告、一次性完成报告、优化路线图、拆分计划等）。这些文档描述的工作已经做完，不再维护，仅作归档参考。详见 [archive/README.md](archive/README.md)。

归档包含 14 份顶层文档 + 6 份 superpowers 已完成计划，涵盖：
- 2026-06-16 全量审计系列（AUDIT_REPORT / EXECUTIVE_SUMMARY / COMPLETION_REPORT / CRITICAL_BUGS_FIX_PLAN / BUGS_FIXED_REPORT / ACTION_CHECKLIST）
- 优化路线图系列（OPTIMIZATION_ROADMAP / OPTIMIZATION_REVIEW_2026-06-26 / OPTIMIZATION_PLAN_2026-06-30）
- 模块化系列（MODULARIZATION_PLAN / MODULARIZATION_COMPLETE / MANAGER_EXTRACTION_COMPLETE / IMPLEMENTATION_RECORD / ALL_TASKS_COMPLETED）
- superpowers 已完成计划（qwen-embedding-robustness / cli-job-services / unified-job-queue / web-jobspec-runner 及对应 specs）

---

如需查找历史信息，先看 [archive/README.md](archive/README.md) 的归档清单。如需当前状态，看 [README.md](../README.md) 与最新审计报告。

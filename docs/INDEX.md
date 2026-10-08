# 项目文档索引

**最近更新：2026-10-09**

## 当前状态：从这里开始

- [移动优先改造报告](MOBILE_IMPLEMENTATION_REPORT_2026-10-08.md)：安卓 Chromium、登录保持、推荐/阅读/管理页面，本地验证与未部署边界。
- [移动优先执行计划](superpowers/plans/2026-10-08-mobile-first-web.md)：当前移动改造任务与验收记录，独立于此前服务器整改。
- [本轮整改执行状态](REMEDIATION_STATUS_2026-09-30.md)：当前实现、最新验证、分支与环境验收边界。它替代历史报告中的“当前状态”说明。
- [本轮执行台账](superpowers/plans/2026-09-30-prioritized-completion.md)：按优先级推进及验证记录。
- [09-14原任务清单](superpowers/plans/2026-09-14-ai-writing-split-and-audit-remediation.md)：逐项验收要求；不能把checkbox统计当项目完成率。

main 提供归档/同步/推荐/救援，以及 Provider、模型目录、模型池、统一路由和偏好关键词清洗；写作和成人功能仅在 ai-writing。工作区已修改不等于远端已推送或现网已部署。

写作/成人/检索指南（AI_WRITING_STUDIO_PLAN、ADULT_POLISH_USER_GUIDE、QWEN_EMBEDDING_INTEGRATION 及对应计划/规格）已迁出 main；缺少的历史材料见历史计划汇编中的跨分支说明，不恢复文件或建立坏链接。

## 活跃参考

| 文档 | 路径 |
|---|---|
| [项目入口](../README.md) | README.md |
| [开发约定](../CLAUDE.md) | CLAUDE.md |
| [需求与OUT决策](UNIFIED_PROJECT_REQUIREMENTS.md) | docs/UNIFIED_PROJECT_REQUIREMENTS.md |
| [任务/调度/恢复](JOB_SYSTEM.md) | docs/JOB_SYSTEM.md |
| [页面与交互](frontend-pages.md) | docs/frontend-pages.md |
| [API契约](frontend-api-contract.md) | docs/frontend-api-contract.md |
| [模型与预算](MODEL_ROUTING_GUIDE.md) | docs/MODEL_ROUTING_GUIDE.md |
| [偏好与推荐](PREFERENCE_RECOMMENDER_REQUIREMENTS.md) | docs/PREFERENCE_RECOMMENDER_REQUIREMENTS.md |
| [救援使用](RESCUE_USER_GUIDE.md) | docs/RESCUE_USER_GUIDE.md |
| [界面规范](library-os-style-guide.md) | docs/library-os-style-guide.md |

## 仍有效的专项需求基线

- [AI模型目录、模型池与统一路由需求](superpowers/specs/2026-07-27-ai-model-catalog-pools-unified-requirements.md)：保留为已实施约束，不因历史计划整合而丢失入口。

## 历史材料：报告与计划分开整合

- [历史报告整合](archive/HISTORY_REPORTS.md)：旧审计、阶段完成报告、结构快照、重复优化建议。
- [历史执行计划整合](archive/HISTORY_PLANS.md)：旧计划的分期、替代与OUT关系，以及原文索引。
- [09-30审查快照](AUDIT_REPORT_2026-09-30.md)：修复前的缺陷证据；不再以其中旧测试结果描述新工作区。
- [全分支计划时间线](PLAN_AUDIT_2026-09-30.md)：32份历史计划的首次Git提交与分支快照。
- [原始归档目录](archive/README.md)：保留原文便于追溯。归档不代表其中全部建议已实现。

superpowers 历史 plans/specs 继续留在原路径，不批量改勾、不删除原文消除待办。现有明确 OUT 的 task_logs owner lease、seen-cursor、完整 trash manifest/启动重放等不重新实施；这不排除已有的 AI job/model-sync 租约。修复当前数据恢复流程的缺陷不等于新增完整 manifest 系统。

## 维护规则

当前行为以代码、回归测试和活跃契约为准；状态变化统一更新整改状态与对应任务，不在多份旧审计中反复回写。真实Provider/Pixiv、生产迁移/灰度、浏览器与移动端验收必须注明环境，不得由离线全绿推断完成。

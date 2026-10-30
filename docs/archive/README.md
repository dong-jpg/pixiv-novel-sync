# 历史文档归档

这里存放已经过去的记录：各期审计、部署、整改与计划核对报告，阶段完成报告，以及旧版结构快照。当前状态只看 [项目现状](../STATUS.md)，文档入口见 [文档索引](../INDEX.md)。

按主题汇总的旧结论去向：

- [历史报告整合](HISTORY_REPORTS.md)
- [历史执行计划整合](HISTORY_PLANS.md)

原文保留追溯，归档不代表每条建议都完成；旧行号、测试数与「当前」「本轮」字样只描述当时。main 未收录的写作历史材料在历史计划汇编中以 ai-writing 跨分支文字说明，不恢复已移除文件。

## 带日期的记录（2026-07 起）

| 文档 | 主题 |
|------|------|
| AUDIT_REPORT_2026-07-02.md / 07-03.md | 07 月安全与发布审查 |
| AUDIT_REPORT_2026-08-13.md | 08 月完整性审查 |
| AUDIT_REPORT_2026-09-14.md | 写作模块拆分前的全量审计 |
| AUDIT_REPORT_2026-09-30.md / PLAN_AUDIT_2026-09-30.md | 09-30 修复前审查与全分支计划时间线 |
| REMEDIATION_STATUS_2026-09-30.md | 10-08 ～ 10-20 三轮整改台账（10-28 前曾是当前状态入口） |
| AUDIT_REPORT_2026-10-14.md / PLAN_AUDIT_2026-10-14.md | 10-14 全分支审查与计划核对 |
| AUDIT_REPORT_2026-10-20.md | 10-20 审查与修复批次 |
| SERVER_DEPLOYMENT_REPORT_2026-10-08.md / 10-20.md | 首次部署与源站证书修复；10-20 常规更新 |
| MOBILE_AUDIT_2026-10-08.md / MOBILE_IMPLEMENTATION_REPORT_2026-10-08.md | 安卓移动优先改造的巡检与实施 |
| RESCUE_DEBUG_HANDOFF_2026-10-22.md | 救援脚本手机端排查交接；根因已在 0.9.0 解决，方法沉淀进救援指南 |
| AUDIT_REPORT_2026-10-28.md | 10-28 全项目审查（七个切片），未修复项的待办在 [项目现状](../STATUS.md) |
| [SERVER_DEPLOYMENT_REPORT_2026-10-30.md](SERVER_DEPLOYMENT_REPORT_2026-10-30.md) | 审计后续批次部署、Linux 验证、私有副本迁移 / 恢复及生产安全配置验收 |

## 旧版快照

| 文档 | 主题 | 归档原因 |
|------|------|----------|
| KNOWLEDGE_GRAPH.md | 早期知识图谱（原在仓库根目录） | 结构、行数与接口均已变化 |
| API_COMPLETE.md | 早期完整 API 文档 | 由 [前端 API 契约](../frontend-api-contract.md) 取代 |
| monkeycode/novel-tabs-and-user-detail/ | 04 月小说标签与用户详情页需求/设计（原在 `.monkeycode/specs/`） | 界面与接口已演进 |
| superpowers/ | 06 月统一任务队列、CLI/Web Job、Qwen embedding 计划与设计 | 已落地或已迁出 main |

## 06 月早期报告

| 文档 | 主题 | 归档原因 |
|------|------|----------|
| AUDIT_REPORT.md | 2026-06-16 全量审计 | 已被 2026-07-02/03 审计取代 |
| EXECUTIVE_SUMMARY.md | 上述审计的摘要 | 同上 |
| COMPLETION_REPORT.md | 2026-06-16 审计完成报告 | 同上 |
| CRITICAL_BUGS_FIX_PLAN.md | 6 个并发 bug 修复计划 | bug 已修复，行号已漂移 |
| BUGS_FIXED_REPORT.md | 上述修复的完成报告 | 同上 |
| ACTION_CHECKLIST.md | 4 周行动清单 | 已执行完毕 |
| OPTIMIZATION_ROADMAP.md | 优化路线图 v1 | 已被 06-26/06-30 复查取代 |
| OPTIMIZATION_REVIEW_2026-06-26.md | 2026-06-26 代码复查 | 已被 07-02/03 审计取代 |
| OPTIMIZATION_PLAN_2026-06-30.md | 2026-06-30 复查计划 | 同上 |
| ALL_TASKS_COMPLETED.md | "全部完成"总结 | 阶段性营销文档 |
| MODULARIZATION_PLAN.md | 拆分大文件计划 | 已落地，结构已变 |
| MODULARIZATION_COMPLETE.md | 上述计划完成报告 | 同上 |
| MANAGER_EXTRACTION_COMPLETE.md | manager 类抽取报告 | 同上 |
| IMPLEMENTATION_RECORD.md | 合并 7 份旧文档的实现记录 | 引用的旧文档已删 |

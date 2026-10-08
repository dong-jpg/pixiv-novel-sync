# 历史报告整合

本页整合历史审计、完成报告、架构快照和优化建议。它们用于追溯，不再作为当前功能/接口承诺。

## 统一结论

1. 早期并发、外键、任务取消、CLI/Web共用任务、存储拆分已成为现有架构的一部分，后续修改应维持这些约束，不再照旧报告重复实施。
2. “已归档”“全部完成”等标题仅代表当时阶段，不代表当前无缺陷；后续审计多次发现跨层状态、文件恢复和AI任务边界问题。
3. API_COMPLETE、KNOWLEDGE_GRAPH 与旧写作设计不描述当前接口。查询行为应使用活跃契约与代码。
4. 2026-09-30审查发现与后续修复分开记录：审查原文保留触发条件，修复状态集中于 [本轮整改状态](../REMEDIATION_STATUS_2026-09-30.md)。旧故障复现脚本可能在修复后按预期断言失败。

## 历史主题

| 时期/主题 | 保留的有用结论 | 当前去向 |
|---|---|---|
| 06月并发/数据库审计 | 连接隔离、外键、信号量finally、事务边界 | 现有storage/jobs与回归测试 |
| 06月模块化与优化 | 共享JobSpec、取消终态、拆分职责 | JOB_SYSTEM及当前架构；不按旧文件行号实施 |
| 07月安全/发布审查 | 私有路径、CSRF、配置、密钥与接口契约 | 活跃指南与自动化测试 |
| 08月完整性/推荐/救援审查 | 原文需求部分实现、部分替代、部分OUT | UNIFIED §1.3及现有计划 |
| 09月拆分审计 | main仅AI基础设施，写作/成人留ai-writing | 禁止写作整支反向合并main |
| 09-30复核 | 文件回滚、预算单位、并发保存、状态传播 | 最新整改状态与专项回归 |

## 原始文档索引

原文保留，不移动、不覆盖历史证据；所有当前结论以最新状态文档为准。

- [API_COMPLETE.md](../API_COMPLETE.md)
- [AUDIT_REPORT_2026-07-02.md](../AUDIT_REPORT_2026-07-02.md)
- [AUDIT_REPORT_2026-07-03.md](../AUDIT_REPORT_2026-07-03.md)
- [AUDIT_REPORT_2026-08-13.md](../AUDIT_REPORT_2026-08-13.md)
- [AUDIT_REPORT_2026-09-14.md](../AUDIT_REPORT_2026-09-14.md)
- [ALL_TASKS_COMPLETED.md](ALL_TASKS_COMPLETED.md)
- [AUDIT_REPORT.md](AUDIT_REPORT.md)
- [BUGS_FIXED_REPORT.md](BUGS_FIXED_REPORT.md)
- [COMPLETION_REPORT.md](COMPLETION_REPORT.md)
- [EXECUTIVE_SUMMARY.md](EXECUTIVE_SUMMARY.md)
- [IMPLEMENTATION_RECORD.md](IMPLEMENTATION_RECORD.md)
- [MANAGER_EXTRACTION_COMPLETE.md](MANAGER_EXTRACTION_COMPLETE.md)
- [MODULARIZATION_COMPLETE.md](MODULARIZATION_COMPLETE.md)
- [OPTIMIZATION_REVIEW_2026-06-26.md](OPTIMIZATION_REVIEW_2026-06-26.md)
- [OPTIMIZATION_ROADMAP.md](OPTIMIZATION_ROADMAP.md)

- [09-30修复前审查](../AUDIT_REPORT_2026-09-30.md)
- [旧知识图谱](../../KNOWLEDGE_GRAPH.md)

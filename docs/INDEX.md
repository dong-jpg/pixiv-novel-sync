# 项目文档索引

**最近更新：2026-11-17**

当前版本、测试基线、部署与待办统一看 [项目现状](STATUS.md)。

main 提供归档、同步、阅读、推荐、救援、作品分析、偏好学习，以及 VPS / InstaBox 远程执行试点；ai-writing 继承 main 并增加写作和成人润色。代码交付与实际部署分别记录，先看 [项目现状](STATUS.md)。

## 使用

| 文档 | 内容 |
|---|---|
| [README](../README.md) | 功能、安装、部署与安全 |
| [救援使用指南](RESCUE_USER_GUIDE.md) | 救援 Token、用户脚本安装与排查 |
| [模型路由指南](MODEL_ROUTING_GUIDE.md) | Provider、模型目录、模型池、调用预算 |
| [偏好学习指南](PREFERENCE_LEARNING_GUIDE.md) | 作品报告、读后评价、AI 偏好 / XP 画像、全库批次与持续自动分析 |
| [远程执行](REMOTE_EXECUTION.md) | VPS 与 InstaBox 试点、凭据隔离、运行和恢复 |
| [阅读进度同步](READING_PROGRESS_SYNC.md) | 服务端版本、自动保存、离线记录、跨设备冲突与重置 |
| [本轮全项目审计](archive/AUDIT_REPORT_2026-11-17.md) | 已修复问题、性能证据、原设想缺口与两分支整理 |

## 开发参考

| 文档 | 内容 |
|---|---|
| [开发约定](../CLAUDE.md) | 架构、协议、易错点与代码约定 |
| [统一需求与 OUT 决策](UNIFIED_PROJECT_REQUIREMENTS.md) | 需求基线；§1.3 列出明确不做的项 |
| [任务系统](JOB_SYSTEM.md) | 任务流水线、调度、抢占与恢复；新增任务类型的清单 |
| [前端 API 契约](frontend-api-contract.md) | 接口、参数与错误体 |
| [页面清单](frontend-pages.md) | 路由与页面交互 |
| [界面规范](library-os-style-guide.md) | Library OS 视觉规范 |
| [偏好与推荐需求](PREFERENCE_RECOMMENDER_REQUIREMENTS.md) | 偏好分析与推荐模块 |
| [AI 模型目录与模型池统一需求](superpowers/specs/2026-07-27-ai-model-catalog-pools-unified-requirements.md) | 已实施的约束，修改路由前先读 |

## 历史记录

- [archive/](archive/README.md)：带日期的审计、部署、整改与计划核对记录，移动端改造记录，旧版知识图谱与 API 文档。
- [历史报告整合](archive/HISTORY_REPORTS.md) 与 [历史执行计划整合](archive/HISTORY_PLANS.md)：按时期汇总旧结论的去向。
- [superpowers/plans](superpowers/plans/) 与 [superpowers/specs](superpowers/specs/)：包含已执行、进行中和后续独立阶段的计划与设计；实际状态以项目现状为准，不能用勾选比例推算完成度。11-12 的偏好学习扩展覆盖早期个人画像 OUT 决策；Pixiv 抓取与签到状态适配器的云端接线仍未完成。

## 维护规则

- 当前行为以代码和回归测试为准；状态变化只更新 [项目现状](STATUS.md)。每轮审查或部署另存带日期的记录到 `archive/`，并在现状页的「记录」表里换成最新一份。
- 真实 Pixiv / Provider、生产迁移、浏览器与移动端验收必须注明环境，不能由离线测试全绿推断完成。
- 部分测试直接断言文档内容（见 CLAUDE.md 的 Conventions），改 README、本索引或活跃文档时一并跑 `python -m pytest tests/test_main_readme_branding.py tests/test_ai_model_docs.py tests/test_rescue_userscript_install.py -q`。

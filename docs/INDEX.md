# 项目文档索引

**最近更新：2026-11-23**

当前能力和验证边界看 [项目现状](STATUS.md)，开发任务、依赖与验收标准看 [修复与优化实施计划](IMPLEMENTATION_PLAN.md)。

main 提供本地归档、同步、阅读、推荐、救援、作品分析、偏好学习及 NAS / Docker 部署；ai-writing 继承共享功能并增加写作和成人润色。代码交付与实际部署分别记录，先看 [项目现状](STATUS.md)。

## 使用

| 文档 | 内容 |
|---|---|
| [README](../README.md) | 功能、安装、部署与安全 |
| [救援使用指南](RESCUE_USER_GUIDE.md) | 救援 Token、用户脚本安装与排查 |
| [模型路由指南](MODEL_ROUTING_GUIDE.md) | Provider、模型目录、模型池、调用预算 |
| [偏好学习指南](PREFERENCE_LEARNING_GUIDE.md) | 作品报告、读后评价、AI 偏好 / XP 画像、全库批次与持续自动分析 |
| [NAS / Docker 部署](DOCKER.md) | 单容器运行、目录权限、网络、备份与旧服务器迁移 |
| [阅读进度同步](READING_PROGRESS_SYNC.md) | 服务端版本、自动保存、离线记录、跨设备冲突与重置 |
| [实施计划](IMPLEMENTATION_PLAN.md) | 当前修复、性能优化、本地执行与 NAS 部署验收 |

## 开发参考

| 文档 | 内容 |
|---|---|
| [开发约定](../CLAUDE.md) | 架构、协议、易错点与代码约定 |
| [统一需求与 OUT 决策](UNIFIED_PROJECT_REQUIREMENTS.md) | 需求基线；§1.3 列出明确不做的项 |
| [任务系统](JOB_SYSTEM.md) | 任务流水线、调度、抢占与恢复；新增任务类型的清单 |
| [前端 API 契约](frontend-api-contract.md) | 接口、参数与错误体 |
| [页面清单](frontend-pages.md) | 路由与页面交互 |
| [界面规范](library-os-style-guide.md) | Library OS 视觉规范 |
| [AI 调用设计](AI_ROUTING_DESIGN.md) | 目录同步、池图、完整文本预检、路由、预算与兼容 |
| [作品分析与偏好学习设计](WORK_ANALYSIS_DESIGN.md) | 冻结来源、全文覆盖、证据、预算、评价与画像失效 |
| [Pixiv 抓取边界](PIXIV_FETCH_BOUNDARY.md) | 单本结果、归档校验及本地消费者 |

## 文档职责

- README 介绍项目和安装；指南描述当前操作；设计说明解释必须保留的约束。
- 统一需求定义产品范围；实施计划列出开发任务；STATUS 记录当前完成度、测试环境及生产边界。
- 已删除的审计、部署、验收、完成报告和已执行计划可通过 Git 历史查看，不再维护重复归档树。
- 可复用验证工具放在 `scripts/`，日志、截图和构建产物输出到忽略目录或工作区外，不提交为新的报告文档。

## 维护规则

- 当前行为以代码和回归测试为准；每次功能变化同步指南、契约、计划与状态，不再新增独立报告。
- 真实 Pixiv / Provider、生产迁移、浏览器与移动端验收必须注明环境，不能由离线测试全绿推断完成。
- 部分测试直接断言文档内容（见 CLAUDE.md 的 Conventions），改 README、本索引或活跃文档时一并跑 `python -m pytest tests/test_main_readme_branding.py tests/test_ai_model_docs.py tests/test_rescue_userscript_install.py -q`。

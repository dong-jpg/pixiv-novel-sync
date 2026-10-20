# 全分支执行计划时间线（2026-10-14）

## 范围和方法

- 检查时本地及远端仅有 main `57def50` 与 ai-writing `3143893`，远端由 `git ls-remote --heads origin` 确认。完整版本和验证见 [审查报告](AUDIT_REPORT_2026-10-14.md)。
- 共 **38份计划/行动清单**：29份共享、3份仅main、6份仅ai-writing。main包含32份，ai-writing包含35份；跨分支同路径只计一次。另核对早期设计、偏好需求和统一规格，不把所有spec或历史报告重复计为独立执行计划。
- 在各分支对每个现存计划使用 `git log <ref> --follow --diff-filter=A --format='%H %cI'`，追溯改名/归档前的最早加入提交；合并两分支结果后按**提交者时间**排序，同一提交按路径排序。它证明可追溯的首次入库时间，不证明最初草拟时间，也不使用文件系统创建时间。
- 09-30旧表的32份，加上遗漏的ACTION_CHECKLIST/OPTIMIZATION_ROADMAP及后来入库的整改台账、移动、品牌、安装4份，共38份。文件名09-30的台账实际首次入库为10-08。
- M=main，A=ai-writing；“勾/空”取本轮修改文档前的上述提交，**不是完成率**。历史测试/提交步骤不批量补勾，OUT不改写为已实现。
- “主体已实施”只表示当前代码及相应回归能对照历史主要交付物；未执行的生产、性能、手工视觉、第三方验收仍明确保留。当前存在5项P2缺陷，项目整体不能标记完全完成。

## 按创建记录依次核对

| # | 首次Git提交时间 | 提交 | 计划 | 分支及接手勾/空 | 本轮结论 | 对照证据与剩余边界 |
|---|---|---|---|---|---|---|
| 1 | 2026-05-27T09:36:18+08:00 | `24c8bec` | [AI_WRITING_STUDIO_PLAN.md](AI_WRITING_STUDIO_PLAN.md) | A 0/0 | 历史阶段主体存在；发布未全验收 | A：projects/generation/retrieval、写作与成人测试；Phase 5 的旧进度改由09-14与当前状态解释。 |
| 2 | 2026-06-08T14:17:41+08:00 | `185bb2d` | [2026-06-08-unified-job-queue.md](archive/superpowers/plans/2026-06-08-unified-job-queue.md) | M 0/64；A 0/64 | 历史主体已实施；取消边界仍有缺口 | jobs/models、manager、runner、tasks；test_jobs_*。偏好发布取消见 A14-02。 |
| 3 | 2026-06-08T14:17:41+08:00 | `185bb2d` | [2026-06-08-web-jobspec-runner.md](archive/superpowers/plans/2026-06-08-web-jobspec-runner.md) | M 0/14；A 0/14 | 历史主体已实施 | 共享 Web JobSpec 与状态查询；test_webapp_jobs。不是全任务业务语义无缺陷的证明。 |
| 4 | 2026-06-08T14:20:18+08:00 | `3546a4f` | [2026-06-06-qwen-embedding-robustness.md](archive/superpowers/plans/2026-06-06-qwen-embedding-robustness.md) | M 0/15；A 0/15 | A 历史实现；M 仅保留归档计划 | A：ai/retrieval.py、test_ai_retrieval；M 按拆分决策不提供检索。 |
| 5 | 2026-06-08T15:03:27+08:00 | `d4a5ce1` | [2026-06-08-cli-job-services.md](archive/superpowers/plans/2026-06-08-cli-job-services.md) | M 30/0；A 30/0 | 主体已实施，备份仍有缺陷 | jobs/services、test_cli_jobs/test_jobs_services；错误 JSON 误报成功见 A14-01。 |
| 6 | 2026-06-15T18:01:07+08:00 | `23e5a81` | [MODULARIZATION_PLAN.md](archive/MODULARIZATION_PLAN.md) | M 0/0；A 0/0 | 部分实施；旧完整拆分目标未达成 | storage mixin/门面已落地；webapp/sync_engine 仍超过2000行，原 <500 行目标不成立；大重构现为候选。 |
| 7 | 2026-06-18T09:15:24+08:00 | `0dab8cf` | [ACTION_CHECKLIST.md](archive/ACTION_CHECKLIST.md) | M 9/65；A 9/65 | 历史清单；非全部完成 | 9个已勾里程碑与65个空步骤不能作为完成率；OpenAPI、首次配置向导、性能/覆盖率旧目标未全交付，按UNIFIED §19解释。 |
| 8 | 2026-06-18T09:15:24+08:00 | `0dab8cf` | [CRITICAL_BUGS_FIX_PLAN.md](archive/CRITICAL_BUGS_FIX_PLAN.md) | M 0/8；A 0/8 | 历史修复主体存在；非完整新验收 | 连接/外键/事务/JobManager及回归可核对；旧行号、压力测试与所有未来建议不视为本轮已完成。 |
| 9 | 2026-06-18T09:15:24+08:00 | `0dab8cf` | [OPTIMIZATION_ROADMAP.md](archive/OPTIMIZATION_ROADMAP.md) | M 0/92；A 0/92 | SUPERSEDED / OUT | 被06-26评审及UNIFIED取代；Docker、多账号、插件、国际化等未实现且不属当前承诺。 |
| 10 | 2026-06-30T10:52:47+08:00 | `e32ed06` | [OPTIMIZATION_PLAN_2026-06-30.md](archive/OPTIMIZATION_PLAN_2026-06-30.md) | M 0/0；A 0/0 | 部分实施与后续建议混合 | 第一节修复有后续回归；统一响应/两任务系统归并/大模块拆分不是已交付功能。 |
| 11 | 2026-06-30T10:52:47+08:00 | `e32ed06` | [2026-06-26-job-cancellation-hardening.md](superpowers/plans/2026-06-26-job-cancellation-hardening.md) | M 13/0；A 13/0 | 原 quick-sync/sleep 范围已实施 | test_sync_cancel_propagation、test_jobs_concurrency；后续偏好任务取消仍有 A14-02，不能推广为全任务无缺口。 |
| 12 | 2026-07-14T15:29:56+08:00 | `3a7a8d2` | [2026-07-14-release-blocker-fixes.md](superpowers/plans/2026-07-14-release-blocker-fixes.md) | M 0/42；A 0/42 | 代码主体已实施；Linux验收待补 | test_env_security、test_webapp_security、test_ai_security_hardening；两分支各6项Windows跳过不能算通过。 |
| 13 | 2026-07-16T10:20:09+08:00 | `e31631f` | [2026-07-16-ai-cover-style-controls.md](superpowers/plans/2026-07-16-ai-cover-style-controls.md) | A 0/36 | A 主体已实施；真机/外部待验收 | test_ai_project_covers、test_style_control、章节风格保存；M有意不收录。 |
| 14 | 2026-07-16T10:20:09+08:00 | `e31631f` | [2026-07-16-ai-page-layout-refactor.md](superpowers/plans/2026-07-16-ai-page-layout-refactor.md) | A 0/21 | A 页面拆分已实施 | dashboard_ai_*、test_ai_page_routes；原页面视觉结论不等于本次真机验收。 |
| 15 | 2026-07-16T10:20:09+08:00 | `e31631f` | [2026-07-16-documentation-cleanup-verification.md](superpowers/plans/2026-07-16-documentation-cleanup-verification.md) | M 0/20；A 0/20 | 历史整理；持续维护 | 当前文档已多次演进，日志保留默认为14天；原3天说明不能复用。本轮再次更新活跃契约。 |
| 16 | 2026-07-16T10:20:09+08:00 | `e31631f` | [2026-07-16-preference-task-log-closure.md](superpowers/plans/2026-07-16-preference-task-log-closure.md) | M 0/31；A 0/31 | 主体已实施，取消发布仍有缺口 | 清洗词/增量画像/统一日志有回归；A14-02证明取消后仍发布画像，不能声称完全闭环。 |
| 17 | 2026-07-17T16:21:53+08:00 | `e095689` | [2026-07-17-ai-project-overview-single-panel.md](superpowers/plans/2026-07-17-ai-project-overview-single-panel.md) | A 0/12 | A 单面板已实施；视觉验收有边界 | dashboard_ai_project、test_ai_page_routes；自动测试不替代真实手机/Provider。 |
| 18 | 2026-07-20T17:55:37+08:00 | `0b83255` | [2026-07-20-cloudflare-https.md](superpowers/plans/2026-07-20-cloudflare-https.md) | M 0/16；A 0/16 | 部署部分有历史证据；未全验收 | 10-08源站证书/TLS/Nginx已记录；账户侧Full(strict)、真实SSE和持续观察未确认，本轮未连服务器。 |
| 19 | 2026-07-21T10:44:09+08:00 | `94d6d84` | [2026-07-21-rescue-library-userscript.md](superpowers/plans/2026-07-21-rescue-library-userscript.md) | M 0/31；A 0/31 | 主体已实施；仍有脚本缺陷 | 两分支迟到响应 A14-05；M新版列表另有 A14-03/04。真实GM管理器/手机安装仍待验收。 |
| 20 | 2026-07-22T10:06:14+08:00 | `e6a7add` | [2026-07-22-rescue-catalog-sources.md](superpowers/plans/2026-07-22-rescue-catalog-sources.md) | M 0/42；A 0/42 | 目录主体已实施；性能/生产有边界 | storage/rescue、test_rescue_storage/catalog_performance；保留source/personal_hold语义，不把后续lookup缺陷写成目录缺失。 |
| 21 | 2026-07-23T20:50:31+08:00 | `21e9265` | [2026-07-23-adult-polish-agent.md](superpowers/plans/2026-07-23-adult-polish-agent.md) | A 55/0 | A 主体及后续整改已落地；外部待验收 | test_ai_adult_*、策略/认证/CAS回归纳入1785通过；未做真实Provider、SSE断连和真机新验收。 |
| 22 | 2026-07-23T20:50:31+08:00 | `21e9265` | [2026-07-23-ai-model-catalog-pools.md](superpowers/plans/2026-07-23-ai-model-catalog-pools.md) | M 115/0；A 115/0 | 第一阶段已实施；实连待验收 | catalog/pools/router、model-sync租约/CAS、相关全量测试；M只保留基础设施，写作调用链仅A。 |
| 23 | 2026-08-04T10:32:55+08:00 | `713715e` | [2026-08-04-github-readme-and-logo-refresh.md](superpowers/plans/2026-08-04-github-readme-and-logo-refresh.md) | M 0/13；A 0/13 | 历史已实施；M品牌后续被替代 | 原logo.svg资源在A使用；M采用10-09 main-logo.svg。旧方案不再覆盖M当前品牌。 |
| 24 | 2026-08-05T11:55:46+08:00 | `000d33d` | [2026-08-05-project-audit-remediation.md](superpowers/plans/2026-08-05-project-audit-remediation.md) | M 0/34；A 0/34 | 历史整改主体存在；不能概括无缺陷 | test_audit_remediation及后续09-14/09-30；本轮5项新问题仍OPEN。 |
| 25 | 2026-08-14T14:09:53+08:00 | `104c717` | [2026-08-14-ai-preference-adult-remediation.md](superpowers/plans/2026-08-14-ai-preference-adult-remediation.md) | A 0/26 | SUPERSEDED / 部分OUT | AI总结/解释、成人偏好注入不按原稿实现；被保留的取消/安全事项由09-14接管。 |
| 26 | 2026-08-14T14:09:53+08:00 | `104c717` | [2026-08-14-recommendation-completion.md](superpowers/plans/2026-08-14-recommendation-completion.md) | M 0/28；A 0/28 | SUPERSEDED / 部分OUT | 搜索计划持久表、专用RECOMMENDATION_SYNC、偏好SSE等OUT；现有推荐保留。 |
| 27 | 2026-08-14T14:09:53+08:00 | `104c717` | [2026-08-14-rescue-completion.md](superpowers/plans/2026-08-14-rescue-completion.md) | M 0/19；A 0/19 | SUPERSEDED / 部分OUT | 现有refresh_rescue_item与目录回归保留；新函数名及4593条专用夹具不是当前漏开发。 |
| 28 | 2026-08-14T14:09:53+08:00 | `104c717` | [2026-08-14-runtime-integrity-remediation.md](superpowers/plans/2026-08-14-runtime-integrity-remediation.md) | M 0/35；A 0/35 | SUPERSEDED / 部分OUT | 全量task-log lease、seen-cursor、完整trash manifest OUT；现有回滚保护及AI租约仍有效。 |
| 29 | 2026-08-28T23:50:22+08:00 | `b9bfa52` | [2026-08-28-sync-throughput-and-budget.md](superpowers/plans/2026-08-28-sync-throughput-and-budget.md) | M 0/60；A 0/60 | SUPERSEDED | 由phase1/2/3拆分取代，不能按60个空框重复开发。 |
| 30 | 2026-08-31T16:03:28+08:00 | `c5f0aa1` | [2026-08-28-phase1-sync-throughput.md](superpowers/plans/2026-08-28-phase1-sync-throughput.md) | M 0/36；A 0/36 | 仓库主体已实施；生产指标未新验收 | FTS rowid/schema、作者配额、系列页数及对应回归；没有本轮生产吞吐数据。 |
| 31 | 2026-08-31T16:03:28+08:00 | `c5f0aa1` | [2026-08-28-phase2-schedule-budget.md](superpowers/plans/2026-08-28-phase2-schedule-budget.md) | M 0/42；A 0/42 | 代码/样例已实施；灰度待验收 | cron、低频用户巡检、task registry、JOB_SYSTEM；计划Task6的生产观察未完成。 |
| 32 | 2026-08-31T16:03:28+08:00 | `c5f0aa1` | [2026-08-28-phase3-settings-ai-pages.md](superpowers/plans/2026-08-28-phase3-settings-ai-pages.md) | M 0/54；A 0/54 | 拆页/公共层已实施；浏览器有边界 | test_settings_sections、test_ai_agent_candidates及现有模板回归；M后续移动改造未合入A。 |
| 33 | 2026-09-04T11:50:58+08:00 | `b9d3686` | [2026-09-03-ai-settings-operability.md](superpowers/plans/2026-09-03-ai-settings-operability.md) | M 0/56；A 0/56 | 主体已实施；真实Provider待验收 | AI health/probe/models/batch-bindings回归；未把零网络体检当连接成功。 |
| 34 | 2026-09-16T04:05:20+08:00 | `3f5860a` | [2026-09-14-ai-writing-split-and-audit-remediation.md](superpowers/plans/2026-09-14-ai-writing-split-and-audit-remediation.md) | M 123/0；A 123/0 | 原123项为历史收口；本轮有OPEN | 分支拆分保留；A14-01/02/05为共享补漏，M还含A14-03/04和两个无调用helper。不是完全开发完成。 |
| 35 | 2026-10-08T11:08:00+08:00 | `dd23ad8` | [2026-09-30-prioritized-completion.md](superpowers/plans/2026-09-30-prioritized-completion.md) | M 11/4；A 11/4 | 原整改已交付；当前仍未全闭环 | 11勾/4空为接手快照；本轮增加缺陷，不改变旧代码交付事实。四类外部验收未全部完成。 |
| 36 | 2026-10-08T20:24:42+08:00 | `54fcacd` | M：`docs/superpowers/plans/2026-10-08-mobile-first-web.md` | M 37/0 | M本地范围已完成；真机/部署未确认 | 37勾包含本地验证/集成记录；test_mobile_*及10-09报告。30天可撤销设备登录仅M。 |
| 37 | 2026-10-09T15:07:27+08:00 | `3fa6ca6` | M：`docs/superpowers/plans/2026-10-09-main-readme-branding.md` | M 10/0 | M仓库交付完成 | main-logo.svg、README、test_main_logo/test_main_readme_branding；历史logo保留有用途。 |
| 38 | 2026-10-09T16:39:32+08:00 | `94ca7cd` | M：`docs/superpowers/plans/2026-10-09-rescue-private-install.md` | M 10/0 | 安装分发已交付；后续运行时有缺陷 | 当时0.1.1仅元信息改动；当前0.2.2已扩展运行时，旧字节一致性结论不可套用。A14-03/04/05仍OPEN。 |

## 补充需求与早期设计

| 首次提交时间 | 提交 | 文件 | 本轮判断 |
|---|---|---|---|
| 2026-04-29T08:45:15Z | `303c183` | [.monkeycode 小说页需求](../.monkeycode/specs/novel-tabs-and-user-detail/requirements.md)与[设计](../.monkeycode/specs/novel-tabs-and-user-detail/design.md) | 两文件分别追溯到同一提交；旧标签/接口已经演进，用户详情与备份主体存在，备份错误边界见A14-01。 |
| 2026-06-02T15:56:34+08:00 | `3f69422` | [偏好与推荐需求](PREFERENCE_RECOMMENDER_REQUIREMENTS.md) | 前17节是历史目标，第18节及UNIFIED描述现状；AI总结/完整多画像等OUT，取消发布缺口见A14-02。 |
| 2026-08-03T18:20:14+08:00 | `a05bba8` | [统一需求](UNIFIED_PROJECT_REQUIREMENTS.md) | 目标基线与OUT的当前依据；不能把其文件内“整合日期07-28”当首次提交日。 |

## 状态收口

- 原09-14计划的123个编号任务在两分支接手提交中均已勾选；本轮新发现通过A14编号补充，不用撤销无关历史修复或保留误导性“全部无缺陷”结论。
- A14-01/02/05涉及共享代码或同构脚本；A14-03/04仅涉及main新版列表救援。main另有两处无调用私有方法。缺陷与证据见 [审查报告](AUDIT_REPORT_2026-10-14.md)。
- 旧模块拆分、OpenAPI、首次配置向导、吞吐/覆盖率数字及远期功能不是已经全部交付；按原文、替代决策和当前范围分别标明，不能用归档掩盖未实现。
- 移动、品牌、安装是main专属的新交付；缺失于ai-writing不自动成为漏合并，也不能把main的功能文档复制成ai-writing现状。
- 所有仍需代码整改、Linux/生产副本、真实服务、真机或部署观察的项目统一以 [当前整改状态](REMEDIATION_STATUS_2026-09-30.md) 为入口。历史原文保留以便追溯。


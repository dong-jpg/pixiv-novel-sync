# 全分支执行计划时间线（2026-09-30）

> **历史审查快照**：本文记录修复前的证据与测试基线。后续修复、最新测试与剩余环境验收请看 [当前整改状态](REMEDIATION_STATUS_2026-09-30.md)。旧复现命令在修复后可能按预期断言失败。

## 方法与范围

- 枚举全部本地分支和远端跟踪引用，并以 `git ls-remote --heads origin` 确认远端当前只有 main，SHA 与 origin/main 一致。
- 范围：32 份文件名/目录明确标识为计划的 Markdown，以及 2 份 `.monkeycode` 早期设计/需求。规格文档按计划引用核对，不把所有 spec 都当独立执行计划。
- 时间使用 `git log --all --follow --diff-filter=A` 追溯改名前首次加入记录的提交时间（含时区），不是文件名日期、文件系统创建时间或归档搬迁日期；不能证明最初草拟时间。
- 同一提交的文档并列，按路径排序；以下勾选数只统计 `- [x]` 与 `- [ ]`，取当前 ai-writing 工作区，不代表完成率。
- A=ai-writing；M=main；W=三条 worktree-agent-*（同一SHA）；O=origin/main。分支缺少写作计划是有意拆分，不算丢失。
- “历史主体存在”表示历史记录/当前实现和相关测试可对照，不等于本轮逐条完成所有手工、性能、生产验收；也不因空勾就判未开发。

## 按首次提交时间排列

| 首次提交时间 | 提交 | 计划路径 | 分支 | 已勾/未勾 | 状态 | 证据/边界 |
|---|---|---|---|---|---|---|
| 2026-05-27T09:36:18+08:00 | `24c8bec` | `docs/AI_WRITING_STUDIO_PLAN.md` | A/O | 0/0 | 历史设计；当前写作闭环仍未完成 | 09-14 的 T4、T5-11 取代旧进度结论 |
| 2026-06-08T14:17:41+08:00 | `185bb2d` | `docs/archive/superpowers/plans/2026-06-08-unified-job-queue.md` | A/M/W/O | 0/64 | 历史主体已落地；非全任务新验收 | jobs/models.py、manager.py、runner.py；test_jobs_models/manager/runner.py |
| 2026-06-08T14:17:41+08:00 | `185bb2d` | `docs/archive/superpowers/plans/2026-06-08-web-jobspec-runner.md` | A/M/W/O | 0/14 | 历史主体已落地；保留勾选原状 | webapp.py 共享 JobSpec；test_webapp_jobs.py |
| 2026-06-08T14:20:18+08:00 | `3546a4f` | `docs/archive/superpowers/plans/2026-06-06-qwen-embedding-robustness.md` | A/M/W/O | 0/15 | 写作分支历史实现；main 有意移除检索 | ai/retrieval.py；test_ai_retrieval.py |
| 2026-06-08T15:03:27+08:00 | `d4a5ce1` | `docs/archive/superpowers/plans/2026-06-08-cli-job-services.md` | A/M/W/O | 30/0 | 历史主体已落地 | jobs/services.py；test_cli_jobs.py、test_jobs_services.py |
| 2026-06-15T18:01:07+08:00 | `23e5a81` | `docs/archive/MODULARIZATION_PLAN.md` | A/M/W/O | 0/0 | 历史拆分已发生；后续仍有大模块 | storage/、jobs/、web/；不是当前重构清单 |
| 2026-06-18T09:15:24+08:00 | `0dab8cf` | `docs/archive/CRITICAL_BUGS_FIX_PLAN.md` | A/M/W/O | 0/8 | 历史修复记录；不能以归档证明所有现状无错 | storage/connection.py、ai/retrieval.py；原行号已漂移 |
| 2026-06-30T10:52:47+08:00 | `e32ed06` | `docs/archive/OPTIMIZATION_PLAN_2026-06-30.md` | A/M/W/O | 0/0 | 已修复项与未决优化建议混合；非全部完成 | 正文第二节仍有双任务系统与模块拆分建议 |
| 2026-06-30T10:52:47+08:00 | `e32ed06` | `docs/superpowers/plans/2026-06-26-job-cancellation-hardening.md` | A/M/W/O | 13/0 | 已勾选；现有取消回归纳入全量测试 | test_sync_cancel_propagation.py、test_jobs_concurrency.py |
| 2026-07-14T15:29:56+08:00 | `3a7a8d2` | `docs/superpowers/plans/2026-07-14-release-blocker-fixes.md` | A/M/W/O | 0/42 | 历史修复主体存在；验收勾选未回写 | test_env_security.py、test_webapp_security.py |
| 2026-07-16T10:20:09+08:00 | `e31631f` | `docs/superpowers/plans/2026-07-16-ai-cover-style-controls.md` | A/O | 0/36 | 写作分支基础功能存在；不是 T4-06 完成证明 | test_ai_project_covers.py、test_style_control.py |
| 2026-07-16T10:20:09+08:00 | `e31631f` | `docs/superpowers/plans/2026-07-16-ai-page-layout-refactor.md` | A/O | 0/21 | 写作分支页面拆分存在 | dashboard_ai_{project,chapters,notes,reader}.html；test_ai_page_routes.py |
| 2026-07-16T10:20:09+08:00 | `e31631f` | `docs/superpowers/plans/2026-07-16-documentation-cleanup-verification.md` | A/M/W/O | 0/20 | 历史清理；需持续更新，不宜永久 DONE | 日志保留现为默认14天，旧3天不能套用调度器 |
| 2026-07-16T10:20:09+08:00 | `e31631f` | `docs/superpowers/plans/2026-07-16-preference-task-log-closure.md` | A/M/W/O | 0/31 | 历史主体存在；原步骤未逐项新验收 | test_preference_jobs.py、test_unified_task_logs.py |
| 2026-07-17T16:21:53+08:00 | `e095689` | `docs/superpowers/plans/2026-07-17-ai-project-overview-single-panel.md` | A/O | 0/12 | 写作分支基础面板存在 | dashboard_ai_project.html；test_ai_page_routes.py |
| 2026-07-20T17:55:37+08:00 | `0b83255` | `docs/superpowers/plans/2026-07-20-cloudflare-https.md` | A/M/W/O | 0/16 | 仓库配置存在；证书/Full strict 未验证 | deploy.sh；不能替代部署环境验收 |
| 2026-07-21T10:44:09+08:00 | `94d6d84` | `docs/superpowers/plans/2026-07-21-rescue-library-userscript.md` | A/M/W/O | 0/31 | 基础实现存在；线上 Pixiv/浏览器未验收 | test_rescue_api.py、test_rescue_userscript.py |
| 2026-07-22T10:06:14+08:00 | `e6a7add` | `docs/superpowers/plans/2026-07-22-rescue-catalog-sources.md` | A/M/W/O | 0/42 | 基础来源目录存在；不代表后续性能承诺完成 | test_rescue_storage.py、test_rescue_catalog_performance.py |
| 2026-07-23T20:50:31+08:00 | `21e9265` | `docs/superpowers/plans/2026-07-23-adult-polish-agent.md` | A/O | 55/0 | 历史基础已勾选；后续成人缺陷仍未收口 | test_ai_adult_*.py；09-14 T4-08～18 |
| 2026-07-23T20:50:31+08:00 | `21e9265` | `docs/superpowers/plans/2026-07-23-ai-model-catalog-pools.md` | A/M/W/O | 115/0 | 历史基础已勾选；当前续写集成有失败 | test_ai_model_router*.py、test_ai_model_api.py |
| 2026-08-04T10:32:55+08:00 | `713715e` | `docs/superpowers/plans/2026-08-04-github-readme-and-logo-refresh.md` | A/M/W/O | 0/13 | 静态文档/素材存在；历史步骤未逐项新验收 | README.md、assets/；不代表线上 GitHub 渲染验证 |
| 2026-08-05T11:55:46+08:00 | `000d33d` | `docs/superpowers/plans/2026-08-05-project-audit-remediation.md` | A/M/W/O | 0/34 | 历史整改主体存在；后续审计已取代总括结论 | test_audit_remediation.py；09-14/09-30 报告 |
| 2026-08-14T14:09:53+08:00 | `104c717` | `docs/superpowers/plans/2026-08-14-ai-preference-adult-remediation.md` | A/O | 0/26 | 不按原文执行；部分转09-14，其余按OUT | UNIFIED §1.3、09-14 T4；不是全部已完成 |
| 2026-08-14T14:09:53+08:00 | `104c717` | `docs/superpowers/plans/2026-08-14-recommendation-completion.md` | A/M/W/O | 0/28 | 不按原文执行；部分转09-14，其余按OUT | UNIFIED §1.3；无搜索计划表/专用推荐同步类型 |
| 2026-08-14T14:09:53+08:00 | `104c717` | `docs/superpowers/plans/2026-08-14-rescue-completion.md` | A/M/W/O | 0/19 | 不按原文执行；部分转09-14，其余按OUT | UNIFIED §1.3；原函数名/4593性能夹具非现状 |
| 2026-08-14T14:09:53+08:00 | `104c717` | `docs/superpowers/plans/2026-08-14-runtime-integrity-remediation.md` | A/M/W/O | 0/35 | 不按原文执行；部分转09-14，其余按OUT | UNIFIED §1.3；lease/cursor/trash manifest 非当前承诺 |
| 2026-08-28T23:50:22+08:00 | `b9bfa52` | `docs/superpowers/plans/2026-08-28-sync-throughput-and-budget.md` | A/M/W/O | 0/60 | SUPERSEDED，拆为phase1–3 | 不能再按总计划空勾重复开工 |
| 2026-08-31T16:03:28+08:00 | `c5f0aa1` | `docs/superpowers/plans/2026-08-28-phase1-sync-throughput.md` | A/M/W/O | 0/36 | 代码主体存在；生产耗时指标未新验证 | FTS rowid、作者配额、系列页数；test_fts_probe.py |
| 2026-08-31T16:03:28+08:00 | `c5f0aa1` | `docs/superpowers/plans/2026-08-28-phase2-schedule-budget.md` | A/M/W/O | 0/42 | 代码主体存在；生产灰度/观察未验证 | test_cron_validation.py、test_webapp_settings.py；非全部验收 |
| 2026-08-31T16:03:28+08:00 | `c5f0aa1` | `docs/superpowers/plans/2026-08-28-phase3-settings-ai-pages.md` | A/M/W/O | 0/54 | 代码主体存在；浏览器/移动端未新验收 | test_settings_sections.py、test_frontend_shared_layer.py |
| 2026-09-04T11:50:58+08:00 | `b9d3686` | `docs/superpowers/plans/2026-09-03-ai-settings-operability.md` | A/M/W/O | 0/56 | 代码主体存在；实连Provider未验证 | test_ai_health.py、test_ai_agent_batch_bindings.py |
| 2026-09-16T04:05:20+08:00 | `3f5860a` | `docs/superpowers/plans/2026-09-14-ai-writing-split-and-audit-remediation.md` | A/M/W | 106/17 | 进行中；T1/T2新发现、T3清理残留、T4/T5-11未收口 | 双分支测试、静态检查与本次报告；不可判完全开发完成 |

## 早期非计划文件补查

| 首次提交 | 文件 | 现状 |
|---|---|---|
| 2026-04-29T08:45:15Z / `303c183` | `.monkeycode/specs/novel-tabs-and-user-detail/requirements.md` | 六组标签、系列、用户详情、备份需求；主体已演进到现有小说库/用户页。“全部/收藏/追更”旧标签不再是当前UI规格。 |
| 同上 | `.monkeycode/specs/novel-tabs-and-user-detail/design.md` | 历史设计，接口和数据结构以当前 frontend 文档及代码为准，不按旧全文重新实施。 |

## 分支版本差异

| 分支/工作区 | 09-14 计划已勾/未勾 | 注意 |
|---|---|---|
| ai-writing 提交 `6fa9f9f` | 104/19 | T4-02/09 的修复已提交，但计划未勾。 |
| ai-writing 本轮接手时的工作区 | 106/17 | 已补勾 T4-02/09；T4-01 和路由清理代码有未提交修改，报告草稿未同步。 |
| main 提交 `364c8b7` | 77/46 | T0/T1 大量空勾滞后于提交与实现；T4/T5-11 是写作专属，不能用该分母计算 main 完成率。 |
| 三条旧 worktree-agent 分支 `3f5860a` | 各 1/122 | 同一历史起点，均被 main/ai-writing 包含，无独立未合并成果。 |
| origin/main `d7425ba` | 无09-14计划 | 远端仍停在09-11；不能把本地完成状态写成远端已部署。 |

本轮撤回 T1-09、T1-13、T2-15、T2-47、T3-01/T3-02 的完成勾选，最终工作区为100勾/23空；上表保留接手快照，避免修改文档后混淆审计基线。

完整风险、测试结果、文档更新边界见 [审查报告](AUDIT_REPORT_2026-09-30.md)。

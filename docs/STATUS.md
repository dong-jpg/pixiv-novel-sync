# 项目现状

**最近更新：2026-10-29**

这是项目当前状态的唯一入口：版本、测试基线、部署和待办都只在这里更新。每轮审查、部署、整改的详细经过另存带日期的记录，放在 [archive/](archive/README.md)，不在多份旧文档里反复回写。

## 版本

| 分支 / 组件 | 当前 | 说明 |
|---|---|---|
| main | 2026-10-29（审查文档 `fef2ff6` 之后的修复批，见下方待办） | 私人书库：同步、归档、阅读、规则推荐、救援；AI 只用于偏好关键词清洗 |
| ai-writing | `adb2d95`（2026-10-20） | 在 main 的基础上加写作/成人模块；只移植共享修复，不反向合入 main |
| 救援用户脚本 | 0.12.0 | `userscripts/pixiv-rescue.user.js`，管理器经 `@updateURL` 自动更新 |

## 部署

最近一次有书面记录的生产部署是 2026-10-20 的 main `0c7578e`（[部署记录](archive/SERVER_DEPLOYMENT_REPORT_2026-10-20.md)）。之后的提交是否已上线，以服务器上 `git log -1` 为准；更新服务器后在这里改写本节。2026-10-29 的修复批尚未推送和部署。

## 测试基线

| 分支 | 命令 | 结果 | 环境 |
|---|---|---|---|
| main | `python -m pytest -q` | 1519 passed / 6 skipped，227 秒 | Windows，2026-10-29（含本批 22 项回归测试） |

6 个 skip 是 Windows 下的符号链接权限与 POSIX 文件 mode 用例，不算安全验收通过，Linux 发布前仍需跑一次。ai-writing 上一次全量结果是 1788 passed / 6 skipped（2026-10-20），本轮未重跑。

## 待办

### 代码与部署

来自 [2026-10-28 审查](archive/AUDIT_REPORT_2026-10-28.md)，编号见该报告。没有 P1。

**2026-10-29 已修复（main，未部署）**，每项都有先在原代码上失败、修复后通过的回归测试：

- A1：`ai/providers.py` 补上 `logger`（这一项的 bug 已在生产）；新增 `tests/test_static_undefined_names.py`，用 symtable 检查全仓未定义的全局名称。
- S1：关注作者的作品列表与系列翻页遇到 Pixiv 错误页按失败 / 截断计数，不再当成空页。
- S2：`novel_detail` 返回错误 JSON 时在写库前中止，已归档作品不再被覆写成占位数据；作品确实被删时记 `content_unavailable`。
- S3：自动刷新的 Web Cookie 同步写入 `os.environ`；Xvfb 结束后还原 `DISPLAY`。
- S4：配了 Cookie 却读不到追更清单时，追更同步与待删检测都标 `watchlist_unavailable`，任务日志记黄色；待删检测把子检测的中止标记提到顶层。
- D1：取消追更的系列，其章节不再以「我的追更」进救援目录（章节自己被收藏过的除外）。
- D2 / D3：救援 lookup、详情评估与系列统计改用 `has_content`，不再读整本正文；读接口与目录对只含空白的章节判定一致。
- W4：删除收藏在同一事务里刷新救援目录条目。W6：lookup 的 ID 去重生效。F2：同步设置页「有未保存的修改」只在真有改动时出现。
- 性能 P3 / P4：正文详情只发一份正文、中文按 UTF-8 输出（20 万字 2.4 MB → 约 0.6 MB）；日志列表不再带日志行（一页 4–5 MB → 约 10 KB），详情照旧完整。

**仍待处理：**

1. **W1（部署配置，P2）**：生产 Nginx 之后不开 `DASHBOARD_TRUST_PROXY` 时，登录限流与救援 API 限流全站共用一个桶，陌生人输错 5 次就能让你暂时登不上；`PIXIV_COOKIE_SECURE` 缺省时 30 天 cookie 没有 Secure。改 `.env` 与 Nginx real_ip，不需要改代码。部署本批修复前后，用审查报告 S2 一节的只读 SQL 查一次生产是否已有被覆写的作品。
2. **F1（P2）**：阅读页与 EPUB 不解析 Pixiv 正文标记。
3. **性能**：Tailwind Play CDN 换静态 CSS（首屏快 0.5–0.9 s）；封面懒加载；救援脚本的列表扫描与错误分类（0.12.1）。

其余 P3、性能、死代码清理与部署脚本建议见审查报告。需要拍板的：cron 任务的重启补偿（现行为被测试锁定）、user_backup 是否有意每本重抓、统一需求 §9.3「正常页面不得请求救援 API」与 0.12.0 行为的冲突。

### 仍待外部验收

以下是 2026-10-14 列出的外部验收项，之后没有新的书面验收记录：Linux 安全用例；脱敏生产库的迁移、回退与备份恢复演练；真实 Pixiv / Provider 的认证、限流、取消与预算；Cloudflare 账户侧 Full (strict)。救援脚本 0.9～0.12 已在服务器真机页探针和用户手机上验证（方法见 [救援指南](RESCUE_USER_GUIDE.md)）。

### 明确不做

- `rebuild_rescue_catalog` 增量化：全量重建保证绝对一致，继续不做。原依据「5000 本 577 ms」没有留下测量条件（仓库里的测试夹具正文只有两个字）；接近生产的合成库上约 2 s，先做同一事务内的 ID 预筛即可降到约 650 ms（审查报告性能 P13）。
- `webapp.py` / `sync_engine.py` 大文件拆分：可维护性债务，不是缺陷。
- [统一需求](UNIFIED_PROJECT_REQUIREMENTS.md) §1.3 的 OUT 项不重新实施。

## 记录

| 类型 | 最新一份 |
|---|---|
| 代码审查 | [2026-10-28](archive/AUDIT_REPORT_2026-10-28.md) |
| 生产部署 | [2026-10-20](archive/SERVER_DEPLOYMENT_REPORT_2026-10-20.md) |
| 整改台账 | [2026-10-08 ～ 10-20](archive/REMEDIATION_STATUS_2026-09-30.md) |
| 计划时间线 | [2026-10-14](archive/PLAN_AUDIT_2026-10-14.md) |

更早的记录见 [历史报告整合](archive/HISTORY_REPORTS.md) 与 [历史执行计划整合](archive/HISTORY_PLANS.md)。

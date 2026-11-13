# 云端协同后续阶段：代码核对与实施边界

- 核对日期：2026-11-12（Asia/Shanghai）。
- 依据：[总体设计第 7 节](2026-11-10-vps-instabox-execution-design.md#7-后续独立阶段)、当前 `pixiv-novel-sync` rollout 工作区，以及独立 `daily-checkin` 项目的 README、接口文档和源码。下列行号对应本次核对版本。
- 本次只读审阅了签到项目代码，没有运行项目、联网签到、读取或迁移凭据、浏览器会话及用户数据库。本文不构成新增远程任务或外部账户操作授权；下述适配与进度同步均未实施、未上线。
- 当前远程任务注册表仍仅允许 `diagnostic.v1`、`epub.single.v1`（`src/pixiv_novel_sync/remote/contracts.py:13`）。一期执行闭环的验收不能替代后续业务验收。

## 1. 签到：先限定单站只读状态适配

`daily-checkin` 已有固定、有限的调用入口：`src/daily_checkin/cli.py:16` 定义 `run --site ID`，默认只读，显式 `--execute` 才允许签到；`src/daily_checkin/runner.py:37` 的公共 `Runner.run()` 持有同机进程锁。内部 `run_site()` 不应被远程适配绕过锁直接调用。`SiteAdapter.identity/status/submit` 已分开（该项目 `docs/contracts.md`），可复用状态查询边界。

本地 SQLite 已有 `(site_id, uid, day)` 唯一回执、提交前 `BEGIN IMMEDIATE` 写入 `pending`、最多三次尝试及跨日保留冷却（`storage.py:197,363`）。`pending/uncertain` 后只查状态，已发请求发生异常不能触发盲目重投（`runner.py:146`）。这些保证依赖同一私有数据目录和同机锁，尚未接入 VPS 的运行、尝试、租约或执行世代。

下一阶段首先设计固定的**单站只读状态任务**：输入仅含已配置站点 ID、配置版本与冻结业务日；输出脱敏的身份匹配、回执日期和状态证据。先以合成响应验证协议、日期与错误分类，不下发任意 URL、命令或文件路径。新增任务类型须另行实施和验收。

实际签到需另设提交边界：由 VPS 持久记录业务事件、提交前状态和结果核对信息，明确账户/日期互斥；未知副作用始终进入核对流程。`config.py:54` 按站点时区计算日期，`runner.py:115,179` 检查运行及提交时日期，但当前入口不接受原定业务日、计划版本和截止时间。应冻结这些信息，禁止重试时悄然改成当天；各站重置时区与最晚补签时限仍需逐站确认。

账户与 `browser-profile` 的权威位置、备份及撤销方式仍待决定。现有浏览器依赖私有持久 profile、首次交互登录及非 root Chromium 沙箱（`browser.py:830,908`）。项目 README 明确全部站点未真实验收、服务器部署不在首版承诺内。不得复制数据库/profile 后让本机与云端各自继续签到，也不得将本机界面直接反向代理到公网。

## 2. Pixiv：拆出指定单本的抓取结果

`src/pixiv_novel_sync/sync_engine.py:2207` 的 `_sync_novel_inner()` 仍同时执行 API 请求、正文规范化、主库查询和归档路径决策；`jobs/quick_sync.py:52` 的收藏同步也仍走现有本地通道。

下一边界是固定单本任务返回有版本、大小及摘要约束的抓取结果 DTO：云端仅抓取和规范化，VPS 验证结果后决定采用、生成归档路径、写库及更新索引。收藏顺序、来源归属、分页完整性、水位与删除判定继续由 VPS 管理，保留 `incomplete/truncated/fetch_failed` 语义。

接入前必须让 local/remote 共用账户和业务范围互斥、限流预算，核验云端出口与最小凭据范围。不得直接在云端运行整套收藏同步后回传 SQLite，也不将单本试点扩展为通用 shell RPC。

## 3. 阅读进度：独立推进服务端版本与离线同步

当前阅读页滚动时仅自动写 localStorage；只有没有本地记录才读取服务端，服务端保存仍由按钮触发（`templates/dashboard_novel_detail.html:696,757,807`）。`storage/reading_progress.py:13` 的 upsert 无版本比较；`webapp.py:1496` 的写接口也没有条件更新，旧设备可能覆盖较新的进度。

下一阶段为服务端进度增加版本和 CAS 条件写入，配合节流自动保存、离线待同步队列、冲突后的重新读取及明确用户选择。重读、回退和重置应有自己的更新语义，不能只取最大百分比；旧缓存不得无条件覆盖新版本。该工作直接服务跨设备阅读，不依赖云实例在线，应单独编写协议与回归用例后推进。

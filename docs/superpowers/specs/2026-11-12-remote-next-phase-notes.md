# 云端协同后续阶段：代码核对与实施边界

- 代码核对日期：2026-11-12；部署补记：2026-11-13（Asia/Shanghai）。
- 依据：[总体设计第 7 节](2026-11-10-vps-instabox-execution-design.md#7-后续独立阶段)、初次核对时的 `pixiv-novel-sync` rollout 工作区，以及独立 `daily-checkin` 项目的 README、接口文档和源码。下列行号对应代码核对版本。
- 初次核对只读审阅了签到项目代码，没有运行项目、联网签到、读取或迁移凭据、浏览器会话及用户数据库。随后按继续开展后续工作的指令，`4b8133e` 于 11-12 实现阅读同步、Pixiv DTO 与只读签到网关，11-13 部署到 VPS；阅读同步已启用，两个业务适配器仍未注册云端任务或迁移账户。证据见[项目现状](../../STATUS.md)和[本轮验收](../../archive/READING_PROGRESS_ADAPTERS_ACCEPTANCE_2026-11-12.md)。
- 当前远程任务注册表仍仅允许 `diagnostic.v1`、`epub.single.v1`（`src/pixiv_novel_sync/remote/contracts.py:13`）。一期执行闭环的验收不能替代后续业务验收。

## 1. 签到：先限定单站只读状态适配

`daily-checkin` 已有固定、有限的调用入口：`src/daily_checkin/cli.py:16` 定义 `run --site ID`，默认只读，显式 `--execute` 才允许签到；`src/daily_checkin/runner.py:37` 的公共 `Runner.run()` 持有同机进程锁。内部 `run_site()` 不应被远程适配绕过锁直接调用。`SiteAdapter.identity/status/submit` 已分开（该项目 `docs/contracts.md`），可复用状态查询边界。

本地 SQLite 已有 `(site_id, uid, day)` 唯一回执、提交前 `BEGIN IMMEDIATE` 写入 `pending`、最多三次尝试及跨日保留冷却（`storage.py:197,363`）。`pending/uncertain` 后只查状态，已发请求发生异常不能触发盲目重投（`runner.py:146`）。这些保证依赖同一私有数据目录和同机锁，尚未接入 VPS 的运行、尝试、租约或执行世代。

当前代码已实现固定的**单站只读状态适配底座**：输入严格限定站点 ID、配置版本、冻结业务日/时区、身份和会话代次；输出脱敏的身份匹配、日期与证据。`DailyCheckinRunnerGateway` 仅通过公共 `Runner.run([site_id], execute=False)` 接入，并对提交、重新登录、挑战处理及非 GET/HEAD 请求设拒绝边界。合成测试已覆盖协议、日期及越界，详见[适配说明](../../CHECKIN_STATUS_ADAPTER.md)。该模块仍未加入远程注册表、HTTP 或 worker 派发；真实账户和站点需单独验收。

实际签到需另设提交边界：由 VPS 持久记录业务事件、提交前状态和结果核对信息，明确账户/日期互斥；未知副作用始终进入核对流程。`config.py:54` 按站点时区计算日期，`runner.py:115,179` 检查运行及提交时日期，但当前入口不接受原定业务日、计划版本和截止时间。应冻结这些信息，禁止重试时悄然改成当天；各站重置时区与最晚补签时限仍需逐站确认。

账户与 `browser-profile` 的权威位置、备份及撤销方式仍待决定。现有浏览器依赖私有持久 profile、首次交互登录及非 root Chromium 沙箱（`browser.py:830,908`）。项目 README 明确全部站点未真实验收、服务器部署不在首版承诺内。不得复制数据库/profile 后让本机与云端各自继续签到，也不得将本机界面直接反向代理到公网。

## 2. Pixiv：拆出指定单本的抓取结果

初次审查发现 `_sync_novel_inner()` 混合 API 请求、正文规范化、主库查询和归档路径决策。已部署的代码将单本获取与有界 DTO 放入 `pixiv_fetch.py`，由现有本地 `_sync_novel_inner()` 调用；书库写入、归档路径、来源归属与同步水位仍由 `sync_engine.py` 决定。收藏同步仍走现有本地通道。

`FetchedNovel` 已提供固定版本、小说身份、规范化元数据/正文/资源列表、长度与摘要校验，以及严格序列化/反序列化边界。本地同步回归已验证接线与失败语义。下一步才是注册远程单本任务并在 VPS 收口验证结果；当前没有向云端下发 Pixiv 凭据或远程执行抓取。收藏顺序、来源归属、分页完整性、水位与删除判定继续由 VPS 管理，保留 `incomplete/truncated/fetch_failed` 语义。

接入前必须让 local/remote 共用账户和业务范围互斥、限流预算，核验云端出口与最小凭据范围。不得直接在云端运行整套收藏同步后回传 SQLite，也不将单本试点扩展为通用 shell RPC。

## 3. 阅读进度：服务端版本与离线同步已部署

初次核对时阅读页只有本机自动保存、无版本服务端写入。11-13 已部署版本 CAS、固定 mutation 幂等回执、五秒自动保存、不可变离线日志与明确冲突选择；重置保留版本标记，旧缓存作为迁移候选，旧 GET/ACK 和程序滚动不能取得覆盖新进度的权限。该功能独立于云端执行，协议见[跨设备阅读进度](../../READING_PROGRESS_SYNC.md)。

并发、断网重开、跨标签页、重置围栏和旧缓存迁移已完成合成回归与真实 Flask/CAS 的离线 Chromium 五流程验证。VPS 私有副本的两次幂等迁移，以及 11-13 在线迁移和内网/公网协议验收均已通过；真实手机使用体验仍单列，不以离线浏览器结果代替真机验收。重读、回退仍是用户意图，不使用最大百分比自动合并。

# 云端协同后续阶段：代码核对与实施边界

- 初次核对：2026-11-12；实施状态更新：2026-11-16（Asia/Shanghai）。
- 依据：[总体设计第 7 节](2026-11-10-vps-instabox-execution-design.md#7-后续独立阶段)、当前 `pixiv-novel-sync` rollout 工作区，以及独立 `daily-checkin` 项目的 README、接口文档和源码。下列行号对应本次核对版本。
- 初次核对只读审阅签到项目，没有运行项目、联网签到或迁移凭据。后续已实现只读状态适配、Pixiv 单本 DTO 和阅读进度同步；阅读进度 11-13 的部署记录见[验收报告](../../archive/READING_PROGRESS_ADAPTERS_ACCEPTANCE_2026-11-12.md)。本轮没有部署新的云端业务任务或迁移账户资料。
- 当前远程任务注册表仍仅允许 `diagnostic.v1`、`epub.single.v1`（`src/pixiv_novel_sync/remote/contracts.py:13`）。一期执行闭环的验收不能替代后续业务验收。

## 1. 签到：先限定单站只读状态适配

`daily-checkin` 已有固定、有限的调用入口：`src/daily_checkin/cli.py:16` 定义 `run --site ID`，默认只读，显式 `--execute` 才允许签到；`src/daily_checkin/runner.py:37` 的公共 `Runner.run()` 持有同机进程锁。内部 `run_site()` 不应被远程适配绕过锁直接调用。`SiteAdapter.identity/status/submit` 已分开（该项目 `docs/contracts.md`），可复用状态查询边界。

本地 SQLite 已有 `(site_id, uid, day)` 唯一回执、提交前 `BEGIN IMMEDIATE` 写入 `pending`、最多三次尝试及跨日保留冷却（`storage.py:197,363`）。`pending/uncertain` 后只查状态，已发请求发生异常不能触发盲目重投（`runner.py:146`）。这些保证依赖同一私有数据目录和同机锁，尚未接入 VPS 的运行、尝试、租约或执行世代。

已实现[单站只读状态适配](../../CHECKIN_STATUS_ADAPTER.md)：输入限定已配置站点、配置版本、冻结业务日、时区和身份/会话代号，输出脱敏的状态证据。合成回归覆盖日期、身份和错误分类。该模块仍未注册远程任务；真实站点与凭据部署须先落实下述边界。

实际签到需另设提交边界：由 VPS 持久记录业务事件、提交前状态和结果核对信息，明确账户/日期互斥；未知副作用始终进入核对流程。`config.py:54` 按站点时区计算日期，`runner.py:115,179` 检查运行及提交时日期，但当前入口不接受原定业务日、计划版本和截止时间。应冻结这些信息，禁止重试时悄然改成当天；各站重置时区与最晚补签时限仍需逐站确认。

账户与 `browser-profile` 的权威位置、备份及撤销方式仍待决定。现有浏览器依赖私有持久 profile、首次交互登录及非 root Chromium 沙箱（`browser.py:830,908`）。项目 README 明确全部站点未真实验收、服务器部署不在首版承诺内。不得复制数据库/profile 后让本机与云端各自继续签到，也不得将本机界面直接反向代理到公网。

## 2. Pixiv：拆出指定单本的抓取结果

`pixiv_fetch.py` 已拆出网络抓取与规范化 DTO，`_sync_novel_inner()` 使用其结果执行现有本地归档；收藏同步仍走本地通道。协议与大小/摘要约束见[单本抓取边界](../../PIXIV_FETCH_BOUNDARY.md)。

下一边界是把该 DTO 接入固定单本远程任务：云端仅抓取和规范化，VPS 验证后决定采用、生成归档路径、写库及更新索引。收藏顺序、来源归属、分页完整性、水位与删除判定继续由 VPS 管理，保留 `incomplete/truncated/fetch_failed` 语义。

接入前必须让 local/remote 共用账户和业务范围互斥、限流预算，核验云端出口与最小凭据范围。不得直接在云端运行整套收藏同步后回传 SQLite，也不将单本试点扩展为通用 shell RPC。

## 3. 阅读进度：独立推进服务端版本与离线同步

已实现并留下 11-13 部署证据：服务端版本 CAS、五秒节流自动保存、固定 mutation 幂等键、离线队列与冲突选择；重置保留递增版本标记，旧设备不能直接恢复旧位置。

11-16 集成分支保留上述能力并复跑合成浏览器流程；协议见[阅读进度说明](../../READING_PROGRESS_SYNC.md)。它独立于 worker 就绪状态。真实手机体验和本轮候选发布仍须分别记录，不能把历史上线等同于本轮修复已部署。

# VPS 与 InstaBox 远程执行部署验收（2026-11-12）

**状态：一期已上线，真实诊断、EPUB 回传、下载及完整空闲暂停闭环通过。** 备份全库结构检查的限制单独记录；项目当前状态由 [STATUS](../STATUS.md) 维护。

用户此前已明确要求“将功能开启，继续后续步骤”，本轮承接该指令完成部署与联调。范围为一期诊断和单本 EPUB 试点，依据[已确认设计](../superpowers/specs/2026-11-10-vps-instabox-execution-design.md)及[一期计划](../superpowers/plans/2026-11-10-instabox-phase-one.md)。

## 部署版本与进程边界

| 项目 | 已确认事实 |
|---|---|
| VPS 原版本 | `e8801b09340de3a3630e05065aa80dbf8c1af698` |
| VPS 发布版本 | 初始 `9f92d2813f4cb3f766a8f9134acd018d00a9ee59`；最终应用 `2214b22b9ebd57968d25339d537e8e2e5013b77b`，分支 `codex/instabox-vps-release` |
| 应用范围 | 基于线上版本接入远程执行；未携入作品分析第二阶段，也未增加业务主库迁移 |
| VPS 环境 | Python 3.12.3、Nginx 1.24；原 Web 服务与独立 controller 均 active/running，自动重启计数均为 0 |
| 云电脑 | 指定 InstaBox 实例 `leshao1`（`m_agq242rmauusdjgqwcpodbugke`），Python 3.13.5、systemd、专用非 root 用户 `pixivremote` |
| 云端代码 | `/opt/pixiv-remote/app` 下发 23 个 remote/渲染源码文件和 1 份渲染约束；没有下发主应用 `webapp.py`、业务存储模块、书库或业务凭据，虚拟环境没有 Flask |
| 云端依赖 | requests 2.34.2、urllib3 2.8.0；EbookLib/lxml 最终与 VPS 对齐为 0.20/6.1.1，安装于独立虚拟环境 |

VPS 保留业务数据、阅读入口、任务队列与结果接收；worker 主动通过 HTTPS 连接 VPS。原 Web unit 文件、`.env` 和 YAML 字节未改，新增 remote 专用环境配置、Web drop-in 与独立 controller unit。没有推送或合并 GitHub main。

发布归档按 Git LF 导出，校验 439 个文件的摘要与 tar mode，SHA-256 为：

```text
3e2cf885ba9770f1b660a17a9115deb9f6dd87c8460ac86d4b196d0226668ee2
```

最终补丁归档校验 440 个 Git 文件，SHA-256 为 `c2cdcc8115f34e6ed658fd8c5eca8ce1d8a5d23b7229ec9637fdc3e047ed129f`。worker 最终 24 个文件逐字节匹配 `2214b22`；服务进程仅含三项 remote 配置环境变量，没有 InstaBox 管理 key、Dashboard、Pixiv 或 AI 凭据。

## 已完成的验证

| 检查 | 实测结果与范围 |
|---|---|
| VPS 基线发布候选全量测试 | Windows / Python 3.11.3：**2697 passed, 53 skipped**，278.97 秒 |
| Node 回归 | 移动导航及 AI 设置共 **124 passed**，零失败/跳过 |
| 离线 Chromium | 390px / 1280px，启用与关闭共四种页面状态通过；真实 Flask 登录/CSRF、合成任务提交，无横向溢出或意外外部请求 |
| 静态检查 | compileall、pyflakes、Tailwind CSS 一致性通过 |
| VPS 测试 | Python 3.12.3：**149 项通过**；不称为 Linux 全量测试 |
| 最终 idle-grace 修复 | Windows 远程组 **289 passed / 1 skipped**（43.82 秒）；相关组 **153 passed**（40.53 秒）；VPS 增量 **79 passed**（204.07 秒） |
| VPS Nginx | 实际完整安装配置 `nginx -t` 通过 |
| VPS controller unit | 实际安装路径的 `systemd-analyze verify` 通过 |
| 独立 Linux 代理验证 | 合成 TLS、identity 编码、流式上传期间并发心跳、大小限制、429/Retry-After 通过，见[验证 JSON](../superpowers/execution/remote-linux-verification.json) |

本地 53 个跳过涵盖显式浏览器 opt-in、非流式参数以及 Windows 权限/POSIX mode 情况，不能计为通过。完整本地证据见发布候选中的 `docs/superpowers/execution/2026-11-12-instabox-vps-release.md`；它属于该候选分支，不能用主分支的历史测试计数代替。

## 部署保护与一次自动回退

首轮安装检查要求现有文件 mode 与 tar 的 `0644` 完全相同，因既有文件为 `0600` / `0664` 而拒绝。流程自动回退到 `e8801b0`，阅读健康检查返回 200；没有恢复旧数据库。

随后保留现有文件权限，按源码字节、Git 可执行标记、所有者可读及非 world-writable 条件检查，通过后重新部署。

以下均为 **2026-11-12，Asia/Shanghai**：

| 阶段 | 维护开始 | 维护解除 | 结果 |
|---|---|---|---|
| 首轮 | 11:10:54.897 | 11:12:49.866 | 约 115 秒，自动回退旧版并恢复健康 |
| 重试 | 11:18:22.366 | 11:20:39.391 | 约 137 秒，新版成功启用 |
| 空闲计时补丁 | 12:18:47.579 | 12:19:09.160 | 约 22 秒恢复阅读；12:19:44.782 确认新 controller 已取得租约并刷新 provider 状态 |

三个维护窗口分别记录，不将本次部署描述为无中断。服务器私有发布与回退资料位于 `/home/ubuntu/.local/state/pixiv-remote-releases/20261112-remote-v1`。小补丁仅备份独立 control 目录，保留控制器世代与业务数据库，等待旧 leader 租约自然到期后验证。

## 数据备份与恢复证据

- 在线一致 SQLite backup 已完成：2,840,309,760 字节，`quick_check` 与外键检查通过。
- 停服后另保留新鲜 `final-retry1.db` 回退副本。完整外键检查通过（0 违规，12.454 秒），全文件 SHA-256 为 `b8703b7564a255ce04123e593d634acaac291aad0a0e5457503c358188ae07c8`（48.503 秒）。
- 新鲜副本全库 quick_check 的 600 秒及 1800 秒尝试均被时限中止；确认空 WAL/SHM 无持有者后，最后一次 immutable 只读检查也达到 135 秒硬上限（实际 135.104 秒）。三次均为 incomplete，没有取得全库结构通过或损坏的结论，也不能据此归因于只读锁。
- 补充的 `sync_watermarks`、`web_auth_sessions` 两表 quick_check 通过，仅为 partial。完整 FK、全文件摘要和部分检查共 61.662 秒，最高 RSS 约 26 MiB；结果见私有 `backup/final-supplemental-checks.json`。备份与空 WAL/SHM 的大小、修改时间前后未变，校验进程已结束；这些补充检查不替代全库结构结论。
- 约 14.75 GB 归档的硬链接回退副本已保留在 VPS 本地。它与原归档共享底层数据块，不是独立的归档备份，也不是异地备份。
- 首轮回退只恢复应用与服务可用性，没有把业务数据库回滚到旧快照。

## 真实云联调进展

worker 已成功主动 HTTPS 握手。手动打开实例时 controller 的 `owned=false` 保护已实测，未擅自取得该运行会话的自动暂停权。

暂停实例后，通过用户 API 提交诊断任务，由 VPS 自动恢复云实例、取得可证明的运行归属并领取任务。相同幂等键返回同一 run。多次暂停/恢复与专用 worker 进程重启后，新会话正常握手，旧会话退役。

| 最终任务 | 结果 |
|---|---|
| 诊断 `1c30d59a-ee61-4d30-a92a-4cf1c003b638` | 114 字节，1 attempt / 1 publication；SHA-256 `4206aaeadb6d5f8f0aceb558419a3950b88ad4dce27bab8365d9637e2e653ae8` |
| EPUB `47885f8d-c28b-46fa-9062-2f9e143a6063` | 合成正文 121,393 字节、封面 68 字节；产物 55,857 字节，1 attempt / 1 publication；SHA-256 `c62902e67856005fed6fc09e11f7bf3916bc0dd9237611bbc79b4891c0bb191e` |

EPUB ZIP/CRC、正文与封面 XHTML 对照、下载长度与摘要均通过。合成快照直接进入独立控制库，没有在业务书库插入测试小说。worker 成功后工作目录仅剩自身标记文件。

真实联调落实了两项修订：

1. 短任务只执行约 5 秒，可能在两次 controller tick 之间完成。原代码沿用上一个任务的 idle_since，曾在 EPUB 完成约 61.6 秒后发出暂停。现改为真实活动在事务内重置计时，并在暂停预留/首次发送前复核；新增 8 项回归，其中 7 项在原代码失败，终态幂等重放仍不延长空闲。
2. 最初云端 EbookLib 0.21 给封面追加了样式，与 VPS 0.20 不同。保留原 XHTML 比较断言，将两端渲染依赖对齐后重新生成，验证通过；版本约束随代码交付。

最终解除安装 hold 后，最后任务于 13:43:57.546 完成，controller 于 13:49:30.600 发出暂停，间隔 **333.055 秒**，达到完整 300 秒宽限；随后 provider 确认 suspended、存储同步完成。控制库没有未结束任务或错误，hold 为空。云暂停时 VPS 健康、登录后远程页、书库页及书库 API 均正常。

公网 HTTPS 实测：健康/认证页面与书库 API 为 200；匿名下载 401、缺 CSRF 403、Dashboard Cookie 调 worker API 401、协议额外字段 400。全程保留 TLS 证书验证，未打印小说内容或凭据。

复核原始元信息见[真实闭环 JSON](../superpowers/execution/remote-live-2026-11-12.json)与[备份检查 JSON](../superpowers/execution/remote-backup-2026-11-12.json)。临时 Dashboard 探针会话已退出并删除本地凭据文件，安装 hold 已清除；VPS Web/controller 自动重启计数均为 0。

真实 provider 在暂停视图返回 `maxRunMinutes=480`，恢复运行后返回 `120`。控制器读取实际返回值安排预算，不将暂停视图的额度当成当前运行保证，也不通过保活延长平台最大运行时间。

## 待完成或需单独记录的验收

- 新鲜备份全库 quick_check 尚未取得通过证据，保留各次时限结果及补充检查的明确范围。
- 真实小说试用由用户明确选择一本后执行；当前合成输入不能代表私人小说传输已验收。
- VM 完整冷启动、平台强制中断与冷却需要各自证据；worker 握手或进程重启不能代替整机冷启动与外部故障验收。

签到、Pixiv 抓取迁移、生产业务 cron 和跨设备阅读进度仍属于后续独立阶段。本次已审阅 `daily-checkin` 的固定入口、持久去重与未知副作用处理，形成[后续实施边界](../superpowers/specs/2026-11-12-remote-next-phase-notes.md)。部署操作与回退说明见[远程执行运维手册](../REMOTE_EXECUTION.md)。

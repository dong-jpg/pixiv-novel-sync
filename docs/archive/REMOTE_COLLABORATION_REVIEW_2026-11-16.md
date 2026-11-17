# 云电脑与 VPS 协同集成复核（2026-11-16）

本轮完成分支与归档恢复、一期可靠性修复、阅读进度/适配底座集成验证及状态文档校准。当前状态统一见 [STATUS](../STATUS.md)。所有日期按 Asia/Shanghai。

## 事实来源与交付位置

- 主目录 `main=ada9f76` 不含远程执行代码，不能作为整体实现情况的唯一依据。`codex/instabox-phase-one` 提供一期实现与 `dd8209d` 恢复加固；`codex/instabox-rollout` 保存 11-12 真实联调和短任务空闲宽限修复。
- `codex/remote-phase-two=f2b768d` 留存阅读进度与适配底座实现，以及 VPS 在 11-13 部署 `4b8133e` 的历史证据。这条生产分支不含 main 的作品分析第二阶段，不能整树覆盖 main。
- 先前集成分支 `70afa9a` 合并一期与 rollout；归档快照 `31cafb5` 另外保存尚未收尾的阅读进度、Pixiv DTO、签到状态网关、代理配置与验证工具。本轮从该快照创建 `codex/remote-collaboration-audit`，保留已有成果与 main 的作品分析模块。
- 工作区为 Codex 管理的 `instabox-phase-one-8461/pixiv-novel-sync`。原主目录与其既存用户文件未改。本轮没有合入 main、推送、重启生产服务或唤醒云实例。
- 收尾时工作区应用/测试与大部分文档已保存为 `037a2af`；本记录与最后验证台账另行提交，不重写该既有提交。后续验证数量以本报告实测为准。
- 从 `codex/remote-phase-two` 原始 Git blob 恢复[阅读进度历史验收](READING_PROGRESS_ADAPTERS_ACCEPTANCE_2026-11-12.md)与[11-13 线上元信息](../superpowers/execution/reading-progress-live-2026-11-13.json)，保留其原有时间及验证边界。

## 已实现程度

| 能力 | 实现与实际验证 |
|---|---|
| VPS 主库、归档、阅读独立于云实例 | 已有实现；一期历史真实部署通过，本轮公开健康探测通过 |
| 持久任务、session、租约、fencing、冻结输入与结果发布 | 已实现，默认关闭；本轮集成及故障回归通过 |
| 按需唤醒、人工实例保护、空闲五分钟后暂停 | 已实现且有 11-12 真实闭环；本轮新修复尚未部署 |
| 诊断与单本 EPUB 云端执行 | 已有 11-12 真实 HTTPS 回传/下载证据；目前唯一已注册的两类远程任务 |
| 跨设备阅读进度 | 已实现并有 11-13 发布记录；本轮临时合成库和真实 Chromium 五流程通过 |
| Pixiv 抓取 | 单本 DTO 已拆分并用于现有本地同步；尚未接入云端任务、账户/范围互斥与共享限流 |
| 签到 | 单站只读状态网关已实现；云端调用、真实账户与提交副作用台账尚未接入 |
| 统一业务 cron | 尚未迁移；现有生产同步仍由原本地通道执行 |

## 本轮修复

1. **异步永久拒绝反复启动。** 之前 `Operation(state=failed)` 一律等待 30 秒再提交启动，即使原因是无权限、密钥失效或额度禁用。现将永久认证/配置/额度错误持久归类为停止自动提交；临时容量错误按 30/60/120/240/300 秒退避。controller 重启不抹去停止状态。
2. **provider 总读取时限。** 旧代码只在 `read1` 与 urllib3 connection socket 同时可用时收紧超时；HTTP/1.0、`Connection: close` 或旧 urllib3 回退可超出 15 秒预算。现每次读取都使用剩余时间，并从仍持有响应体的 http.client 对象取得已移交 socket；保留原有严格时限断言。
3. **worker 长驻连接池。** 每个 attempt 的传输线程完成后关闭自身 Session 并移除登记，保留监督线程连接池；全局 close 后禁止新分配。交叉审查另复现全局 close 与在途请求重开池的竞态，传输 finally 必须再次幂等关闭自己的池，不能因已被移出登记就跳过。
4. **上传响应丢失分类。** 保留 `upload_in_progress` 409，最多三次重传相同 grant、摘要和大小，每次核验取消与租约。失效授权和其他冲突不重试；持续繁忙按暂时失败交 VPS 有界重试，不记输入损坏或业务成功。
5. **租约时钟。** 在会话、租约、取消、恢复、清理、配额和输入登记的权威边界拒绝 NaN、无穷及布尔时钟，避免失效比较被绕过或无穷期限落库。没有改 schema 或 HTTP 协议。
6. **集成静态资产。** 阅读进度脚本的精确摘要对应 Git LF，但 Windows 检出转成 CRLF；补 `.gitattributes` 保持跨平台字节一致，不改摘要测试。按当前模板重新生成 Tailwind，补漏掉的 `isolate`，同步 CSS 缓存版本。
7. **状态与协议文档。** 移除“阅读进度尚未实现”等过期结论，保留 historical deployment 与当前候选的区别；前端 API 记录同步为 CAS/mutation/重置标记，补远程试点接口与后续阶段入口。

## 验证记录

- 首轮全量与代理的 RED 阶段并行，载入了旧实现并收集新回归，结果为 `25 failed, 3284 passed, 83 skipped`；不计验收通过。其中 provider 的三项时限失败与精确脚本摘要失败是归档候选的真实缺口，已修复而非放宽断言。
- 集成修复后 Windows / Python 3.11.3 全量：`python -m pytest -q --tb=short -ra --durations=10`，**3313 passed, 83 skipped，442.91 秒**。这轮在最后连接池清理竞态补丁之前，后续定向验证单列于下，不将它冒称最后补丁的全量。
- 最后连接池清理竞态补丁增加一项无网络 Event 交错回归，先 RED 后 GREEN；worker、runner、端到端与恢复四模块最终 **101 passed，21.98 秒**。该小范围补丁后未再次重复整仓全量；涉及源码 compileall / pyflakes 与 diff 检查均通过。
- 应用提交 `037a2af` 上再次完整复跑 `tests/remote`：**420 passed, 1 skipped，578.84 秒**；唯一跳过为 Windows 符号链接权限。此轮包含最后连接池竞态修复以及独立只读签到适配测试，源码/资产自该提交后无变化。
- 83 项跳过：66 个未显式启用的 AI/作品分析离线浏览器用例，10 个非流式不适用组合，7 个 Windows 符号链接/POSIX 权限用例；跳过不计通过。本轮没有重跑 Linux 全量。
- Node 所有 11 个 CJS 测试文件：**493 passed，零失败/跳过**。阅读进度 API/前端定向 **22 passed**；CSS/阅读前端/页面结构 **47 passed**；文档契约 **32 passed**。
- 阅读进度真实 Flask 登录、CSRF、CAS 与离线 Chromium：五流程、五截图通过，390/1280px 无横向溢出、可见按钮至少 44×44、浏览器错误和外部请求均为空。见[浏览器证据](../superpowers/execution/reading-progress-ui-2026-11-16/reading-progress-ui-verification.json)；使用 SRI 验证过的本地 Vue，不访问用户小说。
- 全源码 compileall、pyflakes、CSS 一致性与 `git diff --check` 均通过，逐项检查 exit code 为 0。
- WSL Ubuntu / systemd 249 / Nginx 1.18：[可复现脚本](../../scripts/verify_remote_deployment.py)验证 unit 语法、合成 TLS、identity 编码、客户端地址替换、HSTS、流式上传期间并发心跳、16 KiB/33 MiB 限额以及独立 429/Retry-After，均通过。见[JSON 证据](../superpowers/execution/remote-deployment-2026-11-16.json)。原始 unit 仅因 `/opt/.../pixiv-remote` 未安装而返回 1，使用临时 ExecStart drop-in 后语法验证通过；没有安装或启用真实服务。

## 本轮只读现场检查

InstaBox `get_machine` 返回指定 `leshao1` 为 `suspended`、`runtime.ready=false`、无 `cooldownUntil` 或在途 operation；配置为 4 vCPU、8192 MiB、20 GiB 持久盘，当前挂起视图 `maxRunMinutes=480`。这些是控制面元信息，不证明来宾进程、盘内余量或下一次运行额度。

11-16 14:27（Asia/Shanghai），保留证书验证的匿名 HTTPS GET：健康接口 200、远程页面 302、远程任务与阅读进度 API 401。见[公网探测](../superpowers/execution/remote-public-2026-11-16.json)。未使用 Dashboard 凭据或写入真实阅读位置，未登录 SSH、核对生产 HEAD、修改配置或制作新备份。

## 剩余交付与外部验收

本轮候选还需按目标生产版本制备发布差异并核对备份、目录与服务权限；由于候选保留 main 的作品分析，不能把当前完整分支直接当成仅含远程补丁的生产更新。云端 worker 和 VPS controller 的变更应分别检查。

后续 Pixiv 云端任务仍缺任务级凭据、账户/范围互斥、共享限流与结果收口；签到仍缺冻结业务日的副作用台账、profile 权威位置与逐站真实验收。完整 VM 冷启动、平台强制中断/冷却、真实手机体验、私人小说试用以及独立/异地备份继续保留为独立门槛。

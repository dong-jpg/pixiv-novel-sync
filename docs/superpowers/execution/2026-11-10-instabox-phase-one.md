# InstaBox 一期执行记录

> 本文件保留 11-11～11-12 的实现证据。后续真实启用见[11-12 部署验收](../../archive/REMOTE_EXECUTION_ACCEPTANCE_2026-11-12.md)，阅读进度上线见[11-13 验收](../../archive/READING_PROGRESS_ADAPTERS_ACCEPTANCE_2026-11-12.md)，本轮分支/归档汇合与修复见[11-16 协同集成复核](../../archive/REMOTE_COLLABORATION_REVIEW_2026-11-16.md)。当前版本与待办统一以[项目现状](../../STATUS.md)为准。

- 执行日期：2026-11-11 至 2026-11-12，Asia/Shanghai。
- 恢复来源：会话 `01a1c9f5-a5a2-79f1-8899-a3649ab5ee7a`《制定云电脑与VPS整体协同方案 (2)》，最后指令为“开始执行”。
- 依据：[已确认设计](../specs/2026-11-10-vps-instabox-execution-design.md)与[一期计划](../plans/2026-11-10-instabox-phase-one.md)。
- 基线：`ada9f7699f645116047382b4658fda06f7391090`；分支 `codex/instabox-phase-one`。
- 工作区：Codex 管理的独立 worktree `C:\Users\dong\.codex\worktrees\instabox-phase-one\pixiv-novel-sync`。
- 范围：本地实现、合成数据和 fake transport 验证。本记录不授权云实例操作、线上安装、推送或合并。
- 当前环境没有提供 FastCtx，旧计划引用的 Superpowers 技能也已不存在；使用当前原生 worktree、子代理、PowerShell/rg 和 pytest 完成同等的分工、审查与验证。

## 基线证据

Python 3.11.3；已有依赖可导入，未重新绑定共享 editable install。

| 检查 | 实测结果 |
|---|---|
| `python -m pytest -q --ignore=tests/remote` | 2807 passed, 82 skipped，368.87 秒 |
| `python -m compileall -q src` | 通过 |
| `python scripts/build_tailwind_css.py --check` | 通过 |

## 实现与审查进度

| 计划任务 | 初始失败证据 | 当前本地证据 |
|---|---|---|
| 1 协议、配置、测试隔离 | 新模块不存在，2 个 collection errors | 13 项协议/配置测试通过 |
| 2 控制库、会话、租约 | store 模块不存在，2 个 collection errors | 协议/配置/队列/会话合计 23 项通过；另有独立并发与 GC 审查 |
| 3 输入与两阶段结果 | blobs/epub_task/results 模块不存在 | 子代理验证 68 passed, 1 skipped，含原有 EPUB 回归；Windows 无 symlink 创建权限的用例跳过 |
| 4 HTTP 与认证 | 新 API 返回 404，缺失实现导致断言失败 | 新 HTTP/队列/结果组 35 项通过；原登录、安全、任务、EPUB、测试隔离组 172 项通过 |
| 5 provider 适配 | 新模块不存在 | 精确 wire decoder/transport 测试通过，公开契约构造 fixture，不是账户抓包 |
| 6 controller | 新模块不存在 | provider 58 项、lifecycle/GC 审查回归 37 项通过，含显式恢复和未知操作处理 |
| 7 worker | 新模块不存在 | worker/runner/CLI/部署契约/端到端合计 82 项通过；总请求时限、磁盘清理、迟到续租和独立看门狗已纳入回归 |
| 8 试点页面 | 页面与交互功能尚未存在 | 5 项页面与 Node VM 交互测试通过；390/1280px 启用与关闭共四种离线浏览器核验通过 |
| 9 CLI/部署工件 | 新 CLI 不存在 | 离线 doctor、独立角色、配置、服务契约测试通过；未安装服务 |
| 10 故障闭环 | 首次端到端故障注入暴露测试注入次序问题及临时错误分类问题 | 12 项真实本地 HTTP/SQLite/controller/worker 端到端测试通过；全量 3080 passed, 83 skipped |

## 审查中落实的改进

1. 队列不接受未登记输入。`enqueue` 在创建 run 的同一个事务中检查 manifest、EPUB text 和所有 ready 引用；修正原计划演示代码使用任意虚构摘要也可入队的弱点。
2. 上传失败或进程崩溃留下的孤儿文件仍计入配额。预留和释放配额时仅扫描专用平面目录的文件元数据，不持数据库锁读取正文。
3. GC 的最终删除重新检查对象状态，并在短事务内完成 unlink 与登记删除。并行 GC 不能用旧列表删除刚被重新使用的同摘要对象；新文件引用同时核对实际文件存在和长度。
4. 普通重启保留 epoch；旧备份恢复撤销会话与 attempt、关闭 gate 并保留在途云操作。运维显式重新启用 controller 后，先核对旧操作再开放新领取。
5. provider 的 Operation wire 字段为 `type`，内部转换为 `action`；`202` 只表示受理。未知状态、预算或归属均不开放领取或自动暂停。
6. 临时 HTTP 429/5xx 按 `worker_transient` 处理，由 VPS 决定最多三次尝试；不误判为不可重试的正文错误。
7. 独立看门狗在线程等待 HTTP 时仍停止已过期渲染。续租回执跨越原本地停止期限时拒绝恢复执行；文件传输线程也在每个工作边界核对暂停造成的墙钟/单调时钟差异。
8. 首次领取前在 SQLite 事务外探测结果目录可写及实际空闲容量，分配 attempt 的事务内再检查逻辑配额。容量不足仍排队，不消耗重试次数；原 claim 的幂等回执不受新检查影响。

## 综合验证

- 首次全量收集发现 `tests/remote/test_deployment_contract.py` 与既有同名测试模块冲突；新增 `tests/remote/__init__.py` 隔离命名空间后恢复正常收集。
- 随后的全量运行：3079 passed, 83 skipped, 1 failed。失败是新看门狗测试暴露的取消状态竞争，已修复，未将这一轮记录为成功。
- 修复后 `python -m pytest -q`：**3080 passed, 83 skipped，2335.92 秒**。
- 最后增补存储就绪检查后，`python -m pytest tests/remote/test_store.py tests/remote/test_lifecycle.py tests/remote/test_results.py tests/remote/test_end_to_end.py tests/remote/test_worker.py -q`：**130 passed，310.26 秒**。覆盖队列、controller、结果、worker 和端到端；该小范围改动未重跑约 39 分钟的既有全量测试。
- 上传时配额变化的 `storage_quota`（413）保留为临时错误码，并纳入有界纯计算重试；最后 `python -m pytest tests/remote/test_worker.py -q`：**50 passed，1.05 秒**。
- `python -m compileall -q src`、`python scripts/build_tailwind_css.py --check`、`git diff --check` 均通过；空显式环境的 `doctor` 返回 `remote disabled`，没有建立服务或云连接。
- 83 个跳过项包含基线已有的 82 项，以及当前 Windows 用户不能创建符号链接的一项。其他路径校验测试正常执行。

页面证据：[390px](remote-ui-390.png)、[1280px](remote-ui-1280.png)、[关闭状态 390px](remote-ui-disabled-390.png)、[关闭状态 1280px](remote-ui-disabled-1280.png)。实际 Flask 登录/CSRF 与浏览器提交验证通过，所有可见控件至少 44×44，无横向溢出、浏览器错误或意外外部请求；详情见 [QA JSON](remote-ui-verification.json) 与 [复现脚本](verify_remote_ui.py)。Vue 固定公开依赖预先缓存并核验 SRI，浏览器验收本身通过离线请求拦截运行，没有监听服务器。

代码与测试由三名实现子代理和主代理按责任文件并行完成；在共享隔离分支完成协议、并发和安全复审后统一提交，未按原计划逐 Task 交叉提交仍在变化的依赖。

## 外部验证边界

真实 InstaBox 的 key 权限、机器身份、202 操作、冷启动、暂停/恢复、网络路径与平台预算均未调用或验证。Linux `systemd-analyze verify`、完整 Nginx `nginx -t` 仍需目标环境验证。生产版本、备份与恢复、服务用户和目录权限需在获准启用前核对。

具体启用和回退步骤见[远程执行手册](../../REMOTE_EXECUTION.md)。

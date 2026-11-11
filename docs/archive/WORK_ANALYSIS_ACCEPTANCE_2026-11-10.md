# 作品全文与 XP 分析第二阶段验收记录

日期：2026-11-10（Asia/Shanghai）。当前状态入口：[项目现状](../STATUS.md)。

## 验证身份与交付边界

- 应用与测试验证提交：`d5cfb7624d59c4fe06aa2b67008cc1dacc9269d5`。
- 第二阶段源码基线：`e8801b09340de3a3630e05065aa80dbf8c1af698`；分支 `codex/work-xp-analysis-design`。第一阶段发布没有重跑。
- canonical Git 归档包含 421 个文件，逐个核对 Git blob、路径和可执行 mode；Windows 与原生 Linux 验证使用同一 LF 源码，不把 Windows 工作副本 CRLF 宣称为相同字节。
- 本记录之后的状态收尾提交只更新文档，不改变上述应用、测试、配置或静态资产。下列全量结果属于该明确提交，不伪称在后来文档提交上重新运行。
- 仅本地实现与验收；未推送、未创建 PR、未合并主目录或部署。原主目录用户修改保留。
- 没有读取或外发用户小说，没有真实 Pixiv/付费 Provider 调用；所有分析数据为临时库、合成文本和 fake Provider。

功能仅处理用户明确选择的已归档单篇/系列，先确认正文范围、章序、外发目标和预算，再生成带源版本与短证据的作品报告。全文输入覆盖、原作完整性和解释可靠性分开显示；不恢复个人画像 AI 总结、推荐闭环、写作、自动全库扫描或定时付费分析。旧 main continue 仍不开放。

## 最终执行结果

| 门槛 | 实际结果 | 环境/说明 |
| --- | --- | --- |
| Windows 全量 pytest | **2807 passed, 82 skipped**，362.49 秒，exit 0 | Python 3.11.3 / SQLite 3.40.1 / pytest 9.0.3；共 2889 用例 |
| 原生 Linux 全量 pytest | **2756 passed, 133 skipped**，282.48 秒，exit 0 | WSL 原生目录，Python 3.10.12 / SQLite 3.37.2；同样 2889 用例 |
| 全部 9 个 Node 测试文件 | **466 passed**，零失败/跳过 | Node v22.22.1；不是额外 466 个互不重复的 Python 用例 |
| 旧 reader direct 入口 | 通过，exit 0 | 独立直接执行，非只依赖 Node test runner |
| Python 静态 | compileall、pyflakes 均 exit 0 | 实际全部源码；pyflakes 3.4.0，无诊断 |
| 显式离线 Chromium/Vue | **66 passed**，零失败/跳过，173.08 秒 | 30 个作品分析 + 36 个 AI 设置用例；375/768/1440 各 22 个 |
| Tailwind CSS | 已是最新，exit 0 | 离线固定 3.4.17 CLI，另核对官方固定校验和 |
| 离线 wheel | 构建及完整包字节检查通过 | 130 个源码/资产与 canonical LF 完全相等；ZIP/RECORD 另行复核 |
| 来源前后核验 | 四组门槛均通过 | 421 个文件、归档与本地 Vue hash 不变；Linux mode 相同 |

浏览器使用 Playwright 1.60.0、Chromium 148.0.7778.96、本地 SRI 校验 Vue 3.4.38。每个 HTTP 请求由合成 fixture allowlist 拦截；CDN 形状的 Vue URL 由本地字节应答，不从公网下载。禁止 service worker，未知请求和页面错误使测试失败。凭据环境变量清理不是操作系统级网络隔离，离线结论依赖实际 fixture 和请求核验。

wheel 使用已有 Python 3.12.14、pip 26.2.2、wheel 0.48.0、setuptools 84.2.1，参数为 `--no-index --no-deps --no-build-isolation --no-cache-dir`；未安装或更改共享环境。

### 跳过分类

| 原因 | Windows | Linux | 补充证据 |
| --- | ---: | ---: | --- |
| 全量默认未设置 `AI_UI_VUE_JS` | 66 | 66 | 随后的显式浏览器门槛将这 66 项全部实际执行，零跳过 |
| 仅适用流式模式的非流式参数组合 | 10 | 10 | 4 个空流 fallback、4 个部分流、2 个已发布部分文本组合；不是作品 partial 测试缺失 |
| Windows 符号链接权限/POSIX mode | 6 | 0 | Linux 原生环境执行这 6 项 |
| Linux 没有既有 Chromium | 0 | 56 | 既有 rescue/userscript 浏览器用例；Windows 对应文件无跳过；没有安装 Linux 浏览器 |
| Python 3.11 `Connection.setlimit` API | 0 | 1 | Linux Python 3.10 不支持；Windows 执行对应目录用例 |
| 总计 | **82** | **133** | 不把跳过算成通过，不宣称 Linux 浏览器验收 |

### 浏览器与视觉

最终 15 张 PNG 和 15 份请求轨迹覆盖三宽度的系列创建、未知创建回读、取消后检查点、默认折叠报告证据、同 hash 阅读器高亮。轨迹共 321 个累计快照行，不等于 321 个独立网络请求。只有合成来源及本地应答 Vue；请求 URL/body 不含原文、报告、引文或真实凭据，写入携带合成 CSRF，重试保持同一创建请求和幂等键。

最终 15 张 PNG 已全部视觉复核：独立审查者检查创建、未知创建、报告和阅读器的三宽度共 12 张，控制器补齐三张取消检查点，并复核手机截图及两张宽屏截图。没有未关闭的视觉问题：字段和按钮在视口内换行，阅读器证据标题不再被固定导航遮挡，HTML 形状的内容仍为文字，证据默认折叠。该复核不以自动 viewport 断言替代，也不把全页截图中既有固定导航的位置误当作新布局问题。触屏模拟不代表真机软键盘或辅助技术验收。

## 审查闭环与失败历史

逐任务审查之后，又进行了整阶段独立源码审查、统一修复复审及独立覆盖/来源审计。重要问题均通过先失败后修复的实际回归关闭；没有删除或放宽旧断言来掩盖问题。

1. Task 8 删除后，旧报告/证据/单元读取仍可能填回敏感状态：撤销旧请求所有权，并覆盖迟到成功、错误、finally、新选择和未知写入回读。
2. 成功写入的迟到回执可能覆盖已接受的新版本或 tombstone：确认回执只结束该次写入，保留同一任务更新的状态/删除事实，后续读取失败也不回退。
3. 作品 startup recovery 失败时，普通同步仍能启动却不能取消：保留关闭 owner 与 finalization 拒绝，普通取消先走既有 JobManager；作品准入继续 fail-closed。
4. 删除发生在一次 Provider 返回之前，父状态的一秒缓存可能将真实 cancelled 结果误标为 transport_failed：在返回边界刷新既有父状态检查，保留流内节流、特定错误优先级与结算语义。
5. 非法 JSON list/object 进入枚举/集合判断时泄漏 `TypeError`：先校验类型和子 ID，再进行集合判断；实际六个 RED 后修复，四张业务表不产生半成品。

初始冻结候选 `1d3c8ed` 全量失败：Windows 2795 passed / 82 skipped / 2 failed；Linux 2744 passed / 133 skipped / 2 failed。两者均为旧 RouteRequest/RouteJobContext 字段列表未纳入已批准的可选父绑定；同一测试的 RouteResult 也补齐可选 work error 字段。修正保留旧字段顺序、已有不可变性检查及默认 `None`/旧位置参数兼容性，没有改变业务代码。失败记录保留，不移用为后续版本成功证据。

早期 Task 8 浏览器成功发生在滚动标题修复之前，后续还有一次 600 秒验证器超时，都没有当成最终通过。最终结果以本记录的 `d5cfb762` 为准。删除缓存竞态曾在新增大输入测试后的旧回归中出现，单次重跑通过没有关闭问题；继续捕获真实 RouteResult/计时并加入确定性 RED 后才修复。

## W01–W17 证据映射

| 规格 | 最终执行的主要证据 |
| --- | --- |
| W01 单作品范围 | 手动 Web-only、无自动付费 schedule、原偏好/阅读页面无新作品 API 请求 |
| W02 正文/章序 | 短/空/缺失文本行与稳定章序；空来源在 preflight 前拒绝；明确章序与归档确认 |
| W03 纯预览 | 真连接 authorizer，解密/写入/HTTP/DNS/invoke tripwire；冻结配置与过期指纹冲突 |
| W04 容量与完整目标 | 真实 16 MiB 序列化边界、边界再多一个 code point 拒绝、完整分块与实际发送；包括未来 reducer 的小模型披露 |
| W05 输入覆盖 | 无缺口覆盖、overlap 只计一次、失败和 reducer 不计原文覆盖；完整性/解释可靠性独立 |
| W06 证据 | 唯一精确短引文、Unicode 偏移、reducer 证据闭包、不同 hash 拒绝 410、阅读器有界高亮 |
| W07 有效结果 | 真实有效 JSON + `done(length)` 经 Router/executor 仍不保存检查点/报告/覆盖、不自动重试；保留请求预算 |
| W08 请求账本 | 父/子预留同事务、attachment ordinal 0 不计请求、最后预算两连接竞态、每次实际发送扣账 |
| W09 时间与预算 | 正常结算返还一次、失效 owner 不退款、崩溃保守扣片、预算修改 CAS/history |
| W10 原子性/恢复 | 两连接创建与发布/停止竞态、覆盖事务回滚、finalization 两种顺序、最新共享日志重启修复 |
| W11 共享调度 | 保持原单槽，旧 worker 真实退出后才跑 P1；暂停/取消/让位分离，关闭自动同步仍支持手动作品 |
| W12 隐私 | 真 executor/Web 产生非空业务产物后核对 generic jobs/attempts/task logs 不含哨兵；auth/CSRF/no-store/转义 |
| W13 删除 | 直接/级联删除与 rollback、迟到 usage/worker 不复活正文、前端迟到读/写回执均受保护 |
| W14 缓存/来源 | 同 hash/观察时间无变化、真实正文/配置变化失效、新章仅标旧归档；保存最终根后恢复零额外 Provider 调用 |
| W15 交互 | 66 个三宽度浏览器用例、37 个作品 Node 用例；预览刷新重新确认、未知写入与版本单调性 |
| W16 迁移 | 四表 additive migration、重复迁移、调用者事务回滚、FK/CHECK、原数据库回归 |
| W17 兼容性 | 完整旧路由/留存/同步/推荐/共享 job 回归、普通取消恢复故障路径、旧 main continue 409、全部 Node |

这里的覆盖是已执行的本地/自动证据，不承诺穷尽所有组合或真实模型判断正确。全量用例包含各命名新增/旧回归；没有用空选择跳过缺失文件。

## 可追溯产物

主要身份摘要：

| 产物 | SHA256 |
| --- | --- |
| canonical.tar | `7e3984ead610d53af5523787cfce8b3c29368ba74eea21278987f938d19862fc` |
| 421 文件来源清单 | `8c1de4d930b64999de5a7bf90f25a32d446d2f2697c53f4ee0b2b179592efab0` |
| wheel（629503 bytes） | `1bce29797e37d3acff135ff1999f5ebb504a5879067f922757233cc23bda970b` |
| 本地 Vue | `b50eeefe35d41636bb96c92b40f1df0b4fb7914e07b3c625b1ec15e9748767b9` |
| 固定 Tailwind CLI | `67f1c5e3f5a03406a7bf5badf5ada09b79f3ae78ec43450c15f7e983068da346` |
| windows-commands.json | `275d8134a1cc6c327c6d9cb4b26a81a8b63523ba5650d80d080fcfdd06a56698` |
| linux-commands.json | `06b1e4dc289fcd7c01d80ff3b5982a58851a284bf53b49fd54f3551d0ba0e5f4` |
| browser-commands.json | `178289a9efe3fdb47d49c8f9ac2e7b3a473453fbf1ec6bb634b78bd58263624c` |
| wheel-commands.json | `ea80f54f34b24da1e9d80e2d20bda6a89e10fcc1cf892d371d05e80b2c40d16c` |

本机完整日志、JUnit、归档、wheel 和最终浏览器产物备份在隔离工作区的 `.superpowers/verification/work-analysis-final-d5cfb762/`（忽略目录，不随 Git 发布）。原始 manifest/源码位于 `C:/Users/dong/AppData/Local/Temp/waf-00e98130/`；Linux 原生验证目录 `/tmp/pixiv-ai-validation.g8YZLh/source-d5cfb762.V33Ywj`。来源清单保留原运行路径；备份没有伪造重写为新运行。

独立审查与 RED/GREEN 报告保留在 `.superpowers/sdd/work-analysis-tasks/`：`phase2-final-review.md`、`phase2-final-fixes-report.md`、`phase2-final-fixes-review.md`、`phase2-final-coverage-final.md`、`phase2-final-visual.md`。整阶段代码审查经统一修复复审通过，自动/本地来源验收与最终视觉复核均通过，没有未关闭的重要问题。本记录提供可随仓库携带的摘要，不要求读者将忽略目录误认为已提交文档。

最终盘点：Windows 仅有预期 build/lib、egg-info、pyc、pytest cache、截图/轨迹额外文件；130 份 build 副本也匹配源码。Linux 仅有 pyc/cache。没有未说明的额外可执行源码、fixture 或符号链接。完整包集合及 ZIP RECORD 复核无遗漏、重复或字节差异。

## 明确保留的外部验收

真实 Provider 的 1M/长输入准入与结果质量、厂商 token/金额计费、真实网络/远端计算取消、真机软键盘与辅助技术、生产大库性能/部署迁移，均未在本次本地测试中执行，需另行明确授权、目标与预算。应用删除不承诺备份/WAL 的物理擦除。上述边界不因测试通过自动变成发布或付费授权。

# 全分支计划与代码审查报告（2026-10-14）

## 结论

**不能判定项目已完全开发完成。** 两条活动分支的现有完整测试均通过，但本轮额外复现了 **5 项 P2 功能缺陷**，并确认 main 有 **2 个无仓库调用的私有方法**。此前“09-14 的 123 个仓库任务已勾选”是 10-08 的验收快照，不能作为当前没有遗漏的证明。

本次工作是审查和文档校准：保留生产代码及现有测试，缺陷仍为 **OPEN**。没有把复现成功写成修复成功，也没有批量补勾旧计划、恢复明确 OUT 的功能或部署服务。

- main：同步、归档、阅读、推荐、救援及 AI 基础设施；含 10-09 移动改造、独立品牌和 10-13 用户脚本 0.2.2。
- ai-writing：另含写作、向导、检索与成人模块；没有合入上述 main 专属移动改造、品牌和新版列表救援。该差异是当前分支范围，不能据此声称 ai-writing 已具备 main 的 30 天设备登录或新脚本。
- 详细逐份判断见 [按首次提交时间排序的计划台账](PLAN_AUDIT_2026-10-14.md)，当前待办集中于 [项目现状](../STATUS.md)。

## 范围与版本证据

执行了 `git fetch --all --tags`、本地/远端引用枚举、工作树检查和 `git ls-remote --heads origin`。检查时远端只有下列两条分支，均与本地一致：

| 分支 | 本轮代码基线 | 工作树 |
|---|---|---|
| main | `57def50cabb0d9e8fbf0e90cde7fa5d9cef04109` | `D:/gitcode/pixiv-novel-sync` |
| ai-writing | `314389360efe6c543f769c2d7336872279a8b0bc` | `C:/Users/dong/.codex/worktrees/audit-ai-writing/pixiv-novel-sync` |

共同祖先为 `364c8b7`，两侧各有 22 / 10 个独有提交；这不是“22 / 10 个未开发功能”。旧报告中的三个 worktree-agent 分支和旧 main 工作树不在当前活动分支/工作树清单中。此次为 ai-writing 建立隔离工作树并绑定现有同名分支，没有合并、提交或推送。

审查覆盖计划与需求关系、同步/任务/存储、救援 API 与脚本、AI 路由和分支隔离、静态引用、前端契约及部署记录。代码审查针对高风险路径，不等于形式化证明每一行或每种网络时序正确；AI 写作/成人复杂流程主要依据本次全量回归及既有专项测试，不把它写成新的实连 Provider 或真机验收。

## 验证结果

| 验证 | main | ai-writing |
|---|---|---|
| `python -m pytest -q -ra` | **1458 passed，6 skipped**，375.02 秒，退出 0 | **1785 passed，6 skipped**，449.19 秒，退出 0 |
| `python -m compileall -q src tests` | 退出 0 | 退出 0 |
| `python -m pyflakes src tests` | 退出 0 | 退出 0 |
| 用户脚本 `node --check` | 通过 | 通过 |
| 额外问题复现 | 真实 JobRunner + 临时 SQLite；原始用户脚本 + 隔离 Chromium + 模拟 GM/HTTP | 共享缺陷通过相同源码核对；旧响应问题有同构代码证据 |

每条分支的 6 项 skip 都是 Windows 平台限制：1 项安全原子写入符号链接权限、3 项 POSIX mode、2 项归档目录符号链接。它们不算安全测试通过，仍需 Linux 验证。

全量日志在各自工作树 `.superpowers/audit-2026-10-14/`：main 为 `main-pytest.log` / `main-pytest.xml` / `main-pytest.exit`；ai-writing 为 `ai-writing-pytest.log` 及同名 `.exitcode`。这些是被 Git 忽略的本地诊断产物。本报告保留可读结论；文档更新后的定向验证另记于当前整改状态，不与全量数相加。

## 已确认且未修复的缺陷

### A14-01 · P2 · 上游错误被用户备份记为成功（两分支）

- 位置：`jobs/services.py:186–188` 读取 `user_novels` 后直接取 `getattr(result, "novels", [])`，后续按空页结束。
- 触发：Pixiv 返回合法 JSON 错误对象，例如 `{"error":{"message":"Rate Limit"}}`，没有 `novels`。
- 实测：通过真实 JobRunner、任务分发及 Pixiv JSON 解析器运行，任务为 `succeeded`，`failed_users=0`，没有 `incomplete`；轮转 offset 从 0 变为 1。正常空列表的对照也为成功，因此现有实现分不清“确实为空”和“请求失败”。
- 影响：备份未获取作品却显示绿灯，掩盖限流或认证故障。对应 T1-13/T2-17/T2-20 的后续响应边界；失败后继续轮转本身可以保留，错误不能继续算成功。
- 修复方向：先校验错误字段和响应结构，再区分合法空页、失败与截断，并向父任务传播失败/部分完成；补第一页面及后续页面错误的回归。

### A14-02 · P2 · 取消已接受，仍发布新偏好画像（两分支）

- 位置：`jobs/tasks.py:376` 的 AI 清洗调用，以及 `410–415` 的默认画像写入。
- 触发：统计批次完成后、AI 关键词清洗期间请求取消。
- 实测：`request_cancel` 返回 true，任务进入 `cancel_requested`；画像关键词仍由 `old_keyword` 变成 `new_keyword`，样本数由 0 变成 1，最终任务却显示 `cancelled`。
- 原因：清洗没有接收该共享任务的取消信号；发布画像前没有再次检查取消或领取 `FinalizationClaim`。Runner 的最终 cancelled 不能撤销已写入的画像。
- 修复方向：贯通清洗的取消回调，并让画像发布与取消通过终态 claim 互斥。已经完成的增量统计批次可以保留；需要阻止的是取消后覆盖对外可见画像。

### A14-03 · P2 · 列表救援失败后不再重试（main）

- 位置：`userscripts/pixiv-rescue.user.js:334,347,364,401`。
- 原因：发出查询前就写永久 `data-pixiv-rescue="card"`；后续扫描跳过已标记卡片，异常处理不撤销标记。
- 实测：第一次模拟 503，随后恢复服务并触发下一轮扫描，两轮累计请求仍是 1，标记卡片 1，救援徽标 0。
- 修复方向：区分待查询、请求中、成功；失败后允许有界重试，重新设置 Token 或恢复网络后也应能恢复。当前可重新加载列表页重试，但这不算修复。

### A14-04 · P2 · 未知类型卡片整批超出 40 ID 上限（main）

- 位置：`userscripts/pixiv-rescue.user.js:340–341,382–385`；服务端 `rescue_web.py:483–484`。
- 原因：无链接卡片同时尝试 novel 和 series 类型，未按两类 ID 合计做分批。
- 实测：21 张具有 `data-gtm-value` 的失效卡片产生 42 个 ID；调用真实 Flask 路由得到 400「一次最多查询 40 个 ID」，21 张卡片均没有徽标。A14-03 又让失败卡片不再扫描，但两个问题有独立根因。
- 修复方向：按去重后的 novel/series ID 合计最多 40 分批，映射回原卡片并隔离单批失败；服务端的整数 ID 去重也应修正，不能用字符串与整数列表比较。

### A14-05 · P2 · SPA 切页后迟到响应展示旧正文（两分支）

- 位置：main 用户脚本 `524–528`，ai-writing 对应 `381` 附近的 `then(renderNovel)`。
- 实测：在失效小说 12345 发起救援，随后 `pushState` 到健康小说 54321，再释放旧响应。当前 URL 为 `id=54321`，面板却显示旧标题 `STALE NOVEL 12345` 和旧正文 `SYNTHETIC OLD BODY`。
- 分支证据：main 动态复现；ai-writing 保留同样无路由校验的回调和 SPA 跳转处理。本次没有把 ai-writing 的静态核对写成单独动态复现。
- 修复方向：从等待页面稳定到成功/失败回调都校验路由代次与目标 ID；切页后使旧请求失效，必要时 abort。当前整页刷新可以避开本次竞态，不是长期解决方案。

### 最小复现材料

- `python -X utf8 -B .superpowers/audit-2026-10-14/review-sync-reproduce.py`：真实任务状态、临时 SQLite、合法空页/正常画像对照；结果为 `review-sync-findings.json`。
- `review-web-userscript.py`：读取未经修改的脚本，在独立 Chromium 上隔离全部网络、模拟 GM；40 ID 校验使用真实 Flask 路由；结果为 `review-web-userscript-results.json`。
- 主审已独立重跑两份复现，均退出 0。这里退出 0 表示**缺陷按预期被复现**，不表示缺陷已经修复。没有读取真实 .env、业务数据库、Cookie 或 Provider/Pixiv 凭据。

## 实现方式与无用代码

总体结构适合目前的单用户部署：同步任务共用 JobManager/JobRunner，SQLite 事务与线程本地连接承担一致性，派生救援目录承接列表查询，ModelRouter 统一候选与预算。无需为了审查重写成另一套任务系统或引入 PostgreSQL/Blueprint 等已列为候选的方案。

本轮缺陷集中于接口交界：上游错误对象与空结果、任务取消与最后一次写入、脚本扫描状态与失败重试、页面路由与异步响应。应优先修这些有复现的边界，再讨论大范围拆分。

| 项目 | 结论 |
|---|---|
| main `ai/services/admin.py:177 _replay_resume_child` | 仅剩定义，src/templates/userscript/tests 均无调用，可作为 P3 清理项；ai-writing 同名方法有两处真实调用，必须保留。 |
| main `ai/services/keyword_clean.py:67 _conclude_route` | 从写作拆分后留下的私有 helper，main 无调用；ai-writing 的 generation 同名方法被写作/向导调用，不能跨分支机械删除。 |
| `Database` / `AIWritingService` 门面、存量写作表、`empty_authoritative` | 有调用或明确兼容约束，不是仅凭名称或旧历史就可删除的无用代码。 |
| Vulture 高置信扫描 | 两分支仅报 `connection.py:147 __exit__` 的三个未读取参数；这是上下文管理协议签名，不能据此删除。Pyflakes 通过也不意味着没有死方法。 |
| 模块规模 | main 的 webapp 2193 行、sync_engine 2126 行、providers 1659 行；历史“每文件 <500 行”等拆分目标并未全部达成。属于可维护性债务，不将未批准的大重构伪装成本轮必做项。 |

## 文档校准与剩余验收

本轮新增时间线与审查报告，更新当前状态、索引、09-14/09-30 台账、历史计划入口和相关活跃契约；补登记此前漏列的 ACTION_CHECKLIST / OPTIMIZATION_ROADMAP。历史正文与原始 checkbox 保留，新增结论覆盖其“当前状态”含义。

API 静态注册与契约逐路径核对发现 main 漏列救援 Token 撤销、批量 lookup、手动任务取消；ai-writing 漏列其中两个共享接口。已补文档；并纠正 main 无口令本机 CSRF 的过时说明。main 指南补充脚本 0.2.2 的收藏/追更列表、无链接卡片及已知限制；ai-writing 保留其自身脚本能力边界。

尚未完成的工作包括上述 5 项缺陷、main 两个无调用私有方法的清理，以及 Linux 安全回归、脱敏生产库迁移/恢复演练、真实 Pixiv/Provider 认证/限流/取消/预算、安卓真机和生产网络验证。10-08 的源站 TLS 与部署成功记录仍有效为**当日历史证据**；本轮没有重新登录服务器或确认现网 HEAD、Cloudflare 账户侧 Full (strict)、真实 SSE 和长期观察，不能宣称最新 main 已部署或项目已完全交付。


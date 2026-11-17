# 整改与完成情况报告（2026-10-14 复核）

> **已归档（2026-10-28）**：本文是 2026-10-08 ～ 10-20 三轮整改的台账，不再是当前状态入口。当前版本、测试基线与待办见 [项目现状](../STATUS.md)；下文中的「本轮」「当前」「最新」均指当时。

本文件曾沿用09-30路径作为当前状态入口。当时的依据为 [10-14审查](AUDIT_REPORT_2026-10-14.md) 和 [全分支计划时间线](PLAN_AUDIT_2026-10-14.md)；09-30审查及下面的10-08整改记录保留为历史快照。

## 10-14 当前结论

**项目尚未完全开发完成。** 两分支现有全量回归通过，但本轮补充复现了5项P2缺陷，仍未修复；main另有两处无调用私有方法待清理。原123项仓库整改完成是10-08历史验收，不代表当前所有功能、异常边界与生产验收均完成。

| 分支 | 已核对本地/远端HEAD | 本轮全量回归 | 当前范围 |
|---|---|---|---|
| main | `57def50cabb0d9e8fbf0e90cde7fa5d9cef04109` | 1458 passed / 6 skipped，375.02秒，退出0 | 含移动改造、独立品牌、救援脚本0.2.2 |
| ai-writing | `314389360efe6c543f769c2d7336872279a8b0bc` | 1785 passed / 6 skipped，449.19秒，退出0 | 含写作/成人；未合入main上述专属变更 |

两分支compileall与pyflakes均通过，用户脚本Node语法均通过。每条分支的6个skip都是Windows符号链接权限/POSIX mode限制，不能算安全验收通过。详细版本、复现、静态检查与证据边界见 [10-14审查报告](AUDIT_REPORT_2026-10-14.md)。

本轮 `git fetch --all --tags` 与 `git ls-remote --heads origin` 确认只有上述两条远端分支；没有遗留的独立agent分支成果。现工作树为main `D:/gitcode/pixiv-novel-sync`，ai-writing `C:/Users/dong/.codex/worktrees/audit-ai-writing/pixiv-novel-sync`。以下10-08记录中的旧工作树路径只保留历史意义。

## 10-14 待处理代码事项

| 编号 | 优先级/分支 | 确认问题 | 状态 |
|---|---|---|---|
| A14-01 | P2 / 两分支 | 用户备份把上游错误JSON视为空成功；错误不进入失败/部分完成统计 | **两分支已修复（2026-10-20）** |
| A14-02 | P2 / 两分支 | 偏好清洗期间取消已接受，随后仍写入默认画像，任务却显示cancelled | **两分支已修复（2026-10-20）** |
| A14-03 | P2 / main | 列表卡片请求失败后永久标记，后续扫描不再重试 | **已修复（2026-10-20，脚本 0.3.1）** |
| A14-04 | P2 / main | 21张未知类型卡片产生42个ID，整批超出lookup上限 | **已修复（2026-10-20，脚本 0.3.1）** |
| A14-05 | P2 / 两分支 | SPA切换目标后迟到响应仍把旧作品正文插入新页面 | **两分支已修复（2026-10-20，main 0.3.1 / ai-writing 0.1.1）** |
| A14-C1 | P3 / main | `_replay_resume_child`、`_conclude_route` 两处私有方法无调用；ai-writing对应方法仍在使用 | **main 已删除（2026-10-20）** |

### 2026-10-20 修复批次（main）

审查报告（见会话内 AUDIT_REPORT_2026-10-19 汇总）确定的下列问题同批修复，全部有新增回归测试锁定：

- **A14-01**：`jobs/services.py` user_backup 对 `{"error": {...}}` 错误页按失败收口（`aborted_reason="upstream_error_page"`、任务日志 partial）；`sync/utils.py:response_is_pixiv_error_page` 共享判定，`sync_engine.py` 五个分页站点（收藏/关注列表×3/收藏ID反查）同步接入，错误页不再被当空页。
- **A14-02**：`jobs/tasks.py` 偏好分析在 AI 清洗前轮询取消、画像发布前领取 `FinalizationClaim`；取消接受后画像零写入，任务按 cancelled 收口。
- **A14-03/04/05 + P2-5**：用户脚本 0.3.1——卡片 pending 两态标记失败可重试；lookup 按 novel+series 合计 40 分批；明细页请求带路由代次守卫；系列 tab「加载更多」改追加式；列表页 interval 防重复注册。
- **P2-1**：`rescue_web.py:public_auth` 增加 `CatalogNotReadyError → 503` 分支，公开列表端点与 dashboard 端点契约一致。
- **P2-2（修正版）**：重试补丁统一为 `sync/utils.py:apply_pixiv_retry_patch`（幂等，名单含 `user_detail`），`_login` 与 `BookmarkNovelSyncService` 共用；状态检查不再裸奔。
- **P2-4**：`ai/providers.py` 非流式回退与错误体读取均改为 64 KiB 分块限读（1 MiB 上限），不再先整读后检查。
- **新发现（既有 bug）**：`storage/schema.py:_migrate_ai_tables` 中 `owner_scope`/`idempotency_key_hash` 补列在路由迁移整表重建 ai_jobs **之前**执行，被重建抹掉——新库需跑两次 init_schema 才有这两列。已改为迁移后补列，新库一次建齐。
- **P3 批**：`_atomic_write_yaml` 随机临时名+模块级写锁；`list_rescue_series_chapters` 改用 `has_content`（免逐章 TRIM 正文）；`cleanup_ai_jobs` 落实 `owner_scope` 过滤；watchlist 直连翻页接入 rate_limiter；SSE 解码替换字符计数告警；`_terminal_race_result` 孤儿任务不再误报「已取消」；删除 main 无调用的 `_replay_resume_child`/`_conclude_route`；`preference_analyze` 用后显式关闭 `AIWritingService`。
- **性能批（同日第二批）**：`get_rescue_items_batch` 批量 lookup（40 ID 从 40~80 次聚合降为按批）；`crypto.py` 进程级 PBKDF2 KDF 缓存（ai_health 每请求每 provider 数百毫秒 → 首次后零成本）；`_estimate_with_providers` 逐 provider 判定（真实分词器不再被基类 None 拖累）；`fail_stale_ai_jobs` 拆出整点分支改 5 分钟节流（孤儿 AI 任务最长 1 小时 → 5 分钟收口）；`idx_rescue_catalog_item_type` 索引（新系列端点全表扫 → covering index）；dashboard 空闲轮询补 `document.hidden` 门控（20s/3s 节奏为既有契约不动）；dashboard_logs 继续任务切 `window.streamSSE`+`errorText`。

**ai-writing 移植（同日）**：A14-01/02 六文件补丁移植 + A14-05 脚本 0.1.1；全量 1788 passed / 6 skipped。

**明确保留**：`rebuild_rescue_catalog` 增量化（实测 5000 本库 577 ms，收益/风险比不足，见 [10-20 报告](AUDIT_REPORT_2026-10-20.md)）；webapp/sync_engine 大文件拆分；Linux 安全回归与生产外部验收边界沿用 10-14。

验证：main `python -m pytest -q` 全量 **1464 passed / 6 skipped**（基线 1458 → 新增回归后 1464）；ai-writing **1788 passed / 6 skipped**（基线 1785）；`python -m compileall -q src tests` 两分支通过；用户脚本 `node --check` 两分支通过。

这次只进行审查、复现和文档更新；生产代码及现有测试未修改。新缺陷不是已修复项，两个工作树的文档变更尚未提交/推送，也未部署。原有 `audit_routes.py` / `audit_routes_out.txt` 保留不动。

## 10-14 计划核对和外部验收

已按可追溯的首次Git提交时间核对 [38份计划/行动清单](PLAN_AUDIT_2026-10-14.md)：29份共享、3份仅main、6份仅ai-writing；另查早期页面需求、偏好需求及统一规格。保留原始checkbox，区分主体已实施、部分实施、替代、OUT和外部验收，不使用勾选比例宣称完成。

以下仍待完成：Linux安全用例、脱敏生产库的迁移/回退/备份恢复、真实Pixiv/Provider认证与限流/取消/预算、安卓真机、Cloudflare账户侧Full(strict)、真实SSE及长期观察。10-09移动改造已有Chromium窄视口/合成数据验收，不能继续把该本地工作写成未做，也不能扩大为真机或生产验收。

服务器最新**留存记录**仍为10-08的main `481f8a9` 与源站TLS/Nginx验证；本轮未重新检查现网版本。后续main提交已经在远端，不代表已部署。main的30天设备登录、品牌、列表救援不属于当前ai-writing的已交付能力。

---

以下保留10-08整改/交付原始记录；其中“本轮”“当前”“最新”均指该历史阶段，现状以上方10-14结论为准。



## 10-08 历史结论与交付边界

- 已按数据安全、预算/并发、功能闭环、文档的顺序实施整改；最终验收以本页证据为准，不以旧计划空勾推算完成率。
- 09-14 跨分支计划的 **123 个编号仓库任务已按实现和自动回归核实，未勾选为 0**；这不是整个项目“100% 完成”，不能替代下方尚待完成的发布与外部验收。
- **代码整改已分别提交并推送；用户已部署 main `481f8a9`，本轮 Nginx 配置及源站证书均已修复并验证；ai-writing 尚未部署**。新证书与服务器私钥匹配，官方 CA 链、服务器用途、域名及实际源站 TLS 均通过验证；Cloudflare 账户侧模式未直接读取或更改。最新服务器状态见 [部署与体检记录](SERVER_DEPLOYMENT_REPORT_2026-10-08.md)；不把探活成功等同全部生产验收，也不能保证代码不存在任何隐藏缺陷。
- 10-08 发现上轮系统临时目录中的 main 工作树与测试日志已不存在。本轮从 main 重建持久隔离工作树，按共享文件/补丁恢复并重新验证，未沿用丢失日志的成功结论。
- 写作和成人功能只在 ai-writing；没有整支反向合入 main。历史报告与历史计划分别整合，原文留存以便追溯。

## 10-08 历史分支检查

10-08 按用户授权完成提交和推送，并用 `git ls-remote --heads origin` 核对远端。提交前基线为 main `364c8b7`、ai-writing `6fa9f9f`，旧远端 main 为 `d7425ba`；本轮后续提交不改变历史计划的首次 Git 提交顺序。下表记录**代码交付提交**，其后仅追加交付状态文档；最新分支 HEAD 以 Git 引用为准。

| 引用/工作区 | 当前状态 |
|---|---|
| `ai-writing`，`05b461d` | 共享及写作/成人整改已提交；已创建并推送远端同名分支 |
| `main`，`dd23ad8` | 共享整改已在持久隔离工作树提交，远端 main 已正常快进 |
| 三条 `worktree-agent-*`，`3f5860a` | 同一历史起点，已被 main 与 ai-writing 包含；没有独立未合入成果，不重复开发 |
| `origin/main` / `origin/ai-writing` | 代码交付时远端分别核验为 `dd23ad8` / `05b461d`；推送完成不代表部署完成 |

工作区位置：

- ai-writing：`D:/gitcode/pixiv-novel-sync`
- main 持久隔离树（已重新绑定 main）：`C:/Users/dong/.codex/worktrees/main-remediation-verification/pixiv-novel-sync`
- 旧 `C:/Users/dong/AppData/Local/Temp/pns-audit/main-tree` 已失去磁盘目录；提交前核对后只清理这一条失效 Git 注册，没有删除任何现有工作目录。

## 整改覆盖与实现评价

| 范围 | 本轮实现 | 关键验证 |
|---|---|---|
| 归档回滚 T2-15 | 搬移前登记、恢复失败保留双方副本、恢复标记阻止清扫、stage 异常回滚 | 实际 EXDEV 复制后部分删源失败、重建目标不覆盖、恢复重试 |
| 任务状态 T1-13 / T2-20 | 备份传播 truncated/incomplete/aborted_reason，失败用户不报全成功，取消不计作失败用户 | `test_priority_remediation.py` |
| 推荐/画像 T1-09 / T2-47 | 查询错误与候选错误分开，坏候选不丢弃好候选；缺省名称/描述不覆盖用户设置 | 推荐与 Web 请求回归 |
| 清理 T3 | 移除 6 个旧/无生产调用路由、简单 cron 回退、不可达 OAuth 方法、重复路径收集与无用查询/导入；CLI 截断返回非零 | 必要兼容入口保留；测试专用查询迁入 helper；两工作树静态检查 |
| 预算 T4-01 | token 估算、完整轮次裁剪、动态消息开销、固定提示失败关闭、摘要/蒸馏完整成本、只估剩余候选 | 长中文、多轮向导、Provider 估算、内部失败候选隔离；粘贴文本用 document_id 引用 |
| 写作 T4-03～07 / T5-11 | 章节 revision CAS、保存期间保留新编辑、索引不倒退、Pipeline 断连/取消收口与重试合并、按钮参数、风格先保存、批量 EOF 保留已完成数 | Python 并发/事务回归及可执行前端 CJS 用例 |
| 成人 T4-08、10～18 | 别名往返、目标参与者、审查预算、令牌绑定/重放、脱敏进度、取消 CAS、父心跳/deadline、大 diff 降级、显式策略升级与历史 | 旧进程 apply、升级与建 job 竞态、warning 重新确认、只替换目标片段 |

T4-02/09 是已有提交的修复，不计为本轮重新开发。成人偏好注入采用原计划允许的**移除未实现字段并拒绝请求**方案，不把它写成已经支持。

### 末轮逐项复核追加闭环

前一轮全绿之后，再按原计划逐项检查，发现仍有四项实现/覆盖未收口，不能用全绿替代需求验收：

- **T3-01**：补全量 settings 的“仅 API”标识；阅读页接入进度重置与本地收藏删除。批量用户删除没有生产调用且可与后台备份竞争，采用原计划允许的**删接口**方案，而非新增不安全按钮。多画像工作流仍为 OUT，既有兼容 API 明确标记仅 API。
- **T3-02**：CLI 收藏同步截断返回 1；清理急切导入、重复取消分支和无事件来源分支；统一归档候选路径收集但保留原删除边界检查。`empty_authoritative` 是现有适配器/存储空目录确认协议，有正反回归，因此有意保留，不冒充死代码删除。
- **T4-07 / T5-11**：补蒸馏等待取消、默认 Agent 排除 wizard/adult、progress action/reason、检测结果非 alert 与移动布局契约回归。回归进一步暴露“等待取消被标 failed”，现用专用取消异常将两个蒸馏入口正确收口为 cancelled，普通故障仍 failed。
- **新发现的备份 P2**：同一用户前四本成功、第五本失败但未超过熔断阈值时原来仍报全成功。现返回/聚合失败小说数并标 incomplete，直接和调度路径均有先红后绿验证，原熔断及取消语义保留。
- **阅读恢复 P2**：服务端进度原来只改百分比、随后被本机 0 覆盖。现恢复实际滚动位置；手动滚动（含回到原位）或显式保存/重置在动作开始时使旧 GET 恢复失效，避免账号和本机进度分叉。

这些追加修改已纳入本页最终全量结果。共享归档/清理、蒸馏取消与成人策略事务均经独立定向复核；阅读页初审发现的三项 P2 已逐项关闭并通过二次复核。复核结论限于对应代码与测试范围，不等于没有任何潜在缺陷。

实现方向适合现有架构：并发约束落在数据库事务/CAS，展示层保留未保存内容，路由预算与 Provider 使用同一估算契约；没有引入第二套任务系统。删除范围以无调用/旧契约证据为依据，保留兼容门面，避免为“清理”破坏仍在使用的入口。

维护风险仍在：`webapp.py`、成人服务与部分页面脚本较大，离线测试不能覆盖全部真实网络时序和浏览器视觉行为。后续可渐进拆分，但本轮不做无验收收益的大重构；明确 OUT 的全量 task-log lease 重设计、游标体系、完整 trash manifest、搜索计划表/CRUD、专用推荐同步类型与多画像系统不重新排期。

## 10-08 验证记录

| 验证 | 结果 |
|---|---|
| main 提交前全量 `python -m pytest -q -ra` | **1309 passed，6 skipped**，377.31 秒，退出码 0 |
| ai-writing 提交前全量 `python -m pytest -q -ra` | **1785 passed，6 skipped**，523.93 秒，退出码 0 |
| 成人末轮独立复核 | 50 + 7 项定向通过；移除策略 guard 的进程内反向测试按预期失败，恢复后竞态与正常 apply 均通过 |
| 两工作树 `python -m compileall -q src tests` / `python -m pyflakes src tests` / `git diff --check` | 均通过；静态清理覆盖生产代码及测试，不删减原测试断言 |
| 模板脚本 `node --check` | ai-writing 26 段、main 17 段通过（按项目 Jinja 分隔符处理；不是浏览器验收） |
| 合成数据库跨分支初始化 | ai-writing → main → ai-writing，56 张表和策略历史 sentinel 均保留；未访问生产库 |
| 最终文档同步后的文档/前端/推荐定向 | ai-writing **85 passed**；main **74 passed**；包含末轮阅读动作及仅删除未用导入的推荐测试 |
| 更新文档的本地链接 | 两工作区本轮更新文档的相对链接文件目标均存在；不包含外链可达性或页内锚点验收 |

ai-writing 首轮完整结果为 **9 failed、1733 passed、4 skipped**：失败均来自成人旧策略占位夹具或在流关闭后检查“仍未取消”的旧断言。已修改三个成人测试文件：使用真实发布策略元数据，并分别断言运行期间未取消、退出后停止。**没有弱化生产策略 guard**；修正夹具后的 1742 passed 为中间基线，追加末轮修复与回归后的最终结果为上表的 1785 passed。

每个工作树的 6 个 skip 包括 3 个符号链接权限用例和 3 个 POSIX 文件 mode 用例，均因 Windows 限制跳过，不是功能通过；Linux 发布前仍需运行对应安全用例。日志与复核材料在本地主工作区 `.superpowers/verification-2026-10-08/`（Git 忽略的诊断产物，不替代本报告）。提交前全量日志分别为 `main-prepush.log`、`ai-writing-prepush.log`，对应 `.exit` 文件均为 0；定向重叠用例不与全量数相加。

## 10-08 Git 交付记录

- main 代码整改提交：`dd23ad8fa9dd69aabc823a47ca23dadcf07656e7`。
- ai-writing 代码整改提交：`05b461da61611350f176e4a23ba6310003997ab4`。
- 两个提交的 tree 均与经验证的暂存快照一致；提交前再次运行完整套件和静态检查。新增 CJS 文件末尾的多余空行在暂存检查中发现并清理，Node 行为回归再次通过，未改变实现或断言。
- `git push --atomic --set-upstream origin` 同次推送两条分支，main 正常快进、ai-writing 新建远端引用；随后独立查询远端确认上述完整提交号。Git 交付阶段没有强制推送、重写历史、反向合入写作分支或执行部署；后续用户自行部署与 Nginx 专项修复另见服务器记录。
- 两个有效工作树保留；原有 `audit_routes.py` / `audit_routes_out.txt` 内容未改动且未纳入提交。历史 agent 分支没有独立成果，不重复推送。
- 本节记录已完成的**代码交付**；随后的文档收尾提交只回写报告、执行台账及相关活跃文档的交付状态，不修改生产代码。

## 文档整合

- [历史报告整合](HISTORY_REPORTS.md)：旧审计、完成报告、架构快照及重复优化结论。
- [历史执行计划整合](HISTORY_PLANS.md)：按阶段归并历史计划、替代关系、OUT 与环境验收，保留原路径索引。
- [09-14 任务规格](../superpowers/plans/2026-09-14-ai-writing-split-and-audit-remediation.md)和[09-30 执行台账](../superpowers/plans/2026-09-30-prioritized-completion.md)记录本轮仓库事项；历史空勾不会被无差别改为完成。
- 活跃 README、开发指导、需求、任务系统、前端页面/契约、模型路由、偏好及 ai-writing 成人/Embedding 指南按当前行为更新。main 文档不恢复已拆除的写作功能。

## 10-08 当时的发布/外部验收清单

1. **发布与可回滚性**：Git 代码交付已完成，main 已由用户部署。后续切换版本或回退前仍须完整备份实际数据库和归档目录、确认恢复步骤；本次保留的 Nginx 配置备份不等于业务数据恢复点。
2. **Linux 与生产库副本**：运行平台安全用例，用脱敏生产库副本做迁移、回退与备份恢复演练；合成 56 表实验不能替代该验收。
3. **真实 Pixiv / Provider**：用目标账户及服务商验证认证、限流、取消、预算与故障切换；不以 fake router 或离线样本冒充实连。
4. **浏览器/移动端**：在实际支持的浏览器核验保存冲突、SSE 断连、Pipeline 重试、成人升级/重审、布局与交互；脚本语法和 CJS 状态回归不是视觉验收。
5. **部署及观察**：main 的 Nginx 语法测试、平滑加载、源站严格 TLS 和本机/公网探活均已通过，证书用途问题已关闭。仍须确认 Cloudflare 账户侧 Full (strict) 设置，并完成真实 SSE、灰度和长时间观察，不能标记为完全交付。

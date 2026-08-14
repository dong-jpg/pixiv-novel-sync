# 项目审计报告（2026-08-13）

## 范围与证据

本轮检查覆盖需求/README/API/页面文档、近期 2026-08-06 至 2026-08-12 成人润色与模型路由提交、`src/pixiv_novel_sync/` 全部主要模块、测试、部署脚本和调用引用。

本报告记录的是当前代码事实和待修复项，不表示下列生产缺陷已经修复。

验证结果：

- `python -m compileall -q src tests` 通过。
- 独立运行 `tests/test_rescue_api.py`：44 passed。
- 首次全量 `python -m pytest -q`：1093 passed, 4 skipped, 1 failed。唯一失败为 `test_dashboard_rotates_single_active_token`，随后独立用例和整个 `tests/test_rescue_api.py` 均通过。第二次全量重跑为 `1094 passed, 4 skipped`，因此当前没有可复现的救援 Token 业务失败；仍建议 CI 保留该测试以观察跨测试/环境偶发状态。
- `pyflakes src` 报告一个真实未定义名和多项未使用导入；未使用导入中包含测试兼容重导出，不能直接批量删除。
- 未发现纯 CPU 空转死循环；但确认了多处缺少硬上限或重复游标保护的 I/O 分页循环，详见下方 P1。

## 必须优先修复

### P0：运行中的任务日志会被任意请求误判失败

`storage/schema.py:139-140` 每次 `Database.init_schema()` 都调用 `_fix_stale_running_logs()`；`storage/schema.py:283-292` 对 `task_logs` 全表执行 `status='running' -> 'failed'`，没有进程、租约、heartbeat 或 owner 判断。Web 的多个请求路径都会新建数据库并调用 `init_schema()`，例如 `webapp.py:1229-1232`。

触发场景：任务已创建并处于 running，另一个请求打开日志/设置/救援等页面。后一个请求会把仍在执行的任务写成“进程重启，任务中断”；真正的 worker 随后可能无法正确收口，审计和用户界面都显示错误终态。

最小复现已验证：第一个 `Database` 创建 task log 后状态为 `running`，同一路径上的第二个 `Database.init_schema()` 返回后，该记录状态立即变为 `failed`。

建议：只在应用启动阶段执行 stale 回收；或者为日志增加 boot-id/owner/heartbeat，并只回收确认属于已失效进程且 lease 已过期的记录。通用 `init_schema()` 必须保持幂等且不能改变活跃任务状态。

### P0：推荐任务取消会报告 succeeded

`jobs/tasks.py:419-435` 在取消回调中捕获 `InterruptedError` 后返回 `{"stopped": True}`。`jobs/runner.py:69-79` 将任何正常返回交给 finalization，`jobs/manager.py:184-190` 对最后任务无条件写入 `SUCCEEDED`。

建议让推荐任务继续传播 `InterruptedError`，或返回带有统一取消语义的结果并由 `JobRunner` 明确转换为 `CANCELLED`；补充端到端 job 状态回归测试。

### P0：推荐失败/取消会污染已有推荐结果

`recommendations.py:83-130` 在 run 成功前逐条调用 `upsert_recommendation_item()`；`storage/recommendations.py:358-409` 每条 upsert 独立提交。唯一键命中时会覆盖旧 item 的 `run_id` 和内容；`list_recommendation_items()` 与后续去重读取也不要求 run 为 succeeded。

建议使用 run 级 staging/发布事务；失败或取消时删除临时候选并恢复被覆盖的旧值，或让所有可见/去重查询只关联 succeeded run，同时保留用户反馈状态。

### P0：成人取消未传入 ModelRouter，阻塞的 Provider 不会及时停止

`ai/services/adult.py:744-757` 和 `2420-2431` 构造 `AdultRouteRequest` 时没有设置 `is_cancelled`。ModelRouter 只有收到该回调才会在 `model_router.py:875-878,1190-1197` 轮询取消。Web/DB 的 cancel 只把 job 和 attempt 标记 cancelled（`ai_web.py:1329-1342`、`storage/ai/core.py:1380-1443`），Provider 不会收到主动取消信号；主写作最多可能在后续 delta 的数据库 CAS 中察觉终态竞争，审查请求则可能继续到响应结束或超时。

建议把 owner-scoped DB cancel 状态封装为 `is_cancelled` 回调传入主写作和两个 review 阶段；在网络读取间隙也使用可取消超时/关闭响应，确保断连和显式取消都释放 Provider 资源。

### P1：成人 SSE 的 progress 在生成完成后才发送

`ai/services/adult.py:2394-2459` 将所有 progress 收集到列表，调用同步 `model_router.execute()` 完成后才 yield。这样浏览器看不到实时路由/阶段进度；同时在 execute 阻塞期间，`ai_web.py:588-593` 的 `GeneratorExit` 尚未触发，客户端断连无法及时驱动取消。

候选正文继续保持“全量校验后才发送”的隐私边界，但 progress 应由可迭代的 `execute_stream()` 实时转发，并让断连关闭底层迭代器。

### P1：关注用户/用户小说分页缺硬上限和重复游标保护

`sync_engine.py:670-680` 仅在配置 `max_pages` 非空时限制外层分页；`sync_engine.py:722-727,808` 的用户小说内层没有页数上限和 seen cursor；`jobs/services.py:133-142,181` 的用户全量备份同样直接跟随 `next_url`。恶意或损坏的上游如果回显相同 `next_url`，会无限请求并占用唯一 JobRunner 槽位，只能依赖人工取消。

建议所有分页循环统一使用高安全上限、seen cursor、空页停止和取消检查；`max_pages=None` 也必须落到安全上限，而不是无限。

### P1：Provider 空流 fallback 和 Anthropic 非流式路径忽略 max_retries=0

OpenAI-compatible 空流 fallback 在 `ai/providers.py:867-874,917-924`、Anthropic 空流 fallback 在 `:1200-1207,1226-1233` 都硬编码 `max_retries_override=3`，因此配置 `max_retries=0` 时 fallback 仍可能发起最多 4 次请求。Anthropic 普通非流式路径 `:1277` 还使用 `max(3, self.config.max_retries)`；只有 OpenAI-compatible 普通非流式路径 `:970` 在没有 override 时尊重零重试。该行为会放大延迟、费用和取消窗口，且现有 fallback 测试没有覆盖失败请求次数。

建议让两类 Provider 的 fallback 都继承显式配置，并统一采用 `max(0, configured)`；为两条 stream 空响应 fallback 和 Anthropic 普通非流式路径增加零重试及请求计数回归测试。

### P1：legacy systemd 安装入口与 unit 路径/用户必然不一致

`scripts/install_server.sh:7-27` 使用 `/opt/pixiv-novel-sync/app`、当前工作目录 `.venv` 和 `pixivsync` 用户，但 `deploy/systemd/pixiv-novel-sync.service:8-11` 使用 `ubuntu`、`/opt/pixiv-novel-sync/app-redeploy` 及其中不存在的 venv。按脚本安装并启用 timer 后，ExecStart、WorkingDirectory 和权限主体不匹配。

建议统一脚本与 unit 的变量来源，或明确移除/禁用历史入口；至少增加 shellcheck 和路径契约检查。

### P1：归档文件清理与数据库删除缺少可恢复的一致性协议

直接删除小说/用户的路径 `webapp.py:1334-1353` 先调用 `_remove_archive_files()`，再删除数据库记录；`storage_files.py:149-177` 使用不可恢复的目录删除。如果 SQLite 被锁、外键约束失败或进程在两步之间退出，文件已经丢失而数据库记录仍在。相反，pending deletion 确认路径 `webapp.py:1451-1477` 先提交数据库删除再清理文件，清理失败时会留下失去数据库索引的归档。两种顺序都没有跨文件系统和 SQLite 的恢复协议。

建议先在事务中写删除 tombstone/状态并提交，再异步清理文件；或者把文件移动到同一存储根下的可恢复 trash，数据库失败时可重试或恢复。

### P1：pending deletion restore 不是原子操作

`webapp.py:1489` 先提交 `restore_pending_deletion()`，随后 `:1495-1503` 才恢复 source/series 关系。后续数据库异常会返回 500，但 pending 已经不可再次恢复，远端来源仍未恢复。

建议把 pending 状态、source/series 恢复放入同一事务，或者引入明确的 `restoring` 中间态和幂等重试。

## 中优先级与需求缺口

- `settings.py:446` 的 `_simple_cron_next_run` 注解引用未定义的 `datetime`；`typing.get_type_hints()` 可稳定复现 `NameError`。当前安装 croniter 时不影响主路径，但应修正为模块级导入或字符串可解析类型。
- 偏好分析没有实现收藏/追更/作者/标签/时间/失效等范围选择（`preferences.py:70-82`、`jobs/tasks.py:322-343`、`dashboard_preferences.html:310-314`）。
- 偏好推荐需求声明的 `/profiles/analyze/stream`、`/search-plan/stream`、`/run/stream`、run detail 和单项立即同步端点在源码和测试中没有对应实现；当前 `preference_web.py` 只有非流式分析/计划/run 和 runs 列表。
- 成人请求解析并校验 `preference_profile_id`/`preference_injection_strength`，随后写入 job input（`adult.py:1934-1935`），但服务端没有加载画像内容或注入 Prompt；阅读页也不发送字段（`dashboard_ai_reader.html:369-391`）。这是未接通的参数，不是已实现的偏好接入。
- AI 偏好总结与推荐 AI 解释尚未实现；当前 AI 只清洗关键词，画像和推荐理由仍是本地规则。README 的“AI 用于补充总结和解释”应降级为当前能力说明。
- 搜索计划没有独立编辑/保存 CRUD；反馈闭环缺少屏蔽标签、待阅读/待同步和立即同步入口。

## 需求覆盖结论

| 领域 | 状态 |
|---|---|
| Pixiv 同步/归档/阅读/EPUB | 基础能力已实现；分页上限和运行中日志回收存在 P0/P1 风险 |
| 统一任务/取消/调度 | 共享 JobRunner 已实现；推荐取消终态错误 |
| 救援目录/API/userscript | 基础已实现；完整纠错/删除/性能验收仍为 PARTIAL |
| 偏好本地统计 | PARTIAL：基础增量统计存在，范围选择缺失 |
| AI 偏好总结 | MISSING：当前仅关键词清洗 |
| 搜索计划/反馈 | PARTIAL：可生成和部分反馈，缺独立计划与多种反馈动作 |
| AI 工作台 | PARTIAL：项目、章节和 Pipeline 存在，持续产品化中 |
| 模型目录/池/统一路由 | 已实现，测试覆盖较完整 |
| 成人局部润色 | 核心流程已实现；取消实时性、progress 流式和偏好注入缺失 |
| 响应式/可访问性与生产部署 | 无法仅凭仓库验证 |

## 死代码与实现改进候选

- `web/managers.py:391-499` 的 `SyncJobManager/SyncJobState` 主要作为 `webapp.py` 和测试的兼容重导出，生产没有实例化证据；删除前需先决定是否保留公共导入兼容。
- `oauth_helper.py:5`、`preferences.py:5`、`preference_web.py:3,9`、`recommendations.py:4`、`storage_db.py` 多项未使用导入可清理，但不应把兼容导出误删。
- 任务日志 stale 回收、分页游标保护、推荐发布事务和取消回调可抽成共享小工具，避免各模块重复实现不完整的循环/终态逻辑。

## 文档治理

本轮新增 [ADULT_POLISH_USER_GUIDE.md](ADULT_POLISH_USER_GUIDE.md) 作为用户级操作与排障入口。README/API 契约已经提供快速说明和开发者字段，但不能替代该指南。

文档状态漂移已在索引和统一需求中修正：成人设计与 Logo 设计不再标记为“等待实施”，已完成能力与当前缺口分开列出；生产 Cloudflare 状态仍标记为待服务器核验。

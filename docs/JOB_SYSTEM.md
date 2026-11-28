# Job System 开发者文档

定时用户备份传播 `truncated/incomplete/aborted_reason`，单本获取失败也显示部分完成，正常轮转标记不能掩盖失败；取消不计失败用户。归档暂存在搬移前登记；恢复冲突或回滚失败保留已有副本，带 `.recovery-required` 标记的目录不被启动清扫删除。人工恢复应核对原路径与暂存路径，不要直接清空 `.trash`。最新验证见 [项目现状](STATUS.md)。

> 本文描述同步/后台任务(非 AI 生成)的统一 Job 管线。以源码为准:
> `src/pixiv_novel_sync/jobs/{models,manager,runner,tasks}.py`、
> `src/pixiv_novel_sync/web/managers.py`、`src/pixiv_novel_sync/webapp.py`、
> `src/pixiv_novel_sync/settings.py`。

## 1. 核心管线与状态机

### 1.1 数据流

```
JobSpec (source, job_type, task_types, params)
    │  JobManager.submit()        # 生成 uuid job_id,创建 JobState(QUEUED)
    ▼
JobState (status/message/progress/stats/logs/error/时间戳)
    │  JobRunner.run(job_id)      # 获取运行槽位,逐个 task_type 执行
    ▼
executor(task_type, context)     # 即 jobs/tasks.py:execute_task
    │  按字符串 task_type 分派到具体任务实现
    ▼
task_stats dict → merge_stats() 累加进 JobState.stats
```

关键类型(`jobs/models.py`,均为 `slots=True` dataclass 或 `str` Enum):

- `JobSource`:`WEB` / `CLI` / `SCHEDULER` / `SYSTEMD` — 触发来源标记。
- `JobType`:`SYNC` / `STATUS_CHECK` / `PENDING_DELETION_DETECTION` / `USER_BACKUP` / `PREFERENCE_ANALYZE` / `RECOMMENDATION_RUN` / `WORK_ANALYSIS` / `PREFERENCE_SUMMARIZE`。
- `JobSpec`:一次提交的静态描述;`task_types` 是**普通字符串列表**(如 `"bookmark"`、`"user_backup:12345"`),不是枚举。
- `JobState`:运行时状态,内含 `logs`(`JobLogEntry` 列表,上限 `max_logs=500`)、`progress`、`stats`。

### 1.2 JobStatus 状态机

```
QUEUED ──mark_running──▶ RUNNING ──finalization──▶ SUCCEEDED
   │                        │  │
   │                        │  └─异常─▶ FAILED
   │◀──request_cancel 可作用于 QUEUED/RUNNING──┐
   └──▶ CANCEL_REQUESTED ──runner 察觉──▶ CANCELLED
```

- 终态集合 `_TERMINAL_STATUSES = {SUCCEEDED, FAILED, CANCELLED}`;终态后 `request_cancel` 返回 False。
- `mark_running` 在状态已是 `CANCEL_REQUESTED` 时返回 False,runner 据此直接 `mark_cancelled`(取消先于启动的窗口)。
- **Finalization 协议**:任务收尾通过 `try_begin_finalization(job_id)` 领取一次性 claim(token 校验,防重入),`claim.finish(task_stats, is_last_task=...)` 在锁内合并 stats 并在最后一个任务时置为 `SUCCEEDED`。一旦某个 job 进入 finalization,`request_cancel` 也会拒绝——保证"要么完整收尾,要么取消",不会两者交错。
- `JobRunner.run` 的每轮循环:先查 `is_cancel_requested` → 执行 executor → 若任务内部没主动 claim 则由 runner 补 claim → claim 失败则合并已得 stats,已请求取消时 `mark_cancelled`,否则 `mark_failed("finalization claim lost")`。`InterruptedError` 映射为 CANCELLED,其他异常映射为 FAILED(错误写入 job log)。

### 1.3 并发约束(JobManager)

- `JobManager` 持有 `BoundedSemaphore(1)`:`acquire_run_slot()` 非阻塞获取,失败时 runner 直接 `mark_failed(job_id, "已有任务正在运行")`。即**同一个 JobManager 实例内全局串行**。
- Job 历史保留在内存 `OrderedDict`(上限 `max_jobs=50`),`cleanup_old_jobs` 只清理终态任务。
- `merge_task_stats` 在锁内合并,避免 worker 线程写 stats 与 Flask 请求线程序列化 stats 并发冲突。

## 2. 三个触发源

同一套 `JobManager + JobRunner + execute_task`,三种入口:

### 2.1 CLI(`cli.py`)

`pixiv-novel-sync sync ...` 等子命令通过 `build_job_spec_from_args` 构造 `JobSpec(source=JobSource.CLI, ...)`,进程内新建独立的 `JobManager` + `JobRunner`(executor 为 `lambda: execute_task(task_type, settings, context)`),同步跑完退出。CLI 进程与 web 进程**不共享**管理器,互相没有单活跃约束。

### 2.2 Web(`webapp.py:create_app`)

- app 级共享一个 `shared_job_manager`(`app.config["job_manager"]`)与 `shared_job_runner`(executor 为 `run_web_task`,每次执行前用 `SettingsManager` 重新加载 settings)。
- 提交入口 `_submit_shared_job`(暴露为 `app.config["submit_shared_web_job"]`):
  1. **单活跃任务约束**:调用 `_has_active_shared_jobs` 检查共享管理器中是否存在 `QUEUED/RUNNING/CANCEL_REQUESTED` 的 job，有则抛 `RuntimeError("已有同步任务正在运行…")`。这是提交时的软约束，配合 JobManager 信号量。
  2. 写入 **task_logs 镜像**:先在锁外 `db.create_task_log(task_type, task_name, is_auto_sync=...)`(此时还没有 job_id),再次检查单活跃约束后 `submit`,把返回的 `log_id` 存进 `job.progress["log_id"]`,并把 `job_id` 回填到该行(回填失败会把已入队的 job 标成 failed)。
  3. `run_async=True` 时起 **daemon 线程**执行 `_run_shared_web_job(job_id)`。
- `_run_shared_web_job`:调用 `shared_job_runner.run(job_id)`,结束后按最终 `JobStatus` 把 stats/logs/error 回写到 `task_logs` 表(`db.update_task_log`)——这就是「任务日志」页的数据来源。内存 JobState 是实时视图,task_logs 是持久化镜像。

### 2.3 Scheduler(`web/managers.py:AutoSyncScheduler`)

2026-11-12 的 `WORK_ANALYSIS` 与 `PREFERENCE_SUMMARIZE` 是 Web 业务任务，由持久业务意图进入同一个共享槽，优先级为4；同步仍优先。全库自动发现只读取有界元数据并准备原作品父任务，不增加生成队列或 CLI 执行器。画像通用 JobSpec 只携带 run_id，结果日志只含计数/ID；个人画像保存在独立业务表。

自动作品策略与普通同步总开关独立：策略开启时关闭自动同步仍保留原调度线程，循环不提交普通同步任务。进程重启暂停作品/批次、停止遗留画像并保留预算，不自动重发付费调用。等待画像取消后通过窄完成回调立即同步业务终态，即使没有下一次调度 tick 也不会残留 queued。

- `create_app` 里按 `_scheduler_registry_key(db_path)` 的模块级注册表 + Werkzeug reloader 检测(`WERKZEUG_RUN_MAIN`)保证只启动一份。
- 调度器持有回调:`submit_task=_submit_scheduler_task`(内部走 `_submit_shared_job(..., is_auto_sync=True, run_async=False)`)、`run_task=_run_shared_web_job`、`cancel_task=shared_job_manager.request_cancel`——即定时任务与手动 web 任务**共用同一个 JobManager**,天然互斥。
- 主循环(`_run_scheduler_loop`,空闲时每 30s 醒一次):每轮重新 `load_settings`;`auto_sync_enabled=False` 则空转;为新任务补 `_task_next_run`,然后 `_collect_due_tasks` 收集所有到点任务、按 `(priority, 逾期最久)` 排序、**只提交第一个**(详见 3.6)。可让位的长任务跑在独立线程上,主线程同时轮询是否有更高优先级任务到点。
- 附带职责:每小时按 `sync.task_log_retention_days`（默认 14 天）清理 `task_logs`、AI jobs 和模型同步 operation。调度器显式传入配置天数；直接调用清理函数而不传参数时，函数默认仍是 3 天。首轮尝试初始化救援目录。
- `stop()` 会取消当前定时任务并停线程;lifecycle claim/release 回调防止多 owner 竞争。

## 3. 协作式取消协议

取消是**协作式**的,没有强杀线程:

1. 外部调用 `JobManager.request_cancel(job_id)` → 状态置 `CANCEL_REQUESTED`(终态或已进入 finalization 则拒绝)。手动任务走 `POST /api/dashboard/sync/cancel`（可带 `job_id`，缺省取最新任务）；定时任务走 `POST /api/dashboard/auto-sync/stop-task` 或 `scheduler.stop`。
2. 任务侧感知有三层:
   - `JobRunner` 在每个 task_type 开始前检查 `is_cancel_requested`;
   - `execute_task` 通过 `_stop_requested_from_context(context)` 构造 `stop_requested()` 闭包(内部即 `manager.is_cancel_requested(job_id)`),传给各任务实现;长任务在批次边界 / progress 回调里轮询,发现取消就抛 `InterruptedError("Task stopped by user")`;
   - 收尾前的 `claim_finalization()` 失败也等价于取消。
3. `JobRunner` 捕获 `InterruptedError` → `mark_cancelled`;已合并的部分 stats 保留。

因此新任务实现**必须**定期调用 `stop_requested()`,否则无法被取消(manager/job_id 缺失时该闭包恒返回 False,CLI 裸调用也能安全运行)。

## 3.5 partial 终态、熔断与分批轮转

`JobStatus` 只有 `SUCCEEDED/FAILED/CANCELLED` 三个终态,但**持久化到 `task_logs` 的状态多一个 `partial`**。两者不是一一对应:

- `webapp.py:_task_log_status_for_stats(stats)` 在 job 成功收尾时判定写入 `task_logs` 的状态。stats 里带 `aborted_reason`(熔断中止)或 `truncated`(分页触顶)的,一律记 `partial`;单独出现的 `incomplete`(如订阅系列中止)也记 `partial`,唯一例外是同时带 `rotation_pending`(关注作者按轮转只覆盖部分、撞到单作者配额——跑不完是设计如此),这种轮次记 `succeeded`。前端「任务日志」页把 `partial` 显示为黄色「部分完成」并展示中止原因。
- **`remaining` / `users_remaining` 不作为判定依据**——分批轮转任务每轮本来就有剩余,否则每轮都会变成 partial。

这个设计来自一次生产事故:状态检查被限流熔断,只检查了 30/800 篇就中止,任务日志却仍是绿色「成功」,看不出这轮几乎什么都没查。

### 状态检查的双熔断(`jobs/services.py`)

`novel_status` / `series_status` / `user_status` 共用一个检查循环,两个计数器各自独立(状态不属于该类即清零):

| 常量 | 值 | 触发条件 | `aborted_reason` |
|---|---|---|---|
| `MAX_CONSECUTIVE_UNKNOWN` | 15 | 连续 15 次状态无法判定,认定被限流 | `rate_limited` |
| `MAX_CONSECUTIVE_MISSING` | 30 | 连续 30 次**新出现**的删除(`MISSING_STATUSES = {deleted, suspended}`),疑似限流伪装成「不存在」 | `suspicious_missing_streak` |

熔断时写 error 级日志、置 `stopped=True` 并 break,未检查的条目**保持原状态**。熔断中止还会跳过救援目录重建。`unknown` 不覆盖已有状态,只刷新 `last_checked_at` 以推进轮转。

两条去噪规则(2026-08-27 生产实测后加入,都在 `_process_status_items`):

- **已知删除不计入 missing streak**。`build_already_missing` 在每轮开始时用 `db.get_known_missing_novel_ids()` 取一次快照;命中的条目只累加 `stats["confirmed_missing"]`,不进 `consecutive_missing`。Pixiv 再次确认一篇本来就是 `deleted` 的作品是一致结论,不是限流证据。实测两次误判都发生在 `11911679`–`11961577` 这段 2010 年连号老作品上——它们确实全被删且已入库。已知删除**不清零**新删除的连续计数,否则"每 29 个新删除插一个已知删除"就能永久绕过熔断。
- **`MAX_CONSECUTIVE_UNKNOWN` 从 5 放宽到 15**。实测 `user_status` 每轮 193/298 个用户里只有 6 个 unknown,但其中 5 个恰好连续,于是每一轮都在同一处熔断。真限流的形态是"此后全部 unknown"(历史事故 290/290),15 连续足以捕获。

### 轮转顺序必须按 `last_checked_at`

三个状态检查任务的候选清单都要按 `last_checked_at` 升序(NULL 最前),这样熔断跳过的队尾下一轮自然排到最前面:

| 任务 | 清单方法 |
|---|---|
| `novel_status` | `db.get_novel_ids_for_status_check(limit=batch_size)` |
| `user_status` | `db.get_users_for_status_check()` |
| `series_status` | `db.get_series_ids_for_status_check()` |

生产事故:`user_status` 曾复用列表页的 `db.list_users()`(排序是 `status 分桶 + updated_at DESC`,与检查时间无关),顺序每轮固定,熔断一次就把队尾永久饿死——实测 105/298 个用户超过 3 天从未被检查。**不要用列表页的查询方法喂状态检查。**

### 订阅系列的死系列判定(`sync_engine.py`)

`sync_subscribed_series` 的 `max_consecutive_fetch_failures = 5` 熔断必须先分辨"Pixiv 明确说这个系列没了"和"拿不到详情,原因不明":`_classify_series_response` 复用 `web.utils._classify_pixiv_response` 的三态判定(同一份关键词/限流词表),`missing` 记为死系列——写回 `series.status='deleted'`、累加 `stats["series_deleted"]`、**重置**连续失败计数(一个连贯的"已删除"响应恰恰证明 API 还活着),`unknown` 才计入熔断。

生产事故:watchlist 里长期躺着 24 个已删系列(`944540`、`944510`、`7640606`、`16201537`…),队尾有连续 5 个以上,导致 11/11 轮追更同步都在 `series_processed: 213/214` 处被误判成 `rate_limited` 中止,队尾的真系列永远同步不到。

### 分批轮转与分页上限

- `NOVEL_STATUS_BATCH_SIZE = 800`:`novel_status` 每轮按 `last_checked_at` 最久优先取一批,不再单次占满整个周期把其他任务挤后。已确认 `deleted`、最近 `KNOWN_DELETED_RECHECK_DAYS = 7` 天内复查过的作品取批时就排除(名额让给后面的作品,stats 记 `deleted_recheck_deferred`),超过 7 天照常复查;待删检测的收藏候选同样跳过这些作品、不再逐本 `novel_detail`(stats 记 `skipped_known_deleted`)。`confirmed_missing` 与「已知删除不清零 missing 连击」不变。
- `FOLLOWING_LIST_MAX_PAGES = 50`(`sync_engine.py`):关注列表枚举与作品分页上限解耦,避免 `max_pages_per_run` 把候选集截断。
- `sync.bookmark_max_pages_per_run`:**收藏列表专用**翻页上限,留空则回落到 `max_pages_per_run`。`max_pages_per_run` 的语义是"关注作者作品列表 / 系列章节的单轮翻页上限",生产配成 2 是为了压体量;收藏复用同一个值会让每轮只看最新约 60 条就标 `truncated/incomplete`,历史收藏永远补不齐——而收藏是优先级最高的数据。解析在 `sync_engine._resolve_bookmark_max_pages`。
- `sync.max_items_per_run` 在收藏同步里只计**真正发了详情请求、能归档**的条目:已归档跳过的不计,`content_unavailable`(作品已删或不可见,永远归档不了)也不计,但照常按 `delay_seconds_between_items` 间隔。预算只在碰到下一本**未归档**的收藏时才判定触顶,此时置 `truncated` / `incomplete` / `budget_exhausted`(日志黄色);剩下的全是已归档时不算截断。以前失效收藏占预算,攒够上限后排在后面的未归档收藏永远轮不到,而且触顶静默、日志一直绿色(审查 S8)。
- `following_list` 成功遍历全部页后，按账号和 public/private 分别发布完整关注名单。新作同步和备份复用 36 小时内快照；冷启动或过期时刷新。不完整名单不覆盖旧快照，账号状态巡检没有完整成员名单时明确跳过，不退回整个 `users` 表。
- 定时 `following_novels` 使用公开/私密新作流；每个范围每轮 3–20 页（根据全局页预算），先持久保存发现 ID，再按 `max_items_per_run` 下载，未设时为 60 篇。最新作品优先，已完整归档的作品不请求正文；分页失败不丢下载积压。续跑会先核对新头部与旧锚点，最终追平才推进完成水位。`feed_pages`、`feed_pending`、`feed_backlog` 分别表示列表请求、待处理条目及分页积压；手动调用继续使用作者轮转。
- 定时 `user_backup` 每作者每次最多读取 2 页，整页成功才推进 offset。游标 v3 保留整个遍历的有序 ID 摘要，首页新增不再使续页反复回到头部。跨页作者第一次翻到底后继续一轮对账，只有连续两次完整遍历摘要一致才发布 `history_verified`；单页完整响应可以直接核验。`pages_read` 是实际列表请求数，兼容字段 `boundary_pages` 为 0。旧版本水位不能沿用为完整性证明，失败或取消不发布本轮完成标记。
- `history_enumerated` 表示本次翻到末页，`history_verified` 表示目录核验通过，`history_complete` 还要求没有不可获取作品。待对账时返回 `history_verification_pending` / `more_pages`，不会进入 30 天免扫描集合。已核验作者 30 天后再对账；仍不是 Pixiv 原子快照，持续变化的目录可保持待核验而不阻止历史向后推进。每批旧正文复查最多 10 篇，缺图只补图片。批次给续页留名额并轮转其他作者，整批共用登录。手动 `user-backup <id>` 保留每 7 天允许全量重校验的语义。
- 收藏从公开改为私密或反向切换时，同步迁移已确认属于该作品的目录、附件路径和收藏来源。已有冲突文件或不明路径时保留原件、报告 `archive_scope_error`，继续处理同页其他作品且不发布该页完成水位；不要手工覆盖冲突文件。中断后在下次观察到该收藏时恢复。启用的 TXT/Markdown 缺失或为零字节且库内有正文时，本地补写。
- 收藏同步和取消检测共用分页校验：列表必须存在且 ID 有效，下一页必须属于同一账号和公开/私密范围且游标前进。错误响应不当作合法空末页，取消检测在所有页可信前不修改待确认记录。已归档作品新增来源时同步刷新受影响的救援目录来源，不做全库重建。
- 收藏和关注新作的列表、详情或正文遇到明确限流时，本轮停止，标记 `rate_limited`；不继续换范围请求，未下载的 ID 保留，不记为作品失败。作者备份遇到明确限流不推进未完成页，也不继续处理下一位作者。
- OAuth 首登和续期对 408/429/500/502/503/504、连接失败和超时最多尝试 3 次，等待 2 秒、4 秒且支持取消。明确凭据失效及其他错误不盲目重试；任务日志只保留短原因与状态码，不保存上游 HTML 或完整异常。共享客户端最多发起一次续期，仍校验返回账号。网关故障 `auth_unavailable` 不等于 Token 失效。
- 明确 OAuth 失败时共享客户端最多续期一次，验证账号后重试当前请求；再次失败按 `auth_failed` 结束。追更旧正文每轮最多复查 10 篇，新增、正文变化、仅元数据变化、补图分别统计。首次入库前失败的 ID 也持久退避。系列状态巡检最多 300 项，避免系列元数据迁移扩大单轮请求量。
- `sync.following_max_novels_per_author`:**单个关注作者**每轮的同步上限,留空 = 不限。`max_items_per_run` 只在切换作者**之前**检查,单个作者内部没有任何上限,于是第一个高产作者就把整轮预算吃光——生产实测 256 个关注作者每轮只覆盖 1 个,轮完一圈要 95 天。撞到配额是跳到下一个作者,不是结束整轮。解析在 `sync_engine._resolve_following_author_quota`;整轮硬顶(`_resolve_following_run_cap`,取 配额×users_limit×1.5)只兜异常。
- `sync.series_max_pages_per_run`:**系列章节专用**翻页上限,留空则回落到 `max_pages_per_run`。系列章节数远超单个作者的单轮作品体量,共用那个上限会把长系列永久截断,而且每轮都从 watchlist 头部重来、永远在同一页触顶:生产实测每轮 `truncated_series=2`,8 个订阅系列长期缺 76 章,高频重跑也补不齐。解析在 `sync_engine._resolve_series_max_pages`。
- 关注作者同步按「最久未同步」优先轮转,watermark 兼容旧格式。`user_last_synced` 只在作者扫描**自然结束**时写(含配额、翻页上限、连续已存在早停);作品列表拉取失败记进同一水位的 `user_fail_streak`,连续 `FOLLOWING_AUTHOR_MAX_FAIL_STREAK = 3` 次照常写入(防止坏作者霸占队首),成功一次清零;被让位或取消的作者不写也不计数。以前扫描一开始就写,出错或被打断的作者也推后整圈。
- 调度器从 `task_logs` 恢复上次完成时间,重启不再把所有任务推迟一整个周期;逾期任务错峰补偿。cron 任务同样补偿:`settings.cron_to_prev_run` 求最近一个已过去的槽,上次跑完(`succeeded` / `partial`)早于它就补跑一次(只补一次,不追补每个错过的槽),否则照常等下一个槽;没有历史(首次部署)不补。以前 cron 任务完全不恢复,被重启或 `./update.sh` 打断的那一轮直接丢掉,要等下一个槽。

## 3.6 任务优先级与让位(`web/managers.py`)

所有定时任务共用一个 job 槽(`BoundedSemaphore(1)`),所以"谁先抢到槽"必须是显式规则而不是声明顺序的巧合。

每个 `SCHEDULER_TASK_CONFIGS` 条目带两个字段:

| 字段 | 含义 |
|---|---|
| `priority` | 1 最重要。用户口径:收藏 = P1,追更系列 = P2,其余 = P3 |
| `preemptible` | 正在跑的它能否为更高优先级任务让位 |

**挑选规则**(`_collect_due_tasks`):每轮收集所有已到点任务,按 `(priority, 逾期最久)` 升序排序,**只提交第一个**;跑完立刻回到循环顶部重新挑选,不再等满 30s。旧行为是按数组顺序扫描,"收藏优先"只是 `bookmarks` 恰好排在数组第一位。

**退避分级**(`scheduler_retry_seconds`):槽被占用导致 submit 失败时,P1 退避 60s、P2 退避 120s、P3 沿用 `SCHEDULER_SUBMIT_RETRY_SECONDS = 300s`。

**让位**(`_run_and_watch_for_preemption`):可让位任务不再同步阻塞调度线程,而是丢到 `auto-sync-<task>` 线程跑,主线程每 `SCHEDULER_PREEMPT_POLL_SECONDS = 5s` 轮询"有没有更高优先级的任务到点"。命中且护栏允许时,`_request_yield` 先往 job 日志里写一条 warning 说明原因(`_run_shared_web_job` 在任务终结后才把内存日志刷进 `task_logs`,所以这条说明会跟着落库,运维不会看到一条无缘无故的"已取消"),再调 `cancel_task`。被让位的任务走的是既有协作式取消路径,`task_logs` 记 `cancelled`。

`preemptible=True` 只给可恢复的任务：关注作品、三个状态检查、作者备份、偏好增量分析。追更仍不让位，按专用 `subscribed_series_visit` 的访问时间排序；它表示尝试过，不代表章节完整。状态检查继续使用 `series.last_checked_at`，两者互不推进。推荐生成仍保持原子发布。

**护栏**(`_may_preempt`),防止 P1 频繁到点把长任务永久饿死:

| 常量 | 值 | 作用 |
|---|---|---|
| `SCHEDULER_PREEMPT_COOLDOWN_SECONDS` | 6h | 刚让过位的任务在冷却期内必须能跑完 |
| `SCHEDULER_MAX_CONSECUTIVE_PREEMPTIONS` | 2 | 连续被让位到上限后强制跑完一整轮 |
| `SCHEDULER_PREEMPT_RETRY_SECONDS` | 600s | 让位后短退避续跑,而不是顺延整个周期(否则一让位等于跳过一轮) |

让位标记是一次性的(`_consume_preemption_flag`),且只在 `submitted=True` 时消费——否则"反复提交失败"会悄悄把连续让位计数清零绕过护栏。任务完整跑完一轮时同一个方法负责清零计数。

**两道护栏的关系(审查 2026-10-28)**:让位后的续跑排在 600 s 后(`SCHEDULER_PREEMPT_RETRY_SECONDS`),必然落在 6 小时冷却期(`SCHEDULER_PREEMPT_COOLDOWN_SECONDS`)之内,所以续跑那一轮 `_may_preempt` 一定返回 False、一定能跑完,跑完即把连续让位计数清零。日常起作用的是冷却期;`SCHEDULER_MAX_CONSECUTIVE_PREEMPTIONS = 2` 只在续跑开始时冷却期已经过去才可能被用到:续跑那一轮本身跑了接近 6 小时以上,或者槽一直被占、续跑拖到 6 小时后才开始。两者都保留,行为不变;改这三个常量时要一起看,`tests/test_preemption_guardrails.py` 钉住了「续跑落在冷却期内」这个前提。

## 4. 新增 task_type 的步骤

以字符串 `"my_task"` 为例:

1. **实现任务函数**:签名接收 `settings` 与从 context 取出的 `reporter` / `stop_requested`(必要时 `claim_finalization`、`params`),返回 stats dict。放在 `jobs/services.py` 或独立模块,遵循函数内延迟 import 约定。
2. **注册分派**:在 `jobs/tasks.py:execute_task` 中加一个 `if task_type == "my_task":` 分支(未注册的 task_type 会抛 `RuntimeError("Unsupported task type for CLI execution: ...")`)。
3. **注册元数据**:在 `jobs/registry.py:TASKS` 加 `TaskDefinition`，统一提供任务/调度标签、别名、优先级及可让位属性；`jobs/tasks.py` 与 `web/managers.py` 消费其生成的兼容字典。
4. **接入触发源**(按需):
   - CLI:在 `cli.py` 的参数解析 / `build_job_spec_from_args` 中允许该 task_type;
   - Web:在 `webapp.py` 相应路由里用 `_submit_shared_job` 提交;
   - Scheduler:在上述 `TaskDefinition` 填写 `scheduler_name`、`setting_prefix`、`priority` 与 `preemptible`，并在 `SyncSettings` / `SettingsManager.save_sync_settings` 补 `auto_sync_my_task_{enabled,interval_hours,cron}` 三件套。`preemptible=True` 只给能从水位续跑的任务。别名表由注册表生成，`web/utils.py:_scheduler_job_spec` 应与其保持相同映射。
   - 加完后跑 `pytest tests/test_scheduler_priority.py -k registered`:那里的 `EXPECTED_SCHEDULER_TASKS` 会先红,提示上面 2/3 步和 `_job_spec` 分支是否真的都补了。
5. **同步前端契约**:更新 `docs/frontend-api-contract.md`(以及涉及页面时的 `docs/frontend-pages.md`),让前端知道新的 task_type 字符串与标签;若响应结构变化,同步模板中的 Vue 代码。
6. **测试**:参照 `tests/test_jobs_runner.py` 等,依赖 conftest 的 tmp 路径 fixture;验证正常完成、取消(`stop_requested` 生效)、stats 合并。

## 5. auto_sync 配置矩阵(interval-hours vs cron)

2026-10-08：当前 main 已移除 `_simple_cron_next_run` 简化 cron 解析器；缺少 croniter 或解析异常时 `cron_to_next_run` 返回 None。调度器原有的 interval 回落仍保留，不能把“移除简单解析器”理解为取消 interval 调度。

规则(见 `web/managers.py:_run_scheduler_loop` 与 `settings.py:cron_to_next_run`):

- 全局开关 `auto_sync_enabled`(默认 False)+ 每任务 `*_enabled` 同时为真才调度。
- 每个任务同时有 `*_interval_hours` 与 `*_cron` 两个旋钮;**cron 非空则优先**,`cron_to_next_run(cron, now, auto_sync_timezone)` 计算下次时间(解析失败回落到 interval)。cron 在 `auto_sync_timezone`(默认 `"UTC"`)时区求值;`SettingsManager.save_sync_settings` 保存时会校验 cron 合法性。
- env 变量始终覆盖 YAML(`load_settings` 语义)。

`SyncSettings` 中各任务的**代码默认值**(priority / preemptible 见 3.6;生产实际排布见 5.2,与这里的默认值不同):

| 任务(scheduler name) | P | 可让位 | enabled 默认 | interval_hours 默认 | cron 默认 | 附加字段 |
|---|---|---|---|---|---|---|
| bookmarks | 1 | ✗ | True | 6 | "" | `bookmark_max_pages_per_run`(留空跟随 `max_pages_per_run`) |
| subscribed_series | 2 | ✗ | True | 6 | "" | `series_max_pages_per_run`(留空跟随 `max_pages_per_run`) |
| following_list | 3 | ✗ | True | 24 | "" | |
| following_novels | 3 | ✓ | True | 6 | "" | 新作流页预算与 `max_items_per_run`；作者人数/单作者配额仅用于手动扫描 |
| user_status | 3 | ✓ | True | 6 | "" | 已知受限用户按 `users.restricted_streak` 降频(≥3 轮判不出状态 → 每 7 天才巡检一次) |
| novel_status | 3 | ✓ | True | 6 | "" | 批大小是代码常量 `NOVEL_STATUS_BATCH_SIZE`（800），不是设置页字段 |
| series_status | 3 | ✓ | True | 6 | "" | |
| user_backup | 3 | ✓ | False | 24 | "" | `user_backup_users_per_run` 默认 5；手动分页兜底 200，定时每人最多 2 页并续跑 |
| pending_deletion_detection(设置字段名为 `auto_sync_pending_detection_*`) | 3 | ✗ | True | 12 | "" | |
| preference_analyze | 3 | ✓ | False | 12 | `"15 7,19 * * *"` | `preference_analyze_batch_size`(默认 200);scheduler 提交时强制 `max_batches=1` |
| recommendation_run | 3 | ✗ | False | 24 | `"0 11 * * *"` | 待阅读推荐达到 200 时自动跳过搜索，手动不受限；生成需有默认画像 |

表格顺序即 `SCHEDULER_TASK_CONFIGS` 的声明顺序(已按优先级排列),该顺序同时决定重启后逾期任务的错峰补偿次序。

注意:`preference_analyze` 与 `recommendation_run` 是唯二自带默认 cron 的任务(其余默认走 interval),因为这两个都会持续占用那个唯一的 job 槽,却都不是"越勤越好":前者是纯本地计算、生产从未真正跑过,每 30 分钟一次只会挤同步任务;后者每轮最多发 20 条 Pixiv 检索,是唯一额外吃搜索配额的任务。改这两个默认值时,`settings.py` 的 dataclass 默认、`load_settings` 的回落值、`SettingsManager.save_sync_settings` 的 `_save_cron`/`_save_int` 回落值**三处**要一起改——只改前两处的话,"什么都没改就点保存"会把 cron 写成空串,任务悄悄退回按 interval 跑(`tests/test_cron_validation.py` 有防漂断言)。

`preference_analyze` 手动触发(web 按钮)默认 `max_batches=10`(≈2000 篇),定时触发每轮只跑 1 批。

`recommendation_run` 默认关闭:它会消耗 Pixiv 搜索配额,且必须先有默认偏好画像。启用后仍与其他同步任务共用同一个 JobManager,天然互斥,不会和收藏同步并行抢配额。手动入口 `POST /api/dashboard/recommendations/run` 保持不变。

### 5.1 任务元数据统一来源

`jobs/registry.py:TASKS` 同时生成 `JOB_LABELS`、`TASK_LABELS`、`SCHEDULER_TASK_CONFIGS` 和 `SCHEDULER_TASK_TYPE_ALIASES`。原调用位置继续导入这些值，新增任务只在注册表声明标签与调度属性，避免手动与定时日志显示不一致。分派实现、设置字段及 `JobType` 映射仍按 §4 补齐，并运行注册完整性回归。

同理,`web/utils.py:_job_spec` 里若没有对应 task_type 的分支,JobSpec 会静默落到 `JobType.SYNC`,任务统计归错类。这一条以及"四处注册表是否齐全"由 `tests/test_scheduler_priority.py::test_every_scheduler_task_is_registered_in_all_three_tables` / `::test_scheduler_tasks_map_to_their_own_job_type` 锁住:新增定时任务时那里的 `EXPECTED_SCHEDULER_TASKS` 会先红,提示按本文 §4 把四处补齐。

### 5.2 示例 cron 与调度核对

代码默认值与配置示例不同。下面是 `config/config.yaml.example` 的排布，时区 `Asia/Seoul`；实际机器使用的时区、开关和预算必须读取自己的配置，不能由示例推断生产状态。

| 任务 | P | cron | 频率 |
|---|---|---|---|
| bookmarks | 1 | `"20 0,4,8,12,16,20 * * *"` | 6 次/天 |
| subscribed_series | 2 | `"40 1,13 * * *"` | 2 次/天 |
| following_list | 3 | `"30 10 * * *"` | 1 次/天 |
| following_novels | 3 | `"0 3,9,15,21 * * *"` | 4 次/天 |
| user_status | 3 | `"30 6 */2 * *"` | 按日字段步进 |
| novel_status | 3 | `"0 5,17 * * *"` | 2 次/天 |
| series_status | 3 | `"30 18 */2 * *"` | 按日字段步进 |
| user_backup | 3 | `"30 2 */3 * *"` | 按日字段步进，默认关闭 |
| pending_deletion_detection | 3 | `"30 12 * * *"` | 1 次/天 |
| preference_analyze | 3 | `"15 7,19 * * *"` | 2 次/天，默认关闭 |
| recommendation_run | 3 | `"0 11 * * *"` | 1 次/天，默认关闭 |

`*/2`、`*/3` 是每月日字段步进，不保证跨月保持精确 48/72 小时。单轮耗时取决于书库规模、变化量、请求预算和上游响应，不沿用历史测量数字作保证。

排布约束:

1. P1 收藏每天 6 次均匀分布,任何时刻最多等 4 小时。
2. 长任务(`following_novels`、`novel_status`)避开收藏时刻 ±30 分钟,减少无谓让位。
3. 状态检查轮转覆盖与用户取消收藏/追更检测是不同任务，调整频率时分别核对覆盖周期。
4. 定时 `following_novels` 看新作流是否追平及下载积压；作者历史覆盖另看备份枚举完成记录，不能从单轮成功推断全库完整。
5. `subscribed_series` 按独立访问水位轮转，未知错误保留熔断但不直接判定限流；注册表保持 `preemptible=False`。

设置 API 拒绝非法时区和非空非法 cron；直接编辑 YAML 绕过该校验时，调度计算失败会回落到 interval。改完必须逐条验算下次时刻：

```bash
python -c "
from pixiv_novel_sync.settings import load_settings, cron_to_next_run
from datetime import datetime
s = load_settings('config/config.yaml', None); tz = s.sync.auto_sync_timezone
for attr in sorted(dir(s.sync)):
    if attr.endswith('_cron') and getattr(s.sync, attr):
        e = getattr(s.sync, attr); nxt = cron_to_next_run(e, None, tz)
        assert nxt is not None, attr
        print(f'{attr:48s} {e:26s} -> {datetime.fromtimestamp(nxt)}')
"
```

`tests/test_cron_validation.py` 把这 11 条表达式在 `Asia/Seoul` 下的下次运行时刻与每天触发次数都钉死了,改排布时那里要同步更新。

`selected by priority`、`submit failed`、`yielded` 需结合当前队列和失败原因判断。出现让位日志本身不代表故障，不能用旧部署日志推断现行优先级从未触发。

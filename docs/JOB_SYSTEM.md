# Job System 开发者文档

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
- `JobType`:`SYNC` / `SYNC_CHECK` / `STATUS_CHECK` / `PENDING_DELETION_DETECTION` / `USER_BACKUP` / `PREFERENCE_ANALYZE` / `RECOMMENDATION_RUN`。
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
- `JobRunner.run` 的每轮循环:先查 `is_cancel_requested` → 执行 executor → 若任务内部没主动 claim 则由 runner 补 claim → claim 失败(说明已被取消)则合并已得 stats 并 `mark_cancelled`。`InterruptedError` 映射为 CANCELLED,其他异常映射为 FAILED(错误写入 job log)。

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
  1. **单活跃任务约束**:先调 `_has_any_running_web_job()`(实现为 `_has_active_shared_jobs`,检查共享管理器中是否存在 `QUEUED/RUNNING/CANCEL_REQUESTED` 的 job),有则抛 `RuntimeError("已有同步任务正在运行…")`。注意这是提交时的软约束,配合 JobManager 信号量做双保险。
  2. 写入 **task_logs 镜像**:`db.create_task_log(task_type, task_name, job_id, is_auto_sync)`,并把返回的 `log_id` 存进 `job.progress["log_id"]`。
  3. `run_async=True` 时起 **daemon 线程**执行 `_run_shared_web_job(job_id)`。
- `_run_shared_web_job`:调用 `shared_job_runner.run(job_id)`,结束后按最终 `JobStatus` 把 stats/logs/error 回写到 `task_logs` 表(`db.update_task_log`)——这就是「任务日志」页的数据来源。内存 JobState 是实时视图,task_logs 是持久化镜像。

### 2.3 Scheduler(`web/managers.py:AutoSyncScheduler`)

- `create_app` 里按 `_scheduler_registry_key(db_path)` 的模块级注册表 + Werkzeug reloader 检测(`WERKZEUG_RUN_MAIN`)保证只启动一份。
- 调度器持有回调:`submit_task=_submit_scheduler_task`(内部走 `_submit_shared_job(..., is_auto_sync=True, run_async=False)`)、`run_task=_run_shared_web_job`、`cancel_task=shared_job_manager.request_cancel`——即定时任务与手动 web 任务**共用同一个 JobManager**,天然互斥。
- 主循环(`_run_scheduler_loop`,空闲时每 30s 醒一次):每轮重新 `load_settings`;`auto_sync_enabled=False` 则空转;为新任务补 `_task_next_run`,然后 `_collect_due_tasks` 收集所有到点任务、按 `(priority, 逾期最久)` 排序、**只提交第一个**(详见 3.6)。可让位的长任务跑在独立线程上,主线程同时轮询是否有更高优先级任务到点。
- 附带职责:每小时清理超过 3 天的 `task_logs` 与 AI jobs(`cleanup_old_task_logs(days=3)` / `cleanup_ai_jobs(keep_days=3)`);首轮尝试初始化救援目录。
- `stop()` 会取消当前定时任务并停线程;lifecycle claim/release 回调防止多 owner 竞争。

## 3. 协作式取消协议

取消是**协作式**的,没有强杀线程:

1. 外部(web 停止按钮 / scheduler.stop)调用 `JobManager.request_cancel(job_id)` → 状态置 `CANCEL_REQUESTED`(终态或已进入 finalization 则拒绝)。
2. 任务侧感知有三层:
   - `JobRunner` 在每个 task_type 开始前检查 `is_cancel_requested`;
   - `execute_task` 通过 `_stop_requested_from_context(context)` 构造 `stop_requested()` 闭包(内部即 `manager.is_cancel_requested(job_id)`),传给各任务实现;长任务在批次边界 / progress 回调里轮询,发现取消就抛 `InterruptedError("Task stopped by user")`;
   - 收尾前的 `claim_finalization()` 失败也等价于取消。
3. `JobRunner` 捕获 `InterruptedError` → `mark_cancelled`;已合并的部分 stats 保留。

因此新任务实现**必须**定期调用 `stop_requested()`,否则无法被取消(manager/job_id 缺失时该闭包恒返回 False,CLI 裸调用也能安全运行)。

## 3.5 partial 终态、熔断与分批轮转

`JobStatus` 只有 `SUCCEEDED/FAILED/CANCELLED` 三个终态,但**持久化到 `task_logs` 的状态多一个 `partial`**。两者不是一一对应:

- `webapp.py:_task_log_status_for_stats(stats)` 在 job 成功收尾时判定写入 `task_logs` 的状态。stats 里带 `aborted_reason`(熔断中止)或 `incomplete`(订阅系列中止、关注作者只覆盖部分、分页触顶)的,一律记 `partial`,前端「任务日志」页显示黄色「部分完成」并展示中止原因。
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

- `NOVEL_STATUS_BATCH_SIZE = 800`:`novel_status` 每轮按 `last_checked_at` 最久优先取一批,不再单次占满整个周期把其他任务挤后。
- `FOLLOWING_LIST_MAX_PAGES = 50`(`sync_engine.py`):关注列表枚举与作品分页上限解耦,避免 `max_pages_per_run` 把候选集截断。
- `sync.bookmark_max_pages_per_run`:**收藏列表专用**翻页上限,留空则回落到 `max_pages_per_run`。`max_pages_per_run` 的语义是"关注作者作品列表 / 系列章节的单轮翻页上限",生产配成 2 是为了压体量;收藏复用同一个值会让每轮只看最新约 60 条就标 `truncated/incomplete`,历史收藏永远补不齐——而收藏是优先级最高的数据。解析在 `sync_engine._resolve_bookmark_max_pages`。
- `sync.following_max_novels_per_author`:**单个关注作者**每轮的同步上限,留空 = 不限。`max_items_per_run` 只在切换作者**之前**检查,单个作者内部没有任何上限,于是第一个高产作者就把整轮预算吃光——生产实测 256 个关注作者每轮只覆盖 1 个,轮完一圈要 95 天。撞到配额是跳到下一个作者,不是结束整轮。解析在 `sync_engine._resolve_following_author_quota`;整轮硬顶(`_resolve_following_run_cap`,取 配额×users_limit×1.5)只兜异常。
- `sync.series_max_pages_per_run`:**系列章节专用**翻页上限,留空则回落到 `max_pages_per_run`。系列章节数远超单个作者的单轮作品体量,共用那个上限会把长系列永久截断,而且每轮都从 watchlist 头部重来、永远在同一页触顶:生产实测每轮 `truncated_series=2`,8 个订阅系列长期缺 76 章,高频重跑也补不齐。解析在 `sync_engine._resolve_series_max_pages`。
- 关注作者同步按「最久未同步」优先轮转,watermark 兼容旧格式。
- 调度器从 `task_logs` 恢复上次完成时间,重启不再把所有任务推迟一整个周期;逾期任务错峰补偿。

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

`preemptible=True` 只给「按水位轮转、下轮能接着跑」的任务:`following_novels`(`user_last_synced`)、三个 status 检查(`last_checked_at`)、`user_backup`(`offset`)、`preference_analyze`(累加器)。`subscribed_series` 每轮从 watchlist 头部重走一遍、不是水位式续跑,打断等于白跑;`bookmarks` / `following_list` / `pending_deletion_detection` 本身只跑几十秒到几分钟;`recommendation_run` 跑一半的结果没有意义。

**护栏**(`_may_preempt`),防止 P1 频繁到点把长任务永久饿死:

| 常量 | 值 | 作用 |
|---|---|---|
| `SCHEDULER_PREEMPT_COOLDOWN_SECONDS` | 6h | 刚让过位的任务在冷却期内必须能跑完 |
| `SCHEDULER_MAX_CONSECUTIVE_PREEMPTIONS` | 2 | 连续被让位到上限后强制跑完一整轮 |
| `SCHEDULER_PREEMPT_RETRY_SECONDS` | 600s | 让位后短退避续跑,而不是顺延整个周期(否则一让位等于跳过一轮) |

让位标记是一次性的(`_consume_preemption_flag`),且只在 `submitted=True` 时消费——否则"反复提交失败"会悄悄把连续让位计数清零绕过护栏。任务完整跑完一轮时同一个方法负责清零计数。

## 4. 新增 task_type 的步骤

以字符串 `"my_task"` 为例:

1. **实现任务函数**:签名接收 `settings` 与从 context 取出的 `reporter` / `stop_requested`(必要时 `claim_finalization`、`params`),返回 stats dict。放在 `jobs/services.py` 或独立模块,遵循函数内延迟 import 约定。
2. **注册分派**:在 `jobs/tasks.py:execute_task` 中加一个 `if task_type == "my_task":` 分支(未注册的 task_type 会抛 `RuntimeError("Unsupported task type for CLI execution: ...")`)。
3. **注册标签**:同文件 `_TASK_LABELS` 加中文标签(`task_label()` 用它渲染日志/UI);若要出现在 web 端任务列表/定时任务,`web/managers.py:TASK_LABELS` 也要加(两处是独立字典)。
4. **接入触发源**(按需):
   - CLI:在 `cli.py` 的参数解析 / `build_job_spec_from_args` 中允许该 task_type;
   - Web:在 `webapp.py` 相应路由里用 `_submit_shared_job` 提交;
   - Scheduler:在 `web/managers.py:SCHEDULER_TASK_CONFIGS` 增加条目(**必须带 `priority` 与 `preemptible`**,漏了会静默落到 P3/不可让位,`tests/test_scheduler_priority.py` 有防漏断言),并在 `SyncSettings` / `SettingsManager.save_sync_settings` 补 `auto_sync_my_task_{enabled,interval_hours,cron}` 三件套。`preemptible=True` 只给能从水位续跑的任务。调度器名与内部 task_type 不同名时(如 `bookmarks` → `bookmark`),`web/utils.py:_scheduler_job_spec` 与 `web/managers.py:SCHEDULER_TASK_TYPE_ALIASES` 必须是同一套映射,否则按任务名查不到任何历史记录。
   - 加完后跑 `pytest tests/test_scheduler_priority.py -k registered`:那里的 `EXPECTED_SCHEDULER_TASKS` 会先红,提示上面 2/3 步和 `_job_spec` 分支是否真的都补了。
5. **同步前端契约**:更新 `docs/frontend-api-contract.md`(以及涉及页面时的 `docs/frontend-pages.md`),让前端知道新的 task_type 字符串与标签;若响应结构变化,同步模板中的 Vue 代码。
6. **测试**:参照 `tests/test_jobs_runner.py` 等,依赖 conftest 的 tmp 路径 fixture;验证正常完成、取消(`stop_requested` 生效)、stats 合并。

## 5. auto_sync 配置矩阵(interval-hours vs cron)

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
| following_novels | 3 | ✓ | True | 6 | "" | `auto_sync_following_novels_users_limit`(0=全部)、`following_max_novels_per_author`(留空=不限) |
| user_status | 3 | ✓ | True | 6 | "" | 已知受限用户按 `users.restricted_streak` 降频(≥3 轮判不出状态 → 每 7 天才巡检一次) |
| novel_status | 3 | ✓ | True | 6 | "" | `novel_status_batch_size`(默认 800) |
| series_status | 3 | ✓ | True | 6 | "" | |
| user_backup | 3 | ✓ | False | 24 | "" | 复用 `auto_sync_following_novels_users_limit` |
| pending_deletion_detection(设置字段名为 `auto_sync_pending_detection_*`) | 3 | ✗ | True | 12 | "" | |
| preference_analyze | 3 | ✓ | False | 12 | `"15 7,19 * * *"` | `preference_analyze_batch_size`(默认 200);scheduler 提交时强制 `max_batches=1` |
| recommendation_run | 3 | ✗ | False | 24 | `"50 8 * * *"` | 依赖已存在的默认偏好画像;没有画像时任务抛 `RuntimeError("需要先生成默认偏好画像")` |

表格顺序即 `SCHEDULER_TASK_CONFIGS` 的声明顺序(已按优先级排列),该顺序同时决定重启后逾期任务的错峰补偿次序。

注意:`preference_analyze` 与 `recommendation_run` 是唯二自带默认 cron 的任务(其余默认走 interval),因为这两个都会持续占用那个唯一的 job 槽,却都不是"越勤越好":前者是纯本地计算、生产从未真正跑过,每 30 分钟一次只会挤同步任务;后者每轮最多发 20 条 Pixiv 检索,是唯一额外吃搜索配额的任务。改这两个默认值时,`settings.py` 的 dataclass 默认、`load_settings` 的回落值、`SettingsManager.save_sync_settings` 的 `_save_cron`/`_save_int` 回落值**三处**要一起改——只改前两处的话,"什么都没改就点保存"会把 cron 写成空串,任务悄悄退回按 interval 跑(`tests/test_cron_validation.py` 有防漂断言)。

`preference_analyze` 手动触发(web 按钮)默认 `max_batches=10`(≈2000 篇),定时触发每轮只跑 1 批。

`recommendation_run` 默认关闭:它会消耗 Pixiv 搜索配额,且必须先有默认偏好画像。启用后仍与其他同步任务共用同一个 JobManager,天然互斥,不会和收藏同步并行抢配额。手动入口 `POST /api/dashboard/recommendations/run` 保持不变。

### 5.1 两个标签字典是独立的

`recommendation_run` 的接线跨了两个互不相干的标签字典,新增 task_type 时两处都要加:

- `jobs/tasks.py:_TASK_LABELS` —— 供 job 内部日志渲染(`task_label()`)。
- `web/managers.py:TASK_LABELS` —— 供**调度器**把内部任务名翻成中文写进 `task_logs`。缺失时任务日志页会显示英文键名。

手动路由 `POST /api/dashboard/recommendations/run` 提交时显式传 `task_name="生成推荐"`,不依赖任何字典;而定时触发走 `_submit_scheduler_task` → `TASK_LABELS.get(task_name)`。所以**只补一个字典时,手动能显示中文、定时却会显示英文键名**——这类不一致由 `tests/test_recommendation_scheduling.py::test_every_scheduler_task_has_a_web_label` 兜住。

同理,`web/utils.py:_job_spec` 里若没有对应 task_type 的分支,JobSpec 会静默落到 `JobType.SYNC`,任务统计归错类。这一条以及"四处注册表是否齐全"由 `tests/test_scheduler_priority.py::test_every_scheduler_task_is_registered_in_all_three_tables` / `::test_scheduler_tasks_map_to_their_own_job_type` 锁住:新增定时任务时那里的 `EXPECTED_SCHEDULER_TASKS` 会先红,提示按本文 §4 把四处补齐。

### 5.2 推荐 cron 排布与实测预算基线(2026-08-28)

上面那张表是**代码默认值**(大多为空 cron + 6 小时 interval),不是推荐排布。生产与 `config/config.yaml.example` 用下面这套按实测预算排的 cron,时区 `Asia/Seoul`:

| 任务 | P | cron | 频率 | 单轮实测耗时 |
|---|---|---|---|---|
| bookmarks | 1 | `"20 0,4,8,12,16,20 * * *"` | 6 次/天 | ~3 min |
| subscribed_series | 2 | `"40 1,13 * * *"` | 2 次/天 | ~13 min |
| following_list | 3 | `"30 10 * * *"` | 1 次/天 | ~5 min |
| following_novels | 3 | `"0 3,9,15,21 * * *"` | 4 次/天 | ~24 min(阶段一前 42 min) |
| user_status | 3 | `"30 6 */2 * *"` | 隔日 | 全库一轮跑完 |
| novel_status | 3 | `"0 5,17 * * *"` | 2 次/天 | 每轮一批 800 条 |
| series_status | 3 | `"30 18 */2 * *"` | 隔日 | 全库一轮跑完 |
| user_backup | 3 | `"30 2 */3 * *"` | 隔三日(默认关闭) | 最长 2.3 h |
| pending_deletion_detection | 3 | `"30 12 * * *"` | 1 次/天 | ~34 s |
| preference_analyze | 3 | `"15 7,19 * * *"` | 2 次/天(默认关闭) | 每轮 1 批 |
| recommendation_run | 3 | `"50 8 * * *"` | 1 次/天(默认关闭) | ≤20 条检索 |

生产实测 3 天 52 轮,单任务槽串行,总占用 4.9 小时/天(占空比 20.5%)。阶段一吞吐修复 + 阶段二 cron 重排后目标 3.8 小时/天(15.7%)。

排布约束:

1. P1 收藏每天 6 次均匀分布,任何时刻最多等 4 小时。
2. 长任务(`following_novels`、`novel_status`)避开收藏时刻 ±30 分钟,减少无谓让位。
3. 时效性弱的任务拉到隔日或隔三日。`novel_status` 从 4 次/天降到 2 次(全库轮转周期 2.4 → 4.8 天)是安全的:用户主动取消收藏/追更由 `pending_deletion_detection` 每天检测,不走状态巡检。
4. `following_novels` 频率不降:阶段一给单作者加了配额后单轮从 42 分钟降到约 24 分钟,同频率下覆盖率提升约 5 倍,降频反而白丢吞吐。
5. `subscribed_series` 每轮从 watchlist 头部重走(不可让位、不是水位续跑),所以只能靠降频省预算,实测连续 12 轮零新增,4 次/天 → 2 次/天。

cron 解析失败是**静默**回落到 interval,所以改完必须逐条验算下次运行时刻(不是只确认"不抛异常"):

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

**注意:截至 2026-08-28 生产 journald 里 `selected by priority` / `submit failed` / `yielded` 均为 0 次**——旧 cron 排得过散,两个任务几乎从不同时到点,3.6 那套让位逻辑从未被触发过。改动这些 cron 后如果看到让位日志,那是机制首次生效,不是故障。

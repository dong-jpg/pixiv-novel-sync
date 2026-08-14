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
- 主循环(`_run_scheduler_loop`,每 30s 醒一次):每轮重新 `load_settings`;`auto_sync_enabled=False` 则空转;逐任务检查 enabled 开关与 `_task_next_run`,到点且当前无任务在跑(`_current_task_job_id is None`)时**同步**执行 `_run_single_task`(提交失败/被单活跃约束拒绝则跳过,顺延下次时间)。
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

## 4. 新增 task_type 的步骤

以字符串 `"my_task"` 为例:

1. **实现任务函数**:签名接收 `settings` 与从 context 取出的 `reporter` / `stop_requested`(必要时 `claim_finalization`、`params`),返回 stats dict。放在 `jobs/services.py` 或独立模块,遵循函数内延迟 import 约定。
2. **注册分派**:在 `jobs/tasks.py:execute_task` 中加一个 `if task_type == "my_task":` 分支(未注册的 task_type 会抛 `RuntimeError("Unsupported task type for CLI execution: ...")`)。
3. **注册标签**:同文件 `_TASK_LABELS` 加中文标签(`task_label()` 用它渲染日志/UI);若要出现在 web 端任务列表/定时任务,`web/managers.py:TASK_LABELS` 也要加(两处是独立字典)。
4. **接入触发源**(按需):
   - CLI:在 `cli.py` 的参数解析 / `build_job_spec_from_args` 中允许该 task_type;
   - Web:在 `webapp.py` 相应路由里用 `_submit_shared_job` 提交;
   - Scheduler:在 `_run_scheduler_loop` 的 `task_configs` 增加条目,并在 `SyncSettings` / `SettingsManager.save_sync_settings` 补 `auto_sync_my_task_{enabled,interval_hours,cron}` 三件套。
5. **同步前端契约**:更新 `docs/frontend-api-contract.md`(以及涉及页面时的 `docs/frontend-pages.md`),让前端知道新的 task_type 字符串与标签;若响应结构变化,同步模板中的 Vue 代码。
6. **测试**:参照 `tests/test_jobs_runner.py` 等,依赖 conftest 的 tmp 路径 fixture;验证正常完成、取消(`stop_requested` 生效)、stats 合并。

## 5. auto_sync 配置矩阵(interval-hours vs cron)

规则(见 `web/managers.py:_run_scheduler_loop` 与 `settings.py:cron_to_next_run`):

- 全局开关 `auto_sync_enabled`(默认 False)+ 每任务 `*_enabled` 同时为真才调度。
- 每个任务同时有 `*_interval_hours` 与 `*_cron` 两个旋钮;**cron 非空则优先**,`cron_to_next_run(cron, now, auto_sync_timezone)` 计算下次时间(解析失败回落到 interval)。cron 在 `auto_sync_timezone`(默认 `"UTC"`)时区求值;`SettingsManager.save_sync_settings` 保存时会校验 cron 合法性。
- env 变量始终覆盖 YAML(`load_settings` 语义)。

`SyncSettings` 中各任务的默认值:

| 任务(scheduler name) | enabled 默认 | interval_hours 默认 | cron 默认 | 附加字段 |
|---|---|---|---|---|
| bookmarks | True | 6 | "" | |
| following_list | True | 24 | "" | |
| following_novels | True | 6 | "" | `auto_sync_following_novels_users_limit`(0=全部) |
| subscribed_series | True | 6 | "" | |
| user_status | True | 6 | "" | |
| novel_status | True | 6 | "" | |
| series_status | True | 6 | "" | |
| user_backup | False | 24 | "" | |
| pending_deletion_detection(设置字段名为 `auto_sync_pending_detection_*`) | True | 12 | "" | |
| preference_analyze | False | 1 | `"*/30 * * * *"` | `preference_analyze_batch_size`(默认 200);scheduler 提交时强制 `max_batches=1` |

注意:preference_analyze 是唯一自带默认 cron 的任务;手动触发(web 按钮)默认 `max_batches=10`(≈2000 篇),定时触发每轮只跑 1 批。

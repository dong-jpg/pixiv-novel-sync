# CLI Job Services Design

## Goal

补齐 CLI job execution，让 `user-backup`、`status-check` 和 `pending-deletion-detection` 不再停留在 `JobSpec` 生成阶段，而是通过 shared `JobRunner` 执行与 Web 自动任务一致的核心逻辑。

## Scope

本阶段包含：

- `user_backup:<user_id>` CLI task execution
- `user_status` CLI task execution
- `novel_status` CLI task execution
- `series_status` CLI task execution
- `pending_deletion_detection` CLI task execution
- 将 `webapp.py` 中对应 `SyncWorker` 方法委托给新 service
- 保留现有 Web route response shape、DB schema、`JobManager`、`JobRunner` 和 `JobSpec` 模型

本阶段不包含：

- 全量拆分 `webapp.py`
- 重写 `BookmarkNovelSyncService`
- 引入持久化 job queue
- 改变 dashboard API response shape
- 改变现有定时任务配置语义

## Architecture

新增 `src/pixiv_novel_sync/jobs/services.py`，作为 Web 和 CLI 共用的 job service layer。它负责初始化 Pixiv auth、database、file storage、sync service，并暴露独立函数执行每类任务。

`src/pixiv_novel_sync/jobs/tasks.py` 继续作为 `JobRunner` 的 dispatch adapter。它只解析 task name 和 context，然后调用 `jobs.services`。这样 CLI 通过 `JobRunner -> execute_task -> services` 执行；Web legacy worker 通过现有方法委托到同一 service。

`webapp.py` 的 route 和 legacy `SyncJobManager` 保持现状。只调整 `SyncWorker` 的任务方法，让它们调用 service 函数，并传入一个小型 progress/log adapter，避免 duplicate business logic。

## Components

### `jobs.services`

新增公共函数：

- `run_user_backup_task(settings, user_id, reporter=None, stop_requested=None) -> dict[str, Any]`
- `run_user_status_task(settings, reporter=None, stop_requested=None) -> dict[str, Any]`
- `run_novel_status_task(settings, reporter=None, stop_requested=None) -> dict[str, Any]`
- `run_series_status_task(settings, reporter=None, stop_requested=None) -> dict[str, Any]`
- `run_pending_deletion_detection_task(settings, reporter=None, stop_requested=None) -> dict[str, Any]`

新增轻量 reporter protocol：

- `info(message: str) -> None`
- `warning(message: str) -> None`
- `error(message: str) -> None`
- `success(message: str) -> None`
- `progress(**kwargs: Any) -> None`

如果未传 reporter，service 安静执行，只返回 stats。

### `jobs.tasks`

`execute_task()` 改为：

- `user_backup:<id>` 调用 `run_user_backup_task(settings, user_id, reporter, stop_requested)`
- `user_status` 调用 `run_user_status_task(...)`
- `novel_status` 调用 `run_novel_status_task(...)`
- `series_status` 调用 `run_series_status_task(...)`
- `pending_deletion_detection` 调用 `run_pending_deletion_detection_task(...)`

`execute_task()` 根据 `context["manager"]` 和 `context["job_id"]` 构造 `JobManager` reporter，并用 `manager.is_cancel_requested(job_id)` 作为 stop callback。

### `webapp.SyncWorker`

以下方法改为委托 service：

- `_sync_user_status`
- `_sync_novel_status`
- `_sync_series_status`
- `_sync_user_backup`
- `_sync_pending_detection`

Web adapter 使用 legacy `SyncJobManager` 的 `add_log()` / `update_progress()`，并复用 worker 的 `_check_stop()`。

## Data Flow

CLI flow：

1. CLI parser 构建 `JobSpec(source=CLI)`。
2. `run_job_command()` 创建 `JobManager` 和 `JobRunner`。
3. `JobRunner.run()` 逐个 task 调用 `execute_task()`。
4. `execute_task()` 创建 reporter 并调用 `jobs.services`。
5. service 执行业务逻辑，写 logs/progress，返回 stats。
6. `JobRunner` 合并 stats 并输出 JSON。

Web legacy flow：

1. Dashboard 或 auto sync 创建 legacy `SyncJobState`。
2. `SyncWorker` 根据 task 调用对应 `_sync_*` 方法。
3. `_sync_*` 方法创建 legacy reporter 并调用 `jobs.services`。
4. service 执行业务逻辑，legacy manager 继续维护原 response shape。

## Error Handling

- Service 内部对单个 item 的状态检查错误保持现有行为：记录 warning，继续处理后续 item。
- Pixiv 登录失败、远端列表完整性风险、DB 初始化失败等边界错误继续向上抛出，让 `JobRunner` 或 legacy worker 标记任务失败。
- `pending_deletion_detection` 保留现有防误删策略：远端收藏列表获取失败或异常为空时拒绝差集计算。
- `stop_requested()` 返回 true 时 service 尽快停止并返回当前 stats；如果底层必须中断，可抛 `InterruptedError`，由调用方按现有行为处理。

## Testing

测试按 TDD 实施：

1. `tests/test_jobs_tasks.py`
   - 验证 `user_backup:<id>`、`user_status`、`novel_status`、`series_status`、`pending_deletion_detection` 不再抛 “not available yet”。
   - monkeypatch `jobs.services` 函数，断言 dispatch 参数正确。

2. 新增或扩展 `tests/test_jobs_services.py`
   - 用 fake auth/api/db/storage 测试 service 的核心循环和 stats。
   - 验证 reporter 收到关键 log/progress。
   - 验证 stop callback 能终止循环。

3. Web regression tests
   - 保留 `tests/test_webapp_jobs.py` 和 `tests/test_webapp_security.py`。
   - 增加 focused test 证明 `SyncWorker` 对应方法调用 service，而不是复制逻辑。

4. CLI tests
   - 保留 `tests/test_cli_jobs.py`。
   - 增加 monkeypatch test 证明 CLI runner 能通过 `execute_task()` 跑完新任务并输出 succeeded JSON。

## Verification

实施完成后运行：

```bash
python -m pytest tests/test_jobs_tasks.py tests/test_cli_jobs.py tests/test_webapp_jobs.py tests/test_webapp_security.py -q
python -m pytest tests/test_jobs_services.py -q
python -m pytest -q
python -m pixiv_novel_sync.cli user-backup --help
python -m pixiv_novel_sync.cli status-check --help
python -m pixiv_novel_sync.cli pending-deletion-detection --help
```

## Self-Review

- Scope coverage: 设计覆盖 CLI 未接线的五类任务，并把 Web 对应 legacy worker 方法纳入共用 service。
- Placeholder scan: 无 TBD、TODO 或未决占位。
- Consistency: `JobRunner`、`execute_task`、`SyncWorker`、`BookmarkNovelSyncService` 使用方式与当前项目结构一致。
- Risk control: 保持 Web route、DB schema、job model 不变，只抽取任务业务逻辑。
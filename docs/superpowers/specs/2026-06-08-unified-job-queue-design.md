# 统一 Job 队列与部署优先重构设计

日期：2026-06-08

## 背景

`pixiv-novel-sync` 的核心价值是 Pixiv 小说本地长期归档。当前项目已经扩展出 Web 管理、定时任务、删除确认、推荐、偏好画像、AI 写作和 embedding/RAG。主要风险不是功能不足，而是部署路径、任务模型、安全默认值和模块边界正在分裂。

本设计选择“渐进式统一 Job 队列”作为第一轮全量重构的入口：先统一 Web、CLI、systemd/cron 的任务定义和执行语义，同时修复部署前必须处理的安全默认值。

## 目标

第一轮完成后：

1. Web、CLI、systemd/cron 使用同一套 `JobSpec` / `JobRunner` 语义。
2. Web 安全默认值收紧：未配置 `DASHBOARD_TOKEN` 时仅允许 localhost。
3. Pixiv `refresh_token` / `access_token` 不再通过普通状态 API 默认返回明文。
4. Flask secret fallback 不再使用可预测的 db_path 派生值。
5. systemd/cron 可通过 CLI 调用核心同步任务。
6. 现有测试和新增测试通过。

## 非目标

第一轮不做以下内容：

- 不一次性拆完整 `webapp.py` 蓝图。
- 不大规模重写 `sync_engine.py`。
- 不物理拆分 `storage_db.py` 或 SQLite 文件。
- 不把 AI/推荐任务全部队列化。
- 不引入 Redis、Celery 或多进程分布式队列。
- 不做 embedding/RAG 深度改造；只允许修明显低风险问题，例如空索引时避免无效 embedding API 调用。

## 推荐方案

采用方案 A：渐进式统一 Job 队列。

保留现有 Web 行为和 CLI 能力，但新增清晰的 `jobs/` 层。第一阶段以内存 job manager 为主，不引入复杂持久化队列。同步类任务先接入，AI/推荐只预留接口。

## 模块设计

### `src/pixiv_novel_sync/jobs/models.py`

定义统一任务模型：

- `JobSpec`
- `JobState`
- `JobStatus`
- `JobSource`
- `JobType`

建议字段：

- `job_id`
- `source`
- `job_type`
- `task_types`
- `params`
- `status`
- `message`
- `progress`
- `stats`
- `logs`
- `error`
- `created_at`
- `started_at`
- `finished_at`

状态枚举：

- `queued`
- `running`
- `succeeded`
- `failed`
- `cancel_requested`
- `cancelled`

### `src/pixiv_novel_sync/jobs/manager.py`

统一 job 生命周期：

- 创建 job
- 查询 job
- 写日志
- 更新进度
- 请求取消
- 清理旧 job
- 控制并发

第一轮可从现有 `SyncJobManager` 迁移或包裹现有行为，避免一次性重写全部 Web 任务逻辑。

### `src/pixiv_novel_sync/jobs/runner.py`

负责执行 `JobSpec`：

1. 标记 job 为 `running`。
2. 按 `task_types` 顺序执行。
3. 每个 task 边界检查取消状态。
4. 合并 stats。
5. 成功时标记 `succeeded`。
6. 失败时记录 error 并标记 `failed`。
7. CLI 根据最终状态返回 exit code。

### `src/pixiv_novel_sync/jobs/tasks.py`

收敛任务类型到业务函数的映射。

第一轮覆盖任务：

- `bookmark`
- `following_users`
- `following_novels`
- `subscribed_series`
- `sync_check`
- `user_status`
- `novel_status`
- `series_status`
- `pending_deletion_detection`
- `user_backup`

业务执行仍调用现有 `BookmarkNovelSyncService` 和现有状态检查逻辑。

## Web / CLI / systemd 边界

### Web

Web 负责：

- 鉴权
- 参数校验
- 创建 `JobSpec`
- 提交 job
- 查询 `JobState`
- 展示日志、进度和统计

Web 不再直接拼接复杂同步流程。

### CLI

CLI 负责：

- 解析命令参数
- 创建 `JobSpec`
- 同步执行 job 或提交执行
- 输出最终状态
- 失败时返回非 0 exit code

第一轮建议新增或补齐命令：

- `sync`
- `sync-check`
- `status-check`
- `pending-deletion-detection`
- `user-backup`

### systemd / cron

systemd/cron 只调用 CLI，不直接依赖 Web 内部函数。

Web 内置 scheduler 第一轮暂时保留，但改为提交 `JobSpec`。第二轮再决定默认关闭 Web scheduler、与 systemd 互斥，或继续共存。

## 安全设计

### Dashboard 鉴权默认值

未配置 `DASHBOARD_TOKEN` 时：

- localhost 请求允许访问。
- 非 localhost 请求返回 403。
- 日志明确提示当前处于仅本机免鉴权模式。

配置了 `DASHBOARD_TOKEN` 时保持现有登录流程。

### Token 最小暴露

普通 OAuth 状态 API 不再默认返回明文：

- 不返回 `refresh_token`。
- 不返回 `access_token`。
- 返回 `has_refresh_token`、`has_access_token`、`user_id`、`status`、`message`。

如必须支持手动复制 token，应通过单独接口或显式参数控制，并要求已认证。

### Flask secret fallback

优先使用 `PIXIV_FLASK_SECRET`。

若未设置：

- 生成随机 secret。
- 持久化到本地状态文件。
- 后续启动复用该文件。

不再使用 dashboard token + db path 派生 secret。

## sync_check 设计

第一轮保留当前 fingerprint/scope 思路：

- `sync_check` job 成功后记录 scope、fingerprint、task types、user id。
- 正式同步 job 可查找匹配 scope 并复用预检查结果。

后续第二轮再考虑：

- 持久化 sync_check 批次。
- 为 scope 设置 TTL。
- 批量 upsert sync_check items。
- all_novel_ids 去重。

## 错误处理

### 单 job 失败

- `status=failed`
- `error` 写入异常消息
- 写日志
- CLI 返回非 0
- Web 可查询失败状态

### 多 task job

第一轮默认遇到失败即停止并标记整个 job 失败。

不做 `continue_on_error`，避免部分成功被误解为全成功。

### 取消

第一轮先统一取消状态和接口：

- `cancel_requested`
- `cancelled`

JobRunner 在每个 task 边界检查取消标记。同步引擎深层循环取消检查留到第二阶段。

## 测试计划

### 新增测试

`tests/test_jobs_manager.py`：

- submit job
- 状态流转
- 日志裁剪
- cancel_requested
- failed error 记录

`tests/test_jobs_runner.py`：

- fake task 成功
- fake task 失败
- 多 task 顺序执行
- task 边界取消
- stats 合并

`tests/test_webapp_security.py` 或扩展现有 Web 测试：

- 未配置 `DASHBOARD_TOKEN` 时 localhost 允许
- 未配置 `DASHBOARD_TOKEN` 时非 localhost 403
- token 状态 API 不返回 `refresh_token` / `access_token`
- Flask secret fallback 持久化

CLI 测试：

- 新 CLI 命令生成正确 `JobSpec`
- 失败时 exit code 非 0

### 回归测试

保留并扩展：

- `tests/test_oauth_helper.py`
- `tests/test_playwright_login.py`
- `tests/test_webapp_settings.py`
- `tests/test_ai_retrieval.py`

### 验证命令

每个阶段完成后运行：

```bash
pytest
```

CLI smoke test：

```bash
python -m pixiv_novel_sync.cli --help
python -m pixiv_novel_sync.cli sync --help
```

## 分阶段实施建议

### 阶段 1：Job 模型和测试骨架

- 新增 `jobs/models.py`。
- 新增 `jobs/manager.py`。
- 新增 manager 单元测试。

### 阶段 2：JobRunner 和同步任务适配

- 新增 `jobs/runner.py`。
- 新增 `jobs/tasks.py`。
- 迁移最小同步任务执行路径。
- 保持 Web 当前行为可用。

### 阶段 3：Web 接入 JobSpec

- Web 手动同步和自动调度改为提交 `JobSpec`。
- 保留旧 API 响应形状，降低前端改动。

### 阶段 4：CLI/systemd 接入

- 补齐 CLI 任务命令。
- 更新 systemd/cron 文档或服务命令。

### 阶段 5：安全准入修复

- 收紧未配置 `DASHBOARD_TOKEN` 的访问范围。
- token API 最小暴露。
- Flask secret 持久化 fallback。

安全修复也可以提前穿插执行，但必须在第一轮完成前落地。

## 风险与缓解

### 风险：重构范围过大导致现有 Web 行为破坏

缓解：第一轮只抽最小 `jobs/` 核心，保留现有 API 响应形状和主要业务函数。

### 风险：Web scheduler 与 CLI/systemd 同时运行造成重复同步

缓解：第一轮先统一任务语义，第二轮加入互斥策略；文档中明确不要同时启用两套定时。

### 风险：取消只在 task 边界生效

缓解：第一轮明确这是限制；第二轮把 cancellation token 传入同步引擎长循环。

### 风险：当前未提交 embedding/sync_check 改动与 jobs 重构冲突

缓解：先在设计和计划中保留现有改动，实施时优先小步迁移，并在每阶段运行测试。

## 验收标准

- Web 和 CLI 都能通过统一 `JobSpec` 执行核心任务。
- 未配置 `DASHBOARD_TOKEN` 时非 localhost 访问受限。
- 普通 token 状态响应不包含明文 Pixiv token。
- Flask fallback secret 持久化且不可预测。
- systemd/cron 可以通过 CLI 调核心任务。
- `pytest` 通过。
- CLI help smoke test 通过。

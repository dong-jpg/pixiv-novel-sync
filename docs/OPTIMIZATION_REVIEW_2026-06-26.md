# 代码复查与优化方案（2026-06-26）

## 复查范围

- 入口与配置：`pyproject.toml`、`cli.py`、`settings.py`、`webapp.py`
- Web 管理层：`web/managers.py`、`web/utils.py`、Dashboard 设置接口
- 任务链路：`jobs/tasks.py`、`jobs/services.py`、自动同步调度器
- 文档与示例：`README.md`、`.env.example`、`config/config.yaml.example`、`docs/INDEX.md`

## 本次发现并处理的问题

### 1. 设置接口漏返回偏好分析配置

`SettingsManager.save_sync_settings()` 已支持 `auto_sync_preference_analyze_*` 和 `preference_analyze_batch_size`，但 `/api/dashboard/settings` 使用的 `webapp._settings_to_dict()` 未返回这些字段。结果是设置页保存后和刷新后的数据不一致，前端可能出现空值或误用默认值。

处理：已补齐 `webapp.py` 与 `web/utils.py` 的设置序列化字段，并增加测试覆盖。

### 2. pending deletion 清理参数只定义未加载

`SyncSettings` 和 `jobs/services.py` 已使用 `pending_deletion_grace_period_days`、`pending_deletion_cleanup_confirmed_days`，但 `load_settings()` 没有从 YAML 读取，用户配置不会生效。

处理：已接入 YAML 加载、设置保存和设置返回，并更新示例配置。

### 3. 文档安全说明过期

`.env.example` 仍写着 `DASHBOARD_TOKEN` 留空会使站点完全公开，`PIXIV_FLASK_SECRET` 缺省按 `dashboard_token + db_path` 派生。当前代码已经改为：未配置 dashboard token 时仅允许本机访问；Flask secret 缺省随机生成并写回 `.env`。

处理：已更新 `.env.example` 和 README 对应说明。

### 4. 文档索引存在断链

`docs/INDEX.md` 指向仓库中不存在的 `README_NEW.md`，容易误导维护者。

处理：已改为当前 `README.md`，并标注历史审计文档与最新复查文档的关系。

## 仍建议继续优化的方向

### P0：去重设置序列化（已完成）

当前生产代码只保留 `web/utils.py` 中一份 `_settings_to_dict()` 定义，`webapp.py` 和 `web/managers.py` 均改为导入使用。后续新增设置字段时只需要维护这一处，并由 `tests/test_webapp_settings.py` 覆盖。

### P1：收紧配置校验（已推进）

`bookmark_restricts` 已在保存和加载两侧统一限制为 `public` / `private`，加载旧配置时会归一化大小写、去重并过滤无效值；`series_sync_limit` 已在保存和加载两侧 clamp 到非负整数，避免无效配置进入同步任务。

### P1：统一 API 错误格式（已部分推进）

Dashboard 同步启动、单任务同步、预检查、设置保存等高频接口已统一返回 `{ok: false, error, detail?}`。仍建议继续清理剩余低频路由里的 `{"error": ...}` / `{"success": false}` 返回，最后再同步前端错误处理和 API 文档。

### P2：拆分超大模块

`webapp.py`、`sync_engine.py`、`web/managers.py` 和 `templates/dashboard_ai.html` 都偏大。建议按“路由蓝图 / 服务 / 序列化 / 模板组件”逐步拆分，先从无行为变化的提取开始。

### P2：完善长期任务取消机制（继续推进）

共享 JobRunner 的直接同步任务已把取消信号传入进度回调，`following_users`、`following_novels`、`subscribed_series` 等长同步在同步引擎回调点会抛出 `InterruptedError` 并被标记为 cancelled，而不是 failed。`bookmark` 快捷同步和 `sync_check` 也已接入 `stop_requested`，可在登录前和同步引擎进度回调处中断；`jobs/services.py` 的用户全量备份分页等待、状态检查逐项等待也改为短间隔轮询取消。`sync_engine.py` 内部原有裸 `time.sleep()` 已集中改为可取消等待，并避免 `_sync_novel()` 把 `InterruptedError` 吞成普通失败统计。

2026-06-30 继续推进：
- `RateLimiter.wait()` / `handle_response()` 已支持 `stop_requested` + `interval` 轮询参数，命中取消时抛 `InterruptedError`；`sync_engine.py` 的 `check_bookmarks_existence` / `check_all_existence` / `_fetch_remote_bookmark_ids` 共 5 处 `rate_limiter.wait()` 通过 `_stop_requested_from_progress(progress_callback)` 适配器接入取消，预检查与待删除检测的分页限速等待现在也可中断。
- 定时自动同步路径（`web/managers.py` 的 `AutoSyncScheduler._run_single_task` 与 legacy `SyncJobManager._run_job`）原先用 `except Exception` 把 `InterruptedError` 吞成 failed，现已补 `except InterruptedError` 分支，停止定时任务时正确标记为 cancelled。
- `run_check_bookmarks_task` 在 `except Exception` 前补 `except InterruptedError: raise`，避免把用户停止记成"预检查失败"日志。

仍待推进：`recommendations.py` 的 `_page_delay()` 与 AI/Playwright 辅助模块的等待尚未接入 `stop_requested`（推荐/偏好任务目前无取消通道）；`webapp.py` 的 `start_job`/`start_user_backup_job` 这类 legacy 入口在生产中已不被调用，可考虑彻底移除以消除两套任务系统的维护负担。

### P3：清理仓库生成物

源码目录和 tests 目录中存在 `__pycache__`，工作区根目录也有 `.pytest_cache`、`db.sqlite`。建议在确认不需要保留后清理，并确保 `.gitignore` 覆盖这些生成物。

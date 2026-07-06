# 优化方案 2026-06-30

> 基于 2026-06-30 对工作区代码的一次全量审查。本文记录已修复的隐藏 Bug、仍存在的不合适实现，以及按优先级排定的后续优化方向。
> 前序评审见 [OPTIMIZATION_REVIEW_2026-06-26.md](OPTIMIZATION_REVIEW_2026-06-26.md)。

## 一、本次已修复（随本次提交上线）

| # | 类型 | 位置 | 问题 | 修复 |
| --- | --- | --- | --- | --- |
| 1 | 🔴 Bug | `tests/test_rate_limiter.py` + `rate_limiter.py` | 孤儿测试调用 `wait(stop_requested=, interval=)` / `handle_response(stop_requested=, interval=)`，实现未提供，3 个测试全失败，阻塞 `pytest -q` | 给 `RateLimiter.wait()` / `handle_response()` 加 `stop_requested`+`interval` 轮询参数，命中取消抛 `InterruptedError("Task stopped by user")`；`stop_requested=None` 时退化为单次 sleep，保持原行为 |
| 2 | 🔴 Bug | `web/managers.py:295 / :1008` | 定时自动同步任务被用户停止时，`except Exception` 把 `InterruptedError` 吞成 `failed`（状态、DB 日志、前端均显示失败） | 两处 `except Exception` 前补 `except InterruptedError`，标记 `cancelled` 并写 `task_logs.status='cancelled'` |
| 3 | 🟠 Bug | `jobs/quick_sync.py:143` | `run_check_bookmarks_task` 的 `except Exception` 捕获取消信号，先写 error 级"预检查失败"日志再重抛，误导用户 | 补 `except InterruptedError: raise`，取消不再记为预检查失败 |
| 4 | 🟠 Bug | `sync_engine.py` 5 处 `rate_limiter.wait()` | `check_bookmarks_existence` / `check_all_existence` / `_fetch_remote_bookmark_ids` 分页限速等待不可取消，停止后仍阻塞完整 `delay_seconds_between_pages` | 新增 `_stop_requested_from_progress(progress_callback)` 适配器，把进度回调桥接成 `stop_requested` 传入 `rate_limiter.wait()`；`_sync_novel` 已有 `except InterruptedError: raise` 防吞 |
| 5 | 🟡 死代码 | `rate_limiter.handle_response` | 429 重试路径无任何生产调用方，仅测试引用 | 暂保留（已具备可取消能力），后续接入 429 重试时即可生效；见 P2 |
| 6 | 📄 文档 | `docs/API_COMPLETE.md` | 成功响应仍写 `"success": true`（实际为 `"ok": true`）；登录/登出/保存 Token 响应描述与代码不符 | 统一改为 `{ok: true, ...}` 并校正登录（302/401/429）、登出、保存 Token 的真实响应 |
| 7 | 📄 文档 | `docs/OPTIMIZATION_ROADMAP.md` | 2026-06-16 旧规划、含占位符"你的用户名"、已被 REVIEW 取代 | 顶部加取代声明、修正维护者为 `@dong-jpg`、标注归档 |
| 8 | 📄 文档 | `docs/superpowers/plans/2026-06-26-job-cancellation-hardening.md` | Task 3 Step 2/3 未勾选、全量验证因 Bug #1 失败 | 勾选完成、补 2026-06-30 扩展范围说明 |

验证：`python -m pytest -q` → 209 passed。

## 二、仍建议继续优化的方向

### P0：无

本次 P0 已全部闭合。

### P1：统一 API 错误/成功格式（收尾）

- `webapp.py:726 / :737` 阅读进度 POST/DELETE 仍返回 `{"success": True}`，改为 `{"ok": True}`。
- `/api/save-token` 错误仍返回 `{"error": "missing refresh_token"}`（line 524），`/api/auth/login` 限流返回 `{"error": "too many login attempts"}`（line 381）——改用 `_api_error(...)` 统一为 `{ok:false, error, detail?}`。
- 同步前端 `dashboard_*.html` 的错误处理（目前多数只读 `data.message`/`res.ok`，影响小，但应一并核对）。

### P1：归并两套任务系统

仓库现有两套并行机制：`jobs/manager.py`+`JobRunner`（共享，手动 Web 同步与 CLI 走此）与 `web/managers.py`+`AutoSyncScheduler`+`SyncJobManager`（定时自动同步与 legacy 走此）。取消逻辑、状态枚举、日志写库逻辑都得维护两份，是 Bug #2 的根因。建议：
- 把 `AutoSyncScheduler` 改为提交到共享 `JobManager`（用 `JobSource.AUTO`），复用 `JobRunner` 的 `except InterruptedError` → cancelled。
- 移除生产中已不被调用的 `SyncJobManager.start_job` / `start_user_backup_job` / `_run_job`（仅测试使用，迁移测试后删除）。

### P2：补齐取消通道覆盖面

- `recommendations.py:115` 的 `_page_delay()` 与 `_search_novels()` 分页未接入 `stop_requested`；`recommendation_run` / `preference_analyze` 任务在 `jobs/tasks.py` 也未接收 `stop_requested`。需把 `stop_requested` 从 `_run_recommendation_run_task` / `_run_preference_analyze_task` 传入并接到 `RateLimiter.wait(stop_requested=...)`。
- AI/Playwright 辅助模块（`ai/`、`playwright_login.py`）的长等待同上。
- 接入 `rate_limiter.handle_response` 的 429 重试路径（目前是死代码），让限流等待也可取消。

### P2：拆分超大模块

`webapp.py`（约 1600+ 行）、`sync_engine.py`（约 1700+ 行）、`web/managers.py`、`templates/dashboard_ai.html` 偏大。按"路由蓝图 / 服务 / 序列化 / 模板组件"逐步提取，先做无行为变化的拆分。与 P1 归并任务系统可结合进行。

### P3：清理仓库生成物

源码与 tests 目录有 `__pycache__`，根目录有 `.pytest_cache`、`db.sqlite`。确认 `.gitignore` 覆盖并清理已提交的生成物。

### P3：边缘配置行为

`settings._coerce_bookmark_restricts([])` 会把显式空列表静默回退为 `["public","private"]`（旧行为是 `[]` 即不同步收藏）。正确禁用方式是 `sync_bookmarks: false`，但空列表的静默覆盖仍易误导。建议要么保留空列表、要么在加载时对"显式空"与"全无效"做区分日志。

## 三、部署

服务器：`ubuntu@168.107.30.164`，密钥 `C:\Users\dong\Desktop\pixiv.key`。
代码推送到 GitHub 后，在服务器执行：

```bash
cd ~/pixiv-novel-sync && ./update.sh
```

`update.sh` 应执行 `git pull` + 依赖更新 + 服务重启。本次改动仅涉及 Python 源码与文档，无新依赖、无 DB schema 变更，重启 Web 进程即可生效。

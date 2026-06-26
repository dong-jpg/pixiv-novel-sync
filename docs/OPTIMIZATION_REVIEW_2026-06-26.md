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

### P0：去重设置序列化

当前 `_settings_to_dict()` 至少存在三份：`webapp.py`、`web/managers.py`、`web/utils.py`。这正是本次漏字段的根因。建议只保留 `web/utils.py` 一份，其他模块导入使用。

### P1：收紧配置校验

`bookmark_restricts` 目前只要求非空 list，建议限制为 `public` / `private`；`series_sync_limit` 保存时也建议统一 clamp 到非负整数，避免无效配置进入同步任务。

### P1：统一 API 错误格式

当前 Web 路由常见返回格式有 `{"error": ...}`、`{"ok": false}`、`{"success": false}` 等多种。建议统一为 `{ok, error, detail}`，前端处理会简单很多。

### P2：拆分超大模块

`webapp.py`、`sync_engine.py`、`web/managers.py` 和 `templates/dashboard_ai.html` 都偏大。建议按“路由蓝图 / 服务 / 序列化 / 模板组件”逐步拆分，先从无行为变化的提取开始。

### P2：完善长期任务取消机制

Job 层已有 stop 标记，但长同步链路里仍有多个循环和外部请求需要进一步传入取消检查。建议为所有长循环和 Pixiv 请求间隙统一检查 cancellation。

### P3：清理仓库生成物

源码目录和 tests 目录中存在 `__pycache__`，工作区根目录也有 `.pytest_cache`、`db.sqlite`。建议在确认不需要保留后清理，并确保 `.gitignore` 覆盖这些生成物。

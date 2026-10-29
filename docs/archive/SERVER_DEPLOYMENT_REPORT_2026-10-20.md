# 服务器部署记录（2026-10-20）

> **当前状态：main `0c7578e` 已部署并验证通过。** 本次为常规更新（`./update.sh`：拉取、重装依赖、刷新 Nginx/systemd、重启服务），无配置变更、无数据操作。上一份记录见 [2026-10-08](SERVER_DEPLOYMENT_REPORT_2026-10-08.md)。

## 更新内容

main `b104a68` → `0c7578e`：A14-01～05 缺陷修复批 + P2/P3 批 + 性能优化项（详见 [10-20 审查报告](AUDIT_REPORT_2026-10-20.md)与 [整改状态](REMEDIATION_STATUS_2026-09-30.md)）。用户脚本升至 0.3.1（油猴经 `@updateURL` 自行更新）。

## 部署与验证（服务器时间 07:27 UTC 前后）

| 项目 | 结果 |
|---|---|
| `./update.sh` | 六步全部成功；`.env` / `config.yaml` 备份恢复正常 |
| 服务 | `systemctl is-active` → active；waitress 监听 `127.0.0.1:5011` |
| 调度器 | 正常启动，11 个任务按 task_logs 恢复的 last-run 重新排程（未推迟整周期） |
| 健康检查 | 源站 `/api/health` → 200；公网 `https://pixiv.dongboapp.com/api/health` → 200 |
| 救援 API | 未带 Token 请求 `/api/rescue/v1/series` → 401（认证门正常） |
| schema 迁移 | 存量库已生效：`idx_rescue_catalog_item_type` 存在；`ai_jobs.owner_scope` / `idempotency_key_hash` 列在位（新建库一次建齐的修复不影响已有库） |
| 救援目录 | 592 条目，读取正常 |
| 错误日志 | 更新前后 `journalctl -p err` 无新增条目 |

## 遗留观察

- 新增的 `fail_stale_ai_jobs` 5 分钟节流与 `document.hidden` 轮询门控属行为变化，正常使用无感；如需观察可在日志页确认孤儿 AI 任务不再长时间显示 running。
- 油猴脚本 0.3.1 的列表页失败重试/40 分批/路由代次守卫需在浏览器端生效，服务器侧无法验证；如遇异常可在脚本管理器回退 0.3.0。

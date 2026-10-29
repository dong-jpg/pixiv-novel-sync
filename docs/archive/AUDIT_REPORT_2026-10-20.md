# 全项目审查与修复报告（2026-10-20）

基线：main `b104a68`（10-14 审计之后新增「拯救系列 tab」提交），全量 1458 passed / 6 skipped。
方法：四路子代理深审（同步/限流、存储、Web、AI）+ 逐条人工复核全部 P1/P2 主张；子代理的 3 项严重主张被复核证伪（不限量模式静默截断、"not found" 泛匹配、claim 顺序缺陷），未收录。已确认缺陷与修复同批登记在 [整改状态](REMEDIATION_STATUS_2026-09-30.md)。

## 已修复（本批，全部带回归测试）

| 项 | 内容 | 位置 |
|---|---|---|
| A14-01 | user_backup 错误页按失败收口；共享判定 `response_is_pixiv_error_page` 接入五个分页站点 | `jobs/services.py`、`sync/utils.py`、`sync_engine.py` |
| A14-02 | 偏好分析：清洗前轮询取消、画像发布前领 FinalizationClaim；取消后画像零写入 | `jobs/tasks.py` |
| A14-03/04/05 | 用户脚本 0.3.1：pending 两态标记失败可重试；40 ID 合计分批；路由代次守卫 | `userscripts/pixiv-rescue.user.js` |
| P2-5 | 系列 tab「加载更多」改追加式；interval 防重复注册 | 同上 |
| P2-1 | `public_auth` 增加 `CatalogNotReadyError → 503` | `rescue_web.py` |
| P2-2 | 重试补丁统一 `apply_pixiv_retry_patch`（幂等、含 `user_detail`），`_login`/service 共用 | `sync/utils.py`、`jobs/services.py`、`sync_engine.py` |
| P2-4 | Provider 非流式回退与错误体 64 KiB 分块限读（1 MiB 上限） | `ai/providers.py` |
| 新发现 | 新库 `init_schema()` 首跑时 `owner_scope`/`idempotency_key_hash` 被路由迁移的整表重建抹掉（需跑两次才补上）——补列移到迁移之后 | `storage/schema.py:_migrate_ai_tables` |
| P3 批 | `_atomic_write_yaml` 随机临时名+写锁；`list_rescue_series_chapters` 用 `has_content`；`cleanup_ai_jobs` 落实 `owner_scope`；watchlist 直连限速；SSE 替换字符告警；孤儿任务不再误报已取消；删除 `_replay_resume_child`/`_conclude_route`；`AIWritingService` 用后显式关闭 | 各处 |

## 复核证伪的子代理主张（记录以免重查）

1. 「不限量模式 following 只同步第一页」——实际 `while next_following_query` 正常翻页，两个上限均标记 truncated+incomplete。
2. 「`_pixiv_content_unavailable_reason` 泛匹配 not found」——实现只返回 Pixiv 错误 JSON 的 `user_message`，限流/空 user_message 均回退重试路径，fail-safe 语义正确。
3. 「direct sync 的 claim 顺序有竞态」——`工作返回 → stop 检查 → claim → 重建` 正是文档化设计，claim 与取消的仲裁由 Runner 完成。

## 明确不做 / 待 ai-writing 分支

- ~~A14-01/02/05 的 ai-writing 移植~~：**已于 2026-10-20 完成**（A14-01/02 经补丁移植 + 定向套件与全量 1788 passed 验证；A14-05 为该分支脚本 0.1.1 加路由代次守卫）。
- ~~lookup 批量下沉存储层~~：已完成（`get_rescue_items_batch`，40 ID 从 40~80 次聚合降为按批查询，与单条版语义一致性有回归锁定）。
- ~~`ai_health` PBKDF2 缓存~~：已完成（`crypto.py` 进程级 KDF 缓存）。
- ~~`_estimate_with_providers` 逐 provider 判定~~：已完成。
- ~~`fail_stale_ai_jobs` 提频~~：已完成（调度循环独立 5 分钟节流，孤儿任务不再等整点）。
- ~~`rescue_catalog` item_type 索引~~：已完成（`idx_rescue_catalog_item_type`，查询计划确认 covering index）。
- ~~dashboard_logs 手写 SSE 循环~~：已完成（切 `window.streamSSE` + 错误帧经 `errorText`）。
- 保留项：
  - `rebuild_rescue_catalog` 增量化——实测 5000 本库全量重建 577 ms；改造需把受影响 ID 集合贯穿各任务 stats，工程量与风险大于当前收益，且全量重建有「绝对一致」优点。留待库规模再增长后启动。
  - dashboard 空闲轮询降频——20s 节奏被 `test_frontend_library_os` 锁定为既有契约（test_idle_dashboard_polls_slowly），只补了 `document.hidden` 门控（隐藏标签不发请求）。
  - webapp/sync_engine 大文件拆分——可维护性债务，非缺陷，沿用 10-14 结论不动。
- Linux 安全回归、生产部署与外部验收（沿用 10-14 报告边界）。

## 验证

- main `python -m pytest -q`：全量通过（含新增回归；最终数字以整改状态为准）。
- ai-writing `python -m pytest -q`：1788 passed / 6 skipped。
- `python -m compileall -q src tests`：通过。
- `node --check userscripts/pixiv-rescue.user.js`（两分支）：通过。

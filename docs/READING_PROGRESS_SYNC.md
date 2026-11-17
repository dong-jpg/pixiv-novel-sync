# 跨设备阅读进度

阅读位置由 VPS 保存，浏览器保留未确认的离线修改。此功能不依赖云电脑在线，不向远程 worker 发送阅读进度。用户可以回退、重读或重置，冲突时不能简单采用较大的百分比。

应用 `4b8133e` 已于 2026-11-13 部署到 VPS，线上版本及证据见 [STATUS](STATUS.md) 和[本轮验收记录](archive/READING_PROGRESS_ADAPTERS_ACCEPTANCE_2026-11-12.md)。本地并发/浏览器与线上协议验收已通过，真实手机使用体验另行记录。

## 页面行为

- 打开阅读页时始终核对服务器。旧的无版本本地记录保留为迁移候选，由用户选择，不自动覆盖服务器。
- 实际阅读滚动按五秒节流保存；恢复位置、字号变化及程序滚动不产生零进度写入。离开页面先保留离线记录，再通过共享 CSRF 请求尽力发送小型 keepalive 请求。
- 网络失败保留原请求、版本和 mutation ID。恢复网络后先重试原请求，不自动改用最新版本覆盖其他设备。
- 冲突时展示服务器和本机位置，可选择服务器进度，或明确保留本机位置并以已看到的服务器版本再次条件写入。冲突期间继续阅读仍保留本机样本。
- 重置同样需要版本校验，并留下递增版本的重置标记。旧设备的待同步请求不能把已重置进度恢复回来。
- 其他标签页重新登录后，明确重试会刷新 CSRF 令牌，保留原 mutation；通用请求助手不会自动重发任意失败的写请求。

## HTTP 契约

仍使用 `/api/dashboard/novels/<novel_id>/progress`，沿用 Dashboard 登录和 CSRF。响应为 `private, no-store`。单次写请求不超过 4096 字节，额外字段、重复 JSON 键及无效类型被拒绝。

GET 返回 `novel_id, progress, status, version, is_reset, last_read_at, created_at, updated_at`。没有记录时版本为 0；不存在的小说返回 404。`progress` 是 0–100 的整数，状态为 `unread/reading/completed`。时间戳明确为 UTC，版本用于并发比较，不依赖设备时钟。

POST 接受：

```json
{"progress":42,"status":"reading","base_version":3,"mutation_id":"81d0ec46-950e-4333-b187-0416a71ff4d1"}
```

DELETE 接受 `base_version` 和 `mutation_id`，写入 `progress=0/status=unread/is_reset=true`。每次新的成功修改使版本严格加一。成功返回：

```json
{"success":true,"ok":true,"record":{"novel_id":42,"progress":42,"status":"reading","version":4,"is_reset":false,"last_read_at":null,"created_at":null,"updated_at":null},"applied_version":4,"replayed":false}
```

响应丢失后重放相同 mutation 时，`applied_version` 保留原写入版本，`record` 可以包含其他设备之后提交的更高版本。旧回执不能为后续本机样本自动取得新版写权限。

版本不符返回 409 `progress_conflict` 和当前 `record`；同 mutation 的内容改变返回 409 `progress_idempotency_conflict`。缺少版本/标识返回 428，并提示刷新旧页面。格式错误 400，超限 413，存储暂不可用 503；这些失败不改变阅读位置。

## 数据与迁移

旧 `reading_progress` 行保留内容，新增 `version=1` 和 `is_reset=0`。新 `reading_progress_mutations` 表按小说保存最近 32 份请求摘要与原应用版本，与进度写入在同一短事务中提交。旧回执被淘汰后，旧版本请求仍被 CAS 拒绝。删除小说时回执随进度清理；普通阅读页重置保留版本标记。

迁移只增加字段和表，不重写小说正文、归档或远程控制库。升级需先在私有副本核对旧进度与重复迁移结果，再启用新页面。回退到不识别版本的旧后端时应暂时阻断进度写入，保留 GET 与浏览器离线队列，不能让旧接口忽略新版前置条件。

实现与验证入口：`storage/reading_progress.py`、`web/reading_progress.py`、`static/js/reading_progress_sync.js`；并发/重放/重置/认证测试见 `tests/test_reading_progress_sync.py`，浏览器与跨标签页用例见对应 Node 测试和 `scripts/verify_reading_progress_ui.py`。最新上线与验证状态见 [STATUS](STATUS.md)。

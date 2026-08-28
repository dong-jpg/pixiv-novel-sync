# 同步预算重排与设置/AI 页面重构 设计文档

- 日期：2026-08-28
- 状态：待评审
- 依据：生产服务器（`ubuntu@168.107.30.164`，commit `fb91da3`）实测 —— 3 天 52 条 `task_logs`、72 小时 journald、直连生产库度量、生产环境实测 Pixiv API 延迟

## 1. 目标

用户提出三项诉求：

1. 给定时任务排优先级：收藏 = P1，追更系列 = P2，其余 = P3，优先保证重要任务。
2. 依据真实日志重排任务执行间隔，把时效性弱的任务拉长到隔日/隔数日。
3. 重构设置页与 AI 相关页面。

本文档给出三者的统一设计。设计原则：**先修吞吐，再排预算，最后调频率**——因为实测证明当前的瓶颈不是频率，也不是 Pixiv 限流。

## 2. 实测结论（推翻两个前提）

### 2.1 不存在「每天某时段触发风控」

72 小时 journald 内**零条真实 429**。全部 27 条匹配 `429` / `rate limit` / `too many requests` 字样的日志中，22 条是本项目自己的熔断 WARNING，5 条是日志毫秒数恰好为 `,429` 的巧合。12 次 `aborted_reason` 的发生时刻覆盖 KST 全天（00:24 / 03:59 / 04:40 / 06:37 / 12:51 / 15:35 / 18:11 / 19:04 / 22:55 …），无时段聚集。

commit `923dfd0`（今日凌晨部署）已修掉主要误判来源，同一任务前后对比：

| 时刻 | 任务 | 结果 |
|---|---|---|
| 01:17 | subscribed_series | `aborted_reason: rate_limited`，卡在 213/214 |
| 04:40 | subscribed_series | **无 aborted_reason**，跑完 214/214，`series_deleted: 25` |
| 07:20 | bookmark | 三天来首次 `succeeded`（此前 6 轮全 partial） |

**结论：调间隔不是为了躲风控。** 真正要解决的是预算分配——单任务槽（`BoundedSemaphore(1)`）串行，当前 4.9 小时/天的预算花得极不划算。

### 2.2 优先级机制已存在，但从未生效

`web/managers.py:SCHEDULER_TASK_CONFIGS` 已有 11 个任务，分级恰好就是用户想要的。但 journald 显示：`selected by priority` 出现 **0 次**、`submit failed` **0 次**、`yielded` **0 次**——生产 cron 排得过散，两个任务几乎从不同时到点，让位逻辑一直是死代码。

`web/managers.py:SCHEDULER_TASK_CONFIGS` 已有 11 个任务，分级恰好就是用户想要的。但 journald 显示：`selected by priority` 出现 **0 次**、`submit failed` **0 次**、`yielded` **0 次**——生产 cron 排得过散，两个任务几乎从不同时到点，让位逻辑一直是死代码。

### 2.3 当前 11 个任务的真实预算占用（3 天实测均值）

| 优先级 | 任务（scheduler name） | 可让位 | 轮数/3天 | 单轮耗时 | 3天总耗时 | 占比 |
|---|---|---|---|---|---|---|
| **P1** | bookmarks | ✗ | 7 | 178s | 1246s | 2% |
| **P2** | subscribed_series | ✗ | 12 | 767s | 9204s | 17% |
| P3 | following_novels | ✓ | 8 | **2519s** | **20152s** | **38%** |
| P3 | novel_status | ✓ | 11 | 1575s | 17325s | 33% |
| P3 | user_backup | ✓ | 2 | 965s | 1930s | 4% |
| P3 | series_status | ✓ | 3 | 587s | 1761s | 3% |
| P3 | user_status | ✓ | 2 | 400s | 800s | 2% |
| P3 | following_list | ✗ | 2 | 304s | 608s | 1% |
| P3 | pending_deletion_detection | ✗ | 5 | 34s | 170s | <1% |
| P3 | preference_analyze | ✓ | **0** | — | — | 从未运行 |
| P3 | recommendation_run | ✗ | **0** | — | — | 从未运行 |

合计 53196 秒 ≈ 14.8 小时/3 天 ≈ **4.9 小时/天**，占空比 20.5%。

生产库规模：novels 7635（normal 6796 / deleted 693 / restricted 146）、users 298、series 273（订阅 243 normal + 30 deleted）、novel_texts 7627。日增 56–141 篇。

### 2.4 关键发现：三个吞吐缺陷

#### 缺陷 A：`following_novels` 每轮只覆盖 1 个作者

256 个关注作者中 **200 个从未被同步过**（水位线 `user_last_synced` 仅 56 条）。

根因在 `sync_engine.py:1040`：`max_items_per_run=20` 只在**切换作者之前**检查，单个作者内部完全没有配额上限。8-27 21:04 那轮的实况：

```
21:04:21  枚举关注列表第 1 页 …… 第 11 页（12 次 30 秒页间隔 = 6 分钟）
21:09:23  开始同步作者 43158123（该作者本地已有 109 篇）
21:10:05  第 1 篇（+42s）
   ……     连续 60 篇，每篇间隔约 51 秒
22:01:06  撞满 max_pages_per_run=2 → 该作者结束
22:01:06  synced_items(60) >= max_items_per_run(20) → 整轮结束
          Following rotation: synced 1/256 users this run
```

照此速度轮完 256 人需 **95 天**。

#### 缺陷 B：单篇小说耗时 51 秒，其中 40 秒是 FTS 写入

生产实测 Pixiv API 延迟（同一台服务器直连）：

| 调用 | 延迟 |
|---|---|
| `novel_detail` | 0.06–0.13s |
| `webview_novel` | 0.14–0.17s |
| `user_novels`（一页） | 0.13s |

即 API 部分仅 0.24s。配置的 `delay_seconds_between_items: 10.0` 是 10s。那么剩下 **40 秒**在哪？

实测 `storage/novels.py:380 replace_fts()`：

```
replace_fts(83549 字正文):        39.95s
  拆解：DELETE FROM novel_fts WHERE novel_id = ?   39.17s
        SELECT COUNT(*) FROM novel_fts WHERE novel_id=?  38.89s
        SELECT ... WHERE rowid=?                   0.0002s
```

原因：`novel_fts` 建表时把 `novel_id` 声明为 `UNINDEXED`（`storage/schema.py:91`），而 `rowid` 是自增值，与 `novel_id` **完全不对应**（实测 `rowid=12 ↔ novel_id=25310744`，7627 行中 `rowid==novel_id` 的有 **0** 行）。因此每次 `WHERE novel_id = ?` 都要全表扫描 FTS 的 496 MB `novel_fts_content` + 554 MB `novel_fts_data`。

这不只影响写入：`storage/bookmarks.py:24`、`novels.py:413`、`series.py:155` 的搜索都走 `novel_id IN (SELECT novel_id FROM novel_fts WHERE novel_fts MATCH ?)`，实测 `SELECT novel_id ... MATCH` 0.22s 而 `SELECT rowid ... MATCH` 0.00s——搜索也在为此付代价。

**这是本轮最高杠杆的单点修复**：修掉它，`following_novels` 单篇从 51s 降到约 11s（10s 配置延迟 + 1s 实际工作），单轮吞吐提升 4.6 倍，且**不增加任何 Pixiv 请求**。

#### 缺陷 C：`subscribed_series` 连续 12 轮零产出，同时有缺章

12 轮全部 `series_synced: 0, novels: 0, skipped: 2150`——每轮 13 分钟只是在确认 2150 章都还在。但库里同时存在：

- 8 个订阅系列缺 76 章
- 每轮 `truncated_series: 2`——2 个系列被 `max_pages_per_run=2` 卡住取不完

即：高频跑却补不齐，因为分页上限锁死了。

### 2.5 其它实测事实

- **`user_status` 有 6 个用户恒定返回「您的访问权限已经被限制了」**（73342541 / 127445288 / 59683986 / 86295739 / 13766533 / 37152734）。这是账号级权限限制而非限流，三态判定正确地留在 `unknown`，但它们会持续消耗熔断计数额度。
- **`novel_status` 每轮 800 篇 / 26 分钟**，全库 7635 篇轮完需 2.4 天。`remaining` 长期在 6636–6835 波动，说明轮转正常工作。
- **`user_backup` 与 `following_novels` 共用 `auto_sync_following_novels_users_limit`**（`jobs/quick_sync.py:110`）——改后者会连带改前者的批量大小，这是一个隐藏耦合。
- **偏好分析已完成 6957/7635 篇**（`preference_analyzed_novels`），默认画像存在；`recommendation_runs` 为 0（推荐从未跑过）。
- **AI 侧**：3 个 provider、25 个模型、**0 个模型池**、16 个 agent、1 个项目、25 章。模型池为空意味着所有 agent 都是 fixed 绑定。
- **磁盘**：45 GB 可用，DB 2.32 GB（`novel_texts` 984 MB + `novel_fts_*` 1054 MB）。FTS 索引比原文还大。

## 3. 设计总览

分三个阶段，按杠杆率排序。阶段一与阶段二涉及同步侧，阶段三涉及前端，彼此无代码耦合，可分批上线。

| 阶段 | 内容 | 为什么排这个顺序 |
|---|---|---|
| **一. 吞吐修复**（§4） | FTS rowid 修复、每作者配额、系列独立分页上限 | 不改频率就能拿到 4.6 倍吞吐；跳过这步去调频率只是在给低效任务多分预算 |
| **二. 预算重排**（§5） | cron 重排、分级限速铺路、优先级实际生效 | 依赖阶段一的新耗时基线，否则算出来的预算表立刻过期 |
| **三. 页面重构**（§6） | 设置页拆分、AI 公共层、模型路由可视化 | 与前两阶段无代码耦合；调度页要展示阶段二的新预算模型 |

---

## 4. 阶段一：吞吐修复

### 4.1 修复 `novel_fts` 的 rowid 语义（最高杠杆）

**改法**：让 FTS 的 `rowid` 等于 `novel_id`，所有按 ID 的读写改走 `rowid`。

`storage/novels.py`：

```python
def replace_fts(self, novel_id: int, title: str, caption: str, author_name: str, body: str) -> None:
    with self.transaction():
        # 必须用 rowid：novel_id 是 UNINDEXED 列，WHERE novel_id = ? 会全表扫描
        # 1 GB 的 FTS 索引（生产实测单次 39s）。rowid 是 FTS5 的主键，O(1)。
        self.conn.execute("DELETE FROM novel_fts WHERE rowid = ?", (novel_id,))
        self.conn.execute(
            "INSERT INTO novel_fts (rowid, novel_id, title, caption, author_name, body) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (novel_id, novel_id, title, caption, author_name, body),
        )
```

同样改造 `novels.py:350`（删除小说时清 FTS）、`users.py:326`（删除用户时清 FTS，改成 `WHERE rowid IN (SELECT novel_id FROM novels WHERE user_id = ?)`）。

搜索侧三处（`bookmarks.py:24`、`novels.py:413`、`series.py:155`）把 `SELECT novel_id FROM novel_fts WHERE novel_fts MATCH ?` 改为 `SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?`。保留 `novel_id` 列不动——删掉它要改表结构，而 `MATCH` 语义不受影响，收益却只有几 MB。

**迁移**：现存 7627 行的 `rowid` 全部错位，必须重建。放在 `storage/schema.py` 的 `_migrate_*` 里，遵循现有的幂等 DDL 约定：

```python
# 探测：取一行比对 rowid 与 novel_id 是否一致（生产实测 0.01s，非全表扫描）
row = conn.execute("SELECT rowid, novel_id FROM novel_fts LIMIT 1").fetchone()
if row is not None and int(row[0]) != int(row[1]):
    # 重建：DROP + CREATE + 从 novels/novel_texts 回填，显式指定 rowid
```

生产实测重建耗时：取 2000 行样本（33 MB 正文，占全库 168 MB 文本的 19.7%）用 24.5s，**按字节量外推全量约 124 秒（2.1 分钟）**。这段时间发生在 `init_schema()` 内，即服务启动时；`create_app` 本来就在启动期做 `fail_stale_task_logs()`。两分钟的启动延迟可接受，但**必须在日志里输出重建进度**，否则运维会以为服务卡死。若实测超过 5 分钟，改为后台线程重建 + 期间搜索降级（此为备选方案，不作为首选）。

**验证**（生产 scratch 库实测已确认）：

| 操作 | 修复前 | 修复后 |
|---|---|---|
| `DELETE` 单行 | 39.17s | 0.11s |
| 按 ID 查询 | 38.89s | 0.0002s |
| `MATCH` 搜索 | 0.22s | 0.00s |
| `MATCH` 结果正确性 | ✓ | ✓（已验证） |

### 4.2 `following_novels` 每作者独立配额

**改法**：新增 `sync.following_max_novels_per_author`（默认 20），在 `_sync_author` 内部生效；`max_items_per_run` 降级为全局兜底。

`sync_engine.py:_sync_author` 里加一个作者级计数器，达到上限就跳出该作者的翻页循环（而不是结束整轮）；`sync_engine.py:1040` 的作者循环判据从「`synced_items >= max_items`」改为「跑满 `users_limit` 个作者」，`max_items_per_run` 仅作为异常情况下的整轮硬顶（取一个明显更大的值，如 `users_limit × following_max_novels_per_author × 1.5`）。

**效果推算**（基于 §4.1 修复后的 11s/篇）：

- 每轮：枚举关注列表 6 分钟 + 5 个作者 × 最多 20 篇 × 11s ≈ **24 分钟**（当前 42 分钟）
- 全圈 256 人：256 / 5 = 51 轮，按每天 4 轮 → **12.8 天**（当前 95 天）

注意 `user_backup` 复用同一个 `users_limit`（`jobs/quick_sync.py:110`）。本阶段不解耦，但要在设置页把这个复用关系写清楚；如需解耦另开 `auto_sync_user_backup_users_limit`，属于后续项。

### 4.3 系列独立分页上限

**改法**：仿照已有的 `_resolve_bookmark_max_pages`（`sync_engine.py:96`）新增 `_resolve_series_max_pages`，读 `sync.series_max_pages_per_run`（默认 10），缺省回落 `max_pages_per_run`。作用于 `sync_engine.py:1402` 的 `series_safety_limit`。

**效果**：清掉每轮 `truncated_series: 2`，把 8 个系列的 76 缺章补齐。补齐后这些系列回归「全跳过」状态，不再产生额外请求。

---

## 5. 阶段二：预算重排

### 5.1 优先级模型（保持现状，补文档与可视化）

`SCHEDULER_TASK_CONFIGS` 的分级已经符合用户诉求，**不改代码**：

| P | 任务 | 可让位 | 依据 |
|---|---|---|---|
| 1 | bookmarks | ✗ | 用户手动点赞收藏，时效性最高；单轮仅 3 分钟，无需让位 |
| 2 | subscribed_series | ✗ | 用户主动追更；每轮从 watchlist 头部重走，打断等于白跑 |
| 3 | 其余 9 个 | 见下 | — |

`preemptible=True` 只给按水位续跑的任务：`following_novels`（`user_last_synced`）、三个 status 检查（`last_checked_at`）、`user_backup`（`offset`）、`preference_analyze`（累加器）。`following_list` / `pending_deletion_detection` 本身只跑几十秒到几分钟；`recommendation_run` 跑一半没有意义。

让位护栏（`_may_preempt`）保持：冷却 6h、连续让位上限 2 次、让位后 600s 短退避续跑。

### 5.2 新 cron 排布

设计约束：

1. P1 收藏每天 6 次，均匀分布，任何时刻最多等 4 小时。
2. 长任务（`following_novels` 24 分钟、`novel_status` 26 分钟）避开收藏时刻 ±30 分钟，减少无谓让位。
3. 时效性弱的任务拉到隔日或隔三日。
4. 让优先级机制真正有机会生效：保留少量刻意重叠，验证让位路径不是死代码。

时区沿用 `Asia/Seoul`。

| 任务 | 现 cron | 新 cron | 频率变化 | 理由 |
|---|---|---|---|---|
| bookmarks | `20 0,4,8,12,16,20 * * *` | `20 0,4,8,12,16,20 * * *` | 不变（6 次/天） | P1，已是理想排布 |
| subscribed_series | `40 1,7,13,19 * * *` | `40 1,13 * * *` | 4 次 → **2 次/天** | 12 轮零产出；配合 §4.3 补齐缺章后更无需高频 |
| following_novels | `0 3,9,15,21 * * *` | `0 3,9,15,21 * * *` | 不变（4 次/天） | 单轮从 42 分钟降到 24 分钟，同频率下覆盖率提升 5 倍 |
| novel_status | `0 5,11,17,23 * * *` | `0 5,17 * * *` | 4 次 → **2 次/天** | 最大预算消费者却最不紧急；轮转周期 2.4 天 → 4.8 天 |
| series_status | `30 18 */2 * *` | `30 18 */2 * *` | 不变（隔日） | 273 个系列每轮跑完，无积压 |
| user_status | `30 6 */2 * *` | `30 6 */2 * *` | 不变（隔日） | 298 人每轮跑完 |
| following_list | `30 10 * * *` | `30 10 * * *` | 不变（每天） | 5 分钟，是 following_novels 的输入 |
| user_backup | `30 2 */3 * *` | `30 2 */3 * *` | 不变（隔三日） | 全量兜底，最弱时效 |
| pending_deletion_detection | `30 12 * * *` | `30 12 * * *` | 不变（每天） | 34 秒 |
| preference_analyze | interval 12h | `15 7,19 * * *` | 显式 cron | 从未运行过；纯本地计算，不耗 Pixiv 配额 |
| recommendation_run | interval 3h | `50 8 * * *` | 3h → **每天 1 次** | 消耗 Pixiv 搜索配额；3 小时一次过于激进 |

### 5.3 预算对比

以阶段一完成后的耗时基线计算（`following_novels` 24 分钟、`subscribed_series` 13 分钟不变、其余不变）：

| 任务 | 现状 秒/天 | 新方案 秒/天 | 变化 |
|---|---|---|---|
| following_novels | 6717 | 5760 | −14%（但覆盖率 ×5） |
| novel_status | 5775 | 3150 | −45% |
| subscribed_series | 3068 | 1534 | −50% |
| bookmarks | 415 | 1068 | +157%（6 次/天全跑，不再 partial） |
| user_backup | 643 | 322 | — |
| series_status | 587 | 294 | — |
| user_status | 267 | 200 | — |
| following_list | 203 | 304 | — |
| pending_deletion | 57 | 34 | — |
| preference_analyze | 0 | ~600 | 新增（纯本地，不耗 Pixiv 配额） |
| recommendation_run | 0 | ~300 | 新增 |
| **合计** | **17732s ≈ 4.9h** | **13565s ≈ 3.8h** | **−24%** |

占空比从 20.5% 降到 15.7%，同时 `following_novels` 的全圈周期从 95 天降到 **12.8 天**。省出的预算是真实余量，不再投入——若后续 Pixiv 真的开始限流，这段余量就是缓冲。

### 5.4 分级限速（本阶段只铺路，不改数值）

现状是一刀切：`delay_seconds_between_items: 10.0` / `between_pages: 30.0` / `between_series: 10.0` / `between_chapters: 15.0` / `between_skips: 2.0`。收藏（最重要、量最小）与巡检（最不重要、量最大）用同一套保守参数。

既然确认无真实限流，且 §4.1 消除了 40 秒的本地开销（此前的「保守」有一部分实际上是在为 FTS 全表扫描买单），分级是合理的。但**本阶段不改这些默认值**：§4.1 会显著改变单篇耗时，应先上线观察一周真实基线，再基于新数据调整。本阶段要做的只是**让分级成为可能**——设置页把这 5 个延迟参数按「收藏 / 关注作者 / 系列 / 巡检」分组展示，而非现在的扁平列表。

具体数值调整列为后续项，需要新一周的日志支撑。

### 5.5 顺带修复

- **6 个恒定「访问权限被限制」的用户**：`user_status` 每轮都会对它们发请求并计入 `consecutive_unknown`。设计上给 `users` 表加一个「已知受限」标记，连续 N 轮 unknown 后降频巡检（如隔周一次），避免它们持续挤占熔断额度。这一项独立于优先级，但与「减少无谓请求」同源。
- **已核对无需修复**：11 个 scheduler task 在 `web/utils.py:_job_spec`、`jobs/tasks.py:_TASK_LABELS`、`web/managers.py:TASK_LABELS` 三处注册表中全部齐全，`job_type` 映射也都正确（四个同步类任务落到 `JobType.SYNC` 是设计意图，不是漏分支）。已用脚本逐项验证，此处不需要改动，但新增 task_type 时仍须遵守 `docs/JOB_SYSTEM.md` §4 的四处注册清单。

---

## 6. 阶段三：设置页与 AI 页面重构

### 6.1 现状问题

**设置页**（`dashboard_settings.html`，1817 行，内联 `<script>` 1030 行）：

- 9 个分区靠 `v-show` 全量挂载在一棵 DOM 树里，`onMounted` 无条件触发 8 个加载器（含 per-provider 模型目录加载两遍）。
- 三个「保存」按钮都调 `saveSettings(section)`，但**`section` 参数从未被读取**——三者都 POST 整份 `settings` 到 `/api/dashboard/settings`，后端 `save_sync_settings` 重写整个 `sync:` YAML 块。
- 一个 `setup()` 持有约 60 个 ref、返回约 100 个键。
- `#ai-agents` 分区（556–755 行）实际是四个应用拼在一起：普通 agent CRUD、成人润色 agent、按项目 ID 手输的角色管理、两个 review binding。
- cron 字段是裸文本框，只有 placeholder，无校验无预览。
- 手动触发的「同步追更系列」按钮 POST `/api/dashboard/sync/subscribed_series`，而 `webapp.py:1367` 的 `task_map` **没有这个键**（真实路由是连字符的 `/api/dashboard/sync/subscribed-series`）→ 必然 400。
- 无手动入口的任务：`user_backup`、`pending_deletion_detection`、`preference_analyze`、`recommendation_run`。
- `providerForm.timeout_seconds` / `max_retries` 有字段无输入框；`pending_deletion_grace_period_days` 等后端返回但页面无控件。

**AI 页面**：

- `dashboard_ai.html` markup 838 行 + 内联脚本 **1194 行**（约 64 KB）在单个 `setup()` 内。
- `base.html:203-220` 已提供全站 `window.csrfFetch` / `ensureCsrfToken` / `errorText`（这正是 `fb91da3` 修 12 处 CSRF 漏洞时加的），但 **4 处各自重写了一遍 csrfFetch**（`dashboard_ai.html:1019`、`dashboard_wizard.html:215`、`dashboard_ai_reader.html:314`、`dashboard_settings.html:1088`）。
- **SSE 解析重复 7 遍**（`dashboard_ai.html` 内 5 处 + wizard + reader）。
- `window.errorText` 无人使用，所有 AI 页面只读 `data.error`，于是 `detail` 形状的失败一律显示「请求失败」。
- `agents`/`styleProfiles`/`novelProfiles`/`preferenceProfiles` 及其加载器在 ai 页与 wizard 页各存一份。
- **无候选模型预览**：配完 agent 看不出实际会依次调用哪些模型。`ModelRouter.resolve_candidates`（`ai/model_router.py:636`）和 `GET /api/dashboard/ai/model-pools/<id>/attempts`（`ai_web.py:895`）都已存在，**但没有任何模板调用它们**。真实候选顺序只能在任务跑完后去 `/dashboard/logs` 事后看。

### 6.2 设置页拆成四个一级页面

沿用 `vue_components.html:NAV_ITEMS` 的侧栏机制，在 Operations 分组下把「设置」展开为：

| 路由 | 标题 | 内容 | 独立保存端点 |
|---|---|---|---|
| `/dashboard/settings/sync` | 同步与调度 | 内容开关、限速分页（按收藏/关注/系列/巡检分组）、定时任务表、手动触发 | `PUT /api/dashboard/settings/sync` |
| `/dashboard/settings/models` | AI Provider 与模型 | Provider CRUD、模型目录同步、模型池 | 沿用现有 `ai_web.py` 端点 |
| `/dashboard/settings/agents` | AI Agent 绑定 | 普通 agent + 候选链预览 | 沿用现有端点 |
| `/dashboard/settings/system` | 系统维护 | 图片缓存、救援 API、导出、待删除保留期 | `PUT /api/dashboard/settings/system` |

成人润色配置（现 `#ai-agents` 底部 658–755 行）独立成 `/dashboard/settings/adult`，并把「手输项目 ID」改成项目下拉选择。

**保存语义改造**：`save_sync_settings` 当前是「读整份 payload、逐字段 `payload.get(k, 既有值)`、重写整个 `sync:` 块」。改造为接受**分区化 payload**：新增 `SETTINGS_SECTIONS` 声明每个分区包含哪些字段，`save_sync_settings(payload, section=None)` 在给定 section 时只校验并写入该分区字段，其余字段原样保留。`section=None` 保持现有全量行为，向后兼容 CLI 与既有测试。

**调度任务表**要展示阶段二的预算模型——这是用户最需要看见的东西：每个任务的优先级、是否可让位、下次运行时间（已有 `/api/dashboard/auto-sync/status` 提供）、**上一轮实际耗时**与**预估每日预算占用**（从 `task_logs` 聚合）。cron 字段配校验 + 「下次 5 次运行时间」预览，复用后端 `settings.py:cron_to_next_run`。

**同时修掉**：`subscribed_series` 手动按钮的 400、补齐 4 个缺失的手动入口、给 `providerForm` 的两个孤立字段加控件。

### 6.3 AI 页面：先抽公共层，再做模型路由可视化

**公共层**（新建 `templates/dashboard_shared.html`，或扩充 `base.html`）：

- 删除 4 处自建 `csrfFetch`，统一用 `window.csrfFetch`。
- 错误处理统一走 `window.errorText`（读 `detail` / `error` / `message` 三种字段）。
- 抽出 `window.streamSSE(url, options, handlers)` 替换 7 处重复的 SSE 解析。
- 抽出共享的 profile/agent 加载器，ai 页与 wizard 页共用一份状态。

这一步是纯重构，不改行为，可用现有测试（`test_ai_page_routes.py`、`test_frontend_library_os.py`、`test_preference_csrf.py`、`test_settings_save_csrf.py`）守住。

**模型路由可视化**（本项最高价值）：

新增 `GET /api/dashboard/ai/agents/<id>/candidates`，内部调 `ModelRouter.resolve_candidates(agent, stage="main")`，返回解析后的候选链（provider 名、模型 key、顺序、来源是 fixed 还是哪个池节点），**不发起任何真实生成请求**。在 agent 配置界面直接渲染：

```
Agent「章节续写」→ 池「主力池」
  ① openai-compat / deepseek-v3        （池成员 1）
  ② anthropic / claude-sonnet-5        （池成员 2）
  ③ xai / grok-4                       （后备池「兜底」成员 1）
  上限：16 次候选尝试 / 32 次网络请求 / 30 分钟
```

同时把已存在但无人调用的 `GET /api/dashboard/ai/model-pools/<id>/attempts` 接到池编辑界面，展示该池最近的真实尝试记录（哪个候选成功、哪些失败、失败原因）。

注意生产实测 **0 个模型池、16 个 agent 全是 fixed 绑定**——所以候选链预览对当前配置显示的是单元素链。这仍然有价值（它明确回答了「这个 agent 到底会调哪个模型」），但池相关的 UI 需要先能被验证，实施时要在测试里构造池数据。

`/dashboard/ai` 自身的三层嵌套 tab 与 pipeline 弹窗重排**不在本轮范围**——它与调度改动无关，且体量足以单开一份设计。

---

## 7. 涉及文件与测试

### 阶段一（§4）

| 文件 | 改动 |
|---|---|
| `storage/novels.py` | `replace_fts` / `delete_novel` 改走 rowid；搜索 SQL 改 `SELECT rowid` |
| `storage/users.py` | 删除用户时清 FTS 改 rowid |
| `storage/bookmarks.py`、`storage/series.py` | 搜索 SQL 改 `SELECT rowid` |
| `storage/schema.py` | 新增幂等 FTS 重建迁移（探测 rowid 错位 → DROP/CREATE/回填） |
| `sync_engine.py` | `_sync_author` 加作者级配额；作者循环判据改「跑满 users_limit」；新增 `_resolve_series_max_pages` |
| `settings.py` | 新增 `following_max_novels_per_author`、`series_max_pages_per_run` |
| `web/managers.py` | `save_sync_settings` 支持两个新字段 |
| `web/utils.py` | `_settings_to_dict` 暴露两个新字段 |
| `config/config.yaml.example` | 两个新字段 + 注释 |

新增测试：

- `test_storage_db.py`：FTS rowid 等于 novel_id；`replace_fts` 后 `MATCH` 仍命中；删除小说/用户后 FTS 行消失；迁移在错位数据上重建、在正确数据上是 no-op（幂等）。
- `test_sync_engine_incremental.py`：单作者达到 `following_max_novels_per_author` 后跳到下一个作者而非结束整轮；`users_limit` 个作者全部被访问；`series_max_pages_per_run` 生效且缺省回落 `max_pages_per_run`。
- `test_webapp_settings.py`：两个新字段 round-trip。

### 阶段二（§5）

| 文件 | 改动 |
|---|---|
| 生产 `config/config.yaml` | 新 cron 排布（不进仓库） |
| `config/config.yaml.example` | 同步新的推荐 cron 与注释 |
| `settings.py` | `preference_analyze` / `recommendation_run` 的默认 cron |
| `storage/users.py` | 「已知受限」标记与降频巡检 |
| `storage/schema.py` | `users` 表新增受限标记列（幂等 `ALTER TABLE`） |
| `jobs/services.py` | `user_status` 跳过/降频受限用户 |
| `docs/JOB_SYSTEM.md` | §5 配置矩阵更新为新 cron 与新字段 |

新增测试：

- `test_status_check_classification.py`：受限用户被降频，且不计入 `consecutive_unknown`。
- `test_cron_validation.py`：新 cron 全部可解析。
- `test_scheduler_priority.py`：补一条断言，锁住「11 个 scheduler task 在三处注册表齐全」这一已核对结论，防止后续新增任务时回归。

### 阶段三（§6）

| 文件 | 改动 |
|---|---|
| `templates/dashboard_settings*.html` | 拆成 4–5 个模板 |
| `templates/base.html` 或新 `dashboard_shared.html` | `streamSSE` 等公共层 |
| `templates/dashboard_ai.html`、`dashboard_wizard.html`、`dashboard_ai_reader.html` | 删除自建 csrfFetch/SSE，改用公共层 |
| `webapp.py` | 新设置页路由 + 分区保存端点 |
| `ai_web.py` | 新增 `GET .../agents/<id>/candidates` |
| `web/managers.py` | `save_sync_settings` 支持 `section` |
| `docs/frontend-pages.md`、`docs/frontend-api-contract.md` | 路由与端点更新 |

新增测试：`test_settings_sections.py`（分区保存只改本区字段）、`test_ai_agent_candidates.py`（候选链端点不发真实请求）、`test_frontend_library_os.py` 扩充（新模板遵循 library-* 约定）、`test_ai_model_ui.py` 扩充。

## 8. 风险与回滚

| 风险 | 缓解 |
|---|---|
| FTS 重建期间服务不可用约 2 分钟 | 迁移在 `init_schema()` 内，即启动期；日志输出进度；`update.sh` 重启本来就有停机窗口。超过 5 分钟则切备选方案（后台重建 + 期间搜索降级） |
| FTS 重建中断导致索引缺失 | 重建包在单个事务里（`BEGIN IMMEDIATE`）；失败则回滚，下次启动重试。搜索在索引缺失时退化为无结果而非报错 |
| 每作者配额改动引入无限循环 | `safety_limit` 页数兜底保留；新增测试覆盖「作者作品数 > 配额」与「< 配额」两种边界 |
| 降低 `novel_status` 频率延迟发现删除 | 轮转周期 2.4 → 4.8 天。`pending_deletion_detection` 仍每天跑，用户主动取消收藏/追更的检测不受影响 |
| 设置页拆分破坏现有保存 | `section=None` 保持全量行为；先上线分区端点并与旧端点并存，前端切换后再考虑废弃 |
| 公共层重构影响面广 | 纯重构不改行为；分模板逐个切换，每切一个跑一次相关测试 |

回滚：阶段一的 FTS 改动一旦部署无法简单回退（rowid 已重建），但**旧代码在新 rowid 上仍能正确工作**（`WHERE novel_id = ?` 只是慢，不是错），所以代码可回滚、数据不必回滚。阶段二全是配置，改回旧 cron 即可。阶段三按模板粒度回滚。

## 9. 明确不在本轮范围

- `/dashboard/ai` 的三层嵌套 tab 与 pipeline 弹窗重排。
- 限速参数具体数值调整（需阶段一上线后一周的新基线）。
- `user_backup` 与 `following_novels` 的 `users_limit` 解耦。
- 删除 `novel_fts.novel_id` 冗余列。
- 推荐系统功能补全（`docs/superpowers/plans/2026-08-14-recommendation-completion.md` 仍未实施）。


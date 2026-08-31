# 阶段二：调度预算重排 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按实测预算重排 11 个定时任务的 cron，并给「已知受限用户」降频巡检，让 P1/P2 优先级机制真正有机会生效。

**Architecture:** 本阶段以配置为主、代码为辅。cron 排布改 `config/config.yaml.example` 与生产 `config/config.yaml`；代码改动只有两处：`settings.py` 两个默认 cron，以及 `users` 表新增「受限」标记 + `user_status` 巡检降频。优先级配置（`SCHEDULER_TASK_CONFIGS`）**不改**——实测其分级已符合需求，缺的只是让它有机会触发。

**Tech Stack:** Python 3.10+、SQLite、croniter、pytest

**Spec:** `docs/superpowers/specs/2026-08-28-sync-budget-and-settings-redesign-design.md`（§5）

**依赖：** 本阶段的预算数字建立在阶段一（`docs/superpowers/plans/2026-08-28-phase1-sync-throughput.md`）完成后的耗时基线上。**必须先完成阶段一并在生产观察至少 24 小时**，再执行本阶段。

## Global Constraints

- 模块首行 `from __future__ import annotations`；dataclass 用 `slots=True`。
- 代码注释与用户可见字符串用中文。
- 提交信息遵循 `type: subject`（Conventional Commits）。
- 时区固定 `Asia/Seoul`（生产现值，勿改）。
- 迁移必须幂等：`PRAGMA table_info` 守卫 + `ALTER TABLE ... ADD COLUMN`。
- `pytest` 跑全量约 6 分钟；单文件用 `pytest tests/xxx.py -v`。
- **不改 `SCHEDULER_TASK_CONFIGS` 的 priority / preemptible 字段。**

---

## 任务总览

| Task | 内容 | 可独立上线 |
|---|---|---|
| 1 | `users` 表新增 `restricted_streak` 列 + 受限用户降频巡检 | ✓ |
| 2 | `preference_analyze` / `recommendation_run` 默认 cron 改为显式值 | ✓ |
| 3 | `config.yaml.example` 新 cron 排布 + 预算注释 | ✓ |
| 4 | 锁住「11 个 task 三处注册表齐全」的防回归断言 | ✓ |
| 5 | 更新 `docs/JOB_SYSTEM.md` §5 配置矩阵 | ✓ |
| 6 | 生产 cron 灰度上线与验证（手工步骤） | — |

---

### Task 1: 受限用户降频巡检

**背景（spec §2.5）：** 生产有 6 个用户（73342541 / 127445288 / 59683986 / 86295739 / 13766533 / 37152734）每轮都返回「您的访问权限已经被限制了」。三态判定正确地留在 `unknown`（不误标删除），但它们每轮都消耗 `consecutive_unknown` 熔断额度（上限 15），且每轮浪费 6 次 API 请求。

**设计：** 给 `users` 表加 `restricted_streak INTEGER NOT NULL DEFAULT 0`。`unknown` 时 +1，其它状态归零。`restricted_streak >= 3` 的用户降频到每 7 天才巡检一次。

**Files:**
- Modify: `src/pixiv_novel_sync/storage/schema.py`（`_migrate_users_table`，第 267 行）
- Modify: `src/pixiv_novel_sync/storage/users.py`（`upsert_user_status` 第 46 行、`get_users_for_status_check` 第 ~40 行）
- Test: `tests/test_status_check_classification.py`

**Interfaces:**
- Produces:
  - `users.restricted_streak` 列（INTEGER NOT NULL DEFAULT 0）
  - `Database.upsert_user_status(user_id: int, status: str) -> None`（签名不变，行为新增维护 streak）
  - `Database.get_users_for_status_check(limit: int | None = None) -> list[dict]`（签名不变，新增跳过近期已查的受限用户）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_status_check_classification.py` 末尾：

```python
# ── 受限用户降频巡检 ──────────────────────────────────────────

from pixiv_novel_sync.models import UserRecord
from pixiv_novel_sync.storage_db import Database


def test_unknown_status_increments_restricted_streak(tmp_path):
    """连续 unknown 必须累加 restricted_streak，其它状态归零。

    生产实测 6 个用户恒定返回「您的访问权限已经被限制了」，每轮都吃掉
    consecutive_unknown 熔断额度。先要能识别出「这个用户一直查不出来」。
    """
    db = Database(tmp_path / "streak.db")
    db.init_schema()
    db.upsert_user(UserRecord(user_id=7, name="受限作者", account="a", raw_json="{}"))

    for _ in range(3):
        db.upsert_user_status(7, "unknown")
    row = db.conn.execute("SELECT restricted_streak FROM users WHERE user_id = 7").fetchone()
    assert row[0] == 3

    # 一旦查出确定结果，streak 必须归零
    db.upsert_user_status(7, "normal")
    row = db.conn.execute("SELECT restricted_streak FROM users WHERE user_id = 7").fetchone()
    assert row[0] == 0
    db.close()


def test_restricted_users_are_polled_at_most_weekly(tmp_path):
    """restricted_streak >= 3 的用户在 7 天内不再进入巡检清单。"""
    db = Database(tmp_path / "skip.db")
    db.init_schema()
    db.upsert_user(UserRecord(user_id=1, name="正常", account="a", raw_json="{}"))
    db.upsert_user(UserRecord(user_id=2, name="受限", account="b", raw_json="{}"))

    # 用户 2 连续 3 次 unknown → 进入降频档，且 last_checked_at 为刚刚
    for _ in range(3):
        db.upsert_user_status(2, "unknown")
    db.upsert_user_status(1, "normal")

    ids = [u["user_id"] for u in db.get_users_for_status_check()]
    assert 1 in ids
    assert 2 not in ids  # 刚查过且受限 → 本轮跳过

    # 把 last_checked_at 推到 8 天前 → 重新纳入
    db.conn.execute(
        "UPDATE users SET last_checked_at = datetime('now', '-8 days') WHERE user_id = 2"
    )
    db.conn.commit()
    ids = [u["user_id"] for u in db.get_users_for_status_check()]
    assert 2 in ids
    db.close()


def test_restricted_streak_migration_is_idempotent(tmp_path):
    """旧库没有 restricted_streak 列时补列；已有则不重复加。"""
    import sqlite3

    db_path = tmp_path / "legacy-users.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                account TEXT,
                raw_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',
                last_checked_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO users (user_id, name, raw_json) VALUES (5, '旧作者', '{}');
            """
        )

    db = Database(db_path)
    db.init_schema()
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(users)").fetchall()}
    assert "restricted_streak" in cols
    row = db.conn.execute("SELECT restricted_streak FROM users WHERE user_id = 5").fetchone()
    assert row[0] == 0
    db.close()

    # 二次 init_schema 不报错（幂等）
    db2 = Database(db_path)
    db2.init_schema()
    db2.close()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_status_check_classification.py -k "restricted_streak or weekly" -v
```

预期：FAIL，`sqlite3.OperationalError: no such column: restricted_streak`

- [ ] **Step 3: 加迁移**

`storage/schema.py` 的 `_migrate_users_table`（第 267 行）末尾追加：

```python
    def _migrate_users_table(self) -> None:
        """为旧版 users 表添加 status 和 last_checked_at 字段"""
        cursor = self.conn.execute("PRAGMA table_info(users)")
        columns = {row[1] for row in cursor.fetchall()}
        if "status" not in columns:
            self.conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown'")
        if "last_checked_at" not in columns:
            self.conn.execute("ALTER TABLE users ADD COLUMN last_checked_at TEXT")
        if "restricted_streak" not in columns:
            # 连续判不出状态的次数。生产有 6 个账号恒定返回「您的访问权限已经被限制了」，
            # 三态判定正确地留在 unknown（不会误标删除），但它们每轮都白占一次 API 请求
            # 和一格 consecutive_unknown 熔断额度（上限 15）。累计到阈值后降频巡检。
            self.conn.execute(
                "ALTER TABLE users ADD COLUMN restricted_streak INTEGER NOT NULL DEFAULT 0"
            )
```

同时把主 `init_schema()` 的 `CREATE TABLE IF NOT EXISTS users` 补上该列（新库直接带上）：

```python
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                account TEXT,
                raw_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',
                last_checked_at TEXT,
                restricted_streak INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
```

- [ ] **Step 4: 维护 streak**

`storage/users.py` 的 `upsert_user_status`（第 46 行）改为：

```python
    def upsert_user_status(self, user_id: int, status: str) -> None:
        """更新用户状态；status 为 "unknown" 时只刷新 last_checked_at，不改写 status。

        同时维护 restricted_streak：unknown 累加，其它状态归零。见
        get_users_for_status_check 里的降频逻辑。
        """
        with self._lock:
            if status == UNKNOWN_STATUS:
                self.conn.execute(
                    "UPDATE users SET last_checked_at = CURRENT_TIMESTAMP, "
                    "restricted_streak = restricted_streak + 1 WHERE user_id = ?",
                    (user_id,),
                )
            else:
                self.conn.execute(
                    "UPDATE users SET status = ?, last_checked_at = CURRENT_TIMESTAMP, "
                    "restricted_streak = 0 WHERE user_id = ?",
                    (status, user_id),
                )
            self._commit_if_needed()
```

- [ ] **Step 5: 巡检清单跳过近期已查的受限用户**

`storage/users.py` 的 `get_users_for_status_check` 改为（保留原 docstring，追加降频说明）：

```python
    # 连续多少轮判不出状态就算「已知受限」，之后降频巡检
    RESTRICTED_STREAK_THRESHOLD = 3
    # 已知受限用户的巡检间隔（天）
    RESTRICTED_RECHECK_DAYS = 7

    def get_users_for_status_check(self, limit: int | None = None) -> list[dict[str, Any]]:
        """按 last_checked_at 升序返回待状态检查的用户（从未检查过的排最前）。

        必须与 ``get_novel_ids_for_status_check`` 同一套轮转语义，不能复用
        ``list_users``：后者是给列表页用的，排序是 ``status 分桶 + updated_at DESC``，
        与"上次什么时候检查过"无关，因此每轮顺序完全固定。生产事故：队尾连续 5 个
        用户状态判不出来触发 unknown 熔断后，下一轮又从同一个固定顺序的开头跑，
        永远走不到第 194 个，实测 105/298 个用户超过 3 天从未被检查。改成按
        last_checked_at 轮转后，被熔断跳过的尾部下一轮自然排到最前面。

        另外排除「已知受限」用户：连续 RESTRICTED_STREAK_THRESHOLD 轮判不出状态的
        账号（生产实测 6 个恒定返回「您的访问权限已经被限制了」）改为每
        RESTRICTED_RECHECK_DAYS 天才查一次，避免它们每轮白占 API 请求和熔断额度。
        受限判定不是永久的：过期后重新纳入，若这次查出确定结果，streak 归零。
        """
        sql = (
            "SELECT user_id, name FROM users "
            "WHERE restricted_streak < ? "
            "   OR last_checked_at IS NULL "
            "   OR last_checked_at < datetime('now', ?) "
            # (last_checked_at IS NOT NULL) 为 0/1，保证 NULL（从未检查）永远排最前
            "ORDER BY (last_checked_at IS NOT NULL), last_checked_at, user_id"
        )
        params: tuple[Any, ...] = (
            int(self.RESTRICTED_STREAK_THRESHOLD),
            f"-{int(self.RESTRICTED_RECHECK_DAYS)} days",
        )
        if limit is not None and int(limit) > 0:
            sql += " LIMIT ?"
            params = params + (int(limit),)
        rows = self.conn.execute(sql, params).fetchall()
        return [{"user_id": row["user_id"], "name": row["name"]} for row in rows]
```

- [ ] **Step 6: 运行新测试确认通过**

```bash
pytest tests/test_status_check_classification.py -v
```

预期：全部 PASS

- [ ] **Step 7: 回归 users / 状态检查相关测试**

```bash
pytest tests/test_storage_db.py tests/test_status_check_classification.py tests/test_jobs_services.py -q
```

预期：全部通过。若 `test_jobs_services.py` 有断言依赖「全部用户都进巡检清单」，说明该测试构造的用户 `restricted_streak` 为 0（默认值），不会被跳过——如仍失败，读失败信息判断是否需要在该测试里显式设 streak。

- [ ] **Step 8: 提交**

```bash
git add src/pixiv_novel_sync/storage/schema.py src/pixiv_novel_sync/storage/users.py tests/test_status_check_classification.py
git commit -m "feat: 已知受限用户降频巡检，不再每轮白占熔断额度"
```

---

### Task 2: 两个任务的默认 cron 改为显式值

**背景（spec §5.2）：** `preference_analyze` 默认 cron 是 `*/30 * * * *`（每 30 分钟），`recommendation_run` 默认走 interval 3 小时。前者生产从未运行过，后者会消耗 Pixiv 搜索配额，3 小时一次过于激进。

**Files:**
- Modify: `src/pixiv_novel_sync/settings.py`（`SyncSettings` 第 ~60 行、`load_settings` 对应行）
- Test: `tests/test_cron_validation.py`

**Interfaces:**
- Consumes: Task 1 无依赖
- Produces: `SyncSettings.auto_sync_preference_analyze_cron` 默认 `"15 7,19 * * *"`；`SyncSettings.auto_sync_recommendation_run_cron` 默认 `"50 8 * * *"`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_cron_validation.py`：

```python
# ── 阶段二新 cron 排布 ────────────────────────────────────────

from pixiv_novel_sync.settings import SyncSettings, cron_to_next_run

# spec §5.2 的新 cron 排布。任何一条解析不了，调度器会静默回落到 interval，
# 导致「配了 cron 却按小时间隔跑」这种极难发现的偏差。
PHASE2_CRONS = {
    "bookmarks": "20 0,4,8,12,16,20 * * *",
    "subscribed_series": "40 1,13 * * *",
    "following_novels": "0 3,9,15,21 * * *",
    "novel_status": "0 5,17 * * *",
    "series_status": "30 18 */2 * *",
    "user_status": "30 6 */2 * *",
    "following_list": "30 10 * * *",
    "user_backup": "30 2 */3 * *",
    "pending_deletion_detection": "30 12 * * *",
    "preference_analyze": "15 7,19 * * *",
    "recommendation_run": "50 8 * * *",
}


def test_phase2_crons_all_parse_in_seoul():
    """新 cron 必须全部能在 Asia/Seoul 下解析出下次运行时刻。"""
    for name, expr in PHASE2_CRONS.items():
        result = cron_to_next_run(expr, None, "Asia/Seoul")
        assert result is not None, f"{name} 的 cron 无法解析: {expr!r}"


def test_preference_analyze_default_cron_is_twice_daily():
    """preference_analyze 默认 cron 从每 30 分钟改为每天两次。"""
    assert SyncSettings.auto_sync_preference_analyze_cron == "15 7,19 * * *"


def test_recommendation_run_default_cron_is_daily():
    """recommendation_run 消耗 Pixiv 搜索配额，默认降到每天一次。"""
    assert SyncSettings.auto_sync_recommendation_run_cron == "50 8 * * *"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_cron_validation.py -k "phase2 or default_cron" -v
```

预期：两个默认值断言 FAIL（当前分别是 `"*/30 * * * *"` 和 `""`）；`test_phase2_crons_all_parse_in_seoul` 应当已经 PASS（cron 表达式本身合法）。

- [ ] **Step 3: 改默认值**

`settings.py` 的 `SyncSettings` 中：

```python
    # 增量偏好分析: 少量多次分析本地归档,跳过已分析,新增自动续
    auto_sync_preference_analyze_enabled: bool = False  # 自动增量分析本地偏好
    auto_sync_preference_analyze_interval_hours: int = 12  # 分析间隔（小时）
    # 纯本地计算，不耗 Pixiv 配额；但每轮仍占用唯一 job 槽，每 30 分钟一次会频繁
    # 挤掉同步任务。改为每天两次（07:15 / 19:15），避开收藏的 0/4/8/12/16/20 点。
    auto_sync_preference_analyze_cron: str = "15 7,19 * * *"
    preference_analyze_batch_size: int = 200  # 每批分析小说数量
    # 定时生成推荐: 依赖默认偏好画像, 会消耗 Pixiv 搜索配额, 默认关闭
    auto_sync_recommendation_run_enabled: bool = False  # 自动生成推荐
    auto_sync_recommendation_run_interval_hours: int = 24  # 生成推荐间隔（小时）
    # 会消耗 Pixiv 搜索配额（一次最多 20 条检索），每天一次足够。
    auto_sync_recommendation_run_cron: str = "50 8 * * *"
```

注意 `auto_sync_preference_analyze_interval_hours` 同时从 `1` 改为 `12`——cron 非空时它只是回落值，但回落值也不该是 1 小时。

`load_settings` 中对应的默认值同步修改：

```python
            auto_sync_preference_analyze_interval_hours=_coerce_positive_int(
                sync_raw.get("auto_sync_preference_analyze_interval_hours"), 12
            ),
            auto_sync_preference_analyze_cron=str(
                sync_raw.get("auto_sync_preference_analyze_cron", "15 7,19 * * *")
            ),
```

```python
            auto_sync_recommendation_run_cron=str(
                sync_raw.get("auto_sync_recommendation_run_cron", "50 8 * * *")
            ),
```

用 `grep -n "preference_analyze_cron\|recommendation_run_cron\|preference_analyze_interval" src/pixiv_novel_sync/settings.py` 定位确切行号。

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_cron_validation.py -v
```

预期：全部 PASS

- [ ] **Step 5: 检查 save_sync_settings 的回落默认值**

`web/managers.py:save_sync_settings` 里有：

```python
sync_data["auto_sync_preference_analyze_cron"] = _save_cron("auto_sync_preference_analyze_cron", "*/30 * * * *")
```

改为：

```python
sync_data["auto_sync_preference_analyze_cron"] = _save_cron("auto_sync_preference_analyze_cron", "15 7,19 * * *")
```

同一函数里 `auto_sync_preference_analyze_interval_hours` 的 `_save_int(..., 1)` 改为 `_save_int(..., 12)`。

- [ ] **Step 6: 回归设置保存测试**

```bash
pytest tests/test_webapp_settings.py tests/test_cron_validation.py tests/test_recommendation_scheduling.py -q
```

预期：全部通过

- [ ] **Step 7: 提交**

```bash
git add src/pixiv_novel_sync/settings.py src/pixiv_novel_sync/web/managers.py tests/test_cron_validation.py
git commit -m "feat: 偏好分析与推荐任务改为显式低频 cron"
```

---

### Task 3: `config.yaml.example` 新 cron 排布

**Files:**
- Modify: `config/config.yaml.example`
- Test: `tests/test_deployment_config.py`（若存在对 example 的断言）

**Interfaces:**
- Consumes: Task 2 的新默认 cron
- Produces: 无代码接口，仅配置样例

- [ ] **Step 1: 确认 example 当前内容**

```bash
grep -n "auto_sync" config/config.yaml.example
```

- [ ] **Step 2: 写入新排布**

把 `config/config.yaml.example` 的 `sync:` 段中 auto_sync 相关部分改为：

```yaml
  # ── 定时任务排布 ─────────────────────────────────────────────
  # 单任务槽串行（BoundedSemaphore(1)），所以这里排的是「预算」而不只是「频率」。
  # 优先级在 web/managers.py:SCHEDULER_TASK_CONFIGS 里固定：收藏 P1、追更系列 P2、
  # 其余 P3；同级按逾期最久优先。详见 docs/JOB_SYSTEM.md §3.6。
  auto_sync_enabled: false
  auto_sync_timezone: Asia/Seoul

  # P1 收藏：时效性最高，每天 6 次，任何时刻最多等 4 小时。单轮约 3 分钟。
  auto_sync_bookmarks_enabled: true
  auto_sync_bookmarks_interval_hours: 4
  auto_sync_bookmarks_cron: 20 0,4,8,12,16,20 * * *

  # P2 追更系列：单轮约 13 分钟。生产实测连续 12 轮零新增，从 4 次/天降到 2 次。
  auto_sync_subscribed_series_enabled: true
  auto_sync_subscribed_series_interval_hours: 12
  auto_sync_subscribed_series_cron: 40 1,13 * * *

  # 关注用户列表：following_novels 的输入，单轮约 5 分钟。
  auto_sync_following_list_enabled: true
  auto_sync_following_list_interval_hours: 24
  auto_sync_following_list_cron: 30 10 * * *

  # 关注用户小说：最大预算项。每轮只跑 users_limit 个作者，按 user_last_synced 轮转。
  auto_sync_following_novels_enabled: true
  auto_sync_following_novels_interval_hours: 6
  auto_sync_following_novels_cron: 0 3,9,15,21 * * *
  # 每轮同步几个作者。注意 user_backup 复用同一个值（jobs/quick_sync.py）。
  auto_sync_following_novels_users_limit: 5

  # 小说状态巡检：预算第二大但时效性最弱，从 4 次/天降到 2 次（轮转周期 2.4→4.8 天）。
  # 用户主动取消收藏/追更由 pending_deletion_detection 每天检测，不受影响。
  auto_sync_novel_status_enabled: true
  auto_sync_novel_status_interval_hours: 12
  auto_sync_novel_status_cron: 0 5,17 * * *

  # 用户/系列状态巡检：各自每轮都能跑完全库，隔日足够。
  auto_sync_user_status_enabled: true
  auto_sync_user_status_interval_hours: 48
  auto_sync_user_status_cron: 30 6 */2 * *
  auto_sync_series_status_enabled: true
  auto_sync_series_status_interval_hours: 48
  auto_sync_series_status_cron: 30 18 */2 * *

  # 全量备份关注用户小说：兜底，最弱时效。
  auto_sync_user_backup_enabled: false
  auto_sync_user_backup_interval_hours: 72
  auto_sync_user_backup_cron: 30 2 */3 * *

  # 检测取消收藏/追更：单轮约 34 秒，每天一次。
  auto_sync_pending_detection_enabled: true
  auto_sync_pending_detection_interval_hours: 24
  auto_sync_pending_detection_cron: 30 12 * * *

  # 增量偏好分析：纯本地计算不耗 Pixiv 配额，但占用唯一 job 槽。
  auto_sync_preference_analyze_enabled: false
  auto_sync_preference_analyze_interval_hours: 12
  auto_sync_preference_analyze_cron: 15 7,19 * * *
  preference_analyze_batch_size: 200

  # 生成推荐：消耗 Pixiv 搜索配额，且必须先有默认偏好画像，否则任务直接失败。
  auto_sync_recommendation_run_enabled: false
  auto_sync_recommendation_run_interval_hours: 24
  auto_sync_recommendation_run_cron: 50 8 * * *
```

保留文件中已有的 `bookmark_max_pages_per_run`、`following_max_novels_per_author`、`series_max_pages_per_run`（阶段一加的）等非 auto_sync 字段，不要删除。

- [ ] **Step 3: 验证 example 能被 load_settings 解析**

```bash
python -c "
import shutil, tempfile, os
from pathlib import Path
from pixiv_novel_sync.settings import load_settings
tmp = Path(tempfile.mkdtemp())
shutil.copy('config/config.yaml.example', tmp / 'c.yaml')
s = load_settings(str(tmp / 'c.yaml'), None)
print('timezone:', s.sync.auto_sync_timezone)
print('bookmarks cron:', s.sync.auto_sync_bookmarks_cron)
print('novel_status cron:', s.sync.auto_sync_novel_status_cron)
print('series cron:', s.sync.auto_sync_subscribed_series_cron)
print('OK')
"
```

预期：打印出新 cron 值，无异常。

- [ ] **Step 4: 逐条校验 cron 可解析**

```bash
python -c "
import shutil, tempfile
from pathlib import Path
from pixiv_novel_sync.settings import load_settings, cron_to_next_run
tmp = Path(tempfile.mkdtemp())
shutil.copy('config/config.yaml.example', tmp / 'c.yaml')
s = load_settings(str(tmp / 'c.yaml'), None)
tz = s.sync.auto_sync_timezone
bad = []
for attr in dir(s.sync):
    if attr.endswith('_cron'):
        expr = getattr(s.sync, attr)
        if expr and cron_to_next_run(expr, None, tz) is None:
            bad.append((attr, expr))
print('unparseable:', bad)
assert not bad
print('all crons parse OK')
"
```

预期：`unparseable: []`

- [ ] **Step 5: 回归部署契约测试**

```bash
pytest tests/test_deployment_config.py tests/test_deployment_contract.py -q
```

预期：通过。若失败是因为测试断言了 example 里的旧 cron 值，按新值更新断言。

- [ ] **Step 6: 提交**

```bash
git add config/config.yaml.example
git commit -m "docs: config 样例改为实测预算的 cron 排布"
```

---

### Task 4: 锁住三处注册表齐全的防回归断言

**背景（spec §5.5）：** 已用脚本核对 11 个 scheduler task 在 `web/utils.py:_job_spec`、`jobs/tasks.py:_TASK_LABELS`、`web/managers.py:TASK_LABELS` 三处全部齐全，无需修复。但 `CLAUDE.md` 记载「漏注册是静默失败」——缺 `_job_spec` 分支只会让 JobSpec 落到 `JobType.SYNC`，缺 `TASK_LABELS` 只会让日志页显示英文键名。加断言锁住现状。

**Files:**
- Modify: `tests/test_scheduler_priority.py`
- Test: 同上

**Interfaces:**
- Consumes: 无
- Produces: 无代码接口，仅测试

- [ ] **Step 1: 写测试**

追加到 `tests/test_scheduler_priority.py`：

```python
# ── 三处注册表必须与 SCHEDULER_TASK_CONFIGS 保持齐全 ──────────────

def test_every_scheduler_task_is_registered_in_all_three_tables():
    """每个定时任务都必须在三处独立注册表里齐全，否则是静默失败。

    - web/utils.py:_job_spec 缺分支 → JobSpec 静默落到 JobType.SYNC，任务统计归错类
    - jobs/tasks.py:_TASK_LABELS 缺条目 → job 内部日志显示英文键名
    - web/managers.py:TASK_LABELS 缺条目 → 任务日志页显示英文键名

    2026-08-28 已核对 11 个任务全部齐全，此断言防止后续新增任务时回归。
    新增 task_type 的完整清单见 docs/JOB_SYSTEM.md §4。
    """
    from pixiv_novel_sync.jobs.tasks import _TASK_LABELS
    from pixiv_novel_sync.web.managers import SCHEDULER_TASK_CONFIGS, TASK_LABELS
    from pixiv_novel_sync.web.utils import _scheduler_job_spec

    for config in SCHEDULER_TASK_CONFIGS:
        name = config["name"]

        # 1) 调度器名 → 中文标签（任务日志页用）
        assert name in TASK_LABELS, f"web/managers.py:TASK_LABELS 缺少 {name}"

        # 2) JobSpec 能构造出来，且 task_types 非空
        spec = _scheduler_job_spec(name)
        assert spec.task_types, f"{name} 构造出空 task_types"

        # 3) 内部 task_type → 中文标签（job 日志用）
        internal = spec.task_types[0]
        assert internal in _TASK_LABELS, f"jobs/tasks.py:_TASK_LABELS 缺少 {internal}"


def test_status_check_tasks_map_to_status_check_job_type():
    """三个状态检查任务必须落到 STATUS_CHECK，不能静默落到 SYNC。

    这是 _job_spec 里最容易漏的一类分支：漏了不报错，只是任务统计归错类。
    """
    from pixiv_novel_sync.jobs.models import JobType
    from pixiv_novel_sync.web.utils import _scheduler_job_spec

    for name in ("user_status", "novel_status", "series_status"):
        assert _scheduler_job_spec(name).job_type == JobType.STATUS_CHECK, name

    # 对照：四个同步类任务落到 SYNC 是设计意图，不是漏分支
    from pixiv_novel_sync.web.managers import SCHEDULER_TASK_CONFIGS  # noqa: F401

    for name in ("bookmarks", "subscribed_series", "following_list", "following_novels"):
        assert _scheduler_job_spec(name).job_type == JobType.SYNC, name

    # 其余各有专属 job_type
    assert _scheduler_job_spec("user_backup").job_type == JobType.USER_BACKUP
    assert _scheduler_job_spec("pending_deletion_detection").job_type == JobType.PENDING_DELETION_DETECTION
    assert _scheduler_job_spec("preference_analyze").job_type == JobType.PREFERENCE_ANALYZE
    assert _scheduler_job_spec("recommendation_run").job_type == JobType.RECOMMENDATION_RUN
```

- [ ] **Step 2: 运行测试**

```bash
pytest tests/test_scheduler_priority.py -k "registered or status_check_job_type" -v
```

预期：**PASS**（这是锁现状的断言，不是驱动新代码）。若 FAIL，说明实际存在漏注册——读失败信息定位是哪个任务哪张表，按 `docs/JOB_SYSTEM.md` §4 补齐。

- [ ] **Step 3: 跑完整调度测试**

```bash
pytest tests/test_scheduler_priority.py tests/test_recommendation_scheduling.py -q
```

预期：全部通过

- [ ] **Step 4: 提交**

```bash
git add tests/test_scheduler_priority.py
git commit -m "test: 锁住定时任务三处注册表齐全，防止新增任务时静默漏注册"
```

---

### Task 5: 更新 `docs/JOB_SYSTEM.md` §5 配置矩阵

**Files:**
- Modify: `docs/JOB_SYSTEM.md`（§5 配置矩阵表格，第 ~185-215 行）

**Interfaces:**
- Consumes: Task 1（新增 `restricted_streak`）、Task 2（新默认 cron）、阶段一（两个新分页字段）
- Produces: 无代码接口

- [ ] **Step 1: 定位当前表格**

```bash
grep -n "auto_sync 配置矩阵\|## 5\." docs/JOB_SYSTEM.md
sed -n '185,215p' docs/JOB_SYSTEM.md
```

- [ ] **Step 2: 更新表格的 cron 默认值列**

把 §5 表格中 `cron 默认` 一列按 Task 2/3 的新值更新，并在「附加字段」列补上阶段一新增的字段：

| 任务(scheduler name) | P | 可让位 | enabled 默认 | interval_hours 默认 | cron 默认 | 附加字段 |
|---|---|---|---|---|---|---|
| bookmarks | 1 | ✗ | True | 4 | `"20 0,4,8,12,16,20 * * *"` | `bookmark_max_pages_per_run` |
| subscribed_series | 2 | ✗ | True | 12 | `"40 1,13 * * *"` | `series_max_pages_per_run`（阶段一新增） |
| following_list | 3 | ✗ | True | 24 | `"30 10 * * *"` | |
| following_novels | 3 | ✓ | True | 6 | `"0 3,9,15,21 * * *"` | `auto_sync_following_novels_users_limit`、`following_max_novels_per_author`（阶段一新增） |
| user_status | 3 | ✓ | True | 48 | `"30 6 */2 * *"` | 已知受限用户按 `restricted_streak` 降频（阶段二新增） |
| novel_status | 3 | ✓ | True | 12 | `"0 5,17 * * *"` | `novel_status_batch_size`（默认 800） |
| series_status | 3 | ✓ | True | 48 | `"30 18 */2 * *"` | |
| user_backup | 3 | ✓ | False | 72 | `"30 2 */3 * *"` | 复用 `auto_sync_following_novels_users_limit` |
| pending_deletion_detection | 3 | ✗ | True | 24 | `"30 12 * * *"` | |
| preference_analyze | 3 | ✓ | False | 12 | `"15 7,19 * * *"` | `preference_analyze_batch_size`；scheduler 强制 `max_batches=1` |
| recommendation_run | 3 | ✗ | False | 24 | `"50 8 * * *"` | 需已存在默认偏好画像 |

同时把 §5 里「preference_analyze 是唯一自带默认 cron 的任务」这句改掉——现在 `recommendation_run` 也有了。

- [ ] **Step 3: 追加一小节记录实测预算**

在 §5 末尾追加：

```markdown
### 5.2 实测预算基线（2026-08-28）

生产实测 3 天 52 轮，单任务槽串行，总占用 4.9 小时/天（占空比 20.5%）。阶段一
吞吐修复 + 阶段二 cron 重排后目标 3.8 小时/天（15.7%）。

排布约束：
1. P1 收藏每天 6 次均匀分布，任何时刻最多等 4 小时。
2. 长任务（following_novels、novel_status）避开收藏时刻 ±30 分钟，减少无谓让位。
3. 时效性弱的任务拉到隔日或隔三日。

**注意：截至 2026-08-28 生产 journald 里 `selected by priority` / `submit failed` /
`yielded` 均为 0 次**——旧 cron 排得过散，两个任务几乎从不同时到点，让位逻辑从未
被触发过。改动这些 cron 时如果看到让位日志，那是机制首次生效，不是故障。
```

- [ ] **Step 4: 验证文档测试**

```bash
pytest tests/test_ai_model_docs.py -q
```

预期：通过（该测试断言 README 与 docs 内容，改 JOB_SYSTEM.md 一般不影响，但要确认）

- [ ] **Step 5: 提交**

```bash
git add docs/JOB_SYSTEM.md
git commit -m "docs: JOB_SYSTEM 配置矩阵同步新 cron 与实测预算基线"
```

---

### Task 6: 生产灰度上线与验证（手工步骤，非代码）

**这一步不写代码，是上线流程。** 由人执行或在明确授权后执行。

**前置条件：** 阶段一已在生产运行至少 24 小时，且 `task_logs` 里 `following_novels` 的 `following_users_scanned` 已稳定达到 `users_limit`（证明每作者配额生效）。

- [ ] **Step 1: 备份生产配置与数据库**

```bash
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 \
  'cd ~/pixiv-novel-sync && cp config/config.yaml config/config.yaml.bak-$(date +%F) && ls -la data/state/'
```

数据库 2.3 GB，磁盘剩 45 GB，可直接冷备：先停服务再复制，避免 WAL 不一致。

- [ ] **Step 2: 记录改动前的基线**

```bash
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 \
  'cd ~/pixiv-novel-sync && .venv/bin/python -c "
import sqlite3, collections
c = sqlite3.connect(\"data/state/pixiv_sync.db\")
c.row_factory = sqlite3.Row
rows = c.execute(\"SELECT task_type, status, duration_seconds FROM task_logs WHERE started_at > datetime(\\\"now\\\",\\\"-3 days\\\")\").fetchall()
agg = collections.defaultdict(lambda: [0,0.0])
for r in rows:
    a = agg[r[\"task_type\"]]; a[0]+=1; a[1]+=r[\"duration_seconds\"] or 0
for k,(n,t) in sorted(agg.items(), key=lambda kv:-kv[1][1]):
    print(f\"{k:28s} runs={n:3d} total={t:7.0f}s avg={t/max(n,1):6.0f}s\")
print(\"TOTAL/day\", sum(t for _,t in agg.values())/3)
"'
```

把输出存档，作为对比基准。

- [ ] **Step 2b: 部署代码（阶段二的 Task 1/2）**

```bash
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 \
  'cd ~/pixiv-novel-sync && ./update.sh'
```

`update.sh` 会拉代码、重装、重启服务。`init_schema()` 会执行 `restricted_streak` 的 `ALTER TABLE`（毫秒级）。

- [ ] **Step 3: 更新生产 cron**

生产 `config/config.yaml` **不在仓库里**，需按 Task 3 的排布手工编辑对应字段。改完立即校验：

```bash
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 \
  'cd ~/pixiv-novel-sync && .venv/bin/python -c "
from pixiv_novel_sync.settings import load_settings, cron_to_next_run
s = load_settings(\"config/config.yaml\", None)
tz = s.sync.auto_sync_timezone
for attr in sorted(dir(s.sync)):
    if attr.endswith(\"_cron\"):
        e = getattr(s.sync, attr)
        if e:
            nxt = cron_to_next_run(e, None, tz)
            print(f\"{attr:48s} {e:28s} -> {nxt}\")
            assert nxt is not None, attr
print(\"all OK, tz=\", tz)
"'
```

预期：每条 cron 都算出下次运行时刻，无 assert 失败。

- [ ] **Step 4: 重启并确认调度器接受新排布**

```bash
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 \
  'sudo systemctl restart pixiv-novel-sync && sleep 20 && \
   sudo journalctl -u pixiv-novel-sync --since "2 min ago" | grep -E "scheduled, next run|Auto sync scheduler|重建"'
```

预期：11 行 `Task X scheduled, next run: ...`，时刻与新 cron 一致。

- [ ] **Step 5: 观察 48 小时后复核**

重跑 Step 2 的聚合脚本，核对：

- 总预算是否降到约 3.8 小时/天
- `following_novels` 的 `following_users_scanned` 是否稳定等于 `users_limit`（5）
- `user_status` 的 `checked_count` 是否不再被 6 个受限用户拖累
- 是否出现 `selected by priority` / `yielded` 日志（机制首次生效的信号，属预期）
- 是否出现**新的** `aborted_reason`

```bash
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 \
  'sudo journalctl -u pixiv-novel-sync --since "48 hours ago" | \
   grep -cE "selected by priority|yielded to a higher-priority|submit failed"'
```

**若出现新的 `aborted_reason`，不要套用「都是误判」的旧结论**——`923dfd0` 已修掉已知的四类误判，新的中止要当作新信号重新排查。

- [ ] **Step 6: 回滚预案**

cron 改动是纯配置：

```bash
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 \
  'cd ~/pixiv-novel-sync && cp config/config.yaml.bak-<日期> config/config.yaml && sudo systemctl restart pixiv-novel-sync'
```

`restricted_streak` 列是新增列，旧代码忽略它即可，无需回滚数据。

---

## 完成标准

- [ ] `pytest` 全量通过
- [ ] `python -m compileall -q src` 无输出
- [ ] `config/config.yaml.example` 的每条 cron 都能在 `Asia/Seoul` 下解析
- [ ] `docs/JOB_SYSTEM.md` §5 与 `settings.py` 默认值一致
- [ ] 生产观察 48 小时：总预算约 3.8 小时/天，无新增 `aborted_reason`

## 明确不做

- 不改 `SCHEDULER_TASK_CONFIGS` 的 priority / preemptible。
- 不调整 5 个限速参数的数值（需阶段一上线后一周的新基线）。
- 不解耦 `user_backup` 与 `following_novels` 的 `users_limit`。

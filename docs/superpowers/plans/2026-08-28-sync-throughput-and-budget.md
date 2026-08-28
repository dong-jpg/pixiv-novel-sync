# 同步吞吐修复与预算重排 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修掉三个实测到的同步吞吐缺陷（FTS 全表扫描、单作者吃满整轮配额、系列分页被锁死），再依据新的耗时基线重排定时任务预算。

**Architecture:** 全部改动落在 `storage/`（FTS rowid 语义 + 幂等重建迁移）、`sync_engine.py`（作者级配额 + 系列独立分页上限）、`settings.py` / `web/managers.py` / `web/utils.py`（两个新配置字段的三处接线）与配置文档。不新增 task_type，不改调度器代码，优先级机制保持现状。

**Tech Stack:** Python 3.10+、SQLite（FTS5）、pytest。无 lint/type-check 工具链（`python -m compileall -q src` 是唯一静态检查）。

**Spec:** [docs/superpowers/specs/2026-08-28-sync-budget-and-settings-redesign-design.md](../specs/2026-08-28-sync-budget-and-settings-redesign-design.md) §2、§4、§5

## Global Constraints

- 模块首行 `from __future__ import annotations`；dataclass 用 `slots=True`。
- 代码注释与用户可见字符串用中文。commit subject 用 `type: subject`（Conventional Commits）。
- 重型 import 放函数内部（延迟导入），沿用 `cli.py` / `jobs/tasks.py` 的做法。
- 写库统一走 `db.transaction()`（`BEGIN IMMEDIATE`，可安全嵌套）；成组读走 `db.read_transaction()`。
- 迁移是**幂等 DDL**，每次 `init_schema()` 都重跑：加列必须先 `PRAGMA table_info` 判断。没有版本表，没有迁移文件。
- `sync_engine.py` 内**禁止裸 `time.sleep(`**。`tests/test_sync_engine_incremental.py:283` 会 grep 源码并失败。要睡就用 `_sleep_with_progress_cancel` / `rate_limiter.cancellable_sleep` / `jobs/services._sleep_with_cancel`。
- `InterruptedError` 必须能穿透所有循环，不能被宽泛的 `except Exception` 吞掉（有专门的回归测试）。
- 新增配置字段必须同时改四处：`settings.py:SyncSettings` 声明 + `load_settings` 解析、`web/managers.py:save_sync_settings` 保存、`web/utils.py:_settings_to_dict` 暴露、`config/config.yaml.example` 示例。漏一处不会报错，只会静默失效。
- 跑测试：`pytest tests/xxx.py::test_name -v`。全量 `pytest` 约 6 分钟（基线 1308 passed / 4 skipped）。
- 生产库 2.32 GB、`novel_fts_*` 合计 1054 MB。任何在生产库上按 `novel_id` 查 FTS 的操作都要 39 秒——**这正是本计划要修的东西**，实施期间不要在生产库上做这类查询来"验证"。

---

## File Structure

| 文件 | 职责 | 本计划的改动 |
|---|---|---|
| `src/pixiv_novel_sync/storage/novels.py` | 小说与 FTS 的读写 | `replace_fts` / `delete_novel` / 列表搜索改走 rowid |
| `src/pixiv_novel_sync/storage/users.py` | 用户读写与状态轮转 | 删用户清 FTS 改 rowid；新增受限用户降频查询 |
| `src/pixiv_novel_sync/storage/bookmarks.py` | 收藏列表分页查询 | 搜索子查询改 `SELECT rowid` |
| `src/pixiv_novel_sync/storage/series.py` | 系列读写与状态轮转 | 搜索子查询改 `SELECT rowid` |
| `src/pixiv_novel_sync/storage/schema.py` | 建表与幂等迁移 | 新增 FTS 重建迁移、`users` 受限标记列 |
| `src/pixiv_novel_sync/sync_engine.py` | Pixiv API 调用、分页、水位线、落盘 | 作者级配额、系列分页上限解析 |
| `src/pixiv_novel_sync/settings.py` | 配置合并与 `SyncSettings` | 三个新字段 |
| `src/pixiv_novel_sync/web/managers.py` | 设置保存与调度器 | 三个新字段的保存 |
| `src/pixiv_novel_sync/web/utils.py` | 设置序列化与 Pixiv 响应三态判定 | 三个新字段的暴露 |
| `src/pixiv_novel_sync/jobs/services.py` | 状态检查任务 | 受限用户降频接线 |
| `config/config.yaml.example` | 配置示例 | 三个新字段 + 新 cron 排布 |
| `docs/JOB_SYSTEM.md` | 任务系统开发者文档 | §5 配置矩阵更新 |

---

## Task 1: FTS 写入与删除改走 rowid

**背景（必读）**：`novel_fts` 建表时把 `novel_id` 声明成 `UNINDEXED`（`storage/schema.py:91`），而 FTS5 的 `rowid` 是自增值。生产库 7627 行里 `rowid == novel_id` 的有 **0** 行。因此 `WHERE novel_id = ?` 只能全表扫描 1 GB 索引——生产实测单次 39.17 秒。改用 `rowid` 后是 0.0002 秒。

本任务只改写入/删除路径，不动搜索路径（Task 2）也不动历史数据（Task 3）。

**Files:**
- Modify: `src/pixiv_novel_sync/storage/novels.py:380-392`（`replace_fts`）、`:350`（`delete_novel` 里的 FTS 清理）
- Modify: `src/pixiv_novel_sync/storage/users.py:326`（`delete_user` 里的 FTS 清理）
- Test: `tests/test_storage_db.py`

**Interfaces:**
- Consumes: 无（第一个任务）
- Produces: `db.replace_fts(novel_id, title, caption, author_name, body) -> None` 签名不变，但写入后 `novel_fts.rowid == novel_id` 成立。Task 2、3 依赖这个不变式。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_storage_db.py`（放在 `test_delete_novel_cascades_child_rows_and_cleans_satellites` 之前）：

```python
def test_replace_fts_uses_novel_id_as_rowid(db: Database) -> None:
    """FTS 的 rowid 必须等于 novel_id。

    回归：建表时 novel_id 是 UNINDEXED 列，rowid 是自增值，两者零重合。
    结果 WHERE novel_id = ? 全表扫描 1 GB 索引，生产实测单次 39 秒——
    following_novels 单篇小说 51 秒里有 40 秒花在这里。
    """
    _insert_user_and_novel(db, novel_id=100)
    db.replace_fts(100, "标题", "简介", "作者", "正文内容")

    row = db.conn.execute("SELECT rowid, novel_id FROM novel_fts").fetchone()
    assert row is not None
    assert int(row[0]) == 100
    assert int(row[1]) == 100


def test_replace_fts_is_idempotent_on_rowid(db: Database) -> None:
    """重复写同一篇不能留下重复行（DELETE 必须命中）。"""
    _insert_user_and_novel(db, novel_id=100)
    db.replace_fts(100, "旧标题", "简介", "作者", "旧正文")
    db.replace_fts(100, "新标题", "简介", "作者", "新正文")

    assert db.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0] == 1
    hits = db.conn.execute(
        "SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?", ('"新正文"',)
    ).fetchall()
    assert [int(r[0]) for r in hits] == [100]
    assert db.conn.execute(
        "SELECT COUNT(*) FROM novel_fts WHERE novel_fts MATCH ?", ('"旧正文"',)
    ).fetchone()[0] == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_storage_db.py::test_replace_fts_uses_novel_id_as_rowid tests/test_storage_db.py::test_replace_fts_is_idempotent_on_rowid -v`

Expected: 第一个 FAIL（`assert 1 == 100`，rowid 是自增的 1）；第二个 FAIL（`assert 2 == 1`，DELETE 没命中所以留下两行）。

- [ ] **Step 3: 改 `replace_fts`**

把 `src/pixiv_novel_sync/storage/novels.py:380-392` 整个方法替换为：

```python
    def replace_fts(self, novel_id: int, title: str, caption: str, author_name: str, body: str) -> None:
        """更新 FTS 索引。

        ✅ Bug #6 修复: 使用 transaction() 确保 DELETE 和 INSERT 的原子性。
        注意:与upsert_novel分离调用时存在漂移风险(一个成功一个失败)。
        Phase 5批量事务化后自然原子化。当前调用方(sync_engine.py:1723)未封装事务。

        必须按 rowid 定位：novel_id 是 UNINDEXED 列，`WHERE novel_id = ?` 会全表
        扫描整个 FTS 索引（生产 1 GB，实测单次 39 秒），而 rowid 是 FTS5 主键，O(1)。
        因此 INSERT 时显式把 rowid 写成 novel_id，两者必须始终相等。
        """
        with self.transaction():
            self.conn.execute("DELETE FROM novel_fts WHERE rowid = ?", (novel_id,))
            self.conn.execute(
                "INSERT INTO novel_fts (rowid, novel_id, title, caption, author_name, body) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (novel_id, novel_id, title, caption, author_name, body),
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_storage_db.py::test_replace_fts_uses_novel_id_as_rowid tests/test_storage_db.py::test_replace_fts_is_idempotent_on_rowid -v`

Expected: 2 passed

- [ ] **Step 5: 改两处删除路径**

`src/pixiv_novel_sync/storage/novels.py:350`，把

```python
                self.conn.execute("DELETE FROM novel_fts WHERE novel_id = ?", (novel_id,))
```

改为

```python
                # 按 rowid 删：novel_id 是 UNINDEXED 列，按它筛会全表扫描 FTS 索引
                self.conn.execute("DELETE FROM novel_fts WHERE rowid = ?", (novel_id,))
```

`src/pixiv_novel_sync/storage/users.py:326`，把

```python
                self.conn.execute("DELETE FROM novel_fts WHERE novel_id IN (SELECT novel_id FROM novels WHERE user_id = ?)", (user_id,))
```

改为

```python
                # 按 rowid 删：novel_id 是 UNINDEXED 列，按它筛会全表扫描 FTS 索引
                self.conn.execute("DELETE FROM novel_fts WHERE rowid IN (SELECT novel_id FROM novels WHERE user_id = ?)", (user_id,))
```

- [ ] **Step 6: 跑既有删除测试确认没破**

Run: `pytest tests/test_storage_db.py -v -k "delete"`

Expected: 全部 PASS。这两个既有测试用 `SELECT 1 FROM novel_fts WHERE novel_id = 100` 断言删干净了——在测试库里这个查询仍然正确（只是慢，而测试库很小），所以不用改断言。

- [ ] **Step 7: Commit**

```bash
git add src/pixiv_novel_sync/storage/novels.py src/pixiv_novel_sync/storage/users.py tests/test_storage_db.py
git commit -m "perf: FTS 写入与删除改按 rowid 定位"
```

---

## Task 2: FTS 搜索改走 rowid

**背景**：三处搜索都写成 `n.novel_id IN (SELECT novel_id FROM novel_fts WHERE novel_fts MATCH ?)`。取 `novel_id` 列意味着 FTS 要回表读 `novel_fts_content`；取 `rowid` 直接命中索引。生产实测 0.22s → 0.00s。Task 1 已保证 `rowid == novel_id`，所以换列不改语义。

**Files:**
- Modify: `src/pixiv_novel_sync/storage/novels.py:413`（`list_recent_novels`）
- Modify: `src/pixiv_novel_sync/storage/bookmarks.py:24`（`list_bookmarks`）
- Modify: `src/pixiv_novel_sync/storage/series.py:155`（订阅系列列表搜索）
- Test: `tests/test_storage_db.py`

**Interfaces:**
- Consumes: Task 1 建立的不变式 `novel_fts.rowid == novel_id`
- Produces: 无新增签名。三处列表查询的返回结构不变。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_storage_db.py`：

```python
def test_fts_search_subqueries_select_rowid(db: Database) -> None:
    """三处搜索子查询必须取 rowid 而非 novel_id。

    取 novel_id（UNINDEXED 列）会让 FTS 回表读 novel_fts_content；
    取 rowid 直接命中索引。生产实测 0.22s → 0.00s。
    Task 1 已保证 rowid == novel_id，换列不改语义。
    """
    from pathlib import Path

    for rel in (
        "src/pixiv_novel_sync/storage/novels.py",
        "src/pixiv_novel_sync/storage/bookmarks.py",
        "src/pixiv_novel_sync/storage/series.py",
    ):
        source = Path(rel).read_text(encoding="utf-8")
        assert "SELECT novel_id FROM novel_fts" not in source, rel
        assert "SELECT rowid FROM novel_fts" in source, rel


def test_search_still_finds_novels_after_rowid_switch(db: Database) -> None:
    """换成 rowid 后搜索结果必须不变。"""
    _insert_user_and_novel(db, novel_id=100)
    db.upsert_novel_text(
        NovelTextRecord(novel_id=100, text_raw="魔法少女的日常", text_markdown=None, text_hash="t")
    )
    db.replace_fts(100, "title", "caption", "author", "魔法少女的日常")

    result = db.list_recent_novels(search="魔法少女")

    assert result["total"] == 1
    assert [item["novel_id"] for item in result["items"]] == [100]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_storage_db.py::test_fts_search_subqueries_select_rowid tests/test_storage_db.py::test_search_still_finds_novels_after_rowid_switch -v`

Expected: 第一个 FAIL（`assert "SELECT novel_id FROM novel_fts" not in source` 在三个文件上都命中）。第二个应当 PASS（当前代码功能正确，只是慢）——它的作用是锁住改动后语义不变。

- [ ] **Step 3: 改三处搜索 SQL**

`src/pixiv_novel_sync/storage/novels.py:413`：

```python
            where_clauses.append("n.novel_id IN (SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?)")
```

`src/pixiv_novel_sync/storage/bookmarks.py:24`：

```python
            where_clauses.append("n.novel_id IN (SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?)")
```

`src/pixiv_novel_sync/storage/series.py:155`，把该行的子查询同样换掉：

```python
                     SELECT 1 FROM novels n0 WHERE n0.series_id = se.series_id AND n0.novel_id IN (SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_storage_db.py::test_fts_search_subqueries_select_rowid tests/test_storage_db.py::test_search_still_finds_novels_after_rowid_switch -v`

Expected: 2 passed

- [ ] **Step 5: 跑列表相关的既有测试**

Run: `pytest tests/test_storage_db.py tests/test_fts_escape.py tests/test_dashboard_novels_api.py -q`

Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add src/pixiv_novel_sync/storage/novels.py src/pixiv_novel_sync/storage/bookmarks.py src/pixiv_novel_sync/storage/series.py tests/test_storage_db.py
git commit -m "perf: FTS 搜索子查询改取 rowid"
```

---

## Task 3: FTS 错位数据的幂等重建迁移

**背景**：Task 1、2 只让**新写入**的行满足 `rowid == novel_id`。生产库现存 7627 行全部错位，必须重建。生产实测：2000 行 / 33 MB 正文用 24.5 秒，按字节量外推全量（168 MB 文本）约 **124 秒**。这发生在 `init_schema()` 内即服务启动期。

探测必须便宜：`SELECT rowid, novel_id FROM novel_fts LIMIT 1` 实测 0.01 秒（不是全表扫描）。绝不能用 `SELECT COUNT(*) FROM novel_fts WHERE rowid != novel_id` 探测——那是全表扫描。

**Files:**
- Modify: `src/pixiv_novel_sync/storage/schema.py`（新增 `_migrate_novel_fts_rowid`，并在 `init_schema` 尾部调用）
- Test: `tests/test_storage_db.py`

**Interfaces:**
- Consumes: Task 1 的 `replace_fts`（重建时复用同样的 `rowid == novel_id` 写法）
- Produces: `db._migrate_novel_fts_rowid() -> None`，由 `init_schema()` 调用。幂等：rowid 已正确时立即返回。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_storage_db.py`：

```python
def test_migration_rebuilds_misaligned_fts_rowid(tmp_path: Path) -> None:
    """错位的历史 FTS 数据必须被重建成 rowid == novel_id。

    生产库 7627 行全部错位（实测 rowid=12 对应 novel_id=25310744），
    Task 1/2 只影响新写入，历史数据要靠这个迁移修。
    """
    db_path = tmp_path / "legacy-fts.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE novels (
                novel_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                series_id INTEGER,
                title TEXT NOT NULL,
                caption TEXT,
                visible INTEGER NOT NULL,
                restrict_value TEXT NOT NULL,
                x_restrict INTEGER NOT NULL,
                text_length INTEGER NOT NULL,
                total_bookmarks INTEGER NOT NULL,
                total_views INTEGER NOT NULL,
                cover_url TEXT,
                tags_json TEXT NOT NULL,
                create_date TEXT,
                raw_json TEXT NOT NULL,
                meta_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE novel_texts (
                novel_id INTEGER PRIMARY KEY,
                text_raw TEXT NOT NULL,
                text_markdown TEXT,
                text_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE VIRTUAL TABLE novel_fts USING fts5(
                novel_id UNINDEXED, title, caption, author_name, body
            );
            """
        )
        conn.execute(
            "INSERT INTO novels (novel_id, user_id, title, caption, visible, restrict_value,"
            " x_restrict, text_length, total_bookmarks, total_views, tags_json, raw_json, meta_hash)"
            " VALUES (25310744, 7, '标题甲', '简介甲', 1, 'public', 0, 5, 0, 0, '[]', '{}', 'h1')"
        )
        conn.execute(
            "INSERT INTO novels (novel_id, user_id, title, caption, visible, restrict_value,"
            " x_restrict, text_length, total_bookmarks, total_views, tags_json, raw_json, meta_hash)"
            " VALUES (27380872, 7, '标题乙', '简介乙', 1, 'public', 0, 5, 0, 0, '[]', '{}', 'h2')"
        )
        conn.execute(
            "INSERT INTO novel_texts (novel_id, text_raw, text_hash) VALUES (25310744, '魔法正文', 't1')"
        )
        conn.execute(
            "INSERT INTO novel_texts (novel_id, text_raw, text_hash) VALUES (27380872, '剑术正文', 't2')"
        )
        # 不指定 rowid：模拟生产的自增错位
        conn.execute(
            "INSERT INTO novel_fts (novel_id, title, caption, author_name, body)"
            " VALUES (25310744, '标题甲', '简介甲', '作者甲', '魔法正文')"
        )
        conn.execute(
            "INSERT INTO novel_fts (novel_id, title, caption, author_name, body)"
            " VALUES (27380872, '标题乙', '简介乙', '作者乙', '剑术正文')"
        )

    db = Database(db_path)
    try:
        db.init_schema()
        rows = db.conn.execute("SELECT rowid, novel_id FROM novel_fts ORDER BY rowid").fetchall()
        assert [(int(r[0]), int(r[1])) for r in rows] == [
            (25310744, 25310744),
            (27380872, 27380872),
        ]
        # 重建后搜索仍然可用
        hits = db.conn.execute(
            "SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?", ('"剑术正文"',)
        ).fetchall()
        assert [int(r[0]) for r in hits] == [27380872]
    finally:
        db.close()


def test_migration_is_noop_when_fts_rowid_already_aligned(db: Database) -> None:
    """rowid 已正确时迁移必须不动数据（幂等，且不能每次启动都重建 1 GB 索引）。"""
    _insert_user_and_novel(db, novel_id=100)
    db.replace_fts(100, "标题", "简介", "作者", "正文")

    db.init_schema()  # 再跑一次迁移

    rows = db.conn.execute("SELECT rowid, novel_id FROM novel_fts").fetchall()
    assert [(int(r[0]), int(r[1])) for r in rows] == [(100, 100)]


def test_migration_handles_empty_fts_table(db: Database) -> None:
    """空 FTS 表不能让探测崩（新库首次启动走这条路）。"""
    assert db.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0] == 0
    db.init_schema()
    assert db.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0] == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_storage_db.py -v -k "fts_rowid or fts_table or already_aligned"`

Expected: `test_migration_rebuilds_misaligned_fts_rowid` FAIL（rowid 仍是 1、2）。另两个应当 PASS（迁移方法还不存在，等于 no-op）。

- [ ] **Step 3: 实现迁移方法**

在 `src/pixiv_novel_sync/storage/schema.py` 的 `_migrate_novel_texts_table`（`:312`）后面插入：

```python
    def _migrate_novel_fts_rowid(self) -> None:
        """把历史 novel_fts 的 rowid 重建成 novel_id。

        novel_fts 建表时 novel_id 是 UNINDEXED 列，早期写入没有指定 rowid，于是
        rowid 变成自增值、与 novel_id 零重合（生产 7627 行里 0 行相等）。后果是
        每个 `WHERE novel_id = ?` 都全表扫描 1 GB 索引：生产实测 replace_fts 单次
        39 秒，占 following_novels 单篇耗时的 80%。

        探测只读一行（实测 0.01s）。绝不能用 `WHERE rowid != novel_id` 计数探测，
        那本身就是一次全表扫描。重建按字节量外推生产库约 124 秒，属于启动期一次性开销。
        """
        probe = self.conn.execute("SELECT rowid, novel_id FROM novel_fts LIMIT 1").fetchone()
        if probe is None:
            return  # 空表（新库首次启动）
        if int(probe[0]) == int(probe[1]):
            return  # 已经是对齐的，幂等返回
        logger.warning(
            "检测到 novel_fts 的 rowid 与 novel_id 错位，开始重建全文索引（生产规模约需 2 分钟，期间服务不可用）"
        )
        self.conn.execute("DROP TABLE novel_fts")
        self.conn.execute(
            """
            CREATE VIRTUAL TABLE novel_fts USING fts5(
                novel_id UNINDEXED,
                title,
                caption,
                author_name,
                body
            )
            """
        )
        # rowid 显式取 novel_id。author_name 从 users 表取，取不到时留空串
        # （FTS 列不允许 NULL 参与匹配，空串是安全的中性值）。
        self.conn.execute(
            """
            INSERT INTO novel_fts (rowid, novel_id, title, caption, author_name, body)
            SELECT n.novel_id,
                   n.novel_id,
                   COALESCE(n.title, ''),
                   COALESCE(n.caption, ''),
                   COALESCE(u.name, ''),
                   COALESCE(t.text_raw, '')
            FROM novels n
            LEFT JOIN users u ON u.user_id = n.user_id
            LEFT JOIN novel_texts t ON t.novel_id = n.novel_id
            """
        )
        rebuilt = int(self.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0])
        logger.warning("novel_fts 重建完成，共 %d 行", rebuilt)
```

- [ ] **Step 4: 在 `init_schema()` 里接线**

`storage/schema.py` 顶部已有 `logger = logging.getLogger(__name__)`（第 8 行），无需新增 import。

在 `init_schema()` 里、`self._migrate_novel_texts_table()` 那一行之后插入调用：

```python
        # 迁移：为旧版 novel_texts 表添加正文完整度辅助列
        self._migrate_novel_texts_table()
        # 迁移：把历史 novel_fts 的 rowid 重建成 novel_id（必须在 novels/novel_texts/users 就绪之后）
        self._migrate_novel_fts_rowid()
        self._migrate_core_foreign_keys()
        self._commit_if_needed()
```

顺序很关键：重建要读 `novels` / `novel_texts` / `users`，必须排在这三张表的建表与迁移之后。

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_storage_db.py -v -k "fts_rowid or fts_table or already_aligned"`

Expected: 3 passed

- [ ] **Step 6: 跑全套 storage 测试确认没破**

Run: `pytest tests/test_storage_db.py tests/test_archive_integrity.py tests/test_environment_isolation.py -q`

Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add src/pixiv_novel_sync/storage/schema.py tests/test_storage_db.py
git commit -m "fix: 重建 novel_fts 的 rowid 以消除全表扫描"
```

---

## Task 4: `following_novels` 每作者独立配额

**背景**：`max_items_per_run=20` 只在**切换作者之前**检查（`sync_engine.py:1040`），单个作者内部完全没有上限。生产 8-27 21:04 那轮：一个作者从 21:09 跑到 22:01（60 篇），然后 `synced_items(60) >= max_items(20)` 直接结束整轮 → `synced 1/256 users`。256 人全圈需 95 天。

改法：作者级配额 `following_max_novels_per_author`（默认 20）在 `_sync_author` 内生效；作者循环判据改成「跑满 `users_limit` 个作者」，`max_items_per_run` 退化为整轮兜底。

**Files:**
- Modify: `src/pixiv_novel_sync/settings.py`（`SyncSettings` + `load_settings`）
- Modify: `src/pixiv_novel_sync/sync_engine.py`（`_sync_author` 与 `users_limit > 0` 分支）
- Modify: `src/pixiv_novel_sync/web/managers.py`（`save_sync_settings`）
- Modify: `src/pixiv_novel_sync/web/utils.py`（`_settings_to_dict`）
- Modify: `config/config.yaml.example`
- Test: `tests/test_sync_engine_incremental.py`、`tests/test_webapp_settings.py`

**Interfaces:**
- Consumes: 现有 `_order_following_users_for_rotation(users, user_last_synced) -> list`、`_RotationFakeDb`、`_EightAuthorsApi`（测试助手，已在 `tests/test_sync_engine_incremental.py:1042` / `:1057`）
- Produces: `settings.sync.following_max_novels_per_author: int | None`。`stats` 新增可选键 `authors_capped: int`（本轮有多少作者撞到配额），Task 7 的文档会引用它。

- [ ] **Step 1: 写失败测试**

先加一个能产出多篇小说的作者 fake，追加到 `tests/test_sync_engine_incremental.py` 末尾。

**重要**：这三条测试必须用**真实 `Database`**，不能用 `_RotationFakeDb`。因为作者一旦返回非空 `novels`，`_sync_author` 就会走进 `_sync_novel` → `_sync_novel_inner`，那里要 `db.conn.execute` / `upsert_novel` / `replace_fts`，而 `_FollowingFakeDb` 只有四个水位线方法。真实 `Database` 自带 `get_watermark` / `update_watermark`，空库时所有作者都算"从未同步"，稳定排序会保留列表顺序。

```python
class _ProlificAuthorsApi:
    """每个作者都有 ``novels_per_author`` 篇作品，单页返回全部。

    用于验证「单个高产作者不能吃掉整轮配额」。
    """

    def __init__(self, author_ids: list[int], novels_per_author: int) -> None:
        self.author_ids = author_ids
        self.novels_per_author = novels_per_author
        self.scanned_user_ids: list[int] = []
        self.detail_calls: list[int] = []

    def user_following(self, **kwargs):
        return SimpleNamespace(
            user_previews=[
                SimpleNamespace(user=SimpleNamespace(id=uid, name=f"作者{uid}"))
                for uid in self.author_ids
            ],
            next_url=None,
        )

    def user_novels(self, **kwargs):
        uid = int(kwargs["user_id"])
        self.scanned_user_ids.append(uid)
        novels = [
            SimpleNamespace(id=uid * 1000 + i, restrict="public")
            for i in range(self.novels_per_author)
        ]
        return SimpleNamespace(novels=novels, next_url=None)

    def novel_detail(self, novel_id: int):
        self.detail_calls.append(int(novel_id))
        return SimpleNamespace(novel=_Novel())

    def webview_novel(self, novel_id: int) -> dict:
        return {"text": f"body-{novel_id}"}

    def parse_qs(self, next_url):
        return None


def test_single_prolific_author_does_not_consume_whole_run(tmp_path: Path) -> None:
    """单个高产作者最多同步 following_max_novels_per_author 篇，然后换下一个作者。

    回归：生产实测 max_items_per_run=20 只在切作者前检查，作者内部无上限。
    8-27 21:04 那轮一个作者连跑 60 篇（52 分钟），然后整轮结束，
    日志写着 "synced 1/256 users"——256 人全圈需 95 天。
    """
    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    settings.sync.max_items_per_run = 20
    settings.sync.following_max_novels_per_author = 3
    db = Database(settings.storage.db_path)
    db.init_schema()
    api = _ProlificAuthorsApi(author_ids=[11, 12, 13], novels_per_author=10)
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    try:
        stats = service.sync_following_novels(users_limit=3)
    finally:
        db.close()

    # 三个作者都被访问，而不是第一个吃满整轮
    assert api.scanned_user_ids == [11, 12, 13]
    assert stats["following_users_scanned"] == 3
    # 每个作者最多 3 篇
    for uid in (11, 12, 13):
        fetched = [n for n in api.detail_calls if n // 1000 == uid]
        assert len(fetched) == 3, f"作者 {uid} 取了 {len(fetched)} 篇详情"
    assert stats["authors_capped"] == 3


def test_author_quota_leaves_small_authors_untouched(tmp_path: Path) -> None:
    """作者作品数少于配额时不设标记，行为与旧版一致。"""
    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    settings.sync.max_items_per_run = 20
    settings.sync.following_max_novels_per_author = 5
    db = Database(settings.storage.db_path)
    db.init_schema()
    api = _ProlificAuthorsApi(author_ids=[21, 22], novels_per_author=2)
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    try:
        stats = service.sync_following_novels(users_limit=2)
    finally:
        db.close()

    assert api.scanned_user_ids == [21, 22]
    assert len(api.detail_calls) == 4
    assert "authors_capped" not in stats


def test_author_quota_absent_falls_back_to_run_wide_cap(tmp_path: Path) -> None:
    """未配置作者配额时保持旧行为：只受 max_items_per_run 整轮兜底约束。"""
    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    settings.sync.max_items_per_run = 4
    settings.sync.following_max_novels_per_author = None
    db = Database(settings.storage.db_path)
    db.init_schema()
    api = _ProlificAuthorsApi(author_ids=[31, 32], novels_per_author=10)
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    try:
        service.sync_following_novels(users_limit=2)
    finally:
        db.close()

    # 整轮兜底仍然生效：第一个作者跑完 10 篇后 synced_items >= 4，第二个作者轮不到
    assert api.scanned_user_ids == [31]
```

同时在 `_settings()` 助手（`tests/test_sync_engine_incremental.py:22`）的 `sync=SimpleNamespace(...)` 里补上新字段，紧跟 `max_pages_per_run=None,` 之后：

```python
            following_max_novels_per_author=None,
            series_max_pages_per_run=None,
```

（`series_max_pages_per_run` 是 Task 5 要用的，一并加进去省一次改动。）

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_sync_engine_incremental.py -v -k "prolific or author_quota"`

Expected: `test_single_prolific_author_does_not_consume_whole_run` FAIL（`api.scanned_user_ids == [11]`，第一个作者跑完 10 篇后 `synced_items >= max_items` 整轮停止）。`test_author_quota_absent_falls_back_to_run_wide_cap` 应当 PASS（那正是当前行为）。`test_author_quota_leaves_small_authors_untouched` 也应 PASS。

- [ ] **Step 3: 加配置字段**

`src/pixiv_novel_sync/settings.py`，在 `SyncSettings` 的 `bookmark_max_pages_per_run: int | None = None` 之后插入：

```python
    # 单个关注作者在一轮 following_novels 里最多同步多少篇。None = 不限制（旧行为）。
    # 生产事故：max_items_per_run 只在切换作者之前检查，作者内部无上限，于是一个高产
    # 作者能连跑 60 篇（52 分钟）后触发整轮上限收工，日志写着 "synced 1/256 users"，
    # 256 个关注作者全圈需要 95 天。作者级配额把「每轮覆盖 users_limit 个作者」
    # 这个本意真正落实。
    following_max_novels_per_author: int | None = None
    # 系列章节分页专用上限。None = 跟随 max_pages_per_run（旧行为）。
    # 生产实测每轮 truncated_series=2：两个长系列被 max_pages_per_run=2 锁死，
    # 高频重跑也补不齐，同时另有 8 个系列缺 76 章。
    series_max_pages_per_run: int | None = None
```

在 `load_settings` 里，紧跟 `bookmark_max_pages_per_run=_coerce_optional_int(...)` 那行之后插入：

```python
            following_max_novels_per_author=_coerce_optional_int(sync_raw.get("following_max_novels_per_author")),
            series_max_pages_per_run=_coerce_optional_int(sync_raw.get("series_max_pages_per_run")),
```

- [ ] **Step 4: 改 `_sync_author` 加作者级配额**

在 `src/pixiv_novel_sync/sync_engine.py` 的 `_resolve_bookmark_max_pages`（`:96`）之后插入解析助手：

```python
def _resolve_author_novel_quota(settings: Any) -> int | None:
    """单作者单轮同步上限：``sync.following_max_novels_per_author``，None 表示不限。"""
    raw = getattr(getattr(settings, "sync", None), "following_max_novels_per_author", None)
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
```

然后在 `sync_following_novels` 里，把 `user_progress_total = users_limit or 0`（`:880`）那行之后改成：

```python
        user_progress_total = users_limit or 0
        author_quota = _resolve_author_novel_quota(self.settings)
```

在 `_sync_author` 内部（`:907` 附近，`next_novel_query` 初始化处）加计数器：

```python
            next_novel_query: dict[str, Any] | None = {"user_id": author_id}
            author_page_count = 0
            existing_streak = 0
            stop_author_scan = False
            author_synced = 0  # 该作者本轮实际同步（非跳过）的篇数
```

在同步成功的分支里累加。找到 `_sync_author` 内的这段（`:985-987`）：

```python
                    # 只有实际同步（非跳过）才计入 synced_items
                    if counters.get("novels", 0) > 0:
                        synced_items += 1
```

改成：

```python
                    # 只有实际同步（非跳过）才计入 synced_items
                    if counters.get("novels", 0) > 0:
                        synced_items += 1
                        author_synced += 1
                        # 作者级配额：撞到上限就换下一个作者，而不是让这一个作者
                        # 把整轮的 max_items_per_run 吃光（那会导致每轮只覆盖 1 人）
                        if author_quota is not None and author_synced >= author_quota:
                            logger.info(
                                "作者 %s 已达单轮配额 %d 篇，转向下一个作者",
                                author_id,
                                author_quota,
                            )
                            stats["authors_capped"] = stats.get("authors_capped", 0) + 1
                            stop_author_scan = True
```

`stop_author_scan` 已有的两处 `break`（`:1008-1012`）会把它带出翻页循环，无需额外改动。

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_sync_engine_incremental.py -v -k "prolific or author_quota"`

Expected: 3 passed

- [ ] **Step 6: 跑全套 sync_engine 测试**

Run: `pytest tests/test_sync_engine_incremental.py -q`

Expected: 全部 PASS（含那条 grep 裸 `time.sleep(` 的测试）

- [ ] **Step 7: 接线设置保存与序列化**

`src/pixiv_novel_sync/web/managers.py:save_sync_settings`，在 `sync_data["bookmark_max_pages_per_run"] = _normalize_optional_int(...)` 那段之后插入：

```python
        sync_data["following_max_novels_per_author"] = _normalize_optional_int(
            payload.get("following_max_novels_per_author", sync_data.get("following_max_novels_per_author"))
        )
        sync_data["series_max_pages_per_run"] = _normalize_optional_int(
            payload.get("series_max_pages_per_run", sync_data.get("series_max_pages_per_run"))
        )
```

`src/pixiv_novel_sync/web/utils.py:_settings_to_dict`，在 `"bookmark_max_pages_per_run": settings.sync.bookmark_max_pages_per_run,` 之后插入：

```python
        "following_max_novels_per_author": settings.sync.following_max_novels_per_author,
        "series_max_pages_per_run": settings.sync.series_max_pages_per_run,
```

- [ ] **Step 8: 写并跑 round-trip 测试**

追加到 `tests/test_webapp_settings.py`：

```python
def test_save_sync_settings_round_trips_new_throughput_fields(tmp_path):
    """两个新吞吐字段必须能存能读（漏接线不会报错，只会静默失效）。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("sync:\n  max_pages_per_run: 2\n", encoding="utf-8")

    saved = SettingsManager(str(config_path)).save_sync_settings(
        {"following_max_novels_per_author": 20, "series_max_pages_per_run": 10}
    )

    assert saved["following_max_novels_per_author"] == 20
    assert saved["series_max_pages_per_run"] == 10
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["sync"]["following_max_novels_per_author"] == 20
    assert config["sync"]["series_max_pages_per_run"] == 10


def test_save_sync_settings_preserves_absent_throughput_fields(tmp_path):
    """未传这两个字段时保留原值，不能被覆盖成 None。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "sync:\n  following_max_novels_per_author: 20\n  series_max_pages_per_run: 10\n",
        encoding="utf-8",
    )

    saved = SettingsManager(str(config_path)).save_sync_settings({"max_items_per_run": 30})

    assert saved["following_max_novels_per_author"] == 20
    assert saved["series_max_pages_per_run"] == 10
```

Run: `pytest tests/test_webapp_settings.py -v -k "throughput"`

Expected: 2 passed

- [ ] **Step 9: 更新配置示例**

`config/config.yaml.example`，在 `bookmark_max_pages_per_run: 20` 之后插入：

```yaml
  # 单个关注作者在一轮 following_novels 里最多同步多少篇。留空 = 不限制。
  # 留空时一个高产作者能吃掉整轮 max_items_per_run 配额，导致每轮只覆盖 1 个作者。
  following_max_novels_per_author: 20
  # 系列章节分页专用上限。留空则跟随 max_pages_per_run。
  # max_pages_per_run=2 会让长系列永远补不齐章节。
  series_max_pages_per_run: 10
```

- [ ] **Step 10: Commit**

```bash
git add src/pixiv_novel_sync/settings.py src/pixiv_novel_sync/sync_engine.py src/pixiv_novel_sync/web/managers.py src/pixiv_novel_sync/web/utils.py config/config.yaml.example tests/test_sync_engine_incremental.py tests/test_webapp_settings.py
git commit -m "fix: 关注作者同步改为每作者独立配额"
```

---

## Task 5: 系列章节独立分页上限

**背景**：`subscribed_series` 连续 12 轮 `series_synced: 0, novels: 0`，但每轮 `truncated_series: 2`——两个长系列被 `max_pages_per_run=2` 锁死，高频重跑也补不齐；库里另有 8 个订阅系列缺 76 章。配置字段已在 Task 4 Step 3 加好（`series_max_pages_per_run`），本任务只做 `sync_engine` 侧的解析与使用。

**Files:**
- Modify: `src/pixiv_novel_sync/sync_engine.py`（新增 `_resolve_series_max_pages`，用在 `:1402` 的 `series_safety_limit`）
- Test: `tests/test_sync_engine_incremental.py`

**Interfaces:**
- Consumes: Task 4 Step 3 加的 `settings.sync.series_max_pages_per_run`
- Produces: `_resolve_series_max_pages(settings) -> int | None`，语义与既有 `_resolve_bookmark_max_pages` 完全一致（返回 None 表示交给调用方的 100 页兜底）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_sync_engine_incremental.py`：

```python
class _PagedSeriesApi:
    """一个系列、章节分多页返回，用于验证系列专用分页上限。"""

    def __init__(self, page_count: int) -> None:
        self.page_count = page_count
        self.series_pages_fetched = 0

    def novel_series(self, series_id: int, last_order=None):
        self.series_pages_fetched += 1
        page_index = int(last_order or 0)
        next_url = (
            None
            if page_index >= self.page_count - 1
            else f"https://app-api.pixiv.net/v1/novel/series?last_order={page_index + 1}"
        )
        return {
            "novel_series_detail": {
                "id": series_id,
                "title": "长系列",
                "caption": "",
                "user": {"id": 1, "name": "author"},
                "published_total": self.page_count,
            },
            "novels": [{"id": 5000 + page_index, "title": f"第{page_index}章"}],
            "next_url": next_url,
        }

    def parse_qs(self, next_url):
        if not next_url:
            return None
        return {"last_order": str(next_url).split("=")[1]}


def test_series_pagination_uses_its_own_page_cap(tmp_path: Path) -> None:
    """系列章节分页不能被 max_pages_per_run 砍掉。

    回归：生产每轮 truncated_series=2，两个长系列被 max_pages_per_run=2 锁死，
    连续 12 轮高频重跑都补不齐（另有 8 个系列缺 76 章）。
    """
    settings = _settings(tmp_path)
    settings.sync.max_pages_per_run = 2
    settings.sync.series_max_pages_per_run = 10
    assert sync_engine._resolve_series_max_pages(settings) == 10


def test_series_page_cap_falls_back_to_shared_cap(tmp_path: Path) -> None:
    """未配置系列专用上限时保持旧行为：跟随 max_pages_per_run。"""
    settings = _settings(tmp_path)
    settings.sync.max_pages_per_run = 2
    settings.sync.series_max_pages_per_run = None
    assert sync_engine._resolve_series_max_pages(settings) == 2


def test_series_page_cap_treats_zero_as_unlimited(tmp_path: Path) -> None:
    """0 / 负数视为不限制，交给调用方的 100 页兜底（与收藏上限同语义）。"""
    settings = _settings(tmp_path)
    settings.sync.max_pages_per_run = 2
    settings.sync.series_max_pages_per_run = 0
    assert sync_engine._resolve_series_max_pages(settings) is None


def test_subscribed_series_uses_series_page_cap(tmp_path: Path) -> None:
    """订阅系列同步实际使用系列专用上限翻页。

    `web_cookie` 为 None 时 sync_subscribed_series 会回落到「从 DB 读
    is_subscribed=1 的系列」这条路径（sync_engine.py:1254 附近），正好适合测试。
    """
    settings = _settings(tmp_path)
    settings.sync.max_pages_per_run = 2
    settings.sync.series_max_pages_per_run = 6
    settings.pixiv.web_cookie = None
    db = Database(settings.storage.db_path)
    db.init_schema()
    db.upsert_user(UserRecord(user_id=1, name="author", account="acc", raw_json="{}"))
    db.conn.execute(
        "INSERT INTO series (series_id, title, user_id, total_novels, is_subscribed)"
        " VALUES (777, '长系列', 1, 6, 1)"
    )
    db.conn.commit()
    api = _PagedSeriesApi(page_count=6)
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    try:
        stats = service.sync_subscribed_series()
    finally:
        db.close()

    # 6 页全部取回，没有停在第 2 页
    assert api.series_pages_fetched == 6
    assert stats.get("truncated_series", 0) == 0
```

若这条集成型断言因 `sync_subscribed_series` 的其他依赖（如 `repair_blank_series_titles`、章节落盘）而难以跑通，**保留前三条纯函数测试即可**，把这条改成只断言 `_resolve_series_max_pages` 被用在了源码里：

```python
def test_subscribed_series_wires_series_page_cap(tmp_path: Path) -> None:
    """确认系列章节翻页确实调用了专用上限解析函数，而不是直接读 max_pages_per_run。"""
    source = Path(sync_engine.__file__).read_text(encoding="utf-8")
    assert "series_safety_limit = _resolve_series_max_pages(self.settings) or 100" in source
    assert "series_safety_limit = self.settings.sync.max_pages_per_run" not in source
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_sync_engine_incremental.py -v -k "series_page or series_pagination or subscribed_series_uses"`

Expected: 全部 FAIL，前三个报 `AttributeError: module 'pixiv_novel_sync.sync_engine' has no attribute '_resolve_series_max_pages'`。

- [ ] **Step 3: 实现解析助手**

在 `src/pixiv_novel_sync/sync_engine.py` 的 `_resolve_author_novel_quota`（Task 4 加的）之后插入：

```python
def _resolve_series_max_pages(settings: Any) -> int | None:
    """系列章节分页专用上限：优先 ``sync.series_max_pages_per_run``，缺省回落。

    返回 None 表示"不额外限制"，由调用方的 100 页兜底接管。语义与
    ``_resolve_bookmark_max_pages`` 完全一致。生产实测每轮 truncated_series=2：
    两个长系列被共享的 max_pages_per_run=2 锁死，重跑多少轮都补不齐章节。
    """
    sync_settings = getattr(settings, "sync", None)
    raw = getattr(sync_settings, "series_max_pages_per_run", None)
    if raw in (None, ""):
        return getattr(sync_settings, "max_pages_per_run", None)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return getattr(sync_settings, "max_pages_per_run", None)
    return value if value > 0 else None
```

- [ ] **Step 4: 在系列章节翻页处使用它**

`src/pixiv_novel_sync/sync_engine.py:1402`，把

```python
                                series_safety_limit = self.settings.sync.max_pages_per_run or 100  # 3.3翻页上限兜底
```

改为

```python
                                # 系列章节分页用专属上限：与收藏同理，共享 max_pages_per_run=2
                                # 会让长系列永远补不齐（生产每轮 truncated_series=2）
                                series_safety_limit = _resolve_series_max_pages(self.settings) or 100
```

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_sync_engine_incremental.py -v -k "series_page or series_pagination or subscribed_series_uses"`

Expected: 4 passed

- [ ] **Step 6: 跑全套 sync_engine 测试**

Run: `pytest tests/test_sync_engine_incremental.py -q`

Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add src/pixiv_novel_sync/sync_engine.py tests/test_sync_engine_incremental.py
git commit -m "fix: 系列章节分页改用独立上限"
```

---

## Task 6: 受限用户降频巡检

**背景**：生产 `user_status` 有 6 个用户（73342541 / 127445288 / 59683986 / 86295739 / 13766533 / 37152734）每轮都返回「您的访问权限已经被限制了」。这是账号级权限限制而非限流，三态判定正确地留在 `unknown`（`web/utils.py:_check_pixiv_user_status`），但它们每轮都消耗 `consecutive_unknown` 熔断额度（`MAX_CONSECUTIVE_UNKNOWN = 15`）。

改法：`users` 表加 `unknown_streak` 列统计连续判不出来的次数；`get_users_for_status_check` 把连续 unknown 超过阈值的用户排到最后并按周期跳过。不引入新表，不改熔断常数。

**Files:**
- Modify: `src/pixiv_novel_sync/storage/schema.py`（`_migrate_users_table` 加列）
- Modify: `src/pixiv_novel_sync/storage/users.py`（`upsert_user_status` 维护计数、`get_users_for_status_check` 降频）
- Test: `tests/test_storage_db.py`、`tests/test_status_check_classification.py`

**Interfaces:**
- Consumes: 现有 `db.upsert_user_status(user_id, status) -> None`（`storage/users.py:55`）、`db.get_users_for_status_check(limit=None) -> list[dict]`（`:33`）
- Produces: `users.unknown_streak INTEGER NOT NULL DEFAULT 0`；`UNKNOWN_STREAK_DEFER_THRESHOLD = 5` 模块常量（`storage/users.py`）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_storage_db.py`：

```python
def test_user_status_tracks_unknown_streak(db: Database) -> None:
    """连续判不出来要累加计数，判出来立刻清零。

    生产实测 6 个用户每轮都回「您的访问权限已经被限制了」，一直占用
    MAX_CONSECUTIVE_UNKNOWN=15 的熔断额度。
    """
    db.upsert_user(UserRecord(user_id=9, name="u", account="acc", raw_json="{}"))

    db.upsert_user_status(9, "unknown")
    db.upsert_user_status(9, "unknown")
    assert db.conn.execute("SELECT unknown_streak FROM users WHERE user_id = 9").fetchone()[0] == 2

    db.upsert_user_status(9, "normal")
    assert db.conn.execute("SELECT unknown_streak FROM users WHERE user_id = 9").fetchone()[0] == 0


def test_status_check_defers_persistently_unknown_users(db: Database) -> None:
    """连续 unknown 超阈值的用户排到队尾，让正常用户先被检查。"""
    from pixiv_novel_sync.storage.users import UNKNOWN_STREAK_DEFER_THRESHOLD

    for uid in (1, 2, 3):
        db.upsert_user(UserRecord(user_id=uid, name=f"u{uid}", account="acc", raw_json="{}"))
    # user 1 是长期受限的：streak 打满，且刚检查过
    db.conn.execute(
        "UPDATE users SET unknown_streak = ?, last_checked_at = '2000-01-01 00:00:00' WHERE user_id = 1",
        (UNKNOWN_STREAK_DEFER_THRESHOLD,),
    )
    # user 2、3 从未检查
    db.conn.commit()

    ordered = [u["user_id"] for u in db.get_users_for_status_check()]

    # 尽管 user 1 的 last_checked_at 最老，它也必须排在最后
    assert ordered[-1] == 1
    assert set(ordered[:2]) == {2, 3}


def test_users_migration_adds_unknown_streak_column(tmp_path: Path) -> None:
    """旧库没有 unknown_streak 列时迁移要补上（幂等 ALTER TABLE）。"""
    db_path = tmp_path / "legacy-users.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                account TEXT,
                raw_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("INSERT INTO users (user_id, name, raw_json) VALUES (1, 'u', '{}')")

    db = Database(db_path)
    try:
        db.init_schema()
        columns = {row[1] for row in db.conn.execute("PRAGMA table_info(users)")}
        assert "unknown_streak" in columns
        assert db.conn.execute("SELECT unknown_streak FROM users WHERE user_id = 1").fetchone()[0] == 0
    finally:
        db.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_storage_db.py -v -k "unknown_streak or persistently_unknown"`

Expected: 3 FAIL（`sqlite3.OperationalError: no such column: unknown_streak` / `ImportError: cannot import name 'UNKNOWN_STREAK_DEFER_THRESHOLD'`）。

- [ ] **Step 3: 建表与迁移加列**

`src/pixiv_novel_sync/storage/schema.py`，`init_schema()` 里 `CREATE TABLE IF NOT EXISTS users` 的定义（`:21-29`）加一列，放在 `last_checked_at TEXT,` 之后：

```sql
                unknown_streak INTEGER NOT NULL DEFAULT 0,
```

`_migrate_users_table()`（`storage/schema.py:267`）里补幂等加列，紧跟既有的 `last_checked_at` 判断之后：

```python
        if "last_checked_at" not in columns:
            self.conn.execute("ALTER TABLE users ADD COLUMN last_checked_at TEXT")
        if "unknown_streak" not in columns:
            self.conn.execute("ALTER TABLE users ADD COLUMN unknown_streak INTEGER NOT NULL DEFAULT 0")
```

- [ ] **Step 4: 维护计数并降频**

`src/pixiv_novel_sync/storage/users.py`，在文件顶部 `UNKNOWN_STATUS = "unknown"`（第 7 行）之后加阈值常量：

```python
# 连续多少轮判不出状态就把该用户降频到队尾。生产实测 6 个用户每轮都回
# 「您的访问权限已经被限制了」——那是账号级权限限制而非限流，三态判定
# 正确地留在 unknown，但它们会一直占用 MAX_CONSECUTIVE_UNKNOWN 的熔断额度。
UNKNOWN_STREAK_DEFER_THRESHOLD = 5
```

改 `upsert_user_status`（`:55-68`）为：

```python
    def upsert_user_status(self, user_id: int, status: str) -> None:
        """更新用户状态；status 为 "unknown" 时只刷新 last_checked_at，不改写 status。

        同时维护 unknown_streak：连续判不出来就累加，一旦判出来立刻清零。
        超过 UNKNOWN_STREAK_DEFER_THRESHOLD 的用户会被 get_users_for_status_check
        排到队尾，避免长期受限的账号一直挤占熔断额度。
        """
        with self._lock:
            if status == UNKNOWN_STATUS:
                self.conn.execute(
                    "UPDATE users SET last_checked_at = CURRENT_TIMESTAMP,"
                    " unknown_streak = unknown_streak + 1 WHERE user_id = ?",
                    (user_id,),
                )
            else:
                self.conn.execute(
                    "UPDATE users SET status = ?, last_checked_at = CURRENT_TIMESTAMP,"
                    " unknown_streak = 0 WHERE user_id = ?",
                    (status, user_id),
                )
            self._commit_if_needed()
```

改 `get_users_for_status_check` 的 SQL（`:43-47`），在既有排序键前面加一个降频分桶：

```python
        sql = (
            "SELECT user_id, name FROM users "
            # 第一排序键：长期判不出状态的用户降到队尾（值为 0/1）。这类账号是
            # 权限受限而非限流，每轮重试只会白占 MAX_CONSECUTIVE_UNKNOWN 额度。
            "ORDER BY (unknown_streak >= ?), "
            # (last_checked_at IS NOT NULL) 为 0/1，保证 NULL（从未检查）永远排最前
            "(last_checked_at IS NOT NULL), last_checked_at, user_id"
        )
        params: tuple[Any, ...] = (UNKNOWN_STREAK_DEFER_THRESHOLD,)
        if limit is not None and int(limit) > 0:
            sql += " LIMIT ?"
            params = (UNKNOWN_STREAK_DEFER_THRESHOLD, int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        return [{"user_id": row["user_id"], "name": row["name"]} for row in rows]
```

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_storage_db.py -v -k "unknown_streak or persistently_unknown"`

Expected: 3 passed

- [ ] **Step 6: 跑状态检查相关测试**

Run: `pytest tests/test_storage_db.py tests/test_status_check_classification.py tests/test_jobs_services.py -q`

Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add src/pixiv_novel_sync/storage/schema.py src/pixiv_novel_sync/storage/users.py tests/test_storage_db.py
git commit -m "fix: 长期判不出状态的用户降频巡检"
```

---

## Task 7: 新 cron 排布与文档

**背景**：所有代码改动完成后，才能按新的耗时基线重排预算。这一步只改配置默认值与文档，不改逻辑。生产 `config/config.yaml` 不在仓库里，需要单独部署（见 Task 8）。

新排布（时区 `Asia/Seoul`，源自 spec §5.2）：

| 任务 | 新 cron | 变化 |
|---|---|---|
| bookmarks | `20 0,4,8,12,16,20 * * *` | 不变（P1，6 次/天） |
| subscribed_series | `40 1,13 * * *` | 4 → 2 次/天 |
| following_novels | `0 3,9,15,21 * * *` | 不变（4 次/天，但单轮 42 → 24 分钟） |
| novel_status | `0 5,17 * * *` | 4 → 2 次/天 |
| series_status | `30 18 */2 * *` | 不变（隔日） |
| user_status | `30 6 */2 * *` | 不变（隔日） |
| following_list | `30 10 * * *` | 不变（每天） |
| user_backup | `30 2 */3 * *` | 不变（隔三日） |
| pending_deletion_detection | `30 12 * * *` | 不变（每天） |
| preference_analyze | `15 7,19 * * *` | interval 12h → 显式 cron |
| recommendation_run | `50 8 * * *` | interval 3h → 每天 1 次 |

**Files:**
- Modify: `config/config.yaml.example`（新 cron 排布）
- Modify: `docs/JOB_SYSTEM.md`（§5 配置矩阵表）
- Test: `tests/test_cron_validation.py`、`tests/test_scheduler_priority.py`

**Interfaces:**
- Consumes: `settings.py:cron_to_next_run(cron, base_time, tz_name) -> float | None`（已存在，用于校验）；`web/managers.py:SCHEDULER_TASK_CONFIGS`（11 项，不修改）
- Produces: 无代码接口，仅配置与文档

- [ ] **Step 1: 写 cron 合法性与注册表完整性测试**

追加到 `tests/test_cron_validation.py`：

```python
def test_recommended_cron_schedule_is_parseable() -> None:
    """spec §5.2 的新排布必须全部能被 cron_to_next_run 解析。

    非法 cron 会被 save_sync_settings 拒绝（ValueError），但 config.yaml.example
    是直接被复制使用的，不经过那道校验——所以必须在这里锁住。
    """
    schedule = {
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
    for task_name, expr in schedule.items():
        assert cron_to_next_run(expr, _BASE, "Asia/Seoul") is not None, f"{task_name}: {expr}"


def test_config_example_uses_recommended_cron() -> None:
    """config.yaml.example 里的 cron 必须与上面那张表一致，别让示例配置漂移。"""
    from pathlib import Path

    import yaml

    sync = yaml.safe_load(Path("config/config.yaml.example").read_text(encoding="utf-8"))["sync"]

    assert sync["auto_sync_bookmarks_cron"] == "20 0,4,8,12,16,20 * * *"
    assert sync["auto_sync_subscribed_series_cron"] == "40 1,13 * * *"
    assert sync["auto_sync_following_novels_cron"] == "0 3,9,15,21 * * *"
    assert sync["auto_sync_novel_status_cron"] == "0 5,17 * * *"
    assert sync["auto_sync_preference_analyze_cron"] == "15 7,19 * * *"
    assert sync["auto_sync_recommendation_run_cron"] == "50 8 * * *"
    # 示例模板保持 UTC：KST 是生产机的设定，不该写进仓库
    assert sync["auto_sync_timezone"] == "UTC"


def test_config_example_declares_throughput_fields() -> None:
    """Task 4/5 的两个新字段必须出现在示例配置里，否则新部署会静默用旧行为。"""
    from pathlib import Path

    import yaml

    sync = yaml.safe_load(Path("config/config.yaml.example").read_text(encoding="utf-8"))["sync"]

    assert sync["following_max_novels_per_author"] == 20
    assert sync["series_max_pages_per_run"] == 10
```

`cron_to_next_run` 与 `_BASE` 已在该文件顶部导入/定义（`tests/test_cron_validation.py:14` 与 `:18`），无需重复 import。

追加到 `tests/test_scheduler_priority.py`：

```python
def test_every_scheduler_task_maps_through_all_three_registries() -> None:
    """11 个 scheduler task 必须在三处注册表里齐全。

    漏注册不会报错，只会静默降级：_job_spec 缺分支 → JobSpec 落到 JobType.SYNC
    统计归错类；TASK_LABELS 缺条目 → 任务日志页显示英文键名。
    2026-08-28 已核对当时全部齐全，这条断言防止后续新增任务时回归。
    """
    from pixiv_novel_sync.jobs.tasks import _TASK_LABELS
    from pixiv_novel_sync.web.managers import SCHEDULER_TASK_CONFIGS, TASK_LABELS
    from pixiv_novel_sync.web.utils import _scheduler_job_spec

    for config in SCHEDULER_TASK_CONFIGS:
        name = config["name"]
        assert name in TASK_LABELS, f"web/managers.py:TASK_LABELS 缺 {name}"
        spec = _scheduler_job_spec(name)
        assert len(spec.task_types) == 1
        task_type = spec.task_types[0]
        assert task_type in _TASK_LABELS, f"jobs/tasks.py:_TASK_LABELS 缺 {task_type}"


def test_status_and_preference_tasks_do_not_fall_back_to_sync_job_type() -> None:
    """状态检查/偏好/推荐/备份/删除检测必须落到各自的 JobType，不能退化成 SYNC。"""
    from pixiv_novel_sync.jobs.models import JobType
    from pixiv_novel_sync.web.utils import _scheduler_job_spec

    expected = {
        "user_status": JobType.STATUS_CHECK,
        "novel_status": JobType.STATUS_CHECK,
        "series_status": JobType.STATUS_CHECK,
        "user_backup": JobType.USER_BACKUP,
        "pending_deletion_detection": JobType.PENDING_DELETION_DETECTION,
        "preference_analyze": JobType.PREFERENCE_ANALYZE,
        "recommendation_run": JobType.RECOMMENDATION_RUN,
    }
    for task_name, job_type in expected.items():
        assert _scheduler_job_spec(task_name).job_type is job_type, task_name
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_cron_validation.py tests/test_scheduler_priority.py -v -k "recommended_cron or config_example or three_registries or fall_back"`

Expected: `test_config_example_uses_recommended_cron` 与 `test_config_example_declares_throughput_fields` FAIL（示例配置还是旧值 / 缺键）。另三条应当 PASS——它们锁的是已核实正确的现状。

- [ ] **Step 3: 更新 `config/config.yaml.example`**

**注意两点**：(1) 示例文件的 `auto_sync_timezone` 保持 `UTC` 不变——KST 是这台生产机的设定，不该写进仓库模板；cron 的**错峰间距**才是被推荐的东西，与绝对时区无关。(2) 不要改任何 `*_enabled` 的值，那是模板默认。

把 `sync:` 块里从 `# 各任务开关 / interval / cron` 到 `auto_sync_recommendation_run_cron` 之间的内容替换为：

```yaml
  # 各任务开关 / interval / cron。cron 优先于 interval；cron 留空时使用 interval。
  # 下面这套 cron 排布来自 2026-08-28 的生产实测（3 天 52 条 task_logs）：所有任务
  # 共用一个 job 槽（BoundedSemaphore(1)）串行执行，所以时刻要错开，且预算要按
  # 「用户在意程度 ÷ 单轮耗时」分配。时刻在 auto_sync_timezone 时区下解释。
  auto_sync_bookmarks_enabled: true
  auto_sync_bookmarks_interval_hours: 4
  auto_sync_bookmarks_cron: "20 0,4,8,12,16,20 * * *"

  auto_sync_following_list_enabled: true
  auto_sync_following_list_interval_hours: 24
  auto_sync_following_list_cron: "30 10 * * *"

  auto_sync_following_novels_enabled: true
  auto_sync_following_novels_interval_hours: 6
  auto_sync_following_novels_cron: "0 3,9,15,21 * * *"
  # 0 = 不限制每轮扫描的关注用户数。配合 following_max_novels_per_author 使用：
  # 前者决定"每轮覆盖几个作者"，后者决定"每个作者最多几篇"
  auto_sync_following_novels_users_limit: 0

  auto_sync_user_status_enabled: true
  auto_sync_user_status_interval_hours: 48
  auto_sync_user_status_cron: "30 6 */2 * *"

  # 小说状态巡检预算占用最大（每轮 800 篇约 26 分钟）而时效性最弱，每天 2 次即可。
  # 用户主动取消收藏/追更由 pending_detection 每天检测，不依赖这个任务
  auto_sync_novel_status_enabled: true
  auto_sync_novel_status_interval_hours: 12
  auto_sync_novel_status_cron: "0 5,17 * * *"

  auto_sync_series_status_enabled: true
  auto_sync_series_status_interval_hours: 48
  auto_sync_series_status_cron: "30 18 */2 * *"

  # 追更系列每轮只是确认既有章节没变（生产实测连续 12 轮零产出），每天 2 次即可。
  # 缺章要靠 series_max_pages_per_run 放开分页上限解决，而不是靠提高频率
  auto_sync_subscribed_series_enabled: true
  auto_sync_subscribed_series_interval_hours: 12
  auto_sync_subscribed_series_cron: "40 1,13 * * *"

  auto_sync_user_backup_enabled: false
  auto_sync_user_backup_interval_hours: 72
  auto_sync_user_backup_cron: "30 2 */3 * *"

  auto_sync_pending_detection_enabled: true
  auto_sync_pending_detection_interval_hours: 24
  auto_sync_pending_detection_cron: "30 12 * * *"

  # 偏好分析是纯本地统计，不消耗 Pixiv 配额
  auto_sync_preference_analyze_enabled: false
  auto_sync_preference_analyze_interval_hours: 12
  auto_sync_preference_analyze_cron: "15 7,19 * * *"
  preference_analyze_batch_size: 200

  # 定时生成推荐：需要先有默认偏好画像，且会消耗 Pixiv 搜索配额，默认关闭
  auto_sync_recommendation_run_enabled: false
  auto_sync_recommendation_run_interval_hours: 24
  auto_sync_recommendation_run_cron: "50 8 * * *"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_cron_validation.py tests/test_scheduler_priority.py -v -k "recommended_cron or config_example or three_registries or fall_back"`

Expected: 5 passed

- [ ] **Step 5: 更新 `docs/JOB_SYSTEM.md` §5 配置矩阵**

把 §5 那张「各任务默认值」表的 cron 列改成新值，并在 `bookmarks` 行的「附加字段」列补 `series_max_pages_per_run` 与 `following_max_novels_per_author` 的说明：

- `following_novels` 行的附加字段追加：`following_max_novels_per_author`（单作者单轮上限，留空 = 不限；撞到上限的作者数会记入 `stats["authors_capped"]`）
- `subscribed_series` 行的附加字段追加：`series_max_pages_per_run`（章节分页上限，留空跟随 `max_pages_per_run`）
- 在 §5 表格下方加一段说明段落：

```markdown
2026-08-28 依据生产实测（3 天 52 条 task_logs）重排：`subscribed_series` 与
`novel_status` 各从 4 次/天降到 2 次/天，收藏保持 6 次/天不变。这不是为了躲
Pixiv 限流——72 小时 journald 内零条真实 429，此前所有 `aborted_reason` 都是
本地熔断误判（见 `docs/superpowers/specs/2026-08-28-sync-budget-and-settings-redesign-design.md` §2）。
降频的真正原因是：`novel_status` 占预算 33% 却时效性最弱，`subscribed_series`
连续 12 轮零产出。省下的预算留作余量，不再投入。
```

- [ ] **Step 6: 跑文档相关测试**

Run: `pytest tests/test_ai_model_docs.py tests/test_deployment_contract.py tests/test_recommendation_scheduling.py -q`

Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add config/config.yaml.example docs/JOB_SYSTEM.md tests/test_cron_validation.py tests/test_scheduler_priority.py
git commit -m "chore: 依据实测重排定时任务预算"
```

---

## Task 8: 全量验证与生产部署

**Files:**
- Modify: `CLAUDE.md`（架构小节补新字段与 FTS 不变式）
- 生产 `~/pixiv-novel-sync/config/config.yaml`（不在仓库）

**Interfaces:**
- Consumes: Task 1–7 全部改动
- Produces: 生产环境跑在新配置上

- [ ] **Step 1: 跑全量测试**

Run: `pytest -q`

Expected: 全部 PASS。基线是 1308 passed / 4 skipped；本计划新增约 20 条测试，所以期望 ≈1328 passed / 4 skipped，**0 failed**。若有 fail，先修再继续，不要带着红灯部署。

- [ ] **Step 2: 跑静态检查**

Run: `python -m compileall -q src`

Expected: 无输出（无语法错误）

- [ ] **Step 3: 更新 `CLAUDE.md`**

在「Rate limiting, circuit breakers, and resumability」小节的 resumability 段落里，把 bookmark 页帽那句扩成三个页帽/配额并列，并在 Storage 小节加一条 FTS 不变式：

```markdown
- **`novel_fts` 的 `rowid` 必须等于 `novel_id`。** 该表把 `novel_id` 声明为 `UNINDEXED`，所以 `WHERE novel_id = ?` 会全表扫描整个 FTS 索引（生产 1 GB，实测单次 39 秒）。写入时显式指定 `rowid`，读写与删除都按 `rowid` 定位，搜索子查询取 `SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?`。`storage/schema.py:_migrate_novel_fts_rowid` 会在启动时探测错位并重建。
```

- [ ] **Step 4: Commit 文档**

```bash
git add CLAUDE.md
git commit -m "docs: 补充 FTS rowid 不变式与新增同步配额字段"
```

- [ ] **Step 5: 推送**

```bash
git push -u origin main
```

（当前分支就是 `main`，且这是用户自己的仓库。若 CI 或分支保护拒绝，改为先建分支再开 PR。）

- [ ] **Step 6: 部署并观察 FTS 重建**

```bash
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 'cd ~/pixiv-novel-sync && ./update.sh'
```

重启后立刻看重建日志（预期约 2 分钟）：

```bash
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 'sudo journalctl -u pixiv-novel-sync --since "5 min ago" --no-pager | grep -E "novel_fts|重建"'
```

Expected: 看到「检测到 novel_fts 的 rowid 与 novel_id 错位，开始重建全文索引」以及随后的「novel_fts 重建完成，共 7627 行」。若超过 5 分钟没出现完成日志，按 spec §8 切备选方案（后台重建）。

- [ ] **Step 7: 应用新的生产 cron**

生产 `config/config.yaml` 不在仓库里，需要就地编辑。改动项与 Task 7 Step 3 相同，另加三个新字段：

```bash
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 'cd ~/pixiv-novel-sync && cp config/config.yaml config/config.yaml.bak-20260828'
```

先备份，然后用编辑器改这几项：

```yaml
  following_max_novels_per_author: 20
  series_max_pages_per_run: 10
  auto_sync_subscribed_series_cron: 40 1,13 * * *
  auto_sync_novel_status_cron: 0 5,17 * * *
  auto_sync_preference_analyze_cron: 15 7,19 * * *
  auto_sync_recommendation_run_cron: 50 8 * * *
```

改完重启服务让 `SettingsManager` 重新加载：

```bash
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 'sudo systemctl restart pixiv-novel-sync && sleep 20 && systemctl is-active pixiv-novel-sync'
```

- [ ] **Step 8: 验证新排布已生效**

```bash
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 'sudo journalctl -u pixiv-novel-sync --since "3 min ago" --no-pager | grep "scheduled, next run"'
```

Expected: 11 个任务各一行 `next run`，其中 `subscribed_series` 与 `novel_status` 的下次运行时间落在新的 cron 时刻上。

- [ ] **Step 9: 观察一轮 `following_novels` 验证吞吐**

等到下一个 `0 3,9,15,21` 的 KST 时刻之后：

```bash
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 'cd ~/pixiv-novel-sync && python3 -c "
import sqlite3, json
c = sqlite3.connect(\"file:data/state/pixiv_sync.db?mode=ro\", uri=True)
r = c.execute(\"SELECT started_at, duration_seconds, stats_json FROM task_logs WHERE task_type=%s ORDER BY started_at DESC LIMIT 1\" % repr(\"following_novels\")).fetchone()
print(r[0], int(r[1]), \"s\")
print(json.dumps(json.loads(r[2]), ensure_ascii=False, indent=1, sort_keys=True))
"'
```

Expected: `following_users_scanned` 为 5（而非 1），`users_remaining` 每轮递减约 5，单轮耗时约 24 分钟（而非 42 分钟）。若 `following_users_scanned` 仍是 1，检查生产 `config.yaml` 里 `following_max_novels_per_author` 是否真的写进去了（`_settings_to_dict` 的输出可从 `GET /api/dashboard/settings` 确认）。

---

## Self-Review 记录

写完后按 writing-plans 的自审清单核对，发现并已修正的问题：

1. **spec 覆盖**：spec §4.1 → Task 1/2/3；§4.2 → Task 4；§4.3 → Task 5；§5.1 → Task 7 Step 5（文档）；§5.2/§5.3 → Task 7；§5.4 → 显式声明只铺路不改数值，落到阶段三的设置页分组（本计划不含）；§5.5 → Task 6（受限用户）+ Task 7 Step 1（三处注册表防回归断言）。spec §6（设置页与 AI 页面）**不在本计划范围**，需要单独一份计划。
2. **测试助手选型**：Task 4 最初写的是 `_RotationFakeDb`，但那个 fake 只有四个水位线方法，作者返回非空 `novels` 时会走进 `_sync_novel_inner` 并崩在 `db.conn`。已改为真实 `Database`，并在 Step 1 里说明原因。
3. **cron 表达式**：11 条全部用 `cron_to_next_run(expr, None, "Asia/Seoul")` 实机验证可解析（本地已跑），测试里改用文件既有的 `_BASE` 常量保证可复现。
4. **FTS 重建耗时**：最初按行数外推得 93 秒，实测样本只占全库字节的 19.7%，按字节量重算为 124 秒。计划里统一用「约 2 分钟」并给出 5 分钟的切换阈值。
5. **示例配置时区**：`config.yaml.example` 保持 `UTC`，不写 KST——那是这台生产机的设定。测试显式断言这一点，避免有人"顺手统一"。
6. **`logger` 可用性**：已确认 `storage/schema.py:8` 已有 `logger`，Task 3 不需要新增 import。
7. **Task 5 集成测试的退路**：`sync_subscribed_series` 依赖较多（watchlist 抓取、`repair_blank_series_titles`、章节落盘），已给出降级为源码断言的备选写法。

## 不在本计划范围

- spec §6 全部内容：设置页拆成四个一级页面、AI 公共层抽取、模型路由可视化。需要单独一份计划。
- 限速参数（`delay_seconds_*`）的具体数值调整——按 spec §5.4，要等 Task 8 上线后一周的新基线。
- `user_backup` 与 `following_novels` 的 `users_limit` 解耦。
- 删除 `novel_fts.novel_id` 冗余列。

# 阶段一：同步吞吐修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除同步链路的三个吞吐缺陷——FTS 全表扫描（单篇 40 秒）、`following_novels` 每轮只覆盖 1 个作者、系列章节被分页上限锁死——在不改动任何定时频率、不增加任何 Pixiv 请求的前提下把单篇耗时从 51 秒降到约 11 秒。

**Architecture:** 三组互相独立的改动。(1) `novel_fts` 的 `rowid` 对齐到 `novel_id`，按 ID 的读写从全表扫描 1 GB 索引变成 O(1) 主键查找，历史错位数据由一个幂等迁移整表重建。(2) `sync_engine._sync_author` 内部新增作者级配额，把「攒够 20 篇就收工」改成「每个作者最多 20 篇、跑满 `users_limit` 个作者」。(3) 系列章节分页获得独立上限，不再复用压制作者列表体量的那个 `max_pages_per_run`。

**Tech Stack:** Python 3.10+、SQLite（FTS5）、pytest。无新增依赖。

**Spec:** `docs/superpowers/specs/2026-08-28-sync-budget-and-settings-redesign-design.md`（§4 为本阶段依据；§2.4 为实测证据）

## Global Constraints

- 模块首行 `from __future__ import annotations`；dataclass 用 `slots=True`。
- 代码注释与用户可见字符串用中文；注释要写明**为什么**，尤其是回归原因。
- commit 主题遵循 Conventional Commits（`type: subject`），中文或英文均可。
- 重量级 import 放函数内部（延迟导入），保持 CLI 启动速度。
- 迁移必须是**幂等 DDL**，在每次 `init_schema()` 都重跑；无版本表、无迁移文件。加列一律 `PRAGMA table_info` 守卫 + `ALTER TABLE ... ADD COLUMN`。
- 写操作走 `db.transaction()`（`BEGIN IMMEDIATE`，可安全嵌套）。
- 禁止在 `sync_engine.py` 里出现裸 `time.sleep(`——`tests/test_sync_engine_incremental.py` 会 grep 源码并失败。只能用 `_sleep_with_progress_cancel` / `rate_limiter.cancellable_sleep` / `jobs/services._sleep_with_cancel`。
- 禁止用宽 `except Exception` 吞掉 `InterruptedError`（协作式取消依赖它冒泡），已有回归测试守着。
- 跑测试：`pytest`（全量约 6 分钟，基线 1308 passed / 4 skipped）。单文件 `pytest tests/test_storage_db.py`。静态检查只有 `python -m compileall -q src`；`black`/`flake8`/`mypy` 未配置，不要假设存在。
- 测试依赖 `tests/conftest.py` 的 autouse fixture（把 `PIXIV_DB_PATH` / `PIXIV_PUBLIC_DIR` / `PIXIV_PRIVATE_DIR` 指向 tmp 目录），不要自己 mock 路径。

## File Structure

| 文件 | 职责 | 本阶段改动 |
|---|---|---|
| `src/pixiv_novel_sync/storage/schema.py` | 幂等 DDL 与迁移 | 新增 `_migrate_novel_fts_rowid()` 并在 `init_schema()` 调用 |
| `src/pixiv_novel_sync/storage/novels.py` | novels 域存储 | `replace_fts` / `delete_novel` 改走 rowid；`list_recent_novels` 搜索改 `SELECT rowid` |
| `src/pixiv_novel_sync/storage/users.py` | users 域存储 | `delete_user` 清 FTS 改 rowid |
| `src/pixiv_novel_sync/storage/bookmarks.py` | 收藏列表查询 | 搜索改 `SELECT rowid` |
| `src/pixiv_novel_sync/storage/series.py` | 系列列表查询 | 搜索改 `SELECT rowid` |
| `src/pixiv_novel_sync/settings.py` | 配置加载 | `SyncSettings` 新增两个字段 |
| `src/pixiv_novel_sync/sync_engine.py` | Pixiv 同步引擎 | 新增两个 `_resolve_*` helper；`_sync_author` 加作者配额；系列分页用独立上限 |
| `src/pixiv_novel_sync/web/managers.py` | 设置读写 | `save_sync_settings` 支持两个新字段 |
| `src/pixiv_novel_sync/web/utils.py` | 设置序列化 | `_settings_to_dict` 暴露两个新字段 |
| `config/config.yaml.example` | 配置样例 | 两个新字段 + 注释 |

任务顺序有硬依赖：**Task 1（迁移）必须先于 Task 2/3**。原因是若先改写入路径，历史行的 `rowid` 仍错位，`DELETE WHERE rowid = ?` 删不掉旧行，会产生重复索引项。反过来则安全——迁移后旧代码的 `WHERE novel_id = ?` 仍然正确，只是慢。

---

### Task 1: `novel_fts` rowid 对齐迁移

**Files:**
- Modify: `src/pixiv_novel_sync/storage/schema.py`（`init_schema` 迁移调用列表在 131–163 行；新方法加在 `_migrate_novel_texts_table` 之后）
- Test: `tests/test_storage_db.py`

**Interfaces:**
- Consumes: 无（本阶段第一个任务）
- Produces: `SchemaMixin._migrate_novel_fts_rowid() -> None`。执行后保证 `novel_fts` 每行 `rowid == novel_id`。Task 2/3 依赖这条不变量。

- [ ] **Step 1: 写失败测试**

加到 `tests/test_storage_db.py` 末尾：

```python
def _legacy_fts_db(db_path: Path) -> None:
    """构造一个 rowid 与 novel_id 错位的旧库（复刻生产状态）。"""
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
                has_content INTEGER NOT NULL DEFAULT 0,
                text_markdown TEXT,
                text_hash TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE VIRTUAL TABLE novel_fts USING fts5(
                novel_id UNINDEXED, title, caption, author_name, body
            );
            """
        )
        conn.execute(
            "INSERT INTO users (user_id, name, raw_json) VALUES (7, '作者甲', '{}')"
        )
        for novel_id, title, body in (
            (25310744, "沉默的观测者", "第一篇正文内容"),
            (27380872, "海边的图书馆", "第二篇正文内容"),
        ):
            conn.execute(
                "INSERT INTO novels (novel_id, user_id, title, caption, visible, "
                "restrict_value, x_restrict, text_length, total_bookmarks, total_views, "
                "tags_json, raw_json, meta_hash) "
                "VALUES (?, 7, ?, '简介', 1, 'public', 0, 10, 0, 0, '[]', '{}', 'h')",
                (novel_id, title),
            )
            conn.execute(
                "INSERT INTO novel_texts (novel_id, text_raw, has_content, text_hash) "
                "VALUES (?, ?, 1, 'th')",
                (novel_id, body),
            )
            # 关键：不指定 rowid，让它自增成 1/2，与 novel_id 错位（生产实测 0 行对齐）
            conn.execute(
                "INSERT INTO novel_fts (novel_id, title, caption, author_name, body) "
                "VALUES (?, ?, '简介', '作者甲', ?)",
                (novel_id, title, body),
            )


def test_fts_migration_realigns_rowid_to_novel_id(tmp_path: Path) -> None:
    """历史库的 FTS rowid 全部错位，init_schema 必须整表重建并对齐。

    回归：novel_id 是 UNINDEXED 列，rowid 是自增值，两者零重合（生产 7627 行中
    对齐的有 0 行）。于是 DELETE ... WHERE novel_id = ? 要全表扫描 1 GB 索引，
    实测单次 39 秒，每同步一篇小说付一次。
    """
    db_path = tmp_path / "legacy-fts.db"
    _legacy_fts_db(db_path)

    with sqlite3.connect(db_path) as conn:
        before = conn.execute("SELECT rowid, novel_id FROM novel_fts ORDER BY rowid").fetchall()
    assert before == [(1, 25310744), (2, 27380872)]

    db = Database(db_path)
    db.init_schema()
    try:
        rows = db.conn.execute("SELECT rowid, novel_id FROM novel_fts ORDER BY rowid").fetchall()
        assert [(r[0], r[1]) for r in rows] == [
            (25310744, 25310744),
            (27380872, 27380872),
        ]
    finally:
        db.close()


def test_fts_migration_preserves_match_results(tmp_path: Path) -> None:
    """重建后全文检索必须仍能命中，且返回的 rowid 就是 novel_id。"""
    db_path = tmp_path / "legacy-fts-match.db"
    _legacy_fts_db(db_path)

    db = Database(db_path)
    db.init_schema()
    try:
        hits = db.conn.execute(
            "SELECT rowid FROM novel_fts WHERE novel_fts MATCH ? ORDER BY rowid",
            ('"正文内容"',),
        ).fetchall()
        assert [row[0] for row in hits] == [25310744, 27380872]

        title_hits = db.conn.execute(
            "SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?",
            ('"图书馆"',),
        ).fetchall()
        assert [row[0] for row in title_hits] == [27380872]
    finally:
        db.close()


def test_fts_migration_is_idempotent(tmp_path: Path) -> None:
    """已对齐的库上重复 init_schema 必须是 no-op，不重建、不丢数据。"""
    db_path = tmp_path / "aligned-fts.db"
    _legacy_fts_db(db_path)

    db = Database(db_path)
    db.init_schema()
    db.close()

    db2 = Database(db_path)
    db2.init_schema()
    try:
        rows = db2.conn.execute("SELECT rowid, novel_id FROM novel_fts ORDER BY rowid").fetchall()
        assert [(r[0], r[1]) for r in rows] == [
            (25310744, 25310744),
            (27380872, 27380872),
        ]
    finally:
        db2.close()


def test_fts_migration_handles_empty_index(db: Database) -> None:
    """空索引（全新库）不需要重建，也不能抛异常。"""
    assert db.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0] == 0
    db.init_schema()  # 再跑一次
    assert db.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0] == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_storage_db.py -k fts_migration -v`
Expected: FAIL —— 前三个测试断言 rowid 仍是 `(1, 25310744)` 而非对齐值。

- [ ] **Step 3: 写迁移实现**

在 `src/pixiv_novel_sync/storage/schema.py` 的 `_migrate_novel_texts_table` 方法之后加：

```python
    def _migrate_novel_fts_rowid(self) -> None:
        """把 novel_fts 的 rowid 对齐到 novel_id，并重建错位的历史索引。

        建表时 novel_id 声明为 UNINDEXED，而 rowid 是自增值，两者完全不对应
        （生产实测 7627 行里 rowid == novel_id 的有 0 行，如 rowid=12 对应
        novel_id=25310744）。于是 ``WHERE novel_id = ?`` 只能全表扫描 1 GB 的
        FTS 索引——生产实测单次 DELETE 39 秒，而每同步一篇小说都要付一次，
        这是同步吞吐的头号瓶颈（单篇 51 秒里有 40 秒花在这）。搜索路径同样
        受害：SELECT novel_id ... MATCH 要 0.22 秒，走 rowid 只要 0.002 秒。

        改法是让 rowid 等于 novel_id，按 ID 的读写一律走 rowid（FTS5 主键，
        O(1)）。历史数据的 rowid 全部错位，只能整表重建。
        """
        row = self.conn.execute("SELECT rowid, novel_id FROM novel_fts LIMIT 1").fetchone()
        if row is None:
            # 空索引：建表语句本身不写 rowid，后续写入路径会显式指定，无需重建。
            return
        if int(row[0]) == int(row[1]):
            # 已对齐 ⇒ 幂等 no-op。探测只取一行：写入路径是唯一入口，所以要么
            # 全部对齐（新代码）要么全部错位（旧代码）。若曾中途崩溃留下混合状态，
            # LIMIT 1 取到的是最小 rowid，即旧的错位行，仍会触发重建——偏安全侧。
            return

        total = int(self.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0])
        logger.warning(
            "正在重建 novel_fts 索引以对齐 rowid（%d 行）。这是一次性迁移，"
            "期间服务启动会阻塞约 1-3 分钟，请勿中断。",
            total,
        )
        started = time.time()
        # 整个重建包在一个事务里：中断则回滚，下次启动重试，不会留下半个索引。
        with self.transaction():
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
            # 从权威表回填，显式指定 rowid = novel_id。
            self.conn.execute(
                """
                INSERT INTO novel_fts (rowid, novel_id, title, caption, author_name, body)
                SELECT n.novel_id, n.novel_id, n.title, COALESCE(n.caption, ''),
                       COALESCE(u.name, ''), COALESCE(nt.text_raw, '')
                FROM novels n
                LEFT JOIN users u ON u.user_id = n.user_id
                LEFT JOIN novel_texts nt ON nt.novel_id = n.novel_id
                """
            )
        rebuilt = int(self.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0])
        logger.warning(
            "novel_fts 重建完成：%d 行，耗时 %.1f 秒", rebuilt, time.time() - started
        )
```

在 `init_schema()` 的迁移调用列表里（`self._migrate_novel_texts_table()` 那一行之后、`self._migrate_core_foreign_keys()` 之前）加：

```python
        self._migrate_novel_fts_rowid()
```

确认 `schema.py` 顶部已 import `time` 与 `logger`；若缺则补（`import time`，以及模块级 `logger = logging.getLogger(__name__)`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_storage_db.py -k fts_migration -v`
Expected: 4 passed

- [ ] **Step 5: 跑存储与搜索相关全量测试**

Run: `pytest tests/test_storage_db.py tests/test_fts_escape.py tests/test_dashboard_novels_api.py -q`
Expected: 全部 passed（迁移不改变任何检索语义）

- [ ] **Step 6: Commit**

```bash
git add src/pixiv_novel_sync/storage/schema.py tests/test_storage_db.py
git commit -m "fix: 对齐 novel_fts 的 rowid 与 novel_id 并重建历史索引"
```

---

### Task 2: FTS 写入路径改走 rowid

**Files:**
- Modify: `src/pixiv_novel_sync/storage/novels.py:380`（`replace_fts`）、`:350`（`delete_novel` 内清 FTS）
- Modify: `src/pixiv_novel_sync/storage/users.py:326`（`delete_user(user_id)` 内清 FTS，方法定义在 `:315`）
- Test: `tests/test_storage_db.py`

**Interfaces:**
- Consumes: Task 1 保证的不变量 `novel_fts.rowid == novel_id`
- Produces: `replace_fts(novel_id, title, caption, author_name, body) -> None` 签名不变，内部改为 O(1)。Task 3 依赖同一条不变量。

- [ ] **Step 1: 写失败测试**

加到 `tests/test_storage_db.py`：

```python
def test_replace_fts_writes_rowid_equal_to_novel_id(db: Database) -> None:
    """replace_fts 必须显式写 rowid，否则又会退回自增错位。"""
    db.replace_fts(31415926, "标题甲", "简介甲", "作者甲", "正文甲")

    rows = db.conn.execute("SELECT rowid, novel_id FROM novel_fts").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(31415926, 31415926)]


def test_replace_fts_twice_keeps_single_row(db: Database) -> None:
    """重复写同一篇不能产生重复索引项（DELETE 必须真的命中旧行）。"""
    db.replace_fts(31415926, "旧标题", "简介", "作者甲", "旧正文")
    db.replace_fts(31415926, "新标题", "简介", "作者甲", "新正文")

    assert db.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0] == 1
    hits = db.conn.execute(
        "SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?", ('"新正文"',)
    ).fetchall()
    assert [row[0] for row in hits] == [31415926]
    # 旧正文必须已从索引里消失
    stale = db.conn.execute(
        "SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?", ('"旧正文"',)
    ).fetchall()
    assert stale == []


def test_delete_novel_removes_fts_row(db: Database) -> None:
    db.upsert_user(UserRecord(user_id=7, name="作者甲", account="acc", raw_json="{}"))
    db.upsert_novel(
        NovelRecord(
            novel_id=31415926,
            user_id=7,
            series_id=None,
            title="标题甲",
            caption="简介甲",
            visible=True,
            restrict="public",
            x_restrict=0,
            text_length=3,
            total_bookmarks=0,
            total_views=0,
            cover_url=None,
            tags_json="[]",
            create_date=None,
            raw_json="{}",
            meta_hash="h",
        )
    )
    db.replace_fts(31415926, "标题甲", "简介甲", "作者甲", "正文甲")

    db.delete_novel(31415926)

    assert db.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0] == 0


def test_delete_user_removes_fts_rows(db: Database) -> None:
    db.upsert_user(UserRecord(user_id=7, name="作者甲", account="acc", raw_json="{}"))
    for novel_id in (31415926, 27182818):
        db.upsert_novel(
            NovelRecord(
                novel_id=novel_id,
                user_id=7,
                series_id=None,
                title=f"标题{novel_id}",
                caption="简介",
                visible=True,
                restrict="public",
                x_restrict=0,
                text_length=3,
                total_bookmarks=0,
                total_views=0,
                cover_url=None,
                tags_json="[]",
                create_date=None,
                raw_json="{}",
                meta_hash="h",
            )
        )
        db.replace_fts(novel_id, f"标题{novel_id}", "简介", "作者甲", "正文")

    db.delete_user(7)

    assert db.conn.execute("SELECT COUNT(*) FROM novel_fts").fetchone()[0] == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_storage_db.py -k "replace_fts or removes_fts" -v`
Expected: `test_replace_fts_writes_rowid_equal_to_novel_id` FAIL（rowid 是 1 而非 31415926）

- [ ] **Step 3: 改写入路径**

`storage/novels.py` 的 `replace_fts` 整体替换为：

```python
    def replace_fts(self, novel_id: int, title: str, caption: str, author_name: str, body: str) -> None:
        """更新 FTS 索引。

        必须用 rowid：novel_id 是 UNINDEXED 列，``WHERE novel_id = ?`` 会全表
        扫描 1 GB 的 FTS 索引（生产实测单次 39 秒，每同步一篇付一次）。rowid
        是 FTS5 主键，O(1)。INSERT 也要显式写 rowid，否则又退回自增错位。

        用 transaction() 保证 DELETE 与 INSERT 的原子性。
        """
        with self.transaction():
            self.conn.execute("DELETE FROM novel_fts WHERE rowid = ?", (novel_id,))
            self.conn.execute(
                "INSERT INTO novel_fts (rowid, novel_id, title, caption, author_name, body) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (novel_id, novel_id, title, caption, author_name, body),
            )
```

`storage/novels.py` 的 `delete_novel` 内第一条 FTS 语句：

```python
                self.conn.execute("DELETE FROM novel_fts WHERE rowid = ?", (novel_id,))
```

`storage/users.py` 的 `delete_user` 内清 FTS 那句：

```python
                # 走 rowid：见 replace_fts 的注释，按 novel_id 会全表扫描 FTS 索引
                self.conn.execute(
                    "DELETE FROM novel_fts WHERE rowid IN (SELECT novel_id FROM novels WHERE user_id = ?)",
                    (user_id,),
                )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_storage_db.py -k "replace_fts or removes_fts" -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/pixiv_novel_sync/storage/novels.py src/pixiv_novel_sync/storage/users.py tests/test_storage_db.py
git commit -m "perf: FTS 写入与删除改走 rowid，消除每篇 39 秒的全表扫描"
```

---

### Task 3: FTS 搜索路径改走 rowid

**Files:**
- Modify: `src/pixiv_novel_sync/storage/novels.py:413`（`list_recent_novels`）
- Modify: `src/pixiv_novel_sync/storage/bookmarks.py:24`（`list_bookmark_novels`）
- Modify: `src/pixiv_novel_sync/storage/series.py:155`（订阅系列搜索）
- Test: `tests/test_storage_db.py`

**Interfaces:**
- Consumes: Task 1 的不变量 `novel_fts.rowid == novel_id`
- Produces: 无新接口；三处子查询由 `SELECT novel_id` 改为 `SELECT rowid`，语义等价、速度从 0.22s 降到约 0.002s。

- [ ] **Step 1: 写失败测试**

加到 `tests/test_storage_db.py`。这里断言的是**源码不再走慢路径**（行为等价所以行为测试无法区分快慢）：

```python
def test_fts_search_subqueries_select_rowid_not_novel_id() -> None:
    """三处搜索子查询必须走 rowid。

    SELECT novel_id FROM novel_fts 会全表扫描（novel_id 是 UNINDEXED 列），
    生产实测 0.22 秒；SELECT rowid 是 0.002 秒。行为等价，所以只能靠源码断言
    守住——一旦有人改回 novel_id，性能悄悄退化而测试全绿。
    """
    targets = [
        Path("src/pixiv_novel_sync/storage/novels.py"),
        Path("src/pixiv_novel_sync/storage/bookmarks.py"),
        Path("src/pixiv_novel_sync/storage/series.py"),
    ]
    for path in targets:
        source = path.read_text(encoding="utf-8")
        assert "SELECT novel_id FROM novel_fts" not in source, (
            f"{path} 仍在用 SELECT novel_id FROM novel_fts（全表扫描）"
        )
        assert "SELECT rowid FROM novel_fts" in source, f"{path} 缺少走 rowid 的搜索子查询"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_storage_db.py -k select_rowid -v`
Expected: FAIL —— `novels.py 仍在用 SELECT novel_id FROM novel_fts`

- [ ] **Step 3: 改三处搜索 SQL**

`storage/novels.py:413`：

```python
            # 走 rowid（== novel_id）：SELECT novel_id 会全表扫描 UNINDEXED 列
            where_clauses.append("n.novel_id IN (SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?)")
```

`storage/bookmarks.py:24`：

```python
            # 走 rowid（== novel_id）：SELECT novel_id 会全表扫描 UNINDEXED 列
            where_clauses.append("n.novel_id IN (SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?)")
```

`storage/series.py:155`（保持原多行字符串结构，只改子查询）：

```python
            where_clauses.append(
                """(se.title LIKE ? OR (
                   (se.title IS NULL OR se.title = '') AND EXISTS (
                     SELECT 1 FROM novels n0 WHERE n0.series_id = se.series_id AND n0.novel_id IN (SELECT rowid FROM novel_fts WHERE novel_fts MATCH ?)
                   )
                   ) OR u.name LIKE ?)"""
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_storage_db.py -k select_rowid -v`
Expected: PASS

- [ ] **Step 5: 跑搜索行为回归**

Run: `pytest tests/test_storage_db.py tests/test_fts_escape.py tests/test_dashboard_novels_api.py tests/test_rescue_storage.py -q`
Expected: 全部 passed

- [ ] **Step 6: Commit**

```bash
git add src/pixiv_novel_sync/storage/novels.py src/pixiv_novel_sync/storage/bookmarks.py src/pixiv_novel_sync/storage/series.py tests/test_storage_db.py
git commit -m "perf: 全文搜索子查询改走 FTS rowid"
```

---

### Task 4: 两个新配置字段的读写链路

**Files:**
- Modify: `src/pixiv_novel_sync/settings.py`（`SyncSettings` 字段 + `load_settings` 解析）
- Modify: `src/pixiv_novel_sync/web/managers.py`（`save_sync_settings`，在 `bookmark_max_pages_per_run` 那段附近）
- Modify: `src/pixiv_novel_sync/web/utils.py`（`_settings_to_dict`）
- Modify: `config/config.yaml.example`
- Test: `tests/test_webapp_settings.py`

**Interfaces:**
- Consumes: 无
- Produces: `SyncSettings.following_max_novels_per_author: int | None`（默认 `None`）与 `SyncSettings.series_max_pages_per_run: int | None`（默认 `None`）。Task 5 读第一个，Task 6 读第二个。两者语义与既有 `bookmark_max_pages_per_run` 一致：`None` 表示回落到通用上限。

- [ ] **Step 1: 写失败测试**

加到 `tests/test_webapp_settings.py`：

```python
def test_save_sync_settings_round_trips_new_throughput_fields(tmp_path):
    """每作者配额与系列分页上限必须能存进 YAML 并读回。"""
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


def test_save_sync_settings_allows_blank_throughput_fields(tmp_path):
    """留空表示"跟随通用上限"，必须存成 None 而不是 0。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "sync:\n  following_max_novels_per_author: 20\n  series_max_pages_per_run: 10\n",
        encoding="utf-8",
    )

    saved = SettingsManager(str(config_path)).save_sync_settings(
        {"following_max_novels_per_author": "", "series_max_pages_per_run": ""}
    )

    assert saved["following_max_novels_per_author"] is None
    assert saved["series_max_pages_per_run"] is None


def test_load_settings_defaults_new_throughput_fields_to_none(tmp_path):
    """老配置文件（没有这两个字段）必须行为不变。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("sync:\n  max_pages_per_run: 2\n", encoding="utf-8")

    settings = load_settings(config_path, None)

    assert settings.sync.following_max_novels_per_author is None
    assert settings.sync.series_max_pages_per_run is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_webapp_settings.py -k throughput -v`
Expected: FAIL —— `KeyError: 'following_max_novels_per_author'`

- [ ] **Step 3: 加字段与解析**

`settings.py` 的 `SyncSettings`，紧跟现有 `bookmark_max_pages_per_run` 之后：

```python
    # 单个关注作者在一轮 following_novels 里最多同步多少篇。None = 不限（老行为）。
    # 生产事故：max_items_per_run=20 只在切换作者前检查，单作者内部无上限，于是
    # 第一个高产作者就吃掉整轮配额——实测每轮只覆盖 1/256 个作者，全圈要 95 天。
    following_max_novels_per_author: int | None = None
    # 系列章节分页专用上限。None = 跟随 max_pages_per_run（老行为）。
    # max_pages_per_run=2 会把长系列的章节列表截断，生产实测每轮 truncated_series=2、
    # 8 个订阅系列长期缺 76 章，且高频重跑也补不齐。
    series_max_pages_per_run: int | None = None
```

`settings.py` 的 `load_settings`，紧跟 `bookmark_max_pages_per_run=` 那一行之后：

```python
            following_max_novels_per_author=_coerce_optional_int(sync_raw.get("following_max_novels_per_author")),
            series_max_pages_per_run=_coerce_optional_int(sync_raw.get("series_max_pages_per_run")),
```

`web/managers.py` 的 `save_sync_settings`，紧跟 `sync_data["bookmark_max_pages_per_run"] = ...` 之后：

```python
        sync_data["following_max_novels_per_author"] = _normalize_optional_int(
            payload.get("following_max_novels_per_author", sync_data.get("following_max_novels_per_author"))
        )
        sync_data["series_max_pages_per_run"] = _normalize_optional_int(
            payload.get("series_max_pages_per_run", sync_data.get("series_max_pages_per_run"))
        )
```

`web/utils.py` 的 `_settings_to_dict`，紧跟 `"bookmark_max_pages_per_run": ...` 之后：

```python
        "following_max_novels_per_author": settings.sync.following_max_novels_per_author,
        "series_max_pages_per_run": settings.sync.series_max_pages_per_run,
```

`config/config.yaml.example`，在 `bookmark_max_pages_per_run` 附近：

```yaml
  # 单个关注作者每轮最多同步多少篇。留空 = 不限。
  # 必须设一个值：否则第一个高产作者会吃掉整轮配额，每轮只覆盖 1 个作者。
  following_max_novels_per_author: 20
  # 系列章节分页专用上限。留空则跟随 max_pages_per_run。
  # 跟随时 max_pages_per_run=2 会把长系列截断，缺章永远补不齐。
  series_max_pages_per_run: 10
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_webapp_settings.py -k throughput -v`
Expected: 3 passed

- [ ] **Step 5: 跑设置相关全量**

Run: `pytest tests/test_webapp_settings.py tests/test_settings_save_csrf.py tests/test_deployment_config.py -q`
Expected: 全部 passed

- [ ] **Step 6: Commit**

```bash
git add src/pixiv_novel_sync/settings.py src/pixiv_novel_sync/web/managers.py src/pixiv_novel_sync/web/utils.py config/config.yaml.example tests/test_webapp_settings.py
git commit -m "feat: 新增每作者同步配额与系列分页上限配置"
```

---

### Task 5: `following_novels` 每作者配额

**Files:**
- Modify: `src/pixiv_novel_sync/sync_engine.py`（`_resolve_bookmark_max_pages` 之后加两个 helper；`_sync_author` 内部；`users_limit > 0` 分支的作者循环）
- Test: `tests/test_sync_engine_incremental.py`

**Interfaces:**
- Consumes: `SyncSettings.following_max_novels_per_author`（Task 4）
- Produces: 模块级 `_resolve_following_author_quota(settings) -> int | None` 与 `_resolve_following_run_cap(settings, users_limit) -> int | None`。`sync_following_novels` 的 stats 新增 `author_quota_hit: int`（本轮有多少作者是因为撞配额而提前结束的）。

- [ ] **Step 1: 写失败测试**

加到 `tests/test_sync_engine_incremental.py`（放在既有轮转测试之后）。先加一个会真正产出小说的作者 fake：

```python
class _ProlificAuthorsApi:
    """每个作者都有 `novels_per_author` 篇作品，全部是新作（会真正同步）。"""

    def __init__(self, author_ids: list[int], novels_per_author: int) -> None:
        self.author_ids = author_ids
        self.novels_per_author = novels_per_author
        self.scanned_user_ids: list[int] = []
        self.synced_novel_ids: list[int] = []

    def user_following(self, **kwargs):
        return SimpleNamespace(
            user_previews=[
                SimpleNamespace(user=SimpleNamespace(id=uid, name=f"作者{uid}"))
                for uid in self.author_ids
            ],
            next_url=None,
        )

    def user_novels(self, **kwargs):
        user_id = int(kwargs["user_id"])
        self.scanned_user_ids.append(user_id)
        novels = []
        for index in range(self.novels_per_author):
            novel = _Novel()
            novel.id = user_id * 1000 + index
            novels.append(novel)
        return SimpleNamespace(novels=novels, next_url=None)

    def novel_detail(self, novel_id: int):
        self.synced_novel_ids.append(int(novel_id))
        novel = _Novel()
        novel.id = int(novel_id)
        return SimpleNamespace(novel=novel)

    def webview_novel(self, novel_id: int) -> dict:
        return {"text": f"正文 {novel_id}"}

    def parse_qs(self, next_url):
        return None


def test_author_quota_moves_to_next_author_instead_of_ending_run(tmp_path: Path) -> None:
    """单作者撞到配额后必须换下一个作者，而不是结束整轮。

    回归：max_items_per_run=20 只在切换作者前检查，单作者内部无上限。生产实测
    一个作者连跑 60 篇 52 分钟，跑完 synced_items >= 20 直接结束整轮，
    "Following rotation: synced 1/256 users this run"——256 人全圈要 95 天。
    """
    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    settings.sync.following_max_novels_per_author = 2
    settings.sync.max_items_per_run = 20
    db = _RotationFakeDb()
    api = _ProlificAuthorsApi(author_ids=[11, 22, 33], novels_per_author=5)
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    stats = service.sync_following_novels(users_limit=3)

    # 三个作者都被访问到，而不是第一个吃掉全部配额
    assert api.scanned_user_ids == [11, 22, 33]
    # 每个作者只同步了配额内的 2 篇
    assert stats["novels"] == 6
    assert stats["author_quota_hit"] == 3


def test_author_quota_none_keeps_unlimited_per_author(tmp_path: Path) -> None:
    """配额留空时保持老行为：单作者不设上限。"""
    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    settings.sync.following_max_novels_per_author = None
    settings.sync.max_items_per_run = None
    db = _RotationFakeDb()
    api = _ProlificAuthorsApi(author_ids=[11], novels_per_author=4)
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    stats = service.sync_following_novels(users_limit=1)

    assert stats["novels"] == 4
    assert stats.get("author_quota_hit", 0) == 0


def test_author_with_fewer_novels_than_quota_is_not_marked_incomplete(tmp_path: Path) -> None:
    """作者作品数少于配额时不能误报撞配额。"""
    settings = _settings(tmp_path)
    settings.pixiv.user_id = 1
    settings.sync.following_max_novels_per_author = 10
    db = _RotationFakeDb()
    api = _ProlificAuthorsApi(author_ids=[11, 22], novels_per_author=3)
    service = BookmarkNovelSyncService(api, db, _Storage(), settings)

    stats = service.sync_following_novels(users_limit=2)

    assert stats["novels"] == 6
    assert stats.get("author_quota_hit", 0) == 0


def test_run_hard_cap_derives_from_quota_not_max_items(tmp_path: Path) -> None:
    """整轮硬顶按 配额 × users_limit × 1.5 推导，不再直接用 max_items_per_run。"""
    settings = _settings(tmp_path)
    settings.sync.following_max_novels_per_author = 20
    settings.sync.max_items_per_run = 20

    assert sync_engine._resolve_following_author_quota(settings) == 20
    # 5 个作者 × 20 篇 × 1.5 = 150，远大于 max_items_per_run=20
    assert sync_engine._resolve_following_run_cap(settings, 5) == 150


def test_run_hard_cap_falls_back_to_max_items_without_quota(tmp_path: Path) -> None:
    """未配配额时回落 max_items_per_run，保证老配置行为不变。"""
    settings = _settings(tmp_path)
    settings.sync.following_max_novels_per_author = None
    settings.sync.max_items_per_run = 20

    assert sync_engine._resolve_following_run_cap(settings, 5) == 20
```

同时把 `_settings()` 里的 `sync=SimpleNamespace(...)` 补上两个新字段，否则 `getattr` 拿不到：

```python
            max_items_per_run=None,
            max_pages_per_run=None,
            following_max_novels_per_author=None,
            series_max_pages_per_run=None,
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_sync_engine_incremental.py -k "quota or hard_cap" -v`
Expected: FAIL —— `AttributeError: module 'pixiv_novel_sync.sync_engine' has no attribute '_resolve_following_author_quota'`

- [ ] **Step 3: 实现 helper 与配额逻辑**

`sync_engine.py`，在 `_resolve_bookmark_max_pages` 之后加：

```python
def _resolve_following_author_quota(settings: Any) -> int | None:
    """单个关注作者每轮的同步上限：``sync.following_max_novels_per_author``。

    返回 None 表示不限（老行为）。这个配额是必需的：``max_items_per_run`` 只在
    切换作者之前检查，单作者内部完全没有闸门，于是第一个高产作者就把整轮配额
    吃光——生产实测每轮只覆盖 1/256 个作者，全圈需要 95 天。
    """
    raw = getattr(getattr(settings, "sync", None), "following_max_novels_per_author", None)
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _resolve_following_run_cap(settings: Any, users_limit: int) -> int | None:
    """整轮同步硬顶。

    正常收敛靠「每作者配额 × users_limit」，本上限只用来防异常情况（配额没配
    而某作者作品极多）。取 users_limit × 配额 × 1.5 留出余量，避免正常轮次被
    硬顶提前截断——那正是旧代码用 max_items_per_run 当整轮上限时的病根。
    配额或 users_limit 缺失时回落 ``max_items_per_run``，保持老配置行为。
    """
    sync_settings = getattr(settings, "sync", None)
    quota = _resolve_following_author_quota(settings)
    if quota is None or users_limit <= 0:
        return getattr(sync_settings, "max_items_per_run", None)
    return int(quota * users_limit * 1.5)
```

在 `sync_following_novels` 里，紧跟 `max_pages = self.settings.sync.max_pages_per_run` 那段之后加：

```python
        author_quota = _resolve_following_author_quota(self.settings)
```

在 `_sync_author` 内部，把 `author_page_count = 0` 那几行改成（新增 `author_synced`）：

```python
            next_novel_query: dict[str, Any] | None = {"user_id": author_id}
            author_page_count = 0
            author_synced = 0  # 本作者本轮实际同步数（跳过不计）
            existing_streak = 0
            stop_author_scan = False
```

在同一个方法的 `synced_items += 1` 处，改成：

```python
                    # 只有实际同步（非跳过）才计入 synced_items
                    if counters.get("novels", 0) > 0:
                        synced_items += 1
                        author_synced += 1
                        # 撞到每作者配额就收手，把剩余预算让给下一个作者。
                        # 不能像旧代码那样结束整轮，否则高产作者永久饿死队尾。
                        if author_quota is not None and author_synced >= author_quota:
                            logger.info(
                                "Author %s reached per-author quota=%d, moving to next author",
                                author_id,
                                author_quota,
                            )
                            stats["author_quota_hit"] = stats.get("author_quota_hit", 0) + 1
                            stats["incomplete"] = True
                            stop_author_scan = True
```

把 `users_limit > 0` 分支的作者循环改成：

```python
            # 整轮硬顶只防异常：正常靠每作者配额收敛。旧代码在这里用
            # max_items_per_run 当闸门，导致第一个高产作者跑完整轮就结束。
            run_hard_cap = _resolve_following_run_cap(self.settings, users_limit)
            for user in selected:
                if run_hard_cap is not None and synced_items >= run_hard_cap:
                    logger.warning(
                        "Reached run hard cap=%s (synced), stopping sync", run_hard_cap
                    )
                    stats["incomplete"] = True
                    break
                _sync_author(user)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_sync_engine_incremental.py -k "quota or hard_cap" -v`
Expected: 5 passed

- [ ] **Step 5: 跑同步引擎全量回归**

Run: `pytest tests/test_sync_engine_incremental.py tests/test_jobs_services.py tests/test_jobs_quick_sync.py -q`
Expected: 全部 passed（含守着裸 sleep 与 `InterruptedError` 的两条源码断言）

- [ ] **Step 6: Commit**

```bash
git add src/pixiv_novel_sync/sync_engine.py tests/test_sync_engine_incremental.py
git commit -m "fix: 关注作者同步改为每作者独立配额，避免高产作者饿死队尾"
```

---

### Task 6: 系列章节独立分页上限

**Files:**
- Modify: `src/pixiv_novel_sync/sync_engine.py`（新增 `_resolve_series_max_pages`；`sync_subscribed_series` 内 `series_safety_limit`）
- Test: `tests/test_sync_engine_incremental.py`

**Interfaces:**
- Consumes: `SyncSettings.series_max_pages_per_run`（Task 4）
- Produces: 模块级 `_resolve_series_max_pages(settings) -> int | None`，语义与 `_resolve_bookmark_max_pages` 完全一致。

- [ ] **Step 1: 写失败测试**

加到 `tests/test_sync_engine_incremental.py`：

```python
def test_series_max_pages_overrides_generic_cap(tmp_path: Path) -> None:
    """系列章节分页有独立上限，不再被 max_pages_per_run 锁死。

    回归：两者共用 max_pages_per_run=2 时长系列的章节列表被截断，生产实测每轮
    truncated_series=2、8 个订阅系列长期缺 76 章，高频重跑也补不齐。
    """
    settings = _settings(tmp_path)
    settings.sync.max_pages_per_run = 2
    settings.sync.series_max_pages_per_run = 10

    assert sync_engine._resolve_series_max_pages(settings) == 10


def test_series_max_pages_falls_back_to_generic_cap(tmp_path: Path) -> None:
    """留空时回落 max_pages_per_run，保持老配置行为。"""
    settings = _settings(tmp_path)
    settings.sync.max_pages_per_run = 2
    settings.sync.series_max_pages_per_run = None

    assert sync_engine._resolve_series_max_pages(settings) == 2


def test_series_max_pages_treats_non_positive_as_unlimited(tmp_path: Path) -> None:
    """0 / 负数表示不额外限制，交给调用方的 100 页兜底。"""
    settings = _settings(tmp_path)
    settings.sync.max_pages_per_run = 2
    settings.sync.series_max_pages_per_run = 0

    assert sync_engine._resolve_series_max_pages(settings) is None


def test_series_pagination_source_uses_dedicated_resolver() -> None:
    """章节分页必须调用 _resolve_series_max_pages，而不是直读 max_pages_per_run。"""
    source = Path(sync_engine.__file__).read_text(encoding="utf-8")

    assert "series_safety_limit = _resolve_series_max_pages(self.settings)" in source
    assert "series_safety_limit = self.settings.sync.max_pages_per_run" not in source
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_sync_engine_incremental.py -k series_max_pages -v`
Expected: FAIL —— 没有 `_resolve_series_max_pages`

- [ ] **Step 3: 实现**

`sync_engine.py`，在 `_resolve_bookmark_max_pages` 之后（可与 Task 5 的两个 helper 相邻）加：

```python
def _resolve_series_max_pages(settings: Any) -> int | None:
    """系列章节分页专用上限：优先 ``sync.series_max_pages_per_run``，缺省回落。

    返回 None 表示不额外限制，由调用方的 100 页兜底接管。系列章节数可以远超
    作者作品列表的单轮体量，共用 ``max_pages_per_run`` 会把长系列永久截断：
    生产实测每轮 truncated_series=2，8 个订阅系列缺 76 章且补不齐。
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

把 `sync_subscribed_series` 里那行改成：

```python
                                series_safety_limit = _resolve_series_max_pages(self.settings) or 100  # 翻页上限兜底
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_sync_engine_incremental.py -k series_max_pages -v`
Expected: 4 passed

- [ ] **Step 5: 跑全量测试**

Run: `pytest`
Expected: 1308 + 新增测试全部 passed / 4 skipped

- [ ] **Step 6: 更新任务系统文档**

在 `docs/JOB_SYSTEM.md` §5 的配置矩阵里，给 `following_novels` 行的「附加字段」加 `following_max_novels_per_author`（留空=不限），给 `subscribed_series` 行加 `series_max_pages_per_run`（留空跟随 `max_pages_per_run`）。

- [ ] **Step 7: Commit**

```bash
git add src/pixiv_novel_sync/sync_engine.py tests/test_sync_engine_incremental.py docs/JOB_SYSTEM.md
git commit -m "fix: 系列章节分页改用独立上限，补齐被截断的缺章"
```

---

## 部署与验证

阶段一上线后要在生产验证的四件事（依据 `docs/superpowers/specs/...-design.md` §4 的实测基线）：

1. **FTS 重建**：`update.sh` 重启后 journald 应出现「正在重建 novel_fts 索引以对齐 rowid（7627 行）」与「重建完成」两条 WARNING。预估 1–3 分钟。若超过 5 分钟，按 spec §4.1 的备选方案改后台重建。
2. **单篇耗时**：下一轮 `following_novels` 的任务日志里相邻「等待 10.0 秒」的间隔应从约 51 秒降到约 11 秒。
3. **作者覆盖**：`following_users_scanned` 应等于 `users_limit`（当前生产是 5），而非长期为 1。stats 里会出现 `author_quota_hit`。
4. **缺章补齐**：`subscribed_series` 的 `truncated_series` 应归零，`series_synced` 首次出现非零值。

回滚：代码可回滚，数据不必——旧代码在新 rowid 上仍然正确（`WHERE novel_id = ?` 只是慢，不是错）。

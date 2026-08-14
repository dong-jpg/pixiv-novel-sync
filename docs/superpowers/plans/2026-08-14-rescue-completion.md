# Rescue Catalog Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成救援目录纠错/删除接线、完整组合筛选、stale/503 契约和性能验收。

**Architecture:** `rescue_catalog` 继续作为派生快照；所有写入口调用对象级刷新，失败保留上一版。列表过滤、排序、计数和分页全部在 SQL 中完成，当前页来源一次批量加载。

**Tech Stack:** Python、SQLite、Flask、Vue 模板、pytest。

## Global Constraints

- `/api/rescue/v1/` 和 userscript 白名单保持兼容。
- 单项 API 继续实时校验，不能信任过期目录。
- 列表查询不读取正文，不随目录总量增加 SQL 次数。
- 所有新增行为先有 RED。

---

### Task 1: 人工纠错和实体删除刷新接线

**Files:**
- Modify: `src/pixiv_novel_sync/storage/rescue.py`
- Modify: `src/pixiv_novel_sync/rescue_web.py`
- Modify: `src/pixiv_novel_sync/webapp.py`
- Test: `tests/test_rescue_catalog_refresh.py`

- [ ] **Step 1: 写 novel override 刷新自身和父系列 RED**

```python
def test_novel_override_refreshes_novel_and_parent_series(db):
    seed_series_with_chapter(db, series_id=20, novel_id=10)
    db.set_rescue_override("novel", 10, "include")
    assert catalog_ids(db) == {("series", 20)}
```

- [ ] **Step 2: 写 series override、novel/series/user 删除清理目录 RED**

删除后断言 `rescue_catalog` 与 `rescue_catalog_sources` 无孤儿，父系列按剩余章节重新计算。

- [ ] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_rescue_catalog_refresh.py -q`
Expected: FAIL，部分入口未刷新正确粒度。

- [ ] **Step 4: 实现 `refresh_rescue_entities(novel_ids, series_ids)` 单事务入口**

override 和删除协调器传递受影响 ID；重复 ID 去重；失败回滚保留旧快照/meta。

- [ ] **Step 5: 运行 GREEN 并提交**

Run: `python -m pytest tests/test_rescue_catalog_refresh.py tests/test_rescue_storage.py -q`

```bash
git add src/pixiv_novel_sync/storage/rescue.py src/pixiv_novel_sync/rescue_web.py src/pixiv_novel_sync/webapp.py tests/test_rescue_catalog_refresh.py
git commit -m "fix: connect rescue catalog refresh hooks"
```

### Task 2: 完整 SQL 筛选与 stale/503

**Files:**
- Modify: `src/pixiv_novel_sync/storage/rescue.py`
- Modify: `src/pixiv_novel_sync/rescue_web.py`
- Modify: `src/pixiv_novel_sync/templates/dashboard_novels.html`
- Test: `tests/test_rescue_catalog_filters.py`

- [ ] **Step 1: 写 state/kind/source/search/stale 组合 RED**

```python
def test_rescue_filters_use_source_contains_semantics(client):
    seed_multi_source_catalog()
    response = client.get("/api/dashboard/rescues?state=success&content_kind=series&source=bookmark&search=作者")
    assert [item["item_id"] for item in response.json["data"]["items"]] == [20]
```

- [ ] **Step 2: 写未初始化 503 和 stale payload RED**

meta 不存在返回 503；过期 meta 返回 200 且 `stale=true`，页面显示明确标签。

- [ ] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_rescue_catalog_filters.py -q`
Expected: 缺失组合或契约不一致时 FAIL。

- [ ] **Step 4: 实现参数白名单、EXISTS source 筛选和 SQL 分页**

搜索转义；排序字段白名单；来源使用 `EXISTS`，当前页来源单次 `IN` 查询。

- [ ] **Step 5: 页面增加三组筛选并在任一变化时重置 page**

移动端使用 wrapping grid，不产生横向滚动。

- [ ] **Step 6: 运行 GREEN 并提交**

Run: `python -m pytest tests/test_rescue_catalog_filters.py tests/test_rescue_api.py tests/test_frontend_library_os.py -q`

```bash
git add src/pixiv_novel_sync/storage/rescue.py src/pixiv_novel_sync/rescue_web.py src/pixiv_novel_sync/templates/dashboard_novels.html tests
git commit -m "feat: complete rescue catalog filtering"
```

### Task 3: 查询计数与性能 fixture

**Files:**
- Modify: `tests/test_rescue_catalog_performance.py`
- Create: `scripts/verify_rescue_performance.py`

- [ ] **Step 1: 写 4,593 项 fixture 常数查询断言 RED**

```python
def test_large_catalog_first_page_uses_constant_queries(db, query_counter):
    seed_catalog(db, count=4593)
    result = db.list_rescue_catalog(page=1, page_size=20)
    assert result["total"] == 4593
    assert query_counter.count <= 3
```

- [ ] **Step 2: 运行 RED**

Run: `python -m pytest tests/test_rescue_catalog_performance.py -q`
Expected: 若存在 N+1 或 Python 全量分页则 FAIL。

- [ ] **Step 3: 优化索引/查询并增加现场计时脚本**

脚本只读实际数据库，输出 item count、第一页 p50/p95、完整刷新耗时和是否满足 500ms/10s，不修改生产数据。

- [ ] **Step 4: 运行 GREEN 并提交**

Run: `python -m pytest tests/test_rescue_catalog_performance.py -q`

```bash
git add tests/test_rescue_catalog_performance.py scripts/verify_rescue_performance.py
git commit -m "test: verify rescue catalog performance"
```

### Task 4: 域验证和文档

- [ ] 更新 `docs/frontend-api-contract.md`、`docs/frontend-pages.md` 和部署验收说明。
- [ ] Run: `python -m pytest tests/test_rescue_storage.py tests/test_rescue_catalog_refresh.py tests/test_rescue_catalog_filters.py tests/test_rescue_catalog_performance.py tests/test_rescue_api.py tests/test_rescue_userscript.py -q`
- [ ] Run: `python -m compileall -q src tests`
- [ ] Run: `git diff --check`


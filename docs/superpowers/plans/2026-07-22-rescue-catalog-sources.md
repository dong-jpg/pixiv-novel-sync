# 救援目录预计算与来源展示实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**目标：** 将“拯救成功”从每次请求全库实时扫描改为可增量刷新的预计算目录，并展示系列、系列单章、独立小说及全部来源。

**架构：** 在 SQLite 中增加正文完整度辅助列、救援目录表、来源关系表和目录元数据单例。RescueMixin 提供事务化完整刷新、受影响对象增量刷新和 SQL 分页查询；同步任务成功后触发完整刷新，人工纠错和实体删除触发局部刷新。前端通过新增筛选参数读取常数查询结果，单项只读救援 API 继续使用实时资格判断。

**技术栈：** Python 3.10、Flask、SQLite/WAL、Vue 3 CDN、pytest、现有任务调度器。

## 全局约束

- 除专有名词外，界面文案、错误消息和文档使用中文。
- 列表查询不得读取 novel_texts.text_raw，不得先全量加载再在 Python 中分页。
- 目录刷新必须在事务内完成；失败时保留上一次成功目录。
- 来源标签全部展示，多来源按包含关系筛选，不选主来源覆盖其他来源。
- 内容类型固定为 series、series_chapter、standalone；救援完整度独立使用 success、partial。
- /api/rescue/v1/ 单项接口保持实时资格校验和既有字段白名单。
- 每个实现行为先写失败测试，确认红灯后再写生产代码。
- 不提交用户数据、Token、服务器配置或私钥。

---

## 文件职责

| 文件 | 职责 |
| --- | --- |
| src/pixiv_novel_sync/storage/schema.py | 增加 has_content、目录表、索引和迁移回填 |
| src/pixiv_novel_sync/storage/novels.py | 写入正文时维护 has_content，删除时清理目录 |
| src/pixiv_novel_sync/storage/series.py | 删除系列时清理目录，保留章节关联处理 |
| src/pixiv_novel_sync/storage/rescue.py | 目录构建、来源归一化、增量刷新、SQL 列表查询 |
| src/pixiv_novel_sync/rescue_web.py | 参数校验、目录元数据/503 响应、纠错后刷新 |
| src/pixiv_novel_sync/jobs/services.py | 状态任务完成后刷新目录 |
| src/pixiv_novel_sync/jobs/quick_sync.py | 独立收藏同步完成后刷新目录 |
| src/pixiv_novel_sync/jobs/tasks.py | 直接同步任务完成后的统一刷新适配 |
| src/pixiv_novel_sync/web/managers.py | 定时同步任务完成后刷新目录 |
| src/pixiv_novel_sync/templates/dashboard_novels.html | 内容类型/来源筛选、标签和更新时间展示 |
| tests/test_rescue_storage.py | 迁移、分类、来源、刷新和回滚测试 |
| tests/test_rescue_api.py | API 参数、分页、来源和过期状态测试 |
| tests/test_rescue_catalog_performance.py | SQL 不读正文、常数查询次数和分页测试 |
| tests/test_frontend_library_os.py | 前端静态契约测试 |

---

## 任务 1：正文完整度迁移

**文件：**
- 修改：src/pixiv_novel_sync/storage/schema.py
- 修改：src/pixiv_novel_sync/storage/novels.py
- 测试：tests/test_rescue_storage.py

**接口：** novel_texts.has_content 为整数 0/1；upsert_novel_text 根据 text_raw.strip() 写入该值。

- [ ] 步骤 1：写失败测试

~~~python
def test_novel_text_maintains_has_content(db: Database) -> None:
    _seed_novel(db, novel_id=90, text="正文")
    assert db.conn.execute(
        "SELECT has_content FROM novel_texts WHERE novel_id = 90"
    ).fetchone()[0] == 1

    _seed_novel(db, novel_id=91, text="  \n")
    assert db.conn.execute(
        "SELECT has_content FROM novel_texts WHERE novel_id = 91"
    ).fetchone()[0] == 0
~~~

- [ ] 步骤 2：运行红灯测试

运行：python -m pytest tests/test_rescue_storage.py::test_novel_text_maintains_has_content -q

预期：失败，当前 novel_texts 没有 has_content 列。

- [ ] 步骤 3：实现迁移和写入

在基础建表 SQL 增加 has_content INTEGER NOT NULL DEFAULT 0；新增 _migrate_novel_texts_table() 检查旧表列并执行 ALTER TABLE；迁移后执行一次 UPDATE novel_texts SET has_content = CASE WHEN TRIM(text_raw) != '' THEN 1 ELSE 0 END；upsert_novel_text 的插入和冲突更新同时写入 1 if record.text_raw.strip() else 0。

- [ ] 步骤 4：运行绿灯测试

运行：python -m pytest tests/test_rescue_storage.py::test_novel_text_maintains_has_content -q

预期：通过。

- [ ] 步骤 5：提交

~~~powershell
git add src/pixiv_novel_sync/storage/schema.py src/pixiv_novel_sync/storage/novels.py tests/test_rescue_storage.py
git commit -m "feat: 增加正文完整度辅助列"
~~~

## 任务 2：目录表和来源归一化

**文件：**
- 修改：src/pixiv_novel_sync/storage/schema.py
- 修改：src/pixiv_novel_sync/storage/rescue.py
- 测试：tests/test_rescue_storage.py

**接口：** 新增 RescueMixin.rebuild_rescue_catalog() -> dict[str, int]、refresh_rescue_item(item_type: str, item_id: int) -> dict[str, int]、get_rescue_catalog_meta() -> dict[str, Any] | None 和 list_rescue_catalog_sources(item_type, item_id)。

- [ ] 步骤 1：写失败测试

~~~python
def test_rebuild_catalog_classifies_items_and_sources(db: Database) -> None:
    _seed_series(db, 200, status="deleted", total_novels=2)
    _seed_novel(db, 201, series_id=200, status="normal", text="章节一")
    _seed_novel(db, 202, series_id=200, status="normal", text="章节二")
    _seed_novel(db, 203, series_id=None, status="deleted", text="独立篇")
    db.upsert_source(SourceRecord(201, "following_user_scan", "2"))
    db.upsert_source(SourceRecord(202, "subscribed_series", "200"))
    db.upsert_source(SourceRecord(203, "bookmark_public", "1"))

    result = db.rebuild_rescue_catalog()

    assert result["items"] == 2
    rows = db.conn.execute(
        "SELECT item_type, item_id, content_kind FROM rescue_catalog ORDER BY item_id"
    ).fetchall()
    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("series", 200, "series"),
        ("novel", 203, "standalone"),
    ]
    sources = db.list_rescue_catalog_sources("series", 200)
    assert {item["source_kind"] for item in sources} == {
        "following_user", "subscribed_series"
    }
~~~

- [ ] 步骤 2：运行红灯测试

运行：python -m pytest tests/test_rescue_storage.py::test_rebuild_catalog_classifies_items_and_sources -q

预期：失败，目录表和刷新方法不存在。

- [ ] 步骤 3：实现目录表和归一化

在 schema.py 创建 rescue_catalog、rescue_catalog_sources、rescue_catalog_meta 及索引。RescueMixin 内部实现以下函数：

~~~python
def _normalize_source(row: sqlite3.Row) -> dict[str, Any]: ...
def _catalog_series_rows() -> list[dict[str, Any]]: ...
def _catalog_novel_rows(rescue_series_ids: set[int]) -> list[dict[str, Any]]: ...
def _catalog_sources(item_type: str, item_id: int) -> list[dict[str, Any]]: ...
def rebuild_rescue_catalog(self) -> dict[str, int]: ...
~~~

使用 has_content 做完整度聚合；先写系列条目，再写未被系列覆盖的 series_chapter 和 standalone；来源映射严格使用规格中的四类标签，未知值写入 other。完整刷新使用 with self.transaction()，成功后才写 rescue_catalog_meta。

- [ ] 步骤 4：运行绿灯测试

运行：python -m pytest tests/test_rescue_storage.py::test_rebuild_catalog_classifies_items_and_sources -q

预期：通过，且不读取正文列。

- [ ] 步骤 5：提交

~~~powershell
git add src/pixiv_novel_sync/storage/schema.py src/pixiv_novel_sync/storage/rescue.py tests/test_rescue_storage.py
git commit -m "feat: 增加救援预计算目录"
~~~

## 任务 3：目录刷新回滚和增量接口

**文件：**
- 修改：src/pixiv_novel_sync/storage/rescue.py
- 修改：src/pixiv_novel_sync/storage/novels.py
- 修改：src/pixiv_novel_sync/storage/series.py
- 测试：tests/test_rescue_storage.py

**接口：** refresh_rescue_item() 刷新目标小说、父系列及受影响的系列章节；删除实体保证派生目录通过外键或显式删除清理。

- [ ] 步骤 1：写失败测试

~~~python
def test_catalog_refresh_failure_keeps_previous_snapshot(db: Database, monkeypatch) -> None:
    _seed_novel(db, 210, status="deleted", text="旧正文")
    db.rebuild_rescue_catalog()
    before = db.get_rescue_catalog_meta()

    monkeypatch.setattr(
        db, "_catalog_novel_rows",
        lambda _ids: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError, match="boom"):
        db.rebuild_rescue_catalog()

    assert db.get_rescue_catalog_meta() == before
    assert db.get_rescue_catalog_item("novel", 210)["rescue_state"] == "success"
~~~

- [ ] 步骤 2：运行红灯测试

运行：python -m pytest tests/test_rescue_storage.py::test_catalog_refresh_failure_keeps_previous_snapshot -q

预期：失败，目录快照方法不存在或无法保留事务前数据。

- [ ] 步骤 3：实现增量与清理

实现局部刷新时复用同一分类规则，只删除并重建受影响条目的目录和来源；系列变更覆盖父系列及其章节。delete_novel()、delete_series() 在现有事务中显式删除目录记录；系列删除后章节改为无系列并由调用方刷新其单篇分类。纠错写入/删除后由 Web 层调用 refresh_rescue_item()。

- [ ] 步骤 4：运行绿灯测试

运行：python -m pytest tests/test_rescue_storage.py::test_catalog_refresh_failure_keeps_previous_snapshot tests/test_rescue_storage.py::test_delete_novel_cleans_rescue_override tests/test_rescue_storage.py::test_delete_series_cleans_rescue_override -q

预期：全部通过。

- [ ] 步骤 5：提交

~~~powershell
git add src/pixiv_novel_sync/storage/rescue.py src/pixiv_novel_sync/storage/novels.py src/pixiv_novel_sync/storage/series.py tests/test_rescue_storage.py
git commit -m "feat: 支持救援目录增量刷新与回滚"
~~~

## 任务 4：SQL 列表查询和管理接口

**文件：**
- 修改：src/pixiv_novel_sync/storage/rescue.py
- 修改：src/pixiv_novel_sync/rescue_web.py
- 测试：tests/test_rescue_api.py
- 新建：tests/test_rescue_catalog_performance.py

**接口：** list_rescues(page, page_size, state, item_type, search, sort, content_kind="all", source_kind="all")；列表响应增加 content_kind_label、sources、refreshed_at、stale。

- [ ] 步骤 1：写失败测试

~~~python
def test_dashboard_rescue_list_filters_content_kind_and_source(client) -> None:
    response = client.get(
        "/api/dashboard/rescues?content_kind=standalone&source_kind=bookmark"
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert all(item["content_kind"] == "standalone" for item in data["items"])
    assert all(
        any(source["kind"] == "bookmark" for source in item["sources"])
        for item in data["items"]
    )


def test_dashboard_rescue_list_returns_503_before_first_refresh(client, monkeypatch) -> None:
    monkeypatch.setattr(Database, "get_rescue_catalog_meta", lambda self: None)
    response = client.get("/api/dashboard/rescues")
    assert response.status_code == 503
    assert response.get_json()["ok"] is False
~~~

- [ ] 步骤 2：运行红灯测试

运行：python -m pytest tests/test_rescue_api.py::test_dashboard_rescue_list_filters_content_kind_and_source tests/test_rescue_api.py::test_dashboard_rescue_list_returns_503_before_first_refresh -q

预期：失败，接口尚未读取新参数或目录表。

- [ ] 步骤 3：实现 SQL 分页接口

将 RescueMixin.list_rescues() 改为只查询 rescue_catalog：

1. 构造白名单 WHERE 条件和参数；
2. 用 COUNT(*) 获取总数；
3. 用 ORDER BY ... LIMIT ? OFFSET ? 获取当前页；
4. 对当前页项目执行一次来源批量查询并按固定顺序装配。

搜索只匹配目录中的标题和作者名；来源筛选使用 EXISTS，确保多来源条目被任一来源命中。保留 item_type 兼容映射。目录缺少元数据时返回 CatalogNotReadyError，Web 层映射为 503。

在 rescue_web.py 解析 content_kind、source_kind，按规格计算 stale 阈值并返回。禁止把 text_raw 加入列表 SQL。

- [ ] 步骤 4：运行绿灯测试

运行：
~~~powershell
python -m pytest tests/test_rescue_api.py::test_dashboard_rescue_list_filters_content_kind_and_source tests/test_rescue_api.py::test_dashboard_rescue_list_returns_503_before_first_refresh -q
~~~

预期：通过。

- [ ] 步骤 5：写性能契约测试并验证

在 tests/test_rescue_catalog_performance.py 使用 SQLite set_trace_callback() 统计 SELECT 次数，并对 SQL 文本断言不包含 text_raw；构造 200 个目录项后请求第一页，断言查询次数为固定上限（不超过 4 次），而不是随条目数增长。

运行：python -m pytest tests/test_rescue_catalog_performance.py -q

预期：通过。

- [ ] 步骤 6：提交

~~~powershell
git add src/pixiv_novel_sync/storage/rescue.py src/pixiv_novel_sync/rescue_web.py tests/test_rescue_api.py tests/test_rescue_catalog_performance.py
git commit -m "feat: 增加救援目录筛选接口"
~~~

## 任务 5：刷新任务接入

**文件：**
- 修改：src/pixiv_novel_sync/jobs/services.py
- 修改：src/pixiv_novel_sync/jobs/quick_sync.py
- 修改：src/pixiv_novel_sync/jobs/tasks.py
- 修改：src/pixiv_novel_sync/web/managers.py
- 测试：tests/test_jobs_services.py
- 测试：tests/test_jobs_quick_sync.py
- 测试：tests/test_jobs_tasks.py

**接口：** 成功完成收藏、关注用户小说、追更系列、用户备份、小说状态和系列状态任务后调用 db.rebuild_rescue_catalog() 一次；刷新失败只记录 warning，不把已成功的同步任务改成失败。

- [ ] 步骤 1：写失败测试

~~~python
def test_novel_status_task_rebuilds_catalog_after_success(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(
        services, "_process_status_items",
        lambda **kwargs: {"checked_novels": 1},
    )
    monkeypatch.setattr(
        services.Database,
        "rebuild_rescue_catalog",
        lambda self: calls.append(True) or {"items": 1},
    )
    result = services.run_novel_status_task(settings)
    assert result["rescue_catalog_items"] == 1
    assert calls == [True]
~~~

- [ ] 步骤 2：运行红灯测试

运行：python -m pytest tests/test_jobs_services.py::test_novel_status_task_rebuilds_catalog_after_success -q

预期：失败，状态任务未刷新目录。

- [ ] 步骤 3：实现统一刷新适配

新增私有辅助 _rebuild_rescue_catalog(db, reporter) -> dict[str, int]，在任务成功路径调用；将刷新统计并入返回 stats，在 reporter 中记录耗时和条目数。quick_sync.run_bookmark_sync()、jobs.tasks._run_direct_sync_task() 和 AutoSyncScheduler._sync_user_backup() 等同步入口在服务成功返回后调用同一辅助逻辑。刷新异常捕获并记录 warning，原任务结果保持成功。

调度器若发现目录元数据不存在，在启动后的后台线程执行一次刷新；不得在 Flask 请求线程执行首次刷新。若总自动同步开关关闭，也要允许这一次初始化刷新使用本地已有数据，不连接 Pixiv。

- [ ] 步骤 4：运行绿灯测试

运行：
~~~powershell
python -m pytest tests/test_jobs_services.py tests/test_jobs_quick_sync.py tests/test_jobs_tasks.py -q
~~~

预期：全部通过。

- [ ] 步骤 5：提交

~~~powershell
git add src/pixiv_novel_sync/jobs/services.py src/pixiv_novel_sync/jobs/quick_sync.py src/pixiv_novel_sync/jobs/tasks.py src/pixiv_novel_sync/web/managers.py tests/test_jobs_services.py tests/test_jobs_quick_sync.py tests/test_jobs_tasks.py
git commit -m "feat: 将救援目录刷新接入后台任务"
~~~

## 任务 6：纠错增量刷新和实体删除

**文件：**
- 修改：src/pixiv_novel_sync/rescue_web.py
- 修改：src/pixiv_novel_sync/storage/novels.py
- 修改：src/pixiv_novel_sync/storage/series.py
- 测试：tests/test_rescue_api.py
- 测试：tests/test_rescue_storage.py

- [ ] 步骤 1：写失败测试

~~~python
def test_override_updates_catalog_without_full_rebuild(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        Database,
        "refresh_rescue_item",
        lambda self, item_type, item_id: calls.append((item_type, item_id)) or {},
    )
    response = client.put(
        "/api/dashboard/rescue-overrides/novel/10",
        json={"action": "exclude"},
    )
    assert response.status_code == 200
    assert calls == [("novel", 10)]
~~~

- [ ] 步骤 2：运行红灯测试

运行：python -m pytest tests/test_rescue_api.py::test_override_updates_catalog_without_full_rebuild -q

预期：失败，纠错接口只修改人工纠错表。

- [ ] 步骤 3：实现增量刷新调用

纠错 PUT/DELETE 成功提交后调用 refresh_rescue_item()；若刷新失败返回 500 并保留纠错变更，同时记录错误。删除小说/系列时在同一事务清理目录来源；系列删除后刷新原章节分类。补充人工 include/exclude 与父系列覆盖规则测试。

- [ ] 步骤 4：运行绿灯测试

运行：python -m pytest tests/test_rescue_api.py tests/test_rescue_storage.py -q

预期：通过。

- [ ] 步骤 5：提交

~~~powershell
git add src/pixiv_novel_sync/rescue_web.py src/pixiv_novel_sync/storage/novels.py src/pixiv_novel_sync/storage/series.py tests/test_rescue_api.py tests/test_rescue_storage.py
git commit -m "feat: 让救援纠错即时刷新目录"
~~~

## 任务 7：前端筛选和卡片来源展示

**文件：**
- 修改：src/pixiv_novel_sync/templates/dashboard_novels.html
- 修改：tests/test_frontend_library_os.py

- [ ] 步骤 1：写失败静态契约测试

~~~python
def test_rescue_library_exposes_content_and_source_filters():
    html = _read_template("dashboard_novels.html")
    assert "rescueFilters.content_kind" in html
    assert "rescueFilters.source_kind" in html
    assert "item.content_kind_label" in html
    assert "item.sources" in html
    assert "data.refreshed_at" in html
~~~

- [ ] 步骤 2：运行红灯测试

运行：python -m pytest tests/test_frontend_library_os.py::test_rescue_library_exposes_content_and_source_filters -q

预期：失败，模板没有新筛选字段。

- [ ] 步骤 3：实现前端状态和卡片

将 rescueFilters 扩展为 {state, item_type, content_kind, source_kind}；请求参数加入两个新筛选，监听器同时监听四个字段并重置页码。增加三类内容选项和四类来源选项。卡片使用固定高度来源容器，来源文本通过 text interpolation 渲染，不拼接 innerHTML：

~~~html
<div class="h-10 overflow-hidden" :title="(item.sources || []).map(source => source.label).join('、')">
  <span v-for="source in (item.sources || [])"
        :key="source.kind + ':' + (source.user_id || '')">
    {{ source.label }}
  </span>
</div>
~~~

增加目录更新时间和过期提示；根据 content_kind 选择系列详情或小说阅读跳转。保持现有 Vue 结构，不引入新依赖。

- [ ] 步骤 4：运行绿灯测试

运行：python -m pytest tests/test_frontend_library_os.py -q

预期：通过。

- [ ] 步骤 5：提交

~~~powershell
git add src/pixiv_novel_sync/templates/dashboard_novels.html tests/test_frontend_library_os.py
git commit -m "feat: 展示救援类型与来源筛选"
~~~

## 任务 8：完整验证和部署验收

**文件：**
- 修改：必要时仅更新 docs/frontend-api-contract.md 中救援列表契约
- 测试：全量测试集

- [ ] 步骤 1：运行定向测试

运行：
~~~powershell
python -m pytest tests/test_rescue_storage.py tests/test_rescue_api.py tests/test_rescue_catalog_performance.py tests/test_frontend_library_os.py -q
~~~

预期：全部通过。

- [ ] 步骤 2：运行全量测试

运行：python -m pytest -q

预期：0 失败；记录通过和跳过数量。

- [ ] 步骤 3：检查差异和数据库契约

运行：
~~~powershell
git diff --check
rg -n "text_raw" src/pixiv_novel_sync/storage/rescue.py
~~~

预期：列表查询路径不出现 text_raw；只读 API 的正文读取路径仍保留。

- [ ] 步骤 4：提交并推送

~~~powershell
git status --short --branch
git push origin main
~~~

仅在测试通过且确认差异只包含本需求后推送。

- [ ] 步骤 5：服务器更新

~~~powershell
ssh -i "C:\Users\dong\Desktop\pixiv.key" ubuntu@168.107.30.164 "cd ~/pixiv-novel-sync && ./update.sh"
~~~

更新后检查 pixiv-novel-sync、nginx 为 active，确认首次目录刷新完成。

- [ ] 步骤 6：线上性能验收

使用登录会话请求 /api/dashboard/rescues?page=1&page_size=12，记录 refreshed_at、stale、条目总数和响应耗时；目标是第一页不超过 500ms。再确认任务日志包含目录刷新耗时和条目数。

---

## 计划自审

- 规格覆盖：迁移、目录表、来源映射、三类内容、四类筛选、事务回滚、刷新触发、API 兼容、前端展示、测试和部署均有任务。
- 类型一致：所有任务使用 content_kind、source_kind、rebuild_rescue_catalog()、refresh_rescue_item() 和 get_rescue_catalog_meta() 的同一命名。
- 占位符扫描：没有 TBD、TODO、implement later 或“适当处理”等未定义步骤。
- 查询性能：任务 4 明确要求 SQL 分页、正文列禁读和固定查询次数；任务 8 复核实现差异。
- 回滚安全：任务 3 和任务 5 都要求刷新异常不清空旧目录、不污染已成功任务状态。

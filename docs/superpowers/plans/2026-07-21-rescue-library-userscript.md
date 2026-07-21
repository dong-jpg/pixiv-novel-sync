# 拯救成功与 Pixiv 救援阅读实现计划

> **供自动化执行者使用：**使用 `executing-plans`，逐项执行并在每个检查点运行验证。当前按用户偏好采用内联执行，不派发子代理。每个任务完成后单独提交。

**目标：**在现有小说库中增加严格判定的“拯救成功”视图、人工纠错、独立只读救援 API 和安全的 Pixiv 油猴救援阅读脚本。

**架构：**救援状态由数据库实时聚合计算，`rescue_overrides` 只保存人工纠错；独立救援 Token 只保存摘要。管理 API 继续使用现有会话和 CSRF，`/api/rescue/v1/` 由固定 Bearer Token 单独认证。油猴脚本只在 Pixiv 内容明确失效时调用 API，并以纯文本节点渲染备份。

**技术栈：**Python 3.10、Flask、SQLite、Vue 3（现有内嵌方式）、原生 JavaScript、油猴 `GM_xmlhttpRequest`、pytest、Playwright fixture

## 全局约束

- 系列严格判定：`expected_count > 0`、`local_count >= expected_count`、`complete_count == local_count` 才是 `success`。
- `expected_count <= 0` 的失效系列最多是 `partial`，不能是 `success`。
- 人工 `include/exclude` 只能修正 Pixiv 可用性，不能绕过正文完整性。
- 不复用 `pending_deletions`，不保存物化救援状态历史。
- API 固定使用 `https://pixiv.dongboapp.com`，Token 不放 URL、不写日志、不明文落库。
- 正常可访问的 Pixiv 页面不发救援请求、不修改原 DOM。
- 正文只能通过 `textContent` 或安全文本节点渲染，禁止将备份正文写入 `innerHTML`。
- 只读 API 只接受 `GET/HEAD`，每个来源 IP 与 Token 每分钟最多 120 次。
- 每个任务都先写失败测试，再写最小实现，再运行局部测试和完整测试。
- 继续在 `main` 工作，不创建额外分支；不删除已有未跟踪文件。

## 文件地图

- 创建 `src/pixiv_novel_sync/storage/rescue.py`：救援纠错、实时聚合、Token 摘要读写。
- 修改 `src/pixiv_novel_sync/storage/schema.py`：创建救援表和索引。
- 修改 `src/pixiv_novel_sync/storage_db.py`：把 `RescueMixin` 接入 `Database`。
- 创建 `src/pixiv_novel_sync/rescue_web.py`：管理 API、只读 API、Bearer 校验和限流。
- 修改 `src/pixiv_novel_sync/webapp.py`：放行救援 API 前缀并注册路由。
- 修改 `src/pixiv_novel_sync/templates/dashboard_novels.html`：增加救援 Tab、筛选和卡片。
- 修改 `src/pixiv_novel_sync/templates/dashboard_novel_detail.html`：增加单篇纠错操作。
- 修改 `src/pixiv_novel_sync/templates/dashboard_series_detail.html`：增加系列纠错操作和覆盖率。
- 修改 `src/pixiv_novel_sync/templates/dashboard_settings.html`：增加 Token 状态和一次性轮换窗口。
- 创建 `userscripts/pixiv-rescue.user.js`：Pixiv 原页面救援阅读脚本。
- 创建 `tests/test_rescue_storage.py`：实时判定和纠错测试。
- 创建 `tests/test_rescue_api.py`：管理 API、Token 和只读 API 测试。
- 创建 `tests/test_rescue_userscript.py`：脚本静态安全检查和浏览器 fixture 测试。
- 修改 `tests/test_frontend_library_os.py`：页面契约检查。
- 修改 `docs/frontend-pages.md`、`docs/frontend-api-contract.md`：记录新页面和接口。

---

### 任务 1：创建救援表和人工纠错存储

**文件：**

- 修改：`src/pixiv_novel_sync/storage/schema.py`
- 修改：`src/pixiv_novel_sync/storage_db.py`
- 创建：`src/pixiv_novel_sync/storage/rescue.py`
- 创建：`tests/test_rescue_storage.py`

**接口：**

- `get_rescue_override(item_type: str, item_id: int) -> dict[str, Any] | None`
- `set_rescue_override(item_type: str, item_id: int, action: str, note: str = "") -> dict[str, Any]`
- `delete_rescue_override(item_type: str, item_id: int) -> bool`
- `get_rescue_token_record() -> dict[str, Any] | None`
- `save_rescue_token_record(token_hash: str, token_prefix: str) -> dict[str, Any]`

- [ ] **步骤 1：先写失败测试**

在 `tests/test_rescue_storage.py` 写入：

```python
from pixiv_novel_sync.storage_db import Database


def test_rescue_schema_and_override_crud(tmp_path):
    db = Database(tmp_path / "rescue.db")
    db.init_schema()
    db.conn.execute(
        "INSERT INTO novels (novel_id, user_id, title, visible, restrict_value, x_restrict, text_length, total_bookmarks, total_views, tags_json, raw_json, meta_hash) VALUES (1, 2, 'n', 1, 'public', 0, 1, 0, 0, '[]', '{}', 'h')"
    )
    db.conn.commit()

    assert db.get_rescue_override("novel", 1) is None
    saved = db.set_rescue_override("novel", 1, "include", "页面已失效")
    assert saved["action"] == "include"
    assert db.get_rescue_override("novel", 1)["note"] == "页面已失效"
    assert db.delete_rescue_override("novel", 1) is True
    assert db.get_rescue_override("novel", 1) is None
    db.close()


def test_rescue_override_rejects_invalid_values(tmp_path):
    db = Database(tmp_path / "rescue.db")
    db.init_schema()
    try:
        db.set_rescue_override("user", 1, "include")
    except ValueError as exc:
        assert "item_type" in str(exc)
    else:
        raise AssertionError("invalid item_type was accepted")
    db.close()
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest tests/test_rescue_storage.py -q`

预期：因 `rescue_overrides` 表和 `RescueMixin` 方法不存在而失败。

- [ ] **步骤 3：添加迁移和 Mixin**

在 `SchemaMixin.init_schema()` 的待确认删除迁移之后调用 `_migrate_rescue_tables()`，新增：

```python
def _migrate_rescue_tables(self) -> None:
    self.conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rescue_overrides (
            item_type TEXT NOT NULL CHECK (item_type IN ('novel', 'series')),
            item_id INTEGER NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('include', 'exclude')),
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (item_type, item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_rescue_overrides_action
            ON rescue_overrides(action);
        CREATE TABLE IF NOT EXISTS rescue_api_token (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            token_hash TEXT NOT NULL,
            token_prefix TEXT NOT NULL,
            rotated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    self._commit_if_needed()
```

`storage_db.py` 导入 `RescueMixin`，并把它放入 `Database` 继承列表。`storage/rescue.py` 的写入方法必须校验：`item_type in {'novel', 'series'}`、`action in {'include', 'exclude'}`、对象存在、备注长度不超过 500；写入使用 `INSERT` 配合 `ON CONFLICT DO UPDATE` 和 `CURRENT_TIMESTAMP`。

- [ ] **步骤 4：运行局部测试**

运行：`python -m pytest tests/test_rescue_storage.py -q`

预期：`2 passed`。

- [ ] **步骤 5：同步删除逻辑并提交**

在 `NovelsMixin.delete_novel()` 和 `SeriesMixin.delete_series()` 的同一事务中加入：

```python
self.conn.execute(
    "DELETE FROM rescue_overrides WHERE item_type = 'novel' AND item_id = ?",
    (novel_id,),
)
```

系列方法将 `novel` 改为 `series`、参数改为 `series_id`。运行 `python -m pytest tests/test_storage_db.py tests/test_rescue_storage.py -q`，然后提交：

```bash
git add src/pixiv_novel_sync/storage/schema.py src/pixiv_novel_sync/storage/rescue.py src/pixiv_novel_sync/storage_db.py src/pixiv_novel_sync/storage/novels.py src/pixiv_novel_sync/storage/series.py tests/test_rescue_storage.py
git commit -m "feat: 增加救援纠错存储"
```

### 任务 2：实现实时救援判定和 Token 存储接口

**文件：**

- 修改：`src/pixiv_novel_sync/storage/rescue.py`
- 创建：`tests/test_rescue_storage.py`（追加测试）

**接口：**

- `list_rescues(page: int = 1, page_size: int = 12, state: str = "all", item_type: str = "all", search: str = "", sort: str = "checked_desc") -> dict[str, Any]`
- `get_rescue_novel(novel_id: int) -> dict[str, Any] | None`
- `get_rescue_series(series_id: int) -> dict[str, Any] | None`
- `list_rescue_series_chapters(series_id: int, page: int = 1, page_size: int = 100) -> dict[str, Any] | None`

- [ ] **步骤 1：写判定失败测试**

在测试中用直接 SQL 创建小说、正文、系列和状态，覆盖以下断言：

```python
from pathlib import Path

import pytest

from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "rescue.db")
    database.init_schema()
    yield database
    database.close()


def seed_series(db: Database, *, series_id: int, status: str, total_novels: int) -> None:
    db.conn.execute(
        "INSERT OR IGNORE INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')"
    )
    db.conn.execute(
        "INSERT INTO series (series_id, title, user_id, total_novels, status) VALUES (?, ?, 1, ?, ?)",
        (series_id, f"系列 {series_id}", total_novels, status),
    )
    db.conn.commit()


def seed_novel(
    db: Database,
    *,
    novel_id: int,
    status: str = "normal",
    text: str,
    series_id: int | None = None,
) -> None:
    db.conn.execute(
        "INSERT OR IGNORE INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')"
    )
    db.conn.execute(
        """
        INSERT INTO novels (
            novel_id, user_id, series_id, title, visible, restrict_value,
            x_restrict, text_length, total_bookmarks, total_views,
            tags_json, raw_json, meta_hash, status
        ) VALUES (?, 1, ?, ?, 1, 'public', 0, ?, 0, 0, '[]', '{}', ?, ?)
        """,
        (novel_id, series_id, f"小说 {novel_id}", len(text), f"h-{novel_id}", status),
    )
    db.conn.execute(
        "INSERT INTO novel_texts (novel_id, text_raw, text_hash) VALUES (?, ?, ?)",
        (novel_id, text, f"t-{novel_id}"),
    )
    db.conn.commit()


def test_deleted_novel_with_body_is_success(db):
    seed_novel(db, novel_id=10, status="deleted", text="正文")
    item = db.get_rescue_novel(10)
    assert item["rescue_state"] == "success"
    assert item["eligibility_reason"] == "novel_unavailable"


def test_deleted_novel_without_body_is_hidden(db):
    seed_novel(db, novel_id=11, status="deleted", text="")
    assert db.get_rescue_novel(11) is None


def test_series_requires_expected_count_and_every_body(db):
    seed_series(db, series_id=20, status="deleted", total_novels=3)
    seed_novel(db, novel_id=21, series_id=20, text="一")
    seed_novel(db, novel_id=22, series_id=20, text="二")
    seed_novel(db, novel_id=23, series_id=20, text="三")
    item = db.get_rescue_series(20)
    assert item["rescue_state"] == "success"
    assert item["complete_count"] == 3


def test_series_with_unknown_total_is_partial(db):
    seed_series(db, series_id=30, status="deleted", total_novels=0)
    seed_novel(db, novel_id=31, series_id=30, text="一")
    item = db.get_rescue_series(30)
    assert item["rescue_state"] == "partial"


def test_parent_series_allows_normal_chapter_api(db):
    seed_series(db, series_id=40, status="deleted", total_novels=1)
    seed_novel(db, novel_id=41, series_id=40, status="normal", text="章节")
    assert db.get_rescue_novel(41)["eligibility_reason"] == "parent_series_unavailable"
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest tests/test_rescue_storage.py -q`

预期：因三个实时查询方法不存在而失败。

- [ ] **步骤 3：实现聚合查询**

在 `RescueMixin` 中用一个系列聚合查询计算 `expected_count`、`local_count`、`complete_count`，再用单篇查询计算小说正文和作者。查询必须 `TRIM(COALESCE(nt.text_raw, '')) != ''` 才算完整。

系列状态使用以下纯函数，避免列表、详情和 API 出现不同判定：

```python
def _series_state(remote_unavailable: bool, expected: int, local: int, complete: int) -> str | None:
    if not remote_unavailable or complete == 0:
        return None
    if expected > 0 and local >= expected and complete == local:
        return "success"
    return "partial"
```

人工动作优先级固定为：`exclude` → 未失效，`include` → 失效，无纠错 → 使用数据库状态。`list_rescues()` 先生成系列项，再生成不属于已显示救援系列的单篇项，按 `checked_desc` 或 `updated_desc` 排序后分页；`state` 和 `item_type` 在分页前过滤。搜索同时匹配标题和作者名。

返回的统一列表字段为：

```python
{
    "item_type": "novel" or "series",
    "item_id": int,
    "title": str,
    "author_name": str,
    "cover_url": str | None,
    "rescue_state": "success" or "partial",
    "remote_status": str,
    "eligibility_reason": str,
    "expected_count": int | None,
    "local_count": int,
    "complete_count": int,
    "last_checked_at": str | None,
    "updated_at": str | None,
}
```

`get_rescue_novel()` 额外返回正文、简介、标签、系列 ID；`get_rescue_series()` 只返回系列元数据和完整度；`list_rescue_series_chapters()` 只返回有正文的章节目录，并使用 `create_date ASC, novel_id ASC`。

- [ ] **步骤 4：实现 Token 摘要存储**

为 `RescueMixin` 增加：

```python
def get_rescue_token_record(self) -> dict[str, Any] | None:
    row = self.conn.execute(
        "SELECT token_hash, token_prefix, rotated_at FROM rescue_api_token WHERE singleton_id = 1"
    ).fetchone()
    return dict(row) if row else None


def save_rescue_token_record(self, token_hash: str, token_prefix: str) -> dict[str, Any]:
    with self.transaction():
        self.conn.execute(
            """
            INSERT INTO rescue_api_token (singleton_id, token_hash, token_prefix, rotated_at)
            VALUES (1, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(singleton_id) DO UPDATE SET
                token_hash = excluded.token_hash,
                token_prefix = excluded.token_prefix,
                rotated_at = CURRENT_TIMESTAMP
            """,
            (token_hash, token_prefix),
        )
    return self.get_rescue_token_record() or {}
```

- [ ] **步骤 5：运行判定测试并提交**

运行：`python -m pytest tests/test_rescue_storage.py -q`，预期至少 `7 passed`。然后提交：

```bash
git add src/pixiv_novel_sync/storage/rescue.py tests/test_rescue_storage.py
git commit -m "feat: 实现实时救援判定"
```

### 任务 3：注册管理 API、只读 API 和独立认证

**文件：**

- 创建：`src/pixiv_novel_sync/rescue_web.py`
- 修改：`src/pixiv_novel_sync/webapp.py`
- 创建：`tests/test_rescue_api.py`

**接口：**

- `register_rescue_routes(app: Flask, settings: Callable[[], Settings], client_addr: Callable[[], str]) -> None`

- [ ] **步骤 1：写 API 失败测试**

测试先构造临时应用和数据库，验证：

```python
from pathlib import Path

import pytest

from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.webapp import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    db_path = tmp_path / "state" / "rescue.db"
    monkeypatch.setenv("PIXIV_DB_PATH", str(db_path))
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    db = Database(db_path)
    db.init_schema()
    db.conn.execute(
        "INSERT INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')"
    )
    db.conn.execute(
        """
        INSERT INTO novels (
            novel_id, user_id, title, visible, restrict_value, x_restrict,
            text_length, total_bookmarks, total_views, tags_json, raw_json,
            meta_hash, status
        ) VALUES (10, 1, '救援小说', 1, 'public', 0, 2, 0, 0, '[]', '{}', 'h', 'deleted')
        """
    )
    db.conn.execute(
        "INSERT INTO novel_texts (novel_id, text_raw, text_hash) VALUES (10, '正文', 't')"
    )
    db.conn.commit()
    db.close()
    return create_app(env_path=str(env_path)).test_client()


def rotate_token(client) -> str:
    response = client.post("/api/dashboard/rescue-token/rotate")
    assert response.status_code == 200
    return str(response.get_json()["data"]["token"])


def test_rescue_public_api_requires_bearer_token(client):
    response = client.get("/api/rescue/v1/novels/10")
    assert response.status_code == 401
    assert "Bearer" in response.headers["WWW-Authenticate"]


def test_rescue_public_api_rejects_query_token(client):
    response = client.get("/api/rescue/v1/novels/10?token=secret")
    assert response.status_code == 401


def test_dashboard_can_rotate_and_use_only_latest_token(client):
    first = client.post("/api/dashboard/rescue-token/rotate")
    assert first.status_code == 200
    old_token = first.get_json()["data"]["token"]
    second = client.post("/api/dashboard/rescue-token/rotate")
    assert second.status_code == 200
    new_token = second.get_json()["data"]["token"]
    assert old_token != new_token
    assert client.get(
        "/api/rescue/v1/novels/10",
        headers={"Authorization": f"Bearer {old_token}"},
    ).status_code == 401


def test_rescue_response_omits_raw_json_and_paths(client):
    token = rotate_token(client)
    response = client.get(
        "/api/rescue/v1/novels/10",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = response.get_json()
    assert response.status_code == 200
    assert "text_raw" in body["data"]
    assert "raw_json" not in body["data"]
    assert "local_path" not in body["data"]
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest tests/test_rescue_api.py -q`

预期：因路由未注册而出现 `404` 或缺少 Token 实现错误。

- [ ] **步骤 3：接入认证分流**

在 `webapp.py` 的 `_check_auth()` 中，在精确路径判断前加入：

```python
if path.startswith("/api/rescue/v1/"):
    return
```

只放行 v1 前缀，不放行管理 API。注册路由的位置紧跟 `register_preference_routes(app, current_settings_for_routes)`，调用：

```python
from .rescue_web import register_rescue_routes
register_rescue_routes(app, current_settings_for_routes, _client_addr)
```

- [ ] **步骤 4：实现 Token 校验和限流**

`rescue_web.py` 使用固定摘要函数：

```python
def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bearer_token() -> str | None:
    value = request.headers.get("Authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()
```

校验时用 `secrets.compare_digest(stored_hash, _token_digest(candidate))`；不读取查询参数、不读取 Cookie。模块内的滑动窗口限流器以 `(client_addr(), token_prefix)` 为键，复用 `webapp.py` 已有的可信代理地址解析，保存最近 60 秒的时间戳，超过 120 次返回 `429`。异常响应只返回通用中文错误。

为 `/api/rescue/v1/` 响应增加 `Cache-Control: no-store`、`X-Robots-Tag: noindex, nofollow, noarchive` 和 `X-Content-Type-Options: nosniff`。管理 API 使用现有 `{ok, data, error}` 格式。

- [ ] **步骤 5：实现管理和只读路由**

实现以下路由及校验：

```python
@app.get("/api/dashboard/rescues")
@app.put("/api/dashboard/rescue-overrides/<item_type>/<int:item_id>")
@app.delete("/api/dashboard/rescue-overrides/<item_type>/<int:item_id>")
@app.get("/api/dashboard/rescue-token/status")
@app.post("/api/dashboard/rescue-token/rotate")
@app.get("/api/rescue/v1/novels/<int:novel_id>")
@app.get("/api/rescue/v1/series/<int:series_id>")
@app.get("/api/rescue/v1/series/<int:series_id>/chapters")
```

轮换逻辑生成 `rsq_` 加 `secrets.token_urlsafe(32)`，只把明文放入轮换响应；状态响应只返回 `configured`、`token_prefix`、`rotated_at`。只读小说响应使用显式白名单，固定加入：

```json
{
  "source_notice": "内容来自私人备份，并非 Pixiv 官方恢复",
  "rescue_state": "success",
  "eligibility_reason": "novel_unavailable"
}
```

未授权、不可救援或不存在的小说/系列统一返回 `404`（认证失败仍返回 `401`）。Flask 对只读路由的 `POST/PUT/DELETE` 返回 `405`。

- [ ] **步骤 6：运行 API 测试并提交**

运行：`python -m pytest tests/test_rescue_api.py tests/test_rescue_storage.py -q`，预期全部通过。提交：

```bash
git add src/pixiv_novel_sync/rescue_web.py src/pixiv_novel_sync/webapp.py tests/test_rescue_api.py
git commit -m "feat: 增加救援 API 与独立 Token"
```

### 任务 4：增加小说库 Tab 和人工纠错界面

**文件：**

- 修改：`src/pixiv_novel_sync/templates/dashboard_novels.html`
- 修改：`src/pixiv_novel_sync/templates/dashboard_novel_detail.html`
- 修改：`src/pixiv_novel_sync/templates/dashboard_series_detail.html`
- 修改：`src/pixiv_novel_sync/templates/dashboard_settings.html`
- 修改：`tests/test_frontend_library_os.py`
- 修改：`tests/test_rescue_api.py`

**接口：**

- `dashboard_novels.html` 使用 `/api/dashboard/rescues`，不把系列章节扁平化为小说卡片。
- 详情页纠错使用 `PUT/DELETE /api/dashboard/rescue-overrides/<item_type>/<item_id>`，所有写请求携带 `X-CSRF-Token`。
- 设置页轮换使用 `POST /api/dashboard/rescue-token/rotate`，关闭窗口时清空明文变量。

- [ ] **步骤 1：写前端契约失败测试**

在 `tests/test_frontend_library_os.py` 追加：

```python
def test_library_contains_rescue_tab_and_api_contract():
    html = read(TEMPLATES / "dashboard_novels.html")
    assert "filters.category = 'rescue'" in html
    assert "/api/dashboard/rescues" in html
    assert "部分救援" in html
    assert "来自私人备份" in html


def test_settings_contains_rescue_token_rotation():
    html = read(TEMPLATES / "dashboard_settings.html")
    assert "/api/dashboard/rescue-token/status" in html
    assert "/api/dashboard/rescue-token/rotate" in html
    assert "rescueTokenPlaintext" in html
```

运行：`python -m pytest tests/test_frontend_library_os.py -q`，预期新增断言失败。

- [ ] **步骤 2：增加救援 Tab 和筛选状态**

在 `dashboard_novels.html` 将分类白名单改为 `bookmark/following/ai/rescue`；救援分类使用单独的 `rescueFilters.state` 和 `rescueFilters.item_type`，请求参数按以下方式生成：

```javascript
const params = new URLSearchParams({
  page: String(page.value),
  page_size: String(pageSize.value),
  state: rescueFilters.state,
  item_type: rescueFilters.item_type,
  search: filters.search,
  sort: filters.sort || 'checked_desc'
});
const response = await fetch('/api/dashboard/rescues?' + params.toString(), {
  signal: fetchAbortController.signal
});
```

新增混合卡片分支：根据 `item.item_type` 选择 `/dashboard/novels/<id>` 或 `/dashboard/series/<id>`，显示 `success/partial` 标签和系列覆盖率。正常列表的既有分支不得改变。

- [ ] **步骤 3：增加详情页纠错操作**

在两个详情模板各增加 `rescueOverride`、`rescueMessage` 状态、CSRF 获取函数和三个操作函数。请求体固定为：

```javascript
async function saveRescueOverride(action, note) {
  const token = await ensureCsrfToken();
  const response = await fetch(`/api/dashboard/rescue-overrides/${itemType}/${itemId}`, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token},
    body: JSON.stringify({action, note})
  });
  if (!response.ok) throw new Error((await response.json()).error || '保存失败');
}
```

页面重新读取详情后显示当前自动状态、人工动作和系列 `complete_count/expected_count`。只显示清晰的状态操作，不把 Token 或正文写入按钮属性。

- [ ] **步骤 4：增加设置页 Token 区域**

在 `dashboard_settings.html` 增加 `rescue-api` Tab。`loadRescueTokenStatus()` 只读取状态；`rotateRescueToken()` 成功后把返回值放入 `rescueTokenPlaintext`，提供复制按钮和关闭按钮，关闭时执行 `rescueTokenPlaintext = ''`。任何错误提示不得拼接 Token 值。

- [ ] **步骤 5：运行前端和接口测试并提交**

运行：

```bash
python -m pytest tests/test_frontend_library_os.py tests/test_rescue_api.py -q
```

预期全部通过。提交：

```bash
git add src/pixiv_novel_sync/templates/dashboard_novels.html src/pixiv_novel_sync/templates/dashboard_novel_detail.html src/pixiv_novel_sync/templates/dashboard_series_detail.html src/pixiv_novel_sync/templates/dashboard_settings.html tests/test_frontend_library_os.py tests/test_rescue_api.py
git commit -m "feat: 增加救援小说库界面"
```

### 任务 5：实现油猴救援脚本和浏览器 fixture

**文件：**

- 创建：`userscripts/pixiv-rescue.user.js`
- 创建：`tests/test_rescue_userscript.py`

**接口：**

- 脚本固定 `API_ORIGIN = 'https://pixiv.dongboapp.com'`。
- 脚本只使用 `GM_getValue`、`GM_setValue`、`GM_registerMenuCommand`、`GM_xmlhttpRequest`。

- [ ] **步骤 1：写脚本静态安全测试**

创建测试：

```python
from pathlib import Path


SCRIPT = (Path(__file__).parents[1] / "userscripts" / "pixiv-rescue.user.js").read_text(encoding="utf-8")


def test_userscript_metadata_and_security_contract():
    assert "@match        https://www.pixiv.net/novel/show.php*" in SCRIPT
    assert "@match        https://www.pixiv.net/novel/series/*" in SCRIPT
    assert "@connect     pixiv.dongboapp.com" in SCRIPT
    assert "GM_xmlhttpRequest" in SCRIPT
    assert "Authorization" in SCRIPT
    assert "textContent" in SCRIPT
    assert "innerHTML" not in SCRIPT
    assert "?token=" not in SCRIPT


def test_userscript_never_uses_arbitrary_api_origin():
    assert "location.origin" not in SCRIPT
    assert "new URL(response" not in SCRIPT
```

运行：`python -m pytest tests/test_rescue_userscript.py -q`，预期因脚本不存在而失败。

- [ ] **步骤 2：实现脚本元数据、Token 菜单和固定请求函数**

元数据必须包含：

```javascript
// @match        https://www.pixiv.net/novel/show.php*
// @match        https://www.pixiv.net/novel/series/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      pixiv.dongboapp.com
```

请求函数只能拼接固定 `API_ORIGIN` 和经过 `encodeURIComponent` 的数字 ID；Token 只进入 `Authorization` 头。请求失败只返回内部错误类型，不把响应正文或 Token 写入控制台。

- [ ] **步骤 3：实现保守激活和安全渲染**

实现 `isNovelPageHealthy()`、`isSeriesPageHealthy()`、`renderNovel()`、`renderSeries()` 和 `renderText()`。健康判断必须先执行；发现明确正文/目录时直接结束。错误页中出现删除、受限或无正文时才请求 API。

渲染器只能使用 `document.createElement`、`textContent`、`append` 和 CSS `white-space: pre-wrap`。救援根节点使用固定类名，重复执行只复用该节点，不清空页面主容器。单篇正文失败时保留原 Pixiv 错误内容并显示可关闭提示。

- [ ] **步骤 4：增加 Playwright fixture 测试**

使用 `pytest.importorskip("playwright.sync_api")` 和本地页面 fixture：

- 健康小说 fixture：预置正文，断言 `GM_xmlhttpRequest` 调用次数为 `0`；
- 删除小说 fixture：返回固定 JSON，断言页面出现“拯救数据”和正文，且正文中的 `<script>` 字符串作为文本显示；
- 删除系列 fixture：返回目录和章节正文，断言目录可点击并按需只请求被点击章节；
- API 失败 fixture：断言原错误文本仍存在。

浏览器不可用时只跳过 Playwright 测试，静态安全测试仍必须通过。

- [ ] **步骤 5：运行脚本测试并提交**

运行：`python -m pytest tests/test_rescue_userscript.py -q`，预期静态测试通过、浏览器 fixture 在可用环境通过或明确跳过。提交：

```bash
git add userscripts/pixiv-rescue.user.js tests/test_rescue_userscript.py
git commit -m "feat: 增加 Pixiv 救援油猴脚本"
```

### 任务 6：补充文档、完整验证和部署

**文件：**

- 修改：`docs/frontend-pages.md`
- 修改：`docs/frontend-api-contract.md`
- 修改：`tests/test_frontend_library_os.py`

- [ ] **步骤 1：更新接口和页面文档**

在 `docs/frontend-pages.md` 增加 `/dashboard/novels?category=rescue` 和救援脚本文件说明；在 `docs/frontend-api-contract.md` 增加三组管理 API、三组只读 API、认证头、`401/404/405/429` 行为和字段白名单。文档不得记录真实 Token。

- [ ] **步骤 2：运行静态检查和完整测试**

运行：

```bash
python -m pytest tests/test_rescue_storage.py tests/test_rescue_api.py tests/test_rescue_userscript.py tests/test_frontend_library_os.py -q
python -m pytest tests -q
git diff --check
```

预期：新增测试和完整测试全部通过；完整测试不出现失败，Playwright 仅在缺少浏览器时显示明确跳过。

- [ ] **步骤 3：审查敏感信息和工作区**

运行：

```bash
rg -n "Bearer rsq_|RESCUE_API_TOKEN|token_hash|innerHTML|\?token=" src userscripts tests docs
git status --short --branch
```

预期：源码、脚本、测试和文档中没有真实 Token；油猴脚本没有 `innerHTML` 或 URL Token；工作区只包含本功能文件。

- [ ] **步骤 4：提交文档并推送主分支**

```bash
git add docs/frontend-pages.md docs/frontend-api-contract.md tests/test_frontend_library_os.py
git commit -m "docs: 补充救援功能接口文档"
git push origin main
```

- [ ] **步骤 5：服务器部署和端到端检查**

在服务器执行：

```bash
cd ~/pixiv-novel-sync && ./update.sh
```

随后验证：

```bash
curl -i https://pixiv.dongboapp.com/api/rescue/v1/novels/1
curl -i -H 'Authorization: Bearer invalid' https://pixiv.dongboapp.com/api/rescue/v1/novels/1
sudo systemctl is-active nginx pixiv-novel-sync
sudo nginx -t
```

预期：无 Token 返回 `401`，错误 Token 仍返回 `401`，两个服务为 `active`，Nginx 配置测试成功。首个真实 Token 由后台“救援 API”区域生成，不在终端输出或对话中传递。

## 计划自检清单

- 数据层覆盖单篇、系列、部分、总数未知、父系列和人工纠错。
- API 层覆盖认证、轮换、限流、字段白名单、状态码和安全响应头。
- 前端层覆盖 Tab、筛选、详情纠错、Token 一次性显示。
- 油猴层覆盖正常页面不改写、按需加载、纯文本渲染和失败保留原页面。
- 文档和部署步骤覆盖 HTTPS 域名、Token 使用和服务器更新。
- 未引入模型池或成人润色 Agent 的无关改动。

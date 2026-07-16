# AI 封面与风格控制实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 AI 创作项目增加安全的本地封面，并让项目级风格设置拥有清晰保存语义且覆盖关键正文生成路径。

**Architecture:** `ai_writing_projects.cover_path` 只保存公共存储根目录下的相对路径，上传和读取由鉴权后的 Flask 路由处理。风格设置继续存放在 `settings_json.style_control`，通过项目服务中的统一辅助函数注入规划、续写和章节润色 Prompt。

**Tech Stack:** Python 3.10+、Flask、SQLite、Vue 3、pytest

## Global Constraints

- 不引入在线图片生成服务或图像处理依赖。
- 封面只接受 JPEG、PNG、WebP，最大 10 MiB。
- 文件路径必须限制在 `public_dir` 内，并使用原子写入。
- 历史项目没有封面或风格设置时保持当前行为。
- 风格值为 50 时不产生对应指令。
- 严格执行 TDD 和独立提交。

---

## 文件结构

- 修改 `src/pixiv_novel_sync/storage/schema.py`：增加可重复执行的 `cover_path` 迁移。
- 修改 `src/pixiv_novel_sync/storage/ai/writing.py`：读写封面路径。
- 修改 `src/pixiv_novel_sync/ai/services/projects.py`：项目响应补 `cover_url`，集中风格 Prompt 注入。
- 修改 `src/pixiv_novel_sync/ai_web.py`：封面上传、读取、删除接口。
- 修改 `src/pixiv_novel_sync/templates/dashboard_novels.html`：AI 作品卡片显示真实封面。
- 修改 `src/pixiv_novel_sync/templates/dashboard_ai_reader.html`：阅读页显示真实封面。
- 修改 `src/pixiv_novel_sync/templates/dashboard_ai.html`：封面管理、风格独立保存和 2×2 项目总览布局。
- 创建 `tests/test_ai_project_covers.py`：迁移、上传、安全和清理测试。
- 修改 `tests/test_style_control.py`、`tests/test_ai_prompts.py`、`tests/test_ai_service_stream_continue.py`：风格注入测试。

### Task 1: 增加封面数据模型

**Files:**
- Modify: `src/pixiv_novel_sync/storage/schema.py:624-715`
- Modify: `src/pixiv_novel_sync/storage/ai/writing.py:15-122`
- Create: `tests/test_ai_project_covers.py`

**Interfaces:**
- Produces: `ai_writing_projects.cover_path: str | None`
- Changes: `AiWritingMixin.update_ai_writing_project()` 允许 `cover_path`
- Consumes: 相对于 `Settings.storage.public_dir` 的路径，如 `ai_projects/12/cover.webp`

- [ ] **Step 1: 写迁移和 CRUD 失败测试**

```python
def test_ai_project_cover_path_migration_and_crud(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.init_schema()
    project_id = db.create_ai_writing_project({"name": "封面测试"})

    db.update_ai_writing_project(project_id, {"cover_path": "ai_projects/1/cover.png"})
    project = db.get_ai_writing_project(project_id)

    assert project["cover_path"] == "ai_projects/1/cover.png"
    db.init_schema()
    assert db.get_ai_writing_project(project_id)["cover_path"] == "ai_projects/1/cover.png"
    db.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_ai_project_covers.py::test_ai_project_cover_path_migration_and_crud -q`

Expected: FAIL，字段不存在或更新白名单忽略 `cover_path`。

- [ ] **Step 3: 实现可重复迁移**

在建表定义中加入：

```sql
cover_path TEXT,
```

建表后为已有数据库补列：

```python
columns = {row[1] for row in self.conn.execute("PRAGMA table_info(ai_writing_projects)").fetchall()}
if "cover_path" not in columns:
    self.conn.execute("ALTER TABLE ai_writing_projects ADD COLUMN cover_path TEXT")
```

- [ ] **Step 4: 更新项目写入白名单**

```python
allowed = {
    "name", "description", "outline", "style_profile_id", "novel_profile_id",
    "settings", "status", "cover_path",
}
```

- [ ] **Step 5: 运行存储测试**

Run: `python -m pytest tests/test_ai_project_covers.py::test_ai_project_cover_path_migration_and_crud tests/test_storage_db.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add src/pixiv_novel_sync/storage/schema.py src/pixiv_novel_sync/storage/ai/writing.py tests/test_ai_project_covers.py
git commit -m "feat: store AI project cover paths"
```

### Task 2: 实现安全封面 API

**Files:**
- Modify: `src/pixiv_novel_sync/ai_web.py:1-110,597-660`
- Modify: `src/pixiv_novel_sync/ai/services/projects.py:60-100,1905-1915`
- Modify: `tests/test_ai_project_covers.py`

**Interfaces:**
- Produces: `POST /api/dashboard/ai/projects/<int:project_id>/cover`
- Produces: `GET /api/dashboard/ai/projects/<int:project_id>/cover`
- Produces: `DELETE /api/dashboard/ai/projects/<int:project_id>/cover`
- Produces: 项目字典中的 `cover_url: str | None`

- [ ] **Step 1: 写合法上传、读取和删除失败测试**

```python
import io
from pathlib import Path

import pytest

from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.webapp import create_app

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)

def make_cover_client(tmp_path: Path, monkeypatch):
    public_dir = tmp_path / "public"
    private_dir = tmp_path / "private"
    db_path = tmp_path / "ai.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "storage:\n"
        f"  public_dir: {public_dir.as_posix()}\n"
        f"  private_dir: {private_dir.as_posix()}\n"
        f"  db_path: {db_path.as_posix()}\n"
        "sync:\n"
        "  auto_sync_enabled: false\n",
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "cover-test-secret")
    app = create_app(config_path=str(config_path), env_path=str(env_path))
    app.config["TESTING"] = True
    db = Database(db_path)
    db.init_schema()
    project_id = db.create_ai_writing_project({"name": "封面测试"})
    db.close()
    client = app.test_client()
    csrf = client.get("/api/csrf-token").get_json()["csrf_token"]
    return client, project_id, public_dir, csrf

def test_ai_project_cover_upload_read_delete(tmp_path, monkeypatch):
    client, project_id, _public_dir, csrf = make_cover_client(tmp_path, monkeypatch)
    uploaded = client.post(
        f"/api/dashboard/ai/projects/{project_id}/cover",
        data={"cover": (io.BytesIO(PNG_1X1), "cover.png")},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": csrf},
    )
    assert uploaded.status_code == 200
    cover_url = uploaded.get_json()["data"]["cover_url"]
    assert client.get(cover_url).status_code == 200

    deleted = client.delete(cover_url, headers={"X-CSRF-Token": csrf})
    assert deleted.status_code == 200
    assert client.get(cover_url).status_code == 404
```

- [ ] **Step 2: 写伪造文件、超限和路径逃逸失败测试**

```python
@pytest.mark.parametrize(
    ("filename", "payload"),
    [("fake.png", b"not-an-image"), ("cover.exe", PNG_1X1)],
)
def test_ai_project_cover_rejects_invalid_files(tmp_path, monkeypatch, filename, payload):
    client, project_id, _public_dir, csrf = make_cover_client(tmp_path, monkeypatch)
    response = client.post(
        f"/api/dashboard/ai/projects/{project_id}/cover",
        data={"cover": (io.BytesIO(payload), filename)},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400

def test_ai_project_cover_rejects_oversized_file(tmp_path, monkeypatch):
    client, project_id, _public_dir, csrf = make_cover_client(tmp_path, monkeypatch)
    response = client.post(
        f"/api/dashboard/ai/projects/{project_id}/cover",
        data={"cover": (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * (10 * 1024 * 1024 + 1)), "cover.png")},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_ai_project_covers.py -q`

Expected: FAIL，路由不存在。

- [ ] **Step 4: 实现文件类型和路径辅助函数**

在 `ai_web.py` 中定义模块级常量与辅助函数：

```python
_AI_COVER_MAX_BYTES = 10 * 1024 * 1024
_AI_COVER_TYPES = {
    ".jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ".webp": ("image/webp", (b"RIFF",)),
}

def _safe_ai_cover_target(public_dir: Path, project_id: int, suffix: str) -> Path:
    root = public_dir.resolve()
    target = (root / "ai_projects" / str(project_id) / f"cover{suffix}").resolve()
    if not target.is_relative_to(root):
        raise AIServiceError("封面路径无效")
    return target
```

WebP 额外验证字节 8-11 为 `WEBP`。上传先读取最多 `_AI_COVER_MAX_BYTES + 1`，验证后使用 `FileStorage(current_settings()).write_bytes(target, payload)`。

- [ ] **Step 5: 实现三个封面路由**

上传成功后保存相对路径：

```python
relative = target.relative_to(settings.storage.public_dir.resolve()).as_posix()
service.update_writing_project(project_id, {"cover_path": relative})
return ok({"cover_url": f"/api/dashboard/ai/projects/{project_id}/cover"})
```

读取路由从数据库获取 `cover_path`，再次执行根目录包含校验后 `send_file(path)`。删除路由先清空数据库字段，再删除存在的文件及空项目目录。

- [ ] **Step 6: 为项目响应补 cover_url**

在项目服务中统一装饰返回值：

```python
def _with_project_cover_url(self, project: dict[str, Any]) -> dict[str, Any]:
    item = dict(project)
    item["cover_url"] = (
        f"/api/dashboard/ai/projects/{int(item['id'])}/cover"
        if item.get("cover_path") else None
    )
    return item
```

`list_writing_projects()`、`get_writing_project()` 和 `get_writing_project_reader()` 均使用该辅助函数。

- [ ] **Step 7: 删除项目时清理封面**

在删除路由调用服务前读取项目封面路径，业务删除成功后再删除封面文件和空目录。文件缺失不报错，越界路径只记录警告且不删除。

- [ ] **Step 8: 运行封面和安全测试**

Run: `python -m pytest tests/test_ai_project_covers.py tests/test_ai_security_hardening.py tests/test_preference_csrf.py -q`

Expected: PASS。

- [ ] **Step 9: 提交**

```powershell
git add src/pixiv_novel_sync/ai_web.py src/pixiv_novel_sync/ai/services/projects.py tests/test_ai_project_covers.py
git commit -m "feat: add secure AI project cover uploads"
```

### Task 3: 在小说库和阅读页复用封面

**Files:**
- Modify: `src/pixiv_novel_sync/templates/dashboard_novels.html:220-305`
- Modify: `src/pixiv_novel_sync/templates/dashboard_ai_reader.html:80-155,165-250`
- Modify: `src/pixiv_novel_sync/templates/dashboard_ai.html:280-415`
- Modify: `tests/test_frontend_library_os.py`

**Interfaces:**
- Consumes: 项目 `cover_url`。
- Produces: 三个页面相同的“真实封面优先、渐变回退”语义。

- [ ] **Step 1: 写三个页面封面绑定失败测试**

```python
def test_ai_project_pages_prefer_cover_url_with_gradient_fallback():
    novels = read(TEMPLATES / "dashboard_novels.html")
    reader = read(TEMPLATES / "dashboard_ai_reader.html")
    studio = read(TEMPLATES / "dashboard_ai.html")
    assert "item.cover_url" in novels
    assert "project?.cover_url" in reader
    assert "currentProject?.cover_url" in studio
    assert "coverGradient" in reader
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_frontend_library_os.py::test_ai_project_pages_prefer_cover_url_with_gradient_fallback -q`

Expected: FAIL，AI 卡片和阅读页只显示渐变。

- [ ] **Step 3: 小说库 AI 卡片增加图片层**

```html
<img v-if="item.cover_url"
     :src="item.cover_url"
     class="absolute inset-0 w-full h-full object-cover"
     @error="item.cover_url = ''">
```

保留现有渐变底层和首字符，但首字符仅在 `!item.cover_url` 时显示。

- [ ] **Step 4: 阅读页封面增加图片层**

桌面和移动端封面容器都加入：

```html
<img v-if="project?.cover_url" :src="project.cover_url" class="w-full h-full object-cover" @error="project.cover_url = ''">
<span v-else class="text-7xl font-black text-white/90 drop-shadow select-none">{{ firstChar(project?.name) }}</span>
```

- [ ] **Step 5: 项目总览增加上传、替换和删除控件**

```html
<input ref="coverInput" type="file" accept="image/jpeg,image/png,image/webp" class="hidden" @change="uploadProjectCover">
<button @click="$refs.coverInput.click()" class="px-3 py-1.5 bg-brand-500 text-white rounded-lg text-xs">
  {{ currentProject?.cover_url ? '替换封面' : '上传封面' }}
</button>
<button v-if="currentProject?.cover_url" @click="deleteProjectCover" class="px-3 py-1.5 bg-red-50 text-red-600 rounded-lg text-xs">删除封面</button>
```

上传使用 `FormData`；CSRF 头沿用项目现有全局请求配置；成功后更新 `currentProject.cover_url` 并刷新项目列表。

- [ ] **Step 6: 运行模板和封面测试**

Run: `python -m pytest tests/test_frontend_library_os.py tests/test_ai_project_covers.py -q`

Expected: PASS。

- [ ] **Step 7: 提交**

```powershell
git add src/pixiv_novel_sync/templates/dashboard_novels.html src/pixiv_novel_sync/templates/dashboard_ai_reader.html src/pixiv_novel_sync/templates/dashboard_ai.html tests/test_frontend_library_os.py
git commit -m "feat: show AI project covers across library views"
```

### Task 4: 完成风格保存和关键生成链注入

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/projects.py:580-860,1380-1540`
- Modify: `src/pixiv_novel_sync/ai/prompts.py:430-620`
- Modify: `src/pixiv_novel_sync/templates/dashboard_ai.html:296-415,1790-1850`
- Modify: `tests/test_style_control.py`
- Modify: `tests/test_ai_prompts.py`
- Modify: `tests/test_ai_service_stream_continue.py`

**Interfaces:**
- Produces: `AIProjectsMixin._project_style_control_prompt(db, project_id: int) -> str | None`
- Changes: 长篇规划、详细梗概、章节续写、对话润色和心理润色使用项目风格。
- Produces: 前端 `saveProjectStyleControl()` 独立保存风格设置。

- [ ] **Step 1: 写项目风格辅助函数失败测试**

```python
def test_project_style_control_prompt_reads_project_settings(service, fake_db):
    fake_db.get_ai_writing_project = lambda project_id: {
        "id": project_id,
        "settings": {"style_control": {"sliders": {"explicitness": 90}, "tags": ["第一人称"], "custom": "多用对话"}},
    }
    prompt = service._project_style_control_prompt(fake_db, 7)
    assert "直接露骨" in prompt
    assert "第一人称" in prompt
    assert "多用对话" in prompt
```

- [ ] **Step 2: 写规划 Prompt 注入失败测试**

```python
from pixiv_novel_sync.ai.prompts import build_longform_detail_messages, build_longform_plan_messages


def test_longform_prompts_include_project_style_constraint():
    project = {"name": "测试项目", "description": "简介", "settings": {}}
    plan_messages = build_longform_plan_messages(
        system_prompt=None,
        project=project,
        target_words=100_000,
        style_prompt="项目风格约束",
    )
    detail_messages = build_longform_detail_messages(
        system_prompt=None,
        project=project,
        longform_plan={"project_outline": "总纲", "chapters": []},
        chapters=[{"chapter_number": 1, "title": "第一章", "outline": "开篇"}],
        style_prompt="项目风格约束",
    )
    assert "项目风格约束" in plan_messages[-1]["content"]
    assert "项目风格约束" in detail_messages[-1]["content"]
```

- [ ] **Step 3: 写项目章节润色注入失败测试**

在 `tests/test_ai_service_parsing.py` 复用已有 `FakeDB`、`FakeProvider` 和 `make_service()`：

```python
def test_stream_polish_injects_project_style(monkeypatch, tmp_path):
    service = make_service(tmp_path)
    fake_db = FakeDB()
    fake_db.project["settings"] = {
        "style_control": {"sliders": {"lyricism": 90}, "tags": [], "custom": ""}
    }
    agent = AIAgentConfig(id=1, name="润色", task_type="polish_dialogue", provider_id=2, model="m", system_prompt="s")
    provider_config = AIProviderConfig(
        id=2, name="p", provider_type="openai_compatible", base_url=None,
        api_key="k", default_model="m",
    )
    captured = {}

    def capture_messages(**kwargs):
        captured.update(kwargs)
        return [{"role": "user", "content": kwargs["instruction"] or ""}]

    monkeypatch.setattr(service, "_db", lambda: fake_db)
    monkeypatch.setattr(service, "_load_agent_config", lambda _db, _agent_id: agent)
    monkeypatch.setattr(service, "_load_provider_config", lambda _db, _provider_id: provider_config)
    monkeypatch.setattr(service, "_get_provider", lambda _config: FakeProvider("润色结果"))
    monkeypatch.setattr("pixiv_novel_sync.ai.services.projects.build_polish_messages", capture_messages)

    chunks = list(service.stream_polish({"agent_id": 1, "chapter_id": 3, "text": "章节正文"}))

    assert chunks[-1].type == "done"
    assert "抒情唯美" in captured["instruction"]
```

- [ ] **Step 4: 运行测试确认失败**

Run: `python -m pytest tests/test_style_control.py tests/test_ai_prompts.py tests/test_ai_service_stream_continue.py -q`

Expected: FAIL，规划与润色路径没有项目风格。

- [ ] **Step 5: 集中项目风格读取**

```python
def _project_style_control_prompt(self, db: Database, project_id: int) -> str | None:
    if not project_id:
        return None
    project = db.get_ai_writing_project(project_id)
    if not project:
        return None
    return compose_style_control_prompt((project.get("settings") or {}).get("style_control"))
```

章节续写改用该函数，避免重复读取和拼装逻辑。

- [ ] **Step 6: 规划 Prompt 接收风格约束**

给 `build_longform_plan_messages()` 和 `build_longform_detail_messages()` 增加可空参数 `style_prompt: str | None = None`，并在用户规划要求之后加入：

```python
if style_prompt:
    parts.append(f"【全书风格约束】\n{style_prompt}")
```

长篇规划和详细梗概服务从 `project_id` 读取风格并传入。

- [ ] **Step 7: 项目章节润色接收风格约束**

`stream_polish()` 根据 `chapter_id` 读取章节的 `project_id`，取得风格后附加到 `instruction`：

```python
style_instruction = self._project_style_control_prompt(db, int(chapter.get("project_id") or 0))
instruction = str(payload.get("instruction") or "").strip()
if style_instruction:
    instruction = f"{instruction}\n\n{style_instruction}" if instruction else style_instruction
```

只对带项目章节的润色生效，通用文本润色保持原行为。

- [ ] **Step 8: 风格卡片增加独立保存动作**

实现：

```javascript
async function saveProjectStyleControl() {
  if (!currentProject.value) return;
  const existing = currentProject.value.settings && typeof currentProject.value.settings === 'object'
    ? currentProject.value.settings : {};
  const settings = { ...existing, style_control: JSON.parse(JSON.stringify(projectMetaForm.style_control)) };
  await api('/api/dashboard/ai/projects/' + currentProject.value.id, {
    method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ settings })
  });
  currentProject.value.settings = settings;
  showMessage('风格设定已保存');
}
```

风格卡片内添加“保存风格设定”按钮。档案按钮只保存 `style_profile_id` 和 `novel_profile_id`；Pipeline 启动前依次保存档案和风格。

- [ ] **Step 9: 把四张卡片改为直接 2×2 网格子项**

```html
<div v-show="projectDetailTab === 'overview'" class="grid grid-cols-1 lg:grid-cols-2 gap-5 items-stretch">
  <section class="bg-white rounded-xl border border-gray-100 shadow-sm p-5 h-full">项目信息</section>
  <section class="bg-white rounded-xl border border-gray-100 shadow-sm p-5 h-full">项目概况</section>
  <section class="bg-white rounded-xl border border-gray-100 shadow-sm p-5 h-full">套用蒸馏档案</section>
  <section class="bg-white rounded-xl border border-gray-100 shadow-sm p-5 h-full">风格控制</section>
</div>
```

保留原有字段和控件，只改变直接父子关系和保存按钮位置。

- [ ] **Step 10: 运行风格与前端回归**

Run: `python -m pytest tests/test_style_control.py tests/test_ai_prompts.py tests/test_ai_service_stream_continue.py tests/test_frontend_library_os.py -q`

Expected: PASS。

- [ ] **Step 11: 提交**

```powershell
git add src/pixiv_novel_sync/ai/services/projects.py src/pixiv_novel_sync/ai/prompts.py src/pixiv_novel_sync/templates/dashboard_ai.html tests/test_style_control.py tests/test_ai_prompts.py tests/test_ai_service_stream_continue.py tests/test_frontend_library_os.py
git commit -m "feat: complete project style controls"
```

### Task 5: 阶段回归

**Files:**
- Verify only

**Interfaces:**
- Consumes: Tasks 1-4。
- Produces: 可独立交付的封面和风格控制功能。

- [ ] **Step 1: 运行定向测试**

Run: `python -m pytest tests/test_ai_project_covers.py tests/test_style_control.py tests/test_ai_prompts.py tests/test_ai_service_stream_continue.py tests/test_frontend_library_os.py tests/test_ai_security_hardening.py -q`

Expected: PASS，无新增跳过。

- [ ] **Step 2: 检查差异**

Run: `git diff --check HEAD~4..HEAD`

Expected: 无输出。

- [ ] **Step 3: 检查工作区**

Run: `git status --short --branch`

Expected: 没有未提交业务代码。

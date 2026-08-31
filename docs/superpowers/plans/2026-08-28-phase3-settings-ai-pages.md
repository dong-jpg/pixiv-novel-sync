# 阶段三：设置页拆分与 AI 页面公共层 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 1817 行的设置页拆成五个一级页面并支持分区独立保存，抽出前端公共层消除 4 处 csrfFetch / 7 处 SSE 重复，新增 Agent 候选模型链预览。

**Architecture:** 后端 `save_sync_settings` 增加 `section` 参数做字段白名单过滤（`section=None` 保持全量行为向后兼容）；新增 5 个页面路由与 1 个候选链只读端点。前端把公共工具挂到 `base.html` 的 `window` 上，各页删除自建副本。模板按职责拆分，Vue 应用仍是每页一个 `createApp`。

**Tech Stack:** Flask（非 blueprint 的 `register_*` 注册函数）、Jinja2（变量定界符已改为 `{[ ]}`）、Vue 3 CDN 全局构建、Tailwind CDN、pytest。无 JS 构建步骤，无 `package.json`。

**Spec:** `docs/superpowers/specs/2026-08-28-sync-budget-and-settings-redesign-design.md`（§6）

## Global Constraints

- 模块首行 `from __future__ import annotations`；dataclass 用 `slots=True`。
- 代码注释与用户可见文案用中文。commit 用 `type: subject`。
- 前端所有变更类请求必须走 `window.csrfFetch`，错误提示必须走 `window.errorText`。禁止手写 `fetch` 发 POST/PUT/PATCH/DELETE。
- Jinja 变量定界符是 `{[ ]}`（Vue 占用了 `{{ }}`）。新模板必须遵守。
- 每个新模板必须包含 `library-page` 与 `library-page-header` class（`tests/test_frontend_library_os.py` 断言）。
- 测试跑 `python -m pytest`，静态检查只有 `python -m compileall -q src`。
- 业务生成必须走 `ModelRouter`；本阶段新增的候选链端点只调 `resolve_candidates`，**不得**发起任何真实生成请求。

---

## 关键约束：12 处模板断言必须同步迁移

拆分 `dashboard_settings.html` 会破坏 6 个测试文件里 12 处按路径读取该模板的断言。这不是可选的收尾工作，**每个断言都必须在拆分的同一个 commit 里改到新模板路径**，否则测试立刻红。完整清单：

| 测试文件 | 断言内容 | 迁移到 |
|---|---|---|
| `test_webapp_settings.py:14` | `keyword_clean` option 出现 2 次 | `dashboard_settings_agents.html` |
| `test_ai_model_ui.py:6` （模块级 `TEMPLATE`） | 目录同步/隐私边界共 9 个字符串 + `api_key_encrypted` 不出现 | `dashboard_settings_models.html` |
| `test_ai_model_ui.py` `test_model_sync_mutations_use_csrf_fetch` | 同上模板的 csrfFetch | `dashboard_settings_models.html` |
| `test_ai_adult_admin.py:339` | `adult_*_policy` / `adultReviewBindings` / review binding 端点 | `dashboard_settings_adult.html` |
| `test_ai_adult_admin.py:358` | `disabled_guard` 在 `policy_guard` 之前 | `dashboard_settings_adult.html` |
| `test_ai_adult_frontend.py:21` | `adult_safety_policy` / `adult_fact_guard_policy` / `json` | `dashboard_settings_adult.html` |
| `test_ai_adult_frontend.py:58` | 角色确认契约 6 个字符串 + `agent.task_type !== 'adult_polish'` | `dashboard_settings_adult.html` |
| `test_frontend_library_os.py:49` | `dashboard_settings.html` 在 `library-page` 页面列表里 | 列表替换为 5 个新模板名 |
| `test_frontend_library_os.py:342` | 设置页的 library-* 约定 | `dashboard_settings_sync.html` |
| `test_recommendation_scheduling.py:181` | `auto_sync_recommendation_run_*` 三字段 | `dashboard_settings_sync.html` |

Task 3 会逐个处理。在 Task 3 完成前不要删除 `dashboard_settings.html`。

---

## 文件结构

**后端**

| 文件 | 职责 |
|---|---|
| `src/pixiv_novel_sync/web/managers.py` | `SETTINGS_SECTIONS` 字段白名单常量；`save_sync_settings(payload, section=None)` |
| `src/pixiv_novel_sync/webapp.py` | 5 个页面路由；`PUT /api/dashboard/settings/<section>`；修 `task_map` 缺键 |
| `src/pixiv_novel_sync/ai_web.py` | `GET /api/dashboard/ai/agents/<id>/candidates` |
| `src/pixiv_novel_sync/ai/services/admin.py` | `preview_agent_candidates(agent_id)` |

**前端**

| 文件 | 职责 |
|---|---|
| `templates/base.html` | 新增 `window.streamSSE`；已有 `csrfFetch` / `errorText` 保持 |
| `templates/dashboard_settings_sync.html` | 同步开关、分组限速、调度表（优先级/耗时/预算）、手动触发 |
| `templates/dashboard_settings_models.html` | Provider CRUD、模型目录、模型池 |
| `templates/dashboard_settings_agents.html` | 普通 Agent + 候选链预览 |
| `templates/dashboard_settings_adult.html` | 成人润色 Agent、review binding、项目角色（下拉选择） |
| `templates/dashboard_settings_system.html` | 图片缓存、救援 API、导出、待删除保留期 |
| `templates/dashboard_ai.html` / `dashboard_wizard.html` / `dashboard_ai_reader.html` | 删除自建 csrfFetch/SSE，改用公共层 |

**文档**：`docs/frontend-pages.md`、`docs/frontend-api-contract.md`。

---

### Task 1: 后端分区保存

`save_sync_settings` 当前读整份 payload、逐字段 `payload.get(k, 既有值)`、重写整个 `sync:` 块。加 `section` 参数后，只有该分区声明的字段允许从 payload 取值，其余字段强制用 YAML 里的既有值——这样「同步页保存」不会把 AI 页没加载的字段写成默认值。

**Files:**
- Modify: `src/pixiv_novel_sync/web/managers.py`（`save_sync_settings`，919 行起；新增 `SETTINGS_SECTIONS` 常量）
- Test: `tests/test_settings_sections.py`（新建）

**Interfaces:**
- Consumes: 无（本阶段第一个任务）
- Produces: `SETTINGS_SECTIONS: dict[str, frozenset[str]]`，键为 `"sync"` / `"system"`；`SettingsManager.save_sync_settings(payload: dict, section: str | None = None) -> dict`。`section` 为无效值时抛 `ValueError`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_settings_sections.py
from __future__ import annotations

import yaml

from pixiv_novel_sync.webapp import SettingsManager


def _write_config(tmp_path, **sync_values):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"sync": sync_values}, allow_unicode=True), encoding="utf-8"
    )
    return config_path


def test_section_save_only_touches_its_own_fields(tmp_path):
    """分区保存不能把其它分区的字段写回默认值。

    回归意图：设置页拆分后，同步页的表单里没有 pending_deletion_grace_period_days，
    若保存时仍走全量路径，payload.get(k, 默认值) 会把它从 30 覆盖成默认值。
    """
    config_path = _write_config(
        tmp_path,
        max_items_per_run=20,
        pending_deletion_grace_period_days=30,
    )

    saved = SettingsManager(str(config_path)).save_sync_settings(
        {"max_items_per_run": 50}, section="sync"
    )

    assert saved["max_items_per_run"] == 50
    assert saved["pending_deletion_grace_period_days"] == 30
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["sync"]["pending_deletion_grace_period_days"] == 30


def test_section_save_ignores_fields_outside_the_section(tmp_path):
    """payload 里夹带别区字段时必须被忽略，而不是静默写入。"""
    config_path = _write_config(
        tmp_path,
        max_items_per_run=20,
        pending_deletion_grace_period_days=30,
    )

    SettingsManager(str(config_path)).save_sync_settings(
        {"max_items_per_run": 50, "pending_deletion_grace_period_days": 999},
        section="sync",
    )

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["sync"]["pending_deletion_grace_period_days"] == 30


def test_full_save_without_section_keeps_legacy_behaviour(tmp_path):
    """section=None 必须保持原有全量行为（CLI 与既有测试依赖它）。"""
    config_path = _write_config(tmp_path, max_items_per_run=20)

    saved = SettingsManager(str(config_path)).save_sync_settings(
        {"max_items_per_run": 50, "pending_deletion_grace_period_days": 999}
    )

    assert saved["max_items_per_run"] == 50
    assert saved["pending_deletion_grace_period_days"] == 999


def test_invalid_section_is_rejected(tmp_path):
    config_path = _write_config(tmp_path, max_items_per_run=20)

    try:
        SettingsManager(str(config_path)).save_sync_settings({}, section="nope")
    except ValueError as exc:
        assert "分区" in str(exc)
    else:
        raise AssertionError("无效 section 必须抛 ValueError")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_settings_sections.py -v`
Expected: FAIL — `save_sync_settings() got an unexpected keyword argument 'section'`

- [ ] **Step 3: 实现**

在 `web/managers.py` 的 `SCHEDULER_TASK_CONFIGS` 之后（约 100 行附近，`TASK_LABELS` 之前）加常量：

```python
# 设置页按分区独立保存：每个分区声明自己拥有哪些字段。
# 分区保存时只有本区字段允许从 payload 取值，其余字段强制沿用 YAML 既有值——
# 否则「同步页保存」会把 AI 页/系统页没加载的字段写成默认值（save_sync_settings
# 的 payload.get(k, 默认值) 语义在部分 payload 下就是静默覆盖）。
SETTINGS_SECTIONS: dict[str, frozenset[str]] = {
    "sync": frozenset({
        "enabled", "initial_manual_only", "download_assets", "write_markdown",
        "write_raw_text", "bookmark_restricts", "max_items_per_run",
        "max_pages_per_run", "bookmark_max_pages_per_run",
        "series_max_pages_per_run", "following_max_novels_per_author",
        "delay_seconds_between_items", "delay_seconds_between_pages",
        "delay_seconds_between_series", "delay_seconds_between_chapters",
        "delay_seconds_between_skips",
        "sync_bookmarks", "sync_following_users", "sync_following_novels",
        "sync_subscribed_series", "series_sync_limit",
        "auto_sync_timezone",
        "auto_sync_bookmarks_enabled", "auto_sync_bookmarks_interval_hours",
        "auto_sync_bookmarks_cron",
        "auto_sync_following_list_enabled", "auto_sync_following_list_interval_hours",
        "auto_sync_following_list_cron",
        "auto_sync_following_novels_enabled",
        "auto_sync_following_novels_interval_hours",
        "auto_sync_following_novels_cron", "auto_sync_following_novels_users_limit",
        "auto_sync_user_status_enabled", "auto_sync_user_status_interval_hours",
        "auto_sync_user_status_cron",
        "auto_sync_novel_status_enabled", "auto_sync_novel_status_interval_hours",
        "auto_sync_novel_status_cron",
        "auto_sync_series_status_enabled", "auto_sync_series_status_interval_hours",
        "auto_sync_series_status_cron",
        "auto_sync_subscribed_series_enabled",
        "auto_sync_subscribed_series_interval_hours",
        "auto_sync_subscribed_series_cron",
        "auto_sync_user_backup_enabled", "auto_sync_user_backup_interval_hours",
        "auto_sync_user_backup_cron",
        "auto_sync_pending_detection_enabled",
        "auto_sync_pending_detection_interval_hours",
        "auto_sync_pending_detection_cron",
        "auto_sync_preference_analyze_enabled",
        "auto_sync_preference_analyze_interval_hours",
        "auto_sync_preference_analyze_cron", "preference_analyze_batch_size",
        "auto_sync_recommendation_run_enabled",
        "auto_sync_recommendation_run_interval_hours",
        "auto_sync_recommendation_run_cron",
    }),
    "system": frozenset({
        "pending_deletion_grace_period_days",
        "pending_deletion_cleanup_confirmed_days",
    }),
}
```

改 `save_sync_settings` 签名与开头（919-927 行）。原方法体一行不改——只在最前面把 `payload` 换成过滤后的副本：

```python
    def save_sync_settings(
        self, payload: dict[str, Any], section: str | None = None
    ) -> dict[str, Any]:
        if not self.config_path:
            raise ValueError("缺少 config_path，无法保存设置")

        if section is not None:
            allowed = SETTINGS_SECTIONS.get(section)
            if allowed is None:
                raise ValueError(f"未知的设置分区: {section!r}")
            # 只保留本区字段。后面整段逻辑都是 payload.get(k, 既有值)，
            # 所以被过滤掉的键会自动沿用 YAML 里的旧值。
            payload = {k: v for k, v in payload.items() if k in allowed}

        config_path = Path(self.config_path)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_settings_sections.py -v`
Expected: PASS（4 项）

- [ ] **Step 5: 确认未破坏既有保存**

Run: `python -m pytest tests/test_webapp_settings.py tests/test_settings_save_csrf.py tests/test_cron_validation.py -q`
Expected: 全部 PASS（`section=None` 走原路径）

- [ ] **Step 6: 提交**

```bash
git add src/pixiv_novel_sync/web/managers.py tests/test_settings_sections.py
git commit -m "feat: 设置保存支持按分区过滤字段"
```

---

### Task 2: 分区保存端点与页面路由

**Files:**
- Modify: `src/pixiv_novel_sync/webapp.py`（`/dashboard/settings` 路由 873 行；`POST /api/dashboard/settings` 1323 行；`task_map` 1367 行）
- Test: `tests/test_settings_sections.py`（追加）

**Interfaces:**
- Consumes: `SETTINGS_SECTIONS`、`save_sync_settings(payload, section)`（Task 1）
- Produces: `PUT /api/dashboard/settings/<section>` → `{"ok": True, "message": ..., "sync": {...}}`；页面路由 `/dashboard/settings/{sync,models,agents,adult,system}`；`/dashboard/settings` 302 到 `/dashboard/settings/sync`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_settings_sections.py`：

```python
def test_section_endpoint_saves_only_its_section(tmp_path, monkeypatch):
    """PUT /api/dashboard/settings/sync 不能动 system 区字段。"""
    from pixiv_novel_sync.webapp import create_app

    config_path = _write_config(
        tmp_path, max_items_per_run=20, pending_deletion_grace_period_days=30
    )
    monkeypatch.setenv("PIXIV_DB_PATH", str(tmp_path / "state" / "s.db"))
    app = create_app(config_path=str(config_path))
    client = app.test_client()

    token = client.get("/api/csrf-token").get_json()["csrf_token"]
    res = client.put(
        "/api/dashboard/settings/sync",
        json={"max_items_per_run": 50, "pending_deletion_grace_period_days": 999},
        headers={"X-CSRF-Token": token},
    )

    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["sync"]["max_items_per_run"] == 50
    assert body["sync"]["pending_deletion_grace_period_days"] == 30


def test_unknown_section_endpoint_returns_400(tmp_path, monkeypatch):
    from pixiv_novel_sync.webapp import create_app

    config_path = _write_config(tmp_path, max_items_per_run=20)
    monkeypatch.setenv("PIXIV_DB_PATH", str(tmp_path / "state" / "s.db"))
    app = create_app(config_path=str(config_path))
    client = app.test_client()

    token = client.get("/api/csrf-token").get_json()["csrf_token"]
    res = client.put(
        "/api/dashboard/settings/nope", json={}, headers={"X-CSRF-Token": token}
    )

    assert res.status_code == 400


def test_settings_pages_all_render(tmp_path, monkeypatch):
    from pixiv_novel_sync.webapp import create_app

    config_path = _write_config(tmp_path, max_items_per_run=20)
    monkeypatch.setenv("PIXIV_DB_PATH", str(tmp_path / "state" / "s.db"))
    app = create_app(config_path=str(config_path))
    client = app.test_client()

    for section in ("sync", "models", "agents", "adult", "system"):
        res = client.get(f"/dashboard/settings/{section}")
        assert res.status_code == 200, section

    legacy = client.get("/dashboard/settings")
    assert legacy.status_code == 302
    assert legacy.headers["Location"].endswith("/dashboard/settings/sync")


def test_manual_subscribed_series_trigger_is_reachable(tmp_path, monkeypatch):
    """回归：手动触发「同步追更系列」曾因 task_map 缺键必然 400。"""
    from pixiv_novel_sync.webapp import create_app

    config_path = _write_config(tmp_path, max_items_per_run=20)
    monkeypatch.setenv("PIXIV_DB_PATH", str(tmp_path / "state" / "s.db"))
    app = create_app(config_path=str(config_path))
    client = app.test_client()

    token = client.get("/api/csrf-token").get_json()["csrf_token"]
    res = client.post(
        "/api/dashboard/sync/subscribed_series", headers={"X-CSRF-Token": token}
    )

    # 未配置 Pixiv 凭据时提交可能失败，但绝不能是「不支持的任务类型」
    assert "不支持的任务类型" not in (res.get_json() or {}).get("error", "")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_settings_sections.py -v -k "endpoint or pages or manual"`
Expected: FAIL — 404（路由不存在）

- [ ] **Step 3: 实现**

`webapp.py` 把 873 行的单个设置页路由替换为：

```python
    _SETTINGS_PAGES = ("sync", "models", "agents", "adult", "system")

    @app.get("/dashboard/settings")
    def dashboard_settings_page():
        # 旧书签与旧 hash 链接一律落到同步页
        return redirect("/dashboard/settings/sync")

    @app.get("/dashboard/settings/<section>")
    def dashboard_settings_section_page(section: str):
        if section not in _SETTINGS_PAGES:
            abort(404)
        return render_template(f"dashboard_settings_{section}.html")
```

确认文件顶部已从 flask 导入 `abort` 与 `redirect`；缺 `abort` 就加进 import 列表。

在 `POST /api/dashboard/settings`（1323 行）之后加分区端点：

```python
    @app.put("/api/dashboard/settings/<section>")
    def dashboard_settings_save_section(section: str):
        payload = request.get_json(silent=True) or {}
        try:
            saved = settings_manager.save_sync_settings(payload, section=section)
        except ValueError as exc:
            return _api_error("保存设置失败", detail=str(exc))
        except Exception as exc:
            return _api_error("保存设置失败", detail=str(exc))
        return jsonify({"ok": True, "message": "设置已保存", "sync": saved})
```

修 `task_map`（1367 行）——加 `subscribed_series` 及四个缺失的手动入口：

```python
        task_map = {
            "bookmark": ("bookmark", "同步收藏"),
            "following_users": ("following_users", "同步关注用户"),
            "following_novels": ("following_novels", "同步关注小说"),
            # 前端按钮一直发下划线形式，而真实路由是连字符的
            # /api/dashboard/sync/subscribed-series，缺这个键就必然 400。
            "subscribed_series": ("subscribed_series", "同步追更系列"),
            "user_status": ("user_status", "检查用户状态"),
            "novel_status": ("novel_status", "检查小说状态"),
            "series_status": ("series_status", "检查系列状态"),
            "user_backup": ("user_backup", "全量备份关注用户小说"),
            "pending_deletion_detection": (
                "pending_deletion_detection", "检测取消收藏/追更",
            ),
            "preference_analyze": ("preference_analyze", "增量分析本地偏好"),
            "recommendation_run": ("recommendation_run", "生成推荐"),
        }
```

- [ ] **Step 4: 建 5 个占位模板**

只为让路由测试通过；内容在 Task 3 填充。每个文件写：

```html
{% extends 'base.html' %}
{% block content %}
<div class="library-page">
  <header class="library-page-header">
    <h1>设置 · <!-- 分区名 --></h1>
  </header>
</div>
{% endblock %}
```

先确认 `base.html` 的实际 block 名与继承约定：`grep -n "block" src/pixiv_novel_sync/templates/dashboard_logs.html | head -5`，照抄该结构。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_settings_sections.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add src/pixiv_novel_sync/webapp.py src/pixiv_novel_sync/templates/dashboard_settings_*.html tests/test_settings_sections.py
git commit -m "feat: 设置页拆分路由与分区保存端点"
```

---

### Task 3: 迁移设置页内容与 12 处测试断言

这是本阶段最大的一步，也是唯一必须一次做完的一步——模板内容和测试断言不能分开提交，否则中间状态测试必红。

**Files:**
- Modify: 5 个 `dashboard_settings_*.html`（填充内容）
- Delete: `src/pixiv_novel_sync/templates/dashboard_settings.html`
- Modify: `tests/test_webapp_settings.py:14`、`tests/test_ai_model_ui.py:6`、`tests/test_ai_adult_admin.py:339,358`、`tests/test_ai_adult_frontend.py:21,58`、`tests/test_frontend_library_os.py:49,342`、`tests/test_recommendation_scheduling.py:181`

**Interfaces:**
- Consumes: Task 2 的 5 个路由与分区端点
- Produces: 5 个完整模板。同步页保存调 `PUT /api/dashboard/settings/sync`，系统页调 `PUT /api/dashboard/settings/system`；AI 三页沿用现有 `ai_web.py` 端点。

- [ ] **Step 1: 按分区搬运 markup**

从 `dashboard_settings.html` 按原分区行号搬运，`v-show="activeTab === 'x'"` 一律删除（每页只剩自己那一区）：

| 原分区 | 原行号 | 目标模板 |
|---|---|---|
| `#basic` 基础设置 | 40–116 | `dashboard_settings_sync.html` |
| `#limits` 限速与分页 | 119–195 | `dashboard_settings_sync.html` |
| `#scheduler` 定时同步 | 198–283 | `dashboard_settings_sync.html` |
| `#manual` 手动触发 | 286–308 | `dashboard_settings_sync.html` |
| `#cache` 图片缓存 | 311–333 | `dashboard_settings_system.html` |
| `#rescue-api` 救援 API | 336–372 + 模态 759–774 | `dashboard_settings_system.html` |
| `#ai-api` Provider 与目录 | 375–476 | `dashboard_settings_models.html` |
| `#ai-model-pools` 模型池 | 479–553 | `dashboard_settings_models.html` |
| `#ai-agents` 普通 Agent | 556–657 | `dashboard_settings_agents.html` |
| 成人润色区块 | 658–755 | `dashboard_settings_adult.html` |

每页只搬自己需要的 Vue 状态与方法，不要整段复制 1030 行脚本。

- [ ] **Step 2: 限速参数按来源分组**

在 `dashboard_settings_sync.html` 里，把原来扁平的 5 个延迟字段按 spec §5.4 分成四组展示（收藏 / 关注作者 / 系列 / 巡检），**数值与字段名不变**——本阶段只改布局，不改默认值。同时新增 Task（阶段一）加的两个字段控件：

```html
<div class="grid grid-cols-1 md:grid-cols-2 gap-4">
  <label class="block">
    <span class="text-sm font-medium text-gray-700">单作者单轮最多同步篇数</span>
    <input type="number" min="1" v-model.number="settings.following_max_novels_per_author"
           class="mt-1 w-full rounded-lg border-gray-200">
    <span class="text-xs text-pixiv-gray">留空跟随全局上限。避免单个高产作者吃掉整轮配额。</span>
  </label>
  <label class="block">
    <span class="text-sm font-medium text-gray-700">系列章节单轮翻页上限</span>
    <input type="number" min="1" v-model.number="settings.series_max_pages_per_run"
           class="mt-1 w-full rounded-lg border-gray-200">
    <span class="text-xs text-pixiv-gray">留空跟随全局上限。太小会导致长系列永远补不齐章节。</span>
  </label>
</div>
```

- [ ] **Step 3: 调度表加耗时与预算列**

沿用现有 `schedulerTaskMeta(task)` / `formatNextRun`（原 967–986 行）读 `/api/dashboard/auto-sync/status`，新增两列。优先级/可让位列已存在（`c5a7b32` 加的），保留：

```html
<td class="px-4 py-3 text-xs text-pixiv-gray">{[ '' ]}
  <span v-if="taskBudget(task).lastDuration">{{ taskBudget(task).lastDuration }}</span>
  <span v-else class="text-pixiv-light">—</span>
</td>
<td class="px-4 py-3 text-xs text-pixiv-gray">
  <span v-if="taskBudget(task).dailySeconds">{{ taskBudget(task).dailyLabel }}</span>
  <span v-else class="text-pixiv-light">—</span>
</td>
```

`taskBudget` 从 `/api/dashboard/logs?category=sync` 聚合每任务最近一轮 `duration_seconds` 与按 cron 频率折算的每日占用。cron 字段加校验与「下次 5 次运行时间」预览，调用 Task 4 的预览端点。

- [ ] **Step 4: 保存改为分区端点**

删掉那个 `section` 参数从不被读取的 `saveSettings(section)`，每页各写自己的保存函数：

```javascript
      const saveMessage = ref('');
      const saving = ref(false);
      async function saveSettings() {
        saving.value = true;
        try {
          const res = await window.csrfFetch('/api/dashboard/settings/sync', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(settings.value),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok || !data.ok) {
            saveMessage.value = window.errorText(data, res);
            return;
          }
          // 用后端返回值回填，避免界面停留在没存成功的输入上
          settings.value = data.sync;
          saveMessage.value = '设置已保存';
        } catch (err) {
          saveMessage.value = String(err);
        } finally {
          saving.value = false;
        }
      }
```

系统页同样，URL 换 `/api/dashboard/settings/system`。

- [ ] **Step 5: 成人页改项目下拉**

把手输数字项目 ID（原 680–709 行）换成下拉，数据来自 `GET /api/dashboard/ai/projects`：

```html
<select v-model.number="adultProjectId" class="rounded-lg border-gray-200">
  <option :value="null">选择项目…</option>
  <option v-for="p in projects" :key="p.id" :value="p.id">{{ p.title }}</option>
</select>
```

- [ ] **Step 6: 迁移 12 处测试断言**

按本计划开头的表格逐个改路径。示例（`test_ai_model_ui.py:6`）：

```python
TEMPLATE = Path(
    "src/pixiv_novel_sync/templates/dashboard_settings_models.html"
).read_text(encoding="utf-8")
```

`test_frontend_library_os.py:49` 的页面列表把 `"dashboard_settings.html"` 一项替换为五项：

```python
        "dashboard_settings_sync.html",
        "dashboard_settings_models.html",
        "dashboard_settings_agents.html",
        "dashboard_settings_adult.html",
        "dashboard_settings_system.html",
```

- [ ] **Step 7: 删除旧模板并跑全部受影响测试**

```bash
git rm src/pixiv_novel_sync/templates/dashboard_settings.html
python -m pytest tests/test_webapp_settings.py tests/test_ai_model_ui.py \
  tests/test_ai_adult_admin.py tests/test_ai_adult_frontend.py \
  tests/test_frontend_library_os.py tests/test_recommendation_scheduling.py \
  tests/test_settings_sections.py -q
```

Expected: 全部 PASS。任何 `FileNotFoundError: dashboard_settings.html` 说明还有断言没迁移。

- [ ] **Step 8: 提交**

```bash
git add -A src/pixiv_novel_sync/templates tests
git commit -m "refactor: 设置页拆成五个一级页面并迁移模板断言"
```

---

### Task 4: cron 预览端点

调度表的 cron 字段要在保存前就能看到「下次 5 次运行时间」。复用后端 `settings.py:cron_to_next_run`，避免前端自己实现 cron 解析。

**Files:**
- Modify: `src/pixiv_novel_sync/webapp.py`
- Test: `tests/test_cron_validation.py`（追加）

**Interfaces:**
- Consumes: `settings.cron_to_next_run(expr, base_time, tz_name)`
- Produces: `POST /api/dashboard/settings/cron-preview`，请求 `{"cron": str, "timezone": str, "count": int}`，响应 `{"ok": True, "data": {"valid": bool, "next_runs": [ISO8601 字符串]}}`。非法表达式返回 200 且 `valid: False`（这是校验结果，不是请求错误）。

- [ ] **Step 1: 写失败测试**

```python
def test_cron_preview_returns_next_runs(tmp_path, monkeypatch):
    from pixiv_novel_sync.webapp import create_app

    config_path = tmp_path / "config.yaml"
    config_path.write_text("sync: {}\n", encoding="utf-8")
    monkeypatch.setenv("PIXIV_DB_PATH", str(tmp_path / "state" / "s.db"))
    app = create_app(config_path=str(config_path))
    client = app.test_client()

    token = client.get("/api/csrf-token").get_json()["csrf_token"]
    res = client.post(
        "/api/dashboard/settings/cron-preview",
        json={"cron": "20 0,4,8,12,16,20 * * *", "timezone": "Asia/Seoul", "count": 5},
        headers={"X-CSRF-Token": token},
    )

    assert res.status_code == 200
    data = res.get_json()["data"]
    assert data["valid"] is True
    assert len(data["next_runs"]) == 5


def test_cron_preview_reports_invalid_expression(tmp_path, monkeypatch):
    from pixiv_novel_sync.webapp import create_app

    config_path = tmp_path / "config.yaml"
    config_path.write_text("sync: {}\n", encoding="utf-8")
    monkeypatch.setenv("PIXIV_DB_PATH", str(tmp_path / "state" / "s.db"))
    app = create_app(config_path=str(config_path))
    client = app.test_client()

    token = client.get("/api/csrf-token").get_json()["csrf_token"]
    res = client.post(
        "/api/dashboard/settings/cron-preview",
        json={"cron": "99 99 * * *", "timezone": "UTC"},
        headers={"X-CSRF-Token": token},
    )

    assert res.status_code == 200
    assert res.get_json()["data"]["valid"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_cron_validation.py -v -k preview`
Expected: FAIL — 404

- [ ] **Step 3: 实现**

```python
    @app.post("/api/dashboard/settings/cron-preview")
    def dashboard_settings_cron_preview():
        from datetime import datetime, timezone as _tz

        from .settings import cron_to_next_run

        body = request.get_json(silent=True) or {}
        expr = str(body.get("cron") or "").strip()
        tz_name = str(body.get("timezone") or "UTC")
        try:
            count = max(1, min(int(body.get("count") or 5), 10))
        except (TypeError, ValueError):
            count = 5

        if not expr:
            return jsonify({"ok": True, "data": {"valid": False, "next_runs": []}})

        runs: list[str] = []
        cursor = datetime.now(_tz.utc).timestamp()
        for _ in range(count):
            nxt = cron_to_next_run(expr, cursor, tz_name)
            if nxt is None:
                # 非法表达式：这是校验结果而不是请求错误，仍返回 200
                return jsonify({"ok": True, "data": {"valid": False, "next_runs": []}})
            runs.append(datetime.fromtimestamp(float(nxt), _tz.utc).isoformat())
            cursor = float(nxt) + 1.0

        return jsonify({"ok": True, "data": {"valid": True, "next_runs": runs}})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_cron_validation.py -v`
Expected: PASS

- [ ] **Step 5: 前端接上**

`dashboard_settings_sync.html` 里 cron 输入 `@change` 调该端点，`valid: false` 时给输入框加红边并提示「cron 表达式无法解析」，`valid: true` 时在下方列出 5 个时刻。

- [ ] **Step 6: 提交**

```bash
git add src/pixiv_novel_sync/webapp.py src/pixiv_novel_sync/templates/dashboard_settings_sync.html tests/test_cron_validation.py
git commit -m "feat: cron 表达式校验与下次运行时间预览"
```

---

### Task 5: 前端公共层

`base.html` 已提供 `window.csrfFetch` / `ensureCsrfToken` / `errorText`（`fb91da3` 修 12 处 CSRF 漏洞时加的，其注释明确要求全站复用），但 4 个模板各自重写了 `csrfFetch`，SSE 解析重复 7 遍，`errorText` 无人使用。

**Files:**
- Modify: `src/pixiv_novel_sync/templates/base.html`（在 `errorText` 之后加 `streamSSE`）
- Modify: `dashboard_ai.html`（删 1019–1036 自建 csrfFetch；5 处 SSE）、`dashboard_wizard.html`（215–216；1 处 SSE）、`dashboard_ai_reader.html`（314–321；1 处 SSE）
- Test: `tests/test_frontend_shared_layer.py`（新建）

**Interfaces:**
- Consumes: 已有 `window.csrfFetch` / `window.errorText`
- Produces: `window.streamSSE(url, options, handlers)`。`handlers` 形如 `{onEvent(name, data), onDone(), onError(err)}`。内部用 `csrfFetch` + `ReadableStream`，按 `\n\n` 切帧、解析 `event:` / `data:` 行。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_frontend_shared_layer.py
from __future__ import annotations

from pathlib import Path

TEMPLATES = Path("src/pixiv_novel_sync/templates")

AI_PAGES = ("dashboard_ai.html", "dashboard_wizard.html", "dashboard_ai_reader.html")


def test_base_provides_shared_sse_helper():
    html = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "window.streamSSE" in html
    assert "window.csrfFetch" in html
    assert "window.errorText" in html


def test_ai_pages_do_not_redefine_csrf_fetch():
    """回归：4 个模板各自重写 csrfFetch，base.html 的全站版本形同虚设。"""
    for name in AI_PAGES:
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "async function csrfFetch" not in html, name
        assert "function ensureCsrfToken" not in html, name


def test_ai_pages_use_shared_error_text():
    """错误体有 error / detail 两种字段，只读 error 会把失败显示成「请求失败」。"""
    for name in AI_PAGES:
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "window.errorText" in html or "errorText(" in html, name


def test_ai_pages_do_not_hand_roll_sse_parsing():
    for name in AI_PAGES:
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "getReader()" not in html, name
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_frontend_shared_layer.py -v`
Expected: FAIL — `window.streamSSE` 不存在、`getReader()` 仍在

- [ ] **Step 3: 在 base.html 加 streamSSE**

紧跟现有 `window.errorText` 定义之后（约 228 行）：

```javascript
    // SSE 流式读取。此前 ai / wizard / reader 三页共写了 7 份几乎相同的
    // getReader + 手工切帧代码，任何一处修 bug 都修不全，统一收在这里。
    window.streamSSE = async function (url, options, handlers) {
      const h = handlers || {};
      let response;
      try {
        response = await window.csrfFetch(url, options);
      } catch (err) {
        if (h.onError) h.onError(err);
        return;
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        if (h.onError) h.onError(new Error(window.errorText(data, response)));
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          // SSE 以空行分帧；最后一段可能不完整，留在 buffer 里等下一轮
          const frames = buffer.split('\n\n');
          buffer = frames.pop() || '';
          for (const frame of frames) {
            if (!frame.trim()) continue;
            let eventName = 'message';
            const dataLines = [];
            for (const line of frame.split('\n')) {
              if (line.startsWith('event:')) eventName = line.slice(6).trim();
              else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
            }
            if (!dataLines.length) continue;
            let payload = dataLines.join('\n');
            try { payload = JSON.parse(payload); } catch (e) { /* 保持字符串 */ }
            if (h.onEvent) h.onEvent(eventName, payload);
          }
        }
        if (h.onDone) h.onDone();
      } catch (err) {
        if (h.onError) h.onError(err);
      }
    };
```

- [ ] **Step 4: 三个模板逐个切换**

一次改一个文件，每改完立刻跑 Step 5 的测试。删除自建 `csrfFetch` / `ensureCsrfToken`，把 `getReader()` 循环换成 `window.streamSSE(...)`，把 `data.error` 换成 `window.errorText(data, res)`。

`dashboard_ai.html` 有 5 处 SSE，逐处替换，每处替换后确认对应功能的 Vue 方法签名未变。

- [ ] **Step 5: 跑测试确认通过**

```bash
python -m pytest tests/test_frontend_shared_layer.py tests/test_frontend_library_os.py \
  tests/test_ai_page_routes.py tests/test_ai_adult_frontend.py \
  tests/test_settings_save_csrf.py tests/test_preference_csrf.py -q
```

Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add src/pixiv_novel_sync/templates tests/test_frontend_shared_layer.py
git commit -m "refactor: 抽出前端 SSE 公共层并统一 csrfFetch/errorText"
```

---

### Task 6: Agent 候选模型链预览

配完 Agent 看不出实际会依次调用哪些模型——`resolve_candidates` 与 `/model-pools/<id>/attempts` 都已存在但无人调用，真实候选顺序只能事后在日志页看到。

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/admin.py`（新增 `preview_agent_candidates`）
- Modify: `src/pixiv_novel_sync/ai_web.py`（新增 GET 路由）
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings_agents.html`
- Test: `tests/test_ai_agent_candidates.py`（新建）

**Interfaces:**
- Consumes: `self.model_router.resolve_candidates(agent, stage="main")`、`self._load_agent_config(db, agent_id)`；`ModelCandidate` 字段 `provider_id/provider_name/model_key/pool_id/pool_name/pool_position/fallback_depth/candidate_index/capabilities/context_window`
- Produces: `AIAdminMixin.preview_agent_candidates(agent_id: int) -> dict`，返回 `{"agent_id", "agent_name", "binding_type", "pool_name", "candidates": [...], "limits": {...}}`；`GET /api/dashboard/ai/agents/<int:agent_id>/candidates`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_ai_agent_candidates.py
from __future__ import annotations

from pathlib import Path

import pytest

from pixiv_novel_sync.ai.service import AIWritingService


@pytest.fixture
def service(tmp_path: Path) -> AIWritingService:
    return AIWritingService(tmp_path / "ai.db")


def _make_provider_and_agent(service: AIWritingService) -> int:
    provider_id = service.create_provider({
        "name": "测试 Provider",
        "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test",
        "default_model": "test-model",
    })
    return service.create_agent({
        "name": "章节续写",
        "task_type": "continue",
        "binding_type": "fixed",
        "provider_id": provider_id,
        "model": "test-model",
        "system_prompt": "写作助手",
    })


def test_preview_returns_candidate_chain_for_fixed_agent(service):
    agent_id = _make_provider_and_agent(service)

    result = service.preview_agent_candidates(agent_id)

    assert result["agent_id"] == agent_id
    assert result["binding_type"] == "fixed"
    assert len(result["candidates"]) == 1
    first = result["candidates"][0]
    assert first["model_key"] == "test-model"
    assert first["provider_name"] == "测试 Provider"
    assert first["order"] == 1
    assert result["limits"]["max_candidate_attempts"] == 16


def test_preview_never_calls_stream_generate(service, monkeypatch):
    """预览必须是纯解析：绝不能触发任何真实生成请求。"""
    agent_id = _make_provider_and_agent(service)

    def _boom(*args, **kwargs):
        raise AssertionError("预览不得发起真实生成请求")

    monkeypatch.setattr(
        "pixiv_novel_sync.ai.providers.OpenAICompatibleProvider.stream_generate",
        _boom,
        raising=False,
    )

    service.preview_agent_candidates(agent_id)


def test_preview_rejects_unknown_agent(service):
    with pytest.raises(Exception):
        service.preview_agent_candidates(999999)


def test_agents_template_renders_candidate_chain():
    html = Path(
        "src/pixiv_novel_sync/templates/dashboard_settings_agents.html"
    ).read_text(encoding="utf-8")

    assert "/candidates" in html
    assert "candidateChain" in html
```

先跑一次确认 `create_provider` / `create_agent` 的真实签名（`grep -n "def create_provider\|def create_agent" src/pixiv_novel_sync/ai/services/admin.py`），按实际参数调整测试。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_ai_agent_candidates.py -v`
Expected: FAIL — `AIWritingService` 没有 `preview_agent_candidates`

- [ ] **Step 3: 实现 service 方法**

加到 `ai/services/admin.py` 的 `list_model_pool_attempts` 附近：

```python
    def preview_agent_candidates(self, agent_id: int) -> dict[str, Any]:
        """解析并返回该 Agent 的候选模型链，不发起任何真实生成请求。

        配置界面此前完全看不出「这个 Agent 实际会依次调用哪些模型」，只能等任务
        跑完去日志页事后看。这里复用 ModelRouter 的解析路径，保证预览与真实
        执行用的是同一套候选顺序。
        """
        db = self._db()
        try:
            agent = self._load_agent_config(db, agent_id)
        finally:
            db.close()

        snapshot = self.model_router.resolve_candidates(agent, stage="main")
        candidates = [
            {
                "order": index + 1,
                "provider_id": c.provider_id,
                "provider_name": c.provider_name,
                "model_key": c.model_key,
                "pool_id": c.pool_id,
                "pool_name": c.pool_name,
                "pool_position": c.pool_position,
                "fallback_depth": c.fallback_depth,
                "capabilities": list(c.capabilities),
                "context_window": c.context_window,
            }
            for index, c in enumerate(snapshot.candidates)
        ]
        return {
            "agent_id": agent.id,
            "agent_name": agent.name,
            "binding_type": agent.binding_type,
            "pool_name": candidates[0]["pool_name"] if candidates else None,
            "candidates": candidates,
            "limits": {
                "max_candidate_attempts": 16,
                "max_network_requests": 32,
                "job_deadline_minutes": 30,
            },
        }
```

`limits` 里的数字若能从 `ai/model_pools.py` 的现有常量导入就改成导入，避免硬编码两份。

- [ ] **Step 4: 加路由**

`ai_web.py`，紧邻 `list_ai_model_pool_attempts`：

```python
    @app.get("/api/dashboard/ai/agents/<int:agent_id>/candidates")
    def preview_ai_agent_candidates(agent_id: int):
        try:
            return ok(service.preview_agent_candidates(agent_id))
        except Exception as exc:
            return fail(exc)
```

- [ ] **Step 5: 前端渲染**

`dashboard_settings_agents.html` 里选中 Agent 后加载并展示：

```javascript
      const candidateChain = ref(null);
      async function loadCandidateChain(agentId) {
        candidateChain.value = null;
        if (!agentId) return;
        try {
          const res = await fetch(`/api/dashboard/ai/agents/${agentId}/candidates`);
          const data = await res.json().catch(() => ({}));
          if (!res.ok || !data.ok) {
            candidateChain.value = { error: window.errorText(data, res) };
            return;
          }
          candidateChain.value = data.data;
        } catch (err) {
          candidateChain.value = { error: String(err) };
        }
      }
```

模板部分列出 `① provider / model（来源）`，并显示 `limits`。同时把已存在但无人调用的 `GET /api/dashboard/ai/model-pools/<id>/attempts` 接到模型池编辑界面（`dashboard_settings_models.html`），展示最近尝试记录。

注意生产实测 0 个模型池、16 个 Agent 全是 fixed 绑定，所以真实环境下这条链目前只有一个元素。池相关 UI 靠测试里构造的池数据验证。

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_ai_agent_candidates.py tests/test_ai_model_ui.py tests/test_ai_service_facade.py -q`
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add src/pixiv_novel_sync/ai/services/admin.py src/pixiv_novel_sync/ai_web.py src/pixiv_novel_sync/templates/dashboard_settings_agents.html src/pixiv_novel_sync/templates/dashboard_settings_models.html tests/test_ai_agent_candidates.py
git commit -m "feat: Agent 候选模型链预览与模型池尝试记录"
```

---

### Task 7: 侧栏导航、文档与全量验证

**Files:**
- Modify: `src/pixiv_novel_sync/templates/vue_components.html`（`NAV_ITEMS`，21–32 行）
- Modify: `docs/frontend-pages.md`、`docs/frontend-api-contract.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: 前六个任务的全部路由与端点
- Produces: 无新接口

- [ ] **Step 1: 更新侧栏**

`NAV_ITEMS` 里 `{ path: '/dashboard/settings', label: '设置', icon: 'cog' }` 改为指向 `/dashboard/settings/sync`。`isActive` 用的是 `currentPath.startsWith(item.path)`，所以把 `path` 设为 `/dashboard/settings` 前缀能让五个子页都高亮——但链接要落到 `/dashboard/settings/sync`。因此拆成两个字段：

```javascript
    { path: '/dashboard/settings', href: '/dashboard/settings/sync', label: '设置', icon: 'cog' },
```

并把模板里的 `:href="item.path"` 改成 `:href="item.href || item.path"`。

- [ ] **Step 2: 更新文档**

`docs/frontend-pages.md`：把设置页一行替换为五行新路由与模板名。`docs/frontend-api-contract.md`：新增 `PUT /api/dashboard/settings/<section>`、`POST /api/dashboard/settings/cron-preview`、`GET /api/dashboard/ai/agents/<id>/candidates` 三个端点的请求/响应形状；补上 `task_map` 现已支持的 11 个手动触发 task_type。

`CLAUDE.md` 的「Web app assembly and auth」段落补一句：设置页已拆为五个一级页面，保存走分区端点；前端公共层在 `base.html`（`csrfFetch` / `errorText` / `streamSSE`），新页面不得自建副本。

- [ ] **Step 3: 全量测试**

Run: `python -m pytest -q`
Expected: 全绿。基线是本轮开工前的 1308 passed / 4 skipped，加上本阶段新增用例后总数应上升，且 **skipped 不得增加**。

- [ ] **Step 4: 静态检查**

Run: `python -m compileall -q src`
Expected: 无输出

- [ ] **Step 5: 手动验证五个页面**

```bash
pixiv-novel-sync web-token-ui
```

逐个访问 `/dashboard/settings/{sync,models,agents,adult,system}`，确认：页面能渲染、侧栏「设置」高亮、各页保存只影响本页字段（保存同步页后回系统页确认保留期未变）、cron 预览能出 5 个时刻、Agent 页能看到候选链、手动触发「同步追更系列」不再 400。

`/dashboard/settings` 应 302 到 `/dashboard/settings/sync`。

- [ ] **Step 6: 提交**

```bash
git add src/pixiv_novel_sync/templates/vue_components.html docs CLAUDE.md
git commit -m "docs: 更新设置页路由与前端公共层约定"
```

---

## 完成标准

- [ ] `/dashboard/settings` 302 到 `/dashboard/settings/sync`；五个子页均可渲染且侧栏高亮正确
- [ ] 分区保存只写本区字段——保存同步页不会改动 `pending_deletion_*`
- [ ] `save_sync_settings(payload)` 不带 `section` 时行为与改造前完全一致
- [ ] 12 处模板断言全部迁移，`dashboard_settings.html` 已删除且无测试引用它
- [ ] 三个 AI 模板不再自建 `csrfFetch`，不再出现 `getReader()`
- [ ] Agent 候选链预览可用，且经测试证明不触发 `stream_generate`
- [ ] 手动触发覆盖 11 个 task_type，「同步追更系列」不再 400
- [ ] `python -m pytest` 全绿，skipped 数未增加
- [ ] `python -m compileall -q src` 无输出

## 不在本阶段范围

- `/dashboard/ai` 自身的三层嵌套 tab 与 pipeline 弹窗重排（体量足以单开一份设计）
- 限速参数具体数值调整（本阶段只改分组布局，数值等阶段一上线后一周的新基线）
- 废弃旧的 `POST /api/dashboard/settings` 全量端点（与分区端点并存，前端切换稳定后再议）

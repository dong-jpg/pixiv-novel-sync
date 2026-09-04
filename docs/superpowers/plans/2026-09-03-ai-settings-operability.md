# AI 设置页可操作性重做 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AI 设置三页说出「现在到底在用什么、坏没坏」，并把配置 Provider 与批量改绑从十几次点击压到一次。

**Architecture:** 三段各自可独立上线。阶段一是一个**零网络**的只读健康投影（静态体检复用运行时校验器 + 聚合已有的 attempts/sync 战绩），落在三页共享的横幅上；阶段二把「探测（预览、不落库）」与「同步（落库）」分成两条职责清晰的路径；阶段三把 Agent 批量改绑做成单事务端点。无数据库 schema 变更。

**Tech Stack:** Python 3.11 / Flask（`ai_web.py` 注册函数，非 blueprint）/ SQLite（`storage/ai/*` mixin）/ Vue 3 CDN + Jinja（定界符 `{[ ]}`）/ pytest。

**Spec:** `docs/superpowers/specs/2026-09-03-ai-settings-operability-design.md`

## Global Constraints

- 变更类前端请求一律用 `window.csrfFetch`，错误文案一律用 `window.errorText`；不得自建 `ensureCsrfToken` 或手拼 `X-CSRF-Token`（`tests/test_ai_model_ui.py` 有断言）。
- Jinja 变量定界符是 `{[ ]}`，`{{ }}` 归 Vue。
- 模块首行 `from __future__ import annotations`；dataclass 用 `slots=True`。
- 注释与用户可见文案用中文。
- Commit subject 用 `type: subject`（Conventional Commits）。
- 静态体检**必须**调用 `ai/providers.py:validate_base_url(url, resolve=False)`，不得另写一份 scheme 规则（spec §3.2）。
- 落库模型目录**只有** `ai/model_sync.py` 一个写入方；探测端点不写任何库表（spec §4.2）。
- 借用已保存的 API Key 时，请求里的 `base_url` 必须与库里逐字相同，否则拒绝（spec §4.3）。
- 批量改绑只动 `binding_type` / `provider_id` / `model` / `model_pool_id` / `enabled`，不碰 `system_prompt`、采样参数、`required_capabilities`（spec §5）。
- `adult_polish` 系列 Agent 不参与任何批量操作，混入即整体拒绝（spec §5.2）。
- 运行单个测试：`pytest tests/test_x.py::test_y -v`；全量：`pytest -q`（约 5 分钟，基线 1446 passed / 4 skipped）。
- 静态检查只有 `python -m compileall -q src`，没有 black/flake8/mypy。

## File Structure

| 文件 | 职责 | 任务 |
|---|---|---|
| `src/pixiv_novel_sync/storage/ai/core.py` | 新增两个只读聚合：`get_provider_attempt_health` / `get_ai_job_failure_summary` | T1 |
| `src/pixiv_novel_sync/ai/services/admin.py` | 新增 `provider_config_lint` / `ai_health` / `probe_provider_models` / `update_agent_bindings`；改 `test_provider` | T2 T3 T6 T7 T9 |
| `src/pixiv_novel_sync/ai_web.py` | 三个新路由 | T3 T7 T9 |
| `src/pixiv_novel_sync/templates/dashboard_ai_health_band.html` | 新建共享横幅 partial | T4 |
| `src/pixiv_novel_sync/templates/dashboard_settings_models.html` | 卡片健康、表单收敛、探测勾选、`enabled` 开关 | T5 T8 |
| `src/pixiv_novel_sync/templates/dashboard_settings_agents.html` | 行内继承健康点、多选/搜索/批量 | T5 T10 |
| `src/pixiv_novel_sync/templates/dashboard_settings_adult.html` | 仅 include 横幅 | T4 |
| `tests/test_ai_health.py` | 新建：聚合、体检、零网络、端点 | T1 T2 T3 |
| `tests/test_ai_provider_probe.py` | 新建：探测语义、借 Key 约束、不落库 | T7 |
| `tests/test_ai_agent_batch_bindings.py` | 新建：单事务、版本递增、成人拒绝 | T9 |
| `tests/test_ai_model_ui.py` | 扩充模板断言 | T4 T5 T8 T10 |

**阶段边界（也是上线边界）：** T1–T5 = 阶段一；T6–T8 = 阶段二；T9–T10 = 阶段三。

---

### Task 1: 两个只读聚合（attempts 战绩 + AI 任务失败摘要）

**Files:**
- Modify: `src/pixiv_novel_sync/storage/ai/core.py`（紧跟在 `list_ai_job_model_attempts` 之后，约 :440）
- Test: `tests/test_ai_health.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces:
  - `Database.get_provider_attempt_health(days: int = 7) -> dict[int, dict[str, Any]]`，键是 `provider_id`，值含 `attempts` `failures` `last_status` `last_error_scope` `last_error_category` `last_error_message` `last_at`
  - `Database.get_ai_job_failure_summary(days: int = 7) -> list[dict[str, Any]]`，每项含 `task_type` `failures` `last_at`，按 `failures` 降序

- [ ] **Step 1: 写失败测试**

`tests/test_ai_health.py`：

```python
from __future__ import annotations

from pathlib import Path

import pytest

from pixiv_novel_sync.storage_db import Database


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "health.db")
    database.init_schema()
    yield database
    database.close()


def _seed_attempt(db: Database, job_id: str, provider_id: int, status: str, **kw) -> None:
    """直接插 attempts 行：allocate/finish 那套 CAS 是给真实路由用的，这里只需要行。"""
    db.conn.execute(
        """INSERT INTO ai_job_model_attempts
           (job_id, attempt_index, provider_id, model_key, stage, status,
            error_scope, error_category, error_message, started_at, finished_at)
           VALUES (?, ?, ?, ?, 'main', ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (
            job_id, kw.get("attempt_index", 0), provider_id, kw.get("model_key", "m1"),
            status, kw.get("error_scope"), kw.get("error_category"), kw.get("error_message"),
        ),
    )
    db.conn.commit()


def test_provider_attempt_health_aggregates_per_provider(db: Database) -> None:
    _seed_attempt(db, "j1", 3, "failed", error_scope="provider",
                  error_category="configuration", error_message="base_url 必须使用 https（本机回环地址除外）")
    _seed_attempt(db, "j2", 3, "failed", error_scope="provider", error_category="configuration")
    _seed_attempt(db, "j3", 4, "succeeded")

    health = db.get_provider_attempt_health(days=7)

    assert health[3]["attempts"] == 2
    assert health[3]["failures"] == 2
    assert health[3]["last_error_category"] == "configuration"
    assert health[4]["failures"] == 0
```

再追加一条任务级摘要的测试：

```python
def test_ai_job_failure_summary_groups_by_task_type(db: Database) -> None:
    for i in range(3):
        db.conn.execute(
            """INSERT INTO ai_jobs (job_id, task_type, status, created_at, finished_at)
               VALUES (?, 'keyword_clean', 'failed', datetime('now'), datetime('now'))""",
            (f"k{i}",),
        )
    db.conn.execute(
        """INSERT INTO ai_jobs (job_id, task_type, status, created_at, finished_at)
           VALUES ('ok1', 'keyword_clean', 'succeeded', datetime('now'), datetime('now'))"""
    )
    db.conn.commit()

    summary = db.get_ai_job_failure_summary(days=7)

    assert summary == [
        {"task_type": "keyword_clean", "failures": 3, "last_at": summary[0]["last_at"]}
    ]
    assert summary[0]["last_at"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_ai_health.py -v`
Expected: FAIL，`AttributeError: 'Database' object has no attribute 'get_provider_attempt_health'`

- [ ] **Step 3: 写实现**

在 `src/pixiv_novel_sync/storage/ai/core.py` 的 `list_ai_job_model_attempts` 之后加：

```python
    def get_provider_attempt_health(self, days: int = 7) -> dict[int, dict[str, Any]]:
        """按 provider_id 聚合最近 N 天的候选尝试战绩，供设置页健康横幅使用。

        按 attempts 聚合而不是按 ai_jobs：一个 job 可能跨多个 Provider 做故障转移，
        算在任何单一 Provider 头上都不对。

        SQL 里 MAX(...) 与裸列 status/error_* 同时出现是 SQLite 的既定行为（裸列取自
        MAX 命中的那一行），与 storage/tasks.py:get_task_duration_stats 同一手法，
        所以「最近一次」不需要第二次查询。
        """
        rows = self.conn.execute(
            """
            SELECT provider_id,
                   COUNT(*) AS attempts,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failures,
                   MAX(COALESCE(finished_at, started_at)) AS last_at,
                   status AS last_status,
                   error_scope AS last_error_scope,
                   error_category AS last_error_category,
                   error_message AS last_error_message
            FROM ai_job_model_attempts
            WHERE provider_id IS NOT NULL
              AND COALESCE(finished_at, started_at) >= datetime('now', ? || ' days')
            GROUP BY provider_id
            """,
            (f"-{int(days)}",),
        ).fetchall()
        return {
            int(row["provider_id"]): {
                "attempts": int(row["attempts"] or 0),
                "failures": int(row["failures"] or 0),
                "last_at": row["last_at"],
                "last_status": row["last_status"],
                "last_error_scope": row["last_error_scope"],
                "last_error_category": row["last_error_category"],
                "last_error_message": row["last_error_message"],
            }
            for row in rows
        }
```

紧接着加第二个：

```python
    def get_ai_job_failure_summary(self, days: int = 7) -> list[dict[str, Any]]:
        """最近 N 天失败的 AI 任务，按 task_type 归并。

        这是把「红色的 AI 任务」和「绿色的 preference_analyze」关联起来的唯一线索：
        clean_keywords 按设计优雅降级，所以主任务永远报绿，只有这里能看出它在静默失败。
        """
        rows = self.conn.execute(
            """
            SELECT task_type,
                   COUNT(*) AS failures,
                   MAX(COALESCE(finished_at, created_at)) AS last_at
            FROM ai_jobs
            WHERE status = 'failed'
              AND COALESCE(finished_at, created_at) >= datetime('now', ? || ' days')
            GROUP BY task_type
            ORDER BY failures DESC, task_type
            """,
            (f"-{int(days)}",),
        ).fetchall()
        return [
            {
                "task_type": row["task_type"],
                "failures": int(row["failures"] or 0),
                "last_at": row["last_at"],
            }
            for row in rows
        ]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_ai_health.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add tests/test_ai_health.py src/pixiv_novel_sync/storage/ai/core.py
git commit -m "feat: 新增 Provider 尝试战绩与 AI 任务失败摘要的只读聚合"
```

---

### Task 2: 静态体检（复用运行时校验器，零网络）

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/admin.py`（加在 `_require_provider_model_row` 附近，约 :405）
- Test: `tests/test_ai_health.py`

**Interfaces:**
- Consumes: 无
- Produces: `AIWritingService.provider_config_lint(provider: Mapping[str, Any], *, bound_agent_count: int = 0, routable_models: int = 0) -> list[dict[str, str]]`；每项形如 `{"level": "will_fail" | "warn", "code": str, "message": str}`；`level` 取值只有这两个

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_ai_health.py`：

```python
from pixiv_novel_sync.ai.providers import ProviderConfigError, validate_base_url
from pixiv_novel_sync.ai.service import AIWritingService


def test_lint_flags_plain_http_with_the_runtime_message() -> None:
    """体检结论必须与运行时那句报错逐字相同——另写一份规则迟早漂移。"""
    provider = {"base_url": "http://nas.example.com:3000", "has_api_key": True, "enabled": True}

    findings = AIWritingService.provider_config_lint(provider, routable_models=3)

    base_url_finding = next(f for f in findings if f["code"] == "base_url")
    assert base_url_finding["level"] == "will_fail"
    with pytest.raises(ProviderConfigError) as excinfo:
        validate_base_url("http://nas.example.com:3000", resolve=False)
    assert base_url_finding["message"] == str(excinfo.value)


def test_lint_does_not_touch_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """体检必须用 resolve=False：一旦有人改成 resolve=True，这条就会炸。"""
    import socket

    def explode(*args, **kwargs):
        raise AssertionError("静态体检不允许发起 DNS 解析")

    monkeypatch.setattr(socket, "getaddrinfo", explode)
    provider = {"base_url": "https://api.example.com/v1", "has_api_key": True, "enabled": True}

    assert AIWritingService.provider_config_lint(provider, routable_models=5) == []
```

再补两条覆盖其余判据：

```python
def test_lint_flags_missing_key_and_empty_catalog() -> None:
    findings = AIWritingService.provider_config_lint(
        {"base_url": "https://api.example.com/v1", "has_api_key": False, "enabled": True},
        routable_models=0,
    )
    codes = {f["code"]: f["level"] for f in findings}
    assert codes["api_key"] == "will_fail"
    assert codes["no_routable_model"] == "warn"


def test_lint_flags_disabled_provider_only_when_agents_bound() -> None:
    disabled = {"base_url": "https://api.example.com/v1", "has_api_key": True, "enabled": False}

    with_agents = AIWritingService.provider_config_lint(disabled, bound_agent_count=15, routable_models=3)
    without = AIWritingService.provider_config_lint(disabled, bound_agent_count=0, routable_models=3)

    assert any(f["code"] == "disabled_but_bound" for f in with_agents)
    assert not any(f["code"] == "disabled_but_bound" for f in without)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_ai_health.py -v -k lint`
Expected: FAIL，`AttributeError: type object 'AIWritingService' has no attribute 'provider_config_lint'`

- [ ] **Step 3: 写实现**

`src/pixiv_novel_sync/ai/services/admin.py` 顶部导入区补上（若已存在则跳过）：

```python
from ..providers import ProviderConfigError, validate_base_url
```

在 `_require_provider_model_row` 之后加：

```python
    @staticmethod
    def provider_config_lint(
        provider: Mapping[str, Any],
        *,
        bound_agent_count: int = 0,
        routable_models: int = 0,
    ) -> list[dict[str, str]]:
        """只看 Provider 行本身就能下的结论，零网络。

        第一条判据刻意调用运行时那个 validate_base_url（resolve=False 跳过 DNS）：
        结论由构造保证与运行时一致。另写一份 scheme 规则必然与 providers.py 漂移，
        届时横幅报「健康」而任务照样失败——比没有横幅更坏。

        base_url 允许留空（表示用适配器默认地址），此时跳过这条判据。
        """
        findings: list[dict[str, str]] = []
        base_url = provider.get("base_url")
        if base_url:
            try:
                validate_base_url(str(base_url), resolve=False)
            except ProviderConfigError as exc:
                findings.append(
                    {"level": "will_fail", "code": "base_url", "message": str(exc)}
                )
        if not provider.get("has_api_key"):
            findings.append(
                {"level": "will_fail", "code": "api_key", "message": "未保存 API Key"}
            )
        if not provider.get("enabled") and bound_agent_count:
            findings.append({
                "level": "warn",
                "code": "disabled_but_bound",
                "message": f"Provider 已停用，但有 {bound_agent_count} 个 Agent 绑在这里",
            })
        if not routable_models:
            findings.append({
                "level": "warn",
                "code": "no_routable_model",
                "message": "目录里没有可路由模型，模型池选不出成员",
            })
        return findings
```

若 `admin.py` 尚未从 `typing` 导入 `Mapping`，改用已有的 `from collections.abc import Mapping`（文件顶部若无则新增）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_ai_health.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add tests/test_ai_health.py src/pixiv_novel_sync/ai/services/admin.py
git commit -m "feat: Provider 静态体检复用运行时 base_url 校验器"
```

---

### Task 3: `GET /api/dashboard/ai/health` 端点

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/admin.py`（`provider_config_lint` 之后）
- Modify: `src/pixiv_novel_sync/ai_web.py`（加在 `list_ai_providers` 之前，约 :744）
- Test: `tests/test_ai_health.py`

**Interfaces:**
- Consumes: T1 的 `get_provider_attempt_health` / `get_ai_job_failure_summary`；T2 的 `provider_config_lint`
- Produces:
  - `AIWritingService.ai_health(*, days: int = 7) -> dict[str, Any]`
  - `GET /api/dashboard/ai/health?days=N`，响应 `{"ok": true, "data": {...}}`
  - `data` 顶层键：`window_days` `providers` `agents` `totals` `ai_job_failures`
  - `providers[]` 每项：`id` `name` `enabled` `status`（`healthy`/`warn`/`will_fail`）`findings` `bound_agent_count` `routable_models` `models_synced_at` `models_sync_error` `attempts`
  - `agents[]` 每项：`id` `name` `task_type` `status` `reason`
  - `totals`：`providers` `providers_will_fail` `routable_models` `agents` `agents_unhealthy`
  - **响应里不含 `base_url`、不含任何密钥**

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_ai_health.py`（复用 `tests/test_ai_model_ui.py` 之外的既有 app 装配方式；若仓库已有 `_ai_app` 之类的工厂，优先复用）：

```python
from pixiv_novel_sync.webapp import create_app


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "ai-health-test")
    db_path = tmp_path / "app.db"
    monkeypatch.setenv("PIXIV_DB_PATH", str(db_path))
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "storage:\n"
        f"  public_dir: {(tmp_path / 'pub').as_posix()}\n"
        f"  private_dir: {(tmp_path / 'priv').as_posix()}\n"
        f"  db_path: {db_path.as_posix()}\n"
        "sync:\n  auto_sync_enabled: false\n",
        encoding="utf-8",
    )
    app = create_app(config_path=str(config_path), env_path=str(env_path), start_scheduler=False)
    return app, db_path


def test_health_endpoint_surfaces_broken_provider_and_bound_agents(tmp_path, monkeypatch) -> None:
    """复刻 2026-09-03 生产事故：http:// 非回环 Provider + 一堆 Agent 绑在上面。"""
    app, db_path = _app(tmp_path, monkeypatch)
    db = Database(db_path)
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "deepseek-v4-pro", "provider_type": "openai_compatible",
        "base_url": "http://nas.example.com:3000", "api_key_encrypted": "cipher",
        "default_model": None, "enabled": 1,
    })
    for i in range(3):
        db.create_ai_agent({
            "name": f"写作助手{i}", "task_type": "continue", "binding_type": "fixed",
            "provider_id": provider_id, "model": "m1", "system_prompt": "x", "enabled": 1,
        })
    db.close()

    res = app.test_client().get(
        "/api/dashboard/ai/health", environ_base={"REMOTE_ADDR": "127.0.0.1"}
    )

    assert res.status_code == 200
    data = res.get_json()["data"]
    provider = next(p for p in data["providers"] if p["id"] == provider_id)
    assert provider["status"] == "will_fail"
    assert provider["bound_agent_count"] == 3
    assert data["totals"]["agents_unhealthy"] == 3
    # 健康端点不许泄露连接地址
    assert "base_url" not in provider
    assert "nas.example.com" not in res.get_data(as_text=True)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_ai_health.py -v -k health_endpoint`
Expected: FAIL，404（路由不存在）

- [ ] **Step 3: 写服务方法**

`src/pixiv_novel_sync/ai/services/admin.py`：

```python
    def ai_health(self, *, days: int = 7) -> dict[str, Any]:
        """AI 配置的只读健康投影。零网络：不发起任何 Provider 请求。"""
        db = self._db()
        try:
            providers = db.list_ai_providers()
            agents = db.list_ai_agents()
            pools = {int(p["id"]): p for p in db.list_ai_model_pools()}
            attempt_health = db.get_provider_attempt_health(days=days)
            job_failures = db.get_ai_job_failure_summary(days=days)
            routable = {
                int(p["id"]): int(db.list_ai_provider_models(int(p["id"]))["routable"])
                for p in providers
            }
        finally:
            db.close()

        bound: dict[int, int] = {}
        for agent in agents:
            provider_id = agent.get("provider_id")
            if agent.get("binding_type") == "fixed" and provider_id:
                bound[int(provider_id)] = bound.get(int(provider_id), 0) + 1

        provider_status: dict[int, str] = {}
        provider_items: list[dict[str, Any]] = []
        for row in providers:
            provider_id = int(row["id"])
            findings = self.provider_config_lint(
                row,
                bound_agent_count=bound.get(provider_id, 0),
                routable_models=routable.get(provider_id, 0),
            )
            status = (
                "will_fail" if any(f["level"] == "will_fail" for f in findings)
                else ("warn" if findings else "healthy")
            )
            provider_status[provider_id] = status
            provider_items.append({
                "id": provider_id,
                "name": row.get("name"),
                "enabled": bool(row.get("enabled")),
                "status": status,
                "findings": findings,
                "bound_agent_count": bound.get(provider_id, 0),
                "routable_models": routable.get(provider_id, 0),
                "models_synced_at": row.get("models_synced_at"),
                "models_sync_error": row.get("models_sync_error"),
                "attempts": attempt_health.get(provider_id),
            })

        agent_items = [
            self._agent_health_item(agent, provider_status, pools) for agent in agents
        ]
        unhealthy = sum(1 for item in agent_items if item["status"] == "will_fail")
        return {
            "window_days": int(days),
            "providers": provider_items,
            "agents": agent_items,
            "ai_job_failures": job_failures,
            "totals": {
                "providers": len(provider_items),
                "providers_will_fail": sum(
                    1 for item in provider_items if item["status"] == "will_fail"
                ),
                "routable_models": sum(routable.values()),
                "agents": len(agent_items),
                "agents_unhealthy": unhealthy,
            },
        }

    @staticmethod
    def _agent_health_item(
        agent: Mapping[str, Any],
        provider_status: Mapping[int, str],
        pools: Mapping[int, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Agent 的状态是**继承**来的：它自己没坏，是它绑的东西坏了。"""
        status, reason = "healthy", ""
        if agent.get("binding_type") == "pool":
            pool = pools.get(int(agent.get("model_pool_id") or 0))
            members = [m for m in (pool or {}).get("members", []) if m.get("enabled")]
            if not pool or not members:
                status, reason = "will_fail", "绑定的模型池没有启用成员"
            elif all(
                provider_status.get(int(m.get("provider_id") or 0)) == "will_fail"
                for m in members
            ):
                status, reason = "will_fail", "模型池里每个成员的 Provider 都配置必失败"
        else:
            provider_id = int(agent.get("provider_id") or 0)
            if not provider_id:
                status, reason = "will_fail", "没有绑定 Provider"
            elif provider_status.get(provider_id) == "will_fail":
                status, reason = "will_fail", "绑定的 Provider 配置必失败"
        return {
            "id": int(agent["id"]),
            "name": agent.get("name"),
            "task_type": agent.get("task_type"),
            "status": status,
            "reason": reason,
        }
```

- [ ] **Step 4: 注册路由**

`src/pixiv_novel_sync/ai_web.py`，加在 `@app.get("/api/dashboard/ai/providers")` 之前：

```python
    @app.get("/api/dashboard/ai/health")
    def get_ai_health():
        """AI 配置的只读健康投影。零网络，可随意刷新。"""
        try:
            days = max(1, min(request.args.get("days", 7, type=int) or 7, 30))
            return ok(service.ai_health(days=days))
        except Exception as exc:
            return fail(exc)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_ai_health.py -v`
Expected: PASS（7 passed）

- [ ] **Step 6: 提交**

```bash
git add tests/test_ai_health.py src/pixiv_novel_sync/ai/services/admin.py src/pixiv_novel_sync/ai_web.py
git commit -m "feat: 新增 GET /api/dashboard/ai/health 只读健康投影"
```

---

### Task 4: 共享健康横幅 partial + 三页 include

**Files:**
- Create: `src/pixiv_novel_sync/templates/dashboard_ai_health_band.html`
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings_models.html:23`（`dashboard_settings_nav.html` 之后）
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings_agents.html:24`
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings_adult.html:23`
- Test: `tests/test_ai_model_ui.py`

**Interfaces:**
- Consumes: T3 的 `GET /api/dashboard/ai/health`
- Produces: 三页 `setup()` 各自必须导出 `aiHealth`、`aiHealthLoading`、`loadAiHealth`（横幅 markup 引用这三个名字；`tests/test_ai_page_routes.py` 那套 `@event` 导出守卫是给 AI 创作页写的，设置页没有等价守卫，所以这里靠模板断言 + 浏览器验证兜住）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_ai_model_ui.py`：

```python
ADULT_TEMPLATE = Path(
    "src/pixiv_novel_sync/templates/dashboard_settings_adult.html"
).read_text(encoding="utf-8")
HEALTH_BAND = Path(
    "src/pixiv_novel_sync/templates/dashboard_ai_health_band.html"
).read_text(encoding="utf-8")


def test_health_band_is_included_on_all_three_ai_settings_pages():
    """三页都要有：横幅要出现在你正在编辑的那一页上，而不是一个要跳过去的目的地。"""
    for template in (TEMPLATE, AGENTS_TEMPLATE, ADULT_TEMPLATE):
        assert "dashboard_ai_health_band.html" in template
        # 横幅 markup 引用的名字必须在本页 setup() 里导出
        for name in ("aiHealth", "aiHealthLoading", "loadAiHealth"):
            assert name in template


def test_health_band_surfaces_silent_degradation_and_bad_bindings():
    for text in (
        "/api/dashboard/ai/health",
        "providers_will_fail",
        "agents_unhealthy",
        "ai_job_failures",
        "静默降级",
        "继承",
    ):
        assert text in HEALTH_BAND
    # 横幅是只读投影，不该出现任何变更类请求
    assert "window.csrfFetch" not in HEALTH_BAND
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_ai_model_ui.py -v -k health_band`
Expected: FAIL，`FileNotFoundError: dashboard_ai_health_band.html`

- [ ] **Step 3: 写 partial**

`src/pixiv_novel_sync/templates/dashboard_ai_health_band.html`：

```html
{# AI 配置总览。三页（models / agents / adult）共享，常驻页顶。
   数据来自 GET /api/dashboard/ai/health —— 纯 DB 投影，零网络请求，可随意刷新。
   这里只读，不做任何变更操作，所以整段不出现 window.csrfFetch。
   存在理由见 docs/superpowers/specs/2026-09-03-ai-settings-operability-design.md §2.1：
   生产曾经 AI 全线失败四天而没有任何地方说话。 #}
<section class="library-card space-y-2" data-hook="ai-health-band">
  <div class="flex items-start justify-between gap-3 flex-wrap">
    <h2 class="library-section-title mb-0">AI 配置总览</h2>
    <button @click="loadAiHealth" :disabled="aiHealthLoading"
            class="text-xs px-3 py-1.5 rounded bg-gray-100 hover:bg-gray-200 disabled:opacity-50">
      {{ aiHealthLoading ? '检查中…' : '重新检查' }}
    </button>
  </div>

  <p v-if="!aiHealth" class="text-sm text-pixiv-gray">正在读取配置健康…</p>
  <template v-else>
    <div class="text-sm text-gray-800">
      {{ aiHealth.totals.providers }} 个 Provider
      <span v-if="aiHealth.totals.providers_will_fail" class="text-red-600 font-semibold">
        · ✕ {{ aiHealth.totals.providers_will_fail }} 个配置必失败
      </span>
      · {{ aiHealth.totals.routable_models }} 个可路由模型
    </div>
    <div class="text-sm text-gray-800">
      {{ aiHealth.totals.agents }} 个 Agent
      <span v-if="aiHealth.totals.agents_unhealthy" class="text-red-600 font-semibold">
        · ⚠ {{ aiHealth.totals.agents_unhealthy }} 个绑在坏 Provider 上
      </span>
    </div>
    <div v-for="failure in aiHealth.ai_job_failures" :key="failure.task_type"
         class="text-sm text-amber-700">
      ⚠ 最近 {{ aiHealth.window_days }} 天 {{ failure.failures }} 次
      {{ failure.task_type }} 任务失败<span v-if="failure.task_type === 'keyword_clean'">，偏好关键词精炼正在静默降级</span>
    </div>

    <details v-if="aiHealth.totals.providers_will_fail || aiHealth.totals.agents_unhealthy">
      <summary class="text-xs text-pixiv-gray cursor-pointer">展开逐项</summary>
      <div class="mt-2 space-y-1 text-xs">
        <div v-for="provider in aiHealth.providers.filter(p => p.status !== 'healthy')"
             :key="'p' + provider.id">
          <span class="font-semibold text-gray-900">{{ provider.name }}</span>
          <span v-for="finding in provider.findings" :key="finding.code"
                :class="finding.level === 'will_fail' ? 'text-red-600' : 'text-amber-700'">
            · {{ finding.message }}
          </span>
        </div>
        <p class="text-pixiv-light pt-1">
          Agent 的红点是<strong>继承</strong>自它绑定的 Provider / 模型池，不是 Agent 自己坏了——去上面修 Provider，不用改 Agent。
        </p>
      </div>
    </details>
  </template>
</section>
```

- [ ] **Step 4: 三页各自 include 并补 setup 导出**

三个模板都在 `{% include 'dashboard_settings_nav.html' %}` 之后加一行：

```jinja
  {% include 'dashboard_ai_health_band.html' %}
```

三个模板的 `setup()` 里各加同一段（`api` 是各页已有的 `window.aiApi.request` 包装；`adult` 页若无则用 `window.aiApi.request` 直呼）：

```javascript
      const aiHealth = ref(null);
      const aiHealthLoading = ref(false);
      async function loadAiHealth() {
        aiHealthLoading.value = true;
        try { aiHealth.value = await window.aiApi.request('/api/dashboard/ai/health'); }
        catch (e) { aiHealth.value = null; }
        finally { aiHealthLoading.value = false; }
      }
```

`onMounted` 里追加 `loadAiHealth()`，并在 `return {}` 里补 `aiHealth, aiHealthLoading, loadAiHealth`。

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_ai_model_ui.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/pixiv_novel_sync/templates/dashboard_ai_health_band.html src/pixiv_novel_sync/templates/dashboard_settings_models.html src/pixiv_novel_sync/templates/dashboard_settings_agents.html src/pixiv_novel_sync/templates/dashboard_settings_adult.html tests/test_ai_model_ui.py
git commit -m "feat: AI 设置三页常驻配置健康横幅"
```

---

### Task 5: 卡片与列表的继承健康点

**Files:**
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings_models.html:71-90`（Provider 卡片头部）
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings_agents.html:112-118`（Agent 行）
- Test: `tests/test_ai_model_ui.py`

**Interfaces:**
- Consumes: T4 已加载的 `aiHealth`
- Produces: 两页各新增 computed `providerHealth(providerId)` / `agentHealth(agentId)`，返回 T3 响应里对应那一项或 `null`

- [ ] **Step 1: 写失败测试**

```python
def test_provider_card_and_agent_row_show_inherited_health():
    for text in ("providerHealth", "bound_agent_count", "models_sync_error"):
        assert text in TEMPLATE
    for text in ("agentHealth", "继承"):
        assert text in AGENTS_TEMPLATE
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_ai_model_ui.py -v -k inherited_health`
Expected: FAIL，`assert 'providerHealth' in TEMPLATE`

- [ ] **Step 3: models 页实现**

`setup()` 里加：

```javascript
      const providerHealth = (providerId) =>
        (aiHealth.value?.providers || []).find(item => item.id === providerId) || null;
```

Provider 卡片标题那一行（现在是 `<div class="font-semibold text-gray-900">{{ provider.name }}</div>`）改成：

```html
          <div class="flex items-center gap-2">
            <span v-if="providerHealth(provider.id)"
                  :class="providerHealth(provider.id).status === 'will_fail' ? 'text-red-600'
                          : (providerHealth(provider.id).status === 'warn' ? 'text-amber-600' : 'text-green-600')">
              {{ providerHealth(provider.id).status === 'will_fail' ? '✕' : (providerHealth(provider.id).status === 'warn' ? '⚠' : '●') }}
            </span>
            <div class="font-semibold text-gray-900">{{ provider.name }}</div>
            <span v-if="providerHealth(provider.id)?.bound_agent_count"
                  class="text-[10px] px-2 py-0.5 rounded bg-gray-100 text-gray-600">
              {{ providerHealth(provider.id).bound_agent_count }} 个 Agent 绑在这里
            </span>
          </div>
          <div v-for="finding in providerHealth(provider.id)?.findings || []" :key="finding.code"
               class="text-xs" :class="finding.level === 'will_fail' ? 'text-red-600' : 'text-amber-700'">
            {{ finding.message }}
          </div>
          <div v-if="providerHealth(provider.id)?.attempts?.failures" class="text-xs text-amber-700">
            最近 {{ aiHealth.window_days }} 天 {{ providerHealth(provider.id).attempts.failures }} /
            {{ providerHealth(provider.id).attempts.attempts }} 次候选尝试失败：
            {{ providerHealth(provider.id).attempts.last_error_message }}
          </div>
```

在 `return {}` 里补 `providerHealth`。

- [ ] **Step 4: agents 页实现**

```javascript
      const agentHealth = (agentId) =>
        (aiHealth.value?.agents || []).find(item => item.id === agentId) || null;
```

Agent 行的 `{{ agent.task_type }} · {{ agent.binding_summary }}` 之后加：

```html
          <div v-if="agentHealth(agent.id)?.status === 'will_fail'" class="text-xs text-red-600 mt-1">
            ✕ {{ agentHealth(agent.id).reason }}（继承自绑定目标，不用改这个 Agent）
          </div>
```

在 `return {}` 里补 `agentHealth`。

- [ ] **Step 5: 运行测试并提交**

Run: `pytest tests/test_ai_model_ui.py -q`
Expected: PASS

```bash
git add src/pixiv_novel_sync/templates/dashboard_settings_models.html src/pixiv_novel_sync/templates/dashboard_settings_agents.html tests/test_ai_model_ui.py
git commit -m "feat: Provider 卡片与 Agent 行显示继承健康状态"
```

**阶段一在此完成。** 部署后横幅会立刻把生产那两个坏 Provider 与 15 个坏绑定说出来。

---

### Task 6: `test_provider` 解掉 `default_model` 死循环

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/admin.py:454-476`
- Test: `tests/test_ai_health.py`

**Interfaces:**
- Consumes: 无
- Produces: `test_provider` 行为变更——`default_model` 为空时回落到目录里第一个可路由模型；目录也空时抛可读引导

- [ ] **Step 1: 写失败测试**

```python
def test_test_provider_falls_back_to_first_routable_model(tmp_path, monkeypatch) -> None:
    """想验证一个新 Provider 通不通，不该被「先填默认模型」挡住。"""
    from pixiv_novel_sync.ai.service import AIServiceError

    app, db_path = _app(tmp_path, monkeypatch)
    db = Database(db_path)
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "p", "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1", "api_key_encrypted": None,
        "default_model": None, "enabled": 1,
    })
    db.close()

    res = app.test_client().post(
        f"/api/dashboard/ai/providers/{provider_id}/test",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    # 目录为空 ⇒ 不再是「未配置默认模型」，而是指向下一步动作
    assert res.status_code == 400
    assert "获取模型列表" in res.get_json()["error"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_ai_health.py -v -k falls_back`
Expected: FAIL，错误文案是「Provider 未配置默认模型」

- [ ] **Step 3: 写实现**

`admin.py:460` 那两行 `model = provider_config.default_model` / `if not model: raise ...` 换成：

```python
        model = provider_config.default_model
        if not model:
            # 死循环：测试要模型 → 模型要同步 → 同步要先保存。目录里已有可路由模型时
            # 直接借第一个来测，别把用户卡在「先去填默认模型」。
            catalog = self.list_provider_models(provider_id, routable_only=True)
            items = catalog.get("items") or []
            if items:
                model = items[0].get("model_key")
        if not model:
            raise AIServiceError(
                "这个 Provider 还没有可用模型：先点「获取模型列表」，或在高级设置里填默认模型"
            )
```

- [ ] **Step 4: 运行测试并提交**

Run: `pytest tests/test_ai_health.py -q`
Expected: PASS

```bash
git add tests/test_ai_health.py src/pixiv_novel_sync/ai/services/admin.py
git commit -m "fix: 测试 Provider 时回落到首个可路由模型，解掉默认模型死循环"
```

---

### Task 7: 探测端点（预览、不落库）+ 借 Key 约束

**Files:**
- Modify: `src/pixiv_novel_sync/ai/services/admin.py`
- Modify: `src/pixiv_novel_sync/ai_web.py`（加在 `start_ai_provider_model_sync` 之前）
- Test: `tests/test_ai_provider_probe.py`（新建）

**Interfaces:**
- Consumes: `ai/providers.py:create_provider(config)`、`AIProviderConfig`
- Produces:
  - `AIWritingService.probe_provider_models(payload: Mapping[str, Any]) -> dict[str, Any]`，返回 `{"latency_ms": int, "complete": bool, "partial_reason": str | None, "items": [{"model_key","display_name","capabilities","context_window","suggested": bool}]}`
  - `POST /api/dashboard/ai/providers/probe-models`，body `{provider_type, base_url, api_key?, provider_id?, timeout_seconds?}`
  - `suggested` 即 §4.4 的默认勾选：有 chat/text 能力标签或**能力标签缺失**时为 `true`；命中 embedding/rerank/asr 时为 `false`

- [ ] **Step 1: 写失败测试**

`tests/test_ai_provider_probe.py`：

```python
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.ai.service import AIWritingService, AIServiceError
from pixiv_novel_sync.storage_db import Database


class _FakeProvider:
    closed = False

    def list_models(self, **kwargs):
        return SimpleNamespace(
            models=[
                {"model_key": "glm-4.7", "display_name": "GLM 4.7",
                 "capabilities": ["chat"], "context_window": 200000},
                {"model_key": "bge-m3", "display_name": "BGE m3",
                 "capabilities": ["embedding"], "context_window": 8192},
                {"model_key": "mystery", "display_name": None,
                 "capabilities": [], "context_window": None},
            ],
            complete=True, partial_reason=None,
        )

    def close(self):
        type(self).closed = True


def test_probe_marks_chat_and_unlabelled_models_as_suggested(tmp_path, monkeypatch) -> None:
    """能力标签缺失时默认勾上：标签来自上游、不保证齐全，宁可多勾也别让用户以为没获取到。"""
    service = AIWritingService(db_path=tmp_path / "probe.db")
    monkeypatch.setattr(
        "pixiv_novel_sync.ai.services.admin.create_provider", lambda config: _FakeProvider()
    )

    result = service.probe_provider_models({
        "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test",
    })

    suggested = {item["model_key"]: item["suggested"] for item in result["items"]}
    assert suggested == {"glm-4.7": True, "bge-m3": False, "mystery": True}
    assert _FakeProvider.closed is True


def test_probe_refuses_to_borrow_stored_key_for_a_different_address(tmp_path) -> None:
    """否则这个端点就是「把加密存好的 Key 发到我指定的任意地址」。"""
    db = Database(tmp_path / "probe.db")
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "p", "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1", "api_key_encrypted": "cipher",
        "default_model": None, "enabled": 1,
    })
    db.close()
    service = AIWritingService(db_path=tmp_path / "probe.db")

    with pytest.raises(AIServiceError) as excinfo:
        service.probe_provider_models({
            "provider_id": provider_id,
            "provider_type": "openai_compatible",
            "base_url": "https://attacker.example.com/v1",
        })

    assert "不能同时改地址" in str(excinfo.value)


def test_probe_writes_nothing_to_the_catalog(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "probe.db")
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "p", "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1", "api_key_encrypted": "cipher",
        "default_model": None, "enabled": 1,
    })
    before = db.list_ai_provider_models(provider_id)["total"]
    db.close()
    service = AIWritingService(db_path=tmp_path / "probe.db")
    monkeypatch.setattr(
        "pixiv_novel_sync.ai.services.admin.create_provider", lambda config: _FakeProvider()
    )

    service.probe_provider_models({
        "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test",
    })

    db = Database(tmp_path / "probe.db")
    assert db.list_ai_provider_models(provider_id)["total"] == before
    assert db.get_ai_provider(provider_id)["models_synced_at"] is None
    db.close()
```

注：`AIWritingService` 的构造签名已核对为 `AIServiceCore.__init__(self, db_path: Path, secret_manager: AISecretManager | None = None)`（`ai/services/core.py:74`），所以 `AIWritingService(db_path=tmp_path / "probe.db")` 可直接用。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_ai_provider_probe.py -v`
Expected: FAIL，`AttributeError: ... has no attribute 'probe_provider_models'`

- [ ] **Step 3: 写实现**

`admin.py` 顶部导入区补 `from ..providers import create_provider`（`validate_base_url` / `ProviderConfigError` 在 T2 已加），以及 `from ..models import AIProviderConfig`（若已有则跳过）。

```python
_NON_CHAT_CAPABILITY_HINTS = ("embed", "rerank", "asr", "speech", "audio", "image", "vision")


    def probe_provider_models(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """用表单里的凭据拉一次模型列表用于预览。**不写任何库表。**

        与 model_sync 的分工：落库目录永远只有 model_sync 一个写入方，这里只回一份
        预览清单，让错的配置在落库前就被拦下（spec §4.2）。
        """
        base_url = (str(payload.get("base_url") or "")).strip() or None
        provider_type = str(payload.get("provider_type") or "openai_compatible")
        api_key = payload.get("api_key") or None
        provider_id = payload.get("provider_id")

        if provider_id is not None and not api_key:
            db = self._db()
            try:
                row = db.get_ai_provider(int(provider_id), include_secret=True)
            finally:
                db.close()
            if row is None:
                raise AINotFoundError("Provider 不存在")
            # 借库里那把 Key 时地址必须逐字相同：否则这个端点等于「把加密存好的 Key
            # 发到我指定的任意地址」，而 validate_base_url 拦不住它（目标可以是完全
            # 合法的 https 公网主机）。
            if (row.get("base_url") or None) != base_url:
                raise AIServiceError(
                    "借用已保存的 API Key 时不能同时改地址：请在表单里重新填入 Key"
                )
            api_key = self.secret_manager.decrypt(row.get("api_key_encrypted"))
        if not api_key:
            raise AIServiceError("请填入 API Key")

        config = AIProviderConfig(
            id=0,
            name="probe",
            provider_type=provider_type,
            base_url=base_url,
            api_key=api_key,
            default_model=None,
            timeout_seconds=int(payload.get("timeout_seconds") or 120),
            context_window=int(payload.get("context_window") or 128000),
        )
        provider = create_provider(config)
        started = time.time()
        try:
            result = provider.list_models(deadline=time.monotonic() + 60)
        finally:
            provider.close()

        items = []
        for model in result.models:
            capabilities = list(model.get("capabilities") or [])
            lowered = " ".join(capabilities).lower()
            suggested = not any(hint in lowered for hint in _NON_CHAT_CAPABILITY_HINTS)
            items.append({
                "model_key": model.get("model_key"),
                "display_name": model.get("display_name"),
                "capabilities": capabilities,
                "context_window": model.get("context_window"),
                "suggested": suggested,
            })
        return {
            "latency_ms": int((time.time() - started) * 1000),
            "complete": bool(result.complete),
            "partial_reason": result.partial_reason,
            "items": items,
        }
```

`create_provider` 必须以 `pixiv_novel_sync.ai.services.admin.create_provider` 的名字可被 monkeypatch，所以用 `from ..providers import create_provider` 而不是 `providers.create_provider(...)` 的属性访问。

- [ ] **Step 4: 注册路由**

```python
    @app.post("/api/dashboard/ai/providers/probe-models")
    def probe_ai_provider_models():
        """预览上游模型列表。不落库、不建 Provider（spec §4.2）。"""
        try:
            return ok(service.probe_provider_models(require_json_object()))
        except Exception as exc:
            return fail(exc)
```

- [ ] **Step 5: 运行测试并提交**

Run: `pytest tests/test_ai_provider_probe.py -v`
Expected: PASS（3 passed）

```bash
git add tests/test_ai_provider_probe.py src/pixiv_novel_sync/ai/services/admin.py src/pixiv_novel_sync/ai_web.py
git commit -m "feat: 新增只读模型探测端点，借用存量 Key 时锁定地址"
```

---

### Task 8: models 页表单收敛 + 探测勾选 + `enabled` 开关

**Files:**
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings_models.html:27-64`（Provider 表单）、`:99-112`（目录列表行）
- Test: `tests/test_ai_model_ui.py`

**Interfaces:**
- Consumes: T7 的 `POST /providers/probe-models`；既有 `PUT /api/dashboard/ai/provider-models/<id>`
- Produces: 无后端接口

- [ ] **Step 1: 写失败测试**

```python
def test_provider_form_collapses_advanced_fields_and_probes_before_save():
    for text in ("probe-models", "probedModels", "toggleProbedModel", "advancedOpen", "从已有 Provider 复制"):
        assert text in TEMPLATE
    # /v1 建议是一键按钮，不是静默改写
    assert "试试" in TEMPLATE and "/v1" in TEMPLATE


def test_model_catalog_rows_can_toggle_enabled():
    """enabled 决定 routable，后端早就支持改它，之前前端完全没接线。"""
    assert "/api/dashboard/ai/provider-models/" in TEMPLATE
    assert "toggleModelEnabled" in TEMPLATE
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_ai_model_ui.py -v -k "collapses_advanced or toggle_enabled"`
Expected: FAIL

- [ ] **Step 3: 表单收敛**

把 `providerForm` 的 `default_model` / `context_window` / `timeout_seconds` / `max_retries` / `stream_enabled` 五个输入包进：

```html
      <details :open="advancedOpen" @toggle="advancedOpen = $event.target.open">
        <summary class="text-xs text-pixiv-gray cursor-pointer">高级（默认模型 / 超时 / 重试 / 上下文窗口 / 流式）</summary>
        <div class="pt-3 space-y-3">
          <!-- 原来那五个输入框整体搬进来，一个字不改 -->
        </div>
      </details>
```

地址框加失焦体检与名称预填：

```html
      <input v-model="providerForm.base_url" @blur="lintBaseUrl"
             class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm"
             placeholder="base_url，可留空使用默认值">
      <p v-if="baseUrlHint" class="text-xs text-red-600">{{ baseUrlHint }}</p>
```

```javascript
      const advancedOpen = ref(false);
      const baseUrlHint = ref('');
      function lintBaseUrl() {
        const url = (providerForm.value.base_url || '').trim();
        baseUrlHint.value = '';
        if (!url) return;
        // 与运行时同一条规则：非回环主机必须 https（ai/providers.py:validate_base_url）
        const loopback = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:|\/|$)/i.test(url);
        if (url.startsWith('http://') && !loopback) {
          baseUrlHint.value = 'base_url 必须使用 https（本机回环地址除外）——保存后所有调用都会失败';
        }
        if (!providerForm.value.name) {
          try { providerForm.value.name = new URL(url).host; } catch (e) {}
        }
      }
      function copyFromProvider(provider) {
        providerForm.value = { ...provider, id: null, api_key: '' };
        baseUrlHint.value = '';
      }
```

前端这条 lint 是**提示**，落库时以后端为准（后端那条才是唯一判据）。

上游格式下拉每项补一句人话（spec §4.1 第 3 条）：

```html
      <select v-model="providerForm.provider_type" class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm">
        <option value="openai_compatible">OpenAI 兼容 —— 填到 /v1 为止，或网关根地址</option>
        <option value="anthropic">Anthropic —— 官方 /v1/messages 或同协议网关</option>
        <option value="xai">xAI —— 官方 https://api.x.ai/v1</option>
      </select>
```

「从已有 Provider 复制」放在右侧列表每张卡片的按钮组里（与「编辑」并列）：

```html
            <button @click="copyFromProvider(provider)" class="text-xs px-2 py-1 rounded bg-gray-100 hover:bg-gray-200">复制一份</button>
```

- [ ] **Step 4: 探测与勾选**

```javascript
      const probedModels = ref([]);
      const probeStatus = ref('');
      const probeRetryWithV1 = ref('');
      async function probeModels() {
        probeStatus.value = '探测中…';
        probedModels.value = [];
        probeRetryWithV1.value = '';
        try {
          const data = await window.aiApi.request('/api/dashboard/ai/providers/probe-models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              provider_id: providerForm.value.id || undefined,
              provider_type: providerForm.value.provider_type,
              base_url: providerForm.value.base_url,
              api_key: providerForm.value.api_key || undefined,
            }),
          });
          probedModels.value = data.items.map(item => ({ ...item, checked: item.suggested }));
          probeStatus.value = `通，${data.latency_ms}ms，找到 ${data.items.length} 个`;
        } catch (e) {
          probeStatus.value = window.errorText ? e.message : String(e);
          const url = (providerForm.value.base_url || '').replace(/\/+$/, '');
          if (url && !/\/v1$/.test(url) && /404|not found|路径/i.test(e.message || '')) {
            probeRetryWithV1.value = url + '/v1';
          }
        }
      }
      function toggleProbedModel(model) { model.checked = !model.checked; }
      function applyV1Suggestion() {
        providerForm.value.base_url = probeRetryWithV1.value;
        probeRetryWithV1.value = '';
        probeModels();
      }
```

`saveProvider` 成功后，若 `probedModels` 有勾选项，逐个 `POST /providers/<id>/models` 写入（`model_key` + `manual_display_name` + `manual_capabilities` + `manual_context_window`），并把第一个勾选项 `PUT /providers/<id>` 写成 `default_model`。一个都没勾也允许保存（spec §4.2）。

- [ ] **Step 5: `enabled` 开关**

目录行右侧加：

```html
                    <label class="flex items-center gap-1">
                      <input type="checkbox" :checked="model.enabled"
                             @change="toggleModelEnabled(provider.id, model)">启用
                    </label>
```

```javascript
      async function toggleModelEnabled(providerId, model) {
        try {
          await window.aiApi.request(`/api/dashboard/ai/provider-models/${model.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: !model.enabled }),
          });
          await loadProviderModels(providerId);
          await loadAiHealth();
        } catch (e) { showAiMessage(e.message, 'error'); }
      }
```

在 `return {}` 里补 `advancedOpen, baseUrlHint, lintBaseUrl, copyFromProvider, probedModels, probeStatus, probeRetryWithV1, probeModels, toggleProbedModel, applyV1Suggestion, toggleModelEnabled`。

- [ ] **Step 6: 运行测试并提交**

Run: `pytest tests/test_ai_model_ui.py -q`
Expected: PASS

```bash
git add src/pixiv_novel_sync/templates/dashboard_settings_models.html tests/test_ai_model_ui.py
git commit -m "feat: Provider 表单收敛并支持保存前探测勾选模型"
```

**阶段二在此完成。**

---

### Task 9: 批量改绑端点（单事务 + 版本递增 + 成人拒绝）

**Files:**
- Modify: `src/pixiv_novel_sync/storage/ai/core.py`（`update_ai_agent` 之后）
- Modify: `src/pixiv_novel_sync/ai/services/admin.py`
- Modify: `src/pixiv_novel_sync/ai_web.py`（加在 `@app.delete("/api/dashboard/ai/agents/<int:agent_id>")` 之后）
- Test: `tests/test_ai_agent_batch_bindings.py`（新建）

**Interfaces:**
- Consumes: `ADULT_AI_TASK_TYPES`（`storage/ai/core.py` 已导出，`storage/tasks.py` 已在用）
- Produces:
  - `Database.update_ai_agent_bindings(agent_ids: list[int], binding: dict[str, Any]) -> int`，单事务，返回受影响行数，每行 `binding_version = binding_version + 1`
  - `AIWritingService.update_agent_bindings(payload: Mapping[str, Any]) -> dict[str, Any]`，返回 `{"updated": int}`
  - `PUT /api/dashboard/ai/agents/bindings`，body `{"agent_ids": [int], "binding": {"binding_type": "fixed"|"pool", "provider_id": int|None, "model": str|None, "model_pool_id": int|None}}`；另接受 `{"agent_ids": [...], "enabled": bool}` 做批量启停

- [ ] **Step 1: 写失败测试**

`tests/test_ai_agent_batch_bindings.py`：

```python
from __future__ import annotations

import pytest

from pixiv_novel_sync.storage_db import Database


def _agent(db: Database, name: str, task_type: str = "continue", provider_id: int = 1) -> int:
    return db.create_ai_agent({
        "name": name, "task_type": task_type, "binding_type": "fixed",
        "provider_id": provider_id, "model": "old-model", "system_prompt": "p",
        "temperature": 0.7, "max_tokens": 3000, "enabled": 1,
    })


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "batch.db")
    database.init_schema()
    yield database
    database.close()


def test_batch_rebind_bumps_binding_version_for_every_row(db: Database) -> None:
    """binding_version 必须涨：候选快照按 agent_config_hash 缓存，不涨版本在跑的 job 会继续用旧候选。"""
    ids = [_agent(db, f"a{i}") for i in range(3)]
    before = {a["id"]: a["binding_version"] for a in db.list_ai_agents()}

    updated = db.update_ai_agent_bindings(ids, {
        "binding_type": "fixed", "provider_id": 9, "model": "glm-4.7", "model_pool_id": None,
    })

    assert updated == 3
    for agent in db.list_ai_agents():
        assert agent["provider_id"] == 9
        assert agent["model"] == "glm-4.7"
        assert agent["binding_version"] == before[agent["id"]] + 1


def test_batch_rebind_is_atomic(db: Database, monkeypatch) -> None:
    """中途失败不能留下改了一半的状态——那正是「说不清现在到底在用什么」的成因。"""
    ids = [_agent(db, f"a{i}") for i in range(3)]
    original = db.conn.execute
    calls = {"n": 0}

    def flaky(sql, *args, **kwargs):
        if "UPDATE ai_agents" in str(sql):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("boom")
        return original(sql, *args, **kwargs)

    monkeypatch.setattr(db.conn, "execute", flaky)
    with pytest.raises(RuntimeError):
        db.update_ai_agent_bindings(ids, {
            "binding_type": "fixed", "provider_id": 9, "model": "glm-4.7", "model_pool_id": None,
        })
    monkeypatch.undo()

    for agent in db.list_ai_agents():
        assert agent["provider_id"] == 1
        assert agent["model"] == "old-model"


def test_batch_rebind_rejects_adult_agents_outright(db: Database) -> None:
    """fail-closed：混入成人 Agent 要整体拒绝，不是静默跳过。"""
    from pixiv_novel_sync.ai.service import AIServiceError, AIWritingService

    normal = _agent(db, "普通")
    adult = _agent(db, "成人润色", task_type="adult_polish")
    db.close()
    service = AIWritingService(db_path=db.db_path)

    with pytest.raises(AIServiceError) as excinfo:
        service.update_agent_bindings({
            "agent_ids": [normal, adult],
            "binding": {"binding_type": "fixed", "provider_id": 9, "model": "m"},
        })

    assert "成人" in str(excinfo.value)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_ai_agent_batch_bindings.py -v`
Expected: FAIL，`AttributeError: ... 'update_ai_agent_bindings'`

- [ ] **Step 3: 写存储方法**

`storage/ai/core.py`，`update_ai_agent` 之后：

```python
    def update_ai_agent_bindings(
        self, agent_ids: list[int], binding: dict[str, Any]
    ) -> int:
        """一个事务里改完一批 Agent 的绑定。

        不复用 N 次 update_ai_agent：中途失败会留下改了一半的状态。只动绑定字段，
        system_prompt / 采样参数 / required_capabilities 一律不碰——那些是每个 Agent
        的个性，批量覆盖会把十几个精调过的提示词一次抹平。
        """
        if not agent_ids:
            return 0
        provider_id = binding.get("provider_id")
        model_pool_id = binding.get("model_pool_id")
        affected = 0
        with self._lock, self.transaction():
            for agent_id in agent_ids:
                cursor = self.conn.execute(
                    """
                    UPDATE ai_agents
                    SET binding_type = ?, provider_id = ?, model = ?, model_pool_id = ?,
                        binding_version = binding_version + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        binding.get("binding_type") or "fixed",
                        int(provider_id) if provider_id is not None else None,
                        binding.get("model"),
                        int(model_pool_id) if model_pool_id is not None else None,
                        int(agent_id),
                    ),
                )
                affected += cursor.rowcount
        return affected

    def set_ai_agents_enabled(self, agent_ids: list[int], enabled: bool) -> int:
        """批量启停。同样单事务，但不动 binding_version——绑定没变。"""
        if not agent_ids:
            return 0
        affected = 0
        with self._lock, self.transaction():
            for agent_id in agent_ids:
                cursor = self.conn.execute(
                    "UPDATE ai_agents SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (1 if enabled else 0, int(agent_id)),
                )
                affected += cursor.rowcount
        return affected
```

若 `ai_agents` 没有 `updated_at` 列，去掉那一句。（已核对 `storage/ai/model_schema.py:163`：**有** `updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP`，照写即可。）

**同一张表上还有一条 CHECK 约束必须照顾到**（`model_schema.py:164-169`）：

```sql
CHECK ((binding_type = 'fixed' AND provider_id IS NOT NULL AND model_pool_id IS NULL)
    OR (binding_type = 'pool'  AND model_pool_id IS NOT NULL AND provider_id IS NULL))
```

也就是说两个绑定字段是**互斥**的：改成 `fixed` 必须同时把 `model_pool_id` 置 `NULL`，改成 `pool` 必须同时把 `provider_id` 置 `NULL`，否则整条 UPDATE 会被 SQLite 拒掉。归一化放在服务层（见 Step 4），存储层只忠实写入。

- [ ] **Step 4: 写服务方法**

`admin.py`：

```python
    def update_agent_bindings(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """批量改绑 / 批量启停。成人 Agent 混入即整体拒绝（fail-closed）。"""
        raw_ids = payload.get("agent_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise AIServiceError("agent_ids 必须是非空数组")
        agent_ids = [int(value) for value in raw_ids]

        db = self._db()
        try:
            by_id = {int(a["id"]): a for a in db.list_ai_agents()}
            missing = [i for i in agent_ids if i not in by_id]
            if missing:
                raise AINotFoundError(f"Agent 不存在：{missing}")
            adult = [
                by_id[i]["name"] for i in agent_ids
                if by_id[i].get("task_type") in ADULT_AI_TASK_TYPES
            ]
            if adult:
                # 成人 Agent 有独立的生命周期入口与 fail-closed 契约（policy hash /
                # review binding / 角色 revision），混进通用批量等于给安全边界开后门。
                raise AIServiceError(
                    f"成人润色 Agent 不参与批量操作，请单独配置：{'、'.join(adult)}"
                )
            if "enabled" in payload:
                self._require_boolean(payload, "enabled")
                updated = db.set_ai_agents_enabled(agent_ids, bool(payload["enabled"]))
            else:
                binding = payload.get("binding")
                if not isinstance(binding, dict):
                    raise AIServiceError("binding 必须是对象")
                binding_type = binding.get("binding_type") or "fixed"
                if binding_type not in ("fixed", "pool"):
                    raise AIServiceError("binding_type 只能是 fixed 或 pool")
                if binding_type == "fixed" and not binding.get("provider_id"):
                    raise AIServiceError("固定绑定必须指定 provider_id")
                if binding_type == "pool" and not binding.get("model_pool_id"):
                    raise AIServiceError("池绑定必须指定 model_pool_id")
                # ai_agents 上那条 CHECK 要求两个绑定字段互斥（model_schema.py:164），
                # 不归一化就会被 SQLite 整条拒掉。
                normalized = {
                    "binding_type": binding_type,
                    "model": binding.get("model"),
                    "provider_id": binding.get("provider_id") if binding_type == "fixed" else None,
                    "model_pool_id": binding.get("model_pool_id") if binding_type == "pool" else None,
                }
                updated = db.update_ai_agent_bindings(agent_ids, normalized)
        finally:
            db.close()
        return {"updated": int(updated)}
```

`admin.py` 顶部导入 `from ...storage.ai.core import ADULT_AI_TASK_TYPES`（以仓库实际导出位置为准：`storage/tasks.py` 用的是 `from .ai.core import ADULT_AI_TASK_TYPES`）。

- [ ] **Step 5: 注册路由**

```python
    @app.put("/api/dashboard/ai/agents/bindings")
    def update_ai_agent_bindings_route():
        """批量改绑 / 批量启停。单事务，成人 Agent 混入即整体拒绝。"""
        try:
            return ok(service.update_agent_bindings(require_json_object()))
        except Exception as exc:
            return fail(exc)
```

**注意路由顺序**：这条必须注册在 `@app.put("/api/dashboard/ai/agents/<int:agent_id>")` **之后**也无妨——Flask 按规则精确匹配，`bindings` 不是 int，不会撞车。

- [ ] **Step 6: 运行测试并提交**

Run: `pytest tests/test_ai_agent_batch_bindings.py -v`
Expected: PASS（3 passed）

```bash
git add tests/test_ai_agent_batch_bindings.py src/pixiv_novel_sync/storage/ai/core.py src/pixiv_novel_sync/ai/services/admin.py src/pixiv_novel_sync/ai_web.py
git commit -m "feat: Agent 批量改绑端点，单事务且成人 Agent 整体拒绝"
```

---

### Task 10: agents 页多选 / 搜索 / 批量 UI

**Files:**
- Modify: `src/pixiv_novel_sync/templates/dashboard_settings_agents.html:95-127`
- Test: `tests/test_ai_model_ui.py`

**Interfaces:**
- Consumes: T9 的 `PUT /api/dashboard/ai/agents/bindings`；T5 的 `agentHealth`
- Produces: 无后端接口

- [ ] **Step 1: 写失败测试**

```python
def test_agents_page_supports_search_multiselect_and_batch_rebind():
    for text in (
        "agentSearch", "selectedAgentIds", "toggleAgentSelection", "selectAllAgents",
        "batchRebind", "batchSetEnabled", "/api/dashboard/ai/agents/bindings",
        "onlyBadBindings", "成人润色 Agent 不参与批量",
    ):
        assert text in AGENTS_TEMPLATE
    # 确认弹窗必须逐项列出老值 → 新值
    assert "batchPreview" in AGENTS_TEMPLATE
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_ai_model_ui.py -v -k multiselect`
Expected: FAIL

- [ ] **Step 3: 写实现**

```javascript
      const agentSearch = ref('');
      const onlyBadBindings = ref(false);
      const selectedAgentIds = ref([]);
      const batchPreview = ref(null);

      const batchEligibleAgents = computed(() => filteredAgents.value.filter(
        agent => !String(agent.task_type || '').startsWith('adult_')
      ));
      function toggleAgentSelection(agentId) {
        const index = selectedAgentIds.value.indexOf(agentId);
        if (index >= 0) selectedAgentIds.value.splice(index, 1);
        else selectedAgentIds.value.push(agentId);
      }
      function selectAllAgents() {
        selectedAgentIds.value = selectedAgentIds.value.length
          ? [] : batchEligibleAgents.value.map(agent => agent.id);
      }
      function openBatchRebind(binding) {
        batchPreview.value = {
          binding,
          rows: selectedAgentIds.value.map(id => {
            const agent = agents.value.find(item => item.id === id);
            return { name: agent.name, from: agent.binding_summary, to: batchBindingSummary(binding) };
          }),
        };
      }
      async function batchRebind() {
        try {
          const data = await window.aiApi.request('/api/dashboard/ai/agents/bindings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agent_ids: selectedAgentIds.value, binding: batchPreview.value.binding }),
          });
          batchPreview.value = null;
          selectedAgentIds.value = [];
          await loadAgents();
          await loadAiHealth();
          showAiMessage(`已改绑 ${data.updated} 个 Agent`);
        } catch (e) { showAiMessage(e.message, 'error'); }
      }
      async function batchSetEnabled(enabled) {
        try {
          await window.aiApi.request('/api/dashboard/ai/agents/bindings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agent_ids: selectedAgentIds.value, enabled }),
          });
          selectedAgentIds.value = [];
          await loadAgents();
        } catch (e) { showAiMessage(e.message, 'error'); }
      }
```

`filteredAgents` 的过滤条件扩到同时吃 `agentSearch`（匹配 `name` / `task_type`）与 `onlyBadBindings`（`agentHealth(agent.id)?.status === 'will_fail'`）。

列表上方的工具条与行内勾选框：

```html
      <div class="flex items-center gap-2 flex-wrap mb-2">
        <input v-model="agentSearch" placeholder="搜索 Agent 名称 / 类型"
               class="min-w-0 flex-1 px-3 py-1.5 border border-gray-200 rounded-lg text-sm">
        <label class="flex items-center gap-1 text-xs text-gray-700">
          <input type="checkbox" v-model="onlyBadBindings">只看坏绑定
        </label>
        <button @click="selectAllAgents" class="text-xs px-2 py-1 rounded bg-gray-100 hover:bg-gray-200">
          {{ selectedAgentIds.length ? '取消全选' : '全选（' + batchEligibleAgents.length + '）' }}
        </button>
      </div>
      <div v-if="selectedAgentIds.length" class="flex items-center gap-2 flex-wrap mb-2 text-xs">
        <span class="text-gray-700">已选 {{ selectedAgentIds.length }} 个</span>
        <button @click="openBatchRebind({ binding_type: agentForm.binding_type, provider_id: agentForm.provider_id, model: agentForm.model, model_pool_id: agentForm.model_pool_id })"
                class="px-2 py-1 rounded bg-brand-500 text-white">批量改绑到左侧当前绑定…</button>
        <button @click="batchSetEnabled(true)" class="px-2 py-1 rounded bg-gray-100">批量启用</button>
        <button @click="batchSetEnabled(false)" class="px-2 py-1 rounded bg-gray-100">批量停用</button>
      </div>
```

每个 Agent 行最前面插入：

```html
          <label class="flex items-start gap-2">
            <input type="checkbox" :checked="selectedAgentIds.includes(agent.id)"
                   :disabled="String(agent.task_type || '').startsWith('adult_')"
                   @change="toggleAgentSelection(agent.id)" class="mt-1">
            <span v-if="String(agent.task_type || '').startsWith('adult_')" class="text-[10px] text-pixiv-light">
              成人润色 Agent 不参与批量，请单独配置
            </span>
          </label>
```

`batchPreview` 用既有的 `app-modal` 组件渲染逐项差异：

```html
  <app-modal v-if="batchPreview" title="确认批量改绑" @close="batchPreview = null">
    <div class="space-y-1 text-xs max-h-64 overflow-y-auto">
      <div v-for="row in batchPreview.rows" :key="row.name" class="flex justify-between gap-2">
        <span class="truncate">{{ row.name }}</span>
        <span class="text-gray-500">{{ row.from }} → <span class="text-brand-600">{{ row.to }}</span></span>
      </div>
    </div>
    <div class="flex gap-2 pt-3">
      <button @click="batchRebind" class="px-4 py-2 bg-brand-500 text-white rounded-lg text-sm">应用</button>
      <button @click="batchPreview = null" class="px-4 py-2 bg-gray-100 rounded-lg text-sm">取消</button>
    </div>
  </app-modal>
```

`batchBindingSummary(binding)` 自己写一个小函数：`fixed` 时返回 `providerName(binding.provider_id) + ' / ' + (binding.model || '默认模型')`，`pool` 时返回池名 + `（模型池）`。

在 `return {}` 里补上全部新名字：`agentSearch, onlyBadBindings, selectedAgentIds, batchPreview, batchEligibleAgents, toggleAgentSelection, selectAllAgents, openBatchRebind, batchRebind, batchSetEnabled, batchBindingSummary`。

- [ ] **Step 4: 运行测试并提交**

Run: `pytest tests/test_ai_model_ui.py -q && python -m compileall -q src`
Expected: PASS

```bash
git add src/pixiv_novel_sync/templates/dashboard_settings_agents.html tests/test_ai_model_ui.py
git commit -m "feat: Agent 列表支持搜索、多选与批量改绑"
```

**阶段三在此完成。**

---

## 收尾（每个阶段结束时都做一次）

- [ ] 全量测试：`pytest -q`，期望 ≥ 1446 passed / 4 skipped（基线来自 2026-09-03）
- [ ] `python -m compileall -q src`
- [ ] 浏览器验证（用 `.claude/launch.json` 的 `web-preview`，它连隔离的 scratch 库、`auto_sync_enabled: false`）：三页渲染、横幅在坏配置下变红、探测勾选、批量确认弹窗逐项列出差异、零 console error
- [ ] 文档：`docs/frontend-pages.md` 的设置页小节补横幅与三个新端点；`docs/frontend-api-contract.md` 路由表补 `/api/dashboard/ai/health`、`/providers/probe-models`、`/agents/bindings`；`CLAUDE.md` 的 Frontend 段补一句「AI 设置三页共享 `dashboard_ai_health_band.html`，健康是零网络投影」














